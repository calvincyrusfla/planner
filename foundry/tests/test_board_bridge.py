"""Tests for foundry.board_bridge (never touches the real dashboard or shared board)."""

from __future__ import annotations

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
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
    assert ops == [("addTask", None), ("assignTeam", "charlie"), ("unassignTeam", "alpha"), ("unassignTeam", "bravo")]
    assert bridge.board_teams["F-1"] == ["charlie"]
    bridge2 = BoardBridge(mode="dry", dry_autoassign=("alpha", "charlie"))
    bridge2.register_task("F-1", "t", complexity="M", builders=2, team_id="charlie")
    assert [(op["type"], op["args"].get("teamId")) for op in bridge2.ops] == [("addTask", None), ("unassignTeam", "alpha")]


def test_busy_foundry_team_is_never_moved_and_every_board_team_signs_off() -> None:
    # charlie is on another open board task; the board auto-assigned alpha and bravo to the new one.
    bridge = BoardBridge(mode="dry", dry_autoassign=("alpha", "bravo"), dry_busy=("charlie",))
    bridge.register_task("F-1", "t", complexity="S", builders=1, team_id="charlie")
    assert [op["type"] for op in bridge.ops] == ["addTask"]  # no assignTeam(charlie), no unassign
    assert bridge.board_teams["F-1"] == ["alpha", "bravo"]
    bridge.review("F-1", "charlie", "APPROVED", note="ok")
    bridge.done("F-1")
    verdicts = [op["args"] for op in bridge.ops if op["type"] == "reviewTask"]
    assert [(v["teamId"], v["verdict"], v["by"]) for v in verdicts] == [("alpha", "approved", "alpha-6"), ("bravo", "approved", "bravo-6")]
    assert bridge.ops[-1]["args"]["patch"] == {"status": "done"}


def test_busy_team_and_queued_board_task_sends_no_team_ops() -> None:
    bridge = BoardBridge(mode="dry", dry_busy=("alpha",))
    bridge.register_task("F-1", "t", team_id="alpha")
    assert [op["type"] for op in bridge.ops] == ["addTask"]
    assert bridge.board_teams["F-1"] == []


def test_restore_seeds_the_dry_counter() -> None:
    bridge = BoardBridge(mode="dry")
    bridge.restore({"F-1": "DRY-001", "F-2": "DRY-007"}, {"F-1": ["alpha"]})
    bridge.register_task("F-3", "t", team_id="alpha")
    assert bridge.board_ids["F-3"] == "DRY-008"
    assert bridge.board_teams["F-1"] == ["alpha"]


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
    bridge.register_task("F-1", "t", complexity="S", builders=1, team_id="alpha")
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
    assigned: list[str] = ["alpha", "bravo"]
    teams: list[dict[str, Any]] = []

    def do_GET(self) -> None:  # noqa: N802
        data = json.dumps({"ok": True, "result": {"teams": type(self).teams}}).encode()
        self.send_response(200 if self.path == "/api/state" else 404)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        type(self).received.append(body)
        if body["type"] == "addTask":
            result: Any = {"task": {"id": "T-077"}, "assigned": list(type(self).assigned)}
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
    _FakeDashboard.assigned = ["alpha", "bravo"]
    _FakeDashboard.teams = [{"id": "alpha", "taskId": "T-077"}, {"id": "bravo", "taskId": None}]  # after the unassign
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


def _serve() -> HTTPServer:
    server = HTTPServer(("127.0.0.1", 0), _FakeDashboard)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_auto_mode_busy_team_is_left_alone(tmp_path: Path) -> None:
    """The user's own board task T-001 keeps alpha; the Foundry task keeps the board's bravo."""
    _FakeDashboard.received = []
    _FakeDashboard.assigned = ["bravo"]
    _FakeDashboard.teams = [{"id": "alpha", "taskId": "T-001"}, {"id": "bravo", "taskId": "T-077"}, {"id": "charlie", "taskId": None}]
    server = _serve()
    try:
        bridge = BoardBridge(mode="auto", dashboard_url=f"http://127.0.0.1:{server.server_port}", cli_path=tmp_path / "x.mjs")
        bridge.register_task("F-1", "t", complexity="S", builders=1, team_id="alpha")
        bridge.review("F-1", "alpha", "APPROVED")
        bridge.done("F-1")
        assert bridge.flush(5.0)
        bridge.close()
    finally:
        server.shutdown()
    types = [r["type"] for r in _FakeDashboard.received]
    assert "assignTeam" not in types and "unassignTeam" not in types
    verdicts = [r["args"] for r in _FakeDashboard.received if r["type"] == "reviewTask"]
    assert [v["teamId"] for v in verdicts] == ["bravo"]
    assert bridge.board_teams["F-1"] == ["bravo"]


