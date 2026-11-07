"""Unit tests for foundry.rate_limiter (fake clock/sleep/rng: instant and deterministic)."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest

from foundry.rate_limiter import RateLimiter, full_jitter_delay


class FakeTime:
    """A fake monotonic clock whose sleep advances time instantly."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += max(0.0, seconds)
        await asyncio.sleep(0)


class Limited(Exception):
    """Stand-in for ClaudeRateLimited (same retry_after attribute)."""

    def __init__(self, retry_after: float | None = None) -> None:
        super().__init__("rate limited")
        self.retry_after = retry_after


def seq_rng(values: list[float]) -> "callable":
    it: Iterator[float] = iter(values)
    return lambda: next(it)


def make(ft: FakeTime, **kw: object) -> RateLimiter:
    params: dict[str, object] = {"rpm": 1000, "tpm": 10_000_000, "max_concurrent": 100}
    params.update(kw)
    return RateLimiter(clock=ft.clock, sleep=ft.sleep, **params)  # type: ignore[arg-type]


def test_twenty_concurrent_requests_never_exceed_three_in_flight() -> None:
    ft = FakeTime()
    limiter = make(ft, max_concurrent=3)
    observed: list[int] = []
    done: list[int] = []

    async def request(i: int) -> None:
        async with limiter.slot(estimated_tokens=10):
            observed.append(limiter.in_flight)
            await ft.sleep(1.0 + (i % 3) * 0.5)
            observed.append(limiter.in_flight)
        done.append(i)

    async def main() -> None:
        await asyncio.gather(*(request(i) for i in range(20)))

    asyncio.run(main())
    assert len(done) == 20
    assert max(observed) <= 3
    assert limiter.peak_in_flight == 3
    assert limiter.in_flight == 0


def test_rpm_cap_respected_under_fake_clock() -> None:
    ft = FakeTime()
    limiter = make(ft, rpm=10, max_concurrent=50)
    grants: list[float] = []

    async def request() -> None:
        async with limiter.slot():
            grants.append(ft.now)

    async def main() -> None:
        await asyncio.gather(*(request() for _ in range(30)))

    asyncio.run(main())
    assert len(grants) == 30
    # Burst of capacity at t=0, then one request every 60/rpm = 6 seconds.
    assert grants[:10] == [0.0] * 10
    assert grants[10:] == pytest.approx([6.0 * k for k in range(1, 21)])
    # Never more than capacity + rate * t granted by time t.
    for idx, t in enumerate(grants, start=1):
        assert idx <= 10 + (10 / 60) * t + 1e-9
    # Steady state: the last 60 s window holds exactly rpm requests.
    assert limiter.current_rpm() == 10


def test_tpm_cap_respected_under_fake_clock() -> None:
    ft = FakeTime()
    limiter = make(ft, tpm=1000, max_concurrent=50)
    grants: list[float] = []

    async def request() -> None:
        async with limiter.slot(estimated_tokens=400):
            grants.append(ft.now)

    async def main() -> None:
        await asyncio.gather(*(request() for _ in range(8)))

    asyncio.run(main())
    rate = 1000 / 60
    for idx, t in enumerate(grants, start=1):
        assert idx * 400 <= 1000 + rate * t + 1e-6
    assert grants[0] == grants[1] == 0.0
    assert grants[2] == pytest.approx(200 / rate)  # waits for the missing 200 tokens
    assert grants[3] == pytest.approx(grants[2] + 400 / rate)


def test_oversized_estimate_is_clamped_and_does_not_deadlock() -> None:
    ft = FakeTime()
    limiter = make(ft, tpm=100)

    async def main() -> None:
        async with limiter.slot(estimated_tokens=10_000):
            pass
        async with limiter.slot(estimated_tokens=10_000):
            pass

    asyncio.run(main())
    assert ft.now == pytest.approx(60.0)  # second one waited for a full refill


