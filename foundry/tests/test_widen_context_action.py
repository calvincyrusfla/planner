"""WIDEN_CONTEXT is refused once a task is at the widest context level."""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from foundry.floor_manager import FloorManager
from foundry.orchestrator_queue import FoundryQueue
from foundry.schemas import ForemanEscalation, PrescribedAction


class _Queue:
    MAX_CONTEXT_LEVEL = FoundryQueue.MAX_CONTEXT_LEVEL

    def __init__(self, level: int) -> None:
        self.level = level
        self.writes: list[tuple[str, int]] = []

    def get_task(self, task_id: str) -> dict[str, Any]:
        return {"task_id": task_id, "state": "active"}

    def task_context_level(self, task_id: str) -> int:
        return self.level

    def set_task_context_level(self, task_id: str, level: int) -> None:
        self.writes.append((task_id, level))
        self.level = level


class _Bridge:
    def __init__(self) -> None:
        self.notes: list[tuple[str, str]] = []

    def progress(self, task_id: str, note: str) -> None:
        self.notes.append((task_id, note))


def _widen(level: int) -> tuple[tuple[Any, ...], _Queue, _Bridge]:
    manager = FloorManager.__new__(FloorManager)
    queue, bridge = _Queue(level), _Bridge()
    manager.queue = queue
    manager.bridge = bridge
    alert: Any = SimpleNamespace(target_entity=SimpleNamespace(task_id="F-001"))
    return asyncio.run(manager._widen_context(alert)), queue, bridge


def test_not_applied_at_level_3() -> None:
    result, queue, bridge = _widen(3)
    assert result[0] is False
    assert "level 3" in result[1]
    assert queue.level == 3
    assert queue.writes == []
    assert bridge.notes == []


def test_applied_below_level_3() -> None:
    result, queue, _ = _widen(2)
    assert result[0] is True
    assert queue.writes == [("F-001", 3)]
    assert result[3] == {"task_id": "F-001", "level": 2, "widened_to": 3}


def _widened_queue(path: Path) -> FoundryQueue:
    queue = FoundryQueue(path / "queue.db")
    queue.add_task("F-001", "title", "", "alpha")
    queue.set_task_context_level("F-001", 3)
    return queue


def _undo(queue: FoundryQueue) -> tuple[tuple[bool, str], _Bridge]:
    manager = FloorManager.__new__(FloorManager)
    bridge = _Bridge()
    manager.queue = queue
    manager.bridge = bridge
    entry = {"before": {"task_id": "F-001", "level": 2, "widened_to": 3}}
    return manager._undo_widen(entry), bridge


def test_undo_restores_previous_level(tmp_path: Path) -> None:
    with _widened_queue(tmp_path) as queue:
        (ok, detail), bridge = _undo(queue)
        assert ok is True
        assert "level 2" in detail
        assert queue.task_context_level("F-001") == 2
        assert len(bridge.notes) == 1


def test_undo_refused_when_level_changed(tmp_path: Path) -> None:
    with _widened_queue(tmp_path) as queue:
        queue.set_task_context_level("F-001", 1)
        (ok, detail), bridge = _undo(queue)
        assert ok is False
        assert "changed" in detail
        assert queue.task_context_level("F-001") == 1
        assert bridge.notes == []


@pytest.mark.parametrize("state", ["complete", "quarantined", "failed"])
def test_undo_refused_when_task_finished(tmp_path: Path, state: str) -> None:
    with _widened_queue(tmp_path) as queue:
        with queue.transaction() as conn:
            conn.execute("UPDATE tasks SET state=? WHERE task_id=?", (state, "F-001"))
        (ok, detail), bridge = _undo(queue)
        assert ok is False
        assert state in detail
        assert queue.task_context_level("F-001") == 3
        assert bridge.notes == []


def test_undo_refused_for_unknown_task(tmp_path: Path) -> None:
    with FoundryQueue(tmp_path / "queue.db") as queue:
        (ok, detail), _ = _undo(queue)
        assert ok is False
        assert "no longer exists" in detail


