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
  exactly that symbol. Python and plain JavaScript.
* ``insert_after_symbol`` / ``insert_before_symbol {file, symbol, code}`` -
  insert ``code`` next to a symbol, at the symbol's indentation. Python and plain
  JavaScript.
* ``append {file, code}`` - add ``code`` at the end of the file.
* ``search_replace {file, old, new}`` - ``old`` must occur exactly once
  (whitespace-exact).
* ``create_file {file, code}`` - the file must not exist yet.
* ``delete_symbol {file, symbol}`` - remove a symbol. Python and plain JavaScript.

In ``.js``, ``.mjs`` and ``.cjs`` files a symbol is a top-level function, class
or ``const``/``let``/``var``, or ``Class.method``. When a class has several
members of one name, qualify it: ``Cls.get x``, ``Cls.set x``, ``Cls.static x``
or ``Cls.static get x``. A ``replace_symbol`` may also leave it unqualified when
its ``code`` shows which member is meant.

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
from collections import Counter
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
# JavaScript symbol helpers (plain JavaScript only: no JSX/TSX, and regex
# literals are not recognised, so a file they confuse is refused, not guessed)
# --------------------------------------------------------------------------

_JS_SUFFIXES = (".js", ".mjs", ".cjs")
_JS_NAME = r"[A-Za-z_$][\w$]*"
_JS_DECL_RE = re.compile(
    r"(?:export\s+(?:default\s+)?)?(?:"
    rf"(?:async\s+)?function\b\s*\*?\s*(?P<function>{_JS_NAME})"
    rf"|class\s+(?!extends\b)(?P<class>{_JS_NAME})"
    rf"|(?:const|let|var)\s+(?P<variable>{_JS_NAME})"
    r")"
)
_JS_MEMBER_RE = re.compile(
    r"(?P<static>static\s+)?(?:async\s+)?(?:\*\s*)?(?:(?P<accessor>get|set)\s+)?"
    rf"(?P<name>#?{_JS_NAME})\s*\("
)
_JS_WORD_RE = re.compile(r"[\w$]+")
_JS_CONTINUATION_WORD_RE = re.compile(r"(?:in|instanceof)\b")
_JS_QUOTES = "'\"`"
_JS_PAIRS = {"(": ")", "[": "]", "{": "}"}
_JS_CLOSERS = ")]}"
_JS_OPERATOR_END = "=,+-*/%&|^<>?:!~."
_JS_CONTINUATION_START = ",.=+-*/%&|^<>?:([`"
_JS_MEMBER_KINDS = ("method", "get", "set")


@dataclass(frozen=True)
class _JsSymbol:
    """One JavaScript symbol; ``start`` and ``end`` are offsets of its first and last character."""

    name: str
    start: int
    end: int
    kind: str
    static: bool = False

    @property
    def qualifiers(self) -> set[str]:
        """Words that may precede the member name in a symbol reference."""
        found = {"static"} if self.static else set()
        if self.kind in ("get", "set"):
            found.add(self.kind)
        return found

    @property
    def label(self) -> str:
        """The reference naming this symbol, e.g. ``Cls.static get x``."""
        owner, dot, base = self.name.rpartition(".")
        words = ["static"] if self.static else []
        if self.kind in ("get", "set"):
            words.append(self.kind)
        return f"{owner}{dot}{' '.join([*words, base])}"

    @property
    def key(self) -> tuple[str, str, bool]:
        """Identity used to spot duplicates (a getter and a setter do not clash)."""
        return (self.name, self.kind if self.kind in _JS_MEMBER_KINDS else "", self.static)


def _js_error(what: str, text: str, pos: int) -> ValueError:
    line = text.count("\n", 0, pos) + 1
    return ValueError(f"cannot scan JavaScript: {what} at line {line} (use search_replace)")


def _js_string_end(text: str, start: int) -> int:
    """Return the offset just after the quoted string opening at ``start``."""
    quote = text[start]
    i = start + 1
    while i < len(text):
        if text[i] == "\\":
            i += 2
        elif text[i] == quote:
            return i + 1
        elif text[i] == "\n":
            break
        else:
            i += 1
    raise _js_error("unterminated string", text, start)


def _js_substitution_end(text: str, start: int) -> int:
    """Return the offset just after a template substitution whose body begins at ``start``."""
    depth = 1
    i = start
    while i < len(text):
        end = _js_skip_literal(text, i)
        if end is not None:
            i = end
            continue
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if not depth:
                return i + 1
        i += 1
    raise _js_error("unterminated template substitution", text, start)


