"""Shop Foreman prescribing REASSIGN_TEAM and RETRY_TASK (simulated Gemma only)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from foundry.config import FoundryConfig
from foundry.gemma_client import SimulatedGemmaClient
from foundry.schemas import EscalationCategory, ForemanEscalation, PrescribedAction
from foundry.shop_foreman import ShopForeman, classify_quarantine
from foundry.tests.test_shop_foreman import fail_gate, line, run, sized_task  # noqa: F401 - `line` is a fixture

INFRA = "infrastructure: claude CLI timed out after 600 s"


def foreman_for(line: dict[str, Any], gemma: Any = None, builders: int = 1, **kwargs: Any) -> ShopForeman:  # noqa: F811
    config = FoundryConfig(db_path=line["config"].db_path, builders_per_team=builders)
    return ShopForeman(gemma, line["queue"], line["telemetry"], line["engine"], config, **kwargs)


def stuck_line(line: dict[str, Any]) -> str:  # noqa: F811
    """Team alpha (1 builder) busy on F-101 while F-102's ticket waits > stall_s; bravo/charlie idle."""
    queue = line["queue"]
    sized_task(queue, "F-101", "alpha", 1)
    busy = queue.claim_ticket("alpha", 1)
    assert busy is not None and busy.task_id == "F-101"
    sized_task(queue, "F-102", "alpha", 1)
    line["clock"].advance(901)
    queue.record_event("build_progress", "F-101", {"ticket_id": busy.ticket_id})  # the busy builder is working
    waiting = [t for t in queue.tickets_for_task("F-102") if t.status == "open"]
    assert len(waiting) == 1
    return waiting[0].ticket_id


def quarantined(line: dict[str, Any], task_id: str, reason: str, *, after: float = 120) -> None:  # noqa: F811
    sized_task(line["queue"], task_id, "bravo", 1)
    line["queue"].quarantine(task_id, reason)
    line["clock"].advance(after)


# --------------------------------------------------------------------------- classification


@pytest.mark.parametrize(
    ("reason", "kind"),
    [
        (INFRA, "transient"),
        ("sizing failed: TimeoutError: Ollama connection reset", "transient"),
        ("HTTP 429 rate limit from the API", "transient"),
        ("retry limit reached on F-1.1: tests fail", "real"),
        ("retry limit reached (timeout): x", "real"),  # attempts used up: a real failure wins
        ("ticket F-1:F-1.1 failed its gates 3 times (limit 2)", "real"),
        ("Reviewer rejected: wrong approach", "real"),
        ("duplicate of F-009 (resent after the old 30 s timeout)", "real"),
        ("something odd happened", "unclear"),
        ("", "unclear"),
    ],
)
def test_classify_quarantine(reason: str, kind: str) -> None:
    assert classify_quarantine(reason) == kind


# --------------------------------------------------------------------------- REASSIGN_TEAM


def test_stuck_task_on_busy_team_is_reassigned_to_an_idle_team(line: dict[str, Any]) -> None:  # noqa: F811
    ticket_id = stuck_line(line)
    alert = run(foreman_for(line).evaluate_line_anomaly())
    assert isinstance(alert, ForemanEscalation)
    assert alert.prescribed_action == PrescribedAction.REASSIGN_TEAM
    assert alert.category == EscalationCategory.CAPACITY_REBALANCE
    assert alert.target_entity.task_id == "F-102" and alert.target_entity.team_id == "alpha"
    params = alert.action_parameters
    assert params["from_team"] == "alpha" and params["to_team"] == "bravo"
    assert params["ticket_ids"] == [ticket_id] and params["waited_s"] >= 900
    assert "team bravo" in alert.diagnosis
    ForemanEscalation.model_validate(alert.model_dump(mode="json"))


def test_no_reassign_when_no_team_is_idle(line: dict[str, Any]) -> None:  # noqa: F811
    queue = line["queue"]
    for team, tid in (("bravo", "F-110"), ("charlie", "F-111")):
        sized_task(queue, tid, team, 1)
    stuck_line(line)
    for team in ("bravo", "charlie"):  # keep the other teams busy and active
        t = queue.claim_ticket(team, 1)
        queue.record_event("build_progress", t.task_id, {"ticket_id": t.ticket_id})
    actions = [a.action for a in foreman_for(line).detect_anomalies()]
    assert PrescribedAction.REASSIGN_TEAM not in actions


