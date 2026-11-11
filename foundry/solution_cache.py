"""Retry policy for the Foundry gates (the exact-match solution cache was removed)."""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["RetryPolicy"]


@dataclass(frozen=True)
class RetryPolicy:
    """Gate/review retry budget: attempts are numbered ``0..max_retries``.

    With the default ``max_retries=2`` a ticket is built at most three times
    (attempt 0, 1, 2); a failure on attempt 2 means quarantine.
    """

    max_retries: int = 2

    def __post_init__(self) -> None:
        if self.max_retries < 0:
            raise ValueError("max_retries must be >= 0")

    def should_retry(self, attempt: int) -> bool:
        """True if a failure on ``attempt`` may be retried."""
        if attempt < 0:
            raise ValueError("attempt must be >= 0")
        return attempt < self.max_retries

    def next_attempt(self, attempt: int) -> int | None:
        """Attempt number to run after a failure on ``attempt``, or ``None`` (quarantine)."""
        return attempt + 1 if self.should_retry(attempt) else None

    def exhausted(self, attempt: int) -> bool:
        """True if a failure on ``attempt`` must quarantine the task."""
        return not self.should_retry(attempt)
