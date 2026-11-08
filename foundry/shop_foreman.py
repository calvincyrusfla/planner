"""The Shop Foreman: a local Gemma model that runs the factory floor.

The Shop Foreman sits between the Floor Manager (Claude, planning tier) and
the builders/reviewers (Claude). It takes cheap, local decisions so Claude
tokens are spent only on real building and reviewing:

* :meth:`ShopForeman.size_task_scope` -- sizes a work order (S/M/L/XL) into a
  validated :class:`~foundry.schemas.TaskSizingDirective`.
* :meth:`ShopForeman.prune_context_slice` -- builds the minimal context slice
  for one subtask (delegates to :class:`~foundry.context_engine.ContextEngine`).
* :meth:`ShopForeman.condense_error_trace` -- one line (<= 160 chars) fix hint
  from a failing gate's output.
* :meth:`ShopForeman.draft_pr_summary` -- conventional commit message and PR
  body for a finished task.
* :meth:`ShopForeman.pre_review_diff` -- quick Gemma look at a diff that passed
  the gates, before the Claude reviewer; flags only obvious, objective
  problems (a :class:`PreReview`; falls back to "pass", never blocks).
* :meth:`ShopForeman.answer_question` -- streams an answer about the line,
  using the read-only tools below in a bounded tool-calling loop.
* :meth:`ShopForeman.evaluate_line_anomaly` -- deterministic anomaly
  detection that yields a validated :class:`~foundry.schemas.ForemanEscalation`.

**Gemma is always optional.** Every hook validates what Gemma returns and falls
back to a deterministic heuristic when Gemma is absent (``gemma=None``),
unreachable or returns something invalid. Anomaly *detection* is always
deterministic; Gemma only writes the escalation's ``diagnosis``.

Read-only tools (exported as OpenAI tool definitions in :data:`FOREMAN_TOOLS`)
only read the SQLite queue and telemetry:

``get_queue_summary()``, ``get_token_velocity(window_minutes=15)``,
``inspect_task_failure(task_id)``, ``get_team_bottlenecks()``.

Every Gemma-served hook records the Claude tokens it saved through
``Telemetry.record_tokens_saved`` (sizing/condensing/Q&A as ``"triage"``,
PR drafting as ``"release_draft"``, context slices as ``"pruning"``).

``action_parameters`` produced by :meth:`ShopForeman.evaluate_line_anomaly`
(as consumed by the Floor Manager):

==========================  ================================================
Action                      Parameters
==========================  ================================================
QUARANTINE_TASK             ``reason``, ``gate_failure_count``, ``ticket_id``
THROTTLE_DISPATCH           ``factor`` (0..1), ``seconds``
RESET_BUILDER_CONTEXT       ``agent_id``, ``ticket_id``, ``slot``
PROMOTE_PEER_REVIEWER       ``minutes``; ``slot`` when one is free; ``ticket_id``
                            of the waiting submission when known
RESIZE_ALLOCATION           ``builders`` (new allocation), ``from``
REASSIGN_TEAM               ``from_team``, ``to_team``, ``ticket_ids`` (the
                            task's open tickets stuck behind busy slots),
                            ``waited_s``
RETRY_TASK                  ``reason`` (the quarantine reason), ``transient``
                            (True), ``quarantined_s``, ``source``
                            (``heuristic`` or ``gemma``)
==========================  ================================================

REASSIGN_TEAM is raised when a task's open ticket has waited longer than
``stall_s`` on a team with no free builder slot while another team has no
active ticket at all. RETRY_TASK is raised for a quarantined task whose
reason looks transient (infrastructure, timeouts, rate limits); a reason that
is clearly a real build failure (retry limit, gate failures, reviewer
rejection, duplicate) is never retried, and an unclear reason is retried only
when Gemma says so. The Foreman never prescribes RETRY_TASK twice for one task
(it checks its own memory, the stored escalations and the manager's action
log). For these two actions Gemma may also answer ``NONE`` (leave it), in
which case no escalation is raised for that anomaly.

All public hooks are ``async`` (Gemma calls are awaited);
:meth:`ShopForeman.detect_anomalies` and the four tools are synchronous.
"""

from __future__ import annotations

import json
import logging
import asyncio
import contextvars
import inspect
import math
import re
import time
from collections import deque
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ValidationError

from foundry.config import FoundryConfig
from foundry.schemas import (
    BUILDERS_FOR,
    MAX_BUILDERS,
    MAX_DIAGNOSIS_SENTENCES,
    ContextSlice,
    EscalationCategory,
    ForemanEscalation,
    PrescribedAction,
    Severity,
    SubtaskAllocation,
    TaskSizingDirective,
    TaskState,
    count_sentences,
    new_alert_id,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from foundry.context_engine import ContextEngine
    from foundry.gemma_client import GemmaClient, GemmaReply
    from foundry.orchestrator_queue import FoundryQueue
    from foundry.telemetry import Telemetry

log = logging.getLogger(__name__)

__all__ = [
    "ASK_BUDGET_CAP_S",
    "CONVENTIONAL_TYPES",
    "FOREMAN_TOOLS",
    "MAX_HINT_CHARS",
    "MAX_TOOL_ROUNDS",
    "PRE_REVIEW_TIMEOUT_CAP_S",
    "Anomaly",
    "GemmaStats",
    "PreReview",
    "PreReviewIssue",
    "ShopForeman",
    "diff_stats",
    "calibration_shifts",
    "classify_quarantine",
    "heuristic_complexity",
    "merge_tiny_subtasks",
    "similar_outcomes",
]

#: Maximum tool-calling rounds in :meth:`ShopForeman.answer_question`.
MAX_TOOL_ROUNDS: int = 4

#: Upper bound (seconds) on the Ask the Foreman Gemma budget, whatever
#: ``ask_timeout_s`` says: 10 s below ``serve_api.ASK_CHUNK_TIMEOUT_S`` (110)
#: so the fallback answer always arrives before the API gives up on the chunk.
ASK_BUDGET_CAP_S: float = 100.0

#: Hooks that called Gemma in the current asyncio task since their last
#: ``last_source`` write (immutable, so concurrent tasks never share it).
_ASKED: contextvars.ContextVar[frozenset[str]] = contextvars.ContextVar("foreman_gemma_asked", default=frozenset())

#: Upper bound (seconds) on one pre-review Gemma call, whatever
#: ``gemma_timeout_s`` says: a slow Gemma must not hold good work for long.
PRE_REVIEW_TIMEOUT_CAP_S: float = 60.0

#: Diff characters sent to Gemma for a pre-review.
_PRE_REVIEW_DIFF_CHARS = 16000
_PRE_REVIEW_MAX_ISSUES = 8
_PRE_REVIEW_NOTE_CHARS = 240

#: Maximum length of a condensed error hint.
MAX_HINT_CHARS: int = 160

#: Maximum length of a Gemma-written escalation diagnosis.
MAX_DIAGNOSIS_CHARS: int = 300

#: Conventional Commit types accepted for ``commit_message``.
CONVENTIONAL_TYPES: tuple[str, ...] = (
    "feat", "fix", "docs", "style", "refactor", "perf", "test", "build", "ci", "chore", "revert",
)

_CONVENTIONAL_RE = re.compile(
    r"^(?:" + "|".join(CONVENTIONAL_TYPES) + r")(?:\([A-Za-z0-9_./-]+\))?!?: \S.*$"
)
_LOCATION_RE = re.compile(r"[\w./\\-]+\.\w+:\d+")
_TERMINAL = {TaskState.COMPLETE.value, TaskState.QUARANTINED.value, TaskState.FAILED.value}
_ACTIVE_TICKET = ("open", "claimed")
_LARGE_WORDS = ("rewrite", "overhaul", "migrate", "migration", "end-to-end", "end to end", "redesign")
_DOC_SUFFIXES = (".md", ".rst", ".txt", ".adoc")
_QA_TASK_ID = "foreman-qa"

#: A quarantined task is considered for RETRY_TASK only after this many seconds
#: (give the infrastructure a moment) and only within ``_RETRY_MAX_AGE_S``.
_RETRY_MIN_WAIT_S = 60.0
_RETRY_MAX_AGE_S = 6 * 3600.0
#: After the user undoes one of these actions, the Foreman does not prescribe it
#: again for the same task for this long (seconds).
_UNDO_SUPPRESS_S = 2 * 3600.0
_UNDO_SUPPRESSED = frozenset({
    "REASSIGN_TEAM", "RESIZE_ALLOCATION", "THROTTLE_DISPATCH", "PROMOTE_PEER_REVIEWER", "WIDEN_CONTEXT",
})
#: kv key the Floor Manager stores its action log under (read only here).
_ACTIONS_KV_KEY = "floor_manager.actions"
#: Gemma decisions (REASSIGN/RETRY choices) asked per ``evaluate_line_anomaly`` call.
_MAX_DECISIONS_PER_EVAL = 3
#: Quarantine reasons that are real build/review failures: never retried.
_REAL_FAILURE_MARKERS = (
    "retry limit", "failed its gates", "gate fail", "reviewer rejected", "rejected", "duplicate of",
    "could not be recovered",
)
#: Quarantine reasons that look transient (infrastructure, network, limits).
_TRANSIENT_MARKERS = (
    "infrastructure", "timed out", "timeout", "connection", "network", "rate limit", "rate-limit",
    "429", "502", "503", "504", "overloaded", "unavailable", "temporar", "econnreset", "broken pipe",
)
_DECISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "prescribed_action": {"type": "string"},
        "diagnosis": {"type": "string"},
    },
    "required": ["prescribed_action", "diagnosis"],
}


def classify_quarantine(reason: str | None) -> str:
    """``"real"``, ``"transient"`` or ``"unclear"`` for a quarantine reason.

    Real build failures win over transient words (``"retry limit reached
    (timeout)"`` is real: the builder used up its attempts).
    """
    text = " ".join(str(reason or "").lower().split())
    if not text:
        return "unclear"
    if any(m in text for m in _REAL_FAILURE_MARKERS):
        return "real"
    if any(m in text for m in _TRANSIENT_MARKERS):
        return "transient"
    return "unclear"
_UNKNOWN_TASK = "unknown"
#: Failed gate output showing the builder could not see code it needed.
_VISIBILITY_FAILURE_RE = re.compile(
    r"\bNameError\b|\bBLOCKED\b|\bF821\b|[Uu]ndefined name\b|\bis not defined\b|cannot import name"
)
#: Visibility failures on one task (at its current level) before its context is widened.
_WIDEN_AFTER_FAILURES = 2
#: Similar past tasks that must have needed a level before new tasks start at it.
_LEARN_LEVEL_MIN_OUTCOMES = 2

# --------------------------------------------------------------------------- tools

FOREMAN_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "get_queue_summary",
            "description": "Counts of tasks per state, tickets per team and status, and the open-ticket queue depth.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_token_velocity",
            "description": "Claude token burn rate (tokens and requests per minute) over a window, against the configured caps, plus tokens saved by the Foreman.",
            "parameters": {
                "type": "object",
                "properties": {
                    "window_minutes": {
                        "type": "number",
                        "description": "Window length in minutes (default 15).",
                        "minimum": 1,
                        "maximum": 1440,
                    }
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "inspect_task_failure",
            "description": "State, tickets, recent failed gate checks and reviewer verdicts of one task.",
            "parameters": {
                "type": "object",
                "properties": {"task_id": {"type": "string", "description": "Foundry task id."}},
                "required": ["task_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_team_bottlenecks",
            "description": "Per team: active tickets, idle builders, tasks waiting for review, stalled tickets and gate failures.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
]
"""OpenAI-format tool definitions for the Foreman's read-only tools."""

# --------------------------------------------------------------------------- schemas for Gemma

_SIZING_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "complexity": {"type": "string", "enum": ["S", "M", "L", "XL"]},
        "rationale": {"type": "string"},
        "subtasks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
                    "allowed_files": {"type": "array", "items": {"type": "string"}},
                    "independent": {"type": "boolean"},
                    "estimated_lines": {"type": "integer", "minimum": 1},
                    "part_note": {"type": "string"},
                },
                "required": ["title", "part_note", "acceptance_criteria", "allowed_files", "estimated_lines"],
            },
        },
    },
    "required": ["complexity", "rationale", "subtasks"],
}

_PRE_REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["pass", "send_back"]},
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "severity": {"type": "string", "enum": ["blocking", "minor"]},
                    "note": {"type": "string"},
                },
                "required": ["severity", "note"],
            },
        },
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": ["verdict", "issues", "confidence"],
}

_PRE_REVIEW_PROMPT = (
    "You pre-screen a code change (unified diff) before a senior reviewer sees it. It already passed "
    "syntax, lint and test gates. Mark an issue 'blocking' ONLY for an obvious, objective problem: the "
    "diff does not touch the files or the objective it was asked for; unfinished work (TODO, "
    "placeholder, stub, 'not implemented', 'pass' where the work should be); breakage the gates can't "
    "catch (e.g. a broken non-Python file, truncated code); debug leftovers (print/console.log debugging, "
    "breakpoints); secrets or credentials; edits to files outside the allowed list. Style, naming, "
    "design opinions and possible improvements are NEVER blocking (use 'minor' or leave them out). "
    "A '[diff truncated]' marker is not a problem. If unsure, verdict 'pass'. verdict 'send_back' requires at least one blocking issue. confidence "
    "(0..1) is how sure you are of the verdict. Reply with JSON only."
)

_PR_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "commit_message": {"type": "string"},
        "pr_body": {"type": "string"},
    },
    "required": ["commit_message", "pr_body"],
}


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


# --------------------------------------------------------------------------- sizing calibration

