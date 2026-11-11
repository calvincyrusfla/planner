"""Capacity governor: fewer parallel Claude calls as the usage limits near.

Every ``FOUNDRY_GOVERNOR_S`` seconds the governor reads the usage meter from the
board state and sets the rate limiter's concurrency: the configured value below
80% (five-hour) / 85% (weekly), 2 from there, 1 from 90% / 95%. A report older than
30 minutes (or none) counts as unknown and leaves the configured value. It never
pauses work. ``FOUNDRY_GOVERNOR=0`` turns it off.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import time
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger("foundry.governor")

__all__ = ["CapacityGovernor", "governor_enabled", "governor_interval"]

STALE_AFTER_S = 1800.0
FIVE_HOUR_STEPS = ((90.0, 1), (80.0, 2))
SEVEN_DAY_STEPS = ((95.0, 1), (85.0, 2))


def governor_enabled() -> bool:
    """FOUNDRY_GOVERNOR (default on)."""
    return os.environ.get("FOUNDRY_GOVERNOR", "").strip().lower() not in {"0", "false", "no", "off"}


def governor_interval() -> float:
    """Seconds between rounds: FOUNDRY_GOVERNOR_S (default 300)."""
    try:
        value = float(os.environ.get("FOUNDRY_GOVERNOR_S", "").strip() or 300.0)
    except ValueError:
        return 300.0
    return max(value, 1.0) if math.isfinite(value) else 300.0


def _pct(meter: Any, key: str) -> float | None:
    win = meter.get(key) if isinstance(meter, dict) else None
    val = win.get("usedPct") if isinstance(win, dict) else None
    if isinstance(val, bool) or not isinstance(val, (int, float)) or not math.isfinite(val):
        return None
    return float(val)


def _cap_for(pct: float | None, steps: tuple[tuple[float, int], ...]) -> int | None:
    if pct is None:
        return None
    for threshold, cap in steps:
        if pct >= threshold:
            return cap
    return None


class CapacityGovernor:
    """Lowers the limiter's concurrency near a usage limit and restores it after.

    Args:
        service: the running service (``factory.limiter``, ``batch_watcher.board``).
        interval_s: seconds between rounds (default FOUNDRY_GOVERNOR_S).
        wall_clock: epoch seconds source (tests pass a fake).
    """

    def __init__(self, service: Any, *, interval_s: float | None = None, wall_clock: Any = time.time) -> None:
        self.service = service
        self.interval_s = governor_interval() if interval_s is None else interval_s
        self._wall = wall_clock
        self.rounds = 0
        self.last_error: str | None = None
        self.configured: int | None = None
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._loop())

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self.interval_s)
                await self.round()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the governor must outlive a bad round
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.warning("governor: round failed: %s", self.last_error)

    def _limiter(self) -> Any:
        return getattr(getattr(self.service, "factory", None), "limiter", None)

    def _fresh_usage(self, state: Any) -> dict[str, Any] | None:
        usage = state.get("usage") if isinstance(state, dict) else None
        if not isinstance(usage, dict):
            return None
        raw = usage.get("reportedAt")
        if not isinstance(raw, str):
            return None
        try:
            at = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        if float(self._wall()) - at.timestamp() > STALE_AFTER_S:
            return None
        return usage

    def target(self, state: Any) -> int | None:
        """The cap the meter asks for, or None for the configured value."""
        usage = self._fresh_usage(state)
        if usage is None:
            return None
        caps = [c for c in (_cap_for(_pct(usage, "fiveHour"), FIVE_HOUR_STEPS),
                            _cap_for(_pct(usage, "sevenDay"), SEVEN_DAY_STEPS)) if c is not None]
        return min(caps) if caps else None

    async def round(self) -> None:
        """One pass: read the meter and set the limiter's concurrency."""
        limiter = self._limiter()
        board = getattr(getattr(self.service, "batch_watcher", None), "board", None)
        if limiter is None or board is None:
            return
        self.rounds += 1
        state = await asyncio.to_thread(board.state)
        if self.configured is None:
            self.configured = int(limiter.max_concurrent)
        want = self.target(state)
        goal = self.configured if want is None else min(want, self.configured)
        current = int(limiter.max_concurrent)
        if goal == current:
            return
        limiter.set_max_concurrent(goal)
        log.info("governor: concurrency %d -> %d (configured %d)", current, goal, self.configured)
        queue = getattr(getattr(self.service, "factory", None), "queue", None)
        if queue is not None:
            try:
                queue.record_event("capacity", None, {"from": current, "to": goal,
                                                      "configured": self.configured})
            except Exception:  # noqa: BLE001
                log.exception("governor: record_event failed")
