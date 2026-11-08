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
           pass: Gemma pre-review (foreman.pre_review_diff, mode off|advise|gate)
                 gate: a confident send_back with a blocking issue reopens the
                       ticket once ("[pre-review] ..."); later submissions and
                       any Gemma fallback go on to the reviewer
                 then hand to the team's review inbox (with Gemma's notes)

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
import copy
import inspect
import json
import logging
import re
import shutil
import tempfile
import time
from pathlib import Path
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from typing import Any, Protocol

from pydantic import ValidationError

from foundry import claude_client as _claude_client
from foundry.validation_gates import DiffApplyError, normalize_diff, prepare_stacked_workspace
from foundry.schemas import (
    MAX_RETRIES,
    MAX_RETRIES_LIMIT,
    SubtaskAllocation,
    BuilderSubmission,
    ClaudeResult,
    ContextSlice,
    DeterministicGateResult,
    LineDirective,
    ReviewerVerdict,
    TaskAssignment,
    TaskState,
    utc_now_iso,
)

__all__ = [
    "DIFF_CONTRACT",
    "BUILDER_CONTRACT",
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

#: Longest fix hint stored on a reopened ticket (REVISE directives can be long).
MAX_FIX_HINT_CHARS = 1200

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

#: Longest one-line hint produced by the local fallback condenser.
_ONE_LINE_CHARS = 160

EDITS_EXAMPLE = (
    '```edits\n'
    '{"edits": [{"op": "replace_symbol", "file": "pkg/mod.py", "symbol": "Shop.total",\n'
    '  "code": "    def total(self):\\n        return sum(self.items)\\n"}]}\n'
    '```'
)
"""A tiny ```edits example quoted in the builder contract."""

BUILDER_CONTRACT = (
    "Reply with exactly one fenced code block: either ```edits (preferred for changes to "
    "existing code) or ```diff. Only touch the files listed under 'Files you may modify'. "
    "Put no other code blocks in the reply and do not paste whole files.\n"
    "- ```edits holds JSON {\"edits\": [...]}; Foundry applies the edits and builds the diff "
    "itself, so you need no surrounding lines. Ops (each names its \"file\"): "
    "`replace_symbol` {file, symbol, code} replaces a whole top-level function/class or "
    "`Class.method` (code must define exactly that symbol, decorators included); "
    "`insert_after_symbol` / `insert_before_symbol` {file, symbol, code}; `append` {file, code}; "
    "`search_replace` {file, old, new} (`old` must occur exactly once, whitespace-exact); "
    "`create_file` {file, code} (new files only); `delete_symbol` {file, symbol}. Never "
    "re-define a symbol that already exists (see the inventory): use `replace_symbol`. "
    "Example:\n" + EDITS_EXAMPLE + "\n"
    "- ```diff holds a unified diff (`--- a/<path>` / `+++ b/<path>` headers and `@@` hunks "
    "with exact context lines; `--- /dev/null` for a new file).\n"
    "If a body you must change is hidden (outline only), or you cannot do the work, reply with "
    "a single line starting with `BLOCKED:` naming the symbols you need in backticks "
    "(e.g. BLOCKED: need the body of `evaluate_line_anomaly`), and no code block."
)
DIFF_CONTRACT = BUILDER_CONTRACT  # backwards-compatible name
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


_KW_CACHE: dict[tuple[Any, str], bool] = {}


def _accepts_kw(fn: Callable[..., Any], name: str) -> bool:
    """Whether ``fn`` accepts keyword ``name`` (inspected once per function, then cached)."""
    key = (getattr(fn, "__func__", fn), name)
    cached = _KW_CACHE.get(key)
    if cached is None:
        try:
            params = inspect.signature(fn).parameters
        except (TypeError, ValueError):  # builtins without a signature
            cached = False
        else:
            cached = name in params or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())
        _KW_CACHE[key] = cached
    return cached


def _supported_kwargs(fn: Callable[..., Any], kwargs: Mapping[str, Any]) -> dict[str, Any]:
    """The subset of ``kwargs`` that ``fn`` accepts (older collaborators lack newer keywords)."""
    return {k: v for k, v in kwargs.items() if _accepts_kw(fn, k)}


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


@dataclass(frozen=True)
class StackedWork:
    """One earlier approved ticket of the same task (the "Already done on this piece" list)."""

    subtask_id: str
    title: str
    files: tuple[str, ...]
    symbols: tuple[str, ...]
    #: ``((path, (symbol, ...)), ...)``: where each added/changed symbol lives.
    by_file: tuple[tuple[str, tuple[str, ...]], ...] = field(default=(), compare=False)


_DEF_KW = r"(?:async\s+def|def|class|(?:export\s+)?(?:async\s+)?function)"
#: A definition on an added line: ``def x``, ``class X``, ``function x``.
_ADDED_DEF_RE = re.compile(r"^\+\s*" + _DEF_KW + r"\s+([A-Za-z_$][\w$]*)")
#: The enclosing symbol git names in a hunk header (``@@ -1,2 +1,3 @@ def total(self):``).
_HUNK_CTX_RE = re.compile(r"^@@[^@]*@@\s*" + _DEF_KW + r"\s+([A-Za-z_$][\w$]*)")
#: A module-level constant assignment on an added line (``+MAX_X = 3``).
_ADDED_CONST_RE = re.compile(r"^\+([A-Z][A-Z0-9_]*)\s*(?::[^=]+)?=")