def test_no_reassign_before_the_stall_threshold(line: dict[str, Any]) -> None:  # noqa: F811
    queue = line["queue"]
    sized_task(queue, "F-120", "alpha", 1)
    queue.claim_ticket("alpha", 1)
    sized_task(queue, "F-121", "alpha", 1)
    line["clock"].advance(100)
    assert PrescribedAction.REASSIGN_TEAM not in [a.action for a in foreman_for(line).detect_anomalies()]


def test_idle_team_reported_with_zero_idle_builders_is_not_a_target(line: dict[str, Any]) -> None:  # noqa: F811
    stuck_line(line)
    line["telemetry"].set_idle("bravo", 0)
    alert = run(foreman_for(line).evaluate_line_anomaly())
    assert alert is not None and alert.action_parameters["to_team"] == "charlie"


def test_gemma_can_choose_reassign_and_write_the_diagnosis(line: dict[str, Any]) -> None:  # noqa: F811
    stuck_line(line)
    gemma = SimulatedGemmaClient(lambda req: {"prescribed_action": "REASSIGN_TEAM",
                                              "diagnosis": "Alpha is full. Bravo can take it."})
    foreman = foreman_for(line, gemma)
    alert = run(foreman.evaluate_line_anomaly())
    assert alert.prescribed_action == PrescribedAction.REASSIGN_TEAM
    assert alert.diagnosis == "Alpha is full. Bravo can take it."
    assert foreman.last_source["evaluate_line_anomaly"] == "gemma"
    schema = gemma.requests[0].json_schema
    assert schema["properties"]["prescribed_action"]["enum"] == ["REASSIGN_TEAM", "NONE"]


def test_gemma_none_skips_the_reassign(line: dict[str, Any]) -> None:  # noqa: F811
    stuck_line(line)
    gemma = SimulatedGemmaClient(lambda req: {"prescribed_action": "NONE", "diagnosis": "Wait for alpha."})
    foreman = foreman_for(line, gemma)
    assert run(foreman.evaluate_line_anomaly()) is None
    assert run(foreman.evaluate_line_anomaly()) is None  # cooldown: not asked again right away
    assert gemma.calls == 1


@pytest.mark.parametrize("reply", ["not json at all", json.dumps({"prescribed_action": "QUARANTINE_TASK",
                                                                   "diagnosis": "x"})])
def test_invalid_gemma_choice_keeps_the_deterministic_reassign(line: dict[str, Any], reply: str) -> None:  # noqa: F811
    stuck_line(line)
    alert = run(foreman_for(line, SimulatedGemmaClient(lambda req: reply)).evaluate_line_anomaly())
    assert alert.prescribed_action == PrescribedAction.REASSIGN_TEAM  # never an action outside the choices


def test_gemma_outage_falls_back_to_the_deterministic_reassign(line: dict[str, Any]) -> None:  # noqa: F811
    stuck_line(line)
    foreman = foreman_for(line, SimulatedGemmaClient(unavailable=True))
    alert = run(foreman.evaluate_line_anomaly())
    assert alert.prescribed_action == PrescribedAction.REASSIGN_TEAM
    assert foreman.last_source["evaluate_line_anomaly"] == "heuristic"


# --------------------------------------------------------------------------- RETRY_TASK


def test_infrastructure_quarantine_is_retried_once(line: dict[str, Any]) -> None:  # noqa: F811
    quarantined(line, "F-130", INFRA)
    foreman = foreman_for(line)
    alert = run(foreman.evaluate_line_anomaly())
    assert alert is not None and alert.prescribed_action == PrescribedAction.RETRY_TASK
    assert alert.target_entity.task_id == "F-130"
    assert alert.action_parameters["reason"] == INFRA
    assert alert.action_parameters["transient"] is True and alert.action_parameters["source"] == "heuristic"
    line["clock"].advance(10_000 - 120)  # past the cooldown, still within the retry window
    assert run(foreman.evaluate_line_anomaly()) is None  # never twice


def test_retry_is_not_repeated_after_a_restart(line: dict[str, Any]) -> None:  # noqa: F811
    quarantined(line, "F-131", INFRA)
    alert = run(foreman_for(line).evaluate_line_anomaly())
    line["queue"].record_escalation(alert, applied=True)
    line["clock"].advance(400)
    assert run(foreman_for(line).evaluate_line_anomaly()) is None


def test_retry_is_not_repeated_when_the_action_log_has_one(line: dict[str, Any]) -> None:  # noqa: F811
    quarantined(line, "F-132", INFRA)
    log = [{"action": "RETRY_TASK", "task_id": "F-132", "undone_at": None}]
    foreman = foreman_for(line, actions_provider=lambda limit=50: log)
    assert run(foreman.evaluate_line_anomaly()) is None


