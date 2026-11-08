"""Orders return at once; the Shop Foreman sizes them in the background (T-015).

Before the fix, POST /api/orders awaited the Foreman's sizing (a CPU Gemma can
take > 30 s), the API timeout cancelled it half-way and left a task recorded
but never sized. Simulated Claude/Gemma, dry board bridge, temp database.
"""

from __future__ import annotations

import asyncio
import json
import time
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from foundry import serve_api
from foundry.board_bridge import BoardBridge
from foundry.floor_manager import WorkOrder, build_factory
from foundry.main import demo_claude, write_demo_repo
from foundry.serve import Service, run_service
from foundry.tests.test_serve import ORDER, _config, _service, _state, _until


def _slow_sizing(foreman: Any, delay: float | None = None, gate: asyncio.Event | None = None) -> list[str]:
    """Make ``foreman.size_task_scope`` slow (sleep ``delay``, or wait for ``gate``); returns the calls."""
    real = foreman.size_task_scope
    calls: list[str] = []

    async def slow(task: dict[str, Any]) -> Any:
        calls.append(task["task_id"])
        if gate is not None:
            await gate.wait()
        if delay is not None:
            await asyncio.sleep(delay)
        value = real(task)
        return await value if asyncio.iscoroutine(value) else value

    foreman.size_task_scope = slow
    return calls


def test_post_orders_returns_fast_while_sizing_outlasts_the_api_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(serve_api, "CALL_TIMEOUT_S", 0.5)

    async def go() -> None:
        svc = _service(tmp_path, monitor_s=3600)
        stop = asyncio.Event()
        runner = asyncio.create_task(run_service(svc, port=0, stop_event=stop, out=lambda *_: None))
        await _until(lambda: svc.api is not None and svc.running)
        calls = _slow_sizing(svc.factory.foreman, delay=1.5)  # 3x the API timeout
        base = f"http://127.0.0.1:{svc.api.port}"

        def post() -> tuple[float, dict[str, Any]]:
            req = urllib.request.Request(
                f"{base}/api/orders", data=json.dumps(ORDER).encode(), method="POST",
                headers={"Content-Type": "application/json"},
            )
            t0 = time.monotonic()
            with urllib.request.urlopen(req, timeout=10) as r:
                return time.monotonic() - t0, json.loads(r.read())

        def status() -> dict[str, Any]:
            with urllib.request.urlopen(f"{base}/api/status", timeout=5) as r:
                return json.loads(r.read())

        try:
            took, body = await asyncio.to_thread(post)
            assert body["ok"] is True and body["task_id"].startswith("F-")
            assert took < 0.5, took
            tid = body["task_id"]
            row = next(t for t in (await asyncio.to_thread(status))["tasks"] if t["id"] == tid)
            assert row["state"] == "sizing" and row["builders"] is None
            await _until(lambda: _state(svc, tid) == "complete" and tid in svc.factory.manager.pr_summaries)
            assert calls == [tid]  # sized once, never cancelled
            ops = [op for op in svc.factory.bridge.ops if op["type"] == "addTask"]
            assert len(ops) == 1
        finally:
            stop.set()
            assert await asyncio.wait_for(runner, 20) == 0

    asyncio.run(go())


def test_manager_submit_still_awaits_sizing_and_background_flag_returns_at_once(tmp_path: Path) -> None:
    repo = write_demo_repo(tmp_path / "repo")

    async def go() -> None:
        f = build_factory(_config(tmp_path), claude=demo_claude(repo), gemma=None, repo_root=repo,
                          bridge=BoardBridge("dry"), patches_dir=tmp_path / "patches")
        try:
            tid = await f.manager.submit(WorkOrder(**ORDER))
            assert tid in f.manager.directives and f.queue.tickets_for_task(tid)
            gate = asyncio.Event()
            _slow_sizing(f.foreman, gate=gate)
            tid2 = await f.manager.submit(WorkOrder(**ORDER), background=True)
            assert f.manager.is_sizing(tid2) and not f.queue.tickets_for_task(tid2)
            gate.set()
            await _until(lambda: not f.manager.is_sizing(tid2))
            assert tid2 in f.manager.directives and f.queue.tickets_for_task(tid2)
        finally:
            f.close()

    asyncio.run(go())


