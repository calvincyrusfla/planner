"""FOUNDRY_SIZING and FOUNDRY_MAX_CONTEXT_LEVEL settings and heuristic-only sizing."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from foundry.config import FoundryConfig
from foundry.orchestrator_queue import FoundryQueue
from foundry.shop_foreman import ShopForeman
from foundry.telemetry import Telemetry


def test_defaults() -> None:
    cfg = FoundryConfig()
    assert cfg.sizing == "gemma"
    assert cfg.max_context_level == 3


def test_env_parses() -> None:
    cfg = FoundryConfig.from_env({"FOUNDRY_SIZING": "Heuristic", "FOUNDRY_MAX_CONTEXT_LEVEL": "1"})
    assert cfg.sizing == "heuristic"
    assert cfg.max_context_level == 1


@pytest.mark.parametrize(
    "env",
    [{"FOUNDRY_SIZING": "bogus"}, {"FOUNDRY_MAX_CONTEXT_LEVEL": "0"}, {"FOUNDRY_MAX_CONTEXT_LEVEL": "4"}],
)
def test_env_refused(env: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        FoundryConfig.from_env(env)


class _FakeGemma:
    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, messages: list[dict], **kwargs: Any) -> Any:
        self.calls += 1
        raise RuntimeError("fake gemma should not answer")


def _foreman(tmp_path: Path, sizing: str) -> tuple[ShopForeman, _FakeGemma, FoundryQueue]:
    queue = FoundryQueue(tmp_path / "foundry.db")
    telemetry = Telemetry(queue)
    gemma = _FakeGemma()
    config = FoundryConfig(db_path=tmp_path / "foundry.db", sizing=sizing)  # type: ignore[arg-type]
    return ShopForeman(gemma, queue, telemetry, None, config), gemma, queue  # type: ignore[arg-type]


def test_heuristic_sizing_never_calls_gemma(tmp_path: Path) -> None:
    foreman, gemma, queue = _foreman(tmp_path, "heuristic")
    try:
        task = {"task_id": "F-1", "team_id": "alpha", "title": "Fix typo", "description": "One word.",
                "files": ["a.py"]}
        directive = asyncio.run(foreman._size_task_scope(task))
        assert gemma.calls == 0
        assert directive.complexity == "S"
        assert foreman.last_source["size_task_scope"] == "heuristic"
    finally:
        queue.close()


def test_gemma_sizing_still_used_by_default(tmp_path: Path) -> None:
    foreman, gemma, queue = _foreman(tmp_path, "gemma")
    try:
        task = {"task_id": "F-1", "team_id": "alpha", "title": "Fix typo", "description": "One word.",
                "files": ["a.py"]}
        asyncio.run(foreman._size_task_scope(task))
        assert gemma.calls >= 1
        assert foreman.last_source["size_task_scope"] == "heuristic"  # fake failed, fell back
    finally:
        queue.close()
