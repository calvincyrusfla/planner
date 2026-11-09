"""Pausing a ticket for the Floor Manager's help: queue and builder together."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from foundry.builder_worker import BuilderWorker, _is_missing_information, _WorkerBase
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


PRE_REVIEW_MISSING_HINT = "[pre-review] blocking: The core requirement is missing from the provided diff"
REPLY_BLOCKED_HINT = "Fix the reply: builder reported BLOCKED: need the body of `helper`."


class _PauseRuleWorker(_WorkerBase):
    """Just the pause bookkeeping of a worker, on an in-memory kv store."""

    agent_id = "alpha-1"
    role = "builder"
    kv: dict[str, Any]

    @property
    def help_after(self) -> int:
        return 2

    @property
    def max_retries(self) -> int:
        return 5

    def _kv_read(self, key: Any, *_args: Any, **_kwargs: Any) -> Any:
        return self.kv.get(key)

    def _kv_write(self, key: Any, value: Any, *_args: Any, **_kwargs: Any) -> None:
        self.kv[key] = value


def _pause_rule_worker() -> _PauseRuleWorker:
    client: Any = None
    queue = SimpleNamespace(pause_for_help=lambda *_args, **_kwargs: True)
    worker = _PauseRuleWorker("alpha", queue, client, None, None)
    worker.kv = {}
    return worker


def test_pre_review_between_blocked_replies_resets_the_count() -> None:
    worker = _pause_rule_worker()
    assert worker._help_request("t-1", 0, REPLY_BLOCKED_HINT, "blocked") is None
    assert worker._help_request("t-1", 1, PRE_REVIEW_MISSING_HINT, "pre_review") is None
    assert worker._help_request("t-1", 2, REPLY_BLOCKED_HINT, "blocked") is None
    assert worker._help_request("t-1", 3, REPLY_BLOCKED_HINT, "blocked") is not None


@pytest.mark.parametrize(
    ("reason", "hint"),
    [
        ("pre_review", PRE_REVIEW_MISSING_HINT),
        ("pre_review", "BLOCKED: the body is not shown"),
        ("revise", "the helper is not shown"),
        ("review_error", "cannot see the file"),
        ("worker_error", "no context"),
        ("gate_tests", "[tests] expected value not shown in the output"),
    ],
)
def test_judgements_of_the_work_are_not_missing_information(reason: str, hint: str) -> None:
    assert not _is_missing_information(reason, hint)


def test_blocked_reply_is_missing_information() -> None:
    assert _is_missing_information("blocked", REPLY_BLOCKED_HINT)


def test_phrases_match_the_hint_only_and_is_missing_is_gone() -> None:
    assert _is_missing_information("claude_error", "the body is not shown")
    assert not _is_missing_information("not shown", "all fine")
    assert not _is_missing_information("claude_error", "a semicolon is missing")


def test_pre_review_send_back_resets_the_count_on_the_real_queue(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    assert _fail_once(rig, BLOCKED_HINT) == "retry"
    assert _fail_once(rig, PRE_REVIEW_MISSING_HINT, reason="pre_review") == "retry"
    assert _fail_once(rig, BLOCKED_HINT) == "retry"

    ticket = _ticket(rig)
    assert ticket.status == "open"
    assert ticket.attempt == 3
    assert rig.queue.help_requests() == []


def test_two_blocked_in_a_row_after_a_pre_review_pause(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    assert _fail_once(rig, PRE_REVIEW_MISSING_HINT, reason="pre_review") == "retry"
    assert _fail_once(rig, BLOCKED_HINT) == "retry"
    assert _fail_once(rig, BLOCKED_HINT) == "awaiting_help"

    assert _ticket(rig).status == "awaiting_help"
    assert len(rig.queue.help_requests()) == 1


def test_gate_failures_mentioning_not_shown_do_not_pause(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    hint = "[tests] expected value not shown in the output"
    assert _fail_once(rig, hint, reason="gate_tests") == "retry"
    assert _fail_once(rig, hint, reason="gate_tests") == "retry"

    ticket = _ticket(rig)
    assert ticket.status == "open"
    assert ticket.attempt == 2
    assert rig.queue.help_requests() == []


def test_blocked_prefix_counts_without_any_phrase() -> None:
    assert _is_missing_information("blocked", "BLOCKED: stuck on the build")
    assert not _is_missing_information("blocked", "the edits changed nothing")
