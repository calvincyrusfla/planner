"""Foundry's work folders live outside the repository, and stray writes are caught.

The stray-write tests use real git for the workspace and a fake ``git status``
of the user's folder (one real-git test checks the whole path).
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import foundry.builder_worker as bw
from foundry import config as config_module
from foundry import floor_manager, serve
from foundry.base_snapshot import BaseSnapshots
from foundry.builder_worker import BuilderWorker
from foundry.config import FoundryConfig, WorkRootError, default_work_root
from foundry.git_publisher import GitPublisher
from foundry.orchestrator_queue import FoundryQueue
from foundry.schemas import ClaudeResult, SubtaskAllocation, TaskSizingDirective

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

TEAM = "alpha"
TASK = "T-7"
APP = "pkg/app.py"
APP_SRC = b"A = 1\nB = 2\n"
APP_NEW = b"A = 1\nB = 3\n"


# --------------------------------------------------------------------------- the setting


def test_default_on_windows() -> None:
    assert default_work_root({"LOCALAPPDATA": "C:/Users/me/AppData/Local"}, "win32") == Path(
        "C:/Users/me/AppData/Local") / "foundry"


def test_default_on_windows_without_localappdata(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert default_work_root({}, "win32") == tmp_path / "AppData" / "Local" / "foundry"


def test_default_elsewhere(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert default_work_root({}, "linux") == tmp_path / ".local" / "share" / "foundry"
    assert default_work_root({"XDG_DATA_HOME": "/xdg/data"}, "linux") == Path("/xdg/data") / "foundry"


def test_env_setting(tmp_path: Path) -> None:
    assert FoundryConfig().work_root is None
    cfg = FoundryConfig.from_env({"FOUNDRY_WORK_ROOT": str(tmp_path / "work")})
    assert cfg.work_root == str(tmp_path / "work")
    assert cfg.resolved_work_root() == (tmp_path / "work").resolve()
    assert FoundryConfig(work_root="  ").work_root is None


def test_unset_with_the_default_database_is_the_platform_folder(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(config_module, "default_work_root", lambda: tmp_path / "platform")
    assert FoundryConfig().resolved_work_root(tmp_path / "repo") == (tmp_path / "platform").resolve()


def test_unset_with_a_moved_database_stays_next_to_it(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(config_module, "default_work_root", lambda: tmp_path / "platform")
    cfg = FoundryConfig(db_path=tmp_path / "data" / "foundry.db")
    assert cfg.resolved_work_root(tmp_path / "repo") == (tmp_path / "data").resolve()
    # a database inside the served repository never pulls the work folders in with it
    inside = FoundryConfig(db_path=tmp_path / "repo" / "data" / "foundry.db")
    assert inside.resolved_work_root(tmp_path / "repo") == (tmp_path / "platform").resolve()
    assert inside.check_work_root(tmp_path / "repo") == (tmp_path / "platform").resolve()


def test_work_root_inside_the_repo_is_refused(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    for inside in (repo, repo / "foundry" / "data"):
        with pytest.raises(WorkRootError, match="inside the repository"):
            FoundryConfig(work_root=str(inside)).check_work_root(repo)
    outside = tmp_path / "work"
    assert FoundryConfig(work_root=str(outside)).check_work_root(repo) == outside.resolve()


def test_service_refuses_to_start_with_a_work_root_inside_the_repo(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    config = FoundryConfig(db_path=tmp_path / "foundry.db", work_root=str(repo / "work"))
    service = serve.Service(config, repo=repo, claude=object(), gemma=None)
    with pytest.raises(WorkRootError):
        asyncio.run(service.start())
    assert service.factory is None and not service.running


def test_old_folders_are_mentioned_and_left_alone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    data = tmp_path / "old-data"
    (data / "pieces").mkdir(parents=True)
    monkeypatch.setattr(config_module, "DEFAULT_DATA_DIR", data)
    repo = tmp_path / "repo"
    repo.mkdir()
    config = FoundryConfig(db_path=tmp_path / "foundry.db", work_root=str(tmp_path / "work"))
    with caplog.at_level(logging.INFO, logger="foundry.serve"):
        serve.Service(config, repo=repo, claude=object(), gemma=None)._check_work_root()
    assert [r.getMessage().count("old work folders under foundry/data can be deleted")
            for r in caplog.records] == [1]
    assert (data / "pieces").is_dir()


# --------------------------------------------------------------------------- the three roots


def test_service_passes_the_roots_to_publisher_and_snapshots(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: dict[str, Any] = {}

    def fake_build_factory(_config: Any, **kwargs: Any) -> str:
        seen.update(kwargs)
        return "factory"

    monkeypatch.setattr(floor_manager, "build_factory", fake_build_factory)
    work = tmp_path / "work"
    config = FoundryConfig(db_path=tmp_path / "foundry.db", work_root=str(work), auto_pr=True)
    service = serve.Service(config, repo=tmp_path / "repo", claude=object(), gemma=None)
    assert service._build() == "factory"
    assert isinstance(seen["publisher"], GitPublisher)
    assert seen["publisher"].worktree_root == work.resolve() / "worktrees"
    assert isinstance(seen["snapshots"], BaseSnapshots)
    assert seen["snapshots"].root == work.resolve() / "base"


# --------------------------------------------------------------------------- piece workspaces and the guard


def _git(cwd: Path, *args: str) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@e", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@e")
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout


class _Foreman:
    def condense_error_trace(self, trace: str) -> str:
        return trace[-300:]


class _Client:
    def __init__(self, act: Callable[[Path], None]) -> None:
        self.act = act
        self.cwds: list[Path] = []

    async def run(
        self, prompt: str, *, agent_id: str, role: Any, cwd: str | None = None, timeout: float = 600
    ) -> ClaudeResult:
        assert cwd is not None
        self.cwds.append(Path(cwd))
        self.act(Path(cwd))
        return ClaudeResult(text="Done.", input_tokens=10, output_tokens=10)


class Rig:
    """A seeded repo (the user's folder), a snapshot of it, a real queue and one builder."""

    def __init__(self, tmp_path: Path, act: Callable[[Path], None], config: Any = None) -> None:
        self.repo = tmp_path / "repo"
        _git(tmp_path, "init", "-q", "-b", "main", str(self.repo))
        _git(self.repo, "config", "core.autocrlf", "false")
        (self.repo / "pkg").mkdir()
        (self.repo / APP).write_bytes(APP_SRC)
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", "seed")
        snapshot = tmp_path / "snapshot"
        _git(self.repo, "-c", "core.autocrlf=false", "worktree", "add", "--detach", "-q", str(snapshot), "HEAD")
        self.queue = FoundryQueue(tmp_path / "foundry.db")
        self.queue.add_task(TASK, "Edit app", "Set B to 3", TEAM)
        self.queue.apply_sizing(TaskSizingDirective(
            task_id=TASK, team_id=TEAM, complexity="S", allocated_builders=1, rationale="test",
            subtasks=[SubtaskAllocation(subtask_id=f"{TASK}.1", title="Set B", acceptance_criteria=["B is 3"],
                                        allowed_files=[APP])],
        ))
        self.client = _Client(act)

        def base_for(_task_id: str) -> Path:
            return snapshot

        self.worker = BuilderWorker(TEAM, 1, self.queue, self.client, None, None, None, None, None, _Foreman(),
                                    poll=0.01, base_for=base_for, repo_root=self.repo)
        self.worker.config = config or SimpleNamespace(builder_mode="edit", db_path=tmp_path / "foundry.db")

    def run(self) -> str:
        ticket = self.queue.tickets_for_task(TASK)[0]
        claimed = self.queue.claim_ticket(TEAM, ticket.slot)
        assert claimed is not None
        return asyncio.run(self.worker.process_ticket(claimed))

    def payloads(self, kind: str) -> list[dict[str, Any]]:
        return [e["payload"] for e in self.queue.events(task_id=TASK, kind=kind)]


def _edit_app(cwd: Path) -> None:
    (cwd / APP).write_bytes(APP_NEW)


@pytest.fixture
def fake_status(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[..., list[list[str]]]]:
    """``fake_status(out1, out2)``: each ``git status`` call answers the next output (an exception raises it)."""

    def install(*answers: Any, returncode: int = 0) -> list[list[str]]:
        calls: list[list[str]] = []
        queue = list(answers)

        def run(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
            assert "status" in argv and "--porcelain" in argv and "--untracked-files=all" in argv
            calls.append(list(argv))
            answer = queue.pop(0)
            if isinstance(answer, BaseException):
                raise answer
            return subprocess.CompletedProcess(argv, returncode, answer, "")

        monkeypatch.setattr(bw.subprocess, "run", run)
        return calls

    yield install


@needs_git
def test_workspaces_go_under_the_work_root(tmp_path: Path) -> None:
    work = tmp_path / "work"
    config = FoundryConfig(db_path=tmp_path / "foundry.db", work_root=str(work), builder_mode="edit",
                           prompt_archive=False)
    rig = Rig(tmp_path, _edit_app, config)
    assert rig.run() == "review"
    assert [c.parent for c in rig.client.cwds] == [work.resolve() / "pieces"]


@needs_git
def test_new_allowed_file_is_sent_back_and_kept(tmp_path: Path, fake_status: Any) -> None:
    def stray(cwd: Path) -> None:
        _edit_app(cwd)
        (tmp_path / "repo" / APP).write_bytes(APP_NEW)

    rig = Rig(tmp_path, stray)
    calls = fake_status("?? notes.txt\n", f"?? notes.txt\n M {APP}\n")
    assert rig.run() == "retry"
    assert len(calls) == 2
    assert [p["paths"] for p in rig.payloads("stray_write")] == [[APP]]
    retry = rig.payloads("retry")[0]
    assert retry["reason"] == "stray_write"
    assert retry["fix_hint"] == (f"Wrote {APP} into the user's folder instead of the workspace; "
                                 "write only inside the current working directory.")
    assert rig.worker.review_inbox.empty()
    assert (rig.repo / APP).read_bytes() == APP_NEW  # the user's file is never deleted or restored


@needs_git
@pytest.mark.parametrize("before, after", [
    ("?? notes.txt\n", "?? notes.txt\n?? other/scratch.py\n"),  # unrelated untracked files
    (f" M {APP}\n", f" M {APP}\n"),  # already changed before the attempt
])
def test_other_changes_are_ignored(tmp_path: Path, fake_status: Any, before: str, after: str) -> None:
    rig = Rig(tmp_path, _edit_app)
    fake_status(before, after)
    assert rig.run() == "review"
    assert rig.payloads("stray_write") == []


@needs_git
@pytest.mark.parametrize("answers, returncode", [
    ((OSError("no git"), OSError("no git")), 0),
    (("", subprocess.TimeoutExpired("git", 60)), 0),
    (("", f"?? {APP}\n"), 128),
])
def test_git_failure_never_fails_the_attempt(
    tmp_path: Path, fake_status: Any, answers: tuple[Any, ...], returncode: int
) -> None:
    rig = Rig(tmp_path, _edit_app)
    fake_status(*answers, returncode=returncode)
    assert rig.run() == "review"
    assert rig.payloads("stray_write") == []


@needs_git
def test_stray_write_with_real_git(tmp_path: Path) -> None:
    def stray(cwd: Path) -> None:
        _edit_app(cwd)
        (tmp_path / "repo" / APP).write_bytes(APP_NEW)

    rig = Rig(tmp_path, stray)
    assert rig.run() == "retry"
    assert [p["paths"] for p in rig.payloads("stray_write")] == [[APP]]


def test_served_repo_prefers_the_context_engine(tmp_path: Path) -> None:
    worker = BuilderWorker.__new__(BuilderWorker)
    worker.context_engine = SimpleNamespace(repo_root=tmp_path / "engine")
    worker.repo_root = str(tmp_path / "service")
    assert worker._served_repo() == tmp_path / "engine"
    worker.context_engine = None
    assert worker._served_repo() == tmp_path / "service"
    worker.repo_root = None
    assert worker._served_repo() is None


def test_status_lines_are_parsed(tmp_path: Path, fake_status: Any) -> None:
    fake_status(f'?? "a b.py"\nR  old.py -> new.py\n M {APP}\n')
    assert bw._porcelain_paths(tmp_path) == {"a b.py", "new.py", APP}
