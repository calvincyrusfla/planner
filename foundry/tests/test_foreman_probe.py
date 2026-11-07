"""Tests for foundry.foreman_probe and the setup_foreman scripts.

No real Ollama is ever contacted: a local fake HTTP server stands in for it,
and the shell script runs in dry-run mode with a fake ``ollama`` on PATH.
"""

from __future__ import annotations

import json
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from foundry import foreman_probe
from foundry.foreman_probe import ProbeResult, main, model_matches, normalize_base_url, offline_message, probe

FOUNDRY_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = FOUNDRY_DIR.parent
SH_SCRIPT = FOUNDRY_DIR / "setup_foreman.sh"
BAT_SCRIPT = FOUNDRY_DIR / "setup_foreman.bat"


# --------------------------------------------------------------------------- helpers


@contextmanager
def fake_ollama(models: list[str] | None = None, *, status: int = 200, body: bytes | None = None) -> Iterator[str]:
    """Serve a fake Ollama on 127.0.0.1; yields the server root URL."""
    payload = body if body is not None else json.dumps(
        {"object": "list", "data": [{"id": m, "object": "model", "owned_by": "library"} for m in models or []]}
    ).encode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server API
            if self.path == "/api/version":
                data, code = b'{"version":"0.0.0-fake"}', 200
            elif self.path == "/v1/models":
                data, code = payload, status
            else:
                data, code = b"not found", 404
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def free_port() -> int:
    """A port with nothing listening on it (bound then released)."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# --------------------------------------------------------------------------- probe()


def test_normalize_base_url() -> None:
    assert normalize_base_url("http://localhost:11434") == "http://localhost:11434/v1"
    assert normalize_base_url("http://localhost:11434/") == "http://localhost:11434/v1"
    assert normalize_base_url("http://localhost:11434/v1/") == "http://localhost:11434/v1"
    assert normalize_base_url("localhost:11434") == "http://localhost:11434/v1"


@pytest.mark.parametrize(
    ("wanted", "available", "expected"),
    [
        ("gemma4:e4b", "gemma4:e4b", True),
        ("gemma4", "gemma4:latest", True),
        ("gemma4:latest", "gemma4", True),
        ("GEMMA4:E4B", "gemma4:e4b", True),
        ("gemma4:e4b", "gemma4:e2b", False),
        ("gemma4:e4b", "gemma4:latest", False),
        ("gemma4", "gemma3:latest", False),
        ("registry.local:5000/gemma4", "registry.local:5000/gemma4:latest", True),
    ],
)
def test_model_matches(wanted: str, available: str, expected: bool) -> None:
    assert model_matches(wanted, available) is expected


def test_probe_ok_exact_tag() -> None:
    with fake_ollama(["llama3:8b", "gemma4:e4b"]) as root:
        result = probe(root + "/v1", "gemma4:e4b", timeout=2)
    assert isinstance(result, ProbeResult)
    assert result.ok is True
    assert result.models == ["llama3:8b", "gemma4:e4b"]
    assert "ready" in result.message
    assert result.base_url == root + "/v1"


def test_probe_ok_latest_tag_and_root_url() -> None:
    with fake_ollama(["gemma4:latest"]) as root:
        result = probe(root, "gemma4", timeout=2)  # server root, no /v1
    assert result.ok is True
    assert result.models == ["gemma4:latest"]


def test_probe_accepts_native_ollama_tag_list() -> None:
    body = json.dumps({"models": [{"name": "gemma4:e4b", "model": "gemma4:e4b"}]}).encode()
    with fake_ollama(body=body) as root:
        assert probe(root, "gemma4:e4b", timeout=2).ok is True


def test_probe_model_missing() -> None:
    with fake_ollama(["gemma4:e2b"]) as root:
        result = probe(root + "/v1", "gemma4:e4b", timeout=2)
    assert result.ok is False
    assert result.models == ["gemma4:e2b"]
    assert "gemma4:e4b is not available" in result.message
    assert "ollama pull gemma4:e4b" in result.message
    assert "gemma4:e2b" in result.message


def test_probe_nothing_listening_is_graceful_and_fast() -> None:
    port = free_port()
    url = f"http://127.0.0.1:{port}/v1"
    start = time.monotonic()
    result = probe(url, "gemma4:e4b", timeout=1)
    assert time.monotonic() - start < 3
    assert result.ok is False
    assert result.models == []
    assert result.message == (
        f"Gemma Shop Foreman is not reachable at {url}. Run ./foundry/setup_foreman.sh (macOS/Linux) "
        "or foundry\\setup_foreman.bat (Windows) to start Gemma 4 before launching the orchestrator."
    )
    assert result.message == offline_message(url)


def test_probe_silent_server_times_out_gracefully() -> None:
    """A port that accepts connections but never answers must not hang the probe."""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        url = f"http://127.0.0.1:{listener.getsockname()[1]}"
        start = time.monotonic()
        result = probe(url, "gemma4:e4b", timeout=0.5)
        elapsed = time.monotonic() - start
    assert result.ok is False
    assert "not reachable" in result.message
    assert elapsed < 3


def test_probe_http_error_and_bad_json() -> None:
    with fake_ollama(["gemma4:e4b"], status=500) as root:
        result = probe(root, "gemma4:e4b", timeout=2)
    assert result.ok is False and "HTTP 500" in result.message
    with fake_ollama(body=b"<html>not json</html>") as root:
        result = probe(root, "gemma4:e4b", timeout=2)
    assert result.ok is False and "not reachable" in result.message


def test_probe_ignores_http_proxy_for_localhost(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    with fake_ollama(["gemma4:e4b"]) as root:
        assert probe(root, "gemma4:e4b", timeout=2).ok is True


def test_probe_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(url: str, timeout: float) -> object:
        raise RuntimeError("unexpected")

    monkeypatch.setattr(foreman_probe, "_fetch_json", boom)
    result = probe("http://127.0.0.1:1", "gemma4:e4b")
    assert result.ok is False and "RuntimeError" in result.message


# --------------------------------------------------------------------------- CLI


def test_cli_exit_codes(capsys: pytest.CaptureFixture[str]) -> None:
    with fake_ollama(["gemma4:e4b"]) as root:
        assert main(["--base-url", root, "--model", "gemma4:e4b"]) == 0
        assert "ready" in capsys.readouterr().out
        assert main(["--base-url", root, "--model", "gemma4:e2b"]) == 2
        assert "ollama pull gemma4:e2b" in capsys.readouterr().err
    assert main(["--base-url", f"http://127.0.0.1:{free_port()}", "--timeout", "1"]) == 2
    assert "foundry\\setup_foreman.bat (Windows)" in capsys.readouterr().err


def test_cli_json_and_env_defaults(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.delenv("FOUNDRY_GEMMA_MODEL", raising=False)
    monkeypatch.setenv("FOREMAN_MODEL", "gemma4:e2b")
    with fake_ollama(["gemma4:e2b"]) as root:
        monkeypatch.setenv("FOUNDRY_GEMMA_BASE_URL", root + "/v1")
        assert main(["--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is True and data["model"] == "gemma4:e2b" and data["models"] == ["gemma4:e2b"]


def test_cli_module_subprocess_exit_2() -> None:
    url = f"http://127.0.0.1:{free_port()}/v1"
    proc = subprocess.run(
        [sys.executable, "-m", "foundry.foreman_probe", "--base-url", url, "--timeout", "1"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 2
    assert proc.stderr.strip() == offline_message(url)


# --------------------------------------------------------------------------- setup_foreman.sh

needs_bash = pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")


def _fake_bin(tmp_path: Path, *, ollama: bool = True, vram_mb: int | None = None) -> tuple[Path, Path]:
    """Create fake ``ollama`` / ``nvidia-smi`` executables; returns (bin dir, ollama marker)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    marker = tmp_path / "ollama-was-called"
    if ollama:
        script = bin_dir / "ollama"
        script.write_text(f'#!/bin/sh\necho "$@" >> "{marker}"\nexit 0\n')
        script.chmod(0o755)
    if vram_mb is not None:
        smi = bin_dir / "nvidia-smi"
        smi.write_text(f'#!/bin/sh\necho "NVIDIA GeForce Fake, {vram_mb}"\n')
        smi.chmod(0o755)
    return bin_dir, marker


