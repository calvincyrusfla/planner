"""Claude clients for the Foundry: the protocol, the real headless Claude Code client
and a deterministic simulator.

The Foundry never talks to the Anthropic API. Builders, reviewers and the Floor
Manager run as **headless Claude Code** on the user's PC under their subscription::

    claude -p --output-format json   (prompt on stdin)

The prompt goes over stdin so that long prompts never hit the Windows
command-line length limit (about 32k characters), and so the prompt never shows
up in process listings. Prompts are never logged above DEBUG level, and even
then only their length.

Phase 1 runs everything against :class:`SimulatedClaudeClient`; the real
:class:`ClaudeCodeClient` is unit-tested against a fake ``claude`` executable.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

from foundry.schemas import ClaudeResult

__all__ = [
    "ClaudeClient",
    "ClaudeCodeClient",
    "ClaudeError",
    "ClaudeNotFound",
    "ClaudeRateLimited",
    "ClaudeRole",
    "SimulatedCall",
    "SimulatedClaudeClient",
    "detect_rate_limit",
    "parse_claude_json",
    "parse_retry_after",
    "resolve_claude_bin",
]

log = logging.getLogger(__name__)

ClaudeRole = Literal["builder", "reviewer", "manager"]

#: Longest stderr/stdout excerpt kept in ``ClaudeResult.error`` (never the prompt).
_ERROR_EXCERPT = 500


# --------------------------------------------------------------------------- errors


class ClaudeError(RuntimeError):
    """Base class for Claude client errors."""


class ClaudeNotFound(ClaudeError):
    """The ``claude`` executable could not be found on PATH."""


class ClaudeRateLimited(ClaudeError):
    """Claude Code reported a rate limit or a subscription usage limit.

    Attributes:
        retry_after: seconds to wait before retrying, when the message said so
            (``None`` when unknown; the rate limiter then uses its own backoff).
        message: the (short) error text from Claude Code.
    """

    def __init__(self, retry_after: float | None = None, message: str = "rate limited") -> None:
        super().__init__(message)
        self.retry_after = retry_after
        self.message = message

    def __repr__(self) -> str:
        return f"ClaudeRateLimited(retry_after={self.retry_after!r}, message={self.message!r})"


# --------------------------------------------------------------------------- protocol


@runtime_checkable
class ClaudeClient(Protocol):
    """Anything that can run one Claude turn for an agent."""

    async def run(
        self,
        prompt: str,
        *,
        agent_id: str,
        role: ClaudeRole,
        cwd: str | None = None,
        timeout: float = 600,
    ) -> ClaudeResult:
        """Run ``prompt`` as agent ``agent_id`` and return the parsed result.

        Raises:
            ClaudeRateLimited: on a rate limit / usage limit (callers back off).
        """
        ...


# --------------------------------------------------------------------------- helpers

_RATE_LIMIT_RE = re.compile(
    r"rate[\s_-]?limit|usage[\s_-]?limit|too many requests|overloaded|\b429\b|\b529\b"
    r"|limit reached",
    re.IGNORECASE,
)
_RETRY_SECONDS_RE = re.compile(
    r"retry[\s_-]?after[\"']?\s*[:=]?\s*[\"']?(\d+(?:\.\d+)?)\s*(ms|milliseconds?|s|sec|secs|seconds?|m|min|mins|minutes?)?",
    re.IGNORECASE,
)
_TRY_AGAIN_RE = re.compile(
    r"(?:try again|retry|resets?)\s+in\s+(\d+(?:\.\d+)?)\s*(ms|milliseconds?|s|sec|secs|seconds?|m|min|mins|minutes?|h|hours?)",
    re.IGNORECASE,
)
#: Claude Code's usage-limit message has the form "Claude AI usage limit reached|1700000000".
_EPOCH_RE = re.compile(r"\|\s*(\d{10})(?:\d{3})?\b")


def _unit_factor(unit: str | None) -> float:
    if not unit:
        return 1.0
    u = unit.lower()
    if u.startswith("ms") or u.startswith("milli"):
        return 0.001
    if u.startswith("h"):
        return 3600.0
    if u.startswith("m"):
        return 60.0
    return 1.0


def parse_retry_after(text: str, *, now: float | None = None) -> float | None:
    """Pull a retry delay (seconds) out of a rate-limit message, or ``None``.

    Understands ``retry after 30`` / ``retry-after: 2.5s`` / ``try again in 5 minutes``
    and Claude Code's ``usage limit reached|<unix epoch>`` form (converted to the
    seconds remaining from ``now``, never negative).
    """
    if not text:
        return None
    m = _RETRY_SECONDS_RE.search(text) or _TRY_AGAIN_RE.search(text)
    if m:
        return float(m.group(1)) * _unit_factor(m.group(2))
    m = _EPOCH_RE.search(text)
    if m:
        current = time.time() if now is None else now
        return max(0.0, float(m.group(1)) - current)
    return None


def detect_rate_limit(text: str) -> bool:
    """True when ``text`` looks like a rate-limit / usage-limit / overload error."""
    return bool(text) and _RATE_LIMIT_RE.search(text) is not None


def resolve_claude_bin(claude_bin: str = "claude", *, platform: str | None = None) -> str:
    """Return the full path of the Claude Code executable.

    ``claude_bin`` may be a bare name (looked up on PATH) or a path. On Windows the
    npm install creates ``claude.cmd`` (and sometimes ``claude.exe``/``claude.ps1``),
    which a bare ``claude`` lookup through ``CreateProcess`` does not find, so the
    ``.cmd``/``.exe``/``.bat`` variants are tried explicitly.

    Raises:
        ClaudeNotFound: when nothing matches.
    """
    plat = sys.platform if platform is None else platform
    candidates = [claude_bin]
    if plat.startswith("win") and not Path(claude_bin).suffix:
        candidates += [claude_bin + ext for ext in (".cmd", ".exe", ".bat")]
    for cand in candidates:
        found = shutil.which(cand)
        if found:
            return found
        p = Path(cand)
        if (p.is_absolute() or p.parent != Path(".")) and p.is_file():
            return str(p)
    raise ClaudeNotFound(
        f"Claude Code executable {claude_bin!r} not found on PATH. Install Claude Code "
        "(npm install -g @anthropic-ai/claude-code) or set claude_bin to its full path."
    )


def _int(value: object) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def parse_claude_json(stdout: str) -> dict:
    """Parse Claude Code's ``--output-format json`` stdout into a dict.

    Normally stdout is a single JSON object. Some versions print a JSON array of
    events, or extra lines; in those cases the last object with ``type == "result"``
    (or the last JSON object) wins.

    Raises:
        ValueError: when no JSON object can be found.
    """
    text = stdout.strip()
    if not text:
        raise ValueError("empty output from claude")
    candidates: list[object] = []
    try:
        candidates.append(json.loads(text))
    except json.JSONDecodeError:
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("{") or line.startswith("["):
                try:
                    candidates.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    objects: list[dict] = []
    for c in candidates:
        if isinstance(c, dict):
            objects.append(c)
        elif isinstance(c, list):
            objects.extend(x for x in c if isinstance(x, dict))
    if not objects:
        raise ValueError("no JSON object in claude output")
    results = [o for o in objects if o.get("type") == "result"]
    return (results or objects)[-1]


def _result_from_payload(payload: dict, wall_seconds: float) -> ClaudeResult:
    usage = payload.get("usage") or {}
    if not isinstance(usage, dict):
        usage = {}
    # Cached prompt tokens are still input the model read; count them for burn-rate.
    input_tokens = (
        _int(usage.get("input_tokens"))
        + _int(usage.get("cache_creation_input_tokens"))
        + _int(usage.get("cache_read_input_tokens"))
    )
    duration_ms = payload.get("duration_ms")
    duration = _int(duration_ms) / 1000.0 if duration_ms is not None else wall_seconds
    cost = payload.get("total_cost_usd", payload.get("cost_usd"))
    try:
        cost_usd = float(cost) if cost is not None else None
    except (TypeError, ValueError):
        cost_usd = None
    result_text = payload.get("result")
    text = result_text if isinstance(result_text, str) else ""
    is_error = bool(payload.get("is_error", False)) or str(payload.get("subtype", "success")).startswith("error")
    error: str | None = None
    if is_error:
        error = (text or str(payload.get("subtype") or "claude reported an error"))[:_ERROR_EXCERPT]
    return ClaudeResult(
        text=text,
        input_tokens=input_tokens,
        output_tokens=_int(usage.get("output_tokens")),
        duration_seconds=duration,
        cost_usd=cost_usd,
        is_error=is_error,
        error=error,
    )


# --------------------------------------------------------------------------- real client


class ClaudeCodeClient:
    """Runs headless Claude Code (``claude -p --output-format json``) per call.

    Args:
        claude_bin: executable name or path (``claude``; ``claude.cmd`` is found on Windows).
        extra_args: extra CLI flags appended after the fixed ones, e.g.
            ``("--permission-mode", "acceptEdits")`` or ``("--model", "sonnet")``.
        env: extra environment variables for the child (merged over ``os.environ``).

    Behaviour:
        * the prompt is written to stdin, never put on the command line or logged;
        * on timeout the whole process tree is killed and an error result returned;
        * a rate-limit / usage-limit error raises :class:`ClaudeRateLimited`;
        * other failures (non-zero exit, unparseable output) return a
          ``ClaudeResult`` with ``is_error=True`` so workers can retry or block.
    """

    def __init__(
        self,
        claude_bin: str = "claude",
        extra_args: Sequence[str] = (),
        *,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.claude_bin = claude_bin
        self.extra_args = tuple(extra_args)
        self.env = dict(env) if env else None
        self._resolved: str | None = None

    def command(self) -> list[str]:
        """The argv used for each call (without the prompt, which goes to stdin)."""
        if self._resolved is None:
            self._resolved = resolve_claude_bin(self.claude_bin)
        exe = self._resolved
        argv = [exe, "-p", "--output-format", "json", *self.extra_args]
        if sys.platform.startswith("win") and Path(exe).suffix.lower() in (".cmd", ".bat"):
            # Batch shims must go through cmd.exe; arguments are fixed flags only
            # (the prompt is on stdin), so there is no injection surface here.
            argv = [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c", *argv]
        return argv

    async def run(
        self,
        prompt: str,
        *,
        agent_id: str,
        role: ClaudeRole,
        cwd: str | None = None,
        timeout: float = 600,
    ) -> ClaudeResult:
        """Run one headless Claude Code turn. See the class docstring."""
        argv = self.command()
        log.info("claude call agent=%s role=%s cwd=%s", agent_id, role, cwd or ".")
        log.debug("claude prompt length=%d chars", len(prompt))
        env = {**os.environ, **self.env} if self.env else None
        kwargs: dict = {}
        if sys.platform.startswith("win"):
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            kwargs["start_new_session"] = True
        started = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=env,
            **kwargs,
        )
        try:
            out_b, err_b = await asyncio.wait_for(
                proc.communicate(prompt.encode("utf-8")), timeout=timeout
            )
        except asyncio.TimeoutError:
            await self._kill(proc)
            log.warning("claude call agent=%s timed out after %.0fs; killed", agent_id, timeout)
            return ClaudeResult(
                text="",
                input_tokens=0,
                output_tokens=0,
                duration_seconds=time.monotonic() - started,
                cost_usd=None,
                is_error=True,
                error=f"timeout after {timeout:g}s",
            )
        except BaseException:
            await self._kill(proc)
            raise
        wall = time.monotonic() - started
        stdout = out_b.decode("utf-8", errors="replace")
        stderr = err_b.decode("utf-8", errors="replace")

        try:
            payload = parse_claude_json(stdout)
        except ValueError:
            payload = None

        if payload is not None:
            result = _result_from_payload(payload, wall)
            if result.is_error:
                blob = " ".join(filter(None, [result.error, result.text, stderr]))
                if detect_rate_limit(blob):
                    raise ClaudeRateLimited(parse_retry_after(blob), (result.error or "rate limited")[:_ERROR_EXCERPT])
            elif proc.returncode not in (0, None):
                result = result.model_copy(
                    update={"is_error": True, "error": f"claude exited {proc.returncode}: {stderr.strip()[:_ERROR_EXCERPT]}"}
                )
            log.info(
                "claude done agent=%s in=%d out=%d err=%s",
                agent_id, result.input_tokens, result.output_tokens, result.is_error,
            )
            return result

        blob = f"{stderr}\n{stdout}"
        if detect_rate_limit(blob):
            raise ClaudeRateLimited(parse_retry_after(blob), blob.strip()[:_ERROR_EXCERPT])
        excerpt = (stderr.strip() or stdout.strip())[:_ERROR_EXCERPT]
        return ClaudeResult(
            text="",
            input_tokens=0,
            output_tokens=0,
            duration_seconds=wall,
            cost_usd=None,
            is_error=True,
            error=f"claude exited {proc.returncode} without JSON output: {excerpt}",
        )

    @staticmethod
    async def _kill(proc: asyncio.subprocess.Process) -> None:
        """Kill the child and everything it started (npm shims spawn node)."""
        if proc.returncode is not None:
            return
        try:
            if sys.platform.startswith("win"):
                killer = await asyncio.create_subprocess_exec(
                    "taskkill", "/T", "/F", "/PID", str(proc.pid),
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                )
                await killer.wait()
            else:
                os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:  # pragma: no cover - extremely unlikely
            pass


# --------------------------------------------------------------------------- simulator


@dataclass(frozen=True)
class SimulatedCall:
    """One recorded call to :class:`SimulatedClaudeClient` (prompt kept for tests)."""

    agent_id: str
    role: str
    prompt: str
    cwd: str | None


@dataclass
class _Injection:
    kind: Literal["rate_limit", "failure", "exception"]
    agent_id: str | None
    remaining: int
    retry_after: float | None = None
    error: str = "simulated failure"
    exc: BaseException | None = None


ScriptFn = Callable[[str, str], "ClaudeResult | str"]


def _estimate_tokens(text: str) -> int:
    return math.ceil(len(text) / 4)


class SimulatedClaudeClient:
    """Deterministic stand-in for Claude used throughout phase 1 and the tests.

    Args:
        script: ``script(prompt, agent_id) -> ClaudeResult | str``. A plain string
            is wrapped into a ``ClaudeResult`` with token counts estimated as
            ``ceil(len/4)``. When ``None``, a fixed reply naming the role/agent is used.
        latency: seconds to ``asyncio.sleep`` per call (0 still yields once, so
            concurrent callers interleave deterministically).

    Every call, including ones that end in an injected rate limit or failure, is
    counted in ``calls[agent_id]`` and appended to ``history``. Idle builders must
    show ``calls[agent_id] == 0``.
    """

    def __init__(self, script: ScriptFn | None = None, *, latency: float = 0.0) -> None:
        self.script = script
        self.latency = latency
        self.calls: Counter[str] = Counter()
        self.role_calls: Counter[str] = Counter()
        self.history: list[SimulatedCall] = []
        self.in_flight = 0
        self.max_in_flight = 0
        self._replies: dict[str | None, list[ClaudeResult | str]] = {}
        self._injections: list[_Injection] = []

    # ---- scripting -----------------------------------------------------------

    def queue_reply(self, reply: ClaudeResult | str, *, agent_id: str | None = None) -> None:
        """Queue a one-shot reply for ``agent_id`` (or for any agent when ``None``).

        Queued replies are used before ``script``; agent-specific ones first.
        """
        self._replies.setdefault(agent_id, []).append(reply)

    def inject_rate_limit(
        self, *, agent_id: str | None = None, times: int = 1, retry_after: float | None = None
    ) -> None:
        """Make the next ``times`` calls (for ``agent_id`` or anyone) raise ClaudeRateLimited."""
        self._injections.append(_Injection("rate_limit", agent_id, times, retry_after=retry_after))

    def inject_failure(
        self, *, agent_id: str | None = None, times: int = 1, error: str = "simulated failure"
    ) -> None:
        """Make the next ``times`` calls return ``ClaudeResult(is_error=True, error=error)``."""
        self._injections.append(_Injection("failure", agent_id, times, error=error))

    def inject_exception(
        self, exc: BaseException, *, agent_id: str | None = None, times: int = 1
    ) -> None:
        """Make the next ``times`` calls raise ``exc`` (e.g. to simulate a crash)."""
        self._injections.append(_Injection("exception", agent_id, times, exc=exc))

    def prompts_for(self, agent_id: str) -> list[str]:
        """All prompts agent ``agent_id`` sent, in order."""
        return [c.prompt for c in self.history if c.agent_id == agent_id]

    @property
    def total_calls(self) -> int:
        """Total number of calls across all agents."""
        return sum(self.calls.values())

    def reset(self) -> None:
        """Forget counters, history, queued replies and injections."""
        self.calls.clear()
        self.role_calls.clear()
        self.history.clear()
        self._replies.clear()
        self._injections.clear()
        self.max_in_flight = 0

    # ---- ClaudeClient --------------------------------------------------------

    def _take_injection(self, agent_id: str) -> _Injection | None:
        for inj in self._injections:
            if inj.remaining > 0 and inj.agent_id in (None, agent_id):
                inj.remaining -= 1
                if inj.remaining == 0:
                    self._injections.remove(inj)
                return inj
        return None

    def _take_reply(self, agent_id: str) -> ClaudeResult | str | None:
        for key in (agent_id, None):
            queue = self._replies.get(key)
            if queue:
                return queue.pop(0)
        return None

    @staticmethod
    def _wrap(reply: ClaudeResult | str, prompt: str) -> ClaudeResult:
        if isinstance(reply, ClaudeResult):
            return reply
        return ClaudeResult(
            text=reply,
            input_tokens=_estimate_tokens(prompt),
            output_tokens=_estimate_tokens(reply),
            duration_seconds=0.0,
            cost_usd=None,
            is_error=False,
            error=None,
        )

    async def run(
        self,
        prompt: str,
        *,
        agent_id: str,
        role: ClaudeRole,
        cwd: str | None = None,
        timeout: float = 600,
    ) -> ClaudeResult:
        """Record the call, apply injections, then return the scripted reply."""
        self.calls[agent_id] += 1
        self.role_calls[role] += 1
        self.history.append(SimulatedCall(agent_id, role, prompt, cwd))
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(self.latency)
            inj = self._take_injection(agent_id)
            if inj is not None:
                if inj.kind == "rate_limit":
                    raise ClaudeRateLimited(inj.retry_after, "simulated rate limit (429)")
                if inj.kind == "exception" and inj.exc is not None:
                    raise inj.exc
                return ClaudeResult(
                    text="",
                    input_tokens=_estimate_tokens(prompt),
                    output_tokens=0,
                    duration_seconds=0.0,
                    cost_usd=None,
                    is_error=True,
                    error=inj.error,
                )
            reply = self._take_reply(agent_id)
            if reply is None:
                if self.script is not None:
                    reply = self.script(prompt, agent_id)
                else:
                    reply = f"SIMULATED {role} reply for {agent_id}"
            return self._wrap(reply, prompt)
        finally:
            self.in_flight -= 1
