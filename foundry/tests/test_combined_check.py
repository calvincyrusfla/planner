"""Tests for foundry.combined_check: do the open Foundry PRs still work together?"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from foundry import combined_check, suite_check
from foundry.combined_check import (
    COMMENT_MARKER,
    CombinedChecker,
    CombinedResult,
    combined_check_enabled,
    comment_body,
    fingerprint,
    open_foundry_prs,
    run_combined,
    status_line,
)

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

OK_LINE = "\u2705 Works together with the other open Foundry PRs"
FAILURES_PREFIX = "\u274c New test failures when merged with the other open Foundry PRs: "
COULD_NOT_RUN = "Combined check could not run the tests: "

_GIT_LOCATION_VARS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_COMMON_DIR")

# Each PR below changes one of these on its own; the test fails only when both values went up.
BASE_FILES = {
    "pytest.ini": "[pytest]\n",
    "left.py": "LEFT = 1\n",
    "right.py": "RIGHT = 1\n",
    "test_limit.py": "import left\nimport right\n\n\ndef test_limit():\n    assert left.LEFT + right.RIGHT < 4\n",
}


# ------------------------------------------------------------------ helpers


def _git(cwd: Path, *args: str) -> str:
    env = {k: v for k, v in os.environ.items() if k not in _GIT_LOCATION_VARS}
    argv = ["git", "-c", "user.name=Test", "-c", "user.email=test@localhost", "-c", "core.autocrlf=false",
            "-c", "commit.gpgsign=false", *args]
    proc = subprocess.run(argv, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                          check=False)
    assert proc.returncode == 0, f"git {' '.join(args)}: {proc.stderr}"
    return proc.stdout.strip()


def _write(root: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        (root / name).write_bytes(text.encode("utf-8"))


def _pr_row(number: int, head: str | None = None, base: str = "main") -> dict[str, Any]:
    return {"number": number, "title": f"pr {number}", "headRefName": f"foundry/F-{number}",
            "headRefOid": head or f"{number:040d}", "baseRefName": base}


def _worktrees(clone: Path) -> int:
    return sum(line.startswith("worktree ") for line in _git(clone, "worktree", "list", "--porcelain").splitlines())


def _snapshot(clone: Path) -> dict[str, Any]:
    return {
        "head": _git(clone, "rev-parse", "HEAD"),
        "branch": _git(clone, "rev-parse", "--abbrev-ref", "HEAD"),
        "status": _git(clone, "status", "--porcelain"),
        "worktrees": _worktrees(clone),
    }


@dataclass
class World:
    """A bare origin with a ``main`` branch, a clone of it (the user's repo) and a seed repo that makes PRs."""

    seed: Path
    clone: Path
    workdir: Path

    def add_pr(self, number: int, files: dict[str, str]) -> dict[str, Any]:
        """Commit ``files`` on top of ``main`` and push the commit as ``refs/pull/<number>/head``."""
        _git(self.seed, "checkout", "-q", "--detach", "main")
        _write(self.seed, files)
        _git(self.seed, "add", "-A")
        _git(self.seed, "commit", "-q", "-m", f"pr {number}")
        sha = _git(self.seed, "rev-parse", "HEAD")
        _git(self.seed, "push", "-q", "origin", f"HEAD:refs/pull/{number}/head")
        _git(self.seed, "checkout", "-q", "main")
        return _pr_row(number, sha)

    def run(self, prs: list[dict[str, Any]], base: str = "main") -> CombinedResult:
        return run_combined(self.clone, base, prs, timeout_s=120, workdir=self.workdir)

    def base_sha(self) -> str:
        return _git(self.seed, "rev-parse", "main")

    def assert_nothing_left(self) -> None:
        assert _worktrees(self.clone) == 1
        assert list(self.workdir.iterdir()) == []


@pytest.fixture
def make_world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[..., World]:
    """Builds a :class:`World`; the suite is pytest on the tiny repo."""
    for name in (*_GIT_LOCATION_VARS, "PYTEST_ADDOPTS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(suite_check, "suite_commands",
                        lambda root: [[sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"]])
    combined_check._BASE_RESULTS.clear()

    def build(extra: dict[str, str] | None = None) -> World:
        origin, seed, clone = tmp_path / "origin.git", tmp_path / "seed", tmp_path / "clone"
        _git(tmp_path, "init", "-q", "--bare", str(origin))
        _git(origin, "symbolic-ref", "HEAD", "refs/heads/main")
        seed.mkdir()
        _git(seed, "init", "-q")
        _git(seed, "symbolic-ref", "HEAD", "refs/heads/main")
        _write(seed, {**BASE_FILES, **(extra or {})})
        _git(seed, "add", "-A")
        _git(seed, "commit", "-q", "-m", "base")
        _git(seed, "remote", "add", "origin", str(origin))
        _git(seed, "push", "-q", "origin", "main")
        _git(tmp_path, "clone", "-q", "-c", "core.autocrlf=false", str(origin), str(clone))
        workdir = tmp_path / "workdir"
        workdir.mkdir()
        return World(seed=seed, clone=clone, workdir=workdir)

    return build


def _no_suite(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The suite command cannot be started."""
    monkeypatch.setattr(suite_check, "suite_commands", lambda root: [[str(tmp_path / "no-such-suite")]])


def _result(**changes: Any) -> CombinedResult:
    values: dict[str, Any] = {"ok": True, "ran": True, "base": "main", "base_sha": "abcdef1234567890", "prs": [1, 2],
                              "summary": "fine", "checked_at": "2026-11-09T00:00:00Z"}
    values.update(changes)
    return CombinedResult(**values)


# ------------------------------------------------------------------ run_combined


@needs_git
def test_compatible_prs_work_together(make_world: Callable[..., World]) -> None:
    world = make_world()
    first = world.add_pr(1, {"one.py": "ONE = 1\n"})
    second = world.add_pr(2, {"two.py": "TWO = 2\n"})

    result = world.run([second, first])  # merged in ascending number order whatever the listing order

    assert result.ok is True
    assert result.ran is True
    assert result.prs == [1, 2]
    assert result.conflicts == {}
    assert result.new_failures == []
    assert result.base == "main"
    assert result.base_sha == world.base_sha()
    assert result.fingerprint == fingerprint(world.base_sha(), [first, second])
    assert result.checked_at
    world.assert_nothing_left()


@needs_git
def test_only_prs_of_the_base_are_merged(make_world: Callable[..., World]) -> None:
    world = make_world()
    first = world.add_pr(1, {"one.py": "ONE = 1\n"})
    other = world.add_pr(2, {"two.py": "TWO = 2\n"})
    other["baseRefName"] = "release"

    result = world.run([first, other])

    assert result.ok is True
    assert result.prs == [1]


@needs_git
def test_same_line_edits_conflict_on_the_later_pr(make_world: Callable[..., World]) -> None:
    world = make_world()
    first = world.add_pr(1, {"left.py": "LEFT = 2\n"})
    second = world.add_pr(2, {"left.py": "LEFT = 3\n"})

    result = world.run([first, second])

    assert result.ok is False
    assert result.ran is True
    assert result.prs == [1]
    assert result.conflicts == {2: ["left.py"]}
    assert result.new_failures == []
    assert "#2" in result.summary
    world.assert_nothing_left()


@needs_git
def test_prs_fine_alone_but_breaking_a_test_together(make_world: Callable[..., World]) -> None:
    world = make_world()
    first = world.add_pr(1, {"left.py": "LEFT = 2\n"})
    second = world.add_pr(2, {"right.py": "RIGHT = 2\n"})

    assert world.run([first]).ok is True  # each one alone passes
    assert world.run([second]).ok is True
    result = world.run([first, second])

    assert result.ok is False
    assert result.ran is True
    assert result.prs == [1, 2]
    assert result.conflicts == {}
    assert len(result.new_failures) == 1
    assert "test_limit" in result.new_failures[0]
    world.assert_nothing_left()


@needs_git
def test_a_failure_the_base_already_has_is_not_new(make_world: Callable[..., World]) -> None:
    world = make_world({"test_old.py": "def test_old_failure():\n    assert False\n"})
    first = world.add_pr(1, {"one.py": "ONE = 1\n"})
    second = world.add_pr(2, {"two.py": "TWO = 2\n"})

    result = world.run([first, second])

    assert result.ok is True
    assert result.ran is True
    assert result.new_failures == []
    world.assert_nothing_left()


@needs_git
def test_the_users_clone_is_untouched(make_world: Callable[..., World]) -> None:
    world = make_world()
    first = world.add_pr(1, {"left.py": "LEFT = 2\n"})
    second = world.add_pr(2, {"left.py": "LEFT = 3\n"})  # conflicts: the merge is aborted
    third = world.add_pr(3, {"right.py": "RIGHT = 2\n"})  # with #1 breaks the test: the base runs too
    before = _snapshot(world.clone)

    result = world.run([first, second, third])

    assert result.prs == [1, 3]
    assert result.conflicts == {2: ["left.py"]}
    assert len(result.new_failures) == 1
    assert _snapshot(world.clone) == before
    assert before["branch"] == "main"
    assert before["status"] == ""
    world.assert_nothing_left()


@needs_git
def test_a_suite_that_cannot_run_is_no_false_alarm(make_world: Callable[..., World], tmp_path: Path,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    world = make_world()
    first = world.add_pr(1, {"one.py": "ONE = 1\n"})
    second = world.add_pr(2, {"two.py": "TWO = 2\n"})
    _no_suite(monkeypatch, tmp_path)

    result = world.run([first, second])

    assert result.ran is False
    assert result.ok is True
    assert result.new_failures == []
    world.assert_nothing_left()


@needs_git
def test_conflicts_still_count_when_the_suite_cannot_run(make_world: Callable[..., World], tmp_path: Path,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    world = make_world()
    first = world.add_pr(1, {"left.py": "LEFT = 2\n"})
    second = world.add_pr(2, {"left.py": "LEFT = 3\n"})
    _no_suite(monkeypatch, tmp_path)

    result = world.run([first, second])

    assert result.ran is False
    assert result.ok is False
    assert result.conflicts == {2: ["left.py"]}
    world.assert_nothing_left()


@needs_git
def test_a_missing_base_is_no_false_alarm(make_world: Callable[..., World]) -> None:
    world = make_world()
    before = _snapshot(world.clone)

    result = world.run([_pr_row(1, base="missing"), _pr_row(2, base="missing")], base="missing")

    assert result.ran is False
    assert result.ok is True
    assert result.prs == []
    assert _snapshot(world.clone) == before
    world.assert_nothing_left()


def test_no_pr_for_the_base_is_not_a_run(tmp_path: Path) -> None:
    result = run_combined(tmp_path, "main", [_pr_row(1, base="release")], timeout_s=1, workdir=tmp_path / "work")

    assert result.ran is False
    assert result.ok is True
    assert result.prs == []


# ------------------------------------------------------------------ fingerprint


def test_fingerprint_ignores_order_and_follows_commits() -> None:
    prs = [_pr_row(1, "a" * 40), _pr_row(2, "b" * 40)]
    same = fingerprint("base", prs)

    assert fingerprint("base", list(reversed(prs))) == same
    assert fingerprint("other", prs) != same
    assert fingerprint("base", [_pr_row(1, "a" * 40), _pr_row(2, "c" * 40)]) != same


# ------------------------------------------------------------------ comments


@pytest.mark.parametrize(
    ("changes", "number", "expected"),
    [
        ({}, 1, OK_LINE),
        ({"ok": False, "prs": [1], "conflicts": {2: ["left.py", "right.py"]}}, 2,
         "\u274c Conflicts with #1 in left.py, right.py"),
        ({"ok": False, "prs": [1], "conflicts": {2: ["left.py", "right.py"]}}, 1,
         "\u274c Conflicts with #2 in left.py, right.py"),
        ({"ok": False, "new_failures": ["test_limit.py::test_limit"]}, 1,
         FAILURES_PREFIX + "test_limit.py::test_limit"),
    ],
)
def test_comment_body_starts_with_marker_and_status(changes: dict[str, Any], number: int, expected: str) -> None:
    lines = comment_body(_result(**changes), number).splitlines()

    assert lines[0] == COMMENT_MARKER
    assert lines[1] == expected


def test_comment_says_when_the_tests_could_not_run() -> None:
    result = _result(ran=False, summary="combined check could not finish: the suite did not complete on the base")

    line = comment_body(result, 1).splitlines()[1]

    assert line.startswith("\u26a0")
    assert line.endswith(COULD_NOT_RUN + "the suite did not complete on the base")


def test_comment_names_what_was_checked() -> None:
    body = comment_body(_result(), 1)

    assert "#1, #2" in body
    assert "`main`" in body
    assert "`abcdef1`" in body
    assert "2026-11-09T00:00:00Z" in body


def test_status_line_shortens_a_long_failure_list() -> None:
    shown = combined_check._MAX_IDS_SHOWN
    result = _result(ok=False, new_failures=[f"t{i}" for i in range(shown + 2)])

    assert status_line(result, 1).endswith(" and 2 more")


# ------------------------------------------------------------------ listing PRs


class FakeRunner:
    """Stands in for git and gh: records every command and each comment posted."""

    def __init__(self, prs: list[dict[str, Any]]) -> None:
        self.prs = prs
        self.sha = "a" * 40
        self.comment_rc = 0
        self.calls: list[list[str]] = []
        self.comments: list[tuple[int, str]] = []

    async def __call__(self, argv: Any, *, cwd: Any = None, timeout: Any = None,
                       env: Any = None) -> tuple[int, str, str]:
        args = list(argv)
        self.calls.append(args)
        if args[:2] == ["git", "fetch"]:
            return 0, "", ""
        if args[:2] == ["git", "rev-parse"]:
            return 0, self.sha + "\n", ""
        if args[:3] == ["gh", "pr", "list"]:
            return 0, json.dumps(self.prs), ""
        if args[:3] == ["gh", "pr", "comment"]:
            body = Path(args[args.index("--body-file") + 1]).read_text(encoding="utf-8")
            self.comments.append((int(args[3]), body))
            return self.comment_rc, "", ""
        raise AssertionError(f"unexpected command: {args}")


def test_open_foundry_prs_keeps_foundry_branches(tmp_path: Path) -> None:
    other = {**_pr_row(3), "headRefName": "feature/x"}
    runner = FakeRunner([_pr_row(1), other, _pr_row(2)])

    prs = asyncio.run(open_foundry_prs("gh", tmp_path, runner))

    assert [pr["number"] for pr in prs] == [1, 2]
    assert runner.calls == [["gh", "pr", "list", "--state", "open", "--limit", "50", "--json",
                             "number,title,headRefName,headRefOid,baseRefName"]]


@pytest.mark.parametrize("reply", [(1, "", "boom"), (0, "not json", ""), (0, "{}", "")])
def test_open_foundry_prs_gives_nothing_on_a_bad_reply(tmp_path: Path, reply: tuple[int, str, str]) -> None:
    async def runner(argv: Any, *, cwd: Any = None, timeout: Any = None, env: Any = None) -> tuple[int, str, str]:
        return reply

    assert asyncio.run(open_foundry_prs("gh", tmp_path, runner)) == []


def test_open_foundry_prs_survives_a_runner_error(tmp_path: Path) -> None:
    async def runner(argv: Any, *, cwd: Any = None, timeout: Any = None, env: Any = None) -> tuple[int, str, str]:
        raise OSError("gh is gone")

    assert asyncio.run(open_foundry_prs("gh", tmp_path, runner)) == []


# ------------------------------------------------------------------ CombinedChecker


class FakeQueue:
    def __init__(self) -> None:
        self.store: dict[str, Any] = {}

    def kv_get(self, key: str) -> Any:
        return self.store.get(key)

    def kv_set(self, key: str, value: Any) -> None:
        self.store[key] = value


class FakeCheck:
    """Replaces :func:`run_combined`: hands out the given results in order."""

    def __init__(self, *results: CombinedResult) -> None:
        self.results = list(results)
        self.calls: list[tuple[str, list[int]]] = []

    def __call__(self, repo_root: Path, base: str, prs: list[dict[str, Any]], *, timeout_s: float, workdir: Path,
                 git: str = "git") -> CombinedResult:
        self.calls.append((base, [pr["number"] for pr in prs]))
        return self.results.pop(0)


def _service(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(running=True, repo=tmp_path, factory=SimpleNamespace(queue=FakeQueue()))


def _checker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, prs: list[dict[str, Any]],
             *results: CombinedResult) -> tuple[CombinedChecker, FakeRunner, FakeCheck]:
    runner = FakeRunner(prs)
    check = FakeCheck(*results)
    monkeypatch.setattr(combined_check, "run_combined", check)
    return CombinedChecker(_service(tmp_path), interval_s=60, runner=runner, gh="gh", git="git"), runner, check


def _round(checker: CombinedChecker) -> None:
    asyncio.run(checker.run_once())


def test_checker_comments_once_and_skips_an_unchanged_fingerprint(tmp_path: Path,
                                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    checker, runner, check = _checker(tmp_path, monkeypatch, [_pr_row(1), _pr_row(2)], _result())

    _round(checker)

    assert check.calls == [("main", [1, 2])]
    assert [number for number, _ in runner.comments] == [1, 2]
    assert runner.comments[0][1].splitlines()[:2] == [COMMENT_MARKER, OK_LINE]

    _round(checker)

    assert len(check.calls) == 1
    assert len(runner.comments) == 2


def test_checker_runs_again_when_the_code_moves_but_stays_quiet(tmp_path: Path,
                                                                monkeypatch: pytest.MonkeyPatch) -> None:
    prs = [_pr_row(1), _pr_row(2)]
    checker, runner, check = _checker(tmp_path, monkeypatch, prs, _result(), _result(), _result())

    _round(checker)
    runner.sha = "b" * 40  # the base moved
    _round(checker)
    prs[0]["headRefOid"] = "f" * 40  # a PR got a new commit
    _round(checker)

    assert len(check.calls) == 3
    assert len(runner.comments) == 2  # the status lines never changed


def test_checker_comments_only_on_the_pr_whose_line_changed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    changed = _result(ok=False, prs=[2], conflicts={1: ["left.py"]})
    checker, runner, _ = _checker(tmp_path, monkeypatch, [_pr_row(1), _pr_row(2)], _result(), changed)

    _round(checker)
    runner.sha = "b" * 40
    _round(checker)

    assert [number for number, _ in runner.comments] == [1, 2, 1]
    assert runner.comments[-1][1].splitlines()[1] == "\u274c Conflicts with main in left.py"


def test_checker_remembers_across_restarts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    checker, runner, check = _checker(tmp_path, monkeypatch, [_pr_row(1), _pr_row(2)], _result())

    _round(checker)
    saved = json.loads(checker.service.factory.queue.store["combined_check:last"])
    assert saved["status"] == {"1": OK_LINE, "2": OK_LINE}

    restarted = CombinedChecker(checker.service, interval_s=60, runner=runner, gh="gh", git="git")
    _round(restarted)

    assert len(check.calls) == 1
    assert len(runner.comments) == 2


def test_checker_retries_a_comment_that_could_not_be_posted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    checker, runner, check = _checker(tmp_path, monkeypatch, [_pr_row(1), _pr_row(2)], _result())
    runner.comment_rc = 1

    _round(checker)
    assert len(runner.comments) == 2

    runner.comment_rc = 0
    _round(checker)
    assert len(runner.comments) == 4
    assert len(check.calls) == 1

    _round(checker)
    assert len(runner.comments) == 4


def test_checker_skips_a_base_with_a_single_pr(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    prs = [_pr_row(1), _pr_row(2), _pr_row(3, base="release")]
    checker, runner, check = _checker(tmp_path, monkeypatch, prs, _result())

    _round(checker)

    assert check.calls == [("main", [1, 2])]
    assert [number for number, _ in runner.comments] == [1, 2]


def test_checker_does_nothing_while_the_service_is_stopped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    checker, runner, check = _checker(tmp_path, monkeypatch, [_pr_row(1), _pr_row(2)], _result())
    checker.service.running = False

    _round(checker)

    assert runner.calls == []
    assert check.calls == []
    assert checker.rounds == 0


def test_checker_summary_is_json_safe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    result = _result(ok=False, prs=[2], conflicts={1: ["left.py"]})
    checker, _, _ = _checker(tmp_path, monkeypatch, [_pr_row(1), _pr_row(2)], result)

    assert checker.summary()["latest"] is None
    _round(checker)

    summary = json.loads(json.dumps(checker.summary()))
    assert summary["rounds"] == 1
    assert summary["latest"]["prs"] == [2]
    assert summary["results"]["main"]["ok"] is False


def test_checker_loop_survives_a_failing_round_and_stops(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    checker, _, _ = _checker(tmp_path, monkeypatch, [])
    checker.interval_s = 0

    async def boom() -> None:
        raise RuntimeError("round failed")

    monkeypatch.setattr(checker, "run_once", boom)

    async def go() -> None:
        checker.start()
        task = checker._task
        assert task is not None
        for _ in range(5):
            await asyncio.sleep(0)
        assert not task.done()
        await checker.stop()
        assert task.cancelled()

    asyncio.run(go())

    assert checker.last_error is not None
    assert "RuntimeError" in checker.last_error


# ------------------------------------------------------------------ settings


@pytest.mark.parametrize(("value", "expected"), [("0", False), ("false", False), ("No", False), (" off ", False),
                                                 ("1", True), ("yes", True)])
def test_env_switch(monkeypatch: pytest.MonkeyPatch, value: str, expected: bool) -> None:
    monkeypatch.setenv("FOUNDRY_COMBINED_CHECK", value)

    assert combined_check_enabled() is expected


def test_on_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FOUNDRY_COMBINED_CHECK", raising=False)

    assert combined_check_enabled() is True


@pytest.mark.parametrize(("value", "expected"), [(None, 600.0), ("5", 60.0), ("120", 120.0), ("soon", 600.0)])
def test_interval_from_the_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str | None,
                                       expected: float) -> None:
    if value is None:
        monkeypatch.delenv("FOUNDRY_COMBINED_INTERVAL_S", raising=False)
    else:
        monkeypatch.setenv("FOUNDRY_COMBINED_INTERVAL_S", value)

    assert CombinedChecker(_service(tmp_path)).interval_s == expected
