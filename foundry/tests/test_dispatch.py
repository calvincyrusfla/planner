"""Tests for foundry/dispatch.py: models, board clients (fake HTTP board) and CLI."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from foundry import dispatch as d


# --------------------------------------------------------------------------- fake board


class FakeBoardState:
    """Minimal emulation of the board's batch ops."""

    def __init__(self) -> None:
        self.state: dict[str, Any] = {"tasks": [], "foundryBatches": [], "nextBatchNumber": 1}
        self.ops: list[dict[str, Any]] = []
        self.headers: list[dict[str, str]] = []

    def apply(self, type_: str, args: dict[str, Any]) -> Any:
        self.ops.append({"type": type_, "args": args})
        if type_ == "dispatchBatch":
            n = self.state["nextBatchNumber"]
            self.state["nextBatchNumber"] = n + 1
            batch = {"id": f"S-{n:03d}", "title": args["title"], "repo": args["repo"], "base": args.get("base"),
                     "goal": args.get("goal", ""), "createdAt": "2026-11-08T10:00:00Z", "createdBy": args.get("by"),
                     "status": "queued", "subtasks": args["subtasks"], "results": {}}
            self.state["foundryBatches"].append(batch)
            return batch
        if type_ == "cancelBatch":
            for b in self.state["foundryBatches"]:
                if b["id"] == args["id"]:
                    b["status"] = "cancelled"
                    return b
            raise KeyError(args["id"])
        raise ValueError(f"unknown op {type_}")


def _server(fake: FakeBoardState, *, hub: bool) -> ThreadingHTTPServer:
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a: Any) -> None:
            pass

        def _send(self, code: int, body: Any) -> None:
            raw = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:
            fake.headers.append({k.lower(): v for k, v in self.headers.items()})
            if hub and self.path == "/api/department":
                return self._send(200, {"ok": True, "state": fake.state, "rev": 1})
            if not hub and self.path == "/api/state":
                return self._send(200, {"ok": True, "result": fake.state})
            self._send(404, {"ok": False, "error": "Not found"})

        def do_POST(self) -> None:
            fake.headers.append({k.lower(): v for k, v in self.headers.items()})
            body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)) or b"{}")
            want = "/api/department" if hub else "/api/op"
            if self.path != want:
                return self._send(404, {"ok": False, "error": "Not found"})
            try:
                result = fake.apply(body["type"], body.get("args") or {})
            except (KeyError, ValueError) as exc:
                return self._send(400, {"ok": False, "error": str(exc)})
            if hub:
                return self._send(200, {"ok": True, "result": result, "state": fake.state, "rev": 2})
            self._send(200, {"ok": True, "result": result})

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.fixture
def fake() -> FakeBoardState:
    return FakeBoardState()


@pytest.fixture
def dashboard(fake: FakeBoardState) -> Iterator[str]:
    srv = _server(fake, hub=False)
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture
def hub(fake: FakeBoardState) -> Iterator[str]:
    srv = _server(fake, hub=True)
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def _batch(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "title": "Add export", "repo": "octo/planner",
        "subtasks": [
            {"title": "Model", "files": ["src/model.py"], "size": "s"},
            {"title": "UI", "description": "button", "depends_on": ["1"], "acceptance_criteria": ["works"]},
        ],
    }
    base.update(kw)
    return base


class DownBoard:
    name = "down"

    def state(self) -> dict[str, Any]:
        raise d.BoardUnavailable("down")

    def op(self, type: str, args: dict[str, Any]) -> Any:
        raise d.BoardUnavailable("down")


# --------------------------------------------------------------------------- models


def test_batch_assigns_keys_and_board_args() -> None:
    b = d.DispatchFoundryBatch.model_validate(_batch(base="main"))
    assert [s.key for s in b.subtasks] == ["1", "2"]
    args = b.to_board_args(by="fm")
    assert args["repo"] == "octo/planner" and args["base"] == "main" and args["by"] == "fm"
    assert args["subtasks"][0] == {"key": "1", "title": "Model", "files": ["src/model.py"], "size": "S"}
    assert args["subtasks"][1]["dependsOn"] == ["1"]
    assert args["subtasks"][1]["acceptance"] == ["works"]


def test_repo_url_normalised() -> None:
    b = d.DispatchFoundryBatch.model_validate(_batch(repo="https://github.com/octo/planner.git"))
    assert b.repo == "octo/planner"


