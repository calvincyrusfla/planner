"""End-to-end check of the Foundry service (T-013, charlie-3).

Starts the real service (``foundry.serve.run_service`` with the real
``foundry.serve_api`` HTTP layer) on a free loopback port with simulated
Claude and Gemma and a dry board bridge, then over real HTTP:

* GET /api/health, POST /api/orders, polls GET /api/status until the task is
  ``complete`` and checks the patch file was written;
* POST /api/ask and reads the server-sent events;
* starts ``department/server.mjs`` (DEPT_HUB=off, scratch DEPT_DATA, free port,
  FOUNDRY_SERVE_PORT pointing at the service) and checks /api/foundry/health
  and /api/foundry/ask reach the service through the dashboard proxy.

Safety: no real Claude, no Ollama, no shared board; only child processes
started here are stopped.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
SERVER_MJS = ROOT / "department" / "server.mjs"
ANSWER = "The line is quiet. Nothing is blocked."


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _gemma() -> Any:
    """demo_gemma, plus a fixed answer for the Foreman's Q&A prompt."""
    from foundry.gemma_client import GemmaReply, GemmaRequest, SimulatedGemmaClient
    from foundry.main import demo_gemma

    base = demo_gemma()

    def responder(req: GemmaRequest) -> Any:
        system = str(req.messages[0].get("content", "")) if req.messages else ""
        if "Answer questions about the production line" in system:
            return GemmaReply(text=ANSWER)
        return base.responder(req)

    return SimulatedGemmaClient(responder)


class _Running:
    def __init__(self, service: Any, port: int, repo: Path, patches: Path) -> None:
        self.service, self.port, self.repo, self.patches = service, port, repo, patches
        self.base = f"http://127.0.0.1:{port}"


@pytest.fixture
def running_service(tmp_path: Path, clean_foundry_env: pytest.MonkeyPatch) -> Iterator[_Running]:
    from foundry import serve
    from foundry.config import FoundryConfig
    from foundry.main import demo_claude, write_demo_repo

    repo = write_demo_repo(tmp_path / "repo")
    patches = tmp_path / "patches"
    config = FoundryConfig(
        db_path=tmp_path / "foundry.db",
        patches_dir=patches,
        rpm=600,
        tpm=10_000_000,
        max_concurrent=3,
        board_bridge_mode="dry",
    )
    service = serve.Service(
        config, repo=repo, claude=demo_claude(repo, fail_first=None), gemma=_gemma(), monitor_s=0.2
    )
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
        except BaseException as exc:  # surfaced to the test below
            holder["error"] = exc
            ready.set()
            raise

    thread = threading.Thread(target=lambda: asyncio.run(main()), name="foundry-e2e", daemon=True)
    thread.start()
    assert ready.wait(20), f"service did not start: {lines}"
    if "error" in holder:
        raise AssertionError(f"service failed to start: {holder['error']!r}") from holder["error"]
    m = re.search(r"http://127\.0\.0\.1:(\d+)", " ".join(lines))
    assert m, f"no bound port in the start line: {lines}"
    try:
        yield _Running(service, int(m.group(1)), repo, patches)
    finally:
        loop = holder.get("loop")
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(holder["stop"].set)
        thread.join(30)
        assert not thread.is_alive(), "service did not stop"
        assert any("stopped" in ln for ln in lines), lines


def _http(method: str, url: str, body: Any = None, timeout: float = 10.0) -> tuple[int, str, str]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.headers.get("Content-Type", ""), resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Content-Type", ""), exc.read().decode("utf-8")


def _json(method: str, url: str, body: Any = None) -> tuple[int, dict[str, Any]]:
    code, _, text = _http(method, url, body)
    return code, json.loads(text)


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


def _check_ask(code: int, ctype: str, text: str) -> None:
    assert code == 200, text
    assert ctype.startswith("text/event-stream"), ctype
    events = _sse(text)
    assert events and events[-1][0] == "done", events
    answer = "".join(e[1].get("text", "") for e in events if e[0] is None)
    assert ANSWER in answer, answer


