"""Sizing outcomes record the highest context level a task's tickets were built at."""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from foundry.builder_worker import CONTEXT_LEVEL_USED_KV_PREFIX
from foundry.orchestrator_queue import FoundryQueue


@pytest.fixture
def queue(tmp_path: Path) -> Iterator[FoundryQueue]:
    with FoundryQueue(tmp_path / "queue.db") as q:
        yield q


def _add_ticket(q: FoundryQueue, task_id: str, subtask_id: str) -> str:
    ticket_id = q.ticket_id_for(task_id, subtask_id)
    now = q.now()
    with q.transaction() as conn:
        conn.execute(
            "INSERT INTO tickets (ticket_id, task_id, team_id, subtask_id, slot, status,"
            " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (ticket_id, task_id, "alpha", subtask_id, 1, "done", now, now),
        )
    return ticket_id


def _add_task(q: FoundryQueue, task_id: str, *levels: object) -> None:
    """A task with one ticket per entry of ``levels`` (None: the ticket recorded no level)."""
    q.add_task(task_id, "title", "description", "alpha")
    for index, level in enumerate(levels, start=1):
        ticket_id = _add_ticket(q, task_id, f"{task_id}.{index}")
        if level is not None:
            q.kv_set(f"{CONTEXT_LEVEL_USED_KV_PREFIX}{ticket_id}", level)


def _recorded_level(q: FoundryQueue, task_id: str) -> int:
    outcome = q.record_sizing_outcome(task_id)
    assert outcome is not None
    return int(outcome["context_level"])


def test_schema_has_context_level_defaulting_to_one(queue: FoundryQueue) -> None:
    columns = {r["name"]: r for r in queue.conn.execute("PRAGMA table_info(sizing_outcomes)")}
    assert columns["context_level"]["dflt_value"] == "1"
    assert columns["context_level"]["notnull"] == 1


def test_old_database_gets_the_column_defaulting_to_one(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE sizing_outcomes (task_id TEXT PRIMARY KEY, recorded_at TEXT NOT NULL,"
        " repo TEXT, final_state TEXT NOT NULL)"
    )
    conn.execute("INSERT INTO sizing_outcomes VALUES ('T-old', 'then', NULL, 'complete')")
    conn.commit()
    conn.close()
    with FoundryQueue(path) as q:
        row = q.conn.execute(
            "SELECT context_level FROM sizing_outcomes WHERE task_id='T-old'"
        ).fetchone()
    assert row["context_level"] == 1


def test_records_the_highest_level_of_the_tasks_tickets(queue: FoundryQueue) -> None:
    _add_task(queue, "T-1", 1, 3, 2)
    assert _recorded_level(queue, "T-1") == 3


def test_tickets_without_a_recorded_level_count_as_level_one(queue: FoundryQueue) -> None:
    _add_task(queue, "T-1", None, None)
    assert _recorded_level(queue, "T-1") == 1


def test_a_task_without_tickets_records_level_one(queue: FoundryQueue) -> None:
    _add_task(queue, "T-1")
    assert _recorded_level(queue, "T-1") == 1


def test_other_tasks_tickets_are_ignored(queue: FoundryQueue) -> None:
    _add_task(queue, "T-1", 2)
    _add_task(queue, "T-2", 3)
    assert _recorded_level(queue, "T-1") == 2


@pytest.mark.parametrize(("stored", "expected"), [("junk", 2), ([3], 2), (9, 3)])
def test_bad_levels_are_tolerated(queue: FoundryQueue, stored: object, expected: int) -> None:
    _add_task(queue, "T-1", 2, stored)
    assert _recorded_level(queue, "T-1") == expected
