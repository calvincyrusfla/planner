"""Factory telemetry: tokens, savings, durations, gates, idle agents, dwell, burn.

``Telemetry`` stores every metric in its own ``telemetry_*`` tables inside the
same SQLite database as :class:`foundry.orchestrator_queue.FoundryQueue`, using
the queue's connection (``queue.conn``) and lock (``queue.lock``) so that one
process keeps a single writer connection.

Metrics
-------
* tokens per role and per task (``record_tokens``)
* tokens saved by the Gemma Shop Foreman, by source (``record_tokens_saved``)
* build / review durations (``record_duration``)
* deterministic gate failures and retries (``record_gate_failure``,
  ``record_retry``)
* idle agent count per team (``set_idle``)
* queue dwell: time a task spends waiting for a reviewer
  (``mark_review_wait`` / ``mark_review_start`` / ``queue_dwell``)
* moving burn rates over 1- and 15-minute windows, including RPM
  (``burn_rate``)

``snapshot()`` returns one JSON-able dict used by the Shop Foreman and the
board bridge.  All windows use an injectable ``clock`` (epoch seconds) so
tests can move time deterministically.
"""

from __future__ import annotations

import math
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, Iterator, get_args

from foundry.schemas import AgentRole, DurationPhase, TokensSavedSource

if TYPE_CHECKING:  # pragma: no cover - typing only
    from foundry.orchestrator_queue import FoundryQueue

__all__ = [
    "Telemetry",
    "estimate_tokens",
    "Role",
    "SavedSource",
    "Phase",
    "ROLES",
    "SAVED_SOURCES",
    "PHASES",
]

Role = AgentRole
"""Claude agent roles (``schemas.AgentRole``). Gemma usage is not Claude burn."""
SavedSource = TokensSavedSource
Phase = DurationPhase

ROLES: tuple[str, ...] = get_args(Role)
SAVED_SOURCES: tuple[str, ...] = get_args(SavedSource)
PHASES: tuple[str, ...] = get_args(Phase)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS telemetry_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    ts_iso TEXT NOT NULL,
    task_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    role TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_telemetry_tokens_ts ON telemetry_tokens(ts);
