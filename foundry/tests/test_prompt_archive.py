"""Tests for the prompt archive and its use by the workers."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from foundry import prompt_archive
from foundry.builder_worker import _WorkerBase
from foundry.config import FoundryConfig
from foundry.orchestrator_queue import FoundryQueue
from foundry.schemas import ClaudeResult


def _queue(tmp_path: Path) -> FoundryQueue:
    return FoundryQueue(tmp_path / "q.db")


def _result(text: str = "the reply") -> ClaudeResult:
    return ClaudeResult(text=text, input_tokens=7, output_tokens=3, cache_read_tokens=11, session_id="sess-1")


def _store(queue: FoundryQueue, **over: Any) -> int | None:
    args: dict[str, Any] = dict(
        task_id="T-1", ticket_id="K-1", attempt=1, role="builder", agent_id="alpha-1",
        prompt="the prompt", result=_result(),
    )
    args.update(over)
    return prompt_archive.archive_call(queue, **args)


def test_store_and_read_back(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    row_id = _store(queue, base_sha="abc1234")
    assert row_id is not None
    call = prompt_archive.get_call(queue, row_id)
    assert call is not None
    assert call["prompt"] == "the prompt" and call["reply"] == "the reply"
    assert call["task_id"] == "T-1" and call["ticket_id"] == "K-1" and call["attempt"] == 1
    assert call["role"] == "builder" and call["agent_id"] == "alpha-1"
    assert call["session_id"] == "sess-1" and call["base_sha"] == "abc1234"
    assert (call["input_tokens"], call["output_tokens"], call["cache_read_tokens"]) == (7, 3, 11)
    assert prompt_archive.get_call(queue, 999) is None


def test_compression_round_trip(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    big = "line of text ünïcode\n" * 5000
    row_id = _store(queue, prompt=big, result=_result(big[::-1]))
    assert row_id is not None
    stored = queue.conn.execute("SELECT length(prompt) AS n FROM prompt_archive WHERE id=?", (row_id,)).fetchone()
    assert stored["n"] < len(big) // 4
    call = prompt_archive.get_call(queue, row_id)
    assert call is not None and call["prompt"] == big and call["reply"] == big[::-1]


def test_calls_for_filters_and_omits_texts(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    _store(queue)
    _store(queue, role="reviewer", agent_id="alpha-6")
    _store(queue, task_id="T-2", ticket_id="K-2")
    assert len(prompt_archive.calls_for(queue)) == 3
    assert len(prompt_archive.calls_for(queue, task_id="T-1")) == 2
    assert len(prompt_archive.calls_for(queue, ticket_id="K-2")) == 1
    [only] = prompt_archive.calls_for(queue, task_id="T-1", role="reviewer")
    assert only["agent_id"] == "alpha-6"
    assert "prompt" not in only and "reply" not in only


def test_prune(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    old = _store(queue)
    fresh = _store(queue)
    queue.conn.execute("UPDATE prompt_archive SET ts='2000-01-01T00:00:00.000000Z' WHERE id=?", (old,))
    assert prompt_archive.prune(queue, 30) == 1
    assert prompt_archive.get_call(queue, old) is None  # type: ignore[arg-type]
    assert prompt_archive.get_call(queue, fresh) is not None  # type: ignore[arg-type]


def test_archive_call_never_raises(tmp_path: Path) -> None:
    assert prompt_archive.archive_call(
        "not a queue", task_id="T", ticket_id=None, attempt=None, role="builder",
        agent_id="a", prompt="p", result=_result(),
    ) is None
    queue = _queue(tmp_path)
    queue.close()
    assert _store(queue) is None


def test_config_settings() -> None:
    assert FoundryConfig().prompt_archive is True and FoundryConfig().prompt_archive_days == 30
    config = FoundryConfig.from_env({"FOUNDRY_PROMPT_ARCHIVE": "off", "FOUNDRY_PROMPT_ARCHIVE_DAYS": "5"})
    assert config.prompt_archive is False and config.prompt_archive_days == 5


class _Client:
    async def run(self, prompt: str, **_: Any) -> ClaudeResult:
        return _result("worker reply")


def _worker(queue: FoundryQueue, enabled: bool) -> Any:
    worker: Any = _WorkerBase("alpha", queue, _Client(), None, None)
    worker.agent_id = "alpha-1"
    worker.role = "builder"
    worker.config = SimpleNamespace(prompt_archive=enabled, prompt_archive_days=30, turn_ledger=False)
    return worker


def _wait_for_rows(queue: FoundryQueue, count: int) -> list[dict[str, Any]]:
    deadline = time.monotonic() + 5
    rows = prompt_archive.calls_for(queue)
    while len(rows) < count and time.monotonic() < deadline:
        time.sleep(0.02)
        rows = prompt_archive.calls_for(queue)
    return rows


def test_worker_archives_when_on(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    worker = _worker(queue, True)
    asyncio.run(worker._call_claude("hello", task_id="T-9", ticket_id="K-9", attempt=2))
    [row] = _wait_for_rows(queue, 1)
    assert row["task_id"] == "T-9" and row["ticket_id"] == "K-9" and row["attempt"] == 2
    assert row["role"] == "builder" and row["agent_id"] == "alpha-1" and row["session_id"] == "sess-1"
    call = prompt_archive.get_call(queue, row["id"])
    assert call is not None and call["prompt"] == "hello" and call["reply"] == "worker reply"


def test_worker_does_not_archive_when_off(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    worker = _worker(queue, False)
    asyncio.run(worker._call_claude("hello", task_id="T-9"))
    time.sleep(0.2)
    assert prompt_archive.calls_for(queue) == []
