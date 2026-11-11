"""``board_bridge_mode=off`` stops the department-task mirror, not board batches, updates or metrics."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from foundry.board_bridge import BoardBridge
from foundry.config import FoundryConfig
from foundry.main import demo_claude, demo_gemma, write_demo_repo
from foundry.serve import Service


class FakeBoard:
    def __init__(self) -> None:
        self.ops: list[tuple[str, dict[str, Any]]] = []

    def state(self) -> dict[str, Any]:
        return {}

    def op(self, type_: str, args: dict[str, Any]) -> dict[str, Any]:
        self.ops.append((type_, dict(args)))
        return {"ok": True}


class FakeWatcher:
    """Stands in for BatchWatcher and UpdateWatcher: counts starts and stops, touches no board."""

    built: list[FakeWatcher]

    def __init__(self, service: Any, board: Any = None, **kw: Any) -> None:
        self.board = board
        self.repo_slug = "owner/repo"
        self.host = "pc"
        self.service_id = "foundry@pc:owner/repo"
        self.starts = 0
        self.stopped = False
        type(self).built.append(self)

    def start(self) -> object:
        self.starts += 1
        return object()

    async def stop(self) -> None:
        self.stopped = True


class FakeBatchWatcher(FakeWatcher):
    built: list[FakeWatcher] = []


class FakeUpdateWatcher(FakeWatcher):
    built: list[FakeWatcher] = []


@pytest.fixture(autouse=True)
def fake_watchers(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeBatchWatcher.built = []
    FakeUpdateWatcher.built = []
    monkeypatch.setattr("foundry.board_dispatch.BatchWatcher", FakeBatchWatcher)
    monkeypatch.setattr("foundry.update_watcher.UpdateWatcher", FakeUpdateWatcher)


def _service(tmp_path: Path, mode: Any, **kw: Any) -> Service:
    repo = write_demo_repo(tmp_path / "repo")
    config = FoundryConfig(
        db_path=tmp_path / "foundry.db", rpm=1000, tpm=10_000_000, max_concurrent=5,
        teams=["alpha", "bravo"], board_bridge_mode=mode,
    )
    return Service(
        config, repo=repo, claude=demo_claude(repo, fail_first=None), gemma=demo_gemma(),
        # a dry bridge whatever the mode: only the service's own decision is under test
        factory_kwargs={"bridge": BoardBridge("dry"), "patches_dir": tmp_path / "patches"}, monitor_s=3600,
        **kw,
    )


def _started(svc: Service) -> tuple[bool, bool]:
    """Run the service once; whether its batch and update watchers were started."""

    async def go() -> tuple[bool, bool]:
        await svc.start()
        try:
            return (
                svc.batch_watcher is not None and svc.batch_watcher.starts == 1,
                svc.update_watcher is not None and svc.update_watcher.starts == 1,
            )
        finally:
            await svc.stop()

    return asyncio.run(go())


# --------------------------------------------------------------------------- _start_batches


@pytest.mark.parametrize("mode", ["auto", "off"])
def test_live_modes_start_the_batch_and_update_watchers(tmp_path: Path, mode: str) -> None:
    assert _started(_service(tmp_path, mode)) == (True, True)
    assert len(FakeBatchWatcher.built) == 1 and len(FakeUpdateWatcher.built) == 1


def test_dry_mode_starts_neither_watcher(tmp_path: Path) -> None:
    assert _started(_service(tmp_path, "dry")) == (False, False)
    assert FakeBatchWatcher.built == [] and FakeUpdateWatcher.built == []


def test_dry_mode_with_an_injected_board_starts_both(tmp_path: Path) -> None:
    board = FakeBoard()

    assert _started(_service(tmp_path, "dry", batch_board=board)) == (True, True)
    assert FakeBatchWatcher.built[0].board is board


@pytest.mark.parametrize("mode", ["auto", "off"])
def test_update_watcher_uses_the_batch_watchers_board(tmp_path: Path, mode: str) -> None:
    board = FakeBoard()

    assert _started(_service(tmp_path, mode, batch_board=board)) == (True, True)
    assert FakeUpdateWatcher.built[0].board is board


# --------------------------------------------------------------------------- _metrics_sender


def _metrics_service(tmp_path: Path, mode: str, board: FakeBoard) -> Service:
    # the real bridge of that mode: it has no op method, so the sender looks past it
    factory = SimpleNamespace(bridge=BoardBridge("off" if mode == "off" else "dry"))
    return Service(SimpleNamespace(board_bridge_mode=mode), repo=tmp_path, factory=factory, monitor_s=60.0,
                   batch_watcher=SimpleNamespace(board=board))


@pytest.mark.parametrize("mode", ["auto", "off"])
def test_metrics_sender_sends_through_the_batch_board(tmp_path: Path, mode: str) -> None:
    board = FakeBoard()

    sender = _metrics_service(tmp_path, mode, board)._metrics_sender()

    assert sender is not None
    assert asyncio.run(sender("reportFoundryMetrics", {"source": "usage"})) == {"ok": True}
    assert board.ops == [("reportFoundryMetrics", {"source": "usage"})]


def test_metrics_sender_is_none_in_dry_mode(tmp_path: Path) -> None:
    board = FakeBoard()

    assert _metrics_service(tmp_path, "dry", board)._metrics_sender() is None
    assert board.ops == []
