"""Tests for foundry.builder_worker: prompts, reply parsers, workers and TeamPool.

The worker tests run against the real queue, telemetry, rate limiter, solution
cache, context engine and validation gates, with ``SimulatedClaudeClient`` and
a tiny fake foreman (no Gemma, no real Claude).  Every async test uses
``asyncio.run`` (no pytest-asyncio plugin).
"""

from __future__ import annotations

import asyncio
import difflib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from foundry.builder_worker import (
    DIFF_CONTRACT,
    BuilderWorker,
    TeamPool,
    build_builder_prompt,
    build_reviewer_prompt,
    directives_to_hint,
    extract_diff_paths,
    parse_builder_reply,
    parse_reviewer_reply,
)
from foundry.claude_client import SimulatedClaudeClient
from foundry.context_engine import ContextEngine
from foundry.orchestrator_queue import FoundryQueue
from foundry.rate_limiter import RateLimiter
from foundry.schemas import (
    BUILDERS_FOR,
    BuilderSubmission,
    CodeSnippet,
    ContextSlice,
    DeterministicGateResult,
    LineDirective,
    SubtaskAllocation,
    TaskAssignment,
    TaskSizingDirective,
    TaskState,
)
from foundry.solution_cache import SolutionCache
from foundry.telemetry import Telemetry
from foundry.validation_gates import ValidationGates

TEAM = "bravo"
REVIEWER = f"{TEAM}-6"
ORIGINAL = 'def add(a, b):\n    """Add two numbers."""\n    return a + b\n'


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def make_diff(path: str, old: str, new: str) -> str:
    """Unified diff (a/ b/ prefixes) from ``old`` to ``new``."""
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True), new.splitlines(keepends=True), f"a/{path}", f"b/{path}"
        )
    )


def fenced(diff: str) -> str:
    return f"Here is the change.\n\n```diff\n{diff}```\n"


GOOD_NEW = ORIGINAL + '\n\ndef mul(a, b):\n    """Multiply two numbers."""\n    return a * b\n'
GOOD_DIFF = make_diff("pkg/mathx.py", ORIGINAL, GOOD_NEW)
BETTER_NEW = ORIGINAL + '\n\ndef mul(a: int, b: int) -> int:\n    """Multiply two ints."""\n    return a * b\n'
BETTER_DIFF = make_diff("pkg/mathx.py", ORIGINAL, BETTER_NEW)
BROKEN_DIFF = make_diff("pkg/mathx.py", ORIGINAL, ORIGINAL + "\n\ndef mul(a, b)\n    return a * b\n")

APPROVE = json.dumps({"verdict": "APPROVED", "directives": []})


def assignment(**overrides: Any) -> TaskAssignment:
    data: dict[str, Any] = {
        "task_id": "T-1",
        "team_id": TEAM,
        "builder_id": f"{TEAM}-1",
        "subtask_id": "T-1.1",
        "acceptance_criteria": ["mul(a, b) returns the product"],
        "allowed_files": ["pkg/mathx.py"],
        "context": ContextSlice(
            objective="Add a mul function.",
            acceptance_criteria=["mul(a, b) returns the product"],
            snippets=[CodeSnippet(path="pkg/mathx.py", start_line=1, end_line=3, text="def add(a, b):\n    ...")],
            source_line_count=3,
        ),
    }
    data.update(overrides)
    return TaskAssignment(**data)


class FakeForeman:
    """Stand-in Shop Foreman: deterministic trace condensation, records calls."""

    def __init__(self, engine: ContextEngine) -> None:
        self.engine = engine
        self.traces: list[str] = []

    def condense_error_trace(self, trace: str) -> str:
        self.traces.append(trace)
        return self.engine.condense_error_trace(trace)


class Rig:
    """Real queue/telemetry/limiter/cache/context/gates over a temp workspace."""

    def __init__(self, tmp_path: Path, script: Callable[[str, str], str], *, gates: Any = "real",
                 context: bool = True, max_concurrent: int = 3) -> None:
        self.ws = tmp_path / "ws"
        (self.ws / "pkg").mkdir(parents=True)
        (self.ws / "pkg" / "mathx.py").write_text(ORIGINAL, encoding="utf-8")
        self.queue = FoundryQueue(tmp_path / "foundry.db")
        self.telemetry = Telemetry(self.queue)
        self.limiter = RateLimiter(rpm=600, tpm=10_000_000, max_concurrent=max_concurrent,
                                   sleep=lambda _s: asyncio.sleep(0))
        self.cache = SolutionCache(tmp_path / "foundry.db")
        self.engine = ContextEngine(self.ws) if context else None
        self.foreman = FakeForeman(ContextEngine(self.ws))
        self.gates = ValidationGates(checks=("syntax", "linter")) if gates == "real" else gates
        self.client = SimulatedClaudeClient(script)
        self.completed: list[str] = []

    def pool(self, builders: int = 5, **kwargs: Any) -> TeamPool:
        return TeamPool(
            TEAM,
            builders,
            queue=self.queue,
            client=self.client,
            limiter=self.limiter,
            telemetry=self.telemetry,
            context_engine=self.engine,
            gates=self.gates,
            cache=self.cache,
            foreman=self.foreman,
            poll=0.01,
            workspace=self.ws if self.gates is not None and hasattr(self.gates, "validate") else None,
            on_task_complete=self.completed.append,
            **kwargs,
        )

    def submit(self, task_id: str, complexity: str = "S", files: list[str] | None = None,
               criteria: list[str] | None = None, title: str | None = None) -> None:
        n = BUILDERS_FOR[complexity]
        self.queue.add_task(task_id, title or f"Task {task_id}", "add mul", TEAM)
        self.queue.apply_sizing(
            TaskSizingDirective(
                task_id=task_id,
                team_id=TEAM,
                complexity=complexity,
                allocated_builders=n,
                rationale="test",
                subtasks=[
                    SubtaskAllocation(
                        subtask_id=f"{task_id}.{i + 1}",
                        title="Add a mul function to mathx" if i == 0 else f"Part {i + 1}",
                        acceptance_criteria=criteria or ["mul(a, b) returns the product"],
                        allowed_files=files or (["pkg/mathx.py"] if i == 0 else [f"pkg/part{i + 1}.py"]),
                    )
                    for i in range(n)
                ],
            )
        )

    def state(self, task_id: str) -> TaskState:
        return self.queue.get_task(task_id)["state"]


async def run_pool(rig: Rig, task_ids: list[str], builders: int = 5, timeout: float = 15.0) -> dict[str, str]:
    pool = rig.pool(builders)
    async with pool:
        states = await pool.wait_for_tasks(task_ids, timeout=timeout)
        await asyncio.sleep(0.05)  # give idle slots a few more polls
    return states


def is_reviewer(agent_id: str) -> bool:
    return agent_id == REVIEWER


