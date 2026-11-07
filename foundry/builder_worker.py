"""Async builder and reviewer workers: the part of the Foundry that calls Claude.

A :class:`TeamPool` runs one team: five :class:`BuilderWorker` slots and one
:class:`ReviewerWorker`, all asyncio tasks in one process.

Builder loop (one slot)::

    wait_for_ticket(team, slot)        # no Claude call while waiting
      -> TaskAssignment (pruned ContextSlice from the context engine)
      -> SolutionCache.lookup          # exact hit: skip Claude, record tokens saved
      -> Claude (limiter.slot + call_with_backoff), reply must hold one ```diff block
      -> parse_builder_reply -> BuilderSubmission (bad reply -> blocked)
      -> deterministic gates (short-circuit on the first failure)
           fail: foreman.condense_error_trace -> reopen ticket with a 1-line
                 fix hint (attempt + 1); attempt cap reached -> quarantine
           pass: hand to the team's review inbox

Reviewer loop::

    review inbox -> diff-only prompt (criteria + unified diff + gate results)
      -> Claude -> parse_reviewer_reply (strict JSON ReviewerVerdict)
           APPROVED: cache.store, ticket done, task complete when all tickets are
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
import inspect
import json
import logging
import re
import time
from pathlib import Path
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import ValidationError

from foundry import claude_client as _claude_client
from foundry.schemas import (
    MAX_RETRIES,
    SubtaskAllocation,
    BuilderSubmission,
    ClaudeResult,
    ContextSlice,
    DeterministicGateResult,
    LineDirective,
    ReviewerVerdict,
    TaskAssignment,
    TaskState,
)

__all__ = [
    "DIFF_CONTRACT",
    "REVIEWER_CONTRACT",
    "MAX_FIX_HINT_CHARS",
    "build_builder_prompt",
    "build_reviewer_prompt",
    "parse_builder_reply",
    "parse_reviewer_reply",
    "extract_diff_paths",
    "directives_to_hint",
    "GateSummary",
    "ReviewItem",
    "BuilderWorker",
    "ReviewerWorker",
    "TeamPool",
]

log = logging.getLogger(__name__)

#: Longest fix hint stored on a reopened ticket (REVISE directives can be long).
MAX_FIX_HINT_CHARS = 1200

#: Longest one-line hint produced by the local fallback condenser.
_ONE_LINE_CHARS = 160

DIFF_CONTRACT = (
    "Reply with exactly one fenced code block that starts with ```diff and contains a "
    "unified diff (`--- a/<path>` / `+++ b/<path>` headers and `@@` hunks; use "
    "`--- /dev/null` for a new file). Only touch the files listed under 'Files you may "
    "modify'. Put no other code blocks in the reply and do not paste whole files. If you "
    "cannot do the work, reply with a single line starting with `BLOCKED:` and the "
    "reason, and no diff block."
)
"""The builder output contract quoted verbatim in every builder prompt."""

REVIEWER_CONTRACT = (
    "Reply with a single JSON object and nothing else (no prose, no code fence): "
    '{"verdict": "APPROVED" | "REVISE" | "REJECTED", "directives": [{"path": "<file>", '
    '"line": <int, 0 = whole file>, "instruction": "<what to change>"}]}. Use APPROVED when '
    "every acceptance criterion is met, REVISE with at least one directive when the diff "
    "can be fixed, REJECTED when the approach is wrong and the task should be quarantined."
)
"""The reviewer output contract quoted verbatim in every reviewer prompt."""

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


def _get(obj: Any, name: str, default: Any = None) -> Any:
    """Read ``name`` from a mapping or an object."""
    if obj is None:
        return default
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


async def _maybe_await(value: Any) -> Any:
    """Await ``value`` when it is awaitable, else return it."""
    if inspect.isawaitable(value):
        return await value
    return value


def _estimate_tokens(text: str) -> int:
    """``ceil(len(text) / 4)``, the shared token estimate."""
    return -(-len(text) // 4)


def _state_value(state: Any) -> str:
    """Normalize a TaskState / string / None to its string value."""
    if state is None:
        return ""
    return str(getattr(state, "value", state))


def _one_line(text: str, limit: int = _ONE_LINE_CHARS) -> str:
    """Collapse ``text`` to one line of at most ``limit`` characters."""
    line = " ".join(text.split())
    return line if len(line) <= limit else line[: limit - 3].rstrip() + "..."


def _fallback_condense(trace: str) -> str:
    """Deterministic 1-line condensation used when no foreman is configured.

    Picks the last ``File "x", line N`` frame (or ``path:N:`` location) and the
    last line that looks like an exception/error.
    """
    lines = [ln.strip() for ln in trace.splitlines() if ln.strip()]
    if not lines:
        return "gate failed with no output"
    location = ""
    for ln in reversed(lines):
        m = re.search(r'File "([^"]+)", line (\d+)', ln)
        if m:
            location = f"{m.group(1)}:{m.group(2)}"
            break
        m = re.match(r"([\w./\\-]+\.\w+):(\d+)", ln)
        if m:
            location = f"{m.group(1)}:{m.group(2)}"
            break
    error = next(
        (ln for ln in reversed(lines) if re.search(r"(Error|Exception|FAILED|failed|assert)", ln)),
        lines[-1],
    )
    return _one_line(f"{location} {error}" if location and location not in error else error)


def _fence_for(text: str) -> str:
    """A backtick fence longer than any backtick run inside ``text``."""
    longest = max((len(m.group(0)) for m in re.finditer(r"`+", text)), default=0)
    return "`" * max(3, longest + 1)


def _normalize_path(path: str) -> str:
    """Repo-relative, forward-slash path without a leading ``./``."""
    p = path.strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


# ---------------------------------------------------------------------------
# Prompts (pure)
# ---------------------------------------------------------------------------


def build_builder_prompt(assignment: TaskAssignment) -> str:
    """Return the exact prompt a builder sends to Claude for ``assignment``.

    Contains only: who/what (ids, attempt), the objective, acceptance criteria,
    allowed files, the pruned ``ContextSlice`` snippets, the fix hint from the
    previous attempt (if any) and the diff-only output contract.  Never whole
    files: the context engine already capped every snippet at 50 lines.
    """
    ctx = assignment.context
    criteria = assignment.acceptance_criteria or ctx.acceptance_criteria
    parts: list[str] = [
        f"You are builder {assignment.builder_id} on team {assignment.team_id}. "
        f"Task {assignment.task_id}, subtask {assignment.subtask_id}, "
        f"attempt {assignment.attempt + 1} of {MAX_RETRIES + 1}.",
        "",
        "## Objective",
        ctx.objective.strip(),
        "",
        "## Acceptance criteria",
    ]
    parts += [f"- {c.strip()}" for c in criteria] or ["- (none given)"]
    parts += ["", "## Files you may modify"]
    parts += [f"- {f}" for f in assignment.allowed_files] or ["- (no restriction given)"]
    if ctx.snippets:
        parts += ["", "## Context (pruned excerpts; elided bodies are shown as `...`)"]
        for snip in ctx.snippets:
            fence = _fence_for(snip.text)
            parts += [
                f"### {snip.path} (lines {snip.start_line}-{snip.end_line})",
                f"{fence}text",
                snip.text.rstrip("\n"),
                fence,
            ]
    if assignment.fix_hint:
        parts += [
            "",
            "## Fix required (your previous attempt was sent back)",
            assignment.fix_hint.strip(),
        ]
    parts += ["", "## Output contract", DIFF_CONTRACT]
    return "\n".join(parts) + "\n"


def build_reviewer_prompt(
    submission: BuilderSubmission,
    acceptance_criteria: Sequence[str],
    gate_results: Sequence[DeterministicGateResult],
    *,
    reviewer_id: str | None = None,
    retry_count: int = 0,
) -> str:
    """Return the exact, diff-only prompt a reviewer sends to Claude.

    The payload is the acceptance criteria, the unified diff and a one-line
    summary per deterministic gate result.  Full files, context snippets and
    gate stdout are deliberately left out.
    """
    who = f"You are reviewer {reviewer_id}. " if reviewer_id else "You are the reviewer. "
    parts: list[str] = [
        who
        + f"Review task {submission.task_id}, subtask {submission.subtask_id}, built by "
        f"{submission.builder_id} (review round {retry_count + 1} of {MAX_RETRIES + 1}).",
        "",
        "## Acceptance criteria",
    ]
    parts += [f"- {c.strip()}" for c in acceptance_criteria] or ["- (none given)"]
    parts += ["", "## Deterministic gates (already run)"]
    if gate_results:
        for g in gate_results:
            line = f"- {g.check_type}: {'PASS' if g.passed else 'FAIL'} ({g.duration_seconds:.2f}s)"
            if g.note:
                line += f" - {_one_line(g.note, 120)}"
            parts.append(line)
    else:
        parts.append("- (no gates configured)")
    fence = _fence_for(submission.diff)
    parts += [
        "",
        "## Unified diff",
        f"{fence}diff",
        submission.diff.rstrip("\n"),
        fence,
        "",
        "## Output contract",
        REVIEWER_CONTRACT,
    ]
    return "\n".join(parts) + "\n"


# ---------------------------------------------------------------------------
# Reply parsers (pure)
# ---------------------------------------------------------------------------

_DIFF_OPEN_RE = re.compile(r"^[ \t]*```diff[^\n`]*$", re.M)
_DIFF_BLOCK_RE = re.compile(r"^[ \t]*```diff[^\n`]*\n(.*?)^[ \t]*```[ \t]*$", re.M | re.S)
_BLOCKED_RE = re.compile(r"^\s*BLOCKED:\s*(.+)$", re.M)


def extract_diff_paths(diff: str) -> list[str]:
    """Return the files a unified diff touches, in order, without ``a/``/``b/`` prefixes.

    New and modified files come from ``+++`` headers; deleted files (``+++ /dev/null``)
    from the matching ``---`` header.
    """
    paths: list[str] = []
    previous_minus: str | None = None
    for line in diff.splitlines():
        if line.startswith("--- "):
            previous_minus = line[4:].split("\t", 1)[0].strip()
        elif line.startswith("+++ "):
            plus = line[4:].split("\t", 1)[0].strip()
            target = plus if plus != "/dev/null" else (previous_minus or "")
            if target.startswith(("a/", "b/")):
                target = target[2:]
            target = _normalize_path(target)
            if target and target != "/dev/null" and target not in paths:
                paths.append(target)
            previous_minus = None
    return paths


def parse_builder_reply(text: str, assignment: TaskAssignment) -> BuilderSubmission:
    """Turn a builder's Claude reply into a :class:`BuilderSubmission`.

    ``ready_for_review`` only when the reply has exactly one closed ```diff block
    holding a non-empty unified diff whose files are all in
    ``assignment.allowed_files`` (no restriction when that list is empty).
    Anything else (no block, several blocks, unterminated block, no headers or
    hunks, files outside the allowance, an explicit ``BLOCKED:``) gives a
    ``blocked`` submission with ``blocker_notes`` saying why, so it never
    reaches the reviewer.
    """
    base = {
        "task_id": assignment.task_id,
        "subtask_id": assignment.subtask_id,
        "builder_id": assignment.builder_id,
    }

    def blocked(note: str, *, diff: str = "", files: list[str] | None = None, checks: dict[str, bool]) -> BuilderSubmission:
        return BuilderSubmission(
            **base,
            status="blocked",
            modified_files=files or [],
            diff=diff,
            self_check=checks,
            blocker_notes=note,
        )

    openers = len(_DIFF_OPEN_RE.findall(text))
    blocks = _DIFF_BLOCK_RE.findall(text)
    if openers == 0:
        m = _BLOCKED_RE.search(text)
        note = f"builder reported BLOCKED: {m.group(1).strip()}" if m else "reply contained no ```diff block"
        return blocked(note, checks={"single_diff_block": False})
    if openers > 1 or len(blocks) > 1:
        return blocked(
            f"reply contained {max(openers, len(blocks))} ```diff blocks; exactly one is required",
            checks={"single_diff_block": False},
        )
    if not blocks:
        return blocked("the ```diff block was not closed", checks={"single_diff_block": False})

    diff = blocks[0]
    if diff and not diff.endswith("\n"):
        diff += "\n"
    checks: dict[str, bool] = {"single_diff_block": True}
    has_headers = bool(re.search(r"^--- ", diff, re.M)) and bool(re.search(r"^\+\+\+ ", diff, re.M))
    has_hunks = bool(re.search(r"^@@ ", diff, re.M))
    checks["unified_diff"] = has_headers and has_hunks
    files = extract_diff_paths(diff)
    if not diff.strip():
        return blocked("the ```diff block was empty", checks={**checks, "unified_diff": False})
    if not checks["unified_diff"] or not files:
        return blocked(
            "the ```diff block is not a unified diff (needs ---/+++ headers and @@ hunks)",
            diff=diff,
            files=files,
            checks={**checks, "unified_diff": False},
        )
    allowed = {_normalize_path(f) for f in assignment.allowed_files}
    outside = [f for f in files if allowed and f not in allowed]
    checks["files_allowed"] = not outside
    if outside:
        return blocked(
            "diff touches files outside the allowance: " + ", ".join(outside),
            diff=diff,
            files=files,
            checks=checks,
        )
    return BuilderSubmission(
        **base,
        status="ready_for_review",
        modified_files=files,
        diff=diff,
        self_check=checks,
        blocker_notes=None,
    )