def summarize_diff(diff: str) -> tuple[list[str], list[str]]:
    """``(files, symbols)`` a diff adds or changes (symbols in first-seen order, deduplicated)."""
    files = extract_diff_paths(diff)
    symbols: dict[str, None] = {}
    for line in diff.splitlines():
        if line.startswith("+++"):
            continue
        m = _ADDED_DEF_RE.match(line) or _HUNK_CTX_RE.match(line) or _ADDED_CONST_RE.match(line)
        if m:
            symbols.setdefault(m.group(1), None)
    return files, list(symbols)


def symbols_by_file(diff: str) -> dict[str, list[str]]:
    """Added/changed symbols of a diff grouped by the file they are in (deleted files skipped)."""
    out: dict[str, list[str]] = {}
    current: str | None = None
    for line in diff.splitlines():
        if line.startswith("+++ "):
            target = line[4:].split("\t", 1)[0].strip()
            current = None if target == "/dev/null" else _normalize_path(target[2:] if target.startswith("b/") else target)
            continue
        if line.startswith("--- ") or current is None:
            continue
        m = _ADDED_DEF_RE.match(line) or _HUNK_CTX_RE.match(line) or _ADDED_CONST_RE.match(line)
        if m and m.group(1) not in out.setdefault(current, []):
            out[current].append(m.group(1))
    return out


def stacked_work(items: Iterable[tuple[str, str]], titles: Mapping[str, str] | None = None) -> list[StackedWork]:
    """:class:`StackedWork` per ``(subtask_id, diff)`` (titles by subtask id; default the id)."""
    out: list[StackedWork] = []
    for subtask_id, diff in items:
        files, symbols = summarize_diff(diff or "")
        by_file = tuple((p, tuple(names)) for p, names in symbols_by_file(diff or "").items() if names)
        sid = str(subtask_id)
        out.append(StackedWork(sid, str((titles or {}).get(sid) or sid), tuple(files), tuple(symbols), by_file))
    return out


_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")


def _defines_any(path: Path, names: set[str]) -> bool:
    """True when the file defines (def/class/function/CONST =) one of ``names``."""
    if not names:
        return False
    try:
        if not path.is_file() or path.stat().st_size > 2_000_000:
            return False
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    for m in re.finditer(r"^[ \t]*" + _DEF_KW + r"\s+([A-Za-z_$][\w$]*)|^([A-Za-z_][\w]*)\s*(?::[^=\n]+)?=", text, re.M):
        if (m.group(1) or m.group(2)) in names:
            return True
    return False


#: At most this many symbols are listed per earlier ticket.
_MAX_STACK_SYMBOLS = 25


def _already_done_section(done: Sequence[StackedWork]) -> list[str]:
    parts = [
        "",
        "## Already done on this piece",
        "These earlier subtasks of this task are approved and already applied to the code "
        "shown under Context (their new functions and files are real). Build on these; "
        "don't redo them. Write your change relative to that code.",
    ]
    for w in done:
        title = _one_line(w.title, 160)
        parts.append(f"- {w.subtask_id}: {title}" if title != w.subtask_id else f"- {w.subtask_id}")
        if w.files:
            parts.append(f"  - files: {', '.join(w.files)}")
        if w.symbols:
            shown = list(w.symbols[:_MAX_STACK_SYMBOLS])
            more = len(w.symbols) - len(shown)
            parts.append(f"  - added/changed: {', '.join(f'`{s}`' for s in shown)}"
                         + (f" (+{more} more)" if more > 0 else ""))
    return parts


class _ReadOnlyAssignment(TaskAssignment):
    """An assignment with files the builder may read but never change."""

    read_files: list[str] = []
    read_only_excerpts: list[dict[str, Any]] = []


def _read_only_path(path: Any) -> str:
    """A repo-relative path in the form diffs use (forward slashes), so paths compare reliably."""
    return _normalize_path(str(path).strip().replace("\\", "/"))


def _path_covered(path: str, patterns: Iterable[str]) -> bool:
    """True when ``path`` is one of ``patterns``, exactly or through a glob."""
    return any(path == p or fnmatchcase(path, p) for p in map(_read_only_path, patterns))


def _read_only_touched(files: Iterable[str], assignment: TaskAssignment) -> list[str]:
    """The ``files`` the assignment marks read-only, whatever ``allowed_files`` says."""
    protected = {_read_only_path(f) for f in getattr(assignment, "read_files", ())}
    return [f for f in files if _read_only_path(f) in protected]


MAX_ORDER_DETAILS_CHARS = 6000  # cap on the work-order text quoted in builder and reviewer prompts


def _capped_order_text(text: str, label: str) -> str:
    """``text`` cut to the order-details cap, saying how much of the ``label`` was left out."""
    if len(text) <= MAX_ORDER_DETAILS_CHARS:
        return text
    more = len(text) - MAX_ORDER_DETAILS_CHARS
    return f"{text[:MAX_ORDER_DETAILS_CHARS].rstrip()}\n[... {label} truncated: {more} more characters]"


def _order_details_section(description: str | None, part_description: str | None = None) -> list[str]:
    """Prompt lines quoting the original work order and the ticket's own part; empty when there is neither.

    The part is left out when it only repeats the order (a single-ticket task).
    """
    text = (description or "").strip()
    part = (part_description or "").strip()
    if part == text:
        part = ""
    lines: list[str] = []
    if text:
        lines += [
            "The original work order this subtask is part of, for background; the acceptance criteria above set what "
            "this subtask must do.",
            _capped_order_text(text, "order description"),
        ]
    if part:
        lines += ["", "### This part", _capped_order_text(part, "ticket description")]
    return ["", "## Order details", *lines] if lines else []


