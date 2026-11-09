"""BatchWatcher: paused subtasks reach the board, answers reopen them, unanswered ones time out."""

from __future__ import annotations

import asyncio
import copy
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

import pytest

from foundry.board_dispatch import KV_PREFIX, BatchWatcher, BoardRefused

BATCH = "S-1"
CREATED = "2026-11-08T00:00:00Z"
TASK = "F-1"
TICKET = "T-1"
SERVICE = "foundry@pc:o/r"
REQUEST = {"question": "Need `foo`", "context": "", "options": [], "attempts_used": 2, "attempts_left": 2}
WORKER_STATES = {"queued", "sizing", "building", "reviewing", "complete", "quarantined", "failed", "needs_help"}


class FakeBoard:
    """One running batch claimed by the watcher."""

    def __init__(self) -> None:
        self.batch: dict[str, Any] = {
            "id": BATCH, "createdAt": CREATED, "title": "t", "repo": "o/r", "status": "running",
            "claimedBy": {"host": "pc", "service": SERVICE},
            "subtasks": [{"key": "1", "title": "t", "files": ["a.py"]}], "results": {},
        }

    def state(self) -> dict[str, Any]:
        return {"foundryBatches": [copy.deepcopy(self.batch)]}

    def op(self, type_: str, args: Mapping[str, Any]) -> Any:
        assert type_ == "updateBatch"
        for key, result in (args.get("results") or {}).items():
            if result.get("state") not in WORKER_STATES:
                raise BoardRefused(f"bad state {result.get('state')}", 400)
            merged = {**self.batch["results"].get(key, {}), **copy.deepcopy(result)}
            if merged.get("help") is None:
                merged.pop("help", None)
            self.batch["results"][key] = merged
        return {"ok": True}

    def answer(self, text: str, at: str) -> None:
        self.batch["results"]["1"] = {
            "state": "answered", "foundryTask": TASK,
            "help": {"question": "Need `foo`", "answer": text, "answeredBy": "fm", "answeredAt": at},
        }

    @property
    def result(self) -> dict[str, Any]:
        out: dict[str, Any] = self.batch["results"]["1"]
        return out


class LegacyQueue:
    """A queue from before help requests."""

    def __init__(self) -> None:
        self.kv: dict[str, Any] = {}
        self.tasks: dict[str, dict[str, Any]] = {TASK: {"state": "building"}}

    def kv_get(self, key: str) -> Any:
        return copy.deepcopy(self.kv.get(key))

    def kv_set(self, key: str, value: Any) -> None:
        self.kv[key] = copy.deepcopy(value)

    def get_task(self, tid: str) -> dict[str, Any] | None:
        return self.tasks.get(tid)


class HelpQueue(LegacyQueue):
    """The help-request contract of ``FoundryQueue``."""

    def __init__(self) -> None:
        super().__init__()
        self.tickets: dict[str, dict[str, Any]] = {TICKET: {"task_id": TASK, "status": "open", "attempt": 3}}
        self.requests: dict[str, dict[str, Any]] = {}
        self.answer_calls: list[tuple[str, str, list[str] | None]] = []
        self.expire_calls: list[float] = []
        self.asked_age = 0.0
        self.expire_error = False

    def pause_for_help(self, ticket_id: str, request: dict[str, Any]) -> bool:
        self.tickets[ticket_id]["status"] = "awaiting_help"
        self.requests[ticket_id] = dict(request)
        return True

    def help_requests(self) -> list[dict[str, Any]]:
        return [{"ticket_id": tid, "task_id": self.tickets[tid]["task_id"], **req}
                for tid, req in self.requests.items()]

    def answer_help(self, ticket_id: str, note: str, read_files: list[str] | None = None) -> bool:
        self.answer_calls.append((ticket_id, note, read_files))
        self.tickets[ticket_id]["status"] = "open"
        self.kv[f"help_answer:{ticket_id}"] = {"note": note, "read_files": list(read_files or [])}
        del self.requests[ticket_id]
        return True

    def expire_help(self, timeout_s: float) -> list[str]:
        self.expire_calls.append(timeout_s)
        if self.expire_error:
            raise RuntimeError("database is locked")
        expired = list(self.requests) if self.asked_age > timeout_s else []
        for ticket_id in expired:
            task = self.tasks[self.tickets[ticket_id]["task_id"]]
            task["state"] = "quarantined"
            task["quarantine_reason"] = f"no Floor Manager answer within 1 h: {self.requests[ticket_id]['question']}"
        return expired


