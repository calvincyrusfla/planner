"""Tests for the dashboard restart, refusal events and refusal back-off of foundry/update_watcher.py."""

from __future__ import annotations

import asyncio
import logging
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from foundry import board_dispatch, self_update, update_watcher
from foundry.board_dispatch import BoardRefused, BoardUnavailable
from foundry.update_watcher import (
    AUTO_UPDATE_CHECK_S, AUTO_UPDATE_REFUSED_BACKOFF_S, MAX_MESSAGE, UpdateWatcher,
)

REPO = "Owner/Repo"
HEAD = "a" * 40
REMOTE = "b" * 40
OTHER_REMOTE = "c" * 40
LATER = AUTO_UPDATE_CHECK_S + 1
RESTARTED = "; dashboard restarted"
NOT_RUNNING = "; dashboard not restarted: the dashboard is not running"


def _listing(sha: str) -> tuple[int, str, str]:
    return 0, f"{sha}\trefs/heads/main\n", ""


class FakeGit:
    """Stands in for ``self_update._git``; an answer that is an exception is raised."""

    def __init__(self) -> None:
        self.answers: dict[str, Any] = {
            "diff": (0, "department/server.mjs\n", ""),
            "rev-parse": (0, f"{HEAD}\n", ""),
            "symbolic-ref": (0, "main\n", ""),
            "ls-remote": _listing(REMOTE),
            "merge-base": (1, "", ""),
        }
        self.calls: list[tuple[str, ...]] = []
        self.timeouts: list[float] = []

    async def __call__(self, repo: Any, *args: str, timeout: float = 0.0) -> tuple[int, str, str]:
        self.calls.append(args)
        self.timeouts.append(timeout)
        answer = self.answers[args[0]]
        if isinstance(answer, Exception):
            raise answer
        result: tuple[int, str, str] = answer
        return result

    def count(self, name: str) -> int:
        return sum(1 for c in self.calls if c[0] == name)


class FakeDashboard:
    """Stands in for ``_post_dashboard_restart``."""

    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.error: Exception | None = None
        self.calls = 0

    def __call__(self) -> None:
        self.calls += 1
        self.events.append("dashboard")
        if self.error is not None:
            raise self.error


class FakeQueue:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.events: list[tuple[str, str | None, dict[str, Any]]] = []

    def tickets(self) -> list[Any]:
        return []

    def record_event(self, kind: str, task_id: str | None, payload: dict[str, Any] | None = None) -> int:
        if self.error is not None:
            raise self.error
        self.events.append((kind, task_id, dict(payload or {})))
        return len(self.events)


class FakeService:
    def __init__(self, events: list[str], auto_update: bool = False, queue: FakeQueue | None = None) -> None:
        self.repo = Path("/repo")
        self.config = SimpleNamespace(auto_update=auto_update)
        self.queue = queue or FakeQueue()
        self.factory = SimpleNamespace(queue=self.queue)
        self.events = events
        self.restarts = 0

    def request_restart(self) -> None:
        self.restarts += 1
        self.events.append("foundry")


class FakeBoard:
    """A board whose ``finishFoundryUpdate`` raises ``finish_errors`` one by one, then succeeds."""

    def __init__(self, events: list[str], updates: list[dict[str, Any]] | None = None,
                 finish_errors: list[Exception] | None = None) -> None:
        self.events = events
        self.updates = list(updates or [])
        self.finish_errors = list(finish_errors or [])
        self.ops: list[tuple[str, dict[str, Any]]] = []

    def state(self) -> dict[str, Any]:
        return {"foundryUpdates": [dict(u) for u in self.updates]}

    def op(self, type_: str, args: dict[str, Any]) -> Any:
        self.ops.append((type_, dict(args)))
        if type_ == "finishFoundryUpdate":
            if self.finish_errors:
                self.events.append("finish-error")
                raise self.finish_errors.pop(0)
            self.events.append("finish")
        return {"ok": True}

    def finishes(self) -> list[dict[str, Any]]:
        return [a for t, a in self.ops if t == "finishFoundryUpdate"]

    def requests(self) -> list[dict[str, Any]]:
        return [a for t, a in self.ops if t == "requestFoundryUpdate"]


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Env:
    """A watcher with fakes for git, the board, the dashboard call, the clock and the sleep."""

    def __init__(self, git: FakeGit, dashboard: FakeDashboard, events: list[str]) -> None:
        self.git = git
        self.dashboard = dashboard
        self.events = events
        self.clock = FakeClock()
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.events.append("sleep")

    def service(self, **kw: Any) -> FakeService:
        return FakeService(self.events, **kw)

    def board(self, updates: list[dict[str, Any]] | None = None,
              finish_errors: list[Exception] | None = None) -> FakeBoard:
        return FakeBoard(self.events, updates, finish_errors)

    def watcher(self, service: FakeService, board: FakeBoard, result: dict[str, Any]) -> UpdateWatcher:
        async def apply(repo: Any, settings: Any) -> dict[str, Any]:
            return result

        return UpdateWatcher(service, board, repo_slug=REPO, host="pc", service_id="foundry@pc:owner/repo",
                             apply=apply, sleep=self.sleep, clock=self.clock)


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> Env:
    events: list[str] = []
    git = FakeGit()
    dashboard = FakeDashboard(events)
    monkeypatch.setattr(self_update, "_git", git)
    monkeypatch.setattr(update_watcher, "_post_dashboard_restart", dashboard)
    return Env(git, dashboard, events)