# ---------------------------------------------------------------------------
# prompts
# ---------------------------------------------------------------------------


def test_builder_prompt_contains_only_the_slice_and_the_contract() -> None:
    a = assignment(fix_hint="[syntax] pkg/mathx.py:5 SyntaxError: expected ':'", attempt=1)
    prompt = build_builder_prompt(a)
    assert "Add a mul function." in prompt
    assert "- mul(a, b) returns the product" in prompt
    assert "- pkg/mathx.py" in prompt
    assert "### pkg/mathx.py (lines 1-3)" in prompt and "def add(a, b):\n    ..." in prompt
    assert "SyntaxError: expected ':'" in prompt
    assert "attempt 2 of 3" in prompt
    assert DIFF_CONTRACT in prompt
    assert "exactly one fenced code block" in prompt


def test_builder_prompt_without_fix_hint_has_no_fix_section() -> None:
    prompt = build_builder_prompt(assignment())
    assert "Fix required" not in prompt
    assert "attempt 1 of 3" in prompt


def test_builder_prompt_fences_snippets_that_contain_backticks() -> None:
    snip = CodeSnippet(path="README.md", start_line=1, end_line=3, text="```diff\nx\n```")
    a = assignment(context=ContextSlice(objective="Docs.", snippets=[snip]))
    prompt = build_builder_prompt(a)
    assert "````text" in prompt  # longer fence than the snippet's own


def test_reviewer_prompt_is_diff_only() -> None:
    a = assignment()
    sub = parse_builder_reply(fenced(GOOD_DIFF), a)
    gates = [
        DeterministicGateResult(task_id="T-1", subtask_id="T-1.1", passed=True, check_type="syntax",
                                stdout="SECRET-STDOUT", duration_seconds=0.01),
        DeterministicGateResult(task_id="T-1", subtask_id="T-1.1", passed=True, check_type="linter",
                                note="ruff not installed; builtin linter"),
    ]
    prompt = build_reviewer_prompt(sub, a.acceptance_criteria, gates, reviewer_id=REVIEWER)
    assert "- mul(a, b) returns the product" in prompt
    assert GOOD_DIFF.rstrip("\n") in prompt
    assert "- syntax: PASS" in prompt and "- linter: PASS" in prompt and "builtin linter" in prompt
    assert '"verdict"' in prompt
    # never full files, context snippets or gate output
    assert "def add(a, b):\n    ..." not in prompt
    assert "SECRET-STDOUT" not in prompt
    assert "def add(a, b):\n    \"\"\"Add two numbers.\"\"\"\n    return a + b" not in prompt


# ---------------------------------------------------------------------------
# builder reply parser
# ---------------------------------------------------------------------------


def test_parse_builder_reply_single_block_is_ready() -> None:
    sub = parse_builder_reply(fenced(GOOD_DIFF), assignment())
    assert sub.status == "ready_for_review"
    assert sub.modified_files == ["pkg/mathx.py"]
    assert sub.diff == GOOD_DIFF
    assert sub.self_check == {"single_diff_block": True, "unified_diff": True, "files_allowed": True}


@pytest.mark.parametrize(
    "reply, fragment",
    [
        ("I changed it, trust me.", "no ```edits or ```diff block"),
        ("BLOCKED: the file does not exist", "BLOCKED: the file does not exist"),
        (fenced(GOOD_DIFF) + fenced(GOOD_DIFF), "2 ```diff blocks"),
        ("```diff\n" + GOOD_DIFF, "not closed"),
        ("```diff\n```\n", "empty"),
        ("```diff\njust some words\n```\n", "not a unified diff"),
        (fenced(make_diff("other.py", "", "x = 1\n")), "outside the allowance"),
    ],
)
def test_parse_builder_reply_bad_replies_are_blocked(reply: str, fragment: str) -> None:
    sub = parse_builder_reply(reply, assignment())
    assert sub.status == "blocked"
    assert fragment in (sub.blocker_notes or "")


def test_parse_builder_reply_without_allowance_accepts_any_file() -> None:
    sub = parse_builder_reply(fenced(make_diff("new/thing.py", "", "x = 1\n")), assignment(allowed_files=[]))
    assert sub.status == "ready_for_review"


def test_extract_diff_paths_new_modified_deleted() -> None:
    diff = (
        "--- /dev/null\n+++ b/new.py\n@@ -0,0 +1 @@\n+x\n"
        "--- a/mod.py\n+++ b/mod.py\n@@ -1 +1 @@\n-a\n+b\n"
        "--- a/gone.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-z\n"
    )
    assert extract_diff_paths(diff) == ["new.py", "mod.py", "gone.py"]


# ---------------------------------------------------------------------------
# reviewer reply parser
# ---------------------------------------------------------------------------


def test_parse_reviewer_reply_valid_json_and_ids_from_caller() -> None:
    text = json.dumps({
        "verdict": "REVISE",
        "task_id": "IGNORED",
        "reviewer_id": "IGNORED",
        "summary": "extra key dropped",
        "directives": [{"path": "pkg/mathx.py", "line": 5, "instruction": "add type hints", "severity": "low"}],
    })
    v = parse_reviewer_reply(text, task_id="T-1", reviewer_id=REVIEWER, retry_count=7)
    assert v.verdict == "REVISE"
    assert v.task_id == "T-1" and v.reviewer_id == REVIEWER
    assert v.retry_count == 2  # clamped
    assert v.directives == [LineDirective(path="pkg/mathx.py", line=5, instruction="add type hints")]


def test_parse_reviewer_reply_tolerates_single_json_fence_and_lowercase() -> None:
    v = parse_reviewer_reply('```json\n{"verdict": "approved"}\n```', task_id="T-1", reviewer_id=REVIEWER)
    assert v.verdict == "APPROVED" and v.directives == []


@pytest.mark.parametrize(
    "text",
    [
        "Looks good to me!",
        "",
        "[1, 2]",
        '{"verdict": "MAYBE"}',
        '{"verdict": "REVISE", "directives": "fix it"}',
        '{"verdict": "REVISE", "directives": [{"path": "x.py"}]}',
        'Sure: {"verdict": "APPROVED"}',
    ],
)
def test_parse_reviewer_reply_invalid_becomes_revise_with_directive(text: str) -> None:
    v = parse_reviewer_reply(text, task_id="T-1", reviewer_id=REVIEWER, default_path="pkg/mathx.py")
    assert v.verdict == "REVISE"
    assert len(v.directives) == 1
    assert v.directives[0].path == "pkg/mathx.py"
    assert "not a valid verdict" in v.directives[0].instruction


def test_directives_to_hint() -> None:
    hint = directives_to_hint([LineDirective(path="a.py", line=3, instruction="rename x"),
                               LineDirective(path="b.py", line=0, instruction="add docstring")])
    assert hint == "Reviewer asked for changes: a.py:3 rename x; b.py: add docstring"


