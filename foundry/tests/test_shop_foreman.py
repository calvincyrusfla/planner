"""Unit tests for foundry.shop_foreman (simulated Gemma only, never a real Ollama)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from foundry.config import AnomalyThresholds, FoundryConfig
from foundry.context_engine import ContextEngine
from foundry.gemma_client import GemmaRequest, SimulatedGemmaClient
from foundry.orchestrator_queue import FoundryQueue
from foundry.schemas import (
    EscalationCategory,
    ForemanEscalation,
    PrescribedAction,
    Severity,
    SubtaskAllocation,
    TaskSizingDirective,
)
from foundry.shop_foreman import (
    FOREMAN_TOOLS,
    MAX_TOOL_ROUNDS,
    ShopForeman,
    diff_stats,
    heuristic_complexity,
)
from foundry.telemetry import Telemetry

PYTEST_TRACE = """\
============================= FAILURES =============================
___________________________ test_add ___________________________

    def test_add():
>       assert add(1, 2) == 4
E       assert 3 == 4
E        +  where 3 = add(1, 2)

foundry/tests/test_math.py:12: AssertionError
=========================== short test summary info ===========================
FAILED foundry/tests/test_math.py::test_add - assert 3 == 4
"""

NEW_FILE_DIFF = """\
diff --git a/foundry/widget.py b/foundry/widget.py
new file mode 100644
--- /dev/null
+++ b/foundry/widget.py
@@ -0,0 +1,3 @@
+def widget() -> int:
+    \"\"\"Return a widget.\"\"\"
+    return 1
diff --git a/foundry/core.py b/foundry/core.py
--- a/foundry/core.py
+++ b/foundry/core.py
@@ -1,2 +1,2 @@
-x = 1
+x = 2
 y = 3