def _request(rid: str = "U-001", **kw: Any) -> dict[str, Any]:
    return {"id": rid, "repo": "owner/repo", "now": False, "status": "queued", **kw}


def _done(message: str = "pulled", before: Any = HEAD, after: Any = REMOTE) -> dict[str, Any]:
    return {"status": "done", "commit_before": before, "commit_after": after, "message": message}


def _ended(status: str, message: str = "not a fast-forward") -> dict[str, Any]:
    return {"status": status, "commit_before": HEAD, "commit_after": HEAD, "message": message}


def _poll(w: UpdateWatcher) -> Any:
    return asyncio.run(w.poll_once())


# --------------------------------------------------------------------------- dashboard restart


def test_department_change_restarts_the_dashboard_before_foundry(env: Env) -> None:
    service, board = env.service(), env.board([_request()])
    assert _poll(env.watcher(service, board, _done())) == "U-001"
    assert env.git.calls == [("diff", "--name-only", HEAD, REMOTE, "--", "department")]
    assert env.git.timeouts[0] > 0
    assert env.dashboard.calls == 1
    assert board.finishes()[0]["result"]["message"] == "pulled" + RESTARTED
    assert board.finishes()[0]["status"] == "done"
    assert env.events == ["dashboard", "finish", "foundry"]


def test_no_department_change_leaves_the_dashboard_alone(env: Env) -> None:
    env.git.answers["diff"] = (0, "\n", "")
    service, board = env.service(), env.board([_request()])
    _poll(env.watcher(service, board, _done()))
    assert env.dashboard.calls == 0
    assert board.finishes()[0]["result"]["message"] == "pulled"
    assert service.restarts == 1


@pytest.mark.parametrize("before, after", [(HEAD, HEAD), (None, REMOTE), (HEAD, None)])
def test_no_commit_range_runs_no_diff(env: Env, before: Any, after: Any) -> None:
    board = env.board([_request()])
    _poll(env.watcher(env.service(), board, _done(before=before, after=after)))
    assert env.git.calls == [] and env.dashboard.calls == 0
    assert board.finishes()[0]["result"]["message"] == "pulled"


@pytest.mark.parametrize("status", ["refused", "failed"])
def test_an_unsuccessful_update_does_not_restart_the_dashboard(env: Env, status: str) -> None:
    result = {"status": status, "commit_before": HEAD, "commit_after": REMOTE, "message": "no"}
    _poll(env.watcher(env.service(), env.board([_request()]), result))
    assert env.git.calls == [] and env.dashboard.calls == 0


def test_a_dashboard_that_is_down_is_not_an_error(env: Env, caplog: pytest.LogCaptureFixture) -> None:
    env.dashboard.error = urllib.error.URLError(ConnectionRefusedError("refused"))
    service, board = env.service(), env.board([_request()])
    with caplog.at_level(logging.WARNING, logger="foundry.update_watcher"):
        _poll(env.watcher(service, board, _done()))
    finish = board.finishes()[0]
    assert finish["status"] == "done" and finish["result"]["message"] == "pulled" + NOT_RUNNING
    assert service.restarts == 1 and caplog.records == []
    assert env.sleeps == []


def test_a_dashboard_refusing_the_restart_is_named(env: Env) -> None:
    env.dashboard.error = urllib.error.HTTPError("http://x", 404, "Not Found", None, None)  # type: ignore[arg-type]
    board = env.board([_request()])
    _poll(env.watcher(env.service(), board, _done()))
    assert board.finishes()[0]["result"]["message"] == "pulled; dashboard not restarted: the dashboard answered 404"


@pytest.mark.parametrize("answer, reason", [
    ((128, "", "fatal: bad object\n"), "fatal: bad object"),
    (self_update._GitError("git diff timed out"), "git diff timed out"),
])
def test_a_failing_diff_is_reported(env: Env, answer: Any, reason: str) -> None:
    env.git.answers["diff"] = answer
    service, board = env.service(), env.board([_request()])
    _poll(env.watcher(service, board, _done()))
    expected = f"pulled; dashboard not restarted: could not compare the commits: {reason}"
    assert board.finishes()[0]["result"]["message"] == expected
    assert env.dashboard.calls == 0 and service.restarts == 1


