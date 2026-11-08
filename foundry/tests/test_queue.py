"""Unit tests for foundry.orchestrator_queue.FoundryQueue (SQLite WAL queue)."""

from __future__ import annotations

import asyncio
import os
import sqlite3
import threading
from collections import Counter
from pathlib import Path
from typing import Any, Iterator

import pytest

from foundry.orchestrator_queue import FoundryQueue
from foundry.schemas import BUILDERS_FOR, MAX_RETRIES, TaskSizingDirective, TaskState

# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

TABLES = {"tasks", "tickets", "events", "gate_results", "verdicts", "escalations"}


def f(obj: Any, name: str) -> Any:
    """Read a field from a Ticket / task record (attribute or mapping)."""
    if isinstance(obj, dict):
        return obj[name]
    try:
        return obj[name]  # sqlite3.Row
    except (TypeError, KeyError, IndexError):
        return getattr(obj, name)


def state_of(q: FoundryQueue, task_id: str) -> str:
    st = f(q.get_task(task_id), "state")
    return st.value if isinstance(st, TaskState) else str(st)


def directive(
    task_id: str = "T-1",
    team_id: str = "alpha",
    complexity: str = "M",
    n: int | None = None,
) -> TaskSizingDirective:
    n = BUILDERS_FOR[complexity] if n is None else n
    return TaskSizingDirective(
        task_id=task_id,
        team_id=team_id,
        complexity=complexity,  # type: ignore[arg-type]
        allocated_builders=n,
        rationale="test",
        subtasks=[
            {
                "subtask_id": f"{task_id}.{i}",
                "title": f"part {i}",
                "acceptance_criteria": [f"c{i}"],
                "allowed_files": [f"f{i}.py"],
                "independent": True,  # slot mechanics: the slices may run side by side
            }
            for i in range(1, n + 1)
        ],
        override_reason=None if n == BUILDERS_FOR[complexity] else "test override",
    )


