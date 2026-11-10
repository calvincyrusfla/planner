"""Tests for foundry.serve_api: real HTTP on a free port with a simulated service."""

from __future__ import annotations

import asyncio
import http.client
import json
import threading
from types import SimpleNamespace

import pytest

from foundry import __version__
from foundry.orchestrator_queue import FoundryQueue
from foundry.serve_api import FoundryAPI, OrderError, is_local_request, validate_order


class FakeService:
    """Simulated factory service: sync health/status, async submit/ask."""

    def __init__(self) -> None:
        self.repo = "/tmp/repo"
        self.model = "gemma3:4b"
        self.orders: list[dict] = []
        self.asked: list[str] = []
        self.loop: asyncio.AbstractEventLoop | None = None
        self.ask_error = False

    def health(self):
        return {"repo": self.repo, "model": self.model, "gemma_ok": True, "claude_ok": False}

    def status(self):
        return {
            "tasks": [{"id": "F-001", "title": "x", "state": "complete", "team": "alpha", "builders": 1,
                       "patch": "p.patch", "md": "p.md", "rejected": False, "reason": None}],
            "foreman": {"tokens_saved": 3},
            "paused_until": None,
            "last_account_error": None,
        }

    async def submit(self, title, description="", files=None, size=None):
        if title == "boom":
            raise ValueError("title rejected by the factory\nmore detail")
        self.orders.append({"title": title, "description": description, "files": files, "size": size})
        return f"F-{len(self.orders):03d}"

    async def ask(self, question):
        self.asked.append(question)
        for part in ("The line ", "is ", "idle."):
            await asyncio.sleep(0)
            yield part
        if self.ask_error:
            raise RuntimeError("secret internal detail")


@pytest.fixture()
def svc():
    return FakeService()


@pytest.fixture()
def api(svc):
    server = FoundryAPI(svc, port=0).start()
    yield server
    server.stop()


def request(api, method, path, body=None, headers=None, raw=None):
    conn = http.client.HTTPConnection("127.0.0.1", api.port, timeout=10)
    hdrs = {"Host": f"127.0.0.1:{api.port}"}
    hdrs.update(headers or {})
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    if data is not None:
        hdrs.setdefault("Content-Type", "application/json")
    conn.request(method, path, body=data, headers=hdrs)
    resp = conn.getresponse()
    text = resp.read().decode()
    conn.close()
    return resp, text


def parse_sse(text):
    events = []
    for block in text.strip().split("\n\n"):
        name, data = "message", None
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                data = json.loads(line[6:])
        events.append((name, data))
    return events


def test_health(api):
    resp, text = request(api, "GET", "/api/health")
    assert resp.status == 200
    body = json.loads(text)
    assert body == {"ok": True, "repo": "/tmp/repo", "model": "gemma3:4b", "gemma_ok": True,
                    "claude_ok": False, "version": __version__}
    assert "access-control-allow-origin" not in {k.lower() for k in resp.headers}


def test_health_without_health_method():
    class Bare:
        repo = "/r"

        def status(self):
            return {}

    with FoundryAPI(Bare(), port=0) as server:
        body = json.loads(request(server, "GET", "/api/health")[1])
    assert body["repo"] == "/r" and body["gemma_ok"] is False and body["version"] == __version__


def test_status(api):
    resp, text = request(api, "GET", "/api/status")
    body = json.loads(text)
    assert resp.status == 200 and body["ok"] is True
    assert body["tasks"][0]["id"] == "F-001"
    assert body["foreman"] == {"tokens_saved": 3}
    assert "paused_until" in body and "last_account_error" in body


def test_orders_submit(api, svc):
    resp, text = request(api, "POST", "/api/orders",
                         {"title": " Add X ", "description": "do it", "files": "a.py, b/c.py,", "size": "large"})
    assert resp.status == 200, text
    assert json.loads(text) == {"ok": True, "task_id": "F-001"}
    assert svc.orders == [{"title": "Add X", "description": "do it", "files": ["a.py", "b/c.py"], "size": "L"}]


