"""Foreman context widening: visibility-failure detection and the learned starting level."""
from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from foundry import floor_manager as fm
from foundry import shop_foreman as sf
from foundry.orchestrator_queue import FoundryQueue
from foundry.schemas import PrescribedAction

TASK = "F-100"
EARLY = "2026-01-01T00:00:01Z"
WIDENED = "2026-01-01T00:00:05Z"
LATE = "2026-01-01T00:00:10Z"
NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


class FakeQueue:
    """Just enough of the queue for these hooks, on an in-memory database."""

    CONTEXT_LEVEL_KV_PREFIX = "context_level:"
    MIN_CONTEXT_LEVEL = 1
    MAX_CONTEXT_LEVEL = 3
    raise_task_context_level: Any = FoundryQueue.raise_task_context_level

    def __init__(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            "CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT);"
            "CREATE TABLE gate_results (task_id TEXT, subtask_id TEXT, passed INTEGER,"
            " stdout TEXT, stderr TEXT, ts TEXT);"
        )
        self.tickets: list[Any] = []
        self.tasks: list[dict[str, Any]] = []

    def now(self) -> str:
        return WIDENED

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        yield self.conn
        self.conn.commit()

    def all_tasks(self) -> list[dict[str, Any]]:
        return self.tasks

    def tickets_for_task(self, task_id: str) -> list[Any]:
        return [t for t in self.tickets if t.task_id == task_id]

    def task_context_level(self, task_id: str) -> int:
        row = self.conn.execute(
            "SELECT value FROM kv WHERE key=?", (self.CONTEXT_LEVEL_KV_PREFIX + task_id,)
        ).fetchone()
        return int(json.loads(row["value"])) if row is not None else self.MIN_CONTEXT_LEVEL

    def set_level(self, task_id: str, level: int, at: str = WIDENED) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO kv (key, value, updated_at) VALUES (?,?,?)",
            (self.CONTEXT_LEVEL_KV_PREFIX + task_id, json.dumps(level), at),
        )

    def gate_result(
        self, stderr: str, *, subtask: str = "F-100.1", ts: str = LATE, passed: int = 0, stdout: str = ""
    ) -> None:
        self.conn.execute(
            "INSERT INTO gate_results (task_id, subtask_id, passed, stdout, stderr, ts) VALUES (?,?,?,?,?,?)",
            (TASK, subtask, passed, stdout, stderr, ts),
        )


def make_foreman(queue: FakeQueue) -> Any:
    """A foreman built without ``__init__``: only what the hooks under test read."""
    foreman: Any = sf.ShopForeman.__new__(sf.ShopForeman)
    foreman.queue = queue
    foreman.telemetry = None
    foreman.config = SimpleNamespace(thresholds=SimpleNamespace(), builders_per_team=5)
    foreman._sql = lambda sql, params=(): queue.conn.execute(sql, params).fetchall()
    return foreman


def widen_anomalies(foreman: Any) -> list[Any]:
    return foreman._widen_anomalies({TASK: {"team_id": "bravo"}})


@pytest.mark.parametrize(
    "output",
    [
        "NameError: name 'x' is not defined",
        "BLOCKED: need the body of `f`",
        "foo.py:1:1: F821 undefined name",
        "Undefined name `x`",
        "ImportError: cannot import name 'y'",
    ],
)
def test_each_visibility_marker_counts(output: str) -> None:
    queue = FakeQueue()
    queue.gate_result(output)
    assert make_foreman(queue)._visibility_failures(TASK) == 1


def test_unrelated_and_passed_results_do_not_count() -> None:
    queue = FakeQueue()
    queue.gate_result("AssertionError: 1 != 2")
    queue.gate_result("NameError", passed=1)
    assert make_foreman(queue)._visibility_failures(TASK) == 0


def test_stdout_is_searched_too() -> None:
    queue = FakeQueue()
    queue.gate_result("", stdout="F821 undefined name")
    assert make_foreman(queue)._visibility_failures(TASK) == 1


def test_done_tickets_are_left_out() -> None:
    queue = FakeQueue()
    queue.gate_result("NameError", subtask="F-100.1")
    queue.gate_result("NameError", subtask="F-100.2")
    queue.tickets = [SimpleNamespace(task_id=TASK, subtask_id="F-100.1", status="done")]
    assert make_foreman(queue)._visibility_failures(TASK) == 1


def test_failures_before_the_last_level_change_are_left_out() -> None:
    queue = FakeQueue()
    queue.gate_result("NameError", ts=EARLY)
    queue.gate_result("NameError", ts=LATE)
    queue.set_level(TASK, 2)
    assert make_foreman(queue)._visibility_failures(TASK) == 1


