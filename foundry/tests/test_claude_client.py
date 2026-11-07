"""Tests for foundry.claude_client.

``ClaudeCodeClient`` is exercised against a FAKE ``claude`` executable (a tiny
Python script written into a temp dir); no real Claude Code is ever called.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import stat
import sys
import textwrap
import time
from pathlib import Path

import pytest

from foundry.claude_client import (
    ClaudeClient,
    ClaudeCodeClient,
    ClaudeNotFound,
    ClaudeRateLimited,
    SimulatedClaudeClient,
    detect_rate_limit,
    parse_claude_json,
    parse_retry_after,
    resolve_claude_bin,
)
from foundry.schemas import ClaudeResult

SECRET_PROMPT = "TOP-SECRET-PROMPT-" + "x" * 50


def make_fake_claude(tmp_path: Path, body: str, name: str = "claude") -> Path:
    """Write an executable fake ``claude`` that runs ``body`` (Python) and return its path.

    The body sees ``prompt`` (stdin text) and ``args`` (sys.argv[1:]).
    """
    tmp_path.mkdir(parents=True, exist_ok=True)
    script = tmp_path / f"{name}_impl.py"
    script.write_text(
        "import json, sys, os, time\n"
        "args = sys.argv[1:]\n"
        "prompt = sys.stdin.read()\n" + textwrap.dedent(body),
        encoding="utf-8",
    )
    if sys.platform.startswith("win"):
        exe = tmp_path / f"{name}.cmd"
        exe.write_text(f'@"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
    else:
        exe = tmp_path / name
        exe.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
        exe.chmod(exe.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return exe


def run(coro):
    return asyncio.run(coro)


OK_BODY = """
out = {
    "type": "result", "subtype": "success", "is_error": False,
    "duration_ms": 1234, "result": "echo:" + prompt[:20] + "|args=" + " ".join(args) + "|cwd=" + os.getcwd(),
    "total_cost_usd": 0.0123,
    "usage": {"input_tokens": 100, "cache_creation_input_tokens": 10,
              "cache_read_input_tokens": 5, "output_tokens": 42},
}
print(json.dumps(out))
"""


# --------------------------------------------------------------------------- ClaudeCodeClient


def test_code_client_parses_success_and_sends_prompt_on_stdin(tmp_path: Path) -> None:
    exe = make_fake_claude(tmp_path / "bin", OK_BODY)
    work = tmp_path / "work"
    work.mkdir()
    client = ClaudeCodeClient(claude_bin=str(exe), extra_args=("--model", "sonnet"))
    res = run(client.run("hello world prompt", agent_id="alpha-1", role="builder", cwd=str(work), timeout=30))
    assert isinstance(res, ClaudeResult)
    assert not res.is_error and res.error is None
    assert res.text.startswith("echo:hello world prompt")
    assert "|args=-p --output-format json --model sonnet|" in res.text
    assert res.text.endswith("|cwd=" + str(work.resolve())) or str(work) in res.text
    assert res.input_tokens == 115  # input + cache creation + cache read
    assert res.output_tokens == 42
    assert res.cost_usd == pytest.approx(0.0123)
    assert res.duration_seconds == pytest.approx(1.234)


def test_prompt_not_on_command_line(tmp_path: Path) -> None:
    exe = make_fake_claude(tmp_path, OK_BODY)
    client = ClaudeCodeClient(claude_bin=str(exe))
    argv = client.command()
    assert SECRET_PROMPT not in " ".join(argv)
    assert argv[-3:] == ["-p", "--output-format", "json"]


def test_prompt_never_logged(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    exe = make_fake_claude(tmp_path, OK_BODY)
    client = ClaudeCodeClient(claude_bin=str(exe))
    with caplog.at_level(logging.DEBUG, logger="foundry.claude_client"):
        run(client.run(SECRET_PROMPT, agent_id="bravo-2", role="reviewer", timeout=30))
    assert caplog.records, "expected some log records"
    assert all(SECRET_PROMPT not in r.getMessage() for r in caplog.records)
    assert all("TOP-SECRET" not in r.getMessage() for r in caplog.records)


def test_is_error_result_without_rate_limit(tmp_path: Path) -> None:
    body = """
print(json.dumps({"type": "result", "subtype": "error_during_execution", "is_error": True,
                  "result": "tool crashed", "duration_ms": 10, "usage": {"input_tokens": 3, "output_tokens": 0}}))
"""
    exe = make_fake_claude(tmp_path, body)
    res = run(ClaudeCodeClient(claude_bin=str(exe)).run("p", agent_id="a-1", role="builder", timeout=30))
    assert res.is_error
    assert "tool crashed" in (res.error or "")
    assert res.input_tokens == 3


@pytest.mark.parametrize(
    "message, expected_retry",
    [
        ("API Error: 429 rate limit exceeded, retry after 7 seconds", 7.0),
        ("Claude AI usage limit reached|{epoch}", "epoch"),
        ("API Error: 529 Overloaded", None),
    ],
)
def test_rate_limit_in_json_raises(tmp_path: Path, message: str, expected_retry) -> None:
    epoch = int(time.time()) + 120
    msg = message.format(epoch=epoch)
    body = f"""
