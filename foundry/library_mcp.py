"""The code library as tools for Claude Code: an MCP server over stdio.

A minimal Model Context Protocol server written with the standard library only.
It speaks JSON-RPC 2.0, one JSON object per line on stdin and stdout, and offers
five lookup tools (``find``, ``show``, ``callers``, ``tests_for``, ``outline``)
over any object with the :class:`Backend` methods.  Run it with
``python -m foundry.library_mcp --db PATH``.

Stdout carries protocol lines and nothing else.  Replies are pure ASCII (JSON
escapes everything else), so they survive any stdout encoding, including the
legacy code pages Windows gives a piped process.  Logs go to stderr.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import logging
import os
import sys
import tempfile
from typing import Any, Protocol, TextIO

log = logging.getLogger(__name__)

MAX_OUTPUT_CHARS = 20_000
DEFAULT_PROTOCOL_VERSION = "2025-06-18"

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

_TRUNCATION_NOTE = (
    "\n[Output cut at {limit} characters. Narrow the query: use a more specific "
    "name, a lower limit, or `show` one call number with less context.]"
)

_CALL_NUMBER = (
    "A call number names one section of code: `<repo-relative posix path>::<qualified name>`, "
    "e.g. `foundry/floor_manager.py::FloorManager.pr_request` or "
    "`department/lib/order-check.mjs::checkOrder`."
)
_PREFER_SHOW = "Prefer `show` on a call number over reading whole files."

_CALL_PROPERTY: dict[str, Any] = {"type": "string", "description": "The call number."}
_LIMIT_PROPERTY: dict[str, Any] = {
    "type": "integer",
    "minimum": 1,
    "description": "The most results to return.",
}


def _schema(properties: dict[str, Any], required: str) -> dict[str, Any]:
    """A JSON Schema object with ``properties`` of which ``required`` must be given."""
    return {
        "type": "object",
        "properties": properties,
        "required": [required],
        "additionalProperties": False,
    }


TOOLS: list[dict[str, Any]] = [
    {
        "name": "find",
        "description": (
            f"Search the code library by function or class name and list the matching call "
            f"numbers with their line ranges and signatures. {_CALL_NUMBER} {_PREFER_SHOW}"
        ),
        "inputSchema": _schema(
            {
                "name": {"type": "string", "description": "The name, or part of it, to search for."},
                "limit": _LIMIT_PROPERTY,
            },
            "name",
        ),
    },
    {
        "name": "show",
        "description": (
            f"Return the source of one section, with line numbers, by its call number. "
            f"{_CALL_NUMBER} {_PREFER_SHOW}"
        ),
        "inputSchema": _schema(
            {
                "call": _CALL_PROPERTY,
                "context": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "Extra lines to include before and after the section.",
                },
            },
            "call",
        ),
    },
    {
        "name": "callers",
        "description": f"List the places that call a section. {_CALL_NUMBER} {_PREFER_SHOW}",
        "inputSchema": _schema({"call": _CALL_PROPERTY, "limit": _LIMIT_PROPERTY}, "call"),
    },
    {
        "name": "tests_for",
        "description": f"List the tests related to a section. {_CALL_NUMBER} {_PREFER_SHOW}",
        "inputSchema": _schema({"call": _CALL_PROPERTY, "limit": _LIMIT_PROPERTY}, "call"),
    },
    {
        "name": "outline",
        "description": (
            f"List every section of one file with its call number, line range and signature. "
            f"{_CALL_NUMBER} {_PREFER_SHOW}"
        ),
        "inputSchema": _schema(
            {"path": {"type": "string", "description": "The repo-relative posix path of the file."}},
            "path",
        ),
    },
]
_TOOLS_BY_NAME = {tool["name"]: tool for tool in TOOLS}


class Backend(Protocol):
    """What the server needs from a code library."""

    def find(self, name: str, limit: int = 20) -> list[dict[str, Any]]:
        """Sections matching ``name``."""

    def show(self, call: str, context: int = 0) -> str | None:
        """The section's source with line numbers, None if the call number is unknown."""

    def callers(self, call: str, limit: int = 30) -> list[dict[str, Any]]:
        """The places that call the section."""

    def tests_for(self, call: str, limit: int = 30) -> list[dict[str, Any]]:
        """The tests related to the section."""

    def outline(self, path: str) -> list[dict[str, Any]]:
        """The file's sections."""