#: Sizes in order, smallest first (a calibration shift moves one step along it).
SIZE_ORDER: tuple[str, ...] = ("S", "M", "L", "XL")
#: Past outcomes shown to Gemma as sizing examples.
CALIBRATION_EXAMPLES: int = 5
#: Outcomes with the same predicted size needed before a bias shift applies.
CALIBRATION_MIN_OUTCOMES: int = 8
#: Share of those outcomes that must be bigger (or smaller) to shift one size.
CALIBRATION_SHIFT_SHARE: float = 0.6
#: Most recent outcomes per predicted size the bias rule looks at.
CALIBRATION_WINDOW: int = 20
_TITLE_STOP = frozenset({"the", "and", "for", "with", "add", "fix", "into", "from", "that", "this", "make", "use"})


def _outcome_get(outcome: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        value = _field(outcome, name)
        if value not in (None, ""):
            return value
    return default


def _outcome_files(outcome: Any) -> list[str]:
    files = _outcome_get(outcome, "files", default=[])
    if isinstance(files, str):
        try:
            files = json.loads(files)
        except ValueError:
            files = [files]
    return _str_list(files)


def _outcome_predicted(outcome: Any) -> str | None:
    value = str(_outcome_get(outcome, "predicted_complexity", "predicted", "complexity", default="")).upper()
    return value if value in SIZE_ORDER else None


def _outcome_raw(outcome: Any) -> str | None:
    """The size predicted before calibration (``raw_predicted``; ``predicted`` on old rows)."""
    value = str(_outcome_get(outcome, "raw_predicted", default="")).upper()
    return value if value in SIZE_ORDER else _outcome_predicted(outcome)


def _excluded(outcome: Any) -> bool:
    return bool(_field(outcome, "excluded_from_calibration"))


def _outcome_actual(outcome: Any) -> str | None:
    """The outcome's actual size: its stored value, else the queue's ``actual_complexity``."""
    value = str(_outcome_get(outcome, "actual_complexity", "actual", default="")).upper()
    if value in SIZE_ORDER:
        return value
    try:
        from foundry.sizing_outcomes import actual_complexity  # pure effort -> size scorer
    except ImportError:  # pragma: no cover - module missing on an old checkout
        return None
    try:
        value = str(actual_complexity(outcome)).upper()
    except Exception:  # noqa: BLE001 - a malformed row is just skipped
        return None
    return value if value in SIZE_ORDER else None


def _size_delta(predicted: str, actual: str) -> int:
    """``actual - predicted`` in steps; L and XL count as the same size (same crew, same clean effort)."""
    try:
        from foundry.sizing_outcomes import size_delta
    except ImportError:  # pragma: no cover - module missing on an old checkout
        return 0 if {predicted, actual} <= {"L", "XL"} else SIZE_ORDER.index(actual) - SIZE_ORDER.index(predicted)
    return size_delta(predicted, actual)


def _title_words(title: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (title or "").lower()) if len(w) >= 3 and w not in _TITLE_STOP}


def similar_outcomes(
    outcomes: Sequence[Any], title: str, files: Sequence[str], *, task_id: str | None = None,
    limit: int = CALIBRATION_EXAMPLES,
) -> list[Any]:
    """Up to ``limit`` past outcomes most like this task: file overlap first, then title-word overlap.

    Outcomes sharing nothing are left out; ties keep the provider's order
    (most recent first). The task's own earlier outcome is skipped.
    """
    want_files, want_words = set(files), _title_words(title)
    scored = []
    for k, outcome in enumerate(outcomes):
        if task_id and str(_field(outcome, "task_id") or "") == task_id:
            continue
        if _excluded(outcome) or _outcome_raw(outcome) is None or _outcome_actual(outcome) is None:
            continue
        f = len(want_files & set(_outcome_files(outcome)))
        w = len(want_words & _title_words(str(_field(outcome, "title") or "")))
        if f or w:
            scored.append((-f, -w, k, outcome))
    scored.sort(key=lambda item: item[:3])
    return [item[3] for item in scored[:limit]]


def _outcome_context_level(outcome: Any) -> int:
    """The context level a past task finished at (1 when missing or unusable)."""
    try:
        return int(_field(outcome, "context_level") or 1)
    except (TypeError, ValueError):
        return 1


def learned_context_level(
    outcomes: Sequence[Any], title: str, files: Sequence[str], *, task_id: str | None = None, ceiling: int = 3,
) -> int:
    """The level a new task should start at: the highest one most of its similar past tasks needed.

    A level counts only when at least ``_LEARN_LEVEL_MIN_OUTCOMES`` similar
    tasks reached it and they are more than half of the similar tasks.
    """
    levels = [_outcome_context_level(o) for o in similar_outcomes(outcomes, title, files, task_id=task_id)]
    best = 1
    for level in range(2, ceiling + 1):
        reached = sum(1 for value in levels if value >= level)
        if reached >= _LEARN_LEVEL_MIN_OUTCOMES and reached * 2 > len(levels):
            best = level
    return best


def _example_line(outcome: Any) -> str:
    title = _clip(str(_field(outcome, "title") or _field(outcome, "task_id") or "?"), 80)
    files = _outcome_files(outcome)
    shown = ", ".join(files[:4]) + (f" +{len(files) - 4} more" if len(files) > 4 else "") if files else "none listed"
    attempts = _outcome_get(outcome, "builder_attempts", "total_attempts", "attempts", default=0)
    calls = _outcome_get(outcome, "claude_calls", default=0)
    return (
        f"Past task {title} (files {shown}): you predicted {_outcome_raw(outcome)}, "
        f"it actually took {_outcome_actual(outcome)} ({attempts} attempts, {calls} Claude calls)"
    )


def calibration_shifts(outcomes: Sequence[Any]) -> dict[str, dict[str, Any]]:
    """Bias rules: predicted size -> {to, n, bigger, smaller, pct, direction}.

    Rows are bucketed by the *raw* prediction (``raw_predicted``, before any
    shift; ``predicted`` on old rows), so applied shifts never feed back into
    the rule; rows flagged ``excluded_from_calibration`` are ignored. For each
    raw predicted size, the most recent :data:`CALIBRATION_WINDOW`
    outcomes are counted; with at least :data:`CALIBRATION_MIN_OUTCOMES` of
    them and :data:`CALIBRATION_SHIFT_SHARE` or more actually bigger (or
    smaller), sizes predicted that way move one step up (or down). Never more
    than one step, never past S or XL.
    """
    by_pred: dict[str, list[str]] = {}
    for outcome in outcomes:
        if _excluded(outcome):
            continue
        pred, actual = _outcome_raw(outcome), _outcome_actual(outcome)
        if pred is None or actual is None:
            continue
        bucket = by_pred.setdefault(pred, [])
        if len(bucket) < CALIBRATION_WINDOW:
            bucket.append(actual)
    rules: dict[str, dict[str, Any]] = {}
    for pred, actuals in by_pred.items():
        n = len(actuals)
        if n < CALIBRATION_MIN_OUTCOMES:
            continue
        idx = SIZE_ORDER.index(pred)
        bigger = sum(1 for a in actuals if _size_delta(pred, a) > 0)
        smaller = sum(1 for a in actuals if _size_delta(pred, a) < 0)
        if bigger / n >= CALIBRATION_SHIFT_SHARE and idx < len(SIZE_ORDER) - 1:
            to, direction, share = SIZE_ORDER[idx + 1], "up", bigger / n
        elif smaller / n >= CALIBRATION_SHIFT_SHARE and idx > 0:
            to, direction, share = SIZE_ORDER[idx - 1], "down", smaller / n
        else:
            continue
        rules[pred] = {
            "predicted": pred, "to": to, "direction": direction, "n": n,
            "bigger": bigger, "smaller": smaller, "pct": round(share * 100),
        }
    return rules


def diff_stats(diff: str) -> dict[str, Any]:
    """Summarise a unified diff.

    Returns ``{"files": [{"path", "status", "added", "removed"}], "added",
    "removed"}`` where ``status`` is ``added``, ``deleted`` or ``modified``.
    """
    files: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    old_path: str | None = None
    for line in (diff or "").splitlines():
        if line.startswith("diff --git "):
            current, old_path = None, None
            continue
        if line.startswith("--- "):
            old_path = line[4:].split("\t")[0].strip()
            continue
        if line.startswith("+++ ") and old_path is not None:
            new_path = line[4:].split("\t")[0].strip()
            if new_path == "/dev/null":
                status, path = "deleted", old_path
            elif old_path == "/dev/null":
                status, path = "added", new_path
            else:
                status, path = "modified", new_path
            if path.startswith(("a/", "b/")):
                path = path[2:]
            current = {"path": path, "status": status, "added": 0, "removed": 0}
            files.append(current)
            old_path = None
            continue
        if current is None:
            continue
        if line.startswith("+") and not line.startswith("+++"):
            current["added"] += 1
        elif line.startswith("-") and not line.startswith("---"):
            current["removed"] += 1
    return {
        "files": files,
        "added": sum(f["added"] for f in files),
        "removed": sum(f["removed"] for f in files),
    }


def _is_test_path(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return "/tests/" in f"/{path}" or name.startswith("test_") or name.endswith(("_test.py", ".test.js", ".spec.js", ".test.ts", ".spec.ts"))


@dataclass
class Anomaly:
    """A deterministically detected line anomaly, before it becomes an escalation."""

    category: EscalationCategory
    severity: Severity
    action: PrescribedAction
    team_id: str | None = None
    task_id: str | None = None
    agent_id: str | None = None
    dwell_seconds: float = 0.0
    gate_failure_count: int = 0
    action_parameters: dict[str, Any] = field(default_factory=dict)
    extra_metrics: dict[str, Any] = field(default_factory=dict)
    fallback_diagnosis: str = ""
    #: Actions Gemma may choose between (``action`` is the deterministic pick and
    #: is always among them); empty means Gemma only writes the diagnosis.
    choices: tuple[PrescribedAction, ...] = ()

    @property
    def key(self) -> str:
        """Identity used for the re-alert cooldown."""
        base = f"{self.category.value}|{self.team_id}|{self.task_id}|{self.agent_id}"
        # A widen shares LINE_JAM with quarantine: its cooldown must not hold a quarantine back.
        return f"{base}|widen" if self.action == PrescribedAction.WIDEN_CONTEXT else base


# --------------------------------------------------------------------------- the foreman


@dataclass(frozen=True)
class PreReviewIssue:
    """One problem Gemma found in a pre-review (``severity``: blocking|minor)."""

    severity: str
    note: str

    def to_dict(self) -> dict[str, str]:
        return {"severity": self.severity, "note": self.note}


@dataclass(frozen=True)
class PreReview:
    """Result of :meth:`ShopForeman.pre_review_diff`.

    Attributes:
        verdict: ``"pass"`` or ``"send_back"`` (Gemma's call; the fallback is
            always ``"pass"``).
        issues: problems Gemma listed (blocking ones first is not guaranteed).
        confidence: 0..1, Gemma's confidence in ``verdict`` (0 for the fallback).
        source: ``"gemma"`` or ``"fallback"`` (Gemma off, failed, timed out or
            gave an unusable reply).
        error: why the fallback was used, when it was.
    """

    verdict: str = "pass"
    issues: tuple[PreReviewIssue, ...] = ()
    confidence: float = 0.0
    source: str = "fallback"
    error: str | None = None

    @property
    def blocking(self) -> list[PreReviewIssue]:
        """The issues marked ``blocking``."""
        return [i for i in self.issues if i.severity == "blocking"]

    @property
    def is_fallback(self) -> bool:
        return self.source != "gemma"

    def should_send_back(self, min_confidence: float) -> bool:
        """True only for a Gemma ``send_back`` with >= 1 blocking issue and
        ``confidence >= min_confidence`` (never for the fallback)."""
        return (
            self.source == "gemma"
            and self.verdict == "send_back"
            and bool(self.blocking)
            and self.confidence >= float(min_confidence)
        )

    def notes_text(self, limit: int = 600) -> str:
        """Issues as one line (blocking first), e.g. ``"blocking: TODO left in f(); minor: ..."``;
        ``""`` when there are none."""
        ordered = self.blocking + [i for i in self.issues if i.severity != "blocking"]
        text = "; ".join(f"{i.severity}: {i.note}" for i in ordered)
        return _clip(text, limit) if text else ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "issues": [i.to_dict() for i in self.issues],
            "confidence": self.confidence,
            "source": self.source,
            "error": self.error,
        }


def _parse_pre_review(data: Any) -> PreReview | None:
    """Validate Gemma's pre-review JSON; ``None`` when unusable."""
    if not isinstance(data, Mapping):
        return None
    verdict = str(data.get("verdict") or "").strip().lower().replace("-", "_").replace(" ", "_")
    if verdict not in ("pass", "send_back"):
        return None
    raw_conf = data.get("confidence")
    if isinstance(raw_conf, bool) or not isinstance(raw_conf, (int, float)) or math.isnan(float(raw_conf)):
        return None
    confidence = min(1.0, max(0.0, float(raw_conf)))
    raw_issues = data.get("issues") or []
    if not isinstance(raw_issues, list):
        return None
    issues: list[PreReviewIssue] = []
    for item in raw_issues:
        if not isinstance(item, Mapping):
            continue
        note = " ".join(str(item.get("note") or "").split())
        if not note:
            continue
        severity = "blocking" if str(item.get("severity") or "").strip().lower() == "blocking" else "minor"
        issues.append(PreReviewIssue(severity, _clip(note, _PRE_REVIEW_NOTE_CHARS)))
        if len(issues) >= _PRE_REVIEW_MAX_ISSUES:
            break
    return PreReview(verdict=verdict, issues=tuple(issues), confidence=confidence, source="gemma")


#: A subtask estimated below this many changed lines can be tiny.
TINY_SUBTASK_MAX_LINES: int = 30

#: A tiny subtask touches at most this many files.
TINY_SUBTASK_MAX_FILES: int = 1


