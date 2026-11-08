"""T-027 (alpha-3): end-to-end reproductions of the S-003 failures + the retry cap.

Every test wires the real factory (queue, context engine, Shop Foreman
heuristics, workspace gates, TeamPools, FloorManager, dry BoardBridge) and swaps
only Claude for ``SimulatedClaudeClient``. No real Claude, Ollama or GitHub.

(a) F-010.5: a tests ticket sees the impl ticket's new functions in full, its
    workspace contains them, gates pass and the combined patch applies on base.
(b) F-011.2: a ticket whose focus file is ``shop/a.py`` may edit
    ``shop/config.py`` from its sibling's file list (and ``depends_on`` keeps
    the sibling sequential).
(c) F-013: a ticket that fails its gates twice still gets a 3rd attempt at
    context level 3 (default cap ``MAX_RETRIES`` = 3); the cap is configurable.
(d) ``parallel_ok`` tickets (disjoint files, no tests) still run concurrently.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from foundry.claude_client import SimulatedClaudeClient
from foundry.config import FoundryConfig
from foundry.context_engine import ContextEngine
from foundry.floor_manager import Factory, WorkOrder
from foundry.main import write_demo_repo
from foundry.orchestrator_queue import FoundryQueue
from foundry.schemas import (
    MAX_RETRIES,
    MAX_RETRIES_LIMIT,
    ClaudeResult,
    Complexity,
    SubtaskAllocation,
    TaskSizingDirective,
    TaskState,
)
from foundry.tests.test_orchestration import _factory

_SUBTASK_RE = re.compile(r"subtask (\S+), attempt (\d+) of (\d+)\.")
APPROVE = json.dumps({"verdict": "APPROVED", "directives": []})

CLAMP = 'def clamp(value, lo, hi):\n    """Limit value to lo..hi."""\n    return max(lo, min(hi, value))\n'
CLAMP01 = 'def clamp01(value):\n    """Limit value to 0..1."""\n    return clamp(value, 0, 1)\n'
TEST_MATHX = (
    "from shop.mathx import clamp, clamp01\n\n\n"
    "def test_clamp() -> None:\n"
    "    assert clamp(5, 0, 3) == 3\n"
    "    assert clamp01(-1) == 0\n"
)


def edits_reply(*edits: dict[str, Any]) -> str:
    return "Done.\n\n```edits\n" + json.dumps({"edits": list(edits)}, indent=1) + "\n```\n"


def broken_reply(path: str) -> str:
    return edits_reply({"op": "append", "file": path, "code": "def broken(value:\n    return value\n"})


class Script:
    """Simulated Claude: reviewers approve; builders answer per (subtask suffix, attempt)."""

    def __init__(self, replies: dict[str, Any], *, latency: float = 0.0) -> None:
        self.replies = replies
        self.builder_prompts: dict[str, list[str]] = {}
        self.client = SimulatedClaudeClient(self._script, latency=latency)

    def _script(self, prompt: str, agent_id: str) -> ClaudeResult:
        m = _SUBTASK_RE.search(prompt)
        if prompt.startswith("You are reviewer") or "## Unified diff" in prompt or m is None:
            text = APPROVE
        else:
            sub, attempt = m.group(1), int(m.group(2))
            self.builder_prompts.setdefault(sub, []).append(prompt)
            reply = self.replies[sub.rsplit(".", 1)[1]]
            text = reply(attempt) if callable(reply) else reply
        return ClaudeResult(text=text, input_tokens=len(prompt) // 4 + 1,
                            output_tokens=len(text) // 4 + 1, duration_seconds=0.0)

    def prompts(self, suffix: str) -> list[str]:
        return next(v for k, v in self.builder_prompts.items() if k.endswith("." + suffix))


async def _run(f: Factory, order: dict[str, Any], timeout: float = 20) -> tuple[str, str]:
    tid = await f.manager.submit(WorkOrder(**order))
    async with f.running():
        states = await f.wait([tid], timeout=timeout)
        await asyncio.sleep(0.05)
    return tid, states[tid]


def _patch_for(tmp_path: Path) -> str:
    patches = sorted((tmp_path / "patches").glob("*.patch"))
    assert len(patches) == 1, patches
    return patches[0].read_text(encoding="utf-8")


def _apply_on_fresh_base(tmp_path: Path, patch: str, extra: dict[str, str] | None = None) -> Path:
    base = write_demo_repo(tmp_path / "base")
    for rel, text in (extra or {}).items():
        (base / rel).write_text(text, encoding="utf-8")
    (tmp_path / "combined.patch").write_text(patch, encoding="utf-8", newline="\n")
    git = shutil.which("git")
    assert git is not None
    for args in (["apply", "--check"], ["apply"]):
        proc = subprocess.run([git, *args, str(tmp_path / "combined.patch")], cwd=base,
                              capture_output=True, text=True, check=False)
        assert proc.returncode == 0, proc.stderr
    return base


@pytest.fixture
def levels(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Every ``ContextEngine.build_slice`` level the workers ask for (stacked engines too)."""
    seen: list[int] = []
    original = ContextEngine.build_slice

    def spy(self: ContextEngine, *args: Any, **kwargs: Any) -> Any:
        seen.append(int(kwargs.get("level", 1)))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(ContextEngine, "build_slice", spy)
    return seen