def test_auto_mode_free_team_is_assigned_and_others_released(tmp_path: Path) -> None:
    _FakeDashboard.received = []
    _FakeDashboard.assigned = ["bravo"]
    _FakeDashboard.teams = [{"id": "alpha", "taskId": None}, {"id": "bravo", "taskId": "T-077"}]
    server = _serve()
    try:
        bridge = BoardBridge(mode="auto", dashboard_url=f"http://127.0.0.1:{server.server_port}", cli_path=tmp_path / "x.mjs")
        bridge.register_task("F-1", "t", team_id="alpha")
        assert bridge.flush(5.0)
        bridge.close()
    finally:
        server.shutdown()
    team_ops = [(r["type"], r["args"]["teamId"]) for r in _FakeDashboard.received if r["type"] in ("assignTeam", "unassignTeam")]
    assert team_ops == [("assignTeam", "alpha"), ("unassignTeam", "bravo")]


def test_auto_mode_unreadable_board_state_counts_as_busy(tmp_path: Path) -> None:
    _FakeDashboard.received = []
    _FakeDashboard.assigned = []
    _FakeDashboard.teams = "not a list"  # type: ignore[assignment]
    server = _serve()
    try:
        bridge = BoardBridge(mode="auto", dashboard_url=f"http://127.0.0.1:{server.server_port}", cli_path=tmp_path / "x.mjs")
        bridge.register_task("F-1", "t", team_id="alpha")
        assert bridge.flush(5.0)
        bridge.close()
    finally:
        server.shutdown()
    assert [r["type"] for r in _FakeDashboard.received] == ["addTask"]


def test_progress_for_a_team_not_on_the_board_task_keeps_only_the_message() -> None:
    """G3: alpha is busy elsewhere, so progress must not touch alpha's agents or the board's teams."""
    bridge = BoardBridge(mode="dry", dry_autoassign=("bravo",), dry_busy=("alpha",))
    bridge.register_task("F-1", "t", team_id="alpha")
    bridge.progress("F-1", "alpha-1 approved", team_id="alpha", agent_id="alpha-1", status="done")
    bridge.progress("F-1", "agent only", agent_id="alpha-2")
    bridge.escalation(_alert(task_id="F-1", team_id="alpha"))
    progress = [op["args"] for op in bridge.ops if op["type"] == "reportProgress"]
    assert progress[0] == {"taskId": bridge.board_ids["F-1"], "message": "alpha-1 approved"}
    assert progress[1] == {"taskId": bridge.board_ids["F-1"], "message": "agent only"}
    assert set(progress[2]) == {"taskId", "message"}
    # when the Foundry team is on the board task, the full progress goes through
    ok = BoardBridge(mode="dry")
    ok.register_task("F-1", "t", team_id="alpha")
    ok.progress("F-1", "m", agent_id="alpha-1", status="done")
    assert ok.ops[-1]["args"]["agentId"] == "alpha-1" and ok.ops[-1]["args"]["status"] == "done"


def test_queued_board_task_later_dispatched_gets_verdicts_from_the_live_teams_dry() -> None:
    """R1: queued at add (Foundry team busy, nobody free), the dispatcher later gives it charlie."""
    bridge = BoardBridge(mode="dry", dry_busy=("alpha",))
    bridge.register_task("F-1", "t", team_id="alpha")
    assert bridge.board_teams["F-1"] == []
    bridge.dry_dispatch("F-1", ["charlie"])
    bridge.review("F-1", "alpha", "APPROVED")
    bridge.done("F-1")
    types = [op["type"] for op in bridge.ops]
    assert types == ["addTask", "updateTask", "reviewTask", "updateTask"]
    verdict = bridge.ops[2]["args"]
    assert (verdict["teamId"], verdict["by"]) == ("charlie", "charlie-6")
    assert bridge.board_teams["F-1"] == ["charlie"]


def test_still_queued_board_task_skips_review_and_goes_done() -> None:
    bridge = BoardBridge(mode="dry", dry_busy=("alpha",))
    bridge.register_task("F-1", "t", team_id="alpha")
    bridge.review("F-1", "alpha", "APPROVED")
    bridge.done("F-1")
    assert [op["type"] for op in bridge.ops] == ["addTask", "updateTask"]
    assert bridge.ops[-1]["args"]["patch"] == {"status": "done"}


