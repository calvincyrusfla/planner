"""Builder edit mode: Claude edits a private piece workspace and its changes become the submission.

Real git repositories (skipped when git is missing), a real queue and a fake
Claude client that edits files in the directory it is called with.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import foundry.builder_worker as bw
from foundry.builder_worker import BUILDER_CONTRACT, EDIT_CONTRACT, BuilderWorker
from foundry.orchestrator_queue import FoundryQueue
from foundry.schemas import BuilderSubmission, ClaudeResult, SubtaskAllocation, TaskAssignment, TaskSizingDirective

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

TEAM = "alpha"
TASK = "T-5"
APP = "pkg/app.py"
OTHER = "pkg/other.py"
APP_SRC = b"A = 1\nB = 2\n"
APP_NEW = b"A = 1\nB = 3\n"
APP_DIFF = "--- a/pkg/app.py\n+++ b/pkg/app.py\n@@ -1,2 +1,2 @@\n A = 1\n-B = 2\n+B = 3\n"


def _env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@e", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@e")
    return env


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, env=_env(), check=True, capture_output=True,
                          text=True).stdout


class _Foreman:
    def condense_error_trace(self, trace: str) -> str:
        return trace[-300:]


class FakeClient:
    """Records prompts and working directories; ``act(cwd)`` may edit files and returns the reply."""

    def __init__(self, act: Callable[[Path | None], str]) -> None:
        self.act = act
        self.prompts: list[str] = []
        self.cwds: list[str | None] = []

    async def run(
        self, prompt: str, *, agent_id: str, role: Any, cwd: str | None = None, timeout: float = 600
    ) -> ClaudeResult:
        self.prompts.append(prompt)
        self.cwds.append(cwd)
        text = self.act(Path(cwd) if cwd is not None else None)
        return ClaudeResult(text=text, input_tokens=10, output_tokens=10)


class Rig:
    """A seeded git repo, a snapshot worktree of it, a real queue and one builder."""

    def __init__(self, tmp_path: Path, act: Callable[[Path | None], str], *, mode: str = "edit",
                 base: Path | None = None) -> None:
        self.tmp = tmp_path
        repo = tmp_path / "repo"
        git(tmp_path, "init", "-q", "-b", "main", str(repo))
        git(repo, "config", "core.autocrlf", "false")
        (repo / "pkg").mkdir()
        (repo / APP).write_bytes(APP_SRC)
        (repo / OTHER).write_bytes(b"C = 1\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "seed")
        self.snapshot = tmp_path / "snapshot"
        git(repo, "-c", "core.autocrlf=false", "worktree", "add", "--detach", "-q", str(self.snapshot), "HEAD")
        self.queue = FoundryQueue(tmp_path / "foundry.db")
        self.queue.add_task(TASK, "Edit app", "Set B to 3 in pkg/app.py", TEAM)
        self.queue.apply_sizing(TaskSizingDirective(
            task_id=TASK, team_id=TEAM, complexity="S", allocated_builders=1, rationale="test",
            subtasks=[SubtaskAllocation(subtask_id=f"{TASK}.1", title="Set B to 3",
                                        acceptance_criteria=["B is 3"], allowed_files=[APP])],
        ))
        self.client = FakeClient(act)
        chosen: Path = self.snapshot if base is None else base

        def base_for(_task_id: str) -> Path:
            return chosen

        self.worker = BuilderWorker(TEAM, 1, self.queue, self.client, None, None, None, None, None, _Foreman(),
                                    poll=0.01, base_for=base_for)
        self.worker.config = SimpleNamespace(builder_mode=mode, db_path=tmp_path / "foundry.db")

    def run(self) -> str:
        ticket = self.queue.tickets_for_task(TASK)[0]
        claimed = self.queue.claim_ticket(TEAM, ticket.slot)
        assert claimed is not None
        return asyncio.run(self.worker.process_ticket(claimed))

    def payloads(self, kind: str) -> list[dict[str, Any]]:
        return [e["payload"] for e in self.queue.events(task_id=TASK, kind=kind)]

    def only_cwd(self) -> Path:
        assert len(self.client.cwds) == 1
        cwd = self.client.cwds[0]
        assert cwd is not None
        return Path(cwd)


def edit_app(cwd: Path | None) -> str:
    assert cwd is not None
    (cwd / APP).write_bytes(APP_NEW)
    return "Set B to 3."


def edit_other(cwd: Path | None) -> str:
    assert cwd is not None
    (cwd / OTHER).write_bytes(b"C = 2\n")
    return "Changed C."


def do_nothing(cwd: Path | None) -> str:
    return "Nothing needed changing."


def blocked_reply(cwd: Path | None) -> str:
    return "I looked around.\nBLOCKED: need the body of `configure`"


def diff_reply(cwd: Path | None) -> str:
    return f"Here is the change.\n\n```diff\n{APP_DIFF}```\n"


def test_edit_mode_turns_the_edits_into_a_ready_submission(tmp_path: Path) -> None:
    rig = Rig(tmp_path, edit_app)
    assert rig.run() == "review"
    prompt = rig.client.prompts[0]
    assert EDIT_CONTRACT in prompt
    assert BUILDER_CONTRACT not in prompt
    cwd = rig.only_cwd()
    assert cwd.parent.name == "pieces"
    assert cwd.name.endswith("-a1")
    assert not cwd.exists()
    item = rig.worker.review_inbox.get_nowait()
    assert item.submission.status == "ready_for_review"
    assert item.submission.modified_files == [APP]
    assert "+B = 3" in item.submission.diff and "-B = 2" in item.submission.diff
    assert (rig.snapshot / APP).read_bytes() == APP_SRC
    assert rig.payloads("submission")[0]["status"] == "ready_for_review"


def test_edit_outside_the_allowed_files_is_sent_back(tmp_path: Path) -> None:
    rig = Rig(tmp_path, edit_other)
    assert rig.run() == "retry"
    retries = rig.payloads("retry")
    assert [p["reason"] for p in retries] == ["outside_files"]
    assert OTHER in retries[0]["fix_hint"] and APP in retries[0]["fix_hint"]
    assert rig.worker.review_inbox.empty()
    assert not rig.only_cwd().exists()


def test_no_edits_is_blocked(tmp_path: Path) -> None:
    rig = Rig(tmp_path, do_nothing)
    assert rig.run() == "retry"
    assert rig.payloads("submission")[0]["status"] == "blocked"
    retries = rig.payloads("retry")
    assert retries[0]["reason"] == "blocked"
    assert "no changes were made" in retries[0]["fix_hint"]
    assert not rig.only_cwd().exists()


def test_blocked_reply_is_blocked(tmp_path: Path) -> None:
    rig = Rig(tmp_path, blocked_reply)
    assert rig.run() == "retry"
    assert rig.payloads("submission")[0]["status"] == "blocked"
    retries = rig.payloads("retry")
    assert retries[0]["reason"] == "blocked"
    assert "BLOCKED: need the body of `configure`" in retries[0]["fix_hint"]
    assert not rig.only_cwd().exists()


def test_workspace_error_releases_without_an_attempt(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "pkg").mkdir()
    (plain / APP).write_bytes(APP_SRC)
    rig = Rig(tmp_path, edit_app, base=plain)
    assert rig.run() == "released"
    assert rig.client.prompts == []
    assert rig.queue.tickets_for_task(TASK)[0].attempt == 0
    assert rig.payloads("workspace_failed")
    pieces = tmp_path / "pieces"
    assert not pieces.exists() or not any(pieces.iterdir())


def test_diff_mode_is_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    real = bw.parse_builder_reply

    def counting(text: str, assignment: TaskAssignment, *, workspace: str | Path | None = None) -> BuilderSubmission:
        calls.append(text)
        return real(text, assignment, workspace=workspace)

    monkeypatch.setattr(bw, "parse_builder_reply", counting)
    rig = Rig(tmp_path, diff_reply, mode="diff")
    assert rig.run() == "review"
    prompt = rig.client.prompts[0]
    assert BUILDER_CONTRACT in prompt
    assert EDIT_CONTRACT not in prompt
    assert len(calls) == 1
    assert rig.only_cwd() == rig.snapshot
    assert not (tmp_path / "pieces").exists()
    item = rig.worker.review_inbox.get_nowait()
    assert "+B = 3" in item.submission.diff
