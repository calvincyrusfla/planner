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
from datetime import datetime, timezone
from pathlib import Path

import pytest

from foundry.claude_client import (
    ClaudeAuthError,
    ClaudeClient,
    ClaudeCodeClient,
    ClaudeError,
    ClaudeNotFound,
    ClaudeRateLimited,
    SimulatedClaudeClient,
    detect_auth_error,
    detect_rate_limit,
    parse_claude_json,
    parse_retry_after,
    resolve_claude_bin,
    windows_cmd_launch_line,
    windows_shim_command_line,
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
    assert res.input_tokens == 110  # uncached input + cache creation (D11)
    assert res.cache_read_tokens == 5  # cache reads reported separately
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
sys.stderr.write("boom: segfault in tool\\n")
sys.exit(2)
"""
    exe = make_fake_claude(tmp_path, body)
    res = run(ClaudeCodeClient(claude_bin=str(exe)).run("p", agent_id="a-1", role="builder", timeout=30))
    assert res.is_error
    assert "exited 2" in (res.error or "") and "segfault" in (res.error or "")


@pytest.mark.parametrize(
    "body",
    [
        # JSON result form
        """
print(json.dumps({"type": "result", "is_error": True, "result": "Invalid API key · Please run /login",
                  "usage": {"input_tokens": 0, "output_tokens": 0}}))
""",
        # plain stderr form
        """
sys.stderr.write("Error: not logged in. Please run /login\\n")
sys.exit(1)
""",
    ],
)
def test_auth_errors_raise_claude_auth_error(tmp_path: Path, body: str) -> None:
    exe = make_fake_claude(tmp_path, body)
    with pytest.raises(ClaudeAuthError) as ei:
        run(ClaudeCodeClient(claude_bin=str(exe)).run("p", agent_id="a-1", role="builder", timeout=30))
    assert isinstance(ei.value, ClaudeError)
    assert "/login" in str(ei.value)


def test_spawn_oserror_is_mapped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    exe = make_fake_claude(tmp_path, OK_BODY)

    async def boom(*a, **k):
        raise PermissionError(13, "Access is denied")

    async def other(*a, **k):
        raise OSError(8, "Exec format error")

    monkeypatch.setattr("foundry.claude_client.asyncio.create_subprocess_exec", boom)
    with pytest.raises(ClaudeNotFound):
        run(ClaudeCodeClient(claude_bin=str(exe)).run("p", agent_id="a-1", role="builder"))
    monkeypatch.setattr("foundry.claude_client.asyncio.create_subprocess_exec", other)
    with pytest.raises(ClaudeError) as ei:
        run(ClaudeCodeClient(claude_bin=str(exe)).run("p", agent_id="a-1", role="builder"))
    assert not isinstance(ei.value, ClaudeNotFound)


def test_missing_cwd_is_claude_error_not_not_found(tmp_path: Path) -> None:
    exe = make_fake_claude(tmp_path, OK_BODY)
    with pytest.raises(ClaudeError) as ei:
        run(ClaudeCodeClient(claude_bin=str(exe)).run(
            "p", agent_id="a-1", role="builder", cwd=str(tmp_path / "missing")))
    assert not isinstance(ei.value, ClaudeNotFound)
    assert "working directory" in str(ei.value)


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
    # .exe and .cmd are preferred over the extensionless npm sh shim
    assert seen[:2] == ["claude.exe", "claude.cmd"]
    with pytest.raises(ClaudeNotFound):
        resolve_claude_bin("claude", platform="linux")


def test_windows_prefers_exe_and_cmd_over_bare_name(monkeypatch: pytest.MonkeyPatch) -> None:
    table = {
        "claude": r"C:\npm\claude",
        "claude.cmd": r"C:\npm\claude.cmd",
        "claude.exe": r"C:\Users\me\.local\bin\claude.exe",
    }
    monkeypatch.setattr("foundry.claude_client.shutil.which", table.get)
    assert resolve_claude_bin("claude", platform="win32").endswith("claude.exe")
    del table["claude.exe"]
    assert resolve_claude_bin("claude", platform="win32").endswith("claude.cmd")
    del table["claude.cmd"]
    assert resolve_claude_bin("claude", platform="win32") == r"C:\npm\claude"
    # an explicit suffix is used as given
    table["claude.cmd"] = r"C:\npm\claude.cmd"
    assert resolve_claude_bin("claude.cmd", platform="win32") == r"C:\npm\claude.cmd"


def _cmd_c(full_line: str) -> tuple[str, list[str], str]:
    """Simulate how Windows + cmd.exe treat ``"<comspec>" /d /c "<inner>"``.

    Returns (program, switches, executed command). CreateProcess takes the first
    quoted token as the program; cmd then applies its ``/c`` rule: with more than two
    quote characters it strips exactly the first and the last quote of the rest.
    """
    assert full_line.startswith('"')
    end = full_line.index('"', 1)
    program = full_line[1:end]
    rest = full_line[end + 1:].lstrip()
    switches = []
    while rest.startswith("/"):
        sw, _, rest = rest.partition(" ")
        switches.append(sw.lower())
        if sw.lower() == "/c":
            break
    assert rest.count('"') > 2
    first, last = rest.index('"'), rest.rindex('"')
    return program, switches, rest[:first] + rest[first + 1:last] + rest[last + 1:]


def _split_quoted(line: str) -> list[str]:
    """Split a line whose tokens are all double-quoted (MS CRT rules, no embedded quotes):
    a run of 2n backslashes before a closing quote means n literal backslashes."""
    assert line.startswith('"') and line.endswith('"')
    out = []
    for tok in line[1:-1].split('" "'):
        n = len(tok) - len(tok.rstrip("\\"))
        assert n % 2 == 0, "odd trailing backslashes would escape the closing quote"
        out.append(tok[: len(tok) - n // 2] if n else tok)
    return out


WIN_ENV = {"COMSPEC": r"C:\WINDOWS\system32\cmd.exe", "SystemRoot": r"C:\WINDOWS"}


def test_windows_cmd_shim_with_spaces_in_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """B2: C:\\Users\\John Smith\\...\\claude.cmd must survive cmd.exe's quote stripping."""
    exe = r"C:\Users\John Smith\AppData\Roaming\npm\claude.cmd"
    client = ClaudeCodeClient(
        claude_bin=exe,
        extra_args=("--max-turns", "3", "--disallowedTools", "Bash Edit Write", "--append-system-prompt",
                    "You are a Foundry builder & output only a diff > nothing else | ok^"),
        env={"COMSPEC": WIN_ENV["COMSPEC"]},
    )
    client._resolved = exe  # pretend resolution happened on Windows
    mode, spec = client.launch_spec(platform="win32")
    assert mode == "verbatim" and isinstance(spec, str)
    assert spec.startswith(f'"{WIN_ENV["COMSPEC"]}" /d /c ""{exe}" ')
    program, switches, executed = _cmd_c(spec)
    assert program == WIN_ENV["COMSPEC"]
    assert switches == ["/d", "/c"]  # /d: AutoRun disabled, no /s
    tokens = _split_quoted(executed)
    assert tokens[0] == exe
    assert tokens[1:4] == ["-p", "--output-format", "json"]
    assert tokens[4:] == list(client.extra_args)
    assert executed == windows_shim_command_line(exe, client.command()[1:])


def test_windows_launch_line_comspec_handling() -> None:
    line = windows_cmd_launch_line(r"C:\n p\claude.cmd", ["-p"], environ={"SystemRoot": "D:\\Win\\"})
    assert line == r'"D:\Win\System32\cmd.exe" /d /c ""C:\n p\claude.cmd" "-p""'
    quoted = windows_cmd_launch_line(r"C:\x\claude.cmd", [], environ={"COMSPEC": '"C:\\Win Dows\\cmd.exe"'})
    assert quoted.startswith('"C:\\Win Dows\\cmd.exe" /d /c ')
    assert windows_cmd_launch_line(r"C:\x\claude.cmd", [], environ={}).startswith(
        r'"C:\Windows\System32\cmd.exe" /d /c ')
    with pytest.raises(ClaudeError):
        windows_cmd_launch_line(r"C:\x\claude.cmd", [], environ={"COMSPEC": r"%SystemRoot%\cmd.exe"})


def test_windows_shim_rejects_unsafe_chars_and_doubles_trailing_backslash() -> None:
    with pytest.raises(ClaudeError):
        windows_shim_command_line(r"C:\a b\claude.cmd", ["--append-system-prompt", 'say "hi"'])
    with pytest.raises(ClaudeError):
        windows_shim_command_line(r"C:\a b\claude.cmd", ["--x", "100%"])
    with pytest.raises(ClaudeError):
        windows_shim_command_line(r"C:\a b\claude.cmd", ["line1\nline2"])
    line = windows_shim_command_line(r"C:\a b\claude.cmd", ["--add-dir", "C:\\repo\\"])
    assert line == '"C:\\a b\\claude.cmd" "--add-dir" "C:\\repo\\\\"'
    assert _split_quoted(line) == [r"C:\a b\claude.cmd", "--add-dir", "C:\\repo\\"]


def test_non_shim_paths_are_exec_d(tmp_path: Path) -> None:
    client = ClaudeCodeClient(claude_bin="claude")
    client._resolved = r"C:\Program Files\Claude\claude.exe"
    mode, spec = client.launch_spec(platform="win32")
    assert mode == "exec" and spec[0] == r"C:\Program Files\Claude\claude.exe"
    client._resolved = r"C:\npm\CLAUDE.BAT"
    assert client.launch_spec(platform="win32")[0] == "verbatim"
    client._resolved = "/usr/local/bin/claude"
    assert client.launch_spec(platform="linux") == ("exec", client.command())


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


# --------------------------------------------------------------------------- D2 reset times / auth detection

NOON_UTC = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc).timestamp()