def _js_template_end(text: str, start: int) -> int:
    """Return the offset just after the template literal opening at ``start``."""
    i = start + 1
    while i < len(text):
        if text[i] == "\\":
            i += 2
        elif text[i] == "`":
            return i + 1
        elif text.startswith("${", i):
            i = _js_substitution_end(text, i + 2)
        else:
            i += 1
    raise _js_error("unterminated template literal", text, start)


def _js_skip_literal(text: str, i: int) -> int | None:
    """Return the end of the comment, string or template at ``i`` (None if there is none)."""
    if text.startswith("//", i):
        end = text.find("\n", i)
        return len(text) if end < 0 else end
    if text.startswith("/*", i):
        end = text.find("*/", i + 2)
        if end < 0:
            raise _js_error("unterminated comment", text, i)
        return end + 2
    if text[i] == "`":
        return _js_template_end(text, i)
    if text[i] in _JS_QUOTES:
        return _js_string_end(text, i)
    return None


def _js_mask(text: str) -> str:
    """Blank comments and literal contents, keeping every offset and newline."""
    out = list(text)
    i = 0
    while i < len(text):
        end = _js_skip_literal(text, i)
        if end is None:
            i += 1
            continue
        quoted = text[i] in _JS_QUOTES
        for k in range(i + 1 if quoted else i, end - 1 if quoted else end):
            if out[k] != "\n":
                out[k] = " "
        i = end
    return "".join(out)


def _js_skip_space(mask: str, i: int, limit: int) -> int:
    while i < limit and mask[i].isspace():
        i += 1
    return i


def _js_close(mask: str, start: int) -> int:
    """Return the offset of the bracket closing the one at ``start``."""
    stack: list[str] = []
    for i in range(start, len(mask)):
        char = mask[i]
        if char in _JS_PAIRS:
            stack.append(_JS_PAIRS[char])
        elif char in _JS_CLOSERS:
            if char != stack[-1]:
                raise _js_error(f"mismatched {char!r}", mask, i)
            stack.pop()
            if not stack:
                return i
    raise _js_error(f"unbalanced {mask[start]!r}", mask, start)


def _js_function_end(mask: str, paren: int) -> int | None:
    """Return the end of the body after the parameters at ``paren`` (None if no body)."""
    brace = _js_skip_space(mask, _js_close(mask, paren) + 1, len(mask))
    if brace >= len(mask) or mask[brace] != "{":
        return None
    return _js_close(mask, brace)


def _js_continues(mask: str, last: int, i: int) -> bool:
    """True when the token at ``i`` carries on the statement ending at ``last``."""
    if mask.startswith(("++", "--"), i):
        return False
    if mask[last] in _JS_OPERATOR_END and mask[last - 1 : last + 1] not in ("++", "--"):
        return True
    return mask[i] in _JS_CONTINUATION_START or bool(_JS_CONTINUATION_WORD_RE.match(mask, i))


def _js_statement_end(mask: str, pos: int, limit: int) -> int:
    """Return the last offset of the statement at ``pos``, its semicolon when it has one."""
    last = pos - 1
    i = pos
    while True:
        i = _js_skip_space(mask, i, limit)
        if i >= limit:
            return last
        char = mask[i]
        if char == ";":
            return i
        if "\n" in mask[last + 1 : i] and not _js_continues(mask, last, i):
            return last
        if char in _JS_CLOSERS:
            raise _js_error(f"stray {char!r}", mask, i)
        if char in _JS_PAIRS:
            i = _js_close(mask, i)
        last = i
        i += 1


def _js_members(mask: str, brace: int, close: int, prefix: str) -> list[_JsSymbol]:
    """Return the methods and accessors between the class braces ``brace`` and ``close``."""
    out: list[_JsSymbol] = []
    i = brace + 1
    while i < close:
        if mask[i].isspace() or mask[i] == ";":
            i += 1
            continue
        match = _JS_MEMBER_RE.match(mask, i, close)
        end = _js_function_end(mask, match.end() - 1) if match else None
        if match is None or end is None:
            i = _js_statement_end(mask, i, close) + 1
            continue
        kind = match["accessor"] or "method"
        out.append(_JsSymbol(prefix + match["name"], i, end, kind, bool(match["static"])))
        i = end + 1
    return out