def build_builder_prompt(
    assignment: TaskAssignment,
    *,
    max_retries: int = MAX_RETRIES,
    already_done: Sequence[StackedWork] | None = None,
    focus_files: Sequence[str] | None = None,
    order_description: str | None = None,
) -> str:
    """Return the exact prompt a builder sends to Claude for ``assignment``.

    ``already_done`` (stacked tickets): the task's earlier approved tickets,
    listed under "## Already done on this piece" (title, files, symbols).
    ``focus_files``: this subtask's own files; when they are a strict subset
    of the allowed files the prompt lists "## Your files" and, separately,
    "## You may also touch" (the rest of the task's files).
    ``order_description``: the original work-order text, quoted (capped) under "## Order details".
    ``assignment.ticket_description``: the ticket's own description, quoted (capped) under "### This part".

    Contains only: who/what (ids, attempt), the objective, acceptance criteria,
    the order details, allowed files, the pruned ``ContextSlice`` snippets (edit targets in full,
    the rest as outlines, plus a symbol inventory), the fix hint from the
    previous attempt (if any) and the ```edits / ```diff output contract.  Never
    whole files beyond the context engine's line budget.
    """
    ctx = assignment.context
    criteria = assignment.acceptance_criteria or ctx.acceptance_criteria
    parts: list[str] = [
        f"You are builder {assignment.builder_id} on team {assignment.team_id}. "
        f"Task {assignment.task_id}, subtask {assignment.subtask_id}, "
        f"attempt {assignment.attempt + 1} of {max_retries + 1}.",
        "",
        "## Objective",
        ctx.objective.strip(),
        "",
        "## Acceptance criteria",
    ]
    parts += [f"- {c.strip()}" for c in criteria] or ["- (none given)"]
    parts += _order_details_section(order_description, assignment.ticket_description)
    allowed = list(assignment.allowed_files)
    read_files = list(getattr(assignment, "read_files", ()))
    protected = set(read_files)
    allowed = [f for f in allowed if _read_only_path(f) not in protected]
    focus = [f for f in (focus_files or ()) if f in allowed]
    also = [f for f in allowed if f not in focus]
    if focus and also:
        parts += ["", "## Your files"]
        parts += [f"- {f}" for f in focus]
        parts += [
            "",
            "## You may also touch",
            "Other files of this task (teammates' subtasks). Change one only when your subtask "
            "needs it; together with 'Your files' these are the files you may modify.",
        ]
        parts += [f"- {f}" for f in also]
    else:
        parts += ["", "## Files you may modify"]
        parts += [f"- {f}" for f in allowed] or [
            "- (no restriction given)" if not assignment.allowed_files else "- (none: every listed file is read-only)"
        ]
    if already_done:
        parts += _already_done_section(already_done)
    if ctx.snippets:
        parts += [
            "",
            "## Context",
            "Three kinds of excerpt: (1) a per-file **inventory** of the symbols that already "
            "exist (name and line range): never define any of them again; (2) **full targets** "
            "with numbered lines (`N| code`): these are the symbols to edit; (3) **outlines** "
            "ending with `# outline only — body hidden; do not redefine, ask for it`: do not "
            "rewrite them; if you need one, reply `BLOCKED:` naming it in backticks and it is "
            "shown in full next attempt.",
        ]
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
    if read_files:
        parts += [
            "",
            "## Read-only files",
            "For reference only. Do not change these files: a reply that edits any of them is refused. "
            "To see more of one, reply `BLOCKED:` naming the symbol in backticks.",
        ]
        parts += [f"- {f}" for f in read_files]
        for excerpt in getattr(assignment, "read_only_excerpts", ()):
            text = str(excerpt["text"])
            fence = _fence_for(text)
            parts += [
                f"### {excerpt['path']} (lines {excerpt['start_line']}-{excerpt['end_line']})",
                f"{fence}text",
                text.rstrip("\n"),
                fence,
            ]
    parts += ["", "## Output contract", BUILDER_CONTRACT]
    return "\n".join(parts) + "\n"


def build_reviewer_prompt(
    submission: BuilderSubmission,
    acceptance_criteria: Sequence[str],
    gate_results: Sequence[DeterministicGateResult],
    *,
    reviewer_id: str | None = None,
    retry_count: int = 0,
    pre_review_notes: str | None = None,
    max_retries: int = MAX_RETRIES,
    order_description: str | None = None,
    part_description: str | None = None,
) -> str:
    """Return the exact, diff-only prompt a reviewer sends to Claude.

    The payload is the acceptance criteria, the unified diff and a one-line
    summary per deterministic gate result.  Full files, context snippets and
    gate stdout are deliberately left out.  ``pre_review_notes`` (Gemma's
    pre-review findings, if any) are added as advisory text the reviewer
    is told may be wrong.
    ``order_description`` (the original work order, capped) is quoted under "## Order details".
    ``part_description`` (the ticket's own description, capped) is quoted under "### This part".
    """
    who = f"You are reviewer {reviewer_id}. " if reviewer_id else "You are the reviewer. "
    parts: list[str] = [
        who
        + f"Review task {submission.task_id}, subtask {submission.subtask_id}, built by "
        f"{submission.builder_id} (review round {retry_count + 1} of {max_retries + 1}).",
        "",
        "## Acceptance criteria",
    ]
    parts += [f"- {c.strip()}" for c in acceptance_criteria] or ["- (none given)"]
    parts += _order_details_section(order_description, part_description)
    parts += ["", "## Deterministic gates (already run)"]
    if gate_results:
        for g in gate_results:
            line = f"- {g.check_type}: {'PASS' if g.passed else 'FAIL'} ({g.duration_seconds:.2f}s)"
            if g.note:
                line += f" - {_one_line(g.note, 120)}"
            parts.append(line)
    else:
        parts.append("- (no gates configured)")
    if pre_review_notes and pre_review_notes.strip():
        parts += ["", "## Gemma pre-review", f"Gemma pre-review notes (may be wrong): {pre_review_notes.strip()}"]
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
_EDITS_OPEN_RE = re.compile(r"^[ \t]*```edits[^\n`]*$", re.M)
_EDITS_BLOCK_RE = re.compile(r"^[ \t]*```edits[^\n`]*\n(.*?)^[ \t]*```[ \t]*$", re.M | re.S)


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


