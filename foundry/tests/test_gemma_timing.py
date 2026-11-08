"""T-017 alpha-1: daemon-thread Gemma calls, per-call/ask timeouts and Foreman Gemma stats.

No real Ollama: a hanging fake HTTP server on 127.0.0.1 and SimulatedGemmaClient.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import textwrap
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from foundry.config import FoundryConfig
from foundry.gemma_client import GemmaRequest, GemmaUnavailable, OllamaGemmaClient, SimulatedGemmaClient
from foundry.orchestrator_queue import FoundryQueue
from foundry.shop_foreman import ASK_BUDGET_CAP_S, ShopForeman

REPO = Path(__file__).resolve().parents[2]


class HangingOllama:
    """Accepts a chat request and holds it for ``hang`` seconds (then answers)."""

    def __init__(self, hang: float) -> None:
        self.hang = hang
        self.release = threading.Event()
        fake = self

        class _H(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                fake.release.wait(fake.hang)
                body = json.dumps({"choices": [{"message": {"content": "late"}, "finish_reason": "stop"}]}).encode()
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except OSError:
                    pass

            def log_message(self, *args: Any) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _H)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def __enter__(self) -> HangingOllama:
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release.set()
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def hanging() -> Iterator[HangingOllama]:
    with HangingOllama(hang=30) as fake:
        yield fake


def test_cancelled_chat_returns_at_once_while_request_hangs(hanging: HangingOllama) -> None:
    client = OllamaGemmaClient(hanging.base_url, timeout=60)

    async def main() -> float:
        started = time.monotonic()
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(client.chat([{"role": "user", "content": "hi"}]), 0.3)
        return time.monotonic() - started

    started = time.monotonic()
    took = asyncio.run(main())
    assert took < 2
    # asyncio.run itself must not wait for the abandoned worker (to_thread would, up to 60 s).
    assert time.monotonic() - started < 3


def test_per_call_timeout_overrides_client_timeout(hanging: HangingOllama) -> None:
    client = OllamaGemmaClient(hanging.base_url, timeout=60)
    started = time.monotonic()
    with pytest.raises(GemmaUnavailable, match=r"timed out after 0\.4s"):
        asyncio.run(client.chat([{"role": "user", "content": "hi"}], timeout=0.4))
    assert time.monotonic() - started < 3
    assert client.timeout == 60


def test_process_exit_is_not_held_by_in_flight_gemma_call(hanging: HangingOllama) -> None:
    script = textwrap.dedent(
        f"""
        import asyncio
        from foundry.gemma_client import OllamaGemmaClient

        async def main():
            client = OllamaGemmaClient({hanging.base_url!r}, timeout=120)
            task = asyncio.create_task(client.chat([{{"role": "user", "content": "hi"}}]))
            await asyncio.sleep(0.3)
            print("leaving", flush=True)
            # returning leaves `task` pending; asyncio.run cancels it on exit

        asyncio.run(main())
        """
    )
    started = time.monotonic()
    proc = subprocess.run([sys.executable, "-c", script], cwd=REPO, capture_output=True, text=True, timeout=60)
    took = time.monotonic() - started
    assert proc.returncode == 0, proc.stderr
    assert "leaving" in proc.stdout
    assert took < 15


def test_ask_timeout_config_field_and_env() -> None:
    assert FoundryConfig().ask_timeout_s == 90
    assert FoundryConfig.from_env({"FOUNDRY_ASK_TIMEOUT_S": "12.5"}).ask_timeout_s == 12.5
    with pytest.raises(ValueError):
        FoundryConfig(ask_timeout_s=0)


def _foreman(tmp_path: Path, gemma: Any, **config: Any) -> tuple[ShopForeman, FoundryQueue]:
    queue = FoundryQueue(tmp_path / "foundry.db")
    cfg = FoundryConfig(db_path=tmp_path / "foundry.db", **config)
    return ShopForeman(gemma, queue, None, None, cfg), queue


async def _collect(agen: Any) -> list[str]:
    return [c async for c in agen]


def test_slow_gemma_ask_falls_back_within_ask_timeout(tmp_path: Path) -> None:
    async def slow(req: GemmaRequest) -> str:
        await asyncio.sleep(30)
        return "too late"

    gemma = SimulatedGemmaClient(slow)
    foreman, queue = _foreman(tmp_path, gemma, ask_timeout_s=0.3)
    try:
        started = time.monotonic()
        text = "".join(asyncio.run(_collect(foreman.answer_question("status?"))))
        assert time.monotonic() - started < 3
        assert "did not answer within 0.3 s" in text and "line status" in text
        assert foreman.last_source["answer_question"] == "heuristic"
        assert gemma.timeouts and 0 < gemma.timeouts[0] <= 0.3
        stats = foreman.gemma_stats()
        assert stats["calls"] == 1 and stats["timeouts"] == 1 and stats["fallbacks"] == 1
        assert stats["by_hook"]["answer_question"]["timeouts"] == 1
        assert stats["last_error"].startswith("timed out")
        assert foreman.last_timing["answer_question"] >= 250
    finally:
        queue.close()


def test_ask_against_hanging_ollama_uses_client_timeout_kwarg(tmp_path: Path, hanging: HangingOllama) -> None:
    client = OllamaGemmaClient(hanging.base_url, timeout=60)
    foreman, queue = _foreman(tmp_path, client, ask_timeout_s=0.5)
    try:
        started = time.monotonic()
        text = "".join(asyncio.run(_collect(foreman.answer_question("status?"))))
        assert time.monotonic() - started < 3
        assert "did not answer within 0.5 s" in text
    finally:
        queue.close()


def test_offline_wording_kept_when_gemma_errors(tmp_path: Path) -> None:
    foreman, queue = _foreman(tmp_path, SimulatedGemmaClient(unavailable=True))
    try:
        text = "".join(asyncio.run(_collect(foreman.answer_question("status?"))))
        assert "offline" in text
        stats = foreman.gemma_stats()
        assert stats["errors"] == 1 and stats["timeouts"] == 0 and stats["fallbacks"] == 1
    finally:
        queue.close()


def test_gemma_stats_shape_and_sizing_timing(tmp_path: Path) -> None:
    def responder(req: GemmaRequest) -> Any:
        return {"complexity": "S", "rationale": "small", "subtasks": [
            {"title": "do it", "acceptance_criteria": ["works"], "allowed_files": ["a.py"]}]}

    foreman, queue = _foreman(tmp_path, SimulatedGemmaClient(responder))
    try:
        empty = foreman.gemma_stats()
        assert empty == {"calls": 0, "errors": 0, "timeouts": 0, "fallbacks": 0, "served_by_gemma": 0,
                         "avg_ms": None, "last_ms": None, "last_error": None, "by_hook": {}}
        task = {"task_id": "F-1", "team_id": "alpha", "title": "Tiny", "description": "x", "files": ["a.py"]}
        asyncio.run(foreman.size_task_scope(task))
        stats = foreman.gemma_stats()
        hook = stats["by_hook"]["size_task_scope"]
        assert stats["calls"] == 1 and hook["calls"] == 1
        assert hook["served_by_gemma"] + hook["fallbacks"] == 1
        assert stats["avg_ms"] is not None and stats["last_ms"] == foreman.last_timing["size_task_scope"]
        json.dumps(stats)
    finally:
        queue.close()


def test_no_gemma_counts_fallbacks_without_calls(tmp_path: Path) -> None:
    foreman, queue = _foreman(tmp_path, None)
    try:
        task = {"task_id": "F-2", "team_id": "alpha", "title": "Tiny", "description": "x", "files": ["a.py"]}
        asyncio.run(foreman.size_task_scope(task))
        stats = foreman.gemma_stats()
        assert foreman.last_source["size_task_scope"] == "heuristic"
        # Gemma was never asked, so no fallback is counted (fallbacks never exceed calls).
        assert stats["calls"] == 0 and stats["fallbacks"] == 0 and stats["by_hook"] == {}
        assert foreman.last_timing == {}
    finally:
        queue.close()


class _Engine:
    """Context engine stub: builds a slice without asking Gemma."""

    last_stats = type("S", (), {"tokens_saved": 0, "ranked_by_gemma": False})()

    async def abuild_slice(self, subtask: Any, objective: str, **kwargs: Any) -> Any:
        from foundry.schemas import ContextSlice

        return ContextSlice(objective=objective or "x", acceptance_criteria=[])


def test_prune_without_gemma_ranking_is_not_a_fallback(tmp_path: Path) -> None:
    gemma = SimulatedGemmaClient()
    queue = FoundryQueue(tmp_path / "foundry.db")
    foreman = ShopForeman(gemma, queue, None, _Engine(), FoundryConfig(db_path=tmp_path / "foundry.db"))
    try:
        asyncio.run(foreman.prune_context_slice(None, "Do the thing.", task_id="F-3"))
        assert foreman.last_source["prune_context_slice"] == "heuristic"
        stats = foreman.gemma_stats()
        assert stats["calls"] == 0 and stats["fallbacks"] == 0 and gemma.calls == 0
    finally:
        queue.close()


def test_ask_budget_is_capped_below_api_chunk_timeout(tmp_path: Path) -> None:
    from foundry import serve_api

    assert ASK_BUDGET_CAP_S == serve_api.ASK_CHUNK_TIMEOUT_S - 10
    config = FoundryConfig.from_env({"FOUNDRY_ASK_TIMEOUT_S": "200", "FOUNDRY_DB_PATH": str(tmp_path / "f.db")})
    assert config.ask_timeout_s == 200
    gemma = SimulatedGemmaClient(lambda req: "All quiet.")
    queue = FoundryQueue(tmp_path / "f.db")
    foreman = ShopForeman(gemma, queue, None, None, config)
    try:
        text = "".join(asyncio.run(_collect(foreman.answer_question("status?"))))
        assert text == "All quiet."
        assert gemma.timeouts and ASK_BUDGET_CAP_S - 5 < gemma.timeouts[0] <= ASK_BUDGET_CAP_S
    finally:
        queue.close()