def _run_sh(tmp_path: Path, bin_dir: Path, **env: str) -> subprocess.CompletedProcess[str]:
    full_env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "TMPDIR": str(tmp_path),
        "FOREMAN_DRY_RUN": "1",
        "FOREMAN_OLLAMA_URL": f"http://127.0.0.1:{free_port()}",
        **env,
    }
    return subprocess.run(
        ["bash", str(SH_SCRIPT)], cwd=tmp_path, env=full_env, capture_output=True, text=True, timeout=60
    )


@needs_bash
def test_setup_sh_syntax() -> None:
    proc = subprocess.run(["bash", "-n", str(SH_SCRIPT)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


@needs_bash
def test_setup_sh_dry_run_plans_without_running_ollama(tmp_path: Path) -> None:
    bin_dir, marker = _fake_bin(tmp_path, vram_mb=6144)
    proc = _run_sh(tmp_path, bin_dir)
    out = proc.stdout
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert not marker.exists(), "dry run must not execute ollama"
    assert "GPU: NVIDIA GeForce Fake (6144 MB VRAM)" in out
    assert "consider FOREMAN_MODEL=gemma4:e2b" in out
    assert "[dry-run] nohup ollama serve" in out
    assert "[dry-run] ollama pull gemma4:e4b" in out
    assert re.search(r"\[dry-run\] \S*python\S* -m foundry\.foreman_probe --base-url http://127\.0\.0\.1:\d+/v1 --model gemma4:e4b", out)
    assert out.strip().splitlines()[-1].endswith('-m foundry.main --real --repo <path> --title "..."')


@needs_bash
def test_setup_sh_dry_run_custom_model_server_up(tmp_path: Path) -> None:
    bin_dir, marker = _fake_bin(tmp_path, vram_mb=24576)
    with fake_ollama(["gemma4:e2b"]) as root:
        proc = _run_sh(tmp_path, bin_dir, FOREMAN_MODEL="gemma4:e2b", FOREMAN_OLLAMA_URL=root + "/v1/")
    out = proc.stdout
    assert proc.returncode == 0, out + proc.stderr
    assert not marker.exists()
    assert f"Ollama API already answering at {root}" in out
    assert "ollama serve" not in out
    assert "consider FOREMAN_MODEL" not in out
    assert "[dry-run] ollama pull gemma4:e2b" in out
    assert f"--base-url {root}/v1 --model gemma4:e2b" in out


@needs_bash
def test_setup_sh_without_ollama_prints_download_link(tmp_path: Path) -> None:
    bin_dir, _ = _fake_bin(tmp_path, ollama=False)
    proc = _run_sh(tmp_path, bin_dir)
    assert proc.returncode == 1
    assert "https://ollama.com/download" in proc.stdout


# --------------------------------------------------------------------------- setup_foreman.bat (static)


def test_setup_bat_structure() -> None:
    raw = BAT_SCRIPT.read_bytes()
    assert b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b""), "the .bat must use CRLF line endings"
    text = raw.decode("ascii")  # plain ASCII keeps cmd.exe code pages happy
    labels = {m.lower() for m in re.findall(r"^:(\w+)", text, flags=re.M)}
    targets = {m.lower() for m in re.findall(r"\b(?:goto|call)\s+:(\w+)", text, flags=re.I)}
    targets.discard("eof")
    assert targets <= labels, f"undefined labels: {sorted(targets - labels)}"
    for needle in (
        "where ollama",
        "https://ollama.com/download",
        "nvidia-smi --query-gpu=name,memory.total",
        "Get-CimInstance Win32_VideoController | Select Name,AdapterRAM",
        'set "MODEL=gemma4:e4b"',
        "gemma4:e2b",
        "GEQ 8192",
        'start "" /B ollama serve',
        "http://localhost:11434",
        "ollama pull %MODEL%",
        "py -3",
        "-m foundry.foreman_probe",
        'if "%FOREMAN_DRY_RUN%"=="1"',
        "sys.version_info >= (3, 11)",
        "closing this window stops",
        "Ollama app",
        '.venv\\Scripts\\python -m foundry.main --real --repo ^<path^> --title "..."',
    ):
        assert needle in text, needle
    assert (REPO_ROOT / ".gitattributes").read_text().split() == ["*.bat", "text", "eol=crlf", "*.sh", "text", "eol=lf"]
    assert "wmic" not in text.lower().replace("wmic is deprecated", "")
