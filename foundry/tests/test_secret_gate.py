"""Tests for the secrets gate and its FOUNDRY_SECRET_SCAN switch.

The fake credentials are assembled at run time so that this file's own diff
passes the gate it tests.
"""

from __future__ import annotations

from pathlib import Path
from typing import get_args

import pytest

from foundry.config import FoundryConfig
from foundry.schemas import BuilderSubmission, DeterministicGateResult, GateCheck
from foundry.validation_gates import ALL_CHECKS, _gate_secrets, run_gates

_AWS_ACCESS_KEY = "AKIA" + "ABCDEFGH12345678"
_LEAK_LINE = f'TOKEN = "{_AWS_ACCESS_KEY}"'

_LEAKS: list[tuple[str, str, str]] = [
    ("private key", "KEY = ", "-----BEGIN RSA " + "PRIVATE KEY-----"),
    ("AWS access key", "TOKEN = ", _AWS_ACCESS_KEY),
    ("AWS secret key", "aws_secret_access_key = ", "abcd" * 10),
    ("GitHub token", "TOKEN = ", "ghp_" + "a" * 36),
    ("Slack token", "TOKEN = ", "xoxb-" + "1234567890-abc"),
    ("Google API key", "TOKEN = ", "AIza" + "b" * 35),
    ("Anthropic API key", "TOKEN = ", "sk-ant-" + "c" * 24),
    ("OpenAI API key", "TOKEN = ", "sk-" + "d" * 24),
]

_PLACEHOLDERS: list[str] = [
    'API_KEY = os.environ["ANTHROPIC_API_KEY"]',
    'aws_secret_access_key = os.environ["AWS_SECRET_ACCESS_KEY"]',
    'aws_secret_access_key = "<your-secret-key>"',
    'OPENAI_API_KEY = "sk-your-api-key-here"',
    'ANTHROPIC_API_KEY = "sk-ant-..."',
    'GITHUB_TOKEN = "ghp_xxxxxxxx"',
    'SLACK_TOKEN = "xoxb-xxx"',
    'AWS_ACCESS_KEY_ID = "AKIAXXXXXXXX"',
    'GOOGLE_API_KEY = "AIza-placeholder"',
    'PRIVATE_KEY_PATH = "path/to/key.pem"',
]

_SCAN_VALUES = ["0", "false", "no", "off", " off ", "OFF", "1", "true", "on", "maybe", ""]


@pytest.fixture(autouse=True)
def _scan_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test from the default: the variable is not set."""
    monkeypatch.delenv("FOUNDRY_SECRET_SCAN", raising=False)


def _diff(*added: str) -> str:
    """A unified diff creating app.py with ``added`` as its lines."""
    body = "".join(f"+{line}\n" for line in added)
    return f"--- /dev/null\n+++ b/app.py\n@@ -0,0 +1,{len(added)} @@\n{body}"


def _run(workspace: Path, diff: str) -> list[DeterministicGateResult]:
    """Run syntax, secrets and linter on a harmless app.py with ``diff`` submitted."""
    (workspace / "app.py").write_text("SAFE = 1\n", encoding="utf-8")
    submission = BuilderSubmission(
        task_id="F-1",
        subtask_id="F-1.1",
        builder_id="bravo-2",
        status="ready_for_review",
        modified_files=["app.py"],
        diff=diff,
    )
    return run_gates(submission, workspace, checks=("syntax", "secrets", "linter"))


def test_secrets_is_a_registered_gate() -> None:
    assert "secrets" in get_args(GateCheck)
    assert "secrets" in ALL_CHECKS
    result = DeterministicGateResult(
        task_id="F-1", subtask_id="F-1.1", passed=True, check_type="secrets"
    )
    assert result.check_type == "secrets"


@pytest.mark.parametrize(("kind", "prefix", "secret"), _LEAKS, ids=[leak[0] for leak in _LEAKS])
def test_gate_fails_on_a_leak_and_masks_it(kind: str, prefix: str, secret: str) -> None:
    passed, out, err = _gate_secrets(_diff(f'{prefix}"{secret}"'))
    assert passed is False
    assert out == ""
    assert f"app.py:1: {kind}:" in err
    assert "****" in err
    assert secret not in err


@pytest.mark.parametrize("line", _PLACEHOLDERS)
def test_gate_passes_placeholders(line: str) -> None:
    passed, out, err = _gate_secrets(_diff(line))
    assert passed is True
    assert "no secrets" in out
    assert err == ""


def test_gate_reports_the_new_file_line() -> None:
    diff = f"--- a/app.py\n+++ b/app.py\n@@ -10,2 +10,3 @@\n first\n+{_LEAK_LINE}\n second\n"
    passed, _, err = _gate_secrets(diff)
    assert passed is False
    assert "app.py:11: AWS access key:" in err


def test_gate_ignores_removed_and_context_lines() -> None:
    diff = (
        "--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,2 @@\n"
        f" {_LEAK_LINE}\n-{_LEAK_LINE}\n+SAFE = 1\n"
    )
    passed, _, err = _gate_secrets(diff)
    assert passed is True
    assert err == ""


def test_run_gates_fails_on_a_leak_and_short_circuits(tmp_path: Path) -> None:
    results = _run(tmp_path, _diff(_LEAK_LINE))
    assert [r.check_type for r in results] == ["syntax", "secrets"]
    assert results[0].passed is True
    assert results[1].passed is False
    assert _AWS_ACCESS_KEY not in results[1].stderr


def test_run_gates_continues_past_a_clean_diff(tmp_path: Path) -> None:
    results = _run(tmp_path, _diff("SAFE = 1"))
    assert [r.check_type for r in results] == ["syntax", "secrets", "linter"]
    assert results[1].passed is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", " OFF "])
def test_scan_can_be_switched_off(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("FOUNDRY_SECRET_SCAN", value)
    passed, out, err = _gate_secrets(_diff(_LEAK_LINE))
    assert passed is True
    assert "skipped" in out
    assert err == ""


def test_switched_off_scan_is_noted_on_the_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("FOUNDRY_SECRET_SCAN", "off")
    secrets = _run(tmp_path, _diff(_LEAK_LINE))[1]
    assert secrets.passed is True
    assert secrets.note is not None
    assert "FOUNDRY_SECRET_SCAN" in secrets.note


@pytest.mark.parametrize("value", ["1", "true", "on", "maybe"])
def test_any_other_value_still_scans(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("FOUNDRY_SECRET_SCAN", value)
    passed, _, _ = _gate_secrets(_diff(_LEAK_LINE))
    assert passed is False


def test_config_scans_by_default() -> None:
    assert FoundryConfig.from_env(environ={}).secret_scan is True


def test_config_override_wins_over_the_environment() -> None:
    config = FoundryConfig.from_env(environ={"FOUNDRY_SECRET_SCAN": "off"}, secret_scan=True)
    assert config.secret_scan is True


@pytest.mark.parametrize("value", _SCAN_VALUES)
def test_config_agrees_with_the_gate(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("FOUNDRY_SECRET_SCAN", value)
    config = FoundryConfig.from_env(environ={"FOUNDRY_SECRET_SCAN": value})
    passed, _, _ = _gate_secrets(_diff(_LEAK_LINE))
    assert config.secret_scan is (not passed)
