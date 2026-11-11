"""Async builder and reviewer workers: the part of the Foundry that calls Claude.

A :class:`TeamPool` runs one team: five :class:`BuilderWorker` slots and one
:class:`ReviewerWorker`, all asyncio tasks in one process.

Builder loop (one slot)::

    wait_for_ticket(team, slot)        # no Claude call while waiting
      -> TaskAssignment (pruned ContextSlice from the context engine)
      -> Claude (limiter.slot + call_with_backoff), reply must hold one ```diff block
      -> parse_builder_reply -> BuilderSubmission (bad reply -> blocked)
      -> deterministic gates (short-circuit on the first failure)
           fail: foreman.condense_error_trace -> reopen ticket with a 1-line
                 fix hint (attempt + 1); attempt cap reached -> quarantine
           pass: Gemma pre-review (foreman.pre_review_diff, mode off|advise|gate)
                 gate: a confident send_back with a blocking issue reopens the
                       ticket once ("[pre-review] ..."); later submissions and
                       any Gemma fallback go on to the reviewer
                 then hand to the team's review inbox (with Gemma's notes)

Reviewer loop::

    review inbox -> diff-only prompt (criteria + unified diff + gate results)
      -> Claude -> parse_reviewer_reply (strict JSON ReviewerVerdict)
           APPROVED: ticket done, task complete when all tickets are
           REVISE:   reopen the ticket with the line directives (counts as a retry)
           REJECTED: quarantine the task

Dynamic concurrency comes from the queue: ``claim_ticket`` only hands a ticket
to a slot that the sizing directive allocated, so slots above the allocation
sleep in ``wait_for_ticket`` and never call Claude.

The workers only depend on small duck-typed interfaces (``QueueLike``,
``GatesLike`` ...), so the module can be tested with fakes.  The prompt
builders and reply parsers are pure functions so tests can assert exactly what
Claude receives.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import os
import re
import time
from pathlib import Path
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from foundry import claude_client as _claude_client
from foundry.gemma_excerpts import error_excerpt
from foundry.file_map import build_index, file_map
from foundry.gemma_notes import find_related, pitfall_notes
from foundry.piece_workspace import WorkspaceError, collect_changes, make_workspace, remove_workspace
from foundry.validation_gates import DiffApplyError, normalize_diff
from foundry.schemas import (
    MAX_RETRIES,
    MAX_RETRIES_LIMIT,
    SubtaskAllocation,
    BuilderSubmission,
    ClaudeResult,
    ContextSlice,
    DeterministicGateResult,
    ReviewerVerdict,
    TaskAssignment,
    TaskState,
    utc_now_iso,
)
from foundry.worker_helpers import (
    MAX_FIX_HINT_CHARS,
    _KW_CACHE as _KW_CACHE,
    _ONE_LINE_CHARS as _ONE_LINE_CHARS,
    _accepts_kw,
    _estimate_tokens,
    _fallback_condense,
    _fence_for as _fence_for,
    _get,
    _maybe_await,
    _normalize_path as _normalize_path,
    _one_line,
    _read_only_path,
    _read_only_touched as _read_only_touched,
    _state_value,
    _supported_kwargs,
)
from foundry.worker_prompts import (
    BUILDER_CONTRACT,
    DIFF_CONTRACT,
    EDIT_CONTRACT,
    EDITS_EXAMPLE as EDITS_EXAMPLE,
    MAX_ORDER_DETAILS_CHARS as MAX_ORDER_DETAILS_CHARS,
    REVIEWER_CONTRACT,
    _DEF_KW as _DEF_KW,
    _IDENT_RE,
    _PLAN_ITEM_RE as _PLAN_ITEM_RE,
    _PLAN_STOP_WORDS as _PLAN_STOP_WORDS,
    _ReadOnlyAssignment,
    _capped_order_text as _capped_order_text,
    _defines_any,
    _order_details_section as _order_details_section,
    _path_covered,
    _plan_checklist_section as _plan_checklist_section,
    build_builder_prompt,
    build_reviewer_prompt,
    plan_checklist as plan_checklist,
)
from foundry.worker_replies import (
    DEFAULT_HELP_AFTER,
    HELP_ANSWER_KV_PREFIX,
    HELP_PAUSED_KV_PREFIX,
    LAST_HINT_KV_PREFIX,
    MAX_EXTRA_SYMBOLS,
    MAX_HELP_AFTER,
    MAX_HELP_TEXT_CHARS,
    MISSING_INFO_KV_PREFIX,
    PreReviewBreaker,
    _BACKTICK_SYMBOL_RE as _BACKTICK_SYMBOL_RE,
    _BLOCKED_RE,
    _BODY_OF_RE as _BODY_OF_RE,
    _DIFF_BLOCK_RE as _DIFF_BLOCK_RE,
    _DIFF_OPEN_RE as _DIFF_OPEN_RE,
    _EDITS_BLOCK_RE as _EDITS_BLOCK_RE,
    _EDITS_OPEN_RE as _EDITS_OPEN_RE,
    _FILE_SUFFIXES as _FILE_SUFFIXES,
    _INFRA_ERROR_RE as _INFRA_ERROR_RE,
    _JSON_FENCE_RE as _JSON_FENCE_RE,
    _MISSING_INFO_PHRASES as _MISSING_INFO_PHRASES,
    _NOT_SYMBOLS as _NOT_SYMBOLS,
    _PRE_REVIEW_CHEAP_FALLBACKS,
    _WORK_JUDGEMENT_REASONS as _WORK_JUDGEMENT_REASONS,
    _is_missing_information,
    _parse_edits_reply as _parse_edits_reply,
    directives_to_hint,
    extract_diff_paths,
    extract_requested_symbols,
    format_pre_review_notes,
    is_infrastructure_error,
    parse_builder_reply,
    parse_reviewer_reply,
)

__all__ = [
    "DIFF_CONTRACT",
    "BUILDER_CONTRACT",
    "EDIT_CONTRACT",
    "REVIEWER_CONTRACT",
    "MAX_FIX_HINT_CHARS",
    "build_builder_prompt",
    "build_reviewer_prompt",
    "parse_builder_reply",
    "parse_reviewer_reply",
    "extract_diff_paths",
    "extract_requested_symbols",
    "directives_to_hint",
    "format_pre_review_notes",
    "PRE_REVIEW_MODES",
    "PreReviewBreaker",
    "is_infrastructure_error",
    "GateSummary",
    "ReviewItem",
    "BuilderWorker",
    "ReviewerWorker",
    "TeamPool",
]

log = logging.getLogger(__name__)

#: ``hook(task_id, agent_id, work, status=..., team_id=...)``: what one agent is doing now
#: (``BoardBridge.agent_status`` fits; workers never import the bridge).
ActivityHook = Callable[..., Any]

#: Gemma pre-review modes (``FoundryConfig.pre_review`` / ``FOUNDRY_PRE_REVIEW``).
PRE_REVIEW_MODES: tuple[str, ...] = ("off", "advise", "gate")
#: A ticket is sent back by the pre-review at most this many times; later submissions go to Claude.
PRE_REVIEW_MAX_SEND_BACKS = 1
#: Queue kv key prefix holding a ticket's pre-review send-back count (survives restarts).
PRE_REVIEW_KV_PREFIX = "pre_review_sent_back:"
#: Queue kv key prefix holding the symbols a ticket's builder asked to see in full.
EXTRA_SYMBOLS_KV_PREFIX = "extra_symbols:"
#: Queue kv key prefix holding the context level a ticket's latest slice was built at.
CONTEXT_LEVEL_USED_KV_PREFIX = "context_level_used:"
#: Widest context level (``ContextEngine.build_slice(level=...)``); retries step up to it.
MAX_CONTEXT_LEVEL = 3

_TERMINAL_STATES = {TaskState.COMPLETE.value, TaskState.QUARANTINED.value, TaskState.FAILED.value}


# ---------------------------------------------------------------------------
# Duck-typed collaborators
# ---------------------------------------------------------------------------


class ClaudeLike(Protocol):
    """What the workers need from a Claude client (see ``claude_client.ClaudeClient``)."""

    async def run(
        self, prompt: str, *, agent_id: str, role: Any, cwd: str | None = None, timeout: float = 600
    ) -> ClaudeResult: ...


class LimiterLike(Protocol):
    """What the workers need from ``rate_limiter.RateLimiter``."""

    def slot(self, estimated_tokens: int = 0) -> Any: ...

    async def call_with_backoff(self, fn: Callable[[], Awaitable[Any]], **kwargs: Any) -> Any: ...


class GatesLike(Protocol):
    """A gate runner: ``run(submission, assignment)`` returning a GateOutcome or result list.

    Plain callables with the same signature, and sync or async variants, are
    accepted too (see :meth:`BuilderWorker._run_gates`).
    """

    def run(self, submission: BuilderSubmission, assignment: TaskAssignment) -> Any: ...


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


#: Where builder prompts are saved unless ``FOUNDRY_PROMPT_DIR`` says otherwise.
_DEFAULT_PROMPT_DIR = Path(__file__).resolve().parent / "data" / "prompts"
#: Only this many saved prompts are kept; the oldest are deleted first.
_MAX_SAVED_PROMPTS = 200
_PROMPT_SAVE_OFF = frozenset({"0", "false", "no", "off"})


def _save_prompt(prompt: str, ticket_id: str, attempt: int) -> None:
    """Best effort: save a builder prompt for debugging; a failure here never breaks the build.

    ``FOUNDRY_SAVE_PROMPTS`` (``0``/``false``/``no``/``off``) turns it off and ``FOUNDRY_PROMPT_DIR``
    picks the directory.  Writes ``<ticket>_attempt<n>.txt`` and keeps the newest 200 ``.txt`` files.
    """
    try:
        if os.environ.get("FOUNDRY_SAVE_PROMPTS", "").strip().lower() in _PROMPT_SAVE_OFF:
            return
        directory = Path(os.environ.get("FOUNDRY_PROMPT_DIR") or _DEFAULT_PROMPT_DIR)
        directory.mkdir(parents=True, exist_ok=True)
        safe_id = re.sub(r"[^A-Za-z0-9._-]+", "_", str(ticket_id)).strip("._")[:100] or "ticket"
        path = directory / f"{safe_id}_attempt{attempt}.txt"
        path.write_text(prompt, encoding="utf-8", errors="replace")
        others = [p for p in directory.glob("*.txt") if p != path]
        others.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        for old in others[_MAX_SAVED_PROMPTS - 1:]:
            old.unlink(missing_ok=True)
    except Exception as exc:
        log.warning("could not save the prompt of ticket %s: %s", ticket_id, exc)


# ---------------------------------------------------------------------------
# Data passed between builders and the reviewer
# ---------------------------------------------------------------------------


@dataclass
class GateSummary:
    """Normalized outcome of the deterministic gates for one submission."""

    passed: bool
    results: list[DeterministicGateResult] = field(default_factory=list)
    first_failure: DeterministicGateResult | None = None
    summary: str | None = None


@dataclass
class ReviewItem:
    """A gate-passing submission waiting in a team's review inbox."""

    ticket: Any
    assignment: TaskAssignment
    submission: BuilderSubmission
    gate_results: list[DeterministicGateResult]
    enqueued_at: float = field(default_factory=time.monotonic)
    #: Pre-diff contents of the touched files (restored on REVISE/REJECTED).
    snapshot: dict[str, str | None] | None = None
    workspace: Path | None = None
    #: Work-order title/description (the reviewer prompt uses the description).
    cache_meta: dict[str, str] = field(default_factory=dict)
    #: Gemma pre-review notes for the reviewer prompt (advisory; None = none).
    pre_review_notes: str | None = None

    @property
    def ticket_id(self) -> Any:
        """The queue ticket this submission answers."""
        return _get(self.ticket, "ticket_id")


# ---------------------------------------------------------------------------
# Shared worker plumbing
# ---------------------------------------------------------------------------