def test_widen_prescribed_after_repeated_visibility_failures() -> None:
    queue = FakeQueue()
    for _ in range(2):
        queue.gate_result("NameError: name 'x' is not defined")
    [anomaly] = widen_anomalies(make_foreman(queue))
    assert anomaly.action is PrescribedAction.WIDEN_CONTEXT
    assert anomaly.task_id == TASK
    assert anomaly.gate_failure_count == 2
    assert anomaly.action_parameters == {"level": 2, "from": 1}


def test_widen_steps_one_level_from_the_current_one() -> None:
    queue = FakeQueue()
    queue.set_level(TASK, 2, at=EARLY)
    queue.gate_result("NameError")
    queue.gate_result("NameError")
    [anomaly] = widen_anomalies(make_foreman(queue))
    assert anomaly.action_parameters == {"level": 3, "from": 2}


def test_no_widen_below_the_failure_threshold() -> None:
    queue = FakeQueue()
    queue.gate_result("NameError")
    assert widen_anomalies(make_foreman(queue)) == []


def test_no_widen_at_the_maximum_level() -> None:
    queue = FakeQueue()
    queue.set_level(TASK, queue.MAX_CONTEXT_LEVEL, at=EARLY)
    queue.gate_result("NameError")
    queue.gate_result("NameError")
    assert widen_anomalies(make_foreman(queue)) == []


def test_no_widen_from_failures_older_than_the_last_widen() -> None:
    queue = FakeQueue()
    queue.gate_result("NameError", ts=EARLY)
    queue.gate_result("NameError", ts=EARLY)
    queue.set_level(TASK, 2)
    assert widen_anomalies(make_foreman(queue)) == []


def stub_detectors(foreman: Any, gate_failures: dict[str, int]) -> None:
    foreman._now = lambda: NOW
    foreman._gate_fail_limit = lambda: 2
    foreman._ticket_gate_failures = lambda task_id: dict(gate_failures)
    foreman._claimed_ages = lambda team_id, now: []
    foreman._review_wait_items = lambda: {}
    foreman._free_slots = lambda team_id: []
    foreman._reassign_anomalies = lambda tasks, now, idle: []
    foreman._retry_anomalies = lambda now: []


def test_detect_anomalies_puts_widen_after_line_jam() -> None:
    queue = FakeQueue()
    queue.tasks = [{"task_id": TASK, "team_id": "bravo", "state": "building"}]
    queue.gate_result("NameError")
    queue.gate_result("NameError")
    foreman = make_foreman(queue)
    stub_detectors(foreman, {"F-100.1": 2})
    actions = [a.action for a in foreman.detect_anomalies()]
    assert actions == [PrescribedAction.QUARANTINE_TASK, PrescribedAction.WIDEN_CONTEXT]


@pytest.mark.parametrize(
    ("task_id", "undone_seconds_ago", "expected"),
    [(TASK, 60, True), (TASK, sf._UNDO_SUPPRESS_S + 60, False), ("F-999", 60, False)],
)
def test_undone_widen_suppresses_the_same_task(task_id: str, undone_seconds_ago: float, expected: bool) -> None:
    queue = FakeQueue()
    queue.gate_result("NameError")
    queue.gate_result("NameError")
    foreman = make_foreman(queue)
    [anomaly] = widen_anomalies(foreman)
    undone_at = (NOW - timedelta(seconds=undone_seconds_ago)).isoformat()
    foreman._action_log = lambda: [{"action": "WIDEN_CONTEXT", "task_id": task_id, "undone_at": undone_at}]
    assert foreman._undone_recently(anomaly, NOW) is expected


def levels(*values: Any) -> list[dict[str, Any]]:
    return [{"context_level": value} for value in values]


@pytest.fixture
def every_outcome_similar(monkeypatch: pytest.MonkeyPatch) -> None:
    """Treat every outcome as similar, so these tests cover only the level rule."""
    monkeypatch.setattr(sf, "similar_outcomes", lambda outcomes, title, files, **kwargs: list(outcomes))


@pytest.mark.usefixtures("every_outcome_similar")
@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ((), 1),
        ((3,), 1),
        ((2, 2, 1), 2),
        ((2, 1, 1), 1),
        ((2, 2, 1, 1), 1),
        ((3, 3, 1), 3),
        ((3, 2, 1), 2),
        (("bad", None, 2, 2), 1),
    ],
)
def test_learned_level_needs_a_majority_of_similar_tasks(values: tuple[Any, ...], expected: int) -> None:
    assert sf.learned_context_level(levels(*values), "t", []) == expected