class _RpcError(Exception):
    """A request that must be answered with a JSON-RPC error."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _error(request_id: str | int | None, code: int, message: str) -> dict[str, Any]:
    """A JSON-RPC error reply."""
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _cap(text: str) -> str:
    """``text`` cut to the output limit, with a note on how to narrow the query."""
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    note = _TRUNCATION_NOTE.format(limit=MAX_OUTPUT_CHARS)
    return text[: MAX_OUTPUT_CHARS - len(note)] + note


def _tool_result(text: str, is_error: bool) -> dict[str, Any]:
    """The result of a ``tools/call``."""
    return {"content": [{"type": "text", "text": _cap(text)}], "isError": is_error}


def _argument_problem(tool: dict[str, Any], arguments: dict[str, Any]) -> str | None:
    """Why ``arguments`` do not fit the tool's input schema, None when they do."""
    schema = tool["inputSchema"]
    for key in schema["required"]:
        if key not in arguments:
            return f"Missing required argument `{key}` for `{tool['name']}`."
    for key, value in arguments.items():
        spec = schema["properties"].get(key)
        if spec is None:
            return f"Unknown argument `{key}` for `{tool['name']}`."
        if spec["type"] == "string" and not isinstance(value, str):
            return f"Argument `{key}` must be a string."
        if spec["type"] == "integer":
            if isinstance(value, bool) or not isinstance(value, int):
                return f"Argument `{key}` must be an integer."
            if value < spec["minimum"]:
                return f"Argument `{key}` must be at least {spec['minimum']}."
    return None


def _unknown_call(call: str) -> str:
    """The text for a call number the library does not know."""
    return f"Unknown call number: {call}. Use `find` to look up the right one."


def _section_line(section: dict[str, Any]) -> str:
    """One section as ``call  path:start-end  signature``."""
    return (
        f"{section['call']}  {section['path']}:{section['start']}-{section['end']}"
        f"  {section['signature']}"
    )


def _run_tool(backend: Backend, name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
    """Call the backend and render its answer as ``(text, is_error)``."""
    result = getattr(backend, name)(**arguments)
    if name == "show":
        if result is None:
            return _unknown_call(arguments["call"]), True
        return str(result), False
    if name in ("find", "outline"):
        if not result:
            subject = arguments["name"] if name == "find" else arguments["path"]
            return f"No sections found for {subject}.", False
        return "\n".join(_section_line(section) for section in result), False
    if result:
        return "\n".join(f"{ref['path']}:{ref['line']}  {ref['text']}" for ref in result), False
    # An empty list is ambiguous: only `show` can tell an unknown call number apart.
    call = arguments["call"]
    if backend.show(call) is None:
        return _unknown_call(call), True
    return f"No {'callers' if name == 'callers' else 'tests'} found for {call}.", False


def _call_tool(backend: Backend, params: dict[str, Any]) -> dict[str, Any]:
    """Handle ``tools/call``; tool failures are results, never exceptions."""
    name = params.get("name")
    tool = _TOOLS_BY_NAME.get(name) if isinstance(name, str) else None
    if tool is None or not isinstance(name, str):
        raise _RpcError(INVALID_PARAMS, f"Unknown tool: {name}")
    arguments = params.get("arguments")
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        return _tool_result("`arguments` must be an object.", True)
    problem = _argument_problem(tool, arguments)
    if problem is not None:
        return _tool_result(problem, True)
    try:
        text, is_error = _run_tool(backend, name, arguments)
    except Exception as exc:
        log.exception("library tool %s failed", name)
        return _tool_result(f"Library error: {exc}", True)
    return _tool_result(text, is_error)


def _dispatch(backend: Backend, method: str, params: dict[str, Any]) -> dict[str, Any]:
    """The result of one request."""
    if method == "initialize":
        version = params.get("protocolVersion")
        return {
            "protocolVersion": version if isinstance(version, str) else DEFAULT_PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "foundry-library", "version": "1"},
        }
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        return _call_tool(backend, params)
    raise _RpcError(METHOD_NOT_FOUND, f"Method not found: {method}")


