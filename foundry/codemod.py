"""Codemod tools for builders: delete and move files, delete and move symbols.

Plain code over a workspace root.  Every path is repo-relative and checked to
stay inside the root; nothing is overwritten.  Each function returns a short
text report of what changed and what the caller still has to fix, and raises
:class:`CodemodError` (a ``ValueError``) when it refuses.  ``library`` is any
object with the code library's ``outline`` and ``callers`` methods.
"""

from __future__ import annotations

import ast
from pathlib import Path, PurePosixPath
from typing import Any

__all__ = ["CodemodError", "delete_file", "move_file", "delete_symbol", "move_symbol"]

_MAX_LISTED = 30


class CodemodError(ValueError):
    """The operation was refused; nothing was changed."""


def _resolve(root: str | Path, path: str) -> Path:
    """``path`` (repo-relative) as an absolute path inside ``root``."""
    if not isinstance(path, str) or not path.strip():
        raise CodemodError("The path is empty.")
    text = path.strip().replace("\\", "/")
    if text.startswith("/") or (len(text) > 1 and text[1] == ":"):
        raise CodemodError(f"Path must be repo-relative, not absolute: {path}")
    base = Path(root).resolve()
    target = (base / text).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        raise CodemodError(f"Path is outside the workspace: {path}") from None
    if target == base:
        raise CodemodError(f"Path is the workspace itself: {path}")
    return target


def _rel(root: str | Path, target: Path) -> str:
    return target.relative_to(Path(root).resolve()).as_posix()


def _read_lines(target: Path) -> list[str]:
    try:
        return target.read_bytes().decode("utf-8").splitlines(keepends=True)
    except OSError as exc:
        raise CodemodError(f"Cannot read {target.name}: {exc}") from None
    except UnicodeDecodeError:
        raise CodemodError(f"{target.name} is not UTF-8 text.") from None


def _write_lines(target: Path, lines: list[str]) -> None:
    try:
        target.write_bytes("".join(lines).encode("utf-8"))
    except OSError as exc:
        raise CodemodError(f"Cannot write {target.name}: {exc}") from None


def delete_file(root: str | Path, path: str) -> str:
    """Delete one file."""
    target = _resolve(root, path)
    rel = _rel(root, target)
    if not target.exists():
        raise CodemodError(f"No such file: {rel}")
    if not target.is_file():
        raise CodemodError(f"Not a file (folders are not deleted): {rel}")
    try:
        target.unlink()
    except OSError as exc:
        raise CodemodError(f"Cannot delete {rel}: {exc}") from None
    return (
        f"Deleted {rel}.\n"
        "Still to do: remove imports of it and other references (config, docs, tests) elsewhere."
    )


def move_file(root: str | Path, src: str, dst: str) -> str:
    """Move a file, creating the target's folders; refuses to overwrite."""
    source = _resolve(root, src)
    target = _resolve(root, dst)
    rel_src, rel_dst = _rel(root, source), _rel(root, target)
    if not source.exists():
        raise CodemodError(f"No such file: {rel_src}")
    if not source.is_file():
        raise CodemodError(f"Not a file (folders are not moved): {rel_src}")
    if source == target:
        raise CodemodError("Source and target are the same file.")
    if target.exists():
        raise CodemodError(f"Refusing to overwrite {rel_dst}.")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        source.replace(target)
    except OSError as exc:
        raise CodemodError(f"Cannot move {rel_src}: {exc}") from None
    return (
        f"Moved {rel_src} to {rel_dst}.\n"
        "Still to do: update imports and other references that name the old path."
    )


def _find_entry(root: str | Path, library: Any, call: str) -> tuple[Path, str, dict[str, Any], list[str]]:
    """``(file, rel path, entry, file lines)`` for a call number."""
    if not isinstance(call, str) or "::" not in call:
        raise CodemodError(f"Not a call number: {call}. Use `find` to look up the right one.")
    path = call.split("::", 1)[0]
    target = _resolve(root, path)
    entry = next((e for e in library.outline(path) if e["call"] == call), None)
    if entry is None:
        raise CodemodError(f"Unknown call number: {call}. Use `find` to look up the right one.")
    lines = _read_lines(target)
    if not 1 <= entry["start"] <= entry["end"] <= len(lines):
        raise CodemodError(f"The library is out of date for {call}.")
    return target, _rel(root, target), entry, lines


