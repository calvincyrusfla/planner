"""Sizing outcomes: what a finished task actually cost, versus what the Foreman predicted.

When a task completes (approved, patch written) or is quarantined, the floor
manager records one row in the queue's ``sizing_outcomes`` table (see
:meth:`foundry.orchestrator_queue.FoundryQueue.record_sizing_outcome`). The
helpers here are pure: they turn such rows into an *actual* size and into
accuracy figures the Foreman uses to calibrate its sizing.

Actual size thresholds (:func:`actual_complexity`)
--------------------------------------------------
``effort = claude_calls + retries + wall_time_s / 600``

* ``claude_calls`` - Claude builder + reviewer calls recorded for the task.
  When telemetry recorded none (simulated runs without token telemetry),
  ``2 * attempts`` is used (one build plus one review per attempt).
* ``retries`` - builder attempts beyond the first per subtask
  (``attempts - subtasks``, never negative). Each retry already shows up in
  ``claude_calls``; the extra point marks the rework itself.
* ``wall_time_s / 600`` - one point per 10 minutes of wall time.

Then ``S`` when ``effort <= 3``, ``M`` when ``<= 7``, ``L`` when ``<= 14``,
else ``XL``. A clean one-subtask task (1 build + 1 review) is S, a clean
3-subtask task (6 calls) is M, 9 Claude calls is L. A quarantined task is at
least ``L``: it needed more than it was given.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

__all__ = ["SIZES", "EFFORT_THRESHOLDS", "effort_score", "actual_complexity", "sizing_accuracy"]

SIZES: tuple[str, ...] = ("S", "M", "L", "XL")

#: Upper bound (inclusive) of the effort score for each size below XL.
EFFORT_THRESHOLDS: tuple[tuple[str, float], ...] = (("S", 3.0), ("M", 7.0), ("L", 14.0))

#: Seconds of wall time worth one effort point.
SECONDS_PER_POINT = 600.0


def _num(outcome: Mapping[str, Any], key: str) -> float:
    try:
        return max(0.0, float(outcome.get(key) or 0))
    except (TypeError, ValueError):
        return 0.0


def effort_score(outcome: Mapping[str, Any]) -> float:
    """The weighted effort of one outcome row (see the module docstring)."""
    attempts = _num(outcome, "attempts")
    subtasks = _num(outcome, "subtasks")
    calls = _num(outcome, "claude_calls") or 2.0 * attempts
    retries = max(0.0, attempts - subtasks) if subtasks else 0.0
    return calls + retries + _num(outcome, "wall_time_s") / SECONDS_PER_POINT


def actual_complexity(outcome: Mapping[str, Any]) -> str:
    """``"S"|"M"|"L"|"XL"``: the size the task turned out to be, from its effort."""
    score = effort_score(outcome)
    size = "XL"
    for name, limit in EFFORT_THRESHOLDS:
        if score <= limit:
            size = name
            break
    if str(outcome.get("final_state") or "") == "quarantined" and SIZES.index(size) < SIZES.index("L"):
        size = "L"
    return size


def _size(value: Any) -> str | None:
    text = str(value or "").strip().upper()
    return text if text in SIZES else None


def sizing_accuracy(outcomes: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """How often the predicted size matched the actual one.

    Returns ``{n, exact, under, over, by_predicted: {size: {n, exact, under, over}}}``.
    ``under`` = the task was actually bigger than predicted (under-sized),
    ``over`` = actually smaller. Rows without a valid predicted size are skipped.
    An outcome's ``actual`` key is used when present, else :func:`actual_complexity`.
    """
    def blank() -> dict[str, int]:
        return {"n": 0, "exact": 0, "under": 0, "over": 0}

    total = blank()
    by_predicted: dict[str, dict[str, int]] = {}
    for row in outcomes:
        predicted = _size(row.get("predicted"))
        if predicted is None:
            continue
        actual = _size(row.get("actual")) or actual_complexity(row)
        diff = SIZES.index(actual) - SIZES.index(predicted)
        kind = "exact" if diff == 0 else ("under" if diff > 0 else "over")
        bucket = by_predicted.setdefault(predicted, blank())
        for rec in (total, bucket):
            rec["n"] += 1
            rec[kind] += 1
    return {**total, "by_predicted": {s: by_predicted[s] for s in SIZES if s in by_predicted}}
