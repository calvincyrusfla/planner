"""End-to-end checks for T-017 (Gemma group 1: a fast and trustworthy Foreman).

The real Foundry service talks to a *fake Ollama* HTTP server on loopback (the
real ``OllamaGemmaClient``, real HTTP) with configurable slow or hanging
replies, and checks over the service's own HTTP API:

* an order sized by Gemma shows ``sizing_source == "gemma"`` and ``sizing_ms``
  in ``GET /api/status``; ``foreman.gemma`` carries Gemma's stats;
* a Gemma that times out leaves the order sized by the ``"heuristic"``;
* ``POST /api/ask`` with a hanging Gemma streams the built-in status answer
  (not an error) within the ask timeout (``FoundryConfig.ask_timeout_s``);
* ``GET /api/health`` has ``gemma_speed`` {avg_ms, last_ms, fallbacks, calls};
* ``python -m foundry.serve`` stops promptly on SIGINT while a Gemma call
  (120 s hang, 120 s client timeout) is in flight.

Safety: no real Claude (a fake ``claude`` script that is never needed), no
real Ollama, dry board bridge, DEPT_HUB=off and a scratch DEPT_DATA; only the
child process started here is stopped.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
MODEL = "gemma4:e4b"
FILES = ["shop/mathx.py"]
ORDER = {"title": "Add clamp helper", "description": "Add clamp(x, lo, hi).", "files": FILES}
FALLBACK_MARK = "here is the line status instead"
_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


# --------------------------------------------------------------------------- fake Ollama


def _kind(payload: dict[str, Any]) -> str:
    messages = payload.get("messages") or []
    system = str(messages[0].get("content", "")) if messages else ""
    if "Size the work order" in system:
        return "sizing"
    if "Answer questions about the production line" in system:
        return "ask"
    return "other"


class FakeOllama:
    """Loopback OpenAI-compatible Ollama stand-in.

    ``delay[kind]`` seconds are slept before answering (``kind`` is
    ``sizing``/``ask``/``other``); ``requests`` records the kinds received.
    """

    def __init__(self, **delay: float) -> None:
        self.delay = {"sizing": 0.0, "ask": 0.0, "other": 0.0, **delay}
        self.requests: list[str] = []
        self.closing = threading.Event()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_: Any) -> None:  # quiet
                pass

            def _send(self, code: int, body: Any) -> None:
                data = json.dumps(body).encode()
                try:
                    self.send_response(code)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except OSError:
                    pass  # the client gave up (timeout) - expected

            def do_GET(self) -> None:  # noqa: N802
                if self.path.rstrip("/").endswith("/models"):
                    return self._send(200, {"object": "list", "data": [{"id": MODEL, "object": "model"}]})
                if self.path.startswith("/api/tags"):
                    return self._send(200, {"models": [{"name": MODEL, "model": MODEL}]})
                self._send(404, {"error": "not found"})

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length) or b"{}")
                if not self.path.rstrip("/").endswith("/chat/completions"):
                    return self._send(404, {"error": "not found"})
                kind = _kind(payload)
                fake.requests.append(kind)
                fake.closing.wait(fake.delay.get(kind, 0.0))
                self._send(200, fake.reply(kind, payload))

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = int(self.server.server_address[1])
        self.base_url = f"http://127.0.0.1:{self.port}/v1"
        self._thread = threading.Thread(target=self.server.serve_forever, name="fake-ollama", daemon=True)

    @staticmethod
    def reply(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        if kind == "sizing":
            content = json.dumps({
                "complexity": "S",
                "rationale": "One small helper in one file.",
                "subtasks": [{"title": "Add clamp", "acceptance_criteria": ["clamp works"], "allowed_files": FILES}],
            })
        elif kind == "ask":
            content = "Gemma says the line is quiet."
        else:
            content = "{}" if payload.get("response_format") else ""
        return {
            "id": "chatcmpl-fake",
            "object": "chat.completion",
            "model": MODEL,
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
        }

    def __enter__(self) -> FakeOllama:
        self._thread.start()
        return self

    def __exit__(self, *_: Any) -> None:
        self.closing.set()  # wake hanging handlers
        self.server.shutdown()
        self.server.server_close()


# --------------------------------------------------------------------------- helpers


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _http(method: str, url: str, body: Any = None, timeout: float = 15.0) -> tuple[int, str]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with _NO_PROXY.open(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def _json(method: str, url: str, body: Any = None) -> dict[str, Any]:
    code, text = _http(method, url, body)
    assert code == 200, (code, text)
    return json.loads(text)


def _sse(text: str) -> list[tuple[str | None, dict[str, Any]]]:
    events = []
    for block in text.replace("\r\n", "\n").split("\n\n"):
        name, data = None, []
        for line in block.split("\n"):
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].strip())
        if name or data:
            events.append((name, json.loads("\n".join(data)) if data else {}))
    return events


def _row(base: str, tid: str) -> dict[str, Any]:
    status = _json("GET", f"{base}/api/status")
    return next((t for t in status["tasks"] if t["id"] == tid), {})


def _wait_sized(base: str, tid: str, timeout: float = 30.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    row: dict[str, Any] = {}
    while time.monotonic() < deadline:
        row = _row(base, tid)
        if row.get("sizing_source"):
            return row
        time.sleep(0.1)
    raise AssertionError(f"{tid} was not sized in {timeout}s: {row}")


class _Running:
    def __init__(self, service: Any, port: int) -> None:
        self.service = service
        self.base = f"http://127.0.0.1:{port}"


@contextmanager
def _start(tmp_path: Path, fake: FakeOllama, *, gemma_timeout: float, ask_timeout: float = 90.0) -> Iterator[_Running]:
    """Run the real service (real OllamaGemmaClient -> fake) on a free port in a thread."""
    from foundry import serve
    from foundry.board_bridge import BoardBridge
    from foundry.config import FoundryConfig
    from foundry.gemma_client import OllamaGemmaClient
    from foundry.main import demo_claude, write_demo_repo

    repo = write_demo_repo(tmp_path / "repo")
    config = FoundryConfig(
        db_path=tmp_path / "foundry.db", patches_dir=tmp_path / "patches", rpm=600, tpm=10_000_000,
        max_concurrent=3, board_bridge_mode="dry", gemma_base_url=fake.base_url, gemma_model=MODEL,
        gemma_timeout_s=gemma_timeout, ask_timeout_s=ask_timeout,
    )
    gemma = OllamaGemmaClient(fake.base_url, MODEL, timeout=gemma_timeout)
    service = serve.Service(
        config, repo=repo, claude=demo_claude(repo, fail_first=None), gemma=gemma, monitor_s=3600,
        factory_kwargs={"bridge": BoardBridge("dry"), "patches_dir": tmp_path / "patches"},
    )
    service.sizing_stop_s = 1.0
    lines: list[str] = []
    holder: dict[str, Any] = {}
    ready = threading.Event()

    def out(line: str = "") -> None:
        lines.append(str(line))
        if "running for" in str(line):
            ready.set()

    async def main() -> None:
        holder["loop"] = asyncio.get_running_loop()
        holder["stop"] = asyncio.Event()
        try:
            await serve.run_service(service, port=0, stop_event=holder["stop"], out=out)
        except BaseException as exc:
            holder["error"] = exc
            ready.set()
            raise

    thread = threading.Thread(target=lambda: asyncio.run(main()), name="foundry-g1-e2e", daemon=True)
    thread.start()
    assert ready.wait(20), f"service did not start: {lines}"
    assert "error" not in holder, holder.get("error")
    m = re.search(r"http://127\.0\.0\.1:(\d+)", " ".join(lines))
    assert m, lines
    try:
        yield _Running(service, int(m.group(1)))
    finally:
        fake.closing.set()  # release any hanging Gemma reply so shutdown is quick
        loop = holder.get("loop")
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(holder["stop"].set)
        thread.join(30)
        assert not thread.is_alive(), "service did not stop"


def _check_gemma_speed(speed: Any) -> None:
    assert isinstance(speed, dict), speed
    for key in ("avg_ms", "last_ms", "fallbacks", "calls"):
        assert key in speed, (key, speed)


# --------------------------------------------------------------------------- tests


def test_sized_by_gemma_shows_source_and_time(tmp_path: Path, clean_foundry_env: pytest.MonkeyPatch) -> None:
    with FakeOllama(sizing=1.2) as fake:
        with _start(tmp_path, fake, gemma_timeout=20) as running:
            base = running.base
            tid = _json("POST", f"{base}/api/orders", ORDER)["task_id"]
            row = _wait_sized(base, tid)
            assert row["sizing_source"] == "gemma", row
            assert isinstance(row["sizing_ms"], (int, float)), row
            assert 1000 <= row["sizing_ms"] < 20_000, row
            assert "sizing" in fake.requests

            status = _json("GET", f"{base}/api/status")
            gemma = status["foreman"].get("gemma")
            assert isinstance(gemma, dict), status["foreman"]
            assert gemma["calls"] >= 1, gemma
            sizing = gemma["by_hook"]["size_task_scope"]
            assert sizing["calls"] >= 1 and sizing["fallbacks"] == 0, gemma
            assert gemma["last_ms"] is not None and gemma["avg_ms"] is not None, gemma

            health = _json("GET", f"{base}/api/health")
            _check_gemma_speed(health.get("gemma_speed"))
            assert health["gemma_speed"]["calls"] >= 1, health
            assert health["gemma_ok"] is True, health


def test_gemma_timeout_falls_back_to_heuristic(tmp_path: Path, clean_foundry_env: pytest.MonkeyPatch) -> None:
    with FakeOllama(sizing=60) as fake:
        with _start(tmp_path, fake, gemma_timeout=1.0) as running:
            base = running.base
            tid = _json("POST", f"{base}/api/orders", ORDER)["task_id"]
            row = _wait_sized(base, tid, timeout=20)
            assert row["sizing_source"] == "heuristic", row
            assert row["builders"], row  # still sized, by the fallback
            assert isinstance(row.get("sizing_ms"), (int, float)), row

            health = _json("GET", f"{base}/api/health")
            _check_gemma_speed(health.get("gemma_speed"))
            assert health["gemma_speed"]["fallbacks"] >= 1, health


def test_ask_with_slow_gemma_returns_fallback_within_ask_timeout(
    tmp_path: Path, clean_foundry_env: pytest.MonkeyPatch
) -> None:
    ask_timeout = 1.5
    # the general Gemma timeout is long: only the ask timeout may cut the call short
    with FakeOllama(ask=60) as fake:
        with _start(tmp_path, fake, gemma_timeout=60, ask_timeout=ask_timeout) as running:
            t0 = time.monotonic()
            code, text = _http("POST", f"{running.base}/api/ask", {"question": "How is the line?"}, timeout=30)
            took = time.monotonic() - t0
            assert code == 200, text
            events = _sse(text)
            assert events and events[-1][0] == "done", events
            assert not [e for e in events if e[0] == "error"], events
            answer = "".join(e[1].get("text", "") for e in events if e[0] is None)
            assert FALLBACK_MARK in answer, answer
            assert "ask" in fake.requests
            assert took < ask_timeout + 8, took  # one ask-timeout call, not the 60 s Gemma timeout


def test_service_exit_does_not_wait_for_in_flight_gemma(tmp_path: Path) -> None:
    from foundry.main import write_demo_repo

    repo = write_demo_repo(tmp_path / "repo")
    fake_claude = tmp_path / "bin" / "claude"
    fake_claude.parent.mkdir()
    fake_claude.write_text("#!/bin/sh\necho 'fake claude: not used in this test' >&2\nexit 1\n")
    fake_claude.chmod(0o755)
    port = _free_port()
    with FakeOllama(sizing=120) as fake:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("FOUNDRY_", "DEPT_")) and k != "FOREMAN_MODEL"}
        env.update({
            "PYTHONPATH": str(ROOT),
            "PYTHONUNBUFFERED": "1",
            "DEPT_HUB": "off",
            "DEPT_DATA": str(tmp_path / "dept-data"),
            "FOUNDRY_DB_PATH": str(tmp_path / "foundry.db"),
            "FOUNDRY_PATCHES_DIR": str(tmp_path / "patches"),
            "FOUNDRY_GEMMA_BASE_URL": fake.base_url,
            "FOUNDRY_GEMMA_MODEL": MODEL,
            "FOUNDRY_GEMMA_TIMEOUT_S": "120",
            "FOUNDRY_CLAUDE_BIN": str(fake_claude),
            "FOUNDRY_MONITOR_S": "3600",
        })
        log_path = tmp_path / "serve.log"
        with open(log_path, "wb") as log:
            proc = subprocess.Popen(
                [sys.executable, "-m", "foundry.serve", "--repo", str(repo), "--port", str(port), "--board", "dry"],
                cwd=str(ROOT), env=env, stdout=log, stderr=subprocess.STDOUT,
            )
        try:
            base = f"http://127.0.0.1:{port}"
            deadline = time.monotonic() + 30
            while True:
                try:
                    if _http("GET", f"{base}/api/health", timeout=2)[0] == 200:
                        break
                except OSError:
                    pass
                assert proc.poll() is None, log_path.read_text()
                assert time.monotonic() < deadline, log_path.read_text()
                time.sleep(0.2)
            _json("POST", f"{base}/api/orders", ORDER)
            deadline = time.monotonic() + 15
            while "sizing" not in fake.requests:  # the Gemma call is now in flight (hangs 120 s)
                assert time.monotonic() < deadline, log_path.read_text()
                time.sleep(0.1)
            time.sleep(0.5)
            t0 = time.monotonic()
            proc.send_signal(signal.SIGINT)
            try:
                code = proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                raise AssertionError(f"service still running 30 s after SIGINT:\n{log_path.read_text()}") from None
            took = time.monotonic() - t0
            assert took <= 15, f"exit took {took:.1f}s:\n{log_path.read_text()}"
            assert code == 0, log_path.read_text()
            assert "Foundry stopped." in log_path.read_text()
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(10)
