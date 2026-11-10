"""Tests for FOUNDRY_BUILDER_MODEL and FOUNDRY_BUILDER_RETRY_MODEL and the flags they set."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from foundry.config import (
    DEFAULT_BUILDER_EDIT_PROMPT,
    DEFAULT_BUILDER_TESTS_PROMPT_ADDITION,
    FoundryConfig,
)
from foundry.main import (
    builder_claude_args,
    builder_retry_claude_args,
    claude_args,
    reviewer_claude_args,
)

MODEL_ENV = {"FOUNDRY_BUILDER_MODEL": "sonnet"}
RETRY_ENV = {"FOUNDRY_BUILDER_RETRY_MODEL": "opus"}
TESTS_ENV = {"FOUNDRY_BUILDER_MODE": "edit", "FOUNDRY_BUILDER_TESTS": "1"}


def _value_after(args: list[str], flag: str) -> str:
    return args[args.index(flag) + 1]


def test_defaults_give_no_model_and_equal_retry_args() -> None:
    cfg = FoundryConfig.from_env(environ={})
    assert cfg.builder_model is None
    assert cfg.builder_retry_model is None
    assert "--model" not in builder_claude_args(cfg)
    assert builder_retry_claude_args(cfg) == builder_claude_args(cfg)


def test_builder_model_adds_one_model_flag() -> None:
    cfg = FoundryConfig.from_env(environ=MODEL_ENV)
    args = builder_claude_args(cfg)
    assert args.count("--model") == 1
    assert _value_after(args, "--model") == "sonnet"
    assert builder_retry_claude_args(cfg) == args


def test_builder_model_in_edit_mode_with_tests_keeps_their_flags() -> None:
    plain = builder_claude_args(FoundryConfig.from_env(environ=TESTS_ENV))
    args = builder_claude_args(FoundryConfig.from_env(environ={**TESTS_ENV, **MODEL_ENV}))
    assert args.count("--model") == 1
    assert _value_after(args, "--model") == "sonnet"
    position = args.index("--model")
    assert args[:position] + args[position + 2:] == plain
    assert _value_after(args, "--append-system-prompt") == (
        DEFAULT_BUILDER_EDIT_PROMPT + " " + DEFAULT_BUILDER_TESTS_PROMPT_ADDITION
    )


def test_builder_model_in_read_mode() -> None:
    args = builder_claude_args(
        FoundryConfig.from_env(environ={"FOUNDRY_BUILDER_TOOLS": "read", **MODEL_ENV})
    )
    assert args.count("--model") == 1
    assert _value_after(args, "--allowedTools") == "Read Grep Glob"


def test_builder_model_replaces_every_model_from_claude_args() -> None:
    cfg = FoundryConfig.from_env(
        environ={**MODEL_ENV, "FOUNDRY_CLAUDE_ARGS": "--model haiku --model opus"}
    )
    args = builder_claude_args(cfg)
    assert args.count("--model") == 1
    assert _value_after(args, "--model") == "sonnet"


def test_retry_model_changes_only_the_retry_args() -> None:
    cfg = FoundryConfig.from_env(environ=RETRY_ENV)
    assert "--model" not in builder_claude_args(cfg)
    args = builder_retry_claude_args(cfg)
    assert args.count("--model") == 1
    assert _value_after(args, "--model") == "opus"


def test_retry_model_replaces_the_builder_model() -> None:
    cfg = FoundryConfig.from_env(environ={**MODEL_ENV, **RETRY_ENV})
    assert _value_after(builder_claude_args(cfg), "--model") == "sonnet"
    args = builder_retry_claude_args(cfg)
    assert args.count("--model") == 1
    assert _value_after(args, "--model") == "opus"


def test_models_do_not_touch_other_roles_or_the_config() -> None:
    cfg = FoundryConfig.from_env(environ={**MODEL_ENV, **RETRY_ENV})
    before = list(cfg.claude_extra_args)
    builder_retry_claude_args(cfg)
    assert cfg.claude_extra_args == before
    assert "--model" not in claude_args(cfg)
    assert "--model" not in reviewer_claude_args(cfg)


@pytest.mark.parametrize("var", ["FOUNDRY_BUILDER_MODEL", "FOUNDRY_BUILDER_RETRY_MODEL"])
@pytest.mark.parametrize("value", ["claude opus", "claude\topus", 'so"nnet', "so'nnet"])
def test_bad_model_values_are_refused(var: str, value: str) -> None:
    with pytest.raises(ValidationError):
        FoundryConfig.from_env(environ={var: value})


@pytest.mark.parametrize("field", ["builder_model", "builder_retry_model"])
def test_blank_model_is_unset(field: str) -> None:
    assert getattr(FoundryConfig.model_validate({field: "  "}), field) is None
