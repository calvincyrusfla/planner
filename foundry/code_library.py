"""The code library: a catalogue of every function, class and module constant with a call number.

:func:`build_library` walks a repo in plain code and writes a SQLite file. A
*call number* is ``<repo-relative posix path>::<qualified name>``, for example
``foundry/floor_manager.py::FloorManager.pr_request``. :class:`Library` answers
lookups by call number so a builder can read one section instead of a whole file.

Callers are found by *name*: two functions with the same short name share their
callers. That is accepted; the catalogue is a guide, not a call graph.
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import os
import re
import sqlite3
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

__all__ = ["Library", "build_library", "open_library"]

MAX_FILE_BYTES = 2_000_000
_SKIP_DIRS = {"node_modules", "__pycache__", "venv", ".venv", "build", "dist", "data"}
_PY_SUFFIXES = (".py",)
_JS_SUFFIXES = (".js", ".mjs", ".cjs", ".ts")
_MAX_TEXT = 200
_MAX_CONSTANT_SIGNATURE = 160
_JS_VALUE_LINES = 20  # how far a constant's initializer is read to tell a multi-line arrow function

_NAME = r"[A-Za-z_$][\w$]*"
_UPPER = r"[0-9_]*[A-Z][A-Z0-9_]*"
_UPPER_NAME = re.compile(_UPPER)
_JS_FN = re.compile(rf"^([ \t]*)(?:export\s+(?:default\s+)?)?(?:async\s+)?function\s*\*?\s*({_NAME})")
_JS_CLASS = re.compile(rf"^([ \t]*)(?:export\s+(?:default\s+)?)?(?:abstract\s+)?class\s+({_NAME})")
_JS_EXPORT_CONST = re.compile(rf"^([ \t]*)export\s+const\s+({_NAME})\s*[=:]")
_JS_ARROW = re.compile(rf"^([ \t]*)const\s+({_NAME})\s*=\s*(?:async\s+)?(?:\([^)]*\)|{_NAME})\s*=>")
_JS_CALL = re.compile(rf"(?<![\w$])({_NAME})\(")
# The lookahead leaves the ``=`` or type annotation in place for _JS_INITIALIZER.
_JS_CONST = re.compile(rf"^(?:export\s+)?const\s+({_UPPER})\s*(?=[=:])")
# Up to and including the assignment ``=``, stepping over any ``=>`` in a type annotation.
_JS_INITIALIZER = re.compile(r"(?:[^=]|=>)*=(?!>)")
_JS_CALLABLE_VALUE = re.compile(
    rf"\s*(?:async\b\s*)?(?:function\b|class\b|{_NAME}\s*=>"
    r"|(?:<[^>]*>\s*)?\((?:[^()]|\([^()]*\))*\)\s*(?::[^=]*)?=>)"
)

_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE files (path TEXT PRIMARY KEY, lines INTEGER NOT NULL, bytes INTEGER NOT NULL,
                    sha1 TEXT NOT NULL, is_test INTEGER NOT NULL);
CREATE TABLE entries (id INTEGER PRIMARY KEY AUTOINCREMENT, call TEXT NOT NULL, kind TEXT NOT NULL,
                      path TEXT NOT NULL, qname TEXT NOT NULL, short TEXT NOT NULL,
                      start INTEGER NOT NULL, "end" INTEGER NOT NULL, signature TEXT NOT NULL);
CREATE INDEX entries_call ON entries(call);
CREATE INDEX entries_path ON entries(path);
CREATE TABLE refs (name TEXT NOT NULL, path TEXT NOT NULL, line INTEGER NOT NULL,
                   text TEXT NOT NULL, js INTEGER NOT NULL);
CREATE INDEX refs_name ON refs(name);
CREATE TABLE test_lines (path TEXT NOT NULL, line INTEGER NOT NULL, text TEXT NOT NULL);
"""

_Entry = tuple[str, str, str, int, int, str]  # kind, qname, short, start, end, signature


def _is_test_path(path: str) -> bool:
    parts = path.split("/")
    name = parts[-1]
    if any(p in ("tests", "test") for p in parts[:-1]):
        return True
    if name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py")):
        return True
    return name.endswith((".test.mjs", ".test.js"))


def _clip(text: str) -> str:
    text = text.strip()
    return text if len(text) <= _MAX_TEXT else text[:_MAX_TEXT]


