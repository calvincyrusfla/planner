"""Tests for foundry/wakeup.py and the event-woken rounds of the batch watcher."""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from types import SimpleNamespace

import pytest

from foundry.board_dispatch import BatchWatcher, batch_active_poll_interval, batch_poll_interval
from foundry.orchestrator_queue import FoundryQueue
from foundry.serve import Service
from foundry.wakeup import Wakeup

REPO = "o/r"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FOUNDRY_BATCH_POLL_S", raising=False)
    monkeypatch.delenv("FOUNDRY_BATCH_ACTIVE_POLL_S", raising=False)


@pytest.fixture
def q(tmp_path: Path) -> Iterator[FoundryQueue]:
    queue = FoundryQueue(tmp_path / "q.db")
    yield queue
    queue.close()


async def _until(pred: Callable[[], bool]) -> None:
    loop = asyncio.get_running_loop()
    end = loop.time() + 10
    while not pred():
        assert loop.time() < end, "condition not reached in time"
        await asyncio.sleep(0.005)


# --------------------------------------------------------------------------- Wakeup


def test_notify_from_another_thread_wakes_the_waiter() -> None:
    async def go() -> None:
        wakeup = Wakeup()
        waiter = asyncio.create_task(wakeup.wait(30))
        await asyncio.sleep(0)  # the waiter binds the loop
        thread = threading.Thread(target=wakeup.notify, args=("ticket_done",))
        thread.start()
        assert await asyncio.wait_for(waiter, 5) == "ticket_done"
        thread.join()

    asyncio.run(go())


def test_a_wake_before_the_first_wait_is_not_lost() -> None:
    async def go() -> None:
        wakeup = Wakeup()
        wakeup.notify("early")
        assert await asyncio.wait_for(wakeup.wait(30), 5) == "early"

    asyncio.run(go())


def test_wait_times_out_with_none() -> None:
    assert asyncio.run(Wakeup().wait(0.01)) is None


def test_wait_returns_the_first_reason_and_clears_it() -> None:
    async def go() -> None:
        wakeup = Wakeup()
        wakeup.notify("first")
        wakeup.notify("second")
        assert await wakeup.wait(5) == "first"
        assert await wakeup.wait(0) is None

    asyncio.run(go())


def test_a_wake_already_taken_does_not_end_the_next_wait_early() -> None:
    async def go() -> None:
        wakeup = Wakeup()
        await wakeup.wait(0)  # binds the loop
        wakeup.notify("x")  # its event.set() runs only after the wait below took the reason
        assert await wakeup.wait(5) == "x"
        loop = asyncio.get_running_loop()
        started = loop.time()
        assert await wakeup.wait(0.05) is None
        assert loop.time() - started >= 0.04

    asyncio.run(go())


def test_notify_works_as_the_queue_callback(q: FoundryQueue) -> None:
    async def go() -> None:
        wakeup = Wakeup()
        q.on_event = wakeup.notify
        q.record_event("ticket_done", "T-1")
        assert await asyncio.wait_for(wakeup.wait(30), 5) == "ticket_done"

    asyncio.run(go())


# --------------------------------------------------------------------------- queue on_event


def test_on_event_runs_after_commit(q: FoundryQueue) -> None:
    calls: list[tuple[str, str | None]] = []
    q.on_event = lambda kind, task_id: calls.append((kind, task_id))
    with q.transaction() as conn:
        q._event(conn, "a", "T-1", {})
        assert calls == []
    assert calls == [("a", "T-1")]


def test_on_event_is_not_called_after_rollback(q: FoundryQueue) -> None:
    calls: list[str] = []
    q.on_event = lambda kind, task_id: calls.append(kind)
    with pytest.raises(RuntimeError):
        with q.transaction():
            q.record_event("a", None)
            raise RuntimeError("boom")
    assert calls == [] and q.events() == []
    q.record_event("b", None)  # the rolled back event does not come along with the next one
    assert calls == ["b"]


