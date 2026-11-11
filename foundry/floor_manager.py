"""Floor Manager: turns work orders into sized, dispatched, reported tasks.

The Floor Manager is the planning tier of the factory. It owns no polling loop
and makes **no** Claude calls of its own while the line runs: every decision it
takes is driven by an event (a work order arrives, the Shop Foreman raises an
escalation, a task completes).

Flow::

    work order --submit()--> queue.add_task --> foreman.size_task_scope
               --> queue.apply_sizing (one ticket per builder slot)
               --> bridge.register_task (the Agent Department board)

    ForemanEscalation --apply_escalation()--> one of eight actions (logged, most undoable)
    task complete --on_task_complete()--> foreman.draft_pr_summary --> bridge.done

The ``claude`` client is accepted so a phase-2 manager can plan large work
orders with Claude; phase 1 never calls it (tests assert 0 manager calls).
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from foundry import suite_check
from foundry.floor_actions import ForemanActionsMixin
from foundry.floor_helpers import (
    ACTION_LOG_MAX,
    ActionOutcome,
    InfrastructureError,
    PeerReviewerGrant,
    _ACTIONS_KEY,
    _TERMINAL,
    _WIDEN_COOLDOWN_S,
    _WIDEN_UNDO_SUPPRESS_S,
    _get,
    _maybe_await,
    _parse_utc,
    _utc_now,
    _val,
)
from foundry.schemas import (
    TaskSizingDirective,
)
from foundry.validation_gates import copy_ignore

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
    # moved to foundry.floor_helpers, still importable from here
    "ACTION_LOG_MAX",
    "_ACTIONS_KEY",
    "_WIDEN_COOLDOWN_S",
    "_WIDEN_UNDO_SUPPRESS_S",
    "_parse_utc",
    "_utc_now",
]


class WorkOrder(BaseModel):
    """A unit of work handed to the factory (what a human or the board asks for)."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1)
    description: str = ""
    task_id: str | None = Field(default=None, description="Optional id; generated as F-### when absent.")
    team_id: str | None = Field(default=None, description="Optional team; least-loaded team when absent.")
    files: list[str] = Field(default_factory=list, description="Repo-relative files the work may touch.")
    read_files: list[str] = Field(
        default_factory=list, description="Repo-relative files builders are shown but may not change."
    )
    acceptance_criteria: list[str] = Field(default_factory=list)
    repo_root: str | None = Field(
        default=None, description="Repository the task belongs to (resolved); the manager's repo when absent."
    )
    pr_base: str | None = Field(
        default=None, description="Branch the task's pull request targets (a board batch's base); the publisher's when absent."
    )


def _clean_meta(m: Mapping[str, Any]) -> dict[str, Any]:
    """Normalise a sizing record to ``{"source": "gemma"|"heuristic"|None, "ms": int|None}``.

    ``"raw"`` (the Foreman's size before calibration, S/M/L/XL) is kept when valid.
    """
    source = m.get("source")
    source = source if source in ("gemma", "heuristic") else None
    try:
        ms = max(0, int(m.get("ms"))) if m.get("ms") is not None else None
    except (TypeError, ValueError):
        ms = None
    out: dict[str, Any] = {"source": source, "ms": ms}
    raw = str(m.get("raw") or "").upper()
    if raw in ("S", "M", "L", "XL"):
        out["raw"] = raw
    return out


_KV_KEY = "floor_manager.state"
#: How long :meth:`FloorManager.drain` waits by default for in-flight completion
#: work (the Shop Foreman's PR draft, the patch file, the board's review/done).
#: A real Gemma draft can take a minute; the Foreman falls back to a heuristic
#: draft when Gemma times out, so this is an upper bound, not a typical wait.
DEFAULT_FINISH_TIMEOUT_S = 600.0
_TASK_ID_RE = re.compile(r"^F-(\d+)$")


