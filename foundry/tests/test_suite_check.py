"""Whole-suite check: which commands run the suite and which tests failed."""

from __future__ import annotations

import asyncio
import dataclasses
import difflib
import json
import os
import shlex
import shutil
import sys
from pathlib import Path

import pytest

from foundry import suite_check
from foundry.board_bridge import BoardBridge
from foundry.floor_manager import FloorManager
from foundry.orchestrator_queue import FoundryQueue
from foundry.suite_check import SuiteResult, failing_tests, suite_commands

PYTEST_CMD = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-rf"]


def _touch(root: Path, rel: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")


def _npm_at(monkeypatch: pytest.MonkeyPatch, found: dict[str, str]) -> None:
    monkeypatch.setattr(suite_check.shutil, "which", found.get)


@pytest.fixture(autouse=True)
def _no_configured_command(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FOUNDRY_SUITE_CMD", raising=False)


def test_suite_result_has_the_agreed_fields() -> None:
    names = [f.name for f in dataclasses.fields(SuiteResult)]
    assert names == ["ok", "ran", "new_failures", "base_failures", "head_failures", "summary", "duration_s"]


def test_failing_tests_reads_pytest_summary_lines() -> None:
    output = "\n".join([
        "FAILED tests/test_a.py::test_one - assert 1 == 2 - really",
        "ERROR tests/test_b.py::test_two",
        "ERROR    some.logger:mod.py:3 a captured log line",
        "tests/test_c.py::test_three PASSED",
        "3 failed, 1 passed in 0.1s",
    ])
    assert failing_tests(output) == {"tests/test_a.py::test_one", "tests/test_b.py::test_two"}


def test_failing_tests_reads_node_lines() -> None:
    output = "ok 1 - fine\r\nnot ok 2 - breaks - badly\r\n    not ok 3 - nested\r\n"
    assert failing_tests(output) == {"node: breaks - badly", "node: nested"}


def test_failing_tests_is_empty_for_clean_output() -> None:
    assert failing_tests("1 passed in 0.01s\nok 1 - fine\n") == set()


def test_configured_commands_win_over_detection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _touch(tmp_path, "tests/test_a.py")
    monkeypatch.setenv("FOUNDRY_SUITE_CMD", "python -m pytest -q ;; node --test;;")
    assert suite_commands(tmp_path) == [["python", "-m", "pytest", "-q"], ["node", "--test"]]


def test_configured_commands_use_the_platform_quoting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    command = 'tool "a b"'
    monkeypatch.setenv("FOUNDRY_SUITE_CMD", command)
    assert suite_commands(tmp_path) == [shlex.split(command, posix=os.name != "nt")]


def test_blank_setting_falls_back_to_detection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _touch(tmp_path, "tests/test_a.py")
    monkeypatch.setenv("FOUNDRY_SUITE_CMD", "  ")
    assert suite_commands(tmp_path) == [PYTEST_CMD]


def test_detects_pytest_and_package_test_scripts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _touch(tmp_path, "foundry/tests/test_a.py")
    scripts = {"test": "x", "test:unit": "x", "pretest": "x", "lint": "x"}
    (tmp_path / "package.json").write_text(json.dumps({"scripts": scripts}), encoding="utf-8")
    _npm_at(monkeypatch, {"npm": "/bin/npm"})
    assert suite_commands(tmp_path) == [
        PYTEST_CMD,
        ["/bin/npm", "run", "-s", "test"],
        ["/bin/npm", "run", "-s", "test:unit"],
    ]


def test_skips_npm_scripts_when_npm_is_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"test": "x"}}), encoding="utf-8")
    _npm_at(monkeypatch, {})
    assert suite_commands(tmp_path) == []


