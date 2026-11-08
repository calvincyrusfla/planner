"""Sizing outcomes: what a finished task actually cost, versus what the Foreman predicted.

When a task completes (approved, patch written) or is quarantined, the floor
manager records one row in the queue's ``sizing_outcomes`` table (see
:meth:`foundry.orchestrator_queue.FoundryQueue.record_sizing_outcome`). The
helpers here are pure: they turn such rows into an *actual* size and into
accuracy figures the Foreman uses to calibrate its sizing.

Predicted sizes
---------------
Each row keeps two predictions: ``predicted`` is the size that was applied
(after any calibration shift) and ``raw_predicted`` is the Foreman's own size
*before* calibration. Accuracy and calibration compare ``raw_predicted`` with
the actual size (falling back to ``predicted`` for rows without it), so the
calibration never learns from its own corrections (which would oscillate).

Actual size (:func:`actual_complexity`)
---------------------------------------
``effort = claude_calls + retries``

* ``claude_calls`` - Claude builder + reviewer calls recorded for the task.
  When telemetry recorded none (simulated runs without token telemetry),
  ``2 * attempts`` is used (one build plus one review per attempt).
* ``retries`` - builder attempts beyond the first per subtask
  (``attempts - subtasks``, never negative). Each retry already shows up in
  ``claude_calls``; the extra point marks the rework itself.

Wall time is recorded but deliberately NOT part of the effort: it measures
queueing, rate limits and the machine as much as the task.

Thresholds: ``S`` when ``effort <= 3``, ``M`` when ``<= 7``, ``L`` when
``<= 20``, else ``XL``. A clean run (no retries, 2 calls per subtask, one
subtask per builder from :data:`foundry.schemas.BUILDERS_FOR`) scores:
S = 2, M = 4, L = 10, XL = 10. L and XL get the same crew (5 builders), so
effort cannot tell them apart: :func:`same_size` treats L and XL as equal and
:func:`sizing_accuracy` counts L-vs-XL as exact.

Quarantines: ``quarantine_kind`` is ``"retries_exhausted"`` (the retry cap
was hit: the task needed more than it got, so its actual size is at least
``L``) or ``"other"`` (an escalation, a sizing problem ...). ``"other"`` rows
are recorded with ``excluded_from_calibration`` set and are skipped by
:func:`sizing_accuracy`: they say nothing about the size.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

__all__ = [
    "SIZES",
    "EFFORT_THRESHOLDS",
    "effort_score",
    "actual_complexity",
    "same_size",
    "size_delta",
    "predicted_size",
    "sizing_accuracy",
]

SIZES: tuple[str, ...] = ("S", "M", "L", "XL")

#: Upper bound (inclusive) of the effort score for each size below XL.
EFFORT_THRESHOLDS: tuple[tuple[str, float], ...] = (("S", 3.0), ("M", 7.0), ("L", 20.0))


def _num(outcome: Mapping[str, Any], key: str) -> float:
    try:
        return max(0.0, float(outcome.get(key) or 0))
    except (TypeError, ValueError):
        return 0.0


def _size(value: Any) -> str | None:
    text = str(value or "").strip().upper()
    return text if text in SIZES else None


def effort_score(outcome: Mapping[str, Any]) -> float:
    """The effort of one outcome row: Claude calls + retries (see the module docstring)."""
    attempts = _num(outcome, "attempts")
    subtasks = _num(outcome, "subtasks")
    calls = _num(outcome, "claude_calls") or 2.0 * attempts
    retries = max(0.0, attempts - subtasks) if subtasks else 0.0
    return calls + retries


def actual_complexity(outcome: Mapping[str, Any]) -> str:
    """``"S"|"M"|"L"|"XL"``: the size the task turned out to be, from its effort."""
    score = effort_score(outcome)
    size = "XL"
    for name, limit in EFFORT_THRESHOLDS:
        if score <= limit:
            size = name
            break
    if (str(outcome.get("final_state") or "") == "quarantined"
            and outcome.get("quarantine_kind") == "retries_exhausted"
            and SIZES.index(size) < SIZES.index("L")):
        size = "L"
    return size


def size_delta(predicted: str, actual: str) -> int:
    """``actual - predicted`` in size steps, with L and XL counted as the same size."""
    if same_size(predicted, actual):
        return 0
    return SIZES.index(actual) - SIZES.index(predicted)


def same_size(a: Any, b: Any) -> bool:
    """True when two sizes match; L and XL (same crew, same clean effort) match each other."""
    a, b = _size(a), _size(b)
    if a is None or b is None:
        return False
    return a == b or {a, b} == {"L", "XL"}


def predicted_size(outcome: Mapping[str, Any]) -> str | None:
    """The Foreman's size before calibration (``raw_predicted``), else the applied one."""
    return _size(outcome.get("raw_predicted")) or _size(outcome.get("predicted"))


def sizing_accuracy(outcomes: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """How often the (raw) predicted size matched the actual one.

    Returns ``{n, exact, under, over, by_predicted: {size: {n, exact, under, over}}}``.
    ``under`` = the task was actually bigger than predicted (under-sized),
    ``over`` = actually smaller; L vs XL counts as exact. Rows without a valid
    predicted size, and rows marked ``excluded_from_calibration``, are skipped.
    An outcome's ``actual`` key is used when present, else :func:`actual_complexity`.
    """
    def blank() -> dict[str, int]:
        return {"n": 0, "exact": 0, "under": 0, "over": 0}

    total = blank()
    by_predicted: dict[str, dict[str, int]] = {}
    for row in outcomes:
        if row.get("excluded_from_calibration"):
            continue
        predicted = predicted_size(row)
        if predicted is None:
            continue
        actual = _size(row.get("actual")) or actual_complexity(row)
        diff = size_delta(predicted, actual)
        kind = "exact" if diff == 0 else ("under" if diff > 0 else "over")
        bucket = by_predicted.setdefault(predicted, blank())
        for rec in (total, bucket):
            rec["n"] += 1
            rec[kind] += 1
    return {**total, "by_predicted": {s: by_predicted[s] for s in SIZES if s in by_predicted}}
