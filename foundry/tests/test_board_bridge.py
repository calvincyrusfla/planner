"""Tests for foundry.board_bridge (never touches the real dashboard or shared board)."""

from __future__ import annotations

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

from foundry.board_bridge import BoardBridge, cli_args, parse_cli_add
from foundry.schemas import ForemanEscalation


def _alert(**target: str) -> ForemanEscalation:
    return ForemanEscalation(
        alert_id="ALT-20261107-001",
        timestamp="2026-11-07T12:00:00Z",
        severity="HIGH",
        category="LINE_JAM",
        target_entity=target or {"task_id": "F-1"},
        metrics_snapshot={"queue_depth": 1, "dwell_seconds": 0, "gate_failure_count": 2, "current_tpm": 0},
        diagnosis="Task F-1 failed its gates twice.",
        prescribed_action="QUARANTINE_TASK",
        action_parameters={},
    )


def test_dry_mode_records_resolved_ops() -> None:
    bridge = BoardBridge(mode="dry")
    bridge.register_task("F-1", "Add login", "desc", complexity="L", builders=5, team_id="alpha")
    bridge.progress("F-1", "alpha-1 started", team_id="alpha", agent_id="alpha-1", pct=20)
    bridge.review("F-1", "alpha", "APPROVED", note="ok")
    bridge.done("F-1", "feat: add login")
    types = [op["type"] for op in bridge.ops]
    assert types == ["addTask", "assignTeam", "reportProgress", "updateTask", "reviewTask", "reportProgress", "updateTask"]
    add = bridge.ops[0]["args"]
    # always "small" (a large board task would grab every free team), crew = Foundry allocation
    assert add == {"title": "Add login", "description": "[Foundry F-1 · L] desc", "size": "small", "builders": 5}
    board_id = bridge.board_ids["F-1"]
    assert bridge.ops[1]["args"] == {"taskId": board_id, "teamId": "alpha", "focus": "Foundry L", "by": "foundry"}
    assert bridge.ops[2]["args"] == {"taskId": board_id, "message": "alpha-1 started", "teamId": "alpha", "agentId": "alpha-1", "progress": 20}
    assert bridge.ops[3]["args"]["patch"] == {"status": "review"}
    assert bridge.ops[4]["args"]["verdict"] == "approved" and bridge.ops[4]["args"]["by"] == "alpha-6"
    assert bridge.ops[-1]["args"] == {"id": board_id, "patch": {"status": "done"}, "by": "foundry"}


def test_board_autoassigned_teams_are_unassigned_except_the_foundry_team() -> None:
    bridge = BoardBridge(mode="dry", dry_autoassign=("alpha", "bravo"))
    bridge.register_task("F-1", "t", complexity="S", builders=1, team_id="charlie")
    ops = [(op["type"], op["args"].get("teamId")) for op in bridge.ops]
    assert ops == [("addTask", None), ("unassignTeam", "alpha"), ("unassignTeam", "bravo"), ("assignTeam", "charlie")]
    bridge2 = BoardBridge(mode="dry", dry_autoassign=("alpha", "charlie"))
    bridge2.register_task("F-1", "t", complexity="M", builders=2, team_id="charlie")
    assert [(op["type"], op["args"].get("teamId")) for op in bridge2.ops] == [("addTask", None), ("unassignTeam", "alpha")]


def test_register_is_idempotent_and_restore_skips_known_tasks() -> None:
    bridge = BoardBridge(mode="dry")
    bridge.register_task("F-1", "t", team_id="alpha")
    bridge.register_task("F-1", "t", team_id="alpha")
    assert [op["type"] for op in bridge.ops].count("addTask") == 1
    restarted = BoardBridge(mode="dry")
    restarted.restore({"F-1": "T-042"})
    restarted.register_task("F-1", "t", team_id="alpha")
    restarted.progress("F-1", "back")
    assert [op["type"] for op in restarted.ops] == ["reportProgress"]
    assert restarted.ops[0]["args"]["taskId"] == "T-042"


def test_small_sizes_map_to_small_and_builders_clamped() -> None:
    bridge = BoardBridge(mode="dry")
    bridge.register_task("F-2", "Fix typo", complexity="S", builders=9)
    assert bridge.ops[0]["args"]["size"] == "small"
    assert bridge.ops[0]["args"]["builders"] == 5


def test_escalation_message_format() -> None:
    bridge = BoardBridge(mode="dry")
    bridge.register_task("F-1", "t", complexity="S", builders=1)
    bridge.escalation(_alert(task_id="F-1", team_id="alpha"))
    op = bridge.ops[-1]
    assert op["type"] == "reportProgress"
    assert op["args"]["message"].startswith("[Foreman] HIGH LINE_JAM: Task F-1 failed its gates twice.")
    assert op["args"]["teamId"] == "alpha"