class _WorkerBase:
    """Claude calls, telemetry and queue helpers shared by builders and reviewers."""

    agent_id: str
    role: str

    def __init__(
        self,
        team_id: str,
        queue: Any,
        client: ClaudeLike,
        limiter: LimiterLike | None,
        telemetry: Any,
        *,
        foreman: Any = None,
        claude_timeout: float = 600,
        cwd: str | None = None,
        backoff_attempts: int = 5,
        limit_cooldown_s: float = 1200.0,
        infra_backoff_s: float = 5.0,
        infra_failure_cap: int = 3,
        base_for: Callable[[str], Any] | None = None,
    ) -> None:
        self.team_id = team_id
        self.queue = queue
        self.client = client
        self.limiter = limiter
        self.telemetry = telemetry
        self.foreman = foreman
        self.claude_timeout = claude_timeout
        self.cwd = cwd
        #: ``base_for(task_id) -> Path | None`` (sync or async): the clean snapshot of the
        #: publish branch a task is built from (``FloorManager.task_base``); None (or a None
        #: result) = the user's folder (``cwd`` / the gates' base / the engine's repo).
        self.base_for = base_for
        #: task id -> its snapshot directory, as last resolved by this worker.
        self._bases: dict[str, Path] = {}
        self.backoff_attempts = backoff_attempts
        self.limit_cooldown_s = float(limit_cooldown_s)
        self.infra_backoff_s = float(infra_backoff_s)
        self.claude_calls = 0
        #: Consecutive infrastructure failures (reset by a successful Claude call).
        self.infra_failures = 0
        #: Infrastructure failures seen in total (usage limits, timeouts, CLI missing ...).
        self.infra_errors: list[str] = []
        #: Consecutive counted failures before a ticket's task is quarantined (E1).
        self.infra_failure_cap = int(infra_failure_cap)
        self._local_infra: dict[str, int] = {}
        #: Told what this agent is doing (optional, see :data:`ActivityHook`).
        self.on_activity: ActivityHook | None = None
        #: ``on_ticket_change(task_id, team_id=...)``: a ticket of the task changed state.
        self.on_ticket_change: Callable[..., Any] | None = None

    @property
    def max_retries(self) -> int:
        """The queue's retry cap (``FoundryConfig.max_retries``); MAX_RETRIES if it has none."""
        cap = getattr(self.queue, "max_retries", MAX_RETRIES)
        if not isinstance(cap, int) or isinstance(cap, bool):
            cap = MAX_RETRIES  # a fake/old queue without a real cap
        return max(1, min(cap, MAX_RETRIES_LIMIT))

    # -- telemetry (never lets a metrics error break the line) ---------------
    def _tm(self, method: str, *args: Any, **kwargs: Any) -> None:
        fn = getattr(self.telemetry, method, None) if self.telemetry is not None else None
        if fn is None:
            return
        try:
            fn(*args, **_supported_kwargs(fn, kwargs))
        except Exception:  # pragma: no cover - defensive
            log.exception("telemetry.%s failed", method)

    # -- queue helpers --------------------------------------------------------
    def _event(self, kind: str, task_id: str, payload: dict[str, Any]) -> None:
        fn = getattr(self.queue, "record_event", None)
        if fn is None:
            return
        try:
            fn(kind, task_id, payload)
        except Exception:  # pragma: no cover - defensive
            log.exception("record_event(%s) failed", kind)

    def _activity(self, task_id: Any, work: str, *, status: str = "working") -> None:
        """Tell ``on_activity`` what this agent is doing; reporting must never break the work."""
        hook = self.on_activity
        if hook is None or not task_id:
            return
        try:
            hook(str(task_id), self.agent_id, work, status=status, team_id=self.team_id)
        except Exception:  # pragma: no cover - defensive
            log.exception("on_activity hook failed")

    def _ticket_changed(self, task_id: Any) -> None:
        """Tell ``on_ticket_change`` a ticket changed state; reporting must never break the work."""
        hook = self.on_ticket_change
        if hook is None or not task_id:
            return
        try:
            hook(str(task_id), team_id=self.team_id)
        except Exception:
            log.exception("on_ticket_change hook failed")

    def _task_state(self, task_id: str) -> str:
        getter = getattr(self.queue, "get_task", None)
        if getter is None:
            return ""
        try:
            return _state_value(_get(getter(task_id), "state"))
        except Exception:
            return ""

    def _set_state(self, task_id: str, state: TaskState, note: str = "") -> bool:
        """Best-effort state change: illegal transitions (shared tasks) are ignored."""
        fn = getattr(self.queue, "set_state", None)
        if fn is None:
            return False
        if self._task_state(task_id) == state.value:
            return True
        try:
            fn(task_id, state, note)
            return True
        except (ValueError, KeyError) as exc:
            log.debug("set_state(%s, %s) skipped: %s", task_id, state.value, exc)
            return False

    def _quarantine(self, task_id: str, reason: str) -> None:
        if self._task_state(task_id) == TaskState.QUARANTINED.value:
            return
        try:
            self.queue.quarantine(task_id, reason)
        except (ValueError, KeyError) as exc:  # already terminal
            log.debug("quarantine(%s) skipped: %s", task_id, exc)

    def _still_owned(self, ticket: Any) -> bool:
        """False when someone else already reopened/finished the ticket (e.g. RESET_BUILDER_CONTEXT)."""
        getter = getattr(self.queue, "get_ticket", None)
        if getter is None:
            return True
        try:
            current = getter(_get(ticket, "ticket_id"))
        except Exception:
            return True
        if current is None:
            return False
        if _get(current, "status") != "claimed":
            return False
        if int(_get(current, "attempt", 0) or 0) != int(_get(ticket, "attempt", 0) or 0):
            return False
        # the same claim, not a later one (an undo/retry/reset can reopen a ticket with the
        # same attempt and another builder claim it): compare the claim's time and holder
        for key in ("claimed_at", "claimed_by"):
            mine = _get(ticket, key, None)
            if mine is not None and _get(current, key, None) != mine:
                return False
        return True

    def _send_back(self, ticket: Any, hint: str, *, reason: str, limit: int | None = None) -> str:
        """Reopen ``ticket`` with ``hint`` (attempt + 1), pause it for help or quarantine at the cap.

        ``limit``: longest hint stored (None = :data:`MAX_FIX_HINT_CHARS`).

        Returns ``"retry"``, ``"awaiting_help"`` (parked until the Floor Manager
        answers, no attempt spent), ``"quarantined"`` or ``"superseded"`` (the
        ticket was already reopened or closed by someone else, so nothing is done).
        """
        task_id = _get(ticket, "task_id")
        ticket_id = _get(ticket, "ticket_id")
        attempt = int(_get(ticket, "attempt", 0) or 0)
        cap = MAX_FIX_HINT_CHARS if limit is None else limit
        hint = hint if len(hint) <= cap else hint[: cap - 3] + "..."
        if not self._still_owned(ticket):
            self._event("superseded", task_id, {"ticket_id": ticket_id, "agent_id": self.agent_id, "reason": reason})
            return "superseded"
        request = self._help_request(ticket_id, attempt, hint, reason)
        if request is not None and self._pause_for_help(ticket_id, request):
            self._kv_write(f"{HELP_PAUSED_KV_PREFIX}{ticket_id}", True)
            self._event("help_requested", task_id,
                        {"ticket_id": ticket_id, "agent_id": self.agent_id, "reason": reason, **request})
            self._release_later(ticket)
            self._ticket_changed(task_id)
            return "awaiting_help"
        # The queue enforces the cap: past queue.max_retries it quarantines and returns False.
        try:
            ok: Any = self.queue.reopen_ticket(ticket_id, hint)
        except (ValueError, KeyError) as exc:
            log.debug("reopen_ticket(%s) refused: %s", ticket_id, exc)
            ok = False
        if ok:
            self._tm("record_retry", task_id)
            self._event("retry", task_id, {"ticket_id": ticket_id, "attempt": attempt + 1, "reason": reason, "fix_hint": hint})
            self._release_later(ticket)
            self._ticket_changed(task_id)
            return "retry"
        self._quarantine(task_id, f"retry limit reached ({reason}): {_one_line(hint)}")
        self._event("quarantined", task_id, {"ticket_id": ticket_id, "attempt": attempt, "reason": reason})
        self._ticket_changed(task_id)
        return "quarantined"

    @property
    def help_after(self) -> int:
        """Missing-information failures in a row before pausing (FOUNDRY_HELP_AFTER; 0 disables)."""
        configured = getattr(getattr(self, "config", None), "help_after", None)
        if isinstance(configured, int) and not isinstance(configured, bool):
            value = configured
        else:
            try:
                value = int(os.environ.get("FOUNDRY_HELP_AFTER", DEFAULT_HELP_AFTER))
            except ValueError:
                value = DEFAULT_HELP_AFTER
        return max(0, min(value, MAX_HELP_AFTER))

    def _kv_read(self, key: str) -> Any:
        """The queue's value under ``key``; None when absent or the queue has no kv store."""
        fn = getattr(self.queue, "kv_get", None)
        if not callable(fn):
            return None
        try:
            return fn(key)
        except Exception:  # bookkeeping must never break a send-back
            log.exception("kv_get(%s) failed", key)
            return None

    def _kv_write(self, key: str, value: Any) -> None:
        """Store ``value`` under ``key`` in the queue (best effort)."""
        fn = getattr(self.queue, "kv_set", None)
        if not callable(fn):
            return
        try:
            fn(key, value)
        except Exception:  # bookkeeping must never break a send-back
            log.exception("kv_set(%s) failed", key)

    def _help_request(self, ticket_id: Any, attempt: int, hint: str, reason: str) -> dict[str, Any] | None:
        """Record this failure; the help request when the ticket should pause, else None."""
        if not callable(getattr(self.queue, "pause_for_help", None)) or self.help_after <= 0:
            return None
        hint_key = f"{LAST_HINT_KV_PREFIX}{ticket_id}"
        count_key = f"{MISSING_INFO_KV_PREFIX}{ticket_id}"
        previous_hint = self._kv_read(hint_key)
        self._kv_write(hint_key, hint)
        if not _is_missing_information(reason, hint):
            self._kv_write(count_key, 0)
            return None
        previous_count = self._kv_read(count_key)
        count = (previous_count if isinstance(previous_count, int) else 0) + 1
        self._kv_write(count_key, count)
        if count < self.help_after or attempt + 1 > self.max_retries:
            return None
        if self._kv_read(f"{HELP_PAUSED_KV_PREFIX}{ticket_id}"):
            return None
        hints = [previous_hint, hint] if isinstance(previous_hint, str) and previous_hint else [hint]
        return {
            "question": hint[:MAX_HELP_TEXT_CHARS],
            "context": "\n".join(hints)[:MAX_HELP_TEXT_CHARS],
            "options": [],
            "attempts_used": attempt + 1,
            "attempts_left": self.max_retries - attempt,
        }

    def _pause_for_help(self, ticket_id: Any, request: dict[str, Any]) -> bool:
        """``queue.pause_for_help``; False when the queue refuses, so the normal send-back runs."""
        try:
            return bool(self.queue.pause_for_help(ticket_id, request))
        except (ValueError, KeyError) as exc:
            log.debug("pause_for_help(%s) refused: %s", ticket_id, exc)
            return False

    def _help_answer(self, ticket: Any) -> dict[str, Any]:
        """The Floor Manager's stored answer for ``ticket`` (empty when there is none)."""
        answer = self._kv_read(f"{HELP_ANSWER_KV_PREFIX}{_get(ticket, 'ticket_id')}")
        return answer if isinstance(answer, dict) else {}

    def _release_later(self, ticket: Any) -> list[str]:
        """An earlier ticket was reopened: release later claimed tickets built on it.

        Later tickets of the same task that are claimed (building or in
        review) and not ``parallel_ok`` were stacked on code that is about to
        change, so they go back to ``open`` with their attempt unchanged
        (``release_ticket``); their in-flight build/review is then discarded
        (no longer owned).  Uses ``queue.release_later_tickets(ticket_id)``
        when the queue has it.  Returns the released ticket ids.
        """
        ticket_id = str(_get(ticket, "ticket_id"))
        task_id = str(_get(ticket, "task_id"))
        reason = f"earlier ticket {ticket_id} reopened"
        fn = getattr(self.queue, "release_later_tickets", None)
        try:
            if callable(fn):
                released = [str(t) for t in (fn(ticket_id) or [])]
            else:
                released = self._release_later_fallback(ticket_id, task_id, reason)
        except Exception:  # never let this break a send-back
            log.exception("%s: releasing tickets after %s failed", getattr(self, "agent_id", "?"), ticket_id)
            return []
        if released:
            self._event("released_stale", task_id, {"after": ticket_id, "tickets": released})
        return released

    def _release_later_fallback(self, ticket_id: str, task_id: str, reason: str) -> list[str]:
        lister = getattr(self.queue, "tickets_for_task", None)
        release = getattr(self.queue, "release_ticket", None)
        if lister is None or release is None:
            return []
        tickets = list(lister(task_id) or [])
        tickets.sort(key=lambda t: int(_get(t, "order_index", 0) or 0))  # stable: slot order within one index
        ids = [str(_get(t, "ticket_id")) for t in tickets]
        if ticket_id not in ids:
            return []
        mine = tickets[ids.index(ticket_id)]
        my_order = int(_get(mine, "order_index", 0) or 0)
        out: list[str] = []
        for t in tickets[ids.index(ticket_id) + 1:]:
            if int(_get(t, "order_index", 0) or 0) <= my_order or _get(t, "parallel_ok", True):
                continue  # same position, or independent of earlier work
            if str(_get(t, "status")) == "claimed" and release(str(_get(t, "ticket_id")), reason):
                out.append(str(_get(t, "ticket_id")))
        return out

    # -- base snapshot ----------------------------------------------------------
    async def _resolve_base(self, task_id: Any) -> Path | None:
        """Ask ``base_for`` for the task's snapshot (None: build from the user's folder)."""
        if self.base_for is None or not task_id:
            return None
        try:
            got = await _maybe_await(self.base_for(str(task_id)))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if _is_counted_infra(exc):  # BaseUnavailable: snapshots are on, never use the user's folder
                raise
            log.exception("%s: could not get the base snapshot of %s", self.agent_id, task_id)
            got = None
        if not got:
            self._bases.pop(str(task_id), None)
            return None
        path = Path(got)
        self._bases[str(task_id)] = path
        return path

    async def _claude_cwd(self, task_id: Any) -> str | None:
        """Claude reads the task's snapshot when it has one, else ``cwd`` (it never writes)."""
        if self.base_for is None:
            return self.cwd
        path = self._bases.get(str(task_id))
        if path is None or not path.is_dir():
            path = await self._resolve_base(task_id)
        return str(path) if path is not None else self.cwd

    # -- Claude ---------------------------------------------------------------
    async def _call_claude(
        self, prompt: str, *, task_id: str, cwd: str | None = None, client: ClaudeLike | None = None
    ) -> ClaudeResult:
        """One Claude call inside ``limiter.slot`` wrapped by ``call_with_backoff``.

        Records tokens per call; raises whatever the client raises once backoff
        gives up. ``cwd`` (edit mode: the piece workspace) replaces the task's
        snapshot / ``self.cwd`` as Claude's working directory. ``client`` (builder
        retries) is called instead of ``self.client`` when given.
        """
        estimate = _estimate_tokens(prompt)
        if cwd is None:
            cwd = await self._claude_cwd(task_id)
        caller = client if client is not None else self.client

        async def once() -> ClaudeResult:
            if self.limiter is None:
                self.claude_calls += 1
                return await caller.run(
                    prompt, agent_id=self.agent_id, role=self.role, cwd=cwd, timeout=self.claude_timeout
                )
            async with self.limiter.slot(estimate) as handle:
                self.claude_calls += 1
                result = await caller.run(
                    prompt, agent_id=self.agent_id, role=self.role, cwd=cwd, timeout=self.claude_timeout
                )
                # D11: charge uncached input + output only; cache reads are free for TPM.
                actual = result.input_tokens + result.output_tokens
                recorder = getattr(handle, "record_actual", None) or getattr(self.limiter, "record_actual", None)
                if recorder is not None and actual:
                    recorder(actual)
                return result

        if self.limiter is None:
            result = await once()
        else:
            result = await self.limiter.call_with_backoff(once, max_attempts=self.backoff_attempts)
        self._tm(
            "record_tokens", task_id, self.agent_id, self.role, result.input_tokens, result.output_tokens,
            cache_read_tokens=int(getattr(result, "cache_read_tokens", 0) or 0),
        )
        return result

    # -- infrastructure failures (D1/E1) --------------------------------------
    @staticmethod
    def _is_account_wide(exc: BaseException) -> bool:
        """Usage/rate limit, logged out, CLI missing: clears by itself or needs the user."""
        kinds = tuple(
            c for c in (
                getattr(_claude_client, "ClaudeRateLimited", None),
                getattr(_claude_client, "ClaudeAuthError", None),
                getattr(_claude_client, "ClaudeNotFound", None),
            ) if c is not None
        )
        return bool(kinds) and isinstance(exc, kinds)

    async def _handle_infra_error(self, exc: BaseException) -> float:
        """Log, pause and compute backoff for an infrastructure failure.

        Account-wide errors pause the whole limiter (``pause_until``) for
        ``retry_after`` or ``limit_cooldown_s`` and return 0.  Other failures
        (timeouts, CLI crashes, scratch-copy errors) back off this worker only,
        exponentially up to ``limit_cooldown_s``; the returned seconds are that
        backoff.  Counting toward the per-ticket cap is :meth:`_count_infra`.
        """
        self.infra_failures += 1
        excerpt = _one_line(str(exc) or type(exc).__name__, 160)
        self.infra_errors.append(f"{type(exc).__name__}: {excerpt}")
        log.warning("%s infrastructure error %s: %s", self.agent_id, type(exc).__name__, excerpt)
        if self._is_account_wide(exc):
            self._remember_account_error(exc, excerpt)
            retry_after = getattr(exc, "retry_after", None)
            seconds = float(retry_after) if retry_after else self.limit_cooldown_s
            pause = getattr(self.limiter, "pause_until", None)
            if pause is not None:
                pause(seconds=seconds)
                return 0.0
            return seconds
        return min(self.limit_cooldown_s, self.infra_backoff_s * 2 ** (self.infra_failures - 1))

    def _remember_account_error(self, exc: BaseException, excerpt: str) -> None:
        """G5: persist the last account-wide error (type + <= 160-char message, never the prompt)."""
        fn = getattr(self.queue, "kv_set", None)
        if fn is None:
            return
        try:
            fn("last_account_error", {"type": type(exc).__name__, "message": excerpt[:160], "at": utc_now_iso()})
        except Exception:  # pragma: no cover - defensive
            log.exception("kv_set(last_account_error) failed")

    def _count_infra(self, ticket: Any, exc: BaseException) -> bool:
        """Count a non-account-wide failure on ``ticket``; quarantine at the cap.

        Returns True when the task was quarantined (reason ``"infrastructure: ..."``,
        no attempt spent).  Uses ``queue.record_infra_failure`` (consecutive
        count per ticket, shared by builder and reviewer); a worker-local
        counter stands in for queues without it.
        """
        ticket_id = _get(ticket, "ticket_id")
        task_id = _get(ticket, "task_id")
        short = _one_line(str(exc) or type(exc).__name__, 120)
        fn = getattr(self.queue, "record_infra_failure", None)
        if fn is not None:
            try:
                count = int(fn(ticket_id, short))
            except (KeyError, ValueError) as err:
                log.debug("record_infra_failure refused: %s", err)
                return False
        else:
            count = self._local_infra.get(str(ticket_id), 0) + 1
            self._local_infra[str(ticket_id)] = count
        if count < self.infra_failure_cap:
            return False
        log.warning("%s: %d consecutive infrastructure failures on %s; quarantining %s",
                    self.agent_id, count, ticket_id, task_id)
        self._quarantine(task_id, f"infrastructure: {short}")
        self._event("quarantined", task_id, {"ticket_id": ticket_id, "reason": "infrastructure", "error": short,
                                             "infra_failures": count})
        return True

    def _reset_infra(self, ticket: Any) -> None:
        """A parsed reply arrived: the ticket's consecutive infrastructure count starts over."""
        self.infra_failures = 0
        ticket_id = _get(ticket, "ticket_id")
        self._local_infra.pop(str(ticket_id), None)
        fn = getattr(self.queue, "reset_infra_failures", None)
        if fn is not None:
            try:
                fn(ticket_id)
            except (KeyError, ValueError):
                pass

    async def _infra_failure(self, ticket: Any, exc: BaseException) -> tuple[str, float]:
        """Shared E1 policy; returns ``("paused"|"counted"|"quarantined", backoff_seconds)``."""
        backoff = await self._handle_infra_error(exc)
        if self._is_account_wide(exc):
            return "paused", backoff
        if self._count_infra(ticket, exc):
            return "quarantined", 0.0
        return "counted", backoff

    def _release(self, ticket: Any, reason: str) -> bool:
        """Give the ticket back without spending an attempt (``queue.release_ticket``)."""
        fn = getattr(self.queue, "release_ticket", None)
        if fn is None:
            return False
        try:
            ok = bool(fn(_get(ticket, "ticket_id"), reason))
        except (KeyError, ValueError) as exc:
            log.debug("release_ticket refused: %s", exc)
            ok = False
        self._event("ticket_released", _get(ticket, "task_id"),
                    {"ticket_id": _get(ticket, "ticket_id"), "agent_id": self.agent_id, "reason": reason})
        return ok

    def _pause_remaining(self) -> float:
        fn = getattr(self.limiter, "pause_remaining", None)
        try:
            return float(fn()) if fn is not None else 0.0
        except Exception:  # pragma: no cover - defensive
            return 0.0

    async def _wait(self, seconds: float, stop: asyncio.Event | None) -> None:
        """Sleep ``seconds`` (also any limiter pause), returning early when ``stop`` is set."""
        seconds = max(seconds, self._pause_remaining())
        if seconds <= 0:
            return
        if stop is None:
            await asyncio.sleep(seconds)
            return
        try:
            await asyncio.wait_for(stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    def _set_in_review(self, ticket: Any, flag: bool) -> None:
        fn = getattr(self.queue, "set_in_review", None)
        if fn is None:
            return
        try:
            fn(_get(ticket, "ticket_id"), flag)
        except (KeyError, ValueError) as exc:
            log.debug("set_in_review skipped: %s", exc)

    async def _condense(self, trace: str, fallback: str | None = None, *, task_id: str | None = None) -> str:
        """1-line hint via ``foreman.condense_error_trace`` (sync or async).

        Falls back to ``fallback`` (e.g. ``GateOutcome.summary()``) and then to
        a local deterministic condenser when the foreman is absent or fails
        (``GemmaUnavailable`` included).
        """
        fn = getattr(self.foreman, "condense_error_trace", None) if self.foreman is not None else None
        if fn is not None and trace.strip():
            try:
                raw = fn(trace, **_supported_kwargs(fn, {"task_id": task_id}))
                hint = await _maybe_await(raw)
                if isinstance(hint, str) and hint.strip():
                    return _one_line(hint, MAX_FIX_HINT_CHARS)
            except Exception:
                log.warning("foreman.condense_error_trace failed; using fallback", exc_info=True)
        if fallback and fallback.strip():
            return _one_line(fallback)
        return _fallback_condense(trace)


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


class BuilderWorker(_WorkerBase):
    """One builder slot (``<team>-<slot>``) pulling tickets for its team.

    Args:
        team_id: team this slot belongs to.
        slot: 1-based builder slot; only tickets with this slot are claimed.
        queue: ``FoundryQueue`` (``wait_for_ticket``, ``reopen_ticket``,
            ``complete_ticket``, ``set_state``, ``quarantine``, ``get_task``,
            ``record_event``).
        client: Claude client (``ClaudeClient`` protocol).
        limiter: ``RateLimiter``; every Claude call runs in ``limiter.slot``
            inside ``limiter.call_with_backoff``.  ``None`` disables limiting.
        telemetry: ``Telemetry`` (tokens, durations, gate failures, retries).
        context_engine: builds the pruned ``ContextSlice`` for a subtask.
        gates: gate runner (see :class:`GatesLike`); ``None`` passes everything.
        cache: unused; kept for call compatibility.
        foreman: ``ShopForeman`` (``condense_error_trace``); ``None`` uses a
            deterministic local condenser.
        review_inbox: the team's review queue (``TeamPool`` shares one).
        poll: ticket polling interval passed to ``wait_for_ticket``.
        trust_cache: unused; kept for call compatibility.
        limit_cooldown_s: pause used for a usage limit with no parseable reset
            time (``FoundryConfig.limit_cooldown_s``).
        infra_backoff_s: first per-worker backoff after a non-limit
            infrastructure failure (doubles each time, capped at the cooldown).
        on_state: callback ``(worker)`` fired when the worker goes busy/idle.
    """

    role = "builder"

    def __init__(
        self,
        team_id: str,
        slot: int,
        queue: Any,
        client: ClaudeLike,
        limiter: LimiterLike | None,
        telemetry: Any,
        context_engine: Any,
        gates: Any,
        cache: Any,
        foreman: Any,
        *,
        review_inbox: asyncio.Queue[ReviewItem] | None = None,
        poll: float = 0.05,
        trust_cache: bool = False,
        on_state: Callable[[BuilderWorker], None] | None = None,
        on_activity: ActivityHook | None = None,
        on_task_complete: Callable[[str], Any] | None = None,
        workspace: str | Path | None = None,
        repo_root: str | Path | None = None,
        claude_timeout: float = 600,
        cwd: str | None = None,
        backoff_attempts: int = 5,
        limit_cooldown_s: float = 1200.0,
        infra_backoff_s: float = 5.0,
        infra_failure_cap: int = 3,
        pre_review: str = "gate",
        pre_review_min_confidence: float = 0.7,
        pre_review_breaker: PreReviewBreaker | None = None,
        base_for: Callable[[str], Any] | None = None,
        base_lease: Callable[[str], Any] | None = None,
    ) -> None:
        super().__init__(
            team_id,
            queue,
            client,
            limiter,
            telemetry,
            foreman=foreman,
            claude_timeout=claude_timeout,
            cwd=cwd,
            backoff_attempts=backoff_attempts,
            limit_cooldown_s=limit_cooldown_s,
            infra_backoff_s=infra_backoff_s,
            infra_failure_cap=infra_failure_cap,
            base_for=base_for,
        )
        mode = str(pre_review or "off").strip().lower()
        if mode not in PRE_REVIEW_MODES:
            raise ValueError(f"pre_review must be one of {PRE_REVIEW_MODES}, got {pre_review!r}")
        #: Gemma pre-review mode: off | advise | gate (``FoundryConfig.pre_review``).
        self.pre_review = mode
        self.pre_review_min_confidence = float(pre_review_min_confidence)
        #: Stand-in for queues without ``kv_get``/``kv_set`` (per-ticket send-back count).
        self._local_pre_review: dict[str, int] = {}
        #: Stand-in for queues without kv: per-ticket symbols requested in BLOCKED notes.
        self._local_symbols: dict[str, list[str]] = {}
        #: ``read_files_for(task_id) -> [path, ...]``: the task's read-only files (set by ``build_factory``).
        self.read_files_for: Callable[[str], Sequence[str]] | None = None
        #: Skips the pre-review for a while after repeated Gemma errors (TeamPool shares one).
        self.pre_review_breaker = pre_review_breaker if pre_review_breaker is not None else PreReviewBreaker()
        self.workspace: Path | None = Path(workspace) if workspace is not None else None
        #: Only tickets of tasks for this repo are claimed (G1); None = any repo.
        self.repo_root: str | None = str(repo_root) if repo_root is not None else None  # queue normalizes
        if self.workspace is None and (hasattr(gates, "validate_async") or hasattr(gates, "validate")):
            raise ValueError("ValidationGates applies diffs into a workspace: pass workspace=")
        if slot < 1:
            raise ValueError("builder slots are 1-based")
        self.slot = slot
        self.agent_id = f"{team_id}-{slot}"
        self.context_engine = context_engine
        self.gates = gates
        self.cache = cache
        self.review_inbox: asyncio.Queue[ReviewItem] = review_inbox if review_inbox is not None else asyncio.Queue()
        self.poll = poll
        self.trust_cache = trust_cache
        self.on_state = on_state
        self.on_activity = on_activity
        self.on_task_complete = on_task_complete
        self.busy = False
        self.current_ticket: Any = None
        #: Per-worker scratch (last prompt, last reply ...); cleared by reset_context().
        self.scratch: dict[str, Any] = {}
        #: The base snapshot of the ticket being processed (None: the user's folder).
        self._base: Path | None = None
        #: ``base_lease(task_id)`` -> async context manager yielding the task's snapshot and keeping
        #: it from being pruned while the ticket runs (``FloorManager.base_lease``); else ``base_for``.
        self.base_lease = base_lease
        #: Outcomes of processed tickets, in order ("review", "retry", "quarantined", "cached", ...).
        self.outcomes: list[str] = []
        self._backoff_s = 0.0
        #: True while this slot serves as a promoted peer reviewer (M1: claims no build tickets).
        self.promoted = False
        #: ``FoundryConfig`` (``builder_mode``, ``db_path``); None = diff mode.
        self.config: Any = None
        #: The client attempts after the first call (set by ``TeamPool``); None = ``self.client``.
        self.retry_client: ClaudeLike | None = None
        #: Tickets already logged as built in diff mode although ``builder_mode`` is edit.
        self._edit_skip_logged: set[str] = set()

    # -- lifecycle ------------------------------------------------------------
    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        if self.on_state is not None:
            try:
                self.on_state(self)
            except Exception:  # pragma: no cover - defensive
                log.exception("on_state callback failed")

    def reset_context(self) -> None:
        """Forget scratch state (RESET_BUILDER_CONTEXT); the next ticket starts fresh."""
        self.scratch.clear()

    async def run(self, stop: asyncio.Event) -> None:
        """Loop until ``stop`` is set: wait for a ticket (no Claude call), process it."""
        while not stop.is_set():
            # A usage-limit pause holds every slot before it claims anything (no stalled claims).
            await self._wait(0.0, stop)
            if stop.is_set():
                break
            if self.promoted:
                await self._wait(self.poll, stop)
                continue
            ticket = await self._next_ticket(stop)
            if ticket is None:
                if stop.is_set():
                    break
                continue
            if self._pause_remaining() > 0 or self.promoted:
                self._release(ticket, "dispatch paused" if not self.promoted else "slot promoted to reviewer")
                continue
            self.current_ticket = ticket
            self._set_busy(True)
            try:
                self.outcomes.append(await self.process_ticket(ticket))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("%s crashed on ticket %s", self.agent_id, _get(ticket, "ticket_id"))
                self.outcomes.append(self._send_back(ticket, _one_line(f"worker error: {exc}"), reason="worker_error"))
            finally:
                self.current_ticket = None
                self._set_busy(False)
            if self._backoff_s > 0:
                wait, self._backoff_s = self._backoff_s, 0.0
                await self._wait(wait, stop)

    async def _next_ticket(self, stop: asyncio.Event) -> Any:
        """Wait for this slot's next ticket (no Claude call), scoped to ``repo_root`` when set (G1)."""
        if self.repo_root is None:
            return await self.queue.wait_for_ticket(self.team_id, self.slot, poll=self.poll, stop=stop)
        waiter = self.queue.wait_for_ticket
        if _accepts_kw(waiter, "repo_root"):
            return await waiter(self.team_id, self.slot, poll=self.poll, stop=stop, repo_root=self.repo_root)
        claim = self.queue.claim_ticket
        if not _accepts_kw(claim, "repo_root"):
            raise TypeError("this queue cannot scope tickets to a repo (claim_ticket has no repo_root)")
        while not stop.is_set():
            ticket = claim(self.team_id, self.slot, repo_root=self.repo_root)
            if ticket is not None:
                return ticket
            await self._wait(self.poll, stop)
        return None

    # -- assignment -------------------------------------------------------------
    def _subtask_info(self, ticket: Any) -> dict[str, Any]:
        """Title, criteria and allowed files for the ticket's subtask."""
        info = {
            "title": _get(ticket, "title"),
            "acceptance_criteria": _get(ticket, "acceptance_criteria"),
            "allowed_files": _get(ticket, "allowed_files"),
        }
        if info["acceptance_criteria"] is None or info["allowed_files"] is None:
            sub = self._find_subtask(ticket)
            for key in info:
                if info[key] is None:
                    info[key] = _get(sub, key)
        info["title"] = info["title"] or str(_get(ticket, "subtask_id"))
        info["acceptance_criteria"] = list(info["acceptance_criteria"] or [])
        info["allowed_files"] = list(info["allowed_files"] or [])
        allowed = info["allowed_files"]
        focus = [f for f in (_get(ticket, "focus_files") or []) if f in allowed]
        info["focus_files"] = focus or list(allowed)
        info["read_files"] = self._read_files(ticket, allowed)
        return info

    def _find_subtask(self, ticket: Any) -> Any:
        """The ticket's subtask from the queue or its task's allocation; None if not found."""
        for name in ("get_subtask", "subtask_for"):
            fn = getattr(self.queue, name, None)
            if fn is not None:
                try:
                    sub = fn(_get(ticket, "task_id"), _get(ticket, "subtask_id"))
                except (KeyError, ValueError):
                    sub = None
                if sub is not None:
                    return sub
                break
        task = getattr(self.queue, "get_task", lambda _t: None)(_get(ticket, "task_id"))
        subs = _get(task, "subtasks") or _get(_get(task, "allocation"), "subtasks") or []
        return next((s for s in subs if _get(s, "subtask_id") == _get(ticket, "subtask_id")), None)

    def _ticket_description(self, ticket: Any) -> str | None:
        """The ticket's own description, from the ticket or else its subtask; None when there is none."""
        text = _get(ticket, "description")
        if not (isinstance(text, str) and text.strip()):
            try:
                text = _get(self._find_subtask(ticket), "description")
            except Exception:  # best effort, like _work_order: the prompt just goes without it
                return None
        return text.strip() if isinstance(text, str) and text.strip() else None

    def _read_files(self, ticket: Any, allowed: Sequence[str]) -> list[str]:
        """Files this ticket may read but not change.

        The ticket's own ``read_files`` always count. The task's list and the files named
        in a Floor Manager answer are general, so a file this ticket is allowed to edit
        (exactly or through a glob) is left out of them.
        """
        own = [_read_only_path(f) for f in (_get(ticket, "read_files") or [])]
        task_files = self.read_files_for(str(_get(ticket, "task_id"))) if self.read_files_for is not None else ()
        answer_files = self._help_answer(ticket).get("read_files") or []
        general = [p for p in map(_read_only_path, [*answer_files, *task_files]) if not _path_covered(p, allowed)]
        return [f for f in dict.fromkeys(own + general) if f]

    def _read_only_excerpts(self, ticket: Any, info: dict[str, Any]) -> list[dict[str, Any]]:
        """The lines of the ticket's read-only files that its text names (see the context engine)."""
        build = getattr(self._base_engine(), "read_only_excerpts", None)
        if build is None:
            return []
        texts = [
            str(info["title"]),
            *map(str, info["acceptance_criteria"]),
            str(_get(ticket, "description", "") or ""),
            self._work_order(str(_get(ticket, "task_id")))["description"],
            *(str(name) for name in self._requested_symbols(ticket)),
        ]
        return list(build(info["read_files"], "\n".join(t for t in texts if t)))

    def _context_level(self, ticket: Any, attempt: int) -> int:
        """The level for this attempt: the task's level from the queue, widened by retries.

        Falls back to the attempt-based level when the queue has no
        ``task_context_level`` or the call fails; context must never cost a build.
        """
        try:
            cap = int(getattr(getattr(self, "config", None), "max_context_level", MAX_CONTEXT_LEVEL)
                      or MAX_CONTEXT_LEVEL)
        except (TypeError, ValueError):
            cap = MAX_CONTEXT_LEVEL
        cap = max(1, min(cap, MAX_CONTEXT_LEVEL))
        by_attempt = min(attempt + 1, cap)
        getter = getattr(self.queue, "task_context_level", None)
        if getter is None:
            return by_attempt
        task_id = str(_get(ticket, "task_id"))
        try:
            task_level = int(getter(task_id))
        except Exception as exc:
            log.warning("task_context_level failed for %s: %s; using attempt-based level", task_id, exc)
            return by_attempt
        return min(max(task_level, by_attempt), cap)

    def _record_context_level(self, ticket: Any, level: int) -> None:
        """Best effort: store the level ``ticket``'s slice was built at (queue kv)."""
        setter = getattr(self.queue, "kv_set", None)
        if setter is None:
            return
        ticket_id = _get(ticket, "ticket_id")
        try:
            setter(f"{CONTEXT_LEVEL_USED_KV_PREFIX}{ticket_id}", level)
        except Exception as exc:
            log.warning("could not record context level for ticket %s: %s", ticket_id, exc)

    async def _context_for(self, ticket: Any, info: dict[str, Any]) -> tuple[ContextSlice, int]:
        """Ask the context engine for the pruned slice; returns ``(slice, tokens_saved)``.

        Uses ``ContextEngine.abuild_slice`` (Gemma ranking) when the engine has
        a Gemma client, else the deterministic ``build_slice``.  Engines with a
        different shape may expose ``build_slice(subtask, objective, ...)``
        returning a ``ContextSlice`` (or a dict).  No engine -> a bare slice.
        """
        objective = _one_line(str(info["title"]).rstrip(".") + ".", 300)
        engine = self._base_engine()
        if engine is None:
            return ContextSlice(objective=objective, acceptance_criteria=info["acceptance_criteria"]), 0
        subtask = SubtaskAllocation(
            subtask_id=str(_get(ticket, "subtask_id")),
            title=str(info["title"]),
            acceptance_criteria=info["acceptance_criteria"],
            allowed_files=info["allowed_files"],
        )
        kwargs: dict[str, Any] = {
            "acceptance_criteria": info["acceptance_criteria"],
            "allowed_files": info["allowed_files"],
        }
        # Focus first: the ticket's own files are edit targets; the task's other files
        # only when named in the subtask text, else outlines.
        focus = list(info.get("focus_files") or info["allowed_files"])
        others = [f for f in info["allowed_files"] if f not in focus]
        # The task's level is the floor and retries widen past it (attempt 1 -> level 1,
        # attempt 2 -> 2, attempt 3+ -> 3); symbols a builder asked for (BLOCKED note) are
        # always shown in full.
        attempt = max(0, int(_get(ticket, "attempt", 0) or 0))
        widen: dict[str, Any] = {"level": self._context_level(ticket, attempt)}
        # Names in the ticket's description and in the task's work-order description both
        # pick edit targets: either may be the only place a symbol is named.
        texts = [str(_get(ticket, "description", "") or ""),
                 self._work_order(str(_get(ticket, "task_id")))["description"]]
        description = "\n".join(dict.fromkeys(t for t in texts if t))
        if description:
            widen["description"] = description
        extra = self._requested_symbols(ticket)
        if others:
            root = getattr(engine, "repo_root", None)
            text = " ".join([str(info["title"]), *map(str, info["acceptance_criteria"]), description,
                             *(str(e) for e in extra)])
            names = set(_IDENT_RE.findall(text))
            named = [f for f in others if root is not None and _defines_any(Path(root) / f, names)]
            kwargs["allowed_files"] = focus + named
            rest = [f for f in others if f not in named]
            if rest:
                widen["extra_files"] = rest
        if extra:
            widen["extra_symbols"] = extra
        afn = getattr(engine, "abuild_slice", None)
        if afn is not None and getattr(engine, "gemma", None) is not None:
            result = await afn(subtask, objective, **kwargs, **_supported_kwargs(afn, widen))
        else:
            bfn = engine.build_slice
            result = await _maybe_await(bfn(subtask, objective, **kwargs, **_supported_kwargs(bfn, widen)))
        self._record_context_level(ticket, widen["level"])
        stats = getattr(engine, "last_stats", None)
        saved = int(getattr(stats, "tokens_saved", 0) or 0)
        if isinstance(result, Mapping):
            result = ContextSlice.model_validate(result)
        if not saved:
            # Rough estimate when the engine has no stats: ~10 tokens per elided line.
            saved = max(0, result.source_line_count - result.snippet_line_count) * 10
        return result, saved

    async def build_assignment(self, ticket: Any) -> TaskAssignment:
        """Build the :class:`TaskAssignment` for ``ticket`` (context from the context engine).

        Records the pruning savings in telemetry (``tokens_saved`` source ``pruning``).
        Read-only files are kept out of the edit targets and shown in their own excerpts.
        """
        info = self._subtask_info(ticket)
        protected = set(info["read_files"])
        editable = [f for f in info["allowed_files"] if _read_only_path(f) not in protected]
        focus = [f for f in info["focus_files"] if f in editable]
        context, saved = await self._context_for(ticket, {**info, "allowed_files": editable, "focus_files": focus})
        if saved > 0:
            self._tm("record_tokens_saved", _get(ticket, "task_id"), "pruning", saved)
        attempt = max(0, min(int(_get(ticket, "attempt", 0) or 0), self.max_retries))
        fields: dict[str, Any] = {
            "task_id": _get(ticket, "task_id"),
            "team_id": self.team_id,
            "builder_id": self.agent_id,
            "subtask_id": _get(ticket, "subtask_id"),
            "acceptance_criteria": info["acceptance_criteria"],
            "allowed_files": info["allowed_files"],
            "context": context,
            "attempt": attempt,
            "fix_hint": _get(ticket, "fix_hint"),
            "ticket_description": self._ticket_description(ticket),
        }
        if not info["read_files"]:
            return TaskAssignment(**fields)
        return _ReadOnlyAssignment(
            **fields,
            read_files=info["read_files"],
            read_only_excerpts=self._read_only_excerpts(ticket, info),
        )

    # -- gates ------------------------------------------------------------------
    async def _run_gates(self, submission: BuilderSubmission, assignment: TaskAssignment) -> GateSummary:
        """Run the deterministic gates and normalize the outcome.

        With a ``ValidationGates`` instance and a workspace this calls
        ``validate_async(submission, workspace, allowed_files=...)``, which
        applies the diff and runs the checks (an unapplicable diff is a failed
        ``syntax`` result).  Other gate runners may expose
        ``run(submission, assignment)`` or be plain callables.
        """
        gates = self.gates
        if gates is None:
            return GateSummary(True)
        if hasattr(gates, "validate_async") or hasattr(gates, "validate"):
            if self.workspace is None:
                raise TypeError("ValidationGates needs a workspace: pass workspace= to the worker/pool")
            vfn = getattr(gates, "validate_async", None) or gates.validate
            outcome = await _maybe_await(vfn(submission, self.workspace, allowed_files=assignment.allowed_files or None))
        else:
            fn = getattr(gates, "run", None)
            if fn is None and callable(gates):
                fn = gates
            if fn is None:
                raise TypeError("gates must provide validate_async/validate/run or be callable")
            # the gates copy the task's snapshot, not the user's folder (WorkspaceGates(base=))
            based = _supported_kwargs(fn, {"base": self._base}) if self._base is not None else {}
            outcome = await _maybe_await(fn(submission, assignment, **based))
        return self._gate_summary(outcome)

    @staticmethod
    def _gate_summary(outcome: Any) -> GateSummary:
        """Normalize a gate runner's result to a :class:`GateSummary`."""
        if isinstance(outcome, GateSummary):
            return outcome
        if hasattr(outcome, "passed") and hasattr(outcome, "results"):
            results = list(outcome.results)
            first = getattr(outcome, "first_failure", None)
            if first is None and not outcome.passed:
                first = next((r for r in results if not r.passed), None)
            summary_fn = getattr(outcome, "summary", None)
            summary = summary_fn() if callable(summary_fn) else None
            return GateSummary(bool(outcome.passed), results, first, summary)
        results = list(outcome or [])
        first = next((r for r in results if not r.passed), None)
        return GateSummary(first is None, results, first)

    def _snapshot(self, diff: str) -> dict[str, str | None] | None:
        """Contents of the files ``diff`` touches (None = absent), so a rejected diff can be undone."""
        if self.workspace is None:
            return None
        return _snapshot_files(self.workspace, extract_diff_paths(diff))

    def _record_gate_results(self, results: Sequence[DeterministicGateResult]) -> None:
        fn = getattr(self.queue, "record_gate_result", None)
        for r in results:
            if fn is not None:
                try:
                    fn(r)
                    continue
                except Exception:  # pragma: no cover - defensive
                    log.exception("record_gate_result failed")
            self._event("gate", r.task_id, r.model_dump())

    # -- one ticket -----------------------------------------------------------
    async def process_ticket(self, ticket: Any) -> str:
        """Process one claimed ticket end to end; returns the outcome label.

        See :meth:`_process_ticket` for the labels.
        """
        if self._task_state(_get(ticket, "task_id")) in _TERMINAL_STATES:
            return "skipped"
        # everything for this ticket reads the task's snapshot of the publish branch
        # (context, gates' base, normalisation), never the user's folder
        task_id = _get(ticket, "task_id")
        resolved = False
        try:
            if self.base_lease is not None and task_id:
                # held for the whole ticket: the snapshot is never pruned under this worker
                async with self.base_lease(str(task_id)) as base:
                    resolved = True
                    if base:
                        self._bases[str(task_id)] = Path(base)
                    else:
                        self._bases.pop(str(task_id), None)
                    return await self._process_on_base(ticket, Path(base) if base else None)
            snapshot = await self._resolve_base(task_id)
            resolved = True
            return await self._process_on_base(ticket, snapshot)
        except Exception as exc:
            if resolved or not _is_counted_infra(exc):
                raise
            return await self._infra_release(ticket, exc)  # no base (BaseUnavailable): no attempt spent

    async def _process_on_base(self, ticket: Any, base: Path | None) -> str:
        """:meth:`process_ticket` with the task's base snapshot (None: the user's folder) resolved."""
        self._base = base
        try:
            return await self._process_ticket(ticket)
        finally:
            self._base = None

    def _base_engine(self) -> Any:
        """The context engine, re-rooted at the task's snapshot when there is one."""
        engine = self.context_engine
        target = self._base
        if engine is None or target is None or not hasattr(engine, "repo_root"):
            return engine
        try:
            if Path(engine.repo_root).resolve() == target.resolve():
                return engine
            clone = copy.copy(engine)
            clone.repo_root = target.resolve()
            if hasattr(clone, "last_stats"):
                clone.last_stats = None
            return clone
        except Exception:  # pragma: no cover - an engine that cannot be re-rooted
            log.exception("%s: could not re-root the context engine at the task's snapshot", self.agent_id)
            return engine

    @staticmethod
    def _focus_files(ticket: Any, assignment: TaskAssignment) -> list[str]:
        return [f for f in (_get(ticket, "focus_files") or []) if f in assignment.allowed_files]

    async def _process_ticket(self, ticket: Any) -> str:
        """Report this slot's activity around :meth:`_build_ticket`; the last report is always idle."""
        task_id = _get(ticket, "task_id")
        self._activity(task_id, f"Building {_get(ticket, 'ticket_id') or task_id}")
        try:
            return await self._build_ticket(ticket)
        finally:
            self._activity(task_id, "", status="idle")

    async def _build_ticket(self, ticket: Any) -> str:
        """Process one claimed ticket end to end; returns the outcome label.

        Labels: ``"review"`` (sent to the reviewer), ``"retry"`` (reopened with a fix hint),
        ``"awaiting_help"`` (parked until the Floor Manager answers),
        ``"quarantined"``, ``"skipped"`` (task already terminal).

        In edit mode (``config.builder_mode`` ``edit`` and a base snapshot) Claude
        edits a private piece workspace and the workspace's changes become the
        submission; the workspace is always removed.
        """
        task_id = _get(ticket, "task_id")
        if self._task_state(task_id) in _TERMINAL_STATES:
            return "skipped"
        assignment = await self.build_assignment(ticket)
        started = time.monotonic()

        self._ticket_changed(task_id)  # the ticket was just claimed

        meta = self._work_order(task_id)

        # 2. Claude (inside limiter.slot + call_with_backoff)
        edit_mode = (getattr(getattr(self, "config", None), "builder_mode", "diff") == "edit"
                     and self._edit_mode_applies(ticket))
        prompt = build_builder_prompt(assignment, max_retries=self.max_retries,
                                      focus_files=self._focus_files(ticket, assignment),
                                      order_description=meta.get("description"),
                                      floor_manager_note=self._help_answer(ticket).get("note"),
                                      contract=EDIT_CONTRACT if edit_mode else BUILDER_CONTRACT,
                                      pitfall_notes=await self._pitfall_notes(ticket, assignment, meta),
                                      file_map=await self._file_map(ticket, assignment, meta))
        self.scratch["last_prompt"] = prompt
        _save_prompt(prompt, str(_get(ticket, "ticket_id")), assignment.attempt + 1)
        workspace: Path | None = None
        if edit_mode:
            opened = await self._open_piece_workspace(ticket, assignment)
            if isinstance(opened, str):
                return opened
            workspace = opened
        call_options: dict[str, Any] = {}
        if workspace is not None:
            call_options["cwd"] = str(workspace)
        retry_client = getattr(self, "retry_client", None)
        if assignment.attempt >= 1 and retry_client is not None:
            call_options["client"] = retry_client
        try:
            try:
                result = await self._call_claude(prompt, task_id=task_id, **call_options)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # D1/E1: not the builder's fault -> no attempt spent; account-wide errors
                # pause dispatch, the rest count toward the per-ticket infrastructure cap.
                self._tm("record_duration", task_id, "build", time.monotonic() - started)
                return await self._infra_release(ticket, exc)
            self._tm("record_duration", task_id, "build", time.monotonic() - started)
            self.scratch["last_reply"] = result.text
            if is_infrastructure_error(result):
                # timeout / non-zero exit without JSON / no reply at all
                return await self._infra_release(
                    ticket, _claude_client.ClaudeError(result.error or "claude produced no reply")
                )
            if result.is_error:
                return self._send_back(
                    ticket, _one_line(f"Claude call failed: {result.error or 'error'}"), reason="claude_error"
                )

            # 3. parse the reply (diff-only contract) or collect the edits (edit mode);
            # a bad reply never reaches gates or reviewer
            if workspace is None:
                submission = parse_builder_reply(result.text, assignment, workspace=self._edits_root())
            else:
                edited = await self._edited_submission(ticket, assignment, result.text, workspace)
                if isinstance(edited, str):
                    return edited
                submission = edited
        finally:
            if workspace is not None:
                await remove_workspace(workspace)
        self._event(
            "submission",
            task_id,
            {"ticket_id": _get(ticket, "ticket_id"), "builder_id": self.agent_id, "status": submission.status,
             "files": submission.modified_files},
        )
        if submission.status == "blocked":
            self._reset_infra(ticket)  # a parsed reply (E1)
            self._tm("record_gate_failure", task_id)
            note = submission.blocker_notes or "submission blocked"
            self._remember_requested_symbols(ticket, note)
            return self._send_back(ticket, _one_line(f"Fix the reply: {note}. {_SHORT_CONTRACT}", 700), reason="blocked")

        # 4. deterministic gates, short-circuit before any reviewer
        self._activity(task_id, "Running gates")
        self._set_state(task_id, TaskState.VALIDATING)
        snapshot = self._snapshot(submission.diff)
        before = self._before_change(submission.diff)
        try:
            gates = await self._run_gates(submission, assignment)
        except Exception as exc:
            _restore_files(self.workspace, snapshot)
            if not _is_counted_infra(exc):
                raise
            return await self._infra_release(ticket, exc)  # e.g. scratch copy failed
        # E1: a parsed reply that also got through the gate runner resets the count
        # (resetting right after parsing would let a broken scratch copy loop forever).
        self._reset_infra(ticket)
        self._record_gate_results(gates.results)
        if not gates.passed:
            _restore_files(self.workspace, snapshot)
            self._tm("record_gate_failure", task_id)
            failure = gates.first_failure
            trace = (getattr(failure, "stderr", "") or "") + "\n" + (getattr(failure, "stdout", "") or "")
            hint = await self._condense(trace, gates.summary or getattr(failure, "note", None), task_id=task_id)
            check = getattr(failure, "check_type", "gate")
            message = f"[{check}] {hint}"
            chars = int(getattr(getattr(self, "config", None), "gate_error_chars", 0) or 0)
            output = trace.strip()
            if chars > 0 and output:
                # the gate's own output keeps its newlines, so it stays out of _one_line
                excerpt: str | None = None
                config = getattr(self, "config", None)
                gemma = getattr(getattr(self, "context_engine", None), "gemma", None)
                if getattr(config, "gemma_notes", False) and gemma is not None and len(output) > 1500:
                    try:
                        excerpt = await error_excerpt(
                            gemma, output, timeout=float(getattr(config, "gemma_notes_timeout_s", 90)), max_chars=chars)
                    except Exception as exc:  # noqa: BLE001 - advisory; fall back to the tail
                        log.debug("error excerpt unavailable: %s", exc)
                        excerpt = None
                if excerpt:
                    message += f"\n\nGate output (the lines that matter, copied exactly):\n{excerpt}"
                else:
                    message += f"\n\nGate output (last {chars} characters):\n{output[-chars:]}"
                return self._send_back(ticket, message, reason=f"gate_{check}", limit=MAX_FIX_HINT_CHARS + chars + 100)
            return self._send_back(ticket, message, reason=f"gate_{check}")
        # what reviewer, cache, queue and patch see: the exact diff of the change
        submission = self._normalized(ticket, submission, before)

        # 5. Gemma pre-review: may send obvious problems back once per ticket (gate mode).
        self._activity(task_id, "Pre-review")
        send_back, notes, review = await self._pre_review(ticket, assignment, submission, gates.results)
        if send_back is not None:
            _restore_files(self.workspace, snapshot)
            outcome = self._send_back(ticket, f"[pre-review] {send_back}", reason="pre_review")
            sent_back = outcome in ("retry", "awaiting_help")
            if sent_back:
                # Only a real send-back uses the once-per-ticket cap and counts as a review avoided.
                self._mark_pre_review_sent_back(ticket)
                self._tm("record_pre_review", task_id, "sent_back")
            self._pre_review_event(ticket, "sent_back" if sent_back else outcome, review)
            return outcome

        await self._enqueue_review(ticket, assignment, submission, gates.results, snapshot, meta, notes)
        return "review"

    async def _pitfall_notes(self, ticket: Any, assignment: TaskAssignment, meta: Mapping[str, Any]) -> list[str]:
        """Advisory notes on the repo's tests this order could break (Gemma); ``[]`` on any failure."""
        try:
            config = getattr(self, "config", None)
            if not getattr(config, "gemma_notes", False):
                return []
            engine = getattr(self, "context_engine", None)
            gemma = getattr(engine, "gemma", None)
            repo_root = getattr(engine, "repo_root", None)
            if gemma is None or not repo_root:
                return []
            cache: dict[tuple[str, str], list[str]] | None = self.__dict__.get("_pitfall_cache")
            if cache is None:
                cache = {}
                self.__dict__["_pitfall_cache"] = cache
            key = (str(assignment.task_id), str(assignment.subtask_id))
            if key in cache:
                return list(cache[key])
            notes: list[str] = []
            text = str(meta.get("description") or assignment.context.objective or "")
            names = list(dict.fromkeys(re.findall(r"`([A-Za-z_][A-Za-z0-9_.]{3,})`", text)))
            hits = await asyncio.to_thread(find_related, Path(repo_root), list(assignment.allowed_files), names)
            if hits:
                notes = await pitfall_notes(gemma, text, hits, timeout=float(getattr(config, "gemma_notes_timeout_s", 90)))
            cache[key] = list(notes)
            if notes:
                log.info("builder %s: %d advisory pitfall notes for %s", self.agent_id, len(notes),
                         _get(ticket, "ticket_id"))
            return list(notes)
        except Exception as exc:  # noqa: BLE001 - the notes are advisory
            log.debug("pitfall notes unavailable: %s", exc)
            return []

    async def _file_map(self, ticket: Any, assignment: TaskAssignment, meta: Mapping[str, Any]) -> list[str]:
        """Where to look: symbols of the allowed files this order likely needs; ``[]`` on any failure."""
        try:
            config = getattr(self, "config", None)
            if not getattr(config, "gemma_notes", False):
                return []
            engine = getattr(self, "context_engine", None)
            gemma = getattr(engine, "gemma", None)
            repo_root = getattr(engine, "repo_root", None)
            if gemma is None or not repo_root:
                return []
            cache: dict[tuple[str, str], list[str]] | None = self.__dict__.get("_file_map_cache")
            if cache is None:
                cache = {}
                self.__dict__["_file_map_cache"] = cache
            key = (str(assignment.task_id), str(assignment.subtask_id))
            if key in cache:
                return list(cache[key])
            text = str(meta.get("description") or assignment.context.objective or "")
            index = await asyncio.to_thread(build_index, Path(repo_root), list(assignment.allowed_files))
            entries = await file_map(gemma, text, index,
                                     timeout=float(getattr(config, "gemma_notes_timeout_s", 90)))
            cache[key] = list(entries)
            return list(entries)
        except Exception as exc:  # noqa: BLE001 - the map is advisory
            log.debug("file map unavailable: %s", exc)
            return []

    # -- edit mode -----------------------------------------------------------------
    def _edit_mode_applies(self, ticket: Any) -> bool:
        """``builder_mode`` is edit: True when the ticket can be built in a piece workspace.

        Needs the task's base snapshot and a ``db_path`` for the pieces directory;
        otherwise the ticket builds in diff mode (logged once).
        """
        why: str | None = None
        if not isinstance(self._base, Path):
            why = "no base snapshot"
        elif getattr(self.config, "db_path", None) is None:
            why = "no db_path for the pieces directory"
        if why is None:
            return True
        key = str(_get(ticket, "ticket_id"))
        if key not in self._edit_skip_logged:
            self._edit_skip_logged.add(key)
            log.info("%s: ticket %s builds in diff mode although builder_mode is edit (%s)",
                     self.agent_id, key, why)
        return False

    async def _open_piece_workspace(self, ticket: Any, assignment: TaskAssignment) -> Path | str:
        """A fresh piece workspace for this attempt, or the outcome of releasing the ticket."""
        assert self._base is not None
        pieces = Path(self.config.db_path).parent / "pieces"
        name = re.sub(r"[^A-Za-z0-9._-]", "_", str(_get(ticket, "ticket_id")))
        dest = pieces / f"{name}-a{assignment.attempt + 1}"
        try:
            await asyncio.to_thread(pieces.mkdir, parents=True, exist_ok=True)
            return await make_workspace(self._base, dest)
        except (WorkspaceError, OSError) as exc:
            # not this builder's fault: no attempt spent, counted toward the infra cap
            self._event("workspace_failed", _get(ticket, "task_id"),
                        {"ticket_id": _get(ticket, "ticket_id"), "error": _one_line(str(exc), 300)})
            return await self._infra_release(ticket, exc)
        except BaseException:
            await remove_workspace(dest)
            raise

    async def _edited_submission(
        self, ticket: Any, assignment: TaskAssignment, text: str, workspace: Path
    ) -> BuilderSubmission | str:
        """Turn an edit-mode reply plus the workspace's changes into a submission (or an outcome)."""
        fields: dict[str, Any] = {
            "task_id": assignment.task_id,
            "subtask_id": assignment.subtask_id,
            "builder_id": assignment.builder_id,
        }
        blocked = _BLOCKED_RE.search(text or "")
        if blocked:
            return BuilderSubmission(**fields, status="blocked", blocker_notes=blocked.group(0).strip())
        protected = set(getattr(assignment, "read_files", ()))
        allowed = [f for f in assignment.allowed_files if _read_only_path(f) not in protected]
        try:
            changes = await collect_changes(workspace, allowed)
        except WorkspaceError as exc:
            return await self._infra_release(ticket, exc)
        if changes.outside:
            self._reset_infra(ticket)  # a real reply (E1)
            self._tm("record_gate_failure", assignment.task_id)
            return self._send_back(
                ticket,
                f"Edited files outside the allowed files: {', '.join(changes.outside)}. "
                f"Change only: {', '.join(allowed)}.",
                reason="outside_files",
            )
        if not changes.diff.strip():
            return BuilderSubmission(**fields, status="blocked", blocker_notes="no changes were made")
        return BuilderSubmission(**fields, status="ready_for_review", modified_files=list(changes.changed),
                                 diff=changes.diff)

    # -- edits / requested symbols ----------------------------------------------
    def _edits_root(self) -> Path | None:
        """Where ```edits replies are resolved: the gate workspace, else the engine's repo."""
        if self.workspace is not None:
            return self.workspace
        if self._base is not None:
            return self._base
        root = getattr(self.context_engine, "repo_root", None)
        return Path(root) if root is not None else None

    def _symbols_key(self, ticket: Any) -> str:
        return f"{EXTRA_SYMBOLS_KV_PREFIX}{_get(ticket, 'ticket_id')}"

    def _requested_symbols(self, ticket: Any) -> list[str]:
        """Symbols earlier attempts of ``ticket`` asked to see in full (queue kv, survives restarts)."""
        key = self._symbols_key(ticket)
        value: Any = None
        getter = getattr(self.queue, "kv_get", None)
        if getter is not None:
            try:
                value = getter(key, None)
            except Exception:  # pragma: no cover - defensive
                log.exception("kv_get(%s) failed", key)
        if value is None:
            value = self._local_symbols.get(key)
        return [str(v) for v in (value or []) if isinstance(v, str) and v]

    def _remember_requested_symbols(self, ticket: Any, note: str) -> list[str]:
        """Add the symbols named in a blocker ``note`` to the ticket's extra_symbols."""
        names = extract_requested_symbols(note)
        if not names:
            return []
        merged = self._requested_symbols(ticket)
        merged += [n for n in names if n not in merged]
        merged = merged[-MAX_EXTRA_SYMBOLS:]
        key = self._symbols_key(ticket)
        self._local_symbols[key] = merged
        setter = getattr(self.queue, "kv_set", None)
        if setter is not None:
            try:
                setter(key, merged)
            except Exception:  # pragma: no cover - defensive
                log.exception("kv_set(%s) failed", key)
        self._event("extra_symbols", _get(ticket, "task_id"),
                    {"ticket_id": _get(ticket, "ticket_id"), "symbols": merged})
        return merged

    # -- Gemma pre-review -----------------------------------------------------
    def _pre_review_key(self, ticket: Any) -> str:
        return f"{PRE_REVIEW_KV_PREFIX}{_get(ticket, 'ticket_id')}"

    def _pre_review_sent_back(self, ticket: Any) -> int:
        """How many times pre-review already sent this ticket back (persisted in the queue's kv)."""
        key = self._pre_review_key(ticket)
        getter = getattr(self.queue, "kv_get", None)
        if getter is not None:
            try:
                return int(getter(key, 0) or 0)
            except Exception:  # pragma: no cover - defensive
                log.exception("kv_get(%s) failed", key)
        return self._local_pre_review.get(key, 0)

    def _mark_pre_review_sent_back(self, ticket: Any) -> None:
        key = self._pre_review_key(ticket)
        count = self._pre_review_sent_back(ticket) + 1
        self._local_pre_review[key] = count
        setter = getattr(self.queue, "kv_set", None)
        if setter is not None:
            try:
                setter(key, count)
            except Exception:  # pragma: no cover - defensive
                log.exception("kv_set(%s) failed", key)

    async def _pre_review(
        self,
        ticket: Any,
        assignment: TaskAssignment,
        submission: BuilderSubmission,
        results: Sequence[DeterministicGateResult],
    ) -> tuple[str | None, str | None, Any]:
        """Run the Gemma pre-review; returns ``(send_back_hint, reviewer_notes, review)``.

        ``send_back_hint`` is set only in gate mode, for a confident
        ``send_back`` verdict with at least one blocking issue, only on the
        ticket's first pre-review send-back (persisted, so restarts respect
        it) and never on the ticket's last allowed attempt (a send-back there
        would quarantine work no Claude reviewer saw).  The caller sends the
        ticket back and does the counting.  Gemma being off, slow or unusable
        never blocks: it counts as ``fallback`` and the submission goes on to
        the reviewer; repeated Gemma errors trip :class:`PreReviewBreaker`.
        """
        task_id = assignment.task_id
        fn = getattr(self.foreman, "pre_review_diff", None) if self.foreman is not None else None
        if self.pre_review == "off" or fn is None or not callable(fn):
            self._tm("record_pre_review", task_id, "skipped")
            return None, None, None
        if self.pre_review_breaker.is_open():
            self._tm("record_pre_review", task_id, "skipped")
            self._pre_review_event(ticket, "skipped", None, reason="gemma unavailable")
            return None, None, None
        task = None
        getter = getattr(self.queue, "get_task", None)
        if getter is not None:
            try:
                task = getter(task_id)
            except Exception:
                task = None
        if task is None:
            task = {"task_id": task_id, **self._work_order(task_id)}
        try:
            review = await _maybe_await(fn(task, assignment, submission.diff, list(results)))
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("%s: pre_review_diff failed; passing %s on", self.agent_id, task_id)
            review = None
        source = _get(review, "source", None)
        if source is None:
            sources = getattr(self.foreman, "last_source", None) or {}
            source = sources.get("pre_review_diff") if hasattr(sources, "get") else None
        if review is None or (source is not None and source != "gemma"):
            error = None if review is None else _get(review, "error", None)
            # Timeouts/exceptions trip the breaker; "Gemma off" and a bad-JSON reply cost nothing.
            if review is None or str(error or "") not in _PRE_REVIEW_CHEAP_FALLBACKS:
                self.pre_review_breaker.record_failure()
            self._tm("record_pre_review", task_id, "fallback")
            self._pre_review_event(ticket, "fallback", review)
            return None, None, None
        self.pre_review_breaker.record_success()
        verdict = str(_get(review, "verdict", "pass") or "pass")
        issues = list(_get(review, "issues", None) or [])
        try:
            confidence = float(_get(review, "confidence", 0.0) or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        notes = format_pre_review_notes(issues)
        blocking = [i for i in issues if str(_get(i, "severity", "")) == "blocking"]
        wants_back = verdict == "send_back" and bool(blocking) and confidence >= self.pre_review_min_confidence
        if self.pre_review == "advise":
            outcome = "advised" if (notes or verdict == "send_back") else "passed"
            self._tm("record_pre_review", task_id, outcome)
            self._pre_review_event(ticket, outcome, review)
            return None, notes, review
        last_attempt = int(_get(ticket, "attempt", 0) or 0) >= self.max_retries
        capped = wants_back and self._pre_review_sent_back(ticket) >= PRE_REVIEW_MAX_SEND_BACKS
        if wants_back and not capped and not last_attempt:
            hint = format_pre_review_notes(blocking) or "Gemma pre-review flagged a blocking problem"
            return hint, None, review
        # Pass, cap used up, or last attempt: the Claude reviewer decides (with the notes).
        self._tm("record_pre_review", task_id, "passed")
        self._pre_review_event(ticket, "passed", review, capped=capped,
                               last_attempt=wants_back and not capped and last_attempt)
        return None, notes, review

    def _pre_review_event(self, ticket: Any, outcome: str, review: Any, *, capped: bool = False,
                          last_attempt: bool = False, reason: str | None = None) -> None:
        payload: dict[str, Any] = {
            "ticket_id": _get(ticket, "ticket_id"),
            "builder_id": self.agent_id,
            "mode": self.pre_review,
            "outcome": outcome,
        }
        if review is not None:
            payload["verdict"] = _get(review, "verdict", None)
            payload["confidence"] = _get(review, "confidence", None)
            payload["issues"] = len(list(_get(review, "issues", None) or []))
        if capped:
            payload["capped"] = True
        if last_attempt:
            payload["last_attempt"] = True
        if reason:
            payload["reason"] = reason
        self._event("pre_review", _get(ticket, "task_id"), payload)

    # -- diff normalisation --------------------------------------------------------
    def _before_root(self) -> Path | None:
        """Where the files stand before this ticket's change: the workspace, the task's snapshot,
        the gates' base workspace or the engine's repo."""
        if self.workspace is not None:
            return self.workspace
        if self._base is not None:
            return self._base
        base = getattr(self.gates, "base_workspace", None) or getattr(self.context_engine, "repo_root", None)
        return Path(base) if base is not None else None

    def _before_change(self, diff: str) -> dict[str, str | None] | None:
        """Contents of the files ``diff`` touches before the gates apply it (None: unknown)."""
        root = self._before_root()
        if root is None or not root.is_dir():
            return None
        return _snapshot_files(root, extract_diff_paths(diff))

    def _normalized(self, ticket: Any, submission: BuilderSubmission,
                    before: dict[str, str | None] | None) -> BuilderSubmission:
        """``submission`` with its diff re-derived exactly (see ``validation_gates.normalize_diff``).

        Claude's raw diff passed the gates' lenient applier (offsets, loose
        context); the normalised diff has exact context, so it applies
        strictly (``git apply``) in the combined patch.  The raw diff is kept
        when normalising fails or yields nothing.
        """
        raw = submission.diff
        if before is None or not raw.strip():
            return submission
        try:
            norm = normalize_diff(raw, before)
        except (DiffApplyError, OSError, UnicodeDecodeError) as exc:
            log.warning("%s: could not normalise the diff of %s: %s", self.agent_id, _get(ticket, "ticket_id"), exc)
            return submission
        if not norm.strip() or _hunk_lines(norm) == _hunk_lines(raw):
            return submission  # already exact (only the headers differ): keep Claude's diff
        self._event("diff_normalized", submission.task_id,
                    {"ticket_id": _get(ticket, "ticket_id"), "raw_bytes": len(raw), "bytes": len(norm)})
        return submission.model_copy(update={"diff": norm, "modified_files": extract_diff_paths(norm)})

    async def _infra_release(self, ticket: Any, exc: BaseException) -> str:
        """E1 for builders: pause/back off, then release the ticket or quarantine at the cap."""
        kind, backoff = await self._infra_failure(ticket, exc)
        if kind == "quarantined":
            return "quarantined"
        self._backoff_s = backoff
        self._release(ticket, _one_line(f"infrastructure: {type(exc).__name__}: {exc}", 200))
        return "released"

    def _work_order(self, task_id: str) -> dict[str, str]:
        """The task's title and description."""
        task = None
        getter = getattr(self.queue, "get_task", None)
        if getter is not None:
            try:
                task = getter(task_id)
            except Exception:
                task = None
        return {"title": str(_get(task, "title", "") or ""), "description": str(_get(task, "description", "") or "")}

    async def _enqueue_review(
        self,
        ticket: Any,
        assignment: TaskAssignment,
        submission: BuilderSubmission,
        results: list[DeterministicGateResult],
        snapshot: dict[str, str | None] | None = None,
        meta: dict[str, str] | None = None,
        pre_review_notes: str | None = None,
    ) -> None:
        self._set_state(assignment.task_id, TaskState.REVIEWING)
        self._set_in_review(ticket, True)  # D6: the stall detector ignores in-review tickets
        self._tm("mark_review_wait", assignment.task_id, item=str(_get(ticket, "ticket_id")))
        await self.review_inbox.put(
            ReviewItem(ticket, assignment, submission, list(results), snapshot=snapshot,
                       workspace=self.workspace, cache_meta=dict(meta or {}),
                       pre_review_notes=pre_review_notes)
        )


_SHORT_CONTRACT = (
    "Reply with exactly one ```edits block (JSON ops such as replace_symbol) or one ```diff "
    "block, touching only the allowed files."
)


def _hunk_lines(diff: str) -> list[str]:
    """A diff's hunk headers and body lines (file headers dropped), for comparing two diffs."""
    out: list[str] = []
    for line in diff.splitlines():
        if line.startswith(("diff --git ", "--- ", "+++ ", "index ", "new file mode", "deleted file mode")):
            continue
        if line[:1] in ("@", "+", "-", " ", "\\"):
            out.append(line)
    return out


def _is_counted_infra(exc: BaseException) -> bool:
    """Infrastructure errors raised by gate runners (``floor_manager.InfrastructureError``)."""
    return any(c.__name__ == "InfrastructureError" for c in type(exc).__mro__) or isinstance(
        exc, _claude_client.ClaudeError
    )


async def _complete_ticket(
    worker: _WorkerBase, ticket_id: Any, task_id: str, callback: Callable[[str], Any] | None
) -> bool:
    """``queue.complete_ticket``; when it reports the last ticket done, fire ``callback``.

    The queue itself moves the task to ``complete`` when every ticket is done.
    """
    all_done = worker.queue.complete_ticket(ticket_id)
    worker._ticket_changed(task_id)
    if not all_done:
        return False
    worker._event("task_complete", task_id, {"by": worker.agent_id})
    if callback is not None:
        try:
            await _maybe_await(callback(task_id))
        except Exception:
            log.exception("on_task_complete(%s) failed", task_id)
    return True


def _snapshot_files(workspace: Path, paths: Iterable[str]) -> dict[str, str | None]:
    """Read the current text of ``paths`` under ``workspace`` (None when absent).

    Read as bytes and decoded without newline translation, so CRLF files keep
    their ``\\r\\n`` (diff normalisation and restores must be byte-exact).
    A file that cannot be read or decoded is left out (never treated as absent).
    """
    snap: dict[str, str | None] = {}
    root = workspace.resolve()
    for rel in paths:
        target = (root / rel).resolve()
        if not target.is_relative_to(root):
            continue  # apply_diff refuses these anyway
        try:
            snap[rel] = target.read_bytes().decode("utf-8") if target.is_file() else None
        except (OSError, UnicodeDecodeError):
            continue
    return snap


def _restore_files(workspace: Path | None, snapshot: dict[str, str | None] | None) -> None:
    """Undo an applied diff: write back snapshotted files, delete ones that did not exist."""
    if workspace is None or not snapshot:
        return
    root = workspace.resolve()
    for rel, text in snapshot.items():
        target = root / rel
        try:
            if text is None:
                if target.is_file():
                    target.unlink()
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(text.encode("utf-8"))  # no newline translation (CRLF stays CRLF)
        except OSError:
            log.exception("could not restore %s", rel)


def _review_task_id(item: Any) -> str | None:
    """The task a review item belongs to (None when it names none)."""
    for holder in (item, getattr(item, "ticket", None), getattr(item, "assignment", None), getattr(item, "submission", None)):
        if holder is None:
            continue
        value = _get(holder, "task_id")
        if value:
            return str(value)
    return None


# ---------------------------------------------------------------------------
# Reviewer
# ---------------------------------------------------------------------------


class ReviewerWorker(_WorkerBase):
    """A team's reviewer (``<team>-6``, or a promoted peer) draining the review inbox.

    Every review is diff-only (criteria + unified diff + gate results).  The
    verdict is parsed from strict JSON; see :func:`parse_reviewer_reply`.
    """

    role = "reviewer"

    def __init__(
        self,
        team_id: str,
        queue: Any,
        client: ClaudeLike,
        limiter: LimiterLike | None,
        telemetry: Any,
        cache: Any,
        *,
        inbox: asyncio.Queue[ReviewItem],
        reviewer_id: str | None = None,
        foreman: Any = None,
        on_task_complete: Callable[[str], Any] | None = None,
        on_approved: Callable[[ReviewItem, ReviewerVerdict], Any] | None = None,
        exclude_builder: str | None = None,
        on_ticket_change: Callable[..., Any] | None = None,
        on_activity: ActivityHook | None = None,
        poll: float = 0.05,
        claude_timeout: float = 600,
        cwd: str | None = None,
        backoff_attempts: int = 5,
        limit_cooldown_s: float = 1200.0,
        infra_backoff_s: float = 5.0,
        infra_failure_cap: int = 3,
        base_for: Callable[[str], Any] | None = None,
    ) -> None:
        super().__init__(
            team_id,
            queue,
            client,
            limiter,
            telemetry,
            foreman=foreman,
            claude_timeout=claude_timeout,
            cwd=cwd,
            backoff_attempts=backoff_attempts,
            limit_cooldown_s=limit_cooldown_s,
            infra_backoff_s=infra_backoff_s,
            infra_failure_cap=infra_failure_cap,
            base_for=base_for,
        )
        self.agent_id = reviewer_id or f"{team_id}-6"
        #: Builder id whose work this reviewer must not review (a promoted peer's own slot, D13).
        self.exclude_builder: str | None = exclude_builder
        self.on_ticket_change = on_ticket_change
        self.on_activity = on_activity
        self.poll = poll
        self._backoff_s = 0.0
        self.cache = cache
        self.inbox = inbox

        self.on_task_complete = on_task_complete
        self.on_approved = on_approved
        self.busy = False
        self.verdicts: list[ReviewerVerdict] = []

    async def run(self, stop: asyncio.Event) -> None:
        """Review inbox items until ``stop`` is set (idle waiting makes no Claude call)."""
        while not stop.is_set():
            getter = asyncio.ensure_future(self.inbox.get())
            stopper = asyncio.ensure_future(stop.wait())
            try:
                done, _ = await asyncio.wait({getter, stopper}, return_when=asyncio.FIRST_COMPLETED)
            except asyncio.CancelledError:
                getter.cancel()
                stopper.cancel()
                raise
            if getter not in done:
                getter.cancel()
                break
            stopper.cancel()
            item = getter.result()
            if self.exclude_builder is not None and item.submission.builder_id == self.exclude_builder:
                # D13: never review your own slot's work; leave it for the team reviewer.
                self.inbox.put_nowait(item)
                self.inbox.task_done()
                await self._wait(self.poll, stop)
                continue
            self.busy = True
            try:
                await self.review(item)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("%s crashed reviewing %s", self.agent_id, item.ticket_id)
                _restore_files(item.workspace, item.snapshot)
                self._send_back(item.ticket, _one_line(f"review error: {exc}"), reason="review_error")
            finally:
                self.busy = False
                self.inbox.task_done()
            if self._backoff_s > 0 or self._pause_remaining() > 0:
                wait, self._backoff_s = self._backoff_s, 0.0
                await self._wait(wait, stop)

    async def review(self, item: ReviewItem) -> ReviewerVerdict | None:
        """Report this reviewer's activity around :meth:`_review_item`; the last report is always idle."""
        task_id = _review_task_id(item)
        self._activity(task_id, f"Reviewing {getattr(item, 'ticket_id', None) or task_id}")
        try:
            return await self._review_item(item)
        finally:
            self._activity(task_id, "", status="idle")

    async def _review_item(self, item: ReviewItem) -> ReviewerVerdict | None:
        """Review one submission and act on the verdict; ``None`` if the task is already terminal."""
        task_id = item.assignment.task_id
        if self._task_state(task_id) in _TERMINAL_STATES or not self._still_owned(item.ticket):
            # Quarantined/superseded meanwhile: no Claude call, undo the applied diff.
            _restore_files(item.workspace, item.snapshot)
            return None
        item_key = str(item.ticket_id)
        self._tm("mark_review_start", task_id, item=item_key)
        self._ticket_changed(task_id)
        retry = item.assignment.attempt
        prompt = build_reviewer_prompt(
            item.submission,
            item.assignment.acceptance_criteria,
            item.gate_results,
            reviewer_id=self.agent_id,
            retry_count=retry,
            pre_review_notes=item.pre_review_notes,
            max_retries=self.max_retries,
            order_description=(item.cache_meta or {}).get("description"),
            part_description=item.assignment.ticket_description,
            last_part=self._is_last_ticket(item),
        )
        started = time.monotonic()
        error: BaseException | None = None
        result: ClaudeResult | None = None
        try:
            result = await self._call_claude(prompt, task_id=task_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = exc
        finally:
            self._tm("record_duration", task_id, "review", time.monotonic() - started)
        if error is None and result is not None and result.is_error:
            error = _claude_client.ClaudeError(result.error or "reviewer call returned an error")
        if error is not None:
            # D1/E1: reviewer infrastructure failure (any is_error, with or without text)
            # -> back into the inbox, no builder retry; counted toward the ticket's cap.
            kind, self._backoff_s = await self._infra_failure(item.ticket, error)
            if kind == "quarantined":
                _restore_files(item.workspace, item.snapshot)
                return None
            self._tm("mark_review_wait", task_id, item=item_key)
            self.inbox.put_nowait(item)
            self._event("review_requeued", task_id, {"ticket_id": item.ticket_id, "reviewer_id": self.agent_id,
                                                     "error": _one_line(str(error), 200)})
            return None
        assert result is not None
        # D6: the ticket may have been reset/reopened/quarantined while Claude was thinking.
        if self._task_state(task_id) in _TERMINAL_STATES or not self._still_owned(item.ticket):
            _restore_files(item.workspace, item.snapshot)
            self._event("review_discarded", task_id, {"ticket_id": item.ticket_id, "reviewer_id": self.agent_id})
            return None
        verdict = parse_reviewer_reply(
            result.text, task_id=task_id, reviewer_id=self.agent_id, retry_count=retry,
            default_path=(item.submission.modified_files or ["(review)"])[0],
        )
        self._reset_infra(item.ticket)  # a reply was parsed (E1)
        self._set_in_review(item.ticket, False)
        self.verdicts.append(verdict)
        self._record_verdict(verdict, item)

        if verdict.verdict == "APPROVED":
            # the Floor Manager rebuilds the task's patch from this after a restart;
            # recorded before the ticket is done
            record = getattr(self.queue, "record_approved_diff", None)
            if callable(record):
                try:
                    record(item.ticket_id, item.submission.diff)
                except KeyError:
                    pass
            if self.on_approved is not None:
                await _maybe_await(self.on_approved(item, verdict))
            await _complete_ticket(self, item.ticket_id, task_id, self.on_task_complete)
        elif verdict.verdict == "REVISE":
            _restore_files(item.workspace, item.snapshot)
            self._send_back(item.ticket, directives_to_hint(verdict.directives), reason="revise")
        else:
            _restore_files(item.workspace, item.snapshot)
            reason = directives_to_hint(verdict.directives).replace("Reviewer asked for changes", "Reviewer rejected")
            self._quarantine(task_id, _one_line(reason, 300))
            self._event("rejected", task_id, {"ticket_id": item.ticket_id, "reviewer_id": self.agent_id})
        return verdict

    def _record_verdict(self, verdict: ReviewerVerdict, item: ReviewItem) -> None:
        fn = getattr(self.queue, "record_verdict", None)
        if fn is not None:
            try:
                fn(verdict)
                return
            except Exception:  # pragma: no cover - defensive
                log.exception("record_verdict failed")
        self._event("verdict", verdict.task_id, {**verdict.model_dump(), "ticket_id": item.ticket_id})

    def _is_last_ticket(self, item: ReviewItem) -> bool:
        """True when no later ticket of the task is open, claimed or awaiting help; False if that can't be told."""
        listing = getattr(self.queue, "tickets_for_task", None)
        if not callable(listing):
            return False
        try:
            tickets = list(listing(item.assignment.task_id))
            ids = [str(ticket.ticket_id) for ticket in tickets]
            later = tickets[ids.index(str(item.ticket_id)) + 1:]
            return not any(ticket.status in ("open", "claimed", "awaiting_help") for ticket in later)
        except Exception:  # the checklist line is optional: never let it break a review
            log.debug("could not tell whether %s is its task's last ticket", item.ticket_id, exc_info=True)
            return False


# ---------------------------------------------------------------------------
# Team pool
# ---------------------------------------------------------------------------


class TeamPool:
    """One team: ``builders`` builder slots plus one reviewer, as asyncio tasks.

    All slots run all the time; the queue decides who works.  Slots above a
    task's allocation simply sleep in ``wait_for_ticket`` and make no Claude
    calls.  Idle builder counts go to ``telemetry.set_idle``.

    Use as ``async with TeamPool(...) as pool:`` or ``pool.start()`` /
    ``await pool.stop()``.
    """

    def __init__(
        self,
        team_id: str,
        builders: int = 5,
        *,
        queue: Any,
        client: ClaudeLike,
        builder_client: ClaudeLike | None = None,
        builder_retry_client: ClaudeLike | None = None,
        reviewer_client: ClaudeLike | None = None,
        limiter: LimiterLike | None,
        telemetry: Any,
        context_engine: Any = None,
        gates: Any = None,
        cache: Any = None,
        foreman: Any = None,
        reviewer_id: str | None = None,
        poll: float = 0.05,
        trust_cache: bool | None = None,
        on_task_complete: Callable[[str], Any] | None = None,
        on_approved: Callable[[ReviewItem, ReviewerVerdict], Any] | None = None,
        workspace: str | Path | None = None,
        claude_timeout: float = 600,
        cwd: str | None = None,
        config: Any = None,
        on_activity: ActivityHook | None = None,
        repo_root: str | Path | None = None,
        limit_cooldown_s: float | None = None,
        infra_backoff_s: float = 5.0,
        infra_failure_cap: int | None = None,
        pre_review: str | None = None,
        pre_review_min_confidence: float | None = None,
        pre_review_breaker: PreReviewBreaker | None = None,
        approved_diffs_before: Callable[[str], Any] | None = None,  # accepted and ignored: callers and fakes still pass it
        base_for: Callable[[str], Any] | None = None,
        base_lease: Callable[[str], Any] | None = None,
    ) -> None:
        if not 1 <= builders <= 5:
            raise ValueError("a team has 1..5 builder slots")
        if infra_failure_cap is None:
            infra_failure_cap = int(getattr(config, "infra_failure_cap", 3))
        self._infra_failure_cap = infra_failure_cap
        if pre_review is None:
            pre_review = str(getattr(config, "pre_review", "gate") or "gate")
        if pre_review_min_confidence is None:
            pre_review_min_confidence = float(getattr(config, "pre_review_min_confidence", 0.7))
        #: One breaker for the whole pool: Gemma is shared, so are its outages.
        self.pre_review_breaker = pre_review_breaker if pre_review_breaker is not None else PreReviewBreaker()
        if trust_cache is None:
            trust_cache = bool(getattr(config, "trust_cache", False))
        if limit_cooldown_s is None:
            limit_cooldown_s = float(getattr(config, "limit_cooldown_s", 1200.0))
        self.trust_cache = trust_cache
        self.repo_root = repo_root
        self._poll = poll
        self._limit_cooldown_s = limit_cooldown_s
        self._infra_backoff_s = infra_backoff_s
        self.team_id = team_id
        self.queue = queue
        self.client = client
        #: The client builder slots call; reviewers use ``reviewer_client``.
        self.builder_client: ClaudeLike = builder_client if builder_client is not None else client
        #: The client builder slots call on attempts after the first; defaults to ``builder_client``.
        self.builder_retry_client: ClaudeLike = (
            builder_retry_client if builder_retry_client is not None else self.builder_client
        )
        #: The client reviewers (team and promoted peers) call; defaults to ``client``.
        self.reviewer_client: ClaudeLike = reviewer_client if reviewer_client is not None else client
        self.limiter = limiter
        self.telemetry = telemetry
        self.cache = cache
        self.foreman = foreman
        self.on_task_complete = on_task_complete
        self.review_inbox: asyncio.Queue[ReviewItem] = asyncio.Queue()
        self._claude_timeout = claude_timeout
        self.on_activity: ActivityHook | None = on_activity
        self.on_ticket_change: Callable[..., Any] | None = None
        self._cwd = cwd
        self.on_approved = on_approved
        #: ``base_for(task_id)``: the task's base snapshot (``FloorManager.task_base``); None = user's folder.
        self.base_for = base_for
        self.builders: list[BuilderWorker] = [
            BuilderWorker(
                team_id,
                slot,
                queue,
                self.builder_client,
                limiter,
                telemetry,
                context_engine,
                gates,
                cache,
                foreman,
                review_inbox=self.review_inbox,
                poll=poll,
                trust_cache=trust_cache,
                on_state=self._on_builder_state,
                on_activity=on_activity,
                on_task_complete=self._task_completed,
                workspace=workspace,
                repo_root=repo_root,
                claude_timeout=claude_timeout,
                cwd=cwd,
                limit_cooldown_s=limit_cooldown_s,
                infra_backoff_s=infra_backoff_s,
                infra_failure_cap=infra_failure_cap,
                pre_review=pre_review,
                pre_review_min_confidence=pre_review_min_confidence,
                pre_review_breaker=self.pre_review_breaker,
                base_for=base_for,
                base_lease=base_lease,
            )
            for slot in range(1, builders + 1)
        ]
        for worker in self.builders:
            worker.retry_client = self.builder_retry_client
        #: ``FoundryConfig`` handed to every worker of this pool (None: workers keep their default).
        self.config: Any = config
        if config is not None:
            for worker in self.builders:
                worker.config = config
        self.reviewer = self._make_reviewer(reviewer_id or f"{team_id}-6")
        self.peer_reviewers: dict[str, ReviewerWorker] = {}
        #: agent id -> the stop event of its peer reviewer (see :meth:`demote_peer_reviewer`).
        self._peer_stops: dict[str, asyncio.Event] = {}
        self.completed_tasks: list[str] = []
        self._stop: asyncio.Event | None = None
        self._tasks: list[asyncio.Task[None]] = []

    def _make_reviewer(self, reviewer_id: str, exclude_builder: str | None = None) -> ReviewerWorker:
        reviewer = ReviewerWorker(
            self.team_id,
            self.queue,
            self.reviewer_client,
            self.limiter,
            self.telemetry,
            self.cache,
            inbox=self.review_inbox,
            reviewer_id=reviewer_id,
            foreman=self.foreman,
            on_task_complete=self._task_completed,
            on_approved=self.on_approved,
            exclude_builder=exclude_builder,
            on_activity=self.on_activity,
            poll=self._poll,
            claude_timeout=self._claude_timeout,
            cwd=self._cwd,
            limit_cooldown_s=self._limit_cooldown_s,
            infra_backoff_s=self._infra_backoff_s,
            infra_failure_cap=self._infra_failure_cap,
            base_for=self.base_for,
            on_ticket_change=self.on_ticket_change,
        )
        config = getattr(self, "config", None)
        if config is not None:
            reviewer.config = config  # type: ignore[attr-defined]
        return reviewer

    def set_on_ticket_change(self, on_ticket_change: Callable[..., Any] | None) -> None:
        """Point every worker of this pool (peer reviewers too) at the ``on_ticket_change`` hook."""
        self.on_ticket_change = on_ticket_change
        for worker in [*self.builders, self.reviewer, *self.peer_reviewers.values()]:
            worker.on_ticket_change = on_ticket_change

    def set_on_activity(self, on_activity: ActivityHook | None) -> None:
        """Point every worker of this pool (peer reviewers too) at the ``on_activity`` hook."""
        self.on_activity = on_activity
        for worker in [*self.builders, self.reviewer, *self.peer_reviewers.values()]:
            worker.on_activity = on_activity

    # -- callbacks --------------------------------------------------------------
    async def _task_completed(self, task_id: str) -> None:
        self.completed_tasks.append(task_id)
        if self.on_task_complete is not None:
            await _maybe_await(self.on_task_complete(task_id))

    def _on_builder_state(self, _worker: BuilderWorker) -> None:
        self._report_idle()

    def idle_count(self) -> int:
        """Builder slots not working on a ticket right now."""
        return sum(1 for b in self.builders if not b.busy and not b.promoted)

    def _report_idle(self) -> None:
        fn = getattr(self.telemetry, "set_idle", None) if self.telemetry is not None else None
        if fn is not None:
            try:
                fn(self.team_id, self.idle_count())
            except Exception:  # pragma: no cover - defensive
                log.exception("telemetry.set_idle failed")

    # -- lifecycle ----------------------------------------------------------------
    @property
    def running(self) -> bool:
        """True between :meth:`start` and :meth:`stop`."""
        return bool(self._tasks) and self._stop is not None and not self._stop.is_set()

    @property
    def calls_per_agent(self) -> dict[str, int]:
        """Claude calls made by each worker of this team (builders, reviewer, peers)."""
        calls = {b.agent_id: b.claude_calls for b in self.builders}
        calls[self.reviewer.agent_id] = self.reviewer.claude_calls
        for pid, peer in self.peer_reviewers.items():
            calls[f"{pid} (peer reviewer)"] = peer.claude_calls
        return calls

    def start(self) -> TeamPool:
        """Start every builder slot and the reviewer (must run inside an event loop)."""
        if self._tasks:
            return self
        self._stop = asyncio.Event()
        self._report_idle()
        for b in self.builders:
            self._tasks.append(asyncio.create_task(b.run(self._stop), name=b.agent_id))
        self._tasks.append(asyncio.create_task(self.reviewer.run(self._stop), name=self.reviewer.agent_id))
        return self

    async def stop(self, timeout: float = 5.0) -> None:
        """Signal stop, let workers finish their current step, cancel stragglers."""
        if self._stop is None:
            return
        self._stop.set()
        tasks = list(self._tasks)
        if tasks:
            _done, pending = await asyncio.wait(tasks, timeout=timeout)
            for t in pending:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

    async def __aenter__(self) -> TeamPool:
        return self.start()

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    async def wait_for_tasks(self, task_ids: Iterable[str], *, timeout: float = 30.0, poll: float = 0.02) -> dict[str, str]:
        """Wait until every task is complete/quarantined/failed; returns their states.

        Raises ``asyncio.TimeoutError`` if they are not all terminal in time.
        """
        ids = list(task_ids)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            states = {t: _state_value(_get(self.queue.get_task(t), "state")) for t in ids}
            if all(s in _TERMINAL_STATES for s in states.values()):
                return states
            if loop.time() >= deadline:
                raise asyncio.TimeoutError(f"tasks not finished: {states}")
            await asyncio.sleep(poll)

    # -- Floor Manager actions ----------------------------------------------------
    def builder(self, slot: int) -> BuilderWorker:
        """The worker for ``slot`` (1-based)."""
        return self.builders[slot - 1]

    def reset_builder_context(self, slot: int) -> None:
        """RESET_BUILDER_CONTEXT: clear a builder slot's scratch state.

        The Floor Manager reopens the stalled ticket itself; the worker notices
        (``_still_owned``) and drops its stale result instead of acting on it.
        """
        self.builder(slot).reset_context()

    reset_builder = reset_builder_context

    def promote_peer_reviewer(self, slot: int | None = None, minutes: float = 15.0) -> str | None:
        """PROMOTE_PEER_REVIEWER: run an extra reviewer as builder ``slot`` for ``minutes``.

        Picks the highest idle builder slot when ``slot`` is None.  The peer
        shares the team's review inbox and stops when the window ends or the
        pool stops.  Returns the peer's agent id, or None if no slot is idle or
        the pool is not running.
        """
        if not self.running or self._stop is None:
            return None
        if slot is None:
            idle = [b.slot for b in self.builders if not b.busy and b.agent_id not in self.peer_reviewers]
            if not idle:
                return None
            slot = max(idle)
        if not 1 <= slot <= len(self.builders) or self.builder(slot).busy:
            return None  # D13: only an idle slot can be promoted
        agent_id = f"{self.team_id}-{slot}"
        if agent_id in self.peer_reviewers:
            return agent_id
        peer = self._make_reviewer(agent_id, exclude_builder=agent_id)
        self.peer_reviewers[agent_id] = peer
        builder = self.builder(slot)
        builder.promoted = True  # M1: no build tickets while reviewing
        peer_stop = asyncio.Event()
        pool_stop = self._stop
        self._peer_stops[agent_id] = peer_stop

        async def run_peer() -> None:
            async def expire() -> None:
                try:
                    await asyncio.wait_for(pool_stop.wait(), timeout=minutes * 60.0)
                except asyncio.TimeoutError:
                    pass
                peer_stop.set()

            timer = asyncio.create_task(expire())
            try:
                await peer.run(peer_stop)
            finally:
                timer.cancel()
                self.peer_reviewers.pop(agent_id, None)
                builder.promoted = False
                self._peer_stops.pop(agent_id, None)

        self._tasks.append(asyncio.create_task(run_peer(), name=f"{agent_id}-peer-reviewer"))
        return agent_id

    def demote_peer_reviewer(self, agent_id: str) -> bool:
        """Undo PROMOTE_PEER_REVIEWER: stop ``agent_id``'s peer reviewer early.

        The peer finishes its current review (if any) and the slot builds
        again. Returns False when ``agent_id`` is not a running peer.
        """
        stop = self._peer_stops.get(agent_id)
        if stop is None or agent_id not in self.peer_reviewers:
            return False
        stop.set()
        return True