# ---------------------------------------------------------------------------
# workers with the real modules
# ---------------------------------------------------------------------------


def test_small_task_only_slot_one_calls_claude(tmp_path: Path) -> None:
    rig = Rig(tmp_path, lambda p, agent: APPROVE if is_reviewer(agent) else fenced(GOOD_DIFF))
    rig.submit("T-S", "S")
    states = asyncio.run(run_pool(rig, ["T-S"]))
    assert states == {"T-S": "complete"}
    calls = rig.client.calls
    assert calls[f"{TEAM}-1"] == 1 and calls[REVIEWER] == 1
    for slot in range(2, 6):
        assert calls[f"{TEAM}-{slot}"] == 0
    assert (rig.ws / "pkg" / "mathx.py").read_text() == GOOD_NEW
    assert rig.completed == ["T-S"]
    # telemetry: tokens by role, durations, review dwell, idle count, cache stored
    roles = rig.telemetry.tokens_by_role()
    assert roles["builder"]["calls"] == 1 and roles["builder"]["input"] > 0
    assert roles["reviewer"]["calls"] == 1 and roles["reviewer"]["output"] > 0
    durations = rig.telemetry.durations()
    assert durations["build"]["count"] == 1 and durations["review"]["count"] == 1
    assert rig.telemetry.queue_dwell("T-S") >= 0.0
    assert rig.telemetry.waiting_for_review() == {}  # reviewer picked it up
    assert rig.telemetry.idle_counts()[TEAM] == 5
    assert rig.cache.stats()["entries"] == 1
    assert rig.limiter.current_rpm() == 2  # every Claude call went through the limiter


def test_builder_prompt_received_by_claude_is_pruned(tmp_path: Path) -> None:
    rig = Rig(tmp_path, lambda p, agent: APPROVE if is_reviewer(agent) else fenced(GOOD_DIFF))
    rig.submit("T-P", "S")
    asyncio.run(run_pool(rig, ["T-P"]))
    prompt = rig.client.prompts_for(f"{TEAM}-1")[0]
    assert "def add(a, b)" in prompt
    # T-026: a file within the full-body budget is shown whole, with line numbers,
    # next to an inventory of its existing symbols (so nothing gets re-defined).
    assert "  3|     return a + b" in prompt
    assert "inventory of pkg/mathx.py" in prompt
    assert "## Context\n" in prompt and "outline only" in prompt


def test_medium_task_uses_two_slots_and_completes_once(tmp_path: Path) -> None:
    def script(prompt: str, agent: str) -> str:
        if is_reviewer(agent):
            return APPROVE
        if agent == f"{TEAM}-1":
            return fenced(GOOD_DIFF)
        return fenced(make_diff("pkg/part2.py", "", "VALUE = 2\n").replace("--- a/pkg/part2.py", "--- /dev/null"))

    rig = Rig(tmp_path, script)
    rig.submit("T-M", "M")
    states = asyncio.run(run_pool(rig, ["T-M"]))
    assert states == {"T-M": "complete"}
    assert [rig.client.calls[f"{TEAM}-{s}"] for s in range(1, 6)] == [1, 1, 0, 0, 0]
    assert rig.client.calls[REVIEWER] == 2
    assert rig.completed == ["T-M"]
    assert (rig.ws / "pkg" / "part2.py").read_text() == "VALUE = 2\n"


def test_broken_diff_never_reaches_reviewer_and_retry_cap_quarantines(tmp_path: Path) -> None:
    rig = Rig(tmp_path, lambda p, agent: APPROVE if is_reviewer(agent) else fenced(BROKEN_DIFF))
    rig.submit("T-B", "S")
    states = asyncio.run(run_pool(rig, ["T-B"]))
    assert states == {"T-B": "quarantined"}
    assert rig.client.calls[f"{TEAM}-1"] == 3  # first try + 2 retries, no 4th build
    assert rig.client.calls[REVIEWER] == 0
    prompts = rig.client.prompts_for(f"{TEAM}-1")
    assert "Fix required" not in prompts[0]
    for p in prompts[1:]:
        assert "[syntax]" in p and "Fix required" in p
        hint = p.split("## Fix required (your previous attempt was sent back)\n", 1)[1].split("\n", 1)[0]
        assert "\n" not in hint and len(hint) < 200
    assert len(rig.foreman.traces) == 3
    assert rig.telemetry.gate_failures("T-B") == 3
    assert rig.telemetry.retries("T-B") == 2
    assert rig.queue.gate_failure_count("T-B") == 3
    assert (rig.ws / "pkg" / "mathx.py").read_text() == ORIGINAL  # broken diff rolled back


def test_gate_failure_then_fix_is_reviewed(tmp_path: Path) -> None:
    replies = iter([fenced(BROKEN_DIFF), fenced(GOOD_DIFF)])
    rig = Rig(tmp_path, lambda p, agent: APPROVE if is_reviewer(agent) else next(replies))
    rig.submit("T-F", "S")
    states = asyncio.run(run_pool(rig, ["T-F"]))
    assert states == {"T-F": "complete"}
    assert rig.client.calls[f"{TEAM}-1"] == 2 and rig.client.calls[REVIEWER] == 1
    assert (rig.ws / "pkg" / "mathx.py").read_text() == GOOD_NEW


def test_bad_reply_format_is_sent_back_without_review(tmp_path: Path) -> None:
    replies = iter(["I made the change.", fenced(GOOD_DIFF)])
    rig = Rig(tmp_path, lambda p, agent: APPROVE if is_reviewer(agent) else next(replies))
    rig.submit("T-R", "S")
    asyncio.run(run_pool(rig, ["T-R"]))
    second = rig.client.prompts_for(f"{TEAM}-1")[1]
    assert "no ```edits or ```diff block" in second
    assert rig.client.calls[REVIEWER] == 1


def test_revise_loops_back_with_directives_then_approves(tmp_path: Path) -> None:
    verdicts = iter([
        json.dumps({"verdict": "REVISE", "directives": [
            {"path": "pkg/mathx.py", "line": 6, "instruction": "add int type hints to mul"}]}),
        APPROVE,
    ])
    builds = iter([fenced(GOOD_DIFF), fenced(BETTER_DIFF)])
    rig = Rig(tmp_path, lambda p, agent: next(verdicts) if is_reviewer(agent) else next(builds))
    rig.submit("T-V", "S")
    states = asyncio.run(run_pool(rig, ["T-V"]))
    assert states == {"T-V": "complete"}
    second = rig.client.prompts_for(f"{TEAM}-1")[1]
    assert "pkg/mathx.py:6 add int type hints to mul" in second
    assert "attempt 2 of 3" in second
    # the REVISE'd diff was rolled back, so the second diff applied cleanly
    assert (rig.ws / "pkg" / "mathx.py").read_text() == BETTER_NEW
    assert [v["verdict"] for v in rig.queue.verdicts("T-V")] == ["REVISE", "APPROVED"]
    assert rig.telemetry.retries("T-V") == 1