def test_nested_transactions_call_on_event_once_per_event_at_the_outer_commit(q: FoundryQueue) -> None:
    calls: list[str] = []
    q.on_event = lambda kind, task_id: calls.append(kind)
    with q.transaction():
        q.record_event("a", None)
        with q.transaction():
            q.record_event("b", None)
        assert calls == []
        q.record_event("c", None)
    assert calls == ["a", "b", "c"]


def test_a_raising_callback_does_not_break_the_transaction(q: FoundryQueue) -> None:
    calls: list[str] = []

    def callback(kind: str, task_id: str | None) -> None:
        calls.append(kind)
        raise ValueError("broken")

    q.on_event = callback
    with q.transaction():
        q.record_event("a", None)
        q.record_event("b", None)
    assert calls == ["a", "b"]
    assert [e["kind"] for e in q.events()] == ["a", "b"]
    assert not q.conn.in_transaction


def test_without_on_event_nothing_changes(q: FoundryQueue) -> None:
    assert q.on_event is None
    assert q.record_event("a", None) > 0
    assert [e["kind"] for e in q.events()] == ["a"]


# --------------------------------------------------------------------------- settings


def test_active_poll_setting() -> None:
    assert batch_active_poll_interval({}) == 5.0
    assert batch_active_poll_interval({"FOUNDRY_BATCH_ACTIVE_POLL_S": "2"}) == 2.0
    assert batch_active_poll_interval({"FOUNDRY_BATCH_ACTIVE_POLL_S": "0.1"}) == 1.0
    assert batch_active_poll_interval({"FOUNDRY_BATCH_ACTIVE_POLL_S": "nope"}) == 5.0
    assert batch_poll_interval({}) == 15.0


# --------------------------------------------------------------------------- watcher


class _Stop(Exception):
    """Ends the watcher's loop in a test."""


class Board:
    """Answers with the given states in turn, repeating the last one."""

    def __init__(self, *states: list[dict[str, object]]) -> None:
        self.states = list(states) or [[]]

    def state(self) -> dict[str, object]:
        current = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        return {"foundryBatches": current}

    def op(self, type_: str, args: Mapping[str, object]) -> dict[str, object]:
        return {"ok": True}


class Watcher(BatchWatcher):
    """Runs real rounds; advancing a batch does nothing."""

    async def _advance(self, batch: Mapping[str, object]) -> None:
        return None


class ScriptedWakeup(Wakeup):
    """Answers each timed wait from ``replies``, moving the clock first; stops the loop when they run out."""

    def __init__(self, replies: list[str | None], tick: Callable[[], None] = lambda: None) -> None:
        super().__init__()
        self.replies = replies
        self.tick = tick
        self.timeouts: list[float] = []

    async def wait(self, timeout: float) -> str | None:
        if timeout == 0:
            return None
        self.timeouts.append(timeout)
        if not self.replies:
            raise _Stop
        self.tick()
        return self.replies.pop(0)


class FakeTime:
    def __init__(self) -> None:
        self.t = 1000.0
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds


def _watcher(q: FoundryQueue, tmp_path: Path, board: Board, wakeup: Wakeup | None, clock: FakeTime) -> Watcher:
    service = SimpleNamespace(running=True, factory=SimpleNamespace(queue=q), repo=tmp_path, config=None)
    return Watcher(service, board, repo_slug=REPO, host="pc", interval=15.0, enabled=True,
                   wakeup=wakeup, sleep=clock.sleep, now=clock.now)


def _run_until_stopped(watcher: Watcher) -> None:
    async def go() -> None:
        with pytest.raises(_Stop):
            await watcher._loop()

    asyncio.run(go())


def _ours(status: str) -> dict[str, object]:
    return {"id": "S-1", "repo": REPO, "status": status, "claimedBy": {"service": "foundry@pc:o/r"}}