def test_auto_mode_review_uses_the_live_board_teams(tmp_path: Path) -> None:
    """R1 on a fake dashboard: queued at add, assigned to charlie by the dispatcher before review."""
    _FakeDashboard.received = []
    _FakeDashboard.assigned = []  # nobody free when the task was added
    _FakeDashboard.teams = [{"id": "alpha", "taskId": "T-001"}, {"id": "bravo", "taskId": "T-002"}, {"id": "charlie", "taskId": "T-003"}]
    server = _serve()
    try:
        bridge = BoardBridge(mode="auto", dashboard_url=f"http://127.0.0.1:{server.server_port}", cli_path=tmp_path / "x.mjs")
        bridge.register_task("F-1", "t", team_id="alpha")
        assert bridge.flush(5.0)
        assert bridge.board_teams["F-1"] == []
        # charlie finishes T-003; the board's dispatcher moves it onto the queued T-077
        _FakeDashboard.teams = [{"id": "alpha", "taskId": "T-001"}, {"id": "bravo", "taskId": "T-002"}, {"id": "charlie", "taskId": "T-077"}]
        bridge.review("F-1", "alpha", "APPROVED")
        bridge.done("F-1")
        assert bridge.flush(5.0)
        bridge.close()
    finally:
        server.shutdown()
    sent = [(r["type"], r["args"].get("teamId"), (r["args"].get("patch") or {}).get("status")) for r in _FakeDashboard.received]
    assert sent == [("addTask", None, None), ("updateTask", None, "review"), ("reviewTask", "charlie", None), ("updateTask", None, "done")]
    assert bridge.board_teams["F-1"] == ["charlie"]


def test_auto_mode_review_falls_back_to_known_teams_when_state_unreadable(tmp_path: Path) -> None:
    _FakeDashboard.received = []
    _FakeDashboard.assigned = ["bravo"]
    _FakeDashboard.teams = [{"id": "alpha", "taskId": "T-001"}, {"id": "bravo", "taskId": "T-077"}]
    server = _serve()
    try:
        bridge = BoardBridge(mode="auto", dashboard_url=f"http://127.0.0.1:{server.server_port}", cli_path=tmp_path / "x.mjs")
        bridge.register_task("F-1", "t", team_id="alpha")
        assert bridge.flush(5.0)
        _FakeDashboard.teams = "broken"  # type: ignore[assignment]
        bridge.review("F-1", "alpha", "APPROVED")
        assert bridge.flush(5.0)
        bridge.close()
    finally:
        server.shutdown()
    verdicts = [r["args"]["teamId"] for r in _FakeDashboard.received if r["type"] == "reviewTask"]
    assert verdicts == ["bravo"]


# --------------------------------------------------------------- T-012: slow boards


class _SlowDashboard(BaseHTTPRequestHandler):
    """A dashboard whose op POSTs take ``delay`` seconds (it forwards to a slow shared board)."""

    received: list[dict[str, Any]] = []
    delay: dict[str, float] = {}
    tasks: list[dict[str, Any]] = []
    teams: list[dict[str, Any]] = [{"id": "alpha", "taskId": "T-077"}]

    def _send(self, body: Any) -> None:
        data = json.dumps(body).encode()
        try:
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except OSError:
            pass  # the client gave up waiting

    def do_GET(self) -> None:  # noqa: N802
        self._send({"ok": True, "result": {"teams": type(self).teams, "tasks": type(self).tasks}})

    def do_POST(self) -> None:  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        type(self).received.append(body)
        time.sleep(type(self).delay.get(body["type"], 0.0))
        result: Any = {"task": {"id": "T-077"}, "assigned": ["alpha"]} if body["type"] == "addTask" else {"ok": True}
        self._send({"ok": True, "result": result})

    def log_message(self, *args: Any) -> None:
        pass


def _serve_slow(delay: dict[str, float], tasks: list[dict[str, Any]] | None = None) -> ThreadingHTTPServer:
    _SlowDashboard.received = []
    _SlowDashboard.delay = delay
    _SlowDashboard.tasks = tasks or []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _SlowDashboard)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _recording_cli(tmp_path: Path) -> tuple[Path, Path]:
    """A stand-in dept.mjs that records its argv (to prove an op was or was not replayed)."""
    log_file = tmp_path / "cli.log"
    cli = tmp_path / "department" / "dept.mjs"
    cli.parent.mkdir(parents=True)
    cli.write_text(
        "require('fs').appendFileSync(%s, JSON.stringify(process.argv.slice(2)) + '\\n');\n" % json.dumps(str(log_file))
    )
    return cli, log_file


