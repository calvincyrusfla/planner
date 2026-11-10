"""Tests for foundry/update_watcher.py and its wiring in foundry/serve.py."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from foundry import serve
from foundry.board_bridge import BoardBridge
from foundry.board_dispatch import BoardRefused
from foundry.config import FoundryConfig
from foundry.main import builder_claude_args, demo_claude, demo_gemma, write_demo_repo
from foundry.schemas import TaskState
from foundry.serve import Service, run_service
from foundry.update_watcher import MAX_MESSAGE, UpdateWatcher

REPO = "Owner/Repo"


@dataclass
class FakeTicket:
    status: str = "open"
    in_review: bool = False
    task_id: str = "F-1"


class FakeQueue:
    def __init__(self, tickets: list[FakeTicket] | None = None) -> None:
        self.items: list[FakeTicket] = list(tickets or [])
        self.tasks: dict[str, dict[str, Any]] = {"F-1": {"state": TaskState.BUILDING}}
        self.get_task_error: Exception | None = None

    def tickets(self) -> list[FakeTicket]:
        return list(self.items)

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        if self.get_task_error is not None:
            raise self.get_task_error
        return self.tasks.get(task_id)


class FakeFactory:
    def __init__(self, queue: FakeQueue) -> None:
        self.queue = queue


class FakeService:
    def __init__(self, tickets: list[FakeTicket] | None = None) -> None:
        self.repo = Path("/repo")
        self.queue = FakeQueue(tickets)
        self.factory = FakeFactory(self.queue)
        self.restarts = 0

    def request_restart(self) -> None:
        self.restarts += 1


class FakeBoard:
    def __init__(self, updates: list[dict[str, Any]] | None = None, refuse_claim: bool = False) -> None:
        self.updates = list(updates or [])
        self.refuse_claim = refuse_claim
        self.ops: list[tuple[str, dict[str, Any]]] = []

    def state(self) -> dict[str, Any]:
        return {"foundryUpdates": [dict(u) for u in self.updates]}

    def op(self, type_: str, args: dict[str, Any]) -> dict[str, Any]:
        self.ops.append((type_, dict(args)))
        if type_ == "claimFoundryUpdate" and self.refuse_claim:
            raise BoardRefused("already claimed")
        return {"ok": True}


@dataclass
class FakeResult:
    status: str
    commit_before: str | None = "aaa"
    commit_after: str | None = "bbb"
    message: str = "updated"


@dataclass
class FakeApply:
    result: FakeResult
    calls: list[tuple[Any, Any]] = field(default_factory=list)

    async def __call__(self, repo: Any, settings: Any) -> FakeResult:
        self.calls.append((repo, settings))
        return self.result


def _request(rid: str = "U-001", repo: str = "owner/repo", **kw: Any) -> dict[str, Any]:
    return {"id": rid, "repo": repo, "settings": {"FOUNDRY_X": "1"}, "now": False, "status": "queued", **kw}


def _watcher(service: FakeService, board: FakeBoard, apply: FakeApply) -> UpdateWatcher:
    return UpdateWatcher(service, board, repo_slug=REPO, host="pc", service_id="foundry@pc:owner/repo",
                         apply=apply, interval=15.0)


def _types(board: FakeBoard) -> list[str]:
    return [t for t, _ in board.ops]


def test_claims_applies_reports_and_restarts() -> None:
    svc = FakeService()
    board = FakeBoard([_request()])
    apply = FakeApply(FakeResult("done", message="x" * 5000))
    w = _watcher(svc, board, apply)
    assert asyncio.run(w.poll_once()) == "U-001"
    assert apply.calls == [(svc.repo, {"FOUNDRY_X": "1"})]
    assert _types(board) == ["claimFoundryUpdate", "finishFoundryUpdate"]
    assert board.ops[0][1] == {"id": "U-001", "host": "pc", "service": "foundry@pc:owner/repo"}
    finish = board.ops[1][1]
    assert finish["id"] == "U-001" and finish["service"] == "foundry@pc:owner/repo" and finish["status"] == "done"
    assert finish["result"]["commitBefore"] == "aaa" and finish["result"]["commitAfter"] == "bbb"
    assert len(finish["result"]["message"]) == MAX_MESSAGE
    assert svc.restarts == 1
    # nothing more is claimed once a restart was requested
    assert asyncio.run(w.poll_once()) is None
    assert len(board.ops) == 2


def test_takes_the_oldest_request_of_this_repo() -> None:
    svc = FakeService()
    board = FakeBoard([
        _request("U-003", createdAt="2026-11-10T03:00:00Z"),
        _request("U-002", createdAt="2026-11-10T02:00:00Z"),
        _request("U-001", status="done", createdAt="2026-11-10T01:00:00Z"),
    ])
    w = _watcher(svc, board, FakeApply(FakeResult("done")))
    assert asyncio.run(w.poll_once()) == "U-002"


def test_ignores_requests_of_other_repos() -> None:
    svc = FakeService()
    board = FakeBoard([_request(repo="someone/else")])
    apply = FakeApply(FakeResult("done"))
    assert asyncio.run(_watcher(svc, board, apply).poll_once()) is None
    assert board.ops == [] and apply.calls == [] and svc.restarts == 0


@pytest.mark.parametrize("ticket", [FakeTicket(status="claimed"), FakeTicket(status="open", in_review=True)])
def test_waits_while_busy_unless_now(ticket: FakeTicket) -> None:
    svc = FakeService([ticket])
    board = FakeBoard([_request()])
    apply = FakeApply(FakeResult("done"))
    w = _watcher(svc, board, apply)
    assert asyncio.run(w.poll_once()) is None
    assert board.ops == [] and apply.calls == []
    # idle again: the next poll carries it out
    svc.queue.items.clear()
    assert asyncio.run(w.poll_once()) == "U-001"
    assert svc.restarts == 1


def test_now_does_not_wait_for_busy() -> None:
    svc = FakeService([FakeTicket(status="claimed")])
    board = FakeBoard([_request(now=True)])
    apply = FakeApply(FakeResult("done"))
    assert asyncio.run(_watcher(svc, board, apply).poll_once()) == "U-001"
    assert len(apply.calls) == 1 and svc.restarts == 1


@pytest.mark.parametrize("state", [TaskState.QUARANTINED, TaskState.COMPLETE, "quarantined"])
def test_claimed_ticket_of_a_finished_task_is_not_busy(state: Any) -> None:
    svc = FakeService([FakeTicket(status="claimed")])
    svc.queue.tasks["F-1"] = {"state": state}
    board = FakeBoard([_request()])
    apply = FakeApply(FakeResult("done"))
    w = _watcher(svc, board, apply)
    assert w.busy() is False
    assert asyncio.run(w.poll_once()) == "U-001"
    assert len(apply.calls) == 1 and svc.restarts == 1


@pytest.mark.parametrize("state", [TaskState.BUILDING, "queued"])
def test_claimed_ticket_of_a_live_task_is_busy(state: Any) -> None:
    svc = FakeService([FakeTicket(status="claimed")])
    svc.queue.tasks["F-1"] = {"state": state}
    board = FakeBoard([_request()])
    apply = FakeApply(FakeResult("done"))
    w = _watcher(svc, board, apply)
    assert w.busy() is True
    assert asyncio.run(w.poll_once()) is None
    assert board.ops == [] and apply.calls == []


def test_ticket_in_review_of_a_failed_task_is_ignored() -> None:
    svc = FakeService([FakeTicket(status="open", in_review=True)])
    svc.queue.tasks["F-1"] = {"state": TaskState.FAILED}
    w = _watcher(svc, FakeBoard([_request()]), FakeApply(FakeResult("done")))
    assert w.busy() is False
    assert asyncio.run(w.poll_once()) == "U-001"


def test_ticket_of_a_missing_task_is_not_busy() -> None:
    svc = FakeService([FakeTicket(status="claimed", task_id="F-404")])
    w = _watcher(svc, FakeBoard([_request()]), FakeApply(FakeResult("done")))
    assert w.busy() is False


def test_get_task_raising_counts_as_not_busy() -> None:
    svc = FakeService([FakeTicket(status="claimed")])
    svc.queue.get_task_error = RuntimeError("db locked")
    board = FakeBoard([_request()])
    apply = FakeApply(FakeResult("done"))
    w = _watcher(svc, board, apply)
    assert w.busy() is False
    assert asyncio.run(w.poll_once()) == "U-001"
    assert len(apply.calls) == 1


def test_awaiting_help_ticket_is_not_busy() -> None:
    svc = FakeService([FakeTicket(status="awaiting_help")])
    w = _watcher(svc, FakeBoard([_request()]), FakeApply(FakeResult("done")))
    assert w.busy() is False


def test_refused_claim_is_skipped() -> None:
    svc = FakeService()
    board = FakeBoard([_request()], refuse_claim=True)
    apply = FakeApply(FakeResult("done"))
    assert asyncio.run(_watcher(svc, board, apply).poll_once()) is None
    assert _types(board) == ["claimFoundryUpdate"]
    assert apply.calls == [] and svc.restarts == 0


@pytest.mark.parametrize("status", ["refused", "failed"])
def test_refused_or_failed_update_is_reported_without_restart(status: str) -> None:
    svc = FakeService()
    board = FakeBoard([_request()])
    apply = FakeApply(FakeResult(status, commit_after="aaa", message="not a fast-forward"))
    assert asyncio.run(_watcher(svc, board, apply).poll_once()) == "U-001"
    finish = board.ops[-1]
    assert finish[0] == "finishFoundryUpdate" and finish[1]["status"] == status
    assert finish[1]["result"]["message"] == "not a fast-forward"
    assert svc.restarts == 0


def test_board_errors_are_retried_next_poll() -> None:
    class DownBoard(FakeBoard):
        def state(self) -> dict[str, Any]:
            raise OSError("board down")

    svc = FakeService()
    w = _watcher(svc, DownBoard([_request()]), FakeApply(FakeResult("done")))
    assert asyncio.run(w.poll_once()) is None
    assert w.last_error is not None and "board down" in w.last_error
    assert svc.restarts == 0


# --------------------------------------------------------------------------- serve wiring


def _config(tmp_path: Path) -> FoundryConfig:
    return FoundryConfig(
        db_path=tmp_path / "foundry.db", rpm=1000, tpm=10_000_000, max_concurrent=5,
        teams=["alpha", "bravo"], board_bridge_mode="dry",
    )


def _service(tmp_path: Path, **kw: Any) -> Service:
    repo = write_demo_repo(tmp_path / "repo")
    return Service(
        _config(tmp_path), repo=repo, claude=demo_claude(repo, fail_first=None), gemma=demo_gemma(),
        factory_kwargs={"bridge": BoardBridge("dry"), "patches_dir": tmp_path / "patches"}, monitor_s=3600,
        **kw,
    )


async def _until(pred: Any, timeout: float = 15.0) -> None:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not pred():
        assert loop.time() < end, "condition not reached in time"
        await asyncio.sleep(0.01)


def test_run_service_returns_75_after_request_restart(tmp_path: Path) -> None:
    async def go() -> None:
        svc = _service(tmp_path)
        stop = asyncio.Event()
        runner = asyncio.create_task(run_service(svc, port=0, stop_event=stop, out=lambda _l: None))
        await _until(lambda: svc.api is not None and svc.running)
        svc.request_restart()
        assert svc.restart_requested is True and stop.is_set()
        assert await asyncio.wait_for(runner, 20) == 75
        assert svc.factory is None

    asyncio.run(go())


class FakeBatchWatcher:
    """Stands in for BatchWatcher: a real background task, a fake board."""

    def __init__(self, board: FakeBoard, started: bool = True) -> None:
        self.board = board
        self.repo_slug = REPO
        self.host = "pc"
        self.service_id = "foundry@pc:owner/repo"
        self.started = started
        self._task: asyncio.Task[Any] | None = None
        self.stopped = False

    def start(self) -> asyncio.Task[Any] | None:
        if not self.started:
            return None
        self._task = asyncio.get_running_loop().create_task(asyncio.sleep(3600))
        return self._task

    async def stop(self) -> None:
        self.stopped = True
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    def summary(self) -> dict[str, Any]:
        return {"repo": self.repo_slug}


def test_update_watcher_starts_and_stops_with_the_batch_watcher(tmp_path: Path) -> None:
    async def go() -> None:
        board = FakeBoard()
        batches = FakeBatchWatcher(board)
        svc = _service(tmp_path, batch_watcher=batches)
        await svc.start()
        w = svc.update_watcher
        task = w._task if isinstance(w, UpdateWatcher) else None
        try:
            assert isinstance(w, UpdateWatcher)
            assert w.board is board and w.repo_slug == REPO and w.host == "pc"
            assert w.service_id == "foundry@pc:owner/repo"
            assert task is not None and not task.done()
        finally:
            await svc.stop()
        assert isinstance(w, UpdateWatcher) and task is not None
        assert batches.stopped and w._task is None and task.done()

    asyncio.run(go())


def test_no_update_watcher_when_batches_did_not_start(tmp_path: Path) -> None:
    async def go() -> None:
        svc = _service(tmp_path, batch_watcher=FakeBatchWatcher(FakeBoard(), started=False))
        await svc.start()
        try:
            assert svc.update_watcher is None
        finally:
            await svc.stop()

    asyncio.run(go())


class FakeCodeClient:
    def __init__(self, path: str, *, extra_args: list[str] | None = None) -> None:
        self.path = path
        self.extra_args = extra_args


def test_serve_main_creates_builder_client_in_edit_mode(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("FOUNDRY_BUILDER_TOOLS", raising=False)
    monkeypatch.setenv("FOUNDRY_BUILDER_MODE", "edit")
    config = FoundryConfig.from_env()
    assert config.builder_mode == "edit" and config.builder_tools != "read"
    services: list[Any] = []

    async def fake_run_service(service: Any, **_kwargs: Any) -> int:
        services.append(service)
        return 0

    def fake_resolve(claude_bin: str) -> str:
        return claude_bin

    def fake_gemma(*_args: Any, **_kwargs: Any) -> object:
        return object()

    def no_ctrl_c() -> bool:
        return False

    monkeypatch.setattr("foundry.claude_client.ClaudeCodeClient", FakeCodeClient)
    monkeypatch.setattr("foundry.claude_client.resolve_claude_bin", fake_resolve)
    monkeypatch.setattr("foundry.gemma_client.OllamaGemmaClient", fake_gemma)
    monkeypatch.setattr(serve, "run_service", fake_run_service)
    monkeypatch.setattr(serve, "enable_ctrl_c", no_ctrl_c)
    assert serve.main(["--repo", str(tmp_path)]) == 0
    service = services[0]
    assert isinstance(service.builder_claude, FakeCodeClient)
    assert service.builder_claude is not service.claude
    assert service.builder_claude.extra_args == builder_claude_args(config)
