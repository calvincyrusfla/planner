"""Tests for foundry.governor."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from foundry.governor import CapacityGovernor
from foundry.rate_limiter import RateLimiter

NOW = 1_800_000_000.0


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).isoformat()


class FakeBoard:
    def __init__(self) -> None:
        self.usage: dict[str, Any] | None = None

    def set(self, five: float | None = None, seven: float | None = None, age: float = 0.0) -> None:
        self.usage = {
            "fiveHour": {"usedPct": five} if five is not None else None,
            "sevenDay": {"usedPct": seven} if seven is not None else None,
            "reportedAt": iso(NOW - age),
        }

    def state(self) -> dict[str, Any]:
        return {"usage": self.usage}


class FakeQueue:
    def __init__(self) -> None:
        self.events: list[tuple[str, Any, dict[str, Any]]] = []

    def record_event(self, kind: str, task_id: Any, payload: dict[str, Any] | None = None) -> int:
        self.events.append((kind, task_id, payload or {}))
        return len(self.events)


def rig(max_concurrent: int = 4) -> tuple[CapacityGovernor, FakeBoard, RateLimiter, FakeQueue]:
    limiter = RateLimiter(rpm=600, tpm=10_000_000, max_concurrent=max_concurrent)
    board, queue = FakeBoard(), FakeQueue()
    service = SimpleNamespace(factory=SimpleNamespace(limiter=limiter, queue=queue),
                              batch_watcher=SimpleNamespace(board=board))
    return CapacityGovernor(service, interval_s=1, wall_clock=lambda: NOW), board, limiter, queue


@pytest.mark.parametrize("five,seven,expected", [
    (10, 10, 4), (79.9, 84.9, 4), (80, 0, 2), (0, 85, 2), (89.9, 94.9, 2),
    (90, 0, 1), (0, 95, 1), (85, 96, 1),
])
def test_thresholds(five: float, seven: float, expected: int) -> None:
    gov, board, limiter, _ = rig()
    board.set(five, seven)
    asyncio.run(gov.round())
    assert limiter.max_concurrent == expected


def test_stale_or_missing_leaves_configured() -> None:
    gov, board, limiter, queue = rig()
    asyncio.run(gov.round())  # no usage at all
    board.set(99, 99, age=1801)
    asyncio.run(gov.round())
    assert limiter.max_concurrent == 4 and queue.events == []


def test_restores_configured_value_and_records_events() -> None:
    gov, board, limiter, queue = rig()

    async def main() -> None:
        board.set(92)
        await gov.round()
        assert limiter.max_concurrent == 1
        board.set(82)
        await gov.round()
        assert limiter.max_concurrent == 2
        board.set(10)
        await gov.round()
        assert limiter.max_concurrent == 4
        board.set(10)
        await gov.round()  # no change, no event

    asyncio.run(main())
    assert [(e[0], e[2]["from"], e[2]["to"]) for e in queue.events] == [
        ("capacity", 4, 1), ("capacity", 1, 2), ("capacity", 2, 4)]


def test_never_raises_above_configured() -> None:
    gov, board, limiter, _ = rig(max_concurrent=1)
    board.set(82)
    asyncio.run(gov.round())
    assert limiter.max_concurrent == 1


def test_limiter_change_keeps_in_flight_calls_safe() -> None:
    gov, board, limiter, _ = rig(max_concurrent=3)
    done: list[int] = []
    peak_after: list[int] = []

    async def call(i: int, gate: asyncio.Event) -> None:
        async with limiter.slot():
            await gate.wait()
            done.append(i)

    async def main() -> None:
        gate = asyncio.Event()
        tasks = [asyncio.create_task(call(i, gate)) for i in range(6)]
        await asyncio.sleep(0.05)
        assert limiter.in_flight == 3
        board.set(95)
        await gov.round()
        assert limiter.max_concurrent == 1 and limiter.in_flight == 3
        gate.set()
        await asyncio.gather(*tasks)
        peak_after.append(limiter.in_flight)
        board.set(0)
        await gov.round()

    asyncio.run(main())
    assert sorted(done) == list(range(6)) and peak_after == [0]
    assert limiter.max_concurrent == 3


def test_raising_wakes_waiters() -> None:
    gov, board, limiter, _ = rig(max_concurrent=2)
    limiter.set_max_concurrent(1)
    gov.configured = 2

    async def main() -> None:
        gate = asyncio.Event()

        async def call() -> None:
            async with limiter.slot():
                await gate.wait()

        tasks = [asyncio.create_task(call()) for _ in range(2)]
        await asyncio.sleep(0.05)
        assert limiter.in_flight == 1
        board.set(0)
        await gov.round()
        await asyncio.sleep(0.05)
        assert limiter.in_flight == 2
        gate.set()
        await asyncio.gather(*tasks)

    asyncio.run(main())


def test_set_max_concurrent_rejects_nonpositive() -> None:
    with pytest.raises(ValueError):
        RateLimiter(rpm=1, tpm=1, max_concurrent=1).set_max_concurrent(0)
