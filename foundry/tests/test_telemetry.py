"""Unit tests for foundry.telemetry."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

import pytest

from foundry.telemetry import SAVED_SOURCES, Telemetry, estimate_tokens


class FakeClock:
    """Manually advanced epoch clock."""

    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class StubQueue:
    """Minimal stand-in exposing what Telemetry needs from FoundryQueue."""

    def __init__(self, path: Path) -> None:
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.lock = threading.RLock()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def tel(tmp_path: Path, clock: FakeClock) -> Telemetry:
    return Telemetry(StubQueue(tmp_path / "f.db"), clock=clock)


def test_estimate_tokens() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("abc") == 1
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("abcde") == 2
    assert estimate_tokens("x" * 400) == 100


def test_tokens_per_role_task_and_agent(tel: Telemetry) -> None:
    tel.record_tokens("T1", "alpha-1", "builder", 100, 20)
    tel.record_tokens("T1", "alpha-1", "builder", 50, 10)
    tel.record_tokens("T1", "alpha-6", "reviewer", 30, 5)
    tel.record_tokens("T2", "manager", "manager", 7, 3)
    assert tel.tokens_by_role() == {
        "builder": {"input": 150, "output": 30, "total": 180, "cache_read": 0, "calls": 2},
        "manager": {"input": 7, "output": 3, "total": 10, "cache_read": 0, "calls": 1},
        "reviewer": {"input": 30, "output": 5, "total": 35, "cache_read": 0, "calls": 1},
    }
    assert tel.tokens_by_task()["T1"] == {"input": 180, "output": 35, "total": 215, "cache_read": 0, "calls": 3}
    assert tel.tokens_by_task()["T2"]["total"] == 10
    assert tel.tokens_by_agent()["alpha-1"]["calls"] == 2


def test_invalid_inputs_rejected(tel: Telemetry) -> None:
    with pytest.raises(ValueError):
        tel.record_tokens("T1", "a", "janitor", 1, 1)
    with pytest.raises(ValueError):
        tel.record_tokens("T1", "a", "builder", -1, 1)
    with pytest.raises(ValueError):
        tel.record_tokens_saved("T1", "magic", 5)
    with pytest.raises(ValueError):
        tel.record_duration("T1", "deploy", 1.0)
    with pytest.raises(ValueError):
        tel.set_idle("alpha", -2)
    with pytest.raises(ValueError):
        tel.burn_rate(0)


def test_tokens_saved_by_source(tel: Telemetry) -> None:
    assert tel.tokens_saved_by_source() == {s: 0 for s in SAVED_SOURCES}
    tel.record_tokens_saved("T1", "pruning", 900)
    tel.record_tokens_saved("T1", "pruning", 100)
    tel.record_tokens_saved("T2", "cache", 400)
    tel.record_tokens_saved("T2", "triage", 25)
    saved = tel.tokens_saved_by_source()
    assert saved == {"pruning": 1000, "cache": 400, "release_draft": 0, "triage": 25}


def test_durations(tel: Telemetry) -> None:
    tel.record_duration("T1", "build", 10.0)
    tel.record_duration("T1", "build", 30.0)
    tel.record_duration("T1", "review", 4.0)
    d = tel.durations()
    assert d["build"] == {"count": 2, "total_seconds": 40.0, "avg_seconds": 20.0, "max_seconds": 30.0}
    assert d["review"]["count"] == 1 and d["review"]["avg_seconds"] == 4.0


def test_gate_failures_and_retries(tel: Telemetry) -> None:
    tel.record_gate_failure("T1")
    tel.record_gate_failure("T1")
    tel.record_gate_failure("T2")
    tel.record_retry("T1")
    assert tel.gate_failures("T1") == 2
    assert tel.gate_failures("T2") == 1
    assert tel.gate_failures() == 3
    assert tel.retries("T1") == 1
    assert tel.retries("T2") == 0


def test_idle_latest_value_wins(tel: Telemetry) -> None:
    tel.set_idle("alpha", 4)
    tel.set_idle("bravo", 5)
    tel.set_idle("alpha", 2)
    assert tel.idle_counts() == {"alpha": 2, "bravo": 5}


def test_queue_dwell_waiting_then_started(tel: Telemetry, clock: FakeClock) -> None:
    assert tel.queue_dwell("T1") == 0.0
    tel.mark_review_wait("T1")
    clock.advance(42)
    assert tel.queue_dwell("T1") == pytest.approx(42)
    assert tel.waiting_for_review() == {"T1": pytest.approx(42)}
    tel.mark_review_start("T1")
    clock.advance(100)
    assert tel.queue_dwell("T1") == pytest.approx(42)  # frozen once a reviewer picked it up
    assert tel.waiting_for_review() == {}
    # a REVISE round restarts the wait
    tel.mark_review_wait("T1")
    clock.advance(5)
    assert tel.queue_dwell("T1") == pytest.approx(5)


def test_burn_rate_windows(tel: Telemetry, clock: FakeClock) -> None:
    # 20 minutes ago: outside both windows
    tel.record_tokens("T1", "alpha-1", "builder", 10_000, 10_000)
    clock.advance(10 * 60)
    # 10 minutes ago: inside 15m, outside 1m
    tel.record_tokens("T1", "alpha-1", "builder", 1500, 300)
    clock.advance(9 * 60 + 30)
    # 30 s ago: inside both
    tel.record_tokens("T1", "alpha-2", "builder", 600, 60)
    tel.record_tokens("T1", "alpha-6", "reviewer", 400, 40)
    clock.advance(30)

    one = tel.burn_rate(1)
    assert one == {"input_per_min": 1000.0, "output_per_min": 100.0, "tokens_per_min": 1100.0, "rpm": 2.0,
                   "cache_read_per_min": 0.0}
    fifteen = tel.burn_rate(15)
    assert fifteen["input_per_min"] == pytest.approx(2500 / 15)
    assert fifteen["output_per_min"] == pytest.approx(400 / 15)
    assert fifteen["rpm"] == pytest.approx(3 / 15)
    assert tel.current_tpm() == 1100.0

    clock.advance(61)
    assert tel.burn_rate(1)["rpm"] == 0.0


def test_snapshot_shape_and_json(tel: Telemetry, clock: FakeClock) -> None:
    tel.record_tokens("T1", "alpha-1", "builder", 100, 50)
    tel.record_tokens_saved("T1", "cache", 300)
    tel.record_duration("T1", "build", 12.5)
    tel.record_gate_failure("T1")
    tel.record_retry("T1")
    tel.set_idle("alpha", 4)
    tel.set_idle("bravo", 5)
    tel.mark_review_wait("T1")
    clock.advance(20)

    snap = tel.snapshot()
    json.dumps(snap)  # must be JSON-able
    assert snap["generated_at"].endswith("+00:00")
    assert snap["tokens"]["total"] == 150
    assert snap["tokens"]["by_role"]["builder"]["input"] == 100
    assert snap["tokens"]["by_task"]["T1"]["output"] == 50
    assert snap["tokens_saved_by_gemma"] == {
        "by_source": {"pruning": 0, "cache": 300, "release_draft": 0, "triage": 0},
        "total": 300,
    }
    assert snap["durations"]["build"]["count"] == 1
    assert snap["idle_agent_count"] == {"by_team": {"alpha": 4, "bravo": 5}, "total": 9}
    assert snap["gates"] == {
        "failures": 1, "retries": 1, "failures_by_task": {"T1": 1}, "retries_by_task": {"T1": 1},
    }
    assert snap["queue_dwell"]["waiting"]["T1"] == pytest.approx(20)
    assert snap["queue_dwell"]["max_seconds"] == pytest.approx(20)
    assert set(snap["burn_rate"]) == {"1m", "15m"}
    assert snap["current_tpm"] == 150.0
    assert snap["current_rpm"] == 1.0


def test_empty_snapshot(tel: Telemetry) -> None:
    snap = tel.snapshot()
    assert snap["tokens"]["total"] == 0
    assert snap["queue_dwell"] == {"waiting": {}, "waiting_submissions": 0, "max_seconds": 0.0}
    assert snap["current_tpm"] == 0.0


def test_persists_across_instances(tmp_path: Path, clock: FakeClock) -> None:
    db = tmp_path / "p.db"
    Telemetry(StubQueue(db), clock=clock).record_tokens("T1", "alpha-1", "builder", 3, 4)
    again = Telemetry(StubQueue(db), clock=clock)  # schema creation is idempotent
    assert again.tokens_by_task()["T1"]["total"] == 7


def test_accepts_bare_connection(clock: FakeClock) -> None:
    tel = Telemetry(sqlite3.connect(":memory:"), clock=clock)
    tel.record_tokens_saved("T1", "release_draft", 12)
    assert tel.tokens_saved_by_source()["release_draft"] == 12


def test_with_real_foundry_queue(tmp_path: Path, clock: FakeClock) -> None:
    oq = pytest.importorskip("foundry.orchestrator_queue")
    queue = oq.FoundryQueue(tmp_path / "q.db")
    tel = Telemetry(queue, clock=clock)
    tel.record_tokens("T1", "alpha-1", "builder", 10, 2)
    tel.set_idle("alpha", 3)
    assert tel.snapshot()["tokens"]["total"] == 12
    tables = {r[0] for r in queue.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"telemetry_tokens", "telemetry_saved", "telemetry_durations", "telemetry_gates",
            "telemetry_idle", "telemetry_review_wait"} <= tables


def test_concurrent_writers_threadsafe(tel: Telemetry) -> None:
    def work(n: int) -> None:
        for _ in range(50):
            tel.record_tokens(f"T{n}", f"alpha-{n}", "builder", 1, 1)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(1, 5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(v["calls"] for v in tel.tokens_by_task().values()) == 200


# --------------------------------------------------------------- round 1 fixes
def test_dwell_per_submission_oldest_unstarted_still_shows(tel: Telemetry, clock: FakeClock) -> None:
    """alpha-6's repro (D7): 5 waits then one start must not hide the other 4."""
    for n in range(1, 6):
        tel.mark_review_wait("T1", f"tk-{n}")
        clock.advance(10)
    # now: tk-1 waited 50 s, tk-5 waited 10 s
    tel.mark_review_start("T1", "tk-1")
    assert tel.waiting_for_review() == {"T1": pytest.approx(40)}  # tk-2 is now the oldest
    assert tel.queue_dwell("T1") == pytest.approx(40)
    assert [w["item"] for w in tel.waiting_submissions()] == ["tk-2", "tk-3", "tk-4", "tk-5"]
    # starting a later one leaves tk-2 as the oldest
    tel.mark_review_start("T1", "tk-4")
    clock.advance(5)
    assert tel.queue_dwell("T1") == pytest.approx(45)
    for n in (2, 3, 5):
        tel.mark_review_start("T1", f"tk-{n}")
    assert tel.waiting_for_review() == {}
    assert tel.queue_dwell("T1") > 0  # frozen dwell of the last started one
    assert tel.snapshot()["queue_dwell"]["waiting_submissions"] == 0