def _manager(queue: Any) -> tuple[Any, _Bridge]:
    manager: Any = FloorManager.__new__(FloorManager)
    bridge = _Bridge()
    manager.queue = queue
    manager.bridge = bridge
    manager.action_log = []
    manager._save_actions = lambda: None
    manager.save = lambda: None
    return manager, bridge


def _alert(task_id: str | None = "F-001") -> Any:
    return SimpleNamespace(target_entity=SimpleNamespace(task_id=task_id))


def _log_widen(manager: Any, before: dict[str, Any]) -> str:
    action_id = "A-1"
    manager.action_log.append(
        {"action_id": action_id, "action": "WIDEN_CONTEXT", "undoable": True, "before": before}
    )
    return action_id


def test_action_value_is_stable() -> None:
    assert PrescribedAction("WIDEN_CONTEXT") is PrescribedAction.WIDEN_CONTEXT


def test_widen_reports_new_level_on_board() -> None:
    result, queue, bridge = _widen(1)
    assert result[0] is True
    assert result[2] == {"level": 2}
    assert queue.level == 2
    assert bridge.notes == [("F-001", "[Foundry] F-001 context widened from level 1 to 2")]


def test_widen_refused_without_task() -> None:
    queue = _Queue(1)
    manager, bridge = _manager(queue)
    result = asyncio.run(manager._widen_context(_alert(None)))
    assert result[0] is False
    assert queue.writes == []
    assert bridge.notes == []


def test_widen_refused_when_task_finished(tmp_path: Path) -> None:
    with _widened_queue(tmp_path) as queue:
        queue.set_task_context_level("F-001", 2)
        with queue.transaction() as conn:
            conn.execute("UPDATE tasks SET state=? WHERE task_id=?", ("complete", "F-001"))
        manager, bridge = _manager(queue)
        result = asyncio.run(manager._widen_context(_alert()))
        assert result[0] is False
        assert "complete" in result[1]
        assert queue.task_context_level("F-001") == 2
        assert bridge.notes == []


def test_apply_escalation_widens_once_per_alert() -> None:
    queue = _Queue(2)
    manager, _ = _manager(queue)
    manager.bridge = SimpleNamespace(progress=lambda *_: None, escalation=lambda _: None)
    manager.applied_alerts = {}
    manager.outcomes = []
    manager._stored_outcome = lambda _: None
    logged: list[dict[str, Any]] = []
    manager._log_action = lambda _alert, _outcome, before: logged.append(before)
    alert = ForemanEscalation.model_construct(
        alert_id="ESC-1",
        prescribed_action=PrescribedAction.WIDEN_CONTEXT,
        target_entity=SimpleNamespace(task_id="F-001"),
    )
    asyncio.run(manager.apply_escalation(alert))
    asyncio.run(manager.apply_escalation(alert))
    assert queue.writes == [("F-001", 3)]
    assert logged == [{"task_id": "F-001", "level": 2, "widened_to": 3}]


def test_undo_action_round_trip(tmp_path: Path) -> None:
    with FoundryQueue(tmp_path / "queue.db") as queue:
        queue.add_task("F-001", "title", "", "alpha")
        manager, bridge = _manager(queue)
        result = asyncio.run(manager._widen_context(_alert()))
        assert queue.task_context_level("F-001") == 2
        action_id = _log_widen(manager, result[3])

        undone = manager.undo_action(action_id)
        assert undone["ok"] is True
        assert queue.task_context_level("F-001") == 1
        assert len(bridge.notes) == 2

        again = manager.undo_action(action_id)
        assert again["ok"] is True
        assert again["already_undone"] is True
        assert len(bridge.notes) == 2


def test_undo_action_reports_refusal(tmp_path: Path) -> None:
    with _widened_queue(tmp_path) as queue:
        queue.set_task_context_level("F-001", 1)
        manager, _ = _manager(queue)
        action_id = _log_widen(manager, {"task_id": "F-001", "level": 2, "widened_to": 3})

        undone = manager.undo_action(action_id)
        assert undone["ok"] is False
        assert "changed" in undone["reason"]
        assert "undone_at" not in manager.action_log[0]
        assert queue.task_context_level("F-001") == 1