print(json.dumps({{"type": "result", "subtype": "success", "is_error": True, "result": {msg!r},
                  "duration_ms": 5, "usage": {{"input_tokens": 0, "output_tokens": 0}}}}))
"""
    exe = make_fake_claude(tmp_path, body)
    with pytest.raises(ClaudeRateLimited) as ei:
        run(ClaudeCodeClient(claude_bin=str(exe)).run("p", agent_id="a-1", role="builder", timeout=30))
    if expected_retry == "epoch":
        assert ei.value.retry_after is not None and 100 <= ei.value.retry_after <= 121
    else:
        assert ei.value.retry_after == expected_retry


def test_rate_limit_on_stderr_with_nonzero_exit(tmp_path: Path) -> None:
    body = """
sys.stderr.write("Error: 429 Too Many Requests; retry-after: 3\\n")
sys.exit(1)
"""
    exe = make_fake_claude(tmp_path, body)
    with pytest.raises(ClaudeRateLimited) as ei:
        run(ClaudeCodeClient(claude_bin=str(exe)).run("p", agent_id="a-1", role="builder", timeout=30))
    assert ei.value.retry_after == 3.0


def test_nonzero_exit_without_json_is_error_result(tmp_path: Path) -> None:
    body = """
sys.stderr.write("boom: not logged in\\n")
sys.exit(2)
"""
    exe = make_fake_claude(tmp_path, body)
    res = run(ClaudeCodeClient(claude_bin=str(exe)).run("p", agent_id="a-1", role="builder", timeout=30))
    assert res.is_error
    assert "exited 2" in (res.error or "") and "not logged in" in (res.error or "")


def test_timeout_kills_process(tmp_path: Path) -> None:
    marker = tmp_path / "still_alive.txt"
    body = f"""
time.sleep(5)
open({str(marker)!r}, "w").write("alive")
"""
    exe = make_fake_claude(tmp_path / "bin", body)
    started = time.monotonic()
    res = run(ClaudeCodeClient(claude_bin=str(exe)).run("p", agent_id="a-1", role="builder", timeout=0.5))
    elapsed = time.monotonic() - started
    assert res.is_error and "timeout" in (res.error or "")
    assert elapsed < 4
    time.sleep(5.5 - elapsed)
    assert not marker.exists(), "child process (grandchild python) survived the kill"


def test_env_is_passed(tmp_path: Path) -> None:
    body = """
print(json.dumps({"type": "result", "is_error": False, "result": os.environ.get("FOUNDRY_FAKE", ""),
                  "usage": {"input_tokens": 1, "output_tokens": 1}}))
