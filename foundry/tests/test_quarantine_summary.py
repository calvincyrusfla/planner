"""subtask_state: a quarantined piece's board error carries the quarantine report's summary."""

from __future__ import annotations

from typing import Any

from foundry.board_dispatch import subtask_state

REASON = "gate failed: tests"


class FakeQueue:
    def __init__(self, events: list[dict[str, Any]] | None = None, fail: bool = False,
                 state: str = "quarantined") -> None:
        self._events = events or []
        self.fail = fail
        self.state = state
        self.calls: list[dict[str, Any]] = []

    def get_task(self, tid: str) -> dict[str, Any]:
        return {"state": self.state, "quarantine_reason": REASON}

    def events(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError("db locked")
        return self._events


class FakeManager:
    sizing_errors: dict[str, str] = {}


def _event(summary: str, path: str = "/r/F-1.md") -> dict[str, Any]:
    return {"kind": "quarantine_report", "task_id": "F-1", "payload": {"summary": summary, "path": path}}


def test_summary_is_added() -> None:
    q = FakeQueue([_event("old"), _event("what happened")])
    r = subtask_state(FakeManager(), q, "F-1")
    assert r["error"] == f"{REASON}\n\nReport: what happened\n\nFull report: /r/F-1.md on the PC"
    assert q.calls == [{"task_id": "F-1", "kind": "quarantine_report"}]


def test_without_report_error_is_unchanged() -> None:
    assert subtask_state(FakeManager(), FakeQueue(), "F-1")["error"] == REASON


def test_summary_is_capped() -> None:
    r = subtask_state(FakeManager(), FakeQueue([_event("s" * 5000)]), "F-1")
    assert len(r["error"]) == 2000
    assert r["error"].startswith(f"{REASON}\n\nReport: ss")


def test_failing_events_falls_back() -> None:
    assert subtask_state(FakeManager(), FakeQueue([_event("x")], fail=True), "F-1")["error"] == REASON


def test_not_quarantined_does_not_read_events() -> None:
    q = FakeQueue([_event("x")], state="failed")
    assert subtask_state(FakeManager(), q, "F-1")["error"] == REASON
    assert q.calls == []
