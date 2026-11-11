"""Advisory pitfall notes and PR briefs from the Shop Foreman model (Gemma).

:func:`find_related` searches the test files of a repo deterministically.
:func:`pitfall_notes` and :func:`pr_brief` then let Gemma select and point at
what that search and the diff already established: Gemma never adds a location
of its own, because a note on a line the search did not find and a file that
did not change are dropped. Both are advisory, so on any error, timeout or
empty answer they return an empty result instead of raising.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
import logging
import os
import posixpath
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

__all__ = ["Hit", "find_related", "pitfall_notes", "pr_brief"]

MAX_HITS = 40
MAX_NOTES = 5
MAX_NOTE_CHARS = 200
MAX_HIT_TEXT_CHARS = 200
MAX_DIFF_CHARS = 12_000
MAX_CRITERION_CHARS = 80
MAX_UNUSUAL = 3
MAX_UNUSUAL_CHARS = 150

_MAX_FILE_BYTES = 2_000_000
_SKIP_DIRS = frozenset({"node_modules", "__pycache__", "venv", "env", "build", "dist", "data"})
_SCRIPT_SUFFIXES = (".mjs", ".js", ".cjs", ".ts")

# Hits are capped strongest first: what breaks a naive change, then how a file is tied to it.
_RANK_EXACT = 0
_RANK_IMPORT = 1
_RANK_MENTION = 2

_SCRIPT_IMPORT = re.compile(r"""(?:\bfrom\s+|\bimport\s*\(?\s*|\brequire\s*\(\s*)(['"])(?P<spec>[^'"\n]+)\1""")
_SCRIPT_EXACT_CALL = re.compile(r"(?:\bdeep(?:Strict)?Equal|\.to(?:Strict)?Equal|\.deep\.equal)\s*\(")
_PYTHON_EXACT_CALLS = frozenset(
    {"assertEqual", "assertEquals", "assertListEqual", "assertTupleEqual", "assertSequenceEqual", "assertCountEqual"}
)

_NOTES_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "notes": {
            "type": "array",
            "maxItems": MAX_NOTES,
            "items": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "line": {"type": "integer"},
                    "note": {"type": "string"},
                },
                "required": ["path", "line", "note"],
            },
        },
    },
    "required": ["notes"],
}

_BRIEF_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "criteria": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "files": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["index", "files"],
            },
        },
        "unusual": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["criteria", "unusual"],
}


@dataclass(frozen=True)
class Hit:
    """One line the deterministic search found: repo-relative path, 1-based line, stripped text."""

    path: str
    line: int
    text: str


def _is_test_file(rel: str) -> bool:
    """True for ``tests/`` sources, ``test_*.py``, ``*_test.py`` and ``*.test.mjs``."""
    directories, _, name = rel.rpartition("/")
    if name.endswith(".test.mjs"):
        return True
    if name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py")):
        return True
    return "tests" in directories.split("/") and name.endswith((".py", ".mjs", ".js"))


def _test_files(root: Path) -> list[str]:
    """Repo-relative paths of the test files under ``root``, sorted."""
    found: list[str] = []
    for folder, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in _SKIP_DIRS]
        for name in names:
            rel = Path(folder, name).relative_to(root).as_posix()
            if _is_test_file(rel):
                found.append(rel)
    return sorted(found)


def _read(path: Path) -> str | None:
    """The text of ``path`` (UTF-8, errors ignored), or ``None`` when unreadable or very large."""
    try:
        if path.stat().st_size > _MAX_FILE_BYTES:
            return None
        return path.read_text(encoding="utf-8", errors="ignore").removeprefix("﻿")
    except OSError:
        return None


def _python_module(path: str) -> str | None:
    """The dotted module of a repo-relative ``.py`` path."""
    if not path.endswith(".py"):
        return None
    return path[:-3].removesuffix("/__init__").replace("/", ".")


def _parse(source: str) -> ast.Module | None:
    try:
        return ast.parse(source)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return None


def _python_import_lines(tree: ast.Module, rel: str, modules: set[str]) -> set[int]:
    """Lines of the import statements in ``tree`` that load one of ``modules``."""
    package = rel.split("/")[:-1]
    lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name in modules for alias in node.names):
                lines.add(node.lineno)
        elif isinstance(node, ast.ImportFrom):
            parts = (node.module or "").split(".") if node.module else []
            if node.level:
                if node.level - 1 > len(package):
                    continue
                parts = [*package[: len(package) - (node.level - 1)], *parts]
            base = ".".join(parts)
            if base in modules or any(f"{base}.{alias.name}" in modules for alias in node.names):
                lines.add(node.lineno)
    return lines