@pytest.mark.parametrize("bad", [
    {"repo": "not a repo"},
    {"subtasks": []},
    {"subtasks": [{"title": f"t{i}"} for i in range(21)]},
    {"subtasks": [{"title": "a", "files": ["../etc/passwd"]}]},
    {"subtasks": [{"title": "a", "files": ["/abs"]}]},
    {"subtasks": [{"title": "a", "files": ["C:/x"]}]},
    {"subtasks": [{"title": "a", "files": [f"f{i}" for i in range(51)]}]},
    {"subtasks": [{"title": "x" * 201}]},
    {"subtasks": [{"title": "a", "description": "x" * 20_001}]},
    {"subtasks": [{"title": "a", "size": "XXL"}]},
    {"subtasks": [{"title": "a", "depends_on": ["9"]}]},
    {"subtasks": [{"key": "a", "title": "a", "depends_on": ["b"]}, {"key": "b", "title": "b", "depends_on": ["a"]}]},
    {"subtasks": [{"key": "a", "title": "a"}, {"key": "a", "title": "b"}]},
    {"subtasks": [{"title": "a", "extra": 1}]},
    {"base": "-x"},
])
def test_batch_rejects(bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        d.DispatchFoundryBatch.model_validate(_batch(**bad))


def test_sprint_status_from_board() -> None:
    st = d.SprintStatus.from_board({
        "id": "S-004", "title": "T", "repo": "o/n", "status": "running",
        "claimedBy": {"host": "PC", "service": "x", "at": "t"}, "heartbeatAt": "now",
        "subtasks": [{"key": "1", "title": "A"}, {"key": "2", "title": "B"}],
        "results": {"1": {"foundryTask": "F-012", "state": "complete",
                          "pr": {"state": "opened", "url": "https://github.com/o/n/pull/3", "branch": "b"}},
                    "2": {"state": "failed", "error": "boom"}},
        "unknownField": True,
    })
    assert st.sprint_id == "S-004" and st.completed == 1 and not st.terminal
    assert st.subtasks[0].foundry_task == "F-012" and (st.subtasks[0].pr_url or "").endswith("/pull/3")
    assert st.subtasks[1].error == "boom" and st.subtasks[1].terminal
    text = st.summary()
    assert "F-012" in text and "pull/3" in text and "boom" in text and "PC" in text


# --------------------------------------------------------------------------- transports


def test_dispatch_and_poll_via_dashboard(fake: FakeBoardState, dashboard: str) -> None:
    board = d.DashboardBoard(dashboard)
    st = d.dispatch_to_foundry(_batch(), board=board, by="fm-test")
    assert st.sprint_id == "S-001" and st.status == "queued" and len(st.subtasks) == 2
    assert fake.ops[0]["type"] == "dispatchBatch" and fake.ops[0]["args"]["by"] == "fm-test"
    fake.state["foundryBatches"][0]["results"] = {"1": {"foundryTask": "F-001", "state": "building"}}
    st = d.poll_foundry_sprint("S-001", board=board)
    assert st.subtasks[0].foundry_task == "F-001" and st.subtasks[0].state == "building"
    assert st.subtasks[1].state == "queued"


def test_hub_sends_key_and_proxy_placeholder(fake: FakeBoardState, hub: str) -> None:
    d.dispatch_to_foundry(_batch(), board=d.HubBoard(hub, "secret"))
    assert fake.headers[-1].get("x-edit-key") == "secret"
    st = d.poll_foundry_sprint("s-001", board=d.HubBoard(hub, ""))
    assert st.sprint_id == "S-001"
    assert fake.headers[-1].get("x-edit-key") == "proxy"


def test_board_refusal_is_board_error(dashboard: str) -> None:
    with pytest.raises(d.BoardError) as ei:
        d.DashboardBoard(dashboard).op("cancelBatch", {"id": "S-999"})
    assert not isinstance(ei.value, d.BoardUnavailable)


def test_poll_unknown_and_bad_id(dashboard: str) -> None:
    with pytest.raises(d.BoardError):
        d.poll_foundry_sprint("S-077", board=d.DashboardBoard(dashboard))
    with pytest.raises(ValueError):
        d.poll_foundry_sprint("T-001", board=d.DashboardBoard(dashboard))


def test_auto_falls_back_dashboard_to_hub(fake: FakeBoardState, hub: str) -> None:
    dead = d.DashboardBoard("http://127.0.0.1:9", timeout=1)
    auto = d.AutoBoard([dead, d.HubBoard(hub)])
    st = d.dispatch_to_foundry(_batch(), board=auto)
    assert st.sprint_id == "S-001" and auto.active is auto.clients[1]


def test_auto_all_down() -> None:
    with pytest.raises(d.BoardUnavailable):
        d.AutoBoard([DownBoard(), DownBoard()]).state()


def test_hub_config(tmp_path: Path) -> None:
    f = tmp_path / "hub.json"
    assert d.hub_config({"DEPT_HUB": "off", "DEPT_HUB_URL": "https://x"}, f) is None
    assert d.hub_config({"DEPT_HUB_URL": "https://x/", "DEPT_HUB_KEY": "k"}, f) == {"url": "https://x", "key": "k"}
    assert d.hub_config({}, f) is None
    f.write_text(json.dumps({"url": "https://y", "key": ""}))
    assert d.hub_config({}, f) == {"url": "https://y", "key": ""}


def test_cli_board_dispatch_uses_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeBoardState()
    cli = d.CliBoard(cli=tmp_path / "dept.mjs")
    calls: list[tuple[str, ...]] = []

    def run(*argv: str) -> str:
        calls.append(argv)
        if argv == ("json",):
            return json.dumps(fake.state)
        if argv[:2] == ("foundry", "dispatch"):
            fake.apply("dispatchBatch", json.loads(Path(argv[3]).read_text()))
            return "Dispatched S-001 Add export\n"
        raise AssertionError(argv)

    monkeypatch.setattr(cli, "_run", run)
    st = d.dispatch_to_foundry(_batch(), board=cli)
    assert st.sprint_id == "S-001" and ("foundry", "dispatch", "--json", calls[1][3]) == calls[1]
    with pytest.raises(d.BoardUnavailable):
        cli.op("claimBatch", {})


# --------------------------------------------------------------------------- wait + CLI


class ScriptedBoard:
    """Returns a sequence of batch states, one per state() call."""

    def __init__(self, statuses: list[str | None]) -> None:
        self.statuses = statuses
        self.calls = 0

    def state(self) -> dict[str, Any]:
        s = self.statuses[min(self.calls, len(self.statuses) - 1)]
        self.calls += 1
        if s is None:
            raise d.BoardUnavailable("blip")
        return {"foundryBatches": [{"id": "S-001", "title": "T", "status": s,
                                    "subtasks": [{"key": "1", "title": "A"}], "results": {}}]}

    def op(self, type: str, args: dict[str, Any]) -> Any:
        raise AssertionError


def test_wait_until_done_survives_blips() -> None:
    board = ScriptedBoard(["queued", None, "running", "running", "done"])
    seen: list[str] = []
    sleeps: list[float] = []
    st = d.wait_for_sprint("S-001", board=board, interval=5, on_update=lambda s: seen.append(s.status),
                           sleep=sleeps.append, monotonic=lambda: 0.0)
    assert st.status == "done" and seen == ["queued", "running", "done"] and sleeps == [5, 5, 5, 5]


def test_wait_timeout_returns_last() -> None:
    t = [0.0]

    def sleep(s: float) -> None:
        t[0] += s

    st = d.wait_for_sprint("S-001", board=ScriptedBoard(["running"]), timeout=30, interval=10,
                           sleep=sleep, monotonic=lambda: t[0])
    assert st.status == "running" and not st.terminal


def test_cli_send_and_poll(tmp_path: Path, fake: FakeBoardState, dashboard: str,
                           capsys: pytest.CaptureFixture[str]) -> None:
    f = tmp_path / "batch.json"
    f.write_text(json.dumps(_batch()))
    board = d.DashboardBoard(dashboard)
    assert d.main(["send", "--json", str(f), "--out-json"], board=board) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["sprint_id"] == "S-001" and out["status"] == "queued"
    assert d.main(["poll", "S-001"], board=board) == 0
    assert "S-001 Add export [queued]" in capsys.readouterr().out
    f.write_text(json.dumps(_batch(repo="bad repo")))
    assert d.main(["send", "--json", str(f)], board=board) == 2
    assert "invalid batch" in capsys.readouterr().err
    assert d.main(["poll", "S-404"], board=board) == 2


# --------------------------------------------------------------------------- real department code


_NODE = __import__("shutil").which("node")


def _free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.mark.skipif(_NODE is None, reason="node not installed")
def test_real_cli_board_file_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEPT_HUB", "off")
    monkeypatch.setenv("DEPT_DATA", str(tmp_path / "state.json"))
    monkeypatch.setenv("DEPT_URL", f"http://127.0.0.1:{_free_port()}")  # no dashboard -> state file
    cli = d.CliBoard()
    st = d.dispatch_to_foundry(_batch(base="main"), board=cli, by="pytest")
    assert st.sprint_id.startswith("S-") and st.status == "queued" and st.base == "main"
    assert [s.key for s in st.subtasks] == ["1", "2"]
    again = d.poll_foundry_sprint(st.sprint_id, board=cli)
    assert again.title == "Add export" and again.created_by == "pytest"
    cli.op("cancelBatch", {"id": st.sprint_id})
    assert d.poll_foundry_sprint(st.sprint_id, board=cli).status == "cancelled"


@pytest.mark.skipif(_NODE is None, reason="node not installed")
def test_real_dashboard_board(tmp_path: Path) -> None:
    import os
    import subprocess
    import time

    port = _free_port()
    env = {**os.environ, "DEPT_HUB": "off", "DEPT_DATA": str(tmp_path / "state.json"), "PORT": str(port)}
    proc = subprocess.Popen([_NODE, str(d.REPO_ROOT / "department" / "server.mjs")], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        board = d.DashboardBoard(f"http://127.0.0.1:{port}")
        for _ in range(100):
            try:
                board.state()
                break
            except d.BoardUnavailable:
                time.sleep(0.1)
        st = d.dispatch_to_foundry(_batch(), board=board)
        claim = board.op("claimBatch", {"id": st.sprint_id, "host": "PC", "service": "svc"})
        assert claim["ok"] is True
        board.op("updateBatch", {"id": st.sprint_id, "service": "svc", "results": {
            "1": {"foundryTask": "F-001", "state": "complete",
                  "pr": {"state": "opened", "url": "https://github.com/octo/planner/pull/1"}},
            "2": {"foundryTask": "F-002", "state": "complete"}}})
        done = d.wait_for_sprint(st.sprint_id, board=board, timeout=5, interval=0.1)
        assert done.status == "done" and done.succeeded
        assert done.subtasks[0].foundry_task == "F-001" and (done.subtasks[0].pr_url or "").endswith("/pull/1")
        assert done.claimed_by and done.claimed_by["host"] == "PC"
        with pytest.raises(d.BoardError):
            board.op("updateBatch", {"id": "S-999", "service": "svc"})
    finally:
        proc.terminate()
        proc.wait(timeout=10)


# --------------------------------------------------------------------------- review round 1


class RecordingBoard:
    def __init__(self, name: str, exc: BaseException | None = None) -> None:
        self.name = name
        self.exc = exc
        self.ops: list[str] = []
        self.reads = 0

    def state(self) -> dict[str, Any]:
        self.reads += 1
        if self.exc:
            raise self.exc
        return {"foundryBatches": []}

    def op(self, type: str, args: dict[str, Any]) -> Any:
        self.ops.append(type)
        if self.exc:
            raise self.exc
        return {"id": "S-001", "title": args.get("title"), "subtasks": [], "status": "queued"}


def test_auto_does_not_replay_a_write_that_may_have_been_sent() -> None:
    first = RecordingBoard("hub", d.BoardUnavailable("timed out", sent=True))
    second = RecordingBoard("cli")
    with pytest.raises(d.BoardOutcomeUnknown) as ei:
        d.AutoBoard([first, second]).op("dispatchBatch", {"title": "x"})
    assert second.ops == [] and "poll" in str(ei.value) and "foundry list" in str(ei.value)


def test_auto_falls_through_when_not_sent_and_for_reads() -> None:
    second = RecordingBoard("cli")
    auto = d.AutoBoard([RecordingBoard("dash", d.BoardUnavailable("refused", sent=False)), second])
    assert auto.op("dispatchBatch", {"title": "x"})["id"] == "S-001" and second.ops == ["dispatchBatch"]
    reader = RecordingBoard("cli")
    auto = d.AutoBoard([RecordingBoard("hub", d.BoardUnavailable("timed out", sent=True)), reader])
    assert auto.state() == {"foundryBatches": []} and reader.reads == 1


def test_refused_connection_is_not_sent() -> None:
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()  # nothing listens there now
    with pytest.raises(d.BoardUnavailable) as ei:
        d.DashboardBoard(f"http://127.0.0.1:{port}", timeout=2).op("dispatchBatch", {})
    assert ei.value.sent is False


def test_hub_timeout_after_send_is_not_replayed_through_cli(fake: FakeBoardState) -> None:
    import time as _time

    class Slow(BaseHTTPRequestHandler):
        def log_message(self, *a: Any) -> None:
            pass

        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)))
            fake.apply(body["type"], body["args"])  # the hub applies it ...
            _time.sleep(1.5)  # ... but answers too late

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Slow)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        cli = RecordingBoard("cli")
        auto = d.AutoBoard([d.HubBoard(f"http://127.0.0.1:{srv.server_address[1]}", timeout=0.5), cli])
        with pytest.raises(d.BoardOutcomeUnknown):
            d.dispatch_to_foundry(_batch(), board=auto)
        assert cli.ops == [] and len(fake.state["foundryBatches"]) == 1
    finally:
        srv.shutdown()


