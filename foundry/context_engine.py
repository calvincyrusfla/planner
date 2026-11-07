"""Context engine: prune source files down to the slice a builder needs.

The engine is deterministic first; the Gemma Shop Foreman is optional.

* **Python** files are parsed with :mod:`ast`. The skeleton keeps imports,
  module constants, class and function signatures (with decorators), the
  first line of each docstring and Pydantic/dataclass field lines. Function
  bodies become ``...``.
* **TypeScript / JavaScript** files are scanned with regular expressions for
  ``import`` lines and ``export interface|type|function|class|const|enum``
  declarations (plus top-level ``function``/``class``). Interface and type
  bodies are kept (they are the "fields"); function and method bodies become
  ``{ ... }``.
* Each skeleton line keeps its **original line number** (shown in a gutter,
  ``"  12| def f(x: int) -> str:"``) so builders can write accurate diff hunks
  and reviewers can point at lines. ``CodeSnippet.start_line``/``end_line``
  are the original 1-based inclusive range.
* Relevance: symbols are scored by keyword and identifier overlap with the
  subtask title, objective and acceptance criteria. When a
  :class:`~foundry.gemma_client.GemmaClient` is given, :meth:`ContextEngine.abuild_slice`
  lets Gemma re-rank the candidates; any Gemma problem falls back to the
  deterministic order.
* Every snippet has at most :data:`~foundry.schemas.MAX_SNIPPET_LINES` text
  lines, and the objective is trimmed to two sentences.

:func:`condense_error_trace` turns a pytest failure, Python traceback, Node
stack or ruff report into a single ``"path:line: <message>"`` line of at most
160 characters. The Shop Foreman wraps it with Gemma; this is the fallback.
"""

from __future__ import annotations

