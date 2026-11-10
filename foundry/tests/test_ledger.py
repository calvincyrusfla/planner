"""Unit tests for foundry.ledger.Ledger (batch and piece records on the queue's database)."""

from __future__ import annotations

import itertools
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

import pytest

from foundry.ledger import Ledger
from foundry.order_schema import BATCH_STATUSES
from foundry.orchestrator_queue import FoundryQueue

CREATED = "2026-11-10T13:20:00.893Z"
LATER = "2026-11-11T09:00:00.000Z"
START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def ticking_clock() -> Callable[[], datetime]:
    """A clock that moves one second per reading, so every write gets its own timestamp."""
    ticks = itertools.count()
    return lambda: START + timedelta(seconds=next(ticks))


@pytest.fixture()
def q(tmp_path: Path) -> Iterator[FoundryQueue]:
    queue = FoundryQueue(tmp_path / "foundry.db", clock=ticking_clock())
    yield queue
    queue.close()


@pytest.fixture()
def ledger(q: FoundryQueue) -> Ledger:
    return Ledger(q)


def board_batch(**over: Any) -> dict[str, Any]:
    return {
        "id": "S-1", "createdAt": CREATED, "title": "Batch", "goal": "The goal",
        "repo": "me/shop", "base": None, "status": "queued", "claimedBy": None,
        "finishedAt": None, **over,
    }


def stored_batch(
    ledger: Ledger, batch_id: str = "S-1", created_at: str | None = None
) -> dict[str, Any]:
    row = ledger.batch(batch_id, created_at)
    assert row is not None
    return row


def test_tables_created_twice_without_error(q: FoundryQueue) -> None:
    Ledger(q)
    Ledger(q)
    names = {r["name"] for r in q.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"ledger_batches", "ledger_pieces"} <= names


def test_records_survive_a_reopened_database(tmp_path: Path) -> None:
    path = tmp_path / "foundry.db"
    with FoundryQueue(path) as first:
        ledger = Ledger(first)
        ledger.upsert_batch(board_batch())
        ledger.upsert_piece("S-1", CREATED, "1", title="Piece", files=["a.py"])
    with FoundryQueue(path) as second:
        ledger = Ledger(second)
        assert stored_batch(ledger)["title"] == "Batch"
        assert [p["files"] for p in ledger.pieces("S-1", CREATED)] == [["a.py"]]


def test_upsert_batch_maps_the_board_fields(ledger: Ledger) -> None:
    ledger.upsert_batch(board_batch(
        base="main", status="done", finishedAt=LATER,
        claimedBy={"host": "pc", "service": "foundry", "at": CREATED},
    ))
    row = stored_batch(ledger)
    assert row["batch_id"] == "S-1"
    assert row["created_at"] == CREATED
    assert row["goal"] == "The goal"
    assert row["repo"] == "me/shop"
    assert row["base"] == "main"
    assert row["status"] == "done"
    assert row["claimed_at"] == CREATED
    assert row["finished_at"] == LATER
    assert row["updated_at"]


def test_same_batch_twice_is_true_then_false_with_one_event(q: FoundryQueue, ledger: Ledger) -> None:
    assert ledger.upsert_batch(board_batch()) is True
    before = stored_batch(ledger)
    assert ledger.upsert_batch(board_batch()) is False
    assert stored_batch(ledger) == before
    events = q.events(kind="ledger_batch")
    assert len(events) == 1
    assert events[0]["task_id"] is None
    payload = events[0]["payload"]
    assert payload["batch_id"] == "S-1"
    assert payload["created_at"] == CREATED
    assert "key" not in payload
    assert payload["changed"] == {
        "title": "Batch", "goal": "The goal", "repo": "me/shop", "status": "queued",
    }


def test_changed_status_gives_a_second_event_with_only_status(
    q: FoundryQueue, ledger: Ledger
) -> None:
    ledger.upsert_batch(board_batch())
    before = stored_batch(ledger)
    assert ledger.upsert_batch(board_batch(status="running")) is True
    after = stored_batch(ledger)
    assert after["status"] == "running"
    assert after["updated_at"] != before["updated_at"]
    events = q.events(kind="ledger_batch")
    assert len(events) == 2
    assert events[1]["payload"]["changed"] == {"status": "running"}


