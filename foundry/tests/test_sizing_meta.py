"""Sizing source/time per task and Gemma speed in status/health (T-017, owner bravo-1).

Simulated Claude/Gemma, dry board bridge, temp database; nothing real is called.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from foundry.board_bridge import BoardBridge
from foundry.config import FoundryConfig
from foundry.gemma_client import GemmaRequest, SimulatedGemmaClient
from foundry.main import demo_claude, demo_gemma, write_demo_repo
from foundry.serve import Service

ORDER = {"title": "Add clamp helper", "description": "Add clamp(x, lo, hi).", "files": ["shop/mathx.py"]}


def _config(tmp_path: Path) -> FoundryConfig:
    return FoundryConfig(
        db_path=tmp_path / "foundry.db", rpm=1000, tpm=10_000_000, max_concurrent=5,
        teams=["alpha", "bravo"], board_bridge_mode="dry",
    )


def _sizing_gemma(delay: float = 0.0) -> SimulatedGemmaClient:
    """demo_gemma, but it answers the sizing prompt with a valid one-builder plan."""
    base = demo_gemma().responder

    async def responder(req: GemmaRequest) -> Any:
        system = str(req.messages[0].get("content", "")) if req.messages else ""
        if "Size the work order" in system:
            await asyncio.sleep(delay)
            return {
                "complexity": "S",
                "rationale": "One small helper.",
                "subtasks": [{"title": "Add clamp", "acceptance_criteria": ["clamp works"],
                              "allowed_files": ["shop/mathx.py"]}],
            }
        return base(req)

    return SimulatedGemmaClient(responder)


def _service(tmp_path: Path, gemma: Any, repo: Path | None = None) -> Service:
    repo = repo or write_demo_repo(tmp_path / "repo")
    return Service(
        _config(tmp_path), repo=repo, claude=demo_claude(repo, fail_first=None), gemma=gemma,
        monitor_s=3600, probe=lambda: True,
        factory_kwargs={"bridge": BoardBridge("dry"), "patches_dir": tmp_path / "patches"},
    )


async def _until(pred: Any, timeout: float = 15.0) -> None:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not pred():
        assert loop.time() < end, "condition not reached in time"
        await asyncio.sleep(0.01)


def _row(svc: Service, tid: str) -> dict[str, Any] | None:
    return next((t for t in svc.status()["tasks"] if t["id"] == tid), None)


def _sized(svc: Service, tid: str) -> bool:
    row = _row(svc, tid)
    return bool(row and row["sizing_source"] is not None)


def test_gemma_sizing_is_recorded_and_survives_restart(tmp_path: Path) -> None:
    repo = write_demo_repo(tmp_path / "repo")

    async def first() -> str:
        svc = _service(tmp_path, _sizing_gemma(delay=0.05), repo)
        await svc.start()
        try:
            tid = await svc.submit(**ORDER)
            await _until(lambda: _sized(svc, tid))
            row = _row(svc, tid)
            assert row["sizing_source"] == "gemma"
            assert isinstance(row["sizing_ms"], int) and row["sizing_ms"] >= 40
            json.dumps(svc.status())  # the whole status stays JSON-safe
            await svc.factory.manager.drain()
        finally:
            await svc.stop()
        return tid

    tid = asyncio.run(first())

    async def second() -> None:
        svc = _service(tmp_path, demo_gemma(), repo)
        await svc.start()
        try:
            row = _row(svc, tid)
            assert row is not None
            assert row["sizing_source"] == "gemma"  # reloaded from the saved state
            assert row["sizing_ms"] >= 40
        finally:
            await svc.stop()

    asyncio.run(second())


def test_fallback_sizing_is_marked_heuristic(tmp_path: Path) -> None:
    async def go() -> None:
        svc = _service(tmp_path, demo_gemma())  # demo_gemma answers sizing with "{}" -> heuristic
        await svc.start()
        try:
            tid = await svc.submit(**ORDER)
            await _until(lambda: _sized(svc, tid))
            row = _row(svc, tid)
            assert row["sizing_source"] == "heuristic"
            assert isinstance(row["sizing_ms"], int) and row["sizing_ms"] >= 0
            await svc.factory.manager.drain()
        finally:
            await svc.stop()

    asyncio.run(go())


def test_unsized_task_row_has_null_sizing_fields(tmp_path: Path) -> None:
    async def go() -> None:
        hold = asyncio.Event()

        async def responder(req: GemmaRequest) -> Any:
            await hold.wait()
            return "{}"

        svc = _service(tmp_path, SimulatedGemmaClient(responder))
        await svc.start()
        try:
            tid = await svc.submit(**ORDER)
            row = _row(svc, tid)
            assert row["sizing_source"] is None and row["sizing_ms"] is None
            hold.set()
            await _until(lambda: _sized(svc, tid))
            await svc.factory.manager.drain()
        finally:
            hold.set()
            await svc.stop()

    asyncio.run(go())


def test_health_and_status_report_gemma_stats(tmp_path: Path) -> None:
    async def go() -> None:
        svc = _service(tmp_path, demo_gemma())
        await svc.start()
        try:
            foreman = svc.factory.foreman
            stats = {"calls": 4, "fallbacks": 2, "avg_ms": 38123.456, "last_ms": 41000,
                     "by_hook": {"size_task_scope": {"calls": 1, "avg_ms": 41000, "last_ms": 41000}}}
            foreman.gemma_stats = lambda: stats  # alpha-1's shape, pinned here
            speed = svc.health()["gemma_speed"]
            assert speed == {"avg_ms": 38123.5, "last_ms": 41000.0, "fallbacks": 2, "calls": 4}
            assert svc.status()["foreman"]["gemma"] == stats
            json.dumps(svc.health())
            json.dumps(svc.status())

            def boom() -> dict[str, Any]:
                raise RuntimeError("stats broke")

            foreman.gemma_stats = boom  # a broken stats call never breaks health/status
            assert svc.health()["gemma_speed"] == {"avg_ms": None, "last_ms": None, "fallbacks": 0, "calls": 0}
            assert svc.status()["foreman"]["gemma"] == {}
        finally:
            await svc.stop()

    asyncio.run(go())


def test_health_without_factory_has_empty_gemma_speed(tmp_path: Path) -> None:
    svc = _service(tmp_path, demo_gemma())
    h = svc.health()
    assert h["gemma_speed"] == {"avg_ms": None, "last_ms": None, "fallbacks": 0, "calls": 0}


def test_real_foreman_stats_flow_into_health(tmp_path: Path) -> None:
    async def go() -> None:
        svc = _service(tmp_path, _sizing_gemma())
        await svc.start()
        try:
            tid = await svc.submit(**ORDER)
            await _until(lambda: _sized(svc, tid))
            speed = svc.health()["gemma_speed"]
            assert speed["calls"] >= 1 and speed["last_ms"] is not None and speed["avg_ms"] is not None
            assert svc.status()["foreman"]["gemma"]["calls"] == speed["calls"]
            await svc.factory.manager.drain()
        finally:
            await svc.stop()

    asyncio.run(go())
