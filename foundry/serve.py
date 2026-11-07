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
* ``service.health() -> dict``  - ``{ok, repo, model, gemma_ok, claude_ok, version}``
* ``service.status() -> dict``  - ``{ok, tasks, foreman, paused_until, last_account_error}``
* ``await service.submit(payload) -> task_id`` - raises :class:`OrderError` (HTTP 400)
* ``service.ask(question)`` - async iterator of answer text chunks
* ``service.call_threadsafe(fn_or_coro_fn, *args, timeout=...)`` - run on the loop from a thread
* ``service.ask_threadsafe(question)`` - plain generator of chunks, for a thread
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
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Iterator, Mapping, Sequence

from foundry import __version__

log = logging.getLogger("foundry.serve")

__all__ = [
    "DEFAULT_HOST", "DEFAULT_PORT", "DEFAULT_MONITOR_S", "OrderError", "Service",
    "serve_port", "monitor_interval", "build_parser", "main",
]

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 4310
DEFAULT_MONITOR_S = 60.0
#: Upper bounds for user input on POST /api/orders.
MAX_TITLE = 200
MAX_DESCRIPTION = 20_000
MAX_FILES = 200
SIZES = ("S", "M", "L")

_TERMINAL = ("complete", "quarantined", "failed")


class OrderError(ValueError):
    """An invalid work order (the HTTP layer answers 400 with ``str(exc)``)."""


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


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace("+00:00", "Z")


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
        factory_kwargs: extra :func:`~foundry.floor_manager.build_factory` arguments.
    """

    def __init__(
        self,
        config: Any,
        *,
        repo: str | Path,
        claude: Any = None,
        gemma: Any = None,
        factory: Any = None,
        monitor_s: float | None = None,
        sleep: Callable[[float], Any] | None = None,
        probe: Callable[[], Any] | None = None,
        claude_ok: bool | None = None,
        factory_kwargs: Mapping[str, Any] | None = None,
    ) -> None:
        self.config = config
        self.repo = Path(repo).resolve()
        self.claude = claude
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

    # ------------------------------------------------------------------ lifecycle

    def _build(self) -> Any:
        from foundry.floor_manager import build_factory

        kwargs = {"repo_root": self.repo, "workspace": self.repo, "dispatch_repo": self.repo}
        kwargs.update(self._factory_kwargs)
        return build_factory(self.config, claude=self.claude, gemma=self.gemma, **kwargs)

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
        await self.refresh_gemma()
        self._monitor_task = self.loop.create_task(self._monitor_loop())
        log.info("foundry serve: running for %s (%d pool(s))", self.repo, len(self.factory.pools))
        return self

    def _resume(self) -> None:
        """Open tickets of this repo are claimed by the pools; finish approved tasks without a patch."""
        from foundry.main import other_repo_tasks

        f = self.factory
        m = f.manager
        for line in other_repo_tasks(f, self.repo):
            log.info("foundry serve: %s", line)
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
            elif state not in _TERMINAL:
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
        was_running, self.running = self.running, False
        if self._monitor_task is not None:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except (asyncio.CancelledError, Exception):
                pass
            self._monitor_task = None
        f = self.factory
        try:
            if was_running:
                if self._resume_tasks:
                    await asyncio.wait(set(self._resume_tasks), timeout=f.manager.finish_timeout)
                await f.manager.drain()
                for pool in f.pools.values():
                    await pool.stop()
                await f.manager.drain()
        finally:
            f.close()  # flushes the board bridge, saves the manager, closes the databases
            self.factory = None if self._built_here else f
            log.info("foundry serve: stopped")

    @property
    def _built_here(self) -> bool:
        return False

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
            action = await f.manager.apply_escalation(alert)
            self.last_alert = alert.model_dump(mode="json") if hasattr(alert, "model_dump") else dict(alert)
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
            await _maybe_await(self._sleep(self.monitor_s))
            if not self.running:
                return
            await self.monitor_once()
            await self.refresh_gemma()
            await asyncio.sleep(0)  # a fake sleep must still let the loop run

    async def refresh_gemma(self) -> bool | None:
        """Re-probe Gemma (in a thread; the probe does network I/O)."""
        probe = self._probe
        if probe is None:
            if self.gemma is None and self._factory_kwargs.get("gemma") is None and self.factory is not None \
                    and getattr(self.factory, "gemma", None) is None:
                self.gemma_ok = False
                return False

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

    # ------------------------------------------------------------------ API surface

    def health(self) -> dict[str, Any]:
        """``{ok, repo, model, gemma_ok, claude_ok, version}``."""
        return {
            "ok": bool(self.running),
            "repo": str(self.repo),
            "model": getattr(self.config, "gemma_model", None),
            "gemma_ok": bool(self.gemma_ok),
            "claude_ok": bool(self.claude_ok),
            "version": __version__,
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
        reason = task.get("quarantine_reason") if state in ("quarantined", "failed") else None
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
        }

    def status(self) -> dict[str, Any]:
        """``{ok, tasks, foreman, paused_until, last_account_error}`` (this repo's tasks only)."""
        f = self.factory
        if f is None:
            return {"ok": False, "tasks": [], "foreman": {}, "paused_until": None, "last_account_error": None}
        tasks = [self._task_row(t) for t in f.queue.all_tasks() if self._mine(t["task_id"])]
        snap: dict[str, Any] = {}
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

    @staticmethod
    def validate_order(payload: Any) -> dict[str, Any]:
        """Check a POST /api/orders body; returns the cleaned fields or raises :class:`OrderError`."""
        if not isinstance(payload, Mapping):
            raise OrderError("the body must be a JSON object")
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
            raise OrderError("size must be S, M or L")
        else:
            size = size.strip().upper()
        return {"title": title, "description": desc, "files": clean, "size": size}

    async def submit(self, payload: Any) -> str:
        """Submit a work order for the service's repo; returns the Foundry task id."""
        if not self.running or self.factory is None:
            raise RuntimeError("Foundry is not running")
        fields = self.validate_order(payload)
        from foundry.floor_manager import WorkOrder

        desc = fields["description"]
        if fields["size"]:
            # sizing is the Shop Foreman's call; the requested size is passed on as a hint
            desc = (desc + "\n\n" if desc else "") + f"Requested size: {fields['size']}"
        order = WorkOrder(title=fields["title"], description=desc, files=fields["files"], repo_root=str(self.repo))
        return await self.factory.manager.submit(order)

    async def ask(self, question: str) -> AsyncIterator[str]:
        """Stream the Shop Foreman's answer (``foreman.answer_question``)."""
        if not isinstance(question, str) or not question.strip():
            raise OrderError("question is required")
        if self.factory is None:
            raise RuntimeError("Foundry is not running")
        async for chunk in self.factory.foreman.answer_question(question.strip()[:2000]):
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
    loop = asyncio.get_running_loop()
    for sig in (getattr(signal, "SIGINT", None), getattr(signal, "SIGTERM", None)):
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, RuntimeError, ValueError):
            pass  # Windows: KeyboardInterrupt reaches asyncio.run instead
    await service.start()
    api = None
    try:
        api = await _maybe_await(serve_api.start_api(service, host, port if port is not None else serve_port()))
        bound = getattr(api, "port", port)
        out(f"Foundry is running for {service.repo} on http://{host}:{bound} - press Ctrl+C to stop")
        await stop_event.wait()
    finally:
        out("Foundry is stopping (finishing in-flight work)...")
        if api is not None:
            closer = getattr(api, "close", None) or getattr(api, "shutdown", None)
            if callable(closer):
                await _maybe_await(closer())
        await service.stop()
        out("Foundry stopped.")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point; returns the process exit code."""
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    from foundry.config import FoundryConfig
    from foundry.main import claude_args

    config = FoundryConfig.from_env(**({"board_bridge_mode": args.board} if args.board else {}))
    repo = Path(args.repo).resolve()
    if not repo.is_dir():
        print(f"--repo {repo} is not a folder", file=sys.stderr)
        return 2
    from foundry.claude_client import ClaudeCodeClient, ClaudeError, resolve_claude_bin
    from foundry.gemma_client import OllamaGemmaClient

    claude_ok = True
    try:
        claude = ClaudeCodeClient(resolve_claude_bin(config.claude_bin), extra_args=claude_args(config))
        validate = getattr(claude, "validate_launch", None)
        if callable(validate):
            validate()
    except ClaudeError as exc:
        print(f"Claude Code is not ready: {exc}", file=sys.stderr)
        return 2
    gemma = OllamaGemmaClient(config.gemma_base_url, config.gemma_model)
    service = Service(config, repo=repo, claude=claude, gemma=gemma, monitor_s=args.monitor, claude_ok=claude_ok)
    try:
        return asyncio.run(run_service(service, host=DEFAULT_HOST, port=args.port))
    except KeyboardInterrupt:  # Windows Ctrl+C; run_service's finally already shut down
        return 0


if __name__ == "__main__":
    sys.exit(main())
