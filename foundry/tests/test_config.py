"""Tests for foundry.config."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from foundry.config import DEFAULT_DB_PATH, AnomalyThresholds, FoundryConfig


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