def _cut_range(lines: list[str], entry: dict[str, Any], python: bool) -> tuple[int, int]:
    """0-based ``[first, last)`` lines to cut; Python also takes the blank lines right after."""
    first, last = entry["start"] - 1, entry["end"]
    if python:
        while last < len(lines) and not lines[last].strip():
            last += 1
    return first, last


def _parses(lines: list[str]) -> bool:
    try:
        ast.parse("".join(lines))
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return False
    return True


def _caller_lines(library: Any, call: str, rel: str, first: int, last: int) -> list[str]:
    """Callers outside the removed lines, shifted for the cut when in the same file."""
    removed = last - first
    out: list[str] = []
    for ref in library.callers(call, limit=200):
        line = ref["line"]
        if ref["path"] == rel:
            if first < line <= last:
                continue
            if line > last:
                line -= removed
        out.append(f"{ref['path']}:{line}  {ref['text']}")
    return out


def _caller_text(callers: list[str]) -> str:
    if not callers:
        return "No other callers were found (the search is by name)."
    shown = callers[:_MAX_LISTED]
    more = f"\n... and {len(callers) - len(shown)} more" if len(callers) > len(shown) else ""
    return "Callers (by name; line numbers are after the edit):\n" + "\n".join(shown) + more


def delete_symbol(root: str | Path, library: Any, call: str) -> str:
    """Remove a function or class (Python: decorators and the blank lines after it too)."""
    target, rel, entry, lines = _find_entry(root, library, call)
    python = rel.endswith(".py")
    first, last = _cut_range(lines, entry, python)
    kept = lines[:first] + lines[last:]
    if python:
        if first > 0 and last >= len(lines):
            while kept and not kept[-1].strip():
                kept.pop()
            if kept and not kept[-1].endswith(("\n", "\r")):
                kept[-1] += "\n"
        if _parses(lines) and not _parses(kept):
            raise CodemodError(
                f"Deleting {call} would leave {rel} invalid Python (the last statement of a block?). "
                "Nothing was changed; edit the file by hand."
            )
    callers = _caller_lines(library, call, rel, first, last)
    _write_lines(target, kept)
    return (
        f"Deleted {call} from {rel}: lines {first + 1}-{last} ({last - first} lines removed, "
        f"{len(kept)} left).\n"
        "Still to do: remove the callers and imports of it.\n" + _caller_text(callers)
    )


