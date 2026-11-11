"""Tests for foundry/notifier.py: each round is driven directly, with a fake sender and clock."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from foundry.notifier import HELP_DELAY_S, MAX_MESSAGE, PR_DIGEST_WINDOW_S, Notification, Notifier
from foundry.orchestrator_queue import FoundryQueue
from foundry.schemas import BUILDERS_FOR, TaskSizingDirective

ENV = {"FOUNDRY_NTFY_TOPIC": "my-topic", "FOUNDRY_INBOX_URL": "https://inbox.example/"}
START = 1_800_000_000.0


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace("+00:00", "Z")


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> float:
        return self.now


class FakeQueue:
    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.log: list[dict[str, Any]] = []
        self.kv: dict[str, Any] = {}
        self.parked: list[dict[str, Any]] = []
        self.tasks: dict[str, dict[str, Any]] = {}

    def add(self, kind: str, task_id: str | None, payload: dict[str, Any] | None = None) -> None:
        self.log.append({"id": len(self.log) + 1, "ts": iso(self.clock.now), "kind": kind,
                         "task_id": task_id, "payload": payload or {}})

    def events(self, *, since_id: int = 0, kind: str | None = None) -> list[dict[str, Any]]:
        return [e for e in self.log if e["id"] > since_id and (kind is None or e["kind"] == kind)]

    def kv_get(self, key: str, default: Any = None) -> Any:
        return self.kv.get(key, default)

    def kv_set(self, key: str, value: Any) -> None:
        self.kv[key] = value

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        return self.tasks.get(task_id, {"title": f"title of {task_id}", "description": ""})

    def help_requests(self) -> list[dict[str, Any]]:
        return list(self.parked)

    def park(self, task_id: str) -> None:
        """A help request as the queue records it: parked, with its event."""
        self.parked.append({"ticket_id": f"{task_id}#1", "task_id": task_id, "asked_at": iso(self.clock.now),
                            "question": "SECRET model text"})
        self.add("help_requested", task_id, {"ticket_id": f"{task_id}#1", "question": "SECRET model text"})


class FakeLimiter:
    def __init__(self) -> None:
        self.left = 0.0

    def pause_remaining(self) -> float:
        return self.left


class Rig:
    def __init__(self, env: dict[str, str] | None = None, fail: bool = False) -> None:
        self.clock = Clock()
        self.queue = FakeQueue(self.clock)
        self.limiter = FakeLimiter()
        self.service = SimpleNamespace(factory=SimpleNamespace(queue=self.queue, limiter=self.limiter))
        self.sent: list[Notification] = []
        self.fail = fail
        self.env = ENV if env is None else env
        self.notifier = self.fresh()
        self.turn()  # the first round only sets the starting point

    def send(self, note: Notification) -> None:
        if self.fail:
            raise OSError("ntfy is down")
        self.sent.append(note)

    def fresh(self) -> Notifier:
        return Notifier(self.service, sender=self.send, clock=self.clock, env=self.env)

    def turn(self) -> None:
        asyncio.run(self.notifier.round())


@pytest.fixture
def rig() -> Rig:
    return Rig()


# -------------------------------------------------------------- settings and sending

def test_off_without_a_topic() -> None:
    rig = Rig(env={})
    rig.queue.add("quarantined", "F-1", {"reason": "manual"})
    rig.limiter.left = 600.0
    rig.queue.kv_set("last_account_error", {"type": "ClaudeRateLimited"})
    rig.turn()
    assert not rig.notifier.enabled and rig.sent == [] and rig.notifier.rounds == 0
    assert "notifier:last_event" not in rig.queue.kv


def test_start_does_nothing_without_a_topic() -> None:
    async def main() -> None:
        off = Notifier(SimpleNamespace(factory=None), env={})
        assert off.start() is None
        await off.stop()

    asyncio.run(main())


def test_start_and_stop_run_rounds() -> None:
    async def main() -> None:
        on = Notifier(SimpleNamespace(factory=None), env=ENV, sender=lambda note: None, interval=0.001)
        assert on.start() is not None
        for _ in range(100):
            await asyncio.sleep(0.001)
            if on.rounds:
                break
        await on.stop()
        assert on.rounds >= 1

    asyncio.run(main())


def test_post_builds_the_ntfy_request(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    class Response:
        def __enter__(self) -> Response:
            return self

        def __exit__(self, *args: Any) -> None:
            return None

    def urlopen(request: Any, timeout: float) -> Response:
        seen.update(url=request.full_url, data=request.data, method=request.get_method(),
                    headers=dict(request.header_items()), timeout=timeout)
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    env = {**ENV, "FOUNDRY_NTFY_SERVER": "https://push.example/"}
    Notifier(SimpleNamespace(factory=None), env=env)._post(
        Notification("Foundry: paused", "café", "hourglass", "default", "https://inbox.example/"))
    assert seen["url"] == "https://push.example/my-topic"
    assert seen["method"] == "POST" and seen["timeout"] == 10.0
    assert seen["data"] == "café".encode("utf-8")
    assert seen["headers"]["Title"] == "Foundry: paused"
    assert seen["headers"]["Tags"] == "hourglass"
    assert seen["headers"]["Priority"] == "default"
    assert seen["headers"]["Click"] == "https://inbox.example/"


def test_default_server_and_no_click_header(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def urlopen(request: Any, timeout: float) -> Any:
        seen.update(url=request.full_url, headers=dict(request.header_items()))
        raise OSError("stop here")

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    with pytest.raises(OSError):
        Notifier(SimpleNamespace(factory=None), env={"FOUNDRY_NTFY_TOPIC": "t"})._post(
            Notification("a", "b", "c", "default"))
    assert seen["url"] == "https://ntfy.sh/t" and "Click" not in seen["headers"]


def test_a_failing_send_does_not_raise_and_is_dropped() -> None:
    rig = Rig(fail=True)
    rig.queue.add("quarantined", "F-1", {"reason": "manual"})
    rig.turn()
    assert rig.notifier.failed == 1 and rig.notifier.last_error is None
    rig.fail = False
    rig.turn()
    assert rig.sent == []  # dropped, not retried


def test_a_broken_queue_does_not_raise(rig: Rig) -> None:
    def broken(**_kwargs: Any) -> list[dict[str, Any]]:
        raise RuntimeError("database is locked")

    rig.queue.events = broken  # type: ignore[method-assign]
    rig.turn()
    assert rig.notifier.last_error == "RuntimeError: database is locked"


def test_no_factory_yet_is_a_quiet_round() -> None:
    off = Notifier(SimpleNamespace(factory=None), env=ENV, sender=lambda note: None)
    asyncio.run(off.round())
    assert off.last_error is None


# -------------------------------------------------------------- quarantine

def test_quarantine_sends_one_push(rig: Rig) -> None:
    rig.queue.add("quarantined", "F-1", {"reason": "retry limit reached (revise): def secret(): pass"})
    rig.turn()
    assert len(rig.sent) == 1
    note = rig.sent[0]
    assert note.title == "Foundry: piece quarantined" and note.click == ENV["FOUNDRY_INBOX_URL"]
    assert note.message == "F-1 title of F-1\nQuarantined. Used up its build attempts."


@pytest.mark.parametrize("reason, sentence", [
    ("infrastructure: boom", "Repeated infrastructure failures."),
    ("Reviewer rejected: bad code", "The reviewer rejected it."),
    ("no Floor Manager answer within 24 h: what is x?", "No answer to its help request in time."),
    ("sizing failed: TimeoutError", "Sizing failed."),
    ("something the model wrote", "See the dashboard for the reason."),
    (None, "See the dashboard for the reason."),
])
def test_quarantine_reason_is_a_fixed_sentence(rig: Rig, reason: str | None, sentence: str) -> None:
    rig.queue.add("quarantined", "F-1", {"reason": reason})
    rig.turn()
    assert rig.sent[0].message.endswith(sentence)
    assert "boom" not in rig.sent[0].message and "model wrote" not in rig.sent[0].message


def test_the_doubled_quarantine_event_sends_once(rig: Rig) -> None:
    rig.queue.add("quarantined", "F-1", {"reason": "infrastructure: x"})  # from the queue
    rig.queue.add("quarantined", "F-1", {"reason": "infrastructure"})  # from the worker
    rig.turn()
    rig.queue.add("quarantined", "F-1", {"reason": "manual"})  # requeued and quarantined again
    rig.turn()
    assert len(rig.sent) == 1


def test_message_is_capped(rig: Rig) -> None:
    rig.queue.tasks["F-" + "9" * 400] = {"title": "t" * 500, "description": ""}
    rig.queue.add("quarantined", "F-" + "9" * 400, {"reason": "manual"})
    rig.turn()
    assert len(rig.sent[0].message) == MAX_MESSAGE


# -------------------------------------------------------------- help requests

def test_help_waits_ten_minutes(rig: Rig) -> None:
    rig.queue.park("F-2")
    rig.turn()
    rig.clock.now += HELP_DELAY_S - 1
    rig.turn()
    assert rig.sent == []
    rig.clock.now += 1
    rig.turn()
    assert len(rig.sent) == 1
    assert rig.sent[0].title == "Foundry: help needed"
    assert rig.sent[0].message == "F-2 title of F-2\nHelp request unanswered for 10 minutes."
    assert "SECRET" not in rig.sent[0].message
    rig.clock.now += HELP_DELAY_S
    rig.turn()
    assert len(rig.sent) == 1


def test_answered_help_is_not_sent(rig: Rig) -> None:
    rig.queue.park("F-2")
    rig.turn()
    rig.queue.parked.clear()  # the help desk answered
    rig.clock.now += HELP_DELAY_S
    rig.turn()
    assert rig.sent == [] and rig.queue.kv["notifier:pending"]["help"] == {}


def test_help_asked_again_restarts_the_delay(rig: Rig) -> None:
    rig.queue.park("F-2")
    rig.turn()
    rig.queue.parked.clear()
    rig.clock.now += HELP_DELAY_S - 5
    rig.queue.park("F-2")
    rig.clock.now += 5
    rig.turn()
    assert rig.sent == []
    rig.clock.now += HELP_DELAY_S
    rig.turn()
    assert len(rig.sent) == 1


def test_help_is_at_most_one_per_piece(rig: Rig) -> None:
    rig.queue.park("F-2")
    rig.clock.now += HELP_DELAY_S
    rig.turn()
    rig.queue.parked.clear()
    rig.queue.park("F-2")
    rig.clock.now += HELP_DELAY_S
    rig.turn()
    assert len(rig.sent) == 1


def test_pending_help_survives_a_restart(rig: Rig) -> None:
    rig.queue.park("F-2")
    rig.turn()
    rig.notifier = rig.fresh()
    rig.clock.now += HELP_DELAY_S
    rig.turn()
    assert len(rig.sent) == 1


# -------------------------------------------------------------- PR digests

def batch_task(rig: Rig, task_id: str, batch: str = "S-7") -> None:
    rig.queue.tasks[task_id] = {"title": task_id, "description": (
        f"Do it.\n\nPart of \"x\"\n(Foundry batch {batch} of 2026-01-01T00:00:00.000Z, subtask 1)")}


def pr(rig: Rig, task_id: str) -> None:
    rig.queue.add("pr_opened", task_id, {"state": "opened", "url": f"https://gh.example/pull/{task_id}"})


def test_pr_sends_a_digest_listing_it(rig: Rig) -> None:
    batch_task(rig, "F-3")
    pr(rig, "F-3")
    rig.turn()
    assert len(rig.sent) == 1
    assert rig.sent[0].message == "S-7: 1 PR opened\nF-3 https://gh.example/pull/F-3"


def test_pr_digest_window(rig: Rig) -> None:
    for task_id in ("F-3", "F-4", "F-5", "F-6"):
        batch_task(rig, task_id)
    pr(rig, "F-3")
    pr(rig, "F-4")
    rig.turn()
    assert len(rig.sent) == 1 and "F-3" in rig.sent[0].message and "F-4" in rig.sent[0].message
    pr(rig, "F-5")
    rig.clock.now += 60
    rig.turn()
    pr(rig, "F-6")
    rig.clock.now += PR_DIGEST_WINDOW_S - 61
    rig.turn()
    assert len(rig.sent) == 1  # held inside the window
    rig.clock.now += 1
    rig.turn()
    assert len(rig.sent) == 2
    assert rig.sent[1].message.startswith("S-7: 2 PRs opened")
    assert "F-5" in rig.sent[1].message and "F-6" in rig.sent[1].message
    rig.clock.now += PR_DIGEST_WINDOW_S
    rig.turn()
    assert len(rig.sent) == 2


def test_each_batch_has_its_own_window(rig: Rig) -> None:
    batch_task(rig, "F-3", "S-7")
    batch_task(rig, "F-4", "S-8")
    pr(rig, "F-3")
    pr(rig, "F-4")
    rig.turn()
    assert sorted(n.message.split(":")[0] for n in rig.sent) == ["S-7", "S-8"]


def test_task_outside_a_batch_is_its_own_digest(rig: Rig) -> None:
    pr(rig, "F-3")
    rig.turn()
    assert rig.sent[0].message.startswith("F-3: 1 PR opened")


def test_a_republished_piece_is_listed_once(rig: Rig) -> None:
    batch_task(rig, "F-3")
    pr(rig, "F-3")
    pr(rig, "F-3")
    rig.turn()
    pr(rig, "F-3")
    rig.clock.now += PR_DIGEST_WINDOW_S
    rig.turn()
    assert len(rig.sent) == 1 and rig.sent[0].message.count("F-3 https") == 1


def test_a_long_digest_stays_under_the_limit(rig: Rig) -> None:
    ids = [f"F-{n}" for n in range(30)]
    for task_id in ids:
        batch_task(rig, task_id)
        pr(rig, task_id)
    rig.turn()
    message = rig.sent[0].message
    assert len(message) <= MAX_MESSAGE
    listed = message.count("https://")
    assert 0 < listed < 30 and message.endswith(f"+{30 - listed} more")


def test_held_prs_survive_a_restart(rig: Rig) -> None:
    batch_task(rig, "F-3")
    batch_task(rig, "F-4")
    pr(rig, "F-3")
    rig.turn()
    pr(rig, "F-4")
    rig.turn()
    rig.notifier = rig.fresh()
    rig.clock.now += PR_DIGEST_WINDOW_S
    rig.turn()
    assert len(rig.sent) == 2 and "F-4" in rig.sent[1].message


# -------------------------------------------------------------- usage pauses

def pause(rig: Rig, kind: str = "ClaudeRateLimited", seconds: float = 3000.0) -> None:
    rig.queue.kv_set("last_account_error", {"type": kind, "message": "SECRET", "at": "x"})
    rig.limiter.left = seconds


def test_usage_pause_sends_one_push_per_pause(rig: Rig) -> None:
    pause(rig)
    rig.turn()
    rig.limiter.left = 2900.0  # another worker's limit extends the same pause
    rig.turn()
    assert len(rig.sent) == 1
    assert rig.sent[0].message == "Foundry paused for a usage limit. Resumes in about 50 min."
    rig.limiter.left = 0.0
    rig.turn()
    pause(rig)
    rig.turn()
    assert len(rig.sent) == 2


def test_a_pause_for_another_reason_is_not_a_usage_pause(rig: Rig) -> None:
    pause(rig, kind="ClaudeAuthError")
    rig.turn()
    assert rig.sent == []


def test_usage_pause_is_not_repeated_after_a_restart(rig: Rig) -> None:
    pause(rig)
    rig.turn()
    rig.notifier = rig.fresh()
    rig.turn()
    assert len(rig.sent) == 1


# -------------------------------------------------------------- following events

def test_first_round_does_not_replay_history() -> None:
    rig = Rig()
    rig.queue.add("quarantined", "F-old", {"reason": "manual"})
    rig.queue.kv.clear()  # as on the very first start, with old events already there
    rig.notifier = rig.fresh()
    rig.turn()
    assert rig.sent == []
    rig.queue.add("quarantined", "F-new", {"reason": "manual"})
    rig.turn()
    assert [n.message.split()[0] for n in rig.sent] == ["F-new"]


def test_restart_neither_repeats_nor_misses(rig: Rig) -> None:
    rig.queue.add("quarantined", "F-1", {"reason": "manual"})
    rig.turn()
    rig.notifier = rig.fresh()
    rig.queue.add("quarantined", "F-2", {"reason": "manual"})  # while it was down
    rig.turn()
    assert [n.message.split()[0] for n in rig.sent] == ["F-1", "F-2"]


def test_cursor_is_stored_under_the_service_key(rig: Rig) -> None:
    rig.queue.add("quarantined", "F-1", {"reason": "manual"})
    rig.turn()
    assert rig.queue.kv["notifier:last_event"]["quarantined"] == 1


def test_works_on_the_real_queue(tmp_path: Path) -> None:
    queue = FoundryQueue(tmp_path / "queue.db")
    try:
        clock = Clock()
        clock.now = datetime.now(timezone.utc).timestamp()
        sent: list[Notification] = []
        service = SimpleNamespace(factory=SimpleNamespace(queue=queue, limiter=FakeLimiter()))
        notifier = Notifier(service, sender=sent.append, clock=clock, env=ENV)
        asyncio.run(notifier.round())
        queue.add_task("T-1", "A title", "desc", "alpha")
        queue.apply_sizing(TaskSizingDirective.model_validate({
            "task_id": "T-1", "team_id": "alpha", "complexity": "M", "allocated_builders": BUILDERS_FOR["M"],
            "rationale": "test",
            "subtasks": [{"subtask_id": f"T-1.{i}", "title": f"part {i}", "acceptance_criteria": [f"c{i}"],
                          "allowed_files": [f"f{i}.py"], "independent": True} for i in range(BUILDERS_FOR["M"])],
        }))
        ticket = queue.tickets_for_task("T-1")[0]
        assert queue.pause_for_help(ticket.ticket_id, {"question": "SECRET"})
        asyncio.run(notifier.round())
        assert sent == [] and notifier.last_error is None
        clock.now += HELP_DELAY_S + 1
        asyncio.run(notifier.round())
        assert [n.message for n in sent] == ["T-1 A title\nHelp request unanswered for 10 minutes."]
        queue.quarantine("T-1", "retry limit reached: SECRET")
        asyncio.run(notifier.round())
        assert sent[-1].message == "T-1 A title\nQuarantined. Used up its build attempts."
        assert notifier.last_error is None
    finally:
        queue.close()