def _handle_line(backend: Backend, line: str) -> dict[str, Any] | None:
    """The reply to one input line, None when it must go unanswered."""
    try:
        message = json.loads(line)
    except (ValueError, RecursionError):
        return _error(None, PARSE_ERROR, "Parse error")
    if not isinstance(message, dict):
        return _error(None, INVALID_REQUEST, "Invalid request: expected a JSON object")
    if "id" not in message:
        return None
    request_id = message["id"]
    if isinstance(request_id, bool) or not isinstance(request_id, (str, int)):
        return _error(None, INVALID_REQUEST, "Invalid request: id must be a string or an integer")
    method = message.get("method")
    if not isinstance(method, str):
        return _error(request_id, INVALID_REQUEST, "Invalid request: method must be a string")
    params = message.get("params")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return _error(request_id, INVALID_PARAMS, "Invalid params: expected an object")
    try:
        return {"jsonrpc": "2.0", "id": request_id, "result": _dispatch(backend, method, params)}
    except _RpcError as exc:
        return _error(request_id, exc.code, exc.message)
    except Exception:
        log.exception("request %s failed", method)
        return _error(request_id, INTERNAL_ERROR, "Internal error")


def _send(stdout: TextIO, reply: dict[str, Any]) -> bool:
    """Write one reply line; False when the client has gone away."""
    # ensure_ascii keeps the line encodable on any stdout (cp1252 would raise otherwise).
    line = json.dumps(reply, ensure_ascii=True, separators=(",", ":"))
    try:
        stdout.write(line + "\n")
        stdout.flush()
    except (OSError, ValueError):
        log.info("client closed the connection")
        return False
    return True


def serve(backend: Backend, stdin: TextIO, stdout: TextIO) -> None:
    """Answer requests from ``stdin`` on ``stdout`` until end of input."""
    # A stray print in a backend must not land between the protocol lines.
    with contextlib.redirect_stdout(sys.stderr):
        for line in stdin:
            if not line.strip():
                continue
            reply = _handle_line(backend, line)
            if reply is not None and not _send(stdout, reply):
                return


def _use_utf8(stream: TextIO) -> None:
    """Make a real console or pipe stream speak UTF-8 with plain newlines.

    Windows gives piped processes a legacy code page, on which a client's UTF-8
    can be misread or fail to decode at all.
    """
    if isinstance(stream, io.TextIOWrapper):
        stream.reconfigure(encoding="utf-8", errors="replace", newline="\n")


def main(argv: list[str] | None = None) -> int:
    """Open the library at ``--db`` and serve it over stdio."""
    parser = argparse.ArgumentParser(description="Serve the code library over MCP on stdio.")
    parser.add_argument("--db", default=None, help="path of the code library database")
    parser.add_argument(
        "--repo", default=None, help="repo to catalogue when --db is not given (default: cwd)"
    )
    args = parser.parse_args(argv)
    logging.basicConfig(
        stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    _use_utf8(sys.stdin)
    _use_utf8(sys.stdout)
    tmp_dir: tempfile.TemporaryDirectory[str] | None = None
    try:
        from foundry.code_library import build_library, open_library

        db_path = args.db
        if db_path is None:
            repo = os.path.abspath(args.repo or os.getcwd())
            tmp_dir = tempfile.TemporaryDirectory(prefix="foundry-library-")
            db_path = os.path.join(tmp_dir.name, "library.db")
            info = build_library(repo, db_path)
            log.info("catalogued %s: %s entries", repo, info["entries"])
        backend = open_library(db_path)
    except Exception:
        log.exception("could not open the code library")
        if tmp_dir is not None:
            tmp_dir.cleanup()
        return 1
    try:
        serve(backend, sys.stdin, sys.stdout)
    finally:
        if tmp_dir is not None:
            tmp_dir.cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
