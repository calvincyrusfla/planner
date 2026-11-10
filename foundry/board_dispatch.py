"""Board dispatch: Foundry picks up work batches ("sprints") from the department board.

The Floor Manager (often a cloud Claude session that can reach only the shared
board) puts a **Foundry batch** on the board with ``dispatchBatch``. The
Foundry service on the user's PC runs one :class:`BatchWatcher`, which every
``FOUNDRY_BATCH_POLL_S`` seconds (default 15):

1. reads the board state (``GET /api/state`` on the local dashboard, which
   forwards to the shared board when one is configured);
2. claims (``claimBatch``) every ``queued`` batch whose ``repo`` is this
   service's repository (``owner/name`` of its ``origin`` remote, compared
   case-insensitively; ``FOUNDRY_BATCH_REPO`` overrides). A batch claimed by a
   Foundry whose heartbeat is older than 10 minutes is taken over;
3. submits each subtask, in order, as a normal Foundry work order
   (``manager.accept``); its pull request targets the batch's ``base`` when
   given, else the configured base (``FOUNDRY_PR_BASE`` / the repo default). A subtask with ``dependsOn`` is submitted only once
   all its dependencies are ``complete``; when a dependency ends any other way
   the subtask fails without being submitted;
4. pushes each subtask's Foundry task id, state, PR record, token usage and error back with
   ``updateBatch`` when something changed, else as a heartbeat at most every 2 minutes, until every subtask is
   finished and its PR settled.

The mapping batch/subtask -> Foundry task id and the submit failures are kept
in the Ledger, the rest of a batch's record in the queue's key-value table
(``foundry_batch:<id>``), and every order carries a marker line in its
description, so a restarted service resumes a batch without resubmitting.
While the Ledger cannot be used, the key-value record holds them too.

``FOUNDRY_BATCHES=off`` turns the watcher off; it never claims while the
service is not running. The watcher never raises into the service: board and
submission errors are logged and retried on the next round.
"""

from __future__ import annotations

import ast
import asyncio
import json
import logging
import os
import re
import socket
import subprocess
import urllib.error
import urllib.request
from datetime import datetime, timezone
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, Callable, Mapping

from foundry.ledger import Ledger
from foundry.metrics_report import build_metrics
# Board limits from schema/order.json: error texts, and each question, context or option of a help request.
from foundry.order_schema import LIMIT_ERROR, LIMIT_HELP

log = logging.getLogger("foundry.board_dispatch")

__all__ = [
    "HEARTBEAT_S", "BatchWatcher", "BoardRefused", "BoardUnavailable", "DashboardBoard",
    "DEFAULT_BATCH_POLL_S", "STALE_HEARTBEAT_S", "batch_poll_interval", "batches_enabled",
    "order_text", "repo_slug_for", "subtask_state",
]

DEFAULT_BATCH_POLL_S = 15.0
#: Without result changes, updateBatch (a hub write) is sent at most this often as a heartbeat.
HEARTBEAT_S = 120.0
#: A claim whose heartbeat is older than this may be taken over (the board enforces it too).
STALE_HEARTBEAT_S = 600.0
KV_PREFIX = "foundry_batch:"
DEFAULT_DASHBOARD_URL = "http://127.0.0.1:4300"
#: A file path mentioned in a Floor Manager answer.
_PATH_TOKEN = re.compile(r"[A-Za-z0-9_./-]+\.[A-Za-z0-9]+")
_MAX_HELP_FILES = 10

#: Foundry task state -> batch subtask state.
_STATE_FOR = {
    "backlog": "sizing",
    "sizing": "sizing",
    "building": "building",
    "validating": "building",
    "reviewing": "reviewing",
    "complete": "complete",
    "quarantined": "quarantined",
    "failed": "failed",
}
TERMINAL = frozenset({"complete", "quarantined", "failed"})
_OPEN_BATCH = frozenset({"claimed", "running"})
_LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class BoardUnavailable(RuntimeError):
    """The board could not be reached (the op may be retried)."""


class BoardRefused(RuntimeError):
    """The board answered and refused the op (``code``: the HTTP status, 409 when unknown)."""

    def __init__(self, message: str, code: int = 409) -> None:
        super().__init__(message)
        self.code = int(code)


# --------------------------------------------------------------------------- settings


def batches_enabled(env: Mapping[str, str] | None = None) -> bool:
    """``FOUNDRY_BATCHES`` (default on; ``off``/``0``/``false``/``no`` turn it off)."""
    value = (env if env is not None else os.environ).get("FOUNDRY_BATCHES", "")
    return value.strip().lower() not in ("off", "0", "false", "no")


def batch_poll_interval(env: Mapping[str, str] | None = None) -> float:
    """``FOUNDRY_BATCH_POLL_S`` (default 15 s, at least 1 s)."""
    raw = (env if env is not None else os.environ).get("FOUNDRY_BATCH_POLL_S", "")
    try:
        value = float(raw) if raw.strip() else DEFAULT_BATCH_POLL_S
    except ValueError:
        log.warning("board dispatch: FOUNDRY_BATCH_POLL_S=%r is not a number; using %gs", raw, DEFAULT_BATCH_POLL_S)
        value = DEFAULT_BATCH_POLL_S
    return max(1.0, value)


def repo_slug_for(repo: str | Path, env: Mapping[str, str] | None = None) -> str | None:
    """``FOUNDRY_BATCH_REPO``, else ``owner/name`` of the repo's ``origin`` remote (None if unknown)."""
    override = (env if env is not None else os.environ).get("FOUNDRY_BATCH_REPO", "").strip()
    if override:
        return override
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "remote", "get-url", "origin"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    from foundry.git_publisher import github_repo

    return github_repo(proc.stdout.strip())


