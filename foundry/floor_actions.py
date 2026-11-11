"""The Foreman's escalations and their action log: the Foreman only logs and never acts.

Split out of :mod:`foundry.floor_manager`: :class:`ForemanActionsMixin` holds
the methods, and ``FloorManager`` inherits them and sets their attributes.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from foundry.floor_helpers import (
    ACTION_LOG_MAX,
    ActionOutcome,
    _ACTIONS_KEY,
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
    """``FloorManager``'s Foreman escalation log."""

    if TYPE_CHECKING:
        # Set in FloorManager.__init__ or defined on FloorManager; declared for the type checker only.
        queue: Any
        bridge: Any
        directives: dict[str, TaskSizingDirective]
        orders: dict[str, Any]
        outcomes: list[ActionOutcome]
        action_log: list[dict[str, Any]]
        _action_seq: int
        _reported_quarantine: set[str]
        sizing_meta: dict[str, dict[str, Any]]
        applied_alerts: dict[str, ActionOutcome]

        def save(self) -> None: ...

        def _prune_later(self) -> None: ...

        def _start_partial(self, task_id: str, reason: str) -> Any: ...

    # ------------------------------------------------------------ escalations

    async def apply_escalation(
        self, alert: ForemanEscalation | Mapping[str, Any], *, act: bool = True
    ) -> ActionOutcome:
        """Log the escalation (validated first) and report it; its action is never carried out.

        Idempotent per ``alert_id``: an alert that was already handled (in this
        run or before a restart) returns the earlier outcome without storing
        or posting it again.

        ``act`` is accepted and ignored. The outcome says the action was not
        carried out (``applied`` False) and is stored, logged and posted, so
        the alert stays visible.
        """
        if not isinstance(alert, ForemanEscalation):
            alert = ForemanEscalation.model_validate(dict(alert))
        earlier = self.applied_alerts.get(alert.alert_id) or self._stored_outcome(alert.alert_id)
        if earlier is not None:
            self.applied_alerts[alert.alert_id] = earlier
            return earlier
        action = PrescribedAction(_val(alert.prescribed_action))
        detail = f"logged only (FOUNDRY_FOREMAN_ACTIONS=log): {action.value}"
        outcome = ActionOutcome(alert.alert_id, action.value, False, detail, {})
        self.outcomes.append(outcome)
        self.applied_alerts[alert.alert_id] = outcome
        if action != PrescribedAction.NONE:
            self._log_action(alert, outcome, {})
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
        """Always refused: nothing the Foreman logs was carried out, so there is nothing to undo."""
        return {"ok": False, "reason": "undo is gone: the Foreman only logs and never acts"}
