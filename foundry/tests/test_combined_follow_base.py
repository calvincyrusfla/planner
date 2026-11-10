"""Tests for foundry.combined_check: open Foundry PRs follow their base or are flagged."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from foundry import combined_check
from foundry.combined_check import COMMENT_MARKER, CombinedChecker, CombinedResult, open_foundry_prs

CONFLICT_TEXT = "Conflicts with the latest main: this piece needs a rebuild on the new base."


def _pr(number: int, mergeable: str = "MERGEABLE", status: str = "CLEAN", head: str | None = None,
        base: str = "main") -> dict[str, Any]:
    return {"number": number, "title": f"pr {number}", "headRefName": f"foundry/F-{number}",
            "headRefOid": head or f"{number:040d}", "baseRefName": base, "mergeable": mergeable,
            "mergeStateStatus": status}


class FakeRunner:
    """Stands in for git and gh: records every command, each update and each comment."""

    def __init__(self, prs: list[dict[str, Any]]) -> None:
        self.prs = prs
        self.calls: list[list[str]] = []
        self.updates: list[int] = []
        self.comments: list[tuple[int, str]] = []
        self.update_rc = 0
        self.comment_rc = 0

    async def __call__(self, argv: Any, *, cwd: Any = None, timeout: Any = None,
                       env: Any = None) -> tuple[int, str, str]:
        args = list(argv)
        self.calls.append(args)
        if args[:2] == ["git", "fetch"]:
            return 0, "", ""
        if args[:2] == ["git", "rev-parse"]:
            return 0, "a" * 40 + "\n", ""
        if args[:3] == ["gh", "pr", "list"]:
            return 0, json.dumps(self.prs), ""
        if args[:3] == ["gh", "pr", "update-branch"]:
            self.updates.append(int(args[3]))
            return self.update_rc, "", "unknown command" if self.update_rc else ""
        if args[:3] == ["gh", "pr", "comment"]:
            body = Path(args[args.index("--body-file") + 1]).read_text(encoding="utf-8")
            self.comments.append((int(args[3]), body))
            return self.comment_rc, "", ""
        raise AssertionError(f"unexpected command: {args}")

    def conflict_comments(self) -> list[int]:
        return [number for number, body in self.comments if CONFLICT_TEXT in body]


class FakeQueue:
    def __init__(self) -> None:
        self.store: dict[str, Any] = {}

    def kv_get(self, key: str) -> Any:
        return self.store.get(key)

    def kv_set(self, key: str, value: Any) -> None:
        self.store[key] = value


class FakeCheck:
    """Replaces :func:`run_combined`: every run passes."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[int]]] = []

    def __call__(self, repo_root: Path, base: str, prs: list[dict[str, Any]], *, timeout_s: float, workdir: Path,
                 git: str = "git") -> CombinedResult:
        numbers = [pr["number"] for pr in prs]
        self.calls.append((base, numbers))
        return CombinedResult(ok=True, ran=True, base=base, base_sha="a" * 40, prs=numbers, summary="ok")


def _checker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
             prs: list[dict[str, Any]]) -> tuple[CombinedChecker, FakeRunner, FakeCheck]:
    runner = FakeRunner(prs)
    check = FakeCheck()
    monkeypatch.setattr(combined_check, "run_combined", check)
    service = SimpleNamespace(running=True, repo=tmp_path, factory=SimpleNamespace(queue=FakeQueue()))
    return CombinedChecker(service, interval_s=60, runner=runner, gh="gh", git="git"), runner, check


def _round(checker: CombinedChecker) -> None:
    asyncio.run(checker.run_once())


def _saved(checker: CombinedChecker) -> dict[str, Any]:
    return json.loads(checker.service.factory.queue.store["combined_check:last"])


def test_merge_state_adds_the_fields(tmp_path: Path) -> None:
    runner = FakeRunner([_pr(1, "UNKNOWN", "BEHIND")])

    prs = asyncio.run(open_foundry_prs("gh", tmp_path, runner, merge_state=True))

    assert runner.calls[0][-1] == "number,title,headRefName,headRefOid,baseRefName,mergeable,mergeStateStatus"
    assert (prs[0]["mergeable"], prs[0]["mergeStateStatus"]) == ("UNKNOWN", "BEHIND")