# --------------------------------------------------------------------------- board client


class DashboardBoard:
    """The local dashboard: ``GET /api/state`` and ``POST /api/op`` ``{type, args}``.

    The dashboard forwards every op to the shared board when one is configured,
    so this is also the way to the hub from the user's PC.
    """

    def __init__(self, url: str | None = None, *, timeout: float = 20.0) -> None:
        self.url = (url or os.environ.get("FOUNDRY_DASHBOARD_URL") or DEFAULT_DASHBOARD_URL).rstrip("/")
        self.timeout = float(timeout)

    def state(self) -> dict[str, Any]:
        try:
            with _LOCAL_OPENER.open(f"{self.url}/api/state", timeout=self.timeout) as resp:
                body = json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            raise BoardUnavailable(f"the dashboard answered {exc.code}") from exc
        except (urllib.error.URLError, OSError, TimeoutError, ValueError) as exc:
            raise BoardUnavailable(f"the dashboard at {self.url} is not reachable: {exc}") from exc
        if isinstance(body, dict) and isinstance(body.get("result"), dict):
            return body["result"]
        if not isinstance(body, dict):
            raise BoardUnavailable("the dashboard sent no state")
        return body

    def op(self, type_: str, args: Mapping[str, Any]) -> Any:
        req = urllib.request.Request(
            f"{self.url}/api/op", data=json.dumps({"type": type_, "args": dict(args)}).encode(),
            headers={"content-type": "application/json"}, method="POST",
        )
        try:
            with _LOCAL_OPENER.open(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read() or b"{}").get("error", "")
            except Exception:
                detail = ""
            if exc.code in (502, 503, 504):
                raise BoardUnavailable(f"{type_}: {exc.code} {detail}".strip()) from exc
            raise BoardRefused(detail or f"{type_} refused ({exc.code})", exc.code) from exc
        except (urllib.error.URLError, OSError, TimeoutError, ValueError) as exc:
            raise BoardUnavailable(f"{type_}: the dashboard is not reachable: {exc}") from exc
        if isinstance(body, dict) and body.get("ok") is False:
            raise BoardRefused(str(body.get("error") or f"{type_} refused"))
        return body.get("result") if isinstance(body, dict) else body


# --------------------------------------------------------------------------- helpers


def _val(v: Any) -> str:
    return str(getattr(v, "value", v) or "")


def _parse_time(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value) / (1000.0 if value > 1e11 else 1.0)
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def marker(batch_id: str, key: str, created_at: str | None = None) -> str:
    """The line every batch order carries (restart recovery and context for the builders).

    ``created_at`` tells two batches with the same S-id apart (another board, a reset board).
    """
    of = f" of {created_at}" if created_at else ""
    return f"(Foundry batch {batch_id}{of}, subtask {key})"


def order_text(description: str, size: str | None, batch_title: Any, goal: Any, marker_line: str) -> str:
    """The text of a batch subtask's order: description, requested size, batch context, marker line."""
    text = description
    if size:
        text = (text + "\n\n" if text else "") + f"Requested size: {size}"
    goal_text = str(goal or "").strip()
    context = f'Part of "{batch_title}"' + (f": {goal_text}" if goal_text else "")
    return (text + "\n\n" if text else "") + f"{context}\n{marker_line}"


def _cap(value: Any, limit: int) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text[:limit] or None


def subtask_state(manager: Any, queue: Any, tid: str) -> dict[str, Any]:
    """``{foundryTask, state, pr?, error?}`` of one submitted subtask."""
    task = queue.get_task(tid)
    if task is None:
        return {"foundryTask": tid, "state": "failed", "error": "the Foundry task no longer exists"}
    raw = _val(task.get("state"))
    state = _STATE_FOR.get(raw, "building")
    if raw == "backlog":
        # recorded, not sized yet: "sizing" while the Foreman works on it, else waiting
        is_sizing = getattr(manager, "is_sizing", None)
        state = "queued" if callable(is_sizing) and not is_sizing(tid) else "sizing"
    out: dict[str, Any] = {"foundryTask": tid, "state": state}
    error = task.get("quarantine_reason") or (getattr(manager, "sizing_errors", {}) or {}).get(tid)
    if error and state != "complete":
        out["error"] = _cap(error, LIMIT_ERROR)
    pr_fn = getattr(manager, "pr_state", None)
    pr = pr_fn(tid) if callable(pr_fn) else None
    if pr:
        rec = {"state": _cap(pr.get("state"), 40), "url": _cap(pr.get("url"), 1000),
               "branch": _cap(pr.get("branch"), 300), "error": _cap(pr.get("error"), LIMIT_ERROR)}
        is_pub = getattr(manager, "is_publishing", None)
        if callable(is_pub) and is_pub(tid):
            rec["state"] = "pending"
        out["pr"] = rec
    return out


# --------------------------------------------------------------------------- watcher