"""


def run(coro: Any) -> Any:
    """Run a coroutine (no pytest-asyncio plugin)."""
    return asyncio.run(coro)


async def collect(agen: Any) -> list[str]:
    """Drain an async iterator."""
    return [chunk async for chunk in agen]


@pytest.fixture
def line(tmp_path: Path, fake_clock: Any) -> dict[str, Any]:
    """A queue + telemetry sharing the fake clock, plus a tiny repo for the context engine."""
    queue = FoundryQueue(tmp_path / "foundry.db", clock=fake_clock.now)
    telemetry = Telemetry(queue, clock=fake_clock.time)
    repo = tmp_path / "repo"
    repo.mkdir()
    big = "\n".join(
        [
            "import os",
            "",
            "def parse_config(path: str) -> dict:",
            '    """Parse the config file."""',
            *[f"    value_{k} = os.getenv('V{k}')" for k in range(80)],
            "    return {}",
            "",
            "def unrelated_helper() -> None:",
            *["    pass" for _ in range(40)],
        ]
    )
    (repo / "config_loader.py").write_text(big + "\n", encoding="utf-8")
    engine = ContextEngine(repo)
    config = FoundryConfig(db_path=tmp_path / "foundry.db")
    yield {"queue": queue, "telemetry": telemetry, "engine": engine, "config": config, "clock": fake_clock}
    queue.close()


def make_foreman(line: dict[str, Any], gemma: Any = None, **kwargs: Any) -> ShopForeman:
    return ShopForeman(gemma, line["queue"], line["telemetry"], line["engine"], line["config"], **kwargs)


def sized_task(queue: FoundryQueue, task_id: str = "F-001", team: str = "alpha", n: int = 1) -> TaskSizingDirective:
    """Register a task and size it with ``n`` subtasks."""
    queue.add_task(task_id, f"Task {task_id}", "desc", team)
    complexity = {1: "S", 2: "M", 5: "L"}[n]
    directive = TaskSizingDirective(
        task_id=task_id,
        team_id=team,
        complexity=complexity,
        allocated_builders=n,
        rationale="test",
        subtasks=[
            SubtaskAllocation(subtask_id=f"{task_id}.{k}", title=f"part {k}", acceptance_criteria=["works"],
                              allowed_files=[f"f{k}.py"])
            for k in range(1, n + 1)
        ],
    )
    queue.apply_sizing(directive)
    return directive


def fail_gate(line: dict[str, Any], task_id: str, subtask: int = 1) -> None:
    """Record one failed (short-circuited) gate run for a ticket, as the builder worker does."""
    line["queue"].record_gate_result(
        {"task_id": task_id, "subtask_id": f"{task_id}.{subtask}", "passed": False, "check_type": "tests",
         "stdout": "", "stderr": PYTEST_TRACE}
    )
    line["telemetry"].record_gate_failure(task_id)


# --------------------------------------------------------------------------- tools


def test_foreman_tools_are_openai_function_definitions() -> None:
    names = [t["function"]["name"] for t in FOREMAN_TOOLS]
    assert names == ["get_queue_summary", "get_token_velocity", "inspect_task_failure", "get_team_bottlenecks"]
    for tool in FOREMAN_TOOLS:
        assert tool["type"] == "function"
        assert tool["function"]["parameters"]["type"] == "object"
        assert tool["function"]["description"]
    inspect = FOREMAN_TOOLS[2]["function"]["parameters"]
    assert inspect["required"] == ["task_id"]
    json.dumps(FOREMAN_TOOLS)


def test_tools_return_jsonable_dicts_and_are_read_only(line: dict[str, Any]) -> None:
    queue, telemetry = line["queue"], line["telemetry"]
    sized_task(queue, "F-001", "alpha", 2)
    queue.claim_ticket("alpha", 1)
    queue.record_gate_result(
        {"task_id": "F-001", "subtask_id": "F-001.1", "passed": False, "check_type": "tests",
         "stdout": "", "stderr": PYTEST_TRACE}
    )
    telemetry.record_tokens("F-001", "alpha-1", "builder", 1000, 200)
    foreman = make_foreman(line)
    before = queue.summary()
    events_before = len(queue.events())

    summary = foreman.get_queue_summary()
    assert summary["tasks"]["building"] == 1
    assert summary["active_tasks"][0]["task_id"] == "F-001"
    velocity = foreman.get_token_velocity(window_minutes=15)
    assert velocity["tpm_cap"] == 100000
    assert velocity["burn_rate"]["tokens_per_min"] == pytest.approx(1200 / 15)
    failure = foreman.inspect_task_failure("F-001")
    assert failure["found"] and failure["gate_failure_count"] == 1
    assert failure["recent_gate_failures"][0]["check_type"] == "tests"
    assert {t["status"] for t in failure["tickets"]} == {"claimed", "open"}
    bottlenecks = foreman.get_team_bottlenecks()
    assert bottlenecks["teams"]["alpha"]["tickets"] == {"claimed": 1, "open": 1}
    assert bottlenecks["teams"]["alpha"]["free_slots"] == [3, 4, 5]

    for result in (summary, velocity, failure, bottlenecks):
        json.dumps(result)
    assert queue.summary() == before
    assert len(queue.events()) == events_before


def test_execute_tool_dispatch_and_errors(line: dict[str, Any]) -> None:
    foreman = make_foreman(line)
    assert foreman.execute_tool("inspect_task_failure", {"task_id": "nope"}) == {"task_id": "nope", "found": False}
    assert "error" in foreman.execute_tool("inspect_task_failure", {})
    assert "unknown tool" in foreman.execute_tool("drop_tables", {})["error"]
    assert foreman.execute_tool("get_token_velocity", {"window_minutes": 5})["window_minutes"] == 5


# --------------------------------------------------------------------------- sizing


def test_heuristic_complexity_table() -> None:
    assert heuristic_complexity("Fix typo", "One word.", ["a.py"], [])[0] == "S"
    assert heuristic_complexity("Two files", "x", ["a.py", "b.py"], [])[0] == "M"
    assert heuristic_complexity("Many", "x", [f"f{k}.py" for k in range(5)], [])[0] == "L"
    assert heuristic_complexity("Rewrite the parser", "short", [], [])[0] == "L"
    assert heuristic_complexity("Huge", "x", [f"f{k}.py" for k in range(9)], [])[0] == "XL"
    assert heuristic_complexity("Long", "y" * 3000, [], [])[0] == "XL"


@pytest.mark.parametrize(
    ("files", "description", "complexity", "builders"),
    [
        (["a.py"], "Fix a typo.", "S", 1),
        (["a.py", "b.py"], "Small change in two files.", "M", 2),
        ([f"pkg/f{k}.py" for k in range(5)], "Feature across five modules.", "L", 5),
        ([f"pkg/f{k}.py" for k in range(9)], "Very broad change.", "XL", 5),
    ],
)
def test_size_task_scope_heuristic_without_gemma(
    line: dict[str, Any], files: list[str], description: str, complexity: str, builders: int
) -> None:
    foreman = make_foreman(line)
    task = {"task_id": "F-001", "team_id": "bravo", "title": "Work", "description": description,
            "files": files, "acceptance_criteria": ["it works", "tests pass"]}
    directive = run(foreman.size_task_scope(task))
    assert isinstance(directive, TaskSizingDirective)
    assert directive.complexity == complexity
    assert directive.allocated_builders == builders == len(directive.subtasks)
    assert directive.override_reason is None
    assigned = [f for s in directive.subtasks for f in s.allowed_files]
    assert sorted(assigned) == sorted(files)  # every file once, no overlap
    assert all(s.acceptance_criteria for s in directive.subtasks)
    assert [s.subtask_id for s in directive.subtasks] == [f"F-001.{k}" for k in range(1, builders + 1)]
    assert foreman.last_source["size_task_scope"] == "heuristic"


def test_size_caps_builders_at_file_count_with_override(line: dict[str, Any]) -> None:
    foreman = make_foreman(line)
    task = {"task_id": "F-002", "team_id": "alpha", "title": "Rewrite the parser",
            "description": "x", "files": ["parser.py", "lexer.py"]}
    directive = run(foreman.size_task_scope(task))
    assert directive.complexity == "L"
    assert directive.allocated_builders == 2
    assert directive.override_reason and "2 file(s)" in directive.override_reason


def test_size_requires_ids(line: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="task_id"):
        run(make_foreman(line).size_task_scope({"title": "x"}))


def test_size_task_scope_uses_valid_gemma_json(line: dict[str, Any]) -> None:
    reply = {
        "complexity": "M",
        "rationale": "Two independent modules.",
        "subtasks": [
            {"title": "Parser", "acceptance_criteria": ["parses"], "allowed_files": ["parser.py", "evil.py"]},
            {"title": "Lexer", "acceptance_criteria": ["lexes"], "allowed_files": ["lexer.py", "parser.py"]},
            {"title": "Docs", "acceptance_criteria": ["documented"], "allowed_files": ["README.md"]},
        ],
    }
    gemma = SimulatedGemmaClient(lambda req: reply)
    foreman = make_foreman(line, gemma)
    task = {"task_id": "F-003", "team_id": "charlie", "title": "Tokenizer",
            "description": "Build a tokenizer.", "files": ["parser.py", "lexer.py", "README.md"]}
    directive = run(foreman.size_task_scope(task))
    assert foreman.last_source["size_task_scope"] == "gemma"
    assert directive.complexity == "M" and directive.allocated_builders == 2
    assert directive.rationale == "Two independent modules."
    first, second = directive.subtasks
    # evil.py is not in the task's files; the surplus "Docs" subtask merges into the first
    assert first.allowed_files == ["parser.py", "README.md"]
    assert "documented" in first.acceptance_criteria
    assert second.allowed_files == ["lexer.py"]  # no file in two subtasks
    assert gemma.requests[0].json_schema is not None
    saved = line["telemetry"].tokens_saved_by_source()
    assert saved["triage"] > 0


@pytest.mark.parametrize(
    "responder",
    [
        lambda req: "not json at all",
        lambda req: {"complexity": "HUGE", "rationale": "x", "subtasks": []},
        lambda req: {"complexity": "S", "rationale": "x", "subtasks": "one"},
        lambda req: ["a", "list"],
    ],
)
def test_size_task_scope_falls_back_on_invalid_gemma(line: dict[str, Any], responder: Any) -> None:
    foreman = make_foreman(line, SimulatedGemmaClient(responder))
    directive = run(foreman.size_task_scope(
        {"task_id": "F-004", "team_id": "alpha", "title": "Fix typo", "description": "x", "files": ["a.py"]}))
    assert foreman.last_source["size_task_scope"] == "heuristic"
    assert directive.complexity == "S" and directive.allocated_builders == 1
    assert line["telemetry"].tokens_saved_by_source()["triage"] == 0


def test_size_task_scope_falls_back_when_gemma_unreachable(line: dict[str, Any]) -> None:
    foreman = make_foreman(line, SimulatedGemmaClient(unavailable=True))
    directive = run(foreman.size_task_scope({"task_id": "F-005", "team_id": "alpha", "title": "x"}))
    assert directive.complexity == "S"
    assert foreman.last_source["size_task_scope"] == "heuristic"


def test_gemma_with_too_few_subtasks_is_split_deterministically(line: dict[str, Any]) -> None:
    reply = {"complexity": "L", "rationale": "Big.", "subtasks": [
        {"title": "All", "acceptance_criteria": ["a"], "allowed_files": []}]}
    foreman = make_foreman(line, SimulatedGemmaClient(lambda req: reply))
    directive = run(foreman.size_task_scope({"task_id": "F-006", "team_id": "alpha", "title": "Big job"}))
    assert directive.complexity == "L" and len(directive.subtasks) == 5
    assert "split deterministically" in directive.rationale


# --------------------------------------------------------------------------- context


def test_prune_context_slice_delegates_and_records_savings(line: dict[str, Any]) -> None:
    line["engine"].full_lines = 0  # outline-only slices (full edit targets: test_context_targets.py)
    foreman = make_foreman(line)
    subtask = SubtaskAllocation(subtask_id="F-007.1", title="Update parse_config",
                                acceptance_criteria=["parse_config reads V1"], allowed_files=["config_loader.py"])
    sliced = run(foreman.prune_context_slice(subtask, "Make parse_config read V1.", task_id="F-007"))
    assert sliced.snippets and all(s.line_count <= 50 for s in sliced.snippets)
    text = "\n".join(s.text for s in sliced.snippets)
    assert "value_40" not in text  # function bodies are pruned
    assert sliced.source_line_count > 100
    assert line["telemetry"].tokens_saved_by_source()["pruning"] > 0


def test_prune_context_slice_without_engine(line: dict[str, Any]) -> None:
    foreman = ShopForeman(None, line["queue"], line["telemetry"], None, line["config"])
    subtask = SubtaskAllocation(subtask_id="F-1.1", title="Do it", acceptance_criteria=["done"])
    sliced = run(foreman.prune_context_slice(subtask))
    assert sliced.objective == "Do it" and sliced.snippets == [] and sliced.acceptance_criteria == ["done"]


# --------------------------------------------------------------------------- traces


def test_condense_error_trace_fallback_is_one_line_with_location(line: dict[str, Any]) -> None:
    foreman = make_foreman(line)
    hint = run(foreman.condense_error_trace(PYTEST_TRACE))
    assert "\n" not in hint and len(hint) <= 160
    assert "test_math.py:12" in hint
    assert foreman.last_source["condense_error_trace"] == "heuristic"


def test_condense_error_trace_uses_valid_gemma_line(line: dict[str, Any]) -> None:
    gemma = SimulatedGemmaClient(lambda req: "foundry/tests/test_math.py:12: add() returns 3; fix the sum to return 4")
    foreman = make_foreman(line, gemma)
    hint = run(foreman.condense_error_trace(PYTEST_TRACE, task_id="F-008"))
    assert hint.startswith("foundry/tests/test_math.py:12:")
    assert foreman.last_source["condense_error_trace"] == "gemma"
    assert line["telemetry"].tokens_saved_by_source()["triage"] > 0


@pytest.mark.parametrize(
    "bad",
    [
        "line one\nline two foundry/tests/test_math.py:12",
        "foundry/tests/test_math.py:12: " + "x" * 200,
        "The assertion failed somewhere.",  # lost the file:line
        "",
    ],
)
def test_condense_error_trace_rejects_invalid_gemma_output(line: dict[str, Any], bad: str) -> None:
    foreman = make_foreman(line, SimulatedGemmaClient(lambda req: bad))
    hint = run(foreman.condense_error_trace(PYTEST_TRACE))
    assert foreman.last_source["condense_error_trace"] == "heuristic"
    assert "test_math.py:12" in hint and "\n" not in hint and len(hint) <= 160


def test_condense_error_trace_handles_empty_trace(line: dict[str, Any]) -> None:
    hint = run(make_foreman(line).condense_error_trace(""))
    assert hint and "\n" not in hint


# --------------------------------------------------------------------------- PR drafts


def test_diff_stats_counts_files_and_lines() -> None:
    stats = diff_stats(NEW_FILE_DIFF)
    assert [(f["path"], f["status"]) for f in stats["files"]] == [("foundry/widget.py", "added"),
                                                                   ("foundry/core.py", "modified")]
    assert stats["added"] == 4 and stats["removed"] == 1


def test_draft_pr_summary_fallback_from_diff_stats(line: dict[str, Any]) -> None:
    foreman = make_foreman(line)
    summary = run(foreman.draft_pr_summary(NEW_FILE_DIFF))
    assert summary["commit_message"] == "feat(foundry): update 2 files"
    assert "`foundry/widget.py` (added, +3/-0)" in summary["pr_body"]
    assert "2 file(s) changed, 4 insertion(s), 1 deletion(s)" in summary["pr_body"]
    titled = run(foreman.draft_pr_summary(NEW_FILE_DIFF, title="Fix the widget crash"))
    assert titled["commit_message"] == "fix(foundry): fix the widget crash"
    docs = run(foreman.draft_pr_summary("--- a/README.md\n+++ b/README.md\n@@ -1 +1 @@\n-a\n+b\n"))
    assert docs["commit_message"].startswith("docs: ")
    assert foreman.last_source["draft_pr_summary"] == "heuristic"
    assert line["telemetry"].tokens_saved_by_source()["release_draft"] == 0


def test_draft_pr_summary_uses_valid_gemma_draft(line: dict[str, Any]) -> None:
    draft = {"commit_message": "feat(foundry): add widget helper", "pr_body": "## Summary\nAdds a widget."}
    foreman = make_foreman(line, SimulatedGemmaClient(lambda req: draft))
    summary = run(foreman.draft_pr_summary(NEW_FILE_DIFF, task_id="F-009"))
    assert summary == draft
    assert line["telemetry"].tokens_saved_by_source()["release_draft"] > 0


@pytest.mark.parametrize(
    "draft",
    [
        {"commit_message": "Added a widget", "pr_body": "body"},
        {"commit_message": "feat: " + "x" * 80, "pr_body": "body"},
        {"commit_message": "feat(foundry): ok", "pr_body": ""},
        "nonsense",
    ],
)
def test_draft_pr_summary_rejects_invalid_gemma_draft(line: dict[str, Any], draft: Any) -> None:
    foreman = make_foreman(line, SimulatedGemmaClient(lambda req: draft))
    summary = run(foreman.draft_pr_summary(NEW_FILE_DIFF))
    assert summary["commit_message"] == "feat(foundry): update 2 files"
    assert foreman.last_source["draft_pr_summary"] == "heuristic"


# --------------------------------------------------------------------------- Q&A


def test_answer_question_streams_fallback_without_gemma(line: dict[str, Any]) -> None:
    sized_task(line["queue"], "F-010", "alpha", 1)
    chunks = run(collect(make_foreman(line).answer_question("How is the line doing?")))
    assert len(chunks) > 1
    text = "".join(chunks)
    assert "offline" in text and "building: 1" in text


def test_answer_question_runs_tools_then_answers(line: dict[str, Any]) -> None:
    sized_task(line["queue"], "F-011", "bravo", 2)

    def responder(req: GemmaRequest) -> Any:
        if not any(m["role"] == "tool" for m in req.messages):
            return {"tool_calls": [{"name": "get_queue_summary", "arguments": {}},
                                   {"name": "inspect_task_failure", "arguments": {"task_id": "F-011"}}]}
        return "Team bravo is building F-011 with two open tickets."

    gemma = SimulatedGemmaClient(responder)
    foreman = make_foreman(line, gemma)
    text = "".join(run(collect(foreman.answer_question("What is bravo doing?"))))
    assert text == "Team bravo is building F-011 with two open tickets."
    assert gemma.calls == 2
    assert gemma.requests[0].tools == FOREMAN_TOOLS
    tool_msgs = [m for m in gemma.requests[1].messages if m["role"] == "tool"]
    assert len(tool_msgs) == 2
    assert json.loads(tool_msgs[0]["content"])["tasks"]["building"] == 1
    assert json.loads(tool_msgs[1]["content"])["task_id"] == "F-011"
    assert foreman.last_source["answer_question"] == "gemma"
    assert line["telemetry"].tokens_saved_by_source()["triage"] > 0


def test_answer_question_tool_loop_is_bounded(line: dict[str, Any]) -> None:
    def responder(req: GemmaRequest) -> Any:
        if req.tools is None:
            return "Final answer."
        return {"tool_calls": [{"name": "get_team_bottlenecks", "arguments": {}}]}

    gemma = SimulatedGemmaClient(responder)
    text = "".join(run(collect(make_foreman(line, gemma).answer_question("loop forever"))))
    assert text == "Final answer."
    assert gemma.calls == MAX_TOOL_ROUNDS + 1
    assert [r.tools is None for r in gemma.requests] == [False] * MAX_TOOL_ROUNDS + [True]


def test_answer_question_falls_back_when_gemma_fails(line: dict[str, Any]) -> None:
    text = "".join(run(collect(make_foreman(line, SimulatedGemmaClient(unavailable=True)).answer_question("hi"))))
    assert "offline" in text


# --------------------------------------------------------------------------- anomalies


def test_healthy_line_has_no_escalation(line: dict[str, Any]) -> None:
    sized_task(line["queue"], "F-020", "alpha", 1)
    assert run(make_foreman(line).evaluate_line_anomaly()) is None


def test_line_jam_quarantines_task(line: dict[str, Any]) -> None:
    sized_task(line["queue"], "F-021", "alpha", 1)
    fail_gate(line, "F-021")
    foreman = make_foreman(line)
    assert run(foreman.evaluate_line_anomaly()) is None  # 1 < threshold 2
    fail_gate(line, "F-021")
    alert = run(foreman.evaluate_line_anomaly())
    assert isinstance(alert, ForemanEscalation)
    assert alert.category == EscalationCategory.LINE_JAM
    assert alert.prescribed_action == PrescribedAction.QUARANTINE_TASK
    assert alert.severity == Severity.HIGH
    assert alert.target_entity.task_id == "F-021" and alert.target_entity.team_id == "alpha"
    assert alert.metrics_snapshot.gate_failure_count == 2
    assert alert.alert_id == "ALT-20260115-001"
    assert alert.timestamp == "2026-01-15T12:00:00Z"
    ForemanEscalation.model_validate(alert.model_dump(mode="json"))


def test_token_velocity_spike_throttles(line: dict[str, Any]) -> None:
    line["telemetry"].record_tokens("F-022", "charlie-2", "builder", 80000, 15000)
    alert = run(make_foreman(line).evaluate_line_anomaly())
    assert alert is not None
    assert alert.category == EscalationCategory.TOKEN_VELOCITY_SPIKE
    assert alert.prescribed_action == PrescribedAction.THROTTLE_DISPATCH
    assert alert.target_entity.agent_id == "charlie-2" and alert.target_entity.team_id == "charlie"
    assert alert.metrics_snapshot.current_tpm == 95000
    assert 0 < alert.action_parameters["factor"] < 1 and alert.action_parameters["seconds"] > 0


def test_agent_stalled_resets_builder(line: dict[str, Any]) -> None:
    sized_task(line["queue"], "F-023", "bravo", 1)
    ticket = line["queue"].claim_ticket("bravo", 1)
    foreman = make_foreman(line)
    line["clock"].advance(899)
    assert run(foreman.evaluate_line_anomaly()) is None
    line["clock"].advance(2)
    alert = run(foreman.evaluate_line_anomaly())
    assert alert is not None and alert.category == EscalationCategory.AGENT_STALLED
    assert alert.prescribed_action == PrescribedAction.RESET_BUILDER_CONTEXT
    assert alert.target_entity.agent_id == "bravo-1"
    assert alert.action_parameters == {"agent_id": "bravo-1", "ticket_id": ticket.ticket_id, "slot": 1}
    assert alert.metrics_snapshot.dwell_seconds == pytest.approx(901, abs=1)


def test_builder_activity_prevents_stall(line: dict[str, Any]) -> None:
    sized_task(line["queue"], "F-024", "bravo", 1)
    ticket = line["queue"].claim_ticket("bravo", 1)
    line["clock"].advance(800)
    line["queue"].record_event("build_progress", "F-024", {"ticket_id": ticket.ticket_id})
    line["clock"].advance(800)
    assert run(make_foreman(line).evaluate_line_anomaly()) is None


def test_review_bottleneck_promotes_peer_reviewer(line: dict[str, Any]) -> None:
    sized_task(line["queue"], "F-025", "charlie", 2)
    line["telemetry"].mark_review_wait("F-025")
    line["clock"].advance(601)
    alert = run(make_foreman(line).evaluate_line_anomaly())
    assert alert is not None and alert.category == EscalationCategory.REVIEW_BOTTLENECK
    assert alert.prescribed_action == PrescribedAction.PROMOTE_PEER_REVIEWER
    assert alert.target_entity.team_id == "charlie"
    assert alert.action_parameters == {"minutes": 15.0, "slot": 5}  # no ticket id: task-level mark
    assert alert.metrics_snapshot.dwell_seconds == pytest.approx(601, abs=1)


def test_capacity_rebalance_resizes_allocation(line: dict[str, Any]) -> None:
    queue = line["queue"]
    sized_task(queue, "F-026", "alpha", 2)
    queue.resize("F-026", 1)  # both tickets now stacked on slot 1
    line["telemetry"].set_idle("alpha", 4)
    alert = run(make_foreman(line).evaluate_line_anomaly())
    assert alert is not None and alert.category == EscalationCategory.CAPACITY_REBALANCE
    assert alert.prescribed_action == PrescribedAction.RESIZE_ALLOCATION
    assert alert.severity == Severity.LOW
    assert alert.action_parameters == {"builders": 2, "from": 1}
    assert alert.target_entity.task_id == "F-026"


def test_thresholds_come_from_config(line: dict[str, Any]) -> None:
    sized_task(line["queue"], "F-027", "alpha", 1)
    fail_gate(line, "F-027")
    config = FoundryConfig(db_path=line["config"].db_path, thresholds=AnomalyThresholds(gate_fail_quarantine=1))
    foreman = ShopForeman(None, line["queue"], line["telemetry"], line["engine"], config)
    alert = run(foreman.evaluate_line_anomaly())
    assert alert is not None and alert.category == EscalationCategory.LINE_JAM


def test_alert_ids_are_sequential_and_cooldown_skips_repeats(line: dict[str, Any]) -> None:
    sized_task(line["queue"], "F-028", "alpha", 1)
    sized_task(line["queue"], "F-029", "bravo", 1)
    for tid in ("F-028", "F-028", "F-029", "F-029"):
        fail_gate(line, tid)
    foreman = make_foreman(line, cooldown_s=300)
    first = run(foreman.evaluate_line_anomaly())
    second = run(foreman.evaluate_line_anomaly())
    assert (first.alert_id, second.alert_id) == ("ALT-20260115-001", "ALT-20260115-002")
    assert {first.target_entity.task_id, second.target_entity.task_id} == {"F-028", "F-029"}
    assert run(foreman.evaluate_line_anomaly()) is None  # both in cooldown
    line["clock"].advance(301)
    third = run(foreman.evaluate_line_anomaly())
    assert third.alert_id == "ALT-20260115-003"


def test_alert_sequence_continues_after_stored_escalations(line: dict[str, Any]) -> None:
    sized_task(line["queue"], "F-030", "alpha", 1)
    for _ in range(2):
        fail_gate(line, "F-030")
    first = run(make_foreman(line).evaluate_line_anomaly())
    line["queue"].record_escalation(first)
    restarted = make_foreman(line)  # e.g. after a crash
    assert run(restarted.evaluate_line_anomaly()).alert_id == "ALT-20260115-002"


def test_gemma_writes_only_the_diagnosis(line: dict[str, Any]) -> None:
    sized_task(line["queue"], "F-031", "alpha", 1)
    for _ in range(2):
        fail_gate(line, "F-031")
    gemma = SimulatedGemmaClient(lambda req: "The task keeps failing its tests. Quarantine it now.")
    foreman = make_foreman(line, gemma)
    alert = run(foreman.evaluate_line_anomaly())
    assert alert.diagnosis == "The task keeps failing its tests. Quarantine it now."
    assert alert.category == EscalationCategory.LINE_JAM  # detection stays deterministic
    assert foreman.last_source["evaluate_line_anomaly"] == "gemma"
    assert gemma.calls == 1 and gemma.requests[0].tools is None


@pytest.mark.parametrize("bad", ["One. Two. Three sentences.", "", "x" * 400])
def test_invalid_gemma_diagnosis_falls_back(line: dict[str, Any], bad: str) -> None:
    sized_task(line["queue"], "F-032", "alpha", 1)
    for _ in range(2):
        fail_gate(line, "F-032")
    foreman = make_foreman(line, SimulatedGemmaClient(lambda req: bad))
    alert = run(foreman.evaluate_line_anomaly())
    assert alert.diagnosis.startswith("Ticket F-032:F-032.1 failed its deterministic gates 2 times")
    assert foreman.last_source["evaluate_line_anomaly"] == "heuristic"


def test_detect_anomalies_orders_by_urgency(line: dict[str, Any]) -> None:
    sized_task(line["queue"], "F-033", "alpha", 2)
    line["queue"].claim_ticket("alpha", 1)
    line["telemetry"].mark_review_wait("F-033")
    line["clock"].advance(1000)
    for _ in range(2):
        fail_gate(line, "F-033", subtask=2)  # the other ticket; gate events on ticket 1 would count as activity
    line["telemetry"].record_tokens("F-033", "alpha-1", "builder", 99000, 0)
    categories = [a.category for a in make_foreman(line).detect_anomalies()]
    assert categories[:4] == [
        EscalationCategory.LINE_JAM,
        EscalationCategory.TOKEN_VELOCITY_SPIKE,
        EscalationCategory.AGENT_STALLED,
        EscalationCategory.REVIEW_BOTTLENECK,
    ]


def test_terminal_tasks_raise_nothing(line: dict[str, Any]) -> None:
    sized_task(line["queue"], "F-034", "alpha", 1)
    for _ in range(3):
        fail_gate(line, "F-034")
    line["queue"].quarantine("F-034", "test")
    assert run(make_foreman(line).evaluate_line_anomaly()) is None


# --------------------------------------------------------------------------- round 1 fixes (D6, D7, D8, D12)


def submit_for_review(line: dict[str, Any], task_id: str, team: str, slot: int = 1, *,
                      flag: bool = True, mark: bool = True) -> Any:
    """Claim a ticket, submit it, pass gates and hand it to the review inbox (as builder_worker does)."""
    queue = line["queue"]
    ticket = queue.claim_ticket(team, slot)
    queue.record_event("submission", task_id, {"ticket_id": ticket.ticket_id, "builder_id": f"{team}-{slot}"})
    if queue.get_task(task_id)["state"].value == "building":
        queue.set_state(task_id, "validating")
        queue.set_state(task_id, "reviewing")
    if flag:
        queue.set_in_review(ticket.ticket_id, True)
    if mark:
        line["telemetry"].mark_review_wait(task_id, item=ticket.ticket_id)
    return ticket


def test_repro_alpha6_ticket_waiting_for_review_is_not_a_stall(line: dict[str, Any]) -> None:
    """alpha-6: at +16 min a submission waiting for review raised AGENT_STALLED/RESET."""
    sized_task(line["queue"], "F-040", "alpha", 1)
    ticket = submit_for_review(line, "F-040", "alpha")
    line["clock"].advance(16 * 60)
    foreman = make_foreman(line)
    categories = [a.category for a in foreman.detect_anomalies()]
    assert EscalationCategory.AGENT_STALLED not in categories
    alert = run(foreman.evaluate_line_anomaly())
    assert alert.category == EscalationCategory.REVIEW_BOTTLENECK
    assert alert.prescribed_action == PrescribedAction.PROMOTE_PEER_REVIEWER
    assert alert.action_parameters["ticket_id"] == ticket.ticket_id
    assert alert.action_parameters["slot"] == 5
    assert alert.metrics_snapshot.dwell_seconds == pytest.approx(960, abs=1)
    assert run(foreman.evaluate_line_anomaly()) is None  # no RESET follow-up either


def test_repro_charlie6_in_review_flag_alone_suppresses_stall(line: dict[str, Any]) -> None:
    """charlie-6: same scenario with only the queue's in_review flag (no telemetry wait mark)."""
    sized_task(line["queue"], "F-041", "charlie", 1)
    submit_for_review(line, "F-041", "charlie", mark=False)
    line["clock"].advance(16 * 60)
    alert = run(make_foreman(line).evaluate_line_anomaly())
    assert alert.category == EscalationCategory.REVIEW_BOTTLENECK  # falls back to time in 'reviewing'
    assert alert.target_entity.task_id == "F-041"