def test_revise_counts_toward_retry_cap(tmp_path: Path) -> None:
    revise = json.dumps({"verdict": "REVISE", "directives": [
        {"path": "pkg/mathx.py", "line": 1, "instruction": "still wrong"}]})
    rig = Rig(tmp_path, lambda p, agent: revise if is_reviewer(agent) else fenced(GOOD_DIFF))
    rig.submit("T-X", "S")
    states = asyncio.run(run_pool(rig, ["T-X"]))
    assert states == {"T-X": "quarantined"}
    assert rig.client.calls[f"{TEAM}-1"] == 3 and rig.client.calls[REVIEWER] == 3
    assert rig.cache.stats()["entries"] == 0  # never cached without APPROVED
    assert (rig.ws / "pkg" / "mathx.py").read_text() == ORIGINAL


def test_rejected_quarantines_and_rolls_back(tmp_path: Path) -> None:
    reject = json.dumps({"verdict": "REJECTED", "directives": [
        {"path": "pkg/mathx.py", "line": 0, "instruction": "wrong module"}]})
    rig = Rig(tmp_path, lambda p, agent: reject if is_reviewer(agent) else fenced(GOOD_DIFF))
    rig.submit("T-J", "S")
    states = asyncio.run(run_pool(rig, ["T-J"]))
    assert states == {"T-J": "quarantined"}
    assert rig.client.calls[f"{TEAM}-1"] == 1
    assert (rig.ws / "pkg" / "mathx.py").read_text() == ORIGINAL
    assert "wrong module" in (rig.queue.get_task("T-J")["quarantine_reason"] or "")


def _fake_gates(sub: BuilderSubmission, a: TaskAssignment) -> list[DeterministicGateResult]:
    """Pass-through gates that leave the workspace untouched (so identical tasks stay identical)."""
    return [DeterministicGateResult(task_id=a.task_id, subtask_id=a.subtask_id, passed=True, check_type="syntax")]


def _two_tasks(rig: Rig, titles: tuple[str, str], **pool_kwargs: Any) -> None:
    async def scenario() -> None:
        pool = rig.pool(**pool_kwargs)
        async with pool:
            rig.submit("T-C1", "S", title=titles[0])
            await pool.wait_for_tasks(["T-C1"], timeout=10)
            rig.submit("T-C2", "S", title=titles[1])
            await pool.wait_for_tasks(["T-C2"], timeout=10)

    asyncio.run(scenario())


def test_cache_hit_skips_builder_claude_but_still_reviewed_by_default(tmp_path: Path) -> None:
    rig = Rig(tmp_path, lambda p, agent: APPROVE if is_reviewer(agent) else fenced(GOOD_DIFF), gates=_fake_gates)
    _two_tasks(rig, ("Add mul", "Add mul"))
    assert rig.state("T-C1") == TaskState.COMPLETE and rig.state("T-C2") == TaskState.COMPLETE
    assert rig.client.calls[f"{TEAM}-1"] == 1  # second task served from the cache
    assert rig.client.calls[REVIEWER] == 2  # trust_cache defaults to False: still reviewed
    assert rig.telemetry.tokens_saved_by_source()["cache"] > 0
    assert rig.completed == ["T-C1", "T-C2"]


def test_trusted_cache_hit_skips_reviewer_too(tmp_path: Path) -> None:
    rig = Rig(tmp_path, lambda p, agent: APPROVE if is_reviewer(agent) else fenced(GOOD_DIFF), gates=_fake_gates)
    _two_tasks(rig, ("Add mul", "Add mul"), trust_cache=True)
    assert rig.client.calls[f"{TEAM}-1"] == 1 and rig.client.calls[REVIEWER] == 1


def test_trust_cache_comes_from_config(tmp_path: Path) -> None:
    from foundry.config import FoundryConfig

    rig = Rig(tmp_path, lambda p, agent: APPROVE)
    assert rig.pool().trust_cache is False
    cfg = FoundryConfig(trust_cache=True, limit_cooldown_s=77)
    pool = rig.pool(config=cfg)
    assert pool.trust_cache is True and pool.builder(1).trust_cache is True
    assert pool.builder(1).limit_cooldown_s == 77 and pool.reviewer.limit_cooldown_s == 77


def test_different_work_order_title_misses_the_cache(tmp_path: Path) -> None:
    rig = Rig(tmp_path, lambda p, agent: APPROVE if is_reviewer(agent) else fenced(GOOD_DIFF), gates=_fake_gates)
    _two_tasks(rig, ("Add mul", "Add a multiply helper for invoices"))
    assert rig.client.calls[f"{TEAM}-1"] == 2


def test_rate_limited_call_is_retried_without_burning_an_attempt(tmp_path: Path) -> None:
    rig = Rig(tmp_path, lambda p, agent: APPROVE if is_reviewer(agent) else fenced(GOOD_DIFF))
    rig.client.inject_rate_limit(agent_id=f"{TEAM}-1", times=2, retry_after=0.0)
    rig.submit("T-L", "S")
    states = asyncio.run(run_pool(rig, ["T-L"]))
    assert states == {"T-L": "complete"}
    assert rig.client.calls[f"{TEAM}-1"] == 3  # 2 x 429 then success, same attempt
    assert rig.telemetry.retries("T-L") == 0
    assert rig.limiter.rate_limit_hits == 2


def test_concurrency_never_exceeds_limiter_cap(tmp_path: Path) -> None:
    rig = Rig(tmp_path, lambda p, agent: APPROVE, gates=None, context=False, max_concurrent=1)
    rig.client.latency = 0.01
    rig.client.script = lambda p, agent: APPROVE if is_reviewer(agent) else fenced(
        make_diff(f"pkg/f{agent[-1]}.py", "", "X = 1\n").replace(f"--- a/pkg/f{agent[-1]}.py", "--- /dev/null"))
    for t in ("T-A", "T-B"):
        rig.queue.add_task(t, t, "", TEAM)
    rig.queue.apply_sizing(TaskSizingDirective(
        task_id="T-A", team_id=TEAM, complexity="L", allocated_builders=5, rationale="",
        subtasks=[SubtaskAllocation(subtask_id=f"A{i}", title=f"f{i}", allowed_files=[f"pkg/f{i}.py"])
                  for i in range(1, 6)]))
    asyncio.run(run_pool(rig, ["T-A"]))
    assert rig.state("T-A") == TaskState.COMPLETE
    assert rig.client.max_in_flight == 1
    assert rig.limiter.peak_in_flight == 1


