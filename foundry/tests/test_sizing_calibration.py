"""Sizing calibration in the Shop Foreman: past outcomes as examples + one-step bias shift."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from foundry.config import FoundryConfig
from foundry.gemma_client import SimulatedGemmaClient
from foundry.orchestrator_queue import FoundryQueue
from foundry.shop_foreman import ShopForeman, calibration_shifts, similar_outcomes


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def outcome(task_id: str, predicted: str, actual: str, *, title: str = "Work", files: list[str] | None = None,
            attempts: int = 2, calls: int = 4, repo: str = "/repo") -> dict[str, Any]:
    return {"task_id": task_id, "repo": repo, "title": title, "files": files or [], "predicted": predicted,
            "actual": actual, "attempts": attempts, "claude_calls": calls, "final_state": "complete"}


class Provider:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.calls: list[tuple[Any, int]] = []

    def __call__(self, repo: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        self.calls.append((repo, limit))
        return [r for r in self.rows if repo is None or r["repo"] == repo][:limit]


@pytest.fixture
def queue(tmp_path: Path) -> FoundryQueue:
    return FoundryQueue(tmp_path / "foundry.db")


def foreman(queue: FoundryQueue, rows: list[dict[str, Any]] | None, gemma: Any = None) -> ShopForeman:
    provider = Provider(rows) if rows is not None else None
    return ShopForeman(gemma, queue, None, None, FoundryConfig(), outcomes_provider=provider)


M_TASK = {"task_id": "N-1", "team_id": "alpha", "title": "Small change", "description": "Two files.",
          "files": ["a.py", "b.py"], "repo_root": "/repo"}


def history_m_was_l(n: int = 8) -> list[dict[str, Any]]:
    return [outcome(f"P-{k}", "M", "L", title=f"Past {k}") for k in range(n)]


def test_shift_rule_needs_eight_outcomes_and_sixty_percent() -> None:
    assert calibration_shifts(history_m_was_l(7)) == {}
    rules = calibration_shifts(history_m_was_l(8))
    assert rules["M"]["to"] == "L" and rules["M"]["direction"] == "up" and rules["M"]["n"] == 8
    mixed = [outcome(f"P-{k}", "M", "L") for k in range(5)] + [outcome(f"Q-{k}", "M", "M") for k in range(5)]
    assert calibration_shifts(mixed) == {}  # 50 % bigger: no shift
    smaller = [outcome(f"P-{k}", "L", "S") for k in range(9)]
    assert calibration_shifts(smaller)["L"]["to"] == "M"  # one step only, even though actual was S
    assert calibration_shifts([outcome(f"P-{k}", "XL", "XL") for k in range(10)]) == {}
    assert calibration_shifts([outcome(f"P-{k}", "S", "S") for k in range(3)] + [
        outcome(f"Q-{k}", "XL", "L") for k in range(8)])["XL"]["to"] == "L"


def test_heuristic_result_is_calibrated(queue: FoundryQueue) -> None:
    f = foreman(queue, history_m_was_l())
    directive = run(f.size_task_scope(M_TASK))
    assert f.last_source["size_task_scope"] == "heuristic"
    assert directive.complexity == "L"
    assert "calibrated from 8 past tasks" in directive.rationale
    assert directive.allocated_builders == 2  # still capped by the two files


def test_gemma_result_is_calibrated_and_sees_examples(queue: FoundryQueue) -> None:
    rows = history_m_was_l() + [
        outcome("P-files", "M", "L", title="Unrelated", files=["a.py"], attempts=3, calls=9),
    ]
    rows.insert(0, outcome("P-other", "S", "S", title="Nothing shared", files=["z.py"]))
    reply = {"complexity": "M", "rationale": "Two modules.", "subtasks": [
        {"title": "A", "acceptance_criteria": ["a"], "allowed_files": ["a.py"]},
        {"title": "B", "acceptance_criteria": ["b"], "allowed_files": ["b.py"]},
    ]}
    gemma = SimulatedGemmaClient(lambda req: reply)
    f = foreman(queue, rows, gemma)
    task = dict(M_TASK, title="Past change")
    directive = run(f.size_task_scope(task))
    assert f.last_source["size_task_scope"] == "gemma"
    assert directive.complexity == "L" and directive.rationale.startswith("Two modules.")
    assert "calibrated from" in directive.rationale
    prompt = gemma.requests[0].messages[-1]["content"]
    # file overlap ranks first, then title overlap; at most five; unrelated rows left out
    assert "Past task Unrelated (files a.py): you predicted M, it actually took L (3 attempts, 9 Claude calls)" in prompt
    assert prompt.index("Past task Unrelated") < prompt.index("Past task Past 0")
    assert prompt.count("Past task ") == 5
    assert "Nothing shared" not in prompt


def test_similar_outcomes_skips_own_task_and_unrelated() -> None:
    rows = [outcome("N-1", "M", "L", title="Small change", files=["a.py"]),
            outcome("P-1", "S", "S", title="Other words"),
            outcome("P-2", "S", "M", title="Change the thing")]
    picked = similar_outcomes(rows, "Small change", ["a.py"], task_id="N-1")
    assert [o["task_id"] for o in picked] == ["P-2"]


def test_no_history_means_no_change(queue: FoundryQueue) -> None:
    f = foreman(queue, [])
    directive = run(f.size_task_scope(M_TASK))
    assert directive.complexity == "M" and "calibrated" not in directive.rationale
    assert f.calibration_summary() == {"n": 0, "accuracy_pct": None, "exact": 0, "under": 0, "over": 0,
                                       "shift_rules": []}


def test_broken_provider_never_blocks_sizing(queue: FoundryQueue) -> None:
    def boom(repo: str | None = None, limit: int = 50) -> list[Any]:
        raise RuntimeError("db gone")

    f = ShopForeman(None, queue, None, None, FoundryConfig(), outcomes_provider=boom)
    assert run(f.size_task_scope(M_TASK)).complexity == "M"
    assert f.calibration_summary()["n"] == 0


def test_provider_gets_task_repo(queue: FoundryQueue) -> None:
    f = foreman(queue, history_m_was_l())
    run(f.size_task_scope(M_TASK))
    assert f.outcomes_provider.calls[0] == ("/repo", 50)  # type: ignore[union-attr]
    other = foreman(queue, history_m_was_l())
    assert run(other.size_task_scope(dict(M_TASK, repo_root="/elsewhere"))).complexity == "M"


def test_calibration_summary_counts(queue: FoundryQueue) -> None:
    rows = history_m_was_l() + [outcome("E-1", "S", "S"), outcome("O-1", "L", "M")]
    f = ShopForeman(None, queue, None, None, FoundryConfig(), outcomes_provider=Provider(rows))
    summary = f.calibration_summary()
    assert summary["n"] == 10 and summary["exact"] == 1 and summary["under"] == 8 and summary["over"] == 1
    assert summary["accuracy_pct"] == 10
    assert [r["predicted"] for r in summary["shift_rules"]] == ["M"]
    assert summary["shift_rules"][0]["to"] == "L"


def test_actual_computed_when_row_lacks_it(queue: FoundryQueue) -> None:
    row = outcome("P-1", "S", "", attempts=5, calls=12)
    row.pop("actual")
    f = ShopForeman(None, queue, None, None, FoundryConfig(), outcomes_provider=Provider([row]))
    assert f.calibration_summary()["under"] == 1


def test_queue_sizing_outcomes_used_by_default(queue: FoundryQueue) -> None:
    if not hasattr(queue, "sizing_outcomes"):
        pytest.skip("queue has no sizing_outcomes yet")
    f = ShopForeman(None, queue, None, None, FoundryConfig())
    assert f.calibration_summary()["n"] == 0


# ---------------------------------------------------------------- feedback loop (review round 1)

M_REPLY = {"complexity": "M", "rationale": "Two modules.", "subtasks": [
    {"title": "A", "acceptance_criteria": ["a"], "allowed_files": ["a.py"]},
    {"title": "B", "acceptance_criteria": ["b"], "allowed_files": ["b.py"]},
]}


def _size_and_record(f: ShopForeman, rows: list[dict[str, Any]], k: int, actual: str) -> str:
    """Size one M-ish task, then record its outcome the way the floor manager does (newest first)."""
    directive = run(f.size_task_scope(dict(M_TASK, task_id=f"R-{k}")))
    row = outcome(f"R-{k}", directive.complexity, actual)
    row["raw_predicted"] = f.last_raw_complexity
    rows.insert(0, row)
    return directive.complexity


def test_calibration_does_not_learn_from_its_own_shift(queue: FoundryQueue) -> None:
    rows = history_m_was_l()
    f = foreman(queue, None, SimulatedGemmaClient(lambda req: M_REPLY))
    f.outcomes_provider = Provider(rows)
    applied = [_size_and_record(f, rows, k, "L") for k in range(60)]
    assert applied == ["L"] * 60  # no oscillation: the rule holds while M tasks keep taking L
    assert f.last_raw_complexity == "M"
    assert calibration_shifts(rows)["M"]["to"] == "L"


def test_calibration_unlearns_when_evidence_changes(queue: FoundryQueue) -> None:
    rows = history_m_was_l(20)
    f = foreman(queue, None, SimulatedGemmaClient(lambda req: M_REPLY))
    f.outcomes_provider = Provider(rows)
    applied = [_size_and_record(f, rows, k, "M") for k in range(20)]
    assert applied[0] == "L" and applied[-1] == "M"
    assert "M" not in calibration_shifts(rows)


def test_heuristic_sets_raw_complexity(queue: FoundryQueue) -> None:
    f = foreman(queue, history_m_was_l())
    assert run(f.size_task_scope(M_TASK)).complexity == "L"
    assert f.last_raw_complexity == "M"


def test_old_rows_without_raw_and_excluded_rows() -> None:
    rows = history_m_was_l()  # no raw_predicted: falls back to predicted
    assert calibration_shifts(rows)["M"]["to"] == "L"
    rows[0]["excluded_from_calibration"] = 1
    assert calibration_shifts(rows) == {}  # only 7 usable rows left
    rows[1]["raw_predicted"] = "S"  # bucketed by raw, not applied
    assert "S" not in calibration_shifts(rows)


def test_calibration_summary_takes_repo(queue: FoundryQueue) -> None:
    rows = history_m_was_l() + [outcome("X-1", "S", "S", repo="/other")]
    provider = Provider(rows)
    f = ShopForeman(None, queue, None, None, FoundryConfig(), outcomes_provider=provider)
    assert f.calibration_summary()["n"] == 9
    scoped = f.calibration_summary("/other")
    assert scoped["n"] == 1 and scoped["shift_rules"] == []
    assert provider.calls[-1] == ("/other", 50)
