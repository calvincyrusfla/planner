"""SQLite-backed work queue for the Foundry factory floor.

The queue is the single source of truth for task state, builder tickets and
the event log. It is deliberately boring:

* one ``sqlite3`` connection per :class:`FoundryQueue`, opened with
  ``journal_mode=WAL`` and a ``busy_timeout`` so several processes (or the
  dashboard bridge) can read while a worker writes;
* every public method takes :attr:`FoundryQueue.lock` (a re-entrant lock),
  which makes the object safe to share between asyncio tasks and the odd
  worker thread in one process;
* claims run inside ``BEGIN IMMEDIATE`` so two workers can never claim the
  same ticket, even across processes.

Dynamic concurrency rule
------------------------
:meth:`FoundryQueue.apply_sizing` creates one ticket per subtask and gives
each a builder *slot* ``1..N`` where ``N`` is the allocation from the
Foreman's :class:`~foundry.schemas.TaskSizingDirective`. A builder only ever
receives tickets whose slot equals its own slot number and is not above the
task's allocation, so builders with ``slot > allocated_builders`` never get a
ticket and therefore never call Claude. :meth:`FoundryQueue.wait_for_ticket`
is how such idle builders wait: it only polls SQLite and sleeps.

Other modules share the connection (``queue.conn`` guarded by
``queue.lock``) for their own tables, e.g. telemetry.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from foundry.schemas import MAX_RETRIES, TaskSizingDirective, TaskState

__all__ = [
    "ALLOWED_TRANSITIONS",
    "BUSY_TIMEOUT_MS",
    "FoundryQueue",
    "MAX_RETRIES",
    "TERMINAL_STATES",
    "Ticket",
    "TicketStatus",
    "utc_now_iso",
]


#: SQLite busy timeout in milliseconds.
BUSY_TIMEOUT_MS: int = 5000

TicketStatus = Literal["open", "claimed", "done", "blocked"]

#: States a task can never leave.
TERMINAL_STATES: frozenset[TaskState] = frozenset(
    {TaskState.COMPLETE, TaskState.QUARANTINED, TaskState.FAILED}
)

#: Explicit allowed-transition table (from-state -> allowed to-states).
#: ``quarantined`` and ``failed`` are reachable from every non-terminal state.
#: Setting a task to the state it is already in is a harmless no-op.
#: The task state is task-level: with several tickets in flight workers should
#: treat a refused move (e.g. reviewing -> validating) as "already further on".
ALLOWED_TRANSITIONS: dict[TaskState, frozenset[TaskState]] = {
    TaskState.BACKLOG: frozenset({TaskState.SIZING}),
    TaskState.SIZING: frozenset({TaskState.BUILDING}),
    TaskState.BUILDING: frozenset({TaskState.VALIDATING}),
    TaskState.VALIDATING: frozenset({TaskState.REVIEWING, TaskState.BUILDING}),
    TaskState.REVIEWING: frozenset({TaskState.COMPLETE, TaskState.BUILDING}),
    TaskState.COMPLETE: frozenset(),
    TaskState.QUARANTINED: frozenset(),
    TaskState.FAILED: frozenset(),
}
for _state, _targets in list(ALLOWED_TRANSITIONS.items()):
    if _state not in TERMINAL_STATES:
        ALLOWED_TRANSITIONS[_state] = _targets | {TaskState.QUARANTINED, TaskState.FAILED}
del _state, _targets


def utc_now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string (``+00:00`` suffix)."""
    return datetime.now(timezone.utc).isoformat()


class Ticket(BaseModel):
    """One unit of builder work: a subtask of a task bound to a builder slot."""

    model_config = ConfigDict(extra="forbid")

    ticket_id: str
    task_id: str
    team_id: str
    subtask_id: str
    slot: int = Field(ge=1, le=5)
    status: TicketStatus
    attempt: int = Field(ge=0, le=MAX_RETRIES)
    title: str = ""
    acceptance_criteria: list[str] = Field(default_factory=list)
    allowed_files: list[str] = Field(default_factory=list)
    fix_hint: str | None = None
    in_review: bool = False
    claimed_by: str | None = None
    claimed_at: str | None = None
    created_at: str
    updated_at: str


_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id            TEXT PRIMARY KEY,
    title              TEXT NOT NULL,
    description        TEXT NOT NULL DEFAULT '',
    team_id            TEXT NOT NULL,
    state              TEXT NOT NULL,
    complexity         TEXT,
    allocated_builders INTEGER NOT NULL DEFAULT 0,
    sizing_json        TEXT,
    quarantine_reason  TEXT,
    pr_summary_json    TEXT,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    state_changed_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_state ON tasks(state);