def _js_class_brace(mask: str, pos: int) -> int | None:
    """Return the offset of the class body's opening brace, skipping any ``extends`` call."""
    i = pos
    while i < len(mask):
        char = mask[i]
        if char == "{":
            return i
        if char == ";":
            return None
        i = _js_close(mask, i) + 1 if char in "([" else i + 1
    return None


def _js_declared(mask: str, match: re.Match[str]) -> list[_JsSymbol]:
    """Return the symbol a declaration match introduces, then its members (empty if none)."""
    kind = match.lastgroup
    assert kind is not None
    name = match[kind]
    start, pos = match.start(), match.end()
    if kind == "variable":
        return [_JsSymbol(name, start, _js_statement_end(mask, pos, len(mask)), kind)]
    if kind == "function":
        paren = _js_skip_space(mask, pos, len(mask))
        if paren >= len(mask) or mask[paren] != "(":
            return []
        end = _js_function_end(mask, paren)
        return [] if end is None else [_JsSymbol(name, start, end, kind)]
    brace = _js_class_brace(mask, pos)
    if brace is None:
        return []
    close = _js_close(mask, brace)
    return [_JsSymbol(name, start, close, kind), *_js_members(mask, brace, close, name + ".")]


def _js_symbols(mask: str) -> list[_JsSymbol]:
    """Return the top-level symbols of a masked file and the members of its classes."""
    out: list[_JsSymbol] = []
    prev = ";"
    i = 0
    while True:
        i = _js_skip_space(mask, i, len(mask))
        if i >= len(mask):
            return out
        match = None
        if prev not in _JS_OPERATOR_END and not mask[mask.rfind("\n", 0, i) + 1 : i].strip():
            match = _JS_DECL_RE.match(mask, i)
        declared = _js_declared(mask, match) if match else []
        if declared:
            out.extend(declared)
            i = declared[0].end
        elif mask[i] in _JS_CLOSERS:
            raise _js_error(f"stray {mask[i]!r}", mask, i)
        elif mask[i] in _JS_PAIRS:
            i = _js_close(mask, i)
        else:
            word = _JS_WORD_RE.match(mask, i)
            i = word.end() - 1 if word else i
        prev = mask[i]
        i += 1


def _js_symbol_hint(symbols: Sequence[_JsSymbol], full: str) -> str:
    parent, _, base = full.rpartition(".")
    if parent and not any(s.name == parent for s in symbols):
        return f"{parent!r} not found"
    names = list(
        dict.fromkeys(
            s.name.rpartition(".")[2] for s in symbols if s.name.rpartition(".")[0] == parent
        )
    )
    close = difflib.get_close_matches(base, names, n=3)
    if close:
        return f"did you mean {', '.join(close)}?"
    scope = f"{parent} members" if parent else "top-level symbols"
    shown = ", ".join(names[:15]) + (", ..." if len(names) > 15 else "")
    return f"{scope}: {shown or '(none)'}"


def _js_select(
    symbols: Sequence[_JsSymbol], symbol: str, wanted: tuple[str, bool] | None = None
) -> _JsSymbol:
    """Return the one symbol ``symbol`` names; refuse to guess between several.

    ``wanted`` is the ``(kind, static)`` of the replacement code. It settles a
    reference like ``Cls.x`` when that name covers, say, a getter and a setter.
    """
    owner, dot, member = symbol.rpartition(".")
    words = member.split()
    full = f"{owner}{dot}{words[-1] if words else ''}"
    qualifiers = set(words[:-1])
    matches = [s for s in symbols if s.name == full and qualifiers <= s.qualifiers]
    if len(matches) > 1 and wanted is not None:
        matches = [s for s in matches if (s.kind, s.static) == wanted] or matches
    if not matches:
        raise ValueError(f"symbol {symbol!r} not found ({_js_symbol_hint(symbols, full)})")
    if len(matches) > 1:
        labels = ", ".join(dict.fromkeys(s.label for s in matches))
        raise ValueError(
            f"symbol {symbol!r} is ambiguous ({labels}): name one of them or use search_replace"
        )
    return matches[0]