def parse_builder_reply(
    text: str,
    assignment: TaskAssignment,
    *,
    workspace: str | Path | None = None,
) -> BuilderSubmission:
    """Turn a builder's Claude reply into a :class:`BuilderSubmission`.

    ``ready_for_review`` only when the reply has exactly one closed ```diff block
    holding a non-empty unified diff whose files are all in
    ``assignment.allowed_files`` (no restriction when that list is empty), or
    exactly one ```edits block (JSON ``{"edits": [...]}``) that
    :func:`foundry.edits.apply_edits` applies cleanly to ``workspace`` (the
    workspace is not modified; the generated unified diff becomes
    ``submission.diff``, so gates, review and publishing are unchanged).
    Anything else (no block, several blocks, unterminated block, no headers or
    hunks, files outside the allowance, invalid edits, an explicit
    ``BLOCKED:``) gives a ``blocked`` submission with ``blocker_notes`` saying
    why, so it never reaches the reviewer.
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
    edit_openers = len(_EDITS_OPEN_RE.findall(text))
    if openers and edit_openers:
        return blocked(
            "reply contained both an ```edits and a ```diff block; send exactly one",
            checks={"single_diff_block": False},
        )
    if edit_openers:
        return _parse_edits_reply(text, edit_openers, assignment, workspace, blocked, base)
    blocks = _DIFF_BLOCK_RE.findall(text)
    if openers == 0:
        m = _BLOCKED_RE.search(text)
        note = f"builder reported BLOCKED: {m.group(1).strip()}" if m else "reply contained no ```edits or ```diff block"
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
    touched = _read_only_touched(files, assignment)
    if touched:
        return blocked(
            "diff touches read-only files: " + ", ".join(touched),
            diff=diff,
            files=files,
            checks={**checks, "read_only_untouched": False},
        )
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


def _parse_edits_reply(
    text: str,
    openers: int,
    assignment: TaskAssignment,
    workspace: str | Path | None,
    blocked: Callable[..., BuilderSubmission],
    base: dict[str, Any],
) -> BuilderSubmission:
    """The ```edits half of :func:`parse_builder_reply`."""
    blocks = _EDITS_BLOCK_RE.findall(text)
    if openers > 1 or len(blocks) > 1:
        return blocked(
            f"reply contained {max(openers, len(blocks))} ```edits blocks; exactly one is required",
            checks={"single_edits_block": False},
        )
    if not blocks:
        return blocked("the ```edits block was not closed", checks={"single_edits_block": False})
    checks: dict[str, bool] = {"single_edits_block": True}
    raw = blocks[0].strip()
    if not raw:
        return blocked("the ```edits block was empty", checks={**checks, "edits_json": False})
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return blocked(
            f"the ```edits block is not valid JSON ({exc.msg} at line {exc.lineno} col {exc.colno})",
            checks={**checks, "edits_json": False},
        )
    edits = data.get("edits") if isinstance(data, dict) else data
    if not isinstance(edits, list) or not edits:
        return blocked(
            'the ```edits block must be {"edits": [ ... ]} with at least one edit',
            checks={**checks, "edits_json": False},
        )
    checks["edits_json"] = True
    if workspace is None:
        return blocked(
            "```edits replies cannot be applied here (no workspace); send a ```diff block",
            checks={**checks, "edits_applied": False},
        )
    from foundry.edits import apply_edits  # local: keeps the pure parsers importable alone

    allowed = [_normalize_path(f) for f in assignment.allowed_files]
    try:
        diff, errors = apply_edits(Path(workspace), edits, allowed or None)
    except Exception as exc:  # a malformed edit must never crash the worker
        diff, errors = "", [f"{type(exc).__name__}: {exc}"]
    if errors:
        shown = "; ".join(_one_line(str(e), 200) for e in list(errors)[:3])
        more = f" (+{len(errors) - 3} more)" if len(errors) > 3 else ""
        return blocked(f"edits rejected: {shown}{more}", checks={**checks, "edits_applied": False})
    if not diff or not diff.strip():
        return blocked("the edits changed nothing", checks={**checks, "edits_applied": False})
    if not diff.endswith("\n"):
        diff += "\n"
    files = extract_diff_paths(diff)
    allowed_set = set(allowed)
    touched = _read_only_touched(files, assignment)
    if touched:
        return blocked("edits touch read-only files: " + ", ".join(touched), diff=diff, files=files,
                       checks={**checks, "edits_applied": True, "read_only_untouched": False})
    outside = [f for f in files if allowed_set and f not in allowed_set]
    checks["edits_applied"] = True
    checks["files_allowed"] = not outside
    if outside:
        return blocked("edits touch files outside the allowance: " + ", ".join(outside),
                       diff=diff, files=files, checks=checks)
    return BuilderSubmission(
        **base,
        status="ready_for_review",
        modified_files=files,
        diff=diff,
        self_check=checks,
        blocker_notes=None,
    )