@pytest.mark.parametrize("reason", [
    "retry limit reached on F-140.1: tests fail",
    "ticket F-140:F-140.1 failed its gates 3 times (limit 2)",
    "Reviewer rejected: wrong approach",
])
def test_real_build_failures_are_never_retried(line: dict[str, Any], reason: str) -> None:  # noqa: F811
    quarantined(line, "F-140", reason)
    gemma = SimulatedGemmaClient(lambda req: {"prescribed_action": "RETRY_TASK", "diagnosis": "Retry."})
    foreman = foreman_for(line, gemma)
    assert run(foreman.evaluate_line_anomaly()) is None
    assert gemma.calls == 0  # not even offered to Gemma


def test_retry_waits_a_moment_and_ignores_old_quarantines(line: dict[str, Any]) -> None:  # noqa: F811
    quarantined(line, "F-141", INFRA, after=10)
    foreman = foreman_for(line)
    assert run(foreman.evaluate_line_anomaly()) is None  # too fresh
    line["clock"].advance(7 * 3600)
    assert run(foreman.evaluate_line_anomaly()) is None  # too old


def test_unclear_quarantine_needs_gemma(line: dict[str, Any]) -> None:  # noqa: F811
    quarantined(line, "F-142", "something odd happened")
    assert run(foreman_for(line).evaluate_line_anomaly()) is None  # no Gemma: left alone
    gemma = SimulatedGemmaClient(lambda req: {"prescribed_action": "RETRY_TASK",
                                              "diagnosis": "The builder process vanished. Retry once."})
    foreman = foreman_for(line, gemma)
    alert = run(foreman.evaluate_line_anomaly())
    assert alert.prescribed_action == PrescribedAction.RETRY_TASK
    assert alert.action_parameters["source"] == "gemma"
    assert alert.diagnosis == "The builder process vanished. Retry once."
    line["clock"].advance(10_000 - 120)
    assert run(foreman.evaluate_line_anomaly()) is None  # never twice, even when Gemma asks


def test_gemma_declines_an_unclear_retry_once_and_is_not_asked_again(line: dict[str, Any]) -> None:  # noqa: F811
    quarantined(line, "F-143", "something odd happened")
    gemma = SimulatedGemmaClient(lambda req: {"prescribed_action": "NONE", "diagnosis": "Looks like a real bug."})
    foreman = foreman_for(line, gemma)
    assert run(foreman.evaluate_line_anomaly()) is None
    line["clock"].advance(400)
    assert run(foreman.evaluate_line_anomaly()) is None
    assert gemma.calls == 1


def test_gemma_outage_on_unclear_retry_does_nothing_but_asks_later(line: dict[str, Any]) -> None:  # noqa: F811
    quarantined(line, "F-144", "something odd happened")
    gemma = SimulatedGemmaClient(unavailable=True)
    foreman = foreman_for(line, gemma)
    assert run(foreman.evaluate_line_anomaly()) is None
    gemma.unavailable = False
    gemma.responder = lambda req: {"prescribed_action": "RETRY_TASK", "diagnosis": "Retry it."}
    line["clock"].advance(400)
    alert = run(foreman.evaluate_line_anomaly())
    assert alert is not None and alert.prescribed_action == PrescribedAction.RETRY_TASK


def test_gemma_may_decline_a_transient_retry(line: dict[str, Any]) -> None:  # noqa: F811
    quarantined(line, "F-145", INFRA)
    gemma = SimulatedGemmaClient(lambda req: {"prescribed_action": "NONE", "diagnosis": "Leave it."})
    assert run(foreman_for(line, gemma).evaluate_line_anomaly()) is None


def test_gemma_plain_text_reply_keeps_the_retry_with_its_diagnosis(line: dict[str, Any]) -> None:  # noqa: F811
    quarantined(line, "F-146", INFRA)
    gemma = SimulatedGemmaClient(lambda req: "The CLI timed out. Retry once.")
    alert = run(foreman_for(line, gemma).evaluate_line_anomaly())
    assert alert.prescribed_action == PrescribedAction.RETRY_TASK
    assert alert.diagnosis == "The CLI timed out. Retry once."


# --------------------------------------------------------------------------- respecting undo (review round 1)


def _quarantine_and_undo(line: dict[str, Any], task_id: str, note: str = "quarantine undone") -> None:  # noqa: F811
    queue = line["queue"]
    blocked = {t.ticket_id: {} for t in queue.tickets_for_task(task_id)}
    queue.quarantine(task_id, "failed its gates")
    queue.reopen_quarantined(task_id, blocked, note)