def _holds_name(node: ast.expr, names_re: re.Pattern[str]) -> bool:
    """True when ``node`` is a list or tuple literal containing one of the names."""
    return isinstance(node, (ast.List, ast.Tuple)) and names_re.search(ast.unparse(node)) is not None


def _call_name(func: ast.expr) -> str:
    if isinstance(func, ast.Attribute):
        return func.attr
    return func.id if isinstance(func, ast.Name) else ""


def _python_exact_spans(tree: ast.Module, names_re: re.Pattern[str]) -> list[tuple[int, int]]:
    """(first, last) lines of the statements asserting an exact list/tuple holding a name."""
    spans: list[tuple[int, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assert):
            for inner in ast.walk(node.test):
                if (
                    isinstance(inner, ast.Compare)
                    and any(isinstance(op, ast.Eq) for op in inner.ops)
                    and any(_holds_name(side, names_re) for side in (inner.left, *inner.comparators))
                ):
                    spans.append((node.lineno, node.end_lineno or node.lineno))
                    break
        elif (
            isinstance(node, ast.Call)
            and _call_name(node.func) in _PYTHON_EXACT_CALLS
            and any(_holds_name(arg, names_re) for arg in node.args)
        ):
            spans.append((node.lineno, node.end_lineno or node.lineno))
    return spans


def _line_of(source: str, index: int) -> int:
    return source.count("\n", 0, index) + 1


def _script_import_lines(source: str, rel: str, targets: set[str]) -> set[int]:
    """Lines of the relative ``import``/``require`` statements that load one of ``targets``."""
    lines: set[int] = set()
    for match in _SCRIPT_IMPORT.finditer(source):
        spec = match.group("spec")
        if not spec.startswith("."):
            continue
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(rel), spec))
        if resolved in targets or any(f"{resolved}{suffix}" in targets for suffix in _SCRIPT_SUFFIXES):
            lines.add(_line_of(source, match.start("spec")))
    return lines


def _closing(text: str, start: int) -> int:
    """Index of the bracket closing the one at ``start`` (skipping strings), or -1."""
    depth = 0
    quote = ""
    index = start
    while index < len(text):
        char = text[index]
        if quote:
            if char == "\\":
                index += 1
            elif char == quote:
                quote = ""
        elif char in "'\"`":
            quote = char
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return -1


def _script_list_holds_name(args: str, names_re: re.Pattern[str]) -> bool:
    """True when the call arguments ``args`` contain an array literal with one of the names."""
    for match in re.finditer(r"\[", args):
        before = args[: match.start()].rstrip()[-1:]
        if before and (before.isalnum() or before in "_$)]"):
            continue  # a subscript, not a literal
        end = _closing(args, match.start())
        if end > 0 and names_re.search(args[match.start() : end + 1]):
            return True
    return False


def _script_exact_spans(source: str, names_re: re.Pattern[str]) -> list[tuple[int, int]]:
    """(first, last) lines of the deep-equality assertions on an array holding a name."""
    spans: list[tuple[int, int]] = []
    for match in _SCRIPT_EXACT_CALL.finditer(source):
        opening = match.end() - 1
        closing = _closing(source, opening)
        if closing > 0 and _script_list_holds_name(source[opening + 1 : closing], names_re):
            spans.append((_line_of(source, match.start()), _line_of(source, closing)))
    return spans


def _flat(text: str) -> str:
    """``text`` on one line, with runs of whitespace collapsed."""
    return " ".join(text.split())


def _scan_file(
    rel: str,
    source: str,
    modules: set[str],
    scripts: set[str],
    names_re: re.Pattern[str] | None,
    definition_re: re.Pattern[str] | None,
) -> list[tuple[int, Hit]]:
    """The ranked hits of one test file; none when it neither imports nor mentions a target."""
    lines = source.split("\n")
    imports: set[int] = set()
    exact: list[tuple[int, int]] = []
    if rel.endswith(".py"):
        tree = _parse(source)
        if tree is not None:
            try:
                imports = _python_import_lines(tree, rel, modules)
                exact = _python_exact_spans(tree, names_re) if names_re else []
            except RecursionError:
                log.debug("%s is nested too deeply to analyse", rel)
    else:
        imports = _script_import_lines(source, rel, scripts)
        exact = _script_exact_spans(source, names_re) if names_re else []

    mentions: dict[str, int] = {}
    if names_re:
        for number, line in enumerate(lines, 1):
            for match in names_re.finditer(line):
                mentions.setdefault(match.group(0), number)
    if not imports and not mentions:
        return []

    # Later writes win, so a line keeps its strongest reason.
    found: dict[int, tuple[int, str]] = {}
    for number in mentions.values():
        found[number] = (_RANK_MENTION, _flat(lines[number - 1]))
    for number in imports:
        found[number] = (_RANK_IMPORT, _flat(lines[number - 1]))
    if definition_re:
        for number, line in enumerate(lines, 1):
            if definition_re.search(line):
                found[number] = (_RANK_EXACT, _flat(line))
    for first, last in exact:
        found[first] = (_RANK_EXACT, _flat(" ".join(lines[first - 1 : last]))[:MAX_HIT_TEXT_CHARS])
    return [(rank, Hit(rel, number, text)) for number, (rank, text) in found.items()]