def test_a_long_message_keeps_the_suffix(env: Env) -> None:
    board = env.board([_request()])
    _poll(env.watcher(env.service(), board, _done("x" * 5000)))
    message = board.finishes()[0]["result"]["message"]
    assert len(message) == MAX_MESSAGE and message.endswith(RESTARTED)


class Response:
    def __enter__(self) -> Response:
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def read(self) -> bytes:
        return b"{}"


class FakeOpener:
    def __init__(self) -> None:
        self.seen: list[tuple[urllib.request.Request, float]] = []

    def open(self, req: urllib.request.Request, timeout: float = 0.0) -> Response:
        self.seen.append((req, timeout))
        return Response()


@pytest.mark.parametrize("configured, url", [
    (None, "http://127.0.0.1:4300/api/dashboard/restart"),
    ("http://127.0.0.1:9000/", "http://127.0.0.1:9000/api/dashboard/restart"),
])
def test_the_restart_post(monkeypatch: pytest.MonkeyPatch, configured: str | None, url: str) -> None:
    opener = FakeOpener()
    monkeypatch.setattr(board_dispatch, "_LOCAL_OPENER", opener)
    if configured is None:
        monkeypatch.delenv("FOUNDRY_DASHBOARD_URL", raising=False)
    else:
        monkeypatch.setenv("FOUNDRY_DASHBOARD_URL", configured)
    update_watcher._post_dashboard_restart()
    [(req, timeout)] = opener.seen
    assert req.full_url == url and req.get_method() == "POST" and timeout == 10.0


# --------------------------------------------------------------------------- the report after a restart


def test_the_report_waits_for_the_restarted_dashboard(env: Env) -> None:
    down: list[Exception] = [BoardUnavailable("the dashboard is not reachable"), BoardUnavailable("502")]
    service, board = env.service(), env.board([_request()], finish_errors=down)
    w = env.watcher(service, board, _done())
    assert _poll(w) == "U-001"
    assert len(board.finishes()) == 3
    assert all(f["result"]["message"] == "pulled" + RESTARTED for f in board.finishes())
    assert env.events == ["dashboard", "finish-error", "sleep", "finish-error", "sleep", "finish", "foundry"]
    assert w.last_error is None and service.restarts == 1


def test_the_report_gives_up_on_a_dashboard_that_stays_down(env: Env) -> None:
    attempts = update_watcher._FINISH_ATTEMPTS
    down: list[Exception] = [BoardUnavailable("down") for _ in range(attempts + 5)]
    service, board = env.service(), env.board([_request()], finish_errors=down)
    w = env.watcher(service, board, _done())
    assert _poll(w) == "U-001"
    assert len(board.finishes()) == attempts and len(env.sleeps) == attempts - 1
    assert w.last_error is not None and "down" in w.last_error
    assert service.restarts == 1 and env.events[-1] == "foundry"


def test_a_refused_report_is_not_retried(env: Env) -> None:
    service, board = env.service(), env.board([_request()], finish_errors=[BoardRefused("not yours")])
    _poll(env.watcher(service, board, _done()))
    assert len(board.finishes()) == 1 and env.sleeps == [] and service.restarts == 1


def test_without_a_dashboard_restart_the_report_is_tried_once(env: Env) -> None:
    env.git.answers["diff"] = (0, "", "")
    service, board = env.service(), env.board([_request()], finish_errors=[BoardUnavailable("down")])
    w = env.watcher(service, board, _done())
    _poll(w)
    assert len(board.finishes()) == 1 and env.sleeps == [] and service.restarts == 1
    assert w.last_error is not None


# --------------------------------------------------------------------------- refusal events


@pytest.mark.parametrize("status", ["refused", "failed"])
def test_unsuccessful_updates_leave_an_event(env: Env, status: str) -> None:
    service = env.service()
    _poll(env.watcher(service, env.board([_request()]), _ended(status, "y" * 900)))
    assert service.queue.events == [(f"update_{status}", None, {"request_id": "U-001", "message": "y" * 500})]


def test_a_done_update_leaves_no_event(env: Env) -> None:
    service = env.service()
    _poll(env.watcher(service, env.board([_request()]), _done()))
    assert service.queue.events == []


