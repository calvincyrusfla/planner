"""Tests for the long-running service core (foundry/serve.py; owner alpha-1).

Simulated Claude/Gemma, dry board bridge, temp database; the monitor loop runs
with a gated fake sleep so no test waits for real intervals.
"""

from __future__ import annotations

import asyncio
import json
import threading
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from foundry.board_bridge import BoardBridge
from foundry.config import FoundryConfig
from foundry.floor_manager import WorkOrder, build_factory
from foundry.main import demo_claude, demo_gemma, write_demo_repo
from foundry.serve import OrderError, Service, build_parser, enable_ctrl_c, monitor_interval, run_service, serve_port

ORDER = {"title": "Add clamp helper", "description": "Add clamp(x, lo, hi).", "files": ["shop/mathx.py"]}


def _config(tmp_path: Path) -> FoundryConfig:
    return FoundryConfig(
        db_path=tmp_path / "foundry.db", rpm=1000, tpm=10_000_000, max_concurrent=5,
        teams=["alpha", "bravo"], board_bridge_mode="dry",
    )


def _service(tmp_path: Path, repo: Path | None = None, **kw: Any) -> Service:
    repo = repo or write_demo_repo(tmp_path / "repo")
    kw.setdefault("gemma", demo_gemma())
    return Service(
        _config(tmp_path), repo=repo, claude=demo_claude(repo, fail_first=None),
        factory_kwargs={"bridge": BoardBridge("dry"), "patches_dir": tmp_path / "patches"},
        **kw,
    )


async def _until(pred: Any, timeout: float = 15.0) -> None:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not pred():
        assert loop.time() < end, "condition not reached in time"
        await asyncio.sleep(0.01)


def _state(svc: Service, tid: str) -> str | None:
    return next((t["state"] for t in svc.status()["tasks"] if t["id"] == tid), None)


class GatedSleep:
    """Async sleep that blocks until the test releases it; records the delays."""

    def __init__(self) -> None:
        self.delays: list[float] = []
        self.sem = asyncio.Semaphore(0)

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)
        await self.sem.acquire()


# --------------------------------------------------------------------------- lifecycle


def test_submit_runs_to_complete_and_status_lists_outputs(tmp_path: Path) -> None:
    async def go() -> None:
        svc = _service(tmp_path, monitor_s=3600)
        await svc.start()
        try:
            assert svc.health()["ok"] is True
            tid = await svc.submit(**ORDER)  # serve_api calls it with keywords
            await _until(lambda: _state(svc, tid) == "complete" and tid in svc.factory.manager.pr_summaries)
            await svc.factory.manager.drain()
            row = next(t for t in svc.status()["tasks"] if t["id"] == tid)
            assert row["title"] == ORDER["title"] and row["builders"] == 1 and row["team"] in ("alpha", "bravo")
            assert row["patch"] and Path(row["patch"]).is_file()
            assert row["md"] and row["rejected"] is None and row["reason"] is None
            bridge = svc.factory.bridge
        finally:
            await svc.stop()
        assert svc.factory is None and svc.health()["ok"] is False
        assert svc.status()["ok"] is False
        assert any(op["type"] == "addTask" for op in bridge.ops)

    asyncio.run(go())


def test_submit_accepts_a_body_dict_and_passes_size_as_hint(tmp_path: Path) -> None:
    async def go() -> None:
        svc = _service(tmp_path, monitor_s=3600)
        await svc.start()
        try:
            tid = await svc.submit({**ORDER, "files": "shop/mathx.py, ", "size": "m"})
            order = svc.factory.manager.orders[tid]
            assert order.files == ["shop/mathx.py"]
            assert "Requested size: M" in order.description
            assert Path(order.repo_root) == svc.repo
        finally:
            await svc.stop()

    asyncio.run(go())


@pytest.mark.parametrize("body, msg", [
    ({}, "title"),
    ({"title": "  "}, "title"),
    ({"title": "x" * 201}, "title"),
    ({"title": "t", "description": 3}, "description"),
    ({"title": "t", "files": ["/etc/passwd"]}, "repo-relative"),
    ({"title": "t", "files": ["C:\\x.py"]}, "repo-relative"),
    ({"title": "t", "files": ["a/../../b.py"]}, "repo-relative"),
    ({"title": "t", "files": [1]}, "files"),
    ({"title": "t", "size": "huge"}, "size"),
    ([1, 2], "object"),
])
def test_validate_order_rejects_bad_bodies(body: Any, msg: str) -> None:
    with pytest.raises(OrderError, match=msg):
        Service.validate_order(body)