class ErroringBoard(ScriptedBoard):
    def __init__(self, statuses: list[str | None], errors: list[BaseException]) -> None:
        super().__init__(statuses)
        self.errors = errors

    def state(self) -> dict[str, Any]:
        if self.errors:
            raise self.errors.pop(0)
        return super().state()


def test_wait_retries_board_errors() -> None:
    board = ErroringBoard(["running", "done"], [d.BoardError("busy (409)"), d.BoardError("upstream 502")])
    st = d.wait_for_sprint("S-001", board=board, interval=1, sleep=lambda s: None, monotonic=lambda: 0.0)
    assert st.status == "done"


def test_wait_not_found_raises_at_once() -> None:
    class Empty:
        def state(self) -> dict[str, Any]:
            return {"foundryBatches": []}

        def op(self, type: str, args: dict[str, Any]) -> Any:
            raise AssertionError

    sleeps: list[float] = []
    with pytest.raises(d.SprintNotFound):
        d.wait_for_sprint("S-001", board=Empty(), sleep=sleeps.append, monotonic=lambda: 0.0)
    assert sleeps == []


def test_wait_gives_up_with_last_error() -> None:
    t = [0.0]

    def sleep(s: float) -> None:
        t[0] += s

    board = ErroringBoard(["running"], [d.BoardError("busy")] * 10)
    with pytest.raises(d.BoardUnavailable) as ei:
        d.wait_for_sprint("S-001", board=board, timeout=3, interval=1, sleep=sleep, monotonic=lambda: t[0])
    assert "busy" in str(ei.value)


