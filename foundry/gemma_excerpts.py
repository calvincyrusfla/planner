"""Gemma picks the gate-output lines a retry needs (advisory; any failure gives None)."""

from __future__ import annotations

import logging
from typing import Any

from foundry.gemma_notes import _ask_json

log = logging.getLogger(__name__)

MAX_LINES = 400
MAX_LINE_CHARS = 300
MAX_RANGES = 6

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "ranges": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"start": {"type": "integer"}, "end": {"type": "integer"}},
                "required": ["start", "end"],
            },
        }
    },
    "required": ["ranges"],
}


def _valid_ranges(data: Any, first: int, last: int) -> list[tuple[int, int]]:
    """Ranges from Gemma's answer, clipped to ``first..last``, merged when they overlap."""
    items = data.get("ranges") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    found: list[tuple[int, int]] = []
    for item in items[:MAX_RANGES]:
        if not isinstance(item, dict):
            continue
        start, end = item.get("start"), item.get("end")
        if isinstance(start, bool) or isinstance(end, bool) or not isinstance(start, int) or not isinstance(end, int):
            continue
        if start > end or end < first or start > last:
            continue
        found.append((max(start, first), min(end, last)))
    return found


def _merge(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _build(lines: list[str], offset: int, ranges: list[tuple[int, int]]) -> str:
    """The excerpt: ``ranges`` (1-based numbers, ``offset`` = number of the first line) from ``lines``."""
    parts: list[str] = []
    for start, end in ranges:
        parts.append("\n".join(lines[start - offset: end - offset + 1]))
    return "\n...\n".join(parts)


async def error_excerpt(gemma: Any, output: str, *, timeout: float, max_chars: int) -> str | None:
    """The lines of ``output`` that hold the failure, copied exactly; None on any problem."""
    try:
        lines = output.splitlines()
        offset = 1
        if len(lines) > MAX_LINES:
            offset = len(lines) - MAX_LINES + 1
            lines = lines[-MAX_LINES:]
        if not lines:
            return None
        first, last = offset, offset + len(lines) - 1
        listing = "\n".join(f"{offset + i}: {line[:MAX_LINE_CHARS]}" for i, line in enumerate(lines))
        system = (
            "You read the output of a failed check. Say which line ranges hold the failure itself: failing "
            "test names, assertion messages, tracebacks ending in the error, type errors with file:line. "
            f"Give at most {MAX_RANGES} ranges, by the line numbers shown. "
            'Answer with JSON: {"ranges": [{"start": 1, "end": 5}]}.'
        )
        data = await _ask_json(gemma, system, f"Output:\n{listing}", _SCHEMA, timeout)
        picked = _valid_ranges(data, first, last)
        if not picked:
            return None
        ranges = _merge([(max(first, s - 1), min(last, e + 1)) for s, e in picked])
        while ranges:
            text = _build(lines, offset, ranges).strip()
            if text and len(text) <= max_chars:
                return text
            if len(ranges) == 1:
                return None
            ranges.pop()
        return None
    except Exception as exc:  # noqa: BLE001 - the excerpt is advisory
        log.info("Gemma error excerpt unavailable (%s)", str(exc) or type(exc).__name__)
        return None
