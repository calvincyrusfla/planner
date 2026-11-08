"""Tests for foundry.gemma_doctor (pure verdicts + a fake Ollama HTTP server)."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from foundry import gemma_doctor as gd
from foundry.config import FoundryConfig

REPO_ROOT = Path(__file__).resolve().parents[2]

GB = 1024**3


def ps_payload(name: str, size: int, vram: int) -> dict[str, Any]:
    return {"models": [{"name": name, "model": name, "size": size, "size_vram": vram}]}


def gen_payload(count: int, seconds: float) -> dict[str, Any]:
    return {"response": "OK", "done": True, "eval_count": count, "eval_duration": int(seconds * 1e9),
            "load_duration": int(2.5e9)}


@contextmanager
def fake_ollama(
    *,
    tags: list[str] | None = None,
    ps: dict[str, Any] | None = None,
    gen: dict[str, Any] | None = None,
    gen_delay: float = 0.0,
) -> Iterator[tuple[str, list[str]]]:
    """Fake Ollama native API on 127.0.0.1; yields (root url, request log)."""
    log: list[str] = []
    tags_body = json.dumps({"models": [{"name": t, "model": t} for t in (tags or [])]}).encode()

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, data: bytes) -> None:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802
            log.append("GET " + self.path)
            if self.path == "/api/tags":
                self._send(200, tags_body)
            elif self.path == "/api/ps":
                self._send(200, json.dumps(ps or {"models": []}).encode())
            else:
                self._send(404, b"{}")

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            log.append(f"POST {self.path} {body.get('model')}")
            if self.path != "/api/generate":
                self._send(404, b"{}")
                return
            if gen_delay:
                time.sleep(gen_delay)
            try:
                self._send(200, json.dumps(gen or {}).encode())
            except OSError:
                pass

        def log_message(self, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", log
    finally:
        server.shutdown()
        server.server_close()


def _free_port_url() -> str:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    return f"http://127.0.0.1:{port}"


# --------------------------------------------------------------------------- pure


def test_root_url_strips_v1_and_slash() -> None:
    assert gd.root_url("http://localhost:11434/v1/") == "http://localhost:11434"
    assert gd.root_url("localhost:11434") == "http://localhost:11434"
    assert gd.root_url(None) == gd.DEFAULT_ROOT_URL


def test_gpu_percent_from_ps() -> None:
    assert gd.gpu_percent(ps_payload("gemma4:e4b", 8 * GB, 8 * GB), "gemma4:e4b") == 100
    assert gd.gpu_percent(ps_payload("gemma4:e4b", 8 * GB, 0), "gemma4:e4b") == 0
    assert gd.gpu_percent(ps_payload("gemma4:e4b", 8 * GB, 4 * GB), "gemma4:e4b") == 50
    assert gd.gpu_percent(ps_payload("gemma4:latest", 8, 8), "gemma4") == 100
    assert gd.gpu_percent(ps_payload("other:1b", 8, 8), "gemma4:e4b") is None
    assert gd.gpu_percent({"models": []}, "gemma4:e4b") is None
    assert gd.gpu_percent(None, "gemma4:e4b") is None
    assert gd.gpu_percent({"models": [{"name": "gemma4:e4b", "size": 0, "size_vram": 0}]}, "gemma4:e4b") is None


def test_tokens_per_second() -> None:
    assert gd.tokens_per_second(gen_payload(84, 2.0)) == 42.0
    assert gd.tokens_per_second({"eval_count": 0, "eval_duration": 5}) is None
    assert gd.tokens_per_second({}) is None
    assert gd.tokens_per_second("x") is None


def test_smaller_model() -> None:
    assert gd.smaller_model("gemma4:e4b") == "gemma4:e2b"
    assert gd.smaller_model("GEMMA4:E4B") == "gemma4:e2b"
    assert gd.smaller_model("gemma4") == "gemma4:e2b"
    assert gd.smaller_model("gemma4:e2b") is None
    assert gd.smaller_model("llama3:8b") is None


def test_assess_fast_gpu_is_good() -> None:
    a = gd.assess("gemma4:e4b", 100, 42.0)
    assert a.ok and a.exit_code == gd.EXIT_OK
    assert a.verdict == "Gemma is running 100% on the GPU at 42 tokens/s - good."
    assert a.advice == []


def test_assess_cpu_is_slow_and_recommends_smaller() -> None:
    a = gd.assess("gemma4:e4b", 0, 3.0)
    assert not a.ok and a.exit_code == gd.EXIT_SLOW
    assert "on the CPU (0% GPU) at 3 tokens/s" in a.verdict
    assert "sizing will often fall back" in a.verdict and "Make Gemma fast" in a.verdict
    text = " ".join(a.advice)
    assert "update Ollama and your GPU driver" in text
    assert "FOUNDRY_GEMMA_MODEL=gemma4:e2b" in text


def test_assess_partial_offload_means_vram_too_small() -> None:
    a = gd.assess("gemma4:e4b", 60, 25.0)
    assert a.exit_code == gd.EXIT_SLOW
    assert "only 60% on the GPU" in a.verdict
    text = " ".join(a.advice)
    assert "VRAM" in text and "gemma4:e2b" in text


def test_assess_full_gpu_but_slow() -> None:
    a = gd.assess("gemma4:e4b", 100, 4.0)
    assert a.exit_code == gd.EXIT_SLOW
    assert "gemma4:e2b" in " ".join(a.advice)


def test_assess_smallest_model_slow_has_no_smaller_tag() -> None:
    a = gd.assess("gemma4:e2b", 100, 4.0)
    assert a.exit_code == gd.EXIT_SLOW
    assert "ollama pull" not in " ".join(a.advice)


def test_assess_unknown_gpu_but_fast_is_ok() -> None:
    a = gd.assess("gemma4:e4b", None, 30.0)
    assert a.ok and "GPU use unknown" in a.verdict


# --------------------------------------------------------------------------- HTTP


def test_diagnose_fast_gpu() -> None:
    with fake_ollama(
        tags=["gemma4:e4b"], ps=ps_payload("gemma4:e4b", 8 * GB, 8 * GB), gen=gen_payload(84, 2.0)
    ) as (url, log):
        report = gd.diagnose(url + "/v1", "gemma4:e4b", timeout=5)
    assert report["exit_code"] == 0 and report["ok"] is True
    assert report["gpu_pct"] == 100 and report["tokens_per_s"] == 42.0
    assert report["load_s"] == 2.5
    assert log == ["GET /api/tags", "POST /api/generate gemma4:e4b", "GET /api/ps"]


def test_diagnose_cpu_slow() -> None:
    with fake_ollama(tags=["gemma4:e4b"], ps=ps_payload("gemma4:e4b", 8 * GB, 0), gen=gen_payload(9, 3.0)) as (url, _):
        report = gd.diagnose(url, "gemma4:e4b", timeout=5)
    assert report["exit_code"] == 1 and report["gpu_pct"] == 0 and report["tokens_per_s"] == 3.0


def test_diagnose_model_missing() -> None:
    with fake_ollama(tags=["llama3:8b"]) as (url, log):
        report = gd.diagnose(url, "gemma4:e4b", timeout=5)
    assert report["exit_code"] == 2
    assert "not installed" in report["verdict"] and "llama3:8b" in report["verdict"]
    assert "ollama pull gemma4:e4b" in report["advice"][0]
    assert log == ["GET /api/tags"]


def test_diagnose_unreachable() -> None:
    report = gd.diagnose(_free_port_url(), "gemma4:e4b", timeout=2)
    assert report["exit_code"] == 2 and "not reachable" in report["verdict"]


def test_diagnose_generation_timeout_is_slow() -> None:
    with fake_ollama(tags=["gemma4:e4b"], gen=gen_payload(10, 1.0), gen_delay=2.0) as (url, _):
        report = gd.diagnose(url, "gemma4:e4b", timeout=0.5)
    assert report["exit_code"] == 1
    assert "did not answer" in report["verdict"]
    assert "gemma4:e2b" in report["advice"][0]


def test_diagnose_ignores_proxy_for_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    with fake_ollama(tags=["gemma4:e4b"], ps=ps_payload("gemma4:e4b", 8, 8), gen=gen_payload(40, 1.0)) as (url, _):
        report = gd.diagnose(url, "gemma4:e4b", timeout=5)
    assert report["exit_code"] == 0


def test_quick_report_uses_config() -> None:
    with fake_ollama(tags=["gemma4:e2b"], ps=ps_payload("gemma4:e2b", 4, 4), gen=gen_payload(60, 2.0)) as (url, log):
        cfg = FoundryConfig(gemma_base_url=url + "/v1", gemma_model="gemma4:e2b")
        out = gd.quick_report(cfg, timeout=5)
    assert set(out) == {"gpu_pct", "tokens_per_s", "model", "ok", "verdict"}
    assert out["ok"] is True and out["gpu_pct"] == 100 and out["tokens_per_s"] == 30.0
    assert out["model"] == "gemma4:e2b"
    assert "POST /api/generate gemma4:e2b" in log


# --------------------------------------------------------------------------- CLI


def test_main_prints_verdict_and_exit_code(capsys: pytest.CaptureFixture[str]) -> None:
    with fake_ollama(tags=["gemma4:e4b"], ps=ps_payload("gemma4:e4b", 8, 0), gen=gen_payload(6, 2.0)) as (url, _):
        code = gd.main(["--base-url", url, "--model", "gemma4:e4b", "--timeout", "5"])
    out = capsys.readouterr().out
    assert code == 1
    assert "Gemma is running on the CPU (0% GPU) at 3 tokens/s" in out
    assert "FOUNDRY_GEMMA_MODEL=gemma4:e2b" in out


def test_main_json(capsys: pytest.CaptureFixture[str]) -> None:
    with fake_ollama(tags=["gemma4:e4b"], ps=ps_payload("gemma4:e4b", 8, 8), gen=gen_payload(50, 1.0)) as (url, _):
        code = gd.main(["--base-url", url, "--json", "--timeout", "5"])
    data = json.loads(capsys.readouterr().out)
    assert code == 0 and data["tokens_per_s"] == 50.0 and data["gpu_pct"] == 100


def test_main_env_defaults(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    with fake_ollama(tags=["gemma4:e2b"], ps=ps_payload("gemma4:e2b", 8, 8), gen=gen_payload(50, 1.0)) as (url, log):
        monkeypatch.delenv("FOUNDRY_GEMMA_BASE_URL", raising=False)
        monkeypatch.delenv("FOUNDRY_GEMMA_MODEL", raising=False)
        monkeypatch.setenv("FOREMAN_OLLAMA_URL", url)
        monkeypatch.setenv("FOREMAN_MODEL", "gemma4:e2b")
        code = gd.main(["--timeout", "5"])
    assert code == 0
    assert "POST /api/generate gemma4:e2b" in log


def test_module_runs_as_script_unreachable() -> None:
    env = {"PATH": "", "FOUNDRY_GEMMA_BASE_URL": _free_port_url(), "SYSTEMROOT": ""}
    proc = subprocess.run(
        [sys.executable, "-m", "foundry.gemma_doctor", "--timeout", "2"],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 2
    assert "not reachable" in proc.stdout
