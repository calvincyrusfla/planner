"""BatchWatcher records every batch and piece in the Ledger; the board sees no difference."""

from __future__ import annotations

import asyncio
import copy
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Mapping

import pytest

from foundry.board_dispatch import marker
from foundry.ledger import Ledger
from foundry.orchestrator_queue import FoundryQueue
from foundry.tests.test_board_dispatch import FakeBoard, FakePublisher, _pump, _service, _watcher
from foundry.tests.test_help_watcher import BATCH, CREATED, REQUEST, TICKET, HelpQueue, LegacyQueue
from foundry.tests.test_help_watcher import FakeBoard as HelpBoard
from foundry.tests.test_help_watcher import _pump as help_pump
from foundry.tests.test_help_watcher import _service as help_service
from foundry.tests.test_help_watcher import _watcher as help_watcher

SUBTASKS = [
    {"key": "1", "title": "Add clamp helper", "description": "Add clamp(x, lo, hi).", "files": ["shop/mathx.py"],
     "acceptance": ["clamp works"]},
    {"key": "2", "title": "Add lerp helper", "description": "Add lerp(a, b, t).", "files": ["shop/mathx.py"],
     "acceptance": ["lerp works"]},
]


@pytest.fixture()
def db(tmp_path: Path) -> Iterator[FoundryQueue]:
    queue = FoundryQueue(tmp_path / "ledger.db")
    yield queue
    queue.close()


class LedgerHelpQueue(HelpQueue):
    """The scripted help queue of the help tests, with a real Ledger store behind it."""

    def __init__(self, db: FoundryQueue) -> None:
        super().__init__()
        self.transaction = db.transaction
        self.now = db.now
        self.record_event = db.record_event


class ArgsBoard(HelpBoard):
    """Keeps the whole args of every updateBatch."""

    def __init__(self) -> None:
        super().__init__()
        self.updates: list[dict[str, Any]] = []

    def op(self, type_: str, args: Mapping[str, Any]) -> Any:
        if type_ == "updateBatch":
            self.updates.append(copy.deepcopy(dict(args)))
        return super().op(type_, args)


def _help_piece(db: FoundryQueue) -> dict[str, Any]:
    pieces = Ledger(db).pieces(BATCH, CREATED)
    assert len(pieces) == 1
    return pieces[0]


# --------------------------------------------------------------------------- the real service


def test_a_claimed_batch_and_its_submitted_pieces_are_recorded(tmp_path: Path) -> None:
    board = FakeBoard()
    b = board.add(subtasks=copy.deepcopy(SUBTASKS))

    async def go() -> None:
        svc = _service(tmp_path)
        await svc.start()
        try:
            w = _watcher(svc, board)
            await w.poll_once()
            assert w.last_error is None
            ledger = Ledger(svc.factory.queue)
            row = ledger.batch(b["id"], b["createdAt"])
            assert row is not None
            assert row["title"] == b["title"]
            assert row["repo"] == b["repo"]
            assert row["status"] == "running"
            tasks = w._record(b["id"])["tasks"]
            pieces = ledger.pieces(b["id"], b["createdAt"])
            assert [p["key"] for p in pieces] == ["1", "2"]
            for piece, sub in zip(pieces, SUBTASKS):
                assert piece["title"] == sub["title"]
                assert sub["description"] in piece["order_text"]
                assert marker(b["id"], piece["key"], b["createdAt"]) in piece["order_text"]
                assert piece["files"] == sub["files"]
                assert isinstance(piece["read_files"], list)
                assert piece["acceptance"] == sub["acceptance"]
                assert piece["foundry_task"] == tasks[piece["key"]]
                assert piece["state"]
        finally:
            await svc.stop()

    asyncio.run(go())


def test_finished_pieces_show_their_state_and_pr(tmp_path: Path) -> None:
    board = FakeBoard()
    b = board.add(subtasks=copy.deepcopy(SUBTASKS))

    async def go() -> None:
        svc = _service(tmp_path, publisher=FakePublisher())
        await svc.start()
        try:
            w = _watcher(svc, board)
            await _pump(w, lambda: board.get(b["id"])["status"] == "done")
            pieces = Ledger(svc.factory.queue).pieces(b["id"], b["createdAt"])
            assert [p["key"] for p in pieces] == ["1", "2"]
            for piece in pieces:
                assert piece["state"] == "complete"
                assert piece["pr_url"].startswith("https://github.com/me/shop/pull/")
                assert piece["pr_state"] == "opened"
                assert piece["error"] is None
        finally:
            await svc.stop()

    asyncio.run(go())