def test_record_actual_reconciles_estimates() -> None:
    ft = FakeTime()
    limiter = make(ft, tpm=1000)

    async def main() -> None:
        async with limiter.slot(estimated_tokens=100) as handle:
            limiter.record_actual(700)  # uses the current slot's estimate
            handle.record_actual(9999)  # second reconciliation is ignored
        assert limiter.current_tpm() == 700
        # Bucket now holds 300 tokens: a 400-token request must wait for 100 more.
        async with limiter.slot(estimated_tokens=400):
            pass

    asyncio.run(main())
    assert ft.now == pytest.approx(100 / (1000 / 60))


def test_record_actual_credit_when_overestimated() -> None:
    ft = FakeTime()
    limiter = make(ft, tpm=1000)

    async def main() -> None:
        async with limiter.slot(estimated_tokens=900):
            pass
        limiter.record_actual(100, estimated_tokens=900)
        assert limiter.current_tpm() == 100
        async with limiter.slot(estimated_tokens=900):  # credit makes room right away
            pass

    asyncio.run(main())
    assert ft.now == 0.0


def test_backoff_delays_within_full_jitter_bounds() -> None:
    ft = FakeTime()
    rng_values = [0.5, 0.999, 0.25, 0.0, 0.75]
    limiter = RateLimiter(10, 10_000, 2, clock=ft.clock, sleep=ft.sleep, rng=seq_rng(rng_values))
    calls = 0

    async def flaky() -> str:
        nonlocal calls
        calls += 1
        if calls < 6:
            raise Limited()
        return "ok"

    result = asyncio.run(
        limiter.call_with_backoff(flaky, max_attempts=6, base=1.0, cap=10.0, retry_on=(Limited,))
    )
    assert result == "ok"
    assert calls == 6
    ceilings = [1.0, 2.0, 4.0, 8.0, 10.0]
    assert len(ft.sleeps) == 5
    for delay, ceiling, r in zip(ft.sleeps, ceilings, rng_values):
        assert 0.0 <= delay < ceiling
        assert delay == pytest.approx(r * ceiling)
    assert limiter.rate_limit_hits == 5


def test_full_jitter_delay_helper_bounds() -> None:
    for attempt in range(10):
        hi = full_jitter_delay(attempt, base=0.5, cap=30.0, rng=lambda: 0.999999)
        assert 0.0 <= hi < min(30.0, 0.5 * 2**attempt)
        assert full_jitter_delay(attempt, base=0.5, cap=30.0, rng=lambda: 0.0) == 0.0


def test_backoff_honours_retry_after_and_pauses_other_callers() -> None:
    ft = FakeTime()
    limiter = RateLimiter(100, 100_000, 5, clock=ft.clock, sleep=ft.sleep, rng=lambda: 0.0)
    attempts = 0

    async def once_limited() -> int:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise Limited(retry_after=30.0)
        return attempts

    async def main() -> float:
        assert await limiter.call_with_backoff(once_limited, retry_on=(Limited,)) == 2
        # The cooldown is shared: a fresh slot right after must not start before it ends.
        async with limiter.slot(1):
            return ft.now

    started = asyncio.run(main())
    assert ft.sleeps[0] == pytest.approx(30.0)
    assert started >= 30.0


def test_backoff_gives_up_after_max_attempts() -> None:
    ft = FakeTime()
    limiter = RateLimiter(10, 10_000, 2, clock=ft.clock, sleep=ft.sleep, rng=lambda: 0.5)
    calls = 0

    async def always() -> None:
        nonlocal calls
        calls += 1
        raise Limited(retry_after=None)

    with pytest.raises(Limited):
        asyncio.run(limiter.call_with_backoff(always, max_attempts=3, retry_on=(Limited,)))
    assert calls == 3
    assert len(ft.sleeps) == 2


