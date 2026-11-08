"""Dispatch engineering work to Foundry through the shared Agent Department board.

The Floor Manager (often a cloud Claude session that can reach only the shared
board, never the user's PC) routes work to Foundry like this:

1. :func:`dispatch_to_foundry` puts a **Foundry batch** ("sprint") on the board
   (board op ``dispatchBatch``).
2. Foundry on the user's PC (``foundry/board_dispatch.py``) claims batches for
   its repo, submits each subtask as a normal Foundry order and writes progress
   back onto the batch (``updateBatch``).
3. :func:`poll_foundry_sprint` (or :func:`wait_for_sprint`) reads the batch back
   as a :class:`SprintStatus`: per-subtask F-id, state, PR url and error.

Board transports, in order (:func:`default_board`):

1. the local dashboard: ``GET/POST http://127.0.0.1:4300/api/state|op``
   (``FOUNDRY_DASHBOARD_URL`` / ``DEPT_URL`` override); it forwards to the
   shared board itself when one is configured;
2. the shared board ("hub"): ``<DEPT_HUB_URL>/api/department`` with header
   ``x-edit-key`` from ``DEPT_HUB_KEY`` (in cloud sessions the network proxy
   injects the real key), or ``department/data/hub.json``; ``DEPT_HUB=off``
   disables it, exactly like ``department/lib/hub.mjs``;
3. the department CLI: ``node department/dept.mjs``.

Command line::

    python -m foundry.dispatch send --json batch.json [--wait]
    python -m foundry.dispatch poll S-001 [--json]
    python -m foundry.dispatch wait S-001 [--timeout 3600]
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

__all__ = [
    "MAX_SUBTASKS",
    "SubtaskPayload",
    "DispatchFoundryBatch",
    "SubtaskStatus",
    "SprintStatus",
    "BoardClient",
    "BoardError",
    "BoardUnavailable",
    "BoardOutcomeUnknown",
    "SprintNotFound",
    "DashboardBoard",
    "HubBoard",
    "CliBoard",
    "AutoBoard",
    "default_board",
    "hub_config",
    "dispatch_to_foundry",
    "poll_foundry_sprint",
    "wait_for_sprint",
    "main",
]

MAX_SUBTASKS = 20
MAX_TITLE = 200
MAX_DESCRIPTION = 20_000
MAX_FILES = 50
MAX_ACCEPTANCE = 50
MAX_GOAL = 20_000

DEFAULT_DASHBOARD_URL = "http://127.0.0.1:4300"
REPO_ROOT = Path(__file__).resolve().parent.parent
DEPT_CLI = REPO_ROOT / "department" / "dept.mjs"
HUB_FILE = REPO_ROOT / "department" / "data" / "hub.json"

BatchStatus = Literal["queued", "claimed", "running", "done", "failed", "cancelled"]
SubtaskState = Literal["queued", "sizing", "building", "reviewing", "complete", "quarantined", "failed"]
TERMINAL_BATCH = frozenset({"done", "failed", "cancelled"})
TERMINAL_SUBTASK = frozenset({"complete", "quarantined", "failed"})

_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_KEY_RE = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")
_SPRINT_RE = re.compile(r"^S-\d{1,6}$")


# --------------------------------------------------------------------------- models


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


def _check_path(p: str) -> str:
    p = p.strip().replace("\\", "/")
    if not p:
        raise ValueError("empty file path")
    if p.startswith("/") or re.match(r"^[A-Za-z]:", p):
        raise ValueError(f"file path must be relative: {p!r}")
    if ".." in p.split("/"):
        raise ValueError(f"file path must not contain '..': {p!r}")
    if len(p) > 500:
        raise ValueError("file path too long")
    return p


class SubtaskPayload(_Model):
    """One unit of work; Foundry turns it into one work order (one F-task, one PR)."""

    key: str | None = None
    title: str = Field(min_length=1, max_length=MAX_TITLE)
    description: str = Field(default="", max_length=MAX_DESCRIPTION)
    files: list[str] = Field(default_factory=list, max_length=MAX_FILES)
    size: Literal["S", "M", "L", "XL"] | None = None
    acceptance_criteria: list[str] = Field(default_factory=list, max_length=MAX_ACCEPTANCE)
    depends_on: list[str] = Field(default_factory=list, max_length=MAX_SUBTASKS)

    @field_validator("key")
    @classmethod
    def _key(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return None
        if not _KEY_RE.match(v):
            raise ValueError("key must be 1-40 letters, digits, '_', '.' or '-'")
        return v

    @field_validator("files")
    @classmethod
    def _files(cls, v: list[str]) -> list[str]:
        out: list[str] = []
        for p in v:
            p = _check_path(p)
            if p not in out:
                out.append(p)
        return out

    @field_validator("size", mode="before")
    @classmethod
    def _size(cls, v: Any) -> Any:
        return v.strip().upper() if isinstance(v, str) and v.strip() else (None if v == "" else v)

    @field_validator("acceptance_criteria")
    @classmethod
    def _acceptance(cls, v: list[str]) -> list[str]:
        out = [s.strip() for s in v if s and s.strip()]
        if any(len(s) > 2000 for s in out):
            raise ValueError("acceptance criterion too long (max 2000 chars)")
        return out

    def to_board(self) -> dict[str, Any]:
        """The subtask as the board's ``dispatchBatch`` op expects it (camelCase)."""
        d: dict[str, Any] = {"key": self.key, "title": self.title}
        if self.description:
            d["description"] = self.description
        if self.files:
            d["files"] = list(self.files)
        if self.size:
            d["size"] = self.size
        if self.acceptance_criteria:
            d["acceptance"] = list(self.acceptance_criteria)
        if self.depends_on:
            d["dependsOn"] = list(self.depends_on)
        return d


