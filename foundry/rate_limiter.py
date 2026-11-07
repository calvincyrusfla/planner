"""Client-side rate limiting for Claude calls.

`RateLimiter` combines three controls that every Claude call in the factory
passes through:

* a **requests-per-minute** token bucket (``rpm``),
* a **tokens-per-minute** token bucket (``tpm``), charged with an estimate when
  the slot is granted and reconciled with the real usage via
  :meth:`RateLimiter.record_actual`,
* a **max-concurrency** limit (``max_concurrent`` calls in flight at once).

It also provides :meth:`RateLimiter.call_with_backoff`, which retries a call
that raised ``ClaudeRateLimited`` using full-jitter exponential backoff (and
honours a server supplied ``retry_after``), and :meth:`RateLimiter.throttle`,
which temporarily scales the budgets down (the Foreman's THROTTLE_DISPATCH
action).

The clock, the sleep coroutine and the random source are injectable so tests
run instantly and deterministically::

    limiter = RateLimiter(rpm=50, tpm=40_000, max_concurrent=3)
    async with limiter.slot(estimated_tokens=1200):
        result = await client.run(prompt, agent_id="alpha-1", role="builder")
        limiter.record_actual(result.input_tokens + result.output_tokens)
"""

from __future__ import annotations

import asyncio
import contextvars
import math
import random
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, TypeVar

__all__ = ["RateLimiter", "SlotHandle", "TokenBucket", "full_jitter_delay"]

T = TypeVar("T")

WINDOW_SECONDS = 60.0
"""Length of the sliding window used by :meth:`RateLimiter.current_rpm`/`current_tpm`."""


def _rate_limited_exception() -> type[BaseException]:
    """Return ``ClaudeRateLimited`` from :mod:`foundry.claude_client`, imported lazily."""
    from foundry.claude_client import ClaudeRateLimited

    return ClaudeRateLimited


def full_jitter_delay(attempt: int, *, base: float, cap: float, rng: Callable[[], float]) -> float:
    """Full-jitter exponential backoff: a uniform value in ``[0, min(cap, base * 2**attempt))``.

    ``attempt`` is zero-based (the first retry uses ``attempt=0``).
    """
    ceiling = min(cap, base * (2.0 ** attempt))
    return max(0.0, rng() * ceiling)


@dataclass
class TokenBucket:
    """A classic token bucket refilled continuously at ``rate_per_s`` up to ``capacity``.

    The level may go negative after a reconciliation debit (actual usage larger
    than the estimate); callers then wait until it refills.
    """

    capacity: float
    rate_per_s: float
    level: float
    updated_at: float

    def refill(self, now: float, *, factor: float = 1.0) -> None:
        """Add tokens accrued since the last update; ``factor`` scales rate and capacity."""
        elapsed = max(0.0, now - self.updated_at)
        cap = self.capacity * factor
        self.level = min(cap, self.level + elapsed * self.rate_per_s * factor)
        self.updated_at = now

    def wait_for(self, amount: float, *, factor: float = 1.0) -> float:
        """Seconds until ``amount`` tokens are available (0 when available now)."""
        if self.level >= amount:
            return 0.0
        rate = self.rate_per_s * factor
        if rate <= 0:
            return math.inf
        return (amount - self.level) / rate


@dataclass
class SlotHandle:
    """Returned by :meth:`RateLimiter.slot`; lets the caller reconcile its estimate."""

    limiter: RateLimiter
    estimated_tokens: int
    granted_at: float
    reconciled: bool = field(default=False)

    def record_actual(self, actual_tokens: int) -> None:
        """Reconcile this slot's estimate with the real token usage (once)."""
        self.limiter.record_actual(actual_tokens, slot=self)


_current_slot: contextvars.ContextVar[SlotHandle | None] = contextvars.ContextVar(
    "foundry_rate_limiter_slot", default=None
)


