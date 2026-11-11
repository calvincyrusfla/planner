"""Tests for the per-turn token ledger and its wiring."""

from __future__ import annotations

import asyncio
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from foundry import turn_ledger
from foundry.builder_worker import _WorkerBase
from foundry.claude_client import _result_from_payload
from foundry.config import FoundryConfig
from foundry.orchestrator_queue import FoundryQueue
from foundry.schemas import ClaudeResult
from foundry.turn_ledger import (
    ToolResult,
    Turn,
    find_transcript,
    main,
    parse_tool_results,
    parse_turns,
    record_session,
    report,
    split_cache_reads,
)

SESSION = "11111111-2222-3333-4444-555555555555"


def _assistant(message_id: str, usage: dict[str, int], content: list[dict[str, Any]]) -> str:
    return json.dumps({"type": "assistant", "message": {"id": message_id, "usage": usage, "content": content}})


def _user(content: Any) -> str:
    return json.dumps({"type": "user", "message": {"role": "user", "content": content}})


def _usage(inp: int, creation: int, read: int, out: int) -> dict[str, int]:
    return {
        "input_tokens": inp,
        "cache_creation_input_tokens": creation,
        "cache_read_input_tokens": read,
        "output_tokens": out,
    }


FIRST = _usage(100, 900, 0, 10)
LINES = [
    _user("do the thing"),
    _assistant("m1", FIRST, [{"type": "text", "text": "looking"}]),
    _assistant("m1", FIRST, [{"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "a.py"}}]),
    "this is not json {",
    _user([{"type": "tool_result", "tool_use_id": "t1", "content": "x" * 400}]),
    _assistant("m2", _usage(10, 200, 1000, 20), [{"type": "tool_use", "id": "t2", "name": "Grep", "input": {}}]),
    _user([{"type": "tool_result", "tool_use_id": "t2", "content": [{"type": "text", "text": "y" * 80}]}]),
    _assistant("m3", _usage(5, 50, 5000, 30), [{"type": "text", "text": "done"}]),
    "[1, 2, 3]",
    json.dumps({"type": "assistant", "message": {"id": "no-usage"}}),
    json.dumps({"type": "assistant", "message": "not a dict"}),
    json.dumps({"type": "assistant", "message": {"usage": FIRST}}),
]


def _write_transcript(config_dir: Path, session_id: str = SESSION, project: str = "C--some-project") -> Path:
    path = config_dir / "projects" / project / f"{session_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(LINES) + "\n", encoding="utf-8")
    return path


@pytest.fixture
def transcript(tmp_path: Path) -> Path:
    return _write_transcript(tmp_path / "cfg")


@pytest.fixture
def queue(tmp_path: Path) -> Any:
    clock = lambda: datetime(2026, 11, 11, 3, 0, tzinfo=timezone.utc)  # noqa: E731
    with FoundryQueue(tmp_path / "q.db", clock=clock) as q:
        yield q


# --------------------------------------------------------------------------- finding


def test_find_transcript_uses_given_dir(transcript: Path, tmp_path: Path) -> None:
    assert find_transcript(SESSION, tmp_path / "cfg") == transcript


def test_find_transcript_uses_claude_config_dir(
    transcript: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    assert find_transcript(SESSION) == transcript


def test_find_transcript_falls_back_to_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    expected = _write_transcript(tmp_path / ".claude")
    assert find_transcript(SESSION) == expected


@pytest.mark.parametrize("session_id", ["", "../evil", "a\\b", "missing"])
def test_find_transcript_returns_none(transcript: Path, tmp_path: Path, session_id: str) -> None:
    assert find_transcript(session_id, tmp_path / "cfg") is None


def test_find_transcript_without_projects_folder(tmp_path: Path) -> None:
    assert find_transcript(SESSION, tmp_path / "nowhere") is None


# --------------------------------------------------------------------------- parsing


def test_parse_counts_each_message_once_and_merges_blocks(transcript: Path) -> None:
    turns = parse_turns(transcript)
    assert [t.index for t in turns] == [1, 2, 3]
    first = turns[0]
    assert (
        first.input_tokens, first.cache_creation_input_tokens, first.cache_read_input_tokens, first.output_tokens
    ) == (100, 900, 0, 10)
    assert [(u.id, u.name, u.input) for u in first.tool_uses] == [("t1", "Read", {"file_path": "a.py"})]
    assert [u.name for u in turns[1].tool_uses] == ["Grep"]
    assert turns[2].tool_uses == []


def test_parse_tool_results_sizes_and_positions(transcript: Path) -> None:
    assert parse_tool_results(transcript) == [ToolResult("t1", 100, 1), ToolResult("t2", 20, 2)]


def test_parse_tolerates_odd_usage_values(tmp_path: Path) -> None:
    path = tmp_path / "odd.jsonl"
    usage: dict[str, Any] = {"input_tokens": "many", "output_tokens": None, "cache_read_input_tokens": True}
    path.write_text(_assistant("m1", usage, []) + "\n", encoding="utf-8")
    assert parse_turns(path) == [Turn(index=1)]


# --------------------------------------------------------------------------- split


def test_split_arithmetic(transcript: Path) -> None:
    split = split_cache_reads(parse_turns(transcript), parse_tool_results(transcript))
    assert split == {
        "turns": 3,
        "input_tokens": 115,
        "cache_creation_input_tokens": 1150,
        "cache_read_input_tokens": 6000,
        "output_tokens": 60,
        "prefix_est": 3000,
        "tool_reread_est": {"Read": 200, "Grep": 20},
        "conversation_est": 2780,
    }


def test_split_floors_conversation_at_zero() -> None:
    turns = [Turn(index=1, input_tokens=500), Turn(index=2, cache_read_input_tokens=10)]
    split = split_cache_reads(turns, [])
    assert split["prefix_est"] == 1000 and split["conversation_est"] == 0


def test_split_names_results_without_a_tool_use_unknown() -> None:
    split = split_cache_reads([Turn(index=1), Turn(index=2)], [ToolResult("gone", 10, 1)])
    assert split["tool_reread_est"] == {"unknown": 10}


def test_split_of_nothing() -> None:
    split = split_cache_reads([], [])
    assert split["turns"] == 0 and split["prefix_est"] == 0 and split["tool_reread_est"] == {}


# --------------------------------------------------------------------------- storage


def test_record_session_stores_session_and_turns(queue: Any, transcript: Path, tmp_path: Path) -> None:
    assert record_session(queue, "F-1", "alpha-1", "builder", SESSION, tmp_path / "cfg") is True
    session = dict(queue.conn.execute("SELECT * FROM turn_sessions").fetchone())
    assert session["task_id"] == "F-1" and session["agent_id"] == "alpha-1" and session["role"] == "builder"
    assert session["recorded_at"] == "2026-11-11T03:00:00Z"
    assert session["turns"] == 3 and session["cache_read_input_tokens"] == 6000
    assert session["prefix_est"] == 3000 and session["conversation_est"] == 2780
    assert json.loads(session["tool_reread_est"]) == {"Read": 200, "Grep": 20}
    rows = queue.conn.execute("SELECT * FROM turn_records ORDER BY turn").fetchall()
    assert [r["turn"] for r in rows] == [1, 2, 3]
    assert (rows[1]["input_tokens"], rows[1]["output_tokens"]) == (10, 20)
    assert json.loads(rows[0]["tools"]) == ["Read"]


def test_record_session_twice_replaces(queue: Any, transcript: Path, tmp_path: Path) -> None:
    for _ in range(2):
        assert record_session(queue, "F-1", "alpha-1", "builder", SESSION, tmp_path / "cfg")
    assert queue.conn.execute("SELECT COUNT(*) FROM turn_sessions").fetchone()[0] == 1
    assert queue.conn.execute("SELECT COUNT(*) FROM turn_records").fetchone()[0] == 3


def test_record_session_missing_file_returns_false(queue: Any, tmp_path: Path) -> None:
    assert record_session(queue, "F-1", "alpha-1", "builder", SESSION, tmp_path / "cfg") is False


def test_record_session_without_turns_returns_false(queue: Any, tmp_path: Path) -> None:
    path = tmp_path / "cfg" / "projects" / "p" / f"{SESSION}.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("nonsense\n", encoding="utf-8")
    assert record_session(queue, "F-1", "alpha-1", "builder", SESSION, tmp_path / "cfg") is False


def test_record_session_never_raises_on_a_broken_queue(transcript: Path, tmp_path: Path) -> None:
    assert record_session(object(), "F-1", "a", "builder", SESSION, tmp_path / "cfg") is False  # type: ignore[arg-type]


# --------------------------------------------------------------------------- report


def test_report_prints_split_per_role(queue: Any, transcript: Path, tmp_path: Path) -> None:
    assert record_session(queue, "F-1", "alpha-1", "builder", SESSION, tmp_path / "cfg")
    text = report(queue)
    assert "builder: 1 sessions, 3.0 avg turns, 6,000 avg cache reads" in text
    assert "prefix_est: 50.0%" in text
    assert "tool_reread_est: 3.7% (Read 3.3%, Grep 0.3%)" in text
    assert "conversation_est: 46.3%" in text
    assert "estimates" in text


def test_report_keeps_only_the_top_five_tools(queue: Any, tmp_path: Path) -> None:
    uses = [{"type": "tool_use", "id": f"t{i}", "name": f"Tool{i}", "input": {}} for i in range(1, 7)]
    results = [{"type": "tool_result", "tool_use_id": f"t{i}", "content": "z" * 40 * i} for i in range(1, 7)]
    path = tmp_path / "cfg" / "projects" / "p" / f"{SESSION}.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(
        "\n".join([_assistant("m1", _usage(1, 0, 0, 1), uses), _user(results),
                   _assistant("m2", _usage(1, 0, 1000, 1), [])]) + "\n",
        encoding="utf-8",
    )
    assert record_session(queue, "F-1", "alpha-1", "reviewer", SESSION, tmp_path / "cfg")
    text = report(queue)
    assert "Tool6" in text and "Tool2" in text and "Tool1 " not in text


def test_report_since_filters_and_accepts_z(queue: Any, transcript: Path, tmp_path: Path) -> None:
    assert record_session(queue, "F-1", "alpha-1", "builder", SESSION, tmp_path / "cfg")
    assert "builder:" in report(queue, since="2026-11-11T03:00:00Z")
    assert "builder:" in report(queue, since="2026-11-11T04:00:00+01:00")
    assert report(queue, since="2026-11-11T03:00:01Z") == "No turn ledger sessions recorded."


def test_report_empty(queue: Any) -> None:
    assert report(queue) == "No turn ledger sessions recorded."


def test_cli_report(tmp_path: Path, transcript: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = tmp_path / "cli.db"
    with FoundryQueue(db) as q:
        assert record_session(q, "F-1", "alpha-6", "reviewer", SESSION, tmp_path / "cfg")
    assert main(["report", "--db", str(db)]) == 0
    assert "reviewer: 1 sessions" in capsys.readouterr().out


def test_cli_rejects_bad_since_and_missing_db(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = tmp_path / "cli.db"
    assert main(["report", "--db", str(db)]) == 1
    assert not db.exists()
    FoundryQueue(db).close()
    assert main(["report", "--db", str(db), "--since", "yesterday"]) == 2
    assert "--since" in capsys.readouterr().err


# --------------------------------------------------------------------------- result and config


def test_result_from_payload_fills_session_fields() -> None:
    result = _result_from_payload({"result": "ok", "session_id": SESSION, "num_turns": 7}, 1.0)
    assert result.session_id == SESSION and result.num_turns == 7


def test_result_from_payload_without_session_fields() -> None:
    result = _result_from_payload({"result": "ok"}, 1.0)
    assert result.session_id is None and result.num_turns is None


def test_result_from_payload_ignores_bad_session_fields() -> None:
    result = _result_from_payload({"result": "ok", "session_id": 5, "num_turns": "x"}, 1.0)
    assert result.session_id is None and result.num_turns is None


def test_config_turn_ledger_setting() -> None:
    assert FoundryConfig().turn_ledger is True
    assert FoundryConfig.from_env({"FOUNDRY_TURN_LEDGER": "off"}).turn_ledger is False


# --------------------------------------------------------------------------- worker


class _Client:
    def __init__(self, result: ClaudeResult) -> None:
        self.result = result

    async def run(self, prompt: str, **_: Any) -> ClaudeResult:
        return self.result


def _worker(setting: bool | None, session_id: str | None = SESSION) -> Any:
    worker: Any = _WorkerBase("alpha", "the-queue", _Client(ClaudeResult(text="ok", session_id=session_id)), None, None)
    worker.agent_id = "alpha-1"
    worker.role = "builder"
    if setting is not None:
        worker.config = SimpleNamespace(turn_ledger=setting)
    return worker


async def _call_and_drain(worker: Any) -> ClaudeResult:
    result: ClaudeResult = await worker._call_claude("prompt", task_id="F-1")
    while turn_ledger.BACKGROUND_TASKS:
        await asyncio.gather(*turn_ledger.BACKGROUND_TASKS)
    return result


def _spy(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, ...]]:
    calls: list[tuple[Any, ...]] = []

    def fake(*args: Any) -> bool:
        calls.append(args)
        return True

    monkeypatch.setattr(turn_ledger, "record_session", fake)
    return calls


def test_worker_records_with_setting_on(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _spy(monkeypatch)
    result = asyncio.run(_call_and_drain(_worker(True)))
    assert result.text == "ok"
    assert calls == [("the-queue", "F-1", "alpha-1", "builder", SESSION)]


@pytest.mark.parametrize(("setting", "session_id"), [(False, SESSION), (None, SESSION), (True, None)])
def test_worker_skips_recording(
    monkeypatch: pytest.MonkeyPatch, setting: bool | None, session_id: str | None
) -> None:
    calls = _spy(monkeypatch)
    asyncio.run(_call_and_drain(_worker(setting, session_id)))
    assert calls == []


def test_worker_survives_a_failing_recording(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: Any) -> bool:
        raise RuntimeError("no")

    monkeypatch.setattr(turn_ledger, "record_session", boom)
    assert asyncio.run(_call_and_drain(_worker(True))).text == "ok"


def test_worker_does_not_wait_for_the_recording(monkeypatch: pytest.MonkeyPatch) -> None:
    release = threading.Event()

    def slow(*args: Any) -> bool:
        return release.wait(10)

    monkeypatch.setattr(turn_ledger, "record_session", slow)

    async def scenario() -> bool:
        worker = _worker(True)
        await asyncio.wait_for(worker._call_claude("prompt", task_id="F-1"), timeout=5)
        running = bool(turn_ledger.BACKGROUND_TASKS)
        release.set()
        while turn_ledger.BACKGROUND_TASKS:
            await asyncio.gather(*turn_ledger.BACKGROUND_TASKS)
        return running

    assert asyncio.run(scenario()) is True
