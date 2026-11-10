"""The Warehouse Ledger: one record of every batch and piece Foundry works on.

Two tables in the queue's own SQLite database hold the current state of each
board batch and of each of its pieces. Every change also lands in the queue's
event log (kinds ``ledger_batch`` and ``ledger_piece``) in the same
transaction, so the history can be read back as events. A batch is identified
by its id and ``createdAt`` together, because two boards may reuse an S-id.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from foundry.order_schema import OPEN_BATCH
from foundry.orchestrator_queue import FoundryQueue

_CREATE_BATCHES = """
CREATE TABLE IF NOT EXISTS ledger_batches (
    batch_id     TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    title        TEXT,
    goal         TEXT,
    repo         TEXT,
    base         TEXT,
    status       TEXT,
    claimed_at   TEXT,
    finished_at  TEXT,
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (batch_id, created_at)
)
"""

_CREATE_PIECES = """
CREATE TABLE IF NOT EXISTS ledger_pieces (
    batch_id       TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    key            TEXT NOT NULL,
    title          TEXT,
    order_text     TEXT,
    files          TEXT,
    read_files     TEXT,
    acceptance     TEXT,
    size           TEXT,
    foundry_task   TEXT,
    state          TEXT,
    pr_url         TEXT,
    pr_state       TEXT,
    error          TEXT,
    help_question  TEXT,
    help_answer    TEXT,
    usage          TEXT,
    updated_at     TEXT NOT NULL,
    PRIMARY KEY (batch_id, created_at, key)
)
"""

#: The fields :meth:`Ledger.upsert_piece` accepts; ``updated_at`` is stamped by the ledger.
PIECE_FIELDS = frozenset([
    "title", "order_text", "files", "read_files", "acceptance", "size", "foundry_task",
    "state", "pr_url", "pr_state", "error", "help_question", "help_answer", "usage",
])

_JSON_COLUMNS = frozenset(["files", "read_files", "acceptance", "usage"])


def _encode(column: str, value: Any) -> str | None:
    """The text stored for ``value``: stable, so equal values always compare equal."""
    if value is None:
        return None
    if column in _JSON_COLUMNS or isinstance(value, (list, tuple, dict)):
        return json.dumps(value, sort_keys=True, separators=(",", ":"))
    return value if isinstance(value, str) else str(value)


def _decode(column: str, value: Any) -> Any:
    """The caller's view of a stored column value."""
    if column in _JSON_COLUMNS and value is not None:
        return json.loads(value)
    return value


def _decoded_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {column: _decode(column, value) for column, value in dict(row).items()}


def _created(created_at: Any) -> str:
    """``created_at`` as stored: a batch without one is kept under the empty string."""
    return "" if created_at is None else str(created_at)


class Ledger:
    """Batch and piece records on a :class:`FoundryQueue`'s own connection."""

    def __init__(self, queue: FoundryQueue) -> None:
        self.queue = queue
        with queue.transaction() as conn:
            conn.execute(_CREATE_BATCHES)
            conn.execute(_CREATE_PIECES)

    def upsert_batch(self, batch: Mapping[str, Any]) -> bool:
        """Record a board batch dict; True if anything changed.

        The dict is the batch's whole state, so a key it lacks clears that column.
        """
        claim = batch.get("claimedBy")
        values = {
            "title": batch.get("title"),
            "goal": batch.get("goal"),
            "repo": batch.get("repo"),
            "base": batch.get("base"),
            "status": batch.get("status"),
            "claimed_at": claim.get("at") if isinstance(claim, Mapping) else None,
            "finished_at": batch.get("finishedAt"),
        }
        identity = {"batch_id": str(batch["id"]), "created_at": _created(batch.get("createdAt"))}
        return self._apply("ledger_batches", "ledger_batch", identity, values)

    def upsert_piece(self, batch_id: str, created_at: str | None, key: str, **fields: Any) -> bool:
        """Record the given fields of one piece; True if anything changed.

        Fields left out keep their stored value. Raises ValueError for a field
        that is not in :data:`PIECE_FIELDS`.
        """
        unknown = sorted(set(fields) - PIECE_FIELDS)
        if unknown:
            raise ValueError(f"unknown ledger piece field(s): {', '.join(unknown)}")
        identity = {"batch_id": str(batch_id), "created_at": _created(created_at), "key": str(key)}
        return self._apply("ledger_pieces", "ledger_piece", identity, fields)

    def batch(self, batch_id: str, created_at: str | None = None) -> dict[str, Any] | None:
        """One batch, the newest ``created_at`` when none is given; None if unknown."""
        sql = "SELECT * FROM ledger_batches WHERE batch_id=?"
        args = [str(batch_id)]
        if created_at is not None:
            sql += " AND created_at=?"
            args.append(str(created_at))
        with self.queue.lock:
            row = self.queue.conn.execute(sql + " ORDER BY created_at DESC LIMIT 1", args).fetchone()
        return _decoded_row(row) if row else None

    def pieces(self, batch_id: str, created_at: str | None) -> list[dict[str, Any]]:
        """The pieces of one batch, in the order they were first recorded."""
        with self.queue.lock:
            rows = self.queue.conn.execute(
                "SELECT * FROM ledger_pieces WHERE batch_id=? AND created_at=? ORDER BY rowid",
                (str(batch_id), _created(created_at)),
            ).fetchall()
        return [_decoded_row(row) for row in rows]

    def open_batches(self) -> list[dict[str, Any]]:
        """Every batch whose status is open (see ``OPEN_BATCH``), oldest first."""
        statuses = sorted(OPEN_BATCH)
        marks = ",".join("?" for _ in statuses)
        with self.queue.lock:
            rows = self.queue.conn.execute(
                f"SELECT * FROM ledger_batches WHERE status IN ({marks})"
                " ORDER BY created_at, rowid",
                statuses,
            ).fetchall()
        return [_decoded_row(row) for row in rows]

    def _apply(
        self, table: str, kind: str, identity: Mapping[str, str], values: Mapping[str, Any]
    ) -> bool:
        """Write what differs from the stored row and log it, in one transaction."""
        encoded = {column: _encode(column, value) for column, value in values.items()}
        where = " AND ".join(f"{column}=?" for column in identity)
        id_args = list(identity.values())
        with self.queue.transaction() as conn:
            row = conn.execute(f"SELECT * FROM {table} WHERE {where}", id_args).fetchone()
            stored = dict(row) if row else {}
            changed = {c: v for c, v in encoded.items() if v != stored.get(c)}
            if row is not None and not changed:
                return False
            now = self.queue.now()
            if row is None:
                inserted = {**identity, **encoded, "updated_at": now}
                columns = ", ".join(inserted)
                marks = ",".join("?" for _ in inserted)
                conn.execute(
                    f"INSERT INTO {table} ({columns}) VALUES ({marks})", list(inserted.values())
                )
            else:
                assignments = ", ".join(f"{column}=?" for column in [*changed, "updated_at"])
                conn.execute(
                    f"UPDATE {table} SET {assignments} WHERE {where}",
                    [*changed.values(), now, *id_args],
                )
            payload = {
                **identity,
                "changed": {column: _decode(column, value) for column, value in changed.items()},
            }
            self.queue.record_event(kind, {**stored, **encoded}.get("foundry_task"), payload)
        return True