def test_a_round_asks_for_the_merge_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    checker, runner, _ = _checker(tmp_path, monkeypatch, [_pr(1)])

    _round(checker)

    assert runner.calls[0][:3] == ["gh", "pr", "list"]
    assert set(runner.calls[0][-1].split(",")) >= {"mergeable", "mergeStateStatus"}


def test_behind_and_mergeable_updates_that_pr_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    checker, runner, _ = _checker(tmp_path, monkeypatch, [_pr(1), _pr(2, status="BEHIND")])

    _round(checker)

    assert runner.updates == [2]
    assert ["gh", "pr", "update-branch", "2"] in runner.calls
    assert runner.conflict_comments() == []


def test_a_single_pr_on_its_base_is_still_followed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    checker, runner, check = _checker(tmp_path, monkeypatch, [_pr(1, status="BEHIND"), _pr(2, "CONFLICTING", base="dev")])

    _round(checker)

    assert runner.updates == [1]
    assert [number for number, _ in runner.comments] == [2]
    assert check.calls == []


def test_conflicting_comments_once_per_head(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    checker, runner, _ = _checker(tmp_path, monkeypatch, [_pr(1, "CONFLICTING", "DIRTY", head="a" * 40)])

    _round(checker)
    _round(checker)

    assert runner.conflict_comments() == [1]
    assert runner.comments[0][1].splitlines() == [COMMENT_MARKER, CONFLICT_TEXT]
    assert runner.updates == []
    assert _saved(checker)["conflicts"] == {"1": "a" * 40}

    runner.prs = [_pr(1, "CONFLICTING", "DIRTY", head="b" * 40)]
    _round(checker)

    assert runner.conflict_comments() == [1, 1]
    assert _saved(checker)["conflicts"] == {"1": "b" * 40}


def test_conflict_is_remembered_across_restarts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    checker, runner, _ = _checker(tmp_path, monkeypatch, [_pr(1, "CONFLICTING", "DIRTY")])

    _round(checker)
    _round(CombinedChecker(checker.service, interval_s=60, runner=runner, gh="gh", git="git"))

    assert runner.conflict_comments() == [1]


def test_a_conflict_comment_that_failed_is_retried(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    checker, runner, _ = _checker(tmp_path, monkeypatch, [_pr(1, "CONFLICTING", "DIRTY")])
    runner.comment_rc = 1

    _round(checker)
    runner.comment_rc = 0
    _round(checker)

    assert runner.conflict_comments() == [1, 1]
    assert _saved(checker)["conflicts"] == {"1": f"{1:040d}"}


def test_closed_prs_are_dropped_from_the_conflicts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    checker, runner, _ = _checker(tmp_path, monkeypatch, [_pr(1, "CONFLICTING", "DIRTY"), _pr(2, "CONFLICTING", "DIRTY")])

    _round(checker)
    assert set(_saved(checker)["conflicts"]) == {"1", "2"}
    runner.prs = [_pr(2, "CONFLICTING", "DIRTY")]
    _round(checker)

    assert set(_saved(checker)["conflicts"]) == {"2"}


def test_failing_update_is_logged_once_and_the_round_continues(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                               caplog: pytest.LogCaptureFixture) -> None:
    checker, runner, check = _checker(tmp_path, monkeypatch, [_pr(1, status="BEHIND"), _pr(2, status="BEHIND")])
    runner.update_rc = 1

    with caplog.at_level(logging.WARNING, logger="foundry.combined_check"):
        _round(checker)

    assert runner.updates == [1, 2]
    warnings = [r.getMessage() for r in caplog.records if "could not follow the base" in r.getMessage()]
    assert len(warnings) == 1
    assert "#1" in warnings[0] and "#2" in warnings[0] and "unknown command" in warnings[0]
    assert check.calls == [("main", [1, 2])]


@pytest.mark.parametrize("mergeable, status", [
    ("MERGEABLE", "CLEAN"),
    ("UNKNOWN", "UNKNOWN"),
    ("UNKNOWN", "BEHIND"),
    ("MERGEABLE", "UNKNOWN"),
])
def test_other_states_do_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mergeable: str, status: str) -> None:
    checker, runner, _ = _checker(tmp_path, monkeypatch, [_pr(1, mergeable, status)])

    _round(checker)

    assert runner.updates == []
    assert runner.comments == []
    assert _saved(checker)["conflicts"] == {}
