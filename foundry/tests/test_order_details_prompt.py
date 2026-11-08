"""The prompts quote the order and, for multi-ticket tasks, the ticket's own part."""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from foundry.builder_worker import (
    MAX_ORDER_DETAILS_CHARS,
    BuilderWorker,
    build_builder_prompt,
    build_reviewer_prompt,
)
from foundry.schemas import SubtaskAllocation

ORDER = "Build the whole export feature."
PART = "Only add the CSV writer."


def _assignment(part: str | None) -> Any:
    context = SimpleNamespace(objective="Do it", acceptance_criteria=[], snippets=[])
    return SimpleNamespace(
        task_id="T-1",
        team_id="alpha",
        builder_id="alpha-1",
        subtask_id="T-1.2",
        acceptance_criteria=["works"],
        allowed_files=["a.py"],
        context=context,
        attempt=0,
        fix_hint=None,
        ticket_description=part,
    )


def _reviewer_prompt(part: str | None) -> str:
    submission: Any = SimpleNamespace(task_id="T-1", subtask_id="T-1.2", builder_id="alpha-1", diff="--- a/a.py\n")
    return build_reviewer_prompt(submission, ["works"], [], order_description=ORDER, part_description=part)


def _worker(queue: Any) -> Any:
    worker: Any = SimpleNamespace(queue=queue)
    worker._find_subtask = lambda ticket: BuilderWorker._find_subtask(worker, ticket)
    return worker


def test_builder_prompt_shows_the_tickets_own_description() -> None:
    prompt = build_builder_prompt(_assignment(PART), order_description=ORDER)
    assert ORDER in prompt
    assert prompt.index(ORDER) < prompt.index("### This part") < prompt.index(PART)


def test_reviewer_prompt_shows_the_tickets_own_description() -> None:
    prompt = _reviewer_prompt(PART)
    assert prompt.index(ORDER) < prompt.index("### This part") < prompt.index(PART)


def test_reviewer_prompt_contains_the_order_details() -> None:
    prompt = _reviewer_prompt(None)
    assert ORDER in prompt
    assert "## Order details" in prompt
    assert prompt.index("## Acceptance criteria") < prompt.index("## Order details") < prompt.index("## Unified diff")


def test_part_repeating_the_order_is_not_shown() -> None:
    assert "### This part" not in build_builder_prompt(_assignment(f" {ORDER}\n"), order_description=ORDER)
    assert "### This part" not in _reviewer_prompt(ORDER)


def test_no_part_means_no_heading() -> None:
    assert "### This part" not in build_builder_prompt(_assignment(None), order_description=ORDER)
    assert "### This part" not in _reviewer_prompt(None)


def test_part_without_an_order_description_is_still_shown() -> None:
    prompt = build_builder_prompt(_assignment(PART))
    assert "## Order details" in prompt
    assert PART in prompt
    assert "original work order" not in prompt


def test_long_part_is_capped() -> None:
    prompt = build_builder_prompt(_assignment("x" * (MAX_ORDER_DETAILS_CHARS + 5)), order_description=ORDER)
    assert "[... ticket description truncated: 5 more characters]" in prompt
    assert "x" * (MAX_ORDER_DETAILS_CHARS + 1) not in prompt


def test_description_comes_from_the_ticket_first() -> None:
    ticket = SimpleNamespace(task_id="T-1", subtask_id="T-1.2", description=" Own text ")
    assert BuilderWorker._ticket_description(_worker(SimpleNamespace()), ticket) == "Own text"


def test_description_falls_back_to_the_allocation() -> None:
    sub = SubtaskAllocation(subtask_id="T-1.2", title="t", description=PART)
    queue = SimpleNamespace(get_task=lambda _id: SimpleNamespace(subtasks=[sub]))
    ticket = SimpleNamespace(task_id="T-1", subtask_id="T-1.2")
    assert BuilderWorker._ticket_description(_worker(queue), ticket) == PART


def test_description_lookup_failure_means_none() -> None:
    def boom(_id: str) -> Any:
        raise KeyError(_id)

    queue = SimpleNamespace(get_task=boom)
    ticket = SimpleNamespace(task_id="T-1", subtask_id="T-1.2")
    assert BuilderWorker._ticket_description(_worker(queue), ticket) is None
