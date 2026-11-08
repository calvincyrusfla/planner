"""``_context_for`` selects edit targets from both the ticket's and the work order's text."""

from __future__ import annotations

import asyncio
from typing import Any

from foundry.builder_worker import BuilderWorker
from foundry.schemas import ContextSlice, SubtaskAllocation


class _Queue:
    """Only what ``_context_for`` reads: the task's work order."""

    def __init__(self, description: str) -> None:
        self.description = description

    def get_task(self, task_id: str) -> dict[str, str]:
        return {"task_id": task_id, "title": "Work order", "description": self.description}


class _Engine:
    """Deterministic engine that records the target-selection text it is given."""

    def __init__(self) -> None:
        self.calls: list[str | None] = []

    def build_slice(
        self,
        subtask: SubtaskAllocation,
        objective: str,
        *,
        acceptance_criteria: list[str] | None = None,
        allowed_files: list[str] | None = None,
        level: int = 1,
        description: str | None = None,
    ) -> ContextSlice:
        self.calls.append(description)
        return ContextSlice(objective=objective, acceptance_criteria=acceptance_criteria or [])


def _selection_text(ticket_description: str, work_order_description: str) -> str | None:
    """The ``description`` the context engine receives for these two texts."""
    engine = _Engine()
    client: Any = None
    worker = BuilderWorker("charlie", 1, _Queue(work_order_description), client, None, None, engine, None, None, None)
    ticket: dict[str, Any] = {
        "ticket_id": "K-1",
        "task_id": "T-1",
        "subtask_id": "T-1.1",
        "description": ticket_description,
        "attempt": 0,
    }
    info: dict[str, Any] = {
        "title": "Select targets",
        "acceptance_criteria": ["Targets are selected"],
        "allowed_files": ["foundry/example.py"],
        "focus_files": ["foundry/example.py"],
    }
    asyncio.run(worker._context_for(ticket, info))
    assert len(engine.calls) == 1
    return engine.calls[0]


def test_ticket_and_work_order_text_both_select_targets() -> None:
    assert _selection_text("Change `alpha`.", "Also see `beta`.") == "Change `alpha`.\nAlso see `beta`."


def test_identical_texts_are_passed_once() -> None:
    assert _selection_text("Change `alpha`.", "Change `alpha`.") == "Change `alpha`."


def test_work_order_text_alone_selects_targets() -> None:
    assert _selection_text("", "Also see `beta`.") == "Also see `beta`."


def test_no_text_sends_no_description() -> None:
    assert _selection_text("", "") is None
