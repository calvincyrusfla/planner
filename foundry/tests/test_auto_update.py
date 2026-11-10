"""Tests for the auto-update check of foundry/update_watcher.py."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from foundry import self_update
from foundry.board_dispatch import BoardRefused
from foundry.update_watcher import AUTO_UPDATE_CHECK_S, UpdateWatcher

REPO = "Owner/Repo"
HEAD = "a" * 40
REMOTE = "b" * 40
OTHER_REMOTE = "c" * 40
LATER = AUTO_UPDATE_CHECK_S + 1


def _listing(sha: str, branch: str = "main") -> tuple[int, str, str]:
    return 0, f"{sha}\trefs/heads/{branch}\n", ""


class FakeGit:
    """Stands in for ``self_update._git``; an answer that is an exception is raised."""

    def __init__(self) -> None:
        self.answers: dict[str, Any] = {
            "rev-parse": (0, f"{HEAD}\n", ""),
            "symbolic-ref": (0, "main\n", ""),
            "ls-remote": _listing(REMOTE),
            "merge-base": (1, "", ""),  # not an ancestor
        }
        self.calls: list[tuple[str, ...]] = []
        self.repos: list[Any] = []

    async def __call__(self, repo: Any, *args: str, timeout: float = 0.0) -> tuple[int, str, str]:
        self.calls.append(args)
        self.repos.append(repo)
        answer = self.answers[args[0]]
        if isinstance(answer, Exception):
            raise answer
        result: tuple[int, str, str] = answer
        return result


@dataclass
class FakeBoard:
    updates: list[dict[str, Any]] = field(default_factory=list)
    ops: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    op_error: Exception | None = None
    op_result: Any = None

    def state(self) -> dict[str, Any]:
        return {"foundryUpdates": [dict(u) for u in self.updates]}

    def op(self, type_: str, args: dict[str, Any]) -> Any:
        self.ops.append((type_, dict(args)))
        if self.op_error is not None:
            raise self.op_error
        return self.op_result if self.op_result is not None else {"ok": True}

    def requests(self) -> list[dict[str, Any]]:
        return [a for t, a in self.ops if t == "requestFoundryUpdate"]


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class FakeService:
    def __init__(self, auto_update: bool = True, busy: bool = False) -> None:
        self.repo = Path("/repo")
        self.config = SimpleNamespace(auto_update=auto_update)
        task = {"state": "building"}
        ticket = SimpleNamespace(status="claimed", in_review=False, task_id="F-1")
        queue = SimpleNamespace(tickets=lambda: [ticket] if busy else [], get_task=lambda _id: task)
        self.factory = SimpleNamespace(queue=queue)
        self.restarts = 0

    def request_restart(self) -> None:
        self.restarts += 1


async def _no_apply(repo: Any, settings: Any) -> dict[str, Any]:
    raise AssertionError("apply must not run")


def _watcher(board: FakeBoard, clock: FakeClock, service: Any = None, apply: Any = _no_apply) -> UpdateWatcher:
    return UpdateWatcher(service or FakeService(), board, repo_slug=REPO, host="pc",
                         service_id="foundry@pc:owner/repo", apply=apply, clock=clock)


def _poll(w: UpdateWatcher) -> Any:
    return asyncio.run(w.poll_once())


@pytest.fixture
def git(monkeypatch: pytest.MonkeyPatch) -> FakeGit:
    fake = FakeGit()
    monkeypatch.setattr(self_update, "_git", fake)
    return fake


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.mark.parametrize("service", [FakeService(auto_update=False), SimpleNamespace(repo=Path("/repo"), factory=None)])
def test_off_runs_no_git_and_requests_nothing(git: FakeGit, clock: FakeClock, service: Any) -> None:
    board = FakeBoard()
    assert _poll(_watcher(board, clock, service)) is None
    assert git.calls == [] and board.ops == []


def test_new_remote_sha_sends_one_request_by_auto(git: FakeGit, clock: FakeClock) -> None:
    board = FakeBoard()
    assert _poll(_watcher(board, clock)) is None
    assert board.ops == [("requestFoundryUpdate", {"repo": REPO, "by": "auto"})]
    assert git.calls == [
        ("rev-parse", "HEAD"),
        ("symbolic-ref", "--short", "HEAD"),
        ("ls-remote", "origin", "refs/heads/main"),
        ("merge-base", "--is-ancestor", REMOTE, "HEAD"),
    ]
    assert git.repos[0] == Path("/repo")


def test_same_remote_sha_is_not_requested_twice(git: FakeGit, clock: FakeClock) -> None:
    board = FakeBoard()
    w = _watcher(board, clock)
    _poll(w)
    clock.now += LATER
    _poll(w)
    assert len(board.requests()) == 1


def test_a_newer_remote_sha_is_requested(git: FakeGit, clock: FakeClock) -> None:
    board = FakeBoard()
    w = _watcher(board, clock)
    _poll(w)
    git.answers["ls-remote"] = _listing(OTHER_REMOTE)
    clock.now += LATER
    _poll(w)
    assert len(board.requests()) == 2


def test_git_runs_at_most_once_per_minute(git: FakeGit, clock: FakeClock) -> None:
    board = FakeBoard()
    git.answers["ls-remote"] = _listing(HEAD)
    w = _watcher(board, clock)
    _poll(w)
    count = len(git.calls)
    clock.now += AUTO_UPDATE_CHECK_S - 1
    _poll(w)
    assert len(git.calls) == count
    clock.now += 2
    _poll(w)
    assert len(git.calls) == 2 * count


def test_remote_equal_to_head_requests_nothing(git: FakeGit, clock: FakeClock) -> None:
    board = FakeBoard()
    git.answers["ls-remote"] = _listing(HEAD)
    _poll(_watcher(board, clock))
    assert board.ops == []


def test_remote_contained_in_head_requests_nothing_even_after_a_restart(git: FakeGit, clock: FakeClock) -> None:
    board = FakeBoard()
    git.answers["merge-base"] = (0, "", "")  # the local checkout is ahead of origin
    _poll(_watcher(board, clock))
    _poll(_watcher(board, clock))  # a new watcher remembers nothing, as after a restart
    assert board.ops == []


def test_remote_commit_not_fetched_yet_is_requested(git: FakeGit, clock: FakeClock) -> None:
    board = FakeBoard()
    git.answers["merge-base"] = (128, "", "fatal: Not a valid commit name")
    _poll(_watcher(board, clock))
    assert len(board.requests()) == 1


def test_queued_request_blocks_a_new_one(git: FakeGit, clock: FakeClock) -> None:
    board = FakeBoard([{"id": "U-001", "repo": "owner/repo", "status": "queued", "now": False}])
    _poll(_watcher(board, clock, FakeService(busy=True)))  # busy: stays queued
    assert board.ops == [] and git.calls == []


def test_claimed_request_blocks_a_new_one(git: FakeGit, clock: FakeClock) -> None:
    board = FakeBoard([{"id": "U-001", "repo": "owner/repo", "status": "claimed"}])
    _poll(_watcher(board, clock))
    assert board.ops == [] and git.calls == []


@pytest.mark.parametrize("update", [
    {"id": "U-001", "repo": "other/repo", "status": "claimed"},
    {"id": "U-001", "repo": "owner/repo", "status": "done"},
])
def test_finished_or_foreign_request_does_not_block(git: FakeGit, clock: FakeClock, update: dict[str, Any]) -> None:
    board = FakeBoard([update])
    _poll(_watcher(board, clock))
    assert len(board.requests()) == 1


def test_the_normal_path_claims_the_auto_request(git: FakeGit, clock: FakeClock) -> None:
    board = FakeBoard()
    applied: list[Any] = []

    async def apply(repo: Any, settings: Any) -> dict[str, Any]:
        applied.append(repo)
        return {"status": "done", "commit_before": HEAD, "commit_after": REMOTE, "message": "pulled"}

    service = FakeService()
    w = _watcher(board, clock, service, apply)
    _poll(w)
    board.updates.append({"id": "U-001", "repo": "owner/repo", "status": "queued", "now": False})
    assert _poll(w) == "U-001"
    assert applied == [Path("/repo")] and service.restarts == 1
    assert [t for t, _ in board.ops] == ["requestFoundryUpdate", "claimFoundryUpdate", "finishFoundryUpdate"]


@pytest.mark.parametrize("step, answer", [
    ("rev-parse", (128, "", "fatal: bad HEAD")),
    ("symbolic-ref", (128, "", "fatal: ref HEAD is not a symbolic ref")),
    ("ls-remote", (128, "", "fatal: could not read Username")),
    ("ls-remote", self_update._GitError("git ls-remote timed out")),
    ("merge-base", self_update._GitError("cannot run git")),
])
def test_git_failure_requests_nothing_and_retries(git: FakeGit, clock: FakeClock, step: str, answer: Any) -> None:
    board = FakeBoard()
    good = git.answers[step]
    git.answers[step] = answer
    w = _watcher(board, clock)
    assert _poll(w) is None
    assert board.ops == [] and w.last_error is None
    git.answers[step] = good
    clock.now += LATER
    _poll(w)
    assert len(board.requests()) == 1


def test_branch_missing_on_origin_requests_nothing(git: FakeGit, clock: FakeClock) -> None:
    board = FakeBoard()
    git.answers["ls-remote"] = (0, "", "")
    _poll(_watcher(board, clock))
    assert board.ops == []


@pytest.mark.parametrize("error, result", [
    (RuntimeError("board down"), None),
    (BoardRefused("already has an open update request"), None),
    (None, {"ok": False, "reason": "no"}),
])
def test_board_failure_is_retried_for_the_same_sha(
    git: FakeGit, clock: FakeClock, error: Exception | None, result: Any,
) -> None:
    board = FakeBoard(op_error=error, op_result=result)
    w = _watcher(board, clock)
    assert _poll(w) is None
    board.op_error = board.op_result = None
    clock.now += LATER
    _poll(w)
    assert len(board.requests()) == 2


def test_git_runs_non_interactively(monkeypatch: pytest.MonkeyPatch, clock: FakeClock) -> None:
    answers = {
        "rev-parse": (0, f"{HEAD}\n".encode()),
        "symbolic-ref": (0, b"main\n"),
        "ls-remote": (0, f"{REMOTE}\trefs/heads/main\n".encode()),
        "merge-base": (1, b""),
    }
    seen: list[tuple[tuple[str, ...], dict[str, Any]]] = []

    class FakeProc:
        def __init__(self, returncode: int, out: bytes) -> None:
            self.returncode = returncode
            self.out = out

        async def communicate(self) -> tuple[bytes, bytes]:
            return self.out, b""

    async def fake_exec(*cmd: str, **kw: Any) -> FakeProc:
        seen.append((cmd, kw))
        return FakeProc(*answers[cmd[1]])

    monkeypatch.setenv("GIT_DIR", "/elsewhere")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    board = FakeBoard()
    _poll(_watcher(board, clock))
    assert len(board.requests()) == 1
    assert [cmd[1] for cmd, _ in seen] == ["rev-parse", "symbolic-ref", "ls-remote", "merge-base"]
    for _, kw in seen:
        assert kw["stdin"] == asyncio.subprocess.DEVNULL
        assert kw["env"]["GIT_TERMINAL_PROMPT"] == "0"
        assert "GIT_DIR" not in kw["env"]