def _wait_complete(base: str, tid: str, timeout: float = 30.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    row: dict[str, Any] = {}
    while time.monotonic() < deadline:
        code, status = _json("GET", f"{base}/api/status")
        assert code == 200 and status["ok"], status
        row = next((t for t in status["tasks"] if t["id"] == tid), {})
        if row.get("state") == "complete" and row.get("patch"):
            return row
        assert row.get("state") not in ("quarantined", "failed"), row
        time.sleep(0.1)
    raise AssertionError(f"{tid} did not complete with a patch in {timeout}s: {row}")


def test_service_end_to_end(running_service: _Running) -> None:
    base = running_service.base

    code, health = _json("GET", f"{base}/api/health")
    assert code == 200, health
    assert health["ok"] is True
    assert Path(health["repo"]) == running_service.repo.resolve()
    assert health["gemma_ok"] is True and health["claude_ok"] is True
    assert health["version"]

    code, bad = _json("POST", f"{base}/api/orders", {"title": ""})
    assert code == 400 and bad["ok"] is False, bad

    code, order = _json(
        "POST", f"{base}/api/orders",
        {"title": "Add clamp helper", "description": "Add clamp(x, lo, hi).", "files": ["shop/mathx.py"]},
    )
    assert code == 200 and order["ok"] is True, order
    tid = order["task_id"]

    row = _wait_complete(base, tid)
    assert row["team"] and row["builders"] == 1, row
    patch = Path(row["patch"])
    assert patch.is_file(), row
    assert patch.parent.resolve() == running_service.patches.resolve()
    text = patch.read_text(encoding="utf-8")
    assert "shop/mathx.py" in text and "+def mathx_helper(value):" in text, text
    # the repo itself is only read
    assert "mathx_helper" not in (running_service.repo / "shop" / "mathx.py").read_text(encoding="utf-8")

    code, ctype, body = _http("POST", f"{base}/api/ask", {"question": "How is the line doing?"})
    _check_ask(code, ctype, body)

    # a foreign Host header is refused (DNS-rebinding guard)
    req = urllib.request.Request(f"{base}/api/health", headers={"Host": "evil.example:80"})
    with pytest.raises(urllib.error.HTTPError) as err:
        urllib.request.urlopen(req, timeout=5)
    assert err.value.code == 403


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_dashboard_proxy_reaches_service(running_service: _Running, tmp_path: Path) -> None:
    port = _free_port()
    env = {k: v for k, v in os.environ.items() if not k.startswith(("DEPT_", "FOUNDRY_"))}
    env.update({
        "DEPT_HUB": "off",
        "DEPT_HUB_URL": "",
        "DEPT_DATA": str(tmp_path / "dept" / "state.json"),
        "DEPT_USAGE_DIRECT": "off",
        "FOUNDRY_SERVE_PORT": str(running_service.port),
        "HOME": str(tmp_path / "home"),
        "USERPROFILE": str(tmp_path / "home"),
    })
    (tmp_path / "dept").mkdir()
    (tmp_path / "home").mkdir()
    child = subprocess.Popen(
        ["node", str(SERVER_MJS), "--port", str(port)], cwd=str(ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    try:
        dash = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 15
        while True:
            try:
                code, health = _json("GET", f"{dash}/api/foundry/health")
                break
            except (urllib.error.URLError, ConnectionError):
                if time.monotonic() > deadline or child.poll() is not None:
                    child.kill()
                    raise AssertionError(f"dashboard did not start: {child.communicate()[0]!r}")
                time.sleep(0.1)
        assert code == 200, health
        assert health["ok"] is True
        assert Path(health["repo"]) == running_service.repo.resolve()

        code, ctype, body = _http("POST", f"{dash}/api/foundry/ask", {"question": "How is the line doing?"})
        _check_ask(code, ctype, body)
    finally:
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(5)
        if child.stdout:
            child.stdout.close()