@pytest.mark.parametrize(
    "message, expected",
    [
        ("You've hit your limit · resets 3pm (Europe/Berlin)", 2 * 3600),  # Berlin = 13:00 now
        ("5-hour limit reached ∙ resets 3pm", 3 * 3600),  # no tz → local (UTC here)
        ("resets 10:30pm", 10.5 * 3600),
        ("resets at 15:00", 3 * 3600),
        ("resets 11am", 23 * 3600),  # already past today → tomorrow
        ("resets 12am", 12 * 3600),  # midnight
        ("resets 12pm", 0),  # exactly now → already reset
        ("resets 11:57am", 0),  # within the 5-minute skew grace → already reset
        ("resets 3pm (Mars/Olympus_Mons)", 3 * 3600),  # unknown zone → local
        ("resets 9am (America/New_York)", 2 * 3600),  # NY = 07:00 now (EST)
    ],
)
def test_parse_reset_clock_times(message: str, expected: float) -> None:
    got = parse_retry_after(message, now=NOON_UTC, local_tz=timezone.utc)
    assert got == pytest.approx(expected)


@pytest.mark.parametrize(
    "message",
    ["Claude AI usage limit reached", "resets 13pm", "resets 25:00", "resets 5", "resets soon"],
)
def test_unparseable_reset_is_none(message: str) -> None:
    assert parse_retry_after(message, now=NOON_UTC, local_tz=timezone.utc) is None


