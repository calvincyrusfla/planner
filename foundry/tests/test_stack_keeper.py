"""Tests for foundry.stack_keeper: restack a child PR when its parent merged."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from foundry.git_publisher import default_runner
from foundry.stack_keeper import StackKeeper, stack_keeper_interval

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

PARENT_URL = "https://github.com/o/r/pull/1"
CHILD_URL = "https://github.com/o/r/pull/2"


def _git(cwd: Path, *args: str) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    res = subprocess.run(["git", "-c", "user.name=T", "-c", "user.email=t@t", *args], cwd=cwd, env=env,
                         capture_output=True, text=True, check=True)
    return res.stdout.strip()


class FakeQueue:
    def __init__(self, rec: dict[str, Any]) -> None:
        self.store: dict[str, Any] = {"stack:T-2": rec}

    def kv_items(self, prefix: str = "") -> dict[str, Any]:
        return {k: v for k, v in self.store.items() if k.startswith(prefix)}

    def kv_set(self, key: str, value: Any) -> None:
        self.store[key] = value


def _record(**over: Any) -> dict[str, Any]:
    rec = {"task_id": "T-2", "batch": "S-1", "key": "k", "parent_url": PARENT_URL, "parent_branch": "foundry/p",
           "parent_head": "0" * 40, "base": None, "state": "stacked"}
    rec.update(over)
    return rec


def _service(repo: Path, rec: dict[str, Any], pr: bool = True) -> Any:
    states = {"T-2": {"url": CHILD_URL, "branch": "foundry/c"}} if pr else {}
    return SimpleNamespace(repo=repo, running=True,
                           factory=SimpleNamespace(queue=FakeQueue(rec), manager=SimpleNamespace(pr_states=states)))


class GhRunner:
    """Fakes gh; git commands run for real when ``real_git`` else are recorded and answered with success."""

    def __init__(self, parent: dict[str, Any], real_git: bool = False) -> None:
        self.parent = parent
        self.real_git = real_git
        self.calls: list[list[str]] = []

    async def __call__(self, argv: Any, *, cwd: Any = None, timeout: Any = None, env: Any = None) -> Any:
        argv = list(argv)
        self.calls.append(argv)
        if argv[0] == "gh":
            if argv[1:3] == ["pr", "view"]:
                return 0, json.dumps(self.parent), ""
            return 0, "", ""
        if self.real_git:
            return await default_runner(argv, cwd=cwd, timeout=timeout, env=env)
        return 0, "", ""

    def gh_calls(self) -> list[list[str]]:
        return [c for c in self.calls if c[0] == "gh"]


def _keeper(svc: Any, runner: GhRunner) -> StackKeeper:
    return StackKeeper(svc, interval_s=30, runner=runner, gh="gh", git="git")


# ------------------------------------------------------------------ fake runner cases


def test_open_parent_does_nothing(tmp_path: Path) -> None:
    runner = GhRunner({"state": "OPEN", "mergedAt": None, "headRefOid": "a", "baseRefName": "main"})
    svc = _service(tmp_path, _record())

    asyncio.run(_keeper(svc, runner).round())

    assert [c[1:3] for c in runner.calls] == [["pr", "view"]]
    assert svc.factory.queue.store["stack:T-2"]["state"] == "stacked"


def test_closed_parent_marks_gone_and_comments_once(tmp_path: Path) -> None:
    runner = GhRunner({"state": "CLOSED", "mergedAt": None, "headRefOid": "a", "baseRefName": "main"})
    svc = _service(tmp_path, _record())
    keeper = _keeper(svc, runner)

    asyncio.run(keeper.round())
    asyncio.run(keeper.round())

    rec = svc.factory.queue.store["stack:T-2"]
    assert rec["state"] == "gone" and rec["note"]
    comments = [c for c in runner.gh_calls() if c[1:3] == ["pr", "comment"]]
    assert len(comments) == 1 and comments[0][3] == CHILD_URL
    assert [c for c in runner.calls if c[0] == "git"] == []


def test_child_without_a_pr_is_skipped(tmp_path: Path) -> None:
    runner = GhRunner({"state": "CLOSED"})
    svc = _service(tmp_path, _record(), pr=False)

    asyncio.run(_keeper(svc, runner).round())

    assert runner.calls == []


def test_interval_has_a_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOUNDRY_STACK_KEEPER_S", "5")
    assert stack_keeper_interval() == 30
    monkeypatch.setenv("FOUNDRY_STACK_KEEPER_S", "bad")
    assert stack_keeper_interval() == 120


# ------------------------------------------------------------------ real git


class World:
    def __init__(self, root: Path, conflict: bool) -> None:
        self.origin = root / "origin.git"
        self.clone = root / "clone"
        self.other = root / "other"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.origin)], check=True)
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.clone)], check=True,
                       capture_output=True)
        _git(self.clone, "checkout", "-q", "-b", "main")
        (self.clone / "a.txt").write_text("base\n")
        (self.clone / "b.txt").write_text("base\n")
        _git(self.clone, "add", "-A")
        _git(self.clone, "commit", "-q", "-m", "base")
        _git(self.clone, "push", "-q", "origin", "main")
        _git(self.clone, "checkout", "-q", "-b", "foundry/p")
        (self.clone / "a.txt").write_text("parent\n")
        _git(self.clone, "commit", "-qam", "parent")
        _git(self.clone, "push", "-q", "origin", "foundry/p")
        self.parent_head = _git(self.clone, "rev-parse", "HEAD")
        _git(self.clone, "checkout", "-q", "-b", "foundry/c")
        (self.clone / ("a.txt" if conflict else "b.txt")).write_text("child\n")
        _git(self.clone, "commit", "-qam", "child")
        _git(self.clone, "push", "-q", "origin", "foundry/c")
        self.child_head = _git(self.clone, "rev-parse", "HEAD")
        _git(self.clone, "checkout", "-q", "main")
        # squash-merge the parent on the remote's main (a new commit, not the parent's own)
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.other)], check=True, capture_output=True)
        (self.other / "a.txt").write_text("other\n" if conflict else "parent\n")
        _git(self.other, "commit", "-qam", "squash parent")
        _git(self.other, "push", "-q", "origin", "main")
        self.main_head = _git(self.other, "rev-parse", "HEAD")


def _worktrees(clone: Path) -> int:
    return sum(line.startswith("worktree ") for line in _git(clone, "worktree", "list", "--porcelain").splitlines())


def _merged(world: World) -> GhRunner:
    return GhRunner({"state": "MERGED", "mergedAt": "2026-01-01T00:00:00Z", "headRefOid": world.parent_head,
                     "baseRefName": "main"}, real_git=True)


@needs_git
def test_merged_parent_rebases_pushes_and_retargets(tmp_path: Path) -> None:
    world = World(tmp_path, conflict=False)
    before = _git(world.clone, "rev-parse", "HEAD"), _git(world.clone, "status", "--porcelain")
    runner = _merged(world)
    svc = _service(world.clone, _record(parent_head=world.parent_head))

    asyncio.run(_keeper(svc, runner).round())

    rec = svc.factory.queue.store["stack:T-2"]
    assert rec["state"] == "restacked" and rec["old_sha"] == world.child_head
    new_head = _git(world.origin, "rev-parse", "refs/heads/foundry/c")
    assert rec["new_sha"] == new_head != world.child_head
    assert _git(world.origin, "rev-parse", f"{new_head}~1") == world.main_head
    verbs = [[a for a in c[1:] if a not in ("-c", "user.name=Foundry", "user.email=foundry@localhost")][0]
             if c[0] == "git" else "gh-" + c[2] for c in runner.calls]
    order = ["gh-view", "fetch", "worktree", "merge-base", "rebase", "push", "gh-edit"]
    positions = [verbs.index(v) for v in order]
    assert positions == sorted(positions)
    push = next(c for c in runner.calls if c[:2] == ["git", "push"])
    assert push[2] == f"--force-with-lease=foundry/c:{world.child_head}"
    edit = next(c for c in runner.gh_calls() if c[1:3] == ["pr", "edit"])
    assert edit[3:] == [CHILD_URL, "--base", "main"]
    assert _worktrees(world.clone) == 1
    assert (_git(world.clone, "rev-parse", "HEAD"), _git(world.clone, "status", "--porcelain")) == before


@needs_git
def test_conflict_aborts_marks_conflict_and_comments_once(tmp_path: Path) -> None:
    world = World(tmp_path, conflict=True)
    runner = _merged(world)
    svc = _service(world.clone, _record(parent_head=world.parent_head))
    keeper = _keeper(svc, runner)

    asyncio.run(keeper.round())
    asyncio.run(keeper.round())

    rec = svc.factory.queue.store["stack:T-2"]
    assert rec["state"] == "conflict" and rec["files"] == ["a.txt"]
    assert _git(world.origin, "rev-parse", "refs/heads/foundry/c") == world.child_head
    comments = [c for c in runner.gh_calls() if c[1:3] == ["pr", "comment"]]
    assert len(comments) == 1
    assert comments[0][4:] == ["--body", f"Could not restack onto main after {PARENT_URL} merged: conflicts in "
                                         "a.txt. It needs a rebuild on the new base."]
    assert not any(c[:2] == ["git", "push"] for c in runner.calls)
    assert not any(c[1:3] == ["pr", "edit"] for c in runner.gh_calls())
    assert any(c[:3] == ["git", "-c", "user.name=Foundry"] and "rebase" in c and "--abort" not in c
               for c in runner.calls)
    assert any(c[-2:] == ["rebase", "--abort"] for c in runner.calls)
    assert _worktrees(world.clone) == 1


def test_worktree_is_removed_when_git_fails(tmp_path: Path) -> None:
    class Failing(GhRunner):
        async def __call__(self, argv: Any, *, cwd: Any = None, timeout: Any = None, env: Any = None) -> Any:
            if argv[0] == "git" and "merge-base" in argv:
                self.calls.append(list(argv))
                return 1, "", "boom"
            return await super().__call__(argv, cwd=cwd, timeout=timeout, env=env)

    runner = Failing({"state": "MERGED", "headRefOid": "a" * 40, "baseRefName": "main"})
    svc = _service(tmp_path, _record())

    asyncio.run(_keeper(svc, runner).round())

    assert svc.factory.queue.store["stack:T-2"]["state"] == "stacked"
    removes = [c for c in runner.calls if c[:3] == ["git", "worktree", "remove"]]
    assert len(removes) == 1 if any(c[:3] == ["git", "worktree", "add"] for c in runner.calls) else not removes
