"""Module helpers shared by the Shop Foreman and its anomaly mixin.

Split out of :mod:`foundry.shop_foreman`, which imports every name here back.
"""

from __future__ import annotations

import contextvars
import inspect
import json
import math
import re
from collections import deque
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel

from foundry.schemas import TaskState

#: Hooks that called Gemma in the current asyncio task since their last
#: ``last_source`` write (immutable, so concurrent tasks never share it).
_ASKED: contextvars.ContextVar[frozenset[str]] = contextvars.ContextVar("foreman_gemma_asked", default=frozenset())

_TERMINAL = {TaskState.COMPLETE.value, TaskState.QUARANTINED.value, TaskState.FAILED.value}
_ACTIVE_TICKET = ("open", "claimed")
_LARGE_WORDS = ("rewrite", "overhaul", "migrate", "migration", "end-to-end", "end to end", "redesign")
_UNKNOWN_TASK = "unknown"


# --------------------------------------------------------------------------- helpers


class GemmaStats:
    """Small rolling record of Gemma calls and hook fallbacks.

    * ``calls`` -- Gemma requests attempted (successful or not).
    * ``errors`` / ``timeouts`` -- requests that failed (timeouts included in errors).
    * ``fallbacks`` -- hook results produced by the deterministic fallback
      after Gemma was asked (it failed, timed out or gave an unusable reply).
      Hook runs that never asked Gemma (no client, or a hook such as
      ``prune_context_slice`` whose Gemma use lives elsewhere) are not counted,
      so ``fallbacks`` never exceeds ``calls``.
    * ``avg_ms`` / ``last_ms`` -- duration of the last :attr:`window` requests
      (failed ones included: a timeout is time the user waited).
    """

    def __init__(self, window: int = 20) -> None:
        self.window = int(window)
        self._total = self._new()
        self._hooks: dict[str, dict[str, Any]] = {}
        self.last_error: str | None = None

    def _new(self) -> dict[str, Any]:
        return {"calls": 0, "errors": 0, "timeouts": 0, "fallbacks": 0, "gemma": 0,
                "last_ms": None, "durations": deque(maxlen=self.window)}

    def _targets(self, hook: str) -> tuple[dict[str, Any], dict[str, Any]]:
        return self._total, self._hooks.setdefault(hook, self._new())

    def record_call(self, hook: str, ms: float, *, error: str | None = None, timed_out: bool = False) -> None:
        """Record one Gemma request that took ``ms`` milliseconds."""
        ms = round(float(ms), 1)
        for rec in self._targets(hook):
            rec["calls"] += 1
            rec["last_ms"] = ms
            rec["durations"].append(ms)
            if error is not None:
                rec["errors"] += 1
                if timed_out:
                    rec["timeouts"] += 1
        if error is not None:
            self.last_error = error

    def record_source(self, hook: str, source: str) -> None:
        """Record which path served a hook result (``"gemma"`` or ``"heuristic"``)."""
        key = "gemma" if source == "gemma" else "fallbacks"
        for rec in self._targets(hook):
            rec[key] += 1

    @staticmethod
    def _view(rec: dict[str, Any]) -> dict[str, Any]:
        durations = rec["durations"]
        return {
            "calls": rec["calls"],
            "errors": rec["errors"],
            "timeouts": rec["timeouts"],
            "fallbacks": rec["fallbacks"],
            "served_by_gemma": rec["gemma"],
            "avg_ms": round(sum(durations) / len(durations), 1) if durations else None,
            "last_ms": rec["last_ms"],
        }

    def snapshot(self) -> dict[str, Any]:
        """JSON-ready ``{calls, errors, timeouts, fallbacks, served_by_gemma, avg_ms, last_ms, last_error, by_hook}``."""
        out = self._view(self._total)
        out["last_error"] = self.last_error
        out["by_hook"] = {hook: self._view(rec) for hook, rec in sorted(self._hooks.items())}
        return out