def test_backoff_does_not_retry_other_errors() -> None:
    ft = FakeTime()
    limiter = make(ft)
    calls = 0

    async def boom() -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("not a rate limit")

    with pytest.raises(RuntimeError):
        asyncio.run(limiter.call_with_backoff(boom, retry_on=(Limited,)))
    assert calls == 1
    assert ft.sleeps == []


def test_backoff_with_estimated_tokens_runs_each_attempt_in_a_slot() -> None:
    ft = FakeTime()
    limiter = RateLimiter(100, 100_000, 1, clock=ft.clock, sleep=ft.sleep, rng=lambda: 0.1)
    seen: list[int] = []

    async def fn() -> str:
        seen.append(limiter.in_flight)
        if len(seen) < 3:
            raise Limited()
        return "done"

    out = asyncio.run(limiter.call_with_backoff(fn, estimated_tokens=50, retry_on=(Limited,)))
    assert out == "done"
    assert seen == [1, 1, 1]
    assert limiter.in_flight == 0
    assert limiter.current_rpm() == 3


def test_default_retry_on_is_claude_rate_limited() -> None:
    claude_client = pytest.importorskip("foundry.claude_client")
    exc_type = claude_client.ClaudeRateLimited
    ft = FakeTime()
    limiter = RateLimiter(10, 10_000, 2, clock=ft.clock, sleep=ft.sleep, rng=lambda: 0.0)
    calls = 0

    async def fn() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise exc_type(retry_after=2.0)
        return "ok"

    assert asyncio.run(limiter.call_with_backoff(fn)) == "ok"
    assert ft.sleeps == [pytest.approx(2.0)]


def test_throttle_reduces_throughput_then_recovers() -> None:
    ft = FakeTime()
    limiter = make(ft, rpm=60, max_concurrent=1)
    grants: list[float] = []

    async def main() -> None:
        for _ in range(60):  # drain the burst capacity at t=0
            async with limiter.slot():
                pass
        limiter.throttle(0.5, 30.0)
        assert limiter.is_throttled and limiter.throttle_factor == 0.5
        while ft.now < 60.0:
            async with limiter.slot():
                grants.append(ft.now)

    asyncio.run(main())
    throttled = [t for t in grants if t <= 30.0]
    recovered = [t for t in grants if 30.0 < t <= 60.0]
    assert 14 <= len(throttled) <= 16  # ~0.5 req/s while throttled
    assert len(recovered) >= 28  # back to ~1 req/s
    gaps = [b - a for a, b in zip(throttled, throttled[1:])]
    assert all(g == pytest.approx(2.0) for g in gaps)
    assert not limiter.is_throttled
    assert limiter.throttle_factor == 1.0


def test_throttle_clips_tokens_and_validates_arguments() -> None:
    ft = FakeTime()
    limiter = make(ft, tpm=1000)
    limiter.throttle(0.25, 10.0)
    snap = limiter.snapshot()
    assert snap["throttle_factor"] == 0.25

    async def main() -> None:
        async with limiter.slot(estimated_tokens=250):  # the clipped budget allows exactly 250
            pass

    asyncio.run(main())
    assert ft.now == 0.0
    with pytest.raises(ValueError):
        limiter.throttle(0.0, 5.0)
    with pytest.raises(ValueError):
        limiter.throttle(1.5, 5.0)
    with pytest.raises(ValueError):
        RateLimiter(0, 10, 1)


def test_introspection_window_slides() -> None:
    ft = FakeTime()
    limiter = make(ft)

    async def main() -> None:
        async with limiter.slot(estimated_tokens=500):
            assert limiter.in_flight == 1
        assert limiter.current_rpm() == 1
        assert limiter.current_tpm() == 500
        await ft.sleep(61.0)
        assert limiter.current_rpm() == 0
        assert limiter.current_tpm() == 0

    asyncio.run(main())
    snap = limiter.snapshot()
    assert snap["in_flight"] == 0 and snap["peak_in_flight"] == 1