def test_undone_quarantine_is_not_represcribed_without_new_evidence(line: dict[str, Any]) -> None:  # noqa: F811
    sized_task(line["queue"], "F-150", "alpha", 1)
    fail_gate(line, "F-150")
    fail_gate(line, "F-150")
    foreman = foreman_for(line)
    first = run(foreman.evaluate_line_anomaly())
    assert first.prescribed_action == PrescribedAction.QUARANTINE_TASK
    _quarantine_and_undo(line, "F-150")
    line["clock"].advance(301)  # past the cooldown
    assert run(foreman.evaluate_line_anomaly()) is None
    assert run(foreman_for(line).evaluate_line_anomaly()) is None  # a fresh Foreman (restart) too
    fail_gate(line, "F-150")  # new evidence: it fails again on its last attempt
    alert = run(foreman_for(line).evaluate_line_anomaly())
    assert alert is not None and alert.prescribed_action == PrescribedAction.QUARANTINE_TASK
    assert alert.target_entity.task_id == "F-150"


def test_after_a_retry_only_new_gate_failures_count(line: dict[str, Any]) -> None:  # noqa: F811
    sized_task(line["queue"], "F-151", "alpha", 1)
    fail_gate(line, "F-151")
    fail_gate(line, "F-151")
    _quarantine_and_undo(line, "F-151", note="retry (Foreman)")
    line["clock"].advance(10)
    fail_gate(line, "F-151")
    assert run(foreman_for(line).evaluate_line_anomaly()) is None  # 1 fresh failure < limit 2
    fail_gate(line, "F-151")
    alert = run(foreman_for(line).evaluate_line_anomaly())
    assert alert.prescribed_action == PrescribedAction.QUARANTINE_TASK


def _undone_log(action: str, task_id: str | None, undone_at: str) -> list[dict[str, Any]]:
    return [{"action": action, "task_id": task_id, "undone_at": undone_at, "applied": True}]


def test_undone_reassign_is_not_represcribed_within_two_hours(line: dict[str, Any]) -> None:  # noqa: F811
    stuck_line(line)  # fake clock is now 2026-01-15T12:15:01Z
    log = _undone_log("REASSIGN_TEAM", "F-102", "2026-01-15T12:15:00Z")
    foreman = foreman_for(line, actions_provider=lambda limit=50: log)
    assert run(foreman.evaluate_line_anomaly()) is None
    line["clock"].advance(2 * 3600 + 5)
    line["queue"].record_event("build_progress", "F-101",
                               {"ticket_id": line["queue"].tickets_for_task("F-101")[0].ticket_id})
    alert = run(foreman.evaluate_line_anomaly())
    assert alert is not None and alert.prescribed_action == PrescribedAction.REASSIGN_TEAM


def test_undone_reassign_read_from_the_stored_action_log(line: dict[str, Any]) -> None:  # noqa: F811
    stuck_line(line)
    line["queue"].kv_set("floor_manager.actions",
                         {"seq": 1, "actions": _undone_log("REASSIGN_TEAM", "F-102", "2026-01-15T12:15:00Z")})
    assert run(foreman_for(line).evaluate_line_anomaly()) is None


def test_undone_resize_is_suppressed(line: dict[str, Any]) -> None:  # noqa: F811
    queue = line["queue"]
    sized_task(queue, "F-152", "alpha", 2)
    queue.resize("F-152", 1)
    line["telemetry"].set_idle("alpha", 4)
    log = _undone_log("RESIZE_ALLOCATION", "F-152", "2026-01-15T11:59:00Z")
    assert run(foreman_for(line, builders=5, actions_provider=lambda limit=50: log).evaluate_line_anomaly()) is None
    other = _undone_log("RESIZE_ALLOCATION", "F-999", "2026-01-15T11:59:00Z")  # another task's undo
    alert = run(foreman_for(line, builders=5, actions_provider=lambda limit=50: other).evaluate_line_anomaly())
    assert alert is not None and alert.prescribed_action == PrescribedAction.RESIZE_ALLOCATION


def test_quarantine_without_blocked_tickets_is_not_retried(line: dict[str, Any]) -> None:  # noqa: F811
    line["queue"].add_task("F-153", "Task", "desc", "bravo")  # never sized: no tickets
    line["queue"].quarantine("F-153", "sizing failed: TimeoutError: timed out")
    line["clock"].advance(120)
    assert run(foreman_for(line).evaluate_line_anomaly()) is None
