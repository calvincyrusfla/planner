"""The Ledger owns the batch watcher's submitted pieces and submit failures; kv is only the fallback."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from foundry.board_dispatch import KV_PREFIX, BatchWatcher
from foundry.config import FoundryConfig
from foundry.ledger import Ledger
from foundry.main import write_demo_repo
from foundry.orchestrator_queue import FoundryQueue
from foundry.tests.test_board_dispatch import FakeBoard, _pump, _service, _watcher
from foundry.tests.test_board_dispatch_ledger import ArgsBoard
from foundry.tests.test_help_watcher import BATCH, CREATED, LegacyQueue
from foundry.tests.test_help_watcher import _pump as help_pump
from foundry.tests.test_help_watcher import _watcher as help_watcher


class ScriptedQueue(FoundryQueue):
    """A real queue (kv and Ledger) whose tasks report a scripted state, with no ticket machinery."""

    task_state: str = "building"

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        return {"state": self.task_state}


@pytest.fixture()
def db(tmp_path: Path) -> Iterator[ScriptedQueue]:
    queue = ScriptedQueue(tmp_path / "ledger.db")
    yield queue
    queue.close()


def _scripted(queue: Any, repo: Path, *, config: FoundryConfig | None = None, fail: str | None = None) -> Any:
    """A service on ``queue`` whose manager hands out F-1, F-2, ..."""
    accepted: list[Any] = []

    def accept(order: Any) -> str:
        if fail is not None:
            raise RuntimeError(fail)
        accepted.append(order)
        return f"F-{len(accepted)}"

    manager = SimpleNamespace(orders={}, auto_pr=False, publisher=None, accept=accept, accepted=accepted)
    factory = SimpleNamespace(manager=manager, queue=queue, telemetry=None)
    return SimpleNamespace(running=True, factory=factory, repo=repo,
                           config=config if config is not None else FoundryConfig())


def _save_kv(queue: Any, **fields: Any) -> dict[str, Any]:
    rec = {"createdAt": CREATED, "tasks": {}, "failed": {}, "pushed": {}, "usage": {}, **fields}
    queue.kv_set(KV_PREFIX + BATCH, rec)
    return rec


def _boom(*args: Any, **kwargs: Any) -> Any:
    raise RuntimeError("ledger down")


# --------------------------------------------------------------------------- the setting


def test_ledger_read_setting() -> None:
    assert FoundryConfig().ledger_read is False
    assert FoundryConfig.from_env({}).ledger_read is False
    assert FoundryConfig.from_env({"FOUNDRY_LEDGER_READ": "1"}).ledger_read is True
    assert FoundryConfig.from_env({"FOUNDRY_LEDGER_READ": "0"}).ledger_read is False
    with pytest.raises(ValueError):
        FoundryConfig.from_env({"FOUNDRY_LEDGER_READ": "maybe"})


# --------------------------------------------------------------------------- what _record returns


@pytest.mark.parametrize("ledger_read", [False, True])
def test_tasks_and_failures_come_from_the_ledger_whatever_the_setting(
    tmp_path: Path, db: ScriptedQueue, ledger_read: bool
) -> None:
    pushed = {"1": {"state": "building"}}
    _save_kv(db, pushed=pushed, answered={"1": "then"})
    Ledger(db).upsert_piece(BATCH, CREATED, "1", foundry_task="F-ledger", state="building")
    Ledger(db).upsert_piece(BATCH, CREATED, "2", state="failed", error="nope")
    config = FoundryConfig(ledger_read=ledger_read)
    rec = help_watcher(_scripted(db, tmp_path, config=config), ArgsBoard())._record(BATCH, CREATED)
    assert rec["tasks"] == {"1": "F-ledger"}
    assert rec["failed"] == {"2": "nope"}
    assert rec["pushed"] == pushed
    assert rec["answered"] == {"1": "then"}


def test_the_ledger_wins_and_kv_only_keys_are_backfilled(tmp_path: Path, db: ScriptedQueue) -> None:
    pushed = {"1": {"state": "building"}}
    _save_kv(db, tasks={"1": "F-kv", "2": "F-only-kv"}, failed={"3": "kv reason"}, pushed=pushed, closed=True)
    Ledger(db).upsert_piece(BATCH, CREATED, "1", foundry_task="F-ledger", state="building")
    rec = help_watcher(_scripted(db, tmp_path), ArgsBoard())._record(BATCH, CREATED)
    assert rec["tasks"] == {"1": "F-ledger", "2": "F-only-kv"}
    assert rec["failed"] == {"3": "kv reason"}
    assert rec["pushed"] == pushed
    assert rec["closed"] is True
    pieces = {piece["key"]: piece for piece in Ledger(db).pieces(BATCH, CREATED)}
    assert pieces["1"]["foundry_task"] == "F-ledger"
    assert pieces["2"]["foundry_task"] == "F-only-kv"
    assert (pieces["3"]["state"], pieces["3"]["error"]) == ("failed", "kv reason")


def test_a_kv_only_batch_is_backfilled_once(
    tmp_path: Path, db: ScriptedQueue, monkeypatch: pytest.MonkeyPatch
) -> None:
    _save_kv(db, tasks={"1": "F-kv"}, failed={"2": "kv reason"})
    writes: list[str] = []
    real = Ledger.upsert_piece

    def counted(self: Ledger, batch_id: str, created_at: str | None, key: str, **fields: Any) -> bool:
        writes.append(key)
        return real(self, batch_id, created_at, key, **fields)

    monkeypatch.setattr(Ledger, "upsert_piece", counted)
    watcher = help_watcher(_scripted(db, tmp_path), ArgsBoard())
    first = watcher._record(BATCH, CREATED)
    assert sorted(writes) == ["1", "2"]
    assert watcher._record(BATCH, CREATED) == first
    assert sorted(writes) == ["1", "2"]


def test_a_save_takes_tasks_and_failures_out_of_the_kv_record(tmp_path: Path, db: ScriptedQueue) -> None:
    kept = {"pushed": {"1": {"state": "building"}}, "usage": {"1": {"tokens": 5}}, "answered": {"1": "then"},
            "closed": True}
    _save_kv(db, tasks={"1": "F-kv"}, failed={"2": "kv reason"}, **kept)
    watcher = help_watcher(_scripted(db, tmp_path), ArgsBoard())
    watcher._save(BATCH, watcher._record(BATCH, CREATED))
    assert db.kv_get(KV_PREFIX + BATCH) == {"createdAt": CREATED, **kept}
    rec = watcher._record(BATCH, CREATED)
    assert rec["tasks"] == {"1": "F-kv"}
    assert rec["failed"] == {"2": "kv reason"}


def test_a_record_read_without_created_at_finds_the_ledger_rows(tmp_path: Path, db: ScriptedQueue) -> None:
    watcher = help_watcher(_scripted(db, tmp_path), ArgsBoard())
    help_pump(watcher)
    assert watcher._record(BATCH)["tasks"] == {"1": "F-1"}


def test_a_piece_that_is_submitted_lives_only_in_the_ledger(tmp_path: Path, db: ScriptedQueue) -> None:
    board = ArgsBoard()
    help_pump(help_watcher(_scripted(db, tmp_path), board))
    saved = db.kv_get(KV_PREFIX + BATCH)
    assert "tasks" not in saved and "failed" not in saved
    assert Ledger(db).pieces(BATCH, CREATED)[0]["foundry_task"] == "F-1"

    again = _scripted(db, tmp_path)  # a restart: the kv record no longer names the piece
    watcher = help_watcher(again, board)
    help_pump(watcher)
    assert again.factory.manager.accepted == []
    assert watcher._record(BATCH, CREATED)["tasks"] == {"1": "F-1"}


def test_a_piece_that_failed_to_submit_appears_with_its_error(tmp_path: Path, db: ScriptedQueue) -> None:
    ledger = Ledger(db)
    ledger.upsert_piece(BATCH, CREATED, "1", state="failed", error="could not be submitted: boom")
    ledger.upsert_piece(BATCH, CREATED, "2", state="failed")
    ledger.upsert_piece(BATCH, CREATED, "3", state="queued")
    ledger.upsert_piece(BATCH, CREATED, "4", foundry_task="F-4", state="failed", error="built badly")
    rec = help_watcher(_scripted(db, tmp_path), ArgsBoard())._record(BATCH, CREATED)
    assert rec["failed"] == {"1": "could not be submitted: boom", "2": "failed"}
    assert rec["tasks"] == {"4": "F-4"}


def test_a_piece_submitted_in_kv_is_not_turned_into_a_submit_failure(tmp_path: Path, db: ScriptedQueue) -> None:
    """A batch claimed before the Ledger knew foundry_task: its failed row is the task's own state."""
    _save_kv(db, tasks={"1": "F-kv"})
    Ledger(db).upsert_piece(BATCH, CREATED, "1", state="failed", error="quarantined")
    rec = help_watcher(_scripted(db, tmp_path), ArgsBoard())._record(BATCH, CREATED)
    assert rec["tasks"] == {"1": "F-kv"}
    assert rec["failed"] == {}
    assert Ledger(db).pieces(BATCH, CREATED)[0]["foundry_task"] == "F-kv"


