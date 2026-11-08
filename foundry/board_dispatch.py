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
   (``manager.accept``). A subtask with ``dependsOn`` is submitted only once
   all its dependencies are ``complete``; when a dependency ends any other way
   the subtask fails without being submitted;
4. pushes each subtask's Foundry task id, state, PR record and error back with
   ``updateBatch`` (plus a heartbeat every round) until every subtask is
   finished and its PR settled.

The mapping batch/subtask -> Foundry task id is kept in the queue's key-value
table (``foundry_batch:<id>``) and every order carries a marker line in its
description, so a restarted service resumes a batch without resubmitting.

``FOUNDRY_BATCHES=off`` turns the watcher off; it never claims while the
service is not running. The watcher never raises into the service: board and
submission errors are logged and retried on the next round.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import subprocess
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

log = logging.getLogger("foundry.board_dispatch")

__all__ = [
    "BatchWatcher", "BoardRefused", "BoardUnavailable", "DashboardBoard",
    "DEFAULT_BATCH_POLL_S", "STALE_HEARTBEAT_S", "batch_poll_interval", "batches_enabled",
    "repo_slug_for", "subtask_state",
]

DEFAULT_BATCH_POLL_S = 15.0
#: A claim whose heartbeat is older than this may be taken over (the board enforces it too).
STALE_HEARTBEAT_S = 600.0
KV_PREFIX = "foundry_batch:"
DEFAULT_DASHBOARD_URL = "http://127.0.0.1:4300"

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
    """The board answered and refused the op."""


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
            raise BoardRefused(detail or f"{type_} refused ({exc.code})") from exc
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


