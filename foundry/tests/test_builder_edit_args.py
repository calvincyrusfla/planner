"""Tests for builder_claude_args: the flags builders get with FOUNDRY_BUILDER_MODE=edit."""

from __future__ import annotations

from foundry.config import (
    DEFAULT_BUILDER_EDIT_PROMPT,
    DEFAULT_BUILDER_READ_PROMPT,
    FoundryConfig,
)
from foundry.main import builder_claude_args, claude_args, reviewer_claude_args

EDIT_ENV = {"FOUNDRY_BUILDER_MODE": "edit"}


def _value_after(args: list[str], flag: str) -> str:
    return args[args.index(flag) + 1]


def test_default_mode_is_diff_and_builder_args_are_unchanged() -> None:
    cfg = FoundryConfig.from_env(environ={})
    assert cfg.builder_mode == "diff"
    assert builder_claude_args(cfg) == claude_args(cfg)


def test_diff_mode_keeps_the_read_flags() -> None:
    cfg = FoundryConfig.from_env(environ={"FOUNDRY_BUILDER_TOOLS": "read"})
    assert cfg.builder_mode == "diff"
    assert builder_claude_args(cfg) == [
        *cfg.claude_extra_args[:1],
        "30",
        *cfg.claude_extra_args[2:],
        "--append-system-prompt",
        DEFAULT_BUILDER_READ_PROMPT,
        "--allowedTools",
        "Read Grep Glob",
    ]


def test_edit_mode_flags() -> None:
    cfg = FoundryConfig.from_env(environ={**EDIT_ENV, "FOUNDRY_BUILDER_SESSIONS": "1"})
    before = list(cfg.claude_extra_args)
    args = builder_claude_args(cfg)
    assert _value_after(args, "--disallowedTools").split() == [
        "Agent", "Bash", "NotebookEdit", "Task", "WebFetch", "WebSearch",
    ]
    assert args.count("--permission-mode") == 1
    assert _value_after(args, "--permission-mode") == "acceptEdits"
    assert _value_after(args, "--allowedTools") == "Read Grep Glob Edit Write"
    assert args.count("--max-turns") == 1
    assert _value_after(args, "--max-turns") == "60"
    assert "--strict-mcp-config" in args
    assert args.count("--append-system-prompt") == 1
    assert _value_after(args, "--append-system-prompt") == DEFAULT_BUILDER_EDIT_PROMPT
    assert args.count(DEFAULT_BUILDER_EDIT_PROMPT) == 1
    assert cfg.claude_extra_args == before


def test_edit_mode_uses_configured_max_turns() -> None:
    cfg = FoundryConfig.from_env(
        environ={**EDIT_ENV, "FOUNDRY_BUILDER_EDIT_MAX_TURNS": "45", "FOUNDRY_BUILDER_SESSIONS": "1"}
    )
    assert _value_after(builder_claude_args(cfg), "--max-turns") == "45"


def test_edit_mode_with_sessions_uses_session_turns() -> None:
    cfg = FoundryConfig.from_env(environ={**EDIT_ENV, "FOUNDRY_BUILDER_SESSION_TURNS": "12"})
    assert cfg.builder_sessions == 3
    assert _value_after(builder_claude_args(cfg), "--max-turns") == "12"


def test_edit_mode_env_is_case_insensitive() -> None:
    cfg = FoundryConfig.from_env(environ={"FOUNDRY_BUILDER_MODE": "EDIT"})
    assert cfg.builder_mode == "edit"


def test_edit_mode_wins_over_builder_tools() -> None:
    cfg = FoundryConfig.from_env(environ={**EDIT_ENV, "FOUNDRY_BUILDER_TOOLS": "read"})
    args = builder_claude_args(cfg)
    assert _value_after(args, "--allowedTools") == "Read Grep Glob Edit Write"
    assert args.count("--allowedTools") == 1
    assert DEFAULT_BUILDER_READ_PROMPT not in args


def test_edit_mode_cleans_every_disallowed_list_and_keeps_one_permission_mode() -> None:
    cfg = FoundryConfig.from_env(
        environ={
            **EDIT_ENV,
            "FOUNDRY_CLAUDE_ARGS": '--disallowedTools "Bash Edit" --permission-mode default',
        }
    )
    before = list(cfg.claude_extra_args)
    args = builder_claude_args(cfg)
    lists = [args[i + 1].split() for i, arg in enumerate(args) if arg == "--disallowedTools"]
    assert len(lists) == 2
    assert all("Bash" in tools and "Edit" not in tools and "Write" not in tools for tools in lists)
    assert args.count("--permission-mode") == 1
    assert _value_after(args, "--permission-mode") == "acceptEdits"
    assert cfg.claude_extra_args == before


def test_edit_mode_does_not_touch_reviewer_args() -> None:
    cfg = FoundryConfig.from_env(environ=EDIT_ENV)
    assert reviewer_claude_args(cfg) == claude_args(cfg)
    assert reviewer_claude_args(cfg) == reviewer_claude_args(FoundryConfig.from_env(environ={}))
