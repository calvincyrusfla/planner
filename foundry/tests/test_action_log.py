"""The Floor Manager's persisted action log, undo per action kind, REASSIGN_TEAM and RETRY_TASK.

Real queue/manager/pools wired by ``build_factory`` with the simulated Claude
client and a dry board bridge; nothing calls Claude, Ollama or the board.
"""

from __future__ import annotations

import asyncio
import itertools
from pathlib import Path
from typing import Any

from foundry.board_bridge import BoardBridge
from foundry.claude_client import SimulatedClaudeClient
from foundry.config import FoundryConfig
from foundry.floor_manager import Factory, FloorManager, WorkOrder, build_factory
from foundry.main import write_demo_repo
from foundry.schemas import ForemanEscalation

_SEQ = itertools.count(1)


def _factory(tmp_path: Path, teams: list[str] | None = None) -> Factory:
    repo = tmp_path / "repo"
    if not repo.exists():
        write_demo_repo(repo)
    config = FoundryConfig(
        db_path=tmp_path / "foundry.db", rpm=1000, tpm=10_000_000, max_concurrent=5,
        teams=teams or ["alpha", "bravo"], board_bridge_mode="dry", auto_pr=False,
    )
    return build_factory(
        config, claude=SimulatedClaudeClient(), gemma=None, repo_root=repo, workspace=repo,
        bridge=BoardBridge("dry"), patches_dir=tmp_path / "patches",
    )


def _alert(action: str, *, category: str = "LINE_JAM", params: dict[str, Any] | None = None, **target: str) -> ForemanEscalation:
    return ForemanEscalation(
        alert_id=f"ALT-20261108-{next(_SEQ):03d}",
        timestamp="2026-11-08T12:00:00Z",
        severity="HIGH",
        category=category,
        target_entity=target,
        metrics_snapshot={"queue_depth": 1, "dwell_seconds": 0.0, "gate_failure_count": 0, "current_tpm": 0.0},
        diagnosis="Test alert.",
        prescribed_action=action,
        action_parameters=params or {},
    )


def _order(title: str, files: list[str] | None = None, team: str | None = "alpha") -> WorkOrder:
    return WorkOrder(title=title, description="d", files=files or ["shop/pricing.py"], team_id=team)


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_quarantine_is_logged_and_undo_restores_tickets_and_attempts(tmp_path: Path) -> None:
    f = _factory(tmp_path)
    m, q = f.manager, f.queue

    async def go() -> str:
        tid = await m.submit(_order("q me"))
        ticket = q.tickets_for_task(tid)[0]
        assert q.reopen_ticket(ticket.ticket_id, "try again")  # attempt 1
        out = await m.apply_escalation(_alert("QUARANTINE_TASK", task_id=tid, params={"reason": "jam"}))
        assert out.applied and q.get_task(tid)["state"] == "quarantined"
        return tid

    tid = _run(go())
    [entry] = m.actions()
    assert entry["action"] == "QUARANTINE_TASK" and entry["task_id"] == tid and entry["undoable"]
    assert entry["action_id"] == "A-0001" and entry["at"].endswith("Z") and entry["undone_at"] is None
    assert q.sizing_outcomes(include_excluded=True)  # the quarantine recorded an outcome

    res = m.undo_action(entry["action_id"], by="tester")
    assert res["ok"], res
    task = q.get_task(tid)
    assert task["state"] == "building" and task["quarantine_reason"] is None
    [t] = q.tickets_for_task(tid)
    assert t.status == "open" and t.attempt == 1  # attempts as before
    assert not q.sizing_outcomes(include_excluded=True)  # the quarantine outcome is forgotten
    assert m.actions()[0]["undone_by"] == "tester" and m.actions()[0]["undone_at"]
    # idempotent
    again = m.undo_action(entry["action_id"])
    assert again["ok"] and again["already_undone"]
    assert q.get_task(tid)["state"] == "building"
    f.close()


def test_undo_quarantine_refuses_when_state_moved_on(tmp_path: Path) -> None:
    f = _factory(tmp_path)
    m, q = f.manager, f.queue

    async def go() -> str:
        tid = await m.submit(_order("q me"))
        await m.apply_escalation(_alert("QUARANTINE_TASK", task_id=tid))
        return tid

    tid = _run(go())
    aid = m.actions()[0]["action_id"]
    # someone else lifted it (e.g. a RETRY) -> state moved on
    q.reopen_quarantined(tid, {t.ticket_id: {} for t in q.tickets_for_task(tid)})
    res = m.undo_action(aid)
    assert res["ok"] is False and "not quarantined" in res["reason"]
    assert m.actions()[0]["undone_at"] is None
    assert m.undo_action("A-9999")["ok"] is False
    f.close()


