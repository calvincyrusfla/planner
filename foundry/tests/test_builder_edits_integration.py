"""T-026: builders can edit existing code (structured ```edits + wider context on retry).

Reproduces the user's quarantined batch S-002 with a simulated Claude (no real
Claude, Gemma or GitHub):

(a) a ~2,000-line module where the subtask changes the inside of one function:
    the prompt shows that function's full body with line numbers, and a
    ``replace_symbol`` reply becomes a diff that ``git apply --check``s and
    passes the gates;
(b) a builder that re-defines an existing function via ``append`` is sent back
    with the "already defined" hint before any gate runs;
(c) a ``BLOCKED`` reply naming `helper_7` makes the next prompt show `helper_7`
    in full (persisted per ticket in the queue kv).
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
    BUILDER_CONTRACT,
    DIFF_CONTRACT,
    EXTRA_SYMBOLS_KV_PREFIX,
    BuilderWorker,
    TeamPool,
    build_builder_prompt,
    extract_requested_symbols,
    parse_builder_reply,
)
from foundry.claude_client import SimulatedClaudeClient
from foundry.context_engine import ContextEngine
from foundry.orchestrator_queue import FoundryQueue
from foundry.rate_limiter import RateLimiter
from foundry.schemas import ContextSlice, SubtaskAllocation, TaskAssignment, TaskSizingDirective
from foundry.solution_cache import SolutionCache
from foundry.telemetry import Telemetry
from foundry.validation_gates import ValidationGates

TEAM = "alpha"
REVIEWER = f"{TEAM}-6"
BUILDER = f"{TEAM}-1"
MODULE = "pkg/shop_foreman.py"
APPROVE = json.dumps({"verdict": "APPROVED", "directives": []})
TARGET_BODY_LINE = "    cutoff = 3.5  # anomaly cutoff for evaluate_line_anomaly"
NEW_TARGET = (
    "def evaluate_line_anomaly(values: list[float]) -> bool:\n"
    '    """Return True when the last value is an anomaly."""\n'
    "    if not values:\n"
    "        return False\n"
    "    mean = sum(values) / len(values)\n"
    "    cutoff = 4.0  # anomaly cutoff for evaluate_line_anomaly\n"
    "    return abs(values[-1] - mean) > cutoff\n"
)


def big_module(n_helpers: int = 330) -> str:
    """A lint-clean ~2,000-line module with ``evaluate_line_anomaly`` in the middle."""
    parts = ['"""A large module like foundry/shop_foreman.py."""\n', "from __future__ import annotations\n"]
    for i in range(n_helpers):
        parts.append(
            f"\n\ndef helper_{i}(x: int) -> int:\n"
            f'    """Helper number {i}."""\n'
            f"    y = x + {i}\n"
            f"    return y * 2\n"
        )
        if i == n_helpers // 2:
            parts.append(
                "\n\ndef evaluate_line_anomaly(values: list[float]) -> bool:\n"
                '    """Return True when the last value is an anomaly."""\n'
                "    if not values:\n"
                "        return False\n"
                "    mean = sum(values) / len(values)\n"
                f"{TARGET_BODY_LINE}\n"
                "    return abs(values[-1] - mean) > cutoff\n"
            )
    return "".join(parts)


def edits_reply(*edits: dict[str, Any]) -> str:
    return "Done.\n\n```edits\n" + json.dumps({"edits": list(edits)}, indent=1) + "\n```\n"


class Rig:
    """Real queue/telemetry/limiter/cache/context engine/gates over a temp workspace."""

    def __init__(self, tmp_path: Path, script: Any) -> None:
        self.ws = tmp_path / "ws"
        (self.ws / "pkg").mkdir(parents=True)
        (self.ws / "pkg" / "__init__.py").write_text("", encoding="utf-8")
        self.original = big_module()
        (self.ws / MODULE).write_text(self.original, encoding="utf-8")
        self.queue = FoundryQueue(tmp_path / "foundry.db")
        self.telemetry = Telemetry(self.queue)
        self.limiter = RateLimiter(rpm=600, tpm=10_000_000, max_concurrent=3, sleep=lambda _s: asyncio.sleep(0))
        self.cache = SolutionCache(tmp_path / "foundry.db")
        self.engine = ContextEngine(self.ws)
        self.gates = ValidationGates(checks=("syntax", "linter"))
        self.client = SimulatedClaudeClient(script)
        self.submissions: list[Any] = []

    def submit(self, task_id: str, title: str, criteria: list[str]) -> None:
        self.queue.add_task(task_id, f"Task {task_id}", title, TEAM)
        self.queue.apply_sizing(
            TaskSizingDirective(
                task_id=task_id,
                team_id=TEAM,
                complexity="S",
                allocated_builders=1,
                rationale="test",
                subtasks=[SubtaskAllocation(subtask_id=f"{task_id}.1", title=title,
                                            acceptance_criteria=criteria, allowed_files=[MODULE])],
            )
        )

    async def run(self, task_id: str, timeout: float = 20.0) -> dict[str, str]:
        pool = TeamPool(
            TEAM, 1, queue=self.queue, client=self.client, limiter=self.limiter, telemetry=self.telemetry,
            context_engine=self.engine, gates=self.gates, cache=self.cache, foreman=_Foreman(self.engine),
            poll=0.01, workspace=self.ws,
        )
        async with pool:
            states = await pool.wait_for_tasks([task_id], timeout=timeout)
            await asyncio.sleep(0.05)
        return states

    def prompts(self) -> list[str]:
        return self.client.prompts_for(BUILDER)


class _Foreman:
    def __init__(self, engine: ContextEngine) -> None:
        self.engine = engine

    def condense_error_trace(self, trace: str) -> str:
        return self.engine.condense_error_trace(trace)


def _target_line() -> int:
    return next(i for i, ln in enumerate(big_module().splitlines(), 1) if ln == TARGET_BODY_LINE)


def _git_apply_check(tmp_path: Path, original: str, diff: str) -> subprocess.CompletedProcess[str]:
    root = tmp_path / "apply_check"
    (root / "pkg").mkdir(parents=True)
    (root / MODULE).write_text(original, encoding="utf-8")
    (root / "change.diff").write_text(diff, encoding="utf-8")
    return subprocess.run(["git", "apply", "--check", "change.diff"], cwd=root,
                          capture_output=True, text=True, check=False)


def _assignment(**overrides: Any) -> TaskAssignment:
    data: dict[str, Any] = {
        "task_id": "T-1", "team_id": TEAM, "builder_id": BUILDER, "subtask_id": "T-1.1",
        "acceptance_criteria": ["cutoff is 4.0"], "allowed_files": [MODULE],
        "context": ContextSlice(objective="Raise the cutoff.", acceptance_criteria=["cutoff is 4.0"]),
    }
    data.update(overrides)
    return TaskAssignment(**data)


# ---------------------------------------------------------------------------
# contract + parser (pure)
# ---------------------------------------------------------------------------


def test_contract_offers_edits_first_and_still_accepts_diff() -> None:
    prompt = build_builder_prompt(_assignment())
    assert BUILDER_CONTRACT in prompt and DIFF_CONTRACT == BUILDER_CONTRACT
    assert BUILDER_CONTRACT.index("```edits") < BUILDER_CONTRACT.index("```diff")
    for op in ("replace_symbol", "insert_after_symbol", "insert_before_symbol", "append",
               "search_replace", "create_file", "delete_symbol"):
        assert op in BUILDER_CONTRACT
    assert '{"edits": [{"op": "replace_symbol"' in BUILDER_CONTRACT  # the tiny example
    assert "BLOCKED:" in BUILDER_CONTRACT


def test_edits_reply_becomes_a_generated_diff(tmp_path: Path) -> None:
    (tmp_path / "pkg").mkdir()
    original = big_module()
    (tmp_path / MODULE).write_text(original, encoding="utf-8")
    reply = edits_reply({"op": "replace_symbol", "file": MODULE, "symbol": "evaluate_line_anomaly", "code": NEW_TARGET})
    sub = parse_builder_reply(reply, _assignment(), workspace=tmp_path)
    assert sub.status == "ready_for_review", sub.blocker_notes
    assert sub.modified_files == [MODULE]
    assert f"--- a/{MODULE}\n+++ b/{MODULE}\n" in sub.diff
    assert "+    cutoff = 4.0" in sub.diff and f"-{TARGET_BODY_LINE}" in sub.diff
    assert sub.self_check == {"single_edits_block": True, "edits_json": True, "edits_applied": True,
                              "files_allowed": True}
    assert (tmp_path / MODULE).read_text(encoding="utf-8") == original  # workspace untouched
    if shutil.which("git"):
        res = _git_apply_check(tmp_path, original, sub.diff)
        assert res.returncode == 0, res.stderr


@pytest.mark.parametrize(
    "reply, fragment",
    [
        ("```edits\n{not json\n```\n", "not valid JSON"),
        ("```edits\n{\"edits\": []}\n```\n", "at least one edit"),
        ("```edits\n{\"edits\": [{\"op\": \"append\"}]}\n", "not closed"),
        (edits_reply({"op": "append", "file": MODULE, "code": "X = 1\n"}) * 2, "2 ```edits blocks"),
        (edits_reply({"op": "append", "file": MODULE, "code": "X = 1\n"}) + "```diff\n--- a/x\n```\n",
         "both an ```edits and a ```diff"),
        (edits_reply({"op": "append", "file": "other.py", "code": "X = 1\n"}), "edits rejected"),
        (edits_reply({"op": "search_replace", "file": MODULE, "old": "return y * 2", "new": "return y"}),
         "edits rejected"),
    ],
)
def test_bad_edits_replies_are_blocked(tmp_path: Path, reply: str, fragment: str) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / MODULE).write_text(big_module(), encoding="utf-8")
    sub = parse_builder_reply(reply, _assignment(), workspace=tmp_path)
    assert sub.status == "blocked"
    assert fragment in (sub.blocker_notes or "")


def test_edits_without_a_workspace_ask_for_a_diff() -> None:
    sub = parse_builder_reply(edits_reply({"op": "append", "file": MODULE, "code": "X = 1\n"}), _assignment())
    assert sub.status == "blocked" and "send a ```diff block" in (sub.blocker_notes or "")


def test_extract_requested_symbols_from_the_s002_blocker() -> None:
    note = ("The excerpts again leave out the body of `evaluate_line_anomaly` (lines 2358-2392) in "
            "`foundry/shop_foreman.py` and `shop_foreman.py`; also need `Shop.total()`. "
            "`git apply` rejects hunks. Body of summary is hidden too.")
    assert extract_requested_symbols(note) == ["evaluate_line_anomaly", "Shop.total", "summary"]
    assert extract_requested_symbols(None) == [] and extract_requested_symbols("no names here") == []


# ---------------------------------------------------------------------------
# retry widening: level + extra_symbols reach the context engine
# ---------------------------------------------------------------------------


class _RecordingEngine:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def build_slice(self, subtask: Any, objective: str, **kwargs: Any) -> ContextSlice:
        self.calls.append(kwargs)
        return ContextSlice(objective=objective, acceptance_criteria=list(kwargs.get("acceptance_criteria") or []))


class _LegacyEngine:
    def build_slice(self, subtask: Any, objective: str, *, acceptance_criteria: Any = None,
                    allowed_files: Any = None) -> ContextSlice:
        return ContextSlice(objective=objective)


def _worker(tmp_path: Path, engine: Any) -> BuilderWorker:
    queue = FoundryQueue(tmp_path / "q.db")
    return BuilderWorker(TEAM, 1, queue, SimulatedClaudeClient(), None, None, engine, None, None, None)


def test_attempts_widen_the_context_level_and_pass_requested_symbols(tmp_path: Path) -> None:
    engine = _RecordingEngine()
    worker = _worker(tmp_path, engine)
    ticket = {"ticket_id": 7, "task_id": "T-1", "subtask_id": "T-1.1", "title": "Raise the cutoff",
              "acceptance_criteria": ["c"], "allowed_files": [MODULE]}
    for attempt in (0, 1, 2, 5):
        asyncio.run(worker.build_assignment({**ticket, "attempt": attempt}))
    assert [c["level"] for c in engine.calls] == [1, 2, 3, 3]
    assert all("extra_symbols" not in c for c in engine.calls)
    worker._remember_requested_symbols(ticket, "builder reported BLOCKED: need the body of `helper_7`")
    worker._remember_requested_symbols(ticket, "still need `helper_7` and `Shop.total`")
    assert worker.queue.kv_get(f"{EXTRA_SYMBOLS_KV_PREFIX}7") == ["helper_7", "Shop.total"]
    asyncio.run(worker.build_assignment({**ticket, "attempt": 1}))
    assert engine.calls[-1]["extra_symbols"] == ["helper_7", "Shop.total"]
    asyncio.run(worker.build_assignment({**ticket, "description": "Make helper_9 faster"}))
    assert engine.calls[-1]["description"] == "Make helper_9 faster"
    # a fresh worker (restart) still sees them through the queue kv
    again = BuilderWorker(TEAM, 1, worker.queue, SimulatedClaudeClient(), None, None, engine, None, None, None)
    assert again._requested_symbols(ticket) == ["helper_7", "Shop.total"]


def test_engines_without_levels_still_work(tmp_path: Path) -> None:
    worker = _worker(tmp_path, _LegacyEngine())
    ticket = {"ticket_id": 1, "task_id": "T-1", "subtask_id": "T-1.1", "title": "x", "attempt": 2,
              "acceptance_criteria": [], "allowed_files": [MODULE]}
    worker._remember_requested_symbols(ticket, "BLOCKED: need `helper_7`")
    a = asyncio.run(worker.build_assignment(ticket))
    assert a.attempt == 2


# ---------------------------------------------------------------------------
# S-002 reproductions (simulated Claude, real queue/context engine/gates)
# ---------------------------------------------------------------------------


def test_s002_a_replace_symbol_inside_a_2000_line_module(tmp_path: Path) -> None:
    diffs: list[str] = []

    def script(prompt: str, agent: str) -> str:
        if agent == REVIEWER:
            diffs.append(prompt.split("## Unified diff\n```diff\n", 1)[1].split("\n```\n", 1)[0] + "\n")
            return APPROVE
        return edits_reply({"op": "replace_symbol", "file": MODULE, "symbol": "evaluate_line_anomaly",
                            "code": NEW_TARGET})

    rig = Rig(tmp_path, script)
    assert len(rig.original.splitlines()) >= 1900
    rig.submit("T-A", "Raise the cutoff inside evaluate_line_anomaly to 4.0",
               ["evaluate_line_anomaly uses a cutoff of 4.0"])
    states = asyncio.run(rig.run("T-A"))
    assert states == {"T-A": "complete"}
    prompt = rig.prompts()[0]
    # the function to change is shown in full, with its real line number
    assert TARGET_BODY_LINE.strip() in prompt
    assert "return abs(values[-1] - mean) > cutoff" in prompt
    assert f"{_target_line()}| {TARGET_BODY_LINE}" in prompt  # real line number in the gutter
    assert BUILDER_CONTRACT in prompt
    # one builder call, gates passed, reviewer saw a generated diff that git-applies on the original
    assert rig.client.calls[BUILDER] == 1 and rig.client.calls[REVIEWER] == 1
    assert rig.queue.gate_failure_count("T-A") == 0
    assert "cutoff = 4.0" in (rig.ws / MODULE).read_text(encoding="utf-8")
    assert len(diffs) == 1 and f"--- a/{MODULE}" in diffs[0]
    if shutil.which("git"):
        res = _git_apply_check(tmp_path, rig.original, diffs[0])
        assert res.returncode == 0, res.stderr


def test_s002_b_redefining_an_existing_function_is_rejected_before_the_gates(tmp_path: Path) -> None:
    duplicate = "def helper_5(x: int) -> int:\n    return x\n"
    replies = iter([
        edits_reply({"op": "append", "file": MODULE, "code": duplicate}),
        edits_reply({"op": "replace_symbol", "file": MODULE, "symbol": "helper_5", "code": duplicate}),
    ])
    rig = Rig(tmp_path, lambda p, agent: APPROVE if agent == REVIEWER else next(replies))
    rig.submit("T-B", "Simplify helper_5 to return x", ["helper_5 returns x"])
    states = asyncio.run(rig.run("T-B"))
    assert states == {"T-B": "complete"}
    prompts = rig.prompts()
    assert len(prompts) == 2
    hint = prompts[1].split("## Fix required (your previous attempt was sent back)\n", 1)[1].split("\n", 1)[0]
    assert "already defined" in hint and "replace_symbol" in hint
    assert not hint.startswith("[")  # a reply fix, not a gate ([syntax]/[typecheck]) failure
    assert rig.queue.gate_failure_count("T-B") == 0  # the duplicate never reached the gates
    text = (rig.ws / MODULE).read_text(encoding="utf-8")
    assert text.count("def helper_5(") == 1 and "def helper_5(x: int) -> int:\n    return x\n" in text


def test_s002_c_blocked_reply_naming_a_symbol_shows_it_in_full_next_time(tmp_path: Path) -> None:
    body_line = "    y = x + 7"
    replies = iter([
        "BLOCKED: the excerpts leave out the body of `helper_7`, so a hunk has no surrounding lines.",
        edits_reply({"op": "search_replace", "file": MODULE, "old": "    y = x + 7\n    return y * 2\n",
                     "new": "    y = x + 7\n    return y * 3\n"}),
    ])
    rig = Rig(tmp_path, lambda p, agent: APPROVE if agent == REVIEWER else next(replies))
    rig.submit("T-C", "Tune the shop foreman scoring", ["scores are tuned"])
    states = asyncio.run(rig.run("T-C"))
    assert states == {"T-C": "complete"}
    first, second = rig.prompts()
    assert body_line + "\n" not in first  # outline only at first
    assert body_line + "\n" in second  # requested symbol shown in full on the retry
    assert "builder reported BLOCKED" in second
    ticket_id = rig.queue.conn.execute("SELECT ticket_id FROM tickets WHERE task_id='T-C'").fetchone()[0]
    assert rig.queue.kv_get(f"{EXTRA_SYMBOLS_KV_PREFIX}{ticket_id}") == ["helper_7"]