class FloorManager(ForemanActionsMixin):
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
        publisher: Any = None,
        auto_pr: bool | None = None,
        pr_draft: bool = False,
        snapshots: Any = None,
        gemma_notes: bool = False,
        gemma_notes_timeout_s: float = 90.0,
        pr_template: bool = False,
    ) -> None:
        self.queue = queue
        self.pr_template = bool(pr_template)
        self.gemma_notes = bool(gemma_notes)
        self.gemma_notes_timeout_s = float(gemma_notes_timeout_s)
        #: task id -> Gemma's advisory PR brief (in memory only)
        self.pr_briefs: dict[str, str] = {}
        #: :class:`~foundry.base_snapshot.BaseSnapshots` (or a fake): clean checkouts of the
        #: publish branch that every task is built, gated, stacked and checked on.
        self.snapshots = snapshots
        #: task id -> ``{"base": branch, "sha": commit}`` the task is built on (pinned at sizing).
        self.task_bases: dict[str, dict[str, str]] = {}
        #: tasks already warned about building from the user's folder (one WARNING per task).
        self._snapshot_warned: set[str] = set()
        #: held while a snapshot is created and recorded, and while snapshots are pruned
        self._snapshot_lock = asyncio.Lock()
        #: sha -> number of blocks using its snapshot right now (:meth:`base_lease`); never pruned.
        self._snapshot_users: dict[str, int] = {}
        #: sha -> snapshot path verified clean by ``checkout()`` in this run (no git status per ticket).
        self._snapshot_paths: dict[str, Path] = {}
        #: :class:`~foundry.git_publisher.GitPublisher` (or a fake): pushes an
        #: approved task's patch to its own branch and opens the PR.
        self.publisher = publisher
        #: publish every approved task automatically (default: on when a publisher is given)
        self.auto_pr = (publisher is not None) if auto_pr is None else bool(auto_pr)
        self.pr_draft = bool(pr_draft)
        #: task id -> ``{state: pending|opened|failed|skipped, url, branch, error, stage, attempts}``
        self.pr_states: dict[str, dict[str, Any]] = {}
        #: task id -> its background publish task
        self._pr_tasks: dict[str, asyncio.Task[Any]] = {}
        self.finish_timeout = float(finish_timeout)
        self.foreman = foreman
        self.teams = dict(teams)
        self.telemetry = telemetry
        self.bridge = bridge
        report_activity = getattr(bridge, "agent_status", None)
        if callable(report_activity):
            for pool in self.teams.values():
                attach = getattr(pool, "set_on_activity", None)
                if callable(attach):
                    attach(report_activity)
        self.claude = claude
        self.limiter = limiter
        self.builders_per_team = builders_per_team
        self.directives: dict[str, TaskSizingDirective] = {}
        self.orders: dict[str, WorkOrder] = {}
        self.pr_summaries: dict[str, dict[str, str]] = {}
        self.peer_reviewers: dict[str, PeerReviewerGrant] = {}
        #: Every escalation outcome of this run, in order (see :meth:`actions` for the log).
        self.outcomes: list[ActionOutcome] = []
        #: The persisted action log, oldest first (dicts; see :meth:`_log_action`).
        self.action_log: list[dict[str, Any]] = []
        self._action_seq = 0
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
        #: task id -> how it was sized: ``{"source": "gemma"|"heuristic"|None, "ms": int}``
        #: (kept with the directives in the saved state, so it survives a restart).
        self.sizing_meta: dict[str, dict[str, Any]] = {}
        #: sizing info of a finished :meth:`_size` call not yet applied (task id -> meta).
        self._sized_meta: dict[str, dict[str, Any]] = {}
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
            "sizing_meta": {t: dict(m) for t, m in self.sizing_meta.items()},
            "pr_states": {t: dict(v) for t, v in self.pr_states.items()},
            "task_bases": {t: dict(v) for t, v in self.task_bases.items()},
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
            self.sizing_meta = {
                str(t): _clean_meta(m) for t, m in (state.get("sizing_meta") or {}).items() if isinstance(m, dict)
            }
            self.pr_states = {
                str(t): dict(v) for t, v in (state.get("pr_states") or {}).items() if isinstance(v, dict)
            }
            self.task_bases = {
                str(t): {"base": str(v.get("base") or ""), "sha": str(v["sha"])}
                for t, v in (state.get("task_bases") or {}).items() if isinstance(v, dict) and v.get("sha")
            }
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
        self._load_actions()

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
        :meth:`resume_sizing` on the next start. Existing tests of the order's
        files are added to its read-only files.
        """
        order = work_order if isinstance(work_order, WorkOrder) else WorkOrder.model_validate(dict(work_order))
        task_id = order.task_id or self._next_task_id()
        repo = order.repo_root or (str(self.repo_root.resolve()) if self.repo_root else None)
        if repo:
            repo = str(Path(repo).resolve())
        order = order.model_copy(
            update={
                "task_id": task_id,
                "team_id": self._pick_team(order),
                "repo_root": repo,
                "read_files": _read_only_files(order.files, order.read_files, repo),
            }
        )
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
        await self.pin_base(task_id)
        directive = await self._size(order)
        if self.queue.tickets_for_task(task_id):
            return  # sized meanwhile by someone else (a second resume)
        self.queue.apply_sizing(directive)
        # recorded only once the tickets exist: a directive without tickets would look applied
        self.directives[task_id] = directive
        self.allocations[task_id] = directive.allocated_builders
        meta = self._sized_meta.pop(task_id, None)
        if meta is not None:
            self.sizing_meta[task_id] = meta
        self.queue.record_event(
            "sized",
            task_id,
            {"complexity": directive.complexity, "builders": directive.allocated_builders, "team": directive.team_id,
             "source": (meta or {}).get("source"), "ms": (meta or {}).get("ms")},
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

    def read_files_for(self, task_id: str) -> list[str]:
        """The files the task's builders may read but not change (empty for an unknown task)."""
        order = self.orders.get(task_id)
        return list(order.read_files) if order else []

    def task_repo(self, task_id: str) -> str | None:
        """The repository a task belongs to (queue column first, then the saved order)."""
        task = self.queue.get_task(task_id) or {}
        repo = task.get("repo_root")
        if not repo and task_id in self.orders:
            repo = self.orders[task_id].repo_root
        return str(repo) if repo else None

    # ------------------------------------------------------- base snapshots

    @property
    def snapshots_enabled(self) -> bool:
        """True when tasks are built on a clean snapshot of the publish branch (auto PRs + snapshots)."""
        return self.snapshots is not None and self.auto_pr and self.publisher is not None

    def _publish_base(self, task_id: str) -> str | None:
        """The branch the task's PR targets: the order's ``pr_base``, else the publisher's base (None: remote default)."""
        order = self.orders.get(task_id)
        base = getattr(order, "pr_base", None) if order is not None else None
        return base or getattr(self.publisher, "base", None) or None

    def _warn_user_folder(self, task_id: str, why: str) -> None:
        if task_id in self._snapshot_warned:
            return
        self._snapshot_warned.add(task_id)
        log.warning(
            "floor manager: %s is built from your working folder (%s); its patch may not apply to the "
            "remote branch (e.g. on Windows with core.autocrlf=true, or when the folder is on another branch)",
            task_id, why,
        )

    async def pin_base(self, task_id: str) -> dict[str, str] | None:
        """Pin the task to the current commit of its publish branch and create the snapshot.

        Idempotent: an already pinned task keeps its commit (a restart, a second
        sizing). Returns ``{"base", "sha"}``, or None when snapshots are off or
        unavailable (no git, no remote, fetch failed): the task is then built
        from the user's folder as before, with one warning.
        """
        pin = self.task_bases.get(task_id)
        if pin is not None:
            return dict(pin)
        if not self.snapshots_enabled:
            if self.auto_pr and self.publisher is not None:
                self._warn_user_folder(task_id, "base snapshots are not set up")
            return None
        try:
            base, sha = await _maybe_await(self.snapshots.resolve(self._publish_base(task_id)))
            pin = {"base": str(base), "sha": str(sha)}
            async with self._snapshot_lock:  # recorded under the lock: a concurrent prune keeps it
                path = await _maybe_await(self.snapshots.checkout(pin["sha"]))
                self.task_bases[task_id] = pin
                if path:
                    self._snapshot_paths[pin["sha"]] = Path(path)
        except Exception as exc:  # SnapshotUnavailable, or anything else: fall back, never stop the line
            self._warn_user_folder(task_id, f"no snapshot of the publish branch: {exc}")
            return None
        log.info("floor manager: %s builds on %s @ %s", task_id, base, str(sha)[:12])
        try:
            self.queue.record_event("base_pinned", task_id, dict(pin))
        except Exception as exc:  # pragma: no cover - bookkeeping is best effort
            log.debug("floor manager: could not record the pinned base: %s", exc)
        self.save()
        return dict(pin)

    def _can_pin_late(self, task_id: str) -> bool:
        """True when an unpinned task may still be pinned now: nothing of it was approved yet
        (approved diffs are relative to what they were built on, so they never switch base)."""
        if not self.snapshots_enabled or task_id in self._completed:
            return False
        try:
            return not self._ordered_diffs(task_id)
        except Exception:  # pragma: no cover - defensive
            return False

    async def task_base(self, task_id: str) -> Path | None:
        """The snapshot directory the task is built from (may re-create it), or None.

        None means "use the user's folder" (snapshots off/unavailable, or a task
        pinned before snapshots existed). A task whose pin failed at sizing (e.g.
        offline) is pinned again here as long as none of its work is approved yet.
        A snapshot pruned meanwhile (a retry or undo after the task finished) is
        checked out again at the pinned commit. A snapshot already verified clean
        in this run is reused without asking git again.
        """
        tid = str(task_id)
        if self.snapshots is None:
            return None
        if tid not in self.task_bases and self._can_pin_late(tid):
            await self.pin_base(tid)
        pin = self.task_bases.get(tid)
        if pin is None:
            if self.snapshots_enabled:
                self._warn_user_folder(tid, "the task has no pinned base commit")
            return None
        sha = pin["sha"]
        known = self._snapshot_paths.get(sha)
        if known is not None and not self._snapshot_lock.locked() and known.is_dir():
            return known
        try:
            async with self._snapshot_lock:
                path = Path(await _maybe_await(self.snapshots.checkout(sha)))
        except Exception as exc:
            self._snapshot_paths.pop(sha, None)
            self._warn_user_folder(tid, f"the snapshot of {sha[:12]} is unavailable: {exc}")
            return None
        self._snapshot_paths[sha] = path  # verified clean by checkout(): trusted for this run
        return path

    async def build_root(self, task_id: str) -> Path | None:
        """Where the task's files stand before its change: its snapshot, else its repo (user's folder)."""
        snap = await self.task_base(task_id)
        if snap is not None:
            return snap
        repo = self.task_repo(task_id)
        return Path(repo) if repo else self.repo_root

    @asynccontextmanager
    async def base_lease(self, task_id: str) -> AsyncIterator[Path | None]:
        """``async with manager.base_lease(tid) as root``: the task's snapshot (None: the user's folder),
        never pruned while the block runs (stacking, the patch check, a worker's ticket)."""
        root = await self.task_base(task_id)
        pin = self.task_bases.get(str(task_id)) if root is not None else None
        sha = pin["sha"] if pin else None
        if sha:
            self._snapshot_users[sha] = self._snapshot_users.get(sha, 0) + 1
        try:
            yield root
        finally:
            if sha:
                left = self._snapshot_users.get(sha, 1) - 1
                if left > 0:
                    self._snapshot_users[sha] = left
                else:
                    self._snapshot_users.pop(sha, None)

    async def _leased_root(self, task_id: str, fn: Any, *args: Any) -> Any:
        """Run ``fn(*args, root)`` in a thread on the task's build root, holding its snapshot."""
        async with self.base_lease(task_id) as snap:
            if snap is None:
                repo = self.task_repo(task_id)
                snap = Path(repo) if repo else self.repo_root
            return await asyncio.to_thread(lambda: fn(*args, snap))

    def _pin_done(self, tid: str) -> bool:
        """True when the task's snapshot is no longer needed (finished or given up)."""
        task = self.queue.get_task(tid)
        if task is None:
            return True
        state = _val(task.get("state"))
        if state in ("quarantined", "failed"):
            return True
        if tid not in self._completed:
            return False  # still building, or complete in the queue but its patch not yet written
        return not (self.is_publishing(tid) or (self.pr_states.get(tid) or {}).get("state") == "pending")

    def _snapshot_keep(self) -> set[str]:
        """Commits whose snapshot is still needed: unfinished tasks and snapshots in use right now."""
        keep = {sha for sha, n in self._snapshot_users.items() if n > 0}
        for tid, pin in self.task_bases.items():
            if not self._pin_done(tid):
                keep.add(pin["sha"])
        return keep

    def _trim_pins(self) -> bool:
        """Forget the pins nothing will read again: tasks whose PR is settled (opened/skipped,
        or no publisher) and failed tasks. Quarantined tasks keep theirs: an undo or retry rebuilds
        on the same commit, and a failed/retryable PR still needs it (``base_sha``)."""
        drop = []
        for tid in self.task_bases:
            task = self.queue.get_task(tid)
            state = _val(task.get("state")) if task else None
            pr = (self.pr_states.get(tid) or {}).get("state")
            if task is None or state == "failed":
                drop.append(tid)
            elif tid in self._completed and not self.is_publishing(tid) and (
                    self.publisher is None or not self.auto_pr or pr in ("opened", "skipped")):
                drop.append(tid)
        for tid in drop:
            self.task_bases.pop(tid, None)
        return bool(drop)

    async def prune_snapshots(self) -> list[Any]:
        """Remove the snapshots no open task uses (never touches the user's folder).

        Finished and quarantined tasks lose theirs; a retry or undo re-creates
        it from the pinned commit (:meth:`task_base`). Also removes snapshots of
        commits this manager does not know (an earlier database, a crash), and
        forgets the pins of settled tasks.
        """
        if self.snapshots is None:
            return []
        try:
            async with self._snapshot_lock:
                keep = self._snapshot_keep()
                removed = await _maybe_await(self.snapshots.prune(keep=keep))
                for sha in list(self._snapshot_paths):
                    if sha not in keep:
                        self._snapshot_paths.pop(sha, None)  # verify again on the next use
        except Exception as exc:  # pruning is housekeeping only
            log.warning("floor manager: could not prune base snapshots: %s", exc)
            return []
        if self._trim_pins():
            self.save()
        return list(removed or [])

    def _prune_later(self) -> None:
        """Start :meth:`prune_snapshots` in the background (needs a running loop; else skipped)."""
        if self.snapshots is None:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return  # the next start prunes
        self._spawn(self.prune_snapshots())

    async def _size(self, order: WorkOrder) -> TaskSizingDirective:
        task = {
            "task_id": order.task_id,
            "team_id": order.team_id,
            "title": order.title,
            "description": order.description,
            "files": list(order.files),
            "acceptance_criteria": list(order.acceptance_criteria),
            # calibration reads only this repository's past sizing outcomes
            "repo_root": order.repo_root,
        }
        started = time.monotonic()
        result = await _maybe_await(self.foreman.size_task_scope(task))
        # read right after the await (no other coroutine runs in between), so a
        # concurrent sizing cannot overwrite the source before it is captured
        ms = int(round((time.monotonic() - started) * 1000))
        last = getattr(self.foreman, "last_source", None)
        source = last.get("size_task_scope") if isinstance(last, Mapping) else None
        # the Foreman's size before calibration (outcomes learn from this, not the shifted size)
        raw = getattr(self.foreman, "last_raw_complexity", None)
        self._sized_meta[order.task_id] = _clean_meta({"source": source, "ms": ms, "raw": raw})
        if isinstance(result, TaskSizingDirective):
            return result
        return TaskSizingDirective.model_validate(result)

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
        record = getattr(self.queue, "record_approved_diff", None)
        if callable(record) and getattr(item, "ticket_id", None):
            try:  # later tickets of the task build on this diff (stacked tickets)
                record(item.ticket_id, item.submission.diff)
            except Exception as exc:  # pragma: no cover - bookkeeping is best effort
                log.debug("record_approved_diff(%s) failed: %s", item.ticket_id, exc)
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

    def report_task_progress(self, task_id: str, *, team_id: str | None = None) -> None:
        """Send the task's progress percent to the board; a board error never reaches the build.

        ``round(100 * approved / total)`` of the task's tickets, at most 95 until the
        patch is written (:meth:`_finish` records that in ``_completed``), then 100.
        """
        try:
            if task_id in self._completed:
                pct = 100
                note = "[Foundry] task finished"
            else:
                tickets = list(self.queue.tickets_for_task(task_id))
                if not tickets:
                    return
                approved = sum(1 for t in tickets if t.status == "done")
                pct = min(round(100 * approved / len(tickets)), 95)
                note = f"[Foundry] {approved}/{len(tickets)} tickets approved"
            d = self.directives.get(task_id)
            self.bridge.progress(task_id, note, team_id=d.team_id if d else team_id, pct=pct)
        except Exception as exc:
            log.warning("could not report the progress of %s: %s", task_id, exc)

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

    def _approved_parts(self, task_id: str) -> dict[str, str]:
        """Subtask id -> approved diff of the task (unordered)."""
        parts = dict(self.diffs.get(task_id, {}))
        if not parts:  # e.g. the manager's saved state was lost: use the diffs the queue kept
            stored = getattr(self.queue, "approved_diffs", None)
            got = stored(task_id) if callable(stored) else None
            if isinstance(got, (list, tuple)):
                parts = {str(x[0]): str(x[1]) for x in got if isinstance(x, (list, tuple)) and len(x) == 2}
        return parts

    def _ordered_diffs(self, task_id: str) -> list[str]:
        """The task's approved diffs in ticket build order (``order_index``; subtask id as fallback)."""
        parts = self._approved_parts(task_id)
        rank: dict[str, int] = {}
        fn = getattr(self.queue, "tickets_in_order", None)
        if callable(fn):
            try:
                rank = {str(t.subtask_id): i for i, t in enumerate(fn(task_id))}
            except Exception as exc:  # pragma: no cover - fall back to subtask order
                log.debug("tickets_in_order(%s) failed: %s", task_id, exc)
        keys = sorted(parts, key=lambda k: (rank.get(k, len(rank)), _subtask_order(k)))
        return [parts[k] for k in keys if parts[k]]

    def _approved_diff(self, task_id: str) -> str:
        """The task's approved diff, as rebuilt from the diffs kept for it."""
        return "".join(_with_newline(d) for d in self._ordered_diffs(task_id)) or self._task_diff(task_id)

    async def _finish(self, task_id: str, diff: str | None) -> dict[str, str]:
        if diff is None:
            diff = self._approved_diff(task_id)
        known = self.pr_summaries.get(task_id)
        if known is not None:
            # repair: the summary (and the board's review/done) are already recorded
            await self._write_patch(task_id, diff, known)
            if self.auto_pr and self.publisher is not None and task_id not in self.pr_states:
                # its summary was recorded before automatic PRs existed (or were on): never open one now
                self._set_pr(task_id, "skipped", error="finished before automatic PRs", stage="before_auto_pr")
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
        self.record_outcome(task_id)
        try:
            self.queue.store_pr_summary(task_id, summary)
        except Exception as exc:  # pragma: no cover - bookkeeping is best effort
            log.debug("store_pr_summary failed: %s", exc)
        self.report_task_progress(task_id)
        d = self.directives.get(task_id)
        if d is not None:
            self.bridge.review(task_id, d.team_id, "APPROVED", note="Foundry reviewer approved")
        self.bridge.done(task_id, summary.get("commit_message", ""))
        self._queue_publish(task_id)
        self.save()
        self._start_publish(task_id)
        self._prune_later()  # the publisher branches from the pinned commit, not the snapshot
        return summary

    # ------------------------------------------------------- pull requests

    def pr_state(self, task_id: str) -> dict[str, Any] | None:
        """The task's PR record (a copy), or None when no PR was tried."""
        st = self.pr_states.get(task_id)
        return dict(st) if st is not None else None

    def is_publishing(self, task_id: str) -> bool:
        """True while the task's background publish is running."""
        t = self._pr_tasks.get(task_id)
        return t is not None and not t.done()

    def _pr_patch(self, task_id: str) -> Path | None:
        """The task's applicable ``.patch`` (None when it was rejected, missing or empty)."""
        path = self.patch_paths.get(task_id)
        if path is None and self.patches_dir is not None and task_id in self.patch_names:
            path = self.patches_dir / f"{self.patch_names[task_id]}.patch"
        try:
            if path is not None and path.is_file() and path.read_text(encoding="utf-8").strip():
                return path
        except OSError:
            return None
        return None

    def _set_pr(self, task_id: str, state: str, **fields: Any) -> dict[str, Any]:
        old = self.pr_states.get(task_id) or {}
        rec = {
            "state": state,
            "url": fields.get("url"),
            "branch": fields.get("branch"),
            "error": fields.get("error"),
            "stage": fields.get("stage"),
            "commit": fields.get("commit"),
            "base": fields.get("base"),
            "attempts": int(fields.get("attempts", old.get("attempts", 0)) or 0),
        }
        self.pr_states[task_id] = rec
        return rec

    def _queue_publish(self, task_id: str) -> bool:
        """Record the task's PR as ``pending`` (or ``skipped``); True when a publish should start.

        Never queues a task whose PR was already opened, failed, skipped or is in flight.
        """
        if not self.auto_pr or self.publisher is None or task_id in self.pr_states:
            return False
        if self._pr_patch(task_id) is None:
            why = "the combined patch does not apply" if task_id in self.rejected_patches else "no patch to publish"
            self._set_pr(task_id, "skipped", error=why, stage="no_patch")
            return False
        self._set_pr(task_id, "pending", attempts=0)
        return True

    def _start_publish(self, task_id: str) -> asyncio.Task[Any] | None:
        st = self.pr_states.get(task_id)
        if st is None or st.get("state") != "pending" or self.publisher is None:
            return None
        running = self._pr_tasks.get(task_id)
        if running is not None and not running.done():
            return running
        t = asyncio.get_running_loop().create_task(self._publish(task_id))
        self._pr_tasks[task_id] = t
        self._pending.add(t)  # drain() (shutdown) waits for it
        t.add_done_callback(self._pending.discard)
        if self.snapshots is not None:
            # once the PR is settled the task's snapshot (and pin) are no longer needed
            t.add_done_callback(lambda _t: self._prune_later())
        return t

    def pr_request(self, task_id: str) -> dict[str, Any]:
        """``{title, body, draft}`` for the task's PR, from the Foreman's draft."""
        summary = self.pr_summaries.get(task_id) or {}
        order = self.orders.get(task_id)
        lines = [ln.strip() for ln in str(summary.get("commit_message") or "").splitlines() if ln.strip()]
        title = (lines[0] if lines else (order.title if order else task_id))[:200]
        body = str(summary.get("pr_body") or "").strip()
        if not body and len(lines) > 1:
            body = "\n".join(lines[1:])
        footer = f"Opened by Foundry (task {task_id})"
        brief = (self.pr_briefs.get(task_id) or "").strip()
        if self.pr_template:
            body_lines = body.splitlines()
            first = next((i for i, ln in enumerate(body_lines) if ln.strip()), None)
            if first is not None and re.fullmatch(r"#+\s*summary\s*#*", body_lines[first].strip(), re.IGNORECASE):
                body = "\n".join(body_lines[first + 1:]).strip()
            parts = [f"## Summary\n\n{body or (order.title if order else task_id)}"]
            if order and order.acceptance_criteria:
                crit = "\n".join(f"- {c}" for c in order.acceptance_criteria)
                parts.append(f"## Acceptance criteria\n\n{crit}")
            files: list[str] = []
            try:
                patch = self._pr_patch(task_id)
                if patch is not None:
                    text = patch.read_text(encoding="utf-8")
                    files = sorted({
                        m.group(1).strip() for m in re.finditer(r"^\+\+\+ b/(.+)$", text, re.MULTILINE)
                        if m.group(1).strip() != "/dev/null"
                    })
            except (OSError, UnicodeDecodeError):
                files = []
            if files:
                parts.append("## Files changed\n\n" + "\n".join(f"- `{f}`" for f in files))
            if brief:
                parts.append(f"## Brief (Gemma, advisory)\n\n{brief}")
            parts.append(f"---\n{footer}")
            return {"title": title, "body": "\n\n".join(parts) + "\n", "draft": self.pr_draft}
        if brief:
            section = f"## Brief (Gemma, advisory)\n\n{brief}"
            body = f"{body}\n\n{section}" if body else section
        body = f"{body}\n\n---\n{footer}\n" if body else f"{footer}\n"
        return {"title": title, "body": body, "draft": self.pr_draft}

    async def _make_brief(self, task_id: str, patch: Path) -> None:
        """Store Gemma's advisory PR brief for the task (never raises)."""
        gemma = getattr(self.foreman, "gemma", None)
        order = self.orders.get(task_id)
        if not self.gemma_notes or gemma is None or order is None or self.pr_briefs.get(task_id):
            return
        try:
            from foundry.gemma_notes import pr_brief

            patch_text = patch.read_text(encoding="utf-8")
            changed = sorted({
                m.group(1).strip() for m in re.finditer(r"^\+\+\+ b/(.+)$", patch_text, re.MULTILINE)
                if m.group(1).strip() != "/dev/null"
            })
            brief = await pr_brief(
                gemma, order.title + "\n\n" + order.description, order.acceptance_criteria,
                changed, patch_text, timeout=self.gemma_notes_timeout_s,
            )
            if brief and brief.strip():
                self.pr_briefs[task_id] = brief
        except Exception as exc:  # advisory only
            log.debug("floor manager: PR brief failed: %s", exc)

    async def _publish(self, task_id: str) -> dict[str, Any]:
        st = dict(self.pr_states.get(task_id) or {})
        attempts =int(st.get("attempts") or 0) + 1
        # branch + commit known (only the PR step failed), or a pending one whose
        # on_pushed was saved before a restart: it is on the remote -> open_pr only
        pushed = bool(st.get("branch") and (st.get("commit") or st.get("state") == "pending")) and callable(
            getattr(self.publisher, "open_pr", None))
        keep = {k: st.get(k) for k in ("branch", "commit", "base", "stage")} if pushed else {}
        self._set_pr(task_id, "pending", attempts=attempts, **keep)
        self.save()  # a crash from here on leaves it pending: retried once on the next start
        patch = self._pr_patch(task_id)
        if patch is None and not pushed:
            rec = self._set_pr(task_id, "skipped", error="no patch to publish", stage="no_patch", attempts=attempts)
            self.save()
            return rec
        if not pushed and patch is not None:
            held = await self._suite_hold(task_id, patch, attempts)
            if held is not None:
                return held
        order = self.orders.get(task_id)
        if self.gemma_notes and patch is not None:
            await self._make_brief(task_id, patch)
        req = self.pr_request(task_id)
        try:
            publish = self.publisher.publish
            kwargs: dict[str, Any] = {}
            params = _params(publish)
            if "draft" in params:
                kwargs["draft"] = self.pr_draft
            if "on_pushed" in params:
                kwargs["on_pushed"] = lambda branch, commit=None: self._pushed(task_id, branch, commit)
            pin = self.task_bases.get(task_id) or {}
            order_base = (getattr(order, "pr_base", None) if order else None) or pin.get("base") or None
            if order_base and "base" in params:
                kwargs["base"] = order_base
            if pin.get("sha") and "base_sha" in params:
                kwargs["base_sha"] = pin["sha"]  # build == publish: branch from the commit it was built on
            if pushed:
                # only the PR step failed last time: open it for the branch already pushed
                result = await _maybe_await(
                    self.publisher.open_pr(st["branch"], req, base=st.get("base") or order_base))
            else:
                result = await _maybe_await(
                    publish(task_id, order.title if order else req["title"], patch, req, **kwargs)
                )
        except asyncio.CancelledError:
            log.info("floor manager: publishing %s interrupted; it is retried once on the next start", task_id)
            raise
        except Exception as exc:  # publishing must never take the line down
            result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:500], "stage": "error"}
        ok = bool(_get(result, "ok"))
        url = _get(result, "pr_url")
        cur = self.pr_states.get(task_id) or {}  # on_pushed may have recorded the branch meanwhile
        branch = _get(result, "branch") or cur.get("branch") or keep.get("branch")
        extra = {"commit": _get(result, "commit") or cur.get("commit") or keep.get("commit"),
                 "base": _get(result, "base") or cur.get("base") or keep.get("base")}
        if ok:
            rec = self._set_pr(task_id, "opened", url=url, branch=branch, attempts=attempts, **extra)
            d = self.directives.get(task_id)
            try:
                self.bridge.progress(
                    task_id, f"[Foundry] PR opened: {url}" if url else f"[Foundry] pushed {branch}",
                    team_id=d.team_id if d else None,
                )
            except Exception as exc:  # pragma: no cover - the board is best effort
                log.debug("floor manager: PR note failed: %s", exc)
            log.info("floor manager: %s PR opened: %s", task_id, url or branch)
        else:
            err = str(_get(result, "error") or "publishing failed")[:500]
            rec = self._set_pr(task_id, "failed", url=url, branch=branch, error=err,
                               stage=_get(result, "stage"), attempts=attempts, **extra)
            log.warning("floor manager: %s PR not opened (%s): %s", task_id, rec["stage"], err)
        try:
            self.queue.record_event("pr_" + rec["state"], task_id, {k: v for k, v in rec.items() if v is not None})
        except Exception as exc:  # pragma: no cover - bookkeeping is best effort
            log.debug("floor manager: could not record the PR event: %s", exc)
        self.save()
        return rec

    async def _suite_hold(self, task_id: str, patch: Path, attempts: int) -> dict[str, Any] | None:
        """Run the whole-suite check; the failed PR record when the patch breaks tests, else None.

        An error in the check itself is logged and never holds the PR.
        """
        if os.environ.get("FOUNDRY_SUITE_GATE", "1").strip().lower() in {"0", "false", "no", "off"}:
            return None
        try:
            timeout_s = float(os.environ.get("FOUNDRY_SUITE_TIMEOUT_S", "900"))
            workdir = (self.patches_dir or patch.parent).parent / "suite"
            async with self.base_lease(task_id) as snap:
                root = snap
                if root is None:
                    repo = self.task_repo(task_id)
                    root = Path(repo) if repo else self.repo_root
                if root is None:
                    log.warning("floor manager: %s has no repository to run the whole suite on", task_id)
                    return None
                result = await asyncio.to_thread(
                    suite_check.check_patch, root, patch, timeout_s=timeout_s, workdir=workdir)
        except Exception as exc:  # the check must never stop a PR
            log.warning("floor manager: whole-suite check of %s failed: %s: %s", task_id, type(exc).__name__, exc)
            return None
        if result.ok:
            log.info("floor manager: %s %s", task_id, result.summary)
            return None
        listed = ", ".join(result.new_failures[:10])
        rec = self._set_pr(task_id, "failed", error=("new test failures: " + listed)[:500], stage="suite",
                           attempts=attempts)
        self.save()
        log.warning("floor manager: %s PR held: %s", task_id, result.summary)
        d = self.directives.get(task_id)
        try:
            self.bridge.progress(task_id, f"[Foundry] PR held: new test failures: {listed}",
                                 team_id=d.team_id if d else None)
        except Exception as exc:  # pragma: no cover - the board is best effort
            log.debug("floor manager: PR note failed: %s", exc)
        return rec

    def _pushed(self, task_id: str, branch: Any, commit: Any = None) -> None:
        """``on_pushed`` callback: the branch is on the remote; save it at once.

        A restart after this point (even after the PR was created but before
        the result was saved) goes through ``open_pr`` for this branch, which
        finds an existing PR instead of pushing a second branch.
        """
        st = self.pr_states.get(task_id)
        if st is None or not branch:
            return
        st["branch"] = str(branch)
        if commit:
            st["commit"] = str(commit)
        self.save()

    def resume_publishing(self, only: Any = None) -> list[str]:
        """After a restart: retry, once, a publish that was cut off (``pending`` without a result).

        Opened PRs are never opened again; failed ones are only retried by
        :meth:`retry_pr`. A publish interrupted twice is marked failed.
        """
        started = []
        for tid, st in sorted(self.pr_states.items(), key=lambda kv: _task_number(kv[0])):
            if st.get("state") != "pending" or self.is_publishing(tid):
                continue
            if only is not None and not only(tid):
                continue
            if int(st.get("attempts") or 0) >= 2:
                self._set_pr(tid, "failed", error="publishing was interrupted twice; use Retry",
                             stage="interrupted", attempts=st.get("attempts"),
                             **{k: st.get(k) for k in ("branch", "commit", "base")})
                continue
            if self._start_publish(tid) is not None:
                started.append(tid)
        self.save()
        if started:
            log.info("floor manager: publishing again %s", ", ".join(started))
        return started

    async def retry_pr(self, task_id: str) -> dict[str, Any]:
        """Publish a task whose PR failed (or was skipped) again; ``{ok, pr}`` or ``{ok: False, reason}``.

        Returns at once; the publish runs in the background.
        """
        if self.publisher is None:
            return {"ok": False, "reason": "pull requests are not set up (no publisher)"}
        st = self.pr_states.get(task_id)
        if task_id not in self.pr_summaries:
            return {"ok": False, "reason": f"{task_id} is not an approved task"}
        if st is not None and st.get("state") == "opened":
            return {"ok": False, "reason": "the PR is already open", "pr": dict(st)}
        if self.is_publishing(task_id) or (st is not None and st.get("state") == "pending"):
            return {"ok": False, "reason": "already publishing", "pr": dict(st or {})}
        reuse = st is not None and bool(st.get("branch"))
        if not reuse and self._pr_patch(task_id) is None:
            return {"ok": False, "reason": "no applicable patch to publish"}
        # the branch is already pushed when only the PR step failed: keep it so the
        # retry opens the PR for that branch instead of pushing a second "-2" branch
        keep = {k: st.get(k) for k in ("branch", "commit", "base", "stage")} if reuse else {}
        self._set_pr(task_id, "pending", attempts=0, **keep)
        self.save()
        self._start_publish(task_id)
        return {"ok": True, "pr": dict(self.pr_states[task_id])}

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
        # checked against what the task was built on: its snapshot of the publish branch
        # (as stored, LF), else the user's folder
        problem = await self._leased_root(task_id, _git_check, text) if text else None
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
                return "".join(_with_newline(str(x[1] if isinstance(x, (list, tuple)) and len(x) == 2 else x))
                               for x in got)
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


