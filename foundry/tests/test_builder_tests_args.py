"""Tests for FOUNDRY_BUILDER_TESTS: edit-mode builders may run the repository's tests."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest

from foundry import serve
from foundry.claude_client import ClaudeCodeClient
from foundry.config import (
    BUILDER_TEST_COMMANDS,
    DEFAULT_BUILDER_EDIT_PROMPT,
    DEFAULT_BUILDER_TESTS_PROMPT_ADDITION,
    FoundryConfig,
)
from foundry.main import builder_claude_args, claude_args

EDIT_ENV = {"FOUNDRY_BUILDER_MODE": "edit"}
TESTS_ENV = {**EDIT_ENV, "FOUNDRY_BUILDER_TESTS": "1"}


def _value_after(args: list[str], flag: str) -> str:
    return args[args.index(flag) + 1]


def test_commands_are_the_four_test_commands() -> None:
    assert BUILDER_TEST_COMMANDS == ("python -m pytest", "node --test", "npm test", "npm run test")


def test_setting_defaults_off_and_parses_like_a_bool() -> None:
    assert FoundryConfig.from_env(environ={}).builder_tests is False
    assert FoundryConfig.from_env(environ={"FOUNDRY_BUILDER_TESTS": "true"}).builder_tests is True
    assert FoundryConfig.from_env(environ={"FOUNDRY_BUILDER_TESTS": "off"}).builder_tests is False


def test_default_off_gives_the_plain_edit_flags() -> None:
    args = builder_claude_args(FoundryConfig.from_env(environ=EDIT_ENV))
    assert _value_after(args, "--disallowedTools").split() == [
        "Agent", "Bash", "NotebookEdit", "Task", "WebFetch", "WebSearch",
    ]
    assert _value_after(args, "--allowedTools") == "Read Grep Glob Edit Write"
    assert _value_after(args, "--append-system-prompt") == DEFAULT_BUILDER_EDIT_PROMPT


def test_on_in_edit_mode_unblocks_bash_for_the_test_commands_only() -> None:
    cfg = FoundryConfig.from_env(environ=TESTS_ENV)
    before = list(cfg.claude_extra_args)
    args = builder_claude_args(cfg)
    assert _value_after(args, "--disallowedTools").split() == [
        "Agent", "NotebookEdit", "Task", "WebFetch", "WebSearch",
    ]
    assert args.count("--allowedTools") == 1
    assert _value_after(args, "--allowedTools") == (
        "Read Grep Glob Edit Write Bash(python -m pytest *) Bash(node --test *) "
        "Bash(npm test *) Bash(npm run test *)"
    )
    assert cfg.claude_extra_args == before


def test_on_in_edit_mode_adds_the_prompt_addition_once() -> None:
    args = builder_claude_args(FoundryConfig.from_env(environ=TESTS_ENV))
    assert args.count("--append-system-prompt") == 1
    prompt = _value_after(args, "--append-system-prompt")
    assert prompt == DEFAULT_BUILDER_EDIT_PROMPT + " " + DEFAULT_BUILDER_TESTS_PROMPT_ADDITION
    assert prompt.count(DEFAULT_BUILDER_TESTS_PROMPT_ADDITION) == 1


def test_on_cleans_every_disallowed_list() -> None:
    cfg = FoundryConfig.from_env(
        environ={**TESTS_ENV, "FOUNDRY_CLAUDE_ARGS": '--disallowedTools "Bash WebFetch"'}
    )
    args = builder_claude_args(cfg)
    lists = [args[i + 1].split() for i, arg in enumerate(args) if arg == "--disallowedTools"]
    assert len(lists) == 2
    assert all("Bash" not in tools and "WebFetch" in tools for tools in lists)


def test_on_but_diff_mode_changes_nothing() -> None:
    on = FoundryConfig.from_env(environ={"FOUNDRY_BUILDER_TESTS": "1"})
    assert builder_claude_args(on) == claude_args(on)
    assert builder_claude_args(on) == builder_claude_args(FoundryConfig.from_env(environ={}))


def test_on_but_diff_mode_with_read_tools_changes_nothing() -> None:
    read = {"FOUNDRY_BUILDER_TOOLS": "read"}
    on = FoundryConfig.from_env(environ={**read, "FOUNDRY_BUILDER_TESTS": "1"})
    assert builder_claude_args(on) == builder_claude_args(FoundryConfig.from_env(environ=read))


def test_flags_can_launch_through_the_windows_npm_shim() -> None:
    client = ClaudeCodeClient(
        claude_bin="claude", extra_args=builder_claude_args(FoundryConfig.from_env(environ=TESTS_ENV))
    )
    client._resolved = r"C:\npm\claude.cmd"
    assert client.validate_launch(platform="win32")[0] == "verbatim"


class FakeCodeClient:
    """Stands in for ``ClaudeCodeClient``; remembers how it was created."""

    def __init__(self, path: str, *, extra_args: list[str] | None = None, **options: Any) -> None:
        self.path = path
        self.extra_args = extra_args
        self.options = options


def _serve_main(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    """Run ``serve.main`` with fake clients and no real service; returns the Service it built."""
    services: list[Any] = []

    async def fake_run_service(service: Any, **_kwargs: Any) -> int:
        services.append(service)
        return 0

    def fake_resolve(claude_bin: str) -> str:
        return claude_bin

    def fake_gemma(*_args: Any, **_kwargs: Any) -> object:
        return object()

    def no_ctrl_c() -> bool:
        return False

    monkeypatch.setattr("foundry.config.DEFAULT_SETTINGS_FILE", tmp_path / "no-settings.json")
    monkeypatch.setattr("foundry.claude_client.ClaudeCodeClient", FakeCodeClient)
    monkeypatch.setattr("foundry.claude_client.resolve_claude_bin", fake_resolve)
    monkeypatch.setattr("foundry.gemma_client.OllamaGemmaClient", fake_gemma)
    monkeypatch.setattr(serve, "run_service", fake_run_service)
    monkeypatch.setattr(serve, "enable_ctrl_c", no_ctrl_c)
    assert serve.main(["--repo", str(tmp_path)]) == 0
    assert len(services) == 1
    return services[0]


def test_serve_gives_the_builder_client_foundrys_python_first(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("FOUNDRY_BUILDER_MODE", "edit")
    monkeypatch.setenv("FOUNDRY_BUILDER_TESTS", "1")
    monkeypatch.setenv("PATH", "some-other-dir")
    service = _serve_main(monkeypatch, tmp_path)
    assert service.builder_claude.options == {
        "env": {"PATH": os.path.dirname(sys.executable) + os.pathsep + "some-other-dir"}
    }
    assert service.claude.options == {}


def test_serve_passes_no_env_in_edit_mode_with_tests_off(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("FOUNDRY_BUILDER_MODE", "edit")
    monkeypatch.delenv("FOUNDRY_BUILDER_TESTS", raising=False)
    service = _serve_main(monkeypatch, tmp_path)
    assert service.builder_claude.options == {}


def test_serve_passes_no_env_in_diff_mode_with_tests_on(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("FOUNDRY_BUILDER_MODE", "diff")
    monkeypatch.setenv("FOUNDRY_BUILDER_TOOLS", "read")
    monkeypatch.setenv("FOUNDRY_BUILDER_TESTS", "1")
    service = _serve_main(monkeypatch, tmp_path)
    assert service.builder_claude.options == {}
