"""Tests for foundry.solution_cache (RetryPolicy)."""

from __future__ import annotations

import pytest

from foundry.solution_cache import RetryPolicy


def test_retry_policy_default() -> None:
    p = RetryPolicy()
    assert p.max_retries == 2
    assert [p.should_retry(a) for a in (0, 1, 2)] == [True, True, False]
    assert [p.next_attempt(a) for a in (0, 1, 2)] == [1, 2, None]
    assert p.exhausted(2) and not p.exhausted(1)


def test_retry_policy_custom_and_invalid() -> None:
    assert RetryPolicy(max_retries=0).next_attempt(0) is None
    with pytest.raises(ValueError):
        RetryPolicy(max_retries=-1)
    with pytest.raises(ValueError):
        RetryPolicy().should_retry(-1)