def test_dwell_anonymous_submissions_fifo(tel: Telemetry, clock: FakeClock) -> None:
    for _ in range(5):
        tel.mark_review_wait("T1")
        clock.advance(10)
    tel.mark_review_start("T1")  # starts the oldest (50 s)
    assert tel.queue_dwell("T1") == pytest.approx(40)
    assert len(tel.waiting_submissions()) == 4


def test_dwell_rewait_same_item_keeps_original_time(tel: Telemetry, clock: FakeClock) -> None:
    tel.mark_review_wait("T1", "tk-1")
    clock.advance(30)
    tel.mark_review_wait("T1", "tk-1")  # duplicate enqueue does not reset the clock
    clock.advance(10)
    assert tel.queue_dwell("T1") == pytest.approx(40)
    assert len(tel.waiting_submissions()) == 1
    tel.mark_review_start("T1", "tk-1")
    tel.mark_review_wait("T1", "tk-1")  # REVISE round: a fresh wait
    clock.advance(3)
    assert tel.queue_dwell("T1") == pytest.approx(3)


def test_dwell_separate_tasks(tel: Telemetry, clock: FakeClock) -> None:
    tel.mark_review_wait("T1", "a")
    clock.advance(20)
    tel.mark_review_wait("T2", "b")
    clock.advance(5)
    tel.mark_review_start("T2", "unknown")  # no-op
    assert tel.waiting_for_review() == {"T1": pytest.approx(25), "T2": pytest.approx(5)}