def test_submit_when_not_running_raises(tmp_path: Path) -> None:
    svc = _service(tmp_path)
    with pytest.raises(RuntimeError):
        asyncio.run(svc.submit(**ORDER))


def test_stop_flushes_and_closes_the_bridge(tmp_path: Path) -> None:
    async def go() -> None:
        svc = _service(tmp_path, monitor_s=3600)
        await svc.start()
        closed: list[bool] = []
        orig = svc.factory.bridge.close
        svc.factory.bridge.close = lambda *a, **k: (closed.append(True), orig(*a, **k))[1]  # type: ignore[method-assign]
        pools = list(svc.factory.pools.values())
        await svc.stop()
        assert closed == [True]
        assert not any(p.running for p in pools)
        await svc.stop()  # idempotent

    asyncio.run(go())


# --------------------------------------------------------------------------- monitor


def test_monitor_loop_evaluates_and_applies_escalations(tmp_path: Path) -> None:
    async def go() -> None:
        sleep = GatedSleep()
        svc = _service(tmp_path, monitor_s=60, sleep=sleep)
        await svc.start()
        try:
            calls: list[Any] = []
            fake_alert = object()

            async def evaluate() -> Any:
                calls.append("eval")
                return fake_alert if len(calls) == 2 else None

            class Outcome:
                def __init__(self) -> None:
                    self.alert_id, self.action, self.applied, self.detail = "ALT-1", "THROTTLE_DISPATCH", True, "slowed"

            async def apply(alert: Any) -> Any:
                assert alert is fake_alert
                calls.append("apply")
                return Outcome()

            svc.factory.foreman.evaluate_line_anomaly = evaluate  # type: ignore[method-assign]
            svc.factory.manager.apply_escalation = apply  # type: ignore[method-assign]
            await _until(lambda: sleep.delays == [60])
            assert calls == []  # nothing before the first interval elapsed
            sleep.sem.release()
            await _until(lambda: len(sleep.delays) == 2)
            assert calls == ["eval"]
            sleep.sem.release()
            await _until(lambda: len(sleep.delays) == 3)
            assert calls == ["eval", "eval", "apply"]
            foreman = svc.status()["foreman"]
            assert foreman["monitor_runs"] == 2 and foreman["last_action"]["action"] == "THROTTLE_DISPATCH"
            assert foreman["monitor_s"] == 60
        finally:
            await svc.stop()
        assert svc._monitor_task is None

    asyncio.run(go())


def test_monitor_errors_do_not_stop_the_service(tmp_path: Path) -> None:
    async def go() -> None:
        sleep = GatedSleep()
        svc = _service(tmp_path, monitor_s=5, sleep=sleep)
        await svc.start()
        try:
            async def boom() -> Any:
                raise RuntimeError("gemma fell over")

            svc.factory.foreman.evaluate_line_anomaly = boom  # type: ignore[method-assign]
            sleep.sem.release()
            await _until(lambda: len(sleep.delays) == 2)
            assert "gemma fell over" in svc.status()["foreman"]["last_error"]
            assert svc._monitor_task is not None and not svc._monitor_task.done()
        finally:
            await svc.stop()

    asyncio.run(go())


def test_monitor_once_with_the_real_foreman_on_a_healthy_line(tmp_path: Path) -> None:
    async def go() -> None:
        svc = _service(tmp_path, monitor_s=3600)
        await svc.start()
        try:
            assert await svc.monitor_once() is None
            assert svc.monitor_runs == 1 and svc.last_monitor_error is None
        finally:
            await svc.stop()

    asyncio.run(go())


# --------------------------------------------------------------------------- resume