class BatchWatcher:
    """Claims this repo's batches on the board and runs their subtasks as Foundry orders.

    Args:
        service: the :class:`foundry.serve.Service` (``running``, ``factory``, ``repo``, ``config``).
        board: a board client with ``state() -> dict`` and ``op(type, args)``
            (blocking; called in a thread). Default: :class:`DashboardBoard`.
        repo_slug: ``owner/name`` this Foundry works for (default :func:`repo_slug_for`).
        host: this PC's name (default ``socket.gethostname()``).
        interval: seconds between rounds (default :func:`batch_poll_interval`).
        enabled: default :func:`batches_enabled`.
        sleep: async sleep used by the loop (tests).
        now: wall clock in epoch seconds (tests).
    """

    def __init__(
        self,
        service: Any,
        board: Any = None,
        repo_slug: str | None = None,
        host: str | None = None,
        interval: float | None = None,
        *,
        enabled: bool | None = None,
        sleep: Callable[[float], Any] | None = None,
        now: Callable[[], float] | None = None,
    ) -> None:
        import time

        self.service = service
        self.board = board if board is not None else DashboardBoard()
        self.repo_slug = repo_slug if repo_slug is not None else repo_slug_for(service.repo)
        self.host = host or socket.gethostname() or "pc"
        self.interval = float(interval) if interval else batch_poll_interval()
        self.enabled = batches_enabled() if enabled is None else bool(enabled)
        self._sleep = sleep or asyncio.sleep
        self._now = now or time.time
        self.service_id = f"foundry@{self.host}:{(self.repo_slug or '?').lower()}"
        self.rounds = 0
        self.last_error: str | None = None
        self.claimed: list[str] = []
        self._task: asyncio.Task[Any] | None = None
        self._warned_down = False
        self._lock = asyncio.Lock()
        configured = getattr(getattr(service, "config", None), "allowed_repos", None)
        #: FOUNDRY_ALLOWED_REPOS as configured; empty means no allowlist.
        self.allowed_repos: list[str] = [str(r) for r in configured] if isinstance(configured, (list, tuple)) else []
        self._allowed_keys = frozenset(r.strip().lower() for r in self.allowed_repos)
        self._warned_no_allowlist = False
        self.heartbeat_s = HEARTBEAT_S
        #: The board refused a result carrying usage: results go without it for this run.
        self._usage_refused = False
        #: batch id -> when the last updateBatch was accepted (this run).
        self._last_sent: dict[str, float] = {}
        self._ledger_cache: Ledger | None = None
        self._ledger_warned = False
        #: Batches whose pieces only the Ledger knows, while it cannot be read: nothing may be submitted.
        self._ledger_blind: set[str] = set()

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> asyncio.Task[Any] | None:
        if not self.enabled:
            log.info("board dispatch: off (FOUNDRY_BATCHES)")
            return None
        if not self.repo_slug:
            log.warning("board dispatch: the repo has no GitHub origin and FOUNDRY_BATCH_REPO is unset; "
                        "board batches are not picked up")
            return None
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._loop())
            log.info("board dispatch: watching the board for %s batches every %gs", self.repo_slug, self.interval)
        return self._task

    async def stop(self) -> None:
        t, self._task = self._task, None
        if t is not None:
            t.cancel()
            await asyncio.gather(t, return_exceptions=True)

    async def _loop(self) -> None:
        while True:
            await self.poll_once()
            await self._maybe_await(self._sleep(self.interval))
            await asyncio.sleep(0)

    @staticmethod
    async def _maybe_await(v: Any) -> Any:
        return await v if asyncio.iscoroutine(v) or isinstance(v, asyncio.Future) else v

    def summary(self) -> dict[str, Any]:
        return {"enabled": self.enabled and bool(self.repo_slug), "repo": self.repo_slug, "service": self.service_id,
                "rounds": self.rounds, "claimed": list(self.claimed), "last_error": self.last_error,
                "allowed_repos": list(self.allowed_repos)}

    # ------------------------------------------------------------------ one round

    def _ready(self) -> bool:
        return bool(self.enabled and self.repo_slug and getattr(self.service, "running", False)
                    and getattr(self.service, "factory", None) is not None)

    async def poll_once(self) -> list[str]:
        """One round; returns the ids of the batches handled. Never raises (except cancellation)."""
        if not self._ready():
            return []
        async with self._lock:
            self.rounds += 1
            try:
                return await self._round()
            except asyncio.CancelledError:
                raise
            except BoardUnavailable as exc:
                self.last_error = str(exc)
                if not self._warned_down:
                    log.warning("board dispatch: %s (retrying every %gs)", exc, self.interval)
                    self._warned_down = True
                return []
            except Exception as exc:  # the watcher must never take the service down
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.warning("board dispatch: round failed: %s", self.last_error)
                return []

    async def _round(self) -> list[str]:
        state = await asyncio.to_thread(self.board.state)
        self._warned_down = False
        self.last_error = None
        batches = state.get("foundryBatches") if isinstance(state, dict) else None
        handled: list[str] = []
        for batch in batches if isinstance(batches, list) else []:
            if not isinstance(batch, dict) or not batch.get("id"):
                continue
            if not self._ready():
                break  # the service stopped meanwhile: never claim or submit
            bid = str(batch["id"])
            if self._is_ours(batch):
                if not self._record(bid, batch.get("createdAt")).get("closed"):
                    await self._advance(batch)
                    handled.append(bid)
            elif self._claimable(batch):
                claimed = await self._claim(batch)
                if claimed is not None:
                    await self._advance(claimed)
                    handled.append(bid)
        # after the batches, so an answer already on the board wins over the timeout
        self._expire_help()
        return handled

    def _is_ours(self, batch: Mapping[str, Any]) -> bool:
        by = batch.get("claimedBy")
        return isinstance(by, dict) and by.get("service") == self.service_id

    def _matches_repo(self, batch: Mapping[str, Any]) -> bool:
        repo = batch.get("repo")
        return isinstance(repo, str) and bool(self.repo_slug) and repo.strip().lower() == str(self.repo_slug).lower()

    def _allowed(self, batch: Mapping[str, Any]) -> bool:
        """True when ``FOUNDRY_ALLOWED_REPOS`` lets this batch be claimed; an empty list allows all (warned once)."""
        if not self._allowed_keys:
            if not self._warned_no_allowlist:
                log.warning("board dispatch: FOUNDRY_ALLOWED_REPOS is empty; any batch queued for %s can be claimed",
                            self.repo_slug)
                self._warned_no_allowlist = True
            return True
        repo = batch.get("repo")
        return isinstance(repo, str) and repo.strip().lower() in self._allowed_keys

    def _claimable(self, batch: Mapping[str, Any]) -> bool:
        if not self._matches_repo(batch) or not self._allowed(batch):
            return False
        status = batch.get("status")
        if status == "queued":
            return True
        if status in _OPEN_BATCH:
            claim = batch.get("claimedBy")
            by: Mapping[str, Any] = claim if isinstance(claim, dict) else {}
            beat = _parse_time(batch.get("heartbeatAt") or by.get("at"))
            return beat is not None and self._now() - beat > STALE_HEARTBEAT_S
        return False

    async def _claim(self, batch: Mapping[str, Any]) -> dict[str, Any] | None:
        bid = str(batch["id"])
        try:
            out = await asyncio.to_thread(self.board.op, "claimBatch",
                                          {"id": bid, "host": self.host, "service": self.service_id})
        except BoardRefused as exc:
            log.info("board dispatch: could not claim %s: %s", bid, exc)
            return None
        if isinstance(out, dict) and out.get("ok") is False:
            log.info("board dispatch: could not claim %s: %s", bid, out.get("reason"))
            return None
        claimed = out.get("batch") if isinstance(out, dict) and isinstance(out.get("batch"), dict) else None
        if claimed is None:
            claimed = {**batch, "status": "claimed",
                       "claimedBy": {"host": self.host, "service": self.service_id}}
        if bid not in self.claimed:
            self.claimed.append(bid)
        log.info("board dispatch: claimed %s (%s, %d subtask(s))", bid, claimed.get("title"),
                 len(claimed.get("subtasks") or []))
        return claimed

    # ------------------------------------------------------------------ records

    def _queue(self) -> Any:
        return self.service.factory.queue

    def _ledger(self) -> Ledger | None:
        """The one Ledger on this service's queue, made on first use; None when the queue can't be read."""
        if self._ledger_cache is None:
            try:
                self._ledger_cache = Ledger(self._queue())
            except Exception as exc:  # the Ledger is a record only: the board work goes on without it
                if not self._ledger_warned:
                    log.info("board dispatch: the Ledger is not available: %s", exc)
                    self._ledger_warned = True
        return self._ledger_cache

    def _ledger_write(self, write: Callable[[Ledger], Any]) -> None:
        """Run one Ledger write. Never raises: the Ledger must not change what goes to the board."""
        try:
            ledger = self._ledger()
            if ledger is not None:
                write(ledger)
        except Exception as exc:
            log.info("board dispatch: Ledger write failed: %s", exc)

    def _ledger_piece(self, batch: Mapping[str, Any], key: str, **fields: Any) -> None:
        self._ledger_write(lambda ledger: ledger.upsert_piece(str(batch["id"]), batch.get("createdAt"), key, **fields))

    def _ledger_round(self, batch: Mapping[str, Any], results: Mapping[str, Mapping[str, Any]],
                      args: Mapping[str, Any], with_usage: Mapping[str, Mapping[str, Any]]) -> None:
        """Record this round's batch status and every piece's state (the Ledger skips unchanged rows)."""
        status = args["status"] if "status" in args else batch.get("status")
        self._ledger_write(lambda ledger: ledger.upsert_batch({**batch, "status": status}))
        for key, result in results.items():
            pr = result.get("pr") or {}
            fields: dict[str, Any] = {
                "state": result.get("state"), "pr_url": pr.get("url"), "pr_state": pr.get("state"),
                "error": result.get("error"), "help_question": (result.get("help") or {}).get("question"),
            }
            if key in with_usage:
                fields["usage"] = with_usage[key]["usage"]
            self._ledger_piece(batch, key, **fields)

    def _record(self, bid: str, created_at: str | None = None) -> dict[str, Any]:
        """The saved record of a batch; a record of another batch with the same id starts over.

        ``tasks`` and ``failed`` are the Ledger's, once it has taken whatever only the kv record
        still holds; while the Ledger cannot be used they are the kv record's.
        """
        fn = getattr(self._queue(), "kv_get", None)
        rec = fn(KV_PREFIX + bid) if callable(fn) else None
        if not isinstance(rec, dict) or (created_at and rec.get("createdAt") not in (None, created_at)):
            rec = {}
        handed_over = bool(rec) and "tasks" not in rec  # a save gave them to the Ledger
        if created_at:
            rec["createdAt"] = created_at
        rec.setdefault("tasks", {})
        rec.setdefault("failed", {})
        rec.setdefault("pushed", {})
        rec.setdefault("usage", {})
        if self._overlay_ledger(bid, rec) or not handed_over:
            self._ledger_blind.discard(bid)
        else:
            self._ledger_blind.add(bid)
        return rec

    def _overlay_ledger(self, bid: str, rec: dict[str, Any]) -> bool:
        """Replace ``rec``'s tasks and submit failures with the Ledger's; False leaves ``rec`` as it was."""
        try:
            ledger = self._ledger()
            if ledger is None:
                return False
            pieces = self._backfill(ledger, bid, rec)
        except Exception as exc:  # the kv record still holds what the Ledger never took
            log.info("board dispatch: Ledger read failed (or its backfill), using the kv record: %s", exc)
            return False
        tasks: dict[str, str] = {}
        failed: dict[str, str] = {}
        for piece in pieces:
            key = str(piece["key"])
            if piece.get("foundry_task"):
                tasks[key] = str(piece["foundry_task"])
            elif piece.get("state") == "failed":
                failed[key] = str(piece.get("error") or "failed")
        rec["tasks"] = tasks
        rec["failed"] = failed
        return True

    @staticmethod
    def _backfill(ledger: Ledger, bid: str, rec: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Write the tasks and failures of ``rec`` the Ledger lacks; returns its pieces. Raises when it can't."""
        created = rec.get("createdAt")
        pieces = ledger.pieces(bid, created)
        rows = {str(piece["key"]): piece for piece in pieces}
        wrote = False
        for key, tid in rec["tasks"].items():
            if not rows.get(key, {}).get("foundry_task"):  # no row, or one from before foundry_task
                ledger.upsert_piece(bid, created, key, foundry_task=tid)
                wrote = True
        for key, reason in rec["failed"].items():
            row = rows.get(key, {})
            if key not in rec["tasks"] and not row.get("foundry_task") and row.get("state") != "failed":
                ledger.upsert_piece(bid, created, key, state="failed", error=reason)
                wrote = True
        return ledger.pieces(bid, created) if wrote else pieces

    def _save(self, bid: str, rec: Mapping[str, Any]) -> None:
        """Save the record; ``tasks`` and ``failed`` stay out of kv once the Ledger holds them all."""
        fn = getattr(self._queue(), "kv_set", None)
        if not callable(fn):
            return
        saved = dict(rec)
        try:
            ledger = self._ledger()
            if ledger is not None:
                self._backfill(ledger, bid, rec)
                saved.pop("tasks", None)
                saved.pop("failed", None)
        except Exception as exc:  # kept in kv, so a broken Ledger loses nothing
            log.info("board dispatch: Ledger write failed, keeping %s's pieces in the kv record: %s", bid, exc)
        fn(KV_PREFIX + bid, saved)

    def _find_order(self, bid: str, key: str, created_at: str | None) -> str | None:
        """An order already submitted for this subtask (submitted, then cut off before the kv write)."""
        mark = marker(bid, key, created_at)
        for tid, order in (getattr(self.service.factory.manager, "orders", {}) or {}).items():
            if mark in (getattr(order, "description", "") or ""):
                return str(tid)
        return None

    # ------------------------------------------------------------------ advancing a batch

    async def _advance(self, batch: Mapping[str, Any]) -> None:
        bid = str(batch["id"])
        created = batch.get("createdAt")
        rec = self._record(bid, created)
        if bid in self._ledger_blind:
            await self._heartbeat_only(bid)
            return
        f = self.service.factory
        manager, queue = f.manager, f.queue
        subtasks = [s for s in (batch.get("subtasks") or []) if isinstance(s, dict)]
        keys = [str(s.get("key") or i + 1) for i, s in enumerate(subtasks)]
        by_key = dict(zip(keys, subtasks))
        cancelled = batch.get("status") == "cancelled"
        can_submit = not cancelled and batch.get("status") in (None, "claimed", "running")
        first = not rec["tasks"] and not rec["failed"]
        results: dict[str, dict[str, Any]] = {}

        # answers first, so a ticket reopened here is reported with its new state below
        self._apply_answers(batch, bid, rec, queue)

        # 1) every subtask already submitted or failed, whatever its position
        for key in keys:
            if key in rec["failed"]:
                results[key] = {"state": "failed", "error": rec["failed"][key]}
                continue
            tid = rec["tasks"].get(key) or self._find_order(bid, key, created)
            if tid:
                rec["tasks"][key] = tid
                results[key] = subtask_state(manager, queue, tid)

        # 2) the rest, judged against those states (repeated so failures and
        #    submissions propagate along dependsOn in any order)
        def fail(key: str, reason: str) -> None:
            rec["failed"][key] = reason
            results[key] = {"state": "failed", "error": reason}

        progress = can_submit
        while progress:
            progress = False
            for key in keys:
                if key in rec["tasks"] or key in rec["failed"]:
                    continue
                deps = [str(d) for d in (by_key[key].get("dependsOn") or [])]
                if key in self._dep_closure(key, by_key):
                    fail(key, "not started: its dependsOn forms a cycle")
                    progress = True
                    continue
                dep_states = {d: (results.get(d) or {}).get("state") for d in deps}
                bad = [d for d, st in dep_states.items() if (st in TERMINAL and st != "complete") or d not in by_key]
                if bad:
                    fail(key, f"not started: dependency {', '.join(bad)} did not complete")
                    progress = True
                    continue
                if any(st != "complete" for st in dep_states.values()):
                    results[key] = {"state": "queued"}  # waits for its dependencies
                    continue
                try:
                    tid = self._submit(batch, key, by_key[key])
                except Exception as exc:
                    reason = f"could not be submitted: {exc}"[:LIMIT_ERROR]
                    log.warning("board dispatch: %s subtask %s %s", bid, key, reason)
                    fail(key, reason)
                    progress = True
                    continue
                rec["tasks"][key] = tid
                self._save(bid, rec)  # right away: a restart must not submit it again
                log.info("board dispatch: %s subtask %s -> %s", bid, key, tid)
                results[key] = subtask_state(manager, queue, tid)
                progress = True

        self._report_help(results, rec, queue, batch)

        # The board closes a batch (and refuses further updates) once every subtask is
        # terminal, so "complete" is only reported once the subtask's PR is settled.
        auto_pr = self._auto_pr()
        board_view = {k: self._board_result(v, auto_pr) for k, v in results.items()}
        changed = {k: v for k, v in board_view.items() if rec["pushed"].get(k) != v}
        args: dict[str, Any] = {"id": bid, "service": self.service_id, "heartbeat": True}
        if changed:
            args["results"] = changed
        if first and rec["tasks"] and batch.get("status") == "claimed":
            args["status"] = "running"
        self._save(bid, rec)
        last = self._last_sent.get(bid)
        write_due = bool(changed) or "status" in args or last is None or self._now() - last >= self.heartbeat_s
        with_usage = self._usage_results(rec, board_view, changed) if write_due else {}
        if with_usage:
            args["results"] = {**changed, **with_usage}
        self._ledger_round(batch, results, args, with_usage)
        if not write_due:
            # nothing new and the claim is fresh: no write to the (shared) board this round
            if self._settled(keys, results, cancelled):
                rec["closed"] = True
                self._save(bid, rec)
            return
        try:
            await asyncio.to_thread(self.board.op, "updateBatch", args)
        except BoardRefused as exc:
            if exc.code == 400 and with_usage:
                # e.g. a hub that does not know the field: states matter more, so next round goes without it
                log.warning("board dispatch: %s results with usage refused (%s); sending them without usage", bid, exc)
                self._usage_refused = True
                return
            if exc.code == 400 and "results" in args:
                # the board rejected a result (a limit we did not know): keep the heartbeat going
                log.warning("board dispatch: %s results refused (%s); sending the heartbeat only", bid, exc)
                args.pop("results")
                args.pop("status", None)
                changed = {}
                try:
                    await asyncio.to_thread(self.board.op, "updateBatch", args)
                except BoardRefused as again:
                    exc = again
                else:
                    self._last_sent[bid] = self._now()
                    return
            if exc.code not in (404, 409) or not await self._gone(bid):
                # e.g. 409 {"error": "busy"} from the hub: transient, tried again next round
                log.info("board dispatch: %s update refused (%s); retrying next round", bid, exc)
                return
            # cancelled, finished, deleted or taken over by another Foundry: stop pushing
            log.info("board dispatch: %s update refused (%s); no longer followed", bid, exc)
            rec["closed"] = True
            self._save(bid, rec)
            return
        self._last_sent[bid] = self._now()
        rec["pushed"].update(changed)
        rec["usage"].update({k: v["usage"] for k, v in with_usage.items()})
        if self._settled(keys, results, cancelled):
            rec["closed"] = True
            log.info("board dispatch: %s finished", bid)
        self._save(bid, rec)

    async def _heartbeat_only(self, bid: str) -> None:
        """Keep the claim alive in a round that cannot see the batch's pieces: nothing else goes out."""
        last = self._last_sent.get(bid)
        if last is not None and self._now() - last < self.heartbeat_s:
            return
        args = {"id": bid, "service": self.service_id, "heartbeat": True}
        try:
            await asyncio.to_thread(self.board.op, "updateBatch", args)
        except BoardRefused as exc:
            log.info("board dispatch: %s heartbeat refused (%s); retrying next round", bid, exc)
            return
        self._last_sent[bid] = self._now()

    @staticmethod
    def _dep_closure(key: str, by_key: Mapping[str, Mapping[str, Any]]) -> set[str]:
        """Every subtask ``key`` depends on, directly or not."""
        seen: set[str] = set()
        todo = [str(d) for d in (by_key.get(key, {}).get("dependsOn") or [])]
        while todo:
            d = todo.pop()
            if d in seen:
                continue
            seen.add(d)
            todo.extend(str(x) for x in (by_key.get(d, {}).get("dependsOn") or []))
        return seen

    async def _gone(self, bid: str) -> bool:
        """After a refused update: True when a fresh board state shows the batch is no longer ours to run."""
        try:
            state = await asyncio.to_thread(self.board.state)
        except BoardUnavailable:
            return False
        batches = state.get("foundryBatches") if isinstance(state, dict) else None
        b = next((x for x in batches or [] if isinstance(x, dict) and x.get("id") == bid), None)
        return b is None or not self._is_ours(b) or b.get("status") not in _OPEN_BATCH

    def _help_files(self, answer: str) -> list[str]:
        """The repo-relative files named in ``answer`` that exist (deduplicated, at most 10)."""
        root = Path(str(self.service.repo))
        found: list[str] = []
        for match in _PATH_TOKEN.findall(answer):
            path = match.strip("`'\"").removeprefix("./")
            if path.startswith("/") or ".." in path or path in found:
                continue
            if (root / path).is_file():
                found.append(path)
                if len(found) >= _MAX_HELP_FILES:
                    break
        return found

    def _apply_answers(self, batch: Mapping[str, Any], bid: str, rec: dict[str, Any], queue: Any) -> None:
        """Reopen the tickets whose question was answered on the board, once per answer."""
        requests_fn = getattr(queue, "help_requests", None)
        answer_fn = getattr(queue, "answer_help", None)
        board_results = batch.get("results")
        if not callable(requests_fn) or not callable(answer_fn) or not isinstance(board_results, dict):
            return
        handled = rec.setdefault("answered", {})
        for key, tid in list(rec["tasks"].items()):
            result = board_results.get(key)
            if not isinstance(result, dict) or result.get("state") != "answered":
                continue
            help_info = result.get("help")
            if not isinstance(help_info, dict):
                continue
            answer = help_info.get("answer")
            if not isinstance(answer, str) or not answer.strip():
                continue
            stamp = str(help_info.get("answeredAt") or "")
            if handled.get(key) == stamp:
                continue
            try:
                read_files = self._help_files(answer)
                for request in requests_fn():
                    if str(request.get("task_id")) != str(tid):
                        continue
                    ticket_id = request["ticket_id"]
                    applied = answer_fn(ticket_id, answer, read_files)
                    if applied:
                        self._ledger_piece(batch, key, help_answer=answer)
                    log.info("board dispatch: %s subtask %s: answered ticket %s (applied: %s, files: %s)",
                             bid, key, ticket_id, applied, read_files)
            except Exception as exc:  # not marked as handled: tried again next round
                log.warning("board dispatch: %s subtask %s: could not apply the answer: %s", bid, key, exc)
                continue
            handled[key] = stamp
            self._save(bid, rec)

    @staticmethod
    def _help_payload(request: Mapping[str, Any]) -> dict[str, Any]:
        """The board's ``help`` object for one help request."""
        options = request.get("options")
        return {
            "question": str(request.get("question") or "")[:LIMIT_HELP],
            "context": str(request.get("context") or "")[:LIMIT_HELP],
            "options": [str(o)[:LIMIT_HELP] for o in options] if isinstance(options, list) else [],
        }

    def _report_help(self, results: dict[str, dict[str, Any]], rec: Mapping[str, Any], queue: Any,
                     batch: Mapping[str, Any]) -> None:
        """Report paused subtasks as ``needs_help`` and clear the question once they are not."""
        requests_fn = getattr(queue, "help_requests", None)
        if not callable(requests_fn):
            return
        waiting: dict[str, Mapping[str, Any]] = {}
        try:
            for request in requests_fn():
                if isinstance(request, dict):
                    waiting.setdefault(str(request.get("task_id")), request)
        except Exception as exc:  # the normal states still go out
            log.warning("board dispatch: could not read the help requests: %s", exc)
            return
        board_results = batch.get("results")
        shown: Mapping[str, Any] = board_results if isinstance(board_results, dict) else {}
        for key, result in list(results.items()):
            request_for_task = waiting.get(str(result.get("foundryTask")))
            if request_for_task is not None and result.get("state") not in TERMINAL:
                results[key] = {**result, "state": "needs_help", "help": self._help_payload(request_for_task)}
                continue
            pushed = rec["pushed"].get(key)
            on_board = shown.get(key)
            # the board may show a question our record missed (a write whose reply was lost)
            asked = isinstance(on_board, dict) and on_board.get("state") in ("needs_help", "answered")
            # "help" stays None after the first clear, so the result does not look changed next round
            if asked or (isinstance(pushed, dict) and (pushed.get("state") == "needs_help" or "help" in pushed)):
                results[key] = {**result, "help": None}

    def _expire_help(self) -> None:
        """Quarantine tasks whose question waited too long. Never raises."""
        try:
            if not self._ready():
                return
            expire = getattr(self._queue(), "expire_help", None)
            if not callable(expire):
                return
            configured = getattr(getattr(self.service, "config", None), "help_timeout_s", None)
            if isinstance(configured, (int, float)) and not isinstance(configured, bool):
                timeout = float(configured)
            else:
                timeout = float(os.environ.get("FOUNDRY_HELP_TIMEOUT_S", 86400))
            expired = expire(timeout)
            if expired:
                log.info("board dispatch: help requests timed out: %s", expired)
        except Exception as exc:  # the timeout must never break a round
            log.warning("board dispatch: could not expire help requests: %s", exc)

    def _auto_pr(self) -> bool:
        m = self.service.factory.manager
        return bool(getattr(m, "auto_pr", False)) and getattr(m, "publisher", None) is not None

    @staticmethod
    def _pr_pending(result: Mapping[str, Any]) -> bool:
        pr = result.get("pr")
        return not pr or pr.get("state") == "pending"

    def _board_result(self, result: Mapping[str, Any], auto_pr: bool) -> dict[str, Any]:
        out = dict(result)
        if out.get("state") == "complete" and auto_pr and self._pr_pending(out):
            out["state"] = "reviewing"  # approved; the pull request is being opened
        if out.get("foundryTask"):
            out.setdefault("error", None)
        return out

    def _run_settings(self) -> dict[str, Any] | None:
        """The settings the service runs with; None without a readable config."""
        try:
            cfg = getattr(self.service, "config", None)
            if cfg is None:
                return None
            return {
                "builderTools": getattr(cfg, "builder_tools", "off"),
                "reviewerTools": getattr(cfg, "reviewer_tools", "off"),
                "builderMode": getattr(cfg, "builder_mode", "diff"),
                "buildersPerTeam": getattr(cfg, "builders_per_team", None),
                "helpAfter": getattr(cfg, "help_after", None),
                "maxRetries": getattr(cfg, "max_retries", None),
                "preReview": getattr(cfg, "pre_review", None),
            }
        except Exception as exc:  # optional: the usage still goes out
            log.info("board dispatch: run settings not available: %s", exc)
            return None

    def _attempts(self, tid: Any) -> int | None:
        """Builder attempts the task used (``Ticket.attempt`` is 0-based); None when unknown."""
        try:
            tickets = self.service.factory.queue.tickets_for_task(str(tid))
            return max(t.attempt for t in tickets) + 1 if tickets else None
        except Exception as exc:  # optional: the usage still goes out
            log.info("board dispatch: attempts of %s not available: %s", tid, exc)
            return None

    def _usage_results(self, rec: Mapping[str, Any], view: Mapping[str, Mapping[str, Any]],
                       changed: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
        """Results carrying ``usage``: every changed subtask with usage, and any whose usage moved.

        Besides the tokens, ``usage`` holds the service's settings (``run``) and the task's
        builder ``attempts`` when they can be read; a task without usage rows gets neither.

        Called only when a write is due anyway, so usage alone never adds a board write, and on
        the service loop, because a SQLite connection belongs to its thread. Never raises:
        without readable telemetry the results go out without usage.
        """
        if self._usage_refused or not rec["tasks"]:
            return {}
        try:
            by_task = build_metrics(getattr(self.service.factory, "telemetry", None))["byTask"]
        except Exception as exc:  # usage is optional: states and heartbeats must still go out
            log.info("board dispatch: usage not available: %s", exc)
            return {}
        run = self._run_settings()
        out: dict[str, dict[str, Any]] = {}
        for key, tid in rec["tasks"].items():
            metrics = by_task.get(str(tid))
            if metrics is None or key not in view:
                continue
            usage = dict(metrics)
            if run is not None:
                usage["run"] = dict(run)
            attempts = self._attempts(tid)
            if attempts is not None:
                usage["attempts"] = attempts
            if key in changed or rec["usage"].get(key) != usage:
                out[key] = {**view[key], "usage": usage}
        return out

    def _settled(self, keys: list[str], results: Mapping[str, Mapping[str, Any]], cancelled: bool) -> bool:
        """Every subtask finished (or never to start) and every PR settled."""
        auto_pr = self._auto_pr()
        for key in keys:
            r = results.get(key)
            if r is None:
                if cancelled:
                    continue
                return False
            if r.get("state") not in TERMINAL:
                return False
            if r.get("state") == "complete" and auto_pr and self._pr_pending(r):
                return False
        return True

    _MAX_RELATED_TESTS = 3
    _RELATED_TESTS_SKIP = frozenset({"node_modules", "__pycache__", "venv", "env", "build", "dist", "data"})

    @staticmethod
    def _imported_modules(source: str) -> set[str]:
        """The absolute modules ``source`` imports (``from a import b`` counts as ``a`` and ``a.b``)."""
        try:
            tree = ast.parse(source)
        except (SyntaxError, ValueError):
            return set()
        found: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                found.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
                found.add(node.module)
                found.update(f"{node.module}.{alias.name}" for alias in node.names)
        return found

    def _related_tests(self, files: list[str]) -> list[str]:
        """Up to three test files of the repo that import a module in ``files``, ``test_<name>.py`` first."""
        editable = [str(f).strip().replace("\\", "/").removeprefix("./") for f in files]
        modules: set[str] = set()
        stems: set[str] = set()
        for path in editable:
            name = path.rpartition("/")[2]
            if not name.endswith(".py") or name.startswith("test_") or any(c in path for c in "*?["):
                continue
            stems.add(f"test_{name}")
            modules.add(path[:-3].removesuffix("/__init__").replace("/", "."))
        if not modules:
            return []
        root = Path(str(self.service.repo))
        found: list[str] = []
        for folder, dirs, names in os.walk(root):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d not in self._RELATED_TESTS_SKIP]
            for name in names:
                if not name.endswith(".py") or not (name.startswith("test_") or name.endswith("_test.py")):
                    continue
                full = Path(folder, name)
                rel = full.relative_to(root).as_posix()
                if any(rel == e or fnmatchcase(rel, e) for e in editable):
                    continue  # the task may edit it, so it cannot be read-only
                try:
                    source = full.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                if any(m.rpartition(".")[2] in source for m in modules) and modules & self._imported_modules(source):
                    found.append(rel)
        found.sort(key=lambda rel: (rel.rpartition("/")[2] not in stems, rel))
        return found[: self._MAX_RELATED_TESTS]

    def _submit(self, batch: Mapping[str, Any], key: str, sub: Mapping[str, Any]) -> str:
        from foundry.floor_manager import WorkOrder
        from foundry.git_publisher import valid_ref
        from foundry.serve import Service

        fields = Service.validate_order({
            "title": sub.get("title"), "description": sub.get("description") or "",
            "files": list(sub.get("files") or []), "size": sub.get("size"),
        })
        desc = order_text(fields["description"], fields["size"], batch.get("title"), batch.get("goal"),
                          marker(str(batch["id"]), key, batch.get("createdAt")))
        acceptance = [str(a) for a in (sub.get("acceptance") or sub.get("acceptance_criteria") or []) if str(a).strip()]
        base = str(batch.get("base") or "").strip() or None  # else the publisher's (FOUNDRY_PR_BASE / repo default)
        if base is not None and not valid_ref(base):
            # the board checks this too; never hand git anything that could read as an option
            raise ValueError(f"the batch base {base[:80]!r} is not a plain branch name")
        explicit = [str(f).strip() for f in (sub.get("readFiles") or sub.get("read_files") or []) if str(f).strip()]
        read_files = list(dict.fromkeys(explicit + self._related_tests(fields["files"])))
        order = WorkOrder(title=fields["title"], description=desc, files=fields["files"],
                          read_files=read_files, acceptance_criteria=acceptance,
                          repo_root=str(self.service.repo), pr_base=base)
        manager = self.service.factory.manager
        accept = getattr(manager, "accept", None)
        if not callable(accept):
            raise RuntimeError("this Foundry cannot accept orders in the background")
        tid = str(accept(order))
        self._ledger_piece(batch, key, title=fields["title"], order_text=desc, files=fields["files"],
                           read_files=read_files, acceptance=acceptance, size=fields["size"],
                           foundry_task=tid, state="queued")
        return tid

