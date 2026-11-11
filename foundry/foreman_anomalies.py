"""Anomaly detection and escalation for the Shop Foreman.

Holds :class:`AnomalyMixin`, the :class:`Anomaly` dataclass and the constants
only that code uses. Split out of :mod:`foundry.shop_foreman`, which imports
these names back; :class:`~foundry.shop_foreman.ShopForeman` inherits the mixin.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from foundry.foreman_helpers import (
    _ACTIVE_TICKET,
    _TERMINAL,
    _UNKNOWN_TASK,
    _clip,
    _decode_json,
    _first_sentences,
    _jsonable,
    _parse_ts,
    _reply_tokens,
)
from foundry.schemas import (
    MAX_DIAGNOSIS_SENTENCES,
    EscalationCategory,
    ForemanEscalation,
    PrescribedAction,
    Severity,
    TaskState,
    count_sentences,
    new_alert_id,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from foundry.config import FoundryConfig
    from foundry.gemma_client import GemmaClient, GemmaReply
    from foundry.orchestrator_queue import FoundryQueue
    from foundry.telemetry import Telemetry

# Same logger as before the split, so existing log filters keep matching.
log = logging.getLogger("foundry.shop_foreman")

#: Maximum length of a Gemma-written escalation diagnosis.
MAX_DIAGNOSIS_CHARS: int = 300

#: A quarantined task is considered for RETRY_TASK only after this many seconds
#: (give the infrastructure a moment) and only within ``_RETRY_MAX_AGE_S``.
_RETRY_MIN_WAIT_S = 60.0
_RETRY_MAX_AGE_S = 6 * 3600.0
#: After the user undoes one of these actions, the Foreman does not prescribe it
#: again for the same task for this long (seconds).
_UNDO_SUPPRESS_S = 2 * 3600.0
_UNDO_SUPPRESSED = frozenset({
    "REASSIGN_TEAM", "RESIZE_ALLOCATION", "THROTTLE_DISPATCH", "PROMOTE_PEER_REVIEWER", "WIDEN_CONTEXT",
})
#: kv key the Floor Manager stores its action log under (read only here).
_ACTIONS_KV_KEY = "floor_manager.actions"
#: Gemma decisions (REASSIGN/RETRY choices) asked per ``evaluate_line_anomaly`` call.
_MAX_DECISIONS_PER_EVAL = 3
#: Quarantine reasons that are real build/review failures: never retried.
_REAL_FAILURE_MARKERS = (
    "retry limit", "failed its gates", "gate fail", "reviewer rejected", "rejected", "duplicate of",
    "could not be recovered",
)
#: Quarantine reasons that look transient (infrastructure, network, limits).
_TRANSIENT_MARKERS = (
    "infrastructure", "timed out", "timeout", "connection", "network", "rate limit", "rate-limit",
    "429", "502", "503", "504", "overloaded", "unavailable", "temporar", "econnreset", "broken pipe",
)
_DECISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "prescribed_action": {"type": "string"},
        "diagnosis": {"type": "string"},
    },
    "required": ["prescribed_action", "diagnosis"],
}


def classify_quarantine(reason: str | None) -> str:
    """``"real"``, ``"transient"`` or ``"unclear"`` for a quarantine reason.

    Real build failures win over transient words (``"retry limit reached
    (timeout)"`` is real: the builder used up its attempts).
    """
    text = " ".join(str(reason or "").lower().split())
    if not text:
        return "unclear"
    if any(m in text for m in _REAL_FAILURE_MARKERS):
        return "real"
    if any(m in text for m in _TRANSIENT_MARKERS):
        return "transient"
    return "unclear"


#: Failed gate output showing the builder could not see code it needed.
_VISIBILITY_FAILURE_RE = re.compile(
    r"\bNameError\b|\bBLOCKED\b|\bF821\b|[Uu]ndefined name\b|\bis not defined\b|cannot import name"
)
#: Visibility failures on one task (at its current level) before its context is widened.
_WIDEN_AFTER_FAILURES = 2


@dataclass
class Anomaly:
    """A deterministically detected line anomaly, before it becomes an escalation."""

    category: EscalationCategory
    severity: Severity
    action: PrescribedAction
    team_id: str | None = None
    task_id: str | None = None
    agent_id: str | None = None
    dwell_seconds: float = 0.0
    gate_failure_count: int = 0
    action_parameters: dict[str, Any] = field(default_factory=dict)
    extra_metrics: dict[str, Any] = field(default_factory=dict)
    fallback_diagnosis: str = ""
    #: Actions Gemma may choose between (``action`` is the deterministic pick and
    #: is always among them); empty means Gemma only writes the diagnosis.
    choices: tuple[PrescribedAction, ...] = ()

    @property
    def key(self) -> str:
        """Identity used for the re-alert cooldown."""
        base = f"{self.category.value}|{self.team_id}|{self.task_id}|{self.agent_id}"
        # A widen shares LINE_JAM with quarantine: its cooldown must not hold a quarantine back.
        return f"{base}|widen" if self.action == PrescribedAction.WIDEN_CONTEXT else base


class AnomalyMixin:
    """Anomaly detection and escalation of :class:`~foundry.shop_foreman.ShopForeman`."""

    if TYPE_CHECKING:  # pragma: no cover - what the mixin reaches through ``self``
        gemma: GemmaClient | None
        queue: FoundryQueue
        telemetry: Telemetry | None
        config: FoundryConfig
        cooldown_s: float
        peer_review_minutes: float
        throttle_seconds: float
        actions_provider: Any
        last_source: dict[str, str]
        _retry_prescribed: set[str]
        _retry_declined: set[str]
        _raised: dict[str, datetime]
        _alert_day: str | None
        _alert_seq: int

        def _now(self) -> datetime: ...

        def _save(self, task_id: str | None, source: str, tokens: int) -> None: ...

        async def _chat(
            self,
            messages: list[dict[str, Any]],
            *,
            hook: str,
            json_schema: dict[str, Any] | None = None,
            tools: list[dict[str, Any]] | None = None,
            temperature: float = 0.1,
            timeout: float | None = None,
        ) -> GemmaReply | None: ...

        def _sql(self, sql: str, params: Sequence[Any] = ()) -> list[Any]: ...

        def _ticket_gate_failures(self, task_id: str) -> dict[str, int]: ...

        def _review_wait_items(self) -> dict[str, dict[str, Any]]: ...

        def _claimed_ages(self, team_id: str | None, now: datetime) -> list[tuple[Any, float]]: ...

        def _free_slots(self, team_id: str) -> list[int]: ...

        def _top_agent_last_minute(self) -> str | None: ...

    # ---------------------------------------------------------------- anomalies
    def _gate_fail_limit(self) -> int:
        """The effective LINE_JAM limit (explicit threshold, else ``max_retries + 1``)."""
        effective = getattr(self.config, "effective_gate_fail_quarantine", None)
        if callable(effective):
            return int(effective())
        explicit = getattr(self.config.thresholds, "gate_fail_quarantine", None)
        if explicit is not None:
            return int(explicit)
        return int(getattr(self.config, "max_retries", 3)) + 1

    def detect_anomalies(self) -> list[Anomaly]:
        """Deterministically detect every current anomaly, most urgent first.

        Order: LINE_JAM, WIDEN_CONTEXT, TOKEN_VELOCITY_SPIKE, AGENT_STALLED,
        REVIEW_BOTTLENECK, CAPACITY_REBALANCE. Thresholds come from
        ``config.thresholds``.
        """
        th = self.config.thresholds
        now = self._now()
        found: list[Anomaly] = []
        tasks = {t["task_id"]: t for t in self.queue.all_tasks() if _jsonable(t["state"]) not in _TERMINAL}

        # LINE_JAM: one ticket failed its gates repeatedly (per ticket, like the retry policy) -> quarantine.
        # Default limit is max_retries + 1, so a ticket with attempts left is never quarantined here.
        jam_limit = self._gate_fail_limit()
        for tid, task in tasks.items():
            per_ticket = self._ticket_gate_failures(tid)
            if not per_ticket:
                continue
            subtask_id, n = max(per_ticket.items(), key=lambda kv: (kv[1], kv[0]))
            if n < jam_limit:
                continue
            ticket = next((t for t in self.queue.tickets_for_task(tid) if t.subtask_id == subtask_id), None)
            ticket_id = ticket.ticket_id if ticket is not None else f"{tid}:{subtask_id}"
            found.append(
                Anomaly(
                    EscalationCategory.LINE_JAM,
                    Severity.CRITICAL if n > jam_limit else Severity.HIGH,
                    PrescribedAction.QUARANTINE_TASK,
                    team_id=task["team_id"],
                    task_id=tid,
                    agent_id=ticket.claimed_by if ticket is not None else None,
                    gate_failure_count=n,
                    action_parameters={"reason": f"ticket {ticket_id} failed its gates {n} times "
                                                 f"(limit {jam_limit})",
                                       "gate_failure_count": n, "ticket_id": ticket_id},
                    extra_metrics={"threshold": jam_limit, "ticket_id": ticket_id},
                    fallback_diagnosis=f"Ticket {ticket_id} failed its deterministic gates {n} times and is on its "
                                       "last attempt. Quarantine the task before more builder calls are spent.",
                )
            )

        found.extend(self._widen_anomalies(tasks))

        # TOKEN_VELOCITY_SPIKE: burn above the configured share of the TPM cap.
        if self.telemetry is not None:
            current = float(self.telemetry.current_tpm())
            cap = float(self.config.tpm)
            limit = cap * th.tpm_spike_ratio
            if current > limit:
                agent = self._top_agent_last_minute()
                factor = round(min(0.9, max(0.1, (limit * 0.8) / current)), 2)
                found.append(
                    Anomaly(
                        EscalationCategory.TOKEN_VELOCITY_SPIKE,
                        Severity.CRITICAL if current >= cap else Severity.HIGH,
                        PrescribedAction.THROTTLE_DISPATCH,
                        team_id=agent.rsplit("-", 1)[0] if agent and "-" in agent else "floor",
                        agent_id=agent,
                        action_parameters={"factor": factor, "seconds": self.throttle_seconds},
                        extra_metrics={"tpm_cap": cap, "spike_threshold_tpm": limit},
                        fallback_diagnosis=f"Claude burn is {current:.0f} tokens/min, above {th.tpm_spike_ratio:.0%} "
                                           f"of the {cap:.0f} cap. Throttle dispatch to stay under the subscription limit.",
                    )
                )

        # AGENT_STALLED: a claimed ticket with no activity for too long.
        for ticket, secs in self._claimed_ages(None, now):
            if secs > th.stall_s and ticket.task_id in tasks:
                agent = ticket.claimed_by or f"{ticket.team_id}-{ticket.slot}"
                found.append(
                    Anomaly(
                        EscalationCategory.AGENT_STALLED,
                        Severity.HIGH if secs > 2 * th.stall_s else Severity.MEDIUM,
                        PrescribedAction.RESET_BUILDER_CONTEXT,
                        team_id=ticket.team_id,
                        task_id=ticket.task_id,
                        agent_id=agent,
                        dwell_seconds=secs,
                        gate_failure_count=self._ticket_gate_failures(ticket.task_id).get(ticket.subtask_id, 0),
                        action_parameters={"agent_id": agent, "ticket_id": ticket.ticket_id, "slot": ticket.slot},
                        extra_metrics={"threshold_s": th.stall_s},
                        fallback_diagnosis=f"Builder {agent} has shown no activity on {ticket.ticket_id} for {secs:.0f}s. "
                                           "Reset its context and reopen the ticket with a fresh prompt.",
                    )
                )

        # REVIEW_BOTTLENECK: a submission waiting too long for its reviewer (per-submission dwell).
        waits = self._review_wait_items()
        for tid, info in sorted(waits.items(), key=lambda kv: -kv[1]["dwell_seconds"]):
            secs = float(info["dwell_seconds"])
            if secs > th.review_dwell_s and tid in tasks:
                team = tasks[tid]["team_id"]
                free = self._free_slots(team)
                params: dict[str, Any] = {"minutes": self.peer_review_minutes}
                if free:
                    params["slot"] = free[-1]
                if info.get("ticket_id"):
                    params["ticket_id"] = info["ticket_id"]
                found.append(
                    Anomaly(
                        EscalationCategory.REVIEW_BOTTLENECK,
                        Severity.HIGH if secs > 2 * th.review_dwell_s else Severity.MEDIUM,
                        PrescribedAction.PROMOTE_PEER_REVIEWER,
                        team_id=team,
                        task_id=tid,
                        dwell_seconds=secs,
                        gate_failure_count=max(self._ticket_gate_failures(tid).values(), default=0),
                        action_parameters=params,
                        extra_metrics={"threshold_s": th.review_dwell_s},
                        fallback_diagnosis=f"A submission of {tid} has waited {secs:.0f}s for review on team {team}. "
                                           "Promote an idle builder to peer reviewer for a while.",
                    )
                )

        # CAPACITY_REBALANCE: idle builders while tickets queue up behind busy slots.
        idle = self.telemetry.idle_counts() if self.telemetry is not None else {}
        for tid, task in tasks.items():
            team = task["team_id"]
            tickets = self.queue.tickets_for_task(tid)
            active = [t for t in tickets if t.status in _ACTIVE_TICKET]
            stacked = len(active) - len({t.slot for t in active})
            alloc = int(task.get("allocated_builders") or 0)
            free = len(self._free_slots(team))  # type: ignore[assignment]
            idle_n = int(idle.get(team, free))  # type: ignore[arg-type]
            if stacked <= 0 or idle_n <= 0 or alloc >= self.config.builders_per_team:
                continue
            new_n = min(self.config.builders_per_team, alloc + min(stacked, idle_n))
            if new_n <= alloc:
                continue
            found.append(
                Anomaly(
                    EscalationCategory.CAPACITY_REBALANCE,
                    Severity.LOW,
                    PrescribedAction.RESIZE_ALLOCATION,
                    team_id=team,
                    task_id=tid,
                    action_parameters={"builders": new_n, "from": alloc},
                    extra_metrics={"idle_builders": idle_n, "stacked_tickets": stacked},
                    fallback_diagnosis=f"Team {team} has {idle_n} idle builder(s) while {stacked} ticket(s) of {tid} "
                                       f"wait behind busy slots. Resize the allocation from {alloc} to {new_n}.",
                )
            )

        found.extend(self._reassign_anomalies(tasks, now, idle))
        found.extend(self._retry_anomalies(now))
        return found

    def _idle_teams(self, exclude: str, idle: Mapping[str, int]) -> list[str]:
        """Teams with no open or claimed ticket (and not reported as 0 idle), most idle first."""
        out = []
        for team in self.config.teams:
            if team == exclude:
                continue
            if any(t.status in _ACTIVE_TICKET for t in self.queue.tickets(team_id=team)):
                continue
            if team in idle and int(idle[team]) <= 0:
                continue
            out.append(team)
        order = {t: k for k, t in enumerate(self.config.teams)}
        return sorted(out, key=lambda t: (-int(idle.get(t, self.config.builders_per_team)), order[t]))

    def _reassign_anomalies(self, tasks: Mapping[str, Any], now: datetime,
                            idle: Mapping[str, int]) -> list[Anomaly]:
        """REASSIGN_TEAM: open tickets stuck behind busy slots of a full team while another team idles."""
        th = self.config.thresholds
        found: list[Anomaly] = []
        claimed_to: set[str] = set()
        for tid, task in tasks.items():
            team = task["team_id"]
            if self._free_slots(team):
                continue  # the team has room: RESIZE_ALLOCATION / normal dispatch handles it
            team_tickets = self.queue.tickets(team_id=team)
            busy_slots = {t.slot for t in team_tickets if t.status == "claimed"}
            stuck = []
            for t in team_tickets:
                if t.task_id != tid or t.status != "open" or t.slot not in busy_slots:
                    continue
                since = _parse_ts(t.updated_at) or _parse_ts(t.created_at)
                if since is not None and (now - since).total_seconds() > th.stall_s:
                    stuck.append((t, (now - since).total_seconds()))
            if not stuck:
                continue
            targets = [x for x in self._idle_teams(team, idle) if x not in claimed_to]
            if not targets:
                continue
            to_team = targets[0]
            claimed_to.add(to_team)  # one task per idle team per pass
            waited = max(s for _, s in stuck)
            ids = [t.ticket_id for t, _ in sorted(stuck, key=lambda x: x[0].ticket_id)]
            found.append(
                Anomaly(
                    EscalationCategory.CAPACITY_REBALANCE,
                    Severity.HIGH if waited > 2 * th.stall_s else Severity.MEDIUM,
                    PrescribedAction.REASSIGN_TEAM,
                    team_id=team,
                    task_id=tid,
                    dwell_seconds=waited,
                    action_parameters={"from_team": team, "to_team": to_team, "ticket_ids": ids,
                                       "waited_s": round(waited, 1)},
                    extra_metrics={"threshold_s": th.stall_s, "stuck_tickets": len(ids), "to_team": to_team},
                    fallback_diagnosis=f"{len(ids)} ticket(s) of {tid} have waited {waited / 60:.0f} min behind busy "
                                       f"builders on team {team} while team {to_team} is idle. "
                                       f"Move them to team {to_team}.",
                    choices=(PrescribedAction.REASSIGN_TEAM, PrescribedAction.NONE),
                )
            )
        return found

    def _action_log(self) -> list[Mapping[str, Any]]:
        """The Floor Manager's action log: ``actions_provider``, ``queue.actions`` and the stored kv copy."""
        rows: list[Mapping[str, Any]] = []
        for source in (self.actions_provider, getattr(self.queue, "actions", None)):
            if not callable(source):
                continue
            try:
                try:
                    got = source(limit=1000)
                except TypeError:
                    got = source()
            except Exception as exc:  # noqa: BLE001
                log.debug("reading the action log failed: %s", exc)
                continue
            rows.extend(r for r in got or [] if isinstance(r, Mapping))
        kv_get = getattr(self.queue, "kv_get", None)
        if callable(kv_get):
            try:
                raw = kv_get(_ACTIONS_KV_KEY)
                if isinstance(raw, (str, bytes)):
                    raw = json.loads(raw)
                if isinstance(raw, Mapping):
                    rows.extend(r for r in raw.get("actions") or [] if isinstance(r, Mapping))
            except Exception as exc:  # noqa: BLE001
                log.debug("reading the stored action log failed: %s", exc)
        return rows

    def _undone_recently(self, anomaly: Anomaly, now: datetime) -> bool:
        """True when the user undid the same action for the same target within ``_UNDO_SUPPRESS_S``.

        Applies to REASSIGN_TEAM, RESIZE_ALLOCATION, THROTTLE_DISPATCH, WIDEN_CONTEXT and
        PROMOTE_PEER_REVIEWER (a task-less THROTTLE matches any undone throttle).
        QUARANTINE_TASK needs new gate evidence instead (see ``_ticket_gate_failures``).
        """
        if anomaly.action.value not in _UNDO_SUPPRESSED:
            return False
        for row in self._action_log():
            if str(row.get("action")) != anomaly.action.value or not row.get("undone_at"):
                continue
            if anomaly.task_id and row.get("task_id") != anomaly.task_id:
                continue
            undone = _parse_ts(row.get("undone_at"))
            if undone is None or (now - undone).total_seconds() < _UNDO_SUPPRESS_S:
                return True  # a clock mismatch (undo "in the future") also counts as recent
        return False

    def _retried_tasks(self) -> set[str]:
        """Tasks a RETRY_TASK was already prescribed or applied for (memory, escalations, action log)."""
        done = set(self._retry_prescribed)
        try:
            for e in self.queue.escalations():
                if str(e.get("prescribed_action")) == PrescribedAction.RETRY_TASK.value:
                    tid = (e.get("target_entity") or {}).get("task_id")
                    if tid:
                        done.add(str(tid))
        except Exception as exc:  # noqa: BLE001
            log.debug("reading escalations failed: %s", exc)
        for row in self._action_log():
            if str(row.get("action")) == PrescribedAction.RETRY_TASK.value and row.get("task_id"):
                done.add(str(row["task_id"]))
        return done

    def _visibility_failures(self, task_id: str) -> int:
        """Failed gate results of a task showing missing code, since its context level last changed.

        Reads the output stored in ``gate_results``; tickets already done are
        left out, and so are failures from before the last widen or undo.
        """
        key = f"{self.queue.CONTEXT_LEVEL_KV_PREFIX}{task_id}"
        changed = self._sql("SELECT updated_at FROM kv WHERE key=?", (key,))
        sql = "SELECT subtask_id, stdout, stderr FROM gate_results WHERE task_id=? AND passed=0"
        params: list[Any] = [task_id]
        if changed:
            sql += " AND ts>?"
            params.append(changed[0][0])
        done = {t.subtask_id for t in self.queue.tickets_for_task(task_id) if t.status == "done"}
        return sum(
            1 for sub, out, err in self._sql(sql, params)
            if sub not in done and _VISIBILITY_FAILURE_RE.search(f"{out or ''}\n{err or ''}")
        )

    def _widen_anomalies(self, tasks: Mapping[str, Any]) -> list[Anomaly]:
        """WIDEN_CONTEXT: tasks whose builders repeatedly could not see the code they needed."""
        found: list[Anomaly] = []
        for tid, task in tasks.items():
            level = self.queue.task_context_level(tid)
            if level >= self.queue.MAX_CONTEXT_LEVEL:
                continue
            n = self._visibility_failures(tid)
            if n < _WIDEN_AFTER_FAILURES:
                continue
            found.append(
                Anomaly(
                    EscalationCategory.LINE_JAM,
                    Severity.MEDIUM,
                    PrescribedAction.WIDEN_CONTEXT,
                    team_id=task["team_id"],
                    task_id=tid,
                    gate_failure_count=n,
                    action_parameters={"level": level + 1, "from": level},
                    extra_metrics={"threshold": _WIDEN_AFTER_FAILURES, "context_level": level},
                    fallback_diagnosis=f"{tid} failed {n} times because its builder could not see code it needed. "
                                       f"Widen its context from level {level} to {level + 1}.",
                )
            )
        return found

    def _retry_anomalies(self, now: datetime) -> list[Anomaly]:
        """RETRY_TASK: recently quarantined tasks whose reason is transient (or unclear, for Gemma)."""
        try:
            quarantined = self.queue.tasks_in_state(TaskState.QUARANTINED)
        except Exception as exc:  # noqa: BLE001
            log.debug("reading quarantined tasks failed: %s", exc)
            return []
        if not quarantined:
            return []
        retried = self._retried_tasks()
        found: list[Anomaly] = []
        for task in quarantined:
            tid = str(task["task_id"])
            if tid in retried or tid in self._retry_declined:
                continue
            since = _parse_ts(task.get("state_changed_at")) or _parse_ts(task.get("updated_at"))
            if since is None:
                continue
            age = (now - since).total_seconds()
            if age < _RETRY_MIN_WAIT_S or age > _RETRY_MAX_AGE_S:
                continue
            if not any(t.status == "blocked" for t in self.queue.tickets_for_task(tid)):
                continue  # nothing to re-open (e.g. sizing failed: sizing re-runs on restart)
            reason = str(task.get("quarantine_reason") or "")
            kind = classify_quarantine(reason)
            if kind == "real" or (kind == "unclear" and self.gemma is None):
                continue
            transient = kind == "transient"
            short = _clip(" ".join(reason.split()) or "no reason recorded", 120)
            found.append(
                Anomaly(
                    EscalationCategory.LINE_JAM,
                    Severity.MEDIUM,
                    PrescribedAction.RETRY_TASK if transient else PrescribedAction.NONE,
                    team_id=task.get("team_id"),
                    task_id=tid,
                    dwell_seconds=age,
                    action_parameters={"reason": reason[:300], "transient": True,
                                       "quarantined_s": round(age, 1), "source": "heuristic"},
                    extra_metrics={"quarantine_kind": kind},
                    fallback_diagnosis=f"{tid} was quarantined for a transient reason ({short}). "
                                       "Retry it once with fresh attempts.",
                    choices=(PrescribedAction.RETRY_TASK, PrescribedAction.NONE),
                )
            )
        return found

    def _next_alert_id(self, now: datetime) -> str:
        """Issue the next per-day ``ALT-YYYYMMDD-NNN`` id and reserve it in the queue db (D12).

        The sequence is seeded from every id already reserved or recorded, and
        each candidate is persisted with ``queue.reserve_alert_id`` when it is
        issued; a taken id (another Foreman, a restart) moves on to the next
        number, so ids are never reused even if the alert is never applied.
        """
        day = now.strftime("%Y%m%d")
        prefix = f"ALT-{day}-"
        if self._alert_day != day:
            self._alert_day, self._alert_seq = day, 0
            stored: list[str] = []
            try:
                lister = getattr(self.queue, "alert_ids", None)
                if callable(lister):
                    stored = [str(a) for a in lister(prefix)]
                else:
                    stored = [str(e.get("alert_id", "")) for e in self.queue.escalations()]
            except Exception as exc:  # noqa: BLE001
                log.debug("reading stored alert ids failed: %s", exc)
            for aid in stored:
                if aid.startswith(prefix) and aid[len(prefix):].isdigit():
                    self._alert_seq = max(self._alert_seq, int(aid[len(prefix):]))
        reserve = getattr(self.queue, "reserve_alert_id", None)
        while True:
            self._alert_seq += 1
            if self._alert_seq > 999:
                raise RuntimeError("more than 999 escalations today")
            alert_id = new_alert_id(self._alert_seq, now)
            if not callable(reserve) or reserve(alert_id):
                return alert_id

    def _valid_diagnosis(self, text: str) -> str | None:
        candidate = " ".join((text or "").split()).strip().strip('"')
        if not candidate or len(candidate) > MAX_DIAGNOSIS_CHARS:
            return None
        if count_sentences(candidate) > MAX_DIAGNOSIS_SENTENCES:
            return None
        return candidate

    async def evaluate_line_anomaly(self) -> ForemanEscalation | None:
        """Detect the most urgent anomaly and return it as a validated escalation.

        Detection is deterministic (:meth:`detect_anomalies`); anomalies
        raised within ``cooldown_s`` for the same target are skipped. For most
        anomalies Gemma only writes the ``diagnosis`` (<= 2 sentences,
        validated); otherwise a fixed diagnosis is used. For REASSIGN_TEAM and
        RETRY_TASK candidates (``Anomaly.choices``) Gemma also picks the action
        from those choices (``NONE`` = leave it: the candidate is skipped and
        the next one is tried); an invalid or missing choice keeps the
        deterministic one. RETRY_TASK is never prescribed twice for a task.
        Returns ``None`` when the line is healthy.
        """
        now = self._now()
        decisions = 0
        for anomaly in self.detect_anomalies():
            last = self._raised.get(anomaly.key)
            if last is not None and (now - last).total_seconds() < self.cooldown_s:
                continue
            if self._undone_recently(anomaly, now):
                continue  # the user undid this very action: respect that for a while
            if anomaly.choices and self.gemma is not None:
                if decisions >= _MAX_DECISIONS_PER_EVAL:
                    if anomaly.action == PrescribedAction.NONE:
                        continue  # needs Gemma's decision; ask on a later pass
                    alert = await self._escalate(anomaly, now, ask_gemma=False)
                else:
                    decisions += 1
                    alert = await self._escalate(anomaly, now, ask_gemma=True)
            elif anomaly.action == PrescribedAction.NONE:
                continue  # an undecided candidate without Gemma: nothing to do
            else:
                alert = await self._escalate(anomaly, now, ask_gemma=self.gemma is not None)
            if alert is not None:
                return alert
        return None

    def _metrics_and_target(self, anomaly: Anomaly) -> tuple[dict[str, Any], dict[str, Any]]:
        try:
            queue_depth = int(self.queue.summary().get("queue_depth", 0))
        except Exception:  # noqa: BLE001
            queue_depth = 0
        current_tpm = float(self.telemetry.current_tpm()) if self.telemetry is not None else 0.0
        metrics = {
            "queue_depth": queue_depth,
            "dwell_seconds": round(float(anomaly.dwell_seconds), 1),
            "gate_failure_count": int(anomaly.gate_failure_count),
            "current_tpm": round(current_tpm, 1),
            **anomaly.extra_metrics,
        }
        target = {k: v for k, v in (("team_id", anomaly.team_id), ("task_id", anomaly.task_id),
                                     ("agent_id", anomaly.agent_id)) if v}
        return metrics, target

    async def _gemma_decision(
        self, anomaly: Anomaly, target: dict[str, Any], metrics: dict[str, Any]
    ) -> tuple[PrescribedAction | None, str | None, int]:
        """Ask Gemma to pick one of ``anomaly.choices`` and write the diagnosis.

        Returns ``(action or None when unusable, diagnosis or None, tokens)``.
        A plain-text reply (no JSON) counts as a diagnosis only.
        """
        options = [c.value for c in anomaly.choices]
        guide = {
            PrescribedAction.REASSIGN_TEAM.value: "move the task's waiting tickets to the idle team",
            PrescribedAction.RETRY_TASK.value: "re-open the quarantined task once with fresh attempts; only when "
                                               "the quarantine was caused by infrastructure or a transient "
                                               "problem (timeouts, network, rate limits), never for repeated "
                                               "real build or review failures",
            PrescribedAction.NONE.value: "leave it as it is",
        }
        messages = [
            {
                "role": "system",
                "content": "You are the Shop Foreman of a software factory. Decide what to do about this "
                           "production-line alert. Reply with JSON only: "
                           '{"prescribed_action": one of ' + json.dumps(options) + ', "diagnosis": "at most '
                           'two short sentences (max 300 characters)"}. Meaning of the actions: '
                           + "; ".join(f"{o} = {guide.get(o, o)}" for o in options) + ".",
            },
            {
                "role": "user",
                "content": json.dumps(
                    {"category": anomaly.category.value, "severity": anomaly.severity.value,
                     "target": target, "metrics": metrics, "action_parameters": anomaly.action_parameters,
                     "suggested_action": anomaly.action.value, "facts": anomaly.fallback_diagnosis},
                    default=str,
                ),
            },
        ]
        schema = {
            **_DECISION_SCHEMA,
            "properties": {**_DECISION_SCHEMA["properties"], "prescribed_action": {"type": "string", "enum": options}},
        }
        reply = await self._chat(messages, hook="evaluate_line_anomaly", json_schema=schema, temperature=0.0)
        if reply is None:
            return None, None, 0
        tokens = _reply_tokens(reply, messages)
        try:
            data = _decode_json(reply.text)
        except (ValueError, TypeError):
            data = None
        if not isinstance(data, Mapping):
            return None, self._valid_diagnosis(reply.text or ""), tokens
        raw = str(data.get("prescribed_action") or "").strip().upper().replace("-", "_").replace(" ", "_")
        action = next((c for c in anomaly.choices if c.value == raw), None)
        return action, self._valid_diagnosis(str(data.get("diagnosis") or "")), tokens

    async def _escalate(self, anomaly: Anomaly, now: datetime, *, ask_gemma: bool) -> ForemanEscalation | None:
        """Turn one anomaly into a validated escalation (``None`` when the action ends up NONE)."""
        metrics, target = self._metrics_and_target(anomaly)
        action = anomaly.action
        params = dict(anomaly.action_parameters)
        diagnosis = anomaly.fallback_diagnosis
        source = "heuristic"
        gemma_said_none = False
        if ask_gemma and anomaly.choices:
            chosen, written, tokens = await self._gemma_decision(anomaly, target, metrics)
            if chosen is not None:
                if chosen == PrescribedAction.RETRY_TASK and anomaly.action != PrescribedAction.RETRY_TASK:
                    params["source"] = "gemma"  # Gemma judged an unclear quarantine transient
                gemma_said_none = chosen == PrescribedAction.NONE
                if written is not None:
                    diagnosis = written
                elif chosen != anomaly.action:
                    diagnosis = _CHOICE_DIAGNOSIS.get(chosen, anomaly.fallback_diagnosis).format(
                        task=anomaly.task_id or "the task")
                action, source = chosen, "gemma"
            elif written is not None and action != PrescribedAction.NONE:
                diagnosis, source = written, "gemma"  # plain-text reply: a diagnosis only
            if source == "gemma":
                self._save(anomaly.task_id or _UNKNOWN_TASK, "triage", tokens)
        elif ask_gemma:
            messages = [
                {
                    "role": "system",
                    "content": "You are the Shop Foreman. Write a diagnosis of at most two short sentences "
                               "(max 300 characters) for this production-line alert. Plain text only.",
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {"category": anomaly.category.value, "severity": anomaly.severity.value,
                         "target": target, "metrics": metrics,
                         "prescribed_action": anomaly.action.value, "facts": anomaly.fallback_diagnosis},
                        default=str,
                    ),
                },
            ]
            reply = await self._chat(messages, hook="evaluate_line_anomaly", temperature=0.1)
            if reply is not None:
                written = self._valid_diagnosis(reply.text)
                if written is not None:
                    diagnosis, source = written, "gemma"
                    self._save(anomaly.task_id or _UNKNOWN_TASK, "triage", _reply_tokens(reply, messages))

        if action == PrescribedAction.NONE:
            # Gemma (or the missing decision) says leave it: no alert, and don't ask again soon.
            self._raised[anomaly.key] = now
            if gemma_said_none and PrescribedAction.RETRY_TASK in anomaly.choices and anomaly.task_id:
                self._retry_declined.add(anomaly.task_id)  # Gemma looked at it: don't ask again
            self.last_source["evaluate_line_anomaly"] = source
            return None
        if action == PrescribedAction.RETRY_TASK and anomaly.task_id:
            if anomaly.task_id in self._retried_tasks():
                return None  # never twice (another pass or a restart got there first)

        payload = {
            "alert_id": self._next_alert_id(now),
            "timestamp": now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "severity": anomaly.severity,
            "category": anomaly.category,
            "target_entity": target,
            "metrics_snapshot": metrics,
            "diagnosis": diagnosis,
            "prescribed_action": action,
            "action_parameters": params,
        }
        try:
            alert = ForemanEscalation.model_validate(payload)
        except ValidationError:
            payload["diagnosis"] = _first_sentences(anomaly.fallback_diagnosis, MAX_DIAGNOSIS_SENTENCES)
            source = "heuristic"
            alert = ForemanEscalation.model_validate(payload)
        self._raised[anomaly.key] = now
        if action == PrescribedAction.RETRY_TASK and anomaly.task_id:
            self._retry_prescribed.add(anomaly.task_id)
        self.last_source["evaluate_line_anomaly"] = source
        return alert


#: Diagnosis used when Gemma picked a different action without a usable diagnosis.
_CHOICE_DIAGNOSIS: dict[PrescribedAction, str] = {
    PrescribedAction.RETRY_TASK: "{task} was quarantined for what looks like a transient problem. "
                                 "Retry it once with fresh attempts.",
    PrescribedAction.REASSIGN_TEAM: "{task} is stuck behind busy builders. Move its waiting tickets to an idle team.",
}