"""
    exe = make_fake_claude(tmp_path, body)
    res = run(ClaudeCodeClient(claude_bin=str(exe), env={"FOUNDRY_FAKE": "yes"}).run(
        "p", agent_id="a-1", role="manager", timeout=30))
    assert res.text == "yes"
    assert res.duration_seconds >= 0  # no duration_ms: falls back to wall time


def test_bare_name_resolved_from_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    exe = make_fake_claude(tmp_path, OK_BODY)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ.get("PATH", ""))
    assert Path(resolve_claude_bin("claude")).resolve() == exe.resolve()
    res = run(ClaudeCodeClient().run("hi", agent_id="a-1", role="builder", timeout=30))
    assert not res.is_error


def test_windows_cmd_shim_resolution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """On Windows only ``claude.cmd`` exists; the bare name must still resolve."""
    shim = tmp_path / "claude.cmd"
    shim.write_text("@echo off\r\n", encoding="utf-8")
    shim.chmod(0o755)
    seen: list[str] = []

    def fake_which(name: str) -> str | None:
        seen.append(name)
        return str(shim) if name == "claude.cmd" else None

    monkeypatch.setattr("foundry.claude_client.shutil.which", fake_which)
    assert resolve_claude_bin("claude", platform="win32") == str(shim)
    assert seen[:2] == ["claude", "claude.cmd"]
    with pytest.raises(ClaudeNotFound):
        resolve_claude_bin("claude", platform="linux")


def test_missing_executable_raises(tmp_path: Path) -> None:
    with pytest.raises(ClaudeNotFound):
        resolve_claude_bin(str(tmp_path / "nope" / "claude"))
    with pytest.raises(ClaudeNotFound):
        run(ClaudeCodeClient(claude_bin="definitely-not-claude-xyz").run(
            "p", agent_id="a-1", role="builder"))


# --------------------------------------------------------------------------- helpers


def test_parse_claude_json_variants() -> None:
    assert parse_claude_json('{"type":"result","result":"a"}')["result"] == "a"
    arr = json.dumps([{"type": "system"}, {"type": "result", "result": "b"}])
    assert parse_claude_json(arr)["result"] == "b"
    noisy = 'warning: something\n{"type":"result","result":"c"}\n'
    assert parse_claude_json(noisy)["result"] == "c"
    with pytest.raises(ValueError):
        parse_claude_json("")
    with pytest.raises(ValueError):
        parse_claude_json("not json at all")


def test_retry_after_and_detection() -> None:
    assert parse_retry_after("retry after 30") == 30.0
    assert parse_retry_after("Retry-After: 2.5s") == 2.5
    assert parse_retry_after("try again in 5 minutes") == 300.0
    assert parse_retry_after("retry after 500ms") == 0.5
    assert parse_retry_after("usage limit reached|1000", now=0) is None  # not a 10-digit epoch
    assert parse_retry_after("usage limit reached|1700000100", now=1700000000) == 100.0
    assert parse_retry_after("usage limit reached|1700000000", now=1700000100) == 0.0
    assert parse_retry_after("nothing here") is None
    assert detect_rate_limit("Rate limit exceeded")
    assert detect_rate_limit("Claude AI usage limit reached|1700000000")
    assert detect_rate_limit("HTTP 429")
    assert detect_rate_limit("5-hour limit reached - resets 3pm")
    assert detect_rate_limit("You've hit your limit")
    assert not detect_rate_limit("recursion limit reached")
    assert not detect_rate_limit("SyntaxError in file x.py line 4290")
    assert not detect_rate_limit("")


# --------------------------------------------------------------------------- SimulatedClaudeClient


def test_simulated_counts_and_default_reply() -> None:
    sim = SimulatedClaudeClient()
    assert isinstance(sim, ClaudeClient)
    assert isinstance(ClaudeCodeClient(), ClaudeClient)

    async def go() -> list[ClaudeResult]:
        return [
            await sim.run("abcd" * 10, agent_id="alpha-1", role="builder"),
            await sim.run("x", agent_id="alpha-1", role="builder"),
            await sim.run("y", agent_id="alpha-6", role="reviewer"),
        ]

    r = run(go())
    assert sim.calls["alpha-1"] == 2 and sim.calls["alpha-6"] == 1
    assert sim.calls["alpha-2"] == 0  # idle builder never called
    assert sim.total_calls == 3 and sim.role_calls["reviewer"] == 1
    assert r[0].text == "SIMULATED builder reply for alpha-1"
    assert r[0].input_tokens == 10
    assert sim.prompts_for("alpha-1") == ["abcd" * 10, "x"]


def test_simulated_script_and_queued_replies_are_deterministic() -> None:
    def script(prompt: str, agent_id: str) -> ClaudeResult | str:
        if agent_id.endswith("-6"):
            return ClaudeResult(text='{"verdict":"APPROVED"}', input_tokens=7, output_tokens=3)
        return f"{agent_id}:{len(prompt)}"

    def once() -> list[str]:
        sim = SimulatedClaudeClient(script)
        sim.queue_reply("special for bravo-2", agent_id="bravo-2")
        sim.queue_reply("anyone")

        async def go() -> list[str]:
            out = []
            for a in ("bravo-2", "bravo-2", "bravo-1", "bravo-1", "bravo-6"):
                out.append((await sim.run("hello", agent_id=a, role="builder")).text)
            return out

        return run(go())

    first = once()
    assert first == ["special for bravo-2", "anyone", "bravo-1:5", "bravo-1:5", '{"verdict":"APPROVED"}']
    assert once() == first


def test_simulated_injections() -> None:
    sim = SimulatedClaudeClient()
    sim.inject_rate_limit(agent_id="a-1", times=2, retry_after=1.5)
    sim.inject_failure(agent_id="a-2", error="kaput")
    sim.inject_exception(RuntimeError("crash"), agent_id="a-3")

    async def go() -> None:
        for _ in range(2):
            with pytest.raises(ClaudeRateLimited) as ei:
                await sim.run("p", agent_id="a-1", role="builder")
            assert ei.value.retry_after == 1.5
        assert not (await sim.run("p", agent_id="a-1", role="builder")).is_error
        bad = await sim.run("p", agent_id="a-2", role="builder")
        assert bad.is_error and bad.error == "kaput"
        assert not (await sim.run("p", agent_id="a-2", role="builder")).is_error
        with pytest.raises(RuntimeError):
            await sim.run("p", agent_id="a-3", role="builder")
        # an untargeted injection hits whoever calls next
        sim.inject_rate_limit()
        with pytest.raises(ClaudeRateLimited):
            await sim.run("p", agent_id="zz", role="manager")

    run(go())
    assert sim.calls["a-1"] == 3  # rate-limited attempts still count as calls
    assert sim.calls["a-2"] == 2 and sim.calls["a-3"] == 1 and sim.calls["zz"] == 1
    assert sim.in_flight == 0


def test_simulated_tracks_concurrency() -> None:
    sim = SimulatedClaudeClient(latency=0.01)

    async def go() -> None:
        await asyncio.gather(*(sim.run("p", agent_id=f"c-{i}", role="builder") for i in range(5)))

    run(go())
    assert sim.max_in_flight == 5 and sim.in_flight == 0
    sim.reset()
    assert sim.total_calls == 0 and sim.history == [] and sim.max_in_flight == 0