def test_idle_pool_makes_zero_calls_and_stops_cleanly(tmp_path: Path) -> None:
    rig = Rig(tmp_path, lambda p, agent: APPROVE)

    async def scenario() -> TeamPool:
        pool = rig.pool().start()
        await asyncio.sleep(0.1)
        assert pool.running
        await pool.stop(timeout=2)
        return pool

    pool = asyncio.run(scenario())
    assert rig.client.total_calls == 0
    assert not pool.running and not pool._tasks
    assert rig.telemetry.idle_counts()[TEAM] == 5


def test_superseded_ticket_is_dropped(tmp_path: Path) -> None:
    """If the Floor Manager reopens a ticket mid-build, the stale result is not acted on."""
    rig = Rig(tmp_path, lambda p, agent: fenced(BROKEN_DIFF), gates=None, context=False)
    rig.submit("T-Z", "S")

    async def scenario() -> str:
        ticket = rig.queue.claim_ticket(TEAM, 1)
        worker = BuilderWorker(TEAM, 1, rig.queue, rig.client, rig.limiter, rig.telemetry, None, None,
                               rig.cache, rig.foreman)
        rig.queue.reopen_ticket(ticket.ticket_id, None)  # e.g. RESET_BUILDER_CONTEXT
        # broken *format* reply -> blocked -> would send back, but the ticket is no longer ours
        rig.client.script = lambda p, agent: "no diff here"
        return await worker.process_ticket(ticket)

    assert asyncio.run(scenario()) == "superseded"
    t = rig.queue.tickets_for_task("T-Z")[0]
    assert t.status == "open" and t.attempt == 1  # only the manager's reopen counted


def test_promote_peer_reviewer_and_reset_context(tmp_path: Path) -> None:
    rig = Rig(tmp_path, lambda p, agent: APPROVE if agent in (REVIEWER, f"{TEAM}-5") else fenced(GOOD_DIFF))

    async def scenario() -> tuple[str | None, list[str]]:
        pool = rig.pool()
        async with pool:
            peer = pool.promote_peer_reviewer(None, minutes=1)
            pool.builder(2).scratch["x"] = 1
            pool.reset_builder(2)
            assert pool.builder(2).scratch == {}
            rig.submit("T-Q", "S")
            await pool.wait_for_tasks(["T-Q"], timeout=10)
            peers = list(pool.peer_reviewers)
        return peer, peers

    peer, peers = asyncio.run(scenario())
    assert peer == f"{TEAM}-5" and peers == [peer]
    assert rig.state("T-Q") == TaskState.COMPLETE
    assert rig.client.calls[REVIEWER] + rig.client.calls[f"{TEAM}-5"] == 1


def test_gates_without_workspace_is_rejected(tmp_path: Path) -> None:
    rig = Rig(tmp_path, lambda p, agent: APPROVE)
    with pytest.raises(ValueError, match="workspace"):
        BuilderWorker(TEAM, 1, rig.queue, rig.client, rig.limiter, rig.telemetry, None,
                      ValidationGates(), None, None)


def test_async_foreman_condenser_with_task_id(tmp_path: Path) -> None:
    """ShopForeman.condense_error_trace is async and takes task_id; failures fall back."""
    seen: list[str | None] = []

    class AsyncForeman:
        async def condense_error_trace(self, trace: str, *, task_id: str | None = None) -> str:
            seen.append(task_id)
            if len(seen) == 2:
                raise RuntimeError("GemmaUnavailable")
            return "pkg/mathx.py:6 SyntaxError: expected ':'"

    rig = Rig(tmp_path, lambda p, agent: fenced(BROKEN_DIFF))
    rig.foreman = AsyncForeman()  # type: ignore[assignment]
    rig.submit("T-G", "S")
    asyncio.run(run_pool(rig, ["T-G"]))
    assert seen == ["T-G", "T-G", "T-G"]
    prompts = rig.client.prompts_for(f"{TEAM}-1")
    assert "[syntax] pkg/mathx.py:6 SyntaxError: expected ':'" in prompts[1]
    assert "[syntax]" in prompts[2]  # fallback hint (GateOutcome.summary) after the foreman failed


# ---------------------------------------------------------------------------
# round 1 fixes: infrastructure failures (D1), in-review (D6), dwell (D7),
# token accounting (D11), peer reviewers (D13)
# ---------------------------------------------------------------------------


async def _run_for(rig: Rig, seconds: float, **pool_kwargs: Any) -> TeamPool:
    pool = rig.pool(**pool_kwargs)
    async with pool:
        await asyncio.sleep(seconds)
    return pool