CREATE TABLE IF NOT EXISTS telemetry_saved (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    ts_iso TEXT NOT NULL,
    task_id TEXT NOT NULL,
    source TEXT NOT NULL,
    tokens INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS telemetry_durations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    ts_iso TEXT NOT NULL,
    task_id TEXT NOT NULL,
    phase TEXT NOT NULL,
    seconds REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS telemetry_gates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    ts_iso TEXT NOT NULL,
    task_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('failure', 'retry'))
);
CREATE TABLE IF NOT EXISTS telemetry_idle (
    team_id TEXT PRIMARY KEY,
    idle_count INTEGER NOT NULL,
    ts REAL NOT NULL,
    ts_iso TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS telemetry_review_wait (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    item TEXT,
    enqueued_at REAL NOT NULL,
    started_at REAL
);
CREATE INDEX IF NOT EXISTS idx_telemetry_review_wait_open
    ON telemetry_review_wait(task_id, started_at);
"""
# telemetry_review_wait replaces the phase-1 round-0 table ``telemetry_dwell``
# (one row per task), which could not represent several pending submissions.


def estimate_tokens(text: str) -> int:
    """Rough token estimate used across the factory: ``ceil(len(text) / 4)``."""
    if not text:
        return 0
    return math.ceil(len(text) / 4)


def _iso(ts: float) -> str:
    """Epoch seconds -> ISO-8601 UTC string."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _check_choice(value: str, allowed: tuple[str, ...], what: str) -> None:
    if value not in allowed:
        raise ValueError(f"unknown {what} {value!r}; expected one of {allowed}")


def _check_count(value: int | float, what: str) -> None:
    if value < 0:
        raise ValueError(f"{what} must be >= 0, got {value}")


class Telemetry:
    """Collects factory metrics in the queue's SQLite database.

    Args:
        queue: a :class:`FoundryQueue` (anything exposing ``conn``, a
            ``sqlite3.Connection``, and optionally ``lock``).  A bare
            ``sqlite3.Connection`` is accepted too, for tests and tools.
        clock: returns the current time in epoch seconds (default
            ``time.time``); inject a fake for deterministic windows.
    """

    def __init__(
        self,
        queue: "FoundryQueue | sqlite3.Connection",
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.queue = queue
        if isinstance(queue, sqlite3.Connection):
            self.conn: sqlite3.Connection = queue
            self._lock: Any = threading.RLock()
        else:
            self.conn = queue.conn
            self._lock = getattr(queue, "lock", None) or getattr(queue, "_lock", None) or threading.RLock()
        self.clock = clock
        with self._tx() as cur:
            # One statement at a time: ``executescript`` would COMMIT a
            # transaction the caller already has open.
            for stmt in _SCHEMA.split(";"):
                if stmt.strip():
                    cur.execute(stmt)
            cols = {row[1] for row in cur.execute("PRAGMA table_info(telemetry_tokens)")}
            if "cache_read_tokens" not in cols:  # databases created before D11
                cur.execute(
                    "ALTER TABLE telemetry_tokens ADD COLUMN cache_read_tokens INTEGER NOT NULL DEFAULT 0"
                )

    # ------------------------------------------------------------------ infra
    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Cursor]:
        """Run statements under the shared lock and commit.

        If the connection is already inside a transaction on entry (e.g. the
        caller holds ``queue.transaction()``), the statements join it and the
        owner of that transaction decides whether to commit or roll back.
        """
        with self._lock:
            owns = not self.conn.in_transaction
            cur = self.conn.cursor()
            try:
                yield cur
                if owns:
                    self.conn.commit()
            except BaseException:
                if owns and self.conn.in_transaction:
                    self.conn.rollback()
                raise
            finally:
                cur.close()

    def _query(self, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        with self._lock:
            return list(self.conn.execute(sql, params).fetchall())

    def _now(self) -> float:
        return float(self.clock())

    # --------------------------------------------------------------- recorders
    def record_tokens(
        self,
        task_id: str,
        agent_id: str,
        role: Role,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int = 0,
    ) -> None:
        """Record one Claude call's token usage (also counts as one request for RPM).

        ``input_tokens`` are the uncached input tokens (including cache
        creation); ``cache_read_tokens`` are stored separately and are NOT part
        of burn rates or TPM, matching what the rate limiter is charged (D11).
        """
        _check_choice(role, ROLES, "role")
        _check_count(input_tokens, "input_tokens")
        _check_count(output_tokens, "output_tokens")
        _check_count(cache_read_tokens, "cache_read_tokens")
        now = self._now()
        with self._tx() as cur:
            cur.execute(
                "INSERT INTO telemetry_tokens(ts, ts_iso, task_id, agent_id, role, input_tokens, output_tokens,"
                " cache_read_tokens) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (now, _iso(now), task_id, agent_id, role, int(input_tokens), int(output_tokens),
                 int(cache_read_tokens)),
            )

    def record_tokens_saved(self, task_id: str, source: SavedSource, tokens: int) -> None:
        """Record Claude tokens avoided thanks to the Foreman (pruning, cache, release_draft, triage)."""
        _check_choice(source, SAVED_SOURCES, "source")
        _check_count(tokens, "tokens")
        now = self._now()
        with self._tx() as cur:
            cur.execute(
                "INSERT INTO telemetry_saved(ts, ts_iso, task_id, source, tokens) VALUES (?, ?, ?, ?, ?)",
                (now, _iso(now), task_id, source, int(tokens)),
            )

    def record_duration(self, task_id: str, phase: Phase, seconds: float) -> None:
        """Record how long one build or review took."""
        _check_choice(phase, PHASES, "phase")
        _check_count(seconds, "seconds")
        now = self._now()
        with self._tx() as cur:
            cur.execute(
                "INSERT INTO telemetry_durations(ts, ts_iso, task_id, phase, seconds) VALUES (?, ?, ?, ?, ?)",
                (now, _iso(now), task_id, phase, float(seconds)),
            )

    def _record_gate(self, task_id: str, kind: str) -> None:
        now = self._now()
        with self._tx() as cur:
            cur.execute(
                "INSERT INTO telemetry_gates(ts, ts_iso, task_id, kind) VALUES (?, ?, ?, ?)",
                (now, _iso(now), task_id, kind),
            )

    def record_gate_failure(self, task_id: str) -> None:
        """Record one deterministic gate failure on a task."""
        self._record_gate(task_id, "failure")

    def record_retry(self, task_id: str) -> None:
        """Record one retry (ticket reopened with a fix hint or REVISE directives)."""
        self._record_gate(task_id, "retry")

    def set_idle(self, team_id: str, count: int) -> None:
        """Set the current number of idle agents on a team (latest value wins)."""
        _check_count(count, "count")
        now = self._now()
        with self._tx() as cur:
            cur.execute(
                "INSERT INTO telemetry_idle(team_id, idle_count, ts, ts_iso) VALUES (?, ?, ?, ?)"
                " ON CONFLICT(team_id) DO UPDATE SET idle_count=excluded.idle_count,"
                " ts=excluded.ts, ts_iso=excluded.ts_iso",
                (team_id, int(count), now, _iso(now)),
            )

    def mark_review_wait(self, task_id: str, item: str | None = None) -> None:
        """Mark that a submission passed its gates and now waits for a reviewer.

        Dwell is tracked per submission (D7). ``item`` identifies it (the
        ticket id); with ``item=None`` every call adds a new anonymous
        submission. Marking an item that is already waiting keeps its original
        enqueue time, so re-enqueueing never hides how long it has waited.
        After it was picked up, marking it again (a REVISE round) starts a new wait.
        """
        now = self._now()
        with self._tx() as cur:
            if item is not None:
                row = cur.execute(
                    "SELECT 1 FROM telemetry_review_wait WHERE task_id=? AND item=? AND started_at IS NULL",
                    (task_id, item),
                ).fetchone()
                if row is not None:
                    return
            cur.execute(
                "INSERT INTO telemetry_review_wait(task_id, item, enqueued_at, started_at) VALUES (?, ?, ?, NULL)",
                (task_id, item, now),
            )

    def mark_review_start(self, task_id: str, item: str | None = None) -> None:
        """Mark that a reviewer picked a submission up, freezing its dwell time.

        With ``item`` the oldest waiting row of that item is started; with
        ``item=None`` the task's oldest waiting submission (FIFO). No-op when
        nothing matching is waiting.
        """
        now = self._now()
        with self._tx() as cur:
            if item is None:
                row = cur.execute(
                    "SELECT id FROM telemetry_review_wait WHERE task_id=? AND started_at IS NULL"
                    " ORDER BY enqueued_at, id LIMIT 1",
                    (task_id,),
                ).fetchone()
            else:
                row = cur.execute(
                    "SELECT id FROM telemetry_review_wait WHERE task_id=? AND item=? AND started_at IS NULL"
                    " ORDER BY enqueued_at, id LIMIT 1",
                    (task_id, item),
                ).fetchone()
            if row is not None:
                cur.execute("UPDATE telemetry_review_wait SET started_at=? WHERE id=?", (now, row[0]))

    # ----------------------------------------------------------------- readers
    def queue_dwell(self, task_id: str) -> float:
        """Review dwell of a task, in seconds.

        If any submission is still waiting: ``now - enqueued`` of the OLDEST
        waiting one. Otherwise the frozen dwell (``started - enqueued``) of the
        most recently started submission. 0.0 if it never waited for review.
        """
        rows = self._query(
            "SELECT MIN(enqueued_at) FROM telemetry_review_wait WHERE task_id=? AND started_at IS NULL",
            (task_id,),
        )
        if rows and rows[0][0] is not None:
            return max(0.0, self._now() - float(rows[0][0]))
        rows = self._query(
            "SELECT enqueued_at, started_at FROM telemetry_review_wait WHERE task_id=? AND started_at IS NOT NULL"
            " ORDER BY started_at DESC, id DESC LIMIT 1",
            (task_id,),
        )
        if not rows:
            return 0.0
        enqueued, started = rows[0]
        return max(0.0, float(started) - float(enqueued))

    def waiting_for_review(self) -> dict[str, float]:
        """``{task_id: dwell of its oldest waiting submission}`` for tasks with any waiting."""
        now = self._now()
        rows = self._query(
            "SELECT task_id, MIN(enqueued_at) FROM telemetry_review_wait WHERE started_at IS NULL"
            " GROUP BY task_id ORDER BY task_id"
        )
        return {task_id: max(0.0, now - float(enq)) for task_id, enq in rows}

    def waiting_submissions(self) -> list[dict[str, Any]]:
        """Every submission still waiting for review, oldest first."""
        now = self._now()
        rows = self._query(
            "SELECT task_id, item, enqueued_at FROM telemetry_review_wait WHERE started_at IS NULL"
            " ORDER BY enqueued_at, id"
        )
        return [
            {"task_id": t, "item": i, "dwell_seconds": max(0.0, now - float(e))} for t, i, e in rows
        ]

    def gate_failures(self, task_id: str | None = None) -> int:
        """Gate failure count for one task (or all tasks)."""
        return self._count_gates("failure", task_id)

    def retries(self, task_id: str | None = None) -> int:
        """Retry count for one task (or all tasks)."""
        return self._count_gates("retry", task_id)

    def _count_gates(self, kind: str, task_id: str | None) -> int:
        if task_id is None:
            rows = self._query("SELECT COUNT(*) FROM telemetry_gates WHERE kind=?", (kind,))
        else:
            rows = self._query("SELECT COUNT(*) FROM telemetry_gates WHERE kind=? AND task_id=?", (kind, task_id))
        return int(rows[0][0])

    def idle_counts(self) -> dict[str, int]:
        """Latest idle agent count per team."""
        return {team: int(n) for team, n in self._query("SELECT team_id, idle_count FROM telemetry_idle ORDER BY team_id")}

    def burn_rate(self, window_minutes: float = 1) -> dict[str, float]:
        """Moving burn rate over the last ``window_minutes``.

        Returns ``{input_per_min, output_per_min, tokens_per_min, rpm}`` where
        ``rpm`` counts recorded model calls (one ``record_tokens`` = one request).
        The window is ``(now - window, now]``.
        """
        if window_minutes <= 0:
            raise ValueError("window_minutes must be > 0")
        now = self._now()
        start = now - window_minutes * 60.0
        rows = self._query(
            "SELECT COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0), COUNT(*)"
            " FROM telemetry_tokens WHERE ts > ? AND ts <= ?",
            (start, now),
        )
        inp, out, calls = rows[0]
        return {
            "input_per_min": float(inp) / window_minutes,
            "output_per_min": float(out) / window_minutes,
            "tokens_per_min": float(inp + out) / window_minutes,
            "rpm": float(calls) / window_minutes,
        }

    def current_tpm(self) -> float:
        """Total tokens per minute over the last minute (for TOKEN_VELOCITY_SPIKE)."""
        return self.burn_rate(1)["tokens_per_min"]

    def tokens_by_role(self) -> dict[str, dict[str, int]]:
        """``{role: {input, output, total, calls}}`` over all time."""
        rows = self._query(
            "SELECT role, SUM(input_tokens), SUM(output_tokens), COUNT(*) FROM telemetry_tokens GROUP BY role ORDER BY role"
        )
        return {r: {"input": int(i), "output": int(o), "total": int(i + o), "calls": int(c)} for r, i, o, c in rows}

    def tokens_by_task(self) -> dict[str, dict[str, int]]:
        """``{task_id: {input, output, total, calls}}`` over all time."""
        rows = self._query(
            "SELECT task_id, SUM(input_tokens), SUM(output_tokens), COUNT(*) FROM telemetry_tokens"
            " GROUP BY task_id ORDER BY task_id"
        )
        return {t: {"input": int(i), "output": int(o), "total": int(i + o), "calls": int(c)} for t, i, o, c in rows}

    def tokens_by_agent(self) -> dict[str, dict[str, int]]:
        """``{agent_id: {input, output, total, calls}}`` over all time."""
        rows = self._query(
            "SELECT agent_id, SUM(input_tokens), SUM(output_tokens), COUNT(*) FROM telemetry_tokens"
            " GROUP BY agent_id ORDER BY agent_id"
        )
        return {a: {"input": int(i), "output": int(o), "total": int(i + o), "calls": int(c)} for a, i, o, c in rows}

    def tokens_saved_by_source(self) -> dict[str, int]:
        """``{source: tokens}`` for every source (0 when none recorded)."""
        saved = {s: 0 for s in SAVED_SOURCES}
        for source, total in self._query("SELECT source, SUM(tokens) FROM telemetry_saved GROUP BY source"):
            saved[source] = int(total)
        return saved

    def durations(self) -> dict[str, dict[str, float]]:
        """``{phase: {count, total_seconds, avg_seconds, max_seconds}}`` for build and review."""
        out = {p: {"count": 0, "total_seconds": 0.0, "avg_seconds": 0.0, "max_seconds": 0.0} for p in PHASES}
        rows = self._query(
            "SELECT phase, COUNT(*), SUM(seconds), MAX(seconds) FROM telemetry_durations GROUP BY phase"
        )
        for phase, count, total, mx in rows:
            out[phase] = {
                "count": int(count),
                "total_seconds": float(total),
                "avg_seconds": float(total) / int(count) if count else 0.0,
                "max_seconds": float(mx),
            }
        return out

    def snapshot(self) -> dict[str, Any]:
        """Everything at once, JSON-able, for the Shop Foreman and the board bridge.

        Keys: ``generated_at``, ``tokens`` (totals, by_role, by_task, by_agent),
        ``tokens_saved_by_gemma`` (by_source, total), ``durations``,
        ``idle_agent_count`` (per team, total), ``gates`` (failures/retries in
        total and per task), ``queue_dwell`` (waiting tasks, max), ``burn_rate``
        (``1m`` and ``15m``), ``current_tpm`` and ``current_rpm``.
        """
        now = self._now()
        by_role = self.tokens_by_role()
        total_in = sum(v["input"] for v in by_role.values())
        total_out = sum(v["output"] for v in by_role.values())
        saved = self.tokens_saved_by_source()
        idle = self.idle_counts()
        failures = {t: int(n) for t, n in self._query(
            "SELECT task_id, COUNT(*) FROM telemetry_gates WHERE kind='failure' GROUP BY task_id ORDER BY task_id"
        )}
        retries = {t: int(n) for t, n in self._query(
            "SELECT task_id, COUNT(*) FROM telemetry_gates WHERE kind='retry' GROUP BY task_id ORDER BY task_id"
        )}
        waiting = self.waiting_for_review()
        burn_1 = self.burn_rate(1)
        burn_15 = self.burn_rate(15)
        return {
            "generated_at": _iso(now),
            "tokens": {
                "input": total_in,
                "output": total_out,
                "total": total_in + total_out,
                "by_role": by_role,
                "by_task": self.tokens_by_task(),
                "by_agent": self.tokens_by_agent(),
            },
            "tokens_saved_by_gemma": {"by_source": saved, "total": sum(saved.values())},
            "durations": self.durations(),
            "idle_agent_count": {"by_team": idle, "total": sum(idle.values())},
            "gates": {
                "failures": sum(failures.values()),
                "retries": sum(retries.values()),
                "failures_by_task": failures,
                "retries_by_task": retries,
            },
            "queue_dwell": {
                "waiting": waiting,
                "max_seconds": max(waiting.values(), default=0.0),
            },
            "burn_rate": {"1m": burn_1, "15m": burn_15},
            "current_tpm": burn_1["tokens_per_min"],
            "current_rpm": burn_1["rpm"],
        }
