"""BuilderWorker._context_level honours config.max_context_level."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from foundry.builder_worker import BuilderWorker

TICKET = SimpleNamespace(task_id="T-1")


def _worker(max_level: Any = None, task_level: int | None = None, config: bool = True) -> Any:
    queue = SimpleNamespace()
    if task_level is not None:
        queue.task_context_level = lambda task_id: task_level
    worker = SimpleNamespace(queue=queue)
    if config:
        worker.config = SimpleNamespace(max_context_level=max_level) if max_level is not None else SimpleNamespace()
    else:
        worker.config = None
    return worker


def _level(worker: Any, attempt: int) -> int:
    return BuilderWorker._context_level(worker, TICKET, attempt)


def test_without_config_levels_are_unchanged() -> None:
    worker = _worker(config=False)
    assert [_level(worker, a) for a in (0, 1, 2, 5)] == [1, 2, 3, 3]


def test_cap_one_limits_every_attempt() -> None:
    worker = _worker(1, task_level=3)
    assert all(_level(worker, a) == 1 for a in (0, 1, 2))
    assert all(_level(_worker(1), a) == 1 for a in (0, 1, 2))


def test_cap_two() -> None:
    assert _level(_worker(2), 2) == 2
    assert _level(_worker(2, task_level=3), 2) == 2


@pytest.mark.parametrize("value,expected", [(0, 3), (9, 3), (-4, 1)])
def test_invalid_values_are_clamped(value: int, expected: int) -> None:
    assert _level(_worker(value), 5) == expected
