"""Long-running Foundry service: ``python -m foundry.serve --repo <path>``.

Owner: alpha-1 (service core). The HTTP layer lives in :mod:`foundry.serve_api`
(alpha-2); this module starts it.

The service keeps one repo-scoped factory running (team pools started, only
tickets of ``--repo`` are dispatched), runs the Shop Foreman's line monitor
every ``FOUNDRY_MONITOR_S`` seconds (default 60:
``foreman.evaluate_line_anomaly()`` -> ``manager.apply_escalation()``), resumes
unfinished work on start (open tickets are picked up by the pools; approved
tasks whose patch was never written are finished) and shuts down gracefully on
Ctrl+C (pools stopped after in-flight completion work, bridge flushed, factory
closed).

Interface used by the HTTP layer (all ``async`` methods must run on the
service's event loop; ``*_threadsafe`` wrappers are for handler threads):

* ``await service.start()`` / ``await service.stop()``
* ``service.health() -> dict``  - ``{ok, repo, model, gemma_ok, claude_ok, version, gemma_speed, pr_ready, pr_reason}``
* ``await service.retry_pr(task_id) -> {ok, pr} | {ok: False, reason}`` - re-publish a failed PR
* ``service.status() -> dict``  - ``{ok, tasks, foreman, paused_until, last_account_error}``
* ``await service.submit(payload) -> task_id`` - raises :class:`OrderError` (HTTP 400)
* ``service.ask(question)`` - async iterator of answer text chunks
* ``service.call_threadsafe(fn_or_coro_fn, *args, timeout=...)`` - run on the loop from a thread
* ``service.ask_threadsafe(question)`` - plain generator of chunks, for a thread

``run_service`` starts ``serve_api.FoundryAPI(service, host, port)`` (a
threaded ``http.server``) after ``service.start()`` and stops it before
``service.stop()``. Handlers reach the loop through ``service.loop``.
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import logging
import os
import queue as _queue_mod
import signal
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Iterator, Mapping, Sequence

from foundry import __version__

log = logging.getLogger("foundry.serve")

__all__ = [
    "DEFAULT_HOST", "DEFAULT_PORT", "DEFAULT_MONITOR_S", "OrderError", "PortInUse", "Service",
    "serve_port", "monitor_interval", "build_parser", "main",
]

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 4310
DEFAULT_MONITOR_S = 60.0
DEFAULT_METRICS_REPORT_S = 600.0
#: Board-bridge methods that take ``(op type, args)``, tried in this order.
_BRIDGE_OP_METHODS = ("op", "send_op", "post_op", "enqueue_op", "enqueue", "send")
#: How long :meth:`Service.stop` waits for background sizing before cancelling
#: it. Cancelling is safe: nothing is applied until the Foreman answers, and a
#: cancelled order is sized again on the next start.
DEFAULT_SIZING_STOP_S = 10.0
#: Upper bounds for user input on POST /api/orders.
MAX_TITLE = 200
MAX_DESCRIPTION = 20_000
MAX_FILES = 200
SIZES = ("S", "M", "L", "XL")

_TERMINAL = ("complete", "quarantined", "failed")


class OrderError(ValueError):
    """An invalid work order (the HTTP layer answers 400 with ``str(exc)``)."""


class PortInUse(OSError):
    """The HTTP API could not bind its port (another Foundry is probably running)."""


#: Fields a POST /api/orders body may carry.
ORDER_FIELDS = frozenset({"title", "description", "files", "size"})


def _max_question() -> int:
    from foundry.serve_api import MAX_QUESTION

    return int(MAX_QUESTION)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        log.warning("foundry serve: ignoring %s=%r (not a number)", name, raw)
        return default
    return value if value > 0 else default


def serve_port() -> int:
    """``FOUNDRY_SERVE_PORT`` (default 4310)."""
    return int(_env_float("FOUNDRY_SERVE_PORT", DEFAULT_PORT))


def monitor_interval() -> float:
    """``FOUNDRY_MONITOR_S`` (default 60 s)."""
    return _env_float("FOUNDRY_MONITOR_S", DEFAULT_MONITOR_S)


def _state(task: Any) -> str:
    raw = (task or {}).get("state", "unknown")
    return str(getattr(raw, "value", raw))


def _num(v: Any) -> float | None:
    """A finite number (rounded to 0.1) or ``None``."""
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v or v in (float("inf"), float("-inf")):
        return None
    return round(float(v), 1)


def _accepts_repo(fn: Any) -> bool:
    """True when ``fn`` takes a ``repo`` keyword (older foremen don't)."""
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    return "repo" in params or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


def _count(v: Any) -> int:
    if isinstance(v, bool):
        return 0
    try:
        return max(0, int(v))
    except (TypeError, ValueError):
        return 0


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace("+00:00", "Z")


def _at_key(value: Any) -> float:
    """Sort key for an action's ``at`` (epoch seconds or an ISO string); unknown -> 0."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0.0
    return 0.0


def _jsonable(value: Any) -> Any:
    """Make an action row JSON-safe (enums -> value, paths/others -> str)."""
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    inner = getattr(value, "value", None)
    if isinstance(inner, (str, int, float)):
        return inner
    return str(value)


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


class Service:
    """One repo, one factory, one monitor loop.

    Args:
        config: a :class:`~foundry.config.FoundryConfig`.
        repo: the repository the service works for (only its tasks are dispatched).
        claude / gemma: the clients (simulated in tests).
        factory: an already-built factory (tests); otherwise built on :meth:`start`.
        monitor_s: seconds between line checks (default :func:`monitor_interval`).
        sleep: async sleep used by the monitor loop (tests pass a fake clock's).
        probe: ``() -> bool`` telling whether Gemma answers; default probes
            ``config.gemma_base_url`` with :func:`foundry.foreman_probe.probe`.
        claude_ok: whether Claude Code was found and validated (shown in health).
        builder_claude: client for builder calls; None = use ``claude``.
        builder_retry_claude: client for builder attempts after the first; None = the builder client.
        reviewer_claude: client for reviewer calls; None = use ``claude``.
        factory_kwargs: extra :func:`~foundry.floor_manager.build_factory` arguments.
    """

    def __init__(
        self,
        config: Any,
        *,
        repo: str | Path,
        claude: Any = None,
        builder_claude: Any = None,
        reviewer_claude: Any = None,
        gemma: Any = None,
        factory: Any = None,
        monitor_s: float | None = None,
        sleep: Callable[[float], Any] | None = None,
        probe: Callable[[], Any] | None = None,
        claude_ok: bool | None = None,
        factory_kwargs: Mapping[str, Any] | None = None,
        batch_board: Any = None,
        batch_watcher: Any = None,
        builder_retry_claude: Any = None,
    ) -> None:
        self.config = config
        self.repo = Path(repo).resolve()
        self.claude = claude
        self.builder_claude = builder_claude
        self.builder_retry_claude = builder_retry_claude
        self.reviewer_claude = reviewer_claude
        self.gemma = gemma
        self.factory = factory
        self.monitor_s = float(monitor_s) if monitor_s else monitor_interval()
        self._sleep = sleep or asyncio.sleep
        self._probe = probe
        self.claude_ok = bool(claude is not None or factory is not None) if claude_ok is None else bool(claude_ok)
        self.gemma_ok: bool | None = None
        self._factory_kwargs = dict(factory_kwargs or {})
        self.loop: asyncio.AbstractEventLoop | None = None
        self._monitor_task: asyncio.Task | None = None
        self._resume_tasks: set[asyncio.Task] = set()
        self.running = False
        self.monitor_runs = 0
        self.last_alert: dict[str, Any] | None = None
        self.last_action: dict[str, Any] | None = None
        self.last_monitor_error: str | None = None
        self.resumed: list[str] = []
        self.closed_factory: Any = None
        self.api: Any = None
        self.sizing_stop_s = DEFAULT_SIZING_STOP_S
        #: ``GitPublisher.preflight()`` result from the last start (None before it ran).
        self.pr_preflight: dict[str, Any] | None = None
        self._preflight_task: asyncio.Task | None = None
        #: Board batches (``foundry.board_dispatch``): watched when a board client is given,
        #: or when the service builds its own factory with the board bridge in ``auto`` or ``off`` mode.
        self._batch_board = batch_board
        self._own_factory = factory is None
        self.batch_watcher: Any = batch_watcher
        #: Seconds between ``reportFoundryMetrics`` sends (a finished task sends one sooner).
        self.metrics_s = _env_float("FOUNDRY_METRICS_REPORT_S", DEFAULT_METRICS_REPORT_S)
        self.metrics_reports = 0
        self.last_metrics_error: str | None = None
        self._metrics_elapsed = 0.0
        self._metrics_finished: set[str] = set()
        #: Checks that the open Foundry PRs work together (:mod:`foundry.combined_check`).
        self.combined_checker: Any = None
        #: Board update requests (:mod:`foundry.update_watcher`), started next to the batch watcher.
        self.update_watcher: Any = None
        #: Set by :meth:`request_restart`; ``run_service`` then returns 75.
        self.restart_requested = False
        #: The event ``run_service`` waits on (stored when it starts).
        self._stop_event: asyncio.Event | None = None

    def request_restart(self) -> None:
        """Stop the service so the start script restarts it (``run_service`` returns 75)."""
        self.restart_requested = True
        event = self._stop_event
        if event is None:
            return
        loop = self.loop
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if loop is not None and running is not loop and loop.is_running():
            loop.call_soon_threadsafe(event.set)
        else:
            event.set()

    # ------------------------------------------------------------------ lifecycle

    def _build(self) -> Any:
        from foundry.floor_manager import build_factory

        kwargs: dict[str, Any] = {"repo_root": self.repo, "workspace": self.repo, "dispatch_repo": self.repo}
        kwargs.update(self._factory_kwargs)
        if "publisher" not in kwargs and getattr(self.config, "auto_pr", False):
            from foundry.git_publisher import GitPublisher

            kwargs["publisher"] = GitPublisher(
                self.repo, base=getattr(self.config, "pr_base", None),
                draft=bool(getattr(self.config, "pr_draft", False)),
            )
        if "snapshots" not in kwargs and getattr(self.config, "auto_pr", False):
            from foundry.git_publisher import GitPublisher

            pub = kwargs.get("publisher")
            if isinstance(pub, GitPublisher):
                # build every task from a clean snapshot of the branch it is published to
                # (your folder's branch and line endings no longer matter)
                from foundry.base_snapshot import BaseSnapshots

                # next to the publisher's worktrees (foundry/data/base by default; git-ignored)
                kwargs["snapshots"] = BaseSnapshots(self.repo, remote=pub.remote,
                                                    root=Path(pub.worktree_root).parent / "base")
        return build_factory(
            self.config, claude=self.claude, builder_claude=self.builder_claude,
            builder_retry_claude=self.builder_retry_claude,
            reviewer_claude=self.reviewer_claude, gemma=self.gemma, **kwargs,
        )

    async def start(self) -> Service:
        """Build the factory (if needed), start the pools, resume work, start the monitor."""
        if self.running:
            return self
        self.loop = asyncio.get_running_loop()
        if self.factory is None:
            self.factory = self._build()
        forget = getattr(self.factory.queue, "kv_delete", None)
        if callable(forget):
            forget("last_account_error")  # only report errors from this run
        for pool in self.factory.pools.values():
            pool.start()
        self.running = True
        self._resume()
        # gh auth status may take a moment: check in the background (health says "not checked yet")
        self._preflight_task = self.loop.create_task(self.check_pr_ready())
        await self.refresh_gemma()
        self._metrics_finished = self._finished_tasks()  # tasks finished before this run send nothing
        self._monitor_task = self.loop.create_task(self._monitor_loop())
        self._start_batches()
        self._start_combined_check()
        log.info("foundry serve: running for %s (%d pool(s))", self.repo, len(self.factory.pools))
        return self

    def _start_batches(self) -> None:
        """Watch the board for Foundry batches of this repo (see :mod:`foundry.board_dispatch`)."""
        if self.batch_watcher is None:
            # ``off`` only stops the department-task mirror: batches and the remote updates started
            # below keep running, or the mode could not be switched back without the PC. A dry run
            # must not touch the real board.
            live = getattr(self.config, "board_bridge_mode", "auto") in ("auto", "off")
            if self._batch_board is None and not (self._own_factory and live):
                return
            from foundry.board_dispatch import BatchWatcher

            try:
                self.batch_watcher = BatchWatcher(self, self._batch_board)
            except Exception as exc:  # batches are optional: the service runs without them
                log.warning("foundry serve: board batches are off: %s", exc)
                return
        if self.batch_watcher.start() is None:
            return
        self._start_updates()

    def _start_updates(self) -> None:
        """Carry out board update requests (same board client, repo, host and service id as the batches)."""
        if self.update_watcher is None:
            w = self.batch_watcher
            try:
                from foundry.update_watcher import UpdateWatcher

                self.update_watcher = UpdateWatcher(
                    self, getattr(w, "board", None) if w is not None else self._batch_board,
                    repo_slug=getattr(w, "repo_slug", None), host=str(getattr(w, "host", "") or "pc"),
                    service_id=str(getattr(w, "service_id", "") or f"foundry:{self.repo.name}"),
                )
            except Exception as exc:  # updates are optional: the service runs without them
                log.warning("foundry serve: board update requests are off: %s", exc)
                return
        self.update_watcher.start()

    def _start_combined_check(self) -> None:
        """Keep checking that the open Foundry PRs work together (needs automatic PRs)."""
        if self.combined_checker is None:
            if not getattr(self.config, "auto_pr", False):
                return
            from foundry.combined_check import CombinedChecker, combined_check_enabled

            if not combined_check_enabled():
                return
            try:
                self.combined_checker = CombinedChecker(self)
            except Exception as exc:  # the check is optional: the service runs without it
                log.warning("foundry serve: the combined PR check is off: %s", exc)
                return
        self.combined_checker.start()

    def _resume(self) -> None:
        """Open tickets of this repo are claimed by the pools; finish approved tasks without a patch."""
        from foundry.main import other_repo_tasks

        f = self.factory
        m = f.manager
        for line in other_repo_tasks(f, self.repo):
            log.info("foundry serve: %s", line)
        resize = getattr(m, "resume_sizing", None)
        if callable(resize):
            # orders recorded but never sized (e.g. cut off by an old API timeout)
            for tid in resize(self._mine):
                self.resumed.append(tid)
        republish = getattr(m, "resume_publishing", None)
        if callable(republish):
            # a PR publish cut off by a stop/crash is retried once; opened/failed ones are left alone
            for tid in republish(self._mine):
                log.info("foundry serve: publishing the PR for %s again", tid)
        prune = getattr(m, "prune_snapshots", None)
        if callable(prune) and getattr(m, "snapshots", None) is not None:
            # snapshots of tasks that finished while stopped, or of commits nobody knows
            t = asyncio.get_running_loop().create_task(prune())
            self._resume_tasks.add(t)
            t.add_done_callback(self._resume_tasks.discard)
        missing = getattr(m, "_patch_missing", None)
        for task in f.queue.all_tasks():
            tid = task["task_id"]
            if not self._mine(tid):
                continue
            state = _state(task)
            if state == "complete":
                unfinished = tid not in m.pr_summaries or (callable(missing) and missing(tid))
                if unfinished and tid in m.orders:
                    self.resumed.append(tid)
                    t = asyncio.get_running_loop().create_task(m.on_task_complete(tid))
                    self._resume_tasks.add(t)
                    t.add_done_callback(self._resume_tasks.discard)
            elif state not in _TERMINAL and tid not in self.resumed:
                self.resumed.append(tid)
        if self.resumed:
            log.info("foundry serve: resuming %s", ", ".join(self.resumed))

    def _mine(self, tid: str) -> bool:
        from foundry.main import _same_repo

        return _same_repo(self.factory.manager.task_repo(tid), self.repo)

    async def stop(self) -> None:
        """Stop the monitor, finish completion work, stop the pools, flush the bridge, close."""
        if self.factory is None:
            return
        self.running = False
        if self.update_watcher is not None:
            await self.update_watcher.stop()
        if self.batch_watcher is not None:
            await self.batch_watcher.stop()
        if self.combined_checker is not None:
            await self.combined_checker.stop()
        if self._monitor_task is not None:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except (asyncio.CancelledError, Exception):
                pass
            self._monitor_task = None
        if self._preflight_task is not None and not self._preflight_task.done():
            self._preflight_task.cancel()
            await asyncio.gather(self._preflight_task, return_exceptions=True)
        self._preflight_task = None
        f = self.factory
        try:
            stop_sizing = getattr(f.manager, "stop_sizing", None)
            if callable(stop_sizing):
                try:
                    await stop_sizing(self.sizing_stop_s)
                except Exception as exc:  # shutdown must go on
                    log.warning("foundry serve: background sizing did not stop cleanly: %s", exc)
            # also after a start that failed half-way: stopping an unstarted pool is a no-op
            if self._resume_tasks:
                await asyncio.wait(set(self._resume_tasks), timeout=f.manager.finish_timeout)
            await f.manager.drain()
            for pool in f.pools.values():
                await pool.stop()
            await f.manager.drain()
        finally:
            f.close()  # flushes the board bridge, saves the manager, closes the databases
            self.factory = None
            self.closed_factory = f
            log.info("foundry serve: stopped")

    # ------------------------------------------------------------------ monitor

    async def monitor_once(self) -> dict[str, Any] | None:
        """One line check: evaluate the line and let the Floor Manager act on an alert."""
        f = self.factory
        self.monitor_runs += 1
        try:
            f.manager.report_quarantines()
            alert = await f.foreman.evaluate_line_anomaly()
            if alert is None:
                self.last_monitor_error = None
                return None
            if getattr(self.config, "foreman_actions", "act") == "log":
                action = await f.manager.apply_escalation(alert, act=False)
            else:
                action = await f.manager.apply_escalation(alert)
            if hasattr(alert, "model_dump"):
                self.last_alert = alert.model_dump(mode="json")
            else:
                self.last_alert = dict(alert) if isinstance(alert, Mapping) else {"alert": str(alert)}
            self.last_action = dict(getattr(action, "__dict__", {}) or {})
            self.last_monitor_error = None
            log.info("foundry serve: %s -> %s (%s)", self.last_alert.get("alert_id"), self.last_action.get("action"),
                     self.last_action.get("detail"))
            return {"alert": self.last_alert, "action": self.last_action}
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # the monitor must never take the service down
            self.last_monitor_error = f"{type(exc).__name__}: {exc}"
            log.warning("foundry serve: line check failed: %s", self.last_monitor_error)
            return None

    async def _monitor_loop(self) -> None:
        while self.running:
            started = time.monotonic()
            await _maybe_await(self._sleep(self.monitor_s))
            if not self.running:
                return
            await self.monitor_once()
            await self.refresh_gemma()
            # a fake sleep takes no real time; a slow line check takes longer than monitor_s
            await self._report_metrics_if_due(max(self.monitor_s, time.monotonic() - started))
            await asyncio.sleep(0)  # a fake sleep must still let the loop run

    async def refresh_gemma(self) -> bool | None:
        """Re-probe Gemma (in a thread; the probe does network I/O)."""
        probe = self._probe
        if probe is None:
            gemma = self.gemma if self.gemma is not None else getattr(self.factory, "gemma", None)
            if gemma is None or not type(gemma).__name__.startswith("Ollama"):
                self.gemma_ok = gemma is not None  # simulated clients need no probe
                return self.gemma_ok

            def probe() -> bool:
                from foundry.foreman_probe import probe as real_probe

                return bool(real_probe(self.config.gemma_base_url, self.config.gemma_model).ok)
        try:
            value = await asyncio.to_thread(probe)
            self.gemma_ok = bool(getattr(value, "ok", value))
        except Exception as exc:
            log.debug("foundry serve: gemma probe failed: %s", exc)
            self.gemma_ok = False
        return self.gemma_ok

    def _finished_tasks(self) -> set[str]:
        """Ids of this repo's finished tasks (the last known set when the queue can't be read)."""
        try:
            return {str(t["task_id"]) for t in self.factory.queue.all_tasks()
                    if _state(t) in _TERMINAL and self._mine(t["task_id"])}
        except Exception as exc:  # metrics must never take the service down
            log.debug("foundry serve: finished tasks not readable: %s", exc)
            return self._metrics_finished

    async def _report_metrics_if_due(self, elapsed: float) -> None:
        """Report when ``metrics_s`` has passed or a task finished since the last round."""
        self._metrics_elapsed += elapsed
        finished = self._finished_tasks()
        new = finished - self._metrics_finished
        self._metrics_finished = finished
        if new or self._metrics_elapsed >= self.metrics_s:
            self._metrics_elapsed = 0.0
            await self.report_metrics()

    def _metrics_sender(self) -> Callable[[str, Mapping[str, Any]], Any] | None:
        """How a board op is sent: the factory's bridge, else (``auto`` and ``off`` modes) the batch board.

        The bridge is found by method name (:data:`_BRIDGE_OP_METHODS`). In ``dry`` mode
        nothing falls through to the batch board: a dry run must not write to the real one.
        """
        bridge = getattr(self.factory, "bridge", None)
        for name in _BRIDGE_OP_METHODS:
            fn = getattr(bridge, name, None)
            if callable(fn):
                return fn
        if getattr(self.config, "board_bridge_mode", "auto") not in ("auto", "off"):
            return None
        op = getattr(getattr(self.batch_watcher, "board", None), "op", None)
        if not callable(op):
            return None
        return lambda type_, args: asyncio.to_thread(op, type_, args)  # a blocking HTTP call

    async def report_metrics(self) -> bool:
        """Send one ``reportFoundryMetrics``; True when sent. Never raises (except cancellation)."""
        send = self._metrics_sender()
        if send is None:
            return False
        try:
            from foundry.metrics_report import build_metrics

            # on the service loop, because a SQLite connection belongs to its thread
            metrics = build_metrics(getattr(self.factory, "telemetry", None))
            service = getattr(self.batch_watcher, "service_id", None) or f"foundry:{self.repo.name}"
            await _maybe_await(send("reportFoundryMetrics", {**metrics, "service": service}))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # metrics are optional: the next report tries again
            self.last_metrics_error = f"{type(exc).__name__}: {exc}"
            log.info("foundry serve: metrics report not sent: %s", self.last_metrics_error)
            return False
        self.metrics_reports += 1
        self.last_metrics_error = None
        return True

    # ------------------------------------------------------------------ pull requests

    def _publisher(self) -> Any:
        m = getattr(self.factory, "manager", None) if self.factory is not None else None
        return getattr(m, "publisher", None)

    async def check_pr_ready(self, timeout: float = 90.0) -> dict[str, Any]:
        """Run the publisher's ``preflight()`` once and log a clear warning when PRs can't be opened."""
        if not getattr(self.config, "auto_pr", False):
            self.pr_preflight = {"ok": False, "reason": "automatic pull requests are off (FOUNDRY_AUTO_PR)"}
            return self.pr_preflight
        pub = self._publisher()
        fn = getattr(pub, "preflight", None)
        if not callable(fn):
            self.pr_preflight = {"ok": False, "reason": "no git publisher is set up"}
            return self.pr_preflight
        try:
            report = await asyncio.wait_for(_maybe_await(fn()), timeout)
            report = dict(report) if isinstance(report, Mapping) else {"ok": bool(report)}
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # a failed check must not stop the service
            report = {"ok": False, "reason": f"the pull-request check failed: {type(exc).__name__}: {exc}"}
        report["ok"] = bool(report.get("ok"))
        if not report["ok"] and not report.get("reason"):
            report["reason"] = "git or the GitHub CLI is not ready"
        self.pr_preflight = report
        if report["ok"]:
            log.info("foundry serve: automatic pull requests are ready")
        else:
            log.warning(
                "foundry serve: WARNING - automatic pull requests will fail: %s. Approved work is still "
                "saved as a .patch; fix this, then use Retry on the task.", report["reason"],
            )
        return report

    async def retry_pr(self, task_id: str) -> dict[str, Any]:
        """``POST /api/tasks/<id>/pr/retry``: publish a failed PR again (in the background)."""
        f = self.factory
        if f is None:
            return {"ok": False, "reason": "Foundry is not running"}
        if not self._mine(task_id) or f.queue.get_task(task_id) is None:
            return {"ok": False, "reason": f"unknown task {task_id}", "code": "not_found"}
        fn = getattr(f.manager, "retry_pr", None)
        if not callable(fn):
            return {"ok": False, "reason": "this Foundry cannot publish pull requests"}
        return dict(await _maybe_await(fn(task_id)))

    # ------------------------------------------------------------------ API surface

    def health(self) -> dict[str, Any]:
        """``{ok, repo, model, gemma_ok, claude_ok, version, gemma_speed}``.

        ``gemma_speed`` is ``{avg_ms, last_ms, fallbacks, calls}`` from the Shop
        Foreman's call stats (all ``None``/0 before the first Gemma call).
        """
        stats = self._gemma_stats()
        pre = self.pr_preflight or {}
        watcher = self.batch_watcher
        out = {
            "pr_ready": bool(pre.get("ok")),
            "pr_reason": None if pre.get("ok") else (pre.get("reason") or "not checked yet"),
            "ok": bool(self.running),
            "repo": str(self.repo),
            "model": getattr(self.config, "gemma_model", None),
            "gemma_ok": bool(self.gemma_ok),
            "claude_ok": bool(self.claude_ok),
            "version": __version__,
            "gemma_speed": {
                "avg_ms": _num(stats.get("avg_ms")),
                "last_ms": _num(stats.get("last_ms")),
                "fallbacks": _count(stats.get("fallbacks")),
                "calls": _count(stats.get("calls")),
            },
        }
        if watcher is not None:
            out["batches"] = watcher.summary()
        out["combined_check"] = self.combined_checker.summary() if self.combined_checker is not None else None
        return out

    def _gemma_stats(self) -> dict[str, Any]:
        """The Shop Foreman's ``gemma_stats()`` (``{}`` when absent or failing)."""
        foreman = getattr(self.factory, "foreman", None) if self.factory is not None else None
        fn = getattr(foreman, "gemma_stats", None)
        if not callable(fn):
            return {}
        try:
            stats = fn()
        except Exception as exc:  # a status call must not fail
            log.debug("foundry serve: gemma_stats failed: %s", exc)
            return {}
        return dict(stats) if isinstance(stats, Mapping) else {}

    _PRE_REVIEW_KEYS = ("passed", "sent_back", "advised", "skipped", "fallback", "claude_reviews_avoided")

    def _pre_review_stats(self, full: Mapping[str, Any]) -> dict[str, int]:
        """Gemma pre-review counters (``{}`` when telemetry has none).

        Taken from the telemetry snapshot's ``pre_review`` key, else from
        ``telemetry.pre_review_counts()`` (older/odd telemetry: ``{}``).
        """
        raw: Any = full.get("pre_review") if isinstance(full, Mapping) else None
        if raw is None:
            fn = getattr(getattr(self.factory, "telemetry", None), "pre_review_counts", None)
            if callable(fn):
                try:
                    raw = fn()
                except Exception as exc:  # a status call must not fail
                    log.debug("foundry serve: pre_review_counts failed: %s", exc)
        if not isinstance(raw, Mapping):
            return {}
        out = {k: _count(raw.get(k)) for k in self._PRE_REVIEW_KEYS}
        if raw.get("claude_reviews_avoided") is None:
            out["claude_reviews_avoided"] = out["sent_back"]
        return out

    def _calibration(self) -> dict[str, Any]:
        """The Shop Foreman's ``calibration_summary()`` (``{}`` when absent or failing)."""
        foreman = getattr(self.factory, "foreman", None) if self.factory is not None else None
        fn = getattr(foreman, "calibration_summary", None)
        if not callable(fn):
            return {}
        try:
            summary = fn(repo=str(self.repo)) if _accepts_repo(fn) else fn()  # same scope as sizing
        except Exception as exc:  # a status call must not fail
            log.debug("foundry serve: calibration_summary failed: %s", exc)
            return {}
        if not isinstance(summary, Mapping):
            return {}
        rules = summary.get("shift_rules")
        return {
            "n": _count(summary.get("n")),
            "accuracy_pct": _num(summary.get("accuracy_pct")),
            "exact": _count(summary.get("exact")),
            "under": _count(summary.get("under")),
            "over": _count(summary.get("over")),
            "shift_rules": [r if isinstance(r, (str, Mapping)) else str(r) for r in rules]
            if isinstance(rules, (list, tuple)) else [],
        }

    def _task_row(self, task: Mapping[str, Any]) -> dict[str, Any]:
        from foundry.main import task_outputs

        f = self.factory
        tid = task["task_id"]
        d = f.manager.directives.get(tid)
        try:
            files = task_outputs(f, tid)
        except Exception:  # pragma: no cover - defensive, a status call must not fail
            files = {"patch": None, "rejected": None, "md": None}
        state = _state(task)
        is_sizing = getattr(f.manager, "is_sizing", None)
        if state in ("backlog", "sizing") and callable(is_sizing) and is_sizing(tid):
            state = "sizing"  # the Shop Foreman is sizing it in the background
        reason = task.get("quarantine_reason") if state in ("quarantined", "failed") else None
        meta = (getattr(f.manager, "sizing_meta", None) or {}).get(tid) or {}
        return {
            "id": tid,
            "title": task.get("title") or (f.manager.orders[tid].title if tid in f.manager.orders else ""),
            "state": state,
            "team": (d.team_id if d else task.get("team_id")),
            "builders": (d.allocated_builders if d else None),
            "patch": str(files["patch"]) if files.get("patch") else None,
            "md": str(files["md"]) if files.get("md") else None,
            "rejected": str(files["rejected"]) if files.get("rejected") else None,
            "reason": reason,
            "sizing_source": meta.get("source"),
            "sizing_ms": meta.get("ms"),
            "pr": self._pr_row(tid),
        }

    def _pr_row(self, tid: str) -> dict[str, Any] | None:
        """``{state, url, branch, error, stage}`` of the task's PR (None: no PR tried)."""
        m = self.factory.manager
        fn = getattr(m, "pr_state", None)
        st = fn(tid) if callable(fn) else None
        if not st:
            return None
        out = {k: st.get(k) for k in ("state", "url", "branch", "error", "stage")}
        is_pub = getattr(m, "is_publishing", None)
        if callable(is_pub) and is_pub(tid):
            out["state"] = "pending"
        return out

    def status(self) -> dict[str, Any]:
        """``{ok, tasks, foreman, paused_until, last_account_error}`` (this repo's tasks only)."""
        f = self.factory
        if f is None:
            return {"ok": False, "tasks": [], "foreman": {}, "paused_until": None, "last_account_error": None}
        tasks = [self._task_row(t) for t in f.queue.all_tasks() if self._mine(t["task_id"])]
        snap: dict[str, Any] = {}
        full: Any = {}
        try:
            full = f.telemetry.snapshot()
            snap = {k: full[k] for k in ("generated_at", "tokens", "current_tpm", "current_rpm", "burn_rate",
                                         "idle_agent_count", "queue_dwell") if k in full}
        except Exception as exc:  # pragma: no cover - defensive
            snap = {"error": str(exc)}
        snap.update({
            "monitor_s": self.monitor_s,
            "monitor_runs": self.monitor_runs,
            "last_alert": self.last_alert,
            "last_action": self.last_action,
            "last_error": self.last_monitor_error,
            "gemma": self._gemma_stats(),
            "pre_review": self._pre_review_stats(full if isinstance(full, Mapping) else {}),
            "calibration": self._calibration(),
        })
        paused_until = None
        remaining = getattr(f.limiter, "pause_remaining", None)
        if callable(remaining):
            left = float(remaining())
            if left > 0:
                paused_until = (datetime.now(timezone.utc) + timedelta(seconds=left)).isoformat().replace("+00:00", "Z")
        from foundry.main import _account_error

        return {
            "ok": True,
            "tasks": tasks,
            "foreman": snap,
            "paused_until": paused_until,
            "last_account_error": _account_error(f),
        }

    # ------------------------------------------------------------------ Foreman actions (charlie-1)

    def actions(self, limit: int = 50) -> list[dict[str, Any]]:
        """The Foreman's applied actions, newest first (``manager.actions(limit)``); ``[]`` without a log."""
        f = self.factory
        fn = getattr(getattr(f, "manager", None), "actions", None) if f is not None else None
        if not callable(fn):
            return []
        rows = self._all_actions(fn)
        out = [_jsonable(dict(r)) for r in rows]
        out = [r for r in out if not self._foreign(r.get("task_id"))]
        out.sort(key=lambda r: _at_key(r.get("at")), reverse=True)
        return out[: max(0, int(limit))]

    @staticmethod
    def _all_actions(fn: Callable[..., Any]) -> list[Mapping[str, Any]]:
        """The whole action log (filtering by repo happens before the caller's limit is applied)."""
        try:
            from foundry.floor_manager import ACTION_LOG_MAX as cap
        except ImportError:  # pragma: no cover - older manager
            cap = 500
        try:
            rows = fn(limit=max(int(cap), 1))
        except TypeError:
            rows = fn()
        return [r for r in (rows or []) if isinstance(r, Mapping)]

    def _foreign(self, tid: Any) -> bool:
        """True only when the task is known to belong to another repository (its actions stay hidden)."""
        if not tid:
            return False
        try:
            repo = self.factory.manager.task_repo(str(tid))
        except Exception:  # noqa: BLE001 - a fake/older manager: show the action
            return False
        return bool(repo) and not self._mine(str(tid))

    async def undo_action(self, action_id: str, by: str = "user") -> dict[str, Any]:
        """Undo one Foreman action (``manager.undo_action``); ``{ok, ...}`` or ``{ok: False, reason}``."""
        f = self.factory
        fn = getattr(getattr(f, "manager", None), "undo_action", None) if f is not None else None
        if not callable(fn):
            return {"ok": False, "reason": "This Foundry version cannot undo actions.", "code": "unsupported"}
        lister = getattr(f.manager, "actions", None)
        if callable(lister):
            entry = next((r for r in self._all_actions(lister) if str(r.get("action_id")) == str(action_id)), None)
            if entry is not None and self._foreign(entry.get("task_id")):
                return {"ok": False, "action_id": action_id, "reason": "unknown action", "code": "not_found"}
        result = await _maybe_await(fn(action_id, by=by))
        out = _jsonable(dict(result)) if isinstance(result, Mapping) else {"ok": bool(result)}
        out["ok"] = bool(out.get("ok"))
        out.setdefault("action_id", action_id)
        return out

    @staticmethod
    def validate_order(payload: Any) -> dict[str, Any]:
        """Check a POST /api/orders body; returns the cleaned fields or raises :class:`OrderError`."""
        if not isinstance(payload, Mapping):
            raise OrderError("the body must be a JSON object")
        unknown = set(payload) - ORDER_FIELDS
        if unknown:
            raise OrderError(f"unknown field(s): {', '.join(sorted(map(str, unknown)))}")
        title = payload.get("title")
        if not isinstance(title, str) or not title.strip():
            raise OrderError("title is required")
        title = title.strip()
        if len(title) > MAX_TITLE:
            raise OrderError(f"title is longer than {MAX_TITLE} characters")
        desc = payload.get("description", "")
        if desc is None:
            desc = ""
        if not isinstance(desc, str):
            raise OrderError("description must be a string")
        if len(desc) > MAX_DESCRIPTION:
            raise OrderError(f"description is longer than {MAX_DESCRIPTION} characters")
        files = payload.get("files", [])
        if files is None:
            files = []
        if isinstance(files, str):
            files = [p for p in files.split(",")]
        if not isinstance(files, list) or not all(isinstance(p, str) for p in files):
            raise OrderError("files must be a list of repo-relative paths")
        clean: list[str] = []
        for p in files:
            p = p.strip().replace("\\", "/")
            if not p:
                continue
            if p.startswith("/") or (len(p) > 1 and p[1] == ":") or ".." in p.split("/"):
                raise OrderError(f"files must be repo-relative paths inside the repository: {p}")
            if p not in clean:
                clean.append(p)
        if len(clean) > MAX_FILES:
            raise OrderError(f"at most {MAX_FILES} files")
        size = payload.get("size")
        if size in (None, ""):
            size = None
        elif not isinstance(size, str) or size.strip().upper() not in SIZES:
            raise OrderError("size must be S, M, L or XL")
        else:
            size = size.strip().upper()
        return {"title": title, "description": desc, "files": clean, "size": size}

    async def submit(self, payload: Any = None, **fields: Any) -> str:
        """Submit a work order for the service's repo; returns the Foundry task id at once.

        The Shop Foreman sizes the order in the background (a CPU Gemma can take
        longer than the HTTP call may wait); :meth:`status` shows it as
        ``sizing`` until its tickets exist.

        Takes a body dict (``submit({...})``) or keywords
        (``submit(title=, description=, files=, size=)``, as ``serve_api`` calls it).
        """
        if not self.running or self.factory is None:
            raise RuntimeError("Foundry is not running")
        if payload is None:
            payload = {k: v for k, v in fields.items() if v is not None}
        elif fields:
            raise OrderError("pass a body dict or keywords, not both")
        fields = self.validate_order(payload)
        from foundry.floor_manager import WorkOrder

        desc = fields["description"]
        if fields["size"]:
            # sizing is the Shop Foreman's call; the requested size is passed on as a hint
            desc = (desc + "\n\n" if desc else "") + f"Requested size: {fields['size']}"
        order = WorkOrder(title=fields["title"], description=desc, files=fields["files"], repo_root=str(self.repo))
        manager = self.factory.manager
        accept = getattr(manager, "accept", None)
        if callable(accept):
            return accept(order)
        return await manager.submit(order)

    async def ask(self, question: str) -> AsyncIterator[str]:
        """Stream the Shop Foreman's answer (``foreman.answer_question``)."""
        if not isinstance(question, str) or not question.strip():
            raise OrderError("question is required")
        if self.factory is None:
            raise RuntimeError("Foundry is not running")
        async for chunk in self.factory.foreman.answer_question(question.strip()[:_max_question()]):
            if chunk:
                yield chunk

    # ------------------------------------------------------------------ thread bridges

    def call_threadsafe(self, fn: Callable[..., Any], *args: Any, timeout: float = 30.0) -> Any:
        """Run ``fn(*args)`` (sync or async) on the service loop from another thread; returns its result."""
        if self.loop is None or not self.loop.is_running():
            raise RuntimeError("Foundry is not running")

        async def run() -> Any:
            return await _maybe_await(fn(*args))

        return asyncio.run_coroutine_threadsafe(run(), self.loop).result(timeout)

    def ask_threadsafe(self, question: str, *, timeout: float = 300.0) -> Iterator[str]:
        """Plain generator of answer chunks for a handler thread (errors are raised in the thread)."""
        if self.loop is None or not self.loop.is_running():
            raise RuntimeError("Foundry is not running")
        if not isinstance(question, str) or not question.strip():
            raise OrderError("question is required")
        q: _queue_mod.Queue = _queue_mod.Queue()
        done = object()

        async def pump() -> None:
            try:
                async for chunk in self.ask(question):
                    q.put(chunk)
            except BaseException as exc:  # handed to the thread
                q.put(exc)
            finally:
                q.put(done)

        fut = asyncio.run_coroutine_threadsafe(pump(), self.loop)
        try:
            while True:
                item = q.get(timeout=timeout)
                if item is done:
                    return
                if isinstance(item, BaseException):
                    raise item
                yield item
        finally:
            if not fut.done():
                fut.cancel()


# --------------------------------------------------------------------------- CLI


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m foundry.serve", description="Run Foundry as a local service.")
    p.add_argument("--repo", default=".", help="repository the service works for (default: current folder)")
    p.add_argument("--host", default=DEFAULT_HOST, help=argparse.SUPPRESS)  # loopback only
    p.add_argument("--port", type=int, default=None, help="HTTP port (default FOUNDRY_SERVE_PORT or 4310)")
    p.add_argument("--board", choices=["dry", "auto", "off"], default=None, help="board bridge mode")
    p.add_argument("--monitor", type=float, default=None, help="seconds between line checks (FOUNDRY_MONITOR_S)")
    return p


async def run_service(service: Service, *, host: str = DEFAULT_HOST, port: int | None = None,
                      stop_event: asyncio.Event | None = None, out: Any = print) -> int:
    """Start ``service`` and its HTTP API, run until ``stop_event`` (or Ctrl+C), then shut down."""
    from foundry import serve_api

    stop_event = stop_event or asyncio.Event()
    service._stop_event = stop_event  # service.request_restart() sets it
    loop = asyncio.get_running_loop()
    for sig in (getattr(signal, "SIGINT", None), getattr(signal, "SIGTERM", None)):
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, RuntimeError, ValueError):
            pass  # Windows: KeyboardInterrupt reaches asyncio.run instead
    api = None
    try:
        await service.start()  # inside the try: a half-started service is still stopped and closed
        bind_port = port if port is not None else serve_port()
        try:
            api = serve_api.FoundryAPI(service, host, bind_port).start()
        except OSError as exc:
            raise PortInUse(f"Foundry could not listen on {host}:{bind_port}: {exc}") from exc
        service.api = api
        out(f"Foundry is running for {service.repo} on http://{host}:{api.port} - press Ctrl+C to stop")
        await stop_event.wait()
    finally:
        out("Foundry is stopping (finishing in-flight work)...")
        if api is not None:
            await asyncio.to_thread(api.stop)
        await service.stop()
        out("Foundry stopped.")
    # 75: the start script starts Foundry again (after an update was applied)
    return 75 if getattr(service, "restart_requested", False) else 0