_JSON_FENCE_RE = re.compile(r"^```(?:json)?[ \t]*\n(.*?)\n?```$", re.S)


def parse_reviewer_reply(
    text: str,
    *,
    task_id: str,
    reviewer_id: str,
    retry_count: int = 0,
    default_path: str = "(review)",
) -> ReviewerVerdict:
    """Parse a reviewer's strict-JSON reply into a :class:`ReviewerVerdict`.

    The reply must be one JSON object (a single surrounding ```json fence is
    tolerated) with ``verdict`` and optional ``directives``.  ``task_id``,
    ``reviewer_id`` and ``retry_count`` always come from the caller, never from
    the model.  Unknown extra keys are ignored; malformed directives are not.
    Anything invalid yields ``REVISE`` with one directive explaining that the
    review must be redone, so a bad reply can never approve work.
    """
    retry = max(0, min(int(retry_count), MAX_RETRIES))

    def invalid(why: str) -> ReviewerVerdict:
        return ReviewerVerdict(
            task_id=task_id,
            reviewer_id=reviewer_id,
            verdict="REVISE",
            directives=[
                LineDirective(
                    path=default_path or "(review)",
                    line=0,
                    instruction=_one_line(
                        f"Reviewer reply was not a valid verdict ({why}); re-check the "
                        "acceptance criteria and resubmit the diff.",
                        300,
                    ),
                )
            ],
            retry_count=retry,
        )

    raw = text.strip()
    fenced = _JSON_FENCE_RE.match(raw)
    if fenced:
        raw = fenced.group(1).strip()
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        return invalid(f"not JSON: {exc.msg if hasattr(exc, 'msg') else exc}")
    if not isinstance(data, dict):
        return invalid("top level is not an object")
    verdict = data.get("verdict")
    if isinstance(verdict, str):
        verdict = verdict.strip().upper()
    directives = data.get("directives", [])
    if directives is None:
        directives = []
    if not isinstance(directives, list):
        return invalid("directives is not a list")
    known = set(LineDirective.model_fields)
    directives = [
        {k: v for k, v in d.items() if k in known} if isinstance(d, dict) else d for d in directives
    ]
    try:
        return ReviewerVerdict(
            task_id=task_id,
            reviewer_id=reviewer_id,
            verdict=verdict,
            directives=directives,
            retry_count=retry,
        )
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first.get("loc", ()))
        return invalid(f"{where}: {first.get('msg', 'invalid')}")


