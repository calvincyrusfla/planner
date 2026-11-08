"""Board bridge: reports Foundry events to the existing Agent Department board.

Foundry does not replace the Agent Department (``department/``, Node). It feeds
it. Every Foundry event that matters to a human (a task was sized, a builder
made progress, a reviewer gave a verdict, the Foreman raised an escalation, a
task is complete) is mapped onto one of the department's serialisable
operations (``department/lib/ops.mjs``)::

    addTask        {title, description, size, builders}
    updateTask     {id, patch, by}
    assignTeam     {taskId, teamId, focus, by}
    reportProgress {taskId, teamId, agentId, progress, message, status}
    reviewTask     {taskId, teamId, verdict, note, issues, by}

Transports, in order (``mode="auto"``):

1. the local dashboard: ``POST http://127.0.0.1:4300/api/op`` with ``{type, args}``
   (``npm run dashboard``; the dashboard itself forwards to the shared board
   when one is configured);
2. the department CLI: ``node department/dept.mjs ...``;
3. nothing: the op is dropped with a single warning.

``mode="dry"`` records every op in :attr:`BoardBridge.ops` without any I/O (used
by the tests and by ``python -m foundry.main --simulate``); ``mode="off"`` drops
everything silently.

The bridge must never block or crash the factory. In ``auto`` mode ops are put
on an in-memory queue and sent by one daemon thread with bounded timeouts; every
error is caught and logged. Board task ids (``T-0xx``) are resolved inside that
thread, so a later op for the same Foundry task waits for its ``addTask``
without the caller waiting at all.

Because the sender is a daemon thread, ops still queued when the process exits
are lost. :meth:`BoardBridge.close` (called by ``Factory.close``) therefore
drains the queue first: it keeps waiting as long as ops keep completing (a slow
board, e.g. the dashboard forwarding to the shared Netlify board, is waited for)
and gives up only when nothing completes for :attr:`BoardBridge.stall_timeout`
seconds or the overall cap is reached, logging what was left unsent.
"""

from __future__ import annotations

import json
import logging
import os
import re
import queue as queue_mod
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request

# Loopback calls to the local dashboard never go through a system/env proxy.
_LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

log = logging.getLogger(__name__)

BridgeMode = Literal["auto", "off", "dry"]

#: At most one activity update per agent is sent in this many seconds.
AGENT_STATUS_INTERVAL = 10.0

DEFAULT_DASHBOARD_URL = "http://127.0.0.1:4300"
#: The dashboard forwards every op to the shared board when one is configured;
#: a cold Netlify function can take several seconds, so op POSTs get this long.
DEFAULT_OP_TIMEOUT = 20.0
#: Upper bound on how long close()/flush() wait for queued ops at exit.
DEFAULT_FLUSH_TIMEOUT = 120.0
REPO_ROOT = Path(__file__).resolve().parent.parent
DEPT_CLI = REPO_ROOT / "department" / "dept.mjs"

#: Foundry reviewer verdict -> department verdict word.
VERDICT_FOR = {"APPROVED": "approved", "REVISE": "changes", "REJECTED": "changes"}


@dataclass
class BoardOp:
    """One department operation, before Foundry task ids are resolved.

    ``foundry_task`` names the Foundry task the op is about; the sender replaces
    the ``taskId``/``id`` placeholder in ``args`` with the board's task id.
    """

    type: str
    args: dict[str, Any]
    foundry_task: str | None = None
    #: arg name that should receive the board task id ("taskId" or "id").
    task_key: str | None = None
    #: addTask only: the Foundry team that must end up as the board task's only team.
    team_id: str | None = None
    focus: str = ""
    #: reviewTask only: send one verdict per team on the board task (see BoardBridge.board_teams).
    fanout: bool = False

    def resolved(self, board_id: str | None) -> dict[str, Any]:
        """Return ``{type, args}`` with the board task id filled in."""
        args = dict(self.args)
        if self.task_key:
            args[self.task_key] = board_id
        return {"type": self.type, "args": args}


@dataclass
class _Stats:
    sent: int = 0
    failed: int = 0
    dropped: int = 0
    transport: str = "none"
    warnings: list[str] = field(default_factory=list)


