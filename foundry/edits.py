"""Structured edits: a safer alternative to raw unified diffs from builders.

A builder that has to change existing code often cannot see enough of a file
to write a diff whose context lines match exactly (``git apply`` then rejects
the hunk), or it re-writes a function it only saw as an outline and creates a
duplicate definition. Structured edits avoid both: the builder names *what*
to change (a symbol, or an exact snippet) and Foundry works out the lines and
produces the unified diff itself, so gates, review, patching and PRs are
unchanged.

The builder returns a fenced block::

    ```edits
    {"edits": [
      {"op": "replace_symbol", "file": "pkg/mod.py", "symbol": "Cls.method",
       "code": "def method(self):\\n    return 2\\n"}
    ]}
    ```

Operations (``file`` is a workspace-relative POSIX path):

* ``replace_symbol {file, symbol, code}`` - replace a top-level function or
  class, or ``Class.method`` (decorators included). ``code`` must define
  exactly that symbol. Python only.
* ``insert_after_symbol`` / ``insert_before_symbol {file, symbol, code}`` -
  insert ``code`` next to a symbol, at the symbol's indentation. Python only.
* ``append {file, code}`` - add ``code`` at the end of the file.
* ``search_replace {file, old, new}`` - ``old`` must occur exactly once
  (whitespace-exact).
* ``create_file {file, code}`` - the file must not exist yet.
* ``delete_symbol {file, symbol}`` - remove a symbol. Python only.

:func:`apply_edits` validates every edit (allowed paths, the result parses,
no duplicate definitions introduced) and returns ``(diff_text, errors)``.
Errors are one-line strings meant to be sent back to the builder as a retry
hint. Pure standard library.
"""

from __future__ import annotations

import ast
import difflib
import json
import re
import textwrap
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from foundry.validation_gates import (
    DiffApplyError,
    _is_allowed,
    _normalise_rel,
    _resolve_inside,
)

__all__ = [
    "EDIT_OPS",
    "Edit",
    "EditError",
    "apply_edits",
    "extract_edits_block",
    "has_edits_block",
    "list_symbols",
    "parse_edits",
]

EDIT_OPS: tuple[str, ...] = (
    "replace_symbol",
    "insert_after_symbol",
    "insert_before_symbol",
    "append",
    "search_replace",
    "create_file",
    "delete_symbol",
)
_SYMBOL_OPS = frozenset(
    {"replace_symbol", "insert_after_symbol", "insert_before_symbol", "delete_symbol"}
)
_CODE_OPS = frozenset(
    {"replace_symbol", "insert_after_symbol", "insert_before_symbol", "append", "create_file"}
)

# ```edits ... ``` with the closing fence on its own line. JSON strings never
# hold a raw newline, so a line starting with ``` cannot occur inside them.
_EDITS_BLOCK_RE = re.compile(r"^[ \t]*```[ \t]*edits[^\n]*\n(.*?)^[ \t]*```[ \t]*$", re.M | re.S)

# Decorators that legitimately define the same name twice.
_REDEFINE_OK = re.compile(r"(^|\.)(overload|setter|getter|deleter|register)$")


class EditError(ValueError):
    """Raised for an edits payload that cannot be parsed."""


@dataclass(frozen=True)
class Edit:
    """One structured edit."""

    op: str
    file: str
    symbol: str | None = None
    code: str | None = None
    old: str | None = None
    new: str | None = None

    def label(self) -> str:
        """Short description used in error messages."""
        target = f"{self.file}:{self.symbol}" if self.symbol else self.file
        return f"{self.op} {target}"


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def extract_edits_block(text: str) -> str | None:
    """Return the body of the first ```edits fenced block in ``text`` (or None)."""
    if not text:
        return None
    match = _EDITS_BLOCK_RE.search(text)
    return match.group(1) if match else None


def has_edits_block(text: str) -> bool:
    """True when ``text`` contains an ```edits fenced block."""
    return extract_edits_block(text) is not None