class DispatchFoundryBatch(_Model):
    """A batch ("sprint") of subtasks for Foundry to build in one repo."""

    title: str = Field(min_length=1, max_length=MAX_TITLE)
    goal: str = Field(default="", max_length=MAX_GOAL)
    repo: str
    base: str | None = None
    subtasks: list[SubtaskPayload] = Field(min_length=1, max_length=MAX_SUBTASKS)

    @field_validator("repo")
    @classmethod
    def _repo(cls, v: str) -> str:
        v = v.strip()
        if not _REPO_RE.match(v):
            from foundry.git_publisher import github_repo

            slug = github_repo(v)
            if not slug or not _REPO_RE.match(slug):
                raise ValueError("repo must be 'owner/name' (or a github.com remote URL)")
            v = slug
        return v

    @field_validator("base")
    @classmethod
    def _base(cls, v: str | None) -> str | None:
        if not v:
            return None
        if len(v) > 200 or not re.match(r"^[A-Za-z0-9._/-]+$", v) or ".." in v or v.startswith(("-", "/")):
            raise ValueError(f"invalid base branch: {v!r}")
        return v

    @model_validator(mode="after")
    def _keys_and_deps(self) -> DispatchFoundryBatch:
        used = {s.key for s in self.subtasks if s.key}
        n = 0
        for s in self.subtasks:
            if s.key is None:
                n += 1
                while str(n) in used:
                    n += 1
                s.key = str(n)
                used.add(s.key)
        keys = [s.key for s in self.subtasks]
        if len(set(keys)) != len(keys):
            raise ValueError("subtask keys must be unique")
        known = set(keys)
        for s in self.subtasks:
            for d in s.depends_on:
                if d not in known:
                    raise ValueError(f"subtask {s.key!r} depends on unknown key {d!r}")
                if d == s.key:
                    raise ValueError(f"subtask {s.key!r} depends on itself")
        # reject dependency cycles
        deps = {s.key: set(s.depends_on) for s in self.subtasks}
        done: set[str] = set()
        while len(done) < len(deps):
            ready = [k for k, ds in deps.items() if k not in done and ds <= done]
            if not ready:
                raise ValueError("subtask dependencies form a cycle")
            done.update(k for k in ready if k is not None)
        return self

    def to_board_args(self, by: str | None = None) -> dict[str, Any]:
        """Arguments of the board op ``dispatchBatch``."""
        args: dict[str, Any] = {"title": self.title, "repo": self.repo, "subtasks": [s.to_board() for s in self.subtasks]}
        if self.goal:
            args["goal"] = self.goal
        if self.base:
            args["base"] = self.base
        if by:
            args["by"] = by
        return args