def marker(batch_id: str, key: str) -> str:
    """The line every batch order carries (restart recovery and context for the builders)."""
    return f"(Foundry batch {batch_id}, subtask {key})"


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
        out["error"] = str(error)[:500]
    pr_fn = getattr(manager, "pr_state", None)
    pr = pr_fn(tid) if callable(pr_fn) else None
    if pr:
        rec = {k: pr.get(k) for k in ("state", "url", "branch", "error")}
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
                "rounds": self.rounds, "claimed": list(self.claimed), "last_error": self.last_error}

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
                if not self._record(bid).get("closed"):
                    await self._advance(batch)
                    handled.append(bid)
            elif self._claimable(batch):
                claimed = await self._claim(batch)
                if claimed is not None:
                    await self._advance(claimed)
                    handled.append(bid)
        return handled

    def _is_ours(self, batch: Mapping[str, Any]) -> bool:
        by = batch.get("claimedBy")
        return isinstance(by, dict) and by.get("service") == self.service_id

    def _matches_repo(self, batch: Mapping[str, Any]) -> bool:
        repo = batch.get("repo")
        return isinstance(repo, str) and bool(self.repo_slug) and repo.strip().lower() == str(self.repo_slug).lower()

    def _claimable(self, batch: Mapping[str, Any]) -> bool:
        if not self._matches_repo(batch):
            return False
        status = batch.get("status")
        if status == "queued":
            return True
        if status in _OPEN_BATCH:
            beat = _parse_time(batch.get("heartbeatAt"))
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

    def _record(self, bid: str) -> dict[str, Any]:
        fn = getattr(self._queue(), "kv_get", None)
        rec = fn(KV_PREFIX + bid) if callable(fn) else None
        if not isinstance(rec, dict):
            rec = {}
        rec.setdefault("tasks", {})
        rec.setdefault("failed", {})
        rec.setdefault("pushed", {})
        return rec

    def _save(self, bid: str, rec: Mapping[str, Any]) -> None:
        fn = getattr(self._queue(), "kv_set", None)
        if callable(fn):
            fn(KV_PREFIX + bid, dict(rec))

    def _find_order(self, bid: str, key: str) -> str | None:
        """An order already submitted for this subtask (submitted, then cut off before the kv write)."""
        mark = marker(bid, key)
        for tid, order in (getattr(self.service.factory.manager, "orders", {}) or {}).items():
            if mark in (getattr(order, "description", "") or ""):
                return str(tid)
        return None

    # ------------------------------------------------------------------ advancing a batch

    async def _advance(self, batch: Mapping[str, Any]) -> None:
        bid = str(batch["id"])
        rec = self._record(bid)
        f = self.service.factory
        manager, queue = f.manager, f.queue
        subtasks = [s for s in (batch.get("subtasks") or []) if isinstance(s, dict)]
        keys = [str(s.get("key") or i + 1) for i, s in enumerate(subtasks)]
        cancelled = batch.get("status") == "cancelled"
        first = not rec["tasks"] and not rec["failed"]
        results: dict[str, dict[str, Any]] = {}
        note: str | None = None

        for key, sub in zip(keys, subtasks):
            if key in rec["failed"]:
                results[key] = {"state": "failed", "error": rec["failed"][key]}
                continue
            tid = rec["tasks"].get(key) or self._find_order(bid, key)
            if tid:
                rec["tasks"][key] = tid
                results[key] = subtask_state(manager, queue, tid)
                continue
            if cancelled or batch.get("status") not in (None, "claimed", "running"):
                continue
            deps = [str(d) for d in (sub.get("dependsOn") or [])]
            dep_states = {d: (results.get(d) or {}).get("state") for d in deps}
            bad = [d for d, s in dep_states.items() if (s in TERMINAL and s != "complete") or d not in keys]
            if bad:
                reason = f"not started: dependency {', '.join(bad)} did not complete"
                rec["failed"][key] = reason
                results[key] = {"state": "failed", "error": reason}
                continue
            if any(s != "complete" for s in dep_states.values()):
                results[key] = {"state": "queued"}  # waits for its dependencies
                continue
            try:
                tid = self._submit(batch, key, sub)
            except Exception as exc:
                reason = f"could not be submitted: {exc}"[:500]
                log.warning("board dispatch: %s subtask %s %s", bid, key, reason)
                rec["failed"][key] = reason
                results[key] = {"state": "failed", "error": reason}
                continue
            rec["tasks"][key] = tid
            self._save(bid, rec)  # right away: a restart must not submit it again
            log.info("board dispatch: %s subtask %s -> %s", bid, key, tid)
            results[key] = subtask_state(manager, queue, tid)

        if first and batch.get("base"):
            base = getattr(self.service.config, "pr_base", None)
            if base != batch.get("base"):
                note = (f"requested base {batch.get('base')}; this Foundry opens PRs against "
                        f"{base or 'the repo default branch'}")

        changed = {k: v for k, v in results.items() if rec["pushed"].get(k) != v}
        args: dict[str, Any] = {"id": bid, "service": self.service_id, "heartbeat": True}
        if changed:
            args["results"] = changed
        if note:
            args["note"] = note
        if first and rec["tasks"] and batch.get("status") == "claimed":
            args["status"] = "running"
        self._save(bid, rec)
        try:
            await asyncio.to_thread(self.board.op, "updateBatch", args)
        except BoardRefused as exc:
            # cancelled, finished or taken over by another Foundry: stop pushing
            log.info("board dispatch: %s update refused (%s); no longer followed", bid, exc)
            rec["closed"] = True
            self._save(bid, rec)
            return
        rec["pushed"].update(changed)
        if self._settled(keys, results, cancelled):
            rec["closed"] = True
            log.info("board dispatch: %s finished", bid)
        self._save(bid, rec)

    def _settled(self, keys: list[str], results: Mapping[str, Mapping[str, Any]], cancelled: bool) -> bool:
        """Every subtask finished (or never to start) and every PR settled."""
        auto_pr = bool(getattr(self.service.config, "auto_pr", False)) and getattr(
            self.service.factory.manager, "publisher", None) is not None
        for key in keys:
            r = results.get(key)
            if r is None:
                if cancelled:
                    continue
                return False
            if r.get("state") not in TERMINAL:
                return False
            if r.get("state") == "complete" and auto_pr:
                pr = r.get("pr")
                if not pr or pr.get("state") == "pending":
                    return False
        return True

    def _submit(self, batch: Mapping[str, Any], key: str, sub: Mapping[str, Any]) -> str:
        from foundry.floor_manager import WorkOrder
        from foundry.serve import Service

        fields = Service.validate_order({
            "title": sub.get("title"), "description": sub.get("description") or "",
            "files": list(sub.get("files") or []), "size": sub.get("size"),
        })
        desc = fields["description"]
        if fields["size"]:
            desc = (desc + "\n\n" if desc else "") + f"Requested size: {fields['size']}"
        goal = str(batch.get("goal") or "").strip()
        context = f"Part of \"{batch.get('title')}\"" + (f": {goal}" if goal else "")
        desc = (desc + "\n\n" if desc else "") + f"{context}\n{marker(str(batch['id']), key)}"
        acceptance = [str(a) for a in (sub.get("acceptance") or sub.get("acceptance_criteria") or []) if str(a).strip()]
        order = WorkOrder(title=fields["title"], description=desc, files=fields["files"],
                          acceptance_criteria=acceptance, repo_root=str(self.service.repo))
        manager = self.service.factory.manager
        accept = getattr(manager, "accept", None)
        if not callable(accept):
            raise RuntimeError("this Foundry cannot accept orders in the background")
        return str(accept(order))