def test_review_wait_mark_alone_also_suppresses_stall(line: dict[str, Any]) -> None:
    sized_task(line["queue"], "F-042", "bravo", 1)
    submit_for_review(line, "F-042", "bravo", flag=False)
    line["clock"].advance(16 * 60)
    categories = [a.category for a in make_foreman(line).detect_anomalies()]
    assert categories == [EscalationCategory.REVIEW_BOTTLENECK]


def test_stall_detection_resumes_when_review_cleared(line: dict[str, Any]) -> None:
    sized_task(line["queue"], "F-043", "alpha", 1)
    ticket = submit_for_review(line, "F-043", "alpha", mark=False)
    line["queue"].set_in_review(ticket.ticket_id, False)  # reviewer finished, builder owns it again
    line["queue"].set_state("F-043", "building")
    line["clock"].advance(901)
    alert = run(make_foreman(line).evaluate_line_anomaly())
    assert alert.category == EscalationCategory.AGENT_STALLED
    assert alert.action_parameters["ticket_id"] == ticket.ticket_id


def test_review_bottleneck_uses_per_submission_dwell(line: dict[str, Any]) -> None:
    sized_task(line["queue"], "F-044", "alpha", 2)
    first = submit_for_review(line, "F-044", "alpha", 1)
    line["clock"].advance(500)
    second = submit_for_review(line, "F-044", "alpha", 2)
    line["telemetry"].mark_review_start("F-044", item=first.ticket_id)  # reviewer took the old one
    line["clock"].advance(200)
    foreman = make_foreman(line)
    assert run(foreman.evaluate_line_anomaly()) is None  # remaining submission waited only 200 s
    line["clock"].advance(401)
    alert = run(foreman.evaluate_line_anomaly())
    assert alert.category == EscalationCategory.REVIEW_BOTTLENECK
    assert alert.action_parameters["ticket_id"] == second.ticket_id
    assert alert.metrics_snapshot.dwell_seconds == pytest.approx(601, abs=1)