def test_ignores_an_unreadable_package_json(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text("{not json", encoding="utf-8")
    assert suite_commands(tmp_path) == []


@pytest.mark.parametrize("rel", ["tests/test_a.py", "tests/unit/test_a.py", "a/b/tests/test_a.py"])
def test_finds_python_tests(tmp_path: Path, rel: str) -> None:
    _touch(tmp_path, rel)
    assert suite_commands(tmp_path) == [PYTEST_CMD]


@pytest.mark.parametrize("rel", [
    "node_modules/x/tests/test_a.py",
    ".venv/tests/test_a.py",
    ".git/tests/test_a.py",
    "foundry/data/tests/test_a.py",
    "a/b/c/tests/test_a.py",
    "src/test_a.py",
    "tests/helper.py",
])
def test_ignores_files_outside_the_search(tmp_path: Path, rel: str) -> None:
    _touch(tmp_path, rel)
    assert suite_commands(tmp_path) == []


needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

MOD = "def one():\n    return 1\n\n\ndef two():\n    return 2\n"
TESTS = (
    "import mod\n\n\ndef test_one():\n    assert mod.one() == 1\n\n\n"
    "def test_two():\n    assert mod.two() == 2\n\n\ndef test_known_bad():\n    assert False\n"
)
KNOWN_BAD = "tests/test_mod.py::test_known_bad"


def _commands(monkeypatch: pytest.MonkeyPatch, *commands: list[str]) -> None:
    monkeypatch.setattr(suite_check, "suite_commands", lambda root: list(commands))


def test_run_suite_passes_the_environment_and_reads_failures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_DIR", "elsewhere")
    monkeypatch.setenv("GIT_WORK_TREE", "elsewhere")
    code = (
        "import os, sys; e = os.environ; "
        "print('FAILED', e.get('PYTHONDONTWRITEBYTECODE'), e.get('GIT_DIR'), e.get('GIT_WORK_TREE')); sys.exit(1)"
    )
    _commands(monkeypatch, [sys.executable, "-c", code])
    assert suite_check.run_suite(tmp_path, timeout_s=60) == ({"1 None None"}, True)


def test_run_suite_reports_a_bare_non_zero_exit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _commands(monkeypatch, [sys.executable, "-c", "import sys; sys.exit(3)"])
    failures, completed = suite_check.run_suite(tmp_path, timeout_s=60)
    assert completed and len(failures) == 1 and next(iter(failures)).endswith(": exit 3")


def test_run_suite_is_incomplete_when_a_command_cannot_start(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _commands(monkeypatch, [str(tmp_path / "no-such-command")])
    assert suite_check.run_suite(tmp_path, timeout_s=60) == (set(), False)


def test_run_suite_is_incomplete_after_a_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _commands(monkeypatch, [sys.executable, "-c", "import time; time.sleep(30)"])
    assert suite_check.run_suite(tmp_path, timeout_s=0.2) == (set(), False)


def _tiny_repo(root: Path) -> Path:
    (root / "tests").mkdir(parents=True)
    (root / "mod.py").write_text(MOD, encoding="utf-8", newline="\n")
    (root / "tests" / "test_mod.py").write_text(TESTS, encoding="utf-8", newline="\n")
    return root


def _patch(path: Path, new: str) -> Path:
    lines = difflib.unified_diff(MOD.splitlines(True), new.splitlines(True), "a/mod.py", "b/mod.py")
    path.write_text("".join(lines), encoding="utf-8", newline="\n")
    return path


@needs_git
def test_check_patch_passes_a_patch_that_keeps_tests_green(tmp_path: Path) -> None:
    repo = _tiny_repo(tmp_path / "repo")
    patch = _patch(tmp_path / "ok.patch", MOD + "\n\ndef three():\n    return 3\n")
    workdir = tmp_path / "suite"
    result = suite_check.check_patch(repo, patch, timeout_s=120, workdir=workdir)
    assert result.ok and result.ran and result.new_failures == []
    assert result.base_failures == [KNOWN_BAD] and result.head_failures == [KNOWN_BAD]
    assert list(workdir.iterdir()) == []
    assert (repo / "mod.py").read_text(encoding="utf-8") == MOD


@needs_git
def test_check_patch_reports_only_the_newly_failing_test(tmp_path: Path) -> None:
    repo = _tiny_repo(tmp_path / "repo")
    patch = _patch(tmp_path / "bad.patch", MOD.replace("return 1", "return 11"))
    result = suite_check.check_patch(repo, patch, timeout_s=120, workdir=tmp_path / "suite")
    assert not result.ok and result.ran
    assert result.new_failures == ["tests/test_mod.py::test_one"]
    assert result.head_failures == [KNOWN_BAD, "tests/test_mod.py::test_one"]


def test_check_patch_is_skipped_when_the_suite_times_out(tmp_path: Path) -> None:
    repo = _tiny_repo(tmp_path / "repo")
    patch = _patch(tmp_path / "bad.patch", MOD.replace("return 1", "return 11"))
    workdir = tmp_path / "suite"
    result = suite_check.check_patch(repo, patch, timeout_s=0.001, workdir=workdir)
    assert result.ok and not result.ran and "skipped" in result.summary
    assert list(workdir.iterdir()) == []


@needs_git
def test_check_patch_is_skipped_when_the_patch_does_not_apply(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _tiny_repo(tmp_path / "repo")
    patch = tmp_path / "broken.patch"
    patch.write_text("--- a/nope.py\n+++ b/nope.py\n@@ -1 +1 @@\n-a\n+b\n", encoding="utf-8", newline="\n")
    monkeypatch.setattr(suite_check, "run_suite", lambda root, *, timeout_s: (set(), True))
    result = suite_check.check_patch(repo, patch, timeout_s=60, workdir=tmp_path / "suite")
    assert result.ok and not result.ran


@needs_git
def test_check_patch_runs_the_base_once_per_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _tiny_repo(tmp_path / "repo")
    patch = _patch(tmp_path / "ok.patch", MOD + "\n\ndef three():\n    return 3\n")
    runs: list[Path] = []

    def fake_run(root: Path, *, timeout_s: float) -> tuple[set[str], bool]:
        runs.append(root)
        return {"old"}, True

    monkeypatch.setattr(suite_check, "run_suite", fake_run)
    for _ in range(2):
        result = suite_check.check_patch(repo, patch, timeout_s=60, workdir=tmp_path / "suite")
        assert result.ok and result.ran and result.base_failures == ["old"]
    assert len(runs) == 3  # one base run, one patched run per check


@needs_git
def test_check_patch_copies_the_source_and_removes_the_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _tiny_repo(tmp_path / "repo")
    left_out = (".git/marker", "node_modules/a.js", ".venv/cfg", "pkg/__pycache__/a.pyc", "foundry/data/a.db")
    for name in (*left_out, "foundry/keep.txt"):
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text("x\n", encoding="utf-8")
    patch = _patch(tmp_path / "ok.patch", MOD + "\n\ndef three():\n    return 3\n")
    listings: list[set[str]] = []

    def fake_run(root: Path, *, timeout_s: float) -> tuple[set[str], bool]:
        listings.append({path.relative_to(root).as_posix() for path in root.rglob("*")})
        return set(), True

    monkeypatch.setattr(suite_check, "run_suite", fake_run)
    result = suite_check.check_patch(repo, patch, timeout_s=60, workdir=tmp_path / "suite")
    assert result.ok and result.ran
    assert "foundry/keep.txt" in listings[0] and not listings[0].intersection(left_out)
    assert list((tmp_path / "suite").iterdir()) == []


@needs_git
def test_check_patch_does_not_cache_an_unfinished_base_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _tiny_repo(tmp_path / "repo")
    patch = _patch(tmp_path / "ok.patch", MOD + "\n\ndef three():\n    return 3\n")
    outcomes: list[tuple[set[str], bool]] = [(set(), False), ({"old"}, True), ({"old"}, True)]
    runs = iter(outcomes)
    monkeypatch.setattr(suite_check, "run_suite", lambda root, *, timeout_s: next(runs))
    first = suite_check.check_patch(repo, patch, timeout_s=60, workdir=tmp_path / "suite")
    assert first.ok and not first.ran
    second = suite_check.check_patch(repo, patch, timeout_s=60, workdir=tmp_path / "suite")
    assert second.ok and second.ran and second.base_failures == ["old"]


def test_check_patch_is_skipped_on_a_malformed_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _tiny_repo(tmp_path / "repo")
    patch = _patch(tmp_path / "ok.patch", MOD + "\n\ndef three():\n    return 3\n")
    monkeypatch.setenv("FOUNDRY_SUITE_CMD", "pytest 'unclosed")
    result = suite_check.check_patch(repo, patch, timeout_s=60, workdir=tmp_path / "suite")
    assert result.ok and not result.ran and "ValueError" in result.summary
    assert list((tmp_path / "suite").iterdir()) == []


class _Publisher:
    def __init__(self) -> None:
        self.base = None
        self.published: list[str] = []
        self.reopened: list[str] = []

    async def publish(self, task_id: str, title: str, patch_path: object, pr: dict[str, object]) -> dict[str, object]:
        self.published.append(task_id)
        return {"ok": True, "branch": "foundry/x", "commit": "abc", "pr_url": "https://example.test/pr/1"}

    async def open_pr(self, branch: str, pr: dict[str, object], base: object = None) -> dict[str, object]:
        self.reopened.append(branch)
        return {"ok": True, "branch": branch, "pr_url": "https://example.test/pr/1"}


TID = "F-001"


def _manager(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[FloorManager, _Publisher, list[str]]:
    monkeypatch.delenv("FOUNDRY_SUITE_GATE", raising=False)
    monkeypatch.delenv("FOUNDRY_SUITE_TIMEOUT_S", raising=False)
    publisher = _Publisher()
    notes: list[str] = []
    bridge = BoardBridge("dry")

    def progress(task_id: str, message: str, **kwargs: object) -> None:
        notes.append(message)

    monkeypatch.setattr(bridge, "progress", progress)
    repo = tmp_path / "repo"
    repo.mkdir()
    manager = FloorManager(FoundryQueue(str(tmp_path / "foundry.db")), None, {}, None, bridge, publisher=publisher,
                           patches_dir=tmp_path / "patches", repo_root=repo)
    monkeypatch.setattr(manager, "pr_request", lambda task_id: {"title": "t", "body": "b", "draft": False})
    patch = tmp_path / "patches" / f"{TID}.patch"
    patch.parent.mkdir()
    patch.write_text("diff\n", encoding="utf-8")
    manager.patch_paths[TID] = patch
    return manager, publisher, notes


def test_publish_holds_a_pr_with_new_failures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager, publisher, notes = _manager(tmp_path, monkeypatch)
    seen: dict[str, object] = {}

    def fake_check(base_root: Path, patch_path: Path, *, timeout_s: float, workdir: Path) -> SuiteResult:
        seen.update(root=base_root, patch=patch_path, timeout=timeout_s, workdir=workdir)
        return SuiteResult(ok=False, ran=True, new_failures=["tests/test_a.py::test_b"], summary="1 new")

    monkeypatch.setattr(suite_check, "check_patch", fake_check)
    rec = asyncio.run(manager._publish(TID))
    assert rec["state"] == "failed" and rec["stage"] == "suite"
    assert rec["error"] == "new test failures: tests/test_a.py::test_b"
    assert publisher.published == []
    assert manager.pr_states[TID]["state"] == "failed"
    assert any(n.startswith("[Foundry] PR held: new test failures") for n in notes)
    assert seen == {"root": tmp_path / "repo", "patch": manager.patch_paths[TID], "timeout": 900.0,
                    "workdir": tmp_path / "suite"}


def test_publish_continues_when_only_old_failures_exist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager, publisher, _ = _manager(tmp_path, monkeypatch)
    monkeypatch.setattr(suite_check, "check_patch", lambda *a, **k: SuiteResult(
        ok=True, ran=True, base_failures=["old"], head_failures=["old"]))
    assert asyncio.run(manager._publish(TID))["state"] == "opened"
    assert publisher.published == [TID]


def test_publish_continues_when_the_check_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                 caplog: pytest.LogCaptureFixture) -> None:
    manager, publisher, _ = _manager(tmp_path, monkeypatch)

    def broken(*args: object, **kwargs: object) -> SuiteResult:
        raise RuntimeError("boom")

    monkeypatch.setattr(suite_check, "check_patch", broken)
    assert asyncio.run(manager._publish(TID))["state"] == "opened"
    assert publisher.published == [TID]
    assert any("boom" in r.getMessage() for r in caplog.records if r.levelname == "WARNING")


def _never(*args: object, **kwargs: object) -> SuiteResult:
    raise AssertionError("the whole-suite check must not run")


@pytest.mark.parametrize("value", ["0", "false", "No", "OFF"])
def test_publish_skips_the_check_when_the_gate_is_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                      value: str) -> None:
    manager, publisher, _ = _manager(tmp_path, monkeypatch)
    monkeypatch.setenv("FOUNDRY_SUITE_GATE", value)
    monkeypatch.setattr(suite_check, "check_patch", _never)
    assert asyncio.run(manager._publish(TID))["state"] == "opened"
    assert publisher.published == [TID]


def test_publish_skips_the_check_when_the_branch_is_already_pushed(tmp_path: Path,
                                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    manager, publisher, _ = _manager(tmp_path, monkeypatch)
    manager.pr_states[TID] = {"state": "failed", "branch": "foundry/x", "commit": "abc", "attempts": 1}
    monkeypatch.setattr(suite_check, "check_patch", _never)
    assert asyncio.run(manager._publish(TID))["state"] == "opened"
    assert publisher.reopened == ["foundry/x"] and publisher.published == []
