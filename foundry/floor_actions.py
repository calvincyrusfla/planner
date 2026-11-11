"""The Foreman's escalation actions, their action log and their undo.

Split out of :mod:`foundry.floor_manager`: :class:`ForemanActionsMixin` holds
the methods, and ``FloorManager`` inherits them and sets their attributes.
"""

# Moved verbatim: some actions return a fourth "before" item that their annotation omits.
# mypy: disable-error-code="return-value"

from __future__ import annotations

import json
import logging
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from foundry.floor_helpers import (
    ACTION_LOG_MAX,
    ActionOutcome,
    PeerReviewerGrant,
    _ACTIONS_KEY,
    _TERMINAL,
    _WIDEN_COOLDOWN_S,
    _WIDEN_UNDO_SUPPRESS_S,
    _maybe_await,
    _parse_utc,
    _utc_now,
    _val,
)
from foundry.schemas import (
    ForemanEscalation,
    PrescribedAction,
    TaskSizingDirective,
)

# The manager's logger: these messages keep the name they had before the split.
log = logging.getLogger("foundry.floor_manager")

__all__ = ["ForemanActionsMixin"]


class ForemanActionsMixin:
    """``FloorManager``'s Foreman escalation actions and their undo."""

    if TYPE_CHECKING:
        # Set in FloorManager.__init__ or defined on FloorManager; declared for the type checker only.
        queue: Any
        teams: dict[str, Any]
        bridge: Any
        limiter: Any
        builders_per_team: int
        directives: dict[str, TaskSizingDirective]
        orders: dict[str, Any]
        peer_reviewers: dict[str, PeerReviewerGrant]
        outcomes: list[ActionOutcome]
        action_log: list[dict[str, Any]]
        _action_seq: int
        _completed: set[str]
        _reported_quarantine: set[str]
        sizing_meta: dict[str, dict[str, Any]]
        allocations: dict[str, int]
        applied_alerts: dict[str, ActionOutcome]

        def save(self) -> None: ...

        def _prune_later(self) -> None: ...

        def _start_partial(self, task_id: str, reason: str) -> Any: ...

    # ------------------------------------------------------------ escalations

    async def apply_escalation(
        self, alert: ForemanEscalation | Mapping[str, Any], *, act: bool = True
    ) -> ActionOutcome:
        """Execute the escalation's prescribed action (validated first) and report it.

        Idempotent per ``alert_id``: an alert that was already applied (in this
        run or before a restart) returns the earlier outcome without acting,
        storing or posting it again.

        With ``act=False`` (``FOUNDRY_FOREMAN_ACTIONS=log``) the action is not
        carried out: the outcome says so (``applied`` False) and is stored,
        logged and posted like any other, so the alert stays visible.
        """
        if not isinstance(alert, ForemanEscalation):
            alert = ForemanEscalation.model_validate(dict(alert))
        earlier = self.applied_alerts.get(alert.alert_id) or self._stored_outcome(alert.alert_id)
        if earlier is not None:
            self.applied_alerts[alert.alert_id] = earlier
            return earlier
        action = PrescribedAction(_val(alert.prescribed_action))
        handler = {
            PrescribedAction.PROMOTE_PEER_REVIEWER: self._promote_peer_reviewer,
            PrescribedAction.QUARANTINE_TASK: self._quarantine_task,
            PrescribedAction.RESET_BUILDER_CONTEXT: self._reset_builder_context,
            PrescribedAction.RESIZE_ALLOCATION: self._resize_allocation,
            PrescribedAction.THROTTLE_DISPATCH: self._throttle_dispatch,
            PrescribedAction.WIDEN_CONTEXT: self._widen_context,
            PrescribedAction.NONE: self._no_action,
        }
        for name, fn in (("REASSIGN_TEAM", self._reassign_team), ("RETRY_TASK", self._retry_task)):
            member = getattr(PrescribedAction, name, None)
            if member is not None:
                handler[member] = fn
        before: dict[str, Any] = {}
        try:
            result = await handler[action if act else PrescribedAction.NONE](alert)
            applied, detail, params = result[:3]
            if len(result) > 3:
                before = dict(result[3] or {})
        except Exception as exc:  # an action must never take the line down
            log.warning("floor manager: %s failed: %s", action.value, exc)
            applied, detail, params = False, f"failed: {exc}", {}
        if not act:
            applied, detail = False, f"logged only (FOUNDRY_FOREMAN_ACTIONS=log): {action.value}"
        outcome = ActionOutcome(alert.alert_id, action.value, applied, detail, params)
        self.outcomes.append(outcome)
        self.applied_alerts[alert.alert_id] = outcome
        if action != PrescribedAction.NONE:
            self._log_action(alert, outcome, before)
        try:
            self.queue.record_escalation(alert, applied=True, result=outcome.__dict__)
        except Exception as exc:  # pragma: no cover - bookkeeping is best effort
            log.debug("record_escalation failed: %s", exc)
        self.bridge.escalation(alert)
        self.save()
        return outcome

    def _stored_outcome(self, alert_id: str) -> ActionOutcome | None:
        """The outcome the queue stored for an alert already handled (survives lost manager state)."""
        fn = getattr(self.queue, "get_escalation", None)
        if not callable(fn):
            return None
        try:
            row = fn(alert_id)
        except Exception:  # pragma: no cover - defensive
            return None
        result = (row or {}).get("result")
        if not row or not row.get("applied_at") or not isinstance(result, dict):
            return None
        try:
            return ActionOutcome(**result)
        except TypeError:
            return None

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
        """Highest builder slot in use by the team's open tasks (current allocation, after resizes)."""
        n = 0
        for d in self.directives.values():
            if d.team_id != team_id or d.task_id in self._completed:
                continue
            task = self.queue.get_task(d.task_id)
            if task and _val(task.get("state")) in _TERMINAL:
                continue
            n = max(n, self.allocations.get(d.task_id, d.allocated_builders))
        return n

    def _slot_idle(self, team_id: str, slot: int) -> bool:
        """A slot is idle when the pool says it is not busy and it holds no claimed ticket."""
        pool = self.teams.get(team_id)
        builder = getattr(pool, "builder", None)
        if callable(builder):
            try:
                if getattr(builder(slot), "busy", False):
                    return False
            except (IndexError, KeyError):
                return False
        agent = f"{team_id}-{slot}"
        return not any(t.claimed_by == agent for t in self.queue.tickets(team_id=team_id, status="claimed"))

    async def _promote_peer_reviewer(self, alert: ForemanEscalation) -> tuple[bool, str, dict[str, Any]]:
        team_id = self._team_for(alert)
        if not team_id:
            return False, "no team to promote a reviewer in", {}
        minutes = float(alert.action_parameters.get("minutes", alert.action_parameters.get("duration_minutes", 15)))
        alloc = self._allocated(team_id)
        idle = [s for s in range(alloc + 1, self.builders_per_team + 1) if self._slot_idle(team_id, s)]
        wanted = alert.action_parameters.get("slot")
        if wanted is not None and int(wanted) in idle:
            slot = int(wanted)
        elif idle:
            slot = idle[-1]
        else:
            return False, f"{team_id} has no idle builder slot above its allocation ({alloc})", {}
        pool = self.teams.get(team_id)
        hook = getattr(pool, "promote_peer_reviewer", None)
        if not callable(hook):
            return False, f"team {team_id} has no pool to promote a reviewer in", {}
        started = await _maybe_await(hook(slot, minutes))
        if not started:
            return False, f"{team_id}-{slot} was not promoted (pool not running or slot busy)", {"slot": slot}
        grant = PeerReviewerGrant(team_id, slot, str(started), time.monotonic() + minutes * 60)
        self.peer_reviewers[team_id] = grant
        return (
            True, f"{grant.agent_id} reviews for {team_id} for {minutes:g} min", {"slot": slot, "minutes": minutes},
            {"team_id": team_id, "agent_id": grant.agent_id},
        )

    async def _quarantine_task(self, alert: ForemanEscalation) -> tuple[bool, str, dict[str, Any]]:
        task_id = alert.target_entity.task_id
        if not task_id:
            return False, "no task to quarantine", {}
        reason = str(alert.action_parameters.get("reason") or alert.diagnosis)
        task = self.queue.get_task(task_id)
        if task is None:
            return False, f"unknown task {task_id}", {}
        if _val(task.get("state")) in _TERMINAL:
            return False, f"{task_id} is already {_val(task.get('state'))}", {}
        # the tickets the quarantine blocks (and their attempts), so undo can reopen exactly those
        tickets = {
            t.ticket_id: {"status": t.status, "attempt": t.attempt}
            for t in self.queue.tickets_for_task(task_id) if t.status in ("open", "claimed")
        }
        before = {"task_id": task_id, "state": _val(task.get("state")), "tickets": tickets}
        self.queue.quarantine(task_id, reason)
        self._report_quarantine(task_id, reason)
        return True, f"{task_id} quarantined", {"reason": reason}, before

    def _report_quarantine(self, task_id: str, reason: str) -> None:
        """Post a quarantine to the board once per task (and record its sizing outcome)."""
        if task_id in self._reported_quarantine:
            return
        self._reported_quarantine.add(task_id)
        self.record_outcome(task_id)
        self.bridge.quarantined(task_id, reason)
        self._prune_later()
        self._start_partial(task_id, reason)

    def record_outcome(self, task_id: str) -> dict[str, Any] | None:
        """Record the task's sizing outcome in the queue (predicted vs actual effort).

        Called when a task completes (patch written) or is quarantined. The
        queue keeps the first row per task, so repeated calls are harmless.
        Best effort: a queue without ``record_sizing_outcome`` or an error
        never disturbs completion.
        """
        fn = getattr(self.queue, "record_sizing_outcome", None)
        if not callable(fn):
            return None
        order = self.orders.get(task_id)
        d = self.directives.get(task_id)
        meta = self.sizing_meta.get(task_id) or {}
        kwargs: dict[str, Any] = {"sizing_source": meta.get("source"), "raw_predicted": meta.get("raw")}
        if order is not None:
            kwargs["title"] = order.title
            if order.files:
                kwargs["files"] = [str(f) for f in order.files]
        if d is not None:
            kwargs["predicted"] = d.complexity
        try:
            return fn(task_id, **kwargs)
        except Exception as exc:  # pragma: no cover - bookkeeping is best effort
            log.warning("floor manager: could not record the sizing outcome of %s: %s", task_id, exc)
            return None

    async def _reset_builder_context(self, alert: ForemanEscalation) -> tuple[bool, str, dict[str, Any]]:
        agent_id = alert.target_entity.agent_id or alert.action_parameters.get("agent_id")
        team_id = self._team_for(alert)
        if not agent_id or not team_id:
            return False, "no builder to reset", {}
        slot = int(str(agent_id).rsplit("-", 1)[1])
        pool = self.teams.get(team_id)
        detail = [f"{agent_id} context cleared"]
        hook = getattr(pool, "reset_builder_context", None) or getattr(pool, "reset_builder", None)
        if callable(hook):
            await _maybe_await(hook(slot))
        # Put the stalled ticket back with a fresh prompt (attempt unchanged, no fix hint).
        released = self._release_claimed(
            team_id, slot, alert.target_entity.task_id, alert.action_parameters.get("ticket_id")
        )
        if released:
            detail.append(f"ticket {released} released for a fresh start")
        try:  # every reset counts as a stall, even a repeat for the same ticket
            self.queue.record_event(
                "builder_reset", alert.target_entity.task_id, {"agent_id": agent_id, "ticket_id": released, "alert_id": alert.alert_id}
            )
        except Exception as exc:  # pragma: no cover - bookkeeping is best effort
            log.debug("record_event failed: %s", exc)
        return True, "; ".join(detail), {"agent_id": agent_id, "ticket": released}

    def _release_claimed(
        self, team_id: str, slot: int, task_id: str | None, ticket_id: str | None = None
    ) -> str | None:
        """Release the ticket the stalled builder holds (``queue.release_ticket``: attempt unchanged).

        A stall is not the builder's fault, so it costs no retry; the ticket is
        open again for the same slot and the next claim builds a fresh prompt.
        """
        agent_id = f"{team_id}-{slot}"
        for t in self.queue.tickets(team_id=team_id, status="claimed"):
            if ticket_id and t.ticket_id != ticket_id:
                continue
            if task_id and t.task_id != task_id:
                continue
            if t.claimed_by == agent_id or (t.claimed_by is None and t.slot == slot):
                self.queue.release_ticket(t.ticket_id)
                return t.ticket_id
        return None

    async def _resize_allocation(self, alert: ForemanEscalation) -> tuple[bool, str, dict[str, Any]]:
        task_id = alert.target_entity.task_id
        n = alert.action_parameters.get("builders", alert.action_parameters.get("allocated_builders"))
        if not task_id or n is None:
            return False, "resize needs a task and a builder count", {}
        n = max(1, min(self.builders_per_team, int(n)))
        task = self.queue.get_task(task_id)
        previous = self.allocations.get(task_id) or int((task or {}).get("allocated_builders") or 0)
        self.queue.resize(task_id, n)
        self.allocations[task_id] = n
        if task_id in self.directives:
            d = self.directives[task_id]
            self.bridge.progress(task_id, f"[Foundry] resized to {n} builder(s)", team_id=d.team_id)
        return True, f"{task_id} resized to {n} builder(s)", {"builders": n}, {"task_id": task_id, "builders": previous}

    async def _throttle_dispatch(self, alert: ForemanEscalation) -> tuple[bool, str, dict[str, Any]]:
        factor = float(alert.action_parameters.get("factor", 0.5))
        seconds = float(alert.action_parameters.get("seconds", alert.action_parameters.get("duration_seconds", 300)))
        if self.limiter is None:
            return False, "no rate limiter to throttle", {}
        self.limiter.throttle(factor, seconds)
        return (
            True, f"dispatch throttled to {factor:g}x for {seconds:g}s", {"factor": factor, "seconds": seconds},
            {"factor": 1.0},
        )

    # ---------------------------------------------- REASSIGN_TEAM / RETRY_TASK

    def _team_idle(self, team_id: str) -> bool:
        """A team is idle when none of its tickets is open or claimed on a live task."""
        for status in ("open", "claimed"):
            for t in self.queue.tickets(team_id=team_id, status=status):
                task = self.queue.get_task(t.task_id)
                if task and _val(task.get("state")) not in _TERMINAL:
                    return False
        return True

    async def _reassign_team(self, alert: ForemanEscalation) -> tuple[Any, ...]:
        """Move a stuck task's open tickets to another (idle) team's pool."""
        task_id = alert.target_entity.task_id
        if not task_id:
            return False, "no task to reassign", {}
        task = self.queue.get_task(task_id)
        if task is None:
            return False, f"unknown task {task_id}", {}
        if _val(task.get("state")) in _TERMINAL:
            return False, f"{task_id} is already {_val(task.get('state'))}", {}
        current = str(task.get("team_id") or self._team_for(alert) or "")
        p = alert.action_parameters
        wanted = p.get("to_team") or p.get("target_team") or p.get("team")
        if not wanted and p.get("team_id") and p.get("team_id") != current:
            wanted = p.get("team_id")
        if wanted:
            wanted = str(wanted)
            if wanted == current:
                return False, f"{task_id} is already on {current}", {}
            if wanted not in self.teams:
                return False, f"unknown team {wanted}", {}
            target = wanted
        else:
            idle = [t for t in self.teams if t != current and self._team_idle(t)]
            if not idle:
                return False, f"no idle team to take {task_id} from {current}", {}
            target = idle[0]
        open_ids = [t.ticket_id for t in self.queue.tickets(task_id=task_id, status="open") if t.team_id == current]
        named = {str(x) for x in p.get("ticket_ids") or []}
        if named:  # only the stuck tickets the Foreman named (those still open)
            open_ids = [t for t in open_ids if t in named]
        if not open_ids:
            return False, f"{task_id} has no open tickets to move", {}
        moved = self.queue.move_tickets(task_id, target, open_ids)
        self._set_directive_team(task_id, target)
        why = str(p.get("reason") or "").strip()
        self.bridge.progress(
            task_id, f"[Foundry] moved {len(moved)} ticket(s) from {current} to {target}" + (f" ({why})" if why else ""),
            team_id=target,
        )
        return (
            True, f"{task_id} moved from {current} to {target} ({len(moved)} ticket(s))",
            {"from_team": current, "to_team": target, "tickets": moved},
            {"task_id": task_id, "from_team": current, "to_team": target, "tickets": moved},
        )

    def _set_directive_team(self, task_id: str, team_id: str) -> None:
        d = self.directives.get(task_id)
        if d is not None and d.team_id != team_id:
            self.directives[task_id] = d.model_copy(update={"team_id": team_id})

    def _retried(self, task_id: str) -> bool:
        return any(
            e.get("action") == "RETRY_TASK" and e.get("applied") and e.get("task_id") == task_id
            for e in self.action_log
        )

    async def _retry_task(self, alert: ForemanEscalation) -> tuple[Any, ...]:
        """Re-open a quarantined task with fresh attempts (at most once per task)."""
        task_id = alert.target_entity.task_id
        if not task_id:
            return False, "no task to retry", {}
        task = self.queue.get_task(task_id)
        if task is None:
            return False, f"unknown task {task_id}", {}
        state = _val(task.get("state"))
        if state != "quarantined":
            return False, f"{task_id} is {state}, not quarantined", {}
        if self._retried(task_id):
            return False, f"{task_id} was already retried once", {}
        blocked = [t for t in self.queue.tickets_for_task(task_id) if t.status == "blocked"]
        if not blocked:
            return False, f"{task_id} has no blocked tickets to retry", {}
        reason = str(task.get("quarantine_reason") or "")
        before = {
            "task_id": task_id,
            "reason": reason,
            "tickets": {t.ticket_id: {"attempt": t.attempt, "infra_failures": t.infra_failures} for t in blocked},
        }
        reopened = self.queue.reopen_quarantined(
            task_id, {t.ticket_id: {"attempt": 0, "infra_failures": 0} for t in blocked}, "retry (Foreman)"
        )
        self._back_on_line(task_id, f"[Foundry] {task_id} retried with fresh attempts")
        return True, f"{task_id} retried ({len(reopened)} ticket(s) reopened)", {"tickets": reopened}, before

    def _back_on_line(self, task_id: str, note: str) -> None:
        """A quarantine was lifted: board task active again, report the next quarantine, forget the outcome."""
        self._reported_quarantine.discard(task_id)
        resumed = getattr(self.bridge, "resumed", None)
        if callable(resumed):
            resumed(task_id, note)
        else:  # an older bridge: at least leave a note
            self.bridge.progress(task_id, note)
        fn = getattr(self.queue, "delete_sizing_outcome", None)
        if callable(fn):
            try:
                fn(task_id)
            except Exception as exc:  # pragma: no cover - bookkeeping is best effort
                log.debug("delete_sizing_outcome failed: %s", exc)

    async def _widen_context(self, alert: ForemanEscalation) -> tuple[Any, ...]:
        """Raise the task's context level by one, so its next attempts see more of the code.

        Refused at the widest level, within 10 minutes of the task's last widen
        and within 2 hours of an undo of one.
        """
        task_id = alert.target_entity.task_id
        if not task_id:
            return False, "no task to widen context for", {}
        task, why = self._live_task(task_id)
        if task is None:
            return False, why, {}
        level = self.queue.task_context_level(task_id)
        if level >= self.queue.MAX_CONTEXT_LEVEL:
            return False, f"{task_id} is already at the widest context (level {level})", {}
        refusal = self._widen_refusal(task_id)
        if refusal is not None:
            return False, refusal, {}
        widened = level + 1
        self.queue.set_task_context_level(task_id, widened)
        self.bridge.progress(task_id, f"[Foundry] {task_id} context widened from level {level} to {widened}")
        before = {"task_id": task_id, "level": level, "widened_to": widened}
        return True, f"{task_id} context widened to level {widened}", {"level": widened}, before

    def _widen_refusal(self, task_id: str) -> str | None:
        """Why the task's context may not be widened yet, from the action log; ``None`` when it may.

        A timestamp in the future (clock mismatch) counts as recent; an
        unreadable one is ignored. A manager without an action log has
        nothing to refuse on.
        """
        now = _parse_utc(_utc_now()) or datetime.now(timezone.utc)
        for row in reversed(getattr(self, "action_log", None) or []):
            if row.get("action") != "WIDEN_CONTEXT" or row.get("task_id") != task_id:
                continue
            undone = _parse_utc(row.get("undone_at"))
            if undone is not None and (now - undone).total_seconds() < _WIDEN_UNDO_SUPPRESS_S:
                return f"{task_id} had a context widen undone less than 2 hours ago"
            widened = _parse_utc(row.get("at")) if row.get("applied") else None
            if widened is not None and (now - widened).total_seconds() < _WIDEN_COOLDOWN_S:
                return f"{task_id} had its context widened less than 10 minutes ago"
        return None

    # -------------------------------------------------------------- action log

    def _load_actions(self) -> None:
        fn = getattr(self.queue, "kv_get", None)
        if not callable(fn):
            return
        try:
            raw = fn(_ACTIONS_KEY)
            if isinstance(raw, (str, bytes)):
                raw = json.loads(raw)
            if isinstance(raw, dict):
                self.action_log = [dict(e) for e in raw.get("actions") or [] if isinstance(e, dict)]
                self._action_seq = int(raw.get("seq") or 0)
        except Exception as exc:  # a corrupt log must not stop a restart
            log.warning("floor manager: ignoring unreadable action log: %s", exc)
            self.action_log, self._action_seq = [], 0

    def _save_actions(self) -> None:
        fn = getattr(self.queue, "kv_set", None)
        if not callable(fn):
            return
        del self.action_log[:-ACTION_LOG_MAX]
        try:
            fn(_ACTIONS_KEY, {"seq": self._action_seq, "actions": self.action_log})
        except Exception as exc:  # pragma: no cover - persistence must not stop the line
            log.warning("floor manager: could not save the action log: %s", exc)

    def _log_action(self, alert: ForemanEscalation, outcome: ActionOutcome, before: Mapping[str, Any]) -> dict[str, Any]:
        """Append one applied escalation to the persisted action log."""
        self._action_seq += 1
        undoable, why = True, ""
        if not outcome.applied:
            undoable, why = False, "Nothing was changed."
        elif outcome.action == "RESET_BUILDER_CONTEXT":
            undoable, why = False, "A cleared builder context cannot be restored."
        elif not before:
            undoable, why = False, "Nothing recorded to undo."
        t = alert.target_entity
        entry: dict[str, Any] = {
            "action_id": f"A-{self._action_seq:04d}",
            "alert_id": alert.alert_id,
            "at": _utc_now(),
            "action": outcome.action,
            "task_id": t.task_id,
            "team": (before.get("from_team") if before else None) or self._team_for(alert),
            "agent_id": t.agent_id,
            "category": _val(alert.category),
            "reason": alert.diagnosis,
            "detail": outcome.detail,
            "applied": outcome.applied,
            "params": json.loads(json.dumps(outcome.parameters, default=str)),
            "before": json.loads(json.dumps(dict(before), default=str)),
            "undoable": undoable,
            "undo_reason": why or None,
            "undone_at": None,
            "undone_by": None,
        }
        self.action_log.append(entry)
        self._save_actions()
        return entry

    def actions(self, limit: int = 50) -> list[dict[str, Any]]:
        """The action log, newest first (copies; at most ``limit`` entries)."""
        n = max(0, int(limit))
        return [dict(e) for e in reversed(self.action_log)][:n]

    def undo_action(self, action_id: str, by: str = "user") -> dict[str, Any]:
        """Undo one logged action. ``{ok: True, ...}`` or ``{ok: False, reason}``.

        Idempotent: undoing an action twice returns ``ok`` with
        ``already_undone``. Refuses when the action is not undoable or the
        state has moved on since (the task finished, the tickets were picked
        up, a newer throttle replaced it ...).
        """
        entry = next((e for e in self.action_log if e.get("action_id") == action_id), None)
        if entry is None:
            return {"ok": False, "action_id": action_id, "reason": f"Unknown action {action_id}."}
        base = {"action_id": action_id, "action": entry.get("action")}
        if entry.get("undone_at"):
            return {**base, "ok": True, "already_undone": True, "undone_at": entry["undone_at"],
                    "detail": "Already undone."}
        if not entry.get("undoable"):
            return {**base, "ok": False, "reason": entry.get("undo_reason") or "This action cannot be undone."}
        undo = {
            "RESIZE_ALLOCATION": self._undo_resize,
            "THROTTLE_DISPATCH": self._undo_throttle,
            "PROMOTE_PEER_REVIEWER": self._undo_promote,
            "QUARANTINE_TASK": self._undo_quarantine,
            "REASSIGN_TEAM": self._undo_reassign,
            "RETRY_TASK": self._undo_retry,
            "WIDEN_CONTEXT": self._undo_widen,
        }.get(str(entry.get("action")))
        if undo is None:
            return {**base, "ok": False, "reason": "This action cannot be undone."}
        try:
            ok, detail = undo(entry)
        except Exception as exc:  # undo must never take the line down
            log.warning("floor manager: undo of %s failed: %s", action_id, exc)
            ok, detail = False, f"Undo failed: {exc}"
        if not ok:
            return {**base, "ok": False, "reason": detail}
        entry["undone_at"] = _utc_now()
        entry["undone_by"] = str(by or "user")
        entry["undo_detail"] = detail
        self._save_actions()
        self.save()
        return {**base, "ok": True, "detail": detail, "undone_at": entry["undone_at"], "undone_by": entry["undone_by"]}

    def _live_task(self, task_id: str | None) -> tuple[dict[str, Any] | None, str]:
        task = self.queue.get_task(task_id) if task_id else None
        if task is None:
            return None, f"Task {task_id} no longer exists."
        state = _val(task.get("state"))
        if state in _TERMINAL:
            return None, f"{task_id} is already {state}."
        return task, ""

    def _undo_resize(self, entry: Mapping[str, Any]) -> tuple[bool, str]:
        before = entry["before"]
        task_id, previous = before.get("task_id"), int(before.get("builders") or 0)
        task, why = self._live_task(task_id)
        if task is None:
            return False, why
        now = self.allocations.get(task_id) or int(task.get("allocated_builders") or 0)
        if now != int(entry["params"].get("builders") or 0):
            return False, f"{task_id} was resized again since (now {now} builder(s))."
        if previous < 1:
            return False, "The earlier allocation is unknown."
        self.queue.resize(task_id, previous)
        self.allocations[task_id] = previous
        return True, f"{task_id} back to {previous} builder(s)"

    def _undo_throttle(self, entry: Mapping[str, Any]) -> tuple[bool, str]:
        if self.limiter is None:
            return False, "No rate limiter."
        if not getattr(self.limiter, "is_throttled", False):
            return False, "The throttle already ended."
        factor = float(entry["params"].get("factor") or 0)
        current = float(getattr(self.limiter, "throttle_factor", factor))
        if abs(current - factor) > 1e-9:
            return False, "A newer throttle replaced this one."
        self.limiter.throttle(1.0, 0)
        return True, "dispatch back to full speed"

    def _undo_promote(self, entry: Mapping[str, Any]) -> tuple[bool, str]:
        before = entry["before"]
        team_id, agent_id = before.get("team_id"), before.get("agent_id")
        grant = self.peer_reviewers.get(str(team_id))
        if grant is None or grant.agent_id != agent_id or not grant.active():
            return False, f"{agent_id} is no longer a temporary reviewer."
        hook = getattr(self.teams.get(str(team_id)), "demote_peer_reviewer", None)
        if not callable(hook):
            return False, f"Team {team_id} cannot end a temporary reviewer early."
        if not hook(agent_id):
            return False, f"{agent_id} is no longer a temporary reviewer."
        del self.peer_reviewers[str(team_id)]
        return True, f"{agent_id} builds again"

    def _undo_quarantine(self, entry: Mapping[str, Any]) -> tuple[bool, str]:
        before = entry["before"]
        task_id = before.get("task_id")
        task = self.queue.get_task(task_id) if task_id else None
        if task is None:
            return False, f"Task {task_id} no longer exists."
        if _val(task.get("state")) != "quarantined":
            return False, f"{task_id} is {_val(task.get('state'))}, not quarantined."
        tickets = dict(before.get("tickets") or {})
        current = {t.ticket_id: t for t in self.queue.tickets_for_task(task_id)}
        if any(tid not in current or current[tid].status != "blocked" for tid in tickets):
            return False, f"{task_id}'s tickets changed since the quarantine."
        specs = {tid: {"attempt": spec.get("attempt", current[tid].attempt)} for tid, spec in tickets.items()}
        reopened = self.queue.reopen_quarantined(task_id, specs, "quarantine undone")
        self._back_on_line(task_id, f"[Foundry] {task_id} back on the line (quarantine undone)")
        return True, f"{task_id} back on the line ({len(reopened)} ticket(s) open)"

    def _undo_reassign(self, entry: Mapping[str, Any]) -> tuple[bool, str]:
        before = entry["before"]
        task_id, back, there = before.get("task_id"), before.get("from_team"), before.get("to_team")
        task, why = self._live_task(task_id)
        if task is None:
            return False, why
        if task.get("team_id") != there:
            return False, f"{task_id} is on {task.get('team_id')} now, not {there}."
        moved = set(before.get("tickets") or [])
        still = [t.ticket_id for t in self.queue.tickets(task_id=task_id, status="open")
                 if t.ticket_id in moved and t.team_id == there]
        if not still:
            return False, f"Team {there} already picked up {task_id}'s tickets."
        self.queue.move_tickets(task_id, str(back), still)
        self._set_directive_team(task_id, str(back))
        self.bridge.progress(task_id, f"[Foundry] {task_id} moved back to {back} (undo)", team_id=back)
        if len(still) < len(moved):
            return True, (f"{task_id} partly moved back to {back} ({len(still)} of {len(moved)} ticket(s);"
                          f" {there} already started the rest)")
        return True, f"{task_id} moved back to {back} ({len(still)} ticket(s))"

    def _undo_retry(self, entry: Mapping[str, Any]) -> tuple[bool, str]:
        before = entry["before"]
        task_id = before.get("task_id")
        task, why = self._live_task(task_id)
        if task is None:
            return False, why
        tickets = {t.ticket_id: t for t in self.queue.tickets_for_task(task_id)}
        if any(tickets.get(tid) is not None and tickets[tid].status == "done" for tid in before.get("tickets") or {}):
            return False, f"{task_id} already finished work after the retry."
        reason = str(before.get("reason") or "retry undone")
        self.queue.quarantine(task_id, reason)
        self._report_quarantine(task_id, reason)
        return True, f"{task_id} quarantined again"

    def _undo_widen(self, entry: Mapping[str, Any]) -> tuple[bool, str]:
        before = entry["before"]
        task_id = before.get("task_id")
        level = int(before["level"])
        refusal = self.queue.restore_task_context_level(task_id, int(before["widened_to"]), level)
        if refusal is not None:
            return False, refusal
        self.bridge.progress(task_id, f"[Foundry] {task_id} context back to level {level} (undone)")
        return True, f"{task_id} context back to level {level}"

    async def _no_action(self, alert: ForemanEscalation) -> tuple[bool, str, dict[str, Any]]:
        return True, "no action", {}