def test_cache_read_tokens_excluded_from_burn(tel: Telemetry, clock: FakeClock) -> None:
    """D11: cache reads are stored, but burn rates / TPM count uncached input + output only."""
    tel.record_tokens("T1", "alpha-1", "builder", 200, 100, cache_read_tokens=50_000)
    tel.record_tokens("T1", "alpha-6", "reviewer", 100, 0)
    one = tel.burn_rate(1)
    assert one["tokens_per_min"] == 400.0
    assert one["input_per_min"] == 300.0
    assert one["cache_read_per_min"] == 50_000.0
    assert tel.current_tpm() == 400.0
    assert tel.tokens_by_role()["builder"] == {
        "input": 200, "output": 100, "total": 300, "cache_read": 50_000, "calls": 1,
    }
    snap = tel.snapshot()
    assert snap["tokens"]["total"] == 400
    assert snap["tokens"]["cache_read"] == 50_000
    assert snap["current_tpm"] == 400.0
    with pytest.raises(ValueError):
        tel.record_tokens("T1", "a", "builder", 1, 1, cache_read_tokens=-1)


def test_cache_read_column_added_to_old_db(tmp_path: Path, clock: FakeClock) -> None:
    db = tmp_path / "old.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE telemetry_tokens (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,"
        " ts_iso TEXT NOT NULL, task_id TEXT NOT NULL, agent_id TEXT NOT NULL, role TEXT NOT NULL,"
        " input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL)"
    )
    conn.execute(
        "INSERT INTO telemetry_tokens(ts, ts_iso, task_id, agent_id, role, input_tokens, output_tokens)"
        " VALUES (?, 'x', 'T0', 'a', 'builder', 5, 5)", (clock.now,)
    )
    conn.commit()
    conn.close()
    tel = Telemetry(StubQueue(db), clock=clock)
    tel.record_tokens("T1", "a", "builder", 1, 1, cache_read_tokens=9)
    assert tel.tokens_by_task()["T0"]["cache_read"] == 0
    assert tel.tokens_by_task()["T1"]["cache_read"] == 9


