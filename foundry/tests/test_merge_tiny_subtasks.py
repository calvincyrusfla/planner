"""Tests for merging tiny subtasks and the ``min_subtask_lines`` setting."""

from __future__ import annotations

import copy
from typing import Any

import pytest
from pydantic import ValidationError

from foundry.config import FoundryConfig
from foundry.schemas import BUILDERS_FOR, MAX_BUILDERS
from foundry.shop_foreman import TINY_SUBTASK_MAX_LINES, ShopForeman, merge_tiny_subtasks


def _sub(
    title: str, lines: int | None, files: list[str] | None = None, *, independent: bool = False
) -> dict[str, Any]:
    return {
        "title": title,
        "acceptance_criteria": [f"{title} works"],
        "allowed_files": files if files is not None else [f"pkg/{title.lower()}.py"],
        "independent": independent,
        "estimated_lines": lines,
    }


def _foreman(**settings: Any) -> ShopForeman:
    no_queue: Any = None  # _build_directive never touches the queue
    return ShopForeman(None, no_queue, None, None, FoundryConfig(**settings))


def _xl_directive(foreman: ShopForeman, lines: int) -> Any:
    """Size an XL task from one equally big proposal per builder."""
    count = min(BUILDERS_FOR["XL"], MAX_BUILDERS, foreman.config.builders_per_team)
    proposals = [_sub(f"Part{k}", lines) for k in range(count)]
    return foreman._build_directive("F-900", "alpha", "Title", "XL", "sized", [], [], proposals)


def _xl_builders() -> int:
    return min(BUILDERS_FOR["XL"], MAX_BUILDERS, FoundryConfig().builders_per_team)


def test_tiny_subtask_joins_the_previous_one() -> None:
    subs = [_sub("A", 100), _sub("B", 5), _sub("C", 100)]

    merged, notes = merge_tiny_subtasks(subs)

    assert [s["title"] for s in merged] == ["A + B", "C"]
    assert merged[0]["allowed_files"] == ["pkg/a.py", "pkg/b.py"]
    assert merged[0]["acceptance_criteria"] == ["A works", "B works"]
    assert merged[0]["estimated_lines"] == 105
    assert notes == ["B into A"]


def test_tiny_first_subtask_absorbs_the_next_one() -> None:
    merged, notes = merge_tiny_subtasks([_sub("A", 5), _sub("B", 100)])

    assert [s["title"] for s in merged] == ["A + B"]
    assert merged[0]["estimated_lines"] == 105
    assert notes == ["B into A"]


def test_unknown_estimate_is_never_tiny() -> None:
    merged, notes = merge_tiny_subtasks([_sub("A", None), _sub("B", None)])

    assert [s["title"] for s in merged] == ["A", "B"]
    assert notes == []


def test_subtask_with_too_many_files_is_not_tiny() -> None:
    subs = [_sub("A", 100), _sub("B", 5, ["pkg/b.py", "pkg/c.py"])]

    merged, notes = merge_tiny_subtasks(subs)

    assert [s["title"] for s in merged] == ["A", "B"]
    assert notes == []


def test_threshold_is_exclusive() -> None:
    at_limit = [_sub("A", 100), _sub("B", TINY_SUBTASK_MAX_LINES)]
    below_limit = [_sub("A", 100), _sub("B", TINY_SUBTASK_MAX_LINES - 1)]

    assert len(merge_tiny_subtasks(at_limit)[0]) == 2
    assert len(merge_tiny_subtasks(below_limit)[0]) == 1


def test_merge_is_independent_only_when_both_sides_are() -> None:
    both = [_sub("A", 100, independent=True), _sub("B", 5, independent=True)]
    one = [_sub("A", 100, independent=True), _sub("B", 5, independent=False)]

    assert merge_tiny_subtasks(both)[0][0]["independent"] is True
    assert merge_tiny_subtasks(one)[0][0]["independent"] is False


def test_input_is_not_changed() -> None:
    subs = [_sub("A", 100), _sub("B", 5)]
    before = copy.deepcopy(subs)

    merge_tiny_subtasks(subs)

    assert subs == before


def test_config_default_matches_the_merge_default() -> None:
    assert FoundryConfig().min_subtask_lines == TINY_SUBTASK_MAX_LINES


def test_config_reads_the_environment() -> None:
    config = FoundryConfig.from_env({"FOUNDRY_MIN_SUBTASK_LINES": "45"})

    assert config.min_subtask_lines == 45


def test_config_rejects_a_negative_threshold() -> None:
    with pytest.raises(ValidationError):
        FoundryConfig(min_subtask_lines=-1)


def test_directive_keeps_subtasks_at_or_above_the_configured_threshold() -> None:
    directive = _xl_directive(_foreman(min_subtask_lines=40), 40)

    assert directive.allocated_builders == _xl_builders()
    assert len(directive.subtasks) == _xl_builders()


def test_directive_merges_below_the_configured_threshold() -> None:
    directive = _xl_directive(_foreman(min_subtask_lines=50), 40)

    assert directive.allocated_builders == 1
    assert len(directive.subtasks) == 1
    assert "under 50 lines" in directive.rationale
    assert "merged tiny subtasks" in (directive.override_reason or "")


def test_zero_threshold_disables_merging() -> None:
    directive = _xl_directive(_foreman(min_subtask_lines=0), 1)

    assert directive.allocated_builders == _xl_builders()
