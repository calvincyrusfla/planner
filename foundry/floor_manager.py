"""Floor Manager: turns work orders into sized, dispatched, reported tasks.

The Floor Manager is the planning tier of the factory. It owns no polling loop
and makes **no** Claude calls of its own while the line runs: every decision it
takes is driven by an event (a work order arrives, the Shop Foreman raises an
escalation, a task completes).

Flow::

    work order --submit()--> queue.add_task --> foreman.size_task_scope
               --> queue.apply_sizing (one ticket per builder slot)
               --> bridge.register_task (the Agent Department board)

    ForemanEscalation --apply_escalation()--> one of six actions
    task complete --on_task_complete()--> foreman.draft_pr_summary --> bridge.done

The ``claude`` client is accepted so a phase-2 manager can plan large work
orders with Claude; phase 1 never calls it (tests assert 0 manager calls).
"""

from __future__ import annotations

import inspect
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, Field

from foundry.schemas import (
    ForemanEscalation,
    PrescribedAction,
    TaskSizingDirective,
    TaskState,
)

log = logging.getLogger(__name__)

__all__ = ["WorkOrder", "ActionOutcome", "PeerReviewerGrant", "FloorManager"]


class WorkOrder(BaseModel):
    """A unit of work handed to the factory (what a human or the board asks for)."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1)
    description: str = ""
    task_id: str | None = Field(default=None, description="Optional id; generated as F-### when absent.")
    team_id: str | None = Field(default=None, description="Optional team; least-loaded team when absent.")
    files: list[str] = Field(default_factory=list, description="Repo-relative files the work may touch.")
    acceptance_criteria: list[str] = Field(default_factory=list)


@dataclass
class ActionOutcome:
    """What the Floor Manager did for one escalation."""

    alert_id: str
    action: str
    applied: bool
    detail: str
    parameters: dict[str, Any] = field(default_factory=dict)


@dataclass
class PeerReviewerGrant:
    """An idle builder slot temporarily acting as a reviewer."""

    team_id: str
    slot: int
    agent_id: str
    until: float  # monotonic deadline

    def active(self, now: float | None = None) -> bool:
        """True until the grant expires."""
        return (time.monotonic() if now is None else now) < self.until


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _val(x: Any) -> Any:
    return getattr(x, "value", x)


class FloorManager:
    """Planning tier: sizes and registers work orders, executes Foreman escalations.

    Args:
        queue: the :class:`~foundry.orchestrator_queue.FoundryQueue`.
        foreman: the :class:`~foundry.shop_foreman.ShopForeman`.
        teams: team id -> :class:`~foundry.builder_worker.TeamPool`.
        telemetry: the :class:`~foundry.telemetry.Telemetry`.
        bridge: the :class:`~foundry.board_bridge.BoardBridge`.
        claude: a ClaudeClient (unused in phase 1; never polled).
        limiter: the shared :class:`~foundry.rate_limiter.RateLimiter`
            (needed for THROTTLE_DISPATCH).
        builders_per_team: slots per team (for PROMOTE_PEER_REVIEWER).
    """

    def __init__(
        self,
        queue: Any,
        foreman: Any,
        teams: Mapping[str, Any],
        telemetry: Any,
        bridge: Any,
        claude: Any = None,
        *,
        limiter: Any = None,
        builders_per_team: int = 5,
    ) -> None:
        self.queue = queue
        self.foreman = foreman
        self.teams = dict(teams)
        self.telemetry = telemetry
        self.bridge = bridge
        self.claude = claude
        self.limiter = limiter
        self.builders_per_team = builders_per_team
        self.directives: dict[str, TaskSizingDirective] = {}
        self.orders: dict[str, WorkOrder] = {}
        self.pr_summaries: dict[str, dict[str, str]] = {}
        self.peer_reviewers: dict[str, PeerReviewerGrant] = {}
        self.actions: list[ActionOutcome] = []
        self._seq = 0
        self._completed: set[str] = set()

    # ------------------------------------------------------------- work orders

    def _next_task_id(self) -> str:
        self._seq += 1
        return f"F-{self._seq:03d}"

    def _pick_team(self, order: WorkOrder) -> str:
        if order.team_id:
            return order.team_id
        names = list(self.teams) or ["alpha"]
        load = {t: 0 for t in names}
        for d in self.directives.values():
            if d.task_id not in self._completed and d.team_id in load:
                load[d.team_id] += 1
        return min(names, key=lambda t: (load[t], names.index(t)))

    async def submit(self, work_order: WorkOrder | Mapping[str, Any]) -> str:
        """Size a work order with the Foreman, queue its tickets and put it on the board.

        Returns the Foundry task id.
        """
        order = work_order if isinstance(work_order, WorkOrder) else WorkOrder.model_validate(dict(work_order))
        task_id = order.task_id or self._next_task_id()
        order = order.model_copy(update={"task_id": task_id, "team_id": self._pick_team(order)})
        self.orders[task_id] = order
        self.queue.add_task(task_id, order.title, order.description, order.team_id)
        self.queue.set_state(task_id, TaskState.SIZING, "floor manager: sizing")
        directive = await self._size(order)
        self.directives[task_id] = directive
        self.queue.apply_sizing(directive)
        self.queue.record_event(
            "sized",
            task_id,
            {"complexity": directive.complexity, "builders": directive.allocated_builders, "team": directive.team_id},
        )
        self.bridge.register_task(
            task_id,
            order.title,
            order.description,
            complexity=directive.complexity,
            builders=directive.allocated_builders,
            team_id=directive.team_id,
        )
        self.bridge.progress(
            task_id,
            f"[Foundry] sized {directive.complexity}: {directive.allocated_builders} builder(s) on {directive.team_id}. {directive.rationale}"[:300],
            team_id=directive.team_id,
        )
        return task_id

    async def _size(self, order: WorkOrder) -> TaskSizingDirective:
        task = {
            "task_id": order.task_id,
            "team_id": order.team_id,
            "title": order.title,
            "description": order.description,
            "files": list(order.files),
            "acceptance_criteria": list(order.acceptance_criteria),
        }
        result = await _maybe_await(self.foreman.size_task_scope(task))
        if isinstance(result, TaskSizingDirective):
            return result
        return TaskSizingDirective.model_validate(result)

    # ------------------------------------------------------------ escalations

    async def apply_escalation(self, alert: ForemanEscalation | Mapping[str, Any]) -> ActionOutcome:
        """Execute the escalation's prescribed action (validated first) and report it."""
        if not isinstance(alert, ForemanEscalation):
            alert = ForemanEscalation.model_validate(dict(alert))
        action = PrescribedAction(_val(alert.prescribed_action))
        handler = {
            PrescribedAction.PROMOTE_PEER_REVIEWER: self._promote_peer_reviewer,
            PrescribedAction.QUARANTINE_TASK: self._quarantine_task,
            PrescribedAction.RESET_BUILDER_CONTEXT: self._reset_builder_context,
            PrescribedAction.RESIZE_ALLOCATION: self._resize_allocation,
            PrescribedAction.THROTTLE_DISPATCH: self._throttle_dispatch,
            PrescribedAction.NONE: self._no_action,
        }[action]
        try:
            applied, detail, params = await handler(alert)
        except Exception as exc:  # an action must never take the line down
            log.warning("floor manager: %s failed: %s", action.value, exc)
            applied, detail, params = False, f"failed: {exc}", {}
        outcome = ActionOutcome(alert.alert_id, action.value, applied, detail, params)
        self.actions.append(outcome)
        task_id = alert.target_entity.task_id
        try:
            self.queue.record_event(
                "escalation",
                task_id or "",
                {"alert_id": alert.alert_id, "category": _val(alert.category), "action": action.value, "applied": applied, "detail": detail},
            )
        except Exception as exc:  # pragma: no cover - telemetry is best effort
            log.debug("record_event failed: %s", exc)
        self.bridge.escalation(alert)
        return outcome

    def _team_for(self, alert: ForemanEscalation) -> str | None:
        t = alert.target_entity
        if t.team_id:
            return t.team_id
        if t.agent_id and "-" in t.agent_id:
            return t.agent_id.rsplit("-", 1)[0]
        if t.task_id and t.task_id in self.directives:
            return self.directives[t.task_id].team_id
        return None

    def _allocated(self, team_id: str) -> int:
        """Highest builder slot in use by the team's open tasks."""
        n = 0
        for d in self.directives.values():
            if d.team_id == team_id and d.task_id not in self._completed:
                n = max(n, d.allocated_builders)
        return n

    async def _promote_peer_reviewer(self, alert: ForemanEscalation) -> tuple[bool, str, dict[str, Any]]:
        team_id = self._team_for(alert)
        if not team_id:
            return False, "no team to promote a reviewer in", {}
        minutes = float(alert.action_parameters.get("minutes", alert.action_parameters.get("duration_minutes", 15)))
        slot = alert.action_parameters.get("slot")
        if slot is None:
            idle = [s for s in range(self._allocated(team_id) + 1, self.builders_per_team + 1)]
            if not idle:
                return False, f"{team_id} has no idle builder slot", {}
            slot = idle[-1]
        slot = int(slot)
        grant = PeerReviewerGrant(team_id, slot, f"{team_id}-{slot}", time.monotonic() + minutes * 60)
        self.peer_reviewers[team_id] = grant
        pool = self.teams.get(team_id)
        hook = getattr(pool, "promote_peer_reviewer", None)
        if callable(hook):
            await _maybe_await(hook(slot, minutes))
        return True, f"{grant.agent_id} reviews for {team_id} for {minutes:g} min", {"slot": slot, "minutes": minutes}

    async def _quarantine_task(self, alert: ForemanEscalation) -> tuple[bool, str, dict[str, Any]]:
        task_id = alert.target_entity.task_id
        if not task_id:
            return False, "no task to quarantine", {}
        reason = str(alert.action_parameters.get("reason") or alert.diagnosis)
        self.queue.quarantine(task_id, reason)
        self.bridge.quarantined(task_id, reason)
        return True, f"{task_id} quarantined", {"reason": reason}

    async def _reset_builder_context(self, alert: ForemanEscalation) -> tuple[bool, str, dict[str, Any]]:
        agent_id = alert.target_entity.agent_id or alert.action_parameters.get("agent_id")
        team_id = self._team_for(alert)
        if not agent_id or not team_id:
            return False, "no builder to reset", {}
        slot = int(str(agent_id).rsplit("-", 1)[1])
        pool = self.teams.get(team_id)
        detail = [f"{agent_id} context cleared"]
        hook = getattr(pool, "reset_builder", None) or getattr(pool, "reset_builder_context", None)
        if callable(hook):
            await _maybe_await(hook(slot))
        # Reopen the stalled ticket with a fresh prompt (no fix hint carried over).
        reopened = await self._reopen_claimed(team_id, slot, alert.target_entity.task_id)
        if reopened:
            detail.append(f"ticket {reopened} reopened")
        return True, "; ".join(detail), {"agent_id": agent_id, "ticket": reopened}

    async def _reopen_claimed(self, team_id: str, slot: int, task_id: str | None) -> str | None:
        fn = getattr(self.queue, "release_ticket", None) or getattr(self.queue, "reset_ticket", None)
        tickets_fn = getattr(self.queue, "tickets_for", None) or getattr(self.queue, "get_tickets", None)
        if not callable(tickets_fn):
            return None
        tickets = tickets_fn(task_id) if task_id else []
        for t in tickets or []:
            status = _val(_get(t, "status"))
            if _get(t, "team_id") == team_id and int(_get(t, "slot") or 0) == slot and status == "claimed":
                ticket_id = _get(t, "ticket_id")
                if callable(fn):
                    fn(ticket_id)
                return str(ticket_id)
        return None

    async def _resize_allocation(self, alert: ForemanEscalation) -> tuple[bool, str, dict[str, Any]]:
        task_id = alert.target_entity.task_id
        n = alert.action_parameters.get("builders", alert.action_parameters.get("allocated_builders"))
        if not task_id or n is None:
            return False, "resize needs a task and a builder count", {}
        n = max(1, min(self.builders_per_team, int(n)))
        self.queue.resize(task_id, n)
        if task_id in self.directives:
            d = self.directives[task_id]
            self.bridge.progress(task_id, f"[Foundry] resized to {n} builder(s)", team_id=d.team_id)
        return True, f"{task_id} resized to {n} builder(s)", {"builders": n}

    async def _throttle_dispatch(self, alert: ForemanEscalation) -> tuple[bool, str, dict[str, Any]]:
        factor = float(alert.action_parameters.get("factor", 0.5))
        seconds = float(alert.action_parameters.get("seconds", alert.action_parameters.get("duration_seconds", 300)))
        if self.limiter is None:
            return False, "no rate limiter to throttle", {}
        self.limiter.throttle(factor, seconds)
        return True, f"dispatch throttled to {factor:g}x for {seconds:g}s", {"factor": factor, "seconds": seconds}

    async def _no_action(self, alert: ForemanEscalation) -> tuple[bool, str, dict[str, Any]]:
        return True, "no action", {}

    def peer_reviewer_for(self, team_id: str) -> PeerReviewerGrant | None:
        """The team's active temporary reviewer, if any (expired grants are dropped)."""
        grant = self.peer_reviewers.get(team_id)
        if grant and not grant.active():
            del self.peer_reviewers[team_id]
            return None
        return grant

    # -------------------------------------------------------------- completion

    async def on_task_complete(self, task_id: str, diff: str | None = None) -> dict[str, str]:
        """Draft the commit message / PR body with the Foreman, store it, mark the board task done."""
        if task_id in self.pr_summaries:
            return self.pr_summaries[task_id]
        if diff is None:
            diff = self._task_diff(task_id)
        summary = await _maybe_await(self.foreman.draft_pr_summary(diff))
        summary = {k: str(v) for k, v in dict(summary).items()}
        self.pr_summaries[task_id] = summary
        self._completed.add(task_id)
        try:
            self.queue.record_event("pr_summary", task_id, summary)
        except Exception as exc:  # pragma: no cover
            log.debug("record_event failed: %s", exc)
        d = self.directives.get(task_id)
        if d is not None:
            self.bridge.review(task_id, d.team_id, "APPROVED", note="Foundry reviewer approved")
        self.bridge.done(task_id, summary.get("commit_message", ""))
        return summary

    def _task_diff(self, task_id: str) -> str:
        fn = getattr(self.queue, "task_diff", None) or getattr(self.queue, "approved_diffs", None)
        if callable(fn):
            got = fn(task_id)
            if isinstance(got, str):
                return got
            if isinstance(got, (list, tuple)):
                return "\n".join(str(x) for x in got)
        return ""


def _get(obj: Any, name: str) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name)
    return getattr(obj, name, None)
