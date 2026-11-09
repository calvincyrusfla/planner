"""Foundry usage metrics for the board, read from the telemetry database.

The usage table is found by inspection, not by name: it is the table with a
``task_id`` (or ``task``) column and at least one column ending in ``tokens``.
When several tables qualify, the one with the most rows is used (raw records
outnumber rollups) and the result names it under ``source``. When no table
qualifies, or no connection can be found, :class:`MetricsUnavailable` is raised,
so a schema mismatch is never reported as zero usage.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any, Mapping

log = logging.getLogger("foundry.metrics_report")

__all__ = ["MetricsUnavailable", "build_metrics"]

_CONNECTION_ATTRS = ("conn", "_conn", "connection", "_connection", "db", "_db")
_TASK_COLUMNS = ("task_id", "task")
_SAVED_KEYS = {
    "passed": "passed",
    "sent_back": "sentBack",
    "advised": "advised",
    "skipped": "skipped",
    "fallback": "fallback",
}


class MetricsUnavailable(LookupError):
    """The telemetry database or its usage table could not be found."""


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _connection(telemetry: Any) -> sqlite3.Connection:
    if isinstance(telemetry, sqlite3.Connection):
        return telemetry
    for name in _CONNECTION_ATTRS:
        value = getattr(telemetry, name, None)
        if isinstance(value, sqlite3.Connection):
            return value
    raise MetricsUnavailable(f"{type(telemetry).__name__} exposes no SQLite connection")


def _usage_table(conn: sqlite3.Connection) -> tuple[str, str, list[str]]:
    """``(table, task column, token columns)`` of the usage table."""
    names = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )]
    found: list[tuple[int, str, str, list[str]]] = []
    for table in names:
        columns = [str(r[1]) for r in conn.execute(f"PRAGMA table_info({_quote(table)})")]
        task = next((c for c in columns if c.lower() in _TASK_COLUMNS), None)
        tokens = [c for c in columns if c.lower().endswith("tokens") and "saved" not in c.lower()]
        if task is not None and tokens:
            rows = int(conn.execute(f"SELECT COUNT(*) FROM {_quote(table)}").fetchone()[0])
            found.append((rows, table, task, tokens))
    if not found:
        raise MetricsUnavailable("no telemetry table has a task column and token columns")
    found.sort(key=lambda item: (-item[0], item[1]))
    if len(found) > 1:
        log.warning("metrics report: using table %s; also qualified: %s", found[0][1],
                    ", ".join(item[1] for item in found[1:]))
    _, table, task, tokens = found[0]
    return table, task, tokens


def _usage(values: Any, tokens: list[str]) -> dict[str, int]:
    """``{records, <token column>...}`` from a ``COUNT(*), SUM(...)...`` row slice."""
    out = {"records": int(values[0] or 0)}
    out.update({column: int(values[i + 1] or 0) for i, column in enumerate(tokens)})
    return out


def _saved_by_gemma(telemetry: Any) -> dict[str, int]:
    """Gemma pre-review counters; ``{}`` when the telemetry has none."""
    fn = getattr(telemetry, "pre_review_counts", None)
    raw = fn() if callable(fn) else None
    if not isinstance(raw, Mapping):
        return {}
    out = {name: int(raw.get(key) or 0) for key, name in _SAVED_KEYS.items()}
    avoided = raw.get("claude_reviews_avoided")
    out["claudeReviewsAvoided"] = out["sentBack"] if avoided is None else int(avoided or 0)
    return out


def build_metrics(telemetry: Any) -> dict[str, Any]:
    """``{source, totals, byTask, savedByGemma}`` from a telemetry object or SQLite connection.

    ``totals`` covers every usage row, including rows with no task; ``byTask``
    maps each task id to the same fields. Raises :class:`MetricsUnavailable`.
    """
    conn = _connection(telemetry)
    table, task, tokens = _usage_table(conn)
    sums = ", ".join(f"COALESCE(SUM({_quote(c)}), 0)" for c in tokens)
    source = _quote(table)
    column = _quote(task)
    totals = conn.execute(f"SELECT COUNT(*), {sums} FROM {source}").fetchone()
    rows = conn.execute(
        f"SELECT {column}, COUNT(*), {sums} FROM {source} WHERE {column} IS NOT NULL GROUP BY {column}"
    ).fetchall()
    return {
        "source": table,
        "totals": _usage(totals, tokens),
        "byTask": {str(row[0]): _usage(tuple(row)[1:], tokens) for row in rows},
        "savedByGemma": _saved_by_gemma(telemetry),
    }