def test_resize_throttle_reset_and_none(tmp_path: Path) -> None:
    f = _factory(tmp_path)
    m, q = f.manager, f.queue

    async def go() -> str:
        tid = await m.submit(_order("resize me", ["shop/strings.py", "shop/slug.py"]))
        await m.apply_escalation(_alert("RESIZE_ALLOCATION", category="CAPACITY_REBALANCE", task_id=tid, params={"builders": 4}))
        await m.apply_escalation(_alert("THROTTLE_DISPATCH", category="TOKEN_VELOCITY_SPIKE", team_id="alpha", params={"factor": 0.5, "seconds": 60}))
        await m.apply_escalation(_alert("RESET_BUILDER_CONTEXT", category="AGENT_STALLED", task_id=tid, team_id="alpha", agent_id="alpha-1"))
        await m.apply_escalation(_alert("NONE", team_id="alpha"))
        return tid

    tid = _run(go())
    before = q.get_task(tid)["allocated_builders"]
    log_ = m.actions()
    assert [e["action"] for e in log_] == ["RESET_BUILDER_CONTEXT", "THROTTLE_DISPATCH", "RESIZE_ALLOCATION"]  # NONE not logged
    reset, throttle, resize = log_
    assert reset["undoable"] is False and reset["undo_reason"]
    assert m.undo_action(reset["action_id"])["ok"] is False

    assert before == 4
    res = m.undo_action(resize["action_id"])
    assert res["ok"], res
    assert q.get_task(tid)["allocated_builders"] == resize["before"]["builders"] != 4
    assert m.allocations[tid] == resize["before"]["builders"]

    assert f.limiter.is_throttled
    assert m.undo_action(throttle["action_id"])["ok"]
    assert not f.limiter.is_throttled
    assert m.actions(limit=1)[0]["action_id"] == reset["action_id"]
    f.close()


def test_undo_resize_refuses_after_a_newer_resize(tmp_path: Path) -> None:
    f = _factory(tmp_path)
    m, q = f.manager, f.queue

    async def go() -> str:
        tid = await m.submit(_order("resize me", ["shop/strings.py", "shop/slug.py"]))
        await m.apply_escalation(_alert("RESIZE_ALLOCATION", task_id=tid, params={"builders": 4}))
        await m.apply_escalation(_alert("RESIZE_ALLOCATION", task_id=tid, params={"builders": 3}))
        return tid

    tid = _run(go())
    newer, older = m.actions()
    assert m.undo_action(older["action_id"])["ok"] is False
    assert m.undo_action(newer["action_id"])["ok"]
    assert q.get_task(tid)["allocated_builders"] == 4
    f.close()


def test_throttle_undo_refuses_when_replaced(tmp_path: Path) -> None:
    f = _factory(tmp_path)
    m = f.manager

    async def go() -> None:
        await m.apply_escalation(_alert("THROTTLE_DISPATCH", team_id="alpha", params={"factor": 0.5, "seconds": 60}))
        await m.apply_escalation(_alert("THROTTLE_DISPATCH", team_id="alpha", params={"factor": 0.25, "seconds": 60}))

    _run(go())
    newer, older = m.actions()
    assert m.undo_action(older["action_id"])["ok"] is False
    assert m.undo_action(newer["action_id"])["ok"]
    f.close()


def test_promote_peer_reviewer_undo_demotes(tmp_path: Path) -> None:
    f = _factory(tmp_path, teams=["alpha"])
    m = f.manager
    pool = f.pools["alpha"]

    async def go() -> dict[str, Any]:
        async with f.running():
            out = await m.apply_escalation(
                _alert("PROMOTE_PEER_REVIEWER", category="REVIEW_BOTTLENECK", team_id="alpha", params={"minutes": 5, "slot": 5})
            )
            assert out.applied and "alpha-5" in pool.peer_reviewers
            res = m.undo_action(m.actions()[0]["action_id"])
            for _ in range(50):
                if "alpha-5" not in pool.peer_reviewers:
                    break
                await asyncio.sleep(0.02)
            assert "alpha-5" not in pool.peer_reviewers
            assert m.peer_reviewer_for("alpha") is None
            return res

    res = _run(go())
    assert res["ok"], res
    f.close()


