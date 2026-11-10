"""Tests for builder_claude_args: the flags builders get with FOUNDRY_BUILDER_TOOLS."""

from __future__ import annotations

from foundry.config import DEFAULT_BUILDER_READ_PROMPT, FoundryConfig
from foundry.main import builder_claude_args, claude_args


def _value_after(args: list[str], flag: str) -> str:
    return args[args.index(flag) + 1]


def test_default_config_matches_claude_args() -> None:
    cfg = FoundryConfig.from_env(environ={})
    assert builder_claude_args(cfg) == claude_args(cfg)


def test_read_mode_flags() -> None:
    cfg = FoundryConfig.from_env(environ={"FOUNDRY_BUILDER_TOOLS": "read"})
    before = list(cfg.claude_extra_args)
    args = builder_claude_args(cfg)
    base = claude_args(cfg)
    assert _value_after(args, "--max-turns") == "30"
    assert args.count("--max-turns") == 1
    assert _value_after(args, "--allowedTools") == "Read Grep Glob"
    assert _value_after(args, "--disallowedTools") == _value_after(base, "--disallowedTools")
    assert "--strict-mcp-config" in args
    assert _value_after(args, "--append-system-prompt") == DEFAULT_BUILDER_READ_PROMPT
    assert args.count("--append-system-prompt") == 1
    assert args.count(DEFAULT_BUILDER_READ_PROMPT) == 1
    assert cfg.claude_extra_args == before


def test_read_mode_uses_configured_max_turns() -> None:
    cfg = FoundryConfig.from_env(
        environ={"FOUNDRY_BUILDER_TOOLS": "read", "FOUNDRY_BUILDER_MAX_TURNS": "12"}
    )
    assert _value_after(builder_claude_args(cfg), "--max-turns") == "12"


def test_read_mode_appends_max_turns_when_absent() -> None:
    cfg = FoundryConfig.from_env(
        environ={
            "FOUNDRY_BUILDER_TOOLS": "read",
            "FOUNDRY_CLAUDE_ARGS_REPLACE": "1",
            "FOUNDRY_CLAUDE_ARGS": "--model sonnet",
        }
    )
    args = builder_claude_args(cfg)
    assert "--max-turns" not in claude_args(cfg)
    assert _value_after(args, "--max-turns") == "30"
    assert _value_after(args, "--model") == "sonnet"


def test_read_mode_setting_beats_a_repeated_max_turns() -> None:
    cfg = FoundryConfig.from_env(
        environ={"FOUNDRY_BUILDER_TOOLS": "read", "FOUNDRY_CLAUDE_ARGS": "--max-turns 5"}
    )
    before = list(cfg.claude_extra_args)
    args = builder_claude_args(cfg)
    assert args.count("--max-turns") == 1
    assert _value_after(args, "--max-turns") == "30"
    assert cfg.claude_extra_args == before
