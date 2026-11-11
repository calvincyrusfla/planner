"""The builder prompt layout: stable sections first, attempt-specific sections last."""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from foundry.schemas import CodeSnippet, ContextSlice
from foundry.worker_prompts import PROMPT_LAYOUT_VERSION, build_builder_prompt

MARK = "## This attempt"
SECTIONS = [
    "## Output contract",
    "## Your files",
    "## You may also touch",
    "## Read-only files",
    "## Context",
    "## Objective",
    "## Acceptance criteria",
    "## Order details",
    "## Watch out (advisory)",
    "## Where to look (advisory)",
    MARK,
    "## Floor Manager note (follow it)",
    "## Fix required (your previous attempt was sent back)",
]


def _assignment(**overrides: Any) -> Any:
    data: dict[str, Any] = {
        "task_id": "T-1",
        "team_id": "bravo",
        "builder_id": "bravo-1",
        "subtask_id": "T-1.1",
        "acceptance_criteria": ["it works"],
        "allowed_files": ["a.py", "b.py"],
        "read_files": ["c.py"],
        "read_only_excerpts": [{"path": "c.py", "start_line": 1, "end_line": 1, "text": "1| x\n"}],
        "context": ContextSlice(
            objective="Do it.",
            acceptance_criteria=["it works"],
            snippets=[CodeSnippet(path="a.py", start_line=1, end_line=1, text="def f(): ...")],
            source_line_count=1,
        ),
        "attempt": 0,
        "fix_hint": None,
        "ticket_description": None,
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def _prompt(assignment: dict[str, Any] | None = None, floor_manager_note: str | None = None) -> str:
    return build_builder_prompt(
        _assignment(**(assignment or {})),
        focus_files=["a.py"],
        floor_manager_note=floor_manager_note,
        order_description="The whole order.",
        pitfall_notes=["a.py:1: careful"],
        file_map=["a.py:1-2 f"],
    )


def test_layout_version() -> None:
    assert PROMPT_LAYOUT_VERSION == 2


def test_two_attempts_share_the_prefix_up_to_the_attempt_section() -> None:
    first = _prompt()
    second = _prompt({"attempt": 2, "fix_hint": "fix the thing"}, "use the helper")
    assert first != second
    assert first.index(MARK) == second.index(MARK)
    assert first[: first.index(MARK)] == second[: second.index(MARK)]
    assert "attempt 1 of" in first and "attempt 3 of" in second


def test_attempt_section_is_last_and_in_order() -> None:
    prompt = _prompt({"attempt": 1, "fix_hint": "fix it"}, "note")
    tail = prompt[prompt.index(MARK):]
    assert tail.index("You are builder bravo-1") < tail.index("## Floor Manager note")
    assert tail.index("## Floor Manager note") < tail.index("## Fix required")
    head = prompt[: prompt.index(MARK)]
    assert "bravo-1" not in head and "attempt 2 of" not in head


def test_every_section_is_present_exactly_once_in_order() -> None:
    prompt = _prompt({"attempt": 1, "fix_hint": "fix it"}, "note")
    positions = []
    for heading in SECTIONS:
        assert ("\n" + prompt).count(f"\n{heading}\n") == 1, heading
        positions.append(prompt.index(heading))
    assert positions == sorted(positions)
    assert prompt.startswith("## Output contract\n")


def test_single_files_section_when_no_focus_split() -> None:
    prompt = build_builder_prompt(_assignment())
    assert prompt.count("## Files you may modify") == 1
    assert prompt.index("## Files you may modify") < prompt.index("## Objective")