def test_an_apply_that_raises_leaves_a_failed_event(env: Env) -> None:
    async def apply(repo: Any, settings: Any) -> dict[str, Any]:
        raise RuntimeError("boom")

    service, board = env.service(), env.board([_request()])
    w = UpdateWatcher(service, board, repo_slug=REPO, host="pc", service_id="s", apply=apply)
    _poll(w)
    assert [e[0] for e in service.queue.events] == ["update_failed"]
    assert "boom" in service.queue.events[0][2]["message"]


def test_an_event_error_does_not_stop_the_report(env: Env) -> None:
    service = env.service(queue=FakeQueue(RuntimeError("db locked")))
    board = env.board([_request()])
    assert _poll(env.watcher(service, board, _ended("refused"))) == "U-001"
    assert board.finishes()[0]["status"] == "refused"


def test_a_service_without_a_factory_still_reports(env: Env) -> None:
    service = env.service()
    service.factory = None  # type: ignore[assignment]
    board = env.board([_request()])
    assert _poll(env.watcher(service, board, _ended("failed"))) == "U-001"
    assert board.finishes()[0]["status"] == "failed"


def test_warns_once_per_distinct_message(env: Env, caplog: pytest.LogCaptureFixture) -> None:
    service = env.service()
    board = env.board()
    result = _ended("refused", "folder has uncommitted changes")
    w = env.watcher(service, board, result)
    with caplog.at_level(logging.WARNING, logger="foundry.update_watcher"):
        for rid in ("U-001", "U-002"):
            board.updates = [_request(rid)]
            _poll(w)
        result["message"] = "detached HEAD"
        board.updates = [_request("U-003")]
        _poll(w)
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2
    assert "uncommitted changes" in warnings[0] and "detached HEAD" in warnings[1]
    assert len(service.queue.events) == 3  # every update leaves an event


# --------------------------------------------------------------------------- auto-update back-off


def _refuse_one_auto_request(env: Env, w: UpdateWatcher, board: FakeBoard) -> None:
    """The auto check sends a request for REMOTE, and the next poll has it refused."""
    _poll(w)
    assert len(board.requests()) == 1
    board.updates = [_request()]
    assert _poll(w) == "U-001"
    board.updates = []


def test_a_refused_pull_is_not_requested_again_right_away(env: Env, caplog: pytest.LogCaptureFixture) -> None:
    board = env.board()
    w = env.watcher(env.service(auto_update=True), board, _ended("refused"))
    with caplog.at_level(logging.INFO, logger="foundry.update_watcher"):
        _refuse_one_auto_request(env, w, board)
    assert any("auto-update waits" in r.getMessage() for r in caplog.records)
    for _ in range(5):
        env.clock.now += LATER
        _poll(w)
    assert len(board.requests()) == 1


def test_a_moved_remote_ends_the_back_off(env: Env) -> None:
    board = env.board()
    w = env.watcher(env.service(auto_update=True), board, _ended("refused"))
    _refuse_one_auto_request(env, w, board)
    env.git.answers["ls-remote"] = _listing(OTHER_REMOTE)
    env.clock.now += LATER
    _poll(w)
    assert len(board.requests()) == 2


def test_thirty_minutes_end_the_back_off(env: Env) -> None:
    board = env.board()
    w = env.watcher(env.service(auto_update=True), board, _ended("refused"))
    _refuse_one_auto_request(env, w, board)
    env.clock.now += AUTO_UPDATE_REFUSED_BACKOFF_S - LATER
    _poll(w)
    assert len(board.requests()) == 1
    env.clock.now += LATER
    _poll(w)
    assert len(board.requests()) == 2
    # the new request starts over once it is refused too
    board.updates = [_request("U-002")]
    assert _poll(w) == "U-002"
    board.updates = []
    env.clock.now += LATER
    _poll(w)
    assert len(board.requests()) == 2


def test_a_requested_pull_refused_before_any_check_also_backs_off(env: Env) -> None:
    board = env.board([_request()])
    w = env.watcher(env.service(auto_update=True), board, _ended("refused"))
    assert _poll(w) == "U-001"
    board.updates = []
    for _ in range(3):
        env.clock.now += LATER
        _poll(w)
    assert board.requests() == []


def test_bad_settings_do_not_back_off(env: Env) -> None:
    board = env.board([_request(settings={"BAD": "1"})])
    w = env.watcher(env.service(auto_update=True), board, _ended("refused", "settings keys must start with FOUNDRY_"))
    assert _poll(w) == "U-001"
    board.updates = []
    env.clock.now += LATER
    _poll(w)
    assert len(board.requests()) == 1


def test_a_failed_pull_does_not_back_off(env: Env) -> None:
    board = env.board([_request()])
    w = env.watcher(env.service(auto_update=True), board, _ended("failed", "git fetch failed"))
    assert _poll(w) == "U-001"
    board.updates = []
    env.clock.now += LATER
    _poll(w)
    assert len(board.requests()) == 1