def test_escalation_without_task_uses_latest_registered_or_skips() -> None:
    bridge = BoardBridge(mode="dry")
    bridge.escalation(_alert(team_id="alpha"))  # nothing registered: skipped, no crash
    assert bridge.ops == []
    bridge.register_task("F-9", "t")
    bridge.escalation(_alert(team_id="alpha"))
    assert bridge.ops[-1]["args"]["taskId"] == bridge.board_ids["F-9"]


def test_off_mode_drops_everything() -> None:
    bridge = BoardBridge(mode="off")
    bridge.register_task("F-1", "t")
    bridge.progress("F-1", "m")
    assert bridge.ops == []


def test_bad_mode_rejected() -> None:
    with pytest.raises(ValueError):
        BoardBridge(mode="loud")  # type: ignore[arg-type]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_auto_mode_unreachable_never_blocks_or_raises(tmp_path: Path) -> None:
    port = _free_port()  # nothing listens here
    bridge = BoardBridge(mode="auto", dashboard_url=f"http://127.0.0.1:{port}", timeout=0.5, cli_path=tmp_path / "missing.mjs")
    t0 = time.monotonic()
    for i in range(20):
        bridge.register_task(f"F-{i}", "t")
        bridge.progress(f"F-{i}", "m")
    assert time.monotonic() - t0 < 0.5  # emitting is non-blocking
    assert bridge.flush(5.0)
    bridge.close()
    assert bridge.stats.sent == 0
    assert bridge.stats.dropped >= 20
    assert len(bridge.stats.warnings) == 1


class _FakeDashboard(BaseHTTPRequestHandler):
    received: list[dict[str, Any]] = []

    def do_POST(self) -> None:  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        type(self).received.append(body)
        if body["type"] == "addTask":
            result: Any = {"task": {"id": "T-077"}, "assigned": ["alpha", "bravo"]}
        elif body["type"] == "reviewTask":
            self.send_response(400)
            self.end_headers()
            self.wfile.write(json.dumps({"ok": False, "error": "T-077 is not in review"}).encode())
            return
        else:
            result = {"ok": True}
        data = json.dumps({"ok": True, "result": result}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args: Any) -> None:
        pass


def test_auto_mode_posts_to_dashboard_and_resolves_board_id(tmp_path: Path) -> None:
    _FakeDashboard.received = []
    server = HTTPServer(("127.0.0.1", 0), _FakeDashboard)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        bridge = BoardBridge(mode="auto", dashboard_url=f"http://127.0.0.1:{server.server_port}", cli_path=tmp_path / "x.mjs")
        bridge.register_task("F-1", "Add login", complexity="M", builders=2, team_id="alpha")
        bridge.progress("F-1", "hello", agent_id="alpha-1")
        bridge.review("F-1", "alpha", "REVISE")  # board refuses reviewTask: logged, not raised
        assert bridge.flush(5.0)
        bridge.close()
    finally:
        server.shutdown()
    assert bridge.board_ids == {"F-1": "T-077"}
    assert [r["type"] for r in _FakeDashboard.received] == ["addTask", "unassignTeam", "reportProgress", "updateTask", "reviewTask"]
    assert _FakeDashboard.received[1]["args"] == {"taskId": "T-077", "teamId": "bravo", "by": "foundry"}
    assert _FakeDashboard.received[2]["args"]["taskId"] == "T-077"
    assert _FakeDashboard.received[4]["args"]["verdict"] == "changes"
    assert bridge.stats.transport == "dashboard"


def test_cli_args_translation() -> None:
    assert cli_args({"type": "addTask", "args": {"title": "x", "description": "d", "size": "large", "builders": 5}}) == [
        "add", "x", "--desc", "d", "--large", "--builders", "5",
    ]
    assert cli_args({"type": "reportProgress", "args": {"taskId": "T-1", "message": "m", "agentId": "alpha-1", "progress": 10}}) == [
        "progress", "T-1", "-m", "m", "--agent", "alpha-1", "--pct", "10",
    ]
    assert cli_args({"type": "updateTask", "args": {"id": "T-1", "patch": {"status": "review"}}}) == ["review", "T-1"]
    assert cli_args({"type": "updateTask", "args": {"id": "T-1", "patch": {"status": "done"}}}) == ["done", "T-1"]
    assert cli_args({"type": "reviewTask", "args": {"taskId": "T-1", "teamId": "alpha", "verdict": "approved", "note": "n", "issues": 0}})[:5] == [
        "verdict", "T-1", "--team", "alpha", "approve",
    ]
    assert cli_args({"type": "unassignTeam", "args": {"taskId": "T-1", "teamId": "bravo"}}) == ["unassign", "T-1", "bravo"]
    assert cli_args({"type": "dispatch", "args": {}}) is None


def test_parse_cli_add_output() -> None:
    assert parse_cli_add("T-012 created → alpha, bravo · 2 builders + reviewer per team\n") == {
        "task": {"id": "T-012"}, "assigned": ["alpha", "bravo"],
    }
    assert parse_cli_add("T-013 created → queued (no free team) · 1 builder + reviewer per team") == {
        "task": {"id": "T-013"}, "assigned": [],
    }
    assert parse_cli_add("Error: nope") is None
