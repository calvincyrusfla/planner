"""LINE_JAM limit: default ``max_retries + 1``; an explicit FOUNDRY_GATE_FAIL_QUARANTINE still wins."""

from __future__ import annotations

from typing import Any

import pytest

from foundry.config import AnomalyThresholds, FoundryConfig
from foundry.schemas import EscalationCategory, Severity
from foundry.shop_foreman import ShopForeman
from foundry.tests.test_shop_foreman import fail_gate, line, run, sized_task  # noqa: F401 - `line` is a fixture


def _foreman(line: dict[str, Any], **cfg: Any) -> ShopForeman:  # noqa: F811
    config = FoundryConfig(db_path=line["config"].db_path, **cfg)
    return ShopForeman(None, line["queue"], line["telemetry"], line["engine"], config)


def test_default_limit_is_max_retries_plus_one() -> None:
    assert FoundryConfig().thresholds.gate_fail_quarantine is None
    assert FoundryConfig().effective_gate_fail_quarantine() == FoundryConfig().max_retries + 1
    for retries in (1, 2, 3, 5):
        assert FoundryConfig(max_retries=retries).effective_gate_fail_quarantine() == retries + 1


def test_explicit_threshold_wins() -> None:
    cfg = FoundryConfig(max_retries=3, thresholds=AnomalyThresholds(gate_fail_quarantine=2))
    assert cfg.effective_gate_fail_quarantine() == 2
    env = FoundryConfig.from_env({"FOUNDRY_GATE_FAIL_QUARANTINE": "5", "FOUNDRY_MAX_RETRIES": "2"})
    assert env.thresholds.gate_fail_quarantine == 5
    assert env.effective_gate_fail_quarantine() == 5
    unset = FoundryConfig.from_env({"FOUNDRY_MAX_RETRIES": "2"})
    assert unset.thresholds.gate_fail_quarantine is None
    assert unset.effective_gate_fail_quarantine() == 3


def test_invalid_explicit_threshold_still_rejected() -> None:
    with pytest.raises(ValueError):
        FoundryConfig.from_env({"FOUNDRY_GATE_FAIL_QUARANTINE": "0"})


def test_foreman_never_quarantines_a_ticket_with_attempts_left(line: dict[str, Any]) -> None:  # noqa: F811
    sized_task(line["queue"], "F-401", "alpha", 1)
    foreman = _foreman(line, max_retries=3)
    for _ in range(3):  # attempts 0..2 failed; attempt 3 is still to come
        fail_gate(line, "F-401")
        assert run(foreman.evaluate_line_anomaly()) is None
    fail_gate(line, "F-401")  # the last allowed attempt failed too
    alert = run(foreman.evaluate_line_anomaly())
    assert alert is not None and alert.category == EscalationCategory.LINE_JAM
    assert alert.severity == Severity.HIGH
    assert "failed its gates 4 times (limit 4)" in alert.action_parameters["reason"]
    assert alert.metrics_snapshot.gate_failure_count == 4


def test_foreman_follows_max_retries(line: dict[str, Any]) -> None:  # noqa: F811
    sized_task(line["queue"], "F-402", "alpha", 1)
    foreman = _foreman(line, max_retries=1)
    fail_gate(line, "F-402")
    assert run(foreman.evaluate_line_anomaly()) is None
    fail_gate(line, "F-402")
    alert = run(foreman.evaluate_line_anomaly())
    assert alert is not None and "(limit 2)" in alert.action_parameters["reason"]


def test_explicit_limit_applies_in_the_foreman(line: dict[str, Any]) -> None:  # noqa: F811
    sized_task(line["queue"], "F-403", "alpha", 1)
    foreman = _foreman(line, max_retries=5, thresholds=AnomalyThresholds(gate_fail_quarantine=2))
    fail_gate(line, "F-403")
    assert run(foreman.evaluate_line_anomaly()) is None
    fail_gate(line, "F-403")
    alert = run(foreman.evaluate_line_anomaly())
    assert alert is not None and "(limit 2)" in alert.action_parameters["reason"]