def raw(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


def tickets_of(path: Path, task_id: str) -> list[sqlite3.Row]:
    with raw(path) as c:
        return list(c.execute("SELECT * FROM tickets WHERE task_id=? ORDER BY slot", (task_id,)))


def close(q: FoundryQueue) -> None:
    if hasattr(q, "close"):
        q.close()
    else:  # pragma: no cover - fallback
        q.conn.close()


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    return tmp_path / "foundry.db"


@pytest.fixture()
def q(db: Path) -> Iterator[FoundryQueue]:
    queue = FoundryQueue(db)
    yield queue
    close(queue)


def sized(q: FoundryQueue, task_id: str = "T-1", team: str = "alpha", complexity: str = "M",
          n: int | None = None) -> TaskSizingDirective:
    q.add_task(task_id, f"title {task_id}", "desc", team)
    d = directive(task_id, team, complexity, n)
    q.apply_sizing(d)
    return d


# --------------------------------------------------------------------------
# Database setup
# --------------------------------------------------------------------------


def test_wal_mode_enabled(q: FoundryQueue, db: Path) -> None:
    with raw(db) as c:
        mode = c.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"
    # The queue's own connection agrees.
    assert q.conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


def test_busy_timeout_set(q: FoundryQueue) -> None:
    assert q.conn.execute("PRAGMA busy_timeout").fetchone()[0] > 0


def test_tables_created(q: FoundryQueue, db: Path) -> None:
    with raw(db) as c:
        names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert TABLES <= names


def test_schema_idempotent(db: Path) -> None:
    q1 = FoundryQueue(db)
    q1.add_task("T-1", "t", "d", "alpha")
    close(q1)
    q2 = FoundryQueue(db)  # re-running CREATE must not fail or wipe data
    q3 = FoundryQueue(db)  # concurrently open as well
    try:
        assert state_of(q2, "T-1") == "backlog"
        assert state_of(q3, "T-1") == "backlog"
    finally:
        close(q2)
        close(q3)


def test_accepts_str_path(db: Path) -> None:
    q = FoundryQueue(str(db))
    try:
        q.add_task("T-1", "t", "d", "alpha")
        assert state_of(q, "T-1") == "backlog"
    finally:
        close(q)


# --------------------------------------------------------------------------
# Tasks and transitions
# --------------------------------------------------------------------------


def test_add_task_backlog(q: FoundryQueue) -> None:
    q.add_task("T-1", "Title", "Description", "bravo")
    task = q.get_task("T-1")
    assert f(task, "task_id") == "T-1"
    assert f(task, "team_id") == "bravo"
    assert f(task, "title") == "Title"
    assert state_of(q, "T-1") == "backlog"


def test_get_task_unknown(q: FoundryQueue) -> None:
    try:
        result = q.get_task("NOPE")
    except (KeyError, ValueError, LookupError):
        return
    assert result is None


HAPPY = ["sizing", "building", "validating", "reviewing", "complete"]


def test_happy_path_transitions(q: FoundryQueue) -> None:
    q.add_task("T-1", "t", "d", "alpha")
    for st in HAPPY:
        q.set_state("T-1", st, note=f"to {st}")
        assert state_of(q, "T-1") == st


def test_set_state_accepts_enum(q: FoundryQueue) -> None:
    q.add_task("T-1", "t", "d", "alpha")
    q.set_state("T-1", TaskState.SIZING)
    assert state_of(q, "T-1") == "sizing"


def _walk(q: FoundryQueue, task_id: str, to: str) -> None:
    """Drive a fresh backlog task to state ``to`` via legal moves."""
    q.add_task(task_id, "t", "d", "alpha")
    if to == "backlog":
        return
    if to in ("quarantined", "failed"):
        q.set_state(task_id, to)
        return
    for st in HAPPY:
        q.set_state(task_id, st)
        if st == to:
            return


LEGAL = [
    ("backlog", "sizing"),
    ("sizing", "building"),
    ("building", "validating"),
    ("validating", "reviewing"),
    ("reviewing", "complete"),
    ("validating", "building"),  # gate retry
    ("reviewing", "building"),  # REVISE
] + [
    (src, dst)
    for src in ("backlog", "sizing", "building", "validating", "reviewing")
    for dst in ("quarantined", "failed")
]


@pytest.mark.parametrize(("src", "dst"), LEGAL)
def test_legal_transition(q: FoundryQueue, src: str, dst: str) -> None:
    _walk(q, "T-1", src)
    assert state_of(q, "T-1") == src
    q.set_state("T-1", dst)
    assert state_of(q, "T-1") == dst


ILLEGAL = [
    ("backlog", "building"),
    ("backlog", "validating"),
    ("backlog", "reviewing"),
    ("backlog", "complete"),
    ("sizing", "validating"),
    ("sizing", "reviewing"),
    ("sizing", "complete"),
    ("sizing", "backlog"),
    ("building", "reviewing"),
    ("building", "complete"),
    ("building", "sizing"),
    ("validating", "complete"),
    ("validating", "sizing"),
    ("reviewing", "validating"),
    ("complete", "building"),
    ("complete", "reviewing"),
    ("complete", "backlog"),
    ("quarantined", "building"),
    ("quarantined", "complete"),
    ("failed", "building"),
    ("failed", "complete"),
]


@pytest.mark.parametrize(("src", "dst"), ILLEGAL)
def test_illegal_transition(q: FoundryQueue, src: str, dst: str) -> None:
    _walk(q, "T-1", src)
    with pytest.raises(ValueError):
        q.set_state("T-1", dst)
    assert state_of(q, "T-1") == src  # unchanged


def test_unknown_state_rejected(q: FoundryQueue) -> None:
    q.add_task("T-1", "t", "d", "alpha")
    with pytest.raises(ValueError):
        q.set_state("T-1", "shipping")


def test_tasks_in_state(q: FoundryQueue) -> None:
    q.add_task("T-1", "t", "d", "alpha")
    q.add_task("T-2", "t", "d", "alpha")
    q.add_task("T-3", "t", "d", "bravo")
    q.set_state("T-2", "sizing")
    backlog = q.tasks_in_state("backlog")
    ids = {f(t, "task_id") if not isinstance(t, str) else t for t in backlog}
    assert ids == {"T-1", "T-3"}
    sizing = q.tasks_in_state(TaskState.SIZING)
    assert {f(t, "task_id") if not isinstance(t, str) else t for t in sizing} == {"T-2"}


def test_transitions_recorded_as_events(q: FoundryQueue, db: Path) -> None:
    q.add_task("T-1", "t", "d", "alpha")
    q.set_state("T-1", "sizing", note="sizing now")
    q.record_event("custom", "T-1", {"k": 1})
    with raw(db) as c:
        rows = list(c.execute("SELECT * FROM events WHERE task_id='T-1'"))
    assert len(rows) >= 2
    kinds = {r["kind"] for r in rows}
    assert "custom" in kinds


def test_quarantine(q: FoundryQueue) -> None:
    sized(q)
    q.quarantine("T-1", "too many failures")
    assert state_of(q, "T-1") == "quarantined"


# --------------------------------------------------------------------------
# Sizing and tickets
# --------------------------------------------------------------------------


@pytest.mark.parametrize("complexity", ["S", "M", "L", "XL"])
def test_apply_sizing_creates_tickets(q: FoundryQueue, db: Path, complexity: str) -> None:
    d = sized(q, complexity=complexity)
    rows = tickets_of(db, "T-1")
    n = BUILDERS_FOR[complexity]
    assert len(rows) == n
    assert [r["slot"] for r in rows] == list(range(1, n + 1))
    assert {r["subtask_id"] for r in rows} == {s.subtask_id for s in d.subtasks}
    assert all(r["status"] == "open" for r in rows)
    assert all(r["attempt"] == 0 for r in rows)
    assert all(r["team_id"] == "alpha" for r in rows)
    assert state_of(q, "T-1") == "building"


def test_apply_sizing_from_sizing_state(q: FoundryQueue, db: Path) -> None:
    q.add_task("T-1", "t", "d", "alpha")
    q.set_state("T-1", "sizing")
    q.apply_sizing(directive())
    assert state_of(q, "T-1") == "building"
    assert len(tickets_of(db, "T-1")) == 2


def test_claim_returns_ticket_for_slot(q: FoundryQueue) -> None:
    sized(q, complexity="M")
    t1 = q.claim_ticket("alpha", 1)
    t2 = q.claim_ticket("alpha", 2)
    assert t1 is not None and t2 is not None
    assert f(t1, "slot") == 1 and f(t2, "slot") == 2
    assert f(t1, "task_id") == "T-1"
    assert f(t1, "team_id") == "alpha"
    assert f(t1, "subtask_id") == "T-1.1"
    assert f(t2, "subtask_id") == "T-1.2"
    assert f(t1, "status") == "claimed"
    assert f(t1, "ticket_id") != f(t2, "ticket_id")


@pytest.mark.parametrize("complexity", ["S", "M", "L"])
def test_slots_above_allocation_get_none(q: FoundryQueue, complexity: str) -> None:
    sized(q, complexity=complexity)
    n = BUILDERS_FOR[complexity]
    for slot in range(n + 1, 6):
        assert q.claim_ticket("alpha", slot) is None
    for slot in range(1, n + 1):
        assert q.claim_ticket("alpha", slot) is not None


def test_claim_twice_same_slot_none(q: FoundryQueue) -> None:
    sized(q, complexity="S")
    assert q.claim_ticket("alpha", 1) is not None
    assert q.claim_ticket("alpha", 1) is None


def test_claim_other_team_none(q: FoundryQueue) -> None:
    sized(q, team="alpha", complexity="L")
    for slot in range(1, 6):
        assert q.claim_ticket("bravo", slot) is None
        assert q.claim_ticket("charlie", slot) is None


def test_claim_empty_queue_none(q: FoundryQueue) -> None:
    assert q.claim_ticket("alpha", 1) is None


def test_claim_across_tasks_same_team(q: FoundryQueue) -> None:
    sized(q, "T-1", complexity="S")
    sized(q, "T-2", complexity="S")
    a = q.claim_ticket("alpha", 1)
    b = q.claim_ticket("alpha", 1)
    assert a is not None and b is not None
    assert {f(a, "task_id"), f(b, "task_id")} == {"T-1", "T-2"}
    assert q.claim_ticket("alpha", 1) is None
    assert q.claim_ticket("alpha", 2) is None


def test_claim_marks_db_claimed(q: FoundryQueue, db: Path) -> None:
    sized(q, complexity="S")
    t = q.claim_ticket("alpha", 1)
    assert t is not None
    rows = tickets_of(db, "T-1")
    assert rows[0]["status"] == "claimed"


def test_concurrent_claims_never_double_claim(db: Path) -> None:
    """Many threads, several FoundryQueue connections, five slots: each ticket once."""
    setup = FoundryQueue(db)
    tasks = [f"T-{i}" for i in range(8)]
    for tid in tasks:
        sized(setup, tid, complexity="L")  # 8 tasks x 5 tickets = 40
    close(setup)

    queues = [FoundryQueue(db) for _ in range(3)]
    claimed: list[str] = []
    lock = threading.Lock()
    errors: list[BaseException] = []
    barrier = threading.Barrier(15)

    def worker(qi: int, slot: int) -> None:
        try:
            barrier.wait()
            qq = queues[qi]
            while True:
                t = qq.claim_ticket("alpha", slot)
                if t is None:
                    return
                assert f(t, "slot") == slot
                with lock:
                    claimed.append(str(f(t, "ticket_id")))
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=(qi, slot))
        for qi in range(3)
        for slot in range(1, 6)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    for qq in queues:
        close(qq)
    assert not errors, errors
    counts = Counter(claimed)
    assert len(claimed) == 40
    assert all(c == 1 for c in counts.values()), counts.most_common(3)