def test_read_files_validated_deduplicated_and_sent() -> None:
    batch = d.DispatchFoundryBatch(title="T", repo="o/r", subtasks=[
        d.SubtaskPayload(title="A", read_files=["a/b.py", "a/b.py", "c.py"]),
        d.SubtaskPayload(title="B"),
    ])
    first, second = batch.to_board_args()["subtasks"]
    assert first["readFiles"] == ["a/b.py", "c.py"]
    assert "readFiles" not in second


def test_read_files_rejects_unsafe_path() -> None:
    with pytest.raises(ValidationError):
        d.SubtaskPayload(title="A", read_files=["../secret.py"])


class _CapturingBoard:
    """Records the write, then refuses it: these tests only look at what was sent."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def state(self) -> dict[str, Any]:
        return {"foundryBatches": []}

    def op(self, type: str, args: dict[str, Any]) -> Any:
        self.calls.append((type, args))
        raise d.BoardError("captured")


def test_read_files_reach_the_board_from_a_model() -> None:
    board = _CapturingBoard()
    batch = d.DispatchFoundryBatch(title="T", repo="o/r", subtasks=[
        d.SubtaskPayload(title="A", files=["x.py"], read_files=["a/b.py", "a/b.py", "c.py"]),
    ])
    with pytest.raises(d.BoardError):
        d.dispatch_to_foundry(batch, board=board)
    assert len(board.calls) == 1
    op_type, args = board.calls[0]
    assert op_type == "dispatchBatch"
    assert args["subtasks"][0]["readFiles"] == ["a/b.py", "c.py"]
    assert args["subtasks"][0]["files"] == ["x.py"]


def test_read_files_reach_the_board_from_a_dict() -> None:
    board = _CapturingBoard()
    batch: dict[str, Any] = {
        "title": "T",
        "repo": "o/r",
        "subtasks": [{"title": "A", "read_files": ["a/b.py"]}, {"title": "B"}],
    }
    with pytest.raises(d.BoardError):
        d.dispatch_to_foundry(batch, board=board)
    first, second = board.calls[0][1]["subtasks"]
    assert first["readFiles"] == ["a/b.py"]
    assert "readFiles" not in second


@pytest.mark.parametrize("path", ["/etc/passwd", "a/../../x"])
def test_read_files_rejects_paths_outside_the_repo(path: str) -> None:
    with pytest.raises(ValidationError):
        d.SubtaskPayload(title="A", read_files=[path])


def test_read_files_rejects_too_many() -> None:
    with pytest.raises(ValidationError):
        d.SubtaskPayload(title="A", read_files=[f"f{i}.py" for i in range(d.MAX_FILES + 1)])


# --------------------------------------------------------------------------- builder order limit


# What the composed order adds to a description for batch title "T", no size, no goal.
_ORDER_OVERHEAD = len('\n\nPart of "T"\n') + 100


def _order_batch(description: str, *, goal: str = "", size: Any = None) -> d.DispatchFoundryBatch:
    return d.DispatchFoundryBatch(
        title="T", repo="o/r", goal=goal,
        subtasks=[d.SubtaskPayload(key="big", title="Sub", description=description, size=size)],
    )


def test_builder_order_limit_matches_the_builder() -> None:
    from foundry.builder_worker import MAX_ORDER_DETAILS_CHARS

    assert d.MAX_BUILDER_ORDER == MAX_ORDER_DETAILS_CHARS
    assert "MAX_BUILDER_ORDER" in d.__all__


def test_largest_accepted_order_reaches_the_builder_whole() -> None:
    from foundry.board_dispatch import marker, order_text
    from foundry.builder_worker import _capped_order_text

    batch = _order_batch("x" * (d.MAX_BUILDER_ORDER - _ORDER_OVERHEAD))
    sub = batch.subtasks[0]
    line = marker("S-999", "big", "2026-11-09T03:49:18.361Z")
    text = order_text(sub.description, sub.size, batch.title, batch.goal, line)
    assert _capped_order_text(text, "order description") == text
    over = "x" * (d.MAX_BUILDER_ORDER + 1)
    assert _capped_order_text(over, "order description") != over


def test_order_at_the_builder_limit_passes() -> None:
    batch = _order_batch("x" * (d.MAX_BUILDER_ORDER - _ORDER_OVERHEAD))
    assert batch.subtasks[0].key == "big"


def test_order_one_over_the_builder_limit_fails_naming_the_key() -> None:
    limit = d.MAX_BUILDER_ORDER
    with pytest.raises(ValidationError) as err:
        _order_batch("x" * (limit - _ORDER_OVERHEAD + 1))
    message = str(err.value)
    assert "'big'" in message
    assert f"{limit + 1} characters" in message
    assert f"limit of {limit}" in message
    assert "read_files" in message
    assert "split" in message


def test_long_goal_pushes_subtasks_over_the_builder_limit() -> None:
    _order_batch("short", goal="g" * 100)
    with pytest.raises(ValidationError, match="'big'"):
        _order_batch("short", goal="g" * d.MAX_BUILDER_ORDER)


def test_size_line_counts_towards_the_builder_limit() -> None:
    description = "x" * (d.MAX_BUILDER_ORDER - _ORDER_OVERHEAD)
    _order_batch(description)
    with pytest.raises(ValidationError, match="'big'"):
        _order_batch(description, size="M")


def test_dispatch_to_foundry_refuses_a_too_long_order_before_reaching_the_board() -> None:
    board = _CapturingBoard()
    order = {"title": "T", "repo": "o/r",
             "subtasks": [{"key": "big", "title": "Sub", "description": "x" * d.MAX_BUILDER_ORDER}]}
    with pytest.raises(ValidationError, match="'big'"):
        d.dispatch_to_foundry(order, board=board)
    assert board.calls == []