def test_a_failing_ledger_does_not_stop_the_board_update(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: Any, **kwargs: Any) -> bool:
        raise RuntimeError("ledger down")

    monkeypatch.setattr(Ledger, "upsert_piece", boom)
    board = FakeBoard()
    b = board.add(subtasks=copy.deepcopy(SUBTASKS))

    async def go() -> None:
        svc = _service(tmp_path)
        await svc.start()
        try:
            w = _watcher(svc, board)
            assert await w.poll_once() == [b["id"]]
            assert w.last_error is None
            assert set(w._record(b["id"])["tasks"]) == {"1", "2"}
        finally:
            await svc.stop()

    asyncio.run(go())
    updates = [args for type_, args in board.ops if type_ == "updateBatch"]
    assert len(updates) == 1
    assert set(updates[0]["results"]) == {"1", "2"}
    assert updates[0]["status"] == "running"


# --------------------------------------------------------------------------- scripted help rounds


def test_a_help_question_and_its_applied_answer_are_recorded(tmp_path: Path, db: FoundryQueue) -> None:
    queue, board = LedgerHelpQueue(db), HelpBoard()
    watcher = help_watcher(help_service(queue, tmp_path), board)
    queue.pause_for_help(TICKET, REQUEST)
    help_pump(watcher)
    piece = _help_piece(db)
    assert piece["state"] == "needs_help"
    assert piece["help_question"] == REQUEST["question"]
    assert piece["help_answer"] is None

    board.answer("Try again", "2026-11-08T10:00:00Z")
    help_pump(watcher)
    piece = _help_piece(db)
    assert piece["help_answer"] == "Try again"
    assert piece["state"] == "building"


def test_usage_of_the_round_is_recorded(tmp_path: Path, db: FoundryQueue, monkeypatch: pytest.MonkeyPatch) -> None:
    def with_usage(rec: Any, view: Mapping[str, Any], changed: Any) -> dict[str, dict[str, Any]]:
        return {"1": {**view["1"], "usage": {"tokens": 5}}}

    watcher = help_watcher(help_service(LedgerHelpQueue(db), tmp_path), HelpBoard())
    monkeypatch.setattr(watcher, "_usage_results", with_usage)
    help_pump(watcher)
    assert _help_piece(db)["usage"] == {"tokens": 5}


def test_the_ledger_is_written_on_a_round_without_a_board_write(tmp_path: Path, db: FoundryQueue) -> None:
    board = ArgsBoard()
    watcher = help_watcher(help_service(LedgerHelpQueue(db), tmp_path), board)
    help_pump(watcher)
    assert len(board.updates) == 1
    board.batch["title"] = "Renamed"
    help_pump(watcher)
    assert len(board.updates) == 1
    row = Ledger(db).batch(BATCH, CREATED)
    assert row is not None
    assert row["title"] == "Renamed"


def _updates_of_a_help_round(queue: HelpQueue, tmp_path: Path) -> list[dict[str, Any]]:
    board = ArgsBoard()
    watcher = help_watcher(help_service(queue, tmp_path), board)
    queue.pause_for_help(TICKET, REQUEST)
    assert help_pump(watcher) == [BATCH]
    return board.updates


def test_the_board_gets_the_same_args_with_and_without_the_ledger(tmp_path: Path, db: FoundryQueue) -> None:
    with_ledger = _updates_of_a_help_round(LedgerHelpQueue(db), tmp_path)
    assert _help_piece(db)["state"] == "needs_help"
    without_ledger = _updates_of_a_help_round(HelpQueue(), tmp_path)  # a queue the Ledger cannot use
    assert with_ledger == without_ledger
    assert with_ledger[0]["results"]["1"]["state"] == "needs_help"


def test_the_ledger_is_made_once(tmp_path: Path, db: FoundryQueue) -> None:
    watcher = help_watcher(help_service(LedgerHelpQueue(db), tmp_path), HelpBoard())
    first = watcher._ledger()
    assert first is not None
    assert watcher._ledger() is first


def test_an_unreadable_queue_gives_no_ledger_and_is_logged_once(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    watcher = help_watcher(help_service(LegacyQueue(), tmp_path), HelpBoard())
    with caplog.at_level(logging.INFO, logger="foundry.board_dispatch"):
        assert watcher._ledger() is None
        assert watcher._ledger() is None
    assert len([r for r in caplog.records if "Ledger" in r.getMessage()]) == 1
