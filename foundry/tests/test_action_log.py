"""The Floor Manager's persisted action log: every escalation is logged and none is carried out.

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
_UNDO_GONE = {"ok": False, "reason": "undo is gone: the Foreman only logs and never acts"}


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


def test_quarantine_is_logged_and_nothing_changes(tmp_path: Path) -> None:
    f = _factory(tmp_path)
    m, q = f.manager, f.queue

    async def go() -> tuple[str, str]:
        tid = await m.submit(_order("q me"))
        state = q.get_task(tid)["state"]
        out = await m.apply_escalation(_alert("QUARANTINE_TASK", task_id=tid, params={"reason": "jam"}))
        assert out.applied is False and out.parameters == {}
        return tid, state

    tid, state = _run(go())
    assert q.get_task(tid)["state"] == state != "quarantined"
    [entry] = m.actions()
    assert entry["action"] == "QUARANTINE_TASK" and entry["task_id"] == tid and entry["team"] == "alpha"
    assert entry["action_id"] == "A-0001" and entry["at"].endswith("Z") and entry["undone_at"] is None
    assert entry["applied"] is False and entry["undoable"] is False and entry["undo_reason"] == "Nothing was changed."
    assert entry["before"] == {} and entry["params"] == {}
    assert not q.sizing_outcomes(include_excluded=True)  # nothing was quarantined, so no outcome
    f.close()


def test_every_action_kind_is_logged_and_none_is_carried_out(tmp_path: Path) -> None:
    f = _factory(tmp_path)
    m, q = f.manager, f.queue

    async def go() -> tuple[str, str, int]:
        tid = await m.submit(_order("busy", ["shop/strings.py", "shop/slug.py"]))
        stuck = await m.submit(_order("stuck", team="bravo"))
        level = q.task_context_level(tid)
        q.quarantine(stuck, "timeout")
        await m.apply_escalation(_alert("RESIZE_ALLOCATION", category="CAPACITY_REBALANCE", task_id=tid, params={"builders": 4}))
        await m.apply_escalation(_alert("THROTTLE_DISPATCH", category="TOKEN_VELOCITY_SPIKE", team_id="alpha", params={"factor": 0.5, "seconds": 60}))
        await m.apply_escalation(_alert("RESET_BUILDER_CONTEXT", category="AGENT_STALLED", task_id=tid, team_id="alpha", agent_id="alpha-1"))
        await m.apply_escalation(_alert("PROMOTE_PEER_REVIEWER", category="REVIEW_BOTTLENECK", team_id="alpha", params={"minutes": 5, "slot": 5}))
        await m.apply_escalation(_alert("REASSIGN_TEAM", task_id=tid, params={"to_team": "bravo"}))
        await m.apply_escalation(_alert("RETRY_TASK", task_id=stuck))
        await m.apply_escalation(_alert("WIDEN_CONTEXT", task_id=tid))
        return tid, stuck, level

    tid, stuck, level = _run(go())
    allocated_before = m.directives[tid].allocated_builders
    assert [e["action"] for e in m.actions()] == [
        "WIDEN_CONTEXT", "RETRY_TASK", "REASSIGN_TEAM", "PROMOTE_PEER_REVIEWER",
        "RESET_BUILDER_CONTEXT", "THROTTLE_DISPATCH", "RESIZE_ALLOCATION",
    ]
    assert all(e["applied"] is False and e["undoable"] is False for e in m.actions())
    assert q.get_task(tid)["allocated_builders"] == allocated_before != 4
    assert m.allocations[tid] == allocated_before
    assert not f.limiter.is_throttled
    assert m.peer_reviewer_for("alpha") is None
    assert {t.team_id for t in q.tickets_for_task(tid)} == {"alpha"} and m.directives[tid].team_id == "alpha"
    assert q.get_task(stuck)["state"] == "quarantined"
    assert q.task_context_level(tid) == level
    assert m.actions(limit=1)[0]["action"] == "WIDEN_CONTEXT"
    f.close()


def test_none_is_answered_but_not_logged(tmp_path: Path) -> None:
    f = _factory(tmp_path)
    out = _run(f.manager.apply_escalation(_alert("NONE", team_id="alpha")))
    assert out.action == "NONE" and out.applied is False
    assert f.manager.actions() == []
    f.close()


def test_undo_is_refused_with_the_fixed_reason(tmp_path: Path) -> None:
    f = _factory(tmp_path)
    m, q = f.manager, f.queue

    async def go() -> str:
        tid = await m.submit(_order("q me"))
        await m.apply_escalation(_alert("QUARANTINE_TASK", task_id=tid))
        return tid

    tid = _run(go())
    state = q.get_task(tid)["state"]
    entry = m.actions()[0]
    assert m.undo_action(entry["action_id"], by="tester") == _UNDO_GONE
    assert m.undo_action("A-9999") == _UNDO_GONE
    assert m.actions() == [entry]  # the log is untouched
    assert q.get_task(tid)["state"] == state
    f.close()


def test_action_log_survives_a_restart(tmp_path: Path) -> None:
    f = _factory(tmp_path)

    async def go() -> str:
        tid = await f.manager.submit(_order("q me"))
        await f.manager.apply_escalation(_alert("QUARANTINE_TASK", task_id=tid))
        return tid

    _run(go())
    aid = f.manager.actions()[0]["action_id"]
    queue = f.queue
    m2 = FloorManager(queue, f.foreman, f.pools, f.telemetry, BoardBridge("dry"), limiter=f.limiter)
    assert [e["action_id"] for e in m2.actions()] == [aid]

    async def next_action() -> None:
        tid2 = await m2.submit(_order("q me too"))
        await m2.apply_escalation(_alert("QUARANTINE_TASK", task_id=tid2))

    _run(next_action())
    assert m2.actions()[0]["action_id"] == "A-0002"  # numbering continues
    f.close()


def test_foreman_reads_the_action_log(tmp_path: Path) -> None:
    f = _factory(tmp_path)
    assert f.foreman.actions_provider == f.manager.actions
    f.close()


def test_still_owned_rejects_a_later_claim_with_the_same_attempt(tmp_path: Path) -> None:
    f = _factory(tmp_path, teams=["alpha"])
    m, q = f.manager, f.queue

    async def go() -> str:
        return await m.submit(_order("race"))

    tid = _run(go())
    pool = f.pools["alpha"]
    worker = pool.builder(1)
    first = q.claim_ticket("alpha", 1)
    assert first is not None and worker._still_owned(first)
    # a release puts it back (attempt unchanged) and it is claimed again later
    q.release_ticket(first.ticket_id)
    second = q.claim_ticket("alpha", 1)
    assert second is not None and second.attempt == first.attempt
    assert second.claimed_at != first.claimed_at
    assert not worker._still_owned(first)  # the stale review must not complete it
    assert worker._still_owned(second)
    assert tid
    f.close()
