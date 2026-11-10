"""Reviewers can have their own Claude client; without one they keep the main one."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from foundry import serve
from foundry.builder_worker import TeamPool
from foundry.config import FoundryConfig
from foundry.floor_manager import Factory, build_factory
from foundry.main import claude_args, reviewer_claude_args
from foundry.schemas import ClaudeResult


class FakeClaude:
    """A client that must never be called: these tests only check the wiring."""

    async def run(
        self, prompt: str, *, agent_id: str, role: Any, cwd: str | None = None, timeout: float = 600
    ) -> ClaudeResult:
        raise AssertionError("no Claude call expected")


class FakeCodeClient:
    """Stands in for ``ClaudeCodeClient``; remembers its flags."""

    def __init__(self, path: str, *, extra_args: list[str] | None = None) -> None:
        self.path = path
        self.extra_args = extra_args


def _pool(
    client: FakeClaude, builder_client: FakeClaude | None = None, reviewer_client: FakeClaude | None = None
) -> TeamPool:
    return TeamPool(
        "alpha",
        2,
        queue=object(),
        client=client,
        builder_client=builder_client,
        reviewer_client=reviewer_client,
        limiter=None,
        telemetry=None,
    )


async def _hold(stop: asyncio.Event) -> None:
    await stop.wait()


async def _promoted_peer_client(pool: TeamPool) -> Any:
    """The client of a peer reviewer promoted on ``pool``, marked as running."""
    stop = asyncio.Event()
    pool._stop = stop
    pool._tasks.append(asyncio.create_task(_hold(stop)))
    agent_id = pool.promote_peer_reviewer(slot=1)
    assert agent_id is not None
    try:
        return pool.peer_reviewers[agent_id].client
    finally:
        await pool.stop()


def test_builders_use_builder_client_and_reviewer_uses_reviewer_client() -> None:
    client, builder_client, reviewer_client = FakeClaude(), FakeClaude(), FakeClaude()
    pool = _pool(client, builder_client, reviewer_client)
    assert pool.client is client
    assert pool.reviewer_client is reviewer_client
    assert len(pool.builders) == 2
    assert all(builder.client is builder_client for builder in pool.builders)
    assert pool.reviewer.client is reviewer_client


def test_promoted_peer_reviewer_uses_reviewer_client() -> None:
    client, builder_client, reviewer_client = FakeClaude(), FakeClaude(), FakeClaude()
    pool = _pool(client, builder_client, reviewer_client)
    assert asyncio.run(_promoted_peer_client(pool)) is reviewer_client


def test_without_reviewer_client_reviewers_use_client() -> None:
    client, builder_client = FakeClaude(), FakeClaude()
    pool = _pool(client, builder_client)
    assert pool.reviewer_client is client
    assert pool.reviewer.client is client
    assert all(builder.client is builder_client for builder in pool.builders)


def test_promoted_peer_reviewer_without_reviewer_client_uses_client() -> None:
    client, builder_client = FakeClaude(), FakeClaude()
    assert asyncio.run(_promoted_peer_client(_pool(client, builder_client))) is client


def _config(tmp_path: Path) -> FoundryConfig:
    return FoundryConfig(
        db_path=tmp_path / "foundry.db",
        board_bridge_mode="dry",
        teams=["alpha", "bravo"],
        builders_per_team=2,
        auto_pr=False,
    )


def _factory(tmp_path: Path, claude: FakeClaude, **kwargs: Any) -> Factory:
    return build_factory(_config(tmp_path), claude=claude, gemma=object(), repo_root=tmp_path, **kwargs)


def test_build_factory_passes_reviewer_claude_to_every_pool(tmp_path: Path) -> None:
    claude, reviewer_claude = FakeClaude(), FakeClaude()
    factory = _factory(tmp_path, claude, reviewer_claude=reviewer_claude)
    try:
        assert factory.claude is claude
        assert set(factory.pools) == {"alpha", "bravo"}
        for pool in factory.pools.values():
            assert pool.reviewer.client is reviewer_claude
            assert all(builder.client is claude for builder in pool.builders)
    finally:
        factory.close()


def test_build_factory_without_reviewer_claude_uses_claude(tmp_path: Path) -> None:
    claude = FakeClaude()
    factory = _factory(tmp_path, claude)
    try:
        for pool in factory.pools.values():
            assert pool.reviewer.client is claude
    finally:
        factory.close()


def test_service_passes_reviewer_claude_to_the_factory(tmp_path: Path) -> None:
    claude, builder_claude, reviewer_claude = FakeClaude(), FakeClaude(), FakeClaude()
    service = serve.Service(
        _config(tmp_path),
        repo=tmp_path,
        claude=claude,
        builder_claude=builder_claude,
        reviewer_claude=reviewer_claude,
        gemma=object(),
    )
    assert service.reviewer_claude is reviewer_claude
    factory = service._build()
    try:
        for pool in factory.pools.values():
            assert pool.reviewer.client is reviewer_claude
            assert all(builder.client is builder_claude for builder in pool.builders)
    finally:
        factory.close()


def _serve_main(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    """Run ``serve.main`` with fake clients and no real service; returns the Service it built."""
    services: list[Any] = []

    async def fake_run_service(service: Any, **_kwargs: Any) -> int:
        services.append(service)
        return 0

    def fake_resolve(claude_bin: str) -> str:
        return claude_bin

    def fake_gemma(*_args: Any, **_kwargs: Any) -> object:
        return object()

    def no_ctrl_c() -> bool:
        return False

    monkeypatch.setattr("foundry.claude_client.ClaudeCodeClient", FakeCodeClient)
    monkeypatch.setattr("foundry.claude_client.resolve_claude_bin", fake_resolve)
    monkeypatch.setattr("foundry.gemma_client.OllamaGemmaClient", fake_gemma)
    monkeypatch.setattr(serve, "run_service", fake_run_service)
    monkeypatch.setattr(serve, "enable_ctrl_c", no_ctrl_c)
    assert serve.main(["--repo", str(tmp_path)]) == 0
    assert len(services) == 1
    return services[0]


def test_serve_main_creates_reviewer_client_when_read(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FOUNDRY_REVIEWER_TOOLS", "read")
    config = FoundryConfig.from_env()
    service = _serve_main(monkeypatch, tmp_path)
    assert isinstance(service.reviewer_claude, FakeCodeClient)
    assert service.reviewer_claude is not service.claude
    assert service.reviewer_claude.extra_args == reviewer_claude_args(config)
    assert service.claude.extra_args == claude_args(config)


def test_serve_main_has_no_reviewer_client_when_off(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("FOUNDRY_REVIEWER_TOOLS", raising=False)
    config = FoundryConfig.from_env()
    service = _serve_main(monkeypatch, tmp_path)
    assert service.reviewer_claude is None
    assert service.claude.extra_args == claude_args(config)
