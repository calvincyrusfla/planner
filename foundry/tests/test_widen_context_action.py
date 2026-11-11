"""WIDEN_CONTEXT is a stable action name, and an escalation that prescribes it is only logged."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from foundry.floor_manager import FloorManager
from foundry.schemas import ForemanEscalation, PrescribedAction


class _Queue:
    def __init__(self) -> None:
        self.writes: list[tuple[str, int]] = []

    def get_task(self, task_id: str) -> dict[str, Any]:
        return {"task_id": task_id, "state": "active"}

    def task_context_level(self, task_id: str) -> int:
        return 1

    def set_task_context_level(self, task_id: str, level: int) -> None:
        self.writes.append((task_id, level))


def test_action_value_is_stable() -> None:
    assert PrescribedAction("WIDEN_CONTEXT") is PrescribedAction.WIDEN_CONTEXT


def test_apply_escalation_logs_once_per_alert_without_widening() -> None:
    queue = _Queue()
    manager: Any = FloorManager.__new__(FloorManager)
    notes: list[Any] = []
    manager.queue = queue
    manager.bridge = SimpleNamespace(progress=lambda *a: notes.append(a), escalation=lambda _: None)
    manager.action_log = []
    manager.save = lambda: None
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
    out = asyncio.run(manager.apply_escalation(alert))
    assert asyncio.run(manager.apply_escalation(alert)) is out
    assert out.applied is False and out.action == "WIDEN_CONTEXT"
    assert queue.writes == [] and notes == []
    assert logged == [{}]