def test_tx_joins_open_transaction_without_committing(tmp_path: Path, clock: FakeClock) -> None:
    """A9: inside a caller's transaction, Telemetry must neither commit nor roll back."""
    db = tmp_path / "tx.db"
    q = StubQueue(db)
    tel = Telemetry(q, clock=clock)
    q.conn.execute("CREATE TABLE other (x INTEGER)")
    q.conn.commit()

    q.conn.execute("BEGIN")
    q.conn.execute("INSERT INTO other VALUES (1)")
    tel.record_tokens("T1", "alpha-1", "builder", 10, 10)
    assert q.conn.in_transaction  # still the caller's transaction
    q.conn.rollback()
    assert q.conn.execute("SELECT COUNT(*) FROM other").fetchone()[0] == 0
    assert tel.tokens_by_task() == {}  # rolled back with the caller's work

    q.conn.execute("BEGIN")
    q.conn.execute("INSERT INTO other VALUES (2)")
    tel.mark_review_wait("T1", "tk")
    q.conn.commit()
    assert q.conn.execute("SELECT COUNT(*) FROM other").fetchone()[0] == 1
    assert len(tel.waiting_submissions()) == 1


def test_tx_error_inside_caller_transaction_keeps_it_open(tmp_path: Path, clock: FakeClock) -> None:
    q = StubQueue(tmp_path / "tx2.db")
    tel = Telemetry(q, clock=clock)
    q.conn.execute("CREATE TABLE other (x INTEGER)")
    q.conn.commit()
    q.conn.execute("BEGIN")
    q.conn.execute("INSERT INTO other VALUES (1)")
    with pytest.raises(sqlite3.Error):
        with tel._tx() as cur:
            cur.execute("INSERT INTO no_such_table VALUES (1)")
    assert q.conn.in_transaction
    q.conn.commit()
    assert q.conn.execute("SELECT COUNT(*) FROM other").fetchone()[0] == 1


def test_init_inside_caller_transaction_does_not_commit(tmp_path: Path, clock: FakeClock) -> None:
    q = StubQueue(tmp_path / "tx3.db")
    q.conn.execute("CREATE TABLE other (x INTEGER)")
    q.conn.commit()
    q.conn.execute("BEGIN")
    q.conn.execute("INSERT INTO other VALUES (1)")
    Telemetry(q, clock=clock)
    assert q.conn.in_transaction
    q.conn.rollback()
    assert q.conn.execute("SELECT COUNT(*) FROM other").fetchone()[0] == 0


def test_tx_with_real_queue_transaction(tmp_path: Path, clock: FakeClock) -> None:
    oq = pytest.importorskip("foundry.orchestrator_queue")
    queue = oq.FoundryQueue(tmp_path / "rq.db")
    tel = Telemetry(queue, clock=clock)
    if not hasattr(queue, "transaction"):
        pytest.skip("FoundryQueue.transaction() not available")
    with pytest.raises(RuntimeError):
        with queue.transaction():
            tel.record_tokens("T1", "alpha-1", "builder", 10, 10)
            raise RuntimeError("caller aborts")
    assert tel.tokens_by_task() == {}
    with queue.transaction():
        tel.record_tokens("T2", "alpha-1", "builder", 1, 1)
    assert tel.tokens_by_task()["T2"]["total"] == 2


def test_start_with_item_falls_back_to_anonymous_wait(tel: Telemetry, clock: FakeClock) -> None:
    tel.mark_review_wait("T1")
    clock.advance(10)
    tel.mark_review_wait("T1", "tk-9")
    tel.mark_review_start("T1", "tk-1")  # tk-1 never waited by name: starts the anonymous one
    assert [w["item"] for w in tel.waiting_submissions()] == ["tk-9"]