class SubtaskStatus(_Model):
    model_config = ConfigDict(extra="ignore")

    key: str
    title: str = ""
    foundry_task: str | None = None
    state: str = "queued"
    pr_url: str | None = None
    pr_state: str | None = None
    pr_branch: str | None = None
    pr_error: str | None = None
    error: str | None = None
    updated_at: str | None = None

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_SUBTASK


class SprintStatus(_Model):
    """A batch as read back from the board."""

    model_config = ConfigDict(extra="ignore")

    sprint_id: str
    title: str = ""
    goal: str = ""
    repo: str = ""
    base: str | None = None
    status: str = "queued"
    created_at: str | None = None
    created_by: str | None = None
    claimed_by: dict[str, Any] | None = None
    heartbeat_at: str | None = None
    subtasks: list[SubtaskStatus] = Field(default_factory=list)

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_BATCH

    @property
    def succeeded(self) -> bool:
        return self.status == "done"

    @property
    def completed(self) -> int:
        return sum(1 for s in self.subtasks if s.state == "complete")

    @classmethod
    def from_board(cls, batch: dict[str, Any]) -> SprintStatus:
        raw_results = batch.get("results")
        results: dict[str, Any] = raw_results if isinstance(raw_results, dict) else {}
        subs: list[SubtaskStatus] = []
        for i, st in enumerate(batch.get("subtasks") or []):
            if not isinstance(st, dict):
                continue
            key = str(st.get("key") or i + 1)
            raw_r = results.get(key)
            r: dict[str, Any] = raw_r if isinstance(raw_r, dict) else {}
            raw_pr = r.get("pr")
            pr: dict[str, Any] = raw_pr if isinstance(raw_pr, dict) else {}
            subs.append(SubtaskStatus(
                key=key, title=str(st.get("title") or ""),
                foundry_task=r.get("foundryTask") or None, state=str(r.get("state") or "queued"),
                pr_url=pr.get("url") or None, pr_state=pr.get("state") or None,
                pr_branch=pr.get("branch") or None, pr_error=pr.get("error") or None,
                error=r.get("error") or None, updated_at=r.get("updatedAt") or None,
            ))
        claimed = batch.get("claimedBy") if isinstance(batch.get("claimedBy"), dict) else None
        return cls(
            sprint_id=str(batch.get("id") or ""), title=str(batch.get("title") or ""),
            goal=str(batch.get("goal") or ""), repo=str(batch.get("repo") or ""),
            base=batch.get("base") or None, status=str(batch.get("status") or "queued"),
            created_at=batch.get("createdAt"), created_by=batch.get("createdBy"),
            claimed_by=claimed, heartbeat_at=batch.get("heartbeatAt"), subtasks=subs,
        )

    def summary(self) -> str:
        """Readable multi-line summary."""
        lines = [f"{self.sprint_id} {self.title} [{self.status}] repo {self.repo}"
                 + (f" base {self.base}" if self.base else "")
                 + f" — {self.completed}/{len(self.subtasks)} complete"]
        if self.claimed_by:
            lines.append(f"  claimed by Foundry on {self.claimed_by.get('host') or '?'}"
                         + (f", heartbeat {self.heartbeat_at}" if self.heartbeat_at else ""))
        elif not self.terminal:
            lines.append("  waiting for Foundry")
        for s in self.subtasks:
            bits = [f"  {s.key}. {s.title}", s.foundry_task or "-", s.state]
            if s.pr_url:
                bits.append(f"PR {s.pr_url}" + (f" ({s.pr_state})" if s.pr_state else ""))
            elif s.pr_state:
                bits.append(f"PR {s.pr_state}")
            if s.error or s.pr_error:
                bits.append(f"error: {s.error or s.pr_error}")
            lines.append(" · ".join(bits))
        return "\n".join(lines)


