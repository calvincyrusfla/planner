"""Tests for foundry.combined_check: green Foundry PRs that only touch tests or docs merge themselves."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from foundry import combined_check
from foundry.combined_check import COMMENT_MARKER, CombinedChecker, CombinedResult

MERGED_TEXT = "Merged automatically: tests/docs only, green and mergeable."
TESTS_ONLY = ["foundry/tests/test_x.py"]
SHA = "a" * 40


def _pr(number: int, mergeable: str = "MERGEABLE", status: str = "CLEAN", head: str | None = None,
        base: str = "main") -> dict[str, Any]:
    return {"number": number, "title": f"pr {number}", "headRefName": f"foundry/F-{number}",
            "headRefOid": head or f"{number:040d}", "baseRefName": base, "mergeable": mergeable,
            "mergeStateStatus": status}


class FakeRunner:
    """Stands in for git and gh: records every command, each merge and each comment."""

    def __init__(self, prs: list[dict[str, Any]], files: dict[int, list[str]] | None = None) -> None:
        self.prs = prs
        self.files = files or {}
        self.sha = SHA
        self.fetch_rc = 0
        self.comment_rc = 0
        self.merge_rc: dict[int, int] = {}
        self.calls: list[list[str]] = []
        self.merges: list[list[str]] = []
        self.comments: list[tuple[int, str]] = []

    async def __call__(self, argv: Any, *, cwd: Any = None, timeout: Any = None,
                       env: Any = None) -> tuple[int, str, str]:
        args = list(argv)
        self.calls.append(args)
        if args[:2] == ["git", "fetch"]:
            return self.fetch_rc, "", "no network" if self.fetch_rc else ""
        if args[:2] == ["git", "rev-parse"]:
            return 0, self.sha + "\n", ""
        if args[:3] == ["gh", "pr", "list"]:
            return 0, json.dumps(self.prs), ""
        if args[:3] == ["gh", "pr", "update-branch"]:
            return 0, "", ""
        if args[:3] == ["gh", "pr", "view"]:
            assert args[4:] == ["--json", "files"]
            paths = self.files.get(int(args[3]), TESTS_ONLY)
            return 0, json.dumps({"files": [{"path": path} for path in paths]}), ""
        if args[:3] == ["gh", "pr", "merge"]:
            self.merges.append(args)
            rc = self.merge_rc.get(int(args[3]), 0)
            return rc, "", "merge refused" if rc else ""
        if args[:3] == ["gh", "pr", "comment"]:
            body = Path(args[args.index("--body-file") + 1]).read_text(encoding="utf-8")
            self.comments.append((int(args[3]), body))
            return self.comment_rc, "", ""
        raise AssertionError(f"unexpected command: {args}")

    def merged(self) -> list[int]:
        return [int(args[3]) for args in self.merges]

    def merge_comments(self) -> list[int]:
        return [number for number, body in self.comments if MERGED_TEXT in body]

    def views(self) -> list[list[str]]:
        return [args for args in self.calls if args[:3] == ["gh", "pr", "view"]]


class FakeQueue:
    def __init__(self) -> None:
        self.store: dict[str, Any] = {}

    def kv_get(self, key: str) -> Any:
        return self.store.get(key)

    def kv_set(self, key: str, value: Any) -> None:
        self.store[key] = value


class FakeCheck:
    """Replaces :func:`run_combined`: passes unless ``failures`` or ``ran`` say otherwise."""

    def __init__(self, runner: FakeRunner) -> None:
        self.runner = runner
        self.failures: list[str] = []
        self.conflicts: dict[int, list[str]] = {}
        self.ran = True
        self.base_sha: str | None = None
        self.calls: list[tuple[str, list[int]]] = []

    def __call__(self, repo_root: Path, base: str, prs: list[dict[str, Any]], *, timeout_s: float, workdir: Path,
                 git: str = "git") -> CombinedResult:
        numbers = [pr["number"] for pr in prs]
        self.calls.append((base, numbers))
        merged = [number for number in numbers if number not in self.conflicts]
        ok = self.ran and not self.failures and not self.conflicts
        summary = "ok" if self.ran else "combined check could not finish: no time"
        return CombinedResult(ok=ok, ran=self.ran, base=base, base_sha=self.base_sha or self.runner.sha, prs=merged,
                              conflicts=dict(self.conflicts), new_failures=list(self.failures), summary=summary)


def _service(tmp_path: Path, auto_merge: str | None, queue: FakeQueue | None = None) -> SimpleNamespace:
    service = SimpleNamespace(running=True, repo=tmp_path, factory=SimpleNamespace(queue=queue or FakeQueue()))
    if auto_merge is not None:
        service.config = SimpleNamespace(auto_merge=auto_merge)
    return service


def _checker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, prs: list[dict[str, Any]],
             files: dict[int, list[str]] | None = None,
             auto_merge: str | None = "tests_docs") -> tuple[CombinedChecker, FakeRunner, FakeCheck]:
    runner = FakeRunner(prs, files)
    check = FakeCheck(runner)
    monkeypatch.setattr(combined_check, "run_combined", check)
    checker = CombinedChecker(_service(tmp_path, auto_merge), interval_s=60, runner=runner, gh="gh", git="git")
    return checker, runner, check


def _round(checker: CombinedChecker) -> None:
    asyncio.run(checker.run_once())


def _saved(checker: CombinedChecker) -> dict[str, Any]:
    return json.loads(checker.service.factory.queue.store["combined_check:last"])


@pytest.mark.parametrize("setting", [None, "off"])
def test_off_never_merges(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, setting: str | None) -> None:
    checker, runner, _ = _checker(tmp_path, monkeypatch, [_pr(1), _pr(2)], auto_merge=setting)

    _round(checker)

    assert runner.merges == []
    assert runner.views() == []


def test_tests_only_clean_and_green_merges_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    checker, runner, _ = _checker(tmp_path, monkeypatch, [_pr(1), _pr(2)])

    _round(checker)
    _round(checker)  # gh can still list a PR it just merged

    assert runner.merged() == [1, 2]
    assert all(args[:5] == ["gh", "pr", "merge", args[3], "--squash"] for args in runner.merges)
    assert runner.merge_comments() == [1, 2]
    body = dict(runner.comments[-2:])[1]
    assert body.splitlines() == [COMMENT_MARKER, MERGED_TEXT]
    assert set(_saved(checker)["merged"]) == {"1", "2"}


def test_merge_is_pinned_to_the_judged_head(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    checker, runner, _ = _checker(tmp_path, monkeypatch, [_pr(1, head="b" * 40)])

    _round(checker)

    assert runner.merges[0][5:] == ["--match-head-commit", "b" * 40]


def test_alone_on_its_base_needs_no_combined_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    checker, runner, check = _checker(tmp_path, monkeypatch, [_pr(1), _pr(2, base="release")])

    _round(checker)

    assert check.calls == []
    assert runner.merged() == [1, 2]


def test_merged_is_remembered_across_restarts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    checker, runner, _ = _checker(tmp_path, monkeypatch, [_pr(1)])
    _round(checker)

    again = CombinedChecker(checker.service, interval_s=60, runner=runner, gh="gh", git="git")
    _round(again)

    assert runner.merged() == [1]


def test_green_survives_a_restart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    checker, runner, check = _checker(tmp_path, monkeypatch, [_pr(1, status="BLOCKED"), _pr(2, status="BLOCKED")])
    _round(checker)
    assert runner.merges == []

    runner.prs = [_pr(1), _pr(2)]
    again = CombinedChecker(checker.service, interval_s=60, runner=runner, gh="gh", git="git")
    _round(again)

    assert len(check.calls) == 1
    assert runner.merged() == [1, 2]


@pytest.mark.parametrize(
    "path",
    ["foundry/x.py", "foundry/tests.py", "testing/x.py", "foundry/docs/conf.py", "test_x.txt", "README.txt", "x.test.py"],
)
def test_a_pr_touching_any_other_file_is_never_merged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                      path: str) -> None:
    checker, runner, _ = _checker(tmp_path, monkeypatch, [_pr(1)], {1: [*TESTS_ONLY, path]})

    _round(checker)

    assert runner.merges == []
    assert runner.merge_comments() == []


@pytest.mark.parametrize(
    "path",
    ["tests/helpers.py", "foundry/test/data.json", "a/test_x.py", "test_x.py", "a/x_test.py", "web/x.test.mjs",
     "x.test.js", "x.test.ts", "README.md", "a/b/notes.md", "docs/img/a.png"],
)
def test_tests_and_docs_are_recognised(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str) -> None:
    checker, runner, _ = _checker(tmp_path, monkeypatch, [_pr(1)], {1: [path]})

    _round(checker)

    assert runner.merged() == [1]


def test_no_files_is_not_tests_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    checker, runner, _ = _checker(tmp_path, monkeypatch, [_pr(1)], {1: []})

    _round(checker)

    assert runner.merges == []


@pytest.mark.parametrize(
    ("mergeable", "status"),
    [("CONFLICTING", "DIRTY"), ("MERGEABLE", "BEHIND"), ("MERGEABLE", "UNSTABLE"), ("MERGEABLE", "BLOCKED"),
     ("UNKNOWN", "CLEAN")],
)
def test_a_pr_not_ready_is_not_merged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mergeable: str,
                                      status: str) -> None:
    checker, runner, _ = _checker(tmp_path, monkeypatch, [_pr(1, mergeable, status)])

    _round(checker)

    assert runner.merges == []


def test_has_hooks_merges(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    checker, runner, _ = _checker(tmp_path, monkeypatch, [_pr(1, status="HAS_HOOKS")])

    _round(checker)

    assert runner.merged() == [1]


def test_a_failing_merge_is_logged_and_the_round_continues(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                           caplog: pytest.LogCaptureFixture) -> None:
    checker, runner, _ = _checker(tmp_path, monkeypatch, [_pr(1), _pr(2)])
    runner.merge_rc[1] = 1

    with caplog.at_level(logging.WARNING, logger="foundry.combined_check"):
        _round(checker)

    assert runner.merged() == [1, 2]  # one attempt for #1, and #2 still got its turn
    assert runner.merge_comments() == [2]
    assert set(_saved(checker)["merged"]) == {"2"}
    assert any("could not auto-merge #1" in record.getMessage() and record.levelno == logging.WARNING
               for record in caplog.records)


def test_failed_combined_run_does_not_merge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    checker, runner, check = _checker(tmp_path, monkeypatch, [_pr(1), _pr(2)])
    check.failures = ["test_a.py::test_a"]

    _round(checker)

    assert len(check.calls) == 1
    assert runner.merges == []


def test_a_run_that_could_not_finish_does_not_merge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    checker, runner, check = _checker(tmp_path, monkeypatch, [_pr(1), _pr(2)])
    check.ran = False

    _round(checker)

    assert runner.merges == []


def test_conflict_in_the_group_does_not_merge_the_prs_involved(tmp_path: Path,
                                                               monkeypatch: pytest.MonkeyPatch) -> None:
    checker, runner, check = _checker(tmp_path, monkeypatch, [_pr(1), _pr(2)])
    check.conflicts = {2: ["a.md"]}

    _round(checker)

    assert runner.merges == []


def test_stale_posted_green_after_the_base_moved_does_not_merge(tmp_path: Path,
                                                                monkeypatch: pytest.MonkeyPatch) -> None:
    checker, runner, check = _checker(tmp_path, monkeypatch, [_pr(1, status="BLOCKED"), _pr(2, status="BLOCKED")])
    _round(checker)  # green, but not CLEAN yet
    assert _saved(checker)["status"]["1"].startswith("✅")

    runner.prs = [_pr(1), _pr(2)]
    runner.sha = "b" * 40
    runner.comment_rc = 1
    check.failures = ["test_a.py::test_a"]
    _round(checker)

    assert len(check.calls) == 2
    assert _saved(checker)["status"]["1"].startswith("✅")  # the failure could not be posted
    assert runner.merges == []


def test_unreadable_base_does_not_merge_on_an_older_green(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    checker, runner, _ = _checker(tmp_path, monkeypatch, [_pr(1, status="BLOCKED"), _pr(2, status="BLOCKED")])
    _round(checker)

    runner.prs = [_pr(1), _pr(2)]
    runner.fetch_rc = 1
    _round(checker)

    assert runner.merges == []


def test_a_new_head_needs_a_new_green_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    checker, runner, check = _checker(tmp_path, monkeypatch, [_pr(1, status="BLOCKED"), _pr(2, status="BLOCKED")])
    _round(checker)

    runner.prs = [_pr(1, head="c" * 40), _pr(2)]
    check.failures = ["test_a.py::test_a"]
    _round(checker)

    assert len(check.calls) == 2
    assert runner.merges == []


def test_a_run_on_a_newer_base_than_the_one_read_does_not_merge(tmp_path: Path,
                                                                monkeypatch: pytest.MonkeyPatch) -> None:
    checker, runner, check = _checker(tmp_path, monkeypatch, [_pr(1), _pr(2)])
    check.base_sha = "d" * 40

    _round(checker)

    assert runner.merges == []
