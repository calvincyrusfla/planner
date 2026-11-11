"""The lookup server: MCP over stdio against a fake library."""

from __future__ import annotations

import ast
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from foundry import library_mcp
from foundry.library_mcp import MAX_OUTPUT_CHARS, main, serve

KNOWN = "a.py::f"
LONELY = "a.py::g"
UNKNOWN = "a.py::missing"
SECTION = {
    "call": KNOWN,
    "kind": "function",
    "path": "a.py",
    "start": 1,
    "end": 3,
    "signature": "def f()",
}
REF = {"path": "b.py", "line": 7, "text": "f()"}
NON_ASCII = "1: naïve = '→ 日本語 €'"


class FakeBackend:
    """Answers from fixed data and records how it was called."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.sections = {KNOWN: "1: def f():", LONELY: "9: def g():"}

    def find(self, name: str, limit: int = 20) -> list[dict[str, Any]]:
        self.calls.append(("find", name, limit))
        return [SECTION] if name != "nothing" else []

    def show(self, call: str, context: int = 0) -> str | None:
        self.calls.append(("show", call, context))
        return self.sections.get(call)

    def callers(self, call: str, limit: int = 30) -> list[dict[str, Any]]:
        self.calls.append(("callers", call, limit))
        return [REF] if call == KNOWN else []

    def tests_for(self, call: str, limit: int = 30) -> list[dict[str, Any]]:
        self.calls.append(("tests_for", call, limit))
        return [REF] if call == KNOWN else []

    def outline(self, path: str) -> list[dict[str, Any]]:
        self.calls.append(("outline", path))
        return [SECTION]


class BrokenBackend(FakeBackend):
    """Fails on every search."""

    def find(self, name: str, limit: int = 20) -> list[dict[str, Any]]:
        raise RuntimeError("database is locked")


class BadRowBackend(FakeBackend):
    """Returns a row without the promised keys."""

    def find(self, name: str, limit: int = 20) -> list[dict[str, Any]]:
        return [{"call": KNOWN}]


class NoisyBackend(FakeBackend):
    """Prints to stdout while it works."""

    def find(self, name: str, limit: int = 20) -> list[dict[str, Any]]:
        print("stray output")
        return super().find(name, limit)


def _raw(backend: Any, *lines: str) -> str:
    """Feed ``lines`` to the server and return what it wrote."""
    out = io.StringIO()
    serve(backend, io.StringIO("".join(line + "\n" for line in lines)), out)
    return out.getvalue()


def _exchange(backend: Any, *messages: dict[str, Any]) -> list[dict[str, Any]]:
    """Send ``messages`` and return the parsed replies."""
    raw = _raw(backend, *(json.dumps(message) for message in messages))
    return [json.loads(line) for line in raw.splitlines()]


def _call(backend: Any, tool: str, /, **arguments: Any) -> dict[str, Any]:
    """The result of one ``tools/call``."""
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool, "arguments": arguments},
    }
    (reply,) = _exchange(backend, request)
    result: dict[str, Any] = reply["result"]
    return result


def _text(result: dict[str, Any]) -> str:
    """The single text block of a tool result."""
    (block,) = result["content"]
    assert block["type"] == "text"
    text: str = block["text"]
    return text


def test_initialize_echoes_the_client_version() -> None:
    (reply,) = _exchange(
        FakeBackend(),
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2099-01-01"}},
    )
    assert reply == {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "protocolVersion": "2099-01-01",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "foundry-library", "version": "1"},
        },
    }


def test_handshake_answers_requests_but_not_the_initialized_notification() -> None:
    replies = _exchange(
        FakeBackend(),
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "v"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    )
    assert [reply["id"] for reply in replies] == [1, 2]


def test_tools_list_schemas() -> None:
    (reply,) = _exchange(FakeBackend(), {"jsonrpc": "2.0", "id": "a", "method": "tools/list"})
    tools = {tool["name"]: tool for tool in reply["result"]["tools"]}
    expected = {
        "find": ({"name", "limit"}, ["name"]),
        "show": ({"call", "context"}, ["call"]),
        "callers": ({"call", "limit"}, ["call"]),
        "tests_for": ({"call", "limit"}, ["call"]),
        "outline": ({"path"}, ["path"]),
    }
    assert set(tools) == set(expected)
    for name, (properties, required) in expected.items():
        schema = tools[name]["inputSchema"]
        assert schema["type"] == "object"
        assert set(schema["properties"]) == properties
        assert schema["required"] == required
        assert "call number" in tools[name]["description"]
        assert "::" in tools[name]["description"]
        assert "`show`" in tools[name]["description"]


def test_find_renders_compact_lines() -> None:
    backend = FakeBackend()
    result = _call(backend, "find", name="f", limit=5)
    assert result["isError"] is False
    assert _text(result) == "a.py::f  a.py:1-3  def f()"
    assert backend.calls == [("find", "f", 5)]


def test_find_without_matches_is_not_an_error() -> None:
    result = _call(FakeBackend(), "find", name="nothing")
    assert result["isError"] is False
    assert "No sections" in _text(result)


def test_show_returns_the_section() -> None:
    backend = FakeBackend()
    result = _call(backend, "show", call=KNOWN, context=2)
    assert result["isError"] is False
    assert _text(result) == "1: def f():"
    assert backend.calls == [("show", KNOWN, 2)]


@pytest.mark.parametrize("tool", ["callers", "tests_for"])
def test_references_render_compact_lines(tool: str) -> None:
    backend = FakeBackend()
    result = _call(backend, tool, call=KNOWN, limit=4)
    assert result["isError"] is False
    assert _text(result) == "b.py:7  f()"
    assert backend.calls == [(tool, KNOWN, 4)]


@pytest.mark.parametrize("tool", ["callers", "tests_for"])
def test_known_section_without_references_is_not_an_error(tool: str) -> None:
    result = _call(FakeBackend(), tool, call=LONELY)
    assert result["isError"] is False
    assert LONELY in _text(result)


def test_outline_renders_compact_lines() -> None:
    backend = FakeBackend()
    result = _call(backend, "outline", path="a.py")
    assert result["isError"] is False
    assert _text(result) == "a.py::f  a.py:1-3  def f()"
    assert backend.calls == [("outline", "a.py")]


@pytest.mark.parametrize("tool", ["show", "callers", "tests_for"])
def test_unknown_call_number(tool: str) -> None:
    result = _call(FakeBackend(), tool, call=UNKNOWN)
    assert result["isError"] is True
    assert "Unknown call number" in _text(result)
    assert UNKNOWN in _text(result)


def test_backend_error_is_a_tool_error_and_the_server_keeps_going() -> None:
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "find", "arguments": {"name": "f"}},
    }
    failed, alive = _exchange(BrokenBackend(), request, {"jsonrpc": "2.0", "id": 2, "method": "ping"})
    assert failed["result"]["isError"] is True
    assert "database is locked" in _text(failed["result"])
    assert alive == {"jsonrpc": "2.0", "id": 2, "result": {}}


def test_backend_row_without_keys_is_a_tool_error() -> None:
    assert _call(BadRowBackend(), "find", name="f")["isError"] is True


@pytest.mark.parametrize(
    "arguments",
    [{}, {"call": 5}, {"call": KNOWN, "context": -1}, {"call": KNOWN, "context": True}, {"call": KNOWN, "x": 1}],
)
def test_bad_arguments_are_tool_errors(arguments: dict[str, Any]) -> None:
    backend = FakeBackend()
    result = _call(backend, "show", **arguments)
    assert result["isError"] is True
    assert backend.calls == []


def test_unknown_tool_is_invalid_params() -> None:
    (reply,) = _exchange(
        FakeBackend(),
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "nope", "arguments": {}}},
    )
    assert reply["error"]["code"] == -32602


def test_ping_gets_an_empty_result() -> None:
    (reply,) = _exchange(FakeBackend(), {"jsonrpc": "2.0", "id": 9, "method": "ping"})
    assert reply == {"jsonrpc": "2.0", "id": 9, "result": {}}


def test_unknown_method() -> None:
    (reply,) = _exchange(FakeBackend(), {"jsonrpc": "2.0", "id": 3, "method": "nope"})
    assert reply["id"] == 3
    assert reply["error"]["code"] == -32601


def test_malformed_line_keeps_the_server_running() -> None:
    raw = _raw(FakeBackend(), "{not json", json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"}))
    first, second = (json.loads(line) for line in raw.splitlines())
    assert first["id"] is None
    assert first["error"]["code"] == -32700
    assert second["id"] == 2


def test_deeply_nested_line_is_a_parse_error() -> None:
    (line,) = _raw(FakeBackend(), "[" * 200_000).splitlines()
    assert json.loads(line)["error"]["code"] == -32700


@pytest.mark.parametrize("line", ["[1, 2]", "5", '{"id": {"a": 1}, "method": "ping"}', '{"id": 1.5, "method": "ping"}'])
def test_invalid_requests_answer_with_a_null_id(line: str) -> None:
    (reply,) = (json.loads(out) for out in _raw(FakeBackend(), line).splitlines())
    assert reply["id"] is None
    assert reply["error"]["code"] == -32600


def test_notifications_are_never_answered_or_run() -> None:
    backend = FakeBackend()
    raw = _raw(
        backend,
        json.dumps({"jsonrpc": "2.0", "method": "notifications/cancelled"}),
        json.dumps({"jsonrpc": "2.0", "method": "no/such/method"}),
        json.dumps({"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "find"}}),
    )
    assert raw == ""
    assert backend.calls == []


def test_blank_lines_are_skipped() -> None:
    assert _raw(FakeBackend(), "", "   ") == ""


def test_output_is_capped_with_a_note() -> None:
    backend = FakeBackend()
    backend.sections[KNOWN] = "x" * (MAX_OUTPUT_CHARS * 3)
    text = _text(_call(backend, "show", call=KNOWN))
    assert len(text) <= MAX_OUTPUT_CHARS
    assert "Narrow the query" in text


def test_output_at_the_cap_is_untouched() -> None:
    backend = FakeBackend()
    backend.sections[KNOWN] = "x" * MAX_OUTPUT_CHARS
    assert _text(_call(backend, "show", call=KNOWN)) == backend.sections[KNOWN]


def test_stdout_carries_only_protocol_lines(capsys: pytest.CaptureFixture[str]) -> None:
    requests = [
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
        json.dumps(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "find", "arguments": {"name": "f"}}}
        ),
        "{not json",
    ]
    raw = _raw(NoisyBackend(), *requests)
    lines = raw.splitlines()
    assert len(lines) == len(requests)
    assert all(json.loads(line)["jsonrpc"] == "2.0" for line in lines)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "stray output" in captured.err


def _byte_stream(data: bytes, encoding: str) -> tuple[io.BytesIO, io.TextIOWrapper]:
    """A text stream with a strict legacy encoding over in-memory bytes."""
    raw = io.BytesIO(data)
    return raw, io.TextIOWrapper(raw, encoding=encoding, errors="strict", newline="")


def _show_request() -> str:
    return json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "show", "arguments": {"call": KNOWN}}}
    )


@pytest.mark.parametrize("encoding", ["cp1252", "ascii"])
@pytest.mark.parametrize("source", [NON_ASCII, "1: bad = '\udc80'"], ids=["unicode", "surrogate"])
def test_non_ascii_source_survives_a_legacy_stdout(encoding: str, source: str) -> None:
    backend = FakeBackend()
    backend.sections[KNOWN] = source
    raw, stdout = _byte_stream(b"", encoding)
    serve(backend, io.StringIO(_show_request() + "\n"), stdout)
    stdout.flush()
    reply = json.loads(raw.getvalue().decode("ascii"))
    assert reply["result"]["isError"] is False
    assert _text(reply["result"]) == source


def test_client_going_away_ends_the_server_quietly() -> None:
    class ClosedPipe(io.StringIO):
        def write(self, text: str) -> int:
            raise BrokenPipeError("client left")

    serve(FakeBackend(), io.StringIO(_show_request() + "\n"), ClosedPipe())


def test_module_does_not_import_the_library_at_top_level() -> None:
    tree = ast.parse(Path(library_mcp.__file__).read_text(encoding="utf-8"))
    names: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            names.append(node.module or "")
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
    assert not [name for name in names if "code_library" in name]


def test_main_reads_and_writes_utf8_on_legacy_streams(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = FakeBackend()
    opened: list[str] = []

    def open_library(db_path: str) -> FakeBackend:
        opened.append(db_path)
        return backend

    monkeypatch.setitem(sys.modules, "foundry.code_library", SimpleNamespace(open_library=open_library))
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "find", "arguments": {"name": "café→"}},
    }
    # 0x81 is undefined in cp1252: without UTF-8 input this line would not decode at all.
    line = json.dumps(request, ensure_ascii=False).encode("utf-8") + b"\n" + b"\x81\n"
    _, stdin = _byte_stream(line, "cp1252")
    raw, stdout = _byte_stream(b"", "cp1252")
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr(sys, "stdout", stdout)

    assert main(["--db", "lib.db"]) == 0

    stdout.flush()
    found, malformed = (json.loads(out) for out in raw.getvalue().decode("ascii").splitlines())
    assert opened == ["lib.db"]
    assert backend.calls == [("find", "café→", 20)]
    assert found["result"]["isError"] is False
    assert malformed["error"]["code"] == -32700
    assert b"\r" not in raw.getvalue()


def test_main_reports_a_library_that_will_not_open(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def open_library(db_path: str) -> FakeBackend:
        raise OSError("no such database")

    monkeypatch.setitem(sys.modules, "foundry.code_library", SimpleNamespace(open_library=open_library))
    assert main(["--db", "missing.db"]) == 1
    assert capsys.readouterr().out == ""