# --------------------------------------------------------------------------- board clients


class BoardError(RuntimeError):
    """The board refused an op or answered unexpectedly."""


class BoardUnavailable(BoardError):
    """This transport could not get an answer from the board.

    ``sent`` is False only when the request certainly never reached the board
    (connection refused, unknown host, no node/CLI): then another transport may
    safely be tried. When it is True the board may have applied the op.
    """

    def __init__(self, message: str, *, sent: bool = True) -> None:
        super().__init__(message)
        self.sent = sent


class SprintNotFound(BoardError):
    """The board has no batch with that id."""


class BoardOutcomeUnknown(BoardError):
    """An op may have reached the board but no answer came back; it was not resent."""


def _not_sent(exc: BaseException) -> bool:
    """True when a urllib/socket error means the request never left this machine."""
    reason = getattr(exc, "reason", exc)
    if isinstance(reason, (ConnectionRefusedError, socket.gaierror)):
        return True
    return isinstance(reason, OSError) and reason.errno in (
        errno.ECONNREFUSED, errno.EHOSTUNREACH, errno.ENETUNREACH, errno.EADDRNOTAVAIL)


class BoardClient(Protocol):
    def state(self) -> dict[str, Any]: ...

    def op(self, type: str, args: dict[str, Any]) -> Any: ...


def _ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    for var in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "NODE_EXTRA_CA_CERTS"):
        f = os.environ.get(var)
        if f and Path(f).is_file():
            try:
                ctx.load_verify_locations(f)
            except (OSError, ssl.SSLError):
                pass
    return ctx


def _request(opener: urllib.request.OpenerDirector, url: str, *, method: str, body: Any = None,
             headers: dict[str, str] | None = None, timeout: float) -> dict[str, Any]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"content-type": "application/json", **(headers or {})})
    try:
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read() or b"{}")
        except ValueError:
            payload = {}
        if exc.code >= 500 and not payload:
            raise BoardUnavailable(f"{url}: HTTP {exc.code}", sent=True) from exc
        if exc.code == 404 and not payload.get("error"):
            # No such route: nothing was applied, another transport may be tried.
            raise BoardUnavailable(f"{url}: not found", sent=False) from exc
        raise BoardError(str(payload.get("error") or f"HTTP {exc.code}")) from exc
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise BoardUnavailable(f"{url}: {getattr(exc, 'reason', exc)}", sent=not _not_sent(exc)) from exc
    try:
        payload = json.loads(raw or b"{}")
    except ValueError as exc:
        raise BoardError(f"{url}: answer is not JSON") from exc
    if not isinstance(payload, dict):
        raise BoardError(f"{url}: unexpected answer")
    if payload.get("ok") is False:
        raise BoardError(str(payload.get("error") or "board refused the op"))
    return payload


class DashboardBoard:
    """The local dashboard (``department/server.mjs``): ``/api/state`` and ``/api/op``."""

    name = "dashboard"

    def __init__(self, url: str | None = None, timeout: float = 20.0) -> None:
        self.url = (url or os.environ.get("FOUNDRY_DASHBOARD_URL") or os.environ.get("DEPT_URL")
                    or DEFAULT_DASHBOARD_URL).rstrip("/")
        self.timeout = timeout
        # Loopback: never through an env proxy.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def state(self) -> dict[str, Any]:
        p = _request(self._opener, f"{self.url}/api/state", method="GET", timeout=min(self.timeout, 5.0))
        st = p.get("result")
        if not isinstance(st, dict):
            raise BoardError("dashboard /api/state gave no state")
        return st

    def op(self, type: str, args: dict[str, Any]) -> Any:
        return _request(self._opener, f"{self.url}/api/op", method="POST",
                        body={"type": type, "args": args}, timeout=self.timeout).get("result")