def test_piece_json_round_trip(ledger: Ledger) -> None:
    usage = {"input": 10, "nested": {"output": [1, 2]}}
    ledger.upsert_piece(
        "S-1", CREATED, "1", files=["a.py", "b.py"], read_files=[], acceptance=["works"],
        usage=usage,
    )
    piece = ledger.pieces("S-1", CREATED)[0]
    assert piece["key"] == "1"
    assert piece["files"] == ["a.py", "b.py"]
    assert piece["read_files"] == []
    assert piece["acceptance"] == ["works"]
    assert piece["usage"] == usage
    assert piece["title"] is None


def test_piece_event_carries_the_foundry_task(q: FoundryQueue, ledger: Ledger) -> None:
    assert ledger.upsert_piece("S-1", CREATED, "1", foundry_task="F-1", files=["a.py"]) is True
    events = q.events(kind="ledger_piece")
    assert len(events) == 1
    assert events[0]["task_id"] == "F-1"
    assert events[0]["payload"] == {
        "batch_id": "S-1", "created_at": CREATED, "key": "1",
        "changed": {"foundry_task": "F-1", "files": ["a.py"]},
    }


def test_partial_piece_update_keeps_other_fields_and_stored_task(
    q: FoundryQueue, ledger: Ledger
) -> None:
    ledger.upsert_piece("S-1", CREATED, "1", foundry_task="F-1", title="Piece")
    assert ledger.upsert_piece("S-1", CREATED, "1", state="building") is True
    piece = ledger.pieces("S-1", CREATED)[0]
    assert piece["title"] == "Piece"
    assert piece["state"] == "building"
    latest = q.events(kind="ledger_piece")[-1]
    assert latest["task_id"] == "F-1"
    assert latest["payload"]["changed"] == {"state": "building"}


def test_unchanged_piece_writes_nothing(q: FoundryQueue, ledger: Ledger) -> None:
    ledger.upsert_piece("S-1", CREATED, "1", usage={"a": 1, "b": 2}, files=["a.py"])
    before = ledger.pieces("S-1", CREATED)
    assert ledger.upsert_piece("S-1", CREATED, "1", usage={"b": 2, "a": 1}, files=["a.py"]) is False
    assert ledger.pieces("S-1", CREATED) == before
    assert len(q.events(kind="ledger_piece")) == 1


def test_unknown_field_raises_and_writes_nothing(q: FoundryQueue, ledger: Ledger) -> None:
    with pytest.raises(ValueError, match="colour"):
        ledger.upsert_piece("S-1", CREATED, "1", title="Piece", colour="red")
    assert ledger.pieces("S-1", CREATED) == []
    assert q.events(kind="ledger_piece") == []


def test_a_failed_event_rolls_the_row_back(
    q: FoundryQueue, ledger: Ledger, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args: Any, **kwargs: Any) -> int:
        raise RuntimeError("event log down")

    monkeypatch.setattr(q, "record_event", boom)
    with pytest.raises(RuntimeError):
        ledger.upsert_batch(board_batch())
    with pytest.raises(RuntimeError):
        ledger.upsert_piece("S-1", CREATED, "1", title="Piece")
    assert ledger.batch("S-1") is None
    assert ledger.pieces("S-1", CREATED) == []


def test_same_id_with_different_created_at_is_kept_apart(ledger: Ledger) -> None:
    assert ledger.upsert_batch(board_batch(title="Old")) is True
    assert ledger.upsert_batch(board_batch(createdAt=LATER, title="New")) is True
    ledger.upsert_piece("S-1", CREATED, "1", title="Old piece")
    ledger.upsert_piece("S-1", LATER, "1", title="New piece")
    assert stored_batch(ledger, created_at=CREATED)["title"] == "Old"
    assert stored_batch(ledger, created_at=LATER)["title"] == "New"
    assert stored_batch(ledger)["title"] == "New"
    assert [p["title"] for p in ledger.pieces("S-1", CREATED)] == ["Old piece"]
    assert [p["title"] for p in ledger.pieces("S-1", LATER)] == ["New piece"]


def test_unknown_batch_reads_as_none_and_no_pieces(ledger: Ledger) -> None:
    assert ledger.batch("S-404") is None
    assert ledger.pieces("S-404", CREATED) == []


def test_open_batches_excludes_finished_ones(ledger: Ledger) -> None:
    for status in BATCH_STATUSES:
        ledger.upsert_batch(board_batch(id=f"S-{status}", status=status))
    assert {b["batch_id"] for b in ledger.open_batches()} == {
        "S-queued", "S-claimed", "S-running",
    }