def _edit_from_mapping(raw: Mapping[str, Any], index: int) -> Edit:
    where = f"edit {index}"
    op = raw.get("op") or raw.get("type") or raw.get("action")
    if not isinstance(op, str) or not op.strip():
        raise EditError(f"{where}: missing \"op\" (one of {', '.join(EDIT_OPS)})")
    op = op.strip()
    if op not in EDIT_OPS:
        raise EditError(f"{where}: unknown op {op!r} (use one of {', '.join(EDIT_OPS)})")
    file = raw.get("file", raw.get("path"))
    if not isinstance(file, str) or not file.strip():
        raise EditError(f"{where} ({op}): missing \"file\"")
    symbol = raw.get("symbol", raw.get("name"))
    code = raw.get("code", raw.get("content"))
    old = raw.get("old", raw.get("search"))
    new = raw.get("new", raw.get("replace"))
    for key, value in (("symbol", symbol), ("code", code), ("old", old), ("new", new)):
        if value is not None and not isinstance(value, str):
            raise EditError(f"{where} ({op} {file}): \"{key}\" must be a string")
    if op in _SYMBOL_OPS and not (symbol and symbol.strip()):
        raise EditError(f"{where} ({op} {file}): missing \"symbol\"")
    if op in _CODE_OPS and (code is None or not code.strip()):
        raise EditError(f"{where} ({op} {file}): missing or empty \"code\"")
    if op == "search_replace":
        if not old:
            raise EditError(f"{where} (search_replace {file}): missing or empty \"old\"")
        if new is None:
            raise EditError(f"{where} (search_replace {file}): missing \"new\"")
    return Edit(
        op=op,
        file=file.strip(),
        symbol=symbol.strip() if symbol else None,
        code=code,
        old=old,
        new=new,
    )


def parse_edits(source: str | Mapping[str, Any] | Sequence[Any]) -> list[Edit]:
    """Parse an edits payload into :class:`Edit` objects.

    ``source`` may be a builder reply containing an ```edits block, the raw
    JSON text, a ``{"edits": [...]}`` mapping or a list of edit mappings.

    Raises:
        EditError: with a precise one-line message on any problem.
    """
    data: Any = source
    if isinstance(source, str):
        body = extract_edits_block(source)
        text = body if body is not None else source
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise EditError(
                f"edits block is not valid JSON: {exc.msg} at line {exc.lineno} column {exc.colno}"
            ) from None
    if isinstance(data, Mapping):
        if "edits" not in data:
            if "op" in data:
                data = [data]
            else:
                raise EditError('edits JSON must be an object {"edits": [...]}')
        else:
            data = data["edits"]
    if not isinstance(data, Sequence) or isinstance(data, (str, bytes)):
        raise EditError('"edits" must be a list of edit objects')
    if not data:
        raise EditError('"edits" is empty')
    edits: list[Edit] = []
    for index, raw in enumerate(data, start=1):
        if isinstance(raw, Edit):
            edits.append(raw)
            continue
        if not isinstance(raw, Mapping):
            raise EditError(f"edit {index}: must be a JSON object")
        edits.append(_edit_from_mapping(raw, index))
    return edits


# --------------------------------------------------------------------------
# Python symbol helpers
# --------------------------------------------------------------------------

_DefNode = ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef


def _defs(body: Iterable[ast.stmt]) -> list[_DefNode]:
    return [n for n in body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]


def _start_line(node: _DefNode) -> int:
    return min([node.lineno] + [d.lineno for d in node.decorator_list])


def _decorator_name(dec: ast.expr) -> str:
    target = dec.func if isinstance(dec, ast.Call) else dec
    try:
        return ast.unparse(target)
    except Exception:  # pragma: no cover - defensive
        return ""


def list_symbols(text: str) -> list[tuple[str, int, int]]:
    """Return ``(qualified_name, start, end)`` for top-level defs and their methods.

    Line numbers are 1-based and include decorators. Raises ``SyntaxError``.
    """
    tree = ast.parse(text)
    out: list[tuple[str, int, int]] = []

    def walk(body: Iterable[ast.stmt], prefix: str) -> None:
        for node in _defs(body):
            name = f"{prefix}{node.name}"
            out.append((name, _start_line(node), node.end_lineno or node.lineno))
            if isinstance(node, ast.ClassDef):
                walk(node.body, name + ".")

    walk(tree.body, "")
    return out