def hub_config(env: Mapping[str, str] | None = None, hub_file: Path = HUB_FILE) -> dict[str, str] | None:
    """``{url, key}`` of the shared board, or None (mirrors ``department/lib/hub.mjs``)."""
    env = os.environ if env is None else env
    if str(env.get("DEPT_HUB", "")).lower() == "off":
        return None
    if env.get("DEPT_HUB_URL"):
        return {"url": env["DEPT_HUB_URL"].strip().rstrip("/"), "key": env.get("DEPT_HUB_KEY", "")}
    try:
        cfg = json.loads(hub_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if isinstance(cfg, dict) and cfg.get("url"):
        return {"url": str(cfg["url"]).strip().rstrip("/"), "key": str(cfg.get("key") or "")}
    return None


class HubBoard:
    """The shared board: the Netlify function at ``<url>/api/department``."""

    name = "hub"

    def __init__(self, url: str, key: str = "", timeout: float = 20.0) -> None:
        self.url = url.rstrip("/")
        self.key = key
        self.timeout = timeout
        # Honours HTTPS_PROXY (cloud sessions) and the proxy CA bundle.
        self._opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=_ssl_context()))

    def _headers(self) -> dict[str, str]:
        # Empty key: in Claude cloud sessions the network proxy injects the real header.
        return {"x-edit-key": self.key or "proxy"}

    def state(self) -> dict[str, Any]:
        p = _request(self._opener, f"{self.url}/api/department", method="GET",
                     headers=self._headers(), timeout=self.timeout)
        st = p.get("state")
        if not isinstance(st, dict):
            raise BoardError("shared board gave no state")
        return st

    def op(self, type: str, args: dict[str, Any]) -> Any:
        return _request(self._opener, f"{self.url}/api/department", method="POST",
                        body={"type": type, "args": args}, headers=self._headers(),
                        timeout=self.timeout).get("result")


class CliBoard:
    """Fallback: ``node department/dept.mjs`` (which itself tries dashboard → hub → file)."""

    name = "cli"

    def __init__(self, cli: Path = DEPT_CLI, node: str = "node", timeout: float = 60.0) -> None:
        self.cli = cli
        self.node = node
        self.timeout = timeout

    def _run(self, *argv: str) -> str:
        node = shutil.which(self.node)
        if node is None or not self.cli.exists():
            raise BoardUnavailable("node or department/dept.mjs not available", sent=False)
        try:
            proc = subprocess.run([node, str(self.cli), *argv], capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=self.timeout,
                                  cwd=str(self.cli.parent.parent))
        except subprocess.TimeoutExpired as exc:
            raise BoardUnavailable(f"dept.mjs: no answer in {self.timeout:g}s", sent=True) from exc
        except (OSError, subprocess.SubprocessError) as exc:
            raise BoardUnavailable(f"dept.mjs: {exc}", sent=False) from exc
        if proc.returncode != 0:
            raise BoardError((proc.stderr or proc.stdout).strip() or f"dept.mjs exited {proc.returncode}")
        return proc.stdout

    def state(self) -> dict[str, Any]:
        try:
            st = json.loads(self._run("json"))
        except ValueError as exc:
            raise BoardError("dept.mjs json gave no JSON") from exc
        if not isinstance(st, dict):
            raise BoardError("dept.mjs json gave no state")
        return st

    def op(self, type: str, args: dict[str, Any]) -> Any:
        if type == "dispatchBatch":
            before = {b.get("id") for b in _batches(self.state())}
            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as fh:
                json.dump(args, fh)
                tmp = fh.name
            try:
                out = self._run("foundry", "dispatch", "--json", tmp)
            finally:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            m = re.search(r"\bS-\d+\b", out)
            for b in _batches(self.state()):
                if (m and b.get("id") == m.group(0)) or (not m and b.get("id") not in before):
                    return b
            raise BoardError("dept.mjs foundry dispatch: batch not found afterwards")
        if type == "cancelBatch":
            self._run("foundry", "cancel", str(args.get("id")))
            return _find_batch(self.state(), str(args.get("id")))
        raise BoardUnavailable(f"op {type} has no CLI equivalent", sent=False)


