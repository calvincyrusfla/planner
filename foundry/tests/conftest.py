"""Shared pytest fixtures for the foundry suite.

Only generic helpers live here (temp db path, fake clock).  Nothing in this
file imports other foundry modules at import time, so one team's broken
module cannot break collection of another team's tests.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

#: Fixed starting instant for FakeClock (UTC).
FIXED_START = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)


class FakeClock:
    """Deterministic clock: ``monotonic()`` seconds plus a matching UTC ``now()``.

    Pass ``clock.monotonic`` where a ``time.monotonic``-style callable is
    expected, ``clock.now`` for wall-clock datetimes, and ``clock.sleep`` as
    an async sleep that advances time instantly instead of waiting.
    """

    def __init__(self, start: datetime = FIXED_START, monotonic_start: float = 1000.0) -> None:
        self._start = start
        self._mono_start = monotonic_start
        self._elapsed = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        """Current monotonic reading in seconds."""
        return self._mono_start + self._elapsed

    def time(self) -> float:
        """Current wall-clock time as a POSIX timestamp."""
        return self.now().timestamp()

    def now(self) -> datetime:
        """Current wall-clock time (timezone-aware UTC)."""
        return self._start + timedelta(seconds=self._elapsed)

    def advance(self, seconds: float) -> None:
        """Move the clock forward by ``seconds`` (must be >= 0)."""
        if seconds < 0:
            raise ValueError("FakeClock cannot go backwards")
        self._elapsed += seconds

    async def sleep(self, seconds: float) -> None:
        """Async sleep replacement: records the delay and advances the clock."""
        self.sleeps.append(seconds)
        self.advance(max(0.0, seconds))

    def __call__(self) -> float:
        """Calling the clock returns ``monotonic()``."""
        return self.monotonic()


@pytest.fixture
def fake_clock() -> FakeClock:
    """A fresh FakeClock starting at FIXED_START."""
    return FakeClock()


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Path for a throwaway SQLite database (file not created yet)."""
    return tmp_path / "foundry.db"


@pytest.fixture
def clean_foundry_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[pytest.MonkeyPatch]:
    """Remove every FOUNDRY_* / FOREMAN_MODEL variable for the test's duration."""
    import os

    for name in list(os.environ):
        if name.startswith("FOUNDRY_") or name == "FOREMAN_MODEL":
            monkeypatch.delenv(name, raising=False)
    yield monkeypatch
