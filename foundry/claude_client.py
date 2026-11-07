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
from datetime import datetime, timedelta, tzinfo
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from foundry.schemas import ClaudeResult

__all__ = [
    "ClaudeAuthError",
    "ClaudeClient",
    "ClaudeCodeClient",
    "ClaudeError",
    "ClaudeNotFound",
    "ClaudeRateLimited",
    "ClaudeRole",
    "SimulatedCall",
    "SimulatedClaudeClient",
    "detect_auth_error",
    "detect_rate_limit",
    "parse_claude_json",
    "parse_retry_after",
    "resolve_claude_bin",
    "windows_cmd_launch_line",
    "windows_shim_command_line",
]

log = logging.getLogger(__name__)

ClaudeRole = Literal["builder", "reviewer", "manager"]

#: Longest stderr/stdout excerpt kept in ``ClaudeResult.error`` (never the prompt).
_ERROR_EXCERPT = 500


# --------------------------------------------------------------------------- errors


class ClaudeError(RuntimeError):
    """Base class for Claude client errors."""


class ClaudeNotFound(ClaudeError):
    """The ``claude`` executable could not be found on PATH (or could not be started)."""


class ClaudeAuthError(ClaudeError):
    """Claude Code is not logged in / its credentials expired ("Please run /login").

    This is an infrastructure failure: the user must run ``claude`` once
    interactively and ``/login``. Workers must not spend a ticket attempt on it.
    """


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
            ClaudeAuthError: when Claude Code needs ``/login``.
            ClaudeNotFound: when the executable is missing or cannot be started.
        """
        ...


# --------------------------------------------------------------------------- helpers

_RATE_LIMIT_RE = re.compile(
    r"rate[\s_-]?limit|usage[\s_-]?limit|too many requests|overloaded|\b429\b|\b529\b"
    r"|\d+[\s-]?hour limit|weekly limit|session limit|hit your (?:usage )?limit",
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
_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_MONTH_ALT = "|".join(_MONTHS)
#: "resets 3pm (Europe/Berlin)", "resets 10:30pm", "resets at 15:00", and the weekly
#: forms "resets Nov 8, 3pm (Europe/Berlin)", "resets 8 Nov 3pm", "resets Sat 6pm".
#: A bare hour without am/pm or minutes is not accepted (too ambiguous).
_RESET_CLOCK_RE = re.compile(
    r"\bresets?\s+(?:on\s+)?"
    r"(?:(?P<wd>" + "|".join(_WEEKDAYS) + r")[a-z]*\.?,?\s+"
    r"|(?P<mon>" + _MONTH_ALT + r")[a-z]*\.?\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?,?\s+"
    r"|(?P<day2>\d{1,2})(?:st|nd|rd|th)?\s+(?P<mon2>" + _MONTH_ALT + r")[a-z]*\.?,?\s+)?"
    r"(?:at\s+)?(?P<h>\d{1,2})(?::(?P<mi>\d{2}))?\s*(?P<ap>[ap]\.?m\.?)?(?=[\s(.,;·∙|]|$)"
    r"(?:\s*\((?P<tz>[^)]+)\))?",
    re.IGNORECASE,
)
#: A reset time this far in the past is treated as "already reset" (clock skew).
_RESET_GRACE_S = 300.0
#: A month-day reset up to this far in the past already happened (not next year).
_RESET_PAST_DATE_S = 180 * 86400.0
_AUTH_RE = re.compile(
    r"/login\b|not logged in|invalid api key|authentication[_\s-]?error|oauth token (?:has )?expired"
    r"|credentials? (?:are |is )?(?:missing|invalid|expired)|\b401\b|unauthori[sz]ed",
    re.IGNORECASE,
)


def _unit_factor(unit: str | None) -> float:
    if not unit:
        return 1.0
    u = unit.lower()
    if u.startswith(("ms", "milli")):
        return 0.001
    if u.startswith("h"):
        return 3600.0
    if u.startswith("m"):
        return 60.0
    return 1.0


def _zone(name: str | None, local_tz: tzinfo | None) -> tzinfo:
    """``ZoneInfo(name)`` when it resolves, else ``local_tz`` / the machine's local zone.

    Windows has no system tz database; without the ``tzdata`` package an IANA name
    cannot be resolved and local time is used instead (usually the same zone).
    """
    if name:
        try:
            return ZoneInfo(name.strip())
        except (ZoneInfoNotFoundError, ValueError, OSError):
            log.debug("unknown time zone %r in reset message; using local time", name)
    if local_tz is not None:
        return local_tz
    return datetime.now().astimezone().tzinfo or ZoneInfo("UTC")


def _seconds_until_clock(
    hour: int,
    minute: int,
    meridiem: str | None,
    tz: tzinfo,
    now: float,
    *,
    weekday: int | None = None,
    month: int | None = None,
    day: int | None = None,
) -> float | None:
    """Seconds from ``now`` until the described reset, or ``None`` if it is invalid.

    * clock only → the next occurrence of that wall-clock time (today or tomorrow);
    * weekday → the next such weekday at that time (up to 7 days ahead);
    * month + day → that date this year; if it is already past (within ~6 months)
      the limit has reset (0), otherwise the date is next year (Dec → Jan wrap).

    Times up to five minutes in the past count as "already reset" (0) so clock
    skew never turns into a day/week-long wait.
    """
    if meridiem:
        if not 1 <= hour <= 12:
            return None
        pm = meridiem.lower().startswith("p")
        hour = (hour % 12) + (12 if pm else 0)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    now_dt = datetime.fromtimestamp(now, tz)
    naive_now = now_dt.replace(tzinfo=None)

    def at(d: datetime) -> float:
        # wall-clock arithmetic on naive values, then attach the zone (DST-safe)
        return d.replace(tzinfo=tz).timestamp() - now

    if month is not None and day is not None:
        for year in (naive_now.year, naive_now.year + 1):
            try:
                target = datetime(year, month, day, hour, minute)
            except ValueError:
                return None
            delta = at(target)
            if delta > -_RESET_GRACE_S:
                return max(0.0, delta)
            if -delta <= _RESET_PAST_DATE_S:
                return 0.0  # that date already passed: the limit has reset
        return None  # pragma: no cover - the next-year candidate is always ahead

    target = naive_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if weekday is not None:
        target += timedelta(days=(weekday - naive_now.weekday()) % 7)
        step = timedelta(days=7)
    else:
        step = timedelta(days=1)
    delta = at(target)
    if delta > -_RESET_GRACE_S:
        return max(0.0, delta)
    return max(0.0, at(target + step))


def parse_retry_after(
    text: str, *, now: float | None = None, local_tz: tzinfo | None = None
) -> float | None:
    """Pull a retry delay (seconds) out of a rate-limit / usage-limit message, or ``None``.

    Understands, in this order:

    * ``retry after 30`` / ``retry-after: 2.5s`` / ``try again in 5 minutes``;
    * Claude Code's ``usage limit reached|<unix epoch>`` form;
    * a reset clock time: ``resets 3pm (Europe/Berlin)``, ``resets 10:30pm``,
      ``resets at 15:00``, optionally with a date (weekly limits):
      ``resets Nov 8, 3pm (Europe/Berlin)``, ``resets 8 Nov 3pm``,
      ``resets Sat 6pm``. The IANA zone in parentheses is honoured via zoneinfo;
      without one (or when it can't be resolved) local time is used. See
      :func:`_seconds_until_clock` for how past times are handled.

    ``now`` is a Unix timestamp (defaults to ``time.time()``); ``local_tz`` overrides
    the machine's zone (tests). Results are never negative. ``None`` means "no
    usable time": the caller falls back to its configured cooldown
    (``FOUNDRY_LIMIT_COOLDOWN_S``).
    """
    if not text:
        return None
    current = time.time() if now is None else now
    m = _RETRY_SECONDS_RE.search(text) or _TRY_AGAIN_RE.search(text)
    if m:
        return float(m.group(1)) * _unit_factor(m.group(2))
    m = _EPOCH_RE.search(text)
    if m:
        return max(0.0, float(m.group(1)) - current)
    for m in _RESET_CLOCK_RE.finditer(text):
        g = m.groupdict()
        if g["mi"] is None and g["ap"] is None:
            continue
        mon = g["mon"] or g["mon2"]
        day = g["day"] or g["day2"]
        secs = _seconds_until_clock(
            int(g["h"]),
            int(g["mi"] or 0),
            g["ap"],
            _zone(g["tz"], local_tz),
            current,
            weekday=_WEEKDAYS.index(g["wd"][:3].lower()) if g["wd"] else None,
            month=_MONTHS.index(mon[:3].lower()) + 1 if mon else None,
            day=int(day) if day else None,
        )
        if secs is not None:
            return secs
    return None


def detect_auth_error(text: str) -> bool:
    """True when ``text`` says Claude Code is not logged in ("Please run /login")."""
    return bool(text) and _AUTH_RE.search(text) is not None


def detect_rate_limit(text: str) -> bool:
    """True when ``text`` looks like a rate-limit / usage-limit / overload error."""
    return bool(text) and _RATE_LIMIT_RE.search(text) is not None


def resolve_claude_bin(claude_bin: str = "claude", *, platform: str | None = None) -> str:
    """Return the full path of the Claude Code executable.

    ``claude_bin`` may be a bare name (looked up on PATH) or a path. On Windows the
    npm install creates an extensionless ``claude`` sh script (not runnable by
    ``CreateProcess``) next to ``claude.cmd``; the native installer creates
    ``claude.exe``. So for a name without a suffix ``.exe``, ``.cmd`` and ``.bat``
    are tried first and the bare name last.

    Raises:
        ClaudeNotFound: when nothing matches.
    """
    plat = sys.platform if platform is None else platform
    candidates = [claude_bin]
    if plat.startswith("win") and not Path(claude_bin).suffix:
        candidates = [claude_bin + ext for ext in (".exe", ".cmd", ".bat")] + [claude_bin]
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
            if line.startswith(("{", "[")):
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


def _make_result(**fields: object) -> ClaudeResult:
    """Build a ClaudeResult, dropping ``cache_read_tokens`` if the schema predates it."""
    if "cache_read_tokens" not in ClaudeResult.model_fields:
        fields.pop("cache_read_tokens", None)
    return ClaudeResult(**fields)  # type: ignore[arg-type]


#: Characters that cannot be passed safely through a ``.cmd`` shim even when quoted:
#: ``"`` ends cmd's quoting, ``%`` triggers variable expansion, newlines end the line.
_CMD_UNSAFE = frozenset('"%\r\n\x00')


def _auth_message(blob: str) -> str:
    excerpt = " ".join(blob.split())[:200]
    return (
        "Claude Code is not logged in (run `claude` once in a terminal and use /login): " + excerpt
    )


def windows_shim_command_line(exe: str, args: Sequence[str]) -> str:
    """The inner command string for a ``.cmd``/``.bat`` shim (what cmd.exe runs).

    Every token (the shim path too) is wrapped in double quotes, so spaces
    (``C:\\Users\\John Smith\\...``) and cmd metacharacters (``& | < > ^``) are
    inert. It is launched as ``"%COMSPEC%" /d /c "<this string>"`` (see
    :func:`windows_cmd_launch_line`); since the string contains more than two
    quotes, cmd strips exactly the outer pair and runs this string unchanged. A
    trailing backslash is doubled so it does not escape the closing quote for the
    CRT argument parser in node.

    Raises:
        ClaudeError: when a token contains ``"``, ``%`` or a newline, which cannot
            survive cmd.exe; use ``claude.exe`` or remove those characters.
    """
    parts: list[str] = []
    for tok in (exe, *args):
        bad = _CMD_UNSAFE.intersection(tok)
        if bad:
            raise ClaudeError(
                f"argument {tok[:40]!r} contains {''.join(sorted(bad))!r}, which cannot be passed "
                "through claude.cmd; install the native claude.exe or drop those characters"
            )
        if tok.endswith("\\"):
            tok += "\\" * (len(tok) - len(tok.rstrip("\\")))
        parts.append(f'"{tok}"')
    return " ".join(parts)


def _default_comspec(environ: Mapping[str, str]) -> str:
    root = environ.get("SystemRoot") or environ.get("SYSTEMROOT") or r"C:\Windows"
    return root.rstrip("\\") + r"\System32\cmd.exe"


def windows_cmd_launch_line(
    exe: str, args: Sequence[str], *, environ: Mapping[str, str] | None = None
) -> str:
    """The full, verbatim Windows command line for running a ``.cmd``/``.bat`` shim::

        "%COMSPEC%" /d /c ""C:\\Users\\John Smith\\...\\claude.cmd" "-p" ..."

    ``/d`` turns off cmd's AutoRun registry hook (which could change the working
    directory or print noise into the JSON on stdout). The string is passed to
    ``CreateProcess`` unchanged (no list2cmdline re-quoting).

    Raises:
        ClaudeError: on unsafe characters in any token or in ``COMSPEC``.
    """
    env = os.environ if environ is None else environ
    comspec = (env.get("COMSPEC") or env.get("ComSpec") or "").strip().strip('"') or _default_comspec(env)
    if _CMD_UNSAFE.intersection(comspec):
        raise ClaudeError(f"COMSPEC {comspec!r} contains characters that cannot be quoted")
    return f'"{comspec}" /d /c "{windows_shim_command_line(exe, args)}"'


def _result_from_payload(payload: dict, wall_seconds: float) -> ClaudeResult:
    usage = payload.get("usage") or {}
    if not isinstance(usage, dict):
        usage = {}
    # D11: input_tokens = uncached input + cache creation (what the rate limiter is
    # charged); cache reads are reported separately as cache_read_tokens.
    input_tokens = _int(usage.get("input_tokens")) + _int(usage.get("cache_creation_input_tokens"))
    cache_read_tokens = _int(usage.get("cache_read_input_tokens"))
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
    return _make_result(
        text=text,
        input_tokens=input_tokens,
        cache_read_tokens=cache_read_tokens,
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
        * on timeout the whole process tree is killed and an error result returned
          (``error`` starts with ``"timeout"``);
        * a rate-limit / usage-limit error raises :class:`ClaudeRateLimited`
          (``retry_after`` from the message, or ``None`` → caller's cooldown);
        * "Please run /login"-type failures raise :class:`ClaudeAuthError`;
        * a missing / unstartable executable raises :class:`ClaudeNotFound`
          (any other spawn ``OSError`` → :class:`ClaudeError`);
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
        self._spec: tuple[Literal["exec", "verbatim"], list[str] | str] | None = None

    def command(self) -> list[str]:
        """The logical argv used for each call (the prompt goes to stdin, not here)."""
        if self._resolved is None:
            self._resolved = resolve_claude_bin(self.claude_bin)
        return [self._resolved, "-p", "--output-format", "json", *self.extra_args]

    def launch_spec(
        self, *, platform: str | None = None
    ) -> tuple[Literal["exec", "verbatim"], list[str] | str]:
        """How the child is started: ``("exec", argv)`` or ``("verbatim", command_line)``.

        On Windows a ``.cmd``/``.bat`` shim (the npm install) cannot be exec'd
        directly. It is run as ``"%COMSPEC%" /d /c "<line>"`` (built by
        :func:`windows_cmd_launch_line`), handed to ``CreateProcess`` verbatim.
        Everything else (``claude.exe``, POSIX ``claude``) is exec'd with an argv.

        Raises:
            ClaudeNotFound: when the executable cannot be resolved.
            ClaudeError: when an argument cannot be passed safely.
        """
        plat = sys.platform if platform is None else platform
        argv = self.command()
        for tok in argv:
            if not isinstance(tok, str):
                raise ClaudeError(f"claude argument {tok!r} is not a string")
            if "\x00" in tok:
                raise ClaudeError(f"claude argument {tok[:40]!r} contains a NUL character")
        if plat.startswith("win") and Path(argv[0]).suffix.lower() in (".cmd", ".bat"):
            environ = {**os.environ, **(self.env or {})}
            return "verbatim", windows_cmd_launch_line(argv[0], argv[1:], environ=environ)
        return "exec", argv

    def validate_launch(
        self, *, platform: str | None = None
    ) -> tuple[Literal["exec", "verbatim"], list[str] | str]:
        """Resolve the executable and build the launch spec once (for the ``--real`` preflight).

        The spec is cached and reused by :meth:`run`, so a bad configuration fails
        before any ticket is claimed instead of on every call.

        Raises:
            ClaudeNotFound: Claude Code is not installed / not on PATH.
            ClaudeError: an extra argument cannot be passed (e.g. ``"`` or ``%``
                through ``claude.cmd``).
        """
        spec = self.launch_spec(platform=platform)
        if platform is None:
            self._spec = spec
        return spec

    def _spawn_error(self, exc: OSError, cwd: str | None) -> ClaudeError:
        if isinstance(exc, (FileNotFoundError, PermissionError)):
            if cwd is not None and not Path(cwd).is_dir():
                return ClaudeError(f"working directory {cwd!r} does not exist")
            return ClaudeNotFound(f"could not start Claude Code ({self._resolved or self.claude_bin!r}): {exc}")
        return ClaudeError(f"could not start Claude Code: {exc}")

    @staticmethod
    def _popen_flags() -> dict:
        if sys.platform.startswith("win"):
            return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
        return {"start_new_session": True}

    async def _execute(
        self, data: bytes, cwd: str | None, env: dict[str, str] | None, timeout: float
    ) -> tuple[int | None, bytes, bytes, bool]:
        """Run the child; returns ``(returncode, stdout, stderr, timed_out)``."""
        mode, spec = self._spec if self._spec is not None else self.validate_launch()
        if mode == "verbatim":
            return await self._execute_verbatim(spec, data, cwd, env, timeout)
        assert isinstance(spec, list)
        try:
            proc = await asyncio.create_subprocess_exec(
                *spec,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env,
                **self._popen_flags(),
            )
        except OSError as exc:
            raise self._spawn_error(exc, cwd) from exc
        try:
            out_b, err_b = await asyncio.wait_for(proc.communicate(data), timeout=timeout)
        except asyncio.TimeoutError:
            await self._kill(proc)
            return proc.returncode, b"", b"", True
        except BaseException:
            await self._kill(proc)
            raise
        return proc.returncode, out_b, err_b, False

    async def _execute_verbatim(
        self,
        spec: list[str] | str,
        data: bytes,
        cwd: str | None,
        env: dict[str, str] | None,
        timeout: float,
    ) -> tuple[int | None, bytes, bytes, bool]:
        """Run with :class:`subprocess.Popen` in a worker thread.

        asyncio's subprocess API always re-quotes argv (or wraps strings as
        ``cmd /c "..."`` without ``/d``), so the Windows shim line, which must
        reach ``CreateProcess`` verbatim, goes through plain Popen instead. A
        string is passed unchanged on Windows; a list (used by tests on POSIX) is
        exec'd normally.
        """
        holder: dict[str, subprocess.Popen[bytes]] = {}

        def work() -> tuple[int | None, bytes, bytes, bool]:
            proc = subprocess.Popen(  # noqa: S603 - fixed argv, prompt on stdin
                spec,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=cwd,
                env=env,
                **self._popen_flags(),
            )
            holder["proc"] = proc
            try:
                out_b, err_b = proc.communicate(data, timeout=timeout)
                return proc.returncode, out_b, err_b, False
            except subprocess.TimeoutExpired:
                _kill_tree_sync(proc)
                try:
                    proc.communicate(timeout=5)
                except subprocess.TimeoutExpired:  # pragma: no cover - pipes held open
                    pass
                return proc.returncode, b"", b"", True

        try:
            return await asyncio.to_thread(work)
        except OSError as exc:
            if "proc" not in holder:
                raise self._spawn_error(exc, cwd) from exc
            raise
        except BaseException:
            if "proc" in holder:
                _kill_tree_sync(holder["proc"])
            raise

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
        log.info("claude call agent=%s role=%s cwd=%s", agent_id, role, cwd or ".")
        log.debug("claude prompt length=%d chars", len(prompt))
        env = {**os.environ, **self.env} if self.env else None
        started = time.monotonic()
        returncode, out_b, err_b, timed_out = await self._execute(
            prompt.encode("utf-8"), cwd, env, timeout
        )
        if timed_out:
            log.warning("claude call agent=%s timed out after %.0fs; killed", agent_id, timeout)
            return _make_result(
                text="",
                input_tokens=0,
                output_tokens=0,
                duration_seconds=time.monotonic() - started,
                cost_usd=None,
                is_error=True,
                error=f"timeout after {timeout:g}s",
            )
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
                if detect_auth_error(blob):
                    raise ClaudeAuthError(_auth_message(blob))
                if detect_rate_limit(blob):
                    raise ClaudeRateLimited(parse_retry_after(blob), (result.error or "rate limited")[:_ERROR_EXCERPT])
            elif returncode not in (0, None):
                result = result.model_copy(
                    update={"is_error": True, "error": f"claude exited {returncode}: {stderr.strip()[:_ERROR_EXCERPT]}"}
                )
            log.info(
                "claude done agent=%s in=%d out=%d err=%s",
                agent_id, result.input_tokens, result.output_tokens, result.is_error,
            )
            return result

        blob = f"{stderr}\n{stdout}"
        if detect_auth_error(blob):
            raise ClaudeAuthError(_auth_message(blob))
        if detect_rate_limit(blob):
            raise ClaudeRateLimited(parse_retry_after(blob), blob.strip()[:_ERROR_EXCERPT])
        excerpt = (stderr.strip() or stdout.strip())[:_ERROR_EXCERPT]
        return _make_result(
            text="",
            input_tokens=0,
            output_tokens=0,
            duration_seconds=wall,
            cost_usd=None,
            is_error=True,
            error=f"claude exited {returncode} without JSON output: {excerpt}",
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


def _kill_tree_sync(proc: subprocess.Popen[bytes]) -> None:
    """Blocking variant of :meth:`ClaudeCodeClient._kill` for Popen children."""
    if proc.poll() is not None:
        return
    try:
        if sys.platform.startswith("win"):
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
            )
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        pass
    try:
        proc.kill()
    except OSError:
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