CREATE TABLE IF NOT EXISTS tickets (
    ticket_id            TEXT PRIMARY KEY,
    task_id              TEXT NOT NULL REFERENCES tasks(task_id),
    team_id              TEXT NOT NULL,
    subtask_id           TEXT NOT NULL,
    slot                 INTEGER NOT NULL,
    status               TEXT NOT NULL,
    attempt              INTEGER NOT NULL DEFAULT 0,
    title                TEXT NOT NULL DEFAULT '',
    acceptance_criteria  TEXT NOT NULL DEFAULT '[]',
    allowed_files        TEXT NOT NULL DEFAULT '[]',
    fix_hint             TEXT,
    claimed_by           TEXT,
    claimed_at           TEXT,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL,
    UNIQUE (task_id, subtask_id)
);
CREATE INDEX IF NOT EXISTS idx_tickets_claim ON tickets(team_id, slot, status);
CREATE INDEX IF NOT EXISTS idx_tickets_task ON tickets(task_id);

CREATE TABLE IF NOT EXISTS events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       TEXT NOT NULL,
    kind     TEXT NOT NULL,
    task_id  TEXT,
    payload  TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id, id);
CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind, id);

CREATE TABLE IF NOT EXISTS gate_results (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                TEXT NOT NULL,
    task_id           TEXT NOT NULL,
    subtask_id        TEXT NOT NULL,
    check_type        TEXT NOT NULL,
    passed            INTEGER NOT NULL,
    stdout            TEXT NOT NULL DEFAULT '',
    stderr            TEXT NOT NULL DEFAULT '',
    duration_seconds  REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_gate_results_task ON gate_results(task_id);

CREATE TABLE IF NOT EXISTS verdicts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,
    task_id      TEXT NOT NULL,
    reviewer_id  TEXT NOT NULL,
    verdict      TEXT NOT NULL,
    retry_count  INTEGER NOT NULL DEFAULT 0,
    payload      TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_verdicts_task ON verdicts(task_id);

CREATE TABLE IF NOT EXISTS escalations (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                TEXT NOT NULL,
    alert_id          TEXT NOT NULL,
    severity          TEXT NOT NULL,
    category          TEXT NOT NULL,
    prescribed_action TEXT NOT NULL,
    task_id           TEXT,
    payload           TEXT NOT NULL DEFAULT '{}',
    applied_at        TEXT,
    result            TEXT,
    UNIQUE (alert_id)
);

CREATE TABLE IF NOT EXISTS alert_ids (
    alert_id     TEXT PRIMARY KEY,
    reserved_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS kv (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
"""

#: Columns added after the first release; ``_migrate`` adds them to old dbs.
_MIGRATIONS: dict[str, dict[str, str]] = {
    "tickets": {"in_review": "INTEGER NOT NULL DEFAULT 0"},
    "escalations": {"result": "TEXT"},
}


def _state(value: TaskState | str) -> TaskState:
    """Coerce a string or enum into a :class:`TaskState` (ValueError if unknown)."""
    if isinstance(value, TaskState):
        return value
    try:
        return TaskState(value)
    except ValueError as exc:  # pragma: no cover - message only
        raise ValueError(f"unknown task state {value!r}") from exc


def _dump(obj: Any) -> str:
    """Serialize ``obj`` (pydantic models included) to compact JSON."""
    if isinstance(obj, BaseModel):
        return obj.model_dump_json()
    return json.dumps(obj, default=_json_default, separators=(",", ":"))


def _json_default(obj: Any) -> Any:
    if isinstance(obj, BaseModel):
        return obj.model_dump(mode="json")
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, (set, frozenset, tuple)):
        return list(obj)
    if hasattr(obj, "value"):
        return obj.value
    return str(obj)


class FoundryQueue:
    """SQLite (WAL) task/ticket queue with an explicit task state machine.

    Args:
        path: database file path, or ``":memory:"`` for an in-memory queue
            (WAL is unavailable in memory; everything else works the same).
        clock: optional callable returning an aware UTC ``datetime``; used for
            every timestamp so tests can control dwell/stall times.
        busy_timeout_ms: SQLite busy timeout.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        clock: Callable[[], datetime] | None = None,
        busy_timeout_ms: int = BUSY_TIMEOUT_MS,
    ) -> None:
        self.path: str = str(path)
        if self.path != ":memory:" and not self.path.startswith("file:"):
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        #: Re-entrant lock guarding :attr:`conn`; other modules sharing the
        #: connection (telemetry) must hold it while they use ``conn``.
        self.lock: threading.RLock = threading.RLock()
        #: The shared connection (autocommit mode; use :meth:`transaction`).
        self.conn: sqlite3.Connection = sqlite3.connect(
            self.path,
            check_same_thread=False,
            isolation_level=None,
            timeout=busy_timeout_ms / 1000,
        )
        self.conn.row_factory = sqlite3.Row
        with self.lock:
            self.conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
            mode = self.conn.execute("PRAGMA journal_mode=WAL").fetchone()
            self.journal_mode: str = str(mode[0]).lower() if mode else ""
            self.conn.execute("PRAGMA foreign_keys=ON")
            self.conn.execute("PRAGMA synchronous=NORMAL")
            self.conn.executescript(_SCHEMA)
            self._migrate()
        self._closed = False

    def _migrate(self) -> None:
        """Bring a database created by an older version up to the current schema."""
        with self.transaction() as conn:
            for table, columns in _MIGRATIONS.items():
                have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
                for name, ddl in columns.items():
                    if name not in have:
                        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
            # Old escalations tables lack UNIQUE(alert_id): drop duplicates
            # (keep the first) and enforce uniqueness with an index.
            conn.execute(
                "DELETE FROM escalations WHERE id NOT IN"
                " (SELECT MIN(id) FROM escalations GROUP BY alert_id)"
            )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_escalations_alert ON escalations(alert_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_tickets_review ON tickets(in_review, status)"
            )

    # ------------------------------------------------------------------ infra

    def now(self) -> str:
        """Current UTC timestamp (ISO-8601) according to the queue's clock."""
        if self._clock is None:
            return utc_now_iso()
        value = self._clock()
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()

    @contextmanager
    def transaction(self, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        """Run a block atomically: ``BEGIN IMMEDIATE`` ... ``COMMIT``.

        Holds :attr:`lock`. Nested use is safe: an inner ``transaction`` simply
        joins the outer one.
        """
        with self.lock:
            if self.conn.in_transaction:
                yield self.conn
                return
            self.conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            try:
                yield self.conn
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            else:
                self.conn.execute("COMMIT")

    def close(self) -> None:
        """Close the connection (idempotent)."""
        with self.lock:
            if not self._closed:
                self.conn.close()
                self._closed = True

    def __enter__(self) -> FoundryQueue:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ tasks

    def add_task(self, task_id: str, title: str, description: str, team_id: str) -> None:
        """Register a new task in ``backlog``. ValueError if the id exists."""
        now = self.now()
        with self.transaction() as conn:
            if conn.execute("SELECT 1 FROM tasks WHERE task_id=?", (task_id,)).fetchone():
                raise ValueError(f"task {task_id!r} already exists")
            conn.execute(
                "INSERT INTO tasks (task_id, title, description, team_id, state,"
                " created_at, updated_at, state_changed_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (task_id, title, description, team_id, TaskState.BACKLOG.value, now, now, now),
            )
            self._event(conn, "task_added", task_id,
                        {"title": title, "team_id": team_id, "state": TaskState.BACKLOG.value})

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        """Return the task as a dict (``state`` is a :class:`TaskState`), or None."""
        with self.lock:
            row = self.conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        return self._task_dict(row) if row else None

    def tasks_in_state(self, state: TaskState | str) -> list[dict[str, Any]]:
        """All tasks currently in ``state``, oldest first."""
        st = _state(state)
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM tasks WHERE state=? ORDER BY created_at, rowid", (st.value,)
            ).fetchall()
        return [self._task_dict(r) for r in rows]

    def all_tasks(self) -> list[dict[str, Any]]:
        """Every task, oldest first."""
        with self.lock:
            rows = self.conn.execute("SELECT * FROM tasks ORDER BY created_at, rowid").fetchall()
        return [self._task_dict(r) for r in rows]

    def set_state(self, task_id: str, state: TaskState | str, note: str = "") -> None:
        """Move a task to ``state`` following :data:`ALLOWED_TRANSITIONS`.

        Raises:
            KeyError: unknown task.
            ValueError: the transition is not allowed (or the state is unknown).
        """
        target = _state(state)
        with self.transaction() as conn:
            self._transition(conn, task_id, target, note)

    def can_transition(self, current: TaskState | str, target: TaskState | str) -> bool:
        """Whether ``current -> target`` is allowed (same state counts as allowed)."""
        cur, tgt = _state(current), _state(target)
        return cur == tgt or tgt in ALLOWED_TRANSITIONS[cur]

    def quarantine(self, task_id: str, reason: str) -> None:
        """Quarantine a task: state -> quarantined, its unfinished tickets -> blocked."""
        with self.transaction() as conn:
            self._quarantine(conn, task_id, reason)

    def fail(self, task_id: str, reason: str) -> None:
        """Mark a task failed and block its unfinished tickets."""
        now = self.now()
        with self.transaction() as conn:
            self._transition(conn, task_id, TaskState.FAILED, reason)
            conn.execute(
                "UPDATE tickets SET status='blocked', updated_at=?"
                " WHERE task_id=? AND status IN ('open','claimed')",
                (now, task_id),
            )

    def store_pr_summary(self, task_id: str, summary: dict[str, Any]) -> None:
        """Persist the Foreman's commit/PR draft for a finished task."""
        with self.transaction() as conn:
            self._require_task(conn, task_id)
            conn.execute("UPDATE tasks SET pr_summary_json=?, updated_at=? WHERE task_id=?",
                         (_dump(summary), self.now(), task_id))
            self._event(conn, "pr_summary", task_id, {"keys": sorted(summary)})

    # ---------------------------------------------------------------- sizing

    def apply_sizing(self, directive: TaskSizingDirective) -> list[Ticket]:
        """Store the Foreman's allocation and create one ticket per subtask.

        Ticket ``i`` (0-based) gets slot ``i % allocated + 1``, so with the
        normal one-subtask-per-builder directive slot ``k`` holds subtask ``k``.
        The task moves backlog -> sizing -> building. An unknown task id is
        registered on the fly (title = task id). Applying sizing to a task
        that already has tickets raises ValueError; use :meth:`resize`.
        """
        n = int(directive.allocated_builders)
        if not 1 <= n <= 5:
            raise ValueError(f"allocated_builders must be 1..5, got {n}")
        now = self.now()
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE task_id=?", (directive.task_id,)).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO tasks (task_id, title, description, team_id, state,"
                    " created_at, updated_at, state_changed_at) VALUES (?,?,?,?,?,?,?,?)",
                    (directive.task_id, directive.task_id, "", directive.team_id,
                     TaskState.BACKLOG.value, now, now, now),
                )
                self._event(conn, "task_added", directive.task_id,
                            {"title": directive.task_id, "team_id": directive.team_id,
                             "state": TaskState.BACKLOG.value})
                current = TaskState.BACKLOG
            else:
                current = TaskState(row["state"])
            if conn.execute("SELECT 1 FROM tickets WHERE task_id=? LIMIT 1",
                            (directive.task_id,)).fetchone():
                raise ValueError(f"task {directive.task_id!r} is already sized; use resize()")
            if current not in (TaskState.BACKLOG, TaskState.SIZING):
                raise ValueError(
                    f"cannot size task {directive.task_id!r} in state {current.value}")
            if current == TaskState.BACKLOG:
                self._transition(conn, directive.task_id, TaskState.SIZING, "sizing")
            conn.execute(
                "UPDATE tasks SET complexity=?, allocated_builders=?, sizing_json=?, team_id=?,"
                " updated_at=? WHERE task_id=?",
                (directive.complexity, n, _dump(directive), directive.team_id, now,
                 directive.task_id),
            )
            for i, sub in enumerate(directive.subtasks):
                conn.execute(
                    "INSERT INTO tickets (ticket_id, task_id, team_id, subtask_id, slot, status,"
                    " attempt, title, acceptance_criteria, allowed_files, created_at, updated_at)"
                    " VALUES (?,?,?,?,?,'open',0,?,?,?,?,?)",
                    (self.ticket_id_for(directive.task_id, sub.subtask_id), directive.task_id,
                     directive.team_id, sub.subtask_id, i % n + 1, sub.title,
                     _dump(list(sub.acceptance_criteria)), _dump(list(sub.allowed_files)),
                     now, now),
                )
            self._event(conn, "sized", directive.task_id,
                        {"complexity": directive.complexity, "allocated_builders": n,
                         "subtasks": [s.subtask_id for s in directive.subtasks],
                         "override_reason": directive.override_reason})
            self._transition(conn, directive.task_id, TaskState.BUILDING,
                             f"sized {directive.complexity} -> {n} builder(s)")
        return self.tickets_for_task(directive.task_id)

    def resize(self, task_id: str, n: int) -> list[Ticket]:
        """Change a task's builder allocation to ``n`` (1..5).

        Open tickets on slots above ``n`` move to the least-loaded slot
        ``<= n``; when the allocation grows, extra open tickets sharing a slot
        are spread onto the newly available empty slots. Claimed and done
        tickets keep their slot (a worker is already on them). Returns the
        task's tickets after the change.
        """
        if not 1 <= int(n) <= 5:
            raise ValueError(f"allocation must be 1..5, got {n}")
        n = int(n)
        now = self.now()
        with self.transaction() as conn:
            row = self._require_task(conn, task_id)
            if TaskState(row["state"]) in TERMINAL_STATES:
                raise ValueError(f"cannot resize task {task_id!r} in state {row['state']}")
            old = int(row["allocated_builders"])
            conn.execute("UPDATE tasks SET allocated_builders=?, updated_at=? WHERE task_id=?",
                         (n, now, task_id))
            tickets = conn.execute(
                "SELECT ticket_id, slot, status FROM tickets WHERE task_id=? ORDER BY rowid",
                (task_id,),
            ).fetchall()
            load = {s: 0 for s in range(1, n + 1)}
            movable: list[str] = []
            for t in tickets:
                if t["status"] == "open" and t["slot"] > n:
                    movable.append(t["ticket_id"])
                elif t["status"] in ("open", "claimed") and t["slot"] <= n:
                    load[t["slot"]] += 1
            moves: dict[str, int] = {}
            for tid in movable:
                slot = min(load, key=lambda s: (load[s], s))
                load[slot] += 1
                moves[tid] = slot
            # Spread doubled-up open tickets onto empty slots when growing.
            for t in tickets:
                if t["status"] != "open" or t["ticket_id"] in moves:
                    continue
                if load.get(t["slot"], 0) > 1:
                    empty = [s for s in load if load[s] == 0]
                    if empty:
                        load[t["slot"]] -= 1
                        load[empty[0]] += 1
                        moves[t["ticket_id"]] = empty[0]
            for tid, slot in moves.items():
                conn.execute("UPDATE tickets SET slot=?, updated_at=? WHERE ticket_id=?",
                             (slot, now, tid))
            self._event(conn, "resized", task_id,
                        {"from": old, "to": n, "moved": moves})
        return self.tickets_for_task(task_id)

    # --------------------------------------------------------------- tickets

    @staticmethod
    def ticket_id_for(task_id: str, subtask_id: str) -> str:
        """Deterministic ticket id for a task's subtask."""
        return f"{task_id}:{subtask_id}"

    def claim_ticket(self, team_id: str, builder_slot: int) -> Ticket | None:
        """Atomically claim the oldest open ticket for ``team_id`` on ``builder_slot``.

        Only tickets whose slot equals ``builder_slot`` *and* is within the
        task's current allocation are eligible, and only for tasks that are
        not terminal. Returns None when there is nothing for this slot; the
        caller must then not call Claude.
        """
        now = self.now()
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT t.ticket_id, t.task_id FROM tickets t JOIN tasks k ON k.task_id=t.task_id"
                " WHERE t.team_id=? AND t.slot=? AND t.status='open'"
                " AND t.slot <= k.allocated_builders"
                " AND k.state NOT IN ('complete','quarantined','failed')"
                " ORDER BY k.created_at, k.rowid, t.rowid LIMIT 1",
                (team_id, int(builder_slot)),
            ).fetchone()
            if row is None:
                return None
            agent = f"{team_id}-{int(builder_slot)}"
            conn.execute(
                "UPDATE tickets SET status='claimed', claimed_by=?, claimed_at=?, updated_at=?"
                " WHERE ticket_id=?",
                (agent, now, now, row["ticket_id"]),
            )
            self._event(conn, "ticket_claimed", row["task_id"],
                        {"ticket_id": row["ticket_id"], "agent_id": agent})
            return self._ticket(conn, row["ticket_id"])

    async def wait_for_ticket(
        self,
        team_id: str,
        slot: int,
        poll: float = 0.05,
        stop: asyncio.Event | None = None,
    ) -> Ticket | None:
        """Poll until a ticket for ``(team_id, slot)`` can be claimed.

        Never calls Claude or anything else external: it only queries SQLite
        and sleeps. Returns None as soon as ``stop`` is set.
        """
        while True:
            if stop is not None and stop.is_set():
                return None
            ticket = self.claim_ticket(team_id, slot)
            if ticket is not None:
                return ticket
            if stop is None:
                await asyncio.sleep(poll)
            else:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=poll)
                except asyncio.TimeoutError:
                    pass

    def get_ticket(self, ticket_id: str) -> Ticket | None:
        """Return a ticket by id, or None."""
        with self.lock:
            return self._ticket(self.conn, ticket_id)

    def tickets_for_task(self, task_id: str) -> list[Ticket]:
        """All tickets of a task, in slot order."""
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM tickets WHERE task_id=? ORDER BY slot, rowid", (task_id,)
            ).fetchall()
        return [self._ticket_from_row(r) for r in rows]

    def tickets(
        self, *, team_id: str | None = None, status: TicketStatus | None = None
    ) -> list[Ticket]:
        """Tickets filtered by team and/or status (oldest first)."""
        sql, args = "SELECT * FROM tickets WHERE 1=1", []
        if team_id is not None:
            sql += " AND team_id=?"
            args.append(team_id)
        if status is not None:
            sql += " AND status=?"
            args.append(status)
        with self.lock:
            rows = self.conn.execute(sql + " ORDER BY rowid", args).fetchall()
        return [self._ticket_from_row(r) for r in rows]

    def complete_ticket(self, ticket_id: str) -> bool:
        """Mark a ticket done. Returns True when every ticket of its task is done.

        When the last ticket finishes the task is moved to ``complete``
        (from any non-terminal state: the reviewer's approval is what closes it).
        """
        now = self.now()
        with self.transaction() as conn:
            t = self._require_ticket(conn, ticket_id)
            conn.execute("UPDATE tickets SET status='done', updated_at=? WHERE ticket_id=?",
                         (now, ticket_id))
            self._event(conn, "ticket_done", t["task_id"], {"ticket_id": ticket_id})
            left = conn.execute(
                "SELECT COUNT(*) FROM tickets WHERE task_id=? AND status!='done'",
                (t["task_id"],),
            ).fetchone()[0]
            if left:
                return False
            row = self._require_task(conn, t["task_id"])
            cur = TaskState(row["state"])
            if cur not in TERMINAL_STATES:
                self._set_state_row(conn, t["task_id"], cur, TaskState.COMPLETE,
                                    "all tickets done")
            return True

    def block_ticket(self, ticket_id: str, notes: str = "") -> None:
        """Mark a ticket blocked (a builder reported it cannot proceed)."""
        now = self.now()
        with self.transaction() as conn:
            t = self._require_ticket(conn, ticket_id)
            conn.execute("UPDATE tickets SET status='blocked', fix_hint=COALESCE(?, fix_hint),"
                         " updated_at=? WHERE ticket_id=?", (notes or None, now, ticket_id))
            self._event(conn, "ticket_blocked", t["task_id"],
                        {"ticket_id": ticket_id, "notes": notes})

    def reopen_ticket(self, ticket_id: str, fix_hint: str | None) -> bool:
        """Send a ticket back to its builder with ``fix_hint`` (attempt + 1).

        If the new attempt would exceed :data:`MAX_RETRIES` the task is
        quarantined instead (its tickets blocked) and False is returned; the
        ticket is not reopened, so no further build happens. Otherwise the
        ticket is open again (moved onto a slot within the current allocation
        if needed), the task returns to ``building`` and True is returned.
        """
        now = self.now()
        with self.transaction() as conn:
            t = self._require_ticket(conn, ticket_id)
            task = self._require_task(conn, t["task_id"])
            cur = TaskState(task["state"])
            if cur in TERMINAL_STATES:
                return False
            attempt = int(t["attempt"]) + 1
            if attempt > MAX_RETRIES:
                self._event(conn, "retry_exhausted", t["task_id"],
                            {"ticket_id": ticket_id, "attempts": attempt, "fix_hint": fix_hint})
                self._quarantine(conn, t["task_id"],
                                 f"retry limit reached on {t['subtask_id']}: {fix_hint or ''}".strip())
                return False
            slot = int(t["slot"])
            alloc = int(task["allocated_builders"]) or 1
            if slot > alloc:
                slot = self._least_loaded_slot(conn, t["task_id"], alloc)
            conn.execute(
                "UPDATE tickets SET status='open', attempt=?, fix_hint=?, slot=?, claimed_by=NULL,"
                " claimed_at=NULL, updated_at=? WHERE ticket_id=?",
                (attempt, fix_hint, slot, now, ticket_id),
            )
            self._event(conn, "ticket_reopened", t["task_id"],
                        {"ticket_id": ticket_id, "attempt": attempt, "fix_hint": fix_hint})
            if cur != TaskState.BUILDING:
                if TaskState.BUILDING in ALLOWED_TRANSITIONS[cur]:
                    self._transition(conn, t["task_id"], TaskState.BUILDING,
                                     f"retry {attempt}")
                else:  # backlog/sizing cannot happen with tickets; be explicit
                    self._set_state_row(conn, t["task_id"], cur, TaskState.BUILDING,
                                        f"retry {attempt}")
            return True

    def resume(self) -> int:
        """Recover after a crash: reopen every ticket left ``claimed``.

        The attempt counter is unchanged (a crash is not the builder's fault).
        Tasks caught in ``validating``/``reviewing`` go back to ``building``.
        Returns the number of tickets reopened.
        """
        now = self.now()
        with self.transaction() as conn:
            rows = conn.execute(
                "SELECT t.ticket_id, t.task_id, t.claimed_by FROM tickets t"
                " JOIN tasks k ON k.task_id=t.task_id"
                " WHERE t.status='claimed' AND k.state NOT IN ('complete','quarantined','failed')"
            ).fetchall()
            tasks: set[str] = set()
            for r in rows:
                conn.execute(
                    "UPDATE tickets SET status='open', claimed_by=NULL, claimed_at=NULL,"
                    " updated_at=? WHERE ticket_id=?",
                    (now, r["ticket_id"]),
                )
                self._event(conn, "ticket_resumed", r["task_id"],
                            {"ticket_id": r["ticket_id"], "was_claimed_by": r["claimed_by"]})
                tasks.add(r["task_id"])
            for task_id in sorted(tasks):
                cur = TaskState(self._require_task(conn, task_id)["state"])
                if cur in (TaskState.VALIDATING, TaskState.REVIEWING):
                    self._transition(conn, task_id, TaskState.BUILDING, "resumed after crash")
            return len(rows)

    # ---------------------------------------------------------------- events

    def record_event(self, kind: str, task_id: str | None, payload: dict[str, Any] | None = None) -> int:
        """Append an event to the log; returns its id."""
        with self.transaction() as conn:
            return self._event(conn, kind, task_id, payload or {})

    def events(
        self,
        *,
        task_id: str | None = None,
        kind: str | None = None,
        since_id: int = 0,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Events in id order: dicts with id, ts, kind, task_id, payload (dict)."""
        sql, args = "SELECT * FROM events WHERE id>?", [since_id]
        if task_id is not None:
            sql += " AND task_id=?"
            args.append(task_id)
        if kind is not None:
            sql += " AND kind=?"
            args.append(kind)
        sql += " ORDER BY id"
        if limit is not None:
            sql += " LIMIT ?"
            args.append(int(limit))
        with self.lock:
            rows = self.conn.execute(sql, args).fetchall()
        return [
            {"id": r["id"], "ts": r["ts"], "kind": r["kind"], "task_id": r["task_id"],
             "payload": json.loads(r["payload"] or "{}")}
            for r in rows
        ]

    def last_event_at(self, task_id: str) -> str | None:
        """Timestamp of the latest event for a task (stall detection)."""
        with self.lock:
            row = self.conn.execute(
                "SELECT ts FROM events WHERE task_id=? ORDER BY id DESC LIMIT 1", (task_id,)
            ).fetchone()
        return row["ts"] if row else None

    def record_gate_result(self, result: Any) -> None:
        """Store a :class:`~foundry.schemas.DeterministicGateResult` (or equivalent dict)."""
        d = result.model_dump(mode="json") if isinstance(result, BaseModel) else dict(result)
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO gate_results (ts, task_id, subtask_id, check_type, passed, stdout,"
                " stderr, duration_seconds) VALUES (?,?,?,?,?,?,?,?)",
                (self.now(), d["task_id"], d.get("subtask_id", ""), d["check_type"],
                 1 if d["passed"] else 0, d.get("stdout", ""), d.get("stderr", ""),
                 float(d.get("duration_seconds", 0.0))),
            )
            self._event(conn, "gate_result", d["task_id"],
                        {"subtask_id": d.get("subtask_id"), "check_type": d["check_type"],
                         "passed": bool(d["passed"])})

    def gate_failure_count(self, task_id: str) -> int:
        """Number of failed gate results recorded for a task."""
        with self.lock:
            return int(self.conn.execute(
                "SELECT COUNT(*) FROM gate_results WHERE task_id=? AND passed=0", (task_id,)
            ).fetchone()[0])

    def record_verdict(self, verdict: Any) -> None:
        """Store a :class:`~foundry.schemas.ReviewerVerdict` (or equivalent dict)."""
        d = verdict.model_dump(mode="json") if isinstance(verdict, BaseModel) else dict(verdict)
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO verdicts (ts, task_id, reviewer_id, verdict, retry_count, payload)"
                " VALUES (?,?,?,?,?,?)",
                (self.now(), d["task_id"], d["reviewer_id"], d["verdict"],
                 int(d.get("retry_count", 0)), _dump(d)),
            )
            self._event(conn, "verdict", d["task_id"],
                        {"reviewer_id": d["reviewer_id"], "verdict": d["verdict"]})

    def verdicts(self, task_id: str) -> list[dict[str, Any]]:
        """Recorded verdicts for a task (payload parsed)."""
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM verdicts WHERE task_id=? ORDER BY id", (task_id,)
            ).fetchall()
        return [{"id": r["id"], "ts": r["ts"], **json.loads(r["payload"])} for r in rows]

    def record_escalation(self, alert: Any, *, applied: bool = False) -> None:
        """Store a :class:`~foundry.schemas.ForemanEscalation` (or equivalent dict)."""
        d = alert.model_dump(mode="json") if isinstance(alert, BaseModel) else dict(alert)
        task_id = (d.get("target_entity") or {}).get("task_id")
        now = self.now()
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO escalations (ts, alert_id, severity, category, prescribed_action,"
                " task_id, payload, applied_at) VALUES (?,?,?,?,?,?,?,?)",
                (now, d["alert_id"], d["severity"], d["category"], d["prescribed_action"],
                 task_id, _dump(d), now if applied else None),
            )
            self._event(conn, "escalation", task_id,
                        {"alert_id": d["alert_id"], "category": d["category"],
                         "prescribed_action": d["prescribed_action"]})

    def escalations(self) -> list[dict[str, Any]]:
        """All recorded escalations (payload parsed, plus ``applied_at``)."""
        with self.lock:
            rows = self.conn.execute("SELECT * FROM escalations ORDER BY id").fetchall()
        return [{**json.loads(r["payload"]), "applied_at": r["applied_at"]} for r in rows]

    # --------------------------------------------------------------- summary

    def summary(self) -> dict[str, Any]:
        """Counts used by the Foreman/bridge: tasks per state, tickets per team/status."""
        with self.lock:
            states = {r[0]: r[1] for r in self.conn.execute(
                "SELECT state, COUNT(*) FROM tasks GROUP BY state")}
            per_team: dict[str, dict[str, int]] = {}
            for r in self.conn.execute(
                    "SELECT team_id, status, COUNT(*) FROM tickets GROUP BY team_id, status"):
                per_team.setdefault(r[0], {})[r[1]] = r[2]
        return {
            "tasks": {s.value: int(states.get(s.value, 0)) for s in TaskState},
            "tickets": per_team,
            "queue_depth": int(sum(v.get("open", 0) for v in per_team.values())),
        }

    # -------------------------------------------------------------- internals

    def _event(self, conn: sqlite3.Connection, kind: str, task_id: str | None,
               payload: dict[str, Any]) -> int:
        cur = conn.execute(
            "INSERT INTO events (ts, kind, task_id, payload) VALUES (?,?,?,?)",
            (self.now(), kind, task_id, _dump(payload)),
        )
        return int(cur.lastrowid or 0)

    def _require_task(self, conn: sqlite3.Connection, task_id: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown task {task_id!r}")
        return row

    def _require_ticket(self, conn: sqlite3.Connection, ticket_id: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM tickets WHERE ticket_id=?", (ticket_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown ticket {ticket_id!r}")
        return row

    def _transition(self, conn: sqlite3.Connection, task_id: str, target: TaskState,
                    note: str) -> None:
        row = self._require_task(conn, task_id)
        cur = TaskState(row["state"])
        if cur == target:
            return
        if target not in ALLOWED_TRANSITIONS[cur]:
            raise ValueError(
                f"illegal transition for {task_id!r}: {cur.value} -> {target.value}")
        self._set_state_row(conn, task_id, cur, target, note)

    def _set_state_row(self, conn: sqlite3.Connection, task_id: str, cur: TaskState,
                       target: TaskState, note: str) -> None:
        now = self.now()
        conn.execute("UPDATE tasks SET state=?, updated_at=?, state_changed_at=? WHERE task_id=?",
                     (target.value, now, now, task_id))
        self._event(conn, "state", task_id,
                    {"from": cur.value, "to": target.value, "note": note})

    def _quarantine(self, conn: sqlite3.Connection, task_id: str, reason: str) -> None:
        row = self._require_task(conn, task_id)
        cur = TaskState(row["state"])
        if cur == TaskState.QUARANTINED:
            return
        self._transition(conn, task_id, TaskState.QUARANTINED, reason)
        now = self.now()
        conn.execute("UPDATE tasks SET quarantine_reason=?, updated_at=? WHERE task_id=?",
                     (reason, now, task_id))
        conn.execute("UPDATE tickets SET status='blocked', updated_at=?"
                     " WHERE task_id=? AND status IN ('open','claimed')", (now, task_id))
        self._event(conn, "quarantined", task_id, {"reason": reason})

    def _least_loaded_slot(self, conn: sqlite3.Connection, task_id: str, alloc: int) -> int:
        load = {s: 0 for s in range(1, alloc + 1)}
        for r in conn.execute("SELECT slot FROM tickets WHERE task_id=? AND status IN"
                              " ('open','claimed')", (task_id,)):
            if r["slot"] in load:
                load[r["slot"]] += 1
        return min(load, key=lambda s: (load[s], s))

    def _ticket(self, conn: sqlite3.Connection, ticket_id: str) -> Ticket | None:
        row = conn.execute("SELECT * FROM tickets WHERE ticket_id=?", (ticket_id,)).fetchone()
        return self._ticket_from_row(row) if row else None

    @staticmethod
    def _ticket_from_row(row: sqlite3.Row) -> Ticket:
        return Ticket(
            ticket_id=row["ticket_id"], task_id=row["task_id"], team_id=row["team_id"],
            subtask_id=row["subtask_id"], slot=row["slot"], status=row["status"],
            attempt=row["attempt"], title=row["title"],
            acceptance_criteria=json.loads(row["acceptance_criteria"] or "[]"),
            allowed_files=json.loads(row["allowed_files"] or "[]"),
            fix_hint=row["fix_hint"], in_review=bool(row["in_review"]),
            claimed_by=row["claimed_by"],
            claimed_at=row["claimed_at"], created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _task_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "task_id": row["task_id"],
            "title": row["title"],
            "description": row["description"],
            "team_id": row["team_id"],
            "state": TaskState(row["state"]),
            "complexity": row["complexity"],
            "allocated_builders": int(row["allocated_builders"]),
            "sizing": json.loads(row["sizing_json"]) if row["sizing_json"] else None,
            "quarantine_reason": row["quarantine_reason"],
            "pr_summary": json.loads(row["pr_summary_json"]) if row["pr_summary_json"] else None,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "state_changed_at": row["state_changed_at"],
        }