def _find_symbol(tree: ast.Module, symbol: str) -> _DefNode | None:
    body: list[ast.stmt] = tree.body
    found: _DefNode | None = None
    for part in symbol.split("."):
        matches = [n for n in _defs(body) if n.name == part]
        if not matches:
            return None
        found = matches[-1]  # the effective (last) definition
        body = found.body if isinstance(found, ast.ClassDef) else []
    return found


def _symbol_hint(tree: ast.Module, symbol: str) -> str:
    parent, _, _ = symbol.rpartition(".")
    if parent:
        owner = _find_symbol(tree, parent)
        if owner is None:
            return f"{parent!r} not found"
        names = [n.name for n in _defs(owner.body)] if isinstance(owner, ast.ClassDef) else []
        scope = f"{parent} members"
    else:
        names = [n.name for n in _defs(tree.body)]
        scope = "top-level symbols"
    close = difflib.get_close_matches(symbol.rpartition(".")[2], names, n=3)
    if close:
        return f"did you mean {', '.join(close)}?"
    shown = ", ".join(names[:15]) + (", ..." if len(names) > 15 else "")
    return f"{scope}: {shown or '(none)'}"


def _indent_of(line: str) -> str:
    return line[: len(line) - len(line.lstrip(" \t"))]


def _prepare_code(code: str, indent: str) -> list[str]:
    """Dedent ``code``, strip surrounding blank lines and re-indent to ``indent``."""
    lines = textwrap.dedent(code.replace("\r\n", "\n").replace("\r", "\n")).split("\n")
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return [(indent + ln) if ln.strip() else "" for ln in lines]


def _check_defines(code: str, symbol: str) -> str | None:
    """Return an error when ``code`` does not define exactly ``symbol``."""
    name = symbol.rpartition(".")[2]
    try:
        tree = ast.parse(textwrap.dedent(code))
    except SyntaxError as exc:
        return f"code does not parse: line {exc.lineno}: {exc.msg}"
    others = [n for n in tree.body if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))]
    defs = _defs(others)
    if len(others) != 1 or len(defs) != 1:
        return f"code must define exactly {name!r} and nothing else (found {len(others)} statements)"
    if defs[0].name != name:
        return f"code defines {defs[0].name!r} but the symbol is {name!r}"
    return None


def _duplicates(text: str) -> dict[str, list[int]]:
    """Map qualified def names defined more than once to their line numbers."""
    tree = ast.parse(text)
    seen: dict[str, list[int]] = {}

    def walk(body: Iterable[ast.stmt], prefix: str) -> None:
        for node in _defs(body):
            if any(_REDEFINE_OK.search(_decorator_name(d)) for d in node.decorator_list):
                continue
            key = f"{prefix}{node.name}"
            seen.setdefault(key, []).append(node.lineno)
            if isinstance(node, ast.ClassDef):
                walk(node.body, key + ".")

    walk(tree.body, "")
    return seen


# --------------------------------------------------------------------------
# Applying
# --------------------------------------------------------------------------


@dataclass
class _FileState:
    rel: str
    original: str | None  # None for a new file
    text: str
    newline: str


def _split(text: str) -> tuple[list[str], bool]:
    """Split into lines without endings; report whether text ended with a newline."""
    if not text:
        return [], True
    lines = text.replace("\r\n", "\n").split("\n")
    ends = lines[-1] == ""
    if ends:
        lines.pop()
    return lines, ends


def _join(lines: list[str], newline: str, ends: bool = True) -> str:
    if not lines:
        return ""
    return newline.join(lines) + (newline if ends else "")


def _collapse_blanks(lines: list[str], at: int, keep: int) -> None:
    """Leave at most ``keep`` blank lines around index ``at`` (none at file edges)."""
    start = at
    while start > 0 and not lines[start - 1].strip():
        start -= 1
    end = at
    while end < len(lines) and not lines[end].strip():
        end += 1
    if start == 0 or end == len(lines):
        keep = 0
    if end - start > keep:
        del lines[start + keep : end]