def _use_directive(f: Factory, subtasks: list[SubtaskAllocation], complexity: Complexity = "M") -> None:
    """Make the Foreman answer with exactly these subtasks (team alpha)."""

    async def size(order: Any) -> TaskSizingDirective:
        tid = order.task_id if hasattr(order, "task_id") else order["task_id"]
        subs = [s.model_copy(update={"subtask_id": f"{tid}.{i + 1}",
                                     "depends_on": [f"{tid}.{d}" for d in s.depends_on]})
                for i, s in enumerate(subtasks)]
        return TaskSizingDirective(task_id=tid, team_id="alpha", complexity=complexity,
                                   allocated_builders=len(subs), rationale="test",
                                   override_reason=None if len(subs) == 2 else "test", subtasks=subs)

    f.manager._size = size  # type: ignore[method-assign]


# --------------------------------------------------------------------------- (a) impl + tests


def test_tests_ticket_is_built_on_the_impl_ticket(tmp_path: Path) -> None:
    repo = write_demo_repo(tmp_path / "repo")
    script = Script({
        "1": edits_reply({"op": "append", "file": "shop/mathx.py", "code": CLAMP}),
        # clamp01 goes right after clamp, which only exists in the stacked workspace
        "2": edits_reply({"op": "insert_after_symbol", "file": "shop/mathx.py", "symbol": "clamp", "code": CLAMP01},
                         {"op": "create_file", "file": "tests/test_mathx.py", "code": TEST_MATHX}),
    })
    f = _factory(tmp_path, script.client, repo=repo, teams=["alpha"])
    tid, state = asyncio.run(_run(f, {
        "title": "Add clamp and test it", "description": "clamp helper plus its tests", "team_id": "alpha",
        "files": ["shop/mathx.py", "tests/test_mathx.py"]}))
    assert state == TaskState.COMPLETE.value
    tickets = {t.subtask_id: t for t in f.queue.tickets_for_task(tid)}
    impl, tests = tickets[f"{tid}.1"], tickets[f"{tid}.2"]
    assert (impl.order_index, tests.order_index) == (0, 1) and not tests.parallel_ok
    # the tests ticket's prompt lists the earlier work and shows clamp in full
    prompt = script.prompts("2")[0]
    assert "## Already done on this piece" in prompt
    assert "clamp" in prompt.split("## Already done on this piece", 1)[1]
    assert "return max(lo, min(hi, value))" in prompt
    assert "attempt 1 of" in prompt and script.client.calls["alpha-6"] == 2
    # the submitted diff is relative to the stacked workspace: only the tests ticket's change
    diffs = dict(f.queue.approved_diffs_before(tests.ticket_id))
    assert list(diffs) == [f"{tid}.1"] and "def clamp(" in diffs[f"{tid}.1"]
    # the combined patch applies on the base and contains both tickets' work
    base = _apply_on_fresh_base(tmp_path, _patch_for(tmp_path))
    mathx = (base / "shop" / "mathx.py").read_text(encoding="utf-8")
    assert "def clamp(value, lo, hi):" in mathx and "def clamp01(value):" in mathx
    assert mathx.index("def clamp(") < mathx.index("def clamp01(")
    assert (base / "tests" / "test_mathx.py").read_text(encoding="utf-8") == TEST_MATHX
    f.close()


# --------------------------------------------------------------------------- (b) sibling's files