def test_close_keeps_waiting_while_a_slow_board_keeps_answering(tmp_path: Path) -> None:
    """Every op takes 0.15 s: 40 ops need ~6 s, beyond the old fixed 5 s flush; close() still drains."""
    server = _serve_slow({"reportProgress": 0.15})
    try:
        bridge = BoardBridge(mode="auto", dashboard_url=f"http://127.0.0.1:{server.server_port}", cli_path=tmp_path / "x.mjs")
        bridge.register_task("F-1", "t", team_id="alpha")
        for i in range(40):
            bridge.progress("F-1", f"step {i}")
        bridge.done("F-1")
        assert bridge.close() is True
    finally:
        server.shutdown()
    assert bridge.pending == 0
    last = _SlowDashboard.received[-1]
    assert last == {"type": "updateTask", "args": {"id": "T-077", "patch": {"status": "done"}, "by": "foundry"}}


def test_close_gives_up_when_the_board_stalls_and_says_so(tmp_path: Path) -> None:
    server = _serve_slow({"reportProgress": 3.0})
    try:
        bridge = BoardBridge(
            mode="auto", dashboard_url=f"http://127.0.0.1:{server.server_port}", cli_path=tmp_path / "x.mjs",
            op_timeout=5.0, stall_timeout=0.3,
        )
        bridge.register_task("F-1", "t", team_id="alpha")
        bridge.progress("F-1", "stuck")
        bridge.done("F-1")
        t0 = time.monotonic()
        assert bridge.close() is False
        assert time.monotonic() - t0 < 2.5
    finally:
        server.shutdown()
    assert any("not delivered before exit" in w for w in bridge.stats.warnings)


def test_timed_out_op_is_not_replayed_through_the_cli(tmp_path: Path) -> None:
    """A POST the dashboard accepted but answered too late may be applied: never send it twice."""
    cli, cli_log = _recording_cli(tmp_path)
    server = _serve_slow({"reportProgress": 1.5})
    try:
        bridge = BoardBridge(
            mode="auto", dashboard_url=f"http://127.0.0.1:{server.server_port}", cli_path=cli, op_timeout=0.4
        )
        bridge.register_task("F-1", "t", team_id="alpha")
        bridge.progress("F-1", "slow")
        bridge.done("F-1")
        assert bridge.close() is True
    finally:
        server.shutdown()
    assert not cli_log.exists()
    assert [r["type"] for r in _SlowDashboard.received] == ["addTask", "reportProgress", "updateTask"]
    assert bridge.stats.failed == 1 and bridge.stats.transport == "dashboard"


def test_timed_out_add_task_is_found_on_the_board_by_its_foundry_tag(tmp_path: Path) -> None:
    cli, cli_log = _recording_cli(tmp_path)
    tasks = [
        {"id": "T-070", "title": "Add login", "description": "[Foundry F-1 · S] x", "status": "done"},  # an old run
        {"id": "T-081", "title": "Add login", "description": "[Foundry F-1 · S] x", "status": "active"},
        {"id": "T-082", "title": "Other", "description": "[Foundry F-1 · S] x", "status": "active"},
    ]
    _SlowDashboard.teams = [{"id": "alpha", "taskId": "T-081"}]
    server = _serve_slow({"addTask": 1.5}, tasks)
    try:
        bridge = BoardBridge(
            mode="auto", dashboard_url=f"http://127.0.0.1:{server.server_port}", cli_path=cli, op_timeout=0.4
        )
        bridge.register_task("F-1", "Add login", "x", team_id="alpha")
        bridge.done("F-1")
        assert bridge.close() is True
    finally:
        server.shutdown()
        _SlowDashboard.teams = [{"id": "alpha", "taskId": "T-077"}]
    assert not cli_log.exists()
    assert bridge.board_ids == {"F-1": "T-081"}
    assert bridge.board_teams["F-1"] == ["alpha"]
    assert _SlowDashboard.received[-1]["args"]["id"] == "T-081"


def test_dashboard_url_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOUNDRY_DASHBOARD_URL", "http://127.0.0.1:4950/")
    assert BoardBridge("dry").dashboard_url == "http://127.0.0.1:4950"
    assert BoardBridge("dry", dashboard_url="http://x:1").dashboard_url == "http://x:1"
    monkeypatch.delenv("FOUNDRY_DASHBOARD_URL")
    assert BoardBridge("dry").dashboard_url == "http://127.0.0.1:4300"
