"""BatchWatcher: the combined check's status line of a subtask's PR goes out in its usage."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from foundry.board_dispatch import KV_PREFIX, BatchWatcher
from foundry.ledger import Ledger
from foundry.orchestrator_queue import FoundryQueue
from foundry.tests.test_board_dispatch_ledger import ArgsBoard, LedgerHelpQueue
from foundry.tests.test_help_watcher import BATCH, CREATED, TASK
from foundry.tests.test_help_watcher import _pump as pump
from foundry.tests.test_help_watcher import _service as make_service
from foundry.tests.test_help_watcher import _watcher as make_watcher
from foundry.tests.test_metrics_report import add_usage

KEY = "combined_check:last"
OTHER_TASK = "F-2"
PR_URL = "https://github.com/o/r/pull/12"
WORKS = "✅ Works together with the other open Foundry PRs"
CONFLICT = "❌ Conflicts with #7 in a.py"


class CountingQueue(LedgerHelpQueue):
    """Counts the reads of the combined check's entry."""

    def __init__(self, db: FoundryQueue) -> None:
        super().__init__(db)
        self.reads = 0

    def kv_get(self, key: str) -> Any:
        if key == KEY:
            self.reads += 1
        return super().kv_get(key)


class Rig:
    """Subtask 1 complete with PR #12; subtask 2 still building, so the batch stays open."""

    def __init__(self, db: FoundryQueue, conn: sqlite3.Connection, repo: Path, url: str | None = PR_URL) -> None:
        add_usage(conn, [(TASK, 10, 2)])
        self.queue = CountingQueue(db)
        self.queue.tasks[TASK] = {"state": "complete"}
        self.queue.tasks[OTHER_TASK] = {"state": "building"}
        service = make_service(self.queue, repo)
        self.queue.kv_set(KV_PREFIX + BATCH, {"createdAt": CREATED, "tasks": {"1": TASK, "2": OTHER_TASK},
                                              "failed": {}, "pushed": {}, "usage": {}})
        service.factory.telemetry = conn
        service.factory.manager.pr_state = lambda tid: (
            {"state": "opened", "url": url, "branch": "b", "error": None} if tid == TASK else None
        )
        self.board = ArgsBoard()
        self.board.batch["subtasks"].append({"key": "2", "title": "t2", "files": ["b.py"]})
        self.watcher: BatchWatcher = make_watcher(service, self.board)
        self.db = db

    def store(self, status: dict[str, Any]) -> None:
        self.queue.kv_set(KEY, {"fingerprints": {}, "status": status})

    def poll(self) -> None:
        assert pump(self.watcher) == [BATCH]
        assert self.watcher.last_error is None

    def usage(self, update: int = 0) -> dict[str, Any]:
        out: dict[str, Any] = self.board.updates[update]["results"]["1"]["usage"]
        return out


@pytest.fixture()
def db(tmp_path: Path) -> Iterator[FoundryQueue]:
    queue = FoundryQueue(tmp_path / "ledger.db")
    yield queue
    queue.close()


@pytest.fixture()
def conn() -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(":memory:")
    yield connection
    connection.close()


@pytest.fixture()
def rig(db: FoundryQueue, conn: sqlite3.Connection, tmp_path: Path) -> Rig:
    return Rig(db, conn, tmp_path)


def test_a_completed_piece_gets_the_status_line_of_its_pr(rig: Rig) -> None:
    rig.store({"12": WORKS, "7": CONFLICT})
    rig.poll()
    assert rig.board.updates[0]["results"]["1"]["state"] == "complete"
    assert rig.usage()["prCheck"] == WORKS
    assert rig.usage()["input_tokens"] == 10
    assert "usage" not in rig.board.updates[0]["results"]["2"]


def test_the_status_line_is_recorded_with_the_usage_in_the_ledger(rig: Rig) -> None:
    rig.store({"12": WORKS})
    rig.poll()
    piece = next(p for p in Ledger(rig.db).pieces(BATCH, CREATED) if p["key"] == "1")
    assert piece["usage"]["prCheck"] == WORKS