def test_reassign_team_moves_open_tickets_and_undo_moves_back(tmp_path: Path) -> None:
    f = _factory(tmp_path)
    m, q = f.manager, f.queue

    async def go() -> str:
        tid = await m.submit(_order("stuck", ["shop/strings.py", "shop/slug.py"]))
        out = await m.apply_escalation(_alert("REASSIGN_TEAM", task_id=tid, team_id="alpha", params={"reason": "stuck 14 min"}))
        assert out.applied, out.detail
        return tid

    tid = _run(go())
    [entry] = m.actions()
    assert entry["action"] == "REASSIGN_TEAM" and entry["team"] == "alpha" and entry["undoable"]
    assert entry["params"]["to_team"] == "bravo"
    assert {t.team_id for t in q.tickets_for_task(tid)} == {"bravo"}
    assert q.get_task(tid)["team_id"] == "bravo" and m.directives[tid].team_id == "bravo"

    res = m.undo_action(entry["action_id"])
    assert res["ok"], res
    assert {t.team_id for t in q.tickets_for_task(tid)} == {"alpha"}
    assert q.get_task(tid)["team_id"] == "alpha" and m.directives[tid].team_id == "alpha"
    f.close()


def test_reassign_refuses_without_idle_team_and_undo_refuses_after_pickup(tmp_path: Path) -> None:
    f = _factory(tmp_path)
    m, q = f.manager, f.queue

    async def go() -> tuple[str, str]:
        t1 = await m.submit(_order("one", team="alpha"))
        t2 = await m.submit(_order("two", team="bravo"))
        busy = await m.apply_escalation(_alert("REASSIGN_TEAM", task_id=t1))
        assert not busy.applied and "no idle team" in busy.detail
        q.quarantine(t2, "cleanup")  # bravo is idle now
        out = await m.apply_escalation(_alert("REASSIGN_TEAM", task_id=t1, params={"to_team": "bravo"}))
        assert out.applied
        return t1, t2

    t1, _ = _run(go())
    entry = m.actions()[0]
    failed = m.actions()[1]
    assert failed["applied"] is False and failed["undoable"] is False
    # bravo picked the ticket up -> undo refuses
    assert q.claim_ticket("bravo", 1) is not None
    res = m.undo_action(entry["action_id"])
    assert res["ok"] is False and "picked up" in res["reason"]
    f.close()


def test_retry_task_reopens_once_with_fresh_attempts_and_undo_requarantines(tmp_path: Path) -> None:
    f = _factory(tmp_path)
    m, q = f.manager, f.queue

    async def go() -> str:
        tid = await m.submit(_order("flaky"))
        ticket = q.tickets_for_task(tid)[0]
        q.reopen_ticket(ticket.ticket_id, "x")
        q.record_infra_failure(ticket.ticket_id, "timeout")
        q.quarantine(tid, "Claude CLI timed out 3 times")
        not_q = await m.apply_escalation(_alert("RETRY_TASK", task_id="F-999"))
        assert not not_q.applied
        out = await m.apply_escalation(_alert("RETRY_TASK", category="LINE_JAM", task_id=tid, params={"transient": True}))
        assert out.applied, out.detail
        return tid

    tid = _run(go())
    entry = m.actions()[0]
    assert entry["action"] == "RETRY_TASK" and entry["undoable"]
    [t] = q.tickets_for_task(tid)
    assert q.get_task(tid)["state"] == "building" and t.status == "open" and t.attempt == 0 and t.infra_failures == 0

    res = m.undo_action(entry["action_id"])
    assert res["ok"], res
    task = q.get_task(tid)
    assert task["state"] == "quarantined" and task["quarantine_reason"] == "Claude CLI timed out 3 times"

    # never twice for the same task (even after an undo)
    async def again() -> Any:
        return await m.apply_escalation(_alert("RETRY_TASK", task_id=tid))

    out = _run(again())
    assert not out.applied and "already retried" in out.detail
    f.close()


def test_action_log_survives_a_restart(tmp_path: Path) -> None:
    f = _factory(tmp_path)

    async def go() -> str:
        tid = await f.manager.submit(_order("q me"))
        await f.manager.apply_escalation(_alert("QUARANTINE_TASK", task_id=tid))
        return tid

    tid = _run(go())
    aid = f.manager.actions()[0]["action_id"]
    queue = f.queue
    m2 = FloorManager(queue, f.foreman, f.pools, f.telemetry, BoardBridge("dry"), limiter=f.limiter)
    assert [e["action_id"] for e in m2.actions()] == [aid]
    assert m2.undo_action(aid)["ok"]
    assert queue.get_task(tid)["state"] == "building"
    m3 = FloorManager(queue, f.foreman, f.pools, f.telemetry, BoardBridge("dry"), limiter=f.limiter)
    assert m3.actions()[0]["undone_at"] and m3.undo_action(aid)["already_undone"]

    async def next_action() -> None:
        tid2 = await m3.submit(_order("q me too"))
        await m3.apply_escalation(_alert("QUARANTINE_TASK", task_id=tid2))

    _run(next_action())
    assert m3.actions()[0]["action_id"] == "A-0002"  # numbering continues
    f.close()


