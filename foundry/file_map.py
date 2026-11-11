"""A map of where to look: the functions and classes an order likely needs, with line ranges.

:func:`build_index` lists the symbols of the allowed files in plain code.
:func:`file_map` picks from that list: first the names the order quotes in
backticks, then (optionally) the numbers Gemma chooses. Gemma only picks
numbers from the list Foundry built, so the text sent on is always Foundry's
own. Any failure gives just the backtick matches.
"""

from __future__ import annotations

import ast
import asyncio
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from foundry.gemma_notes import _ask_json

log = logging.getLogger(__name__)

__all__ = ["Symbol", "build_index", "file_map"]

MAX_FILE_BYTES = 2_000_000
MAX_SYMBOLS = 400
_SCRIPT_SUFFIXES = (".mjs", ".js", ".ts")
_SCRIPT_SYMBOL = re.compile(
    r"^(?:export\s+(?:async\s+)?function\s+(?P<f1>[A-Za-z_$][\w$]*)"
    r"|(?:async\s+)?function\s+(?P<f2>[A-Za-z_$][\w$]*)"
    r"|(?:export\s+)?class\s+(?P<c>[A-Za-z_$][\w$]*)"
    r"|export\s+const\s+(?P<k>[A-Za-z_$][\w$]*)\s*=)"
)
_BACKTICK_RE = re.compile(r"`([A-Za-z_][A-Za-z0-9_.]{2,})`")

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"picks": {"type": "array", "items": {"type": "integer"}}},
    "required": ["picks"],
}


@dataclass(frozen=True)
class Symbol:
    """One function, class or method: 1-based, inclusive line range."""

    path: str
    name: str
    start: int
    end: int


def _python_symbols(path: str, source: str) -> list[Symbol]:
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return []

    def span(node: Any) -> tuple[int, int]:
        start = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", ())])
        return start, int(node.end_lineno or node.lineno)

    found: list[Symbol] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            start, end = span(node)
            found.append(Symbol(path, node.name, start, end))
            if isinstance(node, ast.ClassDef):
                for sub in node.body:
                    if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        s, e = span(sub)
                        found.append(Symbol(path, f"{node.name}.{sub.name}", s, e))
    return found


def _script_symbols(path: str, source: str) -> list[Symbol]:
    lines = source.splitlines()
    starts: list[tuple[int, str]] = []
    for number, line in enumerate(lines, start=1):
        match = _SCRIPT_SYMBOL.match(line)
        if match:
            name = match.group("f1") or match.group("f2") or match.group("c") or match.group("k")
            starts.append((number, str(name)))
    found: list[Symbol] = []
    for i, (number, name) in enumerate(starts):
        end = starts[i + 1][0] - 1 if i + 1 < len(starts) else len(lines)
        found.append(Symbol(path, name, number, max(end, number)))
    return found


def build_index(repo_root: Path, files: Sequence[str]) -> list[Symbol]:
    """List the symbols of the existing ``files`` under ``repo_root`` (at most 400)."""
    index: list[Symbol] = []
    for rel in files:
        if len(index) >= MAX_SYMBOLS:
            break
        try:
            full = Path(repo_root) / rel
            if not full.is_file() or full.stat().st_size > MAX_FILE_BYTES:
                continue
            if rel.endswith(".py"):
                source = full.read_text(encoding="utf-8")
                index += _python_symbols(rel, source)
            elif rel.endswith(_SCRIPT_SUFFIXES):
                source = full.read_text(encoding="utf-8")
                index += _script_symbols(rel, source)
        except (OSError, UnicodeError, ValueError):
            continue
    return index[:MAX_SYMBOLS]


def _render(sym: Symbol) -> str:
    return f"{sym.path}:{sym.start}-{sym.end} {sym.name}"


async def file_map(
    gemma: Any,
    order_text: str,
    index: Sequence[Symbol],
    *,
    timeout: float,
    max_entries: int = 12,
) -> list[str]:
    """Entries ``"<path>:<start>-<end> <name>"`` for the symbols the order likely needs."""
    try:
        chosen: set[int] = set()
        quoted = set(_BACKTICK_RE.findall(order_text))
        for i, sym in enumerate(index):
            if sym.name in quoted or sym.name.rsplit(".", 1)[-1] in quoted:
                chosen.add(i)
        if gemma is not None and index:
            try:
                listing = "\n".join(f"{i}: {s.path} {s.name} ({s.start}-{s.end})" for i, s in enumerate(index))
                system = (
                    "You help a builder find its way in a codebase. From the numbered list of functions and "
                    "classes, pick the ones the change in the work order will need to read or edit. Pick only "
                    'numbers from the list. Answer with JSON: {"picks": [0, 1]}.'
                )
                user = f"Work order:\n{order_text}\n\nSymbols:\n{listing}"
                data = await asyncio.wait_for(_ask_json(gemma, system, user, _SCHEMA, timeout), timeout + 5)
                picks = data.get("picks") if isinstance(data, dict) else None
                if isinstance(picks, list):
                    for p in picks:
                        if isinstance(p, int) and not isinstance(p, bool) and 0 <= p < len(index):
                            chosen.add(p)
            except Exception as exc:  # noqa: BLE001 - the map is advisory
                log.info("Gemma file map unavailable (%s)", str(exc) or type(exc).__name__)
        return [_render(index[i]) for i in sorted(chosen)][:max_entries]
    except Exception as exc:  # noqa: BLE001
        log.debug("file map unavailable: %s", exc)
        return []