def test_line_jam_counts_gate_failures_per_ticket(line: dict[str, Any]) -> None:
    sized_task(line["queue"], "F-045", "alpha", 2)
    fail_gate(line, "F-045", subtask=1)
    fail_gate(line, "F-045", subtask=2)
    foreman = make_foreman(line)
    assert run(foreman.evaluate_line_anomaly()) is None  # 2 failures on the task, but 1 per ticket
    fail_gate(line, "F-045", subtask=2)
    alert = run(foreman.evaluate_line_anomaly())
    assert alert.category == EscalationCategory.LINE_JAM
    assert alert.action_parameters["ticket_id"] == "F-045:F-045.2"
    assert alert.metrics_snapshot.gate_failure_count == 2
    assert foreman.inspect_task_failure("F-045")["gate_failures_by_ticket"] == {"F-045.1": 1, "F-045.2": 2}


def test_line_jam_ignores_failures_of_done_tickets(line: dict[str, Any]) -> None:
    queue = line["queue"]
    sized_task(queue, "F-046", "alpha", 2)
    for _ in range(2):
        fail_gate(line, "F-046", subtask=1)
    ticket = queue.claim_ticket("alpha", 1)
    queue.complete_ticket(ticket.ticket_id)  # it eventually passed and was approved
    assert run(make_foreman(line).evaluate_line_anomaly()) is None


def test_alert_ids_are_reserved_in_the_db_when_issued(line: dict[str, Any]) -> None:
    queue = line["queue"]
    sized_task(queue, "F-047", "alpha", 1)
    for _ in range(2):
        fail_gate(line, "F-047")
    first = run(make_foreman(line).evaluate_line_anomaly())
    assert first.alert_id == "ALT-20260115-001"
    assert queue.alert_ids("ALT-20260115-") == ["ALT-20260115-001"]  # persisted without record_escalation
    queue.reserve_alert_id("ALT-20260115-002")  # e.g. taken by another Foreman process
    restarted = make_foreman(line)
    assert run(restarted.evaluate_line_anomaly()).alert_id == "ALT-20260115-003"
    assert queue.alert_ids("ALT-20260115-")[-1] == "ALT-20260115-003"