def _apply_one(state: _FileState, edit: Edit) -> None:
    """Apply ``edit`` to ``state.text`` in place. Raises ``ValueError`` with a message."""
    is_py = state.rel.endswith(".py")
    text = state.text
    if edit.op == "search_replace":
        assert edit.old is not None and edit.new is not None
        old = edit.old
        if "\r\n" in text and "\r\n" not in old:
            old_n, new_n = old.replace("\n", "\r\n"), edit.new.replace("\n", "\r\n")
        else:
            old_n, new_n = old, edit.new
        count = text.count(old_n)
        if count != 1:
            hint = ""
            if count == 0:
                loose = " ".join(old.split())
                if loose and loose in " ".join(text.split()):
                    hint = " (it matches if whitespace is ignored: copy it exactly)"
                else:
                    hint = " (copy it exactly from the file, or use replace_symbol)"
            else:
                hint = " (add surrounding lines so it is unique)"
            raise ValueError(f"\"old\" must occur exactly once but occurs {count} times{hint}")
        state.text = text.replace(old_n, new_n, 1)
        return

    lines, ends = _split(text)
    if edit.op in ("append", "create_file"):
        assert edit.code is not None
        new_lines = _prepare_code(edit.code, "")
        if edit.op == "append" and lines:
            while lines and not lines[-1].strip():
                lines.pop()
            lines.extend([""] * (2 if is_py else 1))
        lines.extend(new_lines)
        state.text = _join(lines, state.newline)
        return

    # Symbol operations (Python only).
    if not is_py:
        raise ValueError(f"{edit.op} only works on .py files; use search_replace")
    assert edit.symbol is not None
    tree = ast.parse(text)
    node = _find_symbol(tree, edit.symbol)
    if node is None:
        raise ValueError(f"symbol {edit.symbol!r} not found ({_symbol_hint(tree, edit.symbol)})")
    start = _start_line(node) - 1
    end = node.end_lineno or node.lineno  # exclusive index
    indent = _indent_of(lines[node.lineno - 1])
    gap = 1 if indent else 2

    if edit.op == "replace_symbol":
        assert edit.code is not None
        problem = _check_defines(edit.code, edit.symbol)
        if problem:
            raise ValueError(problem)
        lines[start:end] = _prepare_code(edit.code, indent)
    elif edit.op == "delete_symbol":
        del lines[start:end]
        _collapse_blanks(lines, min(start, len(lines)), gap)
    elif edit.op == "insert_after_symbol":
        assert edit.code is not None
        lines[end:end] = [""] * gap + _prepare_code(edit.code, indent)
        after = end + gap + len(_prepare_code(edit.code, indent))
        if after < len(lines) and lines[after].strip():
            lines[after:after] = [""] * gap
    elif edit.op == "insert_before_symbol":
        assert edit.code is not None
        block = _prepare_code(edit.code, indent) + [""] * gap
        lines[start:start] = block
    state.text = _join(lines, state.newline, ends)


def _keep_ends(text: str) -> list[str]:
    """Split on ``\\n`` only (like git), keeping line endings."""
    parts = text.split("\n")
    out = [part + "\n" for part in parts[:-1]]
    if parts[-1]:
        out.append(parts[-1])
    return out


def _make_diff(state: _FileState) -> str:
    """Unified diff (git style, a/ b/ prefixes) from ``state.original`` to ``state.text``."""
    rel = state.rel
    old_lines = _keep_ends(state.original or "")
    new_lines = _keep_ends(state.text)
    if state.original is None:
        head = f"diff --git a/{rel} b/{rel}\nnew file mode 100644\n--- /dev/null\n+++ b/{rel}\n"
    else:
        head = f"diff --git a/{rel} b/{rel}\n--- a/{rel}\n+++ b/{rel}\n"
    body: list[str] = []
    diff_lines = list(difflib.unified_diff(old_lines, new_lines, n=3, lineterm="\n"))
    for line in diff_lines[2:]:  # drop difflib's own ---/+++ header
        body.append(line)
        if not line.endswith("\n"):
            body.append("\n\\ No newline at end of file\n")
    if not body:
        return ""
    return head + "".join(body)