def test_half_registered_orders_are_sized_again_on_start(tmp_path: Path) -> None:
    """Reproduces the user's stuck tasks: sizing cancelled by a timeout (old behaviour)."""
    repo = write_demo_repo(tmp_path / "repo")
    config = _config(tmp_path)

    async def seed() -> tuple[str, str]:
        f = build_factory(config, claude=demo_claude(repo), gemma=None, repo_root=repo,
                          bridge=BoardBridge("dry"), patches_dir=tmp_path / "patches")
        _slow_sizing(f.foreman, gate=asyncio.Event())  # never answers
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(f.manager.submit(WorkOrder(**ORDER, repo_root=str(repo))), 0.1)
        cut = max(f.manager.orders)
        assert not f.queue.tickets_for_task(cut) and cut not in f.manager.directives
        # a task whose order was never saved at all (only the queue row exists)
        f.queue.add_task("F-050", "Orphan order", "No saved order.", "alpha", repo_root=str(repo))
        f.close()
        return cut, "F-050"

    cut, orphan = asyncio.run(seed())

    async def go() -> None:
        svc = _service(tmp_path, repo=repo, monitor_s=3600)
        await svc.start()
        try:
            assert cut in svc.resumed and orphan in svc.resumed
            assert {_state(svc, cut), _state(svc, orphan)} <= {"sizing", "building", "validating",
                                                               "reviewing", "complete"}
            await _until(lambda: _state(svc, cut) == "complete")
            # the orphan has no files, so the demo Claude cannot build it; it must be sized and registered
            await _until(lambda: bool(svc.factory.queue.tickets_for_task(orphan)))
            assert orphan in svc.factory.manager.directives
            assert svc.factory.manager.orders[orphan].title == "Orphan order"
            assert len([op for op in svc.factory.bridge.ops if op["type"] == "addTask"]) == 2
        finally:
            await svc.stop()

    asyncio.run(go())


def test_stop_during_sizing_does_not_hang_and_the_order_is_sized_next_start(tmp_path: Path) -> None:
    repo = write_demo_repo(tmp_path / "repo")

    async def first() -> str:
        svc = _service(tmp_path, repo=repo, monitor_s=3600)
        await svc.start()
        svc.sizing_stop_s = 0.2
        _slow_sizing(svc.factory.foreman, gate=asyncio.Event())  # hangs until cancelled
        tid = await svc.submit(**ORDER)
        assert _state(svc, tid) == "sizing"
        t0 = time.monotonic()
        await asyncio.wait_for(svc.stop(), 15)
        assert time.monotonic() - t0 < 10
        assert svc.factory is None
        return tid

    tid = asyncio.run(first())

    async def second() -> None:
        svc = _service(tmp_path, repo=repo, monitor_s=3600)
        await svc.start()
        try:
            assert tid in svc.resumed
            await _until(lambda: _state(svc, tid) == "complete")
        finally:
            await svc.stop()

    asyncio.run(second())


def test_sizing_failure_is_recorded_and_does_not_crash_the_service(tmp_path: Path) -> None:
    async def go() -> None:
        svc = _service(tmp_path, monitor_s=3600)
        await svc.start()
        try:
            def broken(task: dict[str, Any]) -> Any:
                raise RuntimeError("foreman exploded")

            svc.factory.foreman.size_task_scope = broken
            tid = await svc.submit(**ORDER)
            await _until(lambda: _state(svc, tid) == "quarantined")
            row = next(t for t in svc.status()["tasks"] if t["id"] == tid)
            assert "sizing failed" in row["reason"] and "foreman exploded" in row["reason"]
            assert "foreman exploded" in svc.factory.manager.sizing_errors[tid]
            assert svc.running and svc.health()["ok"] is True
        finally:
            await svc.stop()

    asyncio.run(go())


def test_service_without_accept_falls_back_to_awaiting_submit(tmp_path: Path) -> None:
    """Older managers (no ``accept``) keep working through ``await submit``."""
    svc = Service(_config(tmp_path), repo=tmp_path)

    class Manager:
        async def submit(self, order: Any) -> str:
            return "F-777"

    class F:
        manager = Manager()

    svc.factory, svc.running = F(), True
    assert asyncio.run(svc.submit(**ORDER)) == "F-777"
