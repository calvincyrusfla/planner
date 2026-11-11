"""Builder sessions: a turn-limited edit-mode session continues in a fresh one with a handoff note."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from foundry.builder_worker import BuilderWorker
from foundry.claude_client import _result_from_payload
from foundry.schemas import ClaudeResult

Call = tuple[tuple[str, ...], str]


class FakeClient:
    """Pops scripted results; records (extra_args, prompt) for every run."""

    calls: list[Call] = []
    script: list[ClaudeResult | Exception] = []

    def __init__(self, claude_bin: str = "claude", extra_args: Any = (), *, env: Any = None) -> None:
        self.claude_bin = claude_bin
        self.extra_args = tuple(extra_args)
        self.env = env

    async def run(self, prompt: str, *, agent_id: str, role: Any, cwd: str | None = None,
                  timeout: float = 600) -> ClaudeResult:
        FakeClient.calls.append((self.extra_args, prompt))
        item = FakeClient.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def maxed(session_id: str | None = "sid-1", text: str = "working on it") -> ClaudeResult:
    return ClaudeResult(text=text, is_error=True, error="error_max_turns", subtype="error_max_turns",
                        session_id=session_id, num_turns=60)


def done(text: str = "```edits\n[]\n```") -> ClaudeResult:
    return ClaudeResult(text=text, session_id="sid-2", num_turns=5, subtype="success")


def run_sessions(tmp_path: Path, script: list[ClaudeResult | Exception], sessions: int | None) -> tuple[
        ClaudeResult, list[Call], list[tuple[str, str, dict[str, Any]]]]:
    FakeClient.calls = []
    FakeClient.script = list(script)
    events: list[tuple[str, str, dict[str, Any]]] = []
    worker = BuilderWorker.__new__(BuilderWorker)
    w: Any = worker
    w.client = FakeClient("claude", ["--max-turns", "60", "--disallowedTools", "Foo"])
    w.limiter = None
    w.claude_calls = 0
    w.agent_id = "alpha-1"
    w.role = "builder"
    w.claude_timeout = 30
    w.telemetry = None
    w.queue = SimpleNamespace(record_event=lambda kind, task, payload: events.append((kind, task, payload)))
    w._bases = {}
    w.cwd = str(tmp_path)
    w.config = SimpleNamespace() if sessions is None else SimpleNamespace(builder_sessions=sessions)
    options: dict[str, Any] = {"ticket_id": "T-1.1", "attempt": 1, "cwd": str(tmp_path)}
    result = asyncio.run(worker._run_builder_sessions("ORIGINAL PROMPT", "T-1", options, tmp_path, 1))
    return result, FakeClient.calls, events


def test_second_session_finishes_with_handoff(tmp_path: Path) -> None:
    result, calls, events = run_sessions(
        tmp_path, [maxed(), done("NOTE: edit foo.py line 3"), done("final")], sessions=3)
    assert result.text == "final"
    assert len(calls) == 3
    resume_args, resume_prompt = calls[1]
    assert "--resume" in resume_args and resume_args[resume_args.index("--resume") + 1] == "sid-1"
    assert resume_args[resume_args.index("--max-turns") + 1] == "2"
    assert resume_args.count("--max-turns") == 1
    disallowed = resume_args[resume_args.index("--disallowedTools") + 1]
    assert all(t in disallowed for t in ("Edit", "Write", "Bash"))
    assert resume_prompt.startswith("Stop working. Write a handoff note")
    nxt = calls[2][1]
    assert nxt.startswith("ORIGINAL PROMPT") and "## Continuing an earlier session" in nxt
    assert "NOTE: edit foo.py line 3" in nxt
    assert "do not start over" in nxt
    sessions = [e for e in events if e[0] == "builder_session"]
    assert [e[2]["session"] for e in sessions] == [1, 2]
    assert sessions[0][2]["reason"] == "max_turns" and sessions[0][2]["turns"] == 60


def test_fallback_note_without_session_id(tmp_path: Path) -> None:
    _, calls, _ = run_sessions(tmp_path, [maxed(session_id=None, text="tail text"), done()], sessions=2)
    assert len(calls) == 2
    assert "Foundry fallback note" in calls[1][1] and "tail text" in calls[1][1]


def test_fallback_note_when_resume_fails(tmp_path: Path) -> None:
    result, calls, _ = run_sessions(tmp_path, [maxed(text="last words"), RuntimeError("boom"), done()], sessions=2)
    assert result.text.startswith("```edits")
    assert "last words" in calls[2][1] and "Foundry fallback note" in calls[2][1]


def test_session_limit_returns_the_error(tmp_path: Path) -> None:
    result, calls, _ = run_sessions(tmp_path, [maxed(), done("note"), maxed()], sessions=2)
    assert result.is_error and result.error == "error_max_turns"
    assert len(calls) == 3  # session, resume, session


def test_one_session_is_todays_behaviour(tmp_path: Path) -> None:
    result, calls, _ = run_sessions(tmp_path, [maxed()], sessions=1)
    assert result.is_error and len(calls) == 1
    result, calls, _ = run_sessions(tmp_path, [maxed()], sessions=None)
    assert result.is_error and len(calls) == 1


def test_other_errors_end_the_loop(tmp_path: Path) -> None:
    err = ClaudeResult(is_error=True, error="boom", subtype="error_during_execution", num_turns=3)
    result, calls, _ = run_sessions(tmp_path, [err], sessions=3)
    assert result is err and len(calls) == 1


def test_subtype_parsed() -> None:
    r = _result_from_payload({"subtype": "error_max_turns", "is_error": True, "result": ""}, 1.0)
    assert r.subtype == "error_max_turns"
    assert _result_from_payload({"result": "x"}, 1.0).subtype is None
