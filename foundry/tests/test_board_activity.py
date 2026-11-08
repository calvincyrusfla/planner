"""Task progress percent reported to the board on ticket state changes."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from foundry.builder_worker import TeamPool, _complete_ticket, _WorkerBase
from foundry.floor_manager import FloorManager

TASK = "F-001"


class _Bridge:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.error = error

    def progress(self, task_id: str, message: str, **kwargs: Any) -> None:
        if self.error is not None:
            raise self.error
        self.calls.append({"task_id": task_id, "message": message, **kwargs})


class _Worker(_WorkerBase):
    agent_id = "alpha-1"
    role = "builder"

    def _still_owned(self, *args: Any, **kwargs: Any) -> bool:
        return True

    def _release_later(self, *args: Any, **kwargs: Any) -> list[str]:
        return []

    def _tm(self, *args: Any, **kwargs: Any) -> None:
        return None

    def _event(self, *args: Any, **kwargs: Any) -> None:
        return None


def _manager(statuses: list[str], bridge: Any = None, directive: bool = True) -> Any:
    manager: Any = FloorManager.__new__(FloorManager)
    manager.queue = SimpleNamespace(tickets_for_task=lambda _task: [SimpleNamespace(status=s) for s in statuses])
    manager.bridge = bridge if bridge is not None else _Bridge()
    manager.directives = {TASK: SimpleNamespace(team_id="alpha")} if directive else {}
    manager._completed = set()
    return manager


def _worker(queue: Any) -> _Worker:
    client: Any = None
    return _Worker("alpha", queue, client, None, None)


def _recording_hook() -> tuple[list[tuple[str, Any]], Any]:
    seen: list[tuple[str, Any]] = []

    def hook(task_id: str, *, team_id: Any = None) -> None:
        seen.append((task_id, team_id))

    return seen, hook


def test_percent_is_rounded_share_of_approved_tickets() -> None:
    manager = _manager(["done", "open", "claimed"])
    manager.report_task_progress(TASK)
    call = manager.bridge.calls[-1]
    assert call["pct"] == 33
    assert call["team_id"] == "alpha"


def test_percent_is_capped_until_the_patch_is_written() -> None:
    manager = _manager(["done", "done"])
    manager.report_task_progress(TASK)
    assert manager.bridge.calls[-1]["pct"] == 95


def test_percent_is_100_once_finished() -> None:
    manager = _manager(["done", "done"])
    manager._completed.add(TASK)
    manager.report_task_progress(TASK)
    assert manager.bridge.calls[-1]["pct"] == 100


def test_team_of_the_caller_is_used_without_a_directive() -> None:
    manager = _manager(["open"], directive=False)
    manager.report_task_progress(TASK, team_id="bravo")
    assert manager.bridge.calls[-1]["team_id"] == "bravo"


def test_no_tickets_reports_nothing() -> None:
    manager = _manager([])
    manager.report_task_progress(TASK)
    assert manager.bridge.calls == []


def test_board_error_is_swallowed() -> None:
    manager = _manager(["done"], bridge=_Bridge(RuntimeError("board down")))
    manager.report_task_progress(TASK)


def test_queue_error_is_swallowed() -> None:
    manager = _manager(["done"])

    def broken(_task: str) -> list[Any]:
        raise RuntimeError("database locked")

    manager.queue = SimpleNamespace(tickets_for_task=broken)
    manager.report_task_progress(TASK)
    assert manager.bridge.calls == []


def test_hook_failure_does_not_reach_the_worker() -> None:
    worker = _worker(SimpleNamespace())

    def broken(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("boom")

    worker.on_ticket_change = broken
    worker._ticket_changed(TASK)


def test_completing_a_ticket_reports_the_change() -> None:
    worker = _worker(SimpleNamespace(complete_ticket=lambda _ticket: False))
    seen, worker.on_ticket_change = _recording_hook()
    assert asyncio.run(_complete_ticket(worker, "t-1", TASK, None)) is False
    assert seen == [(TASK, "alpha")]


def test_reopening_a_ticket_reports_the_change() -> None:
    worker = _worker(SimpleNamespace(reopen_ticket=lambda _ticket, _hint: True))
    seen, worker.on_ticket_change = _recording_hook()
    ticket = SimpleNamespace(task_id=TASK, ticket_id="t-1", attempt=0)
    assert worker._send_back(ticket, "fix it", reason="revise") == "retry"
    assert seen == [(TASK, "alpha")]


def test_pool_points_every_worker_at_the_hook() -> None:
    client: Any = None
    pool = TeamPool("alpha", 2, queue=SimpleNamespace(), client=client, limiter=None, telemetry=None)
    _seen, hook = _recording_hook()
    pool.set_on_ticket_change(hook)
    assert all(worker.on_ticket_change is hook for worker in [*pool.builders, pool.reviewer])
    assert pool._make_reviewer("alpha-2").on_ticket_change is hook
