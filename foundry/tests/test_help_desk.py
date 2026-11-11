"""Tests for foundry.help_desk: fact questions answered from the code library."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

import pytest

from foundry import help_desk
from foundry.help_desk import HelpDesk, extract_identifiers
from foundry.ledger import Ledger
from foundry.orchestrator_queue import FoundryQueue

CREATED = "2026-11-10T13:20:00.893Z"


class FakeBoard:
    def __init__(self) -> None:
        self.ops: list[tuple[str, dict[str, Any]]] = []

    def op(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        self.ops.append((name, args))
        return {"ok": True}


class FakeManager:
    def __init__(self, root: Path) -> None:
        self.root = root

    async def task_base(self, task_id: str) -> Path:
        return self.root


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[SimpleNamespace]:
    monkeypatch.delenv("FOUNDRY_HELP_DESK", raising=False)
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "mod.py").write_text(
        "def compute_total(items, tax=0):\n    return sum(items) + tax\n\n\n"
        "class Cart:\n    def add(self, item):\n        return item\n",
        encoding="utf-8",
    )
    queue = FoundryQueue(tmp_path / "foundry.db")
    ledger = Ledger(queue)
    ledger.upsert_piece("S-1", CREATED, "S-1.1", foundry_task="T-1", title="x")
    board = FakeBoard()
    service = SimpleNamespace(
        factory=SimpleNamespace(queue=queue, manager=FakeManager(repo)),
        batch_watcher=SimpleNamespace(board=board),
    )
    desk = HelpDesk(service, interval_s=0.01)
    yield SimpleNamespace(queue=queue, board=board, desk=desk, repo=repo)
    asyncio.run(desk.stop())
    queue.close()


def _ask(env: SimpleNamespace, question: str, context: str = "") -> None:
    env.queue.record_event(
        "help_requested", "T-1", {"ticket_id": "k1", "question": question, "context": context, "options": []}
    )


def _desk_events(env: SimpleNamespace) -> list[dict[str, Any]]:
    return list(env.queue.events(kind="help_desk"))


def test_fact_question_is_answered(env: SimpleNamespace) -> None:
    _ask(env, "Where is `compute_total` defined and what is its signature?")
    asyncio.run(env.desk.round())
    assert len(env.board.ops) == 1
    name, args = env.board.ops[0]
    assert name == "answerHelp"
    assert (args["id"], args["key"], args["by"]) == ("S-1", "S-1.1", "help-desk")
    assert args["answer"].startswith("Auto-answer from the code library (help desk):")
    assert "pkg/mod.py::compute_total" in args["answer"]
    assert "pkg/mod.py:1-2" in args["answer"]
    assert "return sum(items) + tax" in args["answer"]
    events = _desk_events(env)
    assert len(events) == 1 and events[0]["payload"]["answered"] is True


def test_not_repeated_after_restart(env: SimpleNamespace) -> None:
    _ask(env, "Where is `compute_total`?")
    asyncio.run(env.desk.round())
    asyncio.run(env.desk.round())
    assert len(env.board.ops) == 1


def test_decision_question_left_alone(env: SimpleNamespace) -> None:
    _ask(env, "Should I rename `compute_total` or keep it? Where would you rather it lives?")
    asyncio.run(env.desk.round())
    assert env.board.ops == []
    payload = _desk_events(env)[0]["payload"]
    assert payload["answered"] is False and "fact" in payload["reason"]


def test_unresolved_name_left_alone(env: SimpleNamespace) -> None:
    _ask(env, "Where is `compute_total` and where is `no_such_thing`?")
    asyncio.run(env.desk.round())
    assert env.board.ops == []
    payload = _desk_events(env)[0]["payload"]
    assert payload["answered"] is False and "no_such_thing" in payload["reason"]


def test_answer_is_capped(env: SimpleNamespace) -> None:
    body = "\n".join(f"    x{i} = {i}" for i in range(2000))
    (env.repo / "pkg" / "big.py").write_text(f"def huge_one():\n{body}\n", encoding="utf-8")
    _ask(env, "Where is `huge_one`?")
    asyncio.run(env.desk.round())
    answer = env.board.ops[0][1]["answer"]
    assert len(answer) <= 5500
    assert answer.startswith("Auto-answer from the code library (help desk):")


def test_off_by_setting(env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOUNDRY_HELP_DESK", "0")
    _ask(env, "Where is `compute_total`?")
    asyncio.run(env.desk.round())
    assert env.board.ops == [] and _desk_events(env) == []
    assert help_desk.help_desk_enabled() is False


def test_extract_identifiers() -> None:
    names = extract_identifiers("See `Cart.add`, run helper(x) and foundry/ledger.py for Foo.bar")
    assert "Cart.add" in names and "helper" in names and "foundry/ledger.py" in names and "Foo.bar" in names
