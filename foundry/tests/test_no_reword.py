"""The Foreman hands builders the full order plus a one-line note on their part."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from foundry.config import FoundryConfig
from foundry.context_engine import ContextEngine, trim_objective
from foundry.schemas import MAX_OBJECTIVE_CHARS, MAX_PART_NOTE_CHARS, ContextSlice, SubtaskAllocation
from foundry.shop_foreman import ShopForeman, _join_part_notes

LONG_ORDER = " ".join(f"Sentence {i} of the order." for i in range(1, 9))


def _foreman(**attrs: Any) -> Any:
    """A stand-in ``self`` carrying only what the method under test reads."""
    return SimpleNamespace(config=FoundryConfig(min_subtask_lines=0), **attrs)


def _proposal(title: str, note: str, path: str) -> dict[str, Any]:
    return {
        "title": title,
        "part_note": note,
        "acceptance_criteria": [f"{title} works"],
        "allowed_files": [path],
        "independent": False,
        "estimated_lines": 200,
    }


def test_context_slice_keeps_every_sentence() -> None:
    assert ContextSlice(objective=LONG_ORDER).objective == LONG_ORDER


def test_context_slice_cuts_only_at_the_character_cap() -> None:
    sliced = ContextSlice(objective="x" * (MAX_OBJECTIVE_CHARS + 10))
    assert len(sliced.objective) == MAX_OBJECTIVE_CHARS


def test_context_slice_rejects_a_blank_objective() -> None:
    with pytest.raises(ValidationError):
        ContextSlice(objective="   ")


def test_trim_objective_keeps_the_whole_order_by_default() -> None:
    assert trim_objective(LONG_ORDER) == LONG_ORDER


def test_trim_objective_shortens_only_when_asked() -> None:
    assert trim_objective("One. Two. Three.", max_sentences=2) == "One. Two."


def test_trim_objective_applies_the_character_cap() -> None:
    assert len(trim_objective("word " * 5000)) <= MAX_OBJECTIVE_CHARS


def test_part_note_is_squeezed_onto_one_line() -> None:
    sub = SubtaskAllocation(subtask_id="T-1.1", title="t", part_note="the  first\n  part")
    assert sub.part_note == "the first part"


def test_part_note_is_capped() -> None:
    sub = SubtaskAllocation(subtask_id="T-1.1", title="t", part_note="x" * (MAX_PART_NOTE_CHARS + 50))
    assert len(sub.part_note) == MAX_PART_NOTE_CHARS


def test_none_part_note_means_no_note() -> None:
    sub = SubtaskAllocation.model_validate({"subtask_id": "T-1.1", "title": "t", "part_note": None})
    assert sub.part_note == ""


def test_join_part_notes_drops_blanks_and_repeats() -> None:
    assert _join_part_notes("a", "b") == "a; b"
    assert _join_part_notes("a", "a") == "a"
    assert _join_part_notes(None, "  ") == ""


def test_directive_keeps_gemma_part_notes_and_merges_surplus() -> None:
    proposals = [
        _proposal("One", "first", "a.py"),
        _proposal("Two", "second", "b.py"),
        _proposal("Three", "third", "a.py"),
    ]
    directive = ShopForeman._build_directive(
        _foreman(), "T-1", "alpha", "Title", "M", "why", ["a.py", "b.py"], ["done"], proposals
    )
    assert [s.part_note for s in directive.subtasks] == ["first; third", "second"]


def test_deterministic_split_writes_part_notes() -> None:
    directive = ShopForeman._build_directive(
        _foreman(), "T-1", "alpha", "Title", "M", "why", ["a.py", "b.py"], ["done"], None
    )
    assert [s.part_note for s in directive.subtasks] == ["Covers a.py", "Covers b.py"]


def test_single_builder_note_covers_the_whole_order() -> None:
    directive = ShopForeman._build_directive(
        _foreman(), "T-1", "alpha", "Title", "S", "why", ["a.py"], ["done"], None
    )
    assert directive.subtasks[0].part_note == "Covers the whole order"


def test_sizing_prompt_forbids_rewording() -> None:
    seen: list[dict[str, Any]] = []

    async def chat(messages: list[dict[str, Any]], **_kwargs: Any) -> None:
        seen.extend(messages)

    result = asyncio.run(
        ShopForeman._gemma_sizing(_foreman(_chat=chat), "T-1", "alpha", "Title", "Desc", ["a.py"], ["done"])
    )
    assert result is None
    assert "Never reword" in seen[0]["content"]
    assert "part_note" in seen[0]["content"]


def _objective_after_setup(objective: str, note: str) -> str:
    """The objective the engine builds a slice from, with its file work stubbed out."""
    engine: Any = SimpleNamespace(
        _collect=lambda _sub, text, _criteria, _files, _extra: ("Title", text, [], [], {}),
        _prepare=lambda *_args: ([], {}),
        expand_files=lambda files: list(files),
        _plan=lambda *_args: None,
    )
    sub = SubtaskAllocation(subtask_id="T-1.1", title="t", part_note=note)
    return ContextEngine._setup(engine, sub, objective, None, None, (), "", (), 1)[1]


def test_engine_keeps_the_full_order_and_adds_the_part_note() -> None:
    expected = f"{LONG_ORDER}\n\nThis subtask's part of the order: the tests"
    assert _objective_after_setup(LONG_ORDER, "the tests") == expected


def test_engine_skips_a_blank_part_note() -> None:
    assert _objective_after_setup(LONG_ORDER, "") == LONG_ORDER


def test_engine_skips_a_part_note_that_repeats_the_order() -> None:
    assert _objective_after_setup("One order.", "One order.") == "One order."


def test_character_cap_never_cuts_off_the_part_note() -> None:
    objective = _objective_after_setup("x" * MAX_OBJECTIVE_CHARS, "the tests")
    sliced = ContextSlice(objective=objective)
    assert len(sliced.objective) <= MAX_OBJECTIVE_CHARS
    assert sliced.objective.endswith("This subtask's part of the order: the tests")


def test_slice_without_engine_keeps_the_full_order() -> None:
    sliced = asyncio.run(ShopForeman.prune_context_slice(_foreman(context_engine=None), None, LONG_ORDER))
    assert sliced.objective == LONG_ORDER
