"""Every worker of a TeamPool sees the pool's Foundry settings (builder_mode, help_after ...)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from foundry.builder_worker import TeamPool
from foundry.config import FoundryConfig
from foundry.floor_manager import Factory, build_factory
from foundry.tests.test_builder_client_wiring import FakeClaude, _hold


def _edit_config(**overrides: Any) -> FoundryConfig:
    return FoundryConfig.from_env(environ={"FOUNDRY_BUILDER_MODE": "edit"}, **overrides)


def _pool(config: Any = None) -> TeamPool:
    return TeamPool(
        "alpha", 2, queue=object(), client=FakeClaude(), limiter=None, telemetry=None, config=config
    )


def test_pool_config_reaches_every_builder() -> None:
    config = _edit_config()
    pool = _pool(config)
    assert len(pool.builders) == 2
    for builder in pool.builders:
        assert builder.config is config
        assert builder.config.builder_mode == "edit"


def test_pool_config_reaches_the_reviewer() -> None:
    config = _edit_config()
    pool = _pool(config)
    assert getattr(pool.reviewer, "config", None) is config


async def _promoted_peer_config(config: Any) -> Any:
    pool = _pool(config)
    stop = asyncio.Event()
    pool._stop = stop
    pool._tasks.append(asyncio.create_task(_hold(stop)))
    agent_id = pool.promote_peer_reviewer(slot=1)
    assert agent_id is not None
    try:
        return getattr(pool.peer_reviewers[agent_id], "config", None)
    finally:
        await pool.stop()


def test_pool_config_reaches_a_promoted_peer_reviewer() -> None:
    config = _edit_config()
    assert asyncio.run(_promoted_peer_config(config)) is config


def test_pool_without_config_leaves_workers_config_none() -> None:
    pool = _pool()
    assert all(builder.config is None for builder in pool.builders)
    assert getattr(pool.reviewer, "config", None) is None
    assert asyncio.run(_promoted_peer_config(None)) is None


def test_build_factory_config_reaches_builders_of_every_pool(tmp_path: Path) -> None:
    config = _edit_config(
        db_path=tmp_path / "foundry.db", board_bridge_mode="dry", teams=["alpha", "bravo"], builders_per_team=2
    )
    factory = build_factory(config, claude=FakeClaude(), gemma=object(), repo_root=tmp_path)
    assert isinstance(factory, Factory)
    assert set(factory.pools) == {"alpha", "bravo"}
    for pool in factory.pools.values():
        assert pool.builders
        for builder in pool.builders:
            assert builder.config is config
            assert builder.config.builder_mode == "edit"
        assert getattr(pool.reviewer, "config", None) is config