def _service(queue: Any, repo: Path, config: Any = None) -> Any:
    queue.kv_set(KV_PREFIX + BATCH, {"createdAt": CREATED, "tasks": {"1": TASK}, "failed": {}, "pushed": {}, "usage": {}})
    manager = SimpleNamespace(orders={}, auto_pr=False, publisher=None)
    factory = SimpleNamespace(manager=manager, queue=queue, telemetry=None)
    if config is None:
        config = SimpleNamespace(help_timeout_s=123.0, allowed_repos=[])
    return SimpleNamespace(running=True, factory=factory, repo=repo, config=config)


def _watcher(service: Any, board: FakeBoard) -> BatchWatcher:
    return BatchWatcher(service, board, repo_slug="o/r", host="pc", interval=1, enabled=True, now=lambda: 1000.0)


def _pump(watcher: BatchWatcher) -> list[str]:
    return asyncio.run(watcher.poll_once())


def test_paused_subtask_is_reported_as_needs_help(tmp_path: Path) -> None:
    queue, board = HelpQueue(), FakeBoard()
    watcher = _watcher(_service(queue, tmp_path), board)
    _pump(watcher)
    assert board.result["state"] == "building"
    queue.pause_for_help(TICKET, REQUEST)
    _pump(watcher)
    assert board.result["state"] == "needs_help"
    assert board.result["help"] == {"question": "Need `foo`", "context": "", "options": []}


def test_help_texts_are_cut_for_the_board(tmp_path: Path) -> None:
    queue, board = HelpQueue(), FakeBoard()
    queue.pause_for_help(TICKET, {"question": "q" * 3000, "context": "c" * 3000, "options": ["o" * 3000]})
    _pump(_watcher(_service(queue, tmp_path), board))
    assert len(board.result["help"]["question"]) == 2000
    assert len(board.result["help"]["context"]) == 2000
    assert len(board.result["help"]["options"][0]) == 2000


def test_answer_reopens_the_ticket_and_clears_the_question(tmp_path: Path) -> None:
    (tmp_path / "shop").mkdir()
    (tmp_path / "shop" / "strings.py").write_text("X = 1\n", encoding="utf-8")
    queue, board = HelpQueue(), FakeBoard()
    watcher = _watcher(_service(queue, tmp_path), board)
    queue.pause_for_help(TICKET, REQUEST)
    _pump(watcher)
    board.answer("Use shop/strings.py", "2026-11-08T10:00:00Z")
    _pump(watcher)
    assert queue.tickets[TICKET] == {"task_id": TASK, "status": "open", "attempt": 3}
    assert queue.kv[f"help_answer:{TICKET}"] == {"note": "Use shop/strings.py", "read_files": ["shop/strings.py"]}
    assert board.result["state"] == "building"
    assert "help" not in board.result


def test_same_answer_is_applied_once(tmp_path: Path) -> None:
    queue, board = HelpQueue(), FakeBoard()
    service = _service(queue, tmp_path)
    queue.pause_for_help(TICKET, REQUEST)
    _pump(_watcher(service, board))
    board.answer("Try again", "2026-11-08T10:00:00Z")
    _pump(_watcher(service, board))
    assert len(queue.answer_calls) == 1

    # the board still shows the same answer: a new watcher (a restart) must not apply it again
    queue.pause_for_help(TICKET, REQUEST)
    board.answer("Try again", "2026-11-08T10:00:00Z")
    _pump(_watcher(service, board))
    assert len(queue.answer_calls) == 1
    assert queue.tickets[TICKET]["status"] == "awaiting_help"

    board.answer("Another answer", "2026-11-08T11:00:00Z")
    _pump(_watcher(service, board))
    assert [call[1] for call in queue.answer_calls] == ["Try again", "Another answer"]