@pytest.mark.usefixtures("every_outcome_similar")
def test_learned_level_respects_the_ceiling() -> None:
    assert sf.learned_context_level(levels(3, 3, 3), "t", [], ceiling=2) == 2


@pytest.mark.usefixtures("every_outcome_similar")
def test_seed_raises_a_new_task_to_the_learned_level() -> None:
    queue = FakeQueue()
    foreman = make_foreman(queue)
    foreman._outcomes = lambda repo=None: levels(2, 2)
    foreman._seed_context_level(TASK, {"title": "t", "files": ["a.py"]})
    assert queue.task_context_level(TASK) == 2


@pytest.mark.usefixtures("every_outcome_similar")
def test_seed_never_lowers_the_level() -> None:
    queue = FakeQueue()
    queue.set_level(TASK, 3)
    foreman = make_foreman(queue)
    foreman._outcomes = lambda repo=None: levels(2, 2)
    foreman._seed_context_level(TASK, {"title": "t", "files": ["a.py"]})
    assert queue.task_context_level(TASK) == 3


def test_seed_problems_are_swallowed() -> None:
    queue = FakeQueue()
    foreman = make_foreman(queue)
    def boom(repo: Any = None) -> list[Any]:
        raise RuntimeError("no outcomes")
    foreman._outcomes = boom
    foreman._seed_context_level(TASK, {"title": "t"})
    assert queue.task_context_level(TASK) == 1


def test_size_task_scope_seeds_after_sizing() -> None:
    foreman = make_foreman(FakeQueue())
    directive = SimpleNamespace(task_id=TASK)
    seeded: list[Any] = []
    async def size(task: Any) -> Any:
        return directive
    foreman._size_task_scope = size
    foreman._seed_context_level = lambda task_id, task: seeded.append((task_id, task))
    task = {"task_id": TASK}
    assert asyncio.run(foreman.size_task_scope(task)) is directive
    assert seeded == [(TASK, task)]


def test_raise_clamps_to_the_maximum() -> None:
    queue = FakeQueue()
    assert queue.raise_task_context_level(TASK, 9) == queue.MAX_CONTEXT_LEVEL
    assert queue.task_context_level(TASK) == queue.MAX_CONTEXT_LEVEL


def test_raise_keeps_a_higher_level() -> None:
    queue = FakeQueue()
    queue.set_level(TASK, 3)
    assert queue.raise_task_context_level(TASK, 2) == 3
    assert queue.task_context_level(TASK) == 3


def test_raise_treats_an_unreadable_level_as_the_minimum() -> None:
    queue = FakeQueue()
    key = queue.CONTEXT_LEVEL_KV_PREFIX + TASK
    queue.conn.execute("INSERT INTO kv VALUES (?,?,?)", (key, "not json", EARLY))
    assert queue.raise_task_context_level(TASK, 2) == 2


def stamp(seconds_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def widen_row(**changes: Any) -> dict[str, Any]:
    return {
        "action": "WIDEN_CONTEXT", "task_id": TASK, "applied": True, "at": stamp(60), "undone_at": None, **changes,
    }


def refusal(*rows: dict[str, Any]) -> str | None:
    manager: Any = SimpleNamespace(action_log=list(rows))
    return fm.FloorManager._widen_refusal(manager, TASK)


def test_no_widen_history_allows_a_widen() -> None:
    assert refusal() is None


def test_recent_widen_is_refused() -> None:
    reason = refusal(widen_row())
    assert reason is not None and "10 minutes" in reason


def test_recent_undo_is_refused() -> None:
    old = stamp(fm._WIDEN_COOLDOWN_S + 60)
    reason = refusal(widen_row(at=old, undone_at=stamp(60)))
    assert reason is not None and "undone" in reason


def test_future_widen_counts_as_recent() -> None:
    assert refusal(widen_row(at=stamp(-300))) is not None


@pytest.mark.parametrize(
    "row",
    [
        widen_row(at=stamp(fm._WIDEN_COOLDOWN_S + 60)),
        widen_row(at=stamp(fm._WIDEN_COOLDOWN_S + 60), undone_at=stamp(fm._WIDEN_UNDO_SUPPRESS_S + 60)),
        widen_row(applied=False),
        widen_row(task_id="F-999"),
        widen_row(action="RETRY_TASK"),
        widen_row(at="not a timestamp"),
    ],
)
def test_rows_that_do_not_block_a_widen(row: dict[str, Any]) -> None:
    assert refusal(row) is None