class _StepClock:
    """Monotonic fake clock whose async sleep advances time and yields once."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += max(0.0, seconds)
        await asyncio.sleep(0)


class _SkewClock:
    """Real monotonic time plus a skew; async sleep adds to the skew instead of waiting."""

    def __init__(self) -> None:
        import time

        self._mono = time.monotonic
        self.skew = 0.0

    def __call__(self) -> float:
        return self._mono() + self.skew

    async def sleep(self, seconds: float) -> None:
        self.skew += max(0.0, seconds)
        await asyncio.sleep(0)


@pytest.mark.parametrize("retry_after", [3600.0, None])
def test_permanent_usage_limit_never_quarantines(tmp_path: Path, retry_after: float | None) -> None:
    """bravo-6 repro: a usage limit that never lifts must not burn attempts or quarantine."""
    rig = Rig(tmp_path, lambda p, agent: fenced(GOOD_DIFF))
    clock = _StepClock()  # backoff sleeps advance a fake clock instantly
    rig.limiter = RateLimiter(rpm=600, tpm=10_000_000, max_concurrent=3, clock=clock, sleep=clock.sleep)
    rig.client.inject_rate_limit(times=10_000, retry_after=retry_after)
    rig.submit("T-U", "S")
    asyncio.run(_run_for(rig, 0.4))
    assert rig.state("T-U") == TaskState.BUILDING
    ticket = rig.queue.tickets_for_task("T-U")[0]
    assert ticket.status == "open" and ticket.attempt == 0 and ticket.fix_hint is None
    assert rig.telemetry.retries("T-U") == 0
    assert rig.limiter.is_paused
    expected = 3600.0 if retry_after else 1200.0
    assert rig.limiter.pause_remaining() == pytest.approx(expected, abs=5)
    # paused: the builder stopped instead of spinning (1 call for a long reset; the backoff
    # attempts of call_with_backoff when no reset time was given)
    assert rig.client.calls[f"{TEAM}-1"] == (1 if retry_after else 5)
    assert rig.queue.claim_ticket(TEAM, 1) is not None  # still claimable


def test_timeout_releases_ticket_and_retries_without_attempt(tmp_path: Path) -> None:
    rig = Rig(tmp_path, lambda p, agent: APPROVE if is_reviewer(agent) else fenced(GOOD_DIFF))
    rig.client.inject_exception(asyncio.TimeoutError(), agent_id=f"{TEAM}-1", times=2)
    rig.submit("T-T", "S")

    async def scenario() -> TeamPool:
        pool = rig.pool(infra_backoff_s=0.01)
        async with pool:
            await pool.wait_for_tasks(["T-T"], timeout=10)
        return pool

    pool = asyncio.run(scenario())
    assert rig.state("T-T") == TaskState.COMPLETE
    assert rig.client.calls[f"{TEAM}-1"] == 3
    assert rig.telemetry.retries("T-T") == 0
    assert "Fix required" not in rig.client.prompts_for(f"{TEAM}-1")[-1]
    assert pool.builder(1).outcomes[:2] == ["released", "released"]
    assert not rig.limiter.is_paused  # a timeout backs off this worker only


def test_auth_error_pauses_dispatch(tmp_path: Path) -> None:
    from foundry import claude_client

    exc_type = getattr(claude_client, "ClaudeAuthError", claude_client.ClaudeNotFound)
    rig = Rig(tmp_path, lambda p, agent: fenced(GOOD_DIFF))
    rig.client.inject_exception(exc_type("please run /login"), times=10)
    rig.submit("T-A", "S")
    asyncio.run(_run_for(rig, 0.3, limit_cooldown_s=900))
    assert rig.limiter.pause_remaining() == pytest.approx(900, abs=5)
    assert rig.queue.tickets_for_task("T-A")[0].attempt == 0
    assert rig.client.calls[f"{TEAM}-1"] == 1


def test_reviewer_infra_error_requeues_without_builder_retry(tmp_path: Path) -> None:
    rig = Rig(tmp_path, lambda p, agent: APPROVE if is_reviewer(agent) else fenced(GOOD_DIFF))
    rig.client.inject_exception(asyncio.TimeoutError(), agent_id=REVIEWER, times=1)
    rig.client.inject_failure(agent_id=REVIEWER, times=1, error="overloaded")
    rig.submit("T-RV", "S")

    async def scenario() -> None:
        pool = rig.pool(infra_backoff_s=0.01)
        async with pool:
            await pool.wait_for_tasks(["T-RV"], timeout=10)

    asyncio.run(scenario())
    assert rig.state("T-RV") == TaskState.COMPLETE
    assert rig.client.calls[f"{TEAM}-1"] == 1  # builder never asked again
    assert rig.client.calls[REVIEWER] == 3
    assert rig.telemetry.retries("T-RV") == 0
    assert [v["verdict"] for v in rig.queue.verdicts("T-RV")] == ["APPROVED"]


def test_in_review_flag_and_ownership_recheck_after_review_call(tmp_path: Path) -> None:
    """Stall repro: a ticket reset while the reviewer's Claude call runs is not approved stale."""
    seen: dict[str, Any] = {}

    def script(prompt: str, agent: str) -> str:
        if not is_reviewer(agent):
            return fenced(GOOD_DIFF)
        ticket = rig.queue.tickets_for_task("T-ST")[0]
        if "first" not in seen:
            seen["first"] = ticket.in_review
            rig.queue.release_ticket(ticket.ticket_id, "manager reset")  # e.g. RESET_BUILDER_CONTEXT
        return APPROVE

    rig = Rig(tmp_path, script)
    rig.submit("T-ST", "S")
    asyncio.run(run_pool(rig, ["T-ST"]))
    assert seen["first"] is True  # in_review set while waiting for / in review
    assert rig.state("T-ST") == TaskState.COMPLETE
    assert rig.client.calls[f"{TEAM}-1"] == 2  # rebuilt after the reset
    assert [v["verdict"] for v in rig.queue.verdicts("T-ST")] == ["APPROVED"]  # stale verdict discarded
    t = rig.queue.tickets_for_task("T-ST")[0]
    assert t.attempt == 0 and t.in_review is False
    assert (rig.ws / "pkg" / "mathx.py").read_text() == GOOD_NEW


def test_review_dwell_is_tracked_per_ticket(tmp_path: Path) -> None:
    rig = Rig(tmp_path, lambda p, agent: APPROVE if is_reviewer(agent) else fenced(GOOD_DIFF))
    rig.submit("T-D", "S")
    asyncio.run(run_pool(rig, ["T-D"]))
    ticket_id = rig.queue.tickets_for_task("T-D")[0].ticket_id
    rows = rig.queue.conn.execute(
        "SELECT item, started_at FROM telemetry_review_wait WHERE task_id='T-D'").fetchall()
    assert [r[0] for r in rows] == [ticket_id]
    assert rows[0][1] is not None


def test_limiter_charged_without_cache_reads(tmp_path: Path) -> None:
    from foundry.schemas import ClaudeResult

    def script(prompt: str, agent: str) -> ClaudeResult:
        text = APPROVE if is_reviewer(agent) else fenced(GOOD_DIFF)
        return ClaudeResult(text=text, input_tokens=10, output_tokens=5, cache_read_tokens=5000)

    rig = Rig(tmp_path, script)
    rig.submit("T-K", "S")
    asyncio.run(run_pool(rig, ["T-K"]))
    assert rig.limiter.current_tpm() == 30  # 2 calls x (10 + 5); cache reads not charged
    roles = rig.telemetry.tokens_by_role()
    assert roles["builder"]["cache_read"] == 5000 and roles["builder"]["total"] == 15


def test_peer_reviewer_never_reviews_its_own_slot(tmp_path: Path) -> None:
    """D13: a peer reviewer leaves its own slot's submission in the inbox for the team reviewer."""
    from foundry.builder_worker import ReviewerWorker, ReviewItem

    rig = Rig(tmp_path, lambda p, agent: APPROVE)
    a = assignment(builder_id=f"{TEAM}-1")
    item = ReviewItem({"ticket_id": "X", "task_id": "T-1", "attempt": 0}, a,
                      parse_builder_reply(fenced(GOOD_DIFF), a), [])

    async def scenario() -> tuple[int, int]:
        inbox: asyncio.Queue[ReviewItem] = asyncio.Queue()
        peer = ReviewerWorker(TEAM, rig.queue, rig.client, rig.limiter, rig.telemetry, None,
                              inbox=inbox, reviewer_id=f"{TEAM}-1", exclude_builder=f"{TEAM}-1", poll=0.01)
        await inbox.put(item)
        stop = asyncio.Event()
        task = asyncio.create_task(peer.run(stop))
        await asyncio.sleep(0.1)
        stop.set()
        await task
        return peer.claude_calls, inbox.qsize()

    calls, left = asyncio.run(scenario())
    assert calls == 0 and left == 1
    assert rig.client.total_calls == 0


