"""Pausing a ticket for the Floor Manager's help: queue and builder together."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from foundry.builder_worker import BuilderWorker
from foundry.schemas import TaskState
from foundry.tests.test_builder_worker import TEAM, Rig

#: Loosely typed on purpose: the rig's fakes only implement what these tests exercise.
_make_rig: Any = Rig
_make_worker: Any = BuilderWorker

TASK = "T-HELP"
BLOCKED_HINT = "BLOCKED: need the body of helper_function"
GATE_HINT = "[syntax] app.py:3: invalid syntax"
NOTE = "Call helper_function from utils and leave its signature alone."


@pytest.fixture(autouse=True)
def _default_help_after(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FOUNDRY_HELP_AFTER", raising=False)


def _rig(tmp_path: Path, script: Any = None) -> Any:
    reply = script or (lambda prompt, agent: "BLOCKED: still stuck")
    rig = _make_rig(tmp_path, reply, gates=None, context=False)
    rig.submit(TASK, "S")
    return rig


def _worker(rig: Any, help_after: int | None = None) -> Any:
    worker = _make_worker(TEAM, 1, rig.queue, rig.client, rig.limiter, rig.telemetry, None, None,
                          rig.cache, rig.foreman)
    if help_after is not None:
        worker.config = SimpleNamespace(help_after=help_after)
    return worker


def _ticket(rig: Any) -> Any:
    return rig.queue.tickets_for_task(TASK)[0]


def _fail_once(rig: Any, hint: str, *, reason: str = "blocked", help_after: int | None = None) -> str:
    """Claim the ticket and send it back once, the way a builder does after a failed attempt."""

    async def scenario() -> str:
        ticket = rig.queue.claim_ticket(TEAM, 1)
        assert ticket is not None
        outcome: str = _worker(rig, help_after)._send_back(ticket, hint, reason=reason)
        return outcome

    return asyncio.run(scenario())


def _pause(rig: Any) -> str:
    """Two missing-information failures in a row; returns the paused ticket's id."""
    assert _fail_once(rig, BLOCKED_HINT) == "retry"
    assert _fail_once(rig, BLOCKED_HINT) == "awaiting_help"
    ticket_id: str = _ticket(rig).ticket_id
    return ticket_id


def test_two_blocked_send_backs_pause_instead_of_reopening(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    assert _fail_once(rig, "BLOCKED: need the body of first_helper") == "retry"
    assert _fail_once(rig, BLOCKED_HINT) == "awaiting_help"

    ticket = _ticket(rig)
    assert ticket.status == "awaiting_help"
    assert ticket.attempt == 1
    assert ticket.claimed_by is None
    requests = rig.queue.help_requests()
    assert len(requests) == 1
    request = requests[0]
    assert request["ticket_id"] == ticket.ticket_id
    assert request["task_id"] == TASK
    assert request["attempt"] == 1
    assert request["question"] == BLOCKED_HINT
    assert request["context"] == f"BLOCKED: need the body of first_helper\n{BLOCKED_HINT}"
    assert request["attempts_used"] == 2
    assert request["attempts_left"] == 2
    assert rig.queue.events(task_id=TASK, kind="help_requested")


def test_gate_failure_then_blocked_does_not_pause(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    assert _fail_once(rig, GATE_HINT, reason="gate_failure") == "retry"
    assert _fail_once(rig, BLOCKED_HINT) == "retry"

    ticket = _ticket(rig)
    assert ticket.status == "open"
    assert ticket.attempt == 2
    assert rig.queue.help_requests() == []


def test_a_ticket_pauses_at_most_once(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    ticket_id = _pause(rig)
    assert rig.queue.answer_help(ticket_id, NOTE)

    assert _fail_once(rig, BLOCKED_HINT) == "retry"

    ticket = _ticket(rig)
    assert ticket.status == "open"
    assert ticket.attempt == 2
    assert rig.queue.help_requests() == []


def test_awaiting_help_ticket_is_not_claimed(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    ticket_id = _pause(rig)

    claimed = rig.queue.claim_ticket(TEAM, 1)

    assert claimed is None or claimed.ticket_id != ticket_id
    assert _ticket(rig).status == "awaiting_help"


def test_answer_help_reopens_with_attempt_unchanged(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    ticket_id = _pause(rig)

    assert rig.queue.answer_help(ticket_id, NOTE, ["docs/notes.md"])

    ticket = _ticket(rig)
    assert ticket.status == "open"
    assert ticket.attempt == 1
    assert rig.queue.help_requests() == []
    assert rig.queue.kv_get(f"help_request:{ticket_id}") is None
    answer = rig.queue.kv_get(f"help_answer:{ticket_id}")
    assert answer["note"] == NOTE
    assert answer["read_files"] == ["docs/notes.md"]
    assert rig.queue.events(task_id=TASK, kind="help_answered")
    assert not rig.queue.answer_help(ticket_id, "a second answer")
    assert not rig.queue.answer_help("no-such-ticket", NOTE)


def test_next_prompt_carries_the_floor_manager_note(tmp_path: Path) -> None:
    prompts: list[str] = []

    def script(prompt: str, agent: str) -> str:
        prompts.append(prompt)
        return "BLOCKED: still stuck"

    rig = _rig(tmp_path, script)
    ticket_id = _pause(rig)
    rig.queue.answer_help(ticket_id, NOTE, ["docs/notes.md"])

    async def scenario() -> None:
        ticket = rig.queue.claim_ticket(TEAM, 1)
        assert ticket is not None
        worker = _worker(rig)
        assert "docs/notes.md" in worker._read_files(ticket, ["src/only.py"])
        await worker.process_ticket(ticket)

    asyncio.run(scenario())

    assert any("## Floor Manager note (follow it)" in p and NOTE in p for p in prompts)


def test_expire_help_quarantines_after_the_timeout(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    ticket_id = _pause(rig)

    assert rig.queue.expire_help(3600) == []
    assert _ticket(rig).status == "awaiting_help"

    key = f"help_request:{ticket_id}"
    rig.queue.kv_set(key, {**rig.queue.kv_get(key), "asked_at": "2000-01-01T00:00:00+00:00"})

    assert rig.queue.expire_help(3600) == [ticket_id]

    assert rig.queue.get_task(TASK)["state"] == TaskState.QUARANTINED
    reason = rig.queue.events(task_id=TASK, kind="quarantined")[-1]["payload"]["reason"]
    assert reason == f"no Floor Manager answer within 1 h: {BLOCKED_HINT}"
    assert rig.queue.help_requests() == []


def test_help_after_zero_never_pauses(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    assert _fail_once(rig, BLOCKED_HINT, help_after=0) == "retry"
    assert _fail_once(rig, BLOCKED_HINT, help_after=0) == "retry"

    ticket = _ticket(rig)
    assert ticket.status == "open"
    assert ticket.attempt == 2
    assert rig.queue.help_requests() == []