def test_focus_ticket_may_edit_a_siblings_file(tmp_path: Path) -> None:
    extra = {"shop/a.py": '"""a."""\n\n\ndef run(value):\n    return value\n',
             "shop/config.py": '"""config."""\n\nDEBUG = False\n'}
    repo = write_demo_repo(tmp_path / "repo")
    for rel, text in extra.items():
        (repo / rel).write_text(text, encoding="utf-8")
    script = Script({
        # focus shop/a.py, but it also needs a setting in its sibling's shop/config.py
        "1": edits_reply({"op": "append", "file": "shop/config.py", "code": "def retries():\n    return 3\n"},
                         {"op": "replace_symbol", "file": "shop/a.py", "symbol": "run",
                          "code": "def run(value):\n    from shop.config import retries\n    return value * retries()\n"}),
        "2": edits_reply({"op": "insert_after_symbol", "file": "shop/config.py", "symbol": "retries",
                          "code": "def timeout():\n    return retries() * 10\n"}),
    })
    f = _factory(tmp_path, script.client, repo=repo, teams=["alpha"])
    _use_directive(f, [
        SubtaskAllocation(subtask_id="x.1", title="Retry runner", allowed_files=["shop/a.py"],
                          acceptance_criteria=["run retries"]),
        SubtaskAllocation(subtask_id="x.2", title="Timeout setting", allowed_files=["shop/config.py"],
                          acceptance_criteria=["timeout derives from retries"], depends_on=["1"]),
    ])
    tid, state = asyncio.run(_run(f, {"title": "Retries", "description": "runner + config", "team_id": "alpha",
                                      "files": ["shop/a.py", "shop/config.py"]}))
    assert state == TaskState.COMPLETE.value
    tickets = {t.subtask_id: t for t in f.queue.tickets_for_task(tid)}
    first, second = tickets[f"{tid}.1"], tickets[f"{tid}.2"]
    assert first.allowed_files[0] == "shop/a.py" and "shop/config.py" in first.allowed_files
    assert not second.parallel_ok  # disjoint files, but the Foreman marked depends_on
    assert "shop/config.py" in dict(f.queue.approved_diffs_before(second.ticket_id))[f"{tid}.1"]
    assert len(script.prompts("1")) == 1  # accepted first time: no "outside allowed files" send-back
    base = _apply_on_fresh_base(tmp_path, _patch_for(tmp_path), extra)
    config = (base / "shop" / "config.py").read_text(encoding="utf-8")
    assert "def retries():" in config and "def timeout():" in config
    assert "retries()" in (base / "shop" / "a.py").read_text(encoding="utf-8")
    f.close()


# --------------------------------------------------------------------------- (c) retry cap


def test_two_gate_failures_still_get_a_third_attempt_at_level_3(tmp_path: Path, levels: list[int]) -> None:
    good = edits_reply({"op": "append", "file": "shop/mathx.py", "code": CLAMP})
    script = Script({"1": lambda attempt: broken_reply("shop/mathx.py") if attempt < 3 else good})
    f = _factory(tmp_path, script.client, teams=["alpha"])
    tid, state = asyncio.run(_run(f, {"title": "Add clamp", "description": "clamp helper", "team_id": "alpha",
                                      "files": ["shop/mathx.py"]}))
    assert state == TaskState.COMPLETE.value
    prompts = script.prompts("1")
    assert len(prompts) == 3
    assert [f"attempt {n} of {MAX_RETRIES + 1}." in p for n, p in enumerate(prompts, 1)] == [True] * 3
    assert all("## Fix required" in p for p in prompts[1:])
    assert levels[:3] == [1, 2, 3]  # attempt 1 -> level 1, 2 -> 2, 3 -> 3
    assert "def clamp(value, lo, hi):" in _patch_for(tmp_path)
    f.close()


def test_level_3_attempt_can_fail_once_more_before_quarantine(tmp_path: Path, levels: list[int]) -> None:
    script = Script({"1": lambda attempt: broken_reply("shop/mathx.py")})
    f = _factory(tmp_path, script.client, teams=["alpha"])
    tid, state = asyncio.run(_run(f, {"title": "Add clamp", "description": "clamp helper", "team_id": "alpha",
                                      "files": ["shop/mathx.py"]}))
    assert state == TaskState.QUARANTINED.value
    assert len(script.prompts("1")) == MAX_RETRIES + 1 == 4
    assert levels[:4] == [1, 2, 3, 3]
    assert "retry limit reached" in f.queue.get_task(tid)["quarantine_reason"]
    f.close()