def test_reset_across_dst_uses_wall_clock() -> None:
    # 2026-03-29 is the EU spring-forward day; 00:30 UTC = 01:30 CET. 3am CEST = 01:00 UTC.
    now = datetime(2026, 3, 29, 0, 30, tzinfo=timezone.utc).timestamp()
    assert parse_retry_after("resets 3am (Europe/Berlin)", now=now) == pytest.approx(30 * 60)


def test_usage_limit_reset_reaches_claude_rate_limited(tmp_path: Path) -> None:
    body = """
print(json.dumps({"type": "result", "is_error": True,
                  "result": "You've hit your limit \\u00b7 resets 3pm (Europe/Berlin)",
                  "usage": {"input_tokens": 0, "output_tokens": 0}}))
"""
    exe = make_fake_claude(tmp_path / "a", body)
    with pytest.raises(ClaudeRateLimited) as ei:
        run(ClaudeCodeClient(claude_bin=str(exe)).run("p", agent_id="a-1", role="builder", timeout=30))
    assert ei.value.retry_after is not None and 0 <= ei.value.retry_after <= 86400

    body2 = """
print(json.dumps({"type": "result", "is_error": True, "result": "Claude AI usage limit reached",
                  "usage": {"input_tokens": 0, "output_tokens": 0}}))
"""
    exe2 = make_fake_claude(tmp_path / "b", body2)
    with pytest.raises(ClaudeRateLimited) as ei2:
        run(ClaudeCodeClient(claude_bin=str(exe2)).run("p", agent_id="a-1", role="builder", timeout=30))
    assert ei2.value.retry_after is None  # worker falls back to FOUNDRY_LIMIT_COOLDOWN_S


def test_detect_auth_error() -> None:
    assert detect_auth_error("Invalid API key · Please run /login")
    assert detect_auth_error("OAuth token has expired")
    assert detect_auth_error('{"type":"authentication_error"}')
    assert detect_auth_error("Error: not logged in")
    assert not detect_auth_error("SyntaxError at line 4012")
    assert not detect_auth_error("rate limit exceeded")
    assert not detect_auth_error("")