def directives_to_hint(directives: Iterable[LineDirective], limit: int = MAX_FIX_HINT_CHARS) -> str:
    """Render reviewer directives as the fix hint stored on a reopened ticket."""
    items = [
        f"{d.path}:{d.line} {_one_line(d.instruction, 300)}" if d.line else f"{d.path}: {_one_line(d.instruction, 300)}"
        for d in directives
    ]
    hint = "Reviewer asked for changes: " + ("; ".join(items) if items else "revise the diff to meet every criterion")
    return hint if len(hint) <= limit else hint[: limit - 3] + "..."


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
    #: Work-order title/description used in the solution-cache key (same for lookup and store).
    cache_meta: dict[str, str] = field(default_factory=dict)

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
    ) -> None:
        self.team_id = team_id
        self.queue = queue
        self.client = client
        self.limiter = limiter
        self.telemetry = telemetry
        self.foreman = foreman
        self.claude_timeout = claude_timeout
        self.cwd = cwd
        self.backoff_attempts = backoff_attempts
        self.limit_cooldown_s = float(limit_cooldown_s)
        self.infra_backoff_s = float(infra_backoff_s)
        self.claude_calls = 0
        #: Consecutive infrastructure failures (reset by a successful Claude call).
        self.infra_failures = 0
        #: Infrastructure failures seen in total (usage limits, timeouts, CLI missing ...).
        self.infra_errors: list[str] = []

    # -- telemetry (never lets a metrics error break the line) ---------------
    def _tm(self, method: str, *args: Any, **kwargs: Any) -> None:
        fn = getattr(self.telemetry, method, None) if self.telemetry is not None else None
        if fn is None:
            return
        try:
            try:
                fn(*args, **kwargs)
            except TypeError:
                if not kwargs:
                    raise
                fn(*args)  # older telemetry without the keyword
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
        return int(_get(current, "attempt", 0) or 0) == int(_get(ticket, "attempt", 0) or 0)

    def _send_back(self, ticket: Any, hint: str, *, reason: str) -> str:
        """Reopen ``ticket`` with ``hint`` (attempt + 1) or quarantine at the cap.

        Returns ``"retry"``, ``"quarantined"`` or ``"superseded"`` (the ticket
        was already reopened or closed by someone else, so nothing is done).
        """
        task_id = _get(ticket, "task_id")
        ticket_id = _get(ticket, "ticket_id")
        attempt = int(_get(ticket, "attempt", 0) or 0)
        hint = hint if len(hint) <= MAX_FIX_HINT_CHARS else hint[: MAX_FIX_HINT_CHARS - 3] + "..."
        if not self._still_owned(ticket):
            self._event("superseded", task_id, {"ticket_id": ticket_id, "agent_id": self.agent_id, "reason": reason})
            return "superseded"
        # The queue enforces the cap: past MAX_RETRIES it quarantines and returns False.
        try:
            ok: Any = self.queue.reopen_ticket(ticket_id, hint)
        except (ValueError, KeyError) as exc:
            log.debug("reopen_ticket(%s) refused: %s", ticket_id, exc)
            ok = False
        if ok:
            self._tm("record_retry", task_id)
            self._event("retry", task_id, {"ticket_id": ticket_id, "attempt": attempt + 1, "reason": reason, "fix_hint": hint})
            return "retry"
        self._quarantine(task_id, f"retry limit reached ({reason}): {_one_line(hint)}")
        self._event("quarantined", task_id, {"ticket_id": ticket_id, "attempt": attempt, "reason": reason})
        return "quarantined"

    # -- Claude ---------------------------------------------------------------
    async def _call_claude(self, prompt: str, *, task_id: str) -> ClaudeResult:
        """One Claude call inside ``limiter.slot`` wrapped by ``call_with_backoff``.

        Records tokens per call; raises whatever the client raises once backoff
        gives up.
        """
        estimate = _estimate_tokens(prompt)

        async def once() -> ClaudeResult:
            if self.limiter is None:
                self.claude_calls += 1
                return await self.client.run(
                    prompt, agent_id=self.agent_id, role=self.role, cwd=self.cwd, timeout=self.claude_timeout
                )
            async with self.limiter.slot(estimate) as handle:
                self.claude_calls += 1
                result = await self.client.run(
                    prompt, agent_id=self.agent_id, role=self.role, cwd=self.cwd, timeout=self.claude_timeout
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
        self.infra_failures = 0
        return result

    # -- infrastructure failures (D1): never consume an attempt -------------
    async def _handle_infra_error(self, exc: BaseException) -> float:
        """Record an infrastructure failure and pause; returns this worker's own extra wait.

        Usage/rate limits, logged-out or missing CLI pause the whole limiter
        (``pause_until``) for ``retry_after`` or ``limit_cooldown_s``.  Other
        failures (timeouts, crashes of the CLI) back off this worker only,
        exponentially up to ``limit_cooldown_s``.
        """
        self.infra_failures += 1
        self.infra_errors.append(_one_line(f"{type(exc).__name__}: {exc}", 200))
        account_wide = tuple(
            c for c in (
                getattr(_claude_client, "ClaudeRateLimited", None),
                getattr(_claude_client, "ClaudeAuthError", None),
                getattr(_claude_client, "ClaudeNotFound", None),
            ) if c is not None
        )
        if account_wide and isinstance(exc, account_wide):
            retry_after = getattr(exc, "retry_after", None)
            seconds = float(retry_after) if retry_after else self.limit_cooldown_s
            pause = getattr(self.limiter, "pause_until", None)
            if pause is not None:
                pause(seconds=seconds)
                return 0.0
            return seconds
        return min(self.limit_cooldown_s, self.infra_backoff_s * 2 ** (self.infra_failures - 1))

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
                try:
                    raw = fn(trace, task_id=task_id)
                except TypeError:  # condensers without the task_id keyword
                    raw = fn(trace)
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
        cache: ``SolutionCache`` (``lookup``/``store``); ``None`` disables it.
        foreman: ``ShopForeman`` (``condense_error_trace``); ``None`` uses a
            deterministic local condenser.
        review_inbox: the team's review queue (``TeamPool`` shares one).
        poll: ticket polling interval passed to ``wait_for_ticket``.
        trust_cache: when True an exact cache hit that passes the gates
            completes the ticket without a reviewer call; when False (default,
            ``FoundryConfig.trust_cache``) the cached diff skips the builder's
            Claude call but still goes through the gates and the reviewer.
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
        on_task_complete: Callable[[str], Any] | None = None,
        workspace: str | Path | None = None,
        claude_timeout: float = 600,
        cwd: str | None = None,
        backoff_attempts: int = 5,
        limit_cooldown_s: float = 1200.0,
        infra_backoff_s: float = 5.0,
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
        )
        self.workspace: Path | None = Path(workspace) if workspace is not None else None
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
        self.on_task_complete = on_task_complete
        self.busy = False
        self.current_ticket: Any = None
        #: Per-worker scratch (last prompt, last reply ...); cleared by reset_context().
        self.scratch: dict[str, Any] = {}
        #: Outcomes of processed tickets, in order ("review", "retry", "quarantined", "cached", ...).
        self.outcomes: list[str] = []
        self._backoff_s = 0.0

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
            ticket = await self.queue.wait_for_ticket(self.team_id, self.slot, poll=self.poll, stop=stop)
            if ticket is None:
                if stop.is_set():
                    break
                continue
            if self._pause_remaining() > 0:
                self._release(ticket, "dispatch paused")
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

    # -- assignment -------------------------------------------------------------
    def _subtask_info(self, ticket: Any) -> dict[str, Any]:
        """Title, criteria and allowed files for the ticket's subtask."""
        info = {
            "title": _get(ticket, "title"),
            "acceptance_criteria": _get(ticket, "acceptance_criteria"),
            "allowed_files": _get(ticket, "allowed_files"),
        }
        if info["acceptance_criteria"] is None or info["allowed_files"] is None:
            sub = None
            for name in ("get_subtask", "subtask_for"):
                fn = getattr(self.queue, name, None)
                if fn is not None:
                    try:
                        sub = fn(_get(ticket, "task_id"), _get(ticket, "subtask_id"))
                    except (KeyError, ValueError):
                        sub = None
                    break
            if sub is None:
                task = getattr(self.queue, "get_task", lambda _t: None)(_get(ticket, "task_id"))
                subs = _get(task, "subtasks") or _get(_get(task, "allocation"), "subtasks") or []
                sub = next((s for s in subs if _get(s, "subtask_id") == _get(ticket, "subtask_id")), None)
            for key in info:
                if info[key] is None:
                    info[key] = _get(sub, key)
        info["title"] = info["title"] or str(_get(ticket, "subtask_id"))
        info["acceptance_criteria"] = list(info["acceptance_criteria"] or [])
        info["allowed_files"] = list(info["allowed_files"] or [])
        return info

    async def _context_for(self, ticket: Any, info: dict[str, Any]) -> tuple[ContextSlice, int]:
        """Ask the context engine for the pruned slice; returns ``(slice, tokens_saved)``.

        Uses ``ContextEngine.abuild_slice`` (Gemma ranking) when the engine has
        a Gemma client, else the deterministic ``build_slice``.  Engines with a
        different shape may expose ``build_slice(subtask, objective, ...)``
        returning a ``ContextSlice`` (or a dict).  No engine -> a bare slice.
        """
        objective = _one_line(str(info["title"]).rstrip(".") + ".", 300)
        engine = self.context_engine
        if engine is None:
            return ContextSlice(objective=objective, acceptance_criteria=info["acceptance_criteria"]), 0
        subtask = SubtaskAllocation(
            subtask_id=str(_get(ticket, "subtask_id")),
            title=str(info["title"]),
            acceptance_criteria=info["acceptance_criteria"],
            allowed_files=info["allowed_files"],
        )
        kwargs = {
            "acceptance_criteria": info["acceptance_criteria"],
            "allowed_files": info["allowed_files"],
        }
        afn = getattr(engine, "abuild_slice", None)
        if afn is not None and getattr(engine, "gemma", None) is not None:
            result = await afn(subtask, objective, **kwargs)
        else:
            result = await _maybe_await(engine.build_slice(subtask, objective, **kwargs))
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
        """
        info = self._subtask_info(ticket)
        context, saved = await self._context_for(ticket, info)
        if saved > 0:
            self._tm("record_tokens_saved", _get(ticket, "task_id"), "pruning", saved)
        attempt = max(0, min(int(_get(ticket, "attempt", 0) or 0), MAX_RETRIES))
        return TaskAssignment(
            task_id=_get(ticket, "task_id"),
            team_id=self.team_id,
            builder_id=self.agent_id,
            subtask_id=_get(ticket, "subtask_id"),
            acceptance_criteria=info["acceptance_criteria"],
            allowed_files=info["allowed_files"],
            context=context,
            attempt=attempt,
            fix_hint=_get(ticket, "fix_hint"),
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
            outcome = await _maybe_await(fn(submission, assignment))
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

        Labels: ``"review"`` (sent to the reviewer), ``"cached"`` (cache hit
        completed without Claude), ``"retry"`` (reopened with a fix hint),
        ``"quarantined"``, ``"skipped"`` (task already terminal).
        """
        task_id = _get(ticket, "task_id")
        if self._task_state(task_id) in _TERMINAL_STATES:
            return "skipped"
        assignment = await self.build_assignment(ticket)
        started = time.monotonic()

        # 1. exact-match solution cache: a hit never calls Claude.
        meta = self._work_order(task_id)
        cached = _cache_call(self.cache, "lookup", assignment, meta) if self.cache is not None else None
        if cached:
            outcome = await self._try_cached(ticket, assignment, cached, started, meta)
            if outcome is not None:
                return outcome
            # A stale cached diff no longer passes its gates: build normally, no retry burned.

        # 2. Claude (inside limiter.slot + call_with_backoff)
        prompt = build_builder_prompt(assignment)
        self.scratch["last_prompt"] = prompt
        try:
            result = await self._call_claude(prompt, task_id=task_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # D1: usage limits / timeouts / missing or logged-out CLI are not the
            # builder's fault: release the ticket (attempt unchanged) and pause.
            self._tm("record_duration", task_id, "build", time.monotonic() - started)
            self._backoff_s = await self._handle_infra_error(exc)
            self._release(ticket, _one_line(f"Claude unavailable: {type(exc).__name__}: {exc}", 200))
            return "released"
        self._tm("record_duration", task_id, "build", time.monotonic() - started)
        self.scratch["last_reply"] = result.text
        if result.is_error:
            return self._send_back(
                ticket, _one_line(f"Claude call failed: {result.error or 'error'}"), reason="claude_error"
            )

        # 3. parse the reply (diff-only contract); a bad reply never reaches gates or reviewer
        submission = parse_builder_reply(result.text, assignment)
        self._event(
            "submission",
            task_id,
            {"ticket_id": _get(ticket, "ticket_id"), "builder_id": self.agent_id, "status": submission.status,
             "files": submission.modified_files},
        )
        if submission.status == "blocked":
            self._tm("record_gate_failure", task_id)
            note = submission.blocker_notes or "submission blocked"
            return self._send_back(ticket, _one_line(f"Fix the reply: {note}. {_SHORT_CONTRACT}", 300), reason="blocked")

        # 4. deterministic gates, short-circuit before any reviewer
        self._set_state(task_id, TaskState.VALIDATING)
        snapshot = self._snapshot(submission.diff)
        gates = await self._run_gates(submission, assignment)
        self._record_gate_results(gates.results)
        if not gates.passed:
            _restore_files(self.workspace, snapshot)
            self._tm("record_gate_failure", task_id)
            failure = gates.first_failure
            trace = (getattr(failure, "stderr", "") or "") + "\n" + (getattr(failure, "stdout", "") or "")
            hint = await self._condense(trace, gates.summary or getattr(failure, "note", None), task_id=task_id)
            check = getattr(failure, "check_type", "gate")
            return self._send_back(ticket, f"[{check}] {hint}", reason=f"gate_{check}")

        await self._enqueue_review(ticket, assignment, submission, gates.results, snapshot, meta)
        return "review"

    async def _try_cached(
        self, ticket: Any, assignment: TaskAssignment, cached: str, started: float, meta: dict[str, str]
    ) -> str | None:
        """Use an approved cached diff; returns the outcome, or None if it no longer passes."""
        task_id = assignment.task_id
        submission = BuilderSubmission(
            task_id=task_id,
            subtask_id=assignment.subtask_id,
            builder_id=assignment.builder_id,
            status="ready_for_review",
            modified_files=extract_diff_paths(cached),
            diff=cached,
            self_check={"cache_hit": True},
        )
        self._set_state(task_id, TaskState.VALIDATING)
        snapshot = self._snapshot(cached)
        gates = await self._run_gates(submission, assignment)
        self._record_gate_results(gates.results)
        if not gates.passed:
            _restore_files(self.workspace, snapshot)
            self._event("cache_stale", task_id, {"ticket_id": _get(ticket, "ticket_id")})
            return None
        prompt = build_builder_prompt(assignment)
        self._tm("record_tokens_saved", task_id, "cache", _estimate_tokens(prompt) + _estimate_tokens(cached))
        self._event("cache_hit", task_id, {"ticket_id": _get(ticket, "ticket_id"), "builder_id": self.agent_id})
        self._tm("record_duration", task_id, "build", time.monotonic() - started)
        if self.trust_cache:
            await _complete_ticket(self, _get(ticket, "ticket_id"), task_id, self.on_task_complete)
            return "cached"
        await self._enqueue_review(ticket, assignment, submission, gates.results, snapshot, meta)
        return "review"

    def _work_order(self, task_id: str) -> dict[str, str]:
        """The task's title and description (part of the solution-cache key, D5)."""
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
    ) -> None:
        self._set_state(assignment.task_id, TaskState.REVIEWING)
        self._set_in_review(ticket, True)  # D6: the stall detector ignores in-review tickets
        self._tm("mark_review_wait", assignment.task_id, item=str(_get(ticket, "ticket_id")))
        await self.review_inbox.put(
            ReviewItem(ticket, assignment, submission, list(results), snapshot=snapshot,
                       workspace=self.workspace, cache_meta=dict(meta or {}))
        )