class RateLimiter:
    """Token-bucket RPM/TPM limiter with a concurrency cap, backoff and throttling.

    Args:
        rpm: maximum requests per minute (bucket capacity and refill per minute).
        tpm: maximum tokens per minute (bucket capacity and refill per minute).
        max_concurrent: maximum calls in flight at once.
        clock: monotonic clock in seconds (inject a fake in tests).
        sleep: coroutine used for every wait (inject a fake that advances the clock).
        rng: random source in ``[0, 1)`` used for backoff jitter.
    """

    def __init__(
        self,
        rpm: int,
        tpm: int,
        max_concurrent: int,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        rng: Callable[[], float] = random.random,
    ) -> None:
        if rpm <= 0 or tpm <= 0 or max_concurrent <= 0:
            raise ValueError("rpm, tpm and max_concurrent must all be positive")
        self.rpm = int(rpm)
        self.tpm = int(tpm)
        self.max_concurrent = int(max_concurrent)
        self._clock = clock
        self._sleep = sleep
        self._rng = rng
        now = clock()
        self._req_bucket = TokenBucket(float(rpm), rpm / WINDOW_SECONDS, float(rpm), now)
        self._tok_bucket = TokenBucket(float(tpm), tpm / WINDOW_SECONDS, float(tpm), now)
        self._in_flight = 0
        self._peak_in_flight = 0
        self._cond: asyncio.Condition | None = None
        self._gate: asyncio.Lock | None = None
        self._throttle_factor = 1.0
        self._throttle_until = -math.inf
        self._cooldown_until = -math.inf
        self._requests: deque[float] = deque()
        self._tokens: deque[tuple[float, int]] = deque()
        self.rate_limit_hits = 0

    # ------------------------------------------------------------------ helpers
    def _ensure_primitives(self) -> tuple[asyncio.Condition, asyncio.Lock]:
        """Create asyncio primitives lazily so the limiter can be built outside a loop."""
        if self._cond is None or self._gate is None:
            self._cond = asyncio.Condition()
            self._gate = asyncio.Lock()
        return self._cond, self._gate

    def _factor(self, now: float) -> float:
        """The active throttle factor (1.0 once the throttle window has passed)."""
        if now >= self._throttle_until:
            self._throttle_factor = 1.0
        return self._throttle_factor

    def _prune(self, now: float) -> None:
        cutoff = now - WINDOW_SECONDS
        while self._requests and self._requests[0] <= cutoff:
            self._requests.popleft()
        while self._tokens and self._tokens[0][0] <= cutoff:
            self._tokens.popleft()

    # --------------------------------------------------------------- public API
    @asynccontextmanager
    async def slot(self, estimated_tokens: int = 0) -> AsyncIterator[SlotHandle]:
        """Wait for a concurrency slot plus request and token budget, then hold it.

        The estimate is charged up front; call :meth:`SlotHandle.record_actual`
        (or :meth:`record_actual` inside the block) with the real usage so the
        token bucket is corrected. Estimates larger than the bucket are clamped
        to the bucket capacity so a single big request can never deadlock.
        """
        estimate = max(0, int(estimated_tokens))
        cond, gate = self._ensure_primitives()
        async with cond:
            await cond.wait_for(lambda: self._in_flight < self.max_concurrent)
            self._in_flight += 1
            self._peak_in_flight = max(self._peak_in_flight, self._in_flight)
        handle: SlotHandle | None = None
        token = None
        try:
            async with gate:  # FIFO over the buckets: one waiter computes its wait at a time
                while True:
                    now = self._clock()
                    factor = self._factor(now)
                    self._req_bucket.refill(now, factor=factor)
                    self._tok_bucket.refill(now, factor=factor)
                    charge = min(float(estimate), self._tok_bucket.capacity * factor)
                    wait = max(
                        self._cooldown_until - now,
                        self._req_bucket.wait_for(1.0, factor=factor),
                        self._tok_bucket.wait_for(charge, factor=factor),
                    )
                    if self._throttle_until > now:
                        # Re-evaluate when the throttle lifts (budgets grow back).
                        wait = min(wait, self._throttle_until - now) if wait > 0 else wait
                    if wait <= 0:
                        self._req_bucket.level -= 1.0
                        self._tok_bucket.level -= charge
                        self._prune(now)
                        self._requests.append(now)
                        self._tokens.append((now, estimate))
                        break
                    await self._sleep(wait)
            handle = SlotHandle(self, estimate, self._clock())
            token = _current_slot.set(handle)
            yield handle
        finally:
            if token is not None:
                _current_slot.reset(token)
            async with cond:
                self._in_flight -= 1
                cond.notify(1)

    def record_actual(
        self,
        actual_tokens: int,
        estimated_tokens: int | None = None,
        *,
        slot: SlotHandle | None = None,
    ) -> None:
        """Reconcile an estimate with the real token usage of a call.

        With no ``estimated_tokens``/``slot`` the estimate of the slot currently
        held by this task is used (0 outside a slot, i.e. a pure charge). The
        difference is debited (or credited) to the TPM bucket and to the
        ``current_tpm`` window. A slot is reconciled at most once.
        """
        actual = max(0, int(actual_tokens))
        if slot is None and estimated_tokens is None:
            slot = _current_slot.get()
        if slot is not None:
            if slot.reconciled:
                return
            slot.reconciled = True
            estimate = slot.estimated_tokens
        else:
            estimate = int(estimated_tokens or 0)
        delta = actual - estimate
        if delta == 0:
            return
        now = self._clock()
        self._tok_bucket.refill(now, factor=self._factor(now))
        self._tok_bucket.level = min(self._tok_bucket.capacity, self._tok_bucket.level - delta)
        self._prune(now)
        self._tokens.append((now, delta))

    async def call_with_backoff(
        self,
        fn: Callable[[], Awaitable[T]],
        *,
        max_attempts: int = 5,
        base: float = 1.0,
        cap: float = 60.0,
        estimated_tokens: int | None = None,
        retry_on: tuple[type[BaseException], ...] | None = None,
    ) -> T:
        """Call ``fn()`` and retry on ``ClaudeRateLimited`` with full-jitter backoff.

        The delay before retry ``n`` (zero-based) is uniform in
        ``[0, min(cap, base * 2**n))``; when the exception carries
        ``retry_after`` the delay is at least that long. While backing off, the
        whole limiter is paused (other callers wait too). When
        ``estimated_tokens`` is given each attempt runs inside :meth:`slot`.
        After ``max_attempts`` failed attempts the last exception is re-raised.
        """
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        errors = retry_on if retry_on is not None else (_rate_limited_exception(),)
        attempt = 0
        while True:
            try:
                if estimated_tokens is None:
                    return await fn()
                async with self.slot(estimated_tokens):
                    return await fn()
            except errors as exc:
                attempt += 1
                self.rate_limit_hits += 1
                if attempt >= max_attempts:
                    raise
                delay = full_jitter_delay(attempt - 1, base=base, cap=cap, rng=self._rng)
                retry_after = getattr(exc, "retry_after", None)
                if retry_after is not None:
                    delay = max(delay, float(retry_after))
                self._cooldown_until = max(self._cooldown_until, self._clock() + delay)
                await self._sleep(delay)

    def throttle(self, factor: float, seconds: float) -> None:
        """Scale RPM/TPM budgets by ``factor`` (0 < factor <= 1) for ``seconds``.

        Used for the Foreman's THROTTLE_DISPATCH action. Bucket levels are
        clipped to the reduced capacity immediately; full budgets come back
        once the window has passed. A new call replaces the previous throttle.
        """
        if not 0 < factor <= 1:
            raise ValueError("throttle factor must be in (0, 1]")
        if seconds < 0:
            raise ValueError("throttle seconds must be >= 0")
        now = self._clock()
        self._req_bucket.refill(now, factor=self._factor(now))
        self._tok_bucket.refill(now, factor=self._factor(now))
        self._throttle_factor = float(factor)
        self._throttle_until = now + float(seconds)
        self._req_bucket.level = min(self._req_bucket.level, self._req_bucket.capacity * factor)
        self._tok_bucket.level = min(self._tok_bucket.level, self._tok_bucket.capacity * factor)

    # ------------------------------------------------------------ introspection
    @property
    def in_flight(self) -> int:
        """Calls currently holding a slot."""
        return self._in_flight

    @property
    def peak_in_flight(self) -> int:
        """Highest number of calls ever in flight at once."""
        return self._peak_in_flight

    @property
    def throttle_factor(self) -> float:
        """The active throttle factor (1.0 when not throttled)."""
        return self._factor(self._clock())

    @property
    def is_throttled(self) -> bool:
        """True while a :meth:`throttle` window is active."""
        return self._factor(self._clock()) < 1.0

    def current_rpm(self) -> int:
        """Requests granted in the last 60 seconds."""
        self._prune(self._clock())
        return len(self._requests)

    def current_tpm(self) -> int:
        """Tokens charged (estimates plus reconciliations) in the last 60 seconds."""
        self._prune(self._clock())
        return max(0, sum(tokens for _, tokens in self._tokens))

    def snapshot(self) -> dict[str, Any]:
        """JSON-able view for telemetry and the Foreman."""
        return {
            "rpm_cap": self.rpm,
            "tpm_cap": self.tpm,
            "max_concurrent": self.max_concurrent,
            "in_flight": self.in_flight,
            "peak_in_flight": self.peak_in_flight,
            "current_rpm": self.current_rpm(),
            "current_tpm": self.current_tpm(),
            "throttle_factor": self.throttle_factor,
            "rate_limit_hits": self.rate_limit_hits,
        }