# NOON_UTC is Saturday 2026-11-07 12:00 UTC (13:00 in Berlin).
@pytest.mark.parametrize(
    "message, expected",
    [
        ("You've hit your weekly limit · resets Nov 8, 3pm (Europe/Berlin)", 26 * 3600),
        ("resets Nov 8 at 3pm (Europe/Berlin)", 26 * 3600),
        ("resets November 8th, 15:00", 27 * 3600),  # local = UTC
        ("resets 8 Nov 3pm", 27 * 3600),
        ("resets Sat 6pm", 6 * 3600),  # today, later
        ("resets Saturday 6pm", 6 * 3600),
        ("resets Sat 9am", 7 * 86400 - 3 * 3600),  # today but past → next week
        ("resets Sun 6pm (Europe/Berlin)", 29 * 3600),  # Sun 17:00 UTC
        ("resets Mon, 9:30am", (24 + 21.5) * 3600),
        ("resets Nov 6, 3pm", 0),  # date already passed → limit has reset
        ("resets Jan 2, 3pm", (23 + 31 + 2) * 86400 + 3 * 3600),  # Dec→Jan wrap: next year
    ],
)
def test_parse_weekly_reset_forms(message: str, expected: float) -> None:
    got = parse_retry_after(message, now=NOON_UTC, local_tz=timezone.utc)
    assert got == pytest.approx(expected)


@pytest.mark.parametrize("message", ["resets Feb 30, 3pm", "resets Nov 8", "resets Sat", "resets Sat 6"])
def test_weekly_unparseable_is_none(message: str) -> None:
    assert parse_retry_after(message, now=NOON_UTC, local_tz=timezone.utc) is None


# --------------------------------------------------------------------------- validate_launch / verbatim runner


def test_validate_launch_caches_spec_and_run_uses_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    exe = make_fake_claude(tmp_path, OK_BODY)
    client = ClaudeCodeClient(claude_bin=str(exe))
    mode, spec = client.validate_launch()
    assert mode == "exec" and spec == [str(exe), "-p", "--output-format", "json"]
    calls = []
    real = client.launch_spec

    def counting(**kw):
        calls.append(kw)
        return real(**kw)

    monkeypatch.setattr(client, "launch_spec", counting)
    for _ in range(2):
        assert not run(client.run("hi", agent_id="a-1", role="builder", timeout=30)).is_error
    assert calls == []  # spec built once, in validate_launch


def test_validate_launch_errors() -> None:
    with pytest.raises(ClaudeNotFound):
        ClaudeCodeClient(claude_bin="definitely-not-claude-xyz").validate_launch()
    bad = ClaudeCodeClient(claude_bin="claude", extra_args=("--append-system-prompt", "100% sure"))
    bad._resolved = r"C:\Users\John Smith\AppData\Roaming\npm\claude.cmd"
    with pytest.raises(ClaudeError) as ei:
        bad.validate_launch(platform="win32")
    assert "claude.exe" in str(ei.value)
    bad._resolved = "/usr/bin/claude"
    assert bad.validate_launch(platform="linux")[0] == "exec"  # fine off Windows
    nul = ClaudeCodeClient(claude_bin="claude", extra_args=("a\x00b",))
    nul._resolved = "/usr/bin/claude"
    with pytest.raises(ClaudeError):
        nul.validate_launch(platform="linux")


def _force_verbatim(client: ClaudeCodeClient) -> None:
    """Route the client through the thread/Popen runner (a list spec works on POSIX)."""
    client._spec = ("verbatim", client.command())


def test_verbatim_runner_success(tmp_path: Path) -> None:
    exe = make_fake_claude(tmp_path, OK_BODY)
    client = ClaudeCodeClient(claude_bin=str(exe))
    _force_verbatim(client)
    res = run(client.run("via popen", agent_id="a-1", role="builder", cwd=str(tmp_path), timeout=30))
    assert not res.is_error and res.text.startswith("echo:via popen")
    assert res.input_tokens == 110 and res.cache_read_tokens == 5


def test_verbatim_runner_timeout_kills_tree(tmp_path: Path) -> None:
    marker = tmp_path / "alive.txt"
    exe = make_fake_claude(tmp_path / "bin", f"time.sleep(4)\nopen({str(marker)!r}, 'w').write('x')\n")
    client = ClaudeCodeClient(claude_bin=str(exe))
    _force_verbatim(client)
    started = time.monotonic()
    res = run(client.run("p", agent_id="a-1", role="builder", timeout=0.5))
    assert res.is_error and (res.error or "").startswith("timeout")
    assert time.monotonic() - started < 3.5
    time.sleep(4.5 - (time.monotonic() - started))
    assert not marker.exists()