def _js_check_defines(code: str, symbol: str) -> tuple[str, bool]:
    """Return ``(kind, static)`` of the only symbol ``code`` defines; raise if it is not ``symbol``."""
    _, dot, member = symbol.rpartition(".")
    words = member.split()
    name = words[-1] if words else member
    mask = _js_mask(textwrap.dedent(code.replace("\r\n", "\n")))
    if dot:
        found = _js_members(mask, -1, len(mask), "")
    else:
        found = [s for s in _js_symbols(mask) if "." not in s.name]
    if len(found) != 1:
        raise ValueError(
            f"code must define exactly {name!r} and nothing else (found {len(found)} symbols)"
        )
    only = found[0]
    if only.name != name:
        raise ValueError(f"code defines {only.name!r} but the symbol is {name!r}")
    if (mask[: only.start] + mask[only.end + 1 :]).strip(" \t\n;"):
        raise ValueError(f"code must define exactly {name!r} and nothing else")
    return only.kind, only.static


def _js_line_span(mask: str, sym: _JsSymbol) -> tuple[int, int]:
    """Return the 0-based ``[first, stop)`` lines of ``sym``, which must be alone on them."""
    head = mask.rfind("\n", 0, sym.start) + 1
    tail = mask.find("\n", sym.end)
    tail = len(mask) if tail < 0 else tail
    if mask[head : sym.start].strip() or mask[sym.end + 1 : tail].strip(" \t;"):
        raise ValueError(f"{sym.label} shares a line with other code; use search_replace")
    return mask.count("\n", 0, sym.start), mask.count("\n", 0, sym.end) + 1


def _js_reject_duplicates(
    before: Sequence[_JsSymbol], after: Sequence[_JsSymbol], mask: str
) -> None:
    """Raise when ``after`` defines a symbol more often than ``before`` did."""
    old = Counter(s.key for s in before)
    new = Counter(s.key for s in after)
    for sym in after:
        count = new[sym.key]
        if count > 1 and count > old[sym.key]:
            line = mask.count("\n", 0, sym.start) + 1
            raise ValueError(f"{sym.name!r} already defined at line {line} - use replace_symbol")


def _js_reject_appended_duplicates(before: str, after: str) -> None:
    """Raise when appending to ``before`` redefines a symbol; unscannable files are left alone."""
    try:
        old_symbols = _js_symbols(_js_mask(before.replace("\r\n", "\n")))
        new_mask = _js_mask(after.replace("\r\n", "\n"))
        new_symbols = _js_symbols(new_mask)
    except ValueError:
        return
    _js_reject_duplicates(old_symbols, new_symbols, new_mask)


def _apply_js_symbol(state: _FileState, edit: Edit, lines: list[str], ends: bool) -> None:
    """Apply a symbol edit to a JavaScript file. Raises ``ValueError`` with a message."""
    assert edit.symbol is not None
    mask = _js_mask("\n".join(lines))
    symbols = _js_symbols(mask)
    code = edit.code or ""
    wanted = _js_check_defines(code, edit.symbol) if edit.op == "replace_symbol" else None
    target = _js_select(symbols, edit.symbol, wanted)
    first, stop = _js_line_span(mask, target)
    block = _prepare_code(code, _indent_of(lines[first]))

    if edit.op == "replace_symbol":
        lines[first:stop] = block
    elif edit.op == "delete_symbol":
        del lines[first:stop]
        _collapse_blanks(lines, min(first, len(lines)), 1)
    elif edit.op == "insert_after_symbol":
        lines[stop:stop] = ["", *block]
        after = stop + 1 + len(block)
        if after < len(lines) and lines[after].strip() and not lines[after].lstrip().startswith("}"):
            lines[after:after] = [""]
    else:
        lines[first:first] = [*block, ""]

    try:
        new_mask = _js_mask("\n".join(lines))
        new_symbols = _js_symbols(new_mask)
    except ValueError as exc:
        raise ValueError(f"result does not parse: {exc}") from None
    _js_reject_duplicates(symbols, new_symbols, new_mask)
    state.text = _join(lines, state.newline, ends)


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
        joined = _join(lines, state.newline)
        if edit.op == "append" and state.rel.endswith(_JS_SUFFIXES):
            _js_reject_appended_duplicates(text, joined)
        state.text = joined
        return

    # Symbol operations (Python and plain JavaScript).
    if state.rel.endswith(_JS_SUFFIXES):
        _apply_js_symbol(state, edit, lines, ends)
        return
    if not is_py:
        raise ValueError(
            f"{edit.op} only works on .py files and plain JavaScript; use search_replace"
        )
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