class AutoBoard:
    """Tries dashboard → hub → CLI; sticks with the first transport that answers."""

    name = "auto"

    def __init__(self, clients: list[Any] | None = None) -> None:
        if clients is None:
            clients = [DashboardBoard()]
            cfg = hub_config()
            if cfg:
                clients.append(HubBoard(cfg["url"], cfg["key"]))
            clients.append(CliBoard())
        self.clients = clients
        self.active: Any = None

    def _call(self, fn: Callable[[Any], Any], *, write: str | None = None) -> Any:
        order = [self.active] + [c for c in self.clients if c is not self.active] if self.active else self.clients
        errors: list[str] = []
        for c in order:
            try:
                r = fn(c)
            except BoardUnavailable as exc:
                name = getattr(c, "name", c)
                if write and exc.sent:
                    # The board may already have applied it (e.g. the hub timed out after
                    # writing): replaying it through another transport could apply it twice.
                    hint = ("check with `python -m foundry.dispatch poll <S-id>` or "
                            "`node department/dept.mjs foundry list` before retrying"
                            if write == "dispatchBatch" else "read the board before retrying")
                    raise BoardOutcomeUnknown(
                        f"{write} via {name}: {exc}; it may have reached the board and was not resent; {hint}"
                    ) from exc
                errors.append(f"{name}: {exc}")
                continue
            self.active = c
            return r
        raise BoardUnavailable("no board reachable (" + "; ".join(errors) + ")", sent=False)

    def state(self) -> dict[str, Any]:
        return self._call(lambda c: c.state())  # type: ignore[no-any-return]

    def op(self, type: str, args: dict[str, Any]) -> Any:
        return self._call(lambda c: c.op(type, args), write=type)


def default_board() -> AutoBoard:
    return AutoBoard()


# --------------------------------------------------------------------------- API


def _batches(state: dict[str, Any]) -> list[dict[str, Any]]:
    bs = state.get("foundryBatches") if isinstance(state, dict) else None
    return [b for b in bs if isinstance(b, dict)] if isinstance(bs, list) else []


def _find_batch(state: dict[str, Any], sprint_id: str) -> dict[str, Any] | None:
    for b in _batches(state):
        if str(b.get("id", "")).upper() == sprint_id.upper():
            return b
    return None


def _unwrap_batch(result: Any) -> dict[str, Any] | None:
    if isinstance(result, dict):
        if isinstance(result.get("batch"), dict):
            return result["batch"]  # type: ignore[no-any-return]
        if result.get("id") and "subtasks" in result:
            return result
    return None


def dispatch_to_foundry(batch: DispatchFoundryBatch | dict[str, Any], *, board: BoardClient | None = None,
                        by: str | None = None) -> SprintStatus:
    """Put ``batch`` on the board for Foundry; returns the new sprint (status ``queued``)."""
    if not isinstance(batch, DispatchFoundryBatch):
        batch = DispatchFoundryBatch.model_validate(batch)
    board = board or default_board()
    by = by or os.environ.get("FOUNDRY_DISPATCH_BY") or "floor-manager"
    result = board.op("dispatchBatch", batch.to_board_args(by=by))
    found = _unwrap_batch(result)
    if found is None and isinstance(result, dict) and result.get("id"):
        found = _find_batch(board.state(), str(result["id"]))
    if found is None:
        raise BoardError(f"dispatchBatch gave no batch: {result!r}"[:300])
    return SprintStatus.from_board(found)


def poll_foundry_sprint(sprint_id: str, *, board: BoardClient | None = None) -> SprintStatus:
    """Read a sprint's current status from the board."""
    sprint_id = sprint_id.strip()
    if not _SPRINT_RE.match(sprint_id.upper()):
        raise ValueError(f"not a sprint id: {sprint_id!r} (expected S-001)")
    board = board or default_board()
    found = _find_batch(board.state(), sprint_id)
    if found is None:
        raise SprintNotFound(f"sprint {sprint_id} not found on the board")
    return SprintStatus.from_board(found)


