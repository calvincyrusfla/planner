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

import asyncio
import inspect
import json
import logging
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from foundry.schemas import (
    ForemanEscalation,
    PrescribedAction,
    TaskSizingDirective,
)

log = logging.getLogger(__name__)

__all__ = [
    "InfrastructureError",
    "WorkOrder",
    "ActionOutcome",
    "PeerReviewerGrant",
    "FloorManager",
    "WorkspaceGates",
    "Factory",
    "build_factory",
]


class InfrastructureError(Exception):
    """A failure of Foundry's own plumbing, not of the builder's work.

    Raised e.g. by :class:`WorkspaceGates` when the scratch copy of the
    workspace cannot be made. Workers treat it as a *counted* infrastructure
    failure (D1/E1: release the ticket, no attempt spent; quarantine after the
    configured number of consecutive failures), never as a gate failure.
    """


class WorkOrder(BaseModel):
    """A unit of work handed to the factory (what a human or the board asks for)."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1)
    description: str = ""
    task_id: str | None = Field(default=None, description="Optional id; generated as F-### when absent.")
    team_id: str | None = Field(default=None, description="Optional team; least-loaded team when absent.")
    files: list[str] = Field(default_factory=list, description="Repo-relative files the work may touch.")
    acceptance_criteria: list[str] = Field(default_factory=list)
    repo_root: str | None = Field(
        default=None, description="Repository the task belongs to (resolved); the manager's repo when absent."
    )


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


_KV_KEY = "floor_manager.state"
#: How long :meth:`FloorManager.drain` waits by default for in-flight completion
#: work (the Shop Foreman's PR draft, the patch file, the board's review/done).
#: A real Gemma draft can take a minute; the Foreman falls back to a heuristic
#: draft when Gemma times out, so this is an upper bound, not a typical wait.
DEFAULT_FINISH_TIMEOUT_S = 600.0
_TASK_ID_RE = re.compile(r"^F-(\d+)$")
_TERMINAL = {"complete", "quarantined", "failed"}


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
        patches_dir: where :meth:`on_task_complete` writes ``<task_id>.patch``
            (the combined approved diff) and ``<task_id>.md`` (the PR draft).
            ``None`` writes nothing.
        finish_timeout: default bound (seconds) for :meth:`drain`, i.e. how
            long shutdown waits for in-flight :meth:`on_task_complete` work.

    Restart safety: the manager's own state (directives, orders, allocations,
    board ids, PR drafts, reported quarantines, applied alerts) is kept in the
    queue's key-value table and reloaded on construction, and new task ids
    continue after the highest ``F-###`` already in the database.
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
        patches_dir: str | Path | None = None,
        repo_root: str | Path | None = None,
        finish_timeout: float = DEFAULT_FINISH_TIMEOUT_S,
    ) -> None:
        self.queue = queue
        self.finish_timeout = float(finish_timeout)
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
        #: Approved diffs per task and subtask (collected through :meth:`on_approved`).
        self.diffs: dict[str, dict[str, str]] = {}
        #: Combined patches that failed ``git apply --check`` (task -> .patch.rejected path).
        self.rejected_patches: dict[str, Path] = {}
        self._seq = 0
        self._completed: set[str] = set()
        #: task id -> the one task doing its completion work (shared by every caller).
        self._finish_tasks: dict[str, asyncio.Task[dict[str, str]]] = {}
        #: Foreman drafts whose patch could not be written yet (task -> summary);
        #: the next :meth:`on_task_complete` retries the write without a new draft.
        self._drafts: dict[str, dict[str, str]] = {}
        self._reported_quarantine: set[str] = set()
        self._pending: set[asyncio.Task[Any]] = set()
        #: task id -> its background sizing task (see :meth:`accept`).
        self._sizing_tasks: dict[str, asyncio.Task[Any]] = {}
        #: task id -> why its background sizing failed.
        self.sizing_errors: dict[str, str] = {}
        #: Current builder allocation per task (updated on RESIZE_ALLOCATION).
        self.allocations: dict[str, int] = {}
        #: alert_id -> outcome of every escalation already applied (idempotency).
        self.applied_alerts: dict[str, ActionOutcome] = {}
        self.patches_dir = Path(patches_dir) if patches_dir else None
        self.repo_root = Path(repo_root) if repo_root else None
        self.patch_paths: dict[str, Path] = {}
        #: task id -> file stem this manager uses in patches_dir (``F-001`` or ``F-001-2``).
        self.patch_names: dict[str, str] = {}
        self._load()

    # ------------------------------------------------------------ persistence

    def _kv_get(self) -> dict[str, Any] | None:
        fn = getattr(self.queue, "kv_get", None)
        if not callable(fn):
            return None
        try:
            raw = fn(_KV_KEY)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("floor manager: could not load state: %s", exc)
            return None
        if raw is None:
            return None
        if isinstance(raw, (str, bytes)):
            raw = json.loads(raw)
        return raw if isinstance(raw, dict) else None

    def save(self) -> None:
        """Persist the manager's state in the queue's key-value table (best effort)."""
        fn = getattr(self.queue, "kv_set", None)
        if not callable(fn):
            return
        state = {
            "version": 1,
            "seq": self._seq,
            "orders": {t: o.model_dump(mode="json") for t, o in self.orders.items()},
            "directives": {t: d.model_dump(mode="json") for t, d in self.directives.items()},
            "allocations": dict(self.allocations),
            "board_ids": dict(getattr(self.bridge, "board_ids", {}) or {}),
            "board_teams": dict(getattr(self.bridge, "board_teams", {}) or {}),
            "completed": sorted(self._completed),
            "reported_quarantine": sorted(self._reported_quarantine),
            "pr_summaries": dict(self.pr_summaries),
            "applied_alerts": {k: v.__dict__ for k, v in self.applied_alerts.items()},
            "diffs": {t: v for t, v in self.diffs.items() if t not in self._completed},
            "patch_names": dict(self.patch_names),
        }
        try:
            fn(_KV_KEY, state)
        except Exception as exc:  # pragma: no cover - persistence must not stop the line
            log.warning("floor manager: could not save state: %s", exc)

    def _load(self) -> None:
        state = self._kv_get() or {}
        try:
            self.orders = {t: WorkOrder.model_validate(o) for t, o in (state.get("orders") or {}).items()}
            self.directives = {
                t: TaskSizingDirective.model_validate(d) for t, d in (state.get("directives") or {}).items()
            }
            self.allocations = {t: int(n) for t, n in (state.get("allocations") or {}).items()}
            self._completed = set(state.get("completed") or [])
            self._reported_quarantine = set(state.get("reported_quarantine") or [])
            self.pr_summaries = {t: dict(v) for t, v in (state.get("pr_summaries") or {}).items()}
            self.applied_alerts = {k: ActionOutcome(**v) for k, v in (state.get("applied_alerts") or {}).items()}
            self.patch_names = {str(k): str(v) for k, v in (state.get("patch_names") or {}).items()}
            self.diffs = {}
            for t, v in (state.get("diffs") or {}).items():
                # older saves stored a list; key it by position so nothing is lost
                self.diffs[t] = {str(k): str(d) for k, d in (v.items() if isinstance(v, dict) else enumerate(v))}
        except Exception as exc:  # a corrupt blob must not stop a restart
            log.warning("floor manager: ignoring unreadable saved state: %s", exc)
        board_ids = state.get("board_ids") or {}
        restore = getattr(self.bridge, "restore", None)
        if board_ids and callable(restore):
            restore(board_ids, state.get("board_teams") or {})
        # Continue numbering after anything already in the queue (or the saved counter).
        highest = int(state.get("seq") or 0)
        try:
            tasks = self.queue.all_tasks()
        except Exception:  # pragma: no cover - defensive
            tasks = []
        for t in tasks:
            m = _TASK_ID_RE.match(str(t.get("task_id", "")))
            if m:
                highest = max(highest, int(m.group(1)))
        self._seq = highest

    # ------------------------------------------------------------- work orders

    def _next_task_id(self) -> str:
        while True:
            self._seq += 1
            task_id = f"F-{self._seq:03d}"
            if self.queue.get_task(task_id) is None:
                return task_id

    def _pick_team(self, order: WorkOrder) -> str:
        if order.team_id:
            return order.team_id
        names = list(self.teams) or ["alpha"]
        load = {t: 0 for t in names}
        for d in self.directives.values():
            if d.task_id not in self._completed and d.team_id in load:
                load[d.team_id] += 1
        return min(names, key=lambda t: (load[t], names.index(t)))

    def _prepare(self, work_order: WorkOrder | Mapping[str, Any]) -> str:
        """Assign the task id, team and repo, record the order and add the task (backlog).

        The order is saved right away, so an order whose sizing never finishes
        (cancelled, crashed, service stopped) is found and sized again by
        :meth:`resume_sizing` on the next start.
        """
        order = work_order if isinstance(work_order, WorkOrder) else WorkOrder.model_validate(dict(work_order))
        task_id = order.task_id or self._next_task_id()
        repo = order.repo_root or (str(self.repo_root.resolve()) if self.repo_root else None)
        if repo:
            repo = str(Path(repo).resolve())
        order = order.model_copy(update={"task_id": task_id, "team_id": self._pick_team(order), "repo_root": repo})
        self.orders[task_id] = order
        self._add_task(order)
        self.save()
        return task_id

    async def submit(self, work_order: WorkOrder | Mapping[str, Any], *, background: bool = False) -> str:
        """Size a work order with the Foreman, queue its tickets and put it on the board.

        Returns the Foundry task id. With ``background=True`` this is
        :meth:`accept`: it returns at once and the sizing runs on its own.
        """
        if background:
            return self.accept(work_order)
        task_id = self._prepare(work_order)
        await self._size_and_register(task_id)
        return task_id

    def accept(self, work_order: WorkOrder | Mapping[str, Any]) -> str:
        """Record a work order and return its task id at once; size it in the background.

        Must be called on the event loop that should run the sizing. The
        sizing task belongs to the manager, so cancelling the caller (an HTTP
        timeout) never cancels it. A Foreman on CPU can take a minute or more.
        """
        task_id = self._prepare(work_order)
        self._start_sizing(task_id)
        return task_id

    def is_sizing(self, task_id: str) -> bool:
        """True while the task's background sizing is running."""
        t = self._sizing_tasks.get(task_id)
        return t is not None and not t.done()

    def _start_sizing(self, task_id: str) -> asyncio.Task[Any]:
        running = self._sizing_tasks.get(task_id)
        if running is not None and not running.done():
            return running
        self.sizing_errors.pop(task_id, None)
        t = asyncio.get_running_loop().create_task(self._background_size(task_id))
        self._sizing_tasks[task_id] = t
        self._pending.add(t)
        t.add_done_callback(self._pending.discard)
        return t

    async def _background_size(self, task_id: str) -> None:
        try:
            await self._size_and_register(task_id)
        except asyncio.CancelledError:
            # stopped mid-sizing: nothing was applied; the next start sizes it again
            log.info("floor manager: sizing of %s interrupted; it is sized again on the next start", task_id)
            raise
        except Exception as exc:  # a sizing failure must never take the service down
            reason = f"sizing failed: {type(exc).__name__}: {exc}"[:500]
            log.warning("floor manager: %s %s", task_id, reason)
            self.sizing_errors[task_id] = reason
            try:
                self.queue.record_event("sizing_failed", task_id, {"error": reason})
                task = self.queue.get_task(task_id)
                if task and _val(task.get("state")) not in _TERMINAL and not self.queue.tickets_for_task(task_id):
                    self.queue.quarantine(task_id, reason)
            except Exception as inner:  # pragma: no cover - bookkeeping is best effort
                log.debug("floor manager: could not record the sizing failure: %s", inner)
        finally:
            if self._sizing_tasks.get(task_id) is asyncio.current_task():
                self._sizing_tasks.pop(task_id, None)

    async def _size_and_register(self, task_id: str) -> None:
        """Size a recorded order and apply it (tickets, board, saved state).

        Everything after the Foreman's answer runs without awaiting, so a
        cancellation either leaves the task unsized (backlog, no tickets) or
        fully sized and registered.
        """
        order = self.orders[task_id]
        directive = await self._size(order)
        if self.queue.tickets_for_task(task_id):
            return  # sized meanwhile by someone else (a second resume)
        self.queue.apply_sizing(directive)
        # recorded only once the tickets exist: a directive without tickets would look applied
        self.directives[task_id] = directive
        self.allocations[task_id] = directive.allocated_builders
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
        self.save()

    def unsized_tasks(self, only: Any = None) -> list[str]:
        """Open tasks that have no tickets yet (orders whose sizing never finished)."""
        out = []
        for task in self.queue.all_tasks():
            tid = str(task.get("task_id"))
            if _val(task.get("state")) not in ("backlog", "sizing"):
                continue
            if only is not None and not only(tid):
                continue
            if self.queue.tickets_for_task(tid):
                continue
            out.append(tid)
        return out

    def _rebuild_order(self, tid: str) -> WorkOrder:
        """A work order for a queue task whose saved order is missing (title/description/team/repo only)."""
        task = self.queue.get_task(tid) or {}
        order = WorkOrder(
            title=str(task.get("title") or tid),
            description=str(task.get("description") or ""),
            task_id=tid,
            team_id=task.get("team_id") or self._pick_team(WorkOrder(title=tid)),
            repo_root=task.get("repo_root") or (str(self.repo_root.resolve()) if self.repo_root else None),
        )
        log.warning(
            "floor manager: %s has no saved order; rebuilt it from the queue row without its files "
            "(the Foreman sizes it from the title and description only)", tid,
        )
        return order

    def resume_sizing(self, only: Any = None) -> list[str]:
        """Size again, in the background, every open task left without tickets.

        This repairs orders whose sizing was cancelled half-way (before the
        fix, an HTTP timeout did that). A task with no saved order is rebuilt
        from its queue row. Identical unsized orders (same repo, title,
        description and files: the user resent the order after a timeout) are
        sized once, as the newest of them; the others are quarantined as
        duplicates. ``only(task_id) -> bool`` limits it (e.g. to one repo).
        Returns the task ids being sized; call on the event loop.
        """
        groups: dict[tuple[Any, ...], list[str]] = {}
        for tid in self.unsized_tasks(only):
            if self.is_sizing(tid):
                continue
            if tid not in self.orders:
                try:
                    self.orders[tid] = self._rebuild_order(tid)
                except Exception as exc:  # cannot be rebuilt: say so instead of leaving it stuck
                    reason = f"order could not be recovered for sizing: {exc}"[:500]
                    log.warning("floor manager: %s %s", tid, reason)
                    self.queue.quarantine(tid, reason)
                    continue
            groups.setdefault(_order_key(self.orders[tid]), []).append(tid)
        started = []
        for ids in groups.values():
            ids.sort(key=_task_number)
            keep, dupes = ids[-1], ids[:-1]
            for tid in dupes:
                reason = f"duplicate of {keep} (resent after the old 30 s timeout)"
                log.warning("floor manager: %s is a %s; quarantined, %s is sized instead", tid, reason, keep)
                self.queue.quarantine(tid, reason)
                self._reported_quarantine.add(tid)  # never on the board: nothing to report
            self.directives.pop(keep, None)  # a directive without tickets was never applied
            self._start_sizing(keep)
            started.append(keep)
        started.sort(key=_task_number)
        if groups:
            self.save()
        if started:
            log.info("floor manager: sizing again %s", ", ".join(started))
        return started

    async def stop_sizing(self, timeout: float) -> list[str]:
        """Wait up to ``timeout`` s for background sizing, then cancel what is left.

        Returns the ids whose sizing was cancelled (they stay unsized and are
        sized again by :meth:`resume_sizing` on the next start).
        """
        tasks = {t for t in self._sizing_tasks.values() if not t.done()}
        if tasks:
            await asyncio.wait(tasks, timeout=max(0.0, float(timeout)))
        left = {tid: t for tid, t in self._sizing_tasks.items() if not t.done()}
        for t in left.values():
            t.cancel()
        if left:
            await asyncio.gather(*left.values(), return_exceptions=True)
            log.warning("floor manager: stopped while sizing %s", ", ".join(sorted(left)))
        return sorted(left)

    def _add_task(self, order: WorkOrder) -> None:
        """``queue.add_task`` with the task's repo (G1), on queues that store it."""
        args = (order.task_id, order.title, order.description, order.team_id)
        if order.repo_root and "repo_root" in _params(self.queue.add_task):
            self.queue.add_task(*args, repo_root=order.repo_root)
        else:
            self.queue.add_task(*args)

    def task_repo(self, task_id: str) -> str | None:
        """The repository a task belongs to (queue column first, then the saved order)."""
        task = self.queue.get_task(task_id) or {}
        repo = task.get("repo_root")
        if not repo and task_id in self.orders:
            repo = self.orders[task_id].repo_root
        return str(repo) if repo else None

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
        """Execute the escalation's prescribed action (validated first) and report it.

        Idempotent per ``alert_id``: an alert that was already applied (in this
        run or before a restart) returns the earlier outcome without acting,
        storing or posting it again.
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
            PrescribedAction.NONE: self._no_action,
        }[action]
        try:
            applied, detail, params = await handler(alert)
        except Exception as exc:  # an action must never take the line down
            log.warning("floor manager: %s failed: %s", action.value, exc)
            applied, detail, params = False, f"failed: {exc}", {}
        outcome = ActionOutcome(alert.alert_id, action.value, applied, detail, params)
        self.actions.append(outcome)
        self.applied_alerts[alert.alert_id] = outcome
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
        return True, f"{grant.agent_id} reviews for {team_id} for {minutes:g} min", {"slot": slot, "minutes": minutes}

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
        self.queue.quarantine(task_id, reason)
        self._report_quarantine(task_id, reason)
        return True, f"{task_id} quarantined", {"reason": reason}

    def _report_quarantine(self, task_id: str, reason: str) -> None:
        """Post a quarantine to the board once per task."""
        if task_id in self._reported_quarantine:
            return
        self._reported_quarantine.add(task_id)
        self.bridge.quarantined(task_id, reason)

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
        self.queue.resize(task_id, n)
        self.allocations[task_id] = n
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

    async def on_approved(self, item: Any, verdict: Any) -> None:
        """Reviewer approved one subtask (``TeamPool(on_approved=...)`` hook).

        Keeps the diff for the PR summary, reports the verdict on the board and,
        when this was the task's last open ticket, schedules
        :meth:`on_task_complete` (it runs right after the worker marks the
        ticket done, so completion does not depend on the pool's own callback).
        """
        task_id = item.assignment.task_id
        # keyed by subtask: a re-approval after a resume replaces, never duplicates
        self.diffs.setdefault(task_id, {})[str(item.assignment.subtask_id)] = item.submission.diff
        self.save()
        d = self.directives.get(task_id)
        team_id = d.team_id if d else item.assignment.team_id
        self.bridge.progress(
            task_id,
            f"[Foundry] {verdict.reviewer_id} approved {item.assignment.subtask_id} by {item.assignment.builder_id}",
            team_id=team_id,
            agent_id=item.assignment.builder_id,
            status="done",
        )
        others = [t for t in self.queue.tickets_for_task(task_id) if t.ticket_id != item.ticket_id]
        if all(t.status == "done" for t in others):
            self._spawn(self._complete_when_closed(task_id))

    async def _complete_when_closed(self, task_id: str) -> None:
        for _ in range(200):  # the worker completes the ticket right after on_approved returns
            task = self.queue.get_task(task_id)
            if task and _val(task.get("state")) == "complete":
                await self.on_task_complete(task_id)
                return
            await asyncio.sleep(0.005)
        log.warning("floor manager: %s approved but never reached complete", task_id)

    def _spawn(self, coro: Any) -> None:
        t = asyncio.get_running_loop().create_task(coro)
        self._pending.add(t)
        t.add_done_callback(self._pending.discard)

    async def drain(self, timeout: float | None = None) -> bool:
        """Wait for in-flight completion work (PR drafts, patch files, board review/done).

        Waits up to ``timeout`` seconds (default :attr:`finish_timeout`) for
        every scheduled :meth:`on_task_complete` / completion watcher, including
        ones started while waiting. Never cancels them. Returns True when
        nothing is left in flight.
        """
        limit = self.finish_timeout if timeout is None else float(timeout)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, limit)
        while True:
            pending = {t for t in self._pending if not t.done()}
            if not pending:
                return True
            left = deadline - loop.time()
            if left <= 0:
                busy = sorted(t for t, task in self._finish_tasks.items() if not task.done())
                log.warning(
                    "floor manager: completion work still running after %gs%s",
                    limit, f" ({', '.join(busy)})" if busy else "",
                )
                return False
            await asyncio.wait(pending, timeout=left)

    def report_quarantines(self) -> list[str]:
        """Report tasks the workers quarantined (retry limit, REJECTED) to the board once."""
        out = []
        for task_id in self.directives:
            task = self.queue.get_task(task_id)
            if task and _val(task.get("state")) == "quarantined" and task_id not in self._reported_quarantine:
                self._report_quarantine(task_id, str(task.get("quarantine_reason") or "quarantined"))
                out.append(task_id)
        if out:
            self.save()
        return out

    async def on_task_complete(self, task_id: str, diff: str | None = None) -> dict[str, str]:
        """Draft the commit message / PR body with the Foreman, write the patch, mark the board task done.

        Idempotent: the pool callback, :meth:`on_approved` and ``run_real`` may
        all call it; they share one manager-owned task, so cancelling a caller
        (a pool being stopped, a caller's own timeout) never cancels the work.
        A task whose summary was recorded but whose patch file is missing (the
        write failed, or the file was removed) gets the patch rewritten from the
        stored summary, without asking the Foreman again.
        """
        if task_id in self.pr_summaries and not self._patch_missing(task_id):
            return self.pr_summaries[task_id]
        task = self._finish_tasks.get(task_id)
        if task is None or task.done():
            task = asyncio.get_running_loop().create_task(self._finish(task_id, diff))
            self._finish_tasks[task_id] = task
            self._pending.add(task)
            task.add_done_callback(self._pending.discard)
        return await asyncio.shield(task)

    def _patch_missing(self, task_id: str) -> bool:
        """True when patches are written but neither this task's .patch nor .patch.rejected exists."""
        if self.patches_dir is None:
            return False
        name = self.patch_names.get(task_id)
        if not name:
            return True
        return not any((self.patches_dir / f"{name}{ext}").is_file() for ext in (".patch", ".patch.rejected"))

    def _approved_diff(self, task_id: str) -> str:
        parts = self.diffs.get(task_id, {})
        return "".join(_with_newline(parts[k]) for k in sorted(parts, key=_subtask_order)) or self._task_diff(task_id)

    async def _finish(self, task_id: str, diff: str | None) -> dict[str, str]:
        if diff is None:
            diff = self._approved_diff(task_id)
        known = self.pr_summaries.get(task_id)
        if known is not None:
            # repair: the summary (and the board's review/done) are already recorded
            await self._write_patch(task_id, diff, known)
            self.save()
            return known
        summary = self._drafts.get(task_id)
        if summary is None:
            order = self.orders.get(task_id)
            summary = await _maybe_await(
                self.foreman.draft_pr_summary(diff, task_id=task_id, title=order.title if order else None)
            )
            summary = {k: str(v) for k, v in dict(summary).items()}
            self._drafts[task_id] = summary
        if not await self._write_patch(task_id, diff, summary):
            # Not written (disk error): nothing is recorded, the task stays
            # unfinished and the next call retries the write with this draft.
            return summary
        # Recorded only once the patch is written: a run cut short before this
        # point leaves the task unfinished, and the next call finishes it.
        # Nothing below awaits, so the summary, the board ops and the save
        # happen together or not at all.
        self._drafts.pop(task_id, None)
        self.pr_summaries[task_id] = summary
        self._completed.add(task_id)
        try:
            self.queue.store_pr_summary(task_id, summary)
        except Exception as exc:  # pragma: no cover - bookkeeping is best effort
            log.debug("store_pr_summary failed: %s", exc)
        d = self.directives.get(task_id)
        if d is not None:
            self.bridge.review(task_id, d.team_id, "APPROVED", note="Foundry reviewer approved")
        self.bridge.done(task_id, summary.get("commit_message", ""))
        self.save()
        return summary

    async def _write_patch(self, task_id: str, diff: str, summary: Mapping[str, str]) -> bool:
        """Write ``<name>.patch`` (apply with ``git apply``) and ``<name>.md`` into patches_dir.

        Returns False when the files could not be written (True when written,
        or when this manager writes no patches at all).

        Foundry never edits the target repository itself; these files are the
        output. When git is installed the combined patch is first checked with
        ``git apply --check`` against a scratch copy of the task's repo; a patch
        that does not apply is written as ``<name>.patch.rejected`` instead.

        The output is never misleading (G2):

        * ``<name>`` is the task id, unless files of that name already exist
          with different content (another database, a reset): then the next
          free ``<task>-2``, ``<task>-3`` ... is used. Files this manager wrote
          earlier for the same task are reused and updated;
        * writing one variant removes the other (a stale ``.patch.rejected``
          next to a good ``.patch``, or the reverse).
        """
        if self.patches_dir is None:
            return True
        text = _with_newline(diff) if diff else ""
        repo = self.task_repo(task_id)
        problem = await asyncio.to_thread(_git_check, text, Path(repo) if repo else self.repo_root) if text else None
        md = f"{summary.get('commit_message', '').strip()}\n\n{summary.get('pr_body', '').strip()}\n"
        try:
            self.patches_dir.mkdir(parents=True, exist_ok=True)
            name = self._patch_name(task_id, text, md, rejected=bool(problem))
            good = self.patches_dir / f"{name}.patch"
            bad = self.patches_dir / f"{name}.patch.rejected"
            (self.patches_dir / f"{name}.md").write_text(md, encoding="utf-8")
            target, stale = (bad, good) if problem else (good, bad)
            target.write_text(text, encoding="utf-8", newline="\n")
            stale.unlink(missing_ok=True)
            self.patch_names[task_id] = name
            if problem:
                self.rejected_patches[task_id] = target
                self.patch_paths.pop(task_id, None)
                log.warning("floor manager: combined patch for %s does not apply (%s); wrote %s", task_id, problem[:160], target.name)
            else:
                self.patch_paths[task_id] = target
                self.rejected_patches.pop(task_id, None)
        except OSError as exc:
            log.warning("floor manager: could not write the patch for %s: %s", task_id, exc)
            return False
        return True

    def _patch_name(self, task_id: str, text: str, md: str, *, rejected: bool) -> str:
        """The file stem for a task's patch: its own earlier name, else the first name that is free or identical."""
        own = self.patch_names.get(task_id)
        if own:
            return own
        n = 1
        while True:
            name = task_id if n == 1 else f"{task_id}-{n}"
            files = {
                "patch": self.patches_dir / f"{name}.patch",  # type: ignore[operator]
                "rejected": self.patches_dir / f"{name}.patch.rejected",  # type: ignore[operator]
                "md": self.patches_dir / f"{name}.md",  # type: ignore[operator]
            }
            mine = files["rejected" if rejected else "patch"]
            other = files["patch" if rejected else "rejected"]
            same = (
                not other.exists()
                and (not mine.exists() or mine.read_text(encoding="utf-8") == text)
                and (not files["md"].exists() or files["md"].read_text(encoding="utf-8") == md)
            )
            if same:
                return name
            n += 1

    def _task_diff(self, task_id: str) -> str:
        fn = getattr(self.queue, "task_diff", None) or getattr(self.queue, "approved_diffs", None)
        if callable(fn):
            got = fn(task_id)
            if isinstance(got, str):
                return got
            if isinstance(got, (list, tuple)):
                return "\n".join(str(x) for x in got)
        return ""


def _finish_timeout(config: Any, explicit: float | None) -> float:
    """The drain bound: the explicit value, else ``config.finish_timeout_s``, else the default."""
    if explicit is not None:
        return float(explicit)
    value = getattr(config, "finish_timeout_s", None)
    try:
        return float(value) if value is not None and float(value) >= 0 else DEFAULT_FINISH_TIMEOUT_S
    except (TypeError, ValueError):
        return DEFAULT_FINISH_TIMEOUT_S


def _params(fn: Any) -> set[str]:
    try:
        return set(inspect.signature(fn).parameters)
    except (TypeError, ValueError):
        return set()


def _order_key(order: WorkOrder) -> tuple[Any, ...]:
    """What makes two orders the same request (repo, title, description, files; whitespace/case folded)."""
    def norm(text: str) -> str:
        return " ".join(str(text or "").split()).casefold()

    repo = str(Path(order.repo_root).resolve()).casefold() if order.repo_root else None
    return (repo, norm(order.title), norm(order.description), tuple(sorted(f.strip() for f in order.files)))


def _task_number(task_id: str) -> tuple[int, str]:
    m = _TASK_ID_RE.match(task_id)
    return (int(m.group(1)) if m else -1, task_id)


def _with_newline(text: str) -> str:
    return text if text.endswith("\n") else text + "\n"


def _subtask_order(key: str) -> tuple[int, ...] | tuple[str]:
    """Sort ``F-001.2`` before ``F-001.10``."""
    nums = re.findall(r"\d+", key)
    return tuple(int(n) for n in nums) if nums else (key,)


def _copy_workspace(src: Path, dst: Path) -> None:
    """Copy ``src`` to ``dst`` without VCS data, environments and caches (InfrastructureError on failure)."""
    try:
        if src.is_dir():
            # symlinks are copied as links: never follow them out of the repo (or loop on them)
            shutil.copytree(src, dst, symlinks=True, ignore=shutil.ignore_patterns(*SCRATCH_IGNORE))
        else:
            dst.mkdir(parents=True)
    except (OSError, shutil.Error) as exc:
        raise InfrastructureError(f"could not copy the workspace for the gates: {exc}") from exc


def _git_check(patch: str, repo_root: Path | None) -> str | None:
    """``git apply --check`` the patch in a scratch copy of the repo; the error text, or None.

    Returns None (no check) when git is not installed or there is no repo to check against.
    """
    git = shutil.which("git")
    if git is None or repo_root is None or not repo_root.is_dir():
        return None
    tmp = Path(tempfile.mkdtemp(prefix="foundry-patch-"))
    try:
        ws = tmp / "ws"
        _copy_workspace(repo_root, ws)
        (tmp / "combined.patch").write_text(patch, encoding="utf-8", newline="\n")
        proc = subprocess.run(
            [git, "apply", "--check", str(tmp / "combined.patch")],
            cwd=str(ws), capture_output=True, text=True, timeout=60,
        )
        return None if proc.returncode == 0 else (proc.stderr or proc.stdout or "git apply --check failed").strip()
    except InfrastructureError as exc:
        log.warning("floor manager: patch check skipped: %s", exc)
        return None
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("floor manager: patch check skipped: %s", exc)
        return None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _get(obj: Any, name: str) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name)
    return getattr(obj, name, None)


# ---------------------------------------------------------------------------
# Wiring: gates adapter and a ready-made factory (used by main.py and tests)
# ---------------------------------------------------------------------------


#: Never copied into gate scratch workspaces (VCS data, environments, caches).
SCRATCH_IGNORE: tuple[str, ...] = (
    ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", "*.db", "*.db-wal", "*.db-shm",
)


class WorkspaceGates:
    """Adapts :class:`~foundry.validation_gates.ValidationGates` to the worker's gate interface.

    The builder worker calls ``gates.run(submission, assignment)``. Each call
    copies ``base_workspace`` (minus :data:`SCRATCH_IGNORE`) into a fresh
    temporary directory, applies the diff there (refusing files outside
    ``assignment.allowed_files``) and runs the gates, so a retry never sees the
    previous attempt's patch and the real working tree is never modified.
    """

    def __init__(self, gates: Any, base_workspace: str | Path) -> None:
        self.gates = gates
        self.base_workspace = Path(base_workspace)
        self.calls = 0

    async def run(self, submission: Any, assignment: Any) -> Any:
        """Validate ``submission`` in a scratch copy of the workspace."""
        self.calls += 1
        try:
            tmp = Path(await asyncio.to_thread(tempfile.mkdtemp, prefix="foundry-gates-"))
        except OSError as exc:
            raise InfrastructureError(f"could not create a scratch workspace: {exc}") from exc
        try:
            ws = tmp / "ws"
            # copying a real repo is slow: keep it off the event loop
            await asyncio.to_thread(_copy_workspace, self.base_workspace, ws)
            return await self.gates.validate_async(
                submission, ws, allowed_files=list(assignment.allowed_files) or None
            )
        finally:
            await asyncio.to_thread(shutil.rmtree, tmp, True)


@dataclass
class Factory:
    """Every Foundry component, wired together (see :func:`build_factory`)."""

    config: Any
    queue: Any
    telemetry: Any
    limiter: Any
    context_engine: Any
    foreman: Any
    cache: Any
    gates: Any
    pools: dict[str, Any]
    manager: FloorManager
    bridge: Any
    claude: Any
    gemma: Any

    @asynccontextmanager
    async def running(self, finish_timeout: float | None = None) -> AsyncIterator[Factory]:
        """Start every team pool; on exit wait for completion work, then stop the pools.

        ``finish_timeout`` bounds each wait for in-flight completion work
        (default: the manager's ``finish_timeout``); the work is never cancelled.
        """
        for pool in self.pools.values():
            pool.start()
        try:
            yield self
        finally:
            # Completion work first (the PR draft, the patch, the board's
            # review/done): a pool's stop grace would otherwise cancel it.
            await self.manager.drain(finish_timeout)
            for pool in self.pools.values():
                await pool.stop()
            # completions a stopping worker scheduled on its way out
            await self.manager.drain(finish_timeout)

    async def wait(self, task_ids: list[str], timeout: float = 30.0) -> dict[str, str]:
        """Wait until every task is terminal; returns ``{task_id: state}``."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        terminal = {"complete", "quarantined", "failed"}
        while True:
            states = {t: str(_val(self.queue.get_task(t)["state"])) for t in task_ids}
            if all(s in terminal for s in states.values()):
                await self.manager.drain()
                self.manager.report_quarantines()
                return states
            if loop.time() >= deadline:
                raise asyncio.TimeoutError(f"tasks not finished: {states}")
            await asyncio.sleep(0.01)

    def close(self) -> None:
        """Drain and stop the bridge, save the manager's state, close the databases.

        The bridge sends ops from a daemon thread, so anything still queued when
        the process exits is lost (this is how review/verdict/done went missing
        after a real run against the shared board). ``bridge.close()`` waits
        while ops keep completing (bounded, see ``BoardBridge.flush``) before
        the state is saved, so the board ids it resolved are kept.
        """
        try:
            fn = getattr(self.bridge, "close", None) or getattr(self.bridge, "flush", None)
            if callable(fn):
                fn()
        except Exception as exc:  # the board must never break shutdown
            log.warning("foundry: board bridge did not close cleanly: %s", exc)
        self.manager.save()  # board ids resolved by the bridge thread are kept for the next run
        for obj in (self.cache, self.queue):
            fn = getattr(obj, "close", None)
            if callable(fn):
                fn()


def build_factory(
    config: Any,
    *,
    claude: Any,
    gemma: Any,
    repo_root: str | Path,
    workspace: str | Path | None = None,
    bridge: Any = None,
    gates: Any = None,
    limiter: Any = None,
    queue_clock: Any = None,
    telemetry_clock: Any = None,
    poll: float = 0.01,
    trust_cache: bool | None = None,
    patches_dir: str | Path | None = None,
    finish_timeout: float | None = None,
    dispatch_repo: str | Path | None = None,
) -> Factory:
    """Build queue, telemetry, limiter, context engine, Foreman, cache, gates, pools and manager.

    Args:
        config: a :class:`~foundry.config.FoundryConfig`.
        claude / gemma: the clients (simulated in phase 1).
        repo_root: what the context engine reads.
        workspace: directory gates copy and patch (defaults to ``repo_root``).
        bridge: a :class:`~foundry.board_bridge.BoardBridge` (default from
            ``config.board_bridge_mode``).
        gates: the gate runner the workers use; default
            ``WorkspaceGates(ValidationGates(checks=("syntax", "linter")), workspace)``.
        queue_clock / telemetry_clock: fake clocks for tests (datetime / epoch).
        trust_cache: overrides ``config.trust_cache`` (default False: a cache
            hit still goes through the gates and the reviewer).
        patches_dir: overrides ``config.patches_dir`` for the approved patches
            (default: ``config.resolved_patches_dir()``, i.e. ``patches/`` next
            to the database unless FOUNDRY_PATCHES_DIR says otherwise).
        finish_timeout: how long shutdown (``Factory.running`` / ``wait``)
            waits for in-flight completion work (default
            ``config.finish_timeout_s`` when the config has it, else
            :data:`DEFAULT_FINISH_TIMEOUT_S`).
        dispatch_repo: when given, the pools only claim tickets of tasks that
            belong to this repository (real mode); None dispatches everything.

    The target repository is never modified: gates patch scratch copies only,
    Claude runs with ``cwd=repo_root`` (it reads, it does not write: diff-only
    output), and approved work lands in ``patches_dir`` as ``<task>.patch``.
    The queue is resumed (tickets a crashed worker left claimed are reopened)
    before anything runs.
    """
    from foundry.board_bridge import BoardBridge
    from foundry.builder_worker import TeamPool
    from foundry.context_engine import ContextEngine
    from foundry.orchestrator_queue import FoundryQueue
    from foundry.rate_limiter import RateLimiter
    from foundry.shop_foreman import ShopForeman
    from foundry.solution_cache import SolutionCache
    from foundry.telemetry import Telemetry
    from foundry.validation_gates import ValidationGates

    db = str(config.db_path)
    queue = FoundryQueue(db, clock=queue_clock)
    resumed = queue.resume()
    if resumed:
        log.info("foundry: resumed %d ticket(s) left claimed by a previous run", resumed)
    telemetry = Telemetry(queue, clock=telemetry_clock) if telemetry_clock else Telemetry(queue)
    limiter = limiter or RateLimiter(config.rpm, config.tpm, config.max_concurrent)
    context = ContextEngine(repo_root, gemma)
    foreman = ShopForeman(gemma, queue, telemetry, context, config)
    cache = SolutionCache(db)
    if gates is None:
        gates = WorkspaceGates(ValidationGates(checks=("syntax", "linter")), workspace or repo_root)
    bridge = bridge if bridge is not None else BoardBridge(config.board_bridge_mode)
    if trust_cache is None:
        trust_cache = bool(getattr(config, "trust_cache", False))
    if patches_dir is None:
        resolve = getattr(config, "resolved_patches_dir", None)
        patches_dir = resolve() if callable(resolve) else None
        patches_dir = patches_dir or getattr(config, "patches_dir", None) or Path(config.db_path).parent / "patches"
    manager = FloorManager(
        queue,
        foreman,
        {},
        telemetry,
        bridge,
        claude,
        limiter=limiter,
        builders_per_team=config.builders_per_team,
        patches_dir=patches_dir,
        repo_root=repo_root,
        finish_timeout=_finish_timeout(config, finish_timeout),
    )
    pool_extra: dict[str, Any] = {}
    if dispatch_repo is not None:
        if "repo_root" not in _params(TeamPool):
            raise TypeError("this TeamPool cannot scope dispatch to one repository (no repo_root)")
        pool_extra["repo_root"] = str(Path(dispatch_repo).resolve())
    pools = {
        team: TeamPool(
            team,
            config.builders_per_team,
            queue=queue,
            client=claude,
            limiter=limiter,
            telemetry=telemetry,
            context_engine=context,
            gates=gates,
            cache=cache,
            foreman=foreman,
            poll=poll,
            trust_cache=trust_cache,
            on_task_complete=manager.on_task_complete,
            on_approved=manager.on_approved,
            cwd=str(Path(repo_root).resolve()),
            config=config,
            limit_cooldown_s=getattr(config, "limit_cooldown_s", None),
            infra_failure_cap=int(getattr(config, "infra_failure_cap", 3)),
            **pool_extra,
        )
        for team in config.teams
    }
    manager.teams = dict(pools)
    return Factory(config, queue, telemetry, limiter, context, foreman, cache, gates, pools, manager, bridge, claude, gemma)
