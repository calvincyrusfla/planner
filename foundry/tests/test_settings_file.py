"""FoundryConfig.from_env reads FOUNDRY_* values from a JSON settings file."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

import foundry.config as config_mod
from foundry.config import DEFAULT_DATA_DIR, DEFAULT_SETTINGS_FILE, FoundryConfig


def _write(path: Path, data: Any) -> Path:
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
    return path


def test_default_settings_file_location() -> None:
    assert DEFAULT_SETTINGS_FILE == DEFAULT_DATA_DIR / "settings.json"


def test_file_values_override_environment(clean_foundry_env: pytest.MonkeyPatch, tmp_path: Path) -> None:
    clean_foundry_env.setenv("FOUNDRY_RPM", "10")
    clean_foundry_env.setenv("FOUNDRY_TPM", "50000")
    f = _write(tmp_path / "settings.json", {"FOUNDRY_RPM": "7", "FOUNDRY_BUILDER_TOOLS": "read"})
    cfg = FoundryConfig.from_env(settings_file=f)
    assert cfg.rpm == 7
    assert cfg.tpm == 50000
    assert cfg.builder_tools == "read"


def test_missing_file_changes_nothing(clean_foundry_env: pytest.MonkeyPatch, tmp_path: Path) -> None:
    clean_foundry_env.setenv("FOUNDRY_RPM", "10")
    cfg = FoundryConfig.from_env(settings_file=tmp_path / "nope.json")
    assert cfg == FoundryConfig(rpm=10)


@pytest.mark.parametrize("text", ["{not json", "[1, 2]", ""])
def test_invalid_file_is_ignored_with_warning(
    clean_foundry_env: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture, text: str
) -> None:
    f = _write(tmp_path / "settings.json", text)
    with caplog.at_level(logging.WARNING, logger="foundry.config"):
        cfg = FoundryConfig.from_env(settings_file=f)
    assert cfg == FoundryConfig()
    assert any("ignored" in r.getMessage() for r in caplog.records)


def test_non_foundry_keys_and_non_strings_ignored(
    clean_foundry_env: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    f = _write(tmp_path / "settings.json", {
        "FOUNDRY_RPM": "5", "PATH": "/evil", "FOREMAN_MODEL": "x:1", "FOUNDRY_TPM": 123,
    })
    with caplog.at_level(logging.WARNING, logger="foundry.config"):
        cfg = FoundryConfig.from_env(settings_file=f)
    assert cfg.rpm == 5
    assert cfg.tpm == FoundryConfig().tpm
    assert cfg.gemma_model == FoundryConfig().gemma_model
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "PATH" in warnings[0].getMessage()


def test_invalid_value_in_file_fails_like_env(clean_foundry_env: pytest.MonkeyPatch, tmp_path: Path) -> None:
    f = _write(tmp_path / "settings.json", {"FOUNDRY_BUILDER_TOOLS": "bogus"})
    with pytest.raises(ValidationError):
        FoundryConfig.from_env(settings_file=f)
    with pytest.raises(ValidationError):
        FoundryConfig.from_env({"FOUNDRY_BUILDER_TOOLS": "bogus"})


def test_environ_given_with_settings_file_reads_it(tmp_path: Path) -> None:
    f = _write(tmp_path / "settings.json", {"FOUNDRY_RPM": "4"})
    cfg = FoundryConfig.from_env({"FOUNDRY_RPM": "9", "FOUNDRY_TPM": "500"}, settings_file=f)
    assert (cfg.rpm, cfg.tpm) == (4, 500)


def test_environ_given_without_settings_file_skips_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    f = _write(tmp_path / "settings.json", {"FOUNDRY_BUILDER_TOOLS": "bogus", "FOUNDRY_RPM": "4"})
    monkeypatch.setattr(config_mod, "DEFAULT_SETTINGS_FILE", f)
    assert FoundryConfig.from_env({}) == FoundryConfig()


def test_environ_none_reads_default_file(clean_foundry_env: pytest.MonkeyPatch, tmp_path: Path) -> None:
    f = _write(tmp_path / "settings.json", {"FOUNDRY_RPM": "6"})
    clean_foundry_env.setattr(config_mod, "DEFAULT_SETTINGS_FILE", f)
    assert FoundryConfig.from_env().rpm == 6