def find_related(repo_root: Path, files: Sequence[str], names: Sequence[str]) -> list[Hit]:
    """Find the test lines a change to ``files`` / ``names`` could break.

    Looks at test files that import one of ``files`` or mention one of
    ``names``. From those it reports the import lines, the first mention of
    each name, the definitions of a name (``def``, ``async def``, ``function``)
    and the assertions of an exact list or tuple containing a name.

    Args:
        repo_root: the checkout to search.
        files: repo-relative paths being changed.
        names: function, method or other identifiers being changed.

    Returns:
        At most 40 hits (the definitions and exact lists first when there are
        more), ordered by path and line.
    """
    root = Path(repo_root)
    paths = [str(f).strip().replace("\\", "/").removeprefix("./") for f in files]
    modules = {module for module in map(_python_module, paths) if module}
    scripts = {path for path in paths if path.endswith(_SCRIPT_SUFFIXES)}
    wanted = sorted({str(n).strip() for n in names if str(n).strip()}, key=lambda n: (-len(n), n))
    names_re: re.Pattern[str] | None = None
    definition_re: re.Pattern[str] | None = None
    if wanted:
        alternatives = "|".join(re.escape(n) for n in wanted)
        names_re = re.compile(rf"(?<![\w$])(?:{alternatives})(?![\w$])")
        definition_re = re.compile(rf"(?<![\w$])(?:async\s+def|def|function)\s+(?:{alternatives})\s*\(")

    ranked: list[tuple[int, Hit]] = []
    for rel in _test_files(root):
        source = _read(root / rel)
        if source is not None:
            ranked.extend(_scan_file(rel, source, modules, scripts, names_re, definition_re))
    ranked.sort(key=lambda item: (item[0], item[1].path, item[1].line))
    return sorted((hit for _, hit in ranked[:MAX_HITS]), key=lambda hit: (hit.path, hit.line))


def _accepts_timeout(chat: Any) -> bool:
    """True when a client's ``chat`` takes a ``timeout`` keyword."""
    try:
        params = inspect.signature(chat).parameters
    except (TypeError, ValueError):
        return False
    return "timeout" in params or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


async def _ask_json(gemma: Any, system: str, user: str, schema: dict[str, Any], timeout: float) -> Any:
    """One bounded JSON question to Gemma; raises on any failure (the public functions catch it)."""
    kwargs: dict[str, Any] = {"json_schema": schema, "temperature": 0.1}
    if _accepts_timeout(gemma.chat):
        kwargs["timeout"] = timeout
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    reply = await asyncio.wait_for(gemma.chat(messages, **kwargs), timeout)
    return reply.parse_json()


def _grounded_notes(data: Any, hits: Sequence[Hit]) -> list[str]:
    """Format Gemma's notes, keeping only the ones on a (path, line) of ``hits``."""
    items = data.get("notes") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    allowed = {(hit.path, hit.line) for hit in hits}
    notes: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        path, line, note = item.get("path"), item.get("line"), item.get("note")
        if not isinstance(path, str) or not isinstance(line, int) or isinstance(line, bool):
            continue
        if not isinstance(note, str) or (path, line) not in allowed:
            continue
        text = _flat(note)[:MAX_NOTE_CHARS]
        formatted = f"{path}:{line}: {text}"
        if text and formatted not in notes:
            notes.append(formatted)
        if len(notes) == MAX_NOTES:
            break
    return notes


