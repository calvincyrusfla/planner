"""Tests for foundry.gemma_client (no real Ollama: a local fake HTTP server)."""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Self

import pytest

from foundry.gemma_client import (
    GemmaClient,
    GemmaReply,
    GemmaRequest,
    GemmaToolCall,
    GemmaUnavailable,
    OllamaGemmaClient,
    SimulatedGemmaClient,
    parse_chat_completion,
)

Handler = Callable[[dict[str, Any]], tuple[int, Any]]


def completion(content: str | None = "", tool_calls: list[dict] | None = None, usage: dict | None = None) -> dict:
    """Build an OpenAI-style chat.completion body."""
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    body: dict[str, Any] = {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "model": "gemma4:e4b",
        "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls" if tool_calls else "stop"}],
    }
    if usage is not None:
        body["usage"] = usage
    return body


class FakeOllama:
    """A tiny threaded HTTP server on 127.0.0.1 that records requests."""

    def __init__(self, handler: Handler, delay: float = 0.0) -> None:
        self.handler = handler
        self.delay = delay
        self.requests: list[dict[str, Any]] = []
        self.paths: list[str] = []
        self.headers: list[dict[str, str]] = []
        fake = self

        class _H(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length)
                body = json.loads(raw.decode("utf-8"))
                fake.requests.append(body)
                fake.paths.append(self.path)
                fake.headers.append(dict(self.headers))
                if fake.delay:
                    time.sleep(fake.delay)
                status, resp = fake.handler(body)
                data = resp if isinstance(resp, bytes) else json.dumps(resp).encode("utf-8")
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *args: Any) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _H)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def __enter__(self) -> Self:
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def free_port() -> Iterator[int]:
    """A port with nothing listening on it."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    yield port


# --------------------------------------------------------------------------- Ollama client


def test_plain_chat_posts_openai_payload_and_parses_usage() -> None:
    with FakeOllama(lambda b: (200, completion("hello", usage={"prompt_tokens": 12, "completion_tokens": 3}))) as srv:
        client = OllamaGemmaClient(base_url=srv.base_url + "/", model="gemma4:e4b", timeout=5)
        reply = asyncio.run(client.chat([{"role": "user", "content": "hi"}], temperature=0.2))
    assert isinstance(reply, GemmaReply)
    assert reply.text == "hello"
    assert reply.tool_calls == []
    assert reply.usage.prompt_tokens == 12 and reply.usage.completion_tokens == 3
    assert reply.usage.total_tokens == 15
    assert reply.finish_reason == "stop"
    assert srv.paths == ["/v1/chat/completions"]
    body = srv.requests[0]
    assert body["model"] == "gemma4:e4b"
    assert body["messages"] == [{"role": "user", "content": "hi"}]
    assert body["temperature"] == 0.2
    assert body["stream"] is False
    assert "tools" not in body and "response_format" not in body
    assert srv.headers[0]["Content-Type"] == "application/json"


def test_tools_pass_through_and_tool_calls_are_decoded() -> None:
    tools = [
        {
            "type": "function",
            "function": {
                "name": "get_queue_summary",
                "description": "Queue summary",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    calls = [
        {"id": "call_1", "type": "function", "function": {"name": "get_token_velocity", "arguments": '{"window_minutes": 15}'}},
        {"id": "call_2", "type": "function", "function": {"name": "get_queue_summary", "arguments": {}}},
        {"id": "call_3", "type": "function", "function": {"name": "get_team_bottlenecks", "arguments": ""}},
    ]
    with FakeOllama(lambda b: (200, completion(None, tool_calls=calls))) as srv:
        client = OllamaGemmaClient(base_url=srv.base_url, timeout=5)
        reply = asyncio.run(client.chat([{"role": "user", "content": "how busy?"}], tools=tools))
    assert srv.requests[0]["tools"] == tools
    assert reply.text == ""
    assert reply.tool_calls == [
        GemmaToolCall(name="get_token_velocity", arguments={"window_minutes": 15}, id="call_1"),
        GemmaToolCall(name="get_queue_summary", arguments={}, id="call_2"),
        GemmaToolCall(name="get_team_bottlenecks", arguments={}, id="call_3"),
    ]
    assert reply.usage.prompt_tokens == 0  # usage absent -> zeros


def test_json_schema_response_format() -> None:
    schema = {"title": "Sizing", "type": "object", "properties": {"complexity": {"type": "string"}}}
    with FakeOllama(lambda b: (200, completion('{"complexity": "S"}'))) as srv:
        client = OllamaGemmaClient(base_url=srv.base_url, timeout=5)
        reply = asyncio.run(client.chat([{"role": "user", "content": "size"}], json_schema=schema))
    rf = srv.requests[0]["response_format"]
    assert rf["type"] == "json_schema"
    assert rf["json_schema"]["name"] == "Sizing"
    assert rf["json_schema"]["schema"] == schema
    assert reply.parse_json() == {"complexity": "S"}


def test_json_schema_falls_back_to_json_object_on_400_and_remembers() -> None:
    def handler(body: dict[str, Any]) -> tuple[int, Any]:
        if body.get("response_format", {}).get("type") == "json_schema":
            return 400, {"error": {"message": "response_format json_schema not supported"}}
        return 200, completion('{"ok": true}')

    schema = {"type": "object"}
    with FakeOllama(handler) as srv:
        client = OllamaGemmaClient(base_url=srv.base_url, timeout=5)
        reply = asyncio.run(client.chat([{"role": "user", "content": "x"}], json_schema=schema))
        assert reply.parse_json() == {"ok": True}
        assert [r["response_format"]["type"] for r in srv.requests] == ["json_schema", "json_object"]
        asyncio.run(client.chat([{"role": "user", "content": "y"}], json_schema=schema))
        assert srv.requests[-1]["response_format"] == {"type": "json_object"}
        assert len(srv.requests) == 3


def test_400_without_json_schema_is_not_retried() -> None:
    with FakeOllama(lambda b: (400, {"error": {"message": "bad request"}})) as srv:
        client = OllamaGemmaClient(base_url=srv.base_url, timeout=5)
        with pytest.raises(GemmaUnavailable) as info:
            asyncio.run(client.chat([{"role": "user", "content": "x"}]))
    assert info.value.status == 400
    assert "HTTP 400" in str(info.value) and "bad request" in str(info.value)
    assert len(srv.requests) == 1


def test_server_error_raises_gemma_unavailable() -> None:
    with FakeOllama(lambda b: (500, {"error": "model not loaded"})) as srv:
        client = OllamaGemmaClient(base_url=srv.base_url, timeout=5)
        with pytest.raises(GemmaUnavailable) as info:
            asyncio.run(client.chat([{"role": "user", "content": "x"}], json_schema={"type": "object"}))
    assert info.value.status == 500
    assert len(srv.requests) == 1  # only 400 triggers the json_object fallback


def test_non_json_body_and_missing_choices_raise() -> None:
    with FakeOllama(lambda b: (200, b"<html>nope</html>")) as srv:
        client = OllamaGemmaClient(base_url=srv.base_url, timeout=5)
        with pytest.raises(GemmaUnavailable, match="non-JSON"):
            asyncio.run(client.chat([{"role": "user", "content": "x"}]))
    with FakeOllama(lambda b: (200, {"choices": []})) as srv:
        client = OllamaGemmaClient(base_url=srv.base_url, timeout=5)
        with pytest.raises(GemmaUnavailable, match="no choices"):
            asyncio.run(client.chat([{"role": "user", "content": "x"}]))


def test_bad_tool_call_arguments_raise() -> None:
    calls = [{"id": "c", "type": "function", "function": {"name": "f", "arguments": "{not json"}}]
    with FakeOllama(lambda b: (200, completion("", tool_calls=calls))) as srv:
        client = OllamaGemmaClient(base_url=srv.base_url, timeout=5)
        with pytest.raises(GemmaUnavailable, match="arguments"):
            asyncio.run(client.chat([{"role": "user", "content": "x"}]))


def test_timeout_raises_gemma_unavailable() -> None:
    with FakeOllama(lambda b: (200, completion("late")), delay=1.0) as srv:
        client = OllamaGemmaClient(base_url=srv.base_url, timeout=0.2)
        start = time.monotonic()
        with pytest.raises(GemmaUnavailable, match="timed out"):
            asyncio.run(client.chat([{"role": "user", "content": "x"}]))
        assert time.monotonic() - start < 1.0


def test_connection_refused_gives_clear_message(free_port: int) -> None:
    client = OllamaGemmaClient(base_url=f"http://127.0.0.1:{free_port}/v1", timeout=2)
    with pytest.raises(GemmaUnavailable) as info:
        asyncio.run(client.chat([{"role": "user", "content": "x"}]))
    msg = str(info.value)
    assert "not reachable" in msg and str(free_port) in msg and "setup_foreman" in msg
    assert info.value.status is None


def test_chat_does_not_block_event_loop() -> None:
    with FakeOllama(lambda b: (200, completion("ok")), delay=0.3) as srv:
        client = OllamaGemmaClient(base_url=srv.base_url, timeout=5)

        async def main() -> int:
            ticks = 0

            async def ticker() -> None:
                nonlocal ticks
                while True:
                    await asyncio.sleep(0.02)
                    ticks += 1

            t = asyncio.create_task(ticker())
            await client.chat([{"role": "user", "content": "x"}])
            t.cancel()
            return ticks

        assert asyncio.run(main()) >= 5


def test_parse_chat_completion_ollama_dict_arguments() -> None:
    reply = parse_chat_completion(
        completion("", tool_calls=[{"function": {"name": "inspect_task_failure", "arguments": {"task_id": "T-1"}}}])
    )
    assert reply.tool_calls[0].arguments == {"task_id": "T-1"}
    assert reply.tool_calls[0].id is None


# --------------------------------------------------------------------------- simulated client


def test_simulated_default_replies_and_records_requests() -> None:
    sim = SimulatedGemmaClient()
    assert isinstance(sim, GemmaClient)
    assert isinstance(OllamaGemmaClient(), GemmaClient)
    r1 = asyncio.run(sim.chat([{"role": "user", "content": "hello there"}]))
    r2 = asyncio.run(sim.chat([{"role": "user", "content": "size"}], json_schema={"type": "object"}, temperature=0.0))
    assert r1.text == "ok" and r2.text == "{}"
    assert sim.calls == 2
    assert sim.requests[1] == GemmaRequest(
        messages=[{"role": "user", "content": "size"}], tools=None, json_schema={"type": "object"}, temperature=0.0
    )
    assert r1.usage.prompt_tokens == 3  # ceil(11/4)
    assert r1.usage.completion_tokens == 1


def test_simulated_responder_shapes() -> None:
    def responder(req: GemmaRequest) -> Any:
        content = req.messages[-1]["content"]
        if content == "tool":
            return {"tool_calls": [{"name": "get_queue_summary", "arguments": "{}"}]}
        if content == "dict":
            return {"complexity": "M"}
        if content == "list":
            return [1, 2]
        if content == "reply":
            return GemmaReply(text="explicit")
        if content == "none":
            return None
        return content.upper()

    sim = SimulatedGemmaClient(responder)

    def ask(text: str) -> GemmaReply:
        return asyncio.run(sim.chat([{"role": "user", "content": text}]))

    tool_reply = ask("tool")
    assert tool_reply.tool_calls == [GemmaToolCall(name="get_queue_summary", arguments={})]
    assert ask("dict").parse_json() == {"complexity": "M"}
    assert ask("list").parse_json() == [1, 2]
    assert ask("reply").text == "explicit"
    assert ask("none").text == ""
    assert ask("abc").text == "ABC"
    # deterministic: same input, same output
    assert ask("abc") == ask("abc")


def test_simulated_async_responder_and_unavailable() -> None:
    async def responder(req: GemmaRequest) -> str:
        await asyncio.sleep(0)
        return "async"

    assert asyncio.run(SimulatedGemmaClient(responder).chat([{"role": "user", "content": "x"}])).text == "async"

    down = SimulatedGemmaClient(unavailable=True)
    with pytest.raises(GemmaUnavailable):
        asyncio.run(down.chat([{"role": "user", "content": "x"}]))
    assert down.calls == 1

    def raiser(req: GemmaRequest) -> str:
        raise GemmaUnavailable("boom")

    with pytest.raises(GemmaUnavailable, match="boom"):
        asyncio.run(SimulatedGemmaClient(raiser).chat([{"role": "user", "content": "x"}]))


def test_parse_json_strips_fence_and_rejects_garbage() -> None:
    assert GemmaReply(text='```json\n{"a": 1}\n```').parse_json() == {"a": 1}
    with pytest.raises(GemmaUnavailable):
        GemmaReply(text="not json").parse_json()