def _py_signature(lines: list[str], node: Any) -> str:
    last = node.body[0].lineno if node.body else node.lineno
    last = max(node.lineno, min(last, len(lines)))
    text = " ".join(part.strip() for part in lines[node.lineno - 1 : last])
    depth = 0
    quote = ""
    for i, ch in enumerate(text):
        if quote:
            if ch == quote:
                quote = ""
        elif ch in "'\"":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == ":" and depth == 0:
            return text[: i + 1]
    return text


def _py_scan(source: str) -> tuple[list[_Entry], dict[str, set[int]]]:
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return [], {}
    lines = source.splitlines()
    entries: list[_Entry] = []

    def add(node: Any, qname: str, kind: str) -> None:
        start = min([node.lineno] + [d.lineno for d in node.decorator_list])
        end = int(node.end_lineno or node.lineno)
        entries.append((kind, qname, node.name, start, end, _py_signature(lines, node)))

    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            add(node, node.name, "class")
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    add(sub, f"{node.name}.{sub.name}", "method")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            add(node, node.name, "function")
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets: list[ast.expr] = node.targets if isinstance(node, ast.Assign) else [node.target]
            target = targets[0]
            if len(targets) == 1 and isinstance(target, ast.Name) and _UPPER_NAME.fullmatch(target.id):
                signature = lines[node.lineno - 1].strip()[:_MAX_CONSTANT_SIGNATURE]
                end = int(node.end_lineno or node.lineno)
                entries.append(("constant", target.id, target.id, node.lineno, end, signature))

    refs: dict[str, set[int]] = {}
    try:
        for n in ast.walk(tree):
            if isinstance(n, ast.Call):
                f = n.func
                if isinstance(f, ast.Name):
                    refs.setdefault(f.id, set()).add(n.lineno)
                elif isinstance(f, ast.Attribute):
                    refs.setdefault(f.attr, set()).add(n.lineno)
    except RecursionError:
        pass
    return entries, refs


def _js_constant_name(lines: list[str], index: int) -> str | None:
    """The UPPER_CASE name declared at ``index``, unless its value is a function or class."""
    m = _JS_CONST.match(lines[index])
    if m is None:
        return None
    tail = "\n".join([lines[index][m.end() :], *lines[index + 1 : index + _JS_VALUE_LINES]])
    assignment = _JS_INITIALIZER.match(tail)
    if assignment is not None and _JS_CALLABLE_VALUE.match(tail, assignment.end()):
        return None
    return m.group(1)


def _js_statement_end(lines: list[str], start: int, limit: int) -> int:
    """Index of the line closing the statement at ``start``, at most ``limit``."""
    depth = 0
    quote = ""
    for index in range(start, limit + 1):
        escaped = False
        for ch in lines[index]:
            if escaped:
                escaped = False
            elif quote:
                if ch == "\\":
                    escaped = True
                elif ch == quote:
                    quote = ""
            elif ch in "'\"`":
                quote = ch
            elif ch in "([{":
                depth += 1
            elif ch in ")]}":
                depth -= 1
        if quote != "`":  # only template strings span lines
            quote = ""
        if depth <= 0 and not quote and not lines[index].rstrip().endswith(("=", "+")):
            return index
    return limit


def _js_scan(source: str) -> tuple[list[_Entry], dict[str, set[int]]]:
    lines = source.splitlines()
    found: list[tuple[int, int, str, str]] = []  # line index, indent, kind, name
    for i, line in enumerate(lines):
        constant = _js_constant_name(lines, i)
        if constant is not None:
            found.append((i, 0, "constant", constant))
            continue
        for rx, kind in ((_JS_FN, "function"), (_JS_CLASS, "class"), (_JS_EXPORT_CONST, "function"), (_JS_ARROW, "function")):
            m = rx.match(line)
            if m:
                found.append((i, len(m.group(1).expandtabs()), kind, m.group(2)))
                break
    entries: list[_Entry] = []
    for k, (i, indent, kind, name) in enumerate(found):
        end_idx = len(lines) - 1
        for j in range(k + 1, len(found)):
            if found[j][1] <= indent:
                end_idx = found[j][0] - 1
                break
        while end_idx > i and not lines[end_idx].strip():
            end_idx -= 1
        if kind == "constant":
            end_idx = _js_statement_end(lines, i, end_idx)
        sig = _clip(lines[i]).rstrip("{ \t")
        entries.append((kind, name, name, i + 1, end_idx + 1, sig))
    refs: dict[str, set[int]] = {}
    for i, line in enumerate(lines):
        for m in _JS_CALL.finditer(line):
            refs.setdefault(m.group(1), set()).add(i + 1)
    return entries, refs