class BoardBridge:
    """Maps Foundry events onto Agent Department board operations.

    Args:
        mode: ``"auto"`` (dashboard, then CLI, then no-op), ``"dry"`` (record
            ops in :attr:`ops`, no I/O) or ``"off"`` (drop everything).
        dashboard_url: base URL of the local dashboard (default:
            ``FOUNDRY_DASHBOARD_URL``, else :data:`DEFAULT_DASHBOARD_URL`).
        timeout: timeout in seconds for reading the board state from the
            dashboard (and the minimum for everything else).
        op_timeout: timeout in seconds for one op POST to the dashboard (it
            may forward to the shared board) and for one CLI call. A POST that
            times out after it was sent is NOT replayed through the CLI (the
            board may have applied it); an addTask is then looked up on the
            board by its Foundry tag.
        stall_timeout: flush()/close() give up when no queued op completes
            for this long (default: enough for one op through both transports).
        node_bin: Node executable for the CLI fallback.
        cli_path: path to ``department/dept.mjs``.
        dry_autoassign: dry mode only: the teams the simulated board
            auto-assigns to each new task (so tests can see the bridge
            unassign them); the real board's answer is used otherwise.
        dry_busy: dry mode only: teams the simulated board has on other
            open tasks (the bridge must never move them).
    """

    def __init__(
        self,
        mode: BridgeMode = "auto",
        *,
        dashboard_url: str | None = None,
        timeout: float = 3.0,
        op_timeout: float = DEFAULT_OP_TIMEOUT,
        stall_timeout: float | None = None,
        node_bin: str = "node",
        cli_path: str | Path = DEPT_CLI,
        dry_autoassign: tuple[str, ...] | list[str] = (),
        dry_busy: tuple[str, ...] | list[str] = (),
        agent_status_interval: float = AGENT_STATUS_INTERVAL,
    ) -> None:
        if mode not in ("auto", "off", "dry"):
            raise ValueError(f"unknown board bridge mode {mode!r}")
        self.mode: BridgeMode = mode
        self.dashboard_url = (dashboard_url or os.environ.get("FOUNDRY_DASHBOARD_URL") or DEFAULT_DASHBOARD_URL).rstrip("/")
        self.timeout = timeout
        self.op_timeout = max(float(op_timeout), float(timeout))
        # one op can take: a state read (dashboard + CLI json) and the op (dashboard + CLI)
        self.stall_timeout = (
            float(stall_timeout) if stall_timeout is not None else 2 * self.op_timeout + 2 * self._cli_timeout() + self.timeout
        )
        self.node_bin = node_bin
        self.cli_path = Path(cli_path)
        #: Every op the bridge produced, resolved (dry mode) or attempted (auto).
        self.ops: list[dict[str, Any]] = []
        #: Foundry task id -> board task id.
        self.board_ids: dict[str, str] = {}
        self.stats = _Stats()
        self._dry_seq = 0
        self._lock = threading.Lock()
        self._q: queue_mod.Queue[BoardOp | None] = queue_mod.Queue()
        self._thread: threading.Thread | None = None
        self._warned = False
        self._dashboard_down = False
        self._registered: list[str] = []
        self.dry_autoassign = tuple(dry_autoassign)
        self.dry_busy = frozenset(dry_busy)
        #: Foundry task id -> the board teams left on its board task (who must sign off).
        self.board_teams: dict[str, list[str]] = {}
        self._dry_live: dict[str, list[str]] = {}
        #: Seconds between two activity updates of one agent (see :meth:`agent_status`).
        self.agent_status_interval = max(0.0, float(agent_status_interval))
        self._agent_lock = threading.Lock()
        #: agent id -> when its last activity update was sent (monotonic clock).
        self._agent_sent: dict[str, float] = {}
        #: agent id -> its newest update waiting for the end of the interval.
        self._agent_held: dict[str, BoardOp] = {}
        self._agent_timers: dict[str, threading.Timer] = {}

    # ------------------------------------------------------------------ events

    def register_task(
        self,
        task_id: str,
        title: str,
        description: str = "",
        *,
        complexity: str = "S",
        builders: int = 1,
        team_id: str | None = None,
    ) -> None:
        """A work order was sized: create the board task with the Foundry team as its only team.

        The task is always registered as size ``small`` with ``builders`` = the
        Foundry allocation (a ``large`` board task would grab every free team).
        Whatever teams the board auto-assigns are then unassigned, except the
        Foundry team (assigned if the board did not pick it), so the board's
        sign-off only waits for that team's verdict.
        """
        if task_id in self.board_ids or task_id in self._registered:
            return  # already on the board (e.g. after a restart)
        self._registered.append(task_id)
        desc = f"[Foundry {task_id} · {complexity}] {description}".strip()
        self._emit(
            BoardOp(
                "addTask",
                {"title": title, "description": desc, "size": "small", "builders": max(1, min(5, int(builders)))},
                foundry_task=task_id,
                team_id=team_id,
                focus=f"Foundry {complexity}",
            )
        )

    def restore(self, board_ids: dict[str, str], board_teams: dict[str, list[str]] | None = None) -> None:
        """Reload Foundry task id -> board task id (and its board teams) after a restart."""
        with self._lock:
            self.board_ids.update({str(k): str(v) for k, v in board_ids.items()})
            for k in board_ids:
                if k not in self._registered:
                    self._registered.append(str(k))
            for k, teams in (board_teams or {}).items():
                self.board_teams[str(k)] = [str(t) for t in teams]
            # keep dry ids unique after a restart
            for v in self.board_ids.values():
                m = re.match(r"^DRY-(\d+)$", v)
                if m:
                    self._dry_seq = max(self._dry_seq, int(m.group(1)))

    def _team_fixups(self, op: BoardOp, assigned: list[str], foundry_team_state: str) -> list[BoardOp]:
        """Ops that make the board task's team list safe, and record who must sign off.

        ``foundry_team_state`` is where the board has the Foundry team:
        ``"ours"`` (the board put it on this task), ``"free"``, ``"busy"`` (on
        another task) or ``"unknown"`` (board state unreadable; treated as busy).

        * ours/free: assign it if needed and unassign the other teams the board
          just auto-assigned (they were free a moment ago), so only the Foundry
          team signs off;
        * busy/unknown: never move a team; leave the board's assignment as it
          is and have every team on the board task sign off at review time.
        """
        ftask = op.foundry_task or ""
        team = op.team_id
        if not team:
            self.board_teams[ftask] = list(assigned)
            return []
        if foundry_team_state in ("ours", "free") or team in assigned:
            out = []
            if team not in assigned:
                out.append(
                    BoardOp(
                        "assignTeam",
                        {"taskId": None, "teamId": team, "focus": op.focus, "by": "foundry"},
                        foundry_task=ftask,
                        task_key="taskId",
                    )
                )
            out += [
                BoardOp("unassignTeam", {"taskId": None, "teamId": t, "by": "foundry"}, foundry_task=ftask, task_key="taskId")
                for t in assigned
                if t != team
            ]
            self.board_teams[ftask] = [team]
            return out
        self.board_teams[ftask] = list(assigned)
        log.info(
            "board bridge: team %s is busy on another board task; %s keeps the board's teams %s",
            team, ftask, ", ".join(assigned) or "(none, queued)",
        )
        return []

    def _expand(self, op: BoardOp) -> list[BoardOp]:
        """Resolve board-team dependent ops at send time.

        * a fan-out reviewTask becomes one verdict per team on the board task;
        * a reportProgress for a Foundry team that is *not* on that board task
          (it was busy elsewhere, G3) keeps only the message: no agentId,
          teamId or status, so it never touches another task's team.
        """
        if op.type == "reportProgress" and op.team_id:
            teams = self.board_teams.get(op.foundry_task or "")
            if teams is not None and op.team_id not in teams:
                args = {k: v for k, v in op.args.items() if k not in ("agentId", "teamId", "status")}
                return [BoardOp(op.type, args, foundry_task=op.foundry_task, task_key=op.task_key)]
            return [op]
        if not op.fanout:
            return [op]
        # Review: sign-off teams come from the LIVE board (the dispatcher may have
        # given a queued board task a freed team since it was added).
        ftask = op.foundry_task or ""
        teams = self._sign_off_teams(ftask)
        if teams is None:  # board state unreadable: what we knew at add time
            known = self.board_teams.get(ftask)
            teams = list(known) if known is not None else [op.args.get("teamId")]
        else:
            self.board_teams[ftask] = list(teams)
        if not teams:
            # still queued with no team: there is nobody to sign off; done() goes straight through
            log.info("board bridge: %s has no team on the board; skipping review, sending done directly", ftask)
            return []
        out = [BoardOp("updateTask", {"id": None, "patch": {"status": "review"}, "by": "foundry"}, foundry_task=ftask, task_key="id")]
        for t in teams:
            args = dict(op.args, teamId=t, by=f"{t}-6" if op.args.get("by") == f"{op.team_id}-6" else op.args.get("by"))
            out.append(BoardOp(op.type, args, foundry_task=ftask, task_key=op.task_key))
        return out

    def dry_dispatch(self, task_id: str, teams: list[str]) -> None:
        """Dry mode: simulate the board's dispatcher putting ``teams`` on a Foundry task's board task."""
        self._dry_live[task_id] = list(teams)

    def _sign_off_teams(self, task_id: str) -> list[str] | None:
        """Teams on the task's board task right now (None when the board state can't be read)."""
        if self.mode == "dry":
            live = self._dry_live.get(task_id)
            return list(live) if live is not None else self.board_teams.get(task_id)
        board_id = self.board_ids.get(task_id)
        if not board_id:
            return None
        state = self._fetch_state()
        teams = state.get("teams") if isinstance(state, dict) else None
        if not isinstance(teams, list):
            return None
        return [str(t["id"]) for t in teams if isinstance(t, dict) and t.get("id") and t.get("taskId") == board_id]

    def progress(
        self,
        task_id: str,
        message: str,
        *,
        team_id: str | None = None,
        agent_id: str | None = None,
        pct: int | None = None,
        status: str | None = None,
    ) -> None:
        """Report progress (a builder started, a gate failed, a retry ...)."""
        args: dict[str, Any] = {"taskId": None, "message": message}
        if team_id:
            args["teamId"] = team_id
        if agent_id:
            args["agentId"] = agent_id
        if pct is not None:
            args["progress"] = max(0, min(100, int(pct)))
        if status:
            args["status"] = status
        team = team_id or (agent_id.rsplit("-", 1)[0] if agent_id and "-" in agent_id else None)
        self._emit(BoardOp("reportProgress", args, foundry_task=task_id, task_key="taskId", team_id=team))

    def agent_status(
        self,
        task_id: str,
        agent_id: str,
        work: str,
        *,
        status: str = "working",
        team_id: str | None = None,
    ) -> None:
        """Report what one agent is doing (no progress message), coalesced per agent.

        At most one update per agent is sent every ``agent_status_interval`` seconds.
        An update inside the interval is held and replaced by any newer one, and the
        newest is sent when the interval ends (or at :meth:`flush`), so a final idle
        status is never lost.
        """
        if self.mode == "off":
            return
        args: dict[str, Any] = {"taskId": None, "agentId": agent_id, "work": work, "status": status}
        if team_id:
            args["teamId"] = team_id
        team = team_id or (agent_id.rsplit("-", 1)[0] if "-" in agent_id else None)
        op = BoardOp("reportProgress", args, foundry_task=task_id, task_key="taskId", team_id=team)
        now = time.monotonic()
        with self._agent_lock:
            last = self._agent_sent.get(agent_id)
            wait = 0.0 if last is None else last + self.agent_status_interval - now
            if wait > 0 or agent_id in self._agent_held:
                self._agent_held[agent_id] = op
                self._schedule_agent_update(agent_id, max(0.0, wait))
                return
            self._agent_sent[agent_id] = now
        self._emit(op)

    def _schedule_agent_update(self, agent_id: str, delay: float) -> None:
        """Start the agent's timer unless one is running. Call with ``_agent_lock`` held."""
        if agent_id in self._agent_timers:
            return
        try:
            timer = threading.Timer(delay, self._send_agent_update, args=(agent_id,))
            timer.daemon = True
            timer.start()
        except Exception as exc:  # pragma: no cover - interpreter shutting down; flush() still sends it
            log.warning("board bridge: no timer for %s's activity (%s)", agent_id, exc)
            return
        self._agent_timers[agent_id] = timer

    def _send_agent_update(self, agent_id: str) -> None:
        """The agent's interval ended: send its newest held update."""
        with self._agent_lock:
            self._agent_timers.pop(agent_id, None)
            op = self._agent_held.pop(agent_id, None)
            if op is None:
                return
            self._agent_sent[agent_id] = time.monotonic()
        self._emit(op)

    def _send_held_agent_updates(self) -> None:
        """Send every held activity update now, so a final idle status survives a shutdown."""
        with self._agent_lock:
            held = dict(self._agent_held)
            self._agent_held.clear()
            timers = list(self._agent_timers.values())
            self._agent_timers.clear()
            now = time.monotonic()
            for agent_id in held:
                self._agent_sent[agent_id] = now
        for timer in timers:
            timer.cancel()
        for op in held.values():
            self._emit(op)

    def escalation(self, alert: Any) -> None:
        """Post a Foreman escalation as ``[Foreman] <severity> <category>: <diagnosis>``."""
        target = _get(alert, "target_entity")
        task_id = _get(target, "task_id")
        team_id = _get(target, "team_id")
        msg = f"[Foreman] {_enum(_get(alert, 'severity'))} {_enum(_get(alert, 'category'))}: {_get(alert, 'diagnosis')}"
        action = _enum(_get(alert, "prescribed_action"))
        if action and action != "NONE":
            msg += f" -> {action}"
        if task_id is None:
            # The board needs a task; use any task Foundry registered for this team.
            task_id = self._registered[-1] if self._registered else None
        if task_id is None:
            log.info("board bridge: escalation without a task, not posted: %s", msg)
            return
        self.progress(task_id, msg, team_id=team_id)

    def review(self, task_id: str, team_id: str, verdict: str, *, note: str = "", issues: int = 0, by: str | None = None) -> None:
        """Send the task to review and record a verdict for every team on its board task.

        The teams are read from the live board when the op is sent (see
        :meth:`_expand`); a board task that is still queued without a team gets
        no review at all, and :meth:`done` closes it directly.
        """
        self._emit(
            BoardOp(
                "reviewTask",
                {
                    "taskId": None,
                    "teamId": team_id,
                    "verdict": VERDICT_FOR.get(verdict, "changes"),
                    "note": note,
                    "issues": max(0, int(issues)),
                    "by": by or f"{team_id}-6",
                },
                foundry_task=task_id,
                task_key="taskId",
                team_id=team_id,
                fanout=True,
            )
        )

    def done(self, task_id: str, summary: str = "") -> None:
        """Mark the board task done (after the reviewer approved)."""
        if summary:
            self.progress(task_id, summary[:300], pct=100)
        self._emit(BoardOp("updateTask", {"id": None, "patch": {"status": "done"}, "by": "foundry"}, foundry_task=task_id, task_key="id"))

    def quarantined(self, task_id: str, reason: str) -> None:
        """Report a quarantined task as blocked on the board."""
        self._emit(
            BoardOp("updateTask", {"id": None, "patch": {"status": "paused"}, "by": "foundry"}, foundry_task=task_id, task_key="id")
        )
        self.progress(task_id, f"[Foundry] quarantined: {reason}")

    def resumed(self, task_id: str, note: str = "") -> None:
        """Report a task back on the line (quarantine undone or retried) as active on the board."""
        self._emit(
            BoardOp("updateTask", {"id": None, "patch": {"status": "active"}, "by": "foundry"}, foundry_task=task_id, task_key="id")
        )
        if note:
            self.progress(task_id, note)

    # --------------------------------------------------------------- plumbing

    def _cli_timeout(self) -> float:
        return max(self.op_timeout, 10.0)

    @property
    def pending(self) -> int:
        """Ops queued (or being sent) that have not finished yet."""
        return self._q.unfinished_tasks

    def flush(self, timeout: float | None = None) -> bool:
        """Wait until every queued op was sent (or failed). Returns True when drained.

        Held agent activity updates are queued first. Waits as long as ops keep
        completing, up to ``timeout`` seconds in total
        (default :data:`DEFAULT_FLUSH_TIMEOUT`); gives up early when no op
        completes for :attr:`stall_timeout` seconds. Never raises.
        """
        self._send_held_agent_updates()
        if self.mode != "auto":
            return True
        thread = self._thread
        if self._q.unfinished_tasks == 0:
            return True
        if thread is None or not thread.is_alive():
            if self._q.unfinished_tasks:
                self._ensure_thread()  # ops queued but nobody sending them
                thread = self._thread
        cap = DEFAULT_FLUSH_TIMEOUT if timeout is None else max(0.0, float(timeout))
        start = time.monotonic()
        deadline = start + cap
        last_left = self._q.unfinished_tasks
        last_change = start
        announced = False
        while True:
            left = self._q.unfinished_tasks
            if left == 0:
                return True
            now = time.monotonic()
            if left != last_left:
                last_left, last_change = left, now
            if now >= deadline or now - last_change >= self.stall_timeout:
                break
            if not announced and now - start >= 1.0:
                announced = True
                log.info("board bridge: waiting for %d board op(s) to reach the Agent Department board", left)
            time.sleep(min(0.02, max(0.0, deadline - now)))
        left = self._q.unfinished_tasks
        warning = (
            f"Agent Department board: {left} op(s) not delivered before exit "
            f"(board too slow or not answering); the board may not show the latest Foundry state."
        )
        self.stats.warnings.append(warning)
        log.warning(warning)
        return False

    def close(self, timeout: float | None = None) -> bool:
        """Drain the queue (see :meth:`flush`) and stop the sender thread. Returns True when drained."""
        drained = self.flush(timeout)
        thread = self._thread
        if thread is not None:
            self._q.put(None)
            thread.join(timeout=1.0 if drained else 0.1)
            self._thread = None
        return drained

    def _emit(self, op: BoardOp) -> None:
        """Never raises."""
        try:
            if self.mode == "off":
                return
            if self.mode == "dry":
                for one in self._expand(op):
                    self._send_dry(one)
                return
            self._ensure_thread()
            self._q.put_nowait(op)
        except Exception as exc:  # pragma: no cover - defensive
            self.stats.dropped += 1
            log.warning("board bridge: dropped %s (%s)", op.type, exc)

    def _send_dry(self, op: BoardOp) -> None:
        board_id = self._board_id_for(op)
        resolved = op.resolved(board_id)
        with self._lock:
            self.ops.append(resolved)
            if op.type == "addTask" and op.foundry_task:
                self._dry_seq += 1
                self.board_ids[op.foundry_task] = f"DRY-{self._dry_seq:03d}"
        self.stats.sent += 1
        self.stats.transport = "dry"
        if op.type == "addTask":
            assigned = list(self.dry_autoassign)
            if op.team_id in assigned:
                where = "ours"
            elif op.team_id in self.dry_busy:
                where = "busy"
            else:
                where = "free"
            for fix in self._team_fixups(op, assigned, where):
                self._send_dry(fix)

    def _board_id_for(self, op: BoardOp) -> str | None:
        if not op.task_key:
            return None
        if op.foundry_task and op.foundry_task in self.board_ids:
            return self.board_ids[op.foundry_task]
        return None

    def _ensure_thread(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run, name="foundry-board-bridge", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        while True:
            op = self._q.get()
            try:
                if op is None:
                    return
                for one in self._expand(op):
                    self._deliver(one)
            except Exception as exc:  # never let the thread die
                self.stats.failed += 1
                log.warning("board bridge: %s failed: %s", getattr(op, "type", "?"), exc)
            finally:
                self._q.task_done()

    def _deliver(self, op: BoardOp) -> None:
        if op.task_key and op.foundry_task not in self.board_ids:
            self.stats.dropped += 1
            log.info("board bridge: %s skipped, Foundry task %s is not on the board", op.type, op.foundry_task)
            return
        payload = op.resolved(self._board_id_for(op))
        with self._lock:
            self.ops.append(payload)
        result = self._post(payload)
        transport = "dashboard"
        if result is _UNAVAILABLE:
            result = self._cli(payload)
            transport = "cli"
        if result is _UNAVAILABLE:
            self.stats.dropped += 1
            if not self._warned:
                self._warned = True
                warning = "Agent Department board not reachable (dashboard and CLI); Foundry keeps running without it."
                self.stats.warnings.append(warning)
                log.warning(warning)
            return
        if result is _UNKNOWN:
            self.stats.failed += 1
            result = None
            if op.type == "addTask" and op.foundry_task:
                result = self._find_added_task(op)
        else:
            self.stats.sent += 1
            self.stats.transport = transport
        if op.type == "addTask" and op.foundry_task and isinstance(result, dict):
            task = result.get("task") if isinstance(result.get("task"), dict) else result
            board_id = task.get("id") if isinstance(task, dict) else None
            if board_id:
                with self._lock:
                    self.board_ids[op.foundry_task] = str(board_id)
                assigned = [str(t) for t in (result.get("assigned") or []) if t]
                where = "ours" if op.team_id in assigned else self._team_state(op.team_id, str(board_id))
                for fix in self._team_fixups(op, assigned, where):
                    try:
                        self._deliver(fix)
                    except Exception as exc:  # never let a fix-up stop the sender
                        self.stats.failed += 1
                        log.warning("board bridge: %s failed: %s", fix.type, exc)

    def _find_added_task(self, op: BoardOp) -> dict[str, Any] | None:
        """After an addTask timed out: the board task it created, if the board has it.

        Matches the newest open board task with the same title and Foundry tag
        that no other Foundry task already maps to; returns an addTask-shaped
        result (``assigned`` = the teams on it now) or None.
        """
        state = self._fetch_state()
        tasks = state.get("tasks") if isinstance(state, dict) else None
        if not isinstance(tasks, list):
            return None
        title, desc = op.args.get("title"), str(op.args.get("description", ""))
        taken = set(self.board_ids.values())
        found = [
            t for t in tasks
            if isinstance(t, dict) and t.get("id") and str(t["id"]) not in taken
            and t.get("title") == title and str(t.get("description", "")) == desc
            and t.get("status") not in ("done", "cancelled")
        ]
        if not found:
            log.info("board bridge: %s not found on the board after the addTask timeout", op.foundry_task)
            return None
        task = max(found, key=lambda t: _task_number(str(t["id"])))
        teams = state.get("teams") if isinstance(state.get("teams"), list) else []
        assigned = [str(t["id"]) for t in teams if isinstance(t, dict) and t.get("id") and t.get("taskId") == task["id"]]
        log.info("board bridge: %s found on the board as %s after the addTask timeout", op.foundry_task, task["id"])
        return {"task": {"id": str(task["id"])}, "assigned": assigned}

    def _team_state(self, team_id: str | None, board_id: str) -> str:
        """Where the board has ``team_id``: ours / free / busy / unknown (read from the board state)."""
        if not team_id:
            return "unknown"
        state = self._fetch_state()
        teams = state.get("teams") if isinstance(state, dict) else None
        if not isinstance(teams, list):
            return "unknown"
        for t in teams:
            if isinstance(t, dict) and t.get("id") == team_id:
                task = t.get("taskId")
                if task == board_id:
                    return "ours"
                return "free" if not task else "busy"
        return "unknown"

    def _fetch_state(self) -> dict[str, Any] | None:
        """The board state: ``GET /api/state`` on the dashboard, else ``dept.mjs json``."""
        if not self._dashboard_down:
            try:
                with _LOCAL_OPENER.open(f"{self.dashboard_url}/api/state", timeout=self.timeout) as resp:
                    body = json.loads(resp.read() or b"{}")
                if isinstance(body, dict):
                    return body.get("result") if isinstance(body.get("result"), dict) else body
            except (urllib.error.URLError, OSError, TimeoutError, ValueError):
                pass
        node = shutil.which(self.node_bin)
        if node is None or not self.cli_path.exists():
            return None
        try:
            proc = subprocess.run(
                [node, str(self.cli_path), "json"], capture_output=True, text=True,
                encoding="utf-8", errors="replace",
                timeout=self._cli_timeout(), cwd=str(self.cli_path.parent.parent),
            )
            return json.loads(proc.stdout) if proc.returncode == 0 else None
        except (OSError, subprocess.SubprocessError, ValueError):
            return None

    def _post(self, payload: dict[str, Any]) -> Any:
        """POST /api/op. Returns the op result, or _UNAVAILABLE when the dashboard is down."""
        if self._dashboard_down:
            return _UNAVAILABLE
        req = urllib.request.Request(
            f"{self.dashboard_url}/api/op",
            data=json.dumps(payload).encode(),
            headers={"content-type": "application/json"},
            method="POST",
        )
        try:
            with _LOCAL_OPENER.open(req, timeout=self.op_timeout) as resp:
                body = json.loads(resp.read() or b"{}")
        except TimeoutError:
            # Connected and sent, but no answer in time (the dashboard may still be
            # forwarding it to the shared board). Replaying it through the CLI could
            # apply it twice, so the outcome is just unknown.
            log.warning("board bridge: no answer to %s within %gs; not resent", payload["type"], self.op_timeout)
            return _UNKNOWN
        except urllib.error.HTTPError as exc:
            # The board answered but refused the op (e.g. a team is busy): not a transport failure.
            try:
                detail = json.loads(exc.read() or b"{}").get("error", "")
            except Exception:
                detail = ""
            log.info("board bridge: board refused %s: %s %s", payload["type"], exc.code, detail)
            return None
        except (urllib.error.URLError, OSError, TimeoutError, ValueError):
            self._dashboard_down = True
            return _UNAVAILABLE
        if isinstance(body, dict) and body.get("ok") is False:
            log.info("board bridge: board refused %s: %s", payload["type"], body.get("error"))
            return None
        return body.get("result") if isinstance(body, dict) else body

    def _cli(self, payload: dict[str, Any]) -> Any:
        """Fallback: run the equivalent ``node department/dept.mjs`` command."""
        argv = cli_args(payload)
        node = shutil.which(self.node_bin)
        if argv is None or node is None or not self.cli_path.exists():
            return _UNAVAILABLE
        try:
            proc = subprocess.run(
                [node, str(self.cli_path), *argv],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self._cli_timeout(),
                cwd=str(self.cli_path.parent.parent),
            )
        except subprocess.TimeoutExpired:
            log.warning("board bridge: CLI %s did not finish within %gs; not resent", argv[0], self._cli_timeout())
            return _UNKNOWN
        except (OSError, subprocess.SubprocessError):
            return _UNAVAILABLE
        if proc.returncode != 0:
            log.info("board bridge: CLI %s failed: %s", argv[0], proc.stderr.strip()[:200])
            return None
        if payload["type"] == "addTask":
            return parse_cli_add(proc.stdout)
        return {"ok": True}


_UNAVAILABLE = object()
#: The op was sent but its outcome is unknown (timed out waiting for the answer).
_UNKNOWN = object()

_CLI_ADD_RE = re.compile(r"^(T-\d+) created → (.*?)(?: · |$)", re.M)


def parse_cli_add(stdout: str) -> dict[str, Any] | None:
    """Parse ``dept.mjs add`` output ("T-012 created → alpha, bravo · ...") into an addTask result."""
    m = _CLI_ADD_RE.search(stdout or "")
    if not m:
        return None
    teams_text = m.group(2).strip()
    assigned = [] if teams_text.startswith("queued") else [t.strip() for t in teams_text.split(",") if t.strip()]
    return {"task": {"id": m.group(1)}, "assigned": assigned}


def cli_args(payload: dict[str, Any]) -> list[str] | None:
    """Translate a ``{type, args}`` op into ``dept.mjs`` arguments (None if no CLI form)."""
    t, a = payload["type"], payload.get("args", {})
    if t == "addTask":
        argv = ["add", str(a.get("title", "")), "--desc", str(a.get("description", ""))]
        if a.get("size") == "large":
            argv.append("--large")
        if a.get("builders"):
            argv += ["--builders", str(a["builders"])]
        return argv
    if t == "unassignTeam":
        return ["unassign", str(a["taskId"]), str(a["teamId"])]
    if t == "assignTeam":
        return ["assign", str(a["taskId"]), str(a["teamId"]), "--focus", str(a.get("focus", ""))]
    if t == "reportProgress":
        argv = ["progress", str(a["taskId"]), "-m", str(a.get("message", ""))]
        if a.get("teamId"):
            argv += ["--team", str(a["teamId"])]
        if a.get("agentId"):
            argv += ["--agent", str(a["agentId"])]
        if a.get("progress") is not None:
            argv += ["--pct", str(a["progress"])]
        if a.get("status"):
            argv += ["--status", str(a["status"])]
        return argv
    if t == "reviewTask":
        return [
            "verdict",
            str(a["taskId"]),
            "--team",
            str(a["teamId"]),
            "approve" if a.get("verdict") == "approved" else "changes",
            "-m",
            str(a.get("note", "")),
            "--issues",
            str(a.get("issues", 0)),
        ]
    if t == "updateTask":
        status = (a.get("patch") or {}).get("status")
        if status == "review":
            return ["review", str(a["id"])]
        if status == "done":
            return ["done", str(a["id"])]
        if status:
            return ["set", str(a["id"]), "--status", str(status)]
    return None


def _task_number(board_id: str) -> int:
    m = re.search(r"(\d+)$", board_id)
    return int(m.group(1)) if m else -1


def _get(obj: Any, name: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _enum(value: Any) -> str:
    return str(getattr(value, "value", value) or "")
