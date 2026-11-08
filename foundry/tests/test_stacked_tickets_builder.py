"""T-027 (alpha-2): a ticket is built on its task's earlier approved tickets.

The builder prepares a scratch workspace = repo + the earlier approved diffs
(in task order), builds the context slice from it, resolves ```edits replies
against it and gates the diff on top of it, so the submitted diff is relative
to the stacked workspace. The prompt lists the earlier work under "Already
done on this piece". Simulated Claude only (no real Claude/Gemma/GitHub).
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from foundry.builder_worker import (
    BuilderWorker,
    StackedWork,
    build_builder_prompt,
    stacked_work,
    summarize_diff,
)
from foundry.claude_client import SimulatedClaudeClient
from foundry.context_engine import ContextEngine
from foundry.floor_manager import WorkspaceGates
from foundry.orchestrator_queue import FoundryQueue
from foundry.schemas import ContextSlice, SubtaskAllocation, TaskAssignment, TaskSizingDirective
from foundry.validation_gates import DiffApplyError, ValidationGates, prepare_stacked_workspace

TEAM = "alpha"
CALC = "pkg/calc.py"
TESTS = "tests/test_calc.py"
BASE_CALC = '"""Calculator."""\n\n\ndef add(a: int, b: int) -> int:\n    return a + b\n'

#: T-9.1 (impl): adds ``mul`` to pkg/calc.py (relative to the base).
IMPL_DIFF = (
    f"--- a/{CALC}\n+++ b/{CALC}\n"
    "@@ -3,3 +3,7 @@\n"
    " \n"
    " def add(a: int, b: int) -> int:\n"
    "     return a + b\n"
    "+\n"
    "+\n"
    "+def mul(a: int, b: int) -> int:\n"
    "+    return a * b\n"
)

#: T-9.2 (tests) reply: a new test file plus a helper placed after ``mul``, which
#: only exists in the stacked workspace.
TESTS_CODE = (
    "from pkg.calc import mul, square\n\n\n"
    "def test_mul() -> None:\n"
    "    assert mul(2, 3) == 6\n"
    "    assert square(3) == 9\n"
)
SQUARE = "def square(a: int) -> int:\n    return mul(a, a)\n"


def stacked_reply() -> str:
    edits = [
        {"op": "insert_after_symbol", "file": CALC, "symbol": "mul", "code": SQUARE},
        {"op": "create_file", "file": TESTS, "code": TESTS_CODE},
    ]
    return "Done.\n\n```edits\n" + json.dumps({"edits": edits}, indent=1) + "\n```\n"


def make_repo(root: Path) -> Path:
    (root / "pkg").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "tests" / "__init__.py").write_text("", encoding="utf-8")
    (root / CALC).write_text(BASE_CALC, encoding="utf-8")
    return root


OTHER = "pkg/other.py"
OTHER_SRC = '"""Unrelated."""\n\n\ndef zap(value: int) -> int:\n    secret = value * 7\n    return secret\n'



class _Foreman:
    def condense_error_trace(self, trace: str) -> str:
        return trace[-300:]


class Rig:
    """Real queue + context engine + WorkspaceGates over a temp repo; simulated Claude."""

    def __init__(self, tmp_path: Path, reply: Any) -> None:
        self.repo = make_repo(tmp_path / "repo")
        self.queue = FoundryQueue(str(tmp_path / "foundry.db"))
        self.engine = ContextEngine(self.repo)
        self.gates = WorkspaceGates(ValidationGates(checks=("syntax", "linter")), self.repo)
        self.client = SimulatedClaudeClient(lambda _p, _a: reply)
        self.queue.add_task("T-9", "Calculator", "Add mul and test it", TEAM)
        self.queue.apply_sizing(TaskSizingDirective(
            task_id="T-9", team_id=TEAM, complexity="M", allocated_builders=2, rationale="test",
            subtasks=[
                SubtaskAllocation(subtask_id="T-9.1", title="Add mul to the calculator",
                                  acceptance_criteria=["mul multiplies"], allowed_files=[CALC]),
                SubtaskAllocation(subtask_id="T-9.2", title="Test the calculator",
                                  acceptance_criteria=["tests cover the new helper"], allowed_files=[TESTS]),
            ],
        ))
        self.worker = BuilderWorker(TEAM, 2, self.queue, self.client, None, None, self.engine,
                                    self.gates, None, _Foreman(), poll=0.01)

    def ticket(self, subtask_id: str) -> Any:
        return next(t for t in self.queue.tickets_for_task("T-9") if t.subtask_id == subtask_id)

    def approve_impl(self, diff: str = IMPL_DIFF) -> None:
        t = self.ticket("T-9.1")
        claimed = self.queue.claim_ticket(TEAM, t.slot)
        assert claimed is not None and claimed.ticket_id == t.ticket_id
        self.queue.record_approved_diff(t.ticket_id, diff)
        self.queue.complete_ticket(t.ticket_id)

    def claim_tests(self) -> Any:
        t = self.ticket("T-9.2")
        claimed = self.queue.claim_ticket(TEAM, t.slot)
        assert claimed is not None and claimed.ticket_id == t.ticket_id
        return claimed


def _git_apply_check(root: Path, patch: str) -> subprocess.CompletedProcess[str]:
    (root.parent / "check.patch").write_text(patch, encoding="utf-8", newline="\n")
    return subprocess.run(["git", "apply", "--check", str(root.parent / "check.patch")], cwd=root,
                          capture_output=True, text=True, check=False)


# ---------------------------------------------------------------------------
# pure helpers
# ---------------------------------------------------------------------------


def test_summarize_diff_lists_files_and_new_symbols() -> None:
    diff = IMPL_DIFF + (
        "--- /dev/null\n+++ b/pkg/config.py\n@@ -0,0 +1,4 @@\n"
        "+MAX_X = 3\n+class FoundryConfig:\n+    async def load(self) -> None:\n+        pass\n"
    )
    files, symbols = summarize_diff(diff)
    assert files == [CALC, "pkg/config.py"]
    assert symbols == ["mul", "MAX_X", "FoundryConfig", "load"]


def test_hunk_context_names_the_changed_function() -> None:
    diff = f"--- a/{CALC}\n+++ b/{CALC}\n@@ -4,2 +4,2 @@ def add(a: int, b: int) -> int:\n-    return a + b\n+    return b + a\n"
    assert summarize_diff(diff) == ([CALC], ["add"])


def _assignment() -> TaskAssignment:
    return TaskAssignment(task_id="T-9", team_id=TEAM, builder_id="alpha-2", subtask_id="T-9.2",
                          acceptance_criteria=["tests cover mul"], allowed_files=[TESTS, CALC],
                          context=ContextSlice(objective="Test mul.", acceptance_criteria=["x"]))


def test_prompt_lists_already_done_work() -> None:
    done = stacked_work([("T-9.1", IMPL_DIFF)], {"T-9.1": "Add mul to the calculator"})
    assert done == [StackedWork("T-9.1", "Add mul to the calculator", (CALC,), ("mul",))]
    prompt = build_builder_prompt(_assignment(), already_done=done)
    section = prompt.split("## Already done on this piece", 1)[1].split("## Output contract", 1)[0]
    assert "- T-9.1: Add mul to the calculator" in section
    assert f"files: {CALC}" in section and "`mul`" in section
    assert "Build on these; don't redo them" in section
    # placed after the file list, before the output contract
    assert prompt.index("## Files you may modify") < prompt.index("## Already done") < prompt.index("## Output contract")


def test_prompt_without_earlier_work_has_no_section() -> None:
    assert "Already done" not in build_builder_prompt(_assignment())
    assert "Already done" not in build_builder_prompt(_assignment(), already_done=[])


def test_prepare_stacked_workspace_applies_in_order(tmp_path: Path) -> None:
    base = make_repo(tmp_path / "base")
    (base / ".git").mkdir()  # never copied
    second = (
        f"--- a/{CALC}\n+++ b/{CALC}\n@@ -7,2 +7,6 @@\n"
        " def mul(a: int, b: int) -> int:\n     return a * b\n+\n+\n+def sub(a: int, b: int) -> int:\n+    return a - b\n"
    )
    root = prepare_stacked_workspace(base, tmp_path / "stack", [("T-9.1", IMPL_DIFF), ("T-9.3", ""), ("T-9.2", second)])
    text = (root / CALC).read_text(encoding="utf-8")
    assert "def mul" in text and "def sub" in text and text.index("def mul") < text.index("def sub")
    assert not (root / ".git").exists()
    assert (base / CALC).read_text(encoding="utf-8") == BASE_CALC  # base untouched


def test_prepare_stacked_workspace_names_the_diff_that_does_not_stack(tmp_path: Path) -> None:
    base = make_repo(tmp_path / "base")
    with pytest.raises(DiffApplyError, match="T-9.2"):
        prepare_stacked_workspace(base, tmp_path / "stack", [("T-9.2", IMPL_DIFF.replace("a + b", "a - b"))])


def test_workspace_gates_validate_in_the_given_workspace_without_copying(tmp_path: Path) -> None:
    base = make_repo(tmp_path / "base")
    stacked = prepare_stacked_workspace(base, tmp_path / "stack", [IMPL_DIFF])
    gates = WorkspaceGates(ValidationGates(checks=("syntax",)), base)
    diff = (f"--- a/{CALC}\n+++ b/{CALC}\n@@ -8,1 +8,1 @@\n-    return a * b\n+    return b * a\n")
    from foundry.schemas import BuilderSubmission
    sub = BuilderSubmission(task_id="T-9", subtask_id="T-9.2", builder_id="alpha-2", status="ready_for_review",
                            modified_files=[CALC], diff=diff)
    asg = _assignment()
    assert not asyncio.run(gates.run(sub, asg)).passed  # `mul` is not in the base
    assert asyncio.run(gates.run(sub, asg, workspace=stacked)).passed
    assert "return b * a" in (stacked / CALC).read_text(encoding="utf-8")  # patched in place, no copy
    assert (base / CALC).read_text(encoding="utf-8") == BASE_CALC


def test_stack_is_strict_like_the_combined_patch(tmp_path: Path) -> None:
    """A hunk whose context does not match exactly is refused (git apply, no fuzz)."""
    base = make_repo(tmp_path / "base")
    fuzzy = IMPL_DIFF.replace(" def add(a: int, b: int) -> int:\n", " def add(a: int, b: int) -> int:  \n")
    if not shutil.which("git"):
        pytest.skip("git not installed")
    with pytest.raises(DiffApplyError, match="T-9.1"):
        prepare_stacked_workspace(base, tmp_path / "stack", [("T-9.1", fuzzy)])


# ---------------------------------------------------------------------------
# the worker
# ---------------------------------------------------------------------------


def test_tests_ticket_builds_on_the_approved_impl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rig = Rig(tmp_path, stacked_reply())
    made: list[Path] = []
    real_mkdtemp = __import__("tempfile").mkdtemp

    def spy_mkdtemp(*a: Any, **k: Any) -> str:
        path = real_mkdtemp(*a, **k)
        made.append(Path(path))
        return path

    monkeypatch.setattr("foundry.builder_worker.tempfile.mkdtemp", spy_mkdtemp)
    rig.approve_impl()
    ticket = rig.claim_tests()

    outcome = asyncio.run(rig.worker.process_ticket(ticket))
    assert outcome == "review", rig.worker.scratch.get("last_reply")
    # one repo copy per attempt: the stacked copy is also the gate workspace
    assert [p.name.split("-")[1] for p in made] == ["stack"]

    prompt = rig.client.prompts_for("alpha-2")[0]
    assert "## Your files\n- tests/test_calc.py\n" in prompt
    also = prompt.split("## You may also touch", 1)[1].split("##", 1)[0]
    assert f"- {CALC}" in also and TESTS not in also
    assert "## Already done on this piece" in prompt
    assert "- T-9.1: Add mul to the calculator" in prompt and "`mul`" in prompt
    context = prompt.split("## Context", 1)[1]
    assert "def mul(a: int, b: int) -> int:" in context  # the sibling's new function, in the slice

    item = rig.worker.review_inbox.get_nowait()
    diff = item.submission.diff
    assert "+def square" in diff and "+def mul" not in diff  # relative to the stacked workspace
    assert sorted(item.submission.modified_files) == sorted([CALC, TESTS])
    assert (rig.repo / CALC).read_text(encoding="utf-8") == BASE_CALC  # repo never modified
    assert not (rig.repo / TESTS).exists()
    assert made and not any(p.exists() for p in made)  # stack + gate scratch dirs cleaned up

    # stacked combined patch: impl then tests applies on the base
    final = prepare_stacked_workspace(rig.repo, tmp_path / "final", [IMPL_DIFF, diff])
    assert "return mul(a, a)" in (final / CALC).read_text(encoding="utf-8")
    if shutil.which("git"):
        assert _git_apply_check(rig.repo, IMPL_DIFF).returncode == 0
        on_impl = prepare_stacked_workspace(rig.repo, tmp_path / "on_impl", [IMPL_DIFF])
        res = _git_apply_check(on_impl, diff)
        assert res.returncode == 0, res.stderr


def test_context_takes_focus_files_first_and_others_as_outlines(tmp_path: Path) -> None:
    rig = Rig(tmp_path, stacked_reply())
    (rig.repo / OTHER).write_text(OTHER_SRC, encoding="utf-8")
    t = rig.ticket("T-9.2")
    # the task's union also holds an unrelated sibling file nobody names
    ticket = t.model_copy(update={"allowed_files": [TESTS, CALC, OTHER], "focus_files": [TESTS]})
    rig.approve_impl()
    rig.worker.approved_diffs_before = lambda _tid: [("T-9.1", IMPL_DIFF)]
    info = rig.worker._subtask_info(ticket)
    assert info["focus_files"] == [TESTS]
    rig.worker._stack = asyncio.run(rig.worker._prepare_stack(ticket))
    try:
        ctx, _ = asyncio.run(rig.worker._context_for(ticket, info))
    finally:
        shutil.rmtree(rig.worker._stack.tmp, ignore_errors=True)  # type: ignore[union-attr]
        rig.worker._stack = None
    text = "\n".join(f"### {s.path}\n{s.text}" for s in ctx.snippets)
    assert "def mul(a: int, b: int) -> int:" in text and "return a * b" in text  # named by earlier work: full
    assert "secret = value * 7" not in text  # unnamed sibling file: no body


def test_focus_equal_to_allowed_keeps_the_single_list() -> None:
    prompt = build_builder_prompt(_assignment(), focus_files=[TESTS, CALC])
    assert "## Files you may modify" in prompt and "## You may also touch" not in prompt


def test_first_ticket_is_not_stacked(tmp_path: Path) -> None:
    impl_reply = "```diff\n" + IMPL_DIFF + "```\n"
    rig = Rig(tmp_path, impl_reply)
    t = rig.ticket("T-9.1")
    ticket = rig.queue.claim_ticket(TEAM, t.slot)
    worker = BuilderWorker(TEAM, t.slot, rig.queue, rig.client, None, None, rig.engine, rig.gates, None, _Foreman())
    assert asyncio.run(worker.process_ticket(ticket)) == "review"
    prompt = rig.client.prompts_for(worker.agent_id)[0]
    assert "Already done" not in prompt
    assert worker.review_inbox.get_nowait().submission.diff.count("+def mul") == 1


def test_unstackable_earlier_diff_releases_without_calling_claude(tmp_path: Path) -> None:
    rig = Rig(tmp_path, stacked_reply())
    rig.approve_impl(IMPL_DIFF.replace("a + b", "a - b"))  # no longer matches the repo
    ticket = rig.claim_tests()
    assert asyncio.run(rig.worker.process_ticket(ticket)) == "released"
    assert rig.client.calls["alpha-2"] == 0
    after = rig.queue.get_ticket(ticket.ticket_id)
    assert after is not None and after.status == "open" and after.attempt == ticket.attempt
    assert rig.worker._stack is None


def test_explicit_provider_overrides_the_queue(tmp_path: Path) -> None:
    rig = Rig(tmp_path, stacked_reply())
    rig.approve_impl()
    ticket = rig.claim_tests()
    seen: list[str] = []

    def provider(ticket_id: str) -> list[tuple[str, str]]:
        seen.append(ticket_id)
        return [("T-9.1", IMPL_DIFF)]

    rig.worker.approved_diffs_before = provider
    assert asyncio.run(rig.worker.process_ticket(ticket)) == "review"
    assert seen == [ticket.ticket_id]


# ---------------------------------------------------------------------------
# a reopened earlier ticket releases later tickets built on it
# ---------------------------------------------------------------------------


class _FakeQueue:
    def __init__(self, tickets: list[dict[str, Any]]) -> None:
        self.t = {d["ticket_id"]: dict(d) for d in tickets}
        self.events: list[tuple[str, Any]] = []

    def tickets_for_task(self, task_id: str) -> list[dict[str, Any]]:
        return [d for d in self.t.values() if d["task_id"] == task_id]

    def get_ticket(self, ticket_id: str) -> dict[str, Any]:
        return self.t[ticket_id]

    def reopen_ticket(self, ticket_id: str, hint: str | None) -> bool:
        self.t[ticket_id].update(status="open", attempt=self.t[ticket_id]["attempt"] + 1)
        return True

    def release_ticket(self, ticket_id: str, reason: str = "") -> bool:
        if self.t[ticket_id]["status"] != "claimed":
            return False
        self.t[ticket_id]["status"] = "open"
        self.events.append(("released", ticket_id))
        return True

    def get_task(self, task_id: str) -> dict[str, Any]:
        return {"task_id": task_id, "state": "building"}


def _t(tid: str, order: int, status: str, parallel_ok: bool = False) -> dict[str, Any]:
    return {"ticket_id": tid, "task_id": "T-1", "subtask_id": tid, "order_index": order, "status": status,
            "attempt": 0, "parallel_ok": parallel_ok}


def test_reopening_an_earlier_ticket_releases_later_claimed_ones() -> None:
    q = _FakeQueue([
        _t("A", 0, "claimed"),
        _t("B", 1, "claimed"),              # built on A: released
        _t("C", 2, "open"),                 # not claimed: nothing to do
        _t("D", 3, "claimed", parallel_ok=True),  # independent: keeps running
        _t("E", 4, "done"),
    ])
    w = BuilderWorker(TEAM, 1, q, SimulatedClaudeClient(), None, None, None, None, None, None)
    assert w._send_back(q.get_ticket("A") | {}, "fix it", reason="revise") == "retry"
    assert q.events == [("released", "B")]
    assert q.t["B"]["attempt"] == 0 and q.t["D"]["status"] == "claimed"


def test_queue_release_later_tickets_is_preferred() -> None:
    q = _FakeQueue([_t("A", 0, "claimed"), _t("B", 1, "claimed")])
    calls: list[str] = []
    q.release_later_tickets = lambda tid: calls.append(tid) or ["B"]  # type: ignore[attr-defined]
    w = BuilderWorker(TEAM, 1, q, SimulatedClaudeClient(), None, None, None, None, None, None)
    assert w._send_back(dict(q.get_ticket("A")), "fix", reason="revise") == "retry"
    assert calls == ["A"] and q.events == []