def test_start_resumes_unfinished_tasks_and_ignores_other_repos(tmp_path: Path) -> None:
    repo = write_demo_repo(tmp_path / "repo")
    other = write_demo_repo(tmp_path / "other")
    config = _config(tmp_path)

    async def seed() -> tuple[str, str]:
        f = build_factory(config, claude=demo_claude(repo), gemma=None, repo_root=repo,
                          bridge=BoardBridge("dry"), patches_dir=tmp_path / "patches")
        mine = await f.manager.submit(WorkOrder(**ORDER, repo_root=str(repo)))
        theirs = await f.manager.submit(WorkOrder(**ORDER, repo_root=str(other)))
        f.close()  # never ran: both tasks are still open
        return mine, theirs

    mine, theirs = asyncio.run(seed())

    async def go() -> None:
        svc = _service(tmp_path, repo=repo, monitor_s=3600)
        await svc.start()
        try:
            assert svc.resumed == [mine]
            await _until(lambda: _state(svc, mine) == "complete")
            ids = [t["id"] for t in svc.status()["tasks"]]
            assert ids == [mine]  # the other repo's task is not listed ...
            state = svc.factory.queue.get_task(theirs)["state"]
            assert str(getattr(state, "value", state)) not in ("complete", "in_review")  # ... nor dispatched
        finally:
            await svc.stop()

    asyncio.run(go())


def test_start_rewrites_a_missing_patch_of_an_approved_task(tmp_path: Path) -> None:
    repo = write_demo_repo(tmp_path / "repo")

    async def first() -> tuple[str, Path]:
        svc = _service(tmp_path, repo=repo, monitor_s=3600)
        await svc.start()
        tid = await svc.submit(**ORDER)
        await _until(lambda: tid in svc.factory.manager.pr_summaries)
        await svc.factory.manager.drain()
        patch = Path(next(t for t in svc.status()["tasks"] if t["id"] == tid)["patch"])
        await svc.stop()
        return tid, patch

    tid, patch = asyncio.run(first())
    patch.unlink()

    async def second() -> None:
        svc = _service(tmp_path, repo=repo, monitor_s=3600)
        await svc.start()
        try:
            assert svc.resumed == [tid]
            await _until(lambda: patch.is_file())
        finally:
            await svc.stop()

    asyncio.run(second())


# --------------------------------------------------------------------------- ask, health, threads


def test_ask_streams_chunks_and_threadsafe_bridges_work(tmp_path: Path) -> None:
    async def go() -> None:
        svc = _service(tmp_path, monitor_s=3600, gemma=None)
        await svc.start()
        try:
            chunks = [c async for c in svc.ask("What is on the line?")]
            assert chunks and all(isinstance(c, str) for c in chunks)
            with pytest.raises(OrderError):
                [c async for c in svc.ask("   ")]

            out: dict[str, Any] = {}

            def worker() -> None:
                out["health"] = svc.call_threadsafe(svc.health)
                out["tid"] = svc.call_threadsafe(svc.submit, ORDER)
                out["chunks"] = list(svc.ask_threadsafe("How busy is the line?"))

            t = threading.Thread(target=worker)
            t.start()
            await asyncio.to_thread(t.join, 20)
            assert out["health"]["repo"] == str(svc.repo)
            assert out["tid"].startswith("F-")
            assert "".join(out["chunks"]).strip()
        finally:
            await svc.stop()

    asyncio.run(go())


def test_health_reports_clients(tmp_path: Path) -> None:
    async def go() -> None:
        svc = _service(tmp_path, monitor_s=3600, gemma=None)
        await svc.start()
        try:
            h = svc.health()
            assert set(h) == {"ok", "repo", "model", "gemma_ok", "claude_ok", "version", "gemma_speed"}
            assert h["gemma_ok"] is False and h["claude_ok"] is True
        finally:
            await svc.stop()
        probed = _service(tmp_path / "p", monitor_s=3600, probe=lambda: True)
        await probed.start()
        try:
            assert probed.health()["gemma_ok"] is True
        finally:
            await probed.stop()

    asyncio.run(go())


def test_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOUNDRY_SERVE_PORT", "4999")
    monkeypatch.setenv("FOUNDRY_MONITOR_S", "2.5")
    assert serve_port() == 4999 and monitor_interval() == 2.5
    monkeypatch.setenv("FOUNDRY_MONITOR_S", "nope")
    monkeypatch.delenv("FOUNDRY_SERVE_PORT")
    assert serve_port() == 4310 and monitor_interval() == 60.0
    assert build_parser().parse_args(["--repo", "x"]).repo == "x"