def test_configured_cap_is_used_by_queue_worker_and_prompt(tmp_path: Path) -> None:
    script = Script({"1": lambda attempt: broken_reply("shop/mathx.py")})
    f = _factory(tmp_path, script.client, teams=["alpha"], max_retries=1)
    assert f.queue.max_retries == 1
    tid, state = asyncio.run(_run(f, {"title": "Add clamp", "description": "clamp helper", "team_id": "alpha",
                                      "files": ["shop/mathx.py"]}))
    assert state == TaskState.QUARANTINED.value
    prompts = script.prompts("1")
    assert len(prompts) == 2 and "attempt 2 of 2." in prompts[1]
    f.close()


def test_max_retries_config_and_env() -> None:
    assert FoundryConfig().max_retries == MAX_RETRIES == 3
    assert FoundryConfig.from_env({"FOUNDRY_MAX_RETRIES": "5"}).max_retries == 5
    assert FoundryConfig.from_env({"FOUNDRY_MAX_RETRIES": ""}).max_retries == MAX_RETRIES
    for bad in ("0", str(MAX_RETRIES_LIMIT + 1), "three"):
        with pytest.raises((ValidationError, ValueError)):
            FoundryConfig.from_env({"FOUNDRY_MAX_RETRIES": bad})


def test_queue_cap_is_clamped_and_old_attempts_still_load(tmp_path: Path) -> None:
    assert FoundryQueue(":memory:").max_retries == MAX_RETRIES
    assert FoundryQueue(":memory:", max_retries=0).max_retries == 1
    assert FoundryQueue(":memory:", max_retries=99).max_retries == MAX_RETRIES_LIMIT
    q = FoundryQueue(tmp_path / "q.db", max_retries=MAX_RETRIES_LIMIT)
    q.add_task("T-1", "t", "d", "alpha")
    q.apply_sizing(TaskSizingDirective(task_id="T-1", team_id="alpha", complexity="S", allocated_builders=1,
                                       subtasks=[SubtaskAllocation(subtask_id="T-1.1", title="x")]))
    for n in range(MAX_RETRIES_LIMIT):
        t = q.claim_ticket("alpha", 1)
        assert t is not None and t.attempt == n
        assert q.reopen_ticket(t.ticket_id, f"hint {n}") is True
    t = q.claim_ticket("alpha", 1)
    assert t is not None and t.attempt == MAX_RETRIES_LIMIT  # the Ticket model accepts it
    assert q.reopen_ticket(t.ticket_id, "final") is False
    task = q.get_task("T-1")
    assert task is not None and task["state"] == TaskState.QUARANTINED
    q.close()


# --------------------------------------------------------------------------- (d) parallel_ok


def test_parallel_ok_tickets_still_run_concurrently(tmp_path: Path) -> None:
    script = Script({
        "1": edits_reply({"op": "append", "file": "shop/strings.py", "code": "def mumble(value):\n    return value\n"}),
        "2": edits_reply({"op": "append", "file": "shop/slug.py", "code": "def dash(value):\n    return value\n"}),
    }, latency=0.3)
    f = _factory(tmp_path, script.client, teams=["alpha"])
    tid, state = asyncio.run(_run(f, {"title": "Two helpers", "description": "independent helpers",
                                      "team_id": "alpha", "files": ["shop/strings.py", "shop/slug.py"]}))
    assert state == TaskState.COMPLETE.value
    tickets = sorted(f.queue.tickets_for_task(tid), key=lambda t: t.order_index)
    assert [t.parallel_ok for t in tickets] == [False, True]
    assert script.client.calls["alpha-1"] == 1 and script.client.calls["alpha-2"] == 1
    assert script.client.max_in_flight >= 2  # both builders were in Claude at once
    assert "## Already done on this piece" not in script.prompts("2")[0]
    patch = _patch_for(tmp_path)
    assert "def mumble(value):" in patch and "def dash(value):" in patch
    f.close()


def test_sequential_tickets_never_overlap(tmp_path: Path) -> None:
    script = Script({
        "1": edits_reply({"op": "append", "file": "shop/mathx.py", "code": CLAMP}),
        "2": edits_reply({"op": "create_file", "file": "tests/test_mathx.py",
                          "code": "from shop.mathx import clamp\n\n\ndef test_clamp() -> None:\n"
                                  "    assert clamp(5, 0, 3) == 3\n"}),
    }, latency=0.3)
    f = _factory(tmp_path, script.client, teams=["alpha"])
    tid, state = asyncio.run(_run(f, {"title": "Clamp and tests", "description": "clamp plus tests",
                                      "team_id": "alpha", "files": ["shop/mathx.py", "tests/test_mathx.py"]}))
    assert state == TaskState.COMPLETE.value
    assert script.client.max_in_flight == 1  # the tests ticket waited for the approved impl
    f.close()