def test_concurrent_claims_single_connection(q: FoundryQueue) -> None:
    for i in range(4):
        sized(q, f"T-{i}", complexity="L")
    claimed: list[str] = []
    lock = threading.Lock()

    def worker(slot: int) -> None:
        while (t := q.claim_ticket("alpha", slot)) is not None:
            with lock:
                claimed.append(str(f(t, "ticket_id")))

    threads = [threading.Thread(target=worker, args=(s,)) for s in range(1, 6) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert len(claimed) == 20
    assert len(set(claimed)) == 20


# --------------------------------------------------------------------------
# wait_for_ticket
# --------------------------------------------------------------------------


def test_wait_for_ticket_returns_when_ticket_appears(q: FoundryQueue) -> None:
    async def scenario() -> Any:
        stop = asyncio.Event()

        async def producer() -> None:
            await asyncio.sleep(0.15)
            sized(q, complexity="S")

        prod = asyncio.create_task(producer())
        t = await asyncio.wait_for(q.wait_for_ticket("alpha", 1, poll=0.01, stop=stop), 5)
        await prod
        return t

    t = asyncio.run(scenario())
    assert t is not None
    assert f(t, "slot") == 1
    assert f(t, "task_id") == "T-1"


def test_wait_for_ticket_stops_on_event(q: FoundryQueue) -> None:
    async def scenario() -> Any:
        stop = asyncio.Event()
        asyncio.get_running_loop().call_later(0.1, stop.set)
        return await asyncio.wait_for(q.wait_for_ticket("alpha", 1, poll=0.01, stop=stop), 5)

    assert asyncio.run(scenario()) is None


def test_wait_for_ticket_idle_slot_never_gets_ticket(q: FoundryQueue) -> None:
    sized(q, complexity="S")

    async def scenario() -> Any:
        stop = asyncio.Event()
        asyncio.get_running_loop().call_later(0.2, stop.set)
        return await asyncio.wait_for(q.wait_for_ticket("alpha", 3, poll=0.01, stop=stop), 5)

    assert asyncio.run(scenario()) is None
    # The slot-1 ticket is still there for its owner.
    assert q.claim_ticket("alpha", 1) is not None


def test_wait_for_ticket_does_not_block_loop(q: FoundryQueue) -> None:
    async def scenario() -> int:
        stop = asyncio.Event()
        ticks = 0

        async def ticker() -> None:
            nonlocal ticks
            for _ in range(10):
                await asyncio.sleep(0.01)
                ticks += 1
            stop.set()

        await asyncio.gather(ticker(), q.wait_for_ticket("alpha", 1, poll=0.02, stop=stop))
        return ticks

    assert asyncio.run(scenario()) == 10


# --------------------------------------------------------------------------
# complete / reopen / retry cap
# --------------------------------------------------------------------------


def test_complete_ticket(q: FoundryQueue, db: Path) -> None:
    sized(q, complexity="S")
    t = q.claim_ticket("alpha", 1)
    q.complete_ticket(f(t, "ticket_id"))
    assert tickets_of(db, "T-1")[0]["status"] == "done"
    assert q.claim_ticket("alpha", 1) is None


def test_reopen_increments_attempt_and_reopens(q: FoundryQueue, db: Path) -> None:
    sized(q, complexity="S")
    t = q.claim_ticket("alpha", 1)
    assert q.reopen_ticket(f(t, "ticket_id"), "mod.py:3 NameError: x") is True
    row = tickets_of(db, "T-1")[0]
    assert row["status"] == "open"
    assert row["attempt"] == 1
    t2 = q.claim_ticket("alpha", 1)
    assert t2 is not None
    assert f(t2, "ticket_id") == f(t, "ticket_id")
    assert f(t2, "attempt") == 1


def test_reopen_stores_fix_hint(q: FoundryQueue, db: Path) -> None:
    sized(q, complexity="S")
    t = q.claim_ticket("alpha", 1)
    q.reopen_ticket(f(t, "ticket_id"), "mod.py:3 NameError: x")
    t2 = q.claim_ticket("alpha", 1)
    hint = None
    try:
        hint = f(t2, "fix_hint")
    except (AttributeError, KeyError):
        row = tickets_of(db, "T-1")[0]
        hint = row["fix_hint"] if "fix_hint" in row.keys() else None
    assert hint == "mod.py:3 NameError: x"


def test_retry_cap_quarantines(q: FoundryQueue, db: Path) -> None:
    sized(q, complexity="S")
    tid = None
    for expected_attempt in range(MAX_RETRIES):
        t = q.claim_ticket("alpha", 1)
        assert t is not None
        assert f(t, "attempt") == expected_attempt
        tid = f(t, "ticket_id")
        assert q.reopen_ticket(tid, f"hint {expected_attempt}") is True
        assert state_of(q, "T-1") != "quarantined"
    # attempt is now 2; third failure would make it 3 > 2.
    t = q.claim_ticket("alpha", 1)
    assert t is not None and f(t, "attempt") == MAX_RETRIES
    assert q.reopen_ticket(f(t, "ticket_id"), "hint final") is False
    assert state_of(q, "T-1") == "quarantined"
    # No fourth build: nothing claimable any more.
    assert q.claim_ticket("alpha", 1) is None
    assert tickets_of(db, "T-1")[0]["attempt"] <= MAX_RETRIES


def test_quarantined_task_tickets_not_claimable(q: FoundryQueue) -> None:
    sized(q, complexity="M")
    q.quarantine("T-1", "manual")
    assert q.claim_ticket("alpha", 1) is None
    assert q.claim_ticket("alpha", 2) is None


# --------------------------------------------------------------------------
# resize
# --------------------------------------------------------------------------


def _drain(q: FoundryQueue, team: str = "alpha") -> dict[int, list[Any]]:
    out: dict[int, list[Any]] = {}
    for slot in range(1, 6):
        while (t := q.claim_ticket(team, slot)) is not None:
            out.setdefault(slot, []).append(t)
    return out


def test_resize_down_keeps_slots_within_allocation(q: FoundryQueue) -> None:
    sized(q, complexity="L")  # 5 tickets
    q.resize("T-1", 2)
    got = _drain(q)
    assert set(got) <= {1, 2}, f"slots above 2 claimed: {sorted(got)}"
    total = sum(len(v) for v in got.values())
    assert total == 5, "resizing down must not drop open work"


def test_resize_down_to_one(q: FoundryQueue) -> None:
    sized(q, complexity="M")
    q.resize("T-1", 1)
    assert q.claim_ticket("alpha", 2) is None
    a = q.claim_ticket("alpha", 1)
    b = q.claim_ticket("alpha", 1)
    assert a is not None and b is not None
    assert {f(a, "subtask_id"), f(b, "subtask_id")} == {"T-1.1", "T-1.2"}


def test_resize_up_allows_higher_slots(q: FoundryQueue) -> None:
    sized(q, complexity="S")
    assert q.claim_ticket("alpha", 2) is None
    q.resize("T-1", 5)
    got = _drain(q)
    assert sum(len(v) for v in got.values()) == 1  # still one unit of work
    assert set(got) <= set(range(1, 6))


def test_resize_up_spreads_existing_work(q: FoundryQueue) -> None:
    sized(q, complexity="L")
    q.resize("T-1", 1)
    q.resize("T-1", 5)
    got = _drain(q)
    assert sum(len(v) for v in got.values()) == 5
    assert set(got) <= set(range(1, 6))


@pytest.mark.parametrize("n", [0, 6])
def test_resize_out_of_range(q: FoundryQueue, n: int) -> None:
    sized(q, complexity="M")
    with pytest.raises(ValueError):
        q.resize("T-1", n)


# --------------------------------------------------------------------------
# crash resume
# --------------------------------------------------------------------------


def test_resume_after_crash(db: Path) -> None:
    q1 = FoundryQueue(db)
    sized(q1, "T-1", complexity="L")
    claimed = [q1.claim_ticket("alpha", s) for s in (1, 2, 3)]
    assert all(c is not None for c in claimed)
    q1.complete_ticket(f(claimed[0], "ticket_id"))
    close(q1)  # simulated crash: slots 2 and 3 never finished

    q2 = FoundryQueue(db)
    try:
        # Before resume, the crashed claims are still held.
        assert q2.claim_ticket("alpha", 2) is None
        reopened = q2.resume()
        if isinstance(reopened, int):
            assert reopened == 2
        elif reopened is not None:
            assert len(reopened) == 2
        rows = {r["slot"]: r for r in tickets_of(db, "T-1")}
        assert rows[1]["status"] == "done"
        assert rows[2]["status"] == "open"
        assert rows[3]["status"] == "open"
        assert rows[4]["status"] == "open"
        assert rows[5]["status"] == "open"
        assert q2.claim_ticket("alpha", 1) is None  # done stays done
        for s in (2, 3, 4, 5):
            t = q2.claim_ticket("alpha", s)
            assert t is not None and f(t, "slot") == s
        assert state_of(q2, "T-1") == "building"
    finally:
        close(q2)


def test_resume_noop_when_nothing_claimed(q: FoundryQueue, db: Path) -> None:
    sized(q, complexity="M")
    r = q.resume()
    if isinstance(r, int):
        assert r == 0
    elif r is not None:
        assert len(r) == 0
    assert all(row["status"] == "open" for row in tickets_of(db, "T-1"))


def test_data_survives_reopen(db: Path) -> None:
    q1 = FoundryQueue(db)
    sized(q1, "T-1", complexity="M")
    q1.set_state("T-1", "validating")
    close(q1)
    q2 = FoundryQueue(db)
    try:
        assert state_of(q2, "T-1") == "validating"
        assert len(tickets_of(db, "T-1")) == 2
    finally:
        close(q2)


def test_reopen_after_resize_down_moves_into_allocation(q: FoundryQueue) -> None:
    sized(q, complexity="L")
    t5 = q.claim_ticket("alpha", 5)
    assert t5 is not None
    q.resize("T-1", 2)  # claimed ticket keeps slot 5 while its worker runs
    assert q.reopen_ticket(f(t5, "ticket_id"), "retry") is True
    again = q.get_ticket(f(t5, "ticket_id"))
    assert again is not None and f(again, "slot") <= 2
    assert q.claim_ticket("alpha", 5) is None


def test_last_ticket_done_completes_task(q: FoundryQueue) -> None:
    sized(q, complexity="M")
    a = q.claim_ticket("alpha", 1)
    b = q.claim_ticket("alpha", 2)
    assert q.complete_ticket(f(a, "ticket_id")) is False
    assert state_of(q, "T-1") == "building"
    assert q.complete_ticket(f(b, "ticket_id")) is True
    assert state_of(q, "T-1") == "complete"


def test_resume_returns_count_and_keeps_attempt(db: Path) -> None:
    q1 = FoundryQueue(db)
    sized(q1, complexity="S")
    t = q1.claim_ticket("alpha", 1)
    q1.reopen_ticket(f(t, "ticket_id"), "hint")
    q1.claim_ticket("alpha", 1)  # attempt 1, then crash
    close(q1)
    q2 = FoundryQueue(db)
    try:
        assert q2.resume() == 1
        t2 = q2.claim_ticket("alpha", 1)
        assert t2 is not None and f(t2, "attempt") == 1  # crash is not a retry
    finally:
        close(q2)


def test_resume_moves_validating_task_back_to_building(db: Path) -> None:
    q1 = FoundryQueue(db)
    sized(q1, complexity="S")
    q1.claim_ticket("alpha", 1)
    q1.set_state("T-1", "validating")
    close(q1)
    q2 = FoundryQueue(db)
    try:
        q2.resume()
        assert state_of(q2, "T-1") == "building"
    finally:
        close(q2)


# --------------------------------------------------------------------------
# Round 1 additions: release_ticket (D1), in_review (D6), reopen status check,
# idempotent escalations + alert id reservation (D12), kv store (D9)
# --------------------------------------------------------------------------


def test_release_ticket_keeps_attempt_and_reopens(q: FoundryQueue, db: Path) -> None:
    sized(q, complexity="S")
    t = q.claim_ticket("alpha", 1)
    assert q.reopen_ticket(t.ticket_id, "real failure") is True  # attempt 1
    t = q.claim_ticket("alpha", 1)
    q.set_in_review(t.ticket_id, True)
    assert q.release_ticket(t.ticket_id, "usage limit") is True
    row = tickets_of(db, "T-1")[0]
    assert row["status"] == "open"
    assert row["attempt"] == 1  # unchanged by the release
    assert row["fix_hint"] == "real failure"
    assert row["in_review"] == 0
    assert row["claimed_by"] is None
    again = q.claim_ticket("alpha", 1)
    assert again is not None and again.attempt == 1
    kinds = [e["kind"] for e in q.events(task_id="T-1")]
    assert "ticket_released" in kinds


def test_release_many_times_never_quarantines(q: FoundryQueue) -> None:
    sized(q, complexity="S")
    for _ in range(MAX_RETRIES + 3):
        t = q.claim_ticket("alpha", 1)
        assert t is not None and t.attempt == 0
        assert q.release_ticket(t.ticket_id) is True
    assert state_of(q, "T-1") == "building"


def test_release_non_claimed_is_noop(q: FoundryQueue) -> None:
    sized(q, complexity="M")
    t1 = q.claim_ticket("alpha", 1)
    t2 = q.tickets(task_id="T-1", status="open")[0]
    assert q.release_ticket(t2.ticket_id) is False  # open: nothing to release
    q.complete_ticket(t1.ticket_id)
    assert q.release_ticket(t1.ticket_id) is False  # done stays done
    assert q.get_ticket(t1.ticket_id).status == "done"
    with pytest.raises(KeyError):
        q.release_ticket("T-1:nope")


def test_release_after_quarantine_is_noop(q: FoundryQueue) -> None:
    sized(q, complexity="S")
    t = q.claim_ticket("alpha", 1)
    q.quarantine("T-1", "manual")
    assert q.release_ticket(t.ticket_id) is False
    assert q.claim_ticket("alpha", 1) is None


def test_release_moves_ticket_within_allocation(q: FoundryQueue) -> None:
    sized(q, complexity="L")
    t5 = q.claim_ticket("alpha", 5)
    q.resize("T-1", 2)
    assert q.release_ticket(t5.ticket_id) is True
    assert q.get_ticket(t5.ticket_id).slot <= 2
    assert q.claim_ticket("alpha", 5) is None


@pytest.mark.parametrize("status", ["done", "blocked"])
def test_reopen_refuses_done_or_blocked(q: FoundryQueue, status: str) -> None:
    sized(q, complexity="M")
    t = q.claim_ticket("alpha", 1)
    if status == "done":
        q.complete_ticket(t.ticket_id)
    else:
        q.block_ticket(t.ticket_id, "cannot proceed")
    with pytest.raises(ValueError):
        q.reopen_ticket(t.ticket_id, "hint")
    assert q.get_ticket(t.ticket_id).status == status
    assert q.get_ticket(t.ticket_id).attempt == 0


def test_reopen_open_ticket_allowed(q: FoundryQueue) -> None:
    sized(q, complexity="S")
    t = q.tickets(task_id="T-1")[0]
    assert t.status == "open"
    assert q.reopen_ticket(t.ticket_id, "hint") is True
    assert q.get_ticket(t.ticket_id).attempt == 1


def test_in_review_flag_and_filter(q: FoundryQueue) -> None:
    sized(q, complexity="M")
    a = q.claim_ticket("alpha", 1)
    b = q.claim_ticket("alpha", 2)
    assert a.in_review is False
    q.set_in_review(a.ticket_id, True)
    q.set_in_review(a.ticket_id, True)  # idempotent
    assert q.get_ticket(a.ticket_id).in_review is True
    assert [t.ticket_id for t in q.tickets(in_review=True)] == [a.ticket_id]
    assert [t.ticket_id for t in q.tickets(status="claimed", in_review=False)] == [b.ticket_id]
    q.set_in_review(a.ticket_id, False)
    assert q.tickets(in_review=True) == []
    with pytest.raises(KeyError):
        q.set_in_review("T-1:nope", True)


def test_in_review_cleared_by_reopen_complete_and_resume(db: Path) -> None:
    q1 = FoundryQueue(db)
    sized(q1, complexity="L")
    t1, t2, t3 = (q1.claim_ticket("alpha", s) for s in (1, 2, 3))
    for t in (t1, t2, t3):
        q1.set_in_review(t.ticket_id, True)
    q1.reopen_ticket(t1.ticket_id, "REVISE")
    q1.complete_ticket(t2.ticket_id)
    assert q1.get_ticket(t1.ticket_id).in_review is False
    assert q1.get_ticket(t2.ticket_id).in_review is False
    close(q1)
    q2 = FoundryQueue(db)  # crash with t3 waiting for review
    try:
        assert q2.resume() == 1
        t = q2.get_ticket(t3.ticket_id)
        assert t.status == "open" and t.in_review is False
    finally:
        close(q2)


def test_in_review_cleared_by_quarantine(q: FoundryQueue) -> None:
    sized(q, complexity="S")
    t = q.claim_ticket("alpha", 1)
    q.set_in_review(t.ticket_id, True)
    q.quarantine("T-1", "manual")
    assert q.get_ticket(t.ticket_id).in_review is False


def _alert(alert_id: str = "ALT-20261107-001", task_id: str = "T-1") -> dict[str, Any]:
    return {
        "alert_id": alert_id,
        "timestamp": "2026-11-07T10:00:00+00:00",
        "severity": "HIGH",
        "category": "LINE_JAM",
        "target_entity": {"team_id": "alpha", "task_id": task_id},
        "metrics_snapshot": {"queue_depth": 1, "dwell_seconds": 0.0,
                             "gate_failure_count": 2, "current_tpm": 0},
        "diagnosis": "Gate failures repeat.",
        "prescribed_action": "QUARANTINE_TASK",
        "action_parameters": {},
    }


def test_escalation_alert_id_unique_in_db(q: FoundryQueue) -> None:
    q.record_escalation(_alert())
    with pytest.raises(sqlite3.IntegrityError):
        q.conn.execute(
            "INSERT INTO escalations (ts, alert_id, severity, category, prescribed_action,"
            " payload) VALUES ('t', 'ALT-20261107-001', 'LOW', 'LINE_JAM', 'NONE', '{}')")


def test_record_escalation_idempotent(q: FoundryQueue) -> None:
    first = q.record_escalation(_alert(), applied=True, result={"ok": True, "n": 1})
    assert first["created"] is True
    assert first["result"] == {"ok": True, "n": 1}
    assert first["applied_at"] is not None
    again = q.record_escalation(_alert(), applied=True, result={"ok": False})
    assert again["created"] is False
    assert again["result"] == {"ok": True, "n": 1}  # earlier result wins
    assert again["applied_at"] == first["applied_at"]
    assert len(q.escalations()) == 1
    assert len(q.events(kind="escalation")) == 1


def test_record_then_apply_escalation(q: FoundryQueue) -> None:
    stored = q.record_escalation(_alert())
    assert stored["applied_at"] is None and stored["result"] is None
    later = q.record_escalation(_alert(), applied=True, result={"applied": True})
    assert later["created"] is False and later["result"] == {"applied": True}
    assert q.mark_escalation_applied("ALT-20261107-001", {"applied": "twice"})["result"] == {
        "applied": True}
    assert q.get_escalation("ALT-20261107-001")["result"] == {"applied": True}
    assert q.get_escalation("ALT-20261107-999") is None
    with pytest.raises(KeyError):
        q.mark_escalation_applied("ALT-20261107-999")


def test_record_escalation_accepts_model(q: FoundryQueue) -> None:
    from foundry.schemas import ForemanEscalation

    alert = ForemanEscalation.model_validate(_alert("ALT-20261107-007"))
    assert q.record_escalation(alert)["created"] is True
    assert q.has_escalation("ALT-20261107-007")
    assert q.escalations()[0]["category"] == "LINE_JAM"


def test_has_escalation_and_reserve_alert_id(db: Path) -> None:
    q1 = FoundryQueue(db)
    assert q1.has_escalation("ALT-20261107-001") is False
    assert q1.reserve_alert_id("ALT-20261107-001") is True
    assert q1.reserve_alert_id("ALT-20261107-001") is False  # already issued
    assert q1.has_escalation("ALT-20261107-001") is False  # reserved, not recorded
    q1.record_escalation(_alert("ALT-20261107-002"))
    assert q1.reserve_alert_id("ALT-20261107-002") is False  # recorded counts as taken
    close(q1)
    q2 = FoundryQueue(db)  # survives a restart
    try:
        assert q2.reserve_alert_id("ALT-20261107-001") is False
        assert q2.has_escalation("ALT-20261107-002") is True
        assert q2.alert_ids() == ["ALT-20261107-001", "ALT-20261107-002"]
        assert q2.alert_ids("ALT-20261108-") == []
    finally:
        close(q2)


def test_kv_round_trip_and_persistence(db: Path) -> None:
    q1 = FoundryQueue(db)
    assert q1.kv_get("missing") is None
    assert q1.kv_get("missing", {"d": 1}) == {"d": 1}
    q1.kv_set("manager.next_order", 7)
    q1.kv_set("manager.orders", {"F-001": {"task_id": "T-1", "title": "x"}})
    q1.kv_set("manager.orders", {"F-001": {"task_id": "T-1", "title": "y"}})  # overwrite
    q1.kv_set("raw", '{"already": "json"}')  # a JSON string round-trips as a string
    q1.kv_set("other", [1, 2])
    close(q1)
    q2 = FoundryQueue(db)
    try:
        assert q2.kv_get("manager.next_order") == 7
        assert q2.kv_get("manager.orders") == {"F-001": {"task_id": "T-1", "title": "y"}}
        assert q2.kv_get("raw") == '{"already": "json"}'
        assert set(q2.kv_items("manager.")) == {"manager.next_order", "manager.orders"}
        assert q2.kv_delete("other") is True
        assert q2.kv_delete("other") is False
        assert q2.kv_get("other") is None
    finally:
        close(q2)


def test_migrates_old_database(db: Path) -> None:
    """A db created before in_review / UNIQUE(alert_id) / kv gets upgraded in place."""
    with raw(db) as c:
        c.executescript(
            """
            CREATE TABLE tickets (ticket_id TEXT PRIMARY KEY, task_id TEXT NOT NULL,
              team_id TEXT NOT NULL, subtask_id TEXT NOT NULL, slot INTEGER NOT NULL,
              status TEXT NOT NULL, attempt INTEGER NOT NULL DEFAULT 0,
              title TEXT NOT NULL DEFAULT '', acceptance_criteria TEXT NOT NULL DEFAULT '[]',
              allowed_files TEXT NOT NULL DEFAULT '[]', fix_hint TEXT, claimed_by TEXT,
              claimed_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
              UNIQUE (task_id, subtask_id));
            CREATE TABLE escalations (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,
              alert_id TEXT NOT NULL, severity TEXT NOT NULL, category TEXT NOT NULL,
              prescribed_action TEXT NOT NULL, task_id TEXT,
              payload TEXT NOT NULL DEFAULT '{}', applied_at TEXT);
            INSERT INTO escalations (ts, alert_id, severity, category, prescribed_action, payload)
              VALUES ('a', 'ALT-20261107-001', 'LOW', 'LINE_JAM', 'NONE', '{"alert_id": "ALT-20261107-001"}'),
                     ('b', 'ALT-20261107-001', 'LOW', 'LINE_JAM', 'NONE', '{"alert_id": "ALT-20261107-001"}');
            """
        )
    q = FoundryQueue(db)
    try:
        assert len(q.escalations()) == 1  # duplicate dropped, uniqueness enforced
        assert q.record_escalation(_alert())["created"] is False
        sized(q, complexity="S")
        t = q.claim_ticket("alpha", 1)
        q.set_in_review(t.ticket_id, True)
        assert q.get_ticket(t.ticket_id).in_review is True
        q.kv_set("k", 1)
        assert q.kv_get("k") == 1
    finally:
        close(q)


# --------------------------------------------------------------------------
# Round 2: per-ticket infrastructure failure counter (E1)
# --------------------------------------------------------------------------


def test_infra_counter_counts_consecutive_and_resets(q: FoundryQueue, db: Path) -> None:
    sized(q, complexity="S")
    t = q.claim_ticket("alpha", 1)
    assert t.infra_failures == 0 and t.infra_last_error is None
    assert q.record_infra_failure(t.ticket_id, "timeout after 600s") == 1
    assert q.release_ticket(t.ticket_id) is True
    t = q.claim_ticket("alpha", 1)
    assert t.infra_failures == 1  # survives release and re-claim
    assert t.infra_last_error == "timeout after 600s"
    assert t.attempt == 0  # infra failures never spend an attempt
    assert q.record_infra_failure(t.ticket_id, "exit without JSON") == 2
    q.reset_infra_failures(t.ticket_id)
    t = q.get_ticket(t.ticket_id)
    assert t.infra_failures == 0
    assert t.infra_last_error == "exit without JSON"  # kept as history
    assert q.record_infra_failure(t.ticket_id, "again") == 1
    assert tickets_of(db, "T-1")[0]["infra_failures"] == 1
    assert state_of(q, "T-1") == "building"  # the queue never quarantines on its own
    kinds = [e["kind"] for e in q.events(task_id="T-1")]
    assert kinds.count("infra_failure") == 3 and "infra_failures_reset" in kinds


def test_infra_counter_is_per_ticket(q: FoundryQueue) -> None:
    sized(q, complexity="M")
    a = q.claim_ticket("alpha", 1)
    b = q.claim_ticket("alpha", 2)
    q.record_infra_failure(a.ticket_id, "x")
    q.record_infra_failure(a.ticket_id, "x")
    assert q.record_infra_failure(b.ticket_id, "y") == 1
    assert q.get_ticket(a.ticket_id).infra_failures == 2


def test_infra_error_excerpt_truncated_and_single_line(q: FoundryQueue) -> None:
    from foundry.orchestrator_queue import INFRA_ERROR_MAX

    sized(q, complexity="S")
    t = q.claim_ticket("alpha", 1)
    q.record_infra_failure(t.ticket_id, "line1\nline2\t" + "z" * 2000)
    err = q.get_ticket(t.ticket_id).infra_last_error
    assert err is not None and len(err) <= INFRA_ERROR_MAX
    assert err.startswith("line1 line2 z") and "\n" not in err


def test_infra_counter_unknown_ticket(q: FoundryQueue) -> None:
    with pytest.raises(KeyError):
        q.record_infra_failure("T-1:nope", "x")
    with pytest.raises(KeyError):
        q.reset_infra_failures("T-1:nope")


def test_infra_counter_persists_and_migrates(db: Path) -> None:
    with raw(db) as c:  # tickets table from before round 2 (no infra columns)
        c.executescript(
            """
            CREATE TABLE tickets (ticket_id TEXT PRIMARY KEY, task_id TEXT NOT NULL,
              team_id TEXT NOT NULL, subtask_id TEXT NOT NULL, slot INTEGER NOT NULL,
              status TEXT NOT NULL, attempt INTEGER NOT NULL DEFAULT 0,
              title TEXT NOT NULL DEFAULT '', acceptance_criteria TEXT NOT NULL DEFAULT '[]',
              allowed_files TEXT NOT NULL DEFAULT '[]', fix_hint TEXT,
              in_review INTEGER NOT NULL DEFAULT 0, claimed_by TEXT,
              claimed_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
              UNIQUE (task_id, subtask_id));
            """
        )
    q1 = FoundryQueue(db)
    sized(q1, complexity="S")
    t = q1.claim_ticket("alpha", 1)
    assert t.infra_failures == 0
    q1.record_infra_failure(t.ticket_id, "launch failed")
    close(q1)
    q2 = FoundryQueue(db)
    try:
        assert q2.resume() == 1
        t = q2.get_ticket(t.ticket_id)
        assert t.infra_failures == 1 and t.infra_last_error == "launch failed"
    finally:
        close(q2)


# --------------------------------------------------------------------------
# Round 3: tasks belong to a repo (G1)
# --------------------------------------------------------------------------


def _sized_in(q: FoundryQueue, task_id: str, repo: Path | str | None,
              complexity: str = "S", via_sizing: bool = False) -> None:
    if via_sizing:
        q.add_task(task_id, f"title {task_id}", "desc", "alpha")
        q.apply_sizing(directive(task_id, "alpha", complexity), repo_root=repo)
    else:
        q.add_task(task_id, f"title {task_id}", "desc", "alpha", repo_root=repo)
        q.apply_sizing(directive(task_id, "alpha", complexity))


def test_repo_root_stored_normalized(q: FoundryQueue, tmp_path: Path) -> None:
    from foundry.orchestrator_queue import normalize_repo_root

    repo = tmp_path / "repo_a"
    q.add_task("T-1", "t", "d", "alpha", repo_root=str(repo / "sub" / ".."))
    assert q.get_task("T-1")["repo_root"] == normalize_repo_root(repo)
    q.add_task("T-2", "t", "d", "alpha")
    assert q.get_task("T-2")["repo_root"] is None
    assert normalize_repo_root(None) is None and normalize_repo_root("  ") is None


def test_apply_sizing_sets_repo_root(q: FoundryQueue, tmp_path: Path) -> None:
    repo = tmp_path / "repo_a"
    _sized_in(q, "T-1", repo, via_sizing=True)
    assert q.get_task("T-1")["repo_root"] == str(repo)
    # Auto-registered task (no add_task) also gets it.
    q.apply_sizing(directive("T-9", "alpha", "S"), repo_root=repo)
    assert q.get_task("T-9")["repo_root"] == str(repo)
    # None keeps the repo the task already has.
    q.add_task("T-3", "t", "d", "alpha", repo_root=repo)
    q.apply_sizing(directive("T-3", "alpha", "S"))
    assert q.get_task("T-3")["repo_root"] == str(repo)


def test_claim_ticket_filters_by_repo(q: FoundryQueue, tmp_path: Path) -> None:
    a, b = tmp_path / "A", tmp_path / "B"
    _sized_in(q, "T-A", a)
    _sized_in(q, "T-B", b)
    _sized_in(q, "T-N", None)  # old/simulated row: unknown repo
    t = q.claim_ticket("alpha", 1, repo_root=b)
    assert t is not None and t.task_id == "T-B"
    assert q.claim_ticket("alpha", 1, repo_root=b) is None
    t = q.claim_ticket("alpha", 1, repo_root=str(a) + os.sep)  # trailing sep normalized
    assert t is not None and t.task_id == "T-A"
    assert q.claim_ticket("alpha", 1, repo_root=a) is None  # T-N never handed to a repo
    t = q.claim_ticket("alpha", 1)  # no filter (simulate/tests): anything left
    assert t is not None and t.task_id == "T-N"


def test_no_repo_filter_claims_every_repo(q: FoundryQueue, tmp_path: Path) -> None:
    _sized_in(q, "T-A", tmp_path / "A")
    _sized_in(q, "T-N", None)
    got = {q.claim_ticket("alpha", 1).task_id, q.claim_ticket("alpha", 1).task_id}
    assert got == {"T-A", "T-N"}


def test_wait_for_ticket_respects_repo(q: FoundryQueue, tmp_path: Path) -> None:
    a, b = tmp_path / "A", tmp_path / "B"
    _sized_in(q, "T-A", a)

    async def scenario() -> tuple[Any, Any]:
        stop = asyncio.Event()
        asyncio.get_running_loop().call_later(0.1, stop.set)
        other = await q.wait_for_ticket("alpha", 1, poll=0.01, stop=stop, repo_root=b)
        mine = await q.wait_for_ticket("alpha", 1, poll=0.01, stop=asyncio.Event(), repo_root=a)
        return other, mine

    other, mine = asyncio.run(scenario())
    assert other is None
    assert mine is not None and mine.task_id == "T-A"


def test_unfinished_tasks_lists_other_repos(q: FoundryQueue, tmp_path: Path) -> None:
    a, b = tmp_path / "A", tmp_path / "B"
    _sized_in(q, "T-A", a)
    _sized_in(q, "T-B", b)
    _sized_in(q, "T-N", None)
    q.add_task("T-done", "t", "d", "alpha", repo_root=b)
    q.set_state("T-done", "failed")
    every = q.unfinished_tasks()
    assert [t["task_id"] for t in every] == ["T-A", "T-B", "T-N"]
    assert {t["task_id"]: t["repo_root"] for t in every}["T-N"] is None
    others = q.unfinished_tasks(exclude_repo_root=a)
    assert [(t["task_id"], t["repo_root"]) for t in others] == [("T-B", str(b)), ("T-N", None)]
    assert all("state" in t and "title" in t for t in others)


def test_set_repo_root(q: FoundryQueue, tmp_path: Path) -> None:
    q.add_task("T-1", "t", "d", "alpha")
    q.set_repo_root("T-1", tmp_path)
    assert q.get_task("T-1")["repo_root"] == str(tmp_path)
    q.set_repo_root("T-1", None)
    assert q.get_task("T-1")["repo_root"] is None
    with pytest.raises(KeyError):
        q.set_repo_root("NOPE", tmp_path)


def test_repo_root_migration_old_tasks_table(db: Path, tmp_path: Path) -> None:
    with raw(db) as c:  # tasks table from before round 3 (no repo_root)
        c.executescript(
            """
            CREATE TABLE tasks (task_id TEXT PRIMARY KEY, title TEXT NOT NULL,
              description TEXT NOT NULL DEFAULT '', team_id TEXT NOT NULL, state TEXT NOT NULL,
              complexity TEXT, allocated_builders INTEGER NOT NULL DEFAULT 0, sizing_json TEXT,
              quarantine_reason TEXT, pr_summary_json TEXT, created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL, state_changed_at TEXT NOT NULL);
            INSERT INTO tasks VALUES ('F-001', 'old', '', 'alpha', 'backlog', NULL, 0, NULL,
              NULL, NULL, 't', 't', 't');
            """
        )
    q = FoundryQueue(db)
    try:
        assert q.get_task("F-001")["repo_root"] is None
        assert [t["task_id"] for t in q.unfinished_tasks(exclude_repo_root=tmp_path)] == ["F-001"]
        q.apply_sizing(directive("F-001", "alpha", "S"))
        assert q.claim_ticket("alpha", 1, repo_root=tmp_path) is None  # unknown repo
        assert q.claim_ticket("alpha", 1) is not None
    finally:
        close(q)
