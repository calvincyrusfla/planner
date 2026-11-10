"""Foundry update requests from the department board.

The board keeps ``state.foundryUpdates``: requests ``{id, repo, settings, now, status, ...}``
with ``status`` one of ``queued|claimed|done|refused|failed|cancelled``. The
:class:`UpdateWatcher` runs next to the batch watcher (same board client, repo
slug, host and service id) and every ``interval`` seconds:

1. reads the board state and takes the oldest ``queued`` request whose ``repo``
   is this Foundry's repo (case-insensitive);
2. unless the request says ``now``, waits (until a later poll) while Foundry is
   busy: any ticket ``claimed`` or ``in_review`` whose task is still live
   (not missing, complete, quarantined or failed);
3. claims it (``claimFoundryUpdate``; a refusal means someone else took it);
4. runs ``apply(service.repo, settings)`` (default
   :func:`foundry.self_update.apply_update`);
5. reports the result (``finishFoundryUpdate``) and, when the update is
   ``done``, asks the service to restart (``service.request_restart()``).

Board errors are logged and retried on the next poll; one request is handled at a time.

With the ``auto_update`` setting on, a poll that finds nothing to carry out also
compares this checkout with ``origin`` (at most every :data:`AUTO_UPDATE_CHECK_S`
seconds) and, when the remote branch has a commit that HEAD does not contain,
sends ``requestFoundryUpdate`` (``by: "auto"``) once for that remote commit. The
steps above then carry it out.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, Mapping

log = logging.getLogger("foundry.update_watcher")

__all__ = ["AUTO_UPDATE_CHECK_S", "DEFAULT_UPDATE_POLL_S", "MAX_MESSAGE", "UpdateWatcher"]

DEFAULT_UPDATE_POLL_S = 15.0
#: The result message sent to the board is cut to this many characters.
MAX_MESSAGE = 2000
#: Task states that never come back to life (``builder_worker._TERMINAL_STATES``).
_TERMINAL_STATES = frozenset({"complete", "quarantined", "failed"})
#: Auto-update looks at the remote branch at most this often (seconds).
AUTO_UPDATE_CHECK_S = 60.0
#: Time limit of each git call of the auto-update check (seconds).
_AUTO_GIT_TIMEOUT = 30.0
#: Request states that block a new request (the board's ``OPEN_UPDATE``).
_OPEN_STATES = frozenset({"queued", "claimed"})


def _field(result: Any, *names: str) -> Any:
    """The first of ``names`` found on ``result`` (a mapping or an object)."""
    for name in names:
        if isinstance(result, Mapping):
            if name in result:
                return result[name]
        elif hasattr(result, name):
            return getattr(result, name)
    return None


def _refused(exc: BaseException) -> bool:
    try:
        from foundry.board_dispatch import BoardRefused
    except ImportError:  # pragma: no cover - board_dispatch always ships with Foundry
        return False
    return isinstance(exc, BoardRefused)


class UpdateWatcher:
    """Carries out this repo's board update requests, one at a time.

    Args:
        service: the :class:`foundry.serve.Service` (``repo``, ``factory``, ``request_restart()``).
        board: a board client with ``state() -> dict`` and ``op(type, args)`` (blocking; called in a thread).
        repo_slug: ``owner/name`` this Foundry works for.
        host: this PC's name.
        service_id: this Foundry's service id on the board.
        apply: ``async (repo, settings) -> UpdateResult``; default :func:`foundry.self_update.apply_update`.
        interval: seconds between polls.
        clock: monotonic time source of the auto-update timer; default :func:`time.monotonic`.
    """

    def __init__(
        self,
        service: Any,
        board: Any,
        *,
        repo_slug: str | None,
        host: str,
        service_id: str,
        apply: Callable[[Any, Any], Any] | None = None,
        interval: float = DEFAULT_UPDATE_POLL_S,
        sleep: Callable[[float], Any] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.service = service
        self.board = board
        self.repo_slug = repo_slug
        self.host = host
        self.service_id = service_id
        self._apply = apply
        self.interval = float(interval) if interval and interval > 0 else DEFAULT_UPDATE_POLL_S
        self._sleep = sleep or asyncio.sleep
        self._task: asyncio.Task[Any] | None = None
        self._lock = asyncio.Lock()
        self.rounds = 0
        self.last_error: str | None = None
        self.handled: list[str] = []
        #: Set once an update is done and a restart was requested: nothing more is claimed.
        self.restarting = False
        self._clock: Callable[[], float] = clock or time.monotonic
        self._last_auto_check: float | None = None
        #: Remote commits an automatic request was already sent for.
        self._auto_requested: set[str] = set()

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> asyncio.Task[Any] | None:
        if not self.repo_slug:
            return None
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._loop())
            log.info("foundry updates: watching the board for %s update requests every %gs",
                     self.repo_slug, self.interval)
        return self._task

    async def stop(self) -> None:
        t, self._task = self._task, None
        if t is not None:
            t.cancel()
            await asyncio.gather(t, return_exceptions=True)

    async def _loop(self) -> None:
        while True:
            await self.poll_once()
            v = self._sleep(self.interval)
            if asyncio.iscoroutine(v) or isinstance(v, asyncio.Future):
                await v
            await asyncio.sleep(0)

    def summary(self) -> dict[str, Any]:
        return {"repo": self.repo_slug, "service": self.service_id, "rounds": self.rounds,
                "handled": list(self.handled), "last_error": self.last_error, "restarting": self.restarting}

    # ------------------------------------------------------------------ one poll

    async def poll_once(self) -> str | None:
        """One poll; returns the id of the request carried out (None: nothing done). Never raises."""
        if self.restarting or not self.repo_slug:
            return None
        async with self._lock:
            self.rounds += 1
            try:
                return await self._round()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # the watcher must never take the service down
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.warning("foundry updates: poll failed (retrying next poll): %s", self.last_error)
                return None

    def _same_repo(self, request: Mapping[str, Any]) -> bool:
        repo = request.get("repo")
        return isinstance(repo, str) and repo.strip().lower() == str(self.repo_slug).strip().lower()

    def _matches(self, request: Any) -> bool:
        if not isinstance(request, Mapping) or not request.get("id") or request.get("status") != "queued":
            return False
        return self._same_repo(request)

    def _has_open_request(self, state: Any) -> bool:
        requests = state.get("foundryUpdates") if isinstance(state, Mapping) else None
        return any(isinstance(r, Mapping) and r.get("status") in _OPEN_STATES and self._same_repo(r)
                   for r in (requests if isinstance(requests, list) else []))

    def _next_request(self, state: Any) -> Mapping[str, Any] | None:
        requests = state.get("foundryUpdates") if isinstance(state, Mapping) else None
        mine = [r for r in (requests if isinstance(requests, list) else []) if self._matches(r)]
        if not mine:
            return None
        # oldest first; the board's order breaks ties (sorted is stable)
        mine.sort(key=lambda r: str(r.get("createdAt") or ""))
        return mine[0]

    def busy(self) -> bool:
        """True while any ticket of a live task is claimed or in review (no factory = not busy).

        Tickets whose task is missing or finished (complete, quarantined, failed) do not count;
        an error while reading a ticket's task counts that ticket as not busy.
        """
        factory = getattr(self.service, "factory", None)
        queue = getattr(factory, "queue", None) if factory is not None else None
        tickets = getattr(queue, "tickets", None)
        if not callable(tickets):
            return False
        for t in tickets() or []:
            status = _field(t, "status")
            status = getattr(status, "value", status)
            if status == "claimed" or bool(_field(t, "in_review")):
                if self._task_live(queue, t):
                    return True
        return False

    @staticmethod
    def _task_live(queue: Any, ticket: Any) -> bool:
        """True when the ticket's task exists and is not in a terminal state."""
        task_id = _field(ticket, "task_id")
        try:
            task = queue.get_task(task_id)
            if task is None:
                return False
            state = task.get("state") if isinstance(task, Mapping) else getattr(task, "state", None)
            state = getattr(state, "value", state)
            return state not in _TERMINAL_STATES
        except Exception as exc:
            log.info("foundry updates: could not read task %s of a busy ticket (not counted): %s", task_id, exc)
            return False

    async def _round(self) -> str | None:
        state = await asyncio.to_thread(self.board.state)
        self.last_error = None
        request = self._next_request(state)
        if request is None:
            await self._auto_update(state)
            return None
        rid = str(request["id"])
        if not request.get("now") and self.busy():
            log.info("foundry updates: %s waits until Foundry is idle", rid)
            return None
        if not await self._claim(rid):
            return None
        result = await self._run_apply(request.get("settings"))
        status = str(_field(result, "status") or "failed")
        message = str(_field(result, "message") or "")[:MAX_MESSAGE]
        payload = {
            "id": rid, "service": self.service_id, "status": status,
            "result": {
                "commitBefore": _field(result, "commit_before", "commitBefore"),
                "commitAfter": _field(result, "commit_after", "commitAfter"),
                "message": message,
            },
        }
        self.handled.append(rid)
        try:
            await asyncio.to_thread(self.board.op, "finishFoundryUpdate", payload)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # the update itself already happened
            self.last_error = f"{type(exc).__name__}: {exc}"
            log.warning("foundry updates: could not report %s (%s): %s", rid, status, self.last_error)
        log.info("foundry updates: %s %s: %s", rid, status, message)
        if status == "done":
            self.restarting = True
            restart = getattr(self.service, "request_restart", None)
            if callable(restart):
                restart()
        return rid

    # ------------------------------------------------------------------ auto-update

    async def _auto_update(self, state: Any) -> None:
        """Request an update when ``auto_update`` is on and the remote branch moved.

        A git or board error is logged and tried again at the next check.
        """
        if not getattr(getattr(self.service, "config", None), "auto_update", False):
            return
        if self._has_open_request(state):
            return
        now = self._clock()
        if self._last_auto_check is not None and now - self._last_auto_check < AUTO_UPDATE_CHECK_S:
            return
        self._last_auto_check = now
        try:
            remote = await self._new_remote_sha()
            if remote is None:
                return
            out = await asyncio.to_thread(self.board.op, "requestFoundryUpdate",
                                          {"repo": self.repo_slug, "by": "auto"})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.info("foundry updates: auto-update check failed (retrying later): %s: %s", type(exc).__name__, exc)
            return
        if isinstance(out, Mapping) and out.get("ok") is False:
            log.info("foundry updates: auto-update request refused (retrying later): %s", out.get("reason"))
            return
        self._auto_requested.add(remote)
        log.info("foundry updates: %s moved to %s, update requested", self.repo_slug, remote[:7])

    async def _new_remote_sha(self) -> str | None:
        """The remote branch's commit when it should be pulled, else None.

        None when the remote matches HEAD, was already requested, or is contained in HEAD:
        pulling a commit HEAD already has changes nothing, yet would still restart Foundry.
        """
        head = await self._git_out("rev-parse", "HEAD")
        branch = await self._git_out("symbolic-ref", "--short", "HEAD")
        ref = f"refs/heads/{branch}"
        listing = await self._git_out("ls-remote", "origin", ref)
        rows = [line.split() for line in listing.splitlines()]
        remote = next((row[0] for row in rows if len(row) == 2 and row[1] == ref), None)
        if remote is None:
            log.info("foundry updates: %s is not on origin, no auto-update", branch)
            return None
        if remote == head or remote in self._auto_requested:
            return None
        # exit 1: not an ancestor; 128: the commit is not fetched yet, so HEAD cannot contain it
        rc, _, _ = await self._git("merge-base", "--is-ancestor", remote, "HEAD")
        return None if rc == 0 else remote

    async def _git(self, *args: str) -> tuple[int, str, str]:
        from foundry.self_update import _git  # non-interactive, without the git location variables

        return await _git(self.service.repo, *args, timeout=_AUTO_GIT_TIMEOUT)

    async def _git_out(self, *args: str) -> str:
        rc, out, err = await self._git(*args)
        if rc != 0:
            raise RuntimeError(f"git {args[0]} exited {rc}: {(err or out).strip()}")
        return out.strip()

    async def _claim(self, rid: str) -> bool:
        try:
            out = await asyncio.to_thread(self.board.op, "claimFoundryUpdate",
                                          {"id": rid, "host": self.host, "service": self.service_id})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if _refused(exc):
                log.info("foundry updates: could not claim %s: %s", rid, exc)
                return False
            raise  # board down: retried next poll
        if isinstance(out, Mapping) and out.get("ok") is False:
            log.info("foundry updates: could not claim %s: %s", rid, out.get("reason"))
            return False
        return True

    async def _run_apply(self, settings: Any) -> Any:
        apply = self._apply
        if apply is None:
            from foundry.self_update import apply_update

            apply = apply_update
        try:
            result = apply(self.service.repo, settings if isinstance(settings, Mapping) else None)
            if asyncio.iscoroutine(result) or isinstance(result, asyncio.Future):
                result = await result
            return result
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("foundry updates: the update failed: %s", exc)
            return {"status": "failed", "commit_before": None, "commit_after": None,
                    "message": f"{type(exc).__name__}: {exc}"}
