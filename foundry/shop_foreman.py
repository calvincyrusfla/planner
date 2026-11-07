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
==========================  ================================================

All public hooks are ``async`` (Gemma calls are awaited);
:meth:`ShopForeman.detect_anomalies` and the four tools are synchronous.
"""

from __future__ import annotations

import json
import logging
import math
import re
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
    "CONVENTIONAL_TYPES",
    "FOREMAN_TOOLS",
    "MAX_HINT_CHARS",
    "MAX_TOOL_ROUNDS",
    "Anomaly",
    "ShopForeman",
    "diff_stats",
    "heuristic_complexity",
]

#: Maximum tool-calling rounds in :meth:`ShopForeman.answer_question`.
MAX_TOOL_ROUNDS: int = 4

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
_UNKNOWN_TASK = "unknown"

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
                },
                "required": ["title", "acceptance_criteria", "allowed_files"],
            },
        },
    },
    "required": ["complexity", "rationale", "subtasks"],
}

_PR_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "commit_message": {"type": "string"},
        "pr_body": {"type": "string"},
    },
    "required": ["commit_message", "pr_body"],
}


# --------------------------------------------------------------------------- helpers


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

    @property
    def key(self) -> str:
        """Identity used for the re-alert cooldown."""
        return f"{self.category.value}|{self.team_id}|{self.task_id}|{self.agent_id}"


# --------------------------------------------------------------------------- the foreman


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
    ) -> None:
        self.gemma = gemma
        self.queue = queue
        self.telemetry = telemetry
        self.context_engine = context_engine
        self.config = config or FoundryConfig()
        self.cooldown_s = float(cooldown_s)
        self.peer_review_minutes = float(peer_review_minutes)
        self.throttle_seconds = float(throttle_seconds)
        self._raised: dict[str, datetime] = {}
        self._alert_day: str | None = None
        self._alert_seq = 0
        #: Which hooks were served by Gemma vs. the fallback (for tests/demo).
        self.last_source: dict[str, str] = {}

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

    async def _chat(
        self,
        messages: list[dict[str, Any]],
        *,
        json_schema: dict[str, Any] | None = None,
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.1,
    ) -> GemmaReply | None:
        """Call Gemma; ``None`` when it is absent or fails."""
        if self.gemma is None:
            return None
        try:
            return await self.gemma.chat(messages, tools=tools, json_schema=json_schema, temperature=temperature)
        except Exception as exc:  # noqa: BLE001 - Gemma is optional everywhere
            log.info("Shop Foreman: Gemma unavailable (%s); using fallback", exc)
            return None

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
            "thresholds": self.config.thresholds.model_dump(),
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
        """
        rows = self._sql(
            "SELECT subtask_id, COUNT(*) FROM gate_results WHERE task_id=? AND passed=0 GROUP BY subtask_id",
            (task_id,),
        )
        done = {t.subtask_id for t in self.queue.tickets_for_task(task_id) if t.status == "done"}
        return {str(sub): int(n) for sub, n in rows if sub not in done}

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

        if self.gemma is not None:
            directive = await self._gemma_sizing(task_id, team_id, title, description, files, criteria)
            if directive is not None:
                self.last_source["size_task_scope"] = "gemma"
                return directive
        self.last_source["size_task_scope"] = "heuristic"
        complexity, rationale = heuristic_complexity(title, description, files, criteria)
        return self._build_directive(task_id, team_id, title, complexity, rationale, files, criteria, None)

    async def _gemma_sizing(
        self, task_id: str, team_id: str, title: str, description: str, files: list[str], criteria: list[str]
    ) -> TaskSizingDirective | None:
        messages = [
            {
                "role": "system",
                "content": (
                    "You are the Shop Foreman of a software factory. Size the work order: "
                    "S = one builder, M = two builders, L or XL = a full team of five. "
                    "Split it into exactly one subtask per builder with no file in two subtasks. "
                    "Reply with JSON only."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Task {task_id}: {title}\n\n{_clip(description, 4000)}\n\n"
                    f"Files: {', '.join(files) or '(not listed)'}\n"
                    "Acceptance criteria:\n" + ("\n".join(f"- {c}" for c in criteria) or "- (none given)")
                ),
            },
        ]
        reply = await self._chat(messages, json_schema=_SIZING_SCHEMA, temperature=0.0)
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
                        "acceptance_criteria": _str_list(item.get("acceptance_criteria")),
                        "allowed_files": _str_list(item.get("allowed_files")),
                    }
                )
            directive = self._build_directive(
                task_id, team_id, title, complexity, rationale, files, criteria, proposals
            )
        except (ValueError, TypeError, ValidationError) as exc:
            log.info("Shop Foreman: invalid Gemma sizing (%s); using heuristic", exc)
            return None
        self._save(task_id, "triage", _reply_tokens(reply, messages))
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
        """Normalise subtasks to one per builder and validate the directive."""
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
                target["allowed_files"] += extra["allowed_files"]
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
                        "acceptance_criteria": criteria[k::n] if criteria else [],
                        "allowed_files": part_files,
                    }
                )
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
                    acceptance_criteria=sub["acceptance_criteria"],
                    allowed_files=sub["allowed_files"],
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
            return ContextSlice(objective=_first_sentences(text), acceptance_criteria=list(criteria or []))
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
            reply = await self._chat(messages, temperature=0.0)
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
            reply = await self._chat(messages, json_schema=_PR_SCHEMA, temperature=0.2)
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

    # ---------------------------------------------------------------------- Q&A
    def _fallback_answer(self, text: str) -> str:
        summary = self.get_queue_summary()
        tasks = summary.get("tasks", {})
        busy = ", ".join(f"{k}: {v}" for k, v in tasks.items() if v) or "no tasks"
        velocity = self.get_token_velocity(15)
        bottlenecks = self.get_team_bottlenecks()["teams"]
        waits = sum(len(t["waiting_for_review"]) for t in bottlenecks.values())
        stalls = sum(len(t["stalled_tickets"]) for t in bottlenecks.values())
        return (
            "The Gemma Shop Foreman is offline, so here is the line status instead. "
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
        for round_no in range(MAX_TOOL_ROUNDS + 1):
            final_round = round_no == MAX_TOOL_ROUNDS
            if final_round:
                messages.append({"role": "system", "content": "Tool budget used up. Answer now with what you have."})
            reply = await self._chat(messages, tools=None if final_round else FOREMAN_TOOLS, temperature=0.1)
            if reply is None:
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
            answer = self._fallback_answer(text)
        else:
            self.last_source["answer_question"] = "gemma"
            self._save(_QA_TASK_ID, "triage", tokens)
        for chunk in self._chunks(answer):
            yield chunk

    # ---------------------------------------------------------------- anomalies
    def detect_anomalies(self) -> list[Anomaly]:
        """Deterministically detect every current anomaly, most urgent first.

        Order: LINE_JAM, TOKEN_VELOCITY_SPIKE, AGENT_STALLED,
        REVIEW_BOTTLENECK, CAPACITY_REBALANCE. Thresholds come from
        ``config.thresholds``.
        """
        th = self.config.thresholds
        now = self._now()
        found: list[Anomaly] = []
        tasks = {t["task_id"]: t for t in self.queue.all_tasks() if _jsonable(t["state"]) not in _TERMINAL}

        # LINE_JAM: one ticket failed its gates repeatedly (per ticket, like the retry policy) -> quarantine.
        for tid, task in tasks.items():
            per_ticket = self._ticket_gate_failures(tid)
            if not per_ticket:
                continue
            subtask_id, n = max(per_ticket.items(), key=lambda kv: (kv[1], kv[0]))
            if n < th.gate_fail_quarantine:
                continue
            ticket = next((t for t in self.queue.tickets_for_task(tid) if t.subtask_id == subtask_id), None)
            ticket_id = ticket.ticket_id if ticket is not None else f"{tid}:{subtask_id}"
            found.append(
                Anomaly(
                    EscalationCategory.LINE_JAM,
                    Severity.CRITICAL if n > th.gate_fail_quarantine else Severity.HIGH,
                    PrescribedAction.QUARANTINE_TASK,
                    team_id=task["team_id"],
                    task_id=tid,
                    agent_id=ticket.claimed_by if ticket is not None else None,
                    gate_failure_count=n,
                    action_parameters={"reason": f"ticket {ticket_id} failed its gates {n} times "
                                                 f"(limit {th.gate_fail_quarantine})",
                                       "gate_failure_count": n, "ticket_id": ticket_id},
                    extra_metrics={"threshold": th.gate_fail_quarantine, "ticket_id": ticket_id},
                    fallback_diagnosis=f"Ticket {ticket_id} failed its deterministic gates {n} times and is on its "
                                       "last attempt. Quarantine the task before more builder calls are spent.",
                )
            )

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
        raised within ``cooldown_s`` for the same target are skipped. Gemma
        only writes the ``diagnosis`` (<= 2 sentences, validated); otherwise a
        fixed diagnosis is used. Returns ``None`` when the line is healthy.
        """
        now = self._now()
        anomaly = None
        for candidate in self.detect_anomalies():
            last = self._raised.get(candidate.key)
            if last is not None and (now - last).total_seconds() < self.cooldown_s:
                continue
            anomaly = candidate
            break
        if anomaly is None:
            return None

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

        diagnosis = anomaly.fallback_diagnosis
        source = "heuristic"
        if self.gemma is not None:
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
            reply = await self._chat(messages, temperature=0.1)
            if reply is not None:
                written = self._valid_diagnosis(reply.text)
                if written is not None:
                    diagnosis, source = written, "gemma"
                    self._save(anomaly.task_id or _UNKNOWN_TASK, "triage", _reply_tokens(reply, messages))

        payload = {
            "alert_id": self._next_alert_id(now),
            "timestamp": now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "severity": anomaly.severity,
            "category": anomaly.category,
            "target_entity": target,
            "metrics_snapshot": metrics,
            "diagnosis": diagnosis,
            "prescribed_action": anomaly.action,
            "action_parameters": anomaly.action_parameters,
        }
        try:
            alert = ForemanEscalation.model_validate(payload)
        except ValidationError:
            payload["diagnosis"] = _first_sentences(anomaly.fallback_diagnosis, MAX_DIAGNOSIS_SENTENCES)
            source = "heuristic"
            alert = ForemanEscalation.model_validate(payload)
        self._raised[anomaly.key] = now
        self.last_source["evaluate_line_anomaly"] = source
        return alert


def _first_sentences(text: str, n: int = 2) -> str:
    """Keep the first ``n`` sentences of ``text`` (never empty)."""
    parts = re.split(r"(?<=[.!?])\s+", " ".join((text or "").split()))
    kept = " ".join(parts[:n]).strip()
    return kept or "Complete the subtask."