def test_pieces_of_another_batch_with_the_same_id_are_ignored(tmp_path: Path, db: ScriptedQueue) -> None:
    kv = _save_kv(db)
    Ledger(db).upsert_piece(BATCH, "2020-01-01T00:00:00Z", "1", foundry_task="F-old", state="complete")
    assert help_watcher(_scripted(db, tmp_path), ArgsBoard())._record(BATCH, CREATED) == kv


def test_a_failing_ledger_read_falls_back_to_the_kv_record(
    tmp_path: Path, db: ScriptedQueue, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    kv = _save_kv(db, tasks={"1": "F-kv"}, failed={"2": "kv reason"})
    monkeypatch.setattr(Ledger, "pieces", _boom)
    watcher = help_watcher(_scripted(db, tmp_path), ArgsBoard())
    with caplog.at_level(logging.INFO, logger="foundry.board_dispatch"):
        rec = watcher._record(BATCH, CREATED)
        assert rec == kv
        watcher._save(BATCH, rec)
    assert "Ledger read failed" in caplog.text
    assert "Ledger write failed" in caplog.text
    assert db.kv_get(KV_PREFIX + BATCH) == kv  # still there: the Ledger may never have taken them


def test_a_queue_without_a_ledger_uses_the_kv_record(tmp_path: Path) -> None:
    queue = LegacyQueue()
    kv = _save_kv(queue, tasks={"1": "F-kv"})
    watcher = help_watcher(_scripted(queue, tmp_path), ArgsBoard())
    rec = watcher._record(BATCH, CREATED)
    assert rec == kv
    watcher._save(BATCH, rec)
    assert queue.kv_get(KV_PREFIX + BATCH) == kv


def test_a_failing_ledger_write_keeps_the_pieces_in_kv_until_it_recovers(
    tmp_path: Path, db: ScriptedQueue, monkeypatch: pytest.MonkeyPatch
) -> None:
    board = ArgsBoard()
    monkeypatch.setattr(Ledger, "upsert_piece", _boom)
    help_pump(help_watcher(_scripted(db, tmp_path), board))
    assert db.kv_get(KV_PREFIX + BATCH)["tasks"] == {"1": "F-1"}

    again = _scripted(db, tmp_path)
    help_pump(help_watcher(again, board))
    assert again.factory.manager.accepted == []
    assert db.kv_get(KV_PREFIX + BATCH)["tasks"] == {"1": "F-1"}

    monkeypatch.undo()
    help_pump(help_watcher(again, board))
    assert again.factory.manager.accepted == []
    assert "tasks" not in db.kv_get(KV_PREFIX + BATCH)
    assert Ledger(db).pieces(BATCH, CREATED)[0]["foundry_task"] == "F-1"


def test_a_failing_ledger_write_keeps_a_submit_failure_in_kv(
    tmp_path: Path, db: ScriptedQueue, monkeypatch: pytest.MonkeyPatch
) -> None:
    board = ArgsBoard()
    monkeypatch.setattr(Ledger, "upsert_piece", _boom)
    help_pump(help_watcher(_scripted(db, tmp_path, fail="no disk"), board))
    reason = "could not be submitted: no disk"
    assert db.kv_get(KV_PREFIX + BATCH)["failed"] == {"1": reason}

    again = _scripted(db, tmp_path)
    help_pump(help_watcher(again, board))
    assert again.factory.manager.accepted == []


def test_nothing_is_submitted_while_the_ledger_holding_the_pieces_cannot_be_read(
    tmp_path: Path, db: ScriptedQueue, monkeypatch: pytest.MonkeyPatch
) -> None:
    board = ArgsBoard()
    help_pump(help_watcher(_scripted(db, tmp_path), board))
    assert "tasks" not in db.kv_get(KV_PREFIX + BATCH)

    monkeypatch.setattr(Ledger, "pieces", _boom)
    again = _scripted(db, tmp_path)
    help_pump(help_watcher(again, board))
    assert again.factory.manager.accepted == []
    assert board.updates[-1]["heartbeat"] is True
    assert "results" not in board.updates[-1]
    assert "tasks" not in db.kv_get(KV_PREFIX + BATCH)  # still marked as handed to the Ledger

    monkeypatch.undo()
    watcher = help_watcher(again, board)
    help_pump(watcher)
    assert again.factory.manager.accepted == []
    assert watcher._record(BATCH, CREATED)["tasks"] == {"1": "F-1"}


# --------------------------------------------------------------------------- restarts


def test_a_restart_does_not_resubmit_a_piece_the_kv_record_lost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # the manager's saved orders would also recognise the piece: here only the Ledger can
    monkeypatch.setattr(BatchWatcher, "_find_order", lambda *args: None)
    board = FakeBoard()
    b = board.add()
    repo = write_demo_repo(tmp_path / "repo")
    first: dict[str, str] = {}

    async def run_once() -> None:
        svc = _service(tmp_path, repo=repo)
        await svc.start()
        try:
            w = _watcher(svc, board)
            await w.poll_once()
            first.update(w._record(b["id"], b["createdAt"])["tasks"])
            assert svc.factory.queue.kv_delete(KV_PREFIX + b["id"])
        finally:
            await svc.stop()

    async def run_again() -> None:
        svc = _service(tmp_path, repo=repo)
        await svc.start()
        try:
            before = set(svc.factory.manager.orders)
            w = _watcher(svc, board)
            await _pump(w, lambda: board.get(b["id"])["status"] == "done")
            assert set(svc.factory.manager.orders) == before  # nothing submitted again
            assert w._record(b["id"], b["createdAt"])["tasks"] == first
        finally:
            await svc.stop()

    asyncio.run(run_once())
    assert set(first) == {"1", "2"}
    asyncio.run(run_again())
    results = board.get(b["id"])["results"]
    assert {key: result["foundryTask"] for key, result in results.items()} == first
    assert all(result["state"] == "complete" for result in results.values())


def test_a_restart_does_not_retry_a_piece_that_failed_to_submit(tmp_path: Path, db: ScriptedQueue) -> None:
    board = ArgsBoard()
    help_pump(help_watcher(_scripted(db, tmp_path, fail="no disk"), board))
    reason = "could not be submitted: no disk"
    assert Ledger(db).pieces(BATCH, CREATED)[0]["error"] == reason
    assert db.kv_delete(KV_PREFIX + BATCH)

    again = _scripted(db, tmp_path)
    watcher = help_watcher(again, board)
    help_pump(watcher)
    assert again.factory.manager.accepted == []
    assert watcher._record(BATCH, CREATED)["failed"] == {"1": reason}
    assert board.updates[-1]["results"]["1"] == {"state": "failed", "error": reason}


# --------------------------------------------------------------------------- the board sees no difference


def _normal_run(folder: Path, ledger_read: bool) -> list[dict[str, Any]]:
    folder.mkdir()
    queue = ScriptedQueue(folder / "queue.db")
    try:
        board = ArgsBoard()
        board.batch["status"] = "claimed"
        board.batch["subtasks"].append({"key": "2", "title": "u", "files": ["b.py"], "dependsOn": ["1"]})
        watcher = help_watcher(_scripted(queue, folder, config=FoundryConfig(ledger_read=ledger_read)), board)
        help_pump(watcher)  # 1 is submitted, 2 waits for it
        queue.task_state = "complete"
        help_pump(watcher)  # 1 is done, so 2 is submitted
        return board.updates
    finally:
        queue.close()


def test_the_board_gets_the_same_updates_with_the_setting_on_and_off(tmp_path: Path) -> None:
    on = _normal_run(tmp_path / "on", True)
    off = _normal_run(tmp_path / "off", False)
    assert len(off) == 2
    assert off[0]["status"] == "running"
    assert off[1]["results"]["2"]["foundryTask"] == "F-2"
    assert on == off