def apply_edits(
    workspace: str | Path,
    edits: str | Mapping[str, Any] | Sequence[Any],
    allowed_files: Iterable[str] | None = None,
    *,
    write: bool = False,
) -> tuple[str, list[str]]:
    """Apply structured edits to the files under ``workspace`` and return a diff.

    Edits are applied in order, in memory, each to the result of the previous
    ones. The workspace is left untouched unless ``write=True`` (then the new
    contents are written only when there are no errors).

    Args:
        workspace: Directory the edit paths are relative to (repo root or a
            scratch copy).
        edits: Builder reply text with an ```edits block, the JSON text, a
            ``{"edits": [...]}`` mapping or a list of edits.
        allowed_files: Paths, globs or ``dir/`` prefixes the edits may touch
            (same rules as :func:`foundry.validation_gates.apply_diff`);
            ``None`` means no restriction.
        write: Also write the changed files into ``workspace``.

    Returns:
        ``(diff_text, errors)``. ``diff_text`` is a unified diff (``a/``/``b/``
        paths) that applies with ``git apply`` to the original files; it is
        ``""`` whenever ``errors`` is non-empty. Each error is a one-line hint
        such as ``edit 2 (append pkg/m.py): 'foo' already defined at line 40 -
        use replace_symbol``.
    """
    try:
        parsed = parse_edits(edits)
    except EditError as exc:
        return "", [str(exc)]
    root = Path(workspace)
    if not root.is_dir():
        return "", [f"workspace does not exist: {root}"]
    allowed = list(allowed_files) if allowed_files is not None else None
    states: dict[str, _FileState] = {}
    order: list[str] = []
    errors: list[str] = []

    for index, edit in enumerate(parsed, start=1):
        where = f"edit {index} ({edit.label()})"
        try:
            rel = _normalise_rel(edit.file)
            if not _is_allowed(rel, allowed):
                raise ValueError(f"path not in allowed_files: {rel}")
            target = _resolve_inside(root, rel)
        except (DiffApplyError, ValueError) as exc:
            errors.append(f"{where}: {exc}")
            continue

        state = states.get(rel)
        if state is None:
            if edit.op == "create_file":
                if target.exists():
                    errors.append(f"{where}: file already exists; edit it instead of create_file")
                    continue
                state = _FileState(rel, None, "", "\n")
            else:
                if not target.is_file():
                    errors.append(f"{where}: file does not exist (use create_file for new files)")
                    continue
                try:
                    original = target.read_bytes().decode("utf-8")
                except UnicodeDecodeError:
                    errors.append(f"{where}: file is not UTF-8 text")
                    continue
                newline = "\r\n" if "\r\n" in original else "\n"
                state = _FileState(rel, original, original, newline)
                if rel.endswith(".py"):
                    try:
                        ast.parse(original)
                    except SyntaxError as exc:
                        if edit.op in _SYMBOL_OPS:
                            errors.append(
                                f"{where}: file does not parse before editing (line {exc.lineno}); "
                                "use search_replace"
                            )
                            continue
            states[rel] = state
            order.append(rel)
        elif edit.op == "create_file":
            errors.append(f"{where}: file was already created or edited by an earlier edit")
            continue

        before_text = state.text
        try:
            before_dups = _duplicates(before_text) if rel.endswith(".py") and before_text else {}
        except SyntaxError:
            before_dups = {}
        try:
            _apply_one(state, edit)
        except (ValueError, SyntaxError) as exc:
            state.text = before_text
            errors.append(f"{where}: {exc}")
            continue

        if rel.endswith(".py"):
            try:
                after_dups = _duplicates(state.text)
            except SyntaxError as exc:
                state.text = before_text
                errors.append(f"{where}: result does not parse: line {exc.lineno}: {exc.msg}")
                continue
            clash = next(
                (
                    (name, nums)
                    for name, nums in after_dups.items()
                    if len(nums) > 1 and len(nums) > len(before_dups.get(name, []))
                ),
                None,
            )
            if clash is not None:
                name, nums = clash
                state.text = before_text
                errors.append(
                    f"{where}: {name!r} already defined at line {nums[0]} - use replace_symbol"
                )
                continue

    if errors:
        return "", errors
    parts = [_make_diff(states[rel]) for rel in order]
    diff = "".join(p for p in parts if p)
    if not diff:
        return "", ["edits made no changes"]
    if write:
        for rel in order:
            state = states[rel]
            target = _resolve_inside(root, rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(state.text.encode("utf-8"))
    return diff, []