def test_foreman_reads_the_action_log(tmp_path: Path) -> None:
    f = _factory(tmp_path)
    assert f.foreman.actions_provider == f.manager.actions
    f.close()


def _board_statuses(bridge: Any, task_id: str) -> list[str]:
    return [
        op["args"]["patch"]["status"] for op in bridge.ops
        if op["type"] == "updateTask" and op.get("foundry_task", task_id) == task_id
    ]


def test_retry_and_undo_quarantine_set_the_board_task_active(tmp_path: Path) -> None:
    f = _factory(tmp_path)
    m, q = f.manager, f.queue

    async def go() -> tuple[str, str]:
        t1 = await m.submit(_order("undo me"))
        await m.apply_escalation(_alert("QUARANTINE_TASK", task_id=t1))
        t2 = await m.submit(_order("retry me", team="bravo"))
        q.quarantine(t2, "timeout")
        await m.apply_escalation(_alert("RETRY_TASK", task_id=t2))
        return t1, t2

    t1, t2 = _run(go())
    assert m.undo_action(m.actions()[-1]["action_id"])["ok"]  # undo the quarantine of t1
    statuses = [op["args"]["patch"]["status"] for op in f.bridge.ops if op["type"] == "updateTask"]
    assert statuses.count("active") == 2 and "paused" in statuses
    notes = [op["args"]["message"] for op in f.bridge.ops if op["type"] == "reportProgress"]
    assert any("retried with fresh attempts" in n for n in notes)
    assert any("quarantine undone" in n for n in notes)
    kinds = [e["kind"] for e in q.events(task_id=t2)] if "task_id" in q.events.__code__.co_varnames else []
    assert not kinds or "unquarantined" in kinds
    f.close()


def test_reassign_uses_named_ticket_ids_and_partial_undo_says_so(tmp_path: Path) -> None:
    f = _factory(tmp_path)
    m, q = f.manager, f.queue

    async def go() -> tuple[str, list[str]]:
        tid = await m.submit(_order("three", ["shop/strings.py", "shop/slug.py", "shop/pricing.py"]))
        ids = [t.ticket_id for t in q.tickets_for_task(tid)]
        assert len(ids) >= 2
        out = await m.apply_escalation(_alert("REASSIGN_TEAM", task_id=tid, params={"to_team": "bravo", "ticket_ids": ids[:2]}))
        assert out.applied and out.parameters["tickets"] == ids[:2]
        return tid, ids

    tid, ids = _run(go())
    teams = {t.ticket_id: t.team_id for t in q.tickets_for_task(tid)}
    assert [teams[i] for i in ids[:2]] == ["bravo", "bravo"]
    assert all(teams[i] == "alpha" for i in ids[2:])
    # bravo starts one of the two -> undo moves the other back and says "partly"
    slot = q.get_ticket(ids[0]).slot
    claimed = q.claim_ticket("bravo", slot)
    assert claimed is not None and claimed.ticket_id in ids[:2]
    res = m.undo_action(m.actions()[0]["action_id"])
    assert res["ok"] and "partly" in res["detail"]
    f.close()


def test_still_owned_rejects_a_later_claim_with_the_same_attempt(tmp_path: Path) -> None:
    from foundry.builder_worker import BuilderWorker  # noqa: F401  (the check lives on the worker base)

    f = _factory(tmp_path, teams=["alpha"])
    m, q = f.manager, f.queue

    async def go() -> str:
        return await m.submit(_order("race"))

    tid = _run(go())
    pool = f.pools["alpha"]
    worker = pool.builder(1)
    first = q.claim_ticket("alpha", 1)
    assert first is not None and worker._still_owned(first)
    # an undo/reset releases it (attempt unchanged) and it is claimed again later
    q.release_ticket(first.ticket_id)
    second = q.claim_ticket("alpha", 1)
    assert second is not None and second.attempt == first.attempt
    assert second.claimed_at != first.claimed_at
    assert not worker._still_owned(first)  # the stale review must not complete it
    assert worker._still_owned(second)
    assert tid
    f.close()