async def pitfall_notes(gemma: Any, order_text: str, hits: list[Hit], *, timeout: float) -> list[str]:
    """Ask Gemma what in ``hits`` could break if the order is implemented naively.

    Args:
        gemma: a client with an async ``chat`` (see ``foundry.gemma_client``).
        order_text: the work order.
        hits: what :func:`find_related` found.
        timeout: seconds the call may take.

    Returns:
        At most 5 ``"<path>:<line>: <note>"`` strings, each on a line of
        ``hits``; ``[]`` on any error, timeout or empty answer.
    """
    try:
        shown = hits[:MAX_HITS]
        if not shown:
            return []
        listing = "\n".join(f"{hit.path}:{hit.line}: {hit.text[:MAX_HIT_TEXT_CHARS]}" for hit in shown)
        system = (
            "You review a work order against existing test code. Point out what could break if the order is "
            "implemented naively, such as a test fake with an older signature or an exact list that must be "
            f"updated. Give at most {MAX_NOTES} notes. Each note must use the exact path and line of one of the "
            "listed lines; never name any other location. Give no notes when nothing could break. "
            'Answer with JSON: {"notes": [{"path": "...", "line": 1, "note": "..."}]}.'
        )
        user = f"Work order:\n{order_text}\n\nLines found in the tests:\n{listing}"
        return _grounded_notes(await _ask_json(gemma, system, user, _NOTES_SCHEMA, timeout), shown)
    except Exception as exc:  # noqa: BLE001 - the notes are advisory
        log.info("Gemma pitfall notes unavailable (%s)", str(exc) or type(exc).__name__)
        return []


def _located_files(items: list[Any], count: int, changed: Sequence[str]) -> dict[int, list[str]]:
    """Map each criterion index to the files Gemma named that really changed."""
    known = set(changed)
    located: dict[int, list[str]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        index, files = item.get("index"), item.get("files")
        if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < count:
            continue
        if not isinstance(files, list):
            continue
        target = located.setdefault(index, [])
        for file in files:
            if isinstance(file, str) and file in known and file not in target:
                target.append(file)
    return located


def _build_brief(data: Any, acceptance: Sequence[str], changed_files: Sequence[str]) -> str:
    """Write the brief from Gemma's selection; ``""`` when the answer is empty."""
    if not isinstance(data, dict):
        return ""
    raw_criteria, raw_unusual = data.get("criteria"), data.get("unusual")
    criteria = raw_criteria if isinstance(raw_criteria, list) else []
    unusual = raw_unusual if isinstance(raw_unusual, list) else []
    if not criteria and not unusual:
        return ""
    located = _located_files(criteria, len(acceptance), changed_files)
    lines: list[str] = []
    for index, criterion in enumerate(acceptance):
        text = _flat(str(criterion))
        files = located.get(index)
        if files:
            lines.append(f"✓ {text[:MAX_CRITERION_CHARS]} — {', '.join(files)}")
        else:
            lines.append(f"? {text} — not located")
    remarks = [_flat(item)[:MAX_UNUSUAL_CHARS] for item in unusual if isinstance(item, str) and item.strip()]
    lines.extend(f"! {remark}" for remark in remarks[:MAX_UNUSUAL])
    return "\n".join(lines)


async def pr_brief(
    gemma: Any,
    order_text: str,
    acceptance: Sequence[str],
    changed_files: Sequence[str],
    diff: str,
    *,
    timeout: float,
) -> str:
    """Ask Gemma which changed files address each criterion and write the brief.

    Args:
        gemma: a client with an async ``chat`` (see ``foundry.gemma_client``).
        order_text: the work order.
        acceptance: the acceptance criteria.
        changed_files: the files the change touched.
        diff: the change; only its first 12,000 characters are sent.
        timeout: seconds the call may take.

    Returns:
        One ``"✓ <criterion> — <files>"`` or ``"? <criterion> — not located"``
        line per criterion, then at most 3 ``"! <unusual>"`` lines; ``""`` on
        any error, timeout or empty answer.
    """
    try:
        numbered = "\n".join(f"[{index}] {criterion}" for index, criterion in enumerate(acceptance))
        system = (
            "You map a change to its acceptance criteria. For each criterion, by its number in brackets, list "
            "the changed files that address it, using only exact paths from the list of changed files. Leave a "
            "criterion out when no file addresses it. Under unusual, list anything in the change the order did "
            'not ask for. Answer with JSON: {"criteria": [{"index": 0, "files": ["..."]}], "unusual": ["..."]}.'
        )
        user = (
            f"Work order:\n{order_text}\n\nAcceptance criteria:\n{numbered}\n\n"
            f"Changed files:\n{json.dumps(list(changed_files))}\n\nDiff:\n{diff[:MAX_DIFF_CHARS]}"
        )
        data = await _ask_json(gemma, system, user, _BRIEF_SCHEMA, timeout)
        return _build_brief(data, acceptance, changed_files)
    except Exception as exc:  # noqa: BLE001 - the brief is advisory
        log.info("Gemma PR brief unavailable (%s)", str(exc) or type(exc).__name__)
        return ""