def _module_name(path: str) -> str:
    parts = list(PurePosixPath(path).with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _module_names(tree: ast.Module) -> dict[str, str]:
    """Names bound at the top of a module, with a note on how."""
    names: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names[node.name] = f"defined at line {node.lineno}"
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            note = f"imported (line {node.lineno})"
            for alias in node.names:
                bound = alias.asname or alias.name.split(".")[0]
                names[bound] = note
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                for sub in ast.walk(t):
                    if isinstance(sub, ast.Name):
                        names[sub.id] = f"defined at line {node.lineno}"
    return names


def _import_insert_index(tree: ast.Module, lines: list[str]) -> tuple[int, bool, bool]:
    """``(index, blank before, blank after)`` for a new top-level import."""
    last_import = None
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            last_import = node
    if last_import is not None:
        return int(last_import.end_lineno or last_import.lineno), False, False
    first = tree.body[0] if tree.body else None
    if (
        isinstance(first, ast.Expr)
        and isinstance(first.value, ast.Constant)
        and isinstance(first.value.value, str)
    ):
        return int(first.end_lineno or first.lineno), True, bool(tree.body[1:])
    return 0, False, bool(lines)


def move_symbol(
    root: str | Path, library: Any, call: str, to_path: str, reexport: bool = False
) -> str:
    """Move a top-level Python function or class to another module (Python only)."""
    target_src, rel, entry, lines = _find_entry(root, library, call)
    if not rel.endswith(".py"):
        raise CodemodError("move_symbol is not supported for this language: only Python files.")
    dest = _resolve(root, to_path)
    rel_dest = _rel(root, dest)
    if not rel_dest.endswith(".py"):
        raise CodemodError("move_symbol is not supported for this language: the target must be a .py file.")
    qname = call.split("::", 1)[1]
    if entry["kind"] == "method" or "." in qname:
        raise CodemodError(f"{call} is a method: move whole functions or classes, not methods.")
    if dest == target_src:
        raise CodemodError("Source and target are the same file.")
    if dest.exists() and not dest.is_file():
        raise CodemodError(f"Target is not a file: {rel_dest}")
    try:
        tree = ast.parse("".join(lines))
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        raise CodemodError(f"{rel} is not valid Python; nothing was changed.") from None
    node = next(
        (
            n
            for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name == qname
        ),
        None,
    )
    if node is None:
        raise CodemodError(f"{call} is not a top-level function or class.")

    first, last = _cut_range(lines, entry, True)
    block = lines[entry["start"] - 1 : entry["end"]]
    if not block[-1].endswith(("\n", "\r")):
        block[-1] += "\n"
    remaining = lines[:first] + lines[last:]
    if first > 0 and last >= len(lines):
        while remaining and not remaining[-1].strip():
            remaining.pop()
        if remaining and not remaining[-1].endswith(("\n", "\r")):
            remaining[-1] += "\n"

    existing = _read_lines(dest) if dest.exists() else []
    if existing:
        try:
            dest_tree = ast.parse("".join(existing))
        except (SyntaxError, ValueError, RecursionError, MemoryError):
            raise CodemodError(f"{rel_dest} is not valid Python; nothing was changed.") from None
        if qname in _module_names(dest_tree):
            raise CodemodError(f"{rel_dest} already defines `{qname}`; refusing to overwrite it.")
    appended = list(existing)
    while appended and not appended[-1].strip():
        appended.pop()
    if appended:
        if not appended[-1].endswith(("\n", "\r")):
            appended[-1] += "\n"
        appended.append("\n")
    appended.extend(block)

    if reexport:
        new_tree = ast.parse("".join(remaining)) if remaining else ast.Module(body=[], type_ignores=[])
        index, blank_before, blank_after = _import_insert_index(new_tree, remaining)
        insert = [f"from {_module_name(rel_dest)} import {qname}\n"]
        if blank_before:
            insert.insert(0, "\n")
        if blank_after:
            insert.append("\n")
        remaining[index:index] = insert
    if not _parses(remaining) or not _parses(appended):
        raise CodemodError("The move would produce invalid Python; nothing was changed.")

    names = _module_names(tree)
    used = sorted(
        {n.id for n in ast.walk(node) if isinstance(n, ast.Name)} & (set(names) - {qname})
    )
    callers = _caller_lines(library, call, rel, first, last)

    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise CodemodError(f"Cannot create the folder for {rel_dest}: {exc}") from None
    _write_lines(dest, appended)
    _write_lines(target_src, remaining)

    out = [
        f"Moved {call} ({len(block)} lines) from {rel} to {rel_dest}"
        + (f" and added `from {_module_name(rel_dest)} import {qname}` to {rel}." if reexport else "."),
    ]
    if used:
        out.append(
            f"The moved code uses names from {rel}; add the imports {rel_dest} needs:\n"
            + "\n".join(f"  {name}  ({names[name]} in {rel})" for name in used)
        )
    else:
        out.append(f"The moved code uses no names defined or imported in {rel}.")
    out.append(
        "Still to do: check the imports in "
        + rel_dest
        + (", and update the callers that import it from the old module.\n" if not reexport else ".\n")
        + _caller_text(callers)
    )
    return "\n".join(out)