@pytest.mark.parametrize(("batches", "expected"), [
    ([], 15.0),
    ([_ours("running")], 5.0),
    ([_ours("claimed")], 5.0),
    ([_ours("done")], 15.0),
    ([{"id": "S-2", "repo": REPO, "status": "queued"}], 5.0),
    ([{"id": "S-2", "repo": "other/repo", "status": "queued"}], 15.0),
    ([{"id": "S-2", "repo": "other/repo", "status": "running", "claimedBy": {"service": "someone"}}], 15.0),
])
def test_the_interval_follows_the_open_batches(
    q: FoundryQueue, tmp_path: Path, batches: list[dict[str, object]], expected: float,
) -> None:
    wakeup = ScriptedWakeup([])
    _run_until_stopped(_watcher(q, tmp_path, Board(batches), wakeup, FakeTime()))
    assert wakeup.timeouts == [expected]


def test_a_batch_closed_here_counts_as_idle(q: FoundryQueue, tmp_path: Path) -> None:
    wakeup = ScriptedWakeup([])
    watcher = _watcher(q, tmp_path, Board([_ours("running")]), wakeup, FakeTime())
    watcher._save("S-1", {**watcher._record("S-1"), "closed": True})
    _run_until_stopped(watcher)
    assert wakeup.timeouts == [15.0]


def test_plain_sleep_uses_the_same_intervals_without_a_wakeup(q: FoundryQueue, tmp_path: Path) -> None:
    clock = FakeTime()

    async def sleep(seconds: float) -> None:
        clock.sleeps.append(seconds)
        if len(clock.sleeps) == 2:
            raise _Stop

    service = SimpleNamespace(running=True, factory=SimpleNamespace(queue=q), repo=tmp_path, config=None)
    watcher = Watcher(service, Board([_ours("running")], []), repo_slug=REPO, host="pc", interval=15.0,
                      enabled=True, sleep=sleep, now=clock.now)
    with pytest.raises(_Stop):
        asyncio.run(watcher._loop())
    assert clock.sleeps == [5.0, 15.0]


def test_the_active_interval_is_never_slower_than_the_idle_one(q: FoundryQueue, tmp_path: Path) -> None:
    service = SimpleNamespace(running=True, factory=SimpleNamespace(queue=q), repo=tmp_path, config=None)
    watcher = BatchWatcher(service, Board(), repo_slug=REPO, host="pc", interval=2.0, enabled=True)
    assert watcher.active_interval == 2.0