@pytest.mark.parametrize("body", [
    {},
    {"title": ""},
    {"title": 5},
    {"title": "x", "files": ["/etc/passwd"]},
    {"title": "x", "files": ["../up.py"]},
    {"title": "x", "files": ["C:\\win.py"]},
    {"title": "x", "files": [1]},
    {"title": "x", "size": "huge"},
    {"title": "x", "extra": 1},
    {"title": "x" * 201},
    [1, 2],
])
def test_orders_validation_400(api, svc, body):
    resp, text = request(api, "POST", "/api/orders", body)
    assert resp.status == 400
    out = json.loads(text)
    assert out["ok"] is False and out["code"] == "invalid" and out["error"]
    assert svc.orders == []


def test_orders_bad_json_and_service_value_error(api):
    resp, _ = request(api, "POST", "/api/orders", raw=b"{nope")
    assert resp.status == 400
    resp, text = request(api, "POST", "/api/orders", {"title": "boom"})
    assert resp.status == 400 and json.loads(text)["error"] == "title rejected by the factory"


def test_orders_too_large(api):
    resp, _ = request(api, "POST", "/api/orders", raw=b"{" + b" " * 70_000 + b"}")
    assert resp.status == 413


def test_ask_streams_sse(api, svc):
    resp, text = request(api, "POST", "/api/ask", {"question": "How is the line?"})
    assert resp.status == 200
    assert resp.headers["Content-Type"].startswith("text/event-stream")
    events = parse_sse(text)
    assert events == [("message", {"text": "The line "}), ("message", {"text": "is "}),
                      ("message", {"text": "idle."}), ("done", {})]
    assert svc.asked == ["How is the line?"]


def test_ask_error_mid_stream_hides_details(api, svc):
    svc.ask_error = True
    resp, text = request(api, "POST", "/api/ask", {"question": "q"})
    events = parse_sse(text)
    assert events[-1] == ("done", {})
    assert events[-2][0] == "error"
    assert "secret" not in text


def test_ask_requires_question(api):
    resp, _ = request(api, "POST", "/api/ask", {"question": "  "})
    assert resp.status == 400


def test_ask_sync_generator_and_service_loop(svc):
    """A running service loop is used for async calls; sync iterators also work."""
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    svc.loop = loop
    seen = []

    async def ask(question):
        seen.append(asyncio.get_running_loop())
        yield "on loop"

    svc.ask = ask
    try:
        with FoundryAPI(svc, port=0) as server:
            events = parse_sse(request(server, "POST", "/api/ask", {"question": "q"})[1])
            assert events[0] == ("message", {"text": "on loop"})
            assert seen == [loop]
            svc.ask = lambda q: iter(["a", "b"])
            events = parse_sse(request(server, "POST", "/api/ask", {"question": "q"})[1])
            assert [e[1] for e in events] == [{"text": "a"}, {"text": "b"}, {}]
            # async submit also runs on the service loop
            resp, text = request(server, "POST", "/api/orders", {"title": "t"})
            assert json.loads(text)["task_id"] == "F-001"
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(5)
        loop.close()


@pytest.mark.parametrize("host", ["evil.example", "evil.example:4310", "127.0.0.1.nip.io", ""])
def test_non_local_host_refused(api, host):
    resp, text = request(api, "GET", "/api/health", headers={"Host": host})
    assert resp.status == 403 and json.loads(text)["code"] == "not_local"


@pytest.mark.parametrize("host", ["localhost:4310", "LOCALHOST", "127.0.0.1"])
def test_local_hosts_allowed(api, host):
    resp, _ = request(api, "GET", "/api/health", headers={"Host": host})
    assert resp.status == 200


def test_foreign_origin_post_refused(api, svc):
    resp, _ = request(api, "POST", "/api/orders", {"title": "x"}, headers={"Origin": "https://evil.example"})
    assert resp.status == 403 and svc.orders == []
    resp, _ = request(api, "POST", "/api/orders", {"title": "x"}, headers={"Origin": "http://127.0.0.1:4300"})
    assert resp.status == 200