def _posix_path(path: Any) -> str:
    """A repo-relative path with forward slashes and no leading ``./``."""
    text = str(path).strip().replace("\\", "/")
    return text[2:] if text.startswith("./") else text


def _read_only_files(files: list[str], read_files: list[str], root: str | None) -> list[str]:
    """``read_files`` plus the existing tests of ``files``, minus whatever ``files`` lets the task edit."""
    editable = [_posix_path(f) for f in files]
    wanted = [_posix_path(f) for f in read_files]
    if root is not None:
        for path in editable:
            folder, _, name = path.rpartition("/")
            if not name.endswith(".py") or name.startswith("test_") or any(c in path for c in "*?["):
                continue
            nearby = f"{folder}/tests/test_{name}" if folder else f"tests/test_{name}"
            wanted += [t for t in (nearby, f"tests/test_{name}") if (Path(root) / t).is_file()]
    return [f for f in dict.fromkeys(wanted) if f and not any(f == e or fnmatchcase(f, e) for e in editable)]


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
            shutil.copytree(src, dst, symlinks=True, ignore=copy_ignore(SCRATCH_IGNORE, src))
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
            # no EOL conversion: the user's core.autocrlf=true must never change what is checked
            [git, "-c", "core.autocrlf=false", "-c", "core.eol=lf", "-c", "core.safecrlf=false",
             "apply", "--check", str(tmp / "combined.patch")],
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

    async def run(self, submission: Any, assignment: Any, *, workspace: str | Path | None = None,
                  base: str | Path | None = None) -> Any:
        """Validate ``submission`` in a scratch copy of the workspace.

        ``workspace`` (stacked tickets): a scratch directory the caller owns
        and already prepared (the repo plus the task's earlier approved
        diffs); the diff is applied and checked there directly, without a
        second copy.  It must never be the user's working tree.

        ``base``: copy this directory instead of ``base_workspace`` (the
        task's clean snapshot of the publish branch, see
        :meth:`FloorManager.task_base`); it is only read, never patched.
        """
        self.calls += 1
        allowed = list(assignment.allowed_files) or None
        if workspace is not None:
            return await self.gates.validate_async(submission, Path(workspace), allowed_files=allowed)
        try:
            tmp = Path(await asyncio.to_thread(tempfile.mkdtemp, prefix="foundry-gates-"))
        except OSError as exc:
            raise InfrastructureError(f"could not create a scratch workspace: {exc}") from exc
        try:
            ws = tmp / "ws"
            # copying a real repo is slow: keep it off the event loop
            src = Path(base) if base is not None else self.base_workspace
            await asyncio.to_thread(_copy_workspace, src, ws)
            return await self.gates.validate_async(submission, ws, allowed_files=allowed)
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
    builder_claude: Any = None,
    builder_retry_claude: Any = None,
    reviewer_claude: Any = None,
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
    publisher: Any = None,
    snapshots: Any = None,
) -> Factory:
    """Build queue, telemetry, limiter, context engine, Foreman, cache, gates, pools and manager.

    Args:
        config: a :class:`~foundry.config.FoundryConfig`.
        claude / gemma: the clients (simulated in phase 1).
        builder_claude: client for builder calls; None = use ``claude``.
        builder_retry_claude: client for builder attempts after the first; None = the builder client.
        reviewer_claude: client for reviewer calls (team and peer); None = use ``claude``.
        repo_root: what the context engine reads.
        workspace: directory gates copy and patch (defaults to ``repo_root``).
        bridge: a :class:`~foundry.board_bridge.BoardBridge` (default from
            ``config.board_bridge_mode``).
        gates: the gate runner the workers use; default
            ``WorkspaceGates(ValidationGates(checks=("syntax", "linter", "typecheck")), workspace)``.
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
        publisher: a :class:`~foundry.git_publisher.GitPublisher` that pushes
            each approved task to its own branch and opens a PR (only used
            when ``config.auto_pr``). None (default) publishes nothing; the
            service (``foundry.serve``) passes one.
        snapshots: a :class:`~foundry.base_snapshot.BaseSnapshots`: with automatic
            PRs on, each task is pinned to the commit of its publish branch and
            built (context, gates, stacking, patch check) from a clean checkout
            of it, and published from that commit. None builds from ``repo_root``.

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
    from foundry.telemetry import Telemetry
    from foundry.validation_gates import ValidationGates

    db = str(config.db_path)
    queue = FoundryQueue(db, clock=queue_clock,
                         max_retries=int(getattr(config, "max_retries", 3)))
    resumed = queue.resume()
    if resumed:
        log.info("foundry: resumed %d ticket(s) left claimed by a previous run", resumed)
    telemetry = Telemetry(queue, clock=telemetry_clock) if telemetry_clock else Telemetry(queue)
    limiter = limiter or RateLimiter(config.rpm, config.tpm, config.max_concurrent)
    context = ContextEngine(repo_root, gemma)
    foreman = ShopForeman(gemma, queue, telemetry, context, config)
    # sizing calibration: the Foreman reads past outcomes as
    # ``outcomes_provider(repo=None, limit=50) -> list[dict]`` (newest first)
    foreman.outcomes_provider = queue.sizing_outcomes
    if gates is None:
        gates = WorkspaceGates(
            ValidationGates(
                checks=("syntax", "linter", "typecheck"),
                typecheck=getattr(config, "typecheck", "auto"),
            ),
            workspace or repo_root,
        )
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
        publisher=publisher,
        auto_pr=bool(getattr(config, "auto_pr", True)) and publisher is not None,
        pr_draft=bool(getattr(config, "pr_draft", False)),
        snapshots=snapshots,
        gemma_notes=bool(getattr(config, "gemma_notes", False)),
        pr_template=bool(getattr(config, "pr_template", False)),
        gemma_notes_timeout_s=float(getattr(config, "gemma_notes_timeout_s", 90.0)),
    )
    # the Foreman reads the action log so it never prescribes RETRY_TASK twice for one task
    foreman.actions_provider = manager.actions
    pool_extra: dict[str, Any] = {}
    if dispatch_repo is not None:
        if "repo_root" not in _params(TeamPool):
            raise TypeError("this TeamPool cannot scope dispatch to one repository (no repo_root)")
        pool_extra["repo_root"] = str(Path(dispatch_repo).resolve())
    if snapshots is not None and "base_for" in _params(TeamPool):
        pool_extra["base_for"] = manager.task_base
        if "base_lease" in _params(TeamPool):
            pool_extra["base_lease"] = manager.base_lease
    pools = {
        team: TeamPool(
            team,
            config.builders_per_team,
            queue=queue,
            client=claude,
            builder_client=builder_claude,
            builder_retry_client=builder_retry_claude,
            reviewer_client=reviewer_claude,
            limiter=limiter,
            telemetry=telemetry,
            context_engine=context,
            gates=gates,
            cache=None,
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
    for pool in pools.values():
        for builder in getattr(pool, "builders", ()):
            builder.read_files_for = manager.read_files_for
    for pool in pools.values():
        attach = getattr(pool, "set_on_ticket_change", None)
        if callable(attach):
            attach(manager.report_task_progress)
    return Factory(config, queue, telemetry, limiter, context, foreman, None, gates, pools, manager, bridge, claude, gemma)
