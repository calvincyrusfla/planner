"""Checks for foundry/start_foundry.bat (static) and foundry/start_foundry.sh (dry run).

Nothing here starts the real service: the .sh runs with FOUNDRY_DRY_RUN=1 from a
copy of the script inside a scratch "repo" with a fake .venv python.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

FOUNDRY_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = FOUNDRY_DIR.parent
BAT = FOUNDRY_DIR / "start_foundry.bat"
SH = FOUNDRY_DIR / "start_foundry.sh"

needs_bash = pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")


# --------------------------------------------------------------------------- start_foundry.bat (static)


def test_bat_structure() -> None:
    raw = BAT.read_bytes()
    assert b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b""), "the .bat must use CRLF line endings"
    text = raw.decode("ascii")  # plain ASCII keeps cmd.exe code pages happy
    labels = {m.lower() for m in re.findall(r"^:(\w+)", text, flags=re.M)}
    targets = {m.lower() for m in re.findall(r"\b(?:goto|call)\s+:(\w+)", text, flags=re.I)}
    targets.discard("eof")
    assert targets <= labels, f"undefined labels: {sorted(targets - labels)}"
    for needle in (
        "@echo off",
        "title Foundry",
        'for %%I in ("%~dp0..") do set "REPO_ROOT=%%~fI"',
        'set "PY=%REPO_ROOT%\\.venv\\Scripts\\python.exe"',
        'set "TARGET=%REPO_ROOT%"',
        'if /i "%~1"=="--repo"',
        'if not exist "%PY%" goto :no_venv',
        'cd /d "%REPO_ROOT%"',
        '"%PY%" -m foundry.serve --repo "%TARGET%"',
        'see "Install" in foundry\\README.md',
        "py -3.11 -m venv .venv",
        ".venv\\Scripts\\python -m pip install -r foundry\\requirements.txt",
        "Ctrl+C",
        "close this window",
        'if "%FOUNDRY_DRY_RUN%"=="1" goto :dry_run',
        "pause",
    ):
        assert needle in text, needle
    # The venv check comes before anything is launched, and the window is held open afterwards.
    launch = text.index('\r\n"%PY%" -m foundry.serve')
    assert text.index('if not exist "%PY%" goto :no_venv') < launch
    assert text.index("title Foundry") < launch < text.index("\r\npause\r\n")
    # No parenthesised blocks around paths: "C:\Program Files (x86)" would close them early.
    assert not re.search(r"\(\r\n[^)]*%(?:PY|TARGET|REPO_ROOT)%", text)
    # Trailing backslash is normalised away before launch (drive roots become "C:\\.").
    norm = text.index('for %%I in ("%TARGET%\\.") do set "TARGET=%%~fI"')
    assert norm < text.index('if "%TARGET:~-1%"=="\\" set "TARGET=%TARGET%."') < launch
    assert text.index('\r\n:args_done\r\n') < norm < text.index('if not exist "%TARGET%\\" goto :no_repo')
    # Echoed paths are quoted so "&" in a folder name cannot split the line.
    for line in re.findall(r"^echo .*$", text, flags=re.M):
        for var in ("%PY%", "%TARGET%", "%REPO_ROOT%"):
            assert var not in line or f'"{var}"' in line, line
    # -h/--help exits 0 without pausing; a bad --repo exits 2.
    help_block = text[text.index("\r\n:help\r\n"):].split("\r\n\r\n")[0]
    assert 'set "RC=0"\r\ngoto :end' in help_block
    assert 'if /i "%~1"=="--help" goto :help' in text and 'if /i "%~1"=="-h" goto :help' in text
    rules = {tuple(line.split()) for line in (REPO_ROOT / ".gitattributes").read_text().splitlines() if line.strip()}
    assert ("*.bat", "text", "eol=crlf") in rules and ("*.sh", "text", "eol=lf") in rules


# --------------------------------------------------------------------------- start_foundry.sh (dry run)


def _scratch_repo(tmp_path: Path, *, venv: bool = True) -> Path:
    repo = tmp_path / "repo"
    (repo / "foundry").mkdir(parents=True)
    shutil.copy(SH, repo / "foundry" / "start_foundry.sh")
    if venv:
        py = repo / ".venv" / "bin" / "python"
        py.parent.mkdir(parents=True)
        py.write_text("#!/bin/sh\necho fake-python-ran\n")
        py.chmod(0o755)
    return repo


def _run(repo: Path, *args: str, dry: bool = True) -> subprocess.CompletedProcess[str]:
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(repo.parent)}
    if dry:
        env["FOUNDRY_DRY_RUN"] = "1"
    return subprocess.run(
        ["bash", str(repo / "foundry" / "start_foundry.sh"), *args],
        env=env, cwd=repo.parent, capture_output=True, text=True, timeout=30,
    )


def test_sh_is_lf_and_executable() -> None:
    assert b"\r" not in SH.read_bytes()
    assert SH.read_text().startswith("#!/usr/bin/env bash\n")
    assert os.access(SH, os.X_OK)


@needs_bash
def test_sh_dry_run_defaults_repo_to_repo_root(tmp_path: Path) -> None:
    repo = _scratch_repo(tmp_path).resolve()
    proc = _run(repo)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    last = proc.stdout.strip().splitlines()[-1]
    assert last == f"[dry-run] {repo}/.venv/bin/python -m foundry.serve --repo {repo}"
    assert "fake-python-ran" not in proc.stdout
    assert "Ctrl+C" in proc.stdout


@needs_bash
@pytest.mark.parametrize("flag", [True, False])
def test_sh_dry_run_custom_repo(tmp_path: Path, flag: bool) -> None:
    repo = _scratch_repo(tmp_path).resolve()
    other = (tmp_path / "other").resolve()
    other.mkdir()
    proc = _run(repo, *(["--repo", str(other)] if flag else [str(other)]))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip().endswith(f"-m foundry.serve --repo {other}")


@needs_bash
def test_sh_runs_venv_python_when_not_dry(tmp_path: Path) -> None:
    repo = _scratch_repo(tmp_path)
    proc = _run(repo, dry=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "fake-python-ran" in proc.stdout


@needs_bash
def test_sh_without_venv_points_to_install(tmp_path: Path) -> None:
    repo = _scratch_repo(tmp_path, venv=False)
    proc = _run(repo)
    assert proc.returncode == 1
    assert "No Python virtual environment found" in proc.stdout
    assert '"Install" in foundry/README.md' in proc.stdout
    assert "python3 -m venv .venv" in proc.stdout


@needs_bash
def test_sh_missing_repo_folder(tmp_path: Path) -> None:
    repo = _scratch_repo(tmp_path)
    proc = _run(repo, "--repo", str(tmp_path / "nope"))
    assert proc.returncode == 1
    assert "Repository folder not found" in proc.stdout