def enable_ctrl_c(platform: str | None = None, kernel32: Any = None) -> bool:
    """Re-enable Ctrl+C on Windows; returns True if the call was made.

    A process started in a new process group (as Node's ``detached`` spawn does)
    inherits "ignore Ctrl+C"; ``SetConsoleCtrlHandler(NULL, FALSE)`` turns it back
    on for this process. No-op on other platforms; never raises.
    """
    if (platform or sys.platform) != "win32":
        return False
    try:
        if kernel32 is None:
            import ctypes

            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        kernel32.SetConsoleCtrlHandler(None, False)
        return True
    except Exception:  # noqa: BLE001 - best effort, the service runs anyway
        return False


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point; returns the process exit code."""
    enable_ctrl_c()
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    from foundry.config import FoundryConfig
    from foundry.main import builder_claude_args, builder_retry_claude_args, claude_args, reviewer_claude_args

    config = FoundryConfig.from_env(**({"board_bridge_mode": args.board} if args.board else {}))
    repo = Path(args.repo).resolve()
    if not repo.is_dir():
        print(f"--repo {repo} is not a folder", file=sys.stderr)
        return 2
    from foundry.claude_client import ClaudeCodeClient, ClaudeError, resolve_claude_bin
    from foundry.gemma_client import OllamaGemmaClient

    # Builders that run tests need `python` to be Foundry's own interpreter (it has pytest).
    builder_client_options: dict[str, Any] = {}
    if getattr(config, "builder_mode", "diff") == "edit" and getattr(config, "builder_tests", False):
        builder_client_options["env"] = {
            "PATH": os.path.dirname(sys.executable) + os.pathsep + os.environ.get("PATH", "")
        }
    try:
        claude = ClaudeCodeClient(resolve_claude_bin(config.claude_bin), extra_args=claude_args(config))
        builder_claude = (
            ClaudeCodeClient(
                resolve_claude_bin(config.claude_bin),
                extra_args=builder_claude_args(config),
                **builder_client_options,
            )
            if getattr(config, "builder_tools", "off") == "read"
            or getattr(config, "builder_mode", "diff") == "edit"
            else None
        )
        builder_retry_claude = (
            ClaudeCodeClient(
                resolve_claude_bin(config.claude_bin),
                extra_args=builder_retry_claude_args(config),
                **builder_client_options,
            )
            if getattr(config, "builder_retry_model", None)
            else None
        )
        reviewer_claude = (
            ClaudeCodeClient(resolve_claude_bin(config.claude_bin), extra_args=reviewer_claude_args(config))
            if getattr(config, "reviewer_tools", "off") == "read"
            else None
        )
        validate = getattr(claude, "validate_launch", None)
        if callable(validate):
            validate()
    except ClaudeError as exc:
        print(f"Claude Code is not ready: {exc}", file=sys.stderr)
        return 2
    gemma = OllamaGemmaClient(config.gemma_base_url, config.gemma_model, timeout=config.gemma_timeout_s)
    service = Service(
        config, repo=repo, claude=claude, builder_claude=builder_claude,
        builder_retry_claude=builder_retry_claude, reviewer_claude=reviewer_claude,
        gemma=gemma,
        monitor_s=args.monitor, claude_ok=True,
    )
    try:
        return asyncio.run(run_service(service, host=DEFAULT_HOST, port=args.port))
    except PortInUse as exc:  # another Foundry is probably running
        print(f"{exc} - is Foundry already running?", file=sys.stderr)
        return 2
    except KeyboardInterrupt:  # Windows Ctrl+C; run_service's finally already shut down
        return 0


if __name__ == "__main__":
    sys.exit(main())