class _SourceLog(dict):
    """``last_source`` mapping that also feeds :class:`GemmaStats`.

    A write is counted only when the hook asked Gemma (via ``ShopForeman._chat``)
    in the current asyncio task since its previous write.
    """

    def __init__(self, stats: GemmaStats) -> None:
        super().__init__()
        self._stats = stats

    def __setitem__(self, hook: str, source: str) -> None:
        super().__setitem__(hook, source)
        asked = _ASKED.get()
        if hook in asked:
            _ASKED.set(asked - {hook})
            self._stats.record_source(hook, source)


def _accepts_timeout(chat: Any) -> bool:
    """True when a client's ``chat`` takes a ``timeout`` keyword."""
    try:
        params = inspect.signature(chat).parameters
    except (TypeError, ValueError):
        return False
    return "timeout" in params or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


def _jsonable(value: Any) -> Any:
    """Convert enums, models and datetimes so ``json.dumps`` accepts the value."""
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    return value


def _field(task: Any, name: str, default: Any = None) -> Any:
    """Read ``name`` from a mapping or an object."""
    if isinstance(task, Mapping):
        return task.get(name, default)
    return getattr(task, name, default)


def _str_list(value: Any) -> list[str]:
    """Coerce a value to a list of non-blank strings."""
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    return [str(v).strip() for v in value if str(v).strip()]


def _parse_ts(value: str | None) -> datetime | None:
    """Parse an ISO timestamp (naive values are taken to be UTC)."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _clip(text: str, limit: int) -> str:
    """Shorten ``text`` to ``limit`` characters with an ellipsis."""
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def _estimate_tokens(text: str) -> int:
    """``ceil(len(text) / 4)``, the factory-wide token estimate."""
    return math.ceil(len(text) / 4) if text else 0


def _reply_tokens(reply: Any, messages: Sequence[Mapping[str, Any]]) -> int:
    """Tokens a Gemma call handled (reported usage, else an estimate)."""
    usage = getattr(reply, "usage", None)
    total = int(getattr(usage, "prompt_tokens", 0) or 0) + int(getattr(usage, "completion_tokens", 0) or 0)
    if total > 0:
        return total
    prompt = "".join(str(m.get("content") or "") for m in messages)
    return _estimate_tokens(prompt) + _estimate_tokens(str(getattr(reply, "text", "") or ""))


def _decode_json(text: str) -> Any:
    """Decode JSON from model text, tolerating a Markdown code fence."""
    stripped = (text or "").strip()
    fence = re.match(r"^```(?:json)?\s*\n?(.*?)\n?```\s*$", stripped, re.DOTALL)
    if fence:
        stripped = fence.group(1).strip()
    return json.loads(stripped)


def heuristic_complexity(
    title: str, description: str, files: Sequence[str], criteria: Sequence[str]
) -> tuple[str, str]:
    """Deterministic complexity guess from text length, file and criteria counts.

    Returns ``(complexity, rationale)``. The rules, in order:

    * XL: 8+ files, or 2500+ characters of description;
    * L: 4+ files, 1000+ characters, 6+ criteria, or a large-work keyword
      (rewrite, overhaul, migrate, redesign, end-to-end);
    * M: 2+ files, 300+ characters or 3+ criteria;
    * S: anything smaller.
    """
    text = f"{title}\n{description}".strip()
    n_chars, n_files, n_crit = len(text), len(files), len(criteria)
    lowered = text.lower()
    keyword = next((w for w in _LARGE_WORDS if w in lowered), None)
    facts = f"{n_files} file(s), {n_chars} chars of description, {n_crit} criteria"
    if n_files >= 8 or n_chars >= 2500:
        return "XL", f"Heuristic: {facts}; very broad scope."
    if n_files >= 4 or n_chars >= 1000 or n_crit >= 6 or keyword:
        extra = f"; keyword '{keyword}'" if keyword else ""
        return "L", f"Heuristic: {facts}{extra}; needs the full team."
    if n_files >= 2 or n_chars >= 300 or n_crit >= 3:
        return "M", f"Heuristic: {facts}; two builders."
    return "S", f"Heuristic: {facts}; one builder."


def _first_sentences(text: str, n: int = 2) -> str:
    """Keep the first ``n`` sentences of ``text`` (never empty)."""
    parts = re.split(r"(?<=[.!?])\s+", " ".join((text or "").split()))
    kept = " ".join(parts[:n]).strip()
    return kept or "Complete the subtask."