def wait_for_sprint(sprint_id: str, *, board: BoardClient | None = None, timeout: float = 3600.0,
                    interval: float = 15.0, on_update: Callable[[SprintStatus], None] | None = None,
                    sleep: Callable[[float], None] = time.sleep,
                    monotonic: Callable[[], float] = time.monotonic) -> SprintStatus:
    """Poll until the sprint is done/failed/cancelled or ``timeout`` seconds pass.

    Returns the last status seen (check ``.terminal``). ``on_update`` is called
    whenever the status changes. Board errors (outages, a busy hub answering
    409, a 502 with a JSON error) are retried until ``timeout``; an unknown
    sprint id raises :class:`SprintNotFound` at once.
    """
    board = board or default_board()
    deadline = monotonic() + timeout
    last: SprintStatus | None = None
    last_error: BoardError | None = None
    while True:
        try:
            cur = poll_foundry_sprint(sprint_id, board=board)
        except SprintNotFound:
            raise
        except BoardError as exc:
            cur, last_error = None, exc
        if cur is not None:
            if on_update and (last is None or cur.model_dump() != last.model_dump()):
                on_update(cur)
            last = cur
            if cur.terminal:
                return cur
        if monotonic() >= deadline:
            if last is None:
                raise BoardUnavailable(f"sprint {sprint_id}: no answer from the board within {timeout:g}s"
                                       + (f" (last error: {last_error})" if last_error else "")) from last_error
            return last
        sleep(max(0.0, min(interval, deadline - monotonic())))


# --------------------------------------------------------------------------- CLI


def main(argv: list[str] | None = None, *, board: BoardClient | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m foundry.dispatch",
                                 description="Send work to Foundry through the shared board and follow it.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("send", help="dispatch a batch from a JSON file ('-' = stdin)")
    s.add_argument("--json", dest="file", required=True)
    s.add_argument("--by", default=None)
    s.add_argument("--wait", action="store_true", help="wait until the sprint finishes")
    s.add_argument("--timeout", type=float, default=3600.0)
    s.add_argument("--interval", type=float, default=15.0)
    s.add_argument("--out-json", action="store_true", help="print the status as JSON")
    p = sub.add_parser("poll", help="show a sprint's status")
    p.add_argument("sprint_id")
    p.add_argument("--json", action="store_true")
    w = sub.add_parser("wait", help="wait until a sprint finishes")
    w.add_argument("sprint_id")
    w.add_argument("--timeout", type=float, default=3600.0)
    w.add_argument("--interval", type=float, default=15.0)
    w.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    def show(st: SprintStatus, as_json: bool) -> None:
        print(st.model_dump_json(indent=2) if as_json else st.summary())

    def progress(st: SprintStatus) -> None:
        print(f"{st.sprint_id}: {st.status} {st.completed}/{len(st.subtasks)} complete", file=sys.stderr)

    try:
        if a.cmd == "send":
            raw = sys.stdin.read() if a.file == "-" else Path(a.file).read_text(encoding="utf-8")
            st = dispatch_to_foundry(DispatchFoundryBatch.model_validate_json(raw), board=board, by=a.by)
            if a.wait:
                st = wait_for_sprint(st.sprint_id, board=board, timeout=a.timeout, interval=a.interval,
                                     on_update=progress)
            show(st, a.out_json)
            return 0 if not a.wait or st.succeeded else 1
        if a.cmd == "poll":
            show(poll_foundry_sprint(a.sprint_id, board=board), a.json)
            return 0
        st = wait_for_sprint(a.sprint_id, board=board, timeout=a.timeout, interval=a.interval, on_update=progress)
        show(st, a.json)
        return 0 if st.succeeded else 1
    except ValidationError as exc:
        print(f"invalid batch: {exc}", file=sys.stderr)
        return 2
    except (BoardError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