_SHORT_CONTRACT = "Reply with exactly one ```diff block holding a unified diff of the allowed files."


def _cache_call(cache: Any, method: str, assignment: TaskAssignment, meta: dict[str, str], *args: Any) -> Any:
    """``cache.lookup/store(assignment, *args, title=, description=)``; older caches lack the keywords."""
    fn = getattr(cache, method)
    try:
        return fn(assignment, *args, **meta)
    except TypeError:
        return fn(assignment, *args)


async def _complete_ticket(
    worker: _WorkerBase, ticket_id: Any, task_id: str, callback: Callable[[str], Any] | None
) -> bool:
    """``queue.complete_ticket``; when it reports the last ticket done, fire ``callback``.

    The queue itself moves the task to ``complete`` when every ticket is done.
    """
    all_done = worker.queue.complete_ticket(ticket_id)
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
    """Read the current text of ``paths`` under ``workspace`` (None when absent)."""
    snap: dict[str, str | None] = {}
    root = workspace.resolve()
    for rel in paths:
        target = (root / rel).resolve()
        if not target.is_relative_to(root):
            continue  # apply_diff refuses these anyway
        try:
            snap[rel] = target.read_text(encoding="utf-8") if target.is_file() else None
        except (OSError, UnicodeDecodeError):
            snap[rel] = None
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
                target.write_text(text, encoding="utf-8")
        except OSError:
            log.exception("could not restore %s", rel)


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
        poll: float = 0.05,
        claude_timeout: float = 600,
        cwd: str | None = None,
        backoff_attempts: int = 5,
        limit_cooldown_s: float = 1200.0,
        infra_backoff_s: float = 5.0,
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
        )
        self.agent_id = reviewer_id or f"{team_id}-6"
        #: Builder id whose work this reviewer must not review (a promoted peer's own slot, D13).
        self.exclude_builder: str | None = exclude_builder
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
        """Review one submission and act on the verdict; ``None`` if the task is already terminal."""
        task_id = item.assignment.task_id
        if self._task_state(task_id) in _TERMINAL_STATES or not self._still_owned(item.ticket):
            # Quarantined/superseded meanwhile: no Claude call, undo the applied diff.
            _restore_files(item.workspace, item.snapshot)
            return None
        item_key = str(item.ticket_id)
        self._tm("mark_review_start", task_id, item=item_key)
        retry = item.assignment.attempt
        prompt = build_reviewer_prompt(
            item.submission,
            item.assignment.acceptance_criteria,
            item.gate_results,
            reviewer_id=self.agent_id,
            retry_count=retry,
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
            # D1: reviewer infrastructure failure -> back into the inbox, no builder retry.
            self._backoff_s = await self._handle_infra_error(error)
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
        self._set_in_review(item.ticket, False)
        self.verdicts.append(verdict)
        self._record_verdict(verdict, item)

        if verdict.verdict == "APPROVED":
            if self.cache is not None and item.submission.diff.strip() and not item.submission.self_check.get("cache_hit"):
                _cache_call(self.cache, "store", item.assignment, item.cache_meta, item.submission.diff)
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
        limit_cooldown_s: float | None = None,
        infra_backoff_s: float = 5.0,
    ) -> None:
        if not 1 <= builders <= 5:
            raise ValueError("a team has 1..5 builder slots")
        if trust_cache is None:
            trust_cache = bool(getattr(config, "trust_cache", False))
        if limit_cooldown_s is None:
            limit_cooldown_s = float(getattr(config, "limit_cooldown_s", 1200.0))
        self.trust_cache = trust_cache
        self._poll = poll
        self._limit_cooldown_s = limit_cooldown_s
        self._infra_backoff_s = infra_backoff_s
        self.team_id = team_id
        self.queue = queue
        self.client = client
        self.limiter = limiter
        self.telemetry = telemetry
        self.cache = cache
        self.foreman = foreman
        self.on_task_complete = on_task_complete
        self.review_inbox: asyncio.Queue[ReviewItem] = asyncio.Queue()
        self._claude_timeout = claude_timeout
        self._cwd = cwd
        self.on_approved = on_approved
        self.builders: list[BuilderWorker] = [
            BuilderWorker(
                team_id,
                slot,
                queue,
                client,
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
                on_task_complete=self._task_completed,
                workspace=workspace,
                claude_timeout=claude_timeout,
                cwd=cwd,
                limit_cooldown_s=limit_cooldown_s,
                infra_backoff_s=infra_backoff_s,
            )
            for slot in range(1, builders + 1)
        ]
        self.reviewer = self._make_reviewer(reviewer_id or f"{team_id}-6")
        self.peer_reviewers: dict[str, ReviewerWorker] = {}
        self.completed_tasks: list[str] = []
        self._stop: asyncio.Event | None = None
        self._tasks: list[asyncio.Task[None]] = []

    def _make_reviewer(self, reviewer_id: str, exclude_builder: str | None = None) -> ReviewerWorker:
        return ReviewerWorker(
            self.team_id,
            self.queue,
            self.client,
            self.limiter,
            self.telemetry,
            self.cache,
            inbox=self.review_inbox,
            reviewer_id=reviewer_id,
            foreman=self.foreman,
            on_task_complete=self._task_completed,
            on_approved=self.on_approved,
            exclude_builder=exclude_builder,
            poll=self._poll,
            claude_timeout=self._claude_timeout,
            cwd=self._cwd,
            limit_cooldown_s=self._limit_cooldown_s,
            infra_backoff_s=self._infra_backoff_s,
        )

    # -- callbacks --------------------------------------------------------------
    async def _task_completed(self, task_id: str) -> None:
        self.completed_tasks.append(task_id)
        if self.on_task_complete is not None:
            await _maybe_await(self.on_task_complete(task_id))

    def _on_builder_state(self, _worker: BuilderWorker) -> None:
        self._report_idle()

    def idle_count(self) -> int:
        """Builder slots not working on a ticket right now."""
        return sum(1 for b in self.builders if not b.busy)

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
        peer_stop = asyncio.Event()
        pool_stop = self._stop

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

        self._tasks.append(asyncio.create_task(run_peer(), name=f"{agent_id}-peer-reviewer"))
        return agent_id