def test_promoted_slot_claims_no_build_tickets_until_promotion_ends(tmp_path: Path) -> None:
    """M1: while slot 1 is a peer reviewer it does not build; afterwards it does."""
    rig = Rig(tmp_path, lambda p, agent: APPROVE if is_reviewer(agent) or "Review task" in p else fenced(GOOD_DIFF))

    async def scenario() -> tuple[int, int]:
        pool = rig.pool()
        async with pool:
            assert pool.promote_peer_reviewer(1, minutes=0.3 / 60) == f"{TEAM}-1"
            assert pool.builder(1).promoted and pool.idle_count() == 4
            rig.submit("T-O", "S")  # slot 1's ticket
            await asyncio.sleep(0.15)
            during = rig.client.calls[f"{TEAM}-1"]
            await pool.wait_for_tasks(["T-O"], timeout=10)
            assert not pool.builder(1).promoted
            return during, rig.client.calls[f"{TEAM}-1"]

    during, after = asyncio.run(scenario())
    assert during == 0 and after == 1
    assert rig.state("T-O") == TaskState.COMPLETE
    assert rig.queue.tickets_for_task("T-O")[0].attempt == 0


def test_busy_slot_cannot_be_promoted(tmp_path: Path) -> None:
    rig = Rig(tmp_path, lambda p, agent: APPROVE)

    async def scenario() -> str | None:
        pool = rig.pool()
        async with pool:
            pool.builder(3).busy = True
            return pool.promote_peer_reviewer(3, minutes=1)

    assert asyncio.run(scenario()) is None


def test_is_infrastructure_error_shapes() -> None:
    from foundry.builder_worker import is_infrastructure_error
    from foundry.schemas import ClaudeResult

    def err(error: str, text: str = "", out: int = 0) -> ClaudeResult:
        return ClaudeResult(text=text, output_tokens=out, is_error=True, error=error)

    assert is_infrastructure_error(err("timeout after 600s"))
    assert is_infrastructure_error(err("claude exited 1 without JSON output: boom"))
    assert is_infrastructure_error(err("simulated failure"))  # no reply at all
    assert not is_infrastructure_error(err("error_max_turns", text="partial answer", out=40))
    assert not is_infrastructure_error(ClaudeResult(text="ok"))


