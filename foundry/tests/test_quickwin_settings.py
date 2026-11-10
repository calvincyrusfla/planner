"""Tests for the quick-win settings: reviewer_model, auto_update, auto_merge, gemma_notes."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from foundry.config import FoundryConfig
from foundry.main import claude_args, reviewer_claude_args


def _cfg(**env: str) -> FoundryConfig:
    return FoundryConfig.from_env(environ=env)


def test_defaults_keep_todays_behaviour() -> None:
    cfg = _cfg()
    assert cfg.reviewer_model is None
    assert cfg.auto_update is False
    assert cfg.auto_merge == "off"
    assert cfg.gemma_notes is False
    assert cfg.gemma_notes_timeout_s == 90


def test_parsing() -> None:
    cfg = _cfg(
        FOUNDRY_REVIEWER_MODEL="opus",
        FOUNDRY_AUTO_UPDATE="true",
        FOUNDRY_AUTO_MERGE="TESTS_DOCS",
        FOUNDRY_GEMMA_NOTES="1",
        FOUNDRY_GEMMA_NOTES_TIMEOUT_S="12.5",
    )
    assert cfg.reviewer_model == "opus"
    assert cfg.auto_update is True
    assert cfg.auto_merge == "tests_docs"
    assert cfg.gemma_notes is True
    assert cfg.gemma_notes_timeout_s == 12.5


def test_blank_reviewer_model_is_unset() -> None:
    assert _cfg(FOUNDRY_REVIEWER_MODEL="  ").reviewer_model is None


@pytest.mark.parametrize(
    "env",
    [
        {"FOUNDRY_REVIEWER_MODEL": "two words"},
        {"FOUNDRY_AUTO_MERGE": "all"},
        {"FOUNDRY_AUTO_UPDATE": "maybe"},
        {"FOUNDRY_GEMMA_NOTES": "maybe"},
        {"FOUNDRY_GEMMA_NOTES_TIMEOUT_S": "0"},
        {"FOUNDRY_GEMMA_NOTES_TIMEOUT_S": "abc"},
    ],
)
def test_refusals(env: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        FoundryConfig.from_env(environ=env)


def test_reviewer_model_adds_one_model_flag() -> None:
    cfg = _cfg(FOUNDRY_REVIEWER_MODEL="opus", FOUNDRY_CLAUDE_ARGS="--model sonnet")
    args = reviewer_claude_args(cfg)
    assert args.count("--model") == 1
    assert args[args.index("--model") + 1] == "opus"


def test_reviewer_model_in_read_mode() -> None:
    cfg = _cfg(FOUNDRY_REVIEWER_MODEL="opus", FOUNDRY_REVIEWER_TOOLS="read")
    args = reviewer_claude_args(cfg)
    assert args.count("--model") == 1
    assert args[args.index("--model") + 1] == "opus"
    assert args[args.index("--allowedTools") + 1] == "Read Grep Glob"


def test_unset_reviewer_model_changes_nothing() -> None:
    cfg = _cfg()
    assert reviewer_claude_args(cfg) == claude_args(cfg)
