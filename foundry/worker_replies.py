"""The reply layer of the workers: parsers for Claude's replies and failure classification.

Split out of ``foundry/builder_worker.py``, which imports these names back.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from foundry.schemas import (
    MAX_RETRIES_LIMIT,
    BuilderSubmission,
    ClaudeResult,
    LineDirective,
    ReviewerVerdict,
    TaskAssignment,
)
from foundry.worker_helpers import (
    MAX_FIX_HINT_CHARS,
    _get,
    _normalize_path,
    _one_line,
    _read_only_touched,
)

#: The logger name from before the split, so log output and filters stay the same.
log = logging.getLogger("foundry.builder_worker")

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
            verdict=verdict,  # type: ignore[arg-type]  # validated by pydantic; ValidationError handled below
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


# When a builder pauses for help (see _WorkerBase._help_request): a send-back parks the
# ticket through ``queue.pause_for_help`` instead of reopening it when ALL of these hold:
#   - the queue has ``pause_for_help`` and ``help_after`` (FOUNDRY_HELP_AFTER) is above 0;
#   - this failure is a missing-information one: its reason or hint contains ``BLOCKED`` or,
#     in any case, one of _MISSING_INFO_PHRASES;
#   - at least ``help_after`` such failures came in a row (any other failure resets the count);
#   - the ticket still has an attempt left (attempt + 1 <= max_retries);
#   - the ticket has never been paused before, so it pauses at most once.
# If the queue refuses the pause, the normal send-back runs. The outcome of a pause is
# ``awaiting_help``, which is handled like a retry, not like a quarantine.
#
# How the answer reaches the builder: once ``queue.answer_help`` has stored a note, the next
# prompt for that ticket shows it in a ``## Floor Manager note (follow it)`` section right
# after ``## Order details``, and the answer's read_files join the ticket's read-only files.

#: Queue kv keys of the pause-for-help bookkeeping (each followed by the ticket id).
MISSING_INFO_KV_PREFIX = "missing_info:"
LAST_HINT_KV_PREFIX = "last_hint:"
HELP_PAUSED_KV_PREFIX = "help_paused:"
HELP_ANSWER_KV_PREFIX = "help_answer:"
#: Missing-information failures in a row before a ticket waits for the Floor Manager.
DEFAULT_HELP_AFTER = 2
MAX_HELP_AFTER = 3
MAX_HELP_TEXT_CHARS = 2000
#: Send-back reasons that judge the work; these never mean the builder lacked information
#: (nor does any ``gate_*`` reason).
_WORK_JUDGEMENT_REASONS = frozenset({"pre_review", "revise", "review_error", "worker_error"})
#: Looked for in the hint only, and only for reasons that are not judgements of the work.
_MISSING_INFO_PHRASES = (
    "can't see", "cannot see", "not shown", "isn't shown", "not supplied", "not provided",
    "need the body", "need the exact", "no context",
)


def _is_missing_information(reason: str, hint: str) -> bool:
    """True when a failure says the builder lacked information rather than got the code wrong.

    Send-backs that judge the work (pre-review, reviewer, worker errors, any ``gate_*``)
    never count, whatever their text says.  A ``blocked`` send-back counts when its hint
    holds the builder's ``BLOCKED`` reply; anything else counts only when the hint (not
    the reason) contains one of ``_MISSING_INFO_PHRASES``.
    """
    if reason in _WORK_JUDGEMENT_REASONS or reason.startswith("gate_"):
        return False
    if reason == "blocked" and "BLOCKED" in hint:
        return True
    lowered = hint.lower()
    return any(phrase in lowered for phrase in _MISSING_INFO_PHRASES)
