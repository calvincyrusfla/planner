"""plan_checklist and the reviewer prompt's plan checklist section."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from foundry.builder_worker import REVIEWER_CONTRACT, ReviewerWorker, build_reviewer_prompt, plan_checklist

ORDER = (
    "Register `answerHelp` so a ticket goes to 'needs_help', in `foundry/board_dispatch.py`. "
    'Repeat: `answerHelp` and "needs_help". '
    "Don't touch 'the' or `true`."
)
ITEMS = ["answerHelp", "needs_help", "foundry/board_dispatch.py"]
LAST_PART = "This is the last part of the task:"


def _prompt(order: str | None, *, last_part: bool = False) -> str:
    submission: Any = SimpleNamespace(task_id="T-1", subtask_id="T-1.1", builder_id="alpha-1", diff="--- a/a.py\n")
    return build_reviewer_prompt(submission, ["works"], [], order_description=order, last_part=last_part)


def test_extracts_the_named_items_once_in_order() -> None:
    assert plan_checklist(ORDER) == ITEMS


def test_respects_the_limit() -> None:
    order = " ".join(f"`item{i}`" for i in range(30))
    assert len(plan_checklist(order)) == 25
    assert plan_checklist(order, limit=2) == ["item0", "item1"]
    assert plan_checklist(order, limit=0) == []


@pytest.mark.parametrize("word", ["the", "and", "true", "false", "none", "null", "None", "TRUE"])
def test_skips_stop_words(word: str) -> None:
    assert plan_checklist(f"`{word}` '{word}' \"{word}\" `kept`") == ["kept"]


@pytest.mark.parametrize("order", [None, "", "   ", "no named items here"])
def test_nothing_named_gives_an_empty_list(order: str | None) -> None:
    assert plan_checklist(order) == []


def test_quoted_text_that_is_not_a_name_is_skipped() -> None:
    assert plan_checklist("'two words' \"9lives\" 'x' the builder's and reviewer's work") == []


def test_backticked_span_is_kept_whole() -> None:
    assert plan_checklist("Add `foo(a, b) -> int` now") == ["foo(a, b) -> int"]


def test_prompt_lists_the_items_after_the_order_details() -> None:
    prompt = _prompt(ORDER)
    assert prompt.index("## Order details") < prompt.index("## Plan checklist") < prompt.index("## Deterministic gates")
    section = prompt[prompt.index("## Plan checklist"):prompt.index("## Deterministic gates")]
    assert "reply REVISE with a directive naming it" in section
    assert [line[2:] for line in section.splitlines() if line.startswith("- ")] == ITEMS


@pytest.mark.parametrize("order", [None, "", "nothing named in this order"])
def test_no_section_without_items(order: str | None) -> None:
    for last_part in (False, True):
        prompt = _prompt(order, last_part=last_part)
        assert "## Plan checklist" not in prompt
        assert LAST_PART not in prompt


def test_last_part_line_only_when_asked() -> None:
    assert LAST_PART not in _prompt(ORDER)
    assert LAST_PART in _prompt(ORDER, last_part=True)


def test_contract_requires_the_plan_checklist() -> None:
    sentence = "Approve only when the acceptance criteria and every plan-checklist item this part is responsible for are met."
    assert sentence in REVIEWER_CONTRACT
    assert "every acceptance criterion is met" in REVIEWER_CONTRACT


def _ticket(ticket_id: str, status: str) -> Any:
    return SimpleNamespace(ticket_id=ticket_id, status=status)


def _queue(*tickets: Any) -> Any:
    return SimpleNamespace(tickets_for_task=lambda _task_id: list(tickets))


def _item(ticket_id: str = "t2") -> Any:
    assignment = SimpleNamespace(task_id="T-1", acceptance_criteria=[], attempt=0, ticket_description=None)
    return SimpleNamespace(
        ticket_id=ticket_id,
        ticket=SimpleNamespace(),
        assignment=assignment,
        submission=None,
        gate_results=[],
        pre_review_notes=None,
        cache_meta=None,
    )


def _is_last(queue: Any, item: Any | None = None) -> bool:
    worker: Any = SimpleNamespace(queue=queue)
    return ReviewerWorker._is_last_ticket(worker, item or _item())


@pytest.mark.parametrize(
    ("tickets", "expected"),
    [
        ([_ticket("t2", "claimed")], True),
        ([_ticket("t1", "open"), _ticket("t2", "claimed")], True),
        ([_ticket("t2", "claimed"), _ticket("t3", "done")], True),
        ([_ticket("t2", "claimed"), _ticket("t3", "open")], False),
        ([_ticket("t2", "claimed"), _ticket("t3", "claimed")], False),
        ([_ticket("t2", "claimed"), _ticket("t3", "awaiting_help")], False),
        ([_ticket("t2", "claimed"), _ticket("t3", "done"), _ticket("t4", "open")], False),
    ],
)
def test_last_ticket_means_no_later_unfinished_ticket(tickets: list[Any], expected: bool) -> None:
    assert _is_last(_queue(*tickets)) is expected


def test_last_ticket_unknown_is_false() -> None:
    def boom(_task_id: str) -> Any:
        raise KeyError(_task_id)

    assert _is_last(SimpleNamespace()) is False
    assert _is_last(SimpleNamespace(tickets_for_task=boom)) is False
    assert _is_last(_queue(_ticket("other", "done"))) is False


class _PromptBuilt(Exception):
    """Stops the review once its prompt arguments are known."""


@pytest.mark.parametrize(
    ("tickets", "expected"),
    [
        ([_ticket("t2", "claimed")], True),
        ([_ticket("t2", "claimed"), _ticket("t3", "open")], False),
    ],
)
def test_reviewer_passes_last_part(monkeypatch: pytest.MonkeyPatch, tickets: list[Any], expected: bool) -> None:
    seen: dict[str, Any] = {}

    def capture(*_args: Any, **kwargs: Any) -> str:
        seen.update(kwargs)
        raise _PromptBuilt

    monkeypatch.setattr("foundry.builder_worker.build_reviewer_prompt", capture)
    worker: Any = SimpleNamespace(
        queue=_queue(*tickets),
        agent_id="alpha-6",
        max_retries=3,
        _task_state=lambda _task_id: "building",
        _still_owned=lambda _ticket: True,
        _tm=lambda *_args, **_kwargs: None,
        _ticket_changed=lambda _task_id: None,
    )
    worker._is_last_ticket = lambda item: ReviewerWorker._is_last_ticket(worker, item)
    with pytest.raises(_PromptBuilt):
        asyncio.run(ReviewerWorker._review_item(worker, _item()))
    assert seen["last_part"] is expected