import ast
import json
import math
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from foundry.schemas import (
    MAX_OBJECTIVE_SENTENCES,
    MAX_SNIPPET_LINES,
    CodeSnippet,
    ContextSlice,
    SubtaskAllocation,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from foundry.gemma_client import GemmaClient

__all__ = [
    "MAX_TRACE_CHARS",
    "PYTHON_SUFFIXES",
    "SCRIPT_SUFFIXES",
    "ContextEngine",
    "SliceStats",
    "SourceSymbol",
    "condense_error_trace",
    "estimate_tokens",
    "skeletonize_python",
    "skeletonize_script",
    "trim_objective",
]

MAX_TRACE_CHARS: int = 160
"""Maximum length of a condensed error trace."""

PYTHON_SUFFIXES: frozenset[str] = frozenset({".py", ".pyi"})
SCRIPT_SUFFIXES: frozenset[str] = frozenset({".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts"})

_MAX_LINE_CHARS = 200  # a single kept source line is clipped to this width
_MAX_FIELD_LINES = 3  # multi-line field/constant statements keep at most this many lines
_MAX_TYPE_BODY_LINES = 30  # TS interface/type bodies are clipped to this many lines
_MAX_FILES = 40  # glob expansion of allowed_files is capped

_SENTENCE_END = re.compile(r"[.!?]+(?=\s|$)")
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_CAMEL = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")
_BACKTICK = re.compile(r"`([^`]+)`")

_STOPWORDS = frozenset(
    """a an and are as at be by can for from has have if in into is it its of on or
    should that the then this to was were when where which will with without must not
    no yes all any each every one two new add adds added make makes use uses using
    return returns given file files code test tests function functions class method
    def self cls none true false str int float bool list dict set tuple""".split()
)


def estimate_tokens(text: str) -> int:
    """Rough token estimate used throughout the foundry: ``ceil(len(text) / 4)``."""
    return math.ceil(len(text) / 4)


def trim_objective(text: str, max_sentences: int = MAX_OBJECTIVE_SENTENCES) -> str:
    """Collapse whitespace and keep at most ``max_sentences`` sentences.

    Sentence boundaries follow :func:`foundry.schemas.count_sentences` (``.``,
    ``!`` or ``?`` followed by whitespace or the end), so the result always
    passes the ``ContextSlice.objective`` validator.
    """
    flat = " ".join(text.split())
    if not flat:
        return ""
    ends = list(_SENTENCE_END.finditer(flat))
    if len(ends) >= max_sentences and ends[max_sentences - 1].end() < len(flat):
        return flat[: ends[max_sentences - 1].end()].strip()
    return flat


def _clip(line: str, width: int = _MAX_LINE_CHARS) -> str:
    """Strip trailing whitespace and clip very long lines."""
    line = line.rstrip()
    if len(line) > width:
        return line[: width - 4] + " ..."
    return line


def _words(text: str) -> set[str]:
    """Lower-case word parts of ``text`` (snake_case and camelCase split), minus stopwords."""
    out: set[str] = set()
    for ident in _IDENT.findall(text):
        parts = [p for chunk in ident.split("_") for p in _CAMEL.findall(chunk)]
        for part in parts + [ident]:
            low = part.lower()
            if len(low) >= 3 and low not in _STOPWORDS:
                out.add(low)
    return out


def _identifiers(text: str) -> set[str]:
    """Code-looking identifiers in ``text``: backticked names, snake_case, CamelCase, dotted calls."""
    out: set[str] = set()
    for chunk in _BACKTICK.findall(text):
        out.update(_IDENT.findall(chunk))
    for ident in _IDENT.findall(text):
        if "_" in ident.strip("_") or re.search(r"[a-z][A-Z]", ident) or re.match(r"[A-Z][a-z]+[A-Z]", ident):
            out.add(ident)
    return {i for i in out if len(i) >= 3}


# --------------------------------------------------------------------------- symbols


@dataclass
class SourceSymbol:
    """One prunable unit of a file (an import block, a class, a function, ...).

    ``lines`` holds ``(line_number, text)`` pairs; ``line_number`` is ``None``
    for synthetic placeholder lines such as an elided body (``...``).
    """

    path: str
    name: str
    kind: str
    start_line: int
    end_line: int
    lines: list[tuple[int | None, str]] = field(default_factory=list)

    @property
    def text(self) -> str:
        """The skeleton text without the line-number gutter."""
        return "\n".join(t for _, t in self.lines)

    def keywords(self) -> set[str]:
        """Words used for relevance scoring (name parts weigh in separately)."""
        return _words(self.text)

    def name_words(self) -> set[str]:
        """Word parts of the symbol name (e.g. ``build_slice`` -> ``{build, slice, build_slice}``)."""
        return _words(self.name)


@dataclass
class SliceStats:
    """Size accounting for one built slice (feeds ``Telemetry.record_tokens_saved``)."""

    source_line_count: int
    source_tokens: int
    slice_tokens: int
    files: list[str] = field(default_factory=list)
    candidates: int = 0
    selected: int = 0
    ranked_by_gemma: bool = False

    @property
    def tokens_saved(self) -> int:
        """Estimated tokens not sent to Claude thanks to pruning (never negative)."""
        return max(0, self.source_tokens - self.slice_tokens)


# --------------------------------------------------------------------------- python


class _PySkeleton:
    """Builds :class:`SourceSymbol` objects for one Python module using ``ast``."""

    def __init__(self, path: str, source: str) -> None:
        self.path = path
        self.src = source.splitlines()
        self.tree = ast.parse(source)

    # helpers ------------------------------------------------------------------
    def _line(self, n: int) -> str:
        return self.src[n - 1] if 0 < n <= len(self.src) else ""

    def _span(self, start: int, end: int, limit: int | None = None) -> list[tuple[int | None, str]]:
        """Source lines ``start..end`` (inclusive), clipped to ``limit`` lines plus a marker."""
        out: list[tuple[int | None, str]] = [(n, _clip(self._line(n))) for n in range(start, end + 1)]
        if limit is not None and len(out) > limit:
            indent = re.match(r"\s*", out[0][1]).group(0)  # type: ignore[union-attr]
            out = out[:limit] + [(None, f"{indent}    ...")]
        return out

    @staticmethod
    def _indent_of(text: str) -> str:
        return re.match(r"\s*", text).group(0)  # type: ignore[union-attr]

    def _docstring_line(self, node: ast.AST, indent: str) -> list[tuple[int | None, str]]:
        """First line of ``node``'s docstring, re-quoted on one line."""
        body = getattr(node, "body", None)
        if not body or not isinstance(body[0], ast.Expr):
            return []
        value = body[0].value
        if not (isinstance(value, ast.Constant) and isinstance(value.value, str)):
            return []
        first = next((ln.strip() for ln in value.value.strip().splitlines() if ln.strip()), "")
        if not first:
            return []
        first = first.replace('"""', "'''")
        return [(body[0].lineno, _clip(f'{indent}"""{first}"""'))]

    def _header(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> list[tuple[int | None, str]]:
        """Decorators plus the (possibly multi-line) ``def``/``class`` header."""
        start = min([d.lineno for d in node.decorator_list] + [node.lineno])
        body_start = node.body[0].lineno
        if body_start <= node.lineno:  # one-liner: ``def f(): return 1``
            lines = self._span(start, node.lineno)
            last_no, last = lines[-1]
            colon = _header_colon(last)
            lines[-1] = (last_no, last[: colon + 1] if colon >= 0 else last)
            return lines
        header_end = body_start - 1
        # Skip blank/comment lines between the header and the body.
        while header_end > node.lineno and not self._line(header_end).strip().endswith(":"):
            header_end -= 1
        return self._span(start, header_end)

    # node handlers --------------------------------------------------------------
    def _function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[tuple[int | None, str]]:
        lines = self._header(node)
        indent = self._indent_of(self._line(node.lineno)) + "    "
        lines += self._docstring_line(node, indent)
        lines.append((None, f"{indent}..."))
        return lines

    def _class(self, node: ast.ClassDef) -> list[tuple[int | None, str]]:
        lines = self._header(node)
        indent = self._indent_of(self._line(node.lineno)) + "    "
        lines += self._docstring_line(node, indent)
        kept_member = False
        for child in node.body:
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                lines += self._function(child)
                kept_member = True
            elif isinstance(child, ast.ClassDef):
                lines += self._class(child)
                kept_member = True
            elif isinstance(child, (ast.AnnAssign, ast.Assign)):
                # Pydantic / dataclass fields, model_config, class constants.
                lines += self._span(child.lineno, child.end_lineno or child.lineno, _MAX_FIELD_LINES)
                kept_member = True
        if not kept_member:
            lines.append((None, f"{indent}..."))
        return lines

    def symbols(self) -> list[SourceSymbol]:
        """Top-level symbols in file order: module docstring, imports, constants, classes, functions."""
        out: list[SourceSymbol] = []
        body = self.tree.body
        doc = self._docstring_line(self.tree, "")
        imports: list[tuple[int | None, str]] = list(doc)
        imp_start = doc[0][0] if doc else None
        imp_end = imp_start
        for node in body:
            end = node.end_lineno or node.lineno
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                imports += self._span(node.lineno, end, 4)
                imp_start = imp_start or node.lineno
                imp_end = end
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                start = min([d.lineno for d in node.decorator_list] + [node.lineno])
                out.append(SourceSymbol(self.path, node.name, "function", start, end, self._function(node)))
            elif isinstance(node, ast.ClassDef):
                start = min([d.lineno for d in node.decorator_list] + [node.lineno])
                out.append(SourceSymbol(self.path, node.name, "class", start, end, self._class(node)))
            elif isinstance(node, (ast.Assign, ast.AnnAssign)) or type(node).__name__ == "TypeAlias":
                name = _assign_name(node)
                if name is None:
                    continue
                out.append(
                    SourceSymbol(self.path, name, "constant", node.lineno, end, self._span(node.lineno, end, _MAX_FIELD_LINES))
                )
        if imports and imp_start is not None and imp_end is not None:
            out.insert(0, SourceSymbol(self.path, "<imports>", "imports", imp_start, imp_end, imports))
        return out


def _header_colon(line: str) -> int:
    """Index of the colon ending a one-line ``def``/``class`` header (bracket-aware)."""
    depth = 0
    for i, ch in enumerate(line):
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == ":" and depth == 0 and not line[i + 1 : i + 2] == "=":
            return i
    return -1


def _assign_name(node: ast.AST) -> str | None:
    """Name of a module-level assignment worth keeping (constants, ``__all__``, type aliases)."""
    if type(node).__name__ == "TypeAlias":
        return getattr(getattr(node, "name", None), "id", None)
    target: ast.AST | None
    if isinstance(node, ast.AnnAssign):
        target = node.target
    elif isinstance(node, ast.Assign) and len(node.targets) == 1:
        target = node.targets[0]
    else:
        return None
    if not isinstance(target, ast.Name):
        return None
    name = target.id
    if name == "__all__" or name.isupper() or (name.lstrip("_").isupper() and len(name) > 1):
        return name
    if isinstance(node, ast.AnnAssign):
        return name
    # Type aliases: ``Complexity = Literal[...]`` / ``Json = dict[str, Any]``.
    value = getattr(node, "value", None)
    if name[:1].isupper() and isinstance(value, (ast.Subscript, ast.BinOp, ast.Attribute, ast.Name)):
        return name
    return None


def skeletonize_python(path: str, source: str) -> list[SourceSymbol]:
    """Return the pruned top-level symbols of a Python module.

    Raises:
        SyntaxError: when ``source`` does not parse (callers fall back to
            :func:`_skeletonize_python_lines`).
    """
    return _PySkeleton(path, source).symbols()


_PY_DECL = re.compile(r"^(\s*)(@\w|async\s+def\s|def\s|class\s|import\s|from\s+\S+\s+import\s)")


def _skeletonize_python_lines(path: str, source: str) -> list[SourceSymbol]:
    """Fallback for Python files that do not parse: keep declaration lines only."""
    lines = [(n, _clip(t)) for n, t in enumerate(source.splitlines(), 1) if _PY_DECL.match(t)]
    if not lines:
        return []
    return [SourceSymbol(path, "<outline>", "outline", lines[0][0] or 1, lines[-1][0] or 1, lines)]


# --------------------------------------------------------------------------- ts / js

_TS_IMPORT = re.compile(r"^\s*import\s|^\s*(?:const|let|var)\s+\S.*=\s*require\(")
_TS_DECL = re.compile(
    r"^(?P<indent>\s*)(?:export\s+(?:default\s+)?)?(?:declare\s+)?(?:abstract\s+)?"
    r"(?P<kind>interface|type|enum|class|(?:async\s+)?function\*?|const|let|var)\s+(?P<name>[A-Za-z_$][\w$]*)"
)
_TS_EXPORT = re.compile(r"^\s*export\s")
_TS_MEMBER = re.compile(
    r"^\s*(?:(?:public|private|protected|static|readonly|async|abstract|override|declare|get|set)\s+)*"
    r"(?P<name>#?[A-Za-z_$][\w$]*)\s*[?!]?\s*(?P<tail>[(:=<])"
)
_TS_NOT_MEMBER = frozenset({"if", "for", "while", "switch", "return", "catch", "else", "do", "try", "throw", "new"})


def _brace_delta(line: str) -> int:
    """Net ``{``/``}`` count of a line, ignoring string literals and ``//`` comments (roughly)."""
    stripped = re.sub(r"(['\"`])(?:\\.|(?!\1).)*\1", "", line)
    stripped = stripped.split("//", 1)[0]
    return stripped.count("{") - stripped.count("}")


def _signature_end(lines: list[str], start: int) -> int:
    """Index of the line where a function signature ends (at ``{``, ``=>`` or ``;``)."""
    depth = 0
    for i in range(start, min(len(lines), start + 15)):
        text = re.sub(r"(['\"`])(?:\\.|(?!\1).)*\1", "", lines[i])
        depth += text.count("(") - text.count(")")
        if depth <= 0 and ("{" in text or "=>" in text or text.rstrip().endswith(";")):
            return i
    return start


def _bracket_end(lines: list[str], start: int) -> int:
    """Index of the line where all brackets opened from ``start`` are closed again."""
    depth = 0
    for i in range(start, len(lines)):
        text = re.sub(r"(['\"`])(?:\\.|(?!\1).)*\1", "", lines[i]).split("//", 1)[0]
        depth += sum(text.count(c) for c in "([{") - sum(text.count(c) for c in ")]}")
        if depth <= 0:
            return i
    return len(lines) - 1


def _block_end(lines: list[str], start: int) -> int:
    """Index of the line closing the ``{`` block opened at or after ``start``."""
    depth = 0
    opened = False
    for i in range(start, len(lines)):
        delta = _brace_delta(lines[i])
        if "{" in lines[i]:
            opened = True
        depth += delta
        if opened and depth <= 0:
            return i
        if not opened and i > start + 15:
            return start
    return len(lines) - 1


def _cut_body(text: str) -> str:
    """Replace everything after the first top-level ``{`` (or ``=>``) with ``{ ... }``."""
    arrow = text.find("=>")
    brace = text.find("{")
    if arrow >= 0 and (brace < 0 or arrow < brace):
        return text[: arrow + 2].rstrip() + " ..."
    if brace >= 0:
        return text[:brace].rstrip() + " { ... }"
    return text


def skeletonize_script(path: str, source: str) -> list[SourceSymbol]:
    """Return the pruned top-level declarations of a TS/JS module (regex based).

    Kept: ``import``/``require`` lines; ``export`` (and top-level) interface,
    type, enum, class, function and const declarations. Interface/type/enum
    bodies are kept (clipped), function bodies become ``{ ... }``, classes keep
    member signatures, non-function consts keep their first line.
    """
    lines = source.splitlines()
    out: list[SourceSymbol] = []
    imports: list[tuple[int | None, str]] = []
    i = 0
    while i < len(lines):
        text = lines[i]
        if _TS_IMPORT.match(text) and not text.startswith((" ", "\t")):
            end = i
            while end < len(lines) - 1 and not re.search(r"['\"`]\s*\)?\s*;?\s*$", lines[end]) and end - i < 10:
                end += 1
            imports += [(n + 1, _clip(lines[n])) for n in range(i, end + 1)][:4]
            i = end + 1
            continue
        m = _TS_DECL.match(text)
        top_level = m is not None and (m.group("indent") == "" or _TS_EXPORT.match(text) is not None)
        if not m or not top_level:
            i += 1
            continue
        kind, name = m.group("kind"), m.group("name")
        if kind in {"const", "let", "var"} and not _TS_EXPORT.match(text):
            i += 1  # only exported variables are part of the public surface
            continue
        kept: list[tuple[int | None, str]]
        if kind in {"interface", "enum"} or (kind == "type" and "{" in text):
            end = _block_end(lines, i)
            kept = [(n + 1, _clip(lines[n])) for n in range(i, end + 1)]
            if len(kept) > _MAX_TYPE_BODY_LINES:
                kept = kept[: _MAX_TYPE_BODY_LINES - 1] + [(None, "  // ..."), kept[-1]]
            sym_kind = kind
        elif kind == "type":
            end = i
            while end < len(lines) - 1 and not lines[end].rstrip().endswith(";") and end - i < _MAX_TYPE_BODY_LINES:
                if lines[end + 1].strip() == "" or _TS_DECL.match(lines[end + 1]):
                    break
                end += 1
            kept = [(n + 1, _clip(lines[n])) for n in range(i, end + 1)]
            sym_kind = "type"
        elif kind == "class":
            end = _block_end(lines, i)
            kept = [(i + 1, _clip(lines[i]))]
            depth = _brace_delta(lines[i])
            for n in range(i + 1, end):
                line = lines[n]
                if depth == 1:
                    mm = _TS_MEMBER.match(line)
                    if mm and mm.group("name") not in _TS_NOT_MEMBER:
                        kept.append((n + 1, _clip(_cut_body(line) if mm.group("tail") == "(" or "=>" in line else line)))
                depth += _brace_delta(line)
            kept.append((end + 1, _clip(lines[end])) if end > i else (None, "}"))
            sym_kind = "class"
        elif "function" in kind or (
            kind in {"const", "let", "var"} and re.search(r"=\s*(?:async\s+)?(?:function\b|\(|[A-Za-z_$][\w$]*\s*=>)", text)
        ):
            sig_end = _signature_end(lines, i)
            sig = [(n + 1, _clip(lines[n])) for n in range(i, sig_end + 1)]
            last_no, last = sig[-1]
            sig[-1] = (last_no, _clip(_cut_body(last)))
            kept = sig
            end = _block_end(lines, sig_end) if "{" in lines[sig_end] else sig_end
            sym_kind = "function"
        else:  # exported value
            end = _bracket_end(lines, i)
            first = _clip(text)
            kept = [(i + 1, first)] if end == i else [(i + 1, first), (None, "  // ...")]
            sym_kind = "constant"
        out.append(SourceSymbol(path, name, sym_kind, i + 1, end + 1, kept))
        i = max(end + 1, i + 1)
    if imports:
        out.insert(0, SourceSymbol(path, "<imports>", "imports", imports[0][0] or 1, imports[-1][0] or 1, imports))
    return out


def _skeletonize_other(path: str, source: str) -> list[SourceSymbol]:
    """Non-code files: small files whole, larger ones as a clipped head."""
    lines = source.splitlines()
    if not lines:
        return []
    limit = MAX_SNIPPET_LINES - 1 if len(lines) > MAX_SNIPPET_LINES else len(lines)
    kept: list[tuple[int | None, str]] = [(n + 1, _clip(lines[n])) for n in range(limit)]
    if len(lines) > limit:
        kept.append((None, f"... ({len(lines) - limit} more lines)"))
    return [SourceSymbol(path, Path(path).name, "text", 1, len(lines), kept)]


# --------------------------------------------------------------------------- engine


def _render(lines: Sequence[tuple[int | None, str]], numbered: bool) -> str:
    """Join skeleton lines, with an original-line-number gutter when ``numbered``."""
    if not numbered:
        return "\n".join(t for _, t in lines)
    width = max([len(str(n)) for n, _ in lines if n is not None] + [3])
    return "\n".join(f"{(str(n) if n is not None else ''):>{width}}| {t}" for n, t in lines)


def _chunks(symbol: SourceSymbol, max_lines: int) -> list[list[tuple[int | None, str]]]:
    """Split a symbol's skeleton into pieces of at most ``max_lines`` lines."""
    return [symbol.lines[k : k + max_lines] for k in range(0, len(symbol.lines), max_lines)] or [[]]


class ContextEngine:
    """Builds :class:`~foundry.schemas.ContextSlice` objects for builder subtasks.

    Args:
        repo_root: directory that ``allowed_files`` are relative to. Paths that
            resolve outside it are ignored.
        gemma: optional Shop Foreman client; only :meth:`abuild_slice` uses it
            (to re-rank candidate snippets). Everything works with ``None``.
        numbered: prefix snippet lines with their original line number.
        max_snippets: default cap on snippets per slice.
        max_total_lines: default cap on snippet text lines per slice.
    """

    def __init__(
        self,
        repo_root: str | Path,
        gemma: GemmaClient | None = None,
        *,
        numbered: bool = True,
        max_snippets: int = 12,
        max_total_lines: int = 240,
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.gemma = gemma
        self.numbered = numbered
        self.max_snippets = max_snippets
        self.max_total_lines = max_total_lines
        self.last_stats: SliceStats | None = None

    # files ------------------------------------------------------------------------
    def _resolve(self, rel: str) -> Path | None:
        """Absolute path for ``rel`` if it is an existing file inside the repo."""
        candidate = (self.repo_root / rel).resolve()
        try:
            candidate.relative_to(self.repo_root)
        except ValueError:
            return None
        return candidate if candidate.is_file() else None

    def expand_files(self, patterns: Iterable[str]) -> list[str]:
        """Repo-relative POSIX paths of existing files named by ``patterns`` (globs allowed)."""
        seen: dict[str, None] = {}
        for pattern in patterns:
            pattern = pattern.strip().replace("\\", "/")
            if not pattern:
                continue
            if any(ch in pattern for ch in "*?["):
                try:
                    matches = sorted(self.repo_root.glob(pattern))
                except (ValueError, NotImplementedError):
                    matches = []
                for match in matches:
                    if match.is_file() and self._resolve(str(match.relative_to(self.repo_root))):
                        seen.setdefault(match.relative_to(self.repo_root).as_posix(), None)
            elif self._resolve(pattern) is not None:
                seen.setdefault(Path(pattern).as_posix(), None)
            if len(seen) >= _MAX_FILES:
                break
        return list(seen)[:_MAX_FILES]

    def symbols_for(self, rel_path: str, source: str | None = None) -> list[SourceSymbol]:
        """Pruned symbols of one file (``source`` may be passed to skip reading)."""
        if source is None:
            resolved = self._resolve(rel_path)
            if resolved is None:
                return []
            source = resolved.read_text(encoding="utf-8", errors="replace")
        suffix = Path(rel_path).suffix.lower()
        if suffix in PYTHON_SUFFIXES:
            try:
                return skeletonize_python(rel_path, source)
            except (SyntaxError, ValueError):
                return _skeletonize_python_lines(rel_path, source)
        if suffix in SCRIPT_SUFFIXES:
            return skeletonize_script(rel_path, source)
        return _skeletonize_other(rel_path, source)

    # scoring -------------------------------------------------------------------------
    @staticmethod
    def _query(title: str, objective: str, criteria: Sequence[str]) -> tuple[set[str], set[str]]:
        text = "\n".join([title, objective, *criteria])
        return _words(text), _identifiers(text)

    @staticmethod
    def score(symbol: SourceSymbol, words: set[str], idents: set[str]) -> float:
        """Relevance of ``symbol`` to a query (higher is better, 0 = unrelated)."""
        if symbol.kind == "imports":
            return 0.0
        score = 0.0
        if symbol.name in idents:
            score += 10.0
        member_names = {m for m in _IDENT.findall(symbol.text)} & idents
        score += 3.0 * len(member_names - {symbol.name})
        score += 2.0 * len(symbol.name_words() & words)
        score += 0.5 * len((symbol.keywords() - symbol.name_words()) & words)
        return score

    def _rank(
        self, symbols: list[SourceSymbol], words: set[str], idents: set[str]
    ) -> list[tuple[float, int, SourceSymbol]]:
        ranked = [(self.score(s, words, idents), idx, s) for idx, s in enumerate(symbols) if s.kind != "imports"]
        ranked.sort(key=lambda item: (-item[0], item[1]))
        return ranked

    # slicing --------------------------------------------------------------------------
    def _collect(
        self,
        subtask: SubtaskAllocation | None,
        objective: str,
        acceptance_criteria: Sequence[str] | None,
        allowed_files: Sequence[str] | None,
        extra_files: Sequence[str],
    ) -> tuple[str, str, list[str], list[str], dict[str, str]]:
        title = subtask.title if subtask is not None else ""
        criteria = list(acceptance_criteria if acceptance_criteria is not None else (subtask.acceptance_criteria if subtask else []))
        files = list(allowed_files if allowed_files is not None else (subtask.allowed_files if subtask else []))
        objective = trim_objective(objective or title or (criteria[0] if criteria else "")) or "Implement the subtask."
        paths = self.expand_files([*files, *extra_files])
        sources: dict[str, str] = {}
        for rel in paths:
            resolved = self._resolve(rel)
            if resolved is not None:
                sources[rel] = resolved.read_text(encoding="utf-8", errors="replace")
        return title, objective, criteria, paths, sources

    def _assemble(
        self,
        objective: str,
        criteria: list[str],
        sources: dict[str, str],
        ordered: list[SourceSymbol],
        imports: dict[str, SourceSymbol],
        max_snippets: int,
        max_total_lines: int,
        ranked_by_gemma: bool,
        candidates: int,
    ) -> ContextSlice:
        snippets: list[CodeSnippet] = []
        total = 0
        used_imports: set[str] = set()
        chosen: list[SourceSymbol] = []
        for sym in ordered:
            if len(chosen) >= max_snippets:
                break
            need = len(sym.lines) + (0 if sym.path in used_imports or sym.path not in imports else len(imports[sym.path].lines))
            if total + need > max_total_lines and chosen:
                continue
            chosen.append(sym)
            if sym.path in imports and sym.path not in used_imports:
                used_imports.add(sym.path)
            total += need
        # Emit per file (imports first), symbols in file order, so line numbers read naturally.
        order = {p: k for k, p in enumerate(sources)}
        emit: list[SourceSymbol] = [imports[p] for p in used_imports] + chosen
        emit.sort(key=lambda s: (order.get(s.path, 0), s.start_line))
        for sym in emit:
            pieces = [p for p in _chunks(sym, MAX_SNIPPET_LINES) if p]
            for k, piece in enumerate(pieces):
                numbers = [n for n, _ in piece if n is not None]
                start = sym.start_line if k == 0 else (min(numbers) if numbers else sym.start_line)
                end = sym.end_line if k == len(pieces) - 1 else max(numbers or [start])
                end = max(end, start)
                snippets.append(CodeSnippet(path=sym.path, start_line=start, end_line=end, text=_render(piece, self.numbered)))
        source_lines = sum(len(s.splitlines()) for s in sources.values())
        sliced = ContextSlice(
            objective=objective,
            acceptance_criteria=criteria,
            snippets=snippets,
            source_line_count=source_lines,
        )
        slice_text = "\n".join(s.text for s in snippets)
        self.last_stats = SliceStats(
            source_line_count=source_lines,
            source_tokens=sum(estimate_tokens(s) for s in sources.values()),
            slice_tokens=estimate_tokens(slice_text),
            files=list(sources),
            candidates=candidates,
            selected=len(chosen),
            ranked_by_gemma=ranked_by_gemma,
        )
        return sliced

    def _prepare(
        self, title: str, objective: str, criteria: list[str], sources: dict[str, str]
    ) -> tuple[list[tuple[float, int, SourceSymbol]], dict[str, SourceSymbol]]:
        all_symbols: list[SourceSymbol] = []
        imports: dict[str, SourceSymbol] = {}
        for rel, src in sources.items():
            for sym in self.symbols_for(rel, src):
                if sym.kind == "imports":
                    imports[rel] = sym
                else:
                    all_symbols.append(sym)
        words, idents = self._query(title, objective, criteria)
        return self._rank(all_symbols, words, idents), imports

    @staticmethod
    def _default_order(ranked: list[tuple[float, int, SourceSymbol]]) -> list[SourceSymbol]:
        """Relevant symbols by score; if nothing matched, the file outline in order."""
        relevant = [s for score, _, s in ranked if score > 0]
        if relevant:
            return relevant
        return [s for _, _, s in sorted(ranked, key=lambda item: item[1])]

    def build_slice(
        self,
        subtask: SubtaskAllocation | None = None,
        objective: str = "",
        *,
        acceptance_criteria: Sequence[str] | None = None,
        allowed_files: Sequence[str] | None = None,
        extra_files: Sequence[str] = (),
        max_snippets: int | None = None,
        max_total_lines: int | None = None,
    ) -> ContextSlice:
        """Build a deterministic :class:`ContextSlice` for one subtask (no Gemma).

        Args:
            subtask: the allocation; its title/criteria/allowed_files are used
                unless overridden by the keyword arguments.
            objective: task objective; trimmed to two sentences (defaults to
                the subtask title).
            acceptance_criteria: overrides ``subtask.acceptance_criteria``.
            allowed_files: overrides ``subtask.allowed_files`` (globs allowed;
                files that do not exist yet are skipped).
            extra_files: read-only reference files to consider as well.
            max_snippets / max_total_lines: per-call caps.

        Returns:
            The slice. Size accounting is left in :attr:`last_stats`
            (``tokens_saved`` for telemetry).
        """
        title, objective, criteria, _paths, sources = self._collect(
            subtask, objective, acceptance_criteria, allowed_files, extra_files
        )
        ranked, imports = self._prepare(title, objective, criteria, sources)
        return self._assemble(
            objective,
            criteria,
            sources,
            self._default_order(ranked),
            imports,
            max_snippets or self.max_snippets,
            max_total_lines or self.max_total_lines,
            False,
            len(ranked),
        )

    def build_slice_with_stats(self, *args: Any, **kwargs: Any) -> tuple[ContextSlice, SliceStats]:
        """:meth:`build_slice` returning ``(slice, stats)``."""
        sliced = self.build_slice(*args, **kwargs)
        assert self.last_stats is not None
        return sliced, self.last_stats

    async def abuild_slice(
        self,
        subtask: SubtaskAllocation | None = None,
        objective: str = "",
        *,
        acceptance_criteria: Sequence[str] | None = None,
        allowed_files: Sequence[str] | None = None,
        extra_files: Sequence[str] = (),
        max_snippets: int | None = None,
        max_total_lines: int | None = None,
    ) -> ContextSlice:
        """Like :meth:`build_slice`, but lets Gemma rank the candidates when available.

        Gemma receives only symbol ids and their first signature line and must
        reply with JSON ``{"ranked": [id, ...]}``. Unknown ids are ignored,
        unranked relevant symbols are appended, and any error (no Gemma,
        unreachable, bad JSON) falls back to the deterministic order.
        """
        title, objective, criteria, _paths, sources = self._collect(
            subtask, objective, acceptance_criteria, allowed_files, extra_files
        )
        ranked, imports = self._prepare(title, objective, criteria, sources)
        ordered = self._default_order(ranked)
        used_gemma = False
        if self.gemma is not None and len(ranked) > 1:
            gemma_order = await self._gemma_rank(title, objective, criteria, [s for _, _, s in ranked])
            if gemma_order:
                rest = [s for s in ordered if all(s is not g for g in gemma_order)]
                ordered = gemma_order + rest
                used_gemma = True
        return self._assemble(
            objective,
            criteria,
            sources,
            ordered,
            imports,
            max_snippets or self.max_snippets,
            max_total_lines or self.max_total_lines,
            used_gemma,
            len(ranked),
        )

    async def _gemma_rank(
        self, title: str, objective: str, criteria: list[str], symbols: list[SourceSymbol]
    ) -> list[SourceSymbol]:
        """Ask Gemma for a relevance order; returns ``[]`` on any failure."""
        catalogue = {f"S{k}": s for k, s in enumerate(symbols[:60])}
        listing = "\n".join(
            f"{sid} {s.path}:{s.start_line} {s.kind} {s.name}: "
            f"{next((t.strip() for n, t in s.lines if n is not None and not t.strip().startswith('@')), s.name)}"
            for sid, s in catalogue.items()
        )
        messages = [
            {
                "role": "system",
                "content": "You rank code symbols by relevance to a coding subtask. "
                'Reply with JSON {"ranked": ["S0", ...]} listing only relevant ids, most relevant first.',
            },
            {
                "role": "user",
                "content": f"Subtask: {title}\nObjective: {objective}\nCriteria:\n"
                + "\n".join(f"- {c}" for c in criteria)
                + f"\n\nSymbols:\n{listing}",
            },
        ]
        schema = {
            "type": "object",
            "properties": {"ranked": {"type": "array", "items": {"type": "string"}}},
            "required": ["ranked"],
        }
        try:
            reply = await self.gemma.chat(messages, json_schema=schema, temperature=0.0)  # type: ignore[union-attr]
            data = json.loads(_strip_fence(getattr(reply, "text", "") or ""))
            ids = data.get("ranked") if isinstance(data, dict) else None
            if not isinstance(ids, list):
                return []
        except Exception:  # noqa: BLE001 - Gemma is optional; never break slicing
            return []
        out: list[SourceSymbol] = []
        for sid in ids:
            sym = catalogue.get(str(sid).strip())
            if sym is not None and all(sym is not o for o in out):
                out.append(sym)
        return out

    # error traces -------------------------------------------------------------------------
    def condense_error_trace(self, trace: str) -> str:
        """:func:`condense_error_trace` with paths made relative to :attr:`repo_root`."""
        return condense_error_trace(trace, repo_root=self.repo_root)


def _strip_fence(text: str) -> str:
    """Remove a surrounding ```json fence if present."""
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    return m.group(1) if m else text.strip()


# --------------------------------------------------------------------------- traces

_PY_FRAME = re.compile(r'^\s*File "(?P<path>[^"]+)", line (?P<line>\d+)(?:, in (?P<func>.+))?\s*$')
_PY_EXC = re.compile(r"^(?P<exc>[A-Za-z_][\w.]*(?:Error|Exception|Exit|Interrupt|Warning|Failure|Iteration)|[A-Z]\w+Error)(?::\s*(?P<msg>.*))?$")
_PYTEST_LOC = re.compile(r"^(?P<path>(?:[A-Za-z]:)?[^\s:]+\.\w+):(?P<line>\d+):(?:\s+(?P<rest>.*))?$")
_PYTEST_FAILED = re.compile(r"^(?:FAILED|ERROR)\s+(?P<path>[^\s:]+\.\w+)(?:::(?P<test>\S+))?(?:\s+-\s+(?P<msg>.*))?$")
_NODE_FRAME = re.compile(r"^\s+at (?:(?P<func>.*?) \()?(?P<path>(?:file://)?.+?):(?P<line>\d+):(?P<col>\d+)\)?\s*$")
_RUFF_LINE = re.compile(r"^(?P<path>(?:[A-Za-z]:)?[^\s:]+\.\w+):(?P<line>\d+):(?P<col>\d+):\s*(?P<code>[A-Z]+\d+)\s+(?:\[\*\]\s+)?(?P<msg>.*)$")
_RUFF_HEAD = re.compile(r"^(?P<code>[A-Z]{1,4}\d{2,4})\s+(?:\[\*\]\s+)?(?P<msg>.+)$")
_RUFF_ARROW = re.compile(r"^\s*-->\s*(?P<path>.+?):(?P<line>\d+):(?P<col>\d+)\s*$")
_ANY_LOC = re.compile(r"(?P<path>(?:[A-Za-z]:)?[\w./\\-]+\.\w{1,5}):(?P<line>\d+)")
_LIB_PATH = re.compile(r"site-packages|dist-packages|[\\/]lib[\\/]python\d|<frozen|<string>|^node:|[\\/]node_modules[\\/]|^internal[\\/]")


def _relpath(path: str, repo_root: Path | None) -> str:
    """Normalise a path from a trace: strip ``file://``, use ``/``, relativise to the repo."""
    path = path.strip()
    if path.startswith("file://"):
        path = path[len("file://") :]
        if re.match(r"^/[A-Za-z]:/", path):
            path = path[1:]
    path = path.replace("\\", "/")
    if repo_root is not None:
        root = repo_root.as_posix().rstrip("/") + "/"
        if path.startswith(root):
            path = path[len(root) :]
        else:
            try:
                p = Path(path)
                if p.is_absolute():
                    path = p.resolve().relative_to(repo_root).as_posix()
            except (ValueError, OSError):
                pass
    return path


def _finish(loc: str | None, message: str) -> str:
    """Join location and message into one line of at most :data:`MAX_TRACE_CHARS` chars."""
    message = " ".join(message.split())
    line = f"{loc}: {message}" if loc and message else (loc or message or "unknown error")
    line = " ".join(line.split())
    if len(line) > MAX_TRACE_CHARS:
        line = line[: MAX_TRACE_CHARS - 3].rstrip() + "..."
    return line


def _pick_frame(frames: list[tuple[str, str]], deepest_last: bool) -> tuple[str, str] | None:
    """Deepest project (non-library) frame; any frame if all are library code."""
    if not frames:
        return None
    ordered = frames[::-1] if deepest_last else frames
    for path, line in ordered:
        if not _LIB_PATH.search(path):
            return path, line
    return ordered[0]


def _condense_ruff(lines: list[str], root: Path | None) -> str | None:
    hits: list[tuple[str, str, str]] = []
    for line in lines:
        m = _RUFF_LINE.match(line.strip())
        if m:
            hits.append((_relpath(m["path"], root), m["line"], f"{m['code']} {m['msg']}"))
    if not hits:  # newer "full" output format: header line then ``--> path:line:col``
        for k, line in enumerate(lines):
            m = _RUFF_ARROW.match(line)
            if not m:
                continue
            head = next((_RUFF_HEAD.match(lines[j].strip()) for j in range(k - 1, max(-1, k - 3), -1) if _RUFF_HEAD.match(lines[j].strip())), None)
            if head:
                hits.append((_relpath(m["path"], root), m["line"], f"{head['code']} {head['msg']}"))
    if not hits:
        return None
    path, line, msg = hits[0]
    more = f" (+{len(hits) - 1} more)" if len(hits) > 1 else ""
    suffix = more
    base = _finish(f"{path}:{line}", msg)
    if suffix and len(base) + len(suffix) > MAX_TRACE_CHARS:
        base = _finish(f"{path}:{line}", msg[: max(10, MAX_TRACE_CHARS - len(suffix) - len(path) - len(line) - 8)] + "...")
    return (base + suffix)[:MAX_TRACE_CHARS]


def _condense_python(lines: list[str], root: Path | None) -> str | None:
    frames: list[tuple[str, str]] = []
    last_frame_idx = -1
    for k, line in enumerate(lines):
        m = _PY_FRAME.match(line)
        if m:
            frames.append((m["path"], m["line"]))
            last_frame_idx = k
    if not frames:
        return None
    exc_line = ""
    for line in lines[last_frame_idx + 1 :]:
        if line and not line[0].isspace() and line.strip() and not line.startswith(("Traceback", "During handling", "The above exception")):
            exc_line = line.strip()
            if _PY_EXC.match(exc_line):
                break
    frame = _pick_frame(frames, deepest_last=True)
    assert frame is not None
    return _finish(f"{_relpath(frame[0], root)}:{frame[1]}", exc_line or "error")


def _is_exc_name(text: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z_][\w.]*(?:Error|Exception|Exit|Interrupt|Warning|Failed|Failure)", text))


def _condense_pytest(lines: list[str], root: Path | None) -> str | None:
    locs: list[tuple[int, str, str, str]] = []
    e_blocks: list[tuple[int, list[str]]] = []
    current: list[str] | None = None
    for k, line in enumerate(lines):
        if line.startswith("E ") or line == "E":
            text = line[1:].strip()
            if current is None:
                current = []
                e_blocks.append((k, current))
            if text:
                current.append(text)
            continue
        current = None
        m = _PYTEST_LOC.match(line.strip()) if not line.startswith(" ") else None
        if m and not _RUFF_LINE.match(line.strip()):
            locs.append((k, m["path"], m["line"], (m["rest"] or "").strip()))
    failed = [_PYTEST_FAILED.match(line.strip()) for line in lines]
    failed_hits = [m for m in failed if m]
    if not locs and not e_blocks and not failed_hits:
        return None
    if locs:
        idx, path, lineno, rest = locs[-1]
        # Prefer the deepest project frame if the last location is library code.
        if _LIB_PATH.search(path):
            for _, p, ln, r in reversed(locs):
                if not _LIB_PATH.search(p):
                    path, lineno, rest = p, ln, r
                    break
        block = next((b for start, b in reversed(e_blocks) if start < idx and b), None) or next(
            (b for _, b in reversed(e_blocks) if b), None
        )
        msg = block[0] if block else ""
        exc = rest[3:].strip() if rest.startswith("in ") else rest
        if rest.startswith("in "):
            exc = ""
        if msg:
            head = msg.split(":", 1)[0]
            if exc and _is_exc_name(exc) and not _is_exc_name(head) and not msg.startswith(exc):
                msg = f"{exc}: {msg}"
            elif not exc or not _is_exc_name(head):
                if msg.startswith("assert ") and not exc:
                    msg = f"AssertionError: {msg}"
        else:
            msg = exc or (failed_hits[0]["msg"] if failed_hits and failed_hits[0]["msg"] else "test failed")
        return _finish(f"{_relpath(path, root)}:{lineno}", msg)
    if failed_hits:
        m = failed_hits[0]
        where = _relpath(m["path"], root) + (f"::{m['test']}" if m["test"] else "")
        more = f" (+{len(failed_hits) - 1} more)" if len(failed_hits) > 1 else ""
        return _finish(where, (m["msg"] or "failed") + more)
    block = next((b for _, b in reversed(e_blocks) if b), [])
    return _finish(None, block[0] if block else "test failed")


def _condense_node(lines: list[str], root: Path | None) -> str | None:
    frames: list[tuple[str, str]] = []
    first_frame_idx = -1
    for k, line in enumerate(lines):
        m = _NODE_FRAME.match(line)
        if m:
            if first_frame_idx < 0:
                first_frame_idx = k
            frames.append((m["path"], m["line"]))
    if not frames:
        return None
    message = ""
    for line in reversed(lines[:first_frame_idx]):
        if line.strip() and not set(line.strip()) <= {"^", "~"}:
            message = line.strip()
            break
    # Prefer an explicit "Error: ..." line if the line right above the stack is not one.
    for line in lines[:first_frame_idx]:
        if re.match(r"^\s*(?:Uncaught\s+)?[A-Z]\w*(?:Error|Exception)\b", line):
            message = line.strip()
    frame = _pick_frame(frames, deepest_last=False)
    assert frame is not None
    return _finish(f"{_relpath(frame[0], root)}:{frame[1]}", message or "error")


def condense_error_trace(trace: str, repo_root: str | Path | None = None) -> str:
    """Condense a failure log into one ``"path:line: <message>"`` line (≤ 160 chars).

    Recognised inputs, tried in this order:

    * ruff output (``path:1:8: F401 ...`` or the newer ``F401 ...`` / ``--> path:1:8``);
    * pytest failure reports (``E`` lines plus ``path:line: Exc`` locations, or
      ``FAILED path::test - msg`` summary lines);
    * Python tracebacks (deepest project frame + the exception line);
    * Node.js stacks (first non-internal ``at`` frame + the error line);
    * anything else: the first ``path:line`` found plus the last non-empty line.

    Deterministic and never raises; the result never contains a newline.
    """
    root = Path(repo_root).resolve() if repo_root is not None else None
    text = (trace or "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\x1b\[[0-9;]*m", "", text)  # ANSI colours
    lines = text.split("\n")
    if not text.strip():
        return "unknown error (empty trace)"
    has_traceback = any(_PY_FRAME.match(line) for line in lines)
    is_pytest = any(line.startswith("E ") for line in lines) or "short test summary" in text or "= FAILURES =" in text
    try:
        if not has_traceback and not is_pytest:
            result = _condense_ruff(lines, root)
            if result:
                return result
        if is_pytest:
            result = _condense_pytest(lines, root)
            if result:
                return result
        if has_traceback:
            result = _condense_python(lines, root)
            if result:
                return result
        result = _condense_node(lines, root)
        if result:
            return result
        result = _condense_pytest(lines, root) or _condense_ruff(lines, root)
        if result:
            return result
    except Exception:  # noqa: BLE001 - a trace condenser must never crash the factory
        pass
    meaningful = [line.strip() for line in lines if line.strip()]
    loc_match = next((m for m in (_ANY_LOC.search(line) for line in meaningful) if m), None)
    loc = f"{_relpath(loc_match['path'], root)}:{loc_match['line']}" if loc_match else None
    last = meaningful[-1] if meaningful else ""
    if loc and loc_match and last.startswith(loc_match.group(0)):
        last = last[len(loc_match.group(0)) :].lstrip(": ")
    return _finish(loc, last)