def test_unknown_route_and_wrong_method(api):
    assert request(api, "GET", "/api/nope")[0].status == 404
    assert request(api, "GET", "/api/orders")[0].status == 405
    assert request(api, "POST", "/api/health")[0].status == 405
    assert request(api, "OPTIONS", "/api/orders")[0].status == 405


def test_internal_error_is_generic(svc):
    def broken():
        raise RuntimeError("db password leaked")

    svc.status = broken
    with FoundryAPI(svc, port=0) as server:
        resp, text = request(server, "GET", "/api/status")
    assert resp.status == 500 and "password" not in text


def test_is_local_request_unit():
    assert is_local_request("127.0.0.1", "127.0.0.1:4310")
    assert is_local_request("::1", "[::1]:4310")
    assert not is_local_request("10.0.0.5", "127.0.0.1:4310")
    assert not is_local_request("127.0.0.1", "foundry.example")
    assert not is_local_request("127.0.0.1", "localhost", origin="http://evil.example")
    assert is_local_request("127.0.0.1", "localhost", origin="http://localhost:4300")


def test_validate_order_defaults():
    assert validate_order({"title": "t"}) == {"title": "t", "description": "", "files": [], "size": None}
    assert validate_order({"title": "t", "size": "xl", "files": ["a\\b.py", "a/b.py"]})["files"] == ["a/b.py"]
    with pytest.raises(OrderError):
        validate_order({"title": "t", "description": 3})


def test_refuses_non_loopback_bind(svc):
    with pytest.raises(ValueError):
        FoundryAPI(svc, host="0.0.0.0")


def test_stop_is_idempotent_and_frees_port(svc):
    server = FoundryAPI(svc, port=0).start()
    port = server.port
    server.stop()
    server.stop()
    again = FoundryAPI(svc, port=port).start()
    try:
        assert again.port == port
    finally:
        again.stop()


def test_not_running_service_503(svc):
    svc.running = False
    with FoundryAPI(svc, port=0) as server:
        assert request(server, "POST", "/api/orders", {"title": "t"})[0].status == 503
        resp, text = request(server, "POST", "/api/ask", {"question": "q"})
        assert resp.status == 503 and json.loads(text)["code"] == "not_running"
        assert request(server, "GET", "/api/health")[0].status == 200
    assert svc.orders == [] and svc.asked == []


def test_status_keeps_service_ok_flag(svc):
    svc.status = lambda: {"ok": False, "tasks": []}
    with FoundryAPI(svc, port=0) as server:
        body = json.loads(request(server, "GET", "/api/status")[1])
    assert body["ok"] is False and body["foreman"] == {}


def test_second_api_on_same_port_fails(svc):
    with FoundryAPI(svc, port=0) as first:
        with pytest.raises(OSError):
            FoundryAPI(svc, port=first.port).start()
        assert request(first, "GET", "/api/health")[0].status == 200


class _FakeSocket:
    def __init__(self):
        self.opts = []

    def setsockopt(self, level, name, value):
        self.opts.append((level, name, value))

    def bind(self, addr):
        self.addr = addr

    def getsockname(self):
        return ("127.0.0.1", 4310)


@pytest.mark.parametrize("platform", ["win32", "linux"])
def test_bind_socket_options_per_platform(monkeypatch, platform):
    import socket as socket_mod

    from foundry import serve_api

    monkeypatch.setattr(serve_api.sys, "platform", platform)
    monkeypatch.setattr(socket_mod, "SO_EXCLUSIVEADDRUSE", 0x7FFF0004, raising=False)
    server = serve_api._Server(("127.0.0.1", 4310), serve_api._Handler, bind_and_activate=False)
    real = server.socket
    fake = server.socket = _FakeSocket()
    try:
        server.server_bind()
    finally:
        real.close()
    names = {name for _, name, _ in fake.opts}
    if platform == "win32":
        assert server.allow_reuse_address is False
        assert 0x7FFF0004 in names and socket_mod.SO_REUSEADDR not in names
    else:
        assert server.allow_reuse_address is True
        assert socket_mod.SO_REUSEADDR in names and 0x7FFF0004 not in names


