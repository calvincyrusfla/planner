"""The prompt layer of the workers: output contracts and the builder and reviewer prompts.

Split out of ``foundry/builder_worker.py``, which imports these names back.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

from foundry.order_schema import LIMIT_BUILDER_ORDER
from foundry.schemas import (
    MAX_RETRIES,
    BuilderSubmission,
    DeterministicGateResult,
    TaskAssignment,
)
from foundry.worker_helpers import _fence_for, _normalize_path, _one_line, _read_only_path
from foundry.worker_replies import extract_diff_paths

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

EDIT_CONTRACT = (
    "Make the change by editing the files in your working directory with the Edit and Write tools; "
    "Foundry collects your edits itself, so do not paste a diff or code block. Only change the files "
    "listed under 'Files you may modify'. When you are done, reply with a short plain summary of what "
    "you changed. If you cannot do the work, make no edits and reply with a single line starting with "
    "`BLOCKED:` naming what you need in backticks."
)
"""The output contract of an edit-mode builder (``builder_mode`` ``edit``)."""

REVIEWER_CONTRACT = (
    "Reply with a single JSON object and nothing else (no prose, no code fence): "
    '{"verdict": "APPROVED" | "REVISE" | "REJECTED", "directives": [{"path": "<file>", '
    '"line": <int, 0 = whole file>, "instruction": "<what to change>"}]}. Use APPROVED when '
    "every acceptance criterion is met, REVISE with at least one directive when the diff "
    "can be fixed, REJECTED when the approach is wrong and the task should be quarantined. "
    "Approve only when the acceptance criteria and every plan-checklist item this part is responsible for are met."
)
"""The reviewer output contract quoted verbatim in every reviewer prompt."""


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


def _path_covered(path: str, patterns: Iterable[str]) -> bool:
    """True when ``path`` is one of ``patterns``, exactly or through a glob."""
    return any(path == p or fnmatchcase(path, p) for p in map(_read_only_path, patterns))


#: Cap on the work-order text quoted in builder and reviewer prompts (limits.builderOrder in schema/order.json).
MAX_ORDER_DETAILS_CHARS = LIMIT_BUILDER_ORDER


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


#: A backticked span (group 1), or a quoted token that looks like a name, state or path (group 3).
_PLAN_ITEM_RE = re.compile(r"`([^`\n]+)`|(?<!\w)(['\"])([A-Za-z_][\w.:/-]{1,80})\2(?!\w)")
#: Common words an order may quote without naming anything (compared in lower case).
_PLAN_STOP_WORDS = frozenset({"a", "an", "and", "or", "not", "the", "true", "false", "none", "null"})


def plan_checklist(order_description: str | None, limit: int = 25) -> list[str]:
    """The distinct items ``order_description`` names exactly, in order of first appearance, at most ``limit``.

    An item is a backticked span (without the backticks) or a single- or
    double-quoted token that looks like an identifier, op name, state or file
    path. Very common words are skipped.
    """
    items: list[str] = []
    if not order_description or limit <= 0:
        return items
    for match in _PLAN_ITEM_RE.finditer(order_description):
        item = (match.group(1) or match.group(3)).strip()
        if not item or item.lower() in _PLAN_STOP_WORDS or item in items:
            continue
        items.append(item)
        if len(items) >= limit:
            break
    return items


def _plan_checklist_section(order_description: str | None, *, last_part: bool = False) -> list[str]:
    """Prompt lines listing the order's named items for the reviewer; empty when it names none."""
    items = plan_checklist(order_description)
    if not items:
        return []
    lines = [
        "",
        "## Plan checklist",
        "The order names these exactly. For each one this part is responsible for, check the diff "
        "(or the already-done parts) implements it with exactly this name and shape. If any of this "
        "part's items is missing, renamed or shaped differently from the order, reply REVISE with a "
        "directive naming it.",
    ]
    lines += [f"- {item}" for item in items]
    if last_part:
        lines.append(
            "This is the last part of the task: every item above must now be covered by this diff "
            "or the parts already done."
        )
    return lines


def build_builder_prompt(
    assignment: TaskAssignment,
    *,
    max_retries: int = MAX_RETRIES,
    already_done: Sequence[StackedWork] | None = None,
    focus_files: Sequence[str] | None = None,
    floor_manager_note: str | None = None,
    order_description: str | None = None,
    contract: str = BUILDER_CONTRACT,
    pitfall_notes: Sequence[str] | None = None,
) -> str:
    """Return the exact prompt a builder sends to Claude for ``assignment``.

    ``contract``: the text of the '## Output contract' section (``EDIT_CONTRACT`` in edit mode).
    ``pitfall_notes``: advisory notes from a search of the repo's tests, listed under
    "## Watch out (advisory)" after the order details; none = no section.

    ``already_done`` (stacked tickets): the task's earlier approved tickets,
    listed under "## Already done on this piece" (title, files, symbols).
    ``floor_manager_note``: the Floor Manager's answer to this ticket's help request,
    shown right after the order details.
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
    if pitfall_notes:
        parts += [
            "",
            "## Watch out (advisory)",
            "Found by a search of the repo's tests. Check each against the code; ignore any that do not apply.",
        ]
        parts += [f"- {note}" for note in pitfall_notes]
    if floor_manager_note and floor_manager_note.strip():
        parts += ["", "## Floor Manager note (follow it)", floor_manager_note.strip()]
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
    parts += ["", "## Output contract", contract]
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
    last_part: bool = False,
) -> str:
    """Return the exact, diff-only prompt a reviewer sends to Claude.

    The payload is the acceptance criteria, the unified diff and a one-line
    summary per deterministic gate result.  Full files, context snippets and
    gate stdout are deliberately left out.  ``pre_review_notes`` (Gemma's
    pre-review findings, if any) are added as advisory text the reviewer
    is told may be wrong.
    ``order_description`` (the original work order, capped) is quoted under "## Order details";
    the items it names exactly (:func:`plan_checklist`) are listed under "## Plan checklist".
    ``part_description`` (the ticket's own description, capped) is quoted under "### This part".
    ``last_part``: this is the task's last ticket, so every checklist item must now be covered.
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
    parts += _plan_checklist_section(order_description, last_part=last_part)
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
