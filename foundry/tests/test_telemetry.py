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
        "builder": {"input": 150, "output": 30, "total": 180, "calls": 2},
        "manager": {"input": 7, "output": 3, "total": 10, "calls": 1},
        "reviewer": {"input": 30, "output": 5, "total": 35, "calls": 1},
    }
    assert tel.tokens_by_task()["T1"] == {"input": 180, "output": 35, "total": 215, "calls": 3}
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
    assert one == {"input_per_min": 1000.0, "output_per_min": 100.0, "tokens_per_min": 1100.0, "rpm": 2.0}
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
    assert snap["queue_dwell"] == {"waiting": {}, "max_seconds": 0.0}
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
            "telemetry_idle", "telemetry_dwell"} <= tables


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