def test_a_state_stored_as_a_json_string_is_read(rig: Rig) -> None:
    rig.queue.kv_set(KEY, json.dumps({"status": {"12": WORKS}}))
    rig.poll()
    assert rig.usage()["prCheck"] == WORKS


def test_a_long_line_is_cut_to_300_characters(rig: Rig) -> None:
    rig.store({"12": "x" * 400})
    rig.poll()
    assert rig.usage()["prCheck"] == "x" * 300


def test_control_characters_never_reach_the_board(rig: Rig) -> None:
    rig.store({"12": "⚠️ Could not run:\r\n\tgit\x1b failed\n"})
    rig.poll()
    assert rig.usage()["prCheck"] == "⚠️ Could not run: git  failed"


@pytest.mark.parametrize("status", [{}, {"7": CONFLICT}, {"12": ""}, {"12": None}, {"12": 5}])
def test_no_stored_line_means_no_pr_check(rig: Rig, status: dict[str, Any]) -> None:
    rig.store(status)
    rig.poll()
    assert "prCheck" not in rig.usage()


def test_nothing_stored_means_no_pr_check(rig: Rig) -> None:
    rig.poll()
    assert "prCheck" not in rig.usage()


@pytest.mark.parametrize("value", ["{not json", "[1, 2]", 7, ["12"], {"status": "12"}, {"status": ["12"]}])
def test_a_malformed_value_is_ignored(rig: Rig, value: Any) -> None:
    rig.queue.kv_set(KEY, value)
    rig.poll()
    assert rig.board.updates[0]["results"]["1"]["state"] == "complete"
    assert "prCheck" not in rig.usage()


def test_a_failing_read_is_ignored(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    real = rig.queue.kv_get

    def broken(key: str) -> Any:
        if key == KEY:
            raise RuntimeError("database is locked")
        return real(key)

    monkeypatch.setattr(rig.queue, "kv_get", broken)
    rig.poll()
    assert "prCheck" not in rig.usage()


@pytest.mark.parametrize("url", [None, "https://github.com/o/r/pull/12/files", "https://github.com/o/r/issues/12"])
def test_a_url_not_ending_in_a_pr_number_has_no_status(
    db: FoundryQueue, conn: sqlite3.Connection, tmp_path: Path, url: str | None
) -> None:
    rig = Rig(db, conn, tmp_path, url)
    rig.store({"12": WORKS})
    rig.poll()
    assert "prCheck" not in rig.usage()
    assert rig.queue.reads == 0


def test_a_changed_line_goes_out_with_the_next_due_write(rig: Rig) -> None:
    rig.store({"12": WORKS})
    rig.poll()
    assert len(rig.board.updates) == 1

    rig.store({"12": CONFLICT})
    rig.poll()
    assert len(rig.board.updates) == 1  # a new line alone adds no board write

    rig.watcher.heartbeat_s = 0.0
    rig.poll()
    assert len(rig.board.updates) == 2
    assert rig.usage(1)["prCheck"] == CONFLICT


def test_an_unchanged_line_is_not_sent_again(rig: Rig) -> None:
    rig.store({"12": WORKS})
    rig.poll()
    rig.watcher.heartbeat_s = 0.0
    rig.poll()
    assert len(rig.board.updates) == 2
    assert "results" not in rig.board.updates[1]


def test_the_state_is_read_once_per_round(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    rig.store({"12": WORKS})
    second = {**rig.board.batch, "id": "S-2", "results": {}}
    state = rig.board.state

    def both() -> dict[str, Any]:
        return {"foundryBatches": [*state()["foundryBatches"], dict(second)]}

    monkeypatch.setattr(rig.board, "state", both)
    rig.queue.kv_set(KV_PREFIX + "S-2", {"createdAt": CREATED, "tasks": {"1": TASK, "2": OTHER_TASK},
                                         "failed": {}, "pushed": {}, "usage": {}})
    assert pump(rig.watcher) == [BATCH, "S-2"]
    assert rig.watcher.last_error is None
    assert rig.queue.reads == 1

    rig.watcher.heartbeat_s = 0.0
    pump(rig.watcher)
    assert rig.queue.reads == 2