def _iter_sources(root: Path) -> list[Path]:
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d not in _SKIP_DIRS)
        for fn in sorted(filenames):
            if fn.endswith(_PY_SUFFIXES + _JS_SUFFIXES):
                out.append(Path(dirpath) / fn)
    return out


def build_library(repo_root: str | Path, db_path: str | Path) -> dict[str, Any]:
    """Catalogue ``repo_root`` into a fresh SQLite file at ``db_path`` (replaced atomically)."""
    started = time.monotonic()
    root = Path(repo_root).resolve()
    target = Path(db_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f"{target.name}.tmp-{os.getpid()}")
    if tmp.exists():
        tmp.unlink()
    n_files = 0
    n_entries = 0
    try:
        with contextlib.closing(sqlite3.connect(tmp)) as db:
            db.executescript(_SCHEMA)
            db.execute("INSERT INTO meta VALUES ('repo_root', ?)", (str(root),))
            for file in _iter_sources(root):
                try:
                    if file.stat().st_size > MAX_FILE_BYTES:
                        continue
                    raw = file.read_bytes()
                    if b"\0" in raw:
                        continue
                    source = raw.decode("utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
                rel = file.relative_to(root).as_posix()
                is_js = file.name.endswith(_JS_SUFFIXES)
                entries, refs = _js_scan(source) if is_js else _py_scan(source)
                is_test = _is_test_path(rel)
                text_lines = source.splitlines()
                db.execute(
                    "INSERT INTO files VALUES (?, ?, ?, ?, ?)",
                    (rel, len(text_lines), len(raw), _sha1(raw), int(is_test)),
                )
                n_files += 1
                db.executemany(
                    'INSERT INTO entries (call, kind, path, qname, short, start, "end", signature) '
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [(f"{rel}::{q}", k, rel, q, s, a, b, sig) for k, q, s, a, b, sig in entries],
                )
                n_entries += len(entries)
                db.executemany(
                    "INSERT INTO refs VALUES (?, ?, ?, ?, ?)",
                    [
                        (name, rel, ln, _clip(text_lines[ln - 1]) if ln <= len(text_lines) else "", int(is_js))
                        for name, lns in refs.items()
                        for ln in sorted(lns)
                    ],
                )
                if is_test:
                    db.executemany(
                        "INSERT INTO test_lines VALUES (?, ?, ?)",
                        [(rel, i + 1, _clip(t)) for i, t in enumerate(text_lines) if t.strip()],
                    )
            db.execute("DELETE FROM refs WHERE name NOT IN (SELECT short FROM entries)")
            db.commit()
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            with contextlib.suppress(OSError):
                tmp.unlink()
    return {"files": n_files, "entries": n_entries, "seconds": round(time.monotonic() - started, 3)}


def _sha1(raw: bytes) -> str:
    import hashlib

    return hashlib.sha1(raw).hexdigest()  # noqa: S324 - a content fingerprint, not security


class Library:
    """Read access to a built catalogue."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        with self._connect() as db:
            row = db.execute("SELECT value FROM meta WHERE key = 'repo_root'").fetchone()
        self.root = Path(row[0]) if row else Path(".")

    def _connect(self) -> Any:
        return contextlib.closing(sqlite3.connect(self.db_path))

    @staticmethod
    def _entry(row: Sequence[Any]) -> dict[str, Any]:
        return {
            "call": row[0],
            "kind": row[1],
            "path": row[2],
            "start": row[3],
            "end": row[4],
            "signature": row[5],
        }

    _COLS = 'call, kind, path, start, "end", signature'

    def _lookup(self, db: sqlite3.Connection, call: str) -> Sequence[Any] | None:
        row: Sequence[Any] | None = db.execute(
            f'SELECT {self._COLS}, short FROM entries WHERE call = ? ORDER BY id LIMIT 1', (call,)
        ).fetchone()
        return row

    def find(self, name: str, limit: int = 20) -> list[dict[str, Any]]:
        """Entries matching ``name``: exact short name, then prefix, then substring (case-insensitive)."""
        needle = name.strip().lower()
        if not needle:
            return []
        col = "lower(qname)" if "." in needle else "lower(short)"
        exact_col = "qname" if "." in needle else "short"
        with self._connect() as db:
            rows = db.execute(
                f"SELECT {self._COLS}, {exact_col}, {col} FROM entries WHERE instr({col}, ?) > 0", (needle,)
            ).fetchall()
        ranked: list[tuple[int, str, int, dict[str, Any]]] = []
        for row in rows:
            if row[6] == name.strip():
                rank = 0
            elif row[7].startswith(needle):
                rank = 1
            else:
                rank = 2
            ranked.append((rank, row[2], row[3], self._entry(row)))
        ranked.sort(key=lambda t: (t[0], t[1], t[2]))
        return [r[3] for r in ranked[: max(0, limit)]]

    def show(self, call: str, context: int = 0) -> str | None:
        """The entry's source with ``<line>| `` prefixes, plus ``context`` lines either side."""
        with self._connect() as db:
            row = self._lookup(db, call)
        if row is None:
            return None
        try:
            text = (self.root / row[2]).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None
        lines = text.splitlines()
        pad = max(0, context)
        first = max(1, row[3] - pad)
        last = min(len(lines), row[4] + pad)
        return "\n".join(f"{n}| {lines[n - 1]}" for n in range(first, last + 1))

    def callers(self, call: str, limit: int = 30) -> list[dict[str, Any]]:
        """Call sites of the entry's short name (by name; same-named functions share callers)."""
        with self._connect() as db:
            row = self._lookup(db, call)
            if row is None:
                return []
            path, start, end, short = row[2], row[3], row[4], row[6]
            rows = db.execute(
                "SELECT path, line, text, js FROM refs WHERE name = ? ORDER BY path, line", (short,)
            ).fetchall()
        out: list[dict[str, Any]] = []
        for p, ln, text, js in rows:
            if js and p == path and start <= ln <= end:
                continue
            out.append({"path": p, "line": ln, "text": text})
            if len(out) >= max(0, limit):
                break
        return out

    def tests_for(self, call: str, limit: int = 30) -> list[dict[str, Any]]:
        """Lines in test files that contain the entry's short name as a whole word."""
        with self._connect() as db:
            row = self._lookup(db, call)
            if row is None:
                return []
            path, start, end, short = row[2], row[3], row[4], row[6]
            rows = db.execute(
                "SELECT path, line, text FROM test_lines WHERE instr(text, ?) > 0 ORDER BY path, line", (short,)
            ).fetchall()
        word = re.compile(r"(?<![\w$])" + re.escape(short) + r"(?![\w$])")
        out: list[dict[str, Any]] = []
        for p, ln, text in rows:
            if p == path and start <= ln <= end:
                continue
            if word.search(text):
                out.append({"path": p, "line": ln, "text": text})
                if len(out) >= max(0, limit):
                    break
        return out

    def outline(self, path: str) -> list[dict[str, Any]]:
        """The file's entries in line order."""
        key = path.replace("\\", "/")
        with self._connect() as db:
            rows = db.execute(
                f"SELECT {self._COLS} FROM entries WHERE path = ? ORDER BY start, id", (key,)
            ).fetchall()
        return [self._entry(r) for r in rows]


def open_library(db_path: str | Path) -> Library:
    """Open a built catalogue; raises ``FileNotFoundError`` if it does not exist."""
    if not Path(db_path).is_file():
        raise FileNotFoundError(str(db_path))
    return Library(db_path)


def _fmt_entry(e: dict[str, Any]) -> str:
    return f"{e['call']}  [{e['kind']}] {e['path']}:{e['start']}-{e['end']}  {e['signature']}"


def _fmt_site(s: dict[str, Any]) -> str:
    return f"{s['path']}:{s['line']}: {s['text']}"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m foundry.code_library", description="The code library.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="catalogue a repo")
    b.add_argument("--repo", required=True)
    b.add_argument("--db", required=True)
    for cmd, arg in (("find", "name"), ("show", "call"), ("callers", "call"), ("tests", "call"), ("outline", "path")):
        p = sub.add_parser(cmd)
        p.add_argument(arg)
        p.add_argument("--db", required=True)
        if cmd == "show":
            p.add_argument("--context", type=int, default=0)
    args = parser.parse_args(argv)

    if args.cmd == "build":
        info = build_library(args.repo, args.db)
        print(f"{info['files']} files, {info['entries']} entries in {info['seconds']}s")
        return 0
    try:
        lib = open_library(args.db)
    except FileNotFoundError:
        print(f"no library at {args.db}", file=sys.stderr)
        return 2
    if args.cmd == "find":
        for e in lib.find(args.name):
            print(_fmt_entry(e))
    elif args.cmd == "show":
        text = lib.show(args.call, args.context)
        if text is None:
            print(f"unknown call number: {args.call}", file=sys.stderr)
            return 1
        print(text)
    elif args.cmd == "callers":
        for s in lib.callers(args.call):
            print(_fmt_site(s))
    elif args.cmd == "tests":
        for s in lib.tests_for(args.call):
            print(_fmt_site(s))
    else:
        for e in lib.outline(args.path):
            print(_fmt_entry(e))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
