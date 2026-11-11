"""Durable archive of every Claude call's prompt and reply, linked to its piece."""

from __future__ import annotations

import logging
import zlib
from datetime import datetime, timedelta, timezone
from typing import Any

log = logging.getLogger(__name__)

_CREATE = """
CREATE TABLE IF NOT EXISTS prompt_archive (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                TEXT NOT NULL,
    task_id           TEXT,
    ticket_id         TEXT,
    attempt           INTEGER,
    role              TEXT,
    agent_id          TEXT,
    session_id        TEXT,
    base_sha          TEXT,
    prompt            BLOB NOT NULL,
    reply             BLOB NOT NULL,
    input_tokens      INTEGER NOT NULL DEFAULT 0,
    output_tokens     INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0
)
"""

_META = (
    "id, ts, task_id, ticket_id, attempt, role, agent_id, session_id, base_sha, "
    "input_tokens, output_tokens, cache_read_tokens"
)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _pack(text: Any) -> bytes:
    return zlib.compress(str(text or "").encode("utf-8", errors="replace"), 6)


def _unpack(blob: Any) -> str:
    return zlib.decompress(bytes(blob)).decode("utf-8", errors="replace")


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _opt(value: Any) -> str | None:
    return None if value is None else str(value)


def _ensure(queue: Any) -> None:
    with queue.transaction() as conn:
        conn.execute(_CREATE)


def archive_call(
    queue: Any,
    *,
    task_id: Any,
    ticket_id: Any,
    attempt: int | None,
    role: str,
    agent_id: str,
    prompt: str,
    result: Any,
    base_sha: str | None = None,
) -> int | None:
    """Store one call; its row id, or None when anything went wrong (never raises)."""
    try:
        _ensure(queue)
        with queue.transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO prompt_archive (ts, task_id, ticket_id, attempt, role, agent_id, session_id, "
                "base_sha, prompt, reply, input_tokens, output_tokens, cache_read_tokens) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    _now(), _opt(task_id), _opt(ticket_id), None if attempt is None else int(attempt),
                    str(role), str(agent_id), _opt(getattr(result, "session_id", None)), _opt(base_sha),
                    _pack(prompt), _pack(getattr(result, "text", "")),
                    _int(getattr(result, "input_tokens", 0)), _int(getattr(result, "output_tokens", 0)),
                    _int(getattr(result, "cache_read_tokens", 0)),
                ),
            )
            row_id = cursor.lastrowid
        return int(row_id) if row_id is not None else None
    except Exception as exc:  # noqa: BLE001 - archiving must never fail a call
        log.debug("prompt archive failed: %s", exc, exc_info=True)
        return None


def get_call(queue: Any, id: int) -> dict[str, Any] | None:  # noqa: A002
    """One archived call with its decompressed ``prompt`` and ``reply``; None if unknown."""
    _ensure(queue)
    with queue.lock:
        row = queue.conn.execute(
            f"SELECT {_META}, prompt, reply FROM prompt_archive WHERE id=?", (int(id),)
        ).fetchone()
    if row is None:
        return None
    data = dict(row)
    data["prompt"] = _unpack(data["prompt"])
    data["reply"] = _unpack(data["reply"])
    return data


def calls_for(
    queue: Any, *, task_id: str | None = None, ticket_id: str | None = None, role: str | None = None
) -> list[dict[str, Any]]:
    """Archived calls (without the texts), oldest first, filtered by the given fields."""
    _ensure(queue)
    clauses: list[str] = []
    args: list[Any] = []
    for column, value in (("task_id", task_id), ("ticket_id", ticket_id), ("role", role)):
        if value is not None:
            clauses.append(f"{column}=?")
            args.append(str(value))
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    with queue.lock:
        rows = queue.conn.execute(f"SELECT {_META} FROM prompt_archive{where} ORDER BY id", args).fetchall()
    return [dict(row) for row in rows]


def prune(queue: Any, keep_days: int) -> int:
    """Delete calls older than ``keep_days`` days; the number of rows removed."""
    _ensure(queue)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max(1, int(keep_days)))).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )
    with queue.transaction() as conn:
        return int(conn.execute("DELETE FROM prompt_archive WHERE ts < ?", (cutoff,)).rowcount or 0)
