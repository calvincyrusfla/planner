"""Tests for foundry.config."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from foundry.config import (
    DEFAULT_CLAUDE_APPEND_PROMPT,
    DEFAULT_CLAUDE_EXTRA_ARGS,
    DEFAULT_DB_PATH,
    DEFAULT_PATCHES_DIR,
    AnomalyThresholds,
    FoundryConfig,
    split_claude_args,
)


def test_defaults() -> None:
    cfg = FoundryConfig()
    assert cfg.db_path == DEFAULT_DB_PATH
    assert cfg.db_path.parts[-3:] == ("foundry", "data", "foundry.db")
    assert cfg.db_path.is_absolute()
    assert cfg.gemma_base_url == "http://localhost:11434/v1"
    assert cfg.gemma_model == "gemma4:e4b"
    assert cfg.claude_bin == "claude"
    assert cfg.max_concurrent == 3
    assert 1 <= cfg.rpm <= 60
    assert cfg.tpm > 0
    assert cfg.teams == ["alpha", "bravo", "charlie"]
    assert cfg.builders_per_team == 5
    assert cfg.board_bridge_mode == "auto"
    assert cfg.thresholds == AnomalyThresholds()
    assert cfg.thresholds.gate_fail_quarantine == 2
    assert cfg.thresholds.tpm_spike_ratio == 0.9


def test_from_env_empty_matches_defaults() -> None:
    assert FoundryConfig.from_env({}) == FoundryConfig()


def test_from_env_reads_everything() -> None:
    env = {
        "FOUNDRY_DB_PATH": "/tmp/x/q.db",
        "FOUNDRY_GEMMA_BASE_URL": "http://gpu-box:11434/v1/",
        "FOUNDRY_GEMMA_MODEL": "gemma4:e2b",
        "FOUNDRY_CLAUDE_BIN": "C:/tools/claude.cmd",
        "FOUNDRY_RPM": "10",
        "FOUNDRY_TPM": "50000",
        "FOUNDRY_MAX_CONCURRENT": "2",
        "FOUNDRY_REVIEW_DWELL_S": "120",
        "FOUNDRY_STALL_S": "300.5",
        "FOUNDRY_GATE_FAIL_QUARANTINE": "3",
        "FOUNDRY_TPM_SPIKE_RATIO": "0.75",
        "FOUNDRY_TEAMS": " alpha , bravo ,",
        "FOUNDRY_BUILDERS_PER_TEAM": "4",
        "FOUNDRY_BOARD_BRIDGE_MODE": "DRY",
    }
    cfg = FoundryConfig.from_env(env)
    assert cfg.db_path == Path("/tmp/x/q.db")
    assert cfg.gemma_base_url == "http://gpu-box:11434/v1"
    assert cfg.gemma_model == "gemma4:e2b"
    assert cfg.claude_bin == "C:/tools/claude.cmd"
    assert (cfg.rpm, cfg.tpm, cfg.max_concurrent) == (10, 50000, 2)
    assert cfg.thresholds.review_dwell_s == 120
    assert cfg.thresholds.stall_s == 300.5
    assert cfg.thresholds.gate_fail_quarantine == 3
    assert cfg.thresholds.tpm_spike_ratio == 0.75
    assert cfg.teams == ["alpha", "bravo"]
    assert cfg.builders_per_team == 4
    assert cfg.board_bridge_mode == "dry"


def test_from_env_uses_os_environ(clean_foundry_env: pytest.MonkeyPatch) -> None:
    clean_foundry_env.setenv("FOUNDRY_MAX_CONCURRENT", "1")
    clean_foundry_env.setenv("FOUNDRY_RPM", "  ")  # blank = unset
    cfg = FoundryConfig.from_env()
    assert cfg.max_concurrent == 1
    assert cfg.rpm == FoundryConfig().rpm


def test_foreman_model_fallback() -> None:
    assert FoundryConfig.from_env({"FOREMAN_MODEL": "gemma4:e2b"}).gemma_model == "gemma4:e2b"
    env = {"FOREMAN_MODEL": "gemma4:e2b", "FOUNDRY_GEMMA_MODEL": "gemma4:e4b"}
    assert FoundryConfig.from_env(env).gemma_model == "gemma4:e4b"


def test_overrides_win_over_env() -> None:
    cfg = FoundryConfig.from_env({"FOUNDRY_RPM": "10"}, rpm=5, board_bridge_mode="off")
    assert cfg.rpm == 5
    assert cfg.board_bridge_mode == "off"


@pytest.mark.parametrize(
    ("var", "value"),
    [
        ("FOUNDRY_RPM", "abc"),
        ("FOUNDRY_RPM", "0"),
        ("FOUNDRY_MAX_CONCURRENT", "0"),
        ("FOUNDRY_BUILDERS_PER_TEAM", "6"),
        ("FOUNDRY_BOARD_BRIDGE_MODE", "loud"),
        ("FOUNDRY_GEMMA_BASE_URL", "localhost:11434"),
        ("FOUNDRY_TPM_SPIKE_RATIO", "1.5"),
        ("FOUNDRY_GATE_FAIL_QUARANTINE", "0"),
        ("FOUNDRY_TEAMS", " , "),
    ],
)
def test_from_env_rejects_bad_values(var: str, value: str) -> None:
    with pytest.raises(ValidationError):
        FoundryConfig.from_env({var: value})


def test_extra_fields_forbidden() -> None:
    with pytest.raises(ValidationError):
        FoundryConfig(unknown=1)  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        AnomalyThresholds(nope=1)  # type: ignore[call-arg]


def test_duplicate_teams_rejected() -> None:
    with pytest.raises(ValidationError):
        FoundryConfig(teams=["alpha", "alpha"])


def test_validate_assignment() -> None:
    cfg = FoundryConfig()
    with pytest.raises(ValidationError):
        cfg.max_concurrent = 0


def test_agent_ids_and_data_dir(tmp_path: Path) -> None:
    cfg = FoundryConfig(db_path=tmp_path / "nested" / "dir" / "f.db", builders_per_team=3)
    assert cfg.builder_ids("bravo") == ["bravo-1", "bravo-2", "bravo-3"]
    assert cfg.reviewer_id("bravo") == "bravo-6"
    made = cfg.ensure_data_dir()
    assert made == tmp_path / "nested" / "dir"
    assert made.is_dir()


def test_conftest_helpers(fake_clock, db_path: Path) -> None:  # type: ignore[no-untyped-def]
    import asyncio

    start_mono, start_now = fake_clock.monotonic(), fake_clock.now()
    assert start_now.tzinfo is not None
    asyncio.run(fake_clock.sleep(2.5))
    assert fake_clock.monotonic() - start_mono == 2.5
    assert (fake_clock.now() - start_now).total_seconds() == 2.5
    assert fake_clock.sleeps == [2.5]
    assert db_path.name == "foundry.db" and not db_path.exists()


def test_round1_defaults() -> None:
    cfg = FoundryConfig()
    assert cfg.trust_cache is False
    assert cfg.limit_cooldown_s == 1200
    assert cfg.claude_extra_args == [
        "--max-turns",
        "3",
        "--disallowedTools",
        "Agent Bash Edit Write NotebookEdit Task WebFetch WebSearch",
        "--strict-mcp-config",
    ]
    assert tuple(cfg.claude_extra_args) == DEFAULT_CLAUDE_EXTRA_ARGS
    assert cfg.claude_append_prompt == DEFAULT_CLAUDE_APPEND_PROMPT
    assert "spawn agents" in DEFAULT_CLAUDE_APPEND_PROMPT
    assert cfg.patches_dir is None
    assert cfg.resolved_patches_dir() == DEFAULT_PATCHES_DIR
    assert cfg.resolved_patches_dir().parts[-3:] == ("foundry", "data", "patches")
    # default list is not shared between instances
    cfg.claude_extra_args.append("--x")
    assert FoundryConfig().claude_extra_args == list(DEFAULT_CLAUDE_EXTRA_ARGS)


def test_claude_cli_args() -> None:
    cfg = FoundryConfig()
    args = cfg.claude_cli_args()
    n = len(DEFAULT_CLAUDE_EXTRA_ARGS)
    assert args[:n] == list(DEFAULT_CLAUDE_EXTRA_ARGS)
    assert args[n:] == ["--append-system-prompt", DEFAULT_CLAUDE_APPEND_PROMPT]
    bare = FoundryConfig(claude_extra_args=[], claude_append_prompt="  ")
    assert bare.claude_append_prompt is None
    assert bare.claude_cli_args() == []


@pytest.mark.parametrize(("raw", "expected"), [("1", True), ("true", True), ("yes", True),
                                               ("on", True), ("0", False), ("off", False)])
def test_trust_cache_env(raw: str, expected: bool) -> None:
    assert FoundryConfig.from_env({"FOUNDRY_TRUST_CACHE": raw}).trust_cache is expected


def test_round1_env_vars(tmp_path: Path) -> None:
    env = {
        "FOUNDRY_LIMIT_COOLDOWN_S": "90",
        "FOUNDRY_CLAUDE_ARGS": '--max-turns 5 --disallowedTools "Bash Edit"',
        "FOUNDRY_CLAUDE_APPEND_PROMPT": "Only output a diff.",
        "FOUNDRY_PATCHES_DIR": str(tmp_path / "p"),
    }
    cfg = FoundryConfig.from_env(env)
    assert cfg.limit_cooldown_s == 90
    assert cfg.claude_extra_args == [
        *DEFAULT_CLAUDE_EXTRA_ARGS, "--max-turns", "5", "--disallowedTools", "Bash Edit"
    ]
    assert cfg.claude_append_prompt == "Only output a diff."
    assert cfg.patches_dir == tmp_path / "p"
    assert cfg.resolved_patches_dir() == tmp_path / "p"
    assert cfg.ensure_patches_dir() == tmp_path / "p"
    assert (tmp_path / "p").is_dir()


def test_none_word() -> None:
    # without REPLACE, "none" appends nothing: the safe defaults stay
    cfg = FoundryConfig.from_env(
        {"FOUNDRY_CLAUDE_ARGS": "none", "FOUNDRY_CLAUDE_APPEND_PROMPT": "NONE"}
    )
    assert cfg.claude_extra_args == list(DEFAULT_CLAUDE_EXTRA_ARGS)
    assert cfg.claude_append_prompt is None
    assert cfg.claude_cli_args() == list(DEFAULT_CLAUDE_EXTRA_ARGS)
    # with REPLACE, "none" (or unset) clears every extra flag
    for env in ({"FOUNDRY_CLAUDE_ARGS": "none"}, {}):
        env = {**env, "FOUNDRY_CLAUDE_ARGS_REPLACE": "1", "FOUNDRY_CLAUDE_APPEND_PROMPT": "none"}
        assert FoundryConfig.from_env(env).claude_cli_args() == []


@pytest.mark.parametrize(
    ("var", "value"),
    [
        ("FOUNDRY_TRUST_CACHE", "maybe"),
        ("FOUNDRY_LIMIT_COOLDOWN_S", "0"),
        ("FOUNDRY_LIMIT_COOLDOWN_S", "soon"),
        ("FOUNDRY_CLAUDE_ARGS", '--x "unbalanced'),
        ("FOUNDRY_CLAUDE_ARGS_REPLACE", "sometimes"),
        ("FOUNDRY_INFRA_FAILURE_CAP", "0"),
        ("FOUNDRY_INFRA_FAILURE_CAP", "three"),
    ],
)
def test_round1_bad_values(var: str, value: str) -> None:
    with pytest.raises(ValueError):
        FoundryConfig.from_env({var: value})


def test_claude_args_append_windows_path() -> None:
    # bravo-6's repro: a Windows path must survive (no backslash eating)
    cfg = FoundryConfig.from_env({"FOUNDRY_CLAUDE_ARGS": r"--mcp-config C:\Users\me\mcp.json"})
    assert cfg.claude_extra_args == [
        *DEFAULT_CLAUDE_EXTRA_ARGS, "--mcp-config", r"C:\Users\me\mcp.json"
    ]
    assert "--strict-mcp-config" in cfg.claude_cli_args()


def test_claude_args_replace() -> None:
    env = {
        "FOUNDRY_CLAUDE_ARGS": '--max-turns 8 --disallowedTools "Bash Agent"',
        "FOUNDRY_CLAUDE_ARGS_REPLACE": "true",
    }
    assert FoundryConfig.from_env(env).claude_extra_args == [
        "--max-turns", "8", "--disallowedTools", "Bash Agent"
    ]
    env["FOUNDRY_CLAUDE_ARGS_REPLACE"] = "0"  # explicit off = append
    assert FoundryConfig.from_env(env).claude_extra_args[: len(DEFAULT_CLAUDE_EXTRA_ARGS)] == list(
        DEFAULT_CLAUDE_EXTRA_ARGS
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (r"--mcp-config C:\Users\me\mcp.json", ["--mcp-config", r"C:\Users\me\mcp.json"]),
        (
            r'--mcp-config "C:\Users\John Smith\AppData\mcp.json" --verbose',
            ["--mcp-config", r"C:\Users\John Smith\AppData\mcp.json", "--verbose"],
        ),
        (r'--add-dir="D:\work dir\"', [r"--add-dir=D:\work dir" + "\\"]),
        ("--model 'claude opus'", ["--model", "claude opus"]),
        ("  --a   --b  ", ["--a", "--b"]),
        ("--tag #1", ["--tag", "#1"]),
        ("", []),
    ],
)
def test_split_claude_args(text: str, expected: list[str]) -> None:
    assert split_claude_args(text) == expected


def test_split_claude_args_unbalanced() -> None:
    with pytest.raises(ValueError):
        split_claude_args('--x "oops')


def test_infra_failure_cap() -> None:
    assert FoundryConfig().infra_failure_cap == 3
    assert FoundryConfig.from_env({"FOUNDRY_INFRA_FAILURE_CAP": "5"}).infra_failure_cap == 5
    with pytest.raises(ValidationError):
        FoundryConfig(infra_failure_cap=0)


def test_patches_dir_follows_db(tmp_path: Path) -> None:
    cfg = FoundryConfig(db_path=tmp_path / "run1" / "foundry.db")
    assert cfg.patches_dir is None
    assert cfg.resolved_patches_dir() == tmp_path / "run1" / "patches"
    # from_env: FOUNDRY_DB_PATH alone moves the patches with the db
    env = {"FOUNDRY_DB_PATH": str(tmp_path / "run2" / "q.db")}
    assert FoundryConfig.from_env(env).resolved_patches_dir() == tmp_path / "run2" / "patches"
    # FOUNDRY_PATCHES_DIR still wins
    env["FOUNDRY_PATCHES_DIR"] = str(tmp_path / "elsewhere")
    assert FoundryConfig.from_env(env).resolved_patches_dir() == tmp_path / "elsewhere"
    # assigning db_path later moves the derived folder too
    cfg.db_path = tmp_path / "run3" / "f.db"
    assert cfg.ensure_patches_dir() == tmp_path / "run3" / "patches"
    assert (tmp_path / "run3" / "patches").is_dir()