def test_run_service_serves_http_and_shuts_down(tmp_path: Path) -> None:
    async def go() -> None:
        svc = _service(tmp_path, monitor_s=3600)
        stop = asyncio.Event()
        lines: list[str] = []
        runner = asyncio.create_task(run_service(svc, port=0, stop_event=stop, out=lines.append))
        await _until(lambda: svc.api is not None and svc.running)

        def get() -> dict[str, Any]:
            with urllib.request.urlopen(f"http://127.0.0.1:{svc.api.port}/api/health", timeout=5) as r:
                return json.loads(r.read())

        health = await asyncio.to_thread(get)
        assert health["ok"] is True and health["repo"] == str(svc.repo)
        stop.set()
        assert await asyncio.wait_for(runner, 20) == 0
        assert svc.factory is None and lines[-1] == "Foundry stopped."

    asyncio.run(go())


# --------------------------------------------------------------------------- review round 1


def test_validate_order_rejects_unknown_fields() -> None:
    with pytest.raises(OrderError, match="unknown field"):
        Service.validate_order({"title": "t", "repo_root": "/elsewhere"})


def test_ask_uses_the_api_question_limit(tmp_path: Path) -> None:
    from foundry.serve_api import MAX_QUESTION

    async def go() -> None:
        svc = _service(tmp_path, monitor_s=3600)
        await svc.start()
        seen: list[str] = []

        async def answer(text: str) -> Any:
            seen.append(text)
            yield "ok"

        svc.factory.foreman.answer_question = answer  # type: ignore[method-assign]
        try:
            assert [c async for c in svc.ask("q" * (MAX_QUESTION + 50))] == ["ok"]
            assert len(seen[0]) == MAX_QUESTION
        finally:
            await svc.stop()

    asyncio.run(go())


def test_port_in_use_is_reported_and_the_service_is_closed(tmp_path: Path) -> None:
    import socket

    from foundry.serve import PortInUse

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    try:
        async def go() -> None:
            svc = _service(tmp_path, monitor_s=3600)
            with pytest.raises(PortInUse):
                await run_service(svc, port=sock.getsockname()[1], stop_event=asyncio.Event(), out=lambda _l: None)
            assert svc.factory is None and svc.closed_factory is not None

        asyncio.run(go())
    finally:
        sock.close()


def test_other_oserrors_are_not_labelled_port_in_use(tmp_path: Path) -> None:
    from foundry.serve import PortInUse

    async def go() -> None:
        svc = _service(tmp_path, monitor_s=3600)

        async def broken_start() -> Any:
            svc.factory = svc._build()
            for pool in svc.factory.pools.values():
                pool.start()
            raise OSError("disk full")

        svc.start = broken_start  # type: ignore[method-assign]
        with pytest.raises(OSError) as info:
            await run_service(svc, port=0, stop_event=asyncio.Event(), out=lambda _l: None)
        assert not isinstance(info.value, PortInUse)
        # start failed half-way: pools were stopped and the factory closed anyway
        assert svc.factory is None
        assert not any(p.running for p in svc.closed_factory.pools.values())

    asyncio.run(go())


def test_enable_ctrl_c_calls_kernel32_only_on_windows() -> None:
    calls: list[tuple[Any, Any]] = []

    class FakeKernel32:
        def SetConsoleCtrlHandler(self, handler: Any, add: Any) -> int:  # noqa: N802
            calls.append((handler, add))
            return 1

    assert enable_ctrl_c("linux", FakeKernel32()) is False
    assert calls == []
    assert enable_ctrl_c("win32", FakeKernel32()) is True
    assert calls == [(None, False)]


def test_enable_ctrl_c_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    class Broken:
        def SetConsoleCtrlHandler(self, handler: Any, add: Any) -> int:  # noqa: N802
            raise OSError("no console")

    assert enable_ctrl_c("win32", Broken()) is False
    monkeypatch.setattr("sys.platform", "win32")
    assert enable_ctrl_c() is False  # no ctypes.windll off Windows: swallowed