def _line_estimate(value: Any) -> int | None:
    """A usable per-subtask line estimate: a positive JSON integer, else ``None`` (unknown)."""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _add_estimates(first: Any, second: Any) -> int | None:
    """Sum of two line estimates; ``None`` when either is unknown."""
    if isinstance(first, int) and isinstance(second, int):
        return first + second
    return None


def _join_part_notes(first: Any, second: Any) -> str:
    """One part note for two merged subtasks (blanks and repeats dropped)."""
    notes = [str(n).strip() for n in (first, second) if n and str(n).strip()]
    return "; ".join(dict.fromkeys(notes))


def merge_tiny_subtasks(
    subtasks: Sequence[Mapping[str, Any]],
    *,
    max_lines: int = TINY_SUBTASK_MAX_LINES,
    max_files: int = TINY_SUBTASK_MAX_FILES,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Fold tiny subtasks into a neighbour so each one doesn't cost a whole builder.

    A subtask is tiny when its ``estimated_lines`` is known and below
    ``max_lines`` and it touches at most ``max_files`` files; a subtask with
    no estimate is never tiny. A tiny subtask joins the previous subtask (build
    order is kept); a tiny first subtask absorbs the one after it.

    Returns the new subtasks (the input is not changed) and one
    ``"<title> into <title>"`` entry per merge.
    """

    def is_tiny(sub: Mapping[str, Any]) -> bool:
        lines = sub.get("estimated_lines")
        return isinstance(lines, int) and lines < max_lines and len(sub.get("allowed_files") or []) <= max_files

    kept: list[dict[str, Any]] = []
    merges: list[str] = []
    for sub in subtasks:
        if not kept or not (is_tiny(sub) or is_tiny(kept[-1])):
            kept.append(
                dict(
                    sub,
                    allowed_files=list(sub.get("allowed_files") or []),
                    acceptance_criteria=list(sub.get("acceptance_criteria") or []),
                )
            )
            continue
        target = kept[-1]
        merges.append(f"{_clip(str(sub.get('title')), 40)} into {_clip(str(target.get('title')), 40)}")
        target["title"] = _clip(f"{target.get('title')} + {sub.get('title')}", 200)
        if note := _join_part_notes(target.get("part_note"), sub.get("part_note")):
            target["part_note"] = note
        target["acceptance_criteria"] += list(sub.get("acceptance_criteria") or [])
        target["allowed_files"] = list(dict.fromkeys([*target["allowed_files"], *(sub.get("allowed_files") or [])]))
        target["independent"] = bool(target.get("independent")) and bool(sub.get("independent"))
        target["estimated_lines"] = _add_estimates(target.get("estimated_lines"), sub.get("estimated_lines"))
    return kept, merges


class ShopForeman:
    """The Gemma-backed Shop Foreman (every hook has a deterministic fallback).

    Args:
        gemma: a :class:`~foundry.gemma_client.GemmaClient`, or ``None`` to run
            purely on heuristics.
        queue: the :class:`~foundry.orchestrator_queue.FoundryQueue` (read only;
            its clock is used for alert timestamps and stall checks).
        telemetry: the :class:`~foundry.telemetry.Telemetry` (read, plus
            ``record_tokens_saved``). May be ``None``.
        context_engine: the :class:`~foundry.context_engine.ContextEngine` used
            by :meth:`prune_context_slice` and as the trace-condensing fallback.
        config: thresholds and caps (defaults to ``FoundryConfig()``).
        cooldown_s: seconds before the same anomaly (category + target) may be
            raised again.
        peer_review_minutes: duration requested for PROMOTE_PEER_REVIEWER.
        throttle_seconds: duration requested for THROTTLE_DISPATCH.
        outcomes_provider: ``(repo=None, limit=50) -> list`` of finished sizing
            outcomes, newest first, used to calibrate :meth:`size_task_scope`
            (defaults to ``queue.sizing_outcomes`` when the queue has it).
        actions_provider: ``(limit=...) -> list`` of applied actions (the Floor
            Manager's action log); a task with a RETRY_TASK in it is never retried again.
    """

    def __init__(
        self,
        gemma: GemmaClient | None,
        queue: FoundryQueue,
        telemetry: Telemetry | None,
        context_engine: ContextEngine | None,
        config: FoundryConfig | None = None,
        *,
        cooldown_s: float = 300.0,
        peer_review_minutes: float = 15.0,
        throttle_seconds: float = 60.0,
        outcomes_provider: Any = None,
        actions_provider: Any = None,
    ) -> None:
        self.gemma = gemma
        self.queue = queue
        self.telemetry = telemetry
        self.context_engine = context_engine
        self.config = config or FoundryConfig()
        self.cooldown_s = float(cooldown_s)
        self.peer_review_minutes = float(peer_review_minutes)
        self.throttle_seconds = float(throttle_seconds)
        #: ``(repo=None, limit=50) -> list[outcome]`` (newest first); ``queue.sizing_outcomes`` when absent.
        self.outcomes_provider = outcomes_provider
        #: ``(limit=...) -> list[{action, task_id, ...}]``: the manager's action log, read so
        #: RETRY_TASK is never prescribed twice for one task (``queue.actions`` is read too).
        self.actions_provider = actions_provider
        self._retry_prescribed: set[str] = set()
        self._retry_declined: set[str] = set()
        #: Size Gemma / the heuristic chose for the latest sizing, before the calibration
        #: shift (stored as the outcome's ``raw_predicted`` so calibration never learns from itself).
        self.last_raw_complexity: str | None = None
        self._gemma_raw: str | None = None
        self._raised: dict[str, datetime] = {}
        self._alert_day: str | None = None
        self._alert_seq = 0
        self._stats = GemmaStats()
        #: Which hooks were served by Gemma vs. the fallback (for tests/demo).
        self.last_source: dict[str, str] = _SourceLog(self._stats)
        #: Milliseconds the latest Gemma request of each hook took.
        self.last_timing: dict[str, float] = {}
        #: Whether the latest failed Gemma request timed out (used for the ask fallback wording).
        self._last_timed_out = False

    # ------------------------------------------------------------------ infra
    def _now(self) -> datetime:
        """Current UTC time according to the queue clock."""
        now_fn = getattr(self.queue, "now", None)
        parsed = _parse_ts(now_fn()) if callable(now_fn) else None
        return parsed or datetime.now(timezone.utc)

    def _save(self, task_id: str | None, source: str, tokens: int) -> None:
        """Record saved Claude tokens; telemetry problems never break a hook."""
        if self.telemetry is None or tokens <= 0:
            return
        try:
            self.telemetry.record_tokens_saved(task_id or _UNKNOWN_TASK, source, int(tokens))
        except Exception as exc:  # noqa: BLE001 - bookkeeping is best effort
            log.debug("record_tokens_saved failed: %s", exc)

    def gemma_stats(self) -> dict[str, Any]:
        """Recent Gemma speed and fallback counts (JSON-ready).

        ``{calls, errors, timeouts, fallbacks, served_by_gemma, avg_ms,
        last_ms, last_error, by_hook: {hook: {...same counters...}}}``;
        ``avg_ms``/``last_ms`` are ``None`` before the first call.
        """
        return self._stats.snapshot()

    async def _chat(
        self,
        messages: list[dict[str, Any]],
        *,
        hook: str,
        json_schema: dict[str, Any] | None = None,
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.1,
        timeout: float | None = None,
    ) -> GemmaReply | None:
        """Call Gemma for ``hook``; ``None`` when it is absent, fails or times out.

        ``timeout`` (seconds) bounds this call: it is passed to clients whose
        ``chat`` accepts it and also enforced with :func:`asyncio.wait_for`.
        Every attempt is timed into :attr:`last_timing` and :meth:`gemma_stats`.
        """
        if self.gemma is None:
            return None
        kwargs: dict[str, Any] = {"tools": tools, "json_schema": json_schema, "temperature": temperature}
        if timeout is not None and _accepts_timeout(self.gemma.chat):
            kwargs["timeout"] = timeout
        _ASKED.set(_ASKED.get() | {hook})
        started = time.monotonic()
        error: str | None = None
        timed_out = False
        try:
            call = self.gemma.chat(messages, **kwargs)
            reply = await (asyncio.wait_for(call, timeout) if timeout is not None else call)
        except (asyncio.TimeoutError, TimeoutError) as exc:
            error, timed_out, reply = f"timed out after {timeout:g}s" if timeout else str(exc), True, None
        except Exception as exc:  # noqa: BLE001 - Gemma is optional everywhere
            error, reply = str(exc) or type(exc).__name__, None
            timed_out = "timed out" in error
        ms = (time.monotonic() - started) * 1000.0
        self.last_timing[hook] = round(ms, 1)
        self._stats.record_call(hook, ms, error=error, timed_out=timed_out)
        if error is not None:
            self._last_timed_out = timed_out
            log.info("Shop Foreman: Gemma unavailable for %s (%s); using fallback", hook, error)
        return reply

    def _sql(self, sql: str, params: Sequence[Any] = ()) -> list[Any]:
        """Run a read-only query on the queue database (empty list on error)."""
        conn = getattr(self.queue, "conn", None)
        if conn is None:
            return []
        lock = getattr(self.queue, "lock", None)
        try:
            if lock is not None:
                with lock:
                    return list(conn.execute(sql, tuple(params)).fetchall())
            return list(conn.execute(sql, tuple(params)).fetchall())
        except Exception as exc:  # noqa: BLE001 - tables may be absent
            log.debug("foreman query failed: %s", exc)
            return []

    # ------------------------------------------------------------------ tools
    def get_queue_summary(self) -> dict[str, Any]:
        """Tasks per state, tickets per team/status, open-ticket depth and the active tasks."""
        summary = _jsonable(self.queue.summary())
        active = []
        for task in self.queue.all_tasks():
            state = _jsonable(task["state"])
            if state in _TERMINAL:
                continue
            active.append(
                {
                    "task_id": task["task_id"],
                    "title": task["title"],
                    "team_id": task["team_id"],
                    "state": state,
                    "complexity": task.get("complexity"),
                    "allocated_builders": task.get("allocated_builders", 0),
                }
            )
        summary["active_tasks"] = active
        return summary

    def get_token_velocity(self, window_minutes: float = 15) -> dict[str, Any]:
        """Burn rate over ``window_minutes`` against the TPM/RPM caps, plus tokens saved."""
        window = float(window_minutes or 15)
        window = min(max(window, 1.0), 1440.0)
        cap = float(self.config.tpm)
        out: dict[str, Any] = {
            "window_minutes": window,
            "tpm_cap": cap,
            "rpm_cap": float(self.config.rpm),
            "spike_threshold_tpm": cap * self.config.thresholds.tpm_spike_ratio,
        }
        if self.telemetry is None:
            out.update({"available": False, "current_tpm": 0.0})
            return out
        rate = self.telemetry.burn_rate(window)
        current = float(self.telemetry.current_tpm())
        out.update(
            {
                "available": True,
                "burn_rate": rate,
                "current_tpm": current,
                "utilisation": round(current / cap, 4) if cap else 0.0,
                "tokens_saved_by_source": self.telemetry.tokens_saved_by_source(),
            }
        )
        return out

    def inspect_task_failure(self, task_id: str) -> dict[str, Any]:
        """State, tickets, recent failed gate checks and verdicts of one task."""
        task = self.queue.get_task(str(task_id))
        if task is None:
            return {"task_id": task_id, "found": False}
        failures = [
            {
                "ts": r[0],
                "subtask_id": r[1],
                "check_type": r[2],
                "stderr": _clip(r[3] or "", 600),
                "stdout": _clip(r[4] or "", 600),
            }
            for r in self._sql(
                "SELECT ts, subtask_id, check_type, stderr, stdout FROM gate_results"
                " WHERE task_id=? AND passed=0 ORDER BY id DESC LIMIT 5",
                (task_id,),
            )
        ]
        tickets = [
            {
                "ticket_id": t.ticket_id,
                "subtask_id": t.subtask_id,
                "slot": t.slot,
                "status": t.status,
                "attempt": t.attempt,
                "fix_hint": t.fix_hint,
                "claimed_by": t.claimed_by,
            }
            for t in self.queue.tickets_for_task(task_id)
        ]
        verdicts = [
            {"reviewer_id": v.get("reviewer_id"), "verdict": v.get("verdict"), "retry_count": v.get("retry_count"),
             "directives": v.get("directives", [])[:5]}
            for v in self.queue.verdicts(task_id)
        ]
        return {
            "task_id": task_id,
            "found": True,
            "title": task["title"],
            "team_id": task["team_id"],
            "state": _jsonable(task["state"]),
            "quarantine_reason": task.get("quarantine_reason"),
            "gate_failure_count": self._gate_failures(task_id),
            "gate_failures_by_ticket": self._ticket_gate_failures(task_id),
            "recent_gate_failures": failures,
            "tickets": tickets,
            "verdicts": verdicts,
        }

    def get_team_bottlenecks(self) -> dict[str, Any]:
        """Per team: active tickets, idle builders, review waits, stalls and gate failures."""
        now = self._now()
        idle = self.telemetry.idle_counts() if self.telemetry is not None else {}
        waiting = self._review_waits()
        tasks = {t["task_id"]: t for t in self.queue.all_tasks()}
        teams: dict[str, Any] = {}
        team_ids = list(dict.fromkeys([*self.config.teams, *(t["team_id"] for t in tasks.values())]))
        for team_id in team_ids:
            tickets = self.queue.tickets(team_id=team_id)
            counts: dict[str, int] = {}
            for t in tickets:
                counts[t.status] = counts.get(t.status, 0) + 1
            stalled = [
                {"ticket_id": t.ticket_id, "agent_id": t.claimed_by, "idle_seconds": round(secs, 1)}
                for t, secs in self._claimed_ages(team_id, now)
                if secs > self.config.thresholds.stall_s
            ]
            team_tasks = [tid for tid, t in tasks.items() if t["team_id"] == team_id
                          and _jsonable(t["state"]) not in _TERMINAL]
            teams[team_id] = {
                "tickets": counts,
                "idle_builders": int(idle.get(team_id, len(self._free_slots(team_id)))),
                "free_slots": self._free_slots(team_id),
                "waiting_for_review": {tid: round(s, 1) for tid, s in waiting.items()
                                       if tasks.get(tid, {}).get("team_id") == team_id},
                "stalled_tickets": stalled,
                "gate_failures": {tid: n for tid in team_tasks if (n := self._gate_failures(tid))},
                "active_tasks": team_tasks,
            }
        return {
            "teams": teams,
            "thresholds": {**self.config.thresholds.model_dump(), "gate_fail_quarantine": self._gate_fail_limit()},
            "current_tpm": float(self.telemetry.current_tpm()) if self.telemetry is not None else 0.0,
        }

    def execute_tool(self, name: str, arguments: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Run one of the read-only tools by name (unknown names return an error dict)."""
        args = dict(arguments or {})
        try:
            if name == "get_queue_summary":
                return self.get_queue_summary()
            if name == "get_token_velocity":
                return self.get_token_velocity(float(args.get("window_minutes", 15) or 15))
            if name == "inspect_task_failure":
                task_id = args.get("task_id")
                if not task_id:
                    return {"error": "inspect_task_failure needs task_id"}
                return self.inspect_task_failure(str(task_id))
            if name == "get_team_bottlenecks":
                return self.get_team_bottlenecks()
        except Exception as exc:  # noqa: BLE001 - a tool error is returned to the model
            return {"error": f"{name} failed: {exc}"}
        return {"error": f"unknown tool {name!r}; available: {[t['function']['name'] for t in FOREMAN_TOOLS]}"}

    # --------------------------------------------------------------- readers
    def _gate_failures(self, task_id: str) -> int:
        """Gate failures of a task (max of telemetry and queue records)."""
        counts = [0]
        if self.telemetry is not None:
            try:
                counts.append(int(self.telemetry.gate_failures(task_id)))
            except Exception as exc:  # noqa: BLE001
                log.debug("telemetry gate_failures failed: %s", exc)
        fn = getattr(self.queue, "gate_failure_count", None)
        if callable(fn):
            try:
                counts.append(int(fn(task_id)))
            except Exception as exc:  # noqa: BLE001
                log.debug("queue gate_failure_count failed: %s", exc)
        return max(counts)

    def _ticket_gate_failures(self, task_id: str) -> dict[str, int]:
        """Failed gate results per ticket (subtask) of a task, for tickets not yet done.

        Gates short-circuit on the first failure, so one failed row is one
        failed submission: the same unit the retry policy counts (D8).

        After the task was put back on the line (an ``unquarantined`` event):
        a Foreman RETRY_TASK gave fresh attempts, so only failures since then
        count; an undone quarantine kept the attempts, so the earlier failures
        count again only once a new one arrived (no new evidence -> nothing).
        """
        rows = self._sql(
            "SELECT subtask_id, COUNT(*) FROM gate_results WHERE task_id=? AND passed=0 GROUP BY subtask_id",
            (task_id,),
        )
        done = {t.subtask_id for t in self.queue.tickets_for_task(task_id) if t.status == "done"}
        counts = {str(sub): int(n) for sub, n in rows if sub not in done}
        reopened = self._last_reopen(task_id)
        if counts and reopened is not None:
            since, note = reopened
            fresh = {str(sub): int(n) for sub, n in self._sql(
                "SELECT subtask_id, COUNT(*) FROM gate_results WHERE task_id=? AND passed=0 AND ts>?"
                " GROUP BY subtask_id", (task_id, since)) if sub not in done}
            if note.startswith("retry"):
                counts = fresh
            else:
                counts = {sub: n for sub, n in counts.items() if fresh.get(sub, 0) > 0}
        return counts

    def _last_reopen(self, task_id: str) -> tuple[str, str] | None:
        """``(ts, note)`` of the task's latest ``unquarantined`` event, or ``None``."""
        try:
            events = self.queue.events(task_id=task_id, kind="unquarantined")
        except Exception as exc:  # noqa: BLE001
            log.debug("reading unquarantined events failed: %s", exc)
            return None
        best = None
        for ev in events or []:
            ts = str(ev.get("ts") or "")
            if ts and (best is None or ts >= best[0]):
                note = str((ev.get("payload") or {}).get("note") or "").strip().lower()
                best = (ts, note)
        return best

    def _pending_review_items(self) -> list[dict[str, Any]]:
        """Submissions still waiting for a reviewer: ``{task_id, item, dwell_seconds}``, oldest first."""
        if self.telemetry is None:
            return []
        fn = getattr(self.telemetry, "waiting_submissions", None)
        if callable(fn):
            try:
                return [dict(r) for r in fn()]
            except Exception as exc:  # noqa: BLE001
                log.debug("telemetry waiting_submissions failed: %s", exc)
        try:  # older telemetry: one anonymous wait per task
            return [{"task_id": t, "item": None, "dwell_seconds": s}
                    for t, s in self.telemetry.waiting_for_review().items()]
        except Exception as exc:  # noqa: BLE001
            log.debug("telemetry waiting_for_review failed: %s", exc)
            return []

    def _review_wait_items(self) -> dict[str, dict[str, Any]]:
        """Per non-terminal task: its oldest submission waiting for review.

        Returns ``{task_id: {"dwell_seconds", "ticket_id"}}``. Dwell is per
        submission (D7); a submission whose ticket is no longer awaiting review
        (done, or reopened to ``open``) is ignored. A task in ``reviewing``
        with no wait mark at all falls back to the time since it entered that
        state.
        """
        waits: dict[str, dict[str, Any]] = {}
        for row in self._pending_review_items():
            tid, item = row.get("task_id"), row.get("item")
            if not tid:
                continue
            if item:
                ticket = self.queue.get_ticket(str(item))
                if ticket is not None and ticket.status in ("done", "open"):
                    continue
            secs = float(row.get("dwell_seconds") or 0.0)
            if tid not in waits or secs > waits[tid]["dwell_seconds"]:
                waits[tid] = {"dwell_seconds": secs, "ticket_id": item}
        now = self._now()
        for task in self.queue.tasks_in_state(TaskState.REVIEWING):
            tid = task["task_id"]
            if tid in waits:
                continue
            marked = False
            if self.telemetry is not None:
                try:
                    marked = self.telemetry.queue_dwell(tid) > 0
                except Exception:  # noqa: BLE001
                    marked = False
            if marked:
                continue  # a reviewer already picked it up
            since = _parse_ts(task.get("state_changed_at"))
            if since is not None:
                waits[tid] = {"dwell_seconds": max(0.0, (now - since).total_seconds()), "ticket_id": None}
        live = {}
        for tid, info in waits.items():
            task = self.queue.get_task(tid)
            if task is not None and _jsonable(task["state"]) not in _TERMINAL:
                live[tid] = info
        return live

    def _review_waits(self) -> dict[str, float]:
        """Seconds each non-terminal task's oldest submission has waited for review."""
        return {tid: float(info["dwell_seconds"]) for tid, info in self._review_wait_items().items()}

    def _claimed_ages(self, team_id: str | None, now: datetime) -> list[tuple[Any, float]]:
        """Claimed tickets still being built, with seconds since their builder's last activity.

        Tickets waiting for review are skipped (D6): the ``in_review`` flag, or
        a pending review-wait submission for the ticket in telemetry. Their
        builder is finished; a slow reviewer is a REVIEW_BOTTLENECK, not a stall.
        """
        awaiting = {str(r.get("item")) for r in self._pending_review_items() if r.get("item")}
        out = []
        for ticket in self.queue.tickets(team_id=team_id, status="claimed"):
            if getattr(ticket, "in_review", False) or ticket.ticket_id in awaiting:
                continue
            last = _parse_ts(ticket.claimed_at) or _parse_ts(ticket.updated_at)
            for ev in self.queue.events(task_id=ticket.task_id):
                payload = ev.get("payload") or {}
                mine = (
                    payload.get("ticket_id") == ticket.ticket_id
                    or (ticket.claimed_by and payload.get("agent_id") == ticket.claimed_by)
                    or (payload.get("subtask_id") == ticket.subtask_id)
                )
                ts = _parse_ts(ev.get("ts"))
                if mine and ts is not None and (last is None or ts > last):
                    last = ts
            if last is not None:
                out.append((ticket, max(0.0, (now - last).total_seconds())))
        return out

    def _free_slots(self, team_id: str) -> list[int]:
        """Builder slots of a team that hold no open or claimed ticket."""
        busy = {t.slot for t in self.queue.tickets(team_id=team_id) if t.status in _ACTIVE_TICKET}
        return [s for s in range(1, self.config.builders_per_team + 1) if s not in busy]

    def _top_agent_last_minute(self) -> str | None:
        """Agent with the most tokens in the last minute (telemetry table, read only)."""
        if self.telemetry is None:
            return None
        try:
            now = float(self.telemetry.clock())
        except Exception:  # noqa: BLE001
            return None
        rows = self._sql(
            "SELECT agent_id, SUM(input_tokens + output_tokens) AS n FROM telemetry_tokens"
            " WHERE ts > ? AND ts <= ? GROUP BY agent_id ORDER BY n DESC LIMIT 1",
            (now - 60.0, now),
        )
        return str(rows[0][0]) if rows else None

    # ------------------------------------------------------------------ sizing
    async def size_task_scope(self, task: Any) -> TaskSizingDirective:
        """Size a work order and start it at the context level similar past tasks needed."""
        directive = await self._size_task_scope(task)
        self._seed_context_level(directive.task_id, task)
        return directive

    def _seed_context_level(self, task_id: str, task: Any) -> None:
        """Raise a new task's context level to the learned starting level (best effort, never lowers it)."""
        try:
            repo = str(_field(task, "repo_root") or _field(task, "repo") or "").strip() or None
            files = _str_list(_field(task, "files") or _field(task, "allowed_files"))
            level = learned_context_level(
                self._outcomes(repo), str(_field(task, "title") or ""), files,
                task_id=task_id, ceiling=int(self.queue.MAX_CONTEXT_LEVEL),
            )
            before = self.queue.task_context_level(task_id)
            if self.queue.raise_task_context_level(task_id, level) > before:
                log.info("%s starts at context level %d (learned from similar tasks)", task_id, level)
        except Exception as exc:  # noqa: BLE001
            log.debug("seeding the context level failed: %s", exc)

    async def _size_task_scope(self, task: Any) -> TaskSizingDirective:
        """Size a work order into a validated :class:`TaskSizingDirective`.

        ``task`` is a mapping or object with ``task_id``, ``team_id``,
        ``title``, ``description`` and optionally ``files`` (or
        ``allowed_files``) and ``acceptance_criteria``. Gemma proposes
        complexity and subtasks (JSON schema output); the proposal is
        normalised (one subtask per builder, files limited to the task's
        files, no file in two subtasks) and validated. Any problem falls back
        to :func:`heuristic_complexity`.

        When the task lists fewer files than the sizing table would allocate
        builders, the allocation is capped at the file count (with an
        ``override_reason``) so no builder gets an empty slice.
        """
        task_id = str(_field(task, "task_id") or "").strip()
        team_id = str(_field(task, "team_id") or "").strip()
        if not task_id or not team_id:
            raise ValueError("size_task_scope needs a task with task_id and team_id")
        title = str(_field(task, "title") or task_id).strip()
        description = str(_field(task, "description") or "").strip()
        files = _str_list(_field(task, "files") or _field(task, "allowed_files"))
        criteria = _str_list(_field(task, "acceptance_criteria"))
        repo = str(_field(task, "repo_root") or _field(task, "repo") or "").strip() or None
        outcomes = self._outcomes(repo)
        shifts = calibration_shifts(outcomes)
        examples = similar_outcomes(outcomes, title, files, task_id=task_id)

        if self.gemma is not None:
            directive = await self._gemma_sizing(
                task_id, team_id, title, description, files, criteria, examples=examples, shifts=shifts
            )
            if directive is not None:
                self.last_source["size_task_scope"] = "gemma"
                # set with no await before the return, so the caller reads this task's value
                self.last_raw_complexity = self._gemma_raw
                return directive
        self.last_source["size_task_scope"] = "heuristic"
        complexity, rationale = heuristic_complexity(title, description, files, criteria)
        raw = complexity
        complexity, rationale = self._calibrate(task_id, complexity, rationale, shifts)
        directive = self._build_directive(task_id, team_id, title, complexity, rationale, files, criteria, None)
        self.last_raw_complexity = raw
        return directive

    def _outcomes(self, repo: str | None = None, limit: int = 50) -> list[Any]:
        """Finished sizing outcomes (newest first) from the provider; ``[]`` on any problem."""
        provider = self.outcomes_provider or getattr(self.queue, "sizing_outcomes", None)
        if not callable(provider):
            return []
        try:
            rows = provider(repo=repo, limit=limit)
        except Exception as exc:  # noqa: BLE001 - calibration is best effort, never blocks sizing
            log.info("Shop Foreman: sizing outcomes unavailable (%s)", exc)
            return []
        return list(rows or [])

    @staticmethod
    def _calibrate(
        task_id: str, complexity: str, rationale: str, shifts: Mapping[str, Mapping[str, Any]]
    ) -> tuple[str, str]:
        """Apply the one-step bias shift for ``complexity`` (if a rule exists)."""
        rule = shifts.get(complexity)
        if not rule:
            return complexity, rationale
        new = str(rule["to"])
        word = "bigger" if rule["direction"] == "up" else "smaller"
        log.info(
            "Shop Foreman: calibrated %s from %s to %s (%s%% of %s past %s tasks were %s)",
            task_id, complexity, new, rule["pct"], rule["n"], complexity, word,
        )
        note = (
            f" [calibrated from {rule['n']} past tasks: {rule['pct']}% of {complexity}-sized tasks "
            f"were actually {word}, so {complexity} -> {new}]"
        )
        return new, _clip(rationale, 500 - len(note)) + note

    def calibration_summary(self, repo: str | None = None) -> dict[str, Any]:
        """How well past sizing matched reality, and the bias shifts in force.

        ``{n, accuracy_pct, exact, under, over, shift_rules: [...]}``. The counts
        come from :func:`foundry.sizing_outcomes.sizing_accuracy`: the raw
        (pre-calibration) prediction versus the actual size, L and XL counted
        as the same, rows marked ``excluded_from_calibration`` skipped.
        ``under`` = actually bigger than predicted, ``over`` = smaller;
        ``accuracy_pct`` is ``None`` with no outcomes. ``shift_rules`` use the
        same outcomes (``repo`` scope) as :meth:`size_task_scope`.
        """
        from foundry.sizing_outcomes import sizing_accuracy

        outcomes = self._outcomes(repo)
        rows = [dict(o) for o in outcomes if isinstance(o, Mapping)]
        try:
            acc = sizing_accuracy(rows)
        except Exception as exc:  # noqa: BLE001 - a malformed row must not break /api/status
            log.info("Shop Foreman: sizing accuracy unavailable (%s)", exc)
            acc = {"n": 0, "exact": 0, "under": 0, "over": 0}
        n = int(acc["n"])
        shifts = calibration_shifts(outcomes)
        return {
            "n": n,
            "accuracy_pct": round(100 * acc["exact"] / n) if n else None,
            "exact": int(acc["exact"]),
            "under": int(acc["under"]),
            "over": int(acc["over"]),
            "shift_rules": [shifts[k] for k in SIZE_ORDER if k in shifts],
        }

    async def _gemma_sizing(
        self, task_id: str, team_id: str, title: str, description: str, files: list[str], criteria: list[str],
        *, examples: Sequence[Any] = (), shifts: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> TaskSizingDirective | None:
        history = ""
        if examples:
            history = (
                "\n\nHow similar past tasks in this repo went (learn from these):\n"
                + "\n".join(f"- {_example_line(o)}" for o in examples)
            )
        messages = [
            {
                "role": "system",
                "content": (
                    "You are the Shop Foreman of a software factory. Size the work order: "
                    "S = one builder, M = two builders, L or XL = a full team of five. "
                    "Split it into exactly one subtask per builder with no file in two subtasks. "
                    "Order the subtasks in build order (tests and docs last); each builder sees the "
                    "earlier subtasks' finished work. Set \"independent\": true on a subtask only "
                    "when it needs nothing (no function, file or setting) from any other subtask; "
                    "otherwise false. "
                    "Set \"estimated_lines\" on every subtask to the lines of code it will add or change. "
                    "Never reword, summarise or shorten the order: every builder gets its full text word for word. "
                    "Set \"part_note\" on every subtask to one short line saying which part of the order it covers, "
                    "and do not restate the order there. "
                    "Reply with JSON only."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Task {task_id}: {title}\n\n{_clip(description, 4000)}\n\n"
                    f"Files: {', '.join(files) or '(not listed)'}\n"
                    "Acceptance criteria:\n" + ("\n".join(f"- {c}" for c in criteria) or "- (none given)")
                    + history
                ),
            },
        ]
        reply = await self._chat(messages, hook="size_task_scope", json_schema=_SIZING_SCHEMA, temperature=0.0)
        if reply is None:
            return None
        try:
            data = _decode_json(reply.text)
            if not isinstance(data, dict):
                raise TypeError("sizing reply is not an object")
            complexity = str(data.get("complexity", "")).strip().upper()
            if complexity not in BUILDERS_FOR:
                raise ValueError(f"unknown complexity {complexity!r}")
            rationale = _clip(str(data.get("rationale") or "Sized by the Shop Foreman."), 500)
            raw = complexity
            complexity, rationale = self._calibrate(task_id, complexity, rationale, shifts or {})
            raw_subtasks = data.get("subtasks")
            if not isinstance(raw_subtasks, list):
                raise TypeError("subtasks is not a list")
            proposals = []
            for item in raw_subtasks:
                if not isinstance(item, Mapping):
                    raise TypeError("subtask is not an object")
                proposals.append(
                    {
                        "title": str(item.get("title") or "").strip(),
                        "part_note": str(item.get("part_note") or "").strip(),
                        "acceptance_criteria": _str_list(item.get("acceptance_criteria")),
                        "allowed_files": _str_list(item.get("allowed_files")),
                        # only a literal JSON true counts: anything else keeps the subtask sequential
                        "independent": item.get("independent") is True,
                        "estimated_lines": _line_estimate(item.get("estimated_lines")),
                    }
                )
            directive = self._build_directive(
                task_id, team_id, title, complexity, rationale, files, criteria, proposals
            )
        except (ValueError, TypeError, ValidationError) as exc:
            log.info("Shop Foreman: invalid Gemma sizing (%s); using heuristic", exc)
            return None
        self._save(task_id, "triage", _reply_tokens(reply, messages))
        self._gemma_raw = raw
        return directive

    def _build_directive(
        self,
        task_id: str,
        team_id: str,
        title: str,
        complexity: str,
        rationale: str,
        files: list[str],
        criteria: list[str],
        proposals: list[dict[str, Any]] | None,
    ) -> TaskSizingDirective:
        """Normalise subtasks to one per builder, merge tiny ones and validate the directive."""
        n = BUILDERS_FOR[complexity]
        override: str | None = None
        if files and len(files) < n:
            override = f"only {len(files)} file(s) to split; capped from {n} to {len(files)} builder(s)"
            n = len(files)
        n = max(1, min(n, MAX_BUILDERS, self.config.builders_per_team))
        if override is None and n != BUILDERS_FOR[complexity]:
            override = f"team has {self.config.builders_per_team} builder(s)"

        subtasks: list[dict[str, Any]]
        if proposals and all(p["title"] for p in proposals) and len(proposals) >= n:
            subtasks = [dict(p, allowed_files=list(p["allowed_files"]), acceptance_criteria=list(p["acceptance_criteria"]))
                        for p in proposals[:n]]
            for k, extra in enumerate(proposals[n:]):  # merge surplus proposals round-robin
                target = subtasks[k % n]
                target["acceptance_criteria"] += extra["acceptance_criteria"]
                target["part_note"] = _join_part_notes(target.get("part_note"), extra.get("part_note"))
                target["allowed_files"] += extra["allowed_files"]
                target["independent"] = bool(target.get("independent")) and bool(extra.get("independent"))
                target["estimated_lines"] = _add_estimates(target.get("estimated_lines"), extra.get("estimated_lines"))
            if files:
                allowed, seen = set(files), set()
                for sub in subtasks:
                    kept = []
                    for f in sub["allowed_files"]:
                        if f in allowed and f not in seen:
                            kept.append(f)
                            seen.add(f)
                    sub["allowed_files"] = kept
                missing = [f for f in files if f not in seen]
                for f in missing:  # give unassigned files to the lightest subtask
                    min(subtasks, key=lambda s: len(s["allowed_files"]))["allowed_files"].append(f)
            else:
                taken: set[str] = set()
                for sub in subtasks:
                    unique = [f for f in dict.fromkeys(sub["allowed_files"]) if f not in taken]
                    taken.update(unique)
                    sub["allowed_files"] = unique
        else:
            if proposals is not None:
                rationale = f"{rationale} (subtasks split deterministically)"
            subtasks = []
            for k in range(n):
                part_files = files[k::n] if files else []
                label = ", ".join(part_files[:3]) if part_files else f"part {k + 1} of {n}"
                subtasks.append(
                    {
                        "title": title if n == 1 else _clip(f"{title} ({label})", 200),
                        "part_note": "Covers the whole order" if n == 1 else f"Covers {label}",
                        "acceptance_criteria": criteria[k::n] if criteria else [],
                        "allowed_files": part_files,
                    }
                )
        min_lines = self.config.min_subtask_lines
        subtasks, merges = merge_tiny_subtasks(subtasks, max_lines=min_lines)
        if merges:
            note = _clip(
                f"merged {len(merges)} tiny subtask(s) under {min_lines} lines: " + "; ".join(merges), 300
            )
            rationale = f"{rationale} ({note})"
            summary = f"merged tiny subtasks; {n} to {len(subtasks)} builder(s)"
            override = f"{override}; {summary}" if override else summary
            n = len(subtasks)
        for sub in subtasks:
            if not sub["acceptance_criteria"]:
                sub["acceptance_criteria"] = list(criteria) if criteria else [f"{sub['title']} is implemented and tested"]
        return TaskSizingDirective(
            task_id=task_id,
            team_id=team_id,
            complexity=complexity,  # type: ignore[arg-type]
            allocated_builders=n,
            rationale=rationale,
            subtasks=[
                SubtaskAllocation(
                    subtask_id=f"{task_id}.{k + 1}",
                    title=sub["title"],
                    part_note=sub.get("part_note") or sub["title"],
                    acceptance_criteria=sub["acceptance_criteria"],
                    allowed_files=sub["allowed_files"],
                    independent=bool(sub.get("independent", False)),  # heuristic split: never
                )
                for k, sub in enumerate(subtasks)
            ],
            override_reason=override,
        )

    # ----------------------------------------------------------------- context
    async def prune_context_slice(
        self,
        subtask: SubtaskAllocation | None = None,
        objective: str = "",
        *,
        task_id: str | None = None,
        **kwargs: Any,
    ) -> ContextSlice:
        """Build the context slice for a subtask via the context engine.

        Uses ``ContextEngine.abuild_slice`` (Gemma re-ranking when the engine
        has a Gemma client) and records the pruning savings in telemetry.
        Extra keyword arguments go to the engine unchanged. Without an engine
        a snippet-less slice (objective + criteria) is returned.
        """
        criteria = kwargs.get("acceptance_criteria")
        if criteria is None and subtask is not None:
            criteria = subtask.acceptance_criteria
        if self.context_engine is None:
            text = objective or (subtask.title if subtask is not None else "Complete the subtask.")
            return ContextSlice(objective=text, acceptance_criteria=list(criteria or []))
        builder = getattr(self.context_engine, "abuild_slice", None)
        if callable(builder):
            sliced = await builder(subtask, objective, **kwargs)
        else:  # pragma: no cover - engines without the async API
            sliced = self.context_engine.build_slice(subtask, objective, **kwargs)
        stats = getattr(self.context_engine, "last_stats", None)
        saved = int(getattr(stats, "tokens_saved", 0) or 0)
        self._save(task_id or (subtask.subtask_id.rsplit(".", 1)[0] if subtask else None), "pruning", saved)
        self.last_source["prune_context_slice"] = "gemma" if getattr(stats, "ranked_by_gemma", False) else "heuristic"
        return sliced

    # ------------------------------------------------------------------ traces
    def _fallback_trace(self, trace: str) -> str:
        """Deterministic one-line condensation (context engine, else a local fallback)."""
        line = ""
        try:
            if self.context_engine is not None:
                line = self.context_engine.condense_error_trace(trace)
            else:
                from foundry.context_engine import condense_error_trace as _condense

                line = _condense(trace)
        except Exception as exc:  # noqa: BLE001 - never fail a hint
            log.debug("deterministic condense failed: %s", exc)
        if not line:
            lines = [ln.strip() for ln in (trace or "").splitlines() if ln.strip()]
            line = lines[-1] if lines else "unknown error (empty trace)"
        return _clip(" ".join(line.split()), MAX_HINT_CHARS)

    @staticmethod
    def _valid_hint(text: str, fallback: str) -> str | None:
        """Accept a Gemma hint only if it is one non-empty line of <= 160 chars.

        When the deterministic hint names a ``file:line`` the Gemma hint must
        name one too.
        """
        candidate = (text or "").strip().strip("`").strip()
        if not candidate or "\n" in candidate or len(candidate) > MAX_HINT_CHARS:
            return None
        if _LOCATION_RE.search(fallback) and not _LOCATION_RE.search(candidate):
            return None
        return candidate

    async def condense_error_trace(self, trace: str, *, task_id: str | None = None) -> str:
        """Condense a failing gate's output into a one-line fix hint (<= 160 chars).

        Gemma writes the hint when available; it is validated (single line,
        <= 160 characters, names ``file:line`` when the trace has one).
        Otherwise the context engine's deterministic condensation is used.
        """
        fallback = self._fallback_trace(trace)
        if self.gemma is not None:
            tail = (trace or "")[-4000:]
            messages = [
                {
                    "role": "system",
                    "content": (
                        "Turn a failing check's output into ONE line (max 160 characters) a developer can act on: "
                        "'path:line: what is wrong and how to fix it'. No preamble, no code fences."
                    ),
                },
                {"role": "user", "content": f"Deterministic summary: {fallback}\n\nOutput (tail):\n{tail}"},
            ]
            reply = await self._chat(messages, hook="condense_error_trace", temperature=0.0)
            if reply is not None:
                hint = self._valid_hint(reply.text, fallback)
                if hint is not None:
                    # Claude would otherwise have read the whole trace.
                    self._save(task_id, "triage", max(_estimate_tokens(trace) - _estimate_tokens(hint), 0))
                    self.last_source["condense_error_trace"] = "gemma"
                    return hint
        self.last_source["condense_error_trace"] = "heuristic"
        return fallback

    # --------------------------------------------------------------------- PRs
    @staticmethod
    def _valid_commit(message: str) -> bool:
        first = (message or "").strip().splitlines()[0] if (message or "").strip() else ""
        return bool(first) and len(first) <= 72 and bool(_CONVENTIONAL_RE.match(first))

    def _fallback_pr(self, diff: str, title: str | None) -> dict[str, str]:
        """Conventional commit + PR body built from diff statistics."""
        stats = diff_stats(diff)
        files = stats["files"]
        paths = [f["path"] for f in files]
        if paths and all(p.lower().endswith(_DOC_SUFFIXES) for p in paths):
            ctype = "docs"
        elif paths and all(_is_test_path(p) for p in paths):
            ctype = "test"
        elif title and re.search(r"\b(fix|bug|broken|crash|error)\b", title, re.IGNORECASE):
            ctype = "fix"
        elif any(f["status"] == "added" for f in files):
            ctype = "feat"
        else:
            ctype = "chore"
        tops = {p.split("/", 1)[0] for p in paths if "/" in p}
        scope = f"({next(iter(tops))})" if len(tops) == 1 and len(paths) == len([p for p in paths if "/" in p]) else ""
        if title:
            subject = title.strip().rstrip(".")
            subject = subject[:1].lower() + subject[1:]
        elif not files:
            subject = "update project files"
        elif len(files) == 1:
            verb = {"added": "add", "deleted": "remove", "modified": "update"}[files[0]["status"]]
            subject = f"{verb} {files[0]['path'].rsplit('/', 1)[-1]}"
        else:
            subject = f"update {len(files)} files"
        head = f"{ctype}{scope}: "
        subject = _clip(subject, 72 - len(head))
        commit = head + subject
        lines = ["## Summary", "", f"{subject[:1].upper()}{subject[1:]}.", "", "## Changes", ""]
        if files:
            for f in files:
                lines.append(f"- `{f['path']}` ({f['status']}, +{f['added']}/-{f['removed']})")
        else:
            lines.append("- No file changes detected in the diff.")
        lines += ["", "## Stats", "", f"{len(files)} file(s) changed, {stats['added']} insertion(s), {stats['removed']} deletion(s)."]
        return {"commit_message": commit, "pr_body": "\n".join(lines)}

    async def draft_pr_summary(
        self, diff: str, *, task_id: str | None = None, title: str | None = None
    ) -> dict[str, str]:
        """Draft ``{"commit_message", "pr_body"}`` for a finished task.

        Gemma drafts both (JSON schema); the commit message must be a
        Conventional Commit (``type(scope): subject``, first line <= 72
        chars) and the body non-empty, else a deterministic draft from the
        diff statistics is returned.
        """
        if self.gemma is not None:
            stats = diff_stats(diff)
            messages = [
                {
                    "role": "system",
                    "content": (
                        "Write a Conventional Commit message (first line 'type(scope): subject', at most 72 characters; "
                        f"types: {', '.join(CONVENTIONAL_TYPES)}) and a short Markdown PR body for this diff. Reply with JSON only."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        (f"Task: {title}\n" if title else "")
                        + "Files: " + ", ".join(f"{f['path']} ({f['status']} +{f['added']}/-{f['removed']})" for f in stats["files"])
                        + f"\n\nDiff:\n{(diff or '')[:12000]}"
                    ),
                },
            ]
            reply = await self._chat(messages, hook="draft_pr_summary", json_schema=_PR_SCHEMA, temperature=0.2)
            if reply is not None:
                try:
                    data = _decode_json(reply.text)
                    commit = str(data.get("commit_message") or "").strip()
                    body = str(data.get("pr_body") or "").strip()
                    if self._valid_commit(commit) and body:
                        self._save(task_id, "release_draft", _reply_tokens(reply, messages))
                        self.last_source["draft_pr_summary"] = "gemma"
                        return {"commit_message": commit, "pr_body": body}
                except (ValueError, TypeError, AttributeError) as exc:
                    log.info("Shop Foreman: invalid Gemma PR draft (%s)", exc)
        self.last_source["draft_pr_summary"] = "heuristic"
        return self._fallback_pr(diff, title)

    # --------------------------------------------------------------- pre-review
    async def pre_review_diff(
        self,
        task: Any,
        assignment: Any,
        diff: str,
        gate_results: Any = None,
        *,
        task_id: str | None = None,
    ) -> PreReview:
        """Quick Gemma check of a diff that passed the gates (before the Claude reviewer).

        Args:
            task: the task (mapping or object; ``title``/``description``/``task_id`` read).
            assignment: the subtask/assignment (``title``, ``acceptance_criteria``,
                ``allowed_files``, ``subtask_id``; mapping or object; may be ``None``).
            diff: the builder's unified diff.
            gate_results: the passing gate results (list of
                ``DeterministicGateResult``/mappings, or a ``GateOutcome``); only
                their names/notes are shown to Gemma.
            task_id: telemetry task id (default: read from ``task``).

        Returns a :class:`PreReview`. Gemma flags only obvious, objective
        problems as ``blocking``; the caller decides with
        :meth:`PreReview.should_send_back`. Never raises: Gemma absent,
        failing, timing out (capped at :data:`PRE_REVIEW_TIMEOUT_CAP_S`) or
        replying with unusable JSON gives ``PreReview(verdict="pass",
        source="fallback")``. ``last_source["pre_review_diff"]`` is
        ``"gemma"`` or ``"fallback"``.
        """
        try:
            if self.gemma is None:
                result = PreReview(error="gemma off")
            else:
                messages = self._pre_review_messages(task, assignment, diff, gate_results)
                timeout = min(float(getattr(self.config, "gemma_timeout_s", PRE_REVIEW_TIMEOUT_CAP_S)), PRE_REVIEW_TIMEOUT_CAP_S)
                reply = await self._chat(
                    messages, hook="pre_review_diff", json_schema=_PRE_REVIEW_SCHEMA, temperature=0.0, timeout=timeout
                )
                if reply is None:
                    result = PreReview(error="gemma unavailable")
                else:
                    try:
                        parsed = _parse_pre_review(_decode_json(reply.text))
                    except (ValueError, TypeError, AttributeError):
                        parsed = None
                    if parsed is None:
                        log.info("Shop Foreman: unusable Gemma pre-review; passing it on")
                        result = PreReview(error="unusable reply")
                    else:
                        result = parsed
        except Exception as exc:  # noqa: BLE001 - pre-review must never block work
            log.info("Shop Foreman: pre-review failed (%s); passing it on", exc)
            result = PreReview(error=str(exc) or type(exc).__name__)
        self.last_source["pre_review_diff"] = result.source
        return result

    @staticmethod
    def _pre_review_messages(task: Any, assignment: Any, diff: str, gate_results: Any) -> list[dict[str, Any]]:
        title = str(_field(task, "title", "") or "").strip()
        desc = str(_field(task, "description", "") or "").strip()
        lines = [f"Task: {title}" if title else "Task: (untitled)"]
        if desc:
            lines.append(f"Description: {_clip(desc, 1500)}")
        if assignment is not None:
            sub_title = str(_field(assignment, "title", "") or "").strip()
            if sub_title:
                lines.append(f"Subtask: {sub_title}")
            criteria = _str_list(_field(assignment, "acceptance_criteria"))
            if criteria:
                lines.append("Acceptance criteria:\n" + "\n".join(f"- {c}" for c in criteria[:12]))
            allowed = _str_list(_field(assignment, "allowed_files"))
            lines.append("Allowed files: " + (", ".join(allowed[:40]) if allowed else "(not restricted)"))
        results = getattr(gate_results, "results", gate_results)
        if isinstance(results, (list, tuple)) and results:
            names = [str(_field(r, "check_type", "") or "") for r in results]
            names = [n for n in names if n]
            if names:
                lines.append("Gates passed: " + ", ".join(names))
        stats = diff_stats(diff or "")
        if stats["files"]:
            lines.append("Files changed: " + ", ".join(f"{f['path']} ({f['status']})" for f in stats["files"][:40]))
        text = diff or ""
        if len(text) > _PRE_REVIEW_DIFF_CHARS:
            text = text[:_PRE_REVIEW_DIFF_CHARS] + "\n[diff truncated]"
        lines.append(f"\nDiff:\n{text}")
        return [
            {"role": "system", "content": _PRE_REVIEW_PROMPT},
            {"role": "user", "content": "\n".join(lines)},
        ]

    # ---------------------------------------------------------------------- Q&A
    def _fallback_answer(self, text: str, *, reason: str | None = None) -> str:
        summary = self.get_queue_summary()
        tasks = summary.get("tasks", {})
        busy = ", ".join(f"{k}: {v}" for k, v in tasks.items() if v) or "no tasks"
        velocity = self.get_token_velocity(15)
        bottlenecks = self.get_team_bottlenecks()["teams"]
        waits = sum(len(t["waiting_for_review"]) for t in bottlenecks.values())
        stalls = sum(len(t["stalled_tickets"]) for t in bottlenecks.values())
        return (
            f"{reason or 'The Gemma Shop Foreman is offline'}, so here is the line status instead. "
            f"Tasks: {busy}. Open tickets: {summary.get('queue_depth', 0)}. "
            f"Current burn: {velocity.get('current_tpm', 0.0):.0f} tokens/min of {velocity['tpm_cap']:.0f}. "
            f"Waiting for review: {waits}. Stalled tickets: {stalls}."
        )

    @staticmethod
    def _chunks(text: str, size: int = 24) -> list[str]:
        """Split text into stream chunks on whitespace boundaries."""
        parts = re.findall(r"\S+\s*|\s+", text)
        out, buf = [], ""
        for part in parts:
            buf += part
            if len(buf) >= size:
                out.append(buf)
                buf = ""
        if buf:
            out.append(buf)
        return out

    async def answer_question(self, text: str) -> AsyncIterator[str]:
        """Stream an answer to a question about the line.

        Gemma may call the read-only tools; the loop runs at most
        :data:`MAX_TOOL_ROUNDS` tool rounds, then asks for a final answer
        without tools. Without Gemma (or if it fails) a deterministic status
        answer built from the tools is streamed.

        All rounds together get ``config.ask_timeout_s`` seconds, capped at
        :data:`ASK_BUDGET_CAP_S` (below the API's per-chunk timeout); when Gemma runs out of time the status answer
        is streamed instead of an error.
        """
        if self.gemma is None:
            self.last_source["answer_question"] = "heuristic"
            for chunk in self._chunks(self._fallback_answer(text)):
                yield chunk
            return
        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": (
                    "You are the Shop Foreman of a software factory. Answer questions about the production line "
                    "briefly and factually. Use the tools to read the queue and telemetry; never guess numbers."
                ),
            },
            {"role": "user", "content": text},
        ]
        tokens = 0
        answer: str | None = None
        budget = min(float(getattr(self.config, "ask_timeout_s", 90.0)), ASK_BUDGET_CAP_S)
        deadline = time.monotonic() + budget
        timed_out = False
        self._last_timed_out = False
        for round_no in range(MAX_TOOL_ROUNDS + 1):
            final_round = round_no == MAX_TOOL_ROUNDS
            if final_round:
                messages.append({"role": "system", "content": "Tool budget used up. Answer now with what you have."})
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            reply = await self._chat(
                messages,
                hook="answer_question",
                tools=None if final_round else FOREMAN_TOOLS,
                temperature=0.1,
                timeout=remaining,
            )
            if reply is None:
                timed_out = self._last_timed_out
                break
            tokens += _reply_tokens(reply, messages)
            if reply.tool_calls and not final_round:
                calls = []
                for k, call in enumerate(reply.tool_calls):
                    call_id = call.id or f"call_{round_no}_{k}"
                    calls.append((call_id, call))
                messages.append(
                    {
                        "role": "assistant",
                        "content": reply.text or "",
                        "tool_calls": [
                            {"id": cid, "type": "function",
                             "function": {"name": c.name, "arguments": json.dumps(c.arguments)}}
                            for cid, c in calls
                        ],
                    }
                )
                for cid, c in calls:
                    result = self.execute_tool(c.name, c.arguments)
                    messages.append(
                        {"role": "tool", "tool_call_id": cid, "name": c.name,
                         "content": json.dumps(_jsonable(result), default=str)[:8000]}
                    )
                continue
            answer = (reply.text or "").strip()
            break
        if not answer:
            self.last_source["answer_question"] = "heuristic"
            reason = f"The Gemma Shop Foreman did not answer within {budget:g} s" if timed_out else None
            answer = self._fallback_answer(text, reason=reason)
        else:
            self.last_source["answer_question"] = "gemma"
            self._save(_QA_TASK_ID, "triage", tokens)
        for chunk in self._chunks(answer):
            yield chunk

    # ---------------------------------------------------------------- anomalies
    def _gate_fail_limit(self) -> int:
        """The effective LINE_JAM limit (explicit threshold, else ``max_retries + 1``)."""
        effective = getattr(self.config, "effective_gate_fail_quarantine", None)
        if callable(effective):
            return int(effective())
        explicit = getattr(self.config.thresholds, "gate_fail_quarantine", None)
        if explicit is not None:
            return int(explicit)
        return int(getattr(self.config, "max_retries", 3)) + 1

    def detect_anomalies(self) -> list[Anomaly]:
        """Deterministically detect every current anomaly, most urgent first.

        Order: LINE_JAM, WIDEN_CONTEXT, TOKEN_VELOCITY_SPIKE, AGENT_STALLED,
        REVIEW_BOTTLENECK, CAPACITY_REBALANCE. Thresholds come from
        ``config.thresholds``.
        """
        th = self.config.thresholds
        now = self._now()
        found: list[Anomaly] = []
        tasks = {t["task_id"]: t for t in self.queue.all_tasks() if _jsonable(t["state"]) not in _TERMINAL}

        # LINE_JAM: one ticket failed its gates repeatedly (per ticket, like the retry policy) -> quarantine.
        # Default limit is max_retries + 1, so a ticket with attempts left is never quarantined here.
        jam_limit = self._gate_fail_limit()
        for tid, task in tasks.items():
            per_ticket = self._ticket_gate_failures(tid)
            if not per_ticket:
                continue
            subtask_id, n = max(per_ticket.items(), key=lambda kv: (kv[1], kv[0]))
            if n < jam_limit:
                continue
            ticket = next((t for t in self.queue.tickets_for_task(tid) if t.subtask_id == subtask_id), None)
            ticket_id = ticket.ticket_id if ticket is not None else f"{tid}:{subtask_id}"
            found.append(
                Anomaly(
                    EscalationCategory.LINE_JAM,
                    Severity.CRITICAL if n > jam_limit else Severity.HIGH,
                    PrescribedAction.QUARANTINE_TASK,
                    team_id=task["team_id"],
                    task_id=tid,
                    agent_id=ticket.claimed_by if ticket is not None else None,
                    gate_failure_count=n,
                    action_parameters={"reason": f"ticket {ticket_id} failed its gates {n} times "
                                                 f"(limit {jam_limit})",
                                       "gate_failure_count": n, "ticket_id": ticket_id},
                    extra_metrics={"threshold": jam_limit, "ticket_id": ticket_id},
                    fallback_diagnosis=f"Ticket {ticket_id} failed its deterministic gates {n} times and is on its "
                                       "last attempt. Quarantine the task before more builder calls are spent.",
                )
            )

        found.extend(self._widen_anomalies(tasks))

        # TOKEN_VELOCITY_SPIKE: burn above the configured share of the TPM cap.
        if self.telemetry is not None:
            current = float(self.telemetry.current_tpm())
            cap = float(self.config.tpm)
            limit = cap * th.tpm_spike_ratio
            if current > limit:
                agent = self._top_agent_last_minute()
                factor = round(min(0.9, max(0.1, (limit * 0.8) / current)), 2)
                found.append(
                    Anomaly(
                        EscalationCategory.TOKEN_VELOCITY_SPIKE,
                        Severity.CRITICAL if current >= cap else Severity.HIGH,
                        PrescribedAction.THROTTLE_DISPATCH,
                        team_id=agent.rsplit("-", 1)[0] if agent and "-" in agent else "floor",
                        agent_id=agent,
                        action_parameters={"factor": factor, "seconds": self.throttle_seconds},
                        extra_metrics={"tpm_cap": cap, "spike_threshold_tpm": limit},
                        fallback_diagnosis=f"Claude burn is {current:.0f} tokens/min, above {th.tpm_spike_ratio:.0%} "
                                           f"of the {cap:.0f} cap. Throttle dispatch to stay under the subscription limit.",
                    )
                )

        # AGENT_STALLED: a claimed ticket with no activity for too long.
        for ticket, secs in self._claimed_ages(None, now):
            if secs > th.stall_s and ticket.task_id in tasks:
                agent = ticket.claimed_by or f"{ticket.team_id}-{ticket.slot}"
                found.append(
                    Anomaly(
                        EscalationCategory.AGENT_STALLED,
                        Severity.HIGH if secs > 2 * th.stall_s else Severity.MEDIUM,
                        PrescribedAction.RESET_BUILDER_CONTEXT,
                        team_id=ticket.team_id,
                        task_id=ticket.task_id,
                        agent_id=agent,
                        dwell_seconds=secs,
                        gate_failure_count=self._ticket_gate_failures(ticket.task_id).get(ticket.subtask_id, 0),
                        action_parameters={"agent_id": agent, "ticket_id": ticket.ticket_id, "slot": ticket.slot},
                        extra_metrics={"threshold_s": th.stall_s},
                        fallback_diagnosis=f"Builder {agent} has shown no activity on {ticket.ticket_id} for {secs:.0f}s. "
                                           "Reset its context and reopen the ticket with a fresh prompt.",
                    )
                )

        # REVIEW_BOTTLENECK: a submission waiting too long for its reviewer (per-submission dwell).
        waits = self._review_wait_items()
        for tid, info in sorted(waits.items(), key=lambda kv: -kv[1]["dwell_seconds"]):
            secs = float(info["dwell_seconds"])
            if secs > th.review_dwell_s and tid in tasks:
                team = tasks[tid]["team_id"]
                free = self._free_slots(team)
                params: dict[str, Any] = {"minutes": self.peer_review_minutes}
                if free:
                    params["slot"] = free[-1]
                if info.get("ticket_id"):
                    params["ticket_id"] = info["ticket_id"]
                found.append(
                    Anomaly(
                        EscalationCategory.REVIEW_BOTTLENECK,
                        Severity.HIGH if secs > 2 * th.review_dwell_s else Severity.MEDIUM,
                        PrescribedAction.PROMOTE_PEER_REVIEWER,
                        team_id=team,
                        task_id=tid,
                        dwell_seconds=secs,
                        gate_failure_count=max(self._ticket_gate_failures(tid).values(), default=0),
                        action_parameters=params,
                        extra_metrics={"threshold_s": th.review_dwell_s},
                        fallback_diagnosis=f"A submission of {tid} has waited {secs:.0f}s for review on team {team}. "
                                           "Promote an idle builder to peer reviewer for a while.",
                    )
                )

        # CAPACITY_REBALANCE: idle builders while tickets queue up behind busy slots.
        idle = self.telemetry.idle_counts() if self.telemetry is not None else {}
        for tid, task in tasks.items():
            team = task["team_id"]
            tickets = self.queue.tickets_for_task(tid)
            active = [t for t in tickets if t.status in _ACTIVE_TICKET]
            stacked = len(active) - len({t.slot for t in active})
            alloc = int(task.get("allocated_builders") or 0)
            free = len(self._free_slots(team))
            idle_n = int(idle.get(team, free))
            if stacked <= 0 or idle_n <= 0 or alloc >= self.config.builders_per_team:
                continue
            new_n = min(self.config.builders_per_team, alloc + min(stacked, idle_n))
            if new_n <= alloc:
                continue
            found.append(
                Anomaly(
                    EscalationCategory.CAPACITY_REBALANCE,
                    Severity.LOW,
                    PrescribedAction.RESIZE_ALLOCATION,
                    team_id=team,
                    task_id=tid,
                    action_parameters={"builders": new_n, "from": alloc},
                    extra_metrics={"idle_builders": idle_n, "stacked_tickets": stacked},
                    fallback_diagnosis=f"Team {team} has {idle_n} idle builder(s) while {stacked} ticket(s) of {tid} "
                                       f"wait behind busy slots. Resize the allocation from {alloc} to {new_n}.",
                )
            )

        found.extend(self._reassign_anomalies(tasks, now, idle))
        found.extend(self._retry_anomalies(now))
        return found

    def _idle_teams(self, exclude: str, idle: Mapping[str, int]) -> list[str]:
        """Teams with no open or claimed ticket (and not reported as 0 idle), most idle first."""
        out = []
        for team in self.config.teams:
            if team == exclude:
                continue
            if any(t.status in _ACTIVE_TICKET for t in self.queue.tickets(team_id=team)):
                continue
            if team in idle and int(idle[team]) <= 0:
                continue
            out.append(team)
        order = {t: k for k, t in enumerate(self.config.teams)}
        return sorted(out, key=lambda t: (-int(idle.get(t, self.config.builders_per_team)), order[t]))

    def _reassign_anomalies(self, tasks: Mapping[str, Any], now: datetime,
                            idle: Mapping[str, int]) -> list[Anomaly]:
        """REASSIGN_TEAM: open tickets stuck behind busy slots of a full team while another team idles."""
        th = self.config.thresholds
        found: list[Anomaly] = []
        claimed_to: set[str] = set()
        for tid, task in tasks.items():
            team = task["team_id"]
            if self._free_slots(team):
                continue  # the team has room: RESIZE_ALLOCATION / normal dispatch handles it
            team_tickets = self.queue.tickets(team_id=team)
            busy_slots = {t.slot for t in team_tickets if t.status == "claimed"}
            stuck = []
            for t in team_tickets:
                if t.task_id != tid or t.status != "open" or t.slot not in busy_slots:
                    continue
                since = _parse_ts(t.updated_at) or _parse_ts(t.created_at)
                if since is not None and (now - since).total_seconds() > th.stall_s:
                    stuck.append((t, (now - since).total_seconds()))
            if not stuck:
                continue
            targets = [x for x in self._idle_teams(team, idle) if x not in claimed_to]
            if not targets:
                continue
            to_team = targets[0]
            claimed_to.add(to_team)  # one task per idle team per pass
            waited = max(s for _, s in stuck)
            ids = [t.ticket_id for t, _ in sorted(stuck, key=lambda x: x[0].ticket_id)]
            found.append(
                Anomaly(
                    EscalationCategory.CAPACITY_REBALANCE,
                    Severity.HIGH if waited > 2 * th.stall_s else Severity.MEDIUM,
                    PrescribedAction.REASSIGN_TEAM,
                    team_id=team,
                    task_id=tid,
                    dwell_seconds=waited,
                    action_parameters={"from_team": team, "to_team": to_team, "ticket_ids": ids,
                                       "waited_s": round(waited, 1)},
                    extra_metrics={"threshold_s": th.stall_s, "stuck_tickets": len(ids), "to_team": to_team},
                    fallback_diagnosis=f"{len(ids)} ticket(s) of {tid} have waited {waited / 60:.0f} min behind busy "
                                       f"builders on team {team} while team {to_team} is idle. "
                                       f"Move them to team {to_team}.",
                    choices=(PrescribedAction.REASSIGN_TEAM, PrescribedAction.NONE),
                )
            )
        return found

    def _action_log(self) -> list[Mapping[str, Any]]:
        """The Floor Manager's action log: ``actions_provider``, ``queue.actions`` and the stored kv copy."""
        rows: list[Mapping[str, Any]] = []
        for source in (self.actions_provider, getattr(self.queue, "actions", None)):
            if not callable(source):
                continue
            try:
                try:
                    got = source(limit=1000)
                except TypeError:
                    got = source()
            except Exception as exc:  # noqa: BLE001
                log.debug("reading the action log failed: %s", exc)
                continue
            rows.extend(r for r in got or [] if isinstance(r, Mapping))
        kv_get = getattr(self.queue, "kv_get", None)
        if callable(kv_get):
            try:
                raw = kv_get(_ACTIONS_KV_KEY)
                if isinstance(raw, (str, bytes)):
                    raw = json.loads(raw)
                if isinstance(raw, Mapping):
                    rows.extend(r for r in raw.get("actions") or [] if isinstance(r, Mapping))
            except Exception as exc:  # noqa: BLE001
                log.debug("reading the stored action log failed: %s", exc)
        return rows

    def _undone_recently(self, anomaly: Anomaly, now: datetime) -> bool:
        """True when the user undid the same action for the same target within ``_UNDO_SUPPRESS_S``.

        Applies to REASSIGN_TEAM, RESIZE_ALLOCATION, THROTTLE_DISPATCH, WIDEN_CONTEXT and
        PROMOTE_PEER_REVIEWER (a task-less THROTTLE matches any undone throttle).
        QUARANTINE_TASK needs new gate evidence instead (see ``_ticket_gate_failures``).
        """
        if anomaly.action.value not in _UNDO_SUPPRESSED:
            return False
        for row in self._action_log():
            if str(row.get("action")) != anomaly.action.value or not row.get("undone_at"):
                continue
            if anomaly.task_id and row.get("task_id") != anomaly.task_id:
                continue
            undone = _parse_ts(row.get("undone_at"))
            if undone is None or (now - undone).total_seconds() < _UNDO_SUPPRESS_S:
                return True  # a clock mismatch (undo "in the future") also counts as recent
        return False

    def _retried_tasks(self) -> set[str]:
        """Tasks a RETRY_TASK was already prescribed or applied for (memory, escalations, action log)."""
        done = set(self._retry_prescribed)
        try:
            for e in self.queue.escalations():
                if str(e.get("prescribed_action")) == PrescribedAction.RETRY_TASK.value:
                    tid = (e.get("target_entity") or {}).get("task_id")
                    if tid:
                        done.add(str(tid))
        except Exception as exc:  # noqa: BLE001
            log.debug("reading escalations failed: %s", exc)
        for row in self._action_log():
            if str(row.get("action")) == PrescribedAction.RETRY_TASK.value and row.get("task_id"):
                done.add(str(row["task_id"]))
        return done

    def _visibility_failures(self, task_id: str) -> int:
        """Failed gate results of a task showing missing code, since its context level last changed.

        Reads the output stored in ``gate_results``; tickets already done are
        left out, and so are failures from before the last widen or undo.
        """
        key = f"{self.queue.CONTEXT_LEVEL_KV_PREFIX}{task_id}"
        changed = self._sql("SELECT updated_at FROM kv WHERE key=?", (key,))
        sql = "SELECT subtask_id, stdout, stderr FROM gate_results WHERE task_id=? AND passed=0"
        params: list[Any] = [task_id]
        if changed:
            sql += " AND ts>?"
            params.append(changed[0][0])
        done = {t.subtask_id for t in self.queue.tickets_for_task(task_id) if t.status == "done"}
        return sum(
            1 for sub, out, err in self._sql(sql, params)
            if sub not in done and _VISIBILITY_FAILURE_RE.search(f"{out or ''}\n{err or ''}")
        )

    def _widen_anomalies(self, tasks: Mapping[str, Any]) -> list[Anomaly]:
        """WIDEN_CONTEXT: tasks whose builders repeatedly could not see the code they needed."""
        found: list[Anomaly] = []
        for tid, task in tasks.items():
            level = self.queue.task_context_level(tid)
            if level >= self.queue.MAX_CONTEXT_LEVEL:
                continue
            n = self._visibility_failures(tid)
            if n < _WIDEN_AFTER_FAILURES:
                continue
            found.append(
                Anomaly(
                    EscalationCategory.LINE_JAM,
                    Severity.MEDIUM,
                    PrescribedAction.WIDEN_CONTEXT,
                    team_id=task["team_id"],
                    task_id=tid,
                    gate_failure_count=n,
                    action_parameters={"level": level + 1, "from": level},
                    extra_metrics={"threshold": _WIDEN_AFTER_FAILURES, "context_level": level},
                    fallback_diagnosis=f"{tid} failed {n} times because its builder could not see code it needed. "
                                       f"Widen its context from level {level} to {level + 1}.",
                )
            )
        return found

    def _retry_anomalies(self, now: datetime) -> list[Anomaly]:
        """RETRY_TASK: recently quarantined tasks whose reason is transient (or unclear, for Gemma)."""
        try:
            quarantined = self.queue.tasks_in_state(TaskState.QUARANTINED)
        except Exception as exc:  # noqa: BLE001
            log.debug("reading quarantined tasks failed: %s", exc)
            return []
        if not quarantined:
            return []
        retried = self._retried_tasks()
        found: list[Anomaly] = []
        for task in quarantined:
            tid = str(task["task_id"])
            if tid in retried or tid in self._retry_declined:
                continue
            since = _parse_ts(task.get("state_changed_at")) or _parse_ts(task.get("updated_at"))
            if since is None:
                continue
            age = (now - since).total_seconds()
            if age < _RETRY_MIN_WAIT_S or age > _RETRY_MAX_AGE_S:
                continue
            if not any(t.status == "blocked" for t in self.queue.tickets_for_task(tid)):
                continue  # nothing to re-open (e.g. sizing failed: sizing re-runs on restart)
            reason = str(task.get("quarantine_reason") or "")
            kind = classify_quarantine(reason)
            if kind == "real" or (kind == "unclear" and self.gemma is None):
                continue
            transient = kind == "transient"
            short = _clip(" ".join(reason.split()) or "no reason recorded", 120)
            found.append(
                Anomaly(
                    EscalationCategory.LINE_JAM,
                    Severity.MEDIUM,
                    PrescribedAction.RETRY_TASK if transient else PrescribedAction.NONE,
                    team_id=task.get("team_id"),
                    task_id=tid,
                    dwell_seconds=age,
                    action_parameters={"reason": reason[:300], "transient": True,
                                       "quarantined_s": round(age, 1), "source": "heuristic"},
                    extra_metrics={"quarantine_kind": kind},
                    fallback_diagnosis=f"{tid} was quarantined for a transient reason ({short}). "
                                       "Retry it once with fresh attempts.",
                    choices=(PrescribedAction.RETRY_TASK, PrescribedAction.NONE),
                )
            )
        return found

    def _next_alert_id(self, now: datetime) -> str:
        """Issue the next per-day ``ALT-YYYYMMDD-NNN`` id and reserve it in the queue db (D12).

        The sequence is seeded from every id already reserved or recorded, and
        each candidate is persisted with ``queue.reserve_alert_id`` when it is
        issued; a taken id (another Foreman, a restart) moves on to the next
        number, so ids are never reused even if the alert is never applied.
        """
        day = now.strftime("%Y%m%d")
        prefix = f"ALT-{day}-"
        if self._alert_day != day:
            self._alert_day, self._alert_seq = day, 0
            stored: list[str] = []
            try:
                lister = getattr(self.queue, "alert_ids", None)
                if callable(lister):
                    stored = [str(a) for a in lister(prefix)]
                else:
                    stored = [str(e.get("alert_id", "")) for e in self.queue.escalations()]
            except Exception as exc:  # noqa: BLE001
                log.debug("reading stored alert ids failed: %s", exc)
            for aid in stored:
                if aid.startswith(prefix) and aid[len(prefix):].isdigit():
                    self._alert_seq = max(self._alert_seq, int(aid[len(prefix):]))
        reserve = getattr(self.queue, "reserve_alert_id", None)
        while True:
            self._alert_seq += 1
            if self._alert_seq > 999:
                raise RuntimeError("more than 999 escalations today")
            alert_id = new_alert_id(self._alert_seq, now)
            if not callable(reserve) or reserve(alert_id):
                return alert_id

    def _valid_diagnosis(self, text: str) -> str | None:
        candidate = " ".join((text or "").split()).strip().strip('"')
        if not candidate or len(candidate) > MAX_DIAGNOSIS_CHARS:
            return None
        if count_sentences(candidate) > MAX_DIAGNOSIS_SENTENCES:
            return None
        return candidate

    async def evaluate_line_anomaly(self) -> ForemanEscalation | None:
        """Detect the most urgent anomaly and return it as a validated escalation.

        Detection is deterministic (:meth:`detect_anomalies`); anomalies
        raised within ``cooldown_s`` for the same target are skipped. For most
        anomalies Gemma only writes the ``diagnosis`` (<= 2 sentences,
        validated); otherwise a fixed diagnosis is used. For REASSIGN_TEAM and
        RETRY_TASK candidates (``Anomaly.choices``) Gemma also picks the action
        from those choices (``NONE`` = leave it: the candidate is skipped and
        the next one is tried); an invalid or missing choice keeps the
        deterministic one. RETRY_TASK is never prescribed twice for a task.
        Returns ``None`` when the line is healthy.
        """
        now = self._now()
        decisions = 0
        for anomaly in self.detect_anomalies():
            last = self._raised.get(anomaly.key)
            if last is not None and (now - last).total_seconds() < self.cooldown_s:
                continue
            if self._undone_recently(anomaly, now):
                continue  # the user undid this very action: respect that for a while
            if anomaly.choices and self.gemma is not None:
                if decisions >= _MAX_DECISIONS_PER_EVAL:
                    if anomaly.action == PrescribedAction.NONE:
                        continue  # needs Gemma's decision; ask on a later pass
                    alert = await self._escalate(anomaly, now, ask_gemma=False)
                else:
                    decisions += 1
                    alert = await self._escalate(anomaly, now, ask_gemma=True)
            elif anomaly.action == PrescribedAction.NONE:
                continue  # an undecided candidate without Gemma: nothing to do
            else:
                alert = await self._escalate(anomaly, now, ask_gemma=self.gemma is not None)
            if alert is not None:
                return alert
        return None

    def _metrics_and_target(self, anomaly: Anomaly) -> tuple[dict[str, Any], dict[str, Any]]:
        try:
            queue_depth = int(self.queue.summary().get("queue_depth", 0))
        except Exception:  # noqa: BLE001
            queue_depth = 0
        current_tpm = float(self.telemetry.current_tpm()) if self.telemetry is not None else 0.0
        metrics = {
            "queue_depth": queue_depth,
            "dwell_seconds": round(float(anomaly.dwell_seconds), 1),
            "gate_failure_count": int(anomaly.gate_failure_count),
            "current_tpm": round(current_tpm, 1),
            **anomaly.extra_metrics,
        }
        target = {k: v for k, v in (("team_id", anomaly.team_id), ("task_id", anomaly.task_id),
                                     ("agent_id", anomaly.agent_id)) if v}
        return metrics, target

    async def _gemma_decision(
        self, anomaly: Anomaly, target: dict[str, Any], metrics: dict[str, Any]
    ) -> tuple[PrescribedAction | None, str | None, int]:
        """Ask Gemma to pick one of ``anomaly.choices`` and write the diagnosis.

        Returns ``(action or None when unusable, diagnosis or None, tokens)``.
        A plain-text reply (no JSON) counts as a diagnosis only.
        """
        options = [c.value for c in anomaly.choices]
        guide = {
            PrescribedAction.REASSIGN_TEAM.value: "move the task's waiting tickets to the idle team",
            PrescribedAction.RETRY_TASK.value: "re-open the quarantined task once with fresh attempts; only when "
                                               "the quarantine was caused by infrastructure or a transient "
                                               "problem (timeouts, network, rate limits), never for repeated "
                                               "real build or review failures",
            PrescribedAction.NONE.value: "leave it as it is",
        }
        messages = [
            {
                "role": "system",
                "content": "You are the Shop Foreman of a software factory. Decide what to do about this "
                           "production-line alert. Reply with JSON only: "
                           '{"prescribed_action": one of ' + json.dumps(options) + ', "diagnosis": "at most '
                           'two short sentences (max 300 characters)"}. Meaning of the actions: '
                           + "; ".join(f"{o} = {guide.get(o, o)}" for o in options) + ".",
            },
            {
                "role": "user",
                "content": json.dumps(
                    {"category": anomaly.category.value, "severity": anomaly.severity.value,
                     "target": target, "metrics": metrics, "action_parameters": anomaly.action_parameters,
                     "suggested_action": anomaly.action.value, "facts": anomaly.fallback_diagnosis},
                    default=str,
                ),
            },
        ]
        schema = {
            **_DECISION_SCHEMA,
            "properties": {**_DECISION_SCHEMA["properties"], "prescribed_action": {"type": "string", "enum": options}},
        }
        reply = await self._chat(messages, hook="evaluate_line_anomaly", json_schema=schema, temperature=0.0)
        if reply is None:
            return None, None, 0
        tokens = _reply_tokens(reply, messages)
        try:
            data = _decode_json(reply.text)
        except (ValueError, TypeError):
            data = None
        if not isinstance(data, Mapping):
            return None, self._valid_diagnosis(reply.text or ""), tokens
        raw = str(data.get("prescribed_action") or "").strip().upper().replace("-", "_").replace(" ", "_")
        action = next((c for c in anomaly.choices if c.value == raw), None)
        return action, self._valid_diagnosis(str(data.get("diagnosis") or "")), tokens

    async def _escalate(self, anomaly: Anomaly, now: datetime, *, ask_gemma: bool) -> ForemanEscalation | None:
        """Turn one anomaly into a validated escalation (``None`` when the action ends up NONE)."""
        metrics, target = self._metrics_and_target(anomaly)
        action = anomaly.action
        params = dict(anomaly.action_parameters)
        diagnosis = anomaly.fallback_diagnosis
        source = "heuristic"
        gemma_said_none = False
        if ask_gemma and anomaly.choices:
            chosen, written, tokens = await self._gemma_decision(anomaly, target, metrics)
            if chosen is not None:
                if chosen == PrescribedAction.RETRY_TASK and anomaly.action != PrescribedAction.RETRY_TASK:
                    params["source"] = "gemma"  # Gemma judged an unclear quarantine transient
                gemma_said_none = chosen == PrescribedAction.NONE
                if written is not None:
                    diagnosis = written
                elif chosen != anomaly.action:
                    diagnosis = _CHOICE_DIAGNOSIS.get(chosen, anomaly.fallback_diagnosis).format(
                        task=anomaly.task_id or "the task")
                action, source = chosen, "gemma"
            elif written is not None and action != PrescribedAction.NONE:
                diagnosis, source = written, "gemma"  # plain-text reply: a diagnosis only
            if source == "gemma":
                self._save(anomaly.task_id or _UNKNOWN_TASK, "triage", tokens)
        elif ask_gemma:
            messages = [
                {
                    "role": "system",
                    "content": "You are the Shop Foreman. Write a diagnosis of at most two short sentences "
                               "(max 300 characters) for this production-line alert. Plain text only.",
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {"category": anomaly.category.value, "severity": anomaly.severity.value,
                         "target": target, "metrics": metrics,
                         "prescribed_action": anomaly.action.value, "facts": anomaly.fallback_diagnosis},
                        default=str,
                    ),
                },
            ]
            reply = await self._chat(messages, hook="evaluate_line_anomaly", temperature=0.1)
            if reply is not None:
                written = self._valid_diagnosis(reply.text)
                if written is not None:
                    diagnosis, source = written, "gemma"
                    self._save(anomaly.task_id or _UNKNOWN_TASK, "triage", _reply_tokens(reply, messages))

        if action == PrescribedAction.NONE:
            # Gemma (or the missing decision) says leave it: no alert, and don't ask again soon.
            self._raised[anomaly.key] = now
            if gemma_said_none and PrescribedAction.RETRY_TASK in anomaly.choices and anomaly.task_id:
                self._retry_declined.add(anomaly.task_id)  # Gemma looked at it: don't ask again
            self.last_source["evaluate_line_anomaly"] = source
            return None
        if action == PrescribedAction.RETRY_TASK and anomaly.task_id:
            if anomaly.task_id in self._retried_tasks():
                return None  # never twice (another pass or a restart got there first)

        payload = {
            "alert_id": self._next_alert_id(now),
            "timestamp": now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "severity": anomaly.severity,
            "category": anomaly.category,
            "target_entity": target,
            "metrics_snapshot": metrics,
            "diagnosis": diagnosis,
            "prescribed_action": action,
            "action_parameters": params,
        }
        try:
            alert = ForemanEscalation.model_validate(payload)
        except ValidationError:
            payload["diagnosis"] = _first_sentences(anomaly.fallback_diagnosis, MAX_DIAGNOSIS_SENTENCES)
            source = "heuristic"
            alert = ForemanEscalation.model_validate(payload)
        self._raised[anomaly.key] = now
        if action == PrescribedAction.RETRY_TASK and anomaly.task_id:
            self._retry_prescribed.add(anomaly.task_id)
        self.last_source["evaluate_line_anomaly"] = source
        return alert


#: Diagnosis used when Gemma picked a different action without a usable diagnosis.
_CHOICE_DIAGNOSIS: dict[PrescribedAction, str] = {
    PrescribedAction.RETRY_TASK: "{task} was quarantined for what looks like a transient problem. "
                                 "Retry it once with fresh attempts.",
    PrescribedAction.REASSIGN_TEAM: "{task} is stuck behind busy builders. Move its waiting tickets to an idle team.",
}


def _first_sentences(text: str, n: int = 2) -> str:
    """Keep the first ``n`` sentences of ``text`` (never empty)."""
    parts = re.split(r"(?<=[.!?])\s+", " ".join((text or "").split()))
    kept = " ".join(parts[:n]).strip()
    return kept or "Complete the subtask."