def test_the_mode_switch_is_logged_once(
    q: FoundryQueue, tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    board = Board([_ours("running")], [_ours("running")], [_ours("done")], [_ours("done")])
    wakeup = ScriptedWakeup([None, None, None])
    with caplog.at_level(logging.INFO, logger="foundry.board_dispatch"):
        _run_until_stopped(_watcher(q, tmp_path, board, wakeup, FakeTime()))
    assert wakeup.timeouts == [5.0, 5.0, 15.0, 15.0]
    switches = [r.getMessage() for r in caplog.records if "polling every" in r.getMessage()]
    assert switches == ["board dispatch: batch open, polling every 5s",
                        "board dispatch: no batch open, polling every 15s"]


def test_a_timeout_starts_the_next_round_without_a_pause(q: FoundryQueue, tmp_path: Path) -> None:
    clock = FakeTime()
    watcher = _watcher(q, tmp_path, Board(), ScriptedWakeup([None]), clock)
    _run_until_stopped(watcher)
    assert watcher.rounds == 2 and clock.sleeps == []


@pytest.mark.parametrize(("elapsed", "pause"), [(0.0, [1.0]), (0.4, [pytest.approx(0.6)]), (2.0, [])])
def test_a_wake_waits_out_the_rest_of_the_second(
    q: FoundryQueue, tmp_path: Path, elapsed: float, pause: list[float],
) -> None:
    clock = FakeTime()

    def tick() -> None:
        clock.t += elapsed

    watcher = _watcher(q, tmp_path, Board(), ScriptedWakeup(["ticket_done"], tick), clock)
    _run_until_stopped(watcher)
    assert watcher.rounds == 2 and clock.sleeps == pause


def test_a_clock_stepping_back_never_lengthens_the_pause(q: FoundryQueue, tmp_path: Path) -> None:
    clock = FakeTime()

    def tick() -> None:
        clock.t -= 100

    _run_until_stopped(_watcher(q, tmp_path, Board(), ScriptedWakeup(["x"], tick), clock))
    assert clock.sleeps == [1.0]


def test_a_queue_event_starts_the_next_round_early(q: FoundryQueue, tmp_path: Path) -> None:
    async def go() -> None:
        clock = FakeTime()
        wakeup = Wakeup()
        q.on_event = wakeup.notify
        watcher = _watcher(q, tmp_path, Board(), wakeup, clock)
        assert watcher.start() is not None
        try:
            await _until(lambda: watcher.rounds == 1)
            q.record_event("ticket_done", "T-1")
            await _until(lambda: watcher.rounds == 2)  # the idle interval is 15 s: only the wake gets here
        finally:
            await watcher.stop()

    asyncio.run(go())


def test_two_events_within_a_second_give_one_round(q: FoundryQueue, tmp_path: Path) -> None:
    async def go() -> None:
        clock = FakeTime()
        wakeup = Wakeup()
        q.on_event = wakeup.notify

        async def sleep(seconds: float) -> None:
            clock.sleeps.append(seconds)
            q.record_event("second", None)  # arrives during the debounce
            clock.t += seconds

        service = SimpleNamespace(running=True, factory=SimpleNamespace(queue=q), repo=tmp_path, config=None)
        watcher = Watcher(service, Board(), repo_slug=REPO, host="pc", interval=15.0, enabled=True,
                          wakeup=wakeup, sleep=sleep, now=clock.now)
        assert watcher.start() is not None
        try:
            await _until(lambda: watcher.rounds == 1)
            q.record_event("first", None)
            await _until(lambda: watcher.rounds == 2)
            await asyncio.sleep(0.1)  # a leftover wake would start a third round by now
            assert watcher.rounds == 2 and len(clock.sleeps) == 1
        finally:
            await watcher.stop()

    asyncio.run(go())


# --------------------------------------------------------------------------- serve wiring


class SpyWatcher:
    def __init__(self, service: object, board: object = None, *, wakeup: Wakeup | None = None) -> None:
        self.wakeup = wakeup

    def start(self) -> None:
        return None


class OldWatcher:
    """A watcher from before ``wakeup``."""

    def __init__(self, service: object, board: object = None) -> None:
        pass

    def start(self) -> None:
        return None


def _service(tmp_path: Path, queue: object) -> Service:
    return Service(SimpleNamespace(board_bridge_mode="auto"), repo=tmp_path,
                   factory=SimpleNamespace(queue=queue), batch_board=object())


def test_service_connects_the_queue_to_the_watcher(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("foundry.board_dispatch.BatchWatcher", SpyWatcher)
    queue = SimpleNamespace(on_event=None)
    svc = _service(tmp_path, queue)
    svc._start_batches()
    assert isinstance(svc.batch_watcher.wakeup, Wakeup)
    assert queue.on_event == svc.batch_watcher.wakeup.notify


def test_service_reconnects_a_new_queue_after_a_restart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("foundry.board_dispatch.BatchWatcher", SpyWatcher)
    svc = _service(tmp_path, SimpleNamespace(on_event=None))
    svc._start_batches()
    fresh = SimpleNamespace(on_event=None)
    svc.factory = SimpleNamespace(queue=fresh)
    svc._start_batches()
    assert fresh.on_event == svc.batch_watcher.wakeup.notify


def test_service_copes_with_a_watcher_or_queue_without_the_hooks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("foundry.board_dispatch.BatchWatcher", OldWatcher)
    queue = SimpleNamespace(on_event=None)
    svc = _service(tmp_path, queue)
    svc._start_batches()
    assert isinstance(svc.batch_watcher, OldWatcher) and queue.on_event is None

    monkeypatch.setattr("foundry.board_dispatch.BatchWatcher", SpyWatcher)
    bare = SimpleNamespace()
    svc = _service(tmp_path, bare)
    svc._start_batches()
    assert not hasattr(bare, "on_event")