def test_answer_files_are_repo_relative_existing_and_capped(tmp_path: Path) -> None:
    (tmp_path / "shop").mkdir()
    (tmp_path / "shop" / "strings.py").write_text("", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("", encoding="utf-8")
    for i in range(12):
        (tmp_path / f"f{i}.py").write_text("", encoding="utf-8")
    watcher = _watcher(_service(HelpQueue(), tmp_path), FakeBoard())
    text = "See `shop/strings.py`, ./shop/strings.py, ../secret.txt, /secret.txt and missing.py."
    assert watcher._help_files(text) == ["shop/strings.py"]
    assert len(watcher._help_files(" ".join(f"f{i}.py" for i in range(12)))) == 10


def test_expire_help_runs_every_round_with_the_configured_timeout(tmp_path: Path) -> None:
    queue, board = HelpQueue(), FakeBoard()
    watcher = _watcher(_service(queue, tmp_path), board)
    _pump(watcher)
    _pump(watcher)
    assert queue.expire_calls == [123.0, 123.0]


def test_expire_help_falls_back_to_the_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOUNDRY_HELP_TIMEOUT_S", "7")
    queue, board = HelpQueue(), FakeBoard()
    _pump(_watcher(_service(queue, tmp_path, SimpleNamespace(allowed_repos=[])), board))
    assert queue.expire_calls == [7.0]


def test_unanswered_request_ends_quarantined(tmp_path: Path) -> None:
    queue, board = HelpQueue(), FakeBoard()
    watcher = _watcher(_service(queue, tmp_path), board)
    queue.pause_for_help(TICKET, REQUEST)
    _pump(watcher)
    assert board.result["state"] == "needs_help"
    queue.asked_age = 1000.0
    _pump(watcher)  # expires at the end of the round
    _pump(watcher)
    assert board.result["state"] == "quarantined"
    assert "no Floor Manager answer" in board.result["error"]
    assert "help" not in board.result


def test_failing_expire_help_does_not_break_the_round(tmp_path: Path) -> None:
    queue, board = HelpQueue(), FakeBoard()
    queue.expire_error = True
    watcher = _watcher(_service(queue, tmp_path), board)
    assert _pump(watcher) == [BATCH]
    assert watcher.last_error is None


def test_queue_without_help_methods_keeps_working(tmp_path: Path) -> None:
    queue, board = LegacyQueue(), FakeBoard()
    watcher = _watcher(_service(queue, tmp_path), board)
    assert _pump(watcher) == [BATCH]
    assert board.result["state"] == "building"
    assert "help" not in board.result


class RecordingBoard(FakeBoard):
    """Keeps the results of every updateBatch."""

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[dict[str, Any]] = []

    def op(self, type_: str, args: Mapping[str, Any]) -> Any:
        if type_ == "updateBatch":
            self.sent.append(copy.deepcopy(dict(args.get("results") or {})))
        return super().op(type_, args)


def test_finished_task_is_reported_as_finished_and_cleared(tmp_path: Path) -> None:
    queue, board = HelpQueue(), FakeBoard()
    watcher = _watcher(_service(queue, tmp_path), board)
    queue.pause_for_help(TICKET, REQUEST)
    _pump(watcher)
    assert board.result["state"] == "needs_help"
    queue.tasks[TASK]["state"] = "complete"  # the request is still listed: a finished task stays finished
    _pump(watcher)
    assert board.result["state"] == "complete"
    assert "help" not in board.result


def test_clearing_the_question_is_written_once(tmp_path: Path) -> None:
    queue, board = HelpQueue(), RecordingBoard()
    watcher = _watcher(_service(queue, tmp_path), board)
    queue.pause_for_help(TICKET, REQUEST)
    _pump(watcher)
    assert board.sent[0]["1"]["state"] == "needs_help"
    board.answer("Try again", "2026-11-08T10:00:00Z")
    _pump(watcher)
    assert board.sent[1]["1"]["state"] == "building"
    assert board.sent[1]["1"]["help"] is None
    _pump(watcher)
    assert len(board.sent) == 2


def test_answer_is_cleared_when_the_record_missed_the_question(tmp_path: Path) -> None:
    queue, board = HelpQueue(), FakeBoard()
    watcher = _watcher(_service(queue, tmp_path), board)
    _pump(watcher)
    assert board.result["state"] == "building"
    # needs_help reached the board, but the reply was lost: the record still says building
    queue.pause_for_help(TICKET, REQUEST)
    board.answer("Try again", "2026-11-08T10:00:00Z")
    _pump(watcher)
    assert len(queue.answer_calls) == 1
    assert board.result["state"] == "building"
    assert "help" not in board.result
