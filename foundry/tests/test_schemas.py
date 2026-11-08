"""Unit tests for foundry.schemas (the shared T-010 contract)."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from foundry import schemas
from foundry.schemas import (
    BUILDERS_FOR,
    BuilderSubmission,
    ClaudeResult,
    CodeSnippet,
    ContextSlice,
    DeterministicGateResult,
    ForemanEscalation,
    LineDirective,
    ReviewerVerdict,
    SubtaskAllocation,
    TaskAssignment,
    TaskSizingDirective,
    TaskState,
    new_alert_id,
)


# --------------------------------------------------------------------------
# Factories
# --------------------------------------------------------------------------


def _subtasks(n: int) -> list[dict[str, Any]]:
    return [
        {
            "subtask_id": f"T-1.{i}",
            "title": f"Subtask {i}",
            "acceptance_criteria": [f"criterion {i}"],
            "allowed_files": [f"pkg/mod_{i}.py"],
        }
        for i in range(1, n + 1)
    ]


def _sizing(complexity: str = "M", n: int | None = None, **kw: Any) -> dict[str, Any]:
    n = BUILDERS_FOR[complexity] if n is None else n
    data: dict[str, Any] = {
        "task_id": "T-1",
        "team_id": "alpha",
        "complexity": complexity,
        "allocated_builders": n,
        "rationale": "two independent files",
        "subtasks": _subtasks(n),
    }
    data.update(kw)
    return data


def _snippet(lines: int = 3, start: int = 1) -> dict[str, Any]:
    return {
        "path": "pkg/mod.py",
        "start_line": start,
        "end_line": start + lines - 1,
        "text": "\n".join(f"line {i}" for i in range(lines)),
    }


def _slice(**kw: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "objective": "Add a parser. Keep it small.",
        "acceptance_criteria": ["parses input"],
        "snippets": [_snippet()],
        "source_line_count": 120,
    }
    data.update(kw)
    return data


def _assignment(**kw: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "task_id": "T-1",
        "team_id": "alpha",
        "builder_id": "alpha-2",
        "subtask_id": "T-1.1",
        "acceptance_criteria": ["parses input"],
        "allowed_files": ["pkg/mod.py"],
        "context": _slice(),
        "attempt": 0,
        "fix_hint": None,
    }
    data.update(kw)
    return data


def _submission(**kw: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "task_id": "T-1",
        "subtask_id": "T-1.1",
        "builder_id": "alpha-2",
        "status": "ready_for_review",
        "modified_files": ["pkg/mod.py"],
        "diff": "--- a/pkg/mod.py\n+++ b/pkg/mod.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n",
        "self_check": {"tests": True},
        "blocker_notes": None,
    }
    data.update(kw)
    return data


def _gate(**kw: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "task_id": "T-1",
        "subtask_id": "T-1.1",
        "passed": True,
        "check_type": "syntax",
        "stdout": "",
        "stderr": "",
        "duration_seconds": 0.01,
    }
    data.update(kw)
    return data


def _verdict(**kw: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "task_id": "T-1",
        "reviewer_id": "alpha-6",
        "verdict": "REVISE",
        "directives": [{"path": "pkg/mod.py", "line": 3, "instruction": "rename x"}],
        "retry_count": 1,
    }
    data.update(kw)
    return data


def _escalation(**kw: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "alert_id": "ALT-20261107-001",
        "timestamp": "2026-11-07T12:00:00Z",
        "severity": "HIGH",
        "category": "LINE_JAM",
        "target_entity": {"team_id": "alpha", "task_id": "T-1"},
        "metrics_snapshot": {
            "queue_depth": 3,
            "dwell_seconds": 12.5,
            "gate_failure_count": 2,
            "current_tpm": 1500,
        },
        "diagnosis": "Task T-1 failed its gates twice.",
        "prescribed_action": "QUARANTINE_TASK",
        "action_parameters": {"task_id": "T-1"},
    }
    data.update(kw)
    return data


def _claude_result(**kw: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "text": "ok",
        "input_tokens": 10,
        "output_tokens": 5,
        "duration_seconds": 1.5,
        "cost_usd": None,
        "is_error": False,
        "error": None,
    }
    data.update(kw)
    return data


ALL_VALID: list[tuple[type[BaseModel], Any]] = [
    (SubtaskAllocation, lambda: _subtasks(1)[0]),
    (TaskSizingDirective, _sizing),
    (CodeSnippet, _snippet),
    (ContextSlice, _slice),
    (TaskAssignment, _assignment),
    (BuilderSubmission, _submission),
    (DeterministicGateResult, _gate),
    (LineDirective, lambda: {"path": "a.py", "line": 1, "instruction": "fix"}),
    (ReviewerVerdict, _verdict),
    (ForemanEscalation, _escalation),
    (ClaudeResult, _claude_result),
]
IDS = [m.__name__ for m, _ in ALL_VALID]


# --------------------------------------------------------------------------
# Generic: valid payloads, extra=forbid, JSON round-trip
# --------------------------------------------------------------------------


@pytest.mark.parametrize(("model", "factory"), ALL_VALID, ids=IDS)
def test_valid_payload_accepted(model: type[BaseModel], factory: Any) -> None:
    obj = model.model_validate(factory())
    assert isinstance(obj, model)


@pytest.mark.parametrize(("model", "factory"), ALL_VALID, ids=IDS)
def test_extra_fields_forbidden(model: type[BaseModel], factory: Any) -> None:
    data = factory()
    data["unexpected_field"] = 1
    with pytest.raises(ValidationError):
        model.model_validate(data)


@pytest.mark.parametrize(("model", "factory"), ALL_VALID, ids=IDS)
def test_json_round_trip(model: type[BaseModel], factory: Any) -> None:
    obj = model.model_validate(factory())
    dumped = obj.model_dump_json()
    again = model.model_validate_json(dumped)
    assert again == obj
    # Also through a plain dict / json module.
    again2 = model.model_validate(json.loads(dumped))
    assert again2 == obj


# ClaudeResult is the only contract whose fields all have defaults; it is
# covered by test_claude_result_defaults instead.
WITH_REQUIRED = [(m, fac) for m, fac in ALL_VALID if m is not ClaudeResult]


@pytest.mark.parametrize(
    ("model", "factory"), WITH_REQUIRED, ids=[m.__name__ for m, _ in WITH_REQUIRED]
)
def test_missing_required_field_rejected(model: type[BaseModel], factory: Any) -> None:
    data = factory()
    required = [n for n, f in model.model_fields.items() if f.is_required()]
    assert required, f"{model.__name__} unexpectedly has no required fields"
    for name in required:
        partial = dict(data)
        partial.pop(name)
        with pytest.raises(ValidationError):
            model.model_validate(partial)


def test_all_models_forbid_extra_in_config() -> None:
    for model, _ in ALL_VALID:
        assert model.model_config.get("extra") == "forbid", model.__name__


# --------------------------------------------------------------------------
# Enums / literals
# --------------------------------------------------------------------------


def test_task_state_values() -> None:
    assert {s.value for s in TaskState} == {
        "backlog",
        "sizing",
        "building",
        "validating",
        "reviewing",
        "complete",
        "quarantined",
        "failed",
    }
    assert issubclass(TaskState, str)
    assert TaskState("building") == "building"


def test_task_state_unknown_rejected() -> None:
    with pytest.raises(ValueError):
        TaskState("shipping")


def test_sizing_table() -> None:
    assert BUILDERS_FOR == {"S": 1, "M": 2, "L": 5, "XL": 5}


def test_complexity_literal_rejects_unknown() -> None:
    with pytest.raises(ValidationError):
        TaskSizingDirective.model_validate(_sizing("M") | {"complexity": "XXL"})


@pytest.mark.parametrize("status", ["ready_for_review", "blocked"])
def test_submission_status_values(status: str) -> None:
    assert BuilderSubmission.model_validate(_submission(status=status)).status == status


def test_submission_status_invalid() -> None:
    with pytest.raises(ValidationError):
        BuilderSubmission.model_validate(_submission(status="done"))


@pytest.mark.parametrize("check", ["syntax", "linter", "typecheck", "tests", "schema"])
def test_gate_check_values(check: str) -> None:
    assert DeterministicGateResult.model_validate(_gate(check_type=check)).check_type == check


def test_gate_check_invalid() -> None:
    with pytest.raises(ValidationError):
        DeterministicGateResult.model_validate(_gate(check_type="vibes"))


@pytest.mark.parametrize("verdict", ["APPROVED", "REVISE", "REJECTED"])
def test_verdict_values(verdict: str) -> None:
    assert ReviewerVerdict.model_validate(_verdict(verdict=verdict)).verdict == verdict


@pytest.mark.parametrize("verdict", ["approved", "LGTM", ""])
def test_verdict_invalid(verdict: str) -> None:
    with pytest.raises(ValidationError):
        ReviewerVerdict.model_validate(_verdict(verdict=verdict))


@pytest.mark.parametrize("sev", ["LOW", "MEDIUM", "HIGH", "CRITICAL"])
def test_escalation_severity_values(sev: str) -> None:
    assert ForemanEscalation.model_validate(_escalation(severity=sev)).severity == sev


@pytest.mark.parametrize(
    "cat",
    [
        "LINE_JAM",
        "REVIEW_BOTTLENECK",
        "TOKEN_VELOCITY_SPIKE",
        "AGENT_STALLED",
        "CAPACITY_REBALANCE",
    ],
)
def test_escalation_category_values(cat: str) -> None:
    assert ForemanEscalation.model_validate(_escalation(category=cat)).category == cat


@pytest.mark.parametrize(
    "action",
    [
        "RESET_BUILDER_CONTEXT",
        "QUARANTINE_TASK",
        "PROMOTE_PEER_REVIEWER",
        "THROTTLE_DISPATCH",
        "RESIZE_ALLOCATION",
        "NONE",
    ],
)
def test_escalation_action_values(action: str) -> None:
    obj = ForemanEscalation.model_validate(_escalation(prescribed_action=action))
    assert obj.prescribed_action == action


@pytest.mark.parametrize(
    ("field", "value"),
    [("severity", "URGENT"), ("category", "FIRE"), ("prescribed_action", "REBOOT")],
)
def test_escalation_bad_enum(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        ForemanEscalation.model_validate(_escalation(**{field: value}))


# --------------------------------------------------------------------------
# TaskSizingDirective
# --------------------------------------------------------------------------


@pytest.mark.parametrize("complexity", ["S", "M", "L", "XL"])
def test_sizing_matches_table(complexity: str) -> None:
    d = TaskSizingDirective.model_validate(_sizing(complexity))
    assert d.allocated_builders == BUILDERS_FOR[complexity]
    assert len(d.subtasks) == d.allocated_builders
    assert d.override_reason is None


@pytest.mark.parametrize(("complexity", "n"), [("S", 2), ("M", 1), ("M", 5), ("L", 3), ("XL", 1)])
def test_sizing_mismatch_without_override_rejected(complexity: str, n: int) -> None:
    with pytest.raises(ValidationError):
        TaskSizingDirective.model_validate(_sizing(complexity, n))


@pytest.mark.parametrize(("complexity", "n"), [("S", 2), ("M", 3), ("L", 4)])
def test_sizing_mismatch_with_override_accepted(complexity: str, n: int) -> None:
    d = TaskSizingDirective.model_validate(
        _sizing(complexity, n, override_reason="files are tightly coupled")
    )
    assert d.allocated_builders == n


@pytest.mark.parametrize("n", [0, 6, -1])
def test_sizing_builders_out_of_range(n: int) -> None:
    with pytest.raises(ValidationError):
        TaskSizingDirective.model_validate(
            _sizing("M", n, override_reason="out of range anyway", subtasks=_subtasks(max(n, 0)))
        )


@pytest.mark.parametrize("count", [1, 3])
def test_sizing_subtask_count_must_match(count: int) -> None:
    with pytest.raises(ValidationError):
        TaskSizingDirective.model_validate(_sizing("M", subtasks=_subtasks(count)))


def test_subtask_requires_list_fields() -> None:
    bad = _subtasks(1)[0] | {"acceptance_criteria": "not a list"}
    with pytest.raises(ValidationError):
        SubtaskAllocation.model_validate(bad)


# --------------------------------------------------------------------------
# ContextSlice / CodeSnippet
# --------------------------------------------------------------------------


def test_snippet_50_lines_ok() -> None:
    s = CodeSnippet.model_validate(_snippet(50))
    assert s.text.count("\n") + 1 == 50


def test_snippet_51_lines_rejected() -> None:
    with pytest.raises(ValidationError):
        CodeSnippet.model_validate(_snippet(51))


def test_slice_with_oversize_snippet_rejected() -> None:
    with pytest.raises(ValidationError):
        ContextSlice.model_validate(_slice(snippets=[_snippet(10), _snippet(60, start=100)]))


def test_slice_many_small_snippets_ok() -> None:
    sl = ContextSlice.model_validate(
        _slice(snippets=[_snippet(40, start=1 + 100 * i) for i in range(3)])
    )
    assert len(sl.snippets) == 3


def test_slice_objective_two_sentences_ok() -> None:
    ContextSlice.model_validate(_slice(objective="Do one thing. Then another."))


def test_slice_objective_three_sentences_rejected() -> None:
    with pytest.raises(ValidationError):
        ContextSlice.model_validate(_slice(objective="One. Two. Three."))


def test_slice_source_line_count_int() -> None:
    with pytest.raises(ValidationError):
        ContextSlice.model_validate(_slice(source_line_count="many"))


# --------------------------------------------------------------------------
# TaskAssignment / ReviewerVerdict retry caps
# --------------------------------------------------------------------------


@pytest.mark.parametrize("attempt", [0, 1, 2])
def test_assignment_attempt_ok(attempt: int) -> None:
    assert TaskAssignment.model_validate(_assignment(attempt=attempt)).attempt == attempt


@pytest.mark.parametrize("attempt", [-1, 3])
def test_assignment_attempt_out_of_range(attempt: int) -> None:
    with pytest.raises(ValidationError):
        TaskAssignment.model_validate(_assignment(attempt=attempt))


def test_assignment_nested_context_validated() -> None:
    with pytest.raises(ValidationError):
        TaskAssignment.model_validate(_assignment(context=_slice(snippets=[_snippet(51)])))


def test_assignment_fix_hint_optional() -> None:
    a = TaskAssignment.model_validate(_assignment(fix_hint="mod.py:3 NameError: y"))
    assert a.fix_hint == "mod.py:3 NameError: y"


@pytest.mark.parametrize("n", [0, 1, 2])
def test_verdict_retry_count_ok(n: int) -> None:
    assert ReviewerVerdict.model_validate(_verdict(retry_count=n)).retry_count == n


@pytest.mark.parametrize("n", [-1, 3, 10])
def test_verdict_retry_count_over_cap(n: int) -> None:
    with pytest.raises(ValidationError):
        ReviewerVerdict.model_validate(_verdict(retry_count=n))


def test_verdict_directives_typed() -> None:
    with pytest.raises(ValidationError):
        ReviewerVerdict.model_validate(_verdict(directives=[{"path": "a.py"}]))


def test_submission_self_check_bool_map() -> None:
    with pytest.raises(ValidationError):
        BuilderSubmission.model_validate(_submission(self_check={"tests": "maybe"}))


# --------------------------------------------------------------------------
# ForemanEscalation
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "alert_id",
    ["ALT-20261107-001", "ALT-00000000-000", "ALT-20991231-999"],
)
def test_alert_id_valid(alert_id: str) -> None:
    assert ForemanEscalation.model_validate(_escalation(alert_id=alert_id)).alert_id == alert_id


@pytest.mark.parametrize(
    "alert_id",
    [
        "ALT-2026110-001",
        "ALT-20261107-01",
        "ALT-20261107-0001",
        "alt-20261107-001",
        "ALR-20261107-001",
        "ALT-20261107-001x",
        "xALT-20261107-001",
        "ALT-2026-11-07-001",
        "",
    ],
)
def test_alert_id_invalid(alert_id: str) -> None:
    with pytest.raises(ValidationError):
        ForemanEscalation.model_validate(_escalation(alert_id=alert_id))


@pytest.mark.parametrize(
    "ts",
    [
        "2026-11-07T12:00:00Z",
        "2026-11-07T12:00:00+00:00",
        "2026-11-07T12:00:00.123456+00:00",
    ],
)
def test_timestamp_iso_utc_ok(ts: str) -> None:
    ForemanEscalation.model_validate(_escalation(timestamp=ts))


@pytest.mark.parametrize(
    "ts",
    [
        "yesterday",
        "2026-13-07T12:00:00Z",
        "07/11/2026 12:00",
        "",
        "2026-11-07T12:00:00+02:00",  # not UTC
        "2026-11-07T12:00:00",  # naive: no timezone
    ],
)
def test_timestamp_invalid(ts: str) -> None:
    with pytest.raises(ValidationError):
        ForemanEscalation.model_validate(_escalation(timestamp=ts))


@pytest.mark.parametrize(
    "target",
    [{"team_id": "alpha"}, {"task_id": "T-1"}, {"agent_id": "alpha-3"},
     {"team_id": "alpha", "task_id": "T-1", "agent_id": "alpha-3"}],
)
def test_target_entity_ok(target: dict[str, str]) -> None:
    ForemanEscalation.model_validate(_escalation(target_entity=target))


@pytest.mark.parametrize(
    "target",
    [{}, {"team_id": None, "task_id": None, "agent_id": None}],
)
def test_target_entity_needs_one(target: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ForemanEscalation.model_validate(_escalation(target_entity=target))


def test_target_entity_extra_forbidden() -> None:
    with pytest.raises(ValidationError):
        ForemanEscalation.model_validate(
            _escalation(target_entity={"team_id": "alpha", "planet": "mars"})
        )


def test_metrics_snapshot_allows_extra_keys() -> None:
    snap = _escalation()["metrics_snapshot"] | {"idle_builders": 4}
    obj = ForemanEscalation.model_validate(_escalation(metrics_snapshot=snap))
    dumped = obj.model_dump(mode="json")
    assert dumped["metrics_snapshot"]["idle_builders"] == 4


@pytest.mark.parametrize("missing", ["queue_depth", "dwell_seconds", "gate_failure_count", "current_tpm"])
def test_metrics_snapshot_requires_core_keys(missing: str) -> None:
    snap = dict(_escalation()["metrics_snapshot"])
    snap.pop(missing)
    with pytest.raises(ValidationError):
        ForemanEscalation.model_validate(_escalation(metrics_snapshot=snap))


def test_diagnosis_two_sentences_ok() -> None:
    ForemanEscalation.model_validate(_escalation(diagnosis="Reviews pile up. Promote a peer."))


def test_diagnosis_three_sentences_rejected() -> None:
    with pytest.raises(ValidationError):
        ForemanEscalation.model_validate(_escalation(diagnosis="One. Two. Three."))


def test_escalation_json_matches_user_spec_keys() -> None:
    obj = ForemanEscalation.model_validate(_escalation())
    dumped = json.loads(obj.model_dump_json())
    assert set(dumped) == {
        "alert_id",
        "timestamp",
        "severity",
        "category",
        "target_entity",
        "metrics_snapshot",
        "diagnosis",
        "prescribed_action",
        "action_parameters",
    }
    assert re.fullmatch(r"ALT-\d{8}-\d{3}", dumped["alert_id"])


# --------------------------------------------------------------------------
# new_alert_id
# --------------------------------------------------------------------------


def test_new_alert_id_format() -> None:
    now = datetime(2026, 11, 7, 9, 30, tzinfo=timezone.utc)
    assert new_alert_id(1, now) == "ALT-20261107-001"
    assert new_alert_id(42, now) == "ALT-20261107-042"
    assert new_alert_id(999, now) == "ALT-20261107-999"


def test_new_alert_id_usable_in_escalation() -> None:
    aid = new_alert_id(7, datetime(2026, 1, 2, tzinfo=timezone.utc))
    ForemanEscalation.model_validate(_escalation(alert_id=aid))


# --------------------------------------------------------------------------
# ClaudeResult
# --------------------------------------------------------------------------


def test_claude_result_error_shape() -> None:
    r = ClaudeResult.model_validate(
        _claude_result(is_error=True, error="usage limit reached", cost_usd=0.01)
    )
    assert r.is_error and r.error == "usage limit reached"


def test_claude_result_tokens_int() -> None:
    with pytest.raises(ValidationError):
        ClaudeResult.model_validate(_claude_result(input_tokens="lots"))


def test_module_exports_complexity() -> None:
    assert hasattr(schemas, "Complexity")
    assert hasattr(schemas, "GateCheck")


def test_claude_result_defaults() -> None:
    r = ClaudeResult()
    assert r.text == ""
    assert (r.input_tokens, r.output_tokens, r.cache_read_tokens) == (0, 0, 0)
    assert r.duration_seconds == 0.0
    assert r.cost_usd is None
    assert r.is_error is False and r.error is None


def test_claude_result_cache_read_tokens() -> None:
    r = ClaudeResult.model_validate(_claude_result(cache_read_tokens=1200))
    assert r.cache_read_tokens == 1200
    # Tracked separately: not folded into input_tokens.
    assert r.input_tokens == 10
    assert r.total_tokens == 15  # uncached input + output only
    again = ClaudeResult.model_validate_json(r.model_dump_json())
    assert again.cache_read_tokens == 1200


@pytest.mark.parametrize("bad", [-1, "lots", 1.5])
def test_claude_result_cache_read_tokens_invalid(bad: Any) -> None:
    with pytest.raises(ValidationError):
        ClaudeResult.model_validate(_claude_result(cache_read_tokens=bad))