#: Backticked identifiers (``foo``, ``Class.method``) in a BLOCKED note.
_BACKTICK_SYMBOL_RE = re.compile(r"`([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)(?:\(\))?`")
#: "body of X" / "bodies of X" / "definition of X" in a BLOCKED note.
_BODY_OF_RE = re.compile(
    r"\b(?:body|bodies|definition|implementation|source)\s+of\s+`?([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)",
    re.I,
)
_FILE_SUFFIXES = (".py", ".pyi", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".json", ".md",
                  ".toml", ".yaml", ".yml", ".txt", ".cfg", ".ini", ".html", ".css")
_NOT_SYMBOLS = {"git", "apply", "diff", "edits", "blocked", "none", "true", "false", "self", "cls",
                "replace_symbol", "insert_after_symbol", "insert_before_symbol", "append",
                "search_replace", "create_file", "delete_symbol"}
#: Most symbols carried from a BLOCKED note into the next attempt's slice.
MAX_EXTRA_SYMBOLS = 12


def extract_requested_symbols(note: str | None) -> list[str]:
    """Symbols a builder asked for in a BLOCKED / blocker note, in order, de-duplicated.

    Picks backticked identifiers (``foo``, ``Class.method``, ``foo()``) and
    names after "body of" / "definition of".  File names, ops and short
    words are skipped.
    """
    if not note:
        return []
    found: list[str] = []
    candidates = [m.group(1) for m in _BACKTICK_SYMBOL_RE.finditer(note)]
    candidates += [m.group(1) for m in _BODY_OF_RE.finditer(note)]
    for name in candidates:
        name = name.strip(".")
        if (
            len(name) < 2
            or name.lower() in _NOT_SYMBOLS
            or name.lower().endswith(_FILE_SUFFIXES)
            or name in found
        ):
            continue
        found.append(name)
        if len(found) >= MAX_EXTRA_SYMBOLS:
            break
    return found


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
    retry = max(0, min(int(retry_count), MAX_RETRIES_LIMIT))

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


_INFRA_ERROR_RE = re.compile(
    r"^timeout after\b|without JSON output|^empty output\b|no JSON object", re.I
)


def is_infrastructure_error(result: ClaudeResult) -> bool:
    """True when an ``is_error`` result means Claude never produced a reply (D1).

    Matches the real ``ClaudeCodeClient`` shapes: ``"timeout after Ns"``,
    ``"claude exited N without JSON output: ..."`` (non-zero exit / non-JSON
    output), plus any error result with no reply text and no output tokens.
    Such failures release the ticket without using an attempt.  An error
    result that carries a model reply (e.g. ``error_max_turns`` with text or
    output tokens) is a real build outcome and does use an attempt.
    """
    if not result.is_error:
        return False
    if _INFRA_ERROR_RE.search(result.error or ""):
        return True
    return not (result.text or "").strip() and result.output_tokens == 0


#: Fallback reasons that cost no Gemma time (do not trip the breaker).
_PRE_REVIEW_CHEAP_FALLBACKS = frozenset({"gemma off", "unusable reply"})