def _fake_claude(tmp_path: Path, body: str) -> str:
    import os
    import stat

    script = tmp_path / "bin" / "claude"
    script.parent.mkdir(exist_ok=True)
    script.write_text("#!/bin/sh\ncat > /dev/null\n" + body + "\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    assert os.access(script, os.X_OK)
    return str(script)


@pytest.mark.skipif(__import__("sys").platform.startswith("win"), reason="POSIX shell fake")
@pytest.mark.parametrize("body", ["sleep 5", "echo 'segfault' >&2; exit 3"])
def test_real_client_timeout_or_no_json_quarantines_at_infra_cap(tmp_path: Path, body: str) -> None:
    """Real ClaudeCodeClient shapes: 'timeout after Ns' and 'exited N without JSON output'."""
    from foundry.claude_client import ClaudeCodeClient

    rig = Rig(tmp_path, lambda p, agent: APPROVE)
    rig.client = ClaudeCodeClient(_fake_claude(tmp_path, body))  # type: ignore[assignment]
    rig.submit("T-RC", "S")

    async def scenario() -> TeamPool:
        pool = rig.pool(claude_timeout=0.3, infra_backoff_s=0.05)
        async with pool:
            await pool.wait_for_tasks(["T-RC"], timeout=15)
        return pool

    pool = asyncio.run(scenario())
    outcomes = pool.builder(1).outcomes
    assert outcomes == ["released", "released", "quarantined"]  # E1 cap = 3
    task = rig.queue.get_task("T-RC")
    assert task["state"] == TaskState.QUARANTINED
    assert task["quarantine_reason"].startswith("infrastructure: ")
    t = rig.queue.tickets_for_task("T-RC")[0]
    assert t.attempt == 0 and t.fix_hint is None  # no build attempt spent
    assert rig.telemetry.retries("T-RC") == 0
    errors = pool.builder(1).infra_errors
    assert errors and ("timeout after" in errors[0] or "without JSON output" in errors[0])


# ---------------------------------------------------------------------------
# round 2: E1 infrastructure cap (charlie-6 loop.py shapes)
# ---------------------------------------------------------------------------

TIMEOUT = dict(text="", input_tokens=0, output_tokens=0, duration_seconds=600, is_error=True,
               error="timeout after 600s")
MAX_TURNS = dict(text="I need to look at more files first.", input_tokens=900, output_tokens=200,
                 duration_seconds=30, is_error=True, error="error_max_turns")


def _loop_script(mode: str) -> Callable[[str, str], Any]:
    from foundry.schemas import ClaudeResult

    def script(prompt: str, agent: str) -> Any:
        rev = "## Unified diff" in prompt
        if mode == "builder_timeout" and not rev:
            return ClaudeResult(**TIMEOUT)
        if mode == "reviewer_maxturns" and rev:
            return ClaudeResult(**MAX_TURNS)
        if mode == "builder_maxturns" and not rev:
            return ClaudeResult(**{**MAX_TURNS, "text": "Let me explore."})
        return APPROVE if rev else fenced(GOOD_DIFF)

    return script


def _run_to_end(rig: Rig, task_id: str, **pool_kwargs: Any) -> TeamPool:
    async def scenario() -> TeamPool:
        pool = rig.pool(infra_backoff_s=0.001, **pool_kwargs)
        async with pool:
            await pool.wait_for_tasks([task_id], timeout=10)
        return pool

    return asyncio.run(scenario())


def test_always_timeout_builder_quarantined_after_three_calls(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    rig = Rig(tmp_path, _loop_script("builder_timeout"))
    rig.submit("T-LT", "S")
    with caplog.at_level("WARNING", logger="foundry.builder_worker"):
        _run_to_end(rig, "T-LT")
    task = rig.queue.get_task("T-LT")
    assert task["state"] == TaskState.QUARANTINED
    assert task["quarantine_reason"].startswith("infrastructure: timeout")
    assert rig.client.calls[f"{TEAM}-1"] == 3 and rig.client.calls[REVIEWER] == 0
    assert rig.queue.tickets_for_task("T-LT")[0].attempt == 0
    warnings = [r for r in caplog.records if "infrastructure error" in r.getMessage()]
    assert len(warnings) == 3
    assert all(len(r.getMessage()) < 260 and "## Objective" not in r.getMessage() for r in warnings)


def test_reviewer_error_max_turns_quarantined_after_three(tmp_path: Path) -> None:
    rig = Rig(tmp_path, _loop_script("reviewer_maxturns"))
    rig.submit("T-LR", "S")
    _run_to_end(rig, "T-LR")
    task = rig.queue.get_task("T-LR")
    assert task["state"] == TaskState.QUARANTINED
    assert task["quarantine_reason"].startswith("infrastructure: error_max_turns")
    assert rig.client.calls[REVIEWER] == 3
    assert rig.client.calls[f"{TEAM}-1"] == 1  # the builder was never charged a retry
    assert rig.queue.tickets_for_task("T-LR")[0].attempt == 0
    assert (rig.ws / "pkg" / "mathx.py").read_text() == ORIGINAL  # applied diff rolled back


def test_builder_error_with_reply_uses_attempts(tmp_path: Path) -> None:
    rig = Rig(tmp_path, _loop_script("builder_maxturns"))
    rig.submit("T-LM", "S")
    _run_to_end(rig, "T-LM")
    task = rig.queue.get_task("T-LM")
    assert task["state"] == TaskState.QUARANTINED
    assert rig.client.calls[f"{TEAM}-1"] == 3
    assert not task["quarantine_reason"].startswith("infrastructure")  # normal retry limit


def test_infra_cap_from_config(tmp_path: Path) -> None:
    from foundry.config import FoundryConfig

    rig = Rig(tmp_path, _loop_script("builder_timeout"))
    rig.submit("T-CF", "S")
    cfg = FoundryConfig(**({"infra_failure_cap": 5} if "infra_failure_cap" in FoundryConfig.model_fields else {}))
    expected = getattr(cfg, "infra_failure_cap", 3)
    _run_to_end(rig, "T-CF", config=cfg)
    assert rig.client.calls[f"{TEAM}-1"] == expected


def test_parsed_reply_resets_infra_count(tmp_path: Path) -> None:
    from foundry.schemas import ClaudeResult

    replies = iter([ClaudeResult(**TIMEOUT), ClaudeResult(**TIMEOUT), "no diff, sorry",
                    ClaudeResult(**TIMEOUT), ClaudeResult(**TIMEOUT), fenced(GOOD_DIFF)])
    rig = Rig(tmp_path, lambda p, agent: APPROVE if is_reviewer(agent) else next(replies))
    rig.submit("T-RS", "S")
    _run_to_end(rig, "T-RS")
    assert rig.state("T-RS") == TaskState.COMPLETE  # 4 timeouts, never 3 in a row
    t = rig.queue.tickets_for_task("T-RS")[0]
    assert t.attempt == 1  # only the garbage reply used an attempt


def test_gate_infrastructure_error_is_counted(tmp_path: Path) -> None:
    try:
        from foundry.floor_manager import InfrastructureError
    except ImportError:  # pragma: no cover - older floor_manager
        class InfrastructureError(Exception):  # type: ignore[no-redef]
            pass

    def broken_gates(sub: BuilderSubmission, a: TaskAssignment) -> Any:
        raise InfrastructureError("scratch copy failed: disk full")

    rig = Rig(tmp_path, lambda p, agent: APPROVE if is_reviewer(agent) else fenced(GOOD_DIFF), gates=broken_gates)
    rig.submit("T-GI", "S")
    _run_to_end(rig, "T-GI")
    task = rig.queue.get_task("T-GI")
    assert task["state"] == TaskState.QUARANTINED
    assert task["quarantine_reason"].startswith("infrastructure: scratch copy failed")
    assert rig.client.calls[f"{TEAM}-1"] == 3 and rig.client.calls[REVIEWER] == 0
    assert rig.queue.tickets_for_task("T-GI")[0].attempt == 0


def test_usage_limit_is_not_counted_toward_infra_cap(tmp_path: Path) -> None:
    """Many limit/pause/release cycles in a row never quarantine (account-wide errors aren't counted)."""
    rig = Rig(tmp_path, lambda p, agent: fenced(GOOD_DIFF))
    clock = _SkewClock()  # backoff sleeps advance instantly; the 0.02 s pause passes in real time
    rig.limiter = RateLimiter(rpm=10**6, tpm=10**9, max_concurrent=3, clock=clock, sleep=clock.sleep)
    rig.client.inject_rate_limit(times=10_000, retry_after=0.02)
    rig.submit("T-UL", "S")
    pool = asyncio.run(_run_for(rig, 0.6))
    cycles = pool.builder(1).outcomes.count("released")
    assert cycles >= 4  # well past the cap of 3
    assert rig.state("T-UL") == TaskState.BUILDING  # never quarantined
    t = rig.queue.tickets_for_task("T-UL")[0]
    assert t.attempt == 0 and getattr(t, "infra_failures", 0) == 0


# ---------------------------------------------------------------------------
# round 3: repo-scoped dispatch (G1), last account-wide error (G5)
# ---------------------------------------------------------------------------


def test_pool_only_claims_tickets_of_its_repo(tmp_path: Path) -> None:
    rig = Rig(tmp_path, lambda p, agent: APPROVE if is_reviewer(agent) else fenced(GOOD_DIFF))
    repo_a, repo_b = tmp_path / "repo-a", tmp_path / "repo-b"
    rig.submit("T-RA", "S")
    rig.submit("T-RB", "S")
    rig.queue.set_repo_root("T-RA", repo_a)
    rig.queue.set_repo_root("T-RB", repo_b)

    async def scenario() -> None:
        pool = rig.pool(repo_root=repo_a)
        assert pool.builder(1).repo_root == str(repo_a)
        async with pool:
            await pool.wait_for_tasks(["T-RA"], timeout=10)
            await asyncio.sleep(0.1)

    asyncio.run(scenario())
    assert rig.state("T-RA") == TaskState.COMPLETE
    assert rig.state("T-RB") == TaskState.BUILDING  # other repo's task untouched
    assert rig.queue.tickets_for_task("T-RB")[0].status == "open"
    assert rig.client.calls[f"{TEAM}-1"] == 1


def test_auth_error_is_remembered_for_the_end_of_run_message(tmp_path: Path) -> None:
    from foundry import claude_client

    exc_type = getattr(claude_client, "ClaudeAuthError", claude_client.ClaudeNotFound)
    rig = Rig(tmp_path, lambda p, agent: fenced(GOOD_DIFF))
    rig.client.inject_exception(exc_type("Invalid API key · Please run /login " + "x" * 300), times=10)
    rig.submit("T-AE", "S")
    asyncio.run(_run_for(rig, 0.2, limit_cooldown_s=900))
    saved = rig.queue.kv_get("last_account_error")
    assert saved["type"] == exc_type.__name__
    assert "/login" in saved["message"] and len(saved["message"]) <= 160
    assert "## Objective" not in saved["message"]
    assert saved["at"].endswith("Z")


def test_usage_limit_is_remembered_and_timeouts_are_not(tmp_path: Path) -> None:
    rig = Rig(tmp_path, _loop_script("builder_timeout"))
    rig.submit("T-TO", "S")
    _run_to_end(rig, "T-TO")
    assert rig.queue.kv_get("last_account_error") is None  # timeouts are not account-wide

    rig2 = Rig(tmp_path / "second", lambda p, agent: fenced(GOOD_DIFF))
    rig2.client.inject_rate_limit(times=10, retry_after=3600.0)
    rig2.submit("T-UL2", "S")
    asyncio.run(_run_for(rig2, 0.2))
    assert rig2.queue.kv_get("last_account_error")["type"] == "ClaudeRateLimited"
