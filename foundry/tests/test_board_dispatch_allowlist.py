"""Board dispatch: FOUNDRY_ALLOWED_REPOS decides which repos' batches may be claimed."""
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any

import pytest

from foundry.board_dispatch import BatchWatcher
from foundry.config import FoundryConfig

REPO = "owner/app"
LOGGER = "foundry.board_dispatch"
OLD_HEARTBEAT = "2020-01-01T00:00:00.000Z"


class FakeBoard:
    def __init__(self, *batches: dict[str, Any]) -> None:
        self.batches = list(batches)
        self.ops: list[tuple[str, dict[str, Any]]] = []

    def state(self) -> dict[str, Any]:
        return {"foundryBatches": self.batches}

    def op(self, type_: str, args: dict[str, Any]) -> dict[str, Any]:
        self.ops.append((type_, dict(args)))
        return {"ok": True}

    def claims(self) -> list[str]:
        return [args["id"] for type_, args in self.ops if type_ == "claimBatch"]


def make_watcher(board: FakeBoard, allowed: list[str]) -> BatchWatcher:
    factory = SimpleNamespace(manager=SimpleNamespace(), queue=SimpleNamespace())
    service = SimpleNamespace(
        running=True, factory=factory, repo="app", config=FoundryConfig(allowed_repos=allowed)
    )
    return BatchWatcher(service, board=board, repo_slug=REPO, host="pc", interval=1.0, enabled=True)


def queued(repo: str = REPO) -> dict[str, Any]:
    return {"id": "B-1", "title": "Work", "repo": repo, "status": "queued", "subtasks": []}


def stale(repo: str = REPO) -> dict[str, Any]:
    return {
        "id": "B-2",
        "title": "Abandoned",
        "repo": repo,
        "status": "running",
        "claimedBy": {"host": "other", "service": "foundry@other:owner/app"},
        "heartbeatAt": OLD_HEARTBEAT,
        "subtasks": [],
    }


def allowlist_warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "FOUNDRY_ALLOWED_REPOS" in r.getMessage()
    ]


def test_batch_for_unlisted_repo_is_never_claimed() -> None:
    board = FakeBoard(queued())
    watcher = make_watcher(board, ["owner/other"])

    assert asyncio.run(watcher.poll_once()) == []
    assert asyncio.run(watcher.poll_once()) == []

    assert board.ops == []
    assert watcher.claimed == []


def test_stale_batch_is_taken_over_only_when_listed() -> None:
    batch = stale()

    assert make_watcher(FakeBoard(batch), [REPO])._claimable(batch) is True
    assert make_watcher(FakeBoard(batch), ["owner/other"])._claimable(batch) is False


def test_listed_repo_is_claimed_case_insensitively() -> None:
    board = FakeBoard(queued("Owner/App"))
    watcher = make_watcher(board, ["OWNER/app"])

    assert asyncio.run(watcher.poll_once()) == ["B-1"]

    assert board.claims() == ["B-1"]


def test_listed_repo_logs_no_allowlist_warning(caplog: pytest.LogCaptureFixture) -> None:
    watcher = make_watcher(FakeBoard(), [REPO])

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert watcher._claimable(queued()) is True

    assert allowlist_warnings(caplog) == []


def test_empty_allowlist_keeps_behaviour_and_warns_once(caplog: pytest.LogCaptureFixture) -> None:
    board = FakeBoard(queued())
    watcher = make_watcher(board, [])

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert asyncio.run(watcher.poll_once()) == ["B-1"]
        assert asyncio.run(watcher.poll_once()) == ["B-1"]

    assert board.claims()
    assert len(allowlist_warnings(caplog)) == 1


def test_empty_allowlist_still_ignores_other_repos() -> None:
    board = FakeBoard(queued("owner/other"))
    watcher = make_watcher(board, [])

    assert asyncio.run(watcher.poll_once()) == []
    assert board.ops == []


def test_service_without_config_has_no_allowlist() -> None:
    factory = SimpleNamespace(manager=SimpleNamespace(), queue=SimpleNamespace())
    service = SimpleNamespace(running=True, factory=factory, repo="app")
    watcher = BatchWatcher(service, board=FakeBoard(), repo_slug=REPO, host="pc", interval=1.0, enabled=True)

    assert watcher.allowed_repos == []
    assert watcher._claimable(queued()) is True


def test_summary_exposes_allowed_repos() -> None:
    assert make_watcher(FakeBoard(), [REPO]).summary()["allowed_repos"] == [REPO]
    assert make_watcher(FakeBoard(), []).summary()["allowed_repos"] == []


def test_config_reads_allowed_repos_from_env() -> None:
    config = FoundryConfig.from_env({"FOUNDRY_ALLOWED_REPOS": " a/b, ,c/d,a/b "})

    assert config.allowed_repos == ["a/b", "c/d"]


def test_config_allowed_repos_defaults_to_empty() -> None:
    assert FoundryConfig.from_env({}).allowed_repos == []
    assert FoundryConfig.from_env({"FOUNDRY_ALLOWED_REPOS": "  "}).allowed_repos == []
