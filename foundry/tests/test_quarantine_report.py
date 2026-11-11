"""Tests for foundry/quarantine_report.py: rounds are driven directly with fakes."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from foundry import prompt_archive, quarantine_report
from foundry.quarantine_report import SUMMARY_CHARS, QuarantineReporter


class FakeQueue:
    def __init__(self) -> None:
        self.log: list[dict[str, Any]] = []
        self.kv: dict[str, Any] = {}

    def add(self, kind: str, task_id: str | None, payload: dict[str, Any] | None = None) -> None:
        self.log.append({"id": len(self.log) + 1, "ts": "t", "kind": kind, "task_id": task_id,
                         "payload": payload or {}})

    def events(self, *, since_id: int = 0, kind: str | None = None, task_id: str | None = None,
               limit: int | None = None) -> list[dict[str, Any]]:
        return [e for e in self.log if e["id"] > since_id and (kind is None or e["kind"] == kind)
                and (task_id is None or e["task_id"] == task_id)]

    def kv_get(self, key: str, default: Any = None) -> Any:
        return self.kv.get(key, default)

    def kv_set(self, key: str, value: Any) -> None:
        self.kv[key] = value

    def record_event(self, kind: str, task_id: str | None, payload: dict[str, Any] | None = None) -> int:
        self.add(kind, task_id, payload)
        return len(self.log)


class FakeGemma:
    pass


def make(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, gemma: Any = None,
         calls: list[dict[str, Any]] | None = None) -> tuple[QuarantineReporter, FakeQueue]:
    queue = FakeQueue()
    rows = calls or []
    monkeypatch.setattr(prompt_archive, "calls_for", lambda q, **kw: rows)
    service = SimpleNamespace(factory=SimpleNamespace(queue=queue), gemma=gemma)
    return QuarantineReporter(service, reports_dir=tmp_path), queue


def story(queue: FakeQueue, error: str = "boom") -> None:
    queue.add("submission", "F-1", {"files": ["a.py", "b.py"]})
    queue.add("gate_result", "F-1", {"check_type": "pytest", "passed": False})
    queue.add("ticket_reopened", "F-1", {"attempt": 2, "fix_hint": error})
    queue.add("submission", "F-1", {"files": ["c.py"]})
    queue.add("verdict", "F-1", {"reviewer_id": "bravo-6", "verdict": "revise"})
    queue.add("help_requested", "F-1", {"question": "which file?"})
    queue.add("retry_exhausted", "F-1", {"attempts": 3, "fix_hint": error})
    queue.add("quarantined", "F-1", {"reason": "retry limit reached"})


CALLS = [
    {"attempt": 1, "agent_id": "bravo-1", "input_tokens": 10, "output_tokens": 5},
    {"attempt": 1, "agent_id": "bravo-1", "input_tokens": 1, "output_tokens": 1},
    {"attempt": 2, "agent_id": "bravo-2", "input_tokens": 100, "output_tokens": 50},
]


def test_report_content_and_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rep, queue = make(tmp_path, monkeypatch, calls=CALLS)
    story(queue, "AssertionError: x")
    asyncio.run(rep.round())
    text = (tmp_path / "F-1.md").read_text(encoding="utf-8")
    assert text.index("## Attempt 1") < text.index("## Attempt 2") < text.index("## Last error")
    assert "- Turns: 2" in text and "- Tokens: 17" in text and "- Tokens: 150" in text
    assert "a.py, b.py" in text and "c.py" in text
    assert "gate `pytest` failed" in text
    assert "reviewer bravo-6: revise" in text
    assert "help requested: which file?" in text
    assert "AssertionError: x" in text.split("## Last error")[1]


def test_gemma_excerpt_used(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    async def fake(gemma: Any, output: str, *, timeout: float, max_chars: int) -> str | None:
        seen.append(output)
        return "PICKED LINE"

    monkeypatch.setattr(quarantine_report, "error_excerpt", fake)
    rep, queue = make(tmp_path, monkeypatch, gemma=FakeGemma())
    story(queue, "long error text")
    asyncio.run(rep.round())
    text = (tmp_path / "F-1.md").read_text(encoding="utf-8")
    assert seen == ["long error text"]
    assert "PICKED LINE" in text.split("## Last error")[1]


def test_fallback_tail_without_gemma_or_on_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    error = "x" * 3000 + "THE END"
    rep, queue = make(tmp_path, monkeypatch)
    story(queue, error)
    asyncio.run(rep.round())
    tail = (tmp_path / "F-1.md").read_text(encoding="utf-8").split("```text\n")[1].split("\n```")[0]
    assert tail == error[-1500:]

    async def none(*a: Any, **k: Any) -> str | None:
        return None

    monkeypatch.setattr(quarantine_report, "error_excerpt", none)
    rep2, queue2 = make(tmp_path / "two", monkeypatch, gemma=FakeGemma())
    story(queue2, error)
    asyncio.run(rep2.round())
    text = (tmp_path / "two" / "F-1.md").read_text(encoding="utf-8")
    assert "THE END" in text


def test_event_recorded_summary_capped_and_cursor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rep, queue = make(tmp_path, monkeypatch)
    story(queue, "e" * 5000)
    asyncio.run(rep.round())
    events = queue.events(kind="quarantine_report")
    assert len(events) == 1
    payload = events[0]["payload"]
    assert payload["path"] == str(tmp_path / "F-1.md")
    assert 0 < len(payload["summary"]) <= SUMMARY_CHARS
    assert queue.kv["quarantine_report:last_event"] == 8
    asyncio.run(rep.round())  # a restart does not repeat
    assert len(queue.events(kind="quarantine_report")) == 1


def test_round_never_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rep, queue = make(tmp_path, monkeypatch)
    story(queue)

    def broken(*a: Any, **k: Any) -> int:
        raise RuntimeError("disk")

    monkeypatch.setattr(queue, "record_event", broken)
    asyncio.run(rep.round())
    bare = QuarantineReporter(SimpleNamespace(), reports_dir=tmp_path)
    asyncio.run(bare.round())