def test_ask_chunk_timeout_sends_error_and_cancels(monkeypatch, svc):
    from foundry import serve_api

    monkeypatch.setattr(serve_api, "ASK_CHUNK_TIMEOUT_S", 0.3)
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    svc.loop = loop
    cancelled = threading.Event()

    async def ask(question):
        yield "first "
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        yield "never"

    svc.ask = ask
    try:
        with FoundryAPI(svc, port=0) as server:
            events = parse_sse(request(server, "POST", "/api/ask", {"question": "q"})[1])
        assert events[0] == ("message", {"text": "first "})
        assert events[1][0] == "error" and "too long" in events[1][1]["error"]
        assert events[-1] == ("done", {})
        assert cancelled.wait(5)
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(5)
        loop.close()


@pytest.fixture()
def events_queue(svc):
    """A real queue holding events 1 and 2 (task_added) and 3 (repo_root, F-001)."""
    queue = FoundryQueue(":memory:")
    queue.add_task("F-001", "one", "", "alpha")
    queue.add_task("F-002", "two", "", "alpha")
    queue.set_repo_root("F-001", None)
    svc.factory = SimpleNamespace(queue=queue)
    yield queue
    queue.close()


def event_ids(api, query):
    resp, text = request(api, "GET", "/api/events" + query)
    assert resp.status == 200, text
    body = json.loads(text)
    assert body["ok"] is True
    return [e["id"] for e in body["events"]], body["last_id"]


def test_events_all_in_id_order(api, events_queue):
    resp, text = request(api, "GET", "/api/events")
    assert resp.status == 200
    body = json.loads(text)
    assert [e["id"] for e in body["events"]] == [1, 2, 3]
    assert body["last_id"] == 3
    first = body["events"][0]
    assert set(first) == {"id", "ts", "kind", "task_id", "payload"}
    assert first["kind"] == "task_added" and first["task_id"] == "F-001"
    assert isinstance(first["payload"], dict)


def test_events_after_since_only(api, events_queue):
    assert event_ids(api, "?since=1") == ([2, 3], 3)


def test_events_last_id_is_since_when_none_returned(api, events_queue):
    assert event_ids(api, "?since=3") == ([], 3)


def test_events_limit_respected(api, events_queue):
    assert event_ids(api, "?limit=2") == ([1, 2], 2)
    assert event_ids(api, "?since=1&limit=1") == ([2], 2)


def test_events_kind_and_task_filters(api, events_queue):
    assert event_ids(api, "?kind=repo_root") == ([3], 3)
    assert event_ids(api, "?task=F-002") == ([2], 2)
    assert event_ids(api, "?kind=task_added&task=F-001") == ([1], 1)


@pytest.mark.parametrize("query, expected", [
    ("", 200),
    ("?limit=0", 1),
    ("?limit=-5", 1),
    ("?limit=1000", 1000),
    ("?limit=5000", 1000),
])
def test_events_limit_clamped(api, svc, query, expected):
    seen = []

    def events(**kwargs):
        seen.append(kwargs)
        return []

    svc.factory = SimpleNamespace(queue=SimpleNamespace(events=events))
    assert request(api, "GET", "/api/events" + query)[0].status == 200
    assert seen == [{"task_id": None, "kind": None, "since_id": 0, "limit": expected}]


@pytest.mark.parametrize("query", [
    "?since=abc",
    "?since=1.5",
    "?since=99999999999999999999",
    "?limit=abc",
    "?limit=1.5",
])
def test_events_bad_numbers_400(api, events_queue, query):
    resp, text = request(api, "GET", "/api/events" + query)
    assert resp.status == 400
    out = json.loads(text)
    assert out["ok"] is False and out["code"] == "invalid" and out["error"]


def test_events_no_factory_attribute(api):
    assert event_ids(api, "") == ([], 0)
    assert event_ids(api, "?since=7") == ([], 7)


def test_events_factory_not_built_yet(api, svc):
    svc.factory = None
    assert event_ids(api, "?since=7&limit=3") == ([], 7)


def test_events_no_factory_still_rejects_bad_since(api):
    assert request(api, "GET", "/api/events?since=nope")[0].status == 400
