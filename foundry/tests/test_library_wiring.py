"""Tests for the library server wiring: builder/reviewer args and library_mcp --repo."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest

from foundry import main as fmain
from foundry import library_mcp
from foundry.config import FoundryConfig
from foundry.main import builder_claude_args, reviewer_claude_args

EDIT = {"FOUNDRY_BUILDER_MODE": "edit"}


def _value_after(args: list[str], flag: str) -> str:
    return args[args.index(flag) + 1]


@pytest.fixture(autouse=True)
def _config_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "data" / "library_mcp.json"
    monkeypatch.setattr(fmain, "_LIBRARY_CONFIG_PATH", path)
    monkeypatch.setattr(fmain, "_library_config_written", None)
    return path


def test_library_off_adds_nothing(_config_path: Path) -> None:
    args = builder_claude_args(FoundryConfig.from_env(environ=EDIT))
    assert "--mcp-config" not in args
    assert "mcp__library" not in _value_after(args, "--allowedTools")
    assert fmain.LIBRARY_PROMPT not in _value_after(args, "--append-system-prompt")
    assert not _config_path.exists()


def test_library_on_in_edit_mode(_config_path: Path) -> None:
    args = builder_claude_args(FoundryConfig.from_env(environ={**EDIT, "FOUNDRY_BUILDER_LIBRARY": "on"}))
    assert _value_after(args, "--mcp-config") == str(_config_path)
    assert _value_after(args, "--allowedTools") == "Read Grep Glob Edit Write mcp__library"
    assert _value_after(args, "--append-system-prompt").endswith(fmain.LIBRARY_PROMPT)
    server = json.loads(_config_path.read_text(encoding="utf-8"))["mcpServers"]["library"]
    assert server["args"] == ["-m", "foundry.library_mcp"]
    assert server["env"]["PYTHONPATH"] == str(Path(fmain.__file__).resolve().parent.parent)
    assert "--strict-mcp-config" in args


def test_library_on_in_read_mode_and_not_in_diff_mode() -> None:
    read = builder_claude_args(
        FoundryConfig.from_env(environ={"FOUNDRY_BUILDER_TOOLS": "read", "FOUNDRY_BUILDER_LIBRARY": "1"})
    )
    assert _value_after(read, "--allowedTools") == "Read Grep Glob mcp__library"
    assert "--mcp-config" in read
    diff = builder_claude_args(FoundryConfig.from_env(environ={"FOUNDRY_BUILDER_LIBRARY": "1"}))
    assert "--mcp-config" not in diff


def test_config_file_written_once(_config_path: Path) -> None:
    cfg = FoundryConfig.from_env(environ={**EDIT, "FOUNDRY_BUILDER_LIBRARY": "on"})
    builder_claude_args(cfg)
    _config_path.write_text("marker", encoding="utf-8")
    builder_claude_args(cfg)
    assert _config_path.read_text(encoding="utf-8") == "marker"


def test_strict_flag_for_builder_and_reviewer() -> None:
    on = FoundryConfig.from_env(environ={})
    assert builder_claude_args(on).count("--strict-mcp-config") == 1
    assert reviewer_claude_args(on).count("--strict-mcp-config") == 1
    off = FoundryConfig.from_env(environ={"FOUNDRY_STRICT_MCP": "off", **EDIT})
    assert "--strict-mcp-config" not in builder_claude_args(off)
    assert "--strict-mcp-config" not in reviewer_claude_args(off)
    bare = FoundryConfig.from_env(
        environ={"FOUNDRY_CLAUDE_ARGS_REPLACE": "1", "FOUNDRY_CLAUDE_ARGS": "--model sonnet"}
    )
    assert "--strict-mcp-config" in reviewer_claude_args(bare)


def test_defaults() -> None:
    cfg = FoundryConfig.from_env(environ={})
    assert cfg.builder_library is False and cfg.strict_mcp is True
    assert cfg.builder_sessions == 3 and cfg.builder_session_turns == 25


def test_max_turns_choice() -> None:
    many = FoundryConfig.from_env(environ={**EDIT, "FOUNDRY_BUILDER_SESSION_TURNS": "30"})
    assert _value_after(builder_claude_args(many), "--max-turns") == "30"
    one = FoundryConfig.from_env(environ={**EDIT, "FOUNDRY_BUILDER_SESSIONS": "1"})
    assert _value_after(builder_claude_args(one), "--max-turns") == "60"


def test_main_with_repo_builds_and_serves(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "mod.py").write_text("def greet():\n    return 1\n", encoding="utf-8")
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t"}}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "find", "arguments": {"name": "greet"}}},
    ]
    stdin = io.StringIO("".join(json.dumps(r) + "\n" for r in requests))
    stdout = io.StringIO()
    monkeypatch.setattr("sys.stdin", stdin)
    monkeypatch.setattr("sys.stdout", stdout)
    monkeypatch.setattr(library_mcp, "_use_utf8", lambda stream: None)
    assert library_mcp.main(["--repo", str(repo)]) == 0
    replies: list[dict[str, Any]] = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert replies[0]["id"] == 1 and "result" in replies[0]
    text = replies[1]["result"]["content"][0]["text"]
    assert "mod.py::greet" in text