class PreReviewBreaker:
    """Circuit breaker for the Gemma pre-review, shared by a team's builders.

    After ``threshold`` consecutive fallbacks caused by Gemma timeouts/errors,
    the pre-review is skipped for ``cooldown_s`` seconds (``clock`` is
    monotonic and injectable).  A usable Gemma reply resets the count.
    """

    def __init__(self, threshold: int = 3, cooldown_s: float = 600.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.threshold = int(threshold)
        self.cooldown_s = float(cooldown_s)
        self.clock = clock
        self.failures = 0
        self.open_until = 0.0

    def is_open(self) -> bool:
        return self.clock() < self.open_until

    def record_failure(self) -> None:
        self.failures += 1
        if self.failures >= self.threshold:
            self.open_until = self.clock() + self.cooldown_s
            self.failures = 0
            log.warning("Gemma pre-review: %d consecutive failures; skipping it for %.0f s",
                        self.threshold, self.cooldown_s)

    def record_success(self) -> None:
        self.failures = 0


def format_pre_review_notes(issues: Iterable[Any], limit: int = 600) -> str | None:
    """``"blocking: x; minor: y"`` from pre-review issues (dicts or objects); None when empty."""
    parts: list[str] = []
    for issue in issues:
        note = _one_line(str(_get(issue, "note", "") or ""), 200)
        if not note:
            continue
        severity = str(_get(issue, "severity", "") or "minor")
        parts.append(f"{severity}: {note}")
    if not parts:
        return None
    text = "; ".join(parts)
    return text if len(text) <= limit else text[: limit - 3] + "..."


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
        except Exception:  # never stop the line over a snapshot: fall back to the user's folder
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
    async def _call_claude(self, prompt: str, *, task_id: str) -> ClaudeResult:
        """One Claude call inside ``limiter.slot`` wrapped by ``call_with_backoff``.

        Records tokens per call; raises whatever the client raises once backoff
        gives up.
        """
        estimate = _estimate_tokens(prompt)
        cwd = await self._claude_cwd(task_id)

        async def once() -> ClaudeResult:
            if self.limiter is None:
                self.claude_calls += 1
                return await self.client.run(
                    prompt, agent_id=self.agent_id, role=self.role, cwd=cwd, timeout=self.claude_timeout
                )
            async with self.limiter.slot(estimate) as handle:
                self.claude_calls += 1
                result = await self.client.run(
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
        approved_diffs_before: Callable[[str], Any] | None = None,
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
        #: ``approved_diffs_before(ticket_id) -> [(subtask_id, diff), ...]`` (task order): the
        #: earlier approved work a ticket is stacked on; default ``queue.approved_diffs_before``.
        self.approved_diffs_before = approved_diffs_before
        #: The stacked workspace of the ticket being processed (see :meth:`_prepare_stack`).
        self._stack: _Stack | None = None
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

        The ticket's own ``read_files`` always count. The task's list is general, so
        a file this ticket is allowed to edit (exactly or through a glob) is left out of it.
        """
        own = [_read_only_path(f) for f in (_get(ticket, "read_files") or [])]
        task_files = self.read_files_for(str(_get(ticket, "task_id"))) if self.read_files_for is not None else ()
        general = [p for p in map(_read_only_path, task_files) if not _path_covered(p, allowed)]
        return [f for f in dict.fromkeys(own + general) if f]

    def _read_only_excerpts(self, ticket: Any, info: dict[str, Any]) -> list[dict[str, Any]]:
        """The lines of the ticket's read-only files that its text names (see the context engine)."""
        build = getattr(self._stack_engine(), "read_only_excerpts", None)
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
        by_attempt = min(attempt + 1, MAX_CONTEXT_LEVEL)
        getter = getattr(self.queue, "task_context_level", None)
        if getter is None:
            return by_attempt
        task_id = str(_get(ticket, "task_id"))
        try:
            task_level = int(getter(task_id))
        except Exception as exc:
            log.warning("task_context_level failed for %s: %s; using attempt-based level", task_id, exc)
            return by_attempt
        return min(max(task_level, by_attempt), MAX_CONTEXT_LEVEL)

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
        engine = self._stack_engine()
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
        # only when named (in the subtask text or by an earlier ticket's work), else outlines.
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
        stacked = self._stacked_symbols()
        if others:
            root = getattr(engine, "repo_root", None)
            text = " ".join([str(info["title"]), *map(str, info["acceptance_criteria"]), description,
                             *(str(e) for e in extra)])
            names = set(_IDENT_RE.findall(text))
            by_earlier = {path for path, _name in stacked}
            named = [f for f in others if f in by_earlier
                     or (root is not None and _defines_any(Path(root) / f, names))]
            kwargs["allowed_files"] = focus + named
            rest = [f for f in others if f not in named]
            if rest:
                widen["extra_files"] = rest
        visible = set(kwargs["allowed_files"])
        extra = list(extra) + [f"{p}::{n}" for p, n in stacked if p in visible]
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
            stack = self._stack
            extra: dict[str, Any] = {}
            if stack is not None and stack.tmp is not None and stack.root is not None:
                # gate the diff on top of the earlier approved diffs, in the stacked copy
                # itself (WorkspaceGates(workspace=)): one repo copy per attempt
                extra = _supported_kwargs(fn, {"workspace": stack.root})
            elif self._base is not None:
                # the gates copy the task's snapshot, not the user's folder (WorkspaceGates(base=))
                based = _supported_kwargs(fn, {"base": self._base})
                if based:
                    outcome = await _maybe_await(fn(submission, assignment, **based))
                    return self._gate_summary(outcome)
            if extra:
                assert stack is not None and stack.root is not None
                before = _snapshot_files(stack.root, extract_diff_paths(submission.diff))
                try:
                    outcome = await _maybe_await(fn(submission, assignment, **extra))
                except BaseException:
                    _restore_files(stack.root, before)
                    raise
                if not bool(getattr(outcome, "passed", True)):
                    # e.g. a stale cached diff: the build that follows starts from the clean stack
                    _restore_files(stack.root, before)
            else:
                outcome = await _maybe_await(fn(submission, assignment))
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

        A ticket of a task whose earlier tickets are approved is *stacked*:
        it is built, gated and its edits resolved in a scratch workspace =
        repo + those approved diffs (in task order), its context slice comes
        from that workspace and its prompt lists them under "Already done on
        this piece".  The submitted diff is relative to that workspace.  See
        :meth:`_process_ticket` for the labels.
        """
        if self._task_state(_get(ticket, "task_id")) in _TERMINAL_STATES:
            return "skipped"
        # everything for this ticket reads the task's snapshot of the publish branch
        # (context, gates' base, stacked copy, normalisation), never the user's folder
        task_id = _get(ticket, "task_id")
        if self.base_lease is not None and task_id:
            # held for the whole ticket: the snapshot is never pruned under this worker
            async with self.base_lease(str(task_id)) as base:
                if base:
                    self._bases[str(task_id)] = Path(base)
                else:
                    self._bases.pop(str(task_id), None)
                return await self._process_on_base(ticket, Path(base) if base else None)
        return await self._process_on_base(ticket, await self._resolve_base(task_id))

    async def _process_on_base(self, ticket: Any, base: Path | None) -> str:
        """:meth:`process_ticket` with the task's base snapshot (None: the user's folder) resolved."""
        self._base = base
        try:
            self._stack = await self._prepare_stack(ticket)
        except asyncio.CancelledError:
            raise
        except (DiffApplyError, OSError, shutil.Error) as exc:
            # not this builder's fault: no attempt spent, counted toward the infra cap
            self._base = None
            self._event("stack_failed", _get(ticket, "task_id"),
                        {"ticket_id": _get(ticket, "ticket_id"), "error": _one_line(str(exc), 300)})
            return await self._infra_release(ticket, _StackError(str(exc)))
        except BaseException:
            self._base = None
            raise
        try:
            return await self._process_ticket(ticket)
        finally:
            self._base = None
            stack, self._stack = self._stack, None
            if stack is not None and stack.tmp is not None:
                await asyncio.to_thread(shutil.rmtree, stack.tmp, True)

    # -- stacking -----------------------------------------------------------------
    def _stack_items(self, ticket: Any) -> list[tuple[str, str]]:
        """The task's earlier approved ``(subtask_id, diff)`` pairs, in order (empty diffs dropped)."""
        fn = self.approved_diffs_before or getattr(self.queue, "approved_diffs_before", None)
        if fn is None or not callable(fn):
            return []
        try:
            got = fn(str(_get(ticket, "ticket_id")))
        except KeyError:
            return []
        out: list[tuple[str, str]] = []
        for item in got or []:
            sid, diff = (_get(item, "subtask_id"), _get(item, "diff")) if not isinstance(item, (tuple, list)) else item
            if str(diff or "").strip():
                out.append((str(sid), str(diff)))
        return out

    def _stack_titles(self, task_id: str) -> dict[str, str]:
        fn = getattr(self.queue, "tickets_for_task", None)
        if fn is None:
            return {}
        try:
            return {str(_get(t, "subtask_id")): str(_get(t, "title", "") or "") for t in fn(task_id) or []}
        except Exception:  # pragma: no cover - defensive
            return {}

    def _stack_base(self) -> Path | None:
        """The directory a stacked workspace copies: the task's snapshot, else the gates' base
        workspace, else the engine's repo."""
        if self._base is not None:
            return self._base
        base = getattr(self.gates, "base_workspace", None) or getattr(self.context_engine, "repo_root", None)
        return Path(base) if base is not None else None

    async def _prepare_stack(self, ticket: Any) -> _Stack | None:
        """Build the stacked workspace for ``ticket``; None when nothing earlier is approved.

        With an in-place ``ValidationGates`` workspace (``workspace=``) the
        approved diffs already stay applied there, so that workspace is used
        as is (no copy).  Otherwise the gates' base workspace (or the context
        engine's repo) is copied to a temp dir and the diffs are applied in
        order.  Raises ``DiffApplyError`` when an approved diff does not stack.
        """
        items = self._stack_items(ticket)
        if not items:
            return None
        work = stacked_work(items, self._stack_titles(str(_get(ticket, "task_id"))))
        if self.workspace is not None:
            return _Stack(self.workspace, None, work)
        base = self._stack_base()
        if base is None:
            return _Stack(None, None, work)
        tmp = Path(await asyncio.to_thread(tempfile.mkdtemp, prefix="foundry-stack-"))
        try:
            root = await asyncio.to_thread(prepare_stacked_workspace, base, tmp / "ws", items)
        except BaseException:
            await asyncio.to_thread(shutil.rmtree, tmp, True)
            raise
        self._event("stacked", str(_get(ticket, "task_id")),
                    {"ticket_id": _get(ticket, "ticket_id"), "on": [w.subtask_id for w in work]})
        return _Stack(root, tmp, work)

    def _stack_engine(self) -> Any:
        """The context engine, re-rooted at the stacked workspace (else the task's snapshot) when there is one."""
        engine = self.context_engine
        stack = self._stack
        target = stack.root if stack is not None and stack.root is not None else self._base
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
            log.exception("%s: could not re-root the context engine at the stacked workspace", self.agent_id)
            return engine

    def _stacked_symbols(self, limit: int = 12) -> list[tuple[str, str]]:
        """``(path, symbol)`` the earlier tickets added/changed (newest work first), at most ``limit``."""
        out: list[tuple[str, str]] = []
        for w in reversed(self._already_done()):
            for path, names in w.by_file:
                for n in names:
                    if (path, n) not in out:
                        out.append((path, n))
        return out[:limit]

    @staticmethod
    def _focus_files(ticket: Any, assignment: TaskAssignment) -> list[str]:
        return [f for f in (_get(ticket, "focus_files") or []) if f in assignment.allowed_files]

    def _already_done(self) -> list[StackedWork]:
        return list(self._stack.work) if self._stack is not None else []

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

        Labels: ``"review"`` (sent to the reviewer), ``"cached"`` (cache hit
        completed without Claude), ``"retry"`` (reopened with a fix hint),
        ``"quarantined"``, ``"skipped"`` (task already terminal).
        """
        task_id = _get(ticket, "task_id")
        if self._task_state(task_id) in _TERMINAL_STATES:
            return "skipped"
        assignment = await self.build_assignment(ticket)
        started = time.monotonic()

        self._ticket_changed(task_id)  # the ticket was just claimed

        # 1. exact-match solution cache: a hit never calls Claude.
        meta = self._work_order(task_id)
        cached = _cache_call(self.cache, "lookup", assignment, meta) if self.cache is not None else None
        if cached:
            outcome = await self._try_cached(ticket, assignment, cached, started, meta)
            if outcome is not None:
                return outcome
            # A stale cached diff no longer passes its gates: build normally, no retry burned.

        # 2. Claude (inside limiter.slot + call_with_backoff)
        prompt = build_builder_prompt(assignment, max_retries=self.max_retries, already_done=self._already_done(),
                                      focus_files=self._focus_files(ticket, assignment),
                                      order_description=meta.get("description"))
        self.scratch["last_prompt"] = prompt
        try:
            result = await self._call_claude(prompt, task_id=task_id)
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

        # 3. parse the reply (diff-only contract); a bad reply never reaches gates or reviewer
        submission = parse_builder_reply(result.text, assignment, workspace=self._edits_root())
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
            return self._send_back(ticket, f"[{check}] {hint}", reason=f"gate_{check}")
        # what reviewer, cache, queue and patch see: the exact diff of the change
        submission = self._normalized(ticket, submission, before)

        # 5. Gemma pre-review: may send obvious problems back once per ticket (gate mode).
        self._activity(task_id, "Pre-review")
        send_back, notes, review = await self._pre_review(ticket, assignment, submission, gates.results)
        if send_back is not None:
            _restore_files(self.workspace, snapshot)
            outcome = self._send_back(ticket, f"[pre-review] {send_back}", reason="pre_review")
            if outcome == "retry":
                # Only a real reopen uses the once-per-ticket cap and counts as a review avoided.
                self._mark_pre_review_sent_back(ticket)
                self._tm("record_pre_review", task_id, "sent_back")
            self._pre_review_event(ticket, "sent_back" if outcome == "retry" else outcome, review)
            return outcome

        await self._enqueue_review(ticket, assignment, submission, gates.results, snapshot, meta, notes)
        return "review"

    # -- edits / requested symbols ----------------------------------------------
    def _edits_root(self) -> Path | None:
        """Where ```edits replies are resolved: the gate workspace, else the engine's repo.

        A stacked ticket resolves against its stacked workspace, so the diff it
        submits is relative to the earlier approved work.
        """
        if self._stack is not None and self._stack.root is not None:
            return self._stack.root
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
        before = self._before_change(cached)
        try:
            gates = await self._run_gates(submission, assignment)
        except Exception as exc:
            _restore_files(self.workspace, snapshot)
            if not _is_counted_infra(exc):
                raise
            return await self._infra_release(ticket, exc)
        self._record_gate_results(gates.results)
        if not gates.passed:
            _restore_files(self.workspace, snapshot)
            self._event("cache_stale", task_id, {"ticket_id": _get(ticket, "ticket_id")})
            return None
        submission = self._normalized(ticket, submission, before)
        cached = submission.diff
        prompt = build_builder_prompt(assignment, max_retries=self.max_retries, already_done=self._already_done(),
                                      focus_files=self._focus_files(ticket, assignment))
        self._tm("record_tokens_saved", task_id, "cache", _estimate_tokens(prompt) + _estimate_tokens(cached))
        self._event("cache_hit", task_id, {"ticket_id": _get(ticket, "ticket_id"), "builder_id": self.agent_id})
        self._tm("record_duration", task_id, "build", time.monotonic() - started)
        if self.trust_cache:
            record = getattr(self.queue, "record_approved_diff", None)
            if callable(record):  # later stacked tickets build on it
                try:
                    record(_get(ticket, "ticket_id"), cached)
                except KeyError:
                    pass
            await _complete_ticket(self, _get(ticket, "ticket_id"), task_id, self.on_task_complete)
            return "cached"
        await self._enqueue_review(ticket, assignment, submission, gates.results, snapshot, meta)
        return "review"

    # -- diff normalisation --------------------------------------------------------
    def _before_root(self) -> Path | None:
        """Where the files stand before this ticket's change (stacked copy, workspace or base)."""
        if self._stack is not None and self._stack.root is not None:
            return self._stack.root
        if self.workspace is not None:
            return self.workspace
        return self._stack_base()

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
        context); the normalised diff has exact context, so it stacks
        strictly (``git apply``) under later tickets and in the combined
        patch.  The raw diff is kept when normalising fails or yields nothing.
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


@dataclass
class _Stack:
    """A stacked ticket's workspace: ``root`` (None = nothing to read), ``tmp`` (ours to delete)."""

    root: Path | None
    tmp: Path | None
    work: list[StackedWork]


class _StackError(RuntimeError):
    """The task's earlier approved diffs could not be stacked (an infrastructure failure)."""


def _is_counted_infra(exc: BaseException) -> bool:
    """Infrastructure errors raised by gate runners (``floor_manager.InfrastructureError``)."""
    return any(c.__name__ == "InfrastructureError" for c in type(exc).__mro__) or isinstance(
        exc, _claude_client.ClaudeError
    )


def _cache_call(cache: Any, method: str, assignment: TaskAssignment, meta: dict[str, str], *args: Any) -> Any:
    """``cache.lookup/store(assignment, *args, title=, description=)``; older caches lack the keywords."""
    fn = getattr(cache, method)
    return fn(assignment, *args, **_supported_kwargs(fn, meta))


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
            # stacked tickets: later tickets of the task build on this diff
            # (queue.approved_diffs_before); recorded before the ticket is done
            record = getattr(self.queue, "record_approved_diff", None)
            if callable(record):
                try:
                    record(item.ticket_id, item.submission.diff)
                except KeyError:
                    pass
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
        on_activity: ActivityHook | None = None,
        repo_root: str | Path | None = None,
        limit_cooldown_s: float | None = None,
        infra_backoff_s: float = 5.0,
        infra_failure_cap: int | None = None,
        pre_review: str | None = None,
        pre_review_min_confidence: float | None = None,
        pre_review_breaker: PreReviewBreaker | None = None,
        approved_diffs_before: Callable[[str], Any] | None = None,
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
        #: Earlier approved work per ticket (stacked tickets); None -> queue.approved_diffs_before.
        self.approved_diffs_before = approved_diffs_before
        #: ``base_for(task_id)``: the task's base snapshot (``FloorManager.task_base``); None = user's folder.
        self.base_for = base_for
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
                approved_diffs_before=approved_diffs_before,
                base_for=base_for,
                base_lease=base_lease,
            )
            for slot in range(1, builders + 1)
        ]
        self.reviewer = self._make_reviewer(reviewer_id or f"{team_id}-6")
        self.peer_reviewers: dict[str, ReviewerWorker] = {}
        #: agent id -> the stop event of its peer reviewer (see :meth:`demote_peer_reviewer`).
        self._peer_stops: dict[str, asyncio.Event] = {}
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