def test_verbatim_runner_cancellation_kills_child(tmp_path: Path) -> None:
    marker = tmp_path / "alive.txt"
    exe = make_fake_claude(tmp_path / "bin", f"time.sleep(3)\nopen({str(marker)!r}, 'w').write('x')\n")
    client = ClaudeCodeClient(claude_bin=str(exe))
    _force_verbatim(client)

    async def go() -> None:
        task = asyncio.create_task(client.run("p", agent_id="a-1", role="builder", timeout=30))
        await asyncio.sleep(0.5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(go())
    time.sleep(3.5)
    assert not marker.exists()


def test_verbatim_runner_spawn_error_mapped(tmp_path: Path) -> None:
    client = ClaudeCodeClient(claude_bin="x")
    client._resolved = str(tmp_path / "gone" / "claude")
    client._spec = ("verbatim", [client._resolved, "-p"])
    with pytest.raises(ClaudeNotFound):
        run(client.run("p", agent_id="a-1", role="builder", timeout=5))


# --------------------------------------------------------------------------- round 3 (G6)


def test_auth_error_message_does_not_repeat_excerpt(tmp_path: Path) -> None:
    body = """
sys.stderr.write("Invalid API key · Please run /login\\n")
print(json.dumps({"type": "result", "is_error": True, "result": "Invalid API key · Please run /login",
                  "usage": {"input_tokens": 0, "output_tokens": 0}}))
"""
    exe = make_fake_claude(tmp_path, body)
    with pytest.raises(ClaudeAuthError) as ei:
        run(ClaudeCodeClient(claude_bin=str(exe)).run("p", agent_id="a-1", role="builder", timeout=30))
    assert str(ei.value).count("Invalid API key") == 1


def test_join_unique() -> None:
    from foundry.claude_client import _join_unique

    assert _join_unique("abc", "abc", None, "") == "abc"
    assert _join_unique("abc"[:2], "abc") == "abc"  # truncated copy replaced by the full text
    assert _join_unique("abc", "ab") == "abc"
    assert _join_unique("err", "text", "stderr line") == "err text stderr line"


def test_verbatim_cancel_while_popen_runs_kills_child(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Cancellation lands while the thread is inside Popen: the thread must kill the child."""
    import subprocess as sp
    import threading

    marker = tmp_path / "alive.txt"
    exe = make_fake_claude(tmp_path / "bin", f"time.sleep(2)\nopen({str(marker)!r}, 'w').write('x')\n")
    client = ClaudeCodeClient(claude_bin=str(exe))
    _force_verbatim(client)
    real_popen = sp.Popen
    entered, go = threading.Event(), threading.Event()
    procs: list = []

    def slow_popen(*a, **k):
        entered.set()
        go.wait(5)
        p = real_popen(*a, **k)
        procs.append(p)
        return p

    monkeypatch.setattr(sp, "Popen", slow_popen)

    async def main() -> None:
        task = asyncio.create_task(client.run("p", agent_id="a-1", role="builder", timeout=30))
        while not entered.is_set():
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task  # coroutine saw no process yet
        go.set()

    run(main())  # loop shutdown waits for the worker thread
    assert len(procs) == 1
    assert procs[0].wait(timeout=5) != 0  # killed, not exited normally
    time.sleep(2.5)
    assert not marker.exists()


def test_verbatim_cancel_before_thread_starts_never_spawns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess as sp
    import threading

    exe = make_fake_claude(tmp_path, OK_BODY)
    client = ClaudeCodeClient(claude_bin=str(exe))
    _force_verbatim(client)
    spawned: list = []
    real_popen = sp.Popen
    monkeypatch.setattr(sp, "Popen", lambda *a, **k: spawned.append(a) or real_popen(*a, **k))
    gate = threading.Event()
    real_to_thread = asyncio.to_thread

    async def delayed_to_thread(fn, /, *args, **kwargs):
        def delayed():
            gate.wait(5)
            return fn(*args, **kwargs)
        return await real_to_thread(delayed)

    monkeypatch.setattr(asyncio, "to_thread", delayed_to_thread)

    async def main() -> None:
        task = asyncio.create_task(client.run("p", agent_id="a-1", role="builder", timeout=30))
        await asyncio.sleep(0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        gate.set()

    run(main())
    assert spawned == []
