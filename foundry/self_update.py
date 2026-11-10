"""Carry out a remote update request: pull the current branch and write settings.

``apply_update`` checks the requested settings first (so nothing changes when
they are bad), then requires a clean working tree on a branch, fetches the
branch from ``origin`` and merges it fast-forward only, and finally writes the
merged settings to the settings file.  It never raises: unexpected errors come
back as a ``failed`` result.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from . import config as _config
from .config import ENV_PREFIX, FoundryConfig

try:  # reuse the git env handling of base_snapshot / git_publisher when possible
    from .git_publisher import _GIT_LOCATION_VARS
except Exception:  # pragma: no cover - fallback when the module is unavailable
    _GIT_LOCATION_VARS = frozenset({
        "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_COMMON_DIR",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE",
    })

__all__ = ["UpdateResult", "apply_update"]

_TIMEOUT = 120.0
_NET_TIMEOUT = 300.0
_MAX_LISTED = 10


@dataclass
class UpdateResult:
    """Outcome of one ``apply_update`` call.

    ``status`` is ``"done"``, ``"refused"`` or ``"failed"``.
    """

    status: str
    commit_before: str | None
    commit_after: str | None
    message: str


class _GitError(Exception):
    """A git command could not be run or timed out."""


def _git_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _GIT_LOCATION_VARS}
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.setdefault("LC_ALL", "C")
    return env


async def _git(repo: Path, *args: str, timeout: float = _TIMEOUT) -> tuple[int, str, str]:
    """Run ``git <args>`` in ``repo``; return (returncode, stdout, stderr)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", *args,
            cwd=str(repo),
            env=_git_env(),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise _GitError(f"cannot run git: {exc}") from exc
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError as exc:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        try:
            await proc.wait()
        except Exception:
            pass
        raise _GitError(f"git {' '.join(args)} timed out after {timeout:g}s") from exc
    return (
        proc.returncode if proc.returncode is not None else -1,
        out.decode("utf-8", "replace"),
        err.decode("utf-8", "replace"),
    )


def _git_message(out: str, err: str) -> str:
    text = (err.strip() or out.strip())
    return text or "git failed"


def _check_settings(
    settings: Mapping[str, str], settings_path: Path
) -> tuple[dict[str, str] | None, str | None]:
    """Return (merged settings, None) or (None, error text)."""
    bad_keys = [str(k) for k in settings if not (isinstance(k, str) and k.startswith(ENV_PREFIX))]
    if bad_keys:
        return None, f"settings keys must start with {ENV_PREFIX}: {', '.join(bad_keys)}"
    bad_values = [k for k, v in settings.items() if not isinstance(v, str)]
    if bad_values:
        return None, f"settings values must be strings: {', '.join(bad_values)}"
    current = _config._read_settings_file(settings_path)
    merged = {**current, **dict(settings)}
    try:
        FoundryConfig.from_env(environ={**os.environ, **merged})
    except Exception as exc:
        return None, f"invalid settings: {exc}"
    return merged, None


async def _apply(
    repo: Path, settings: Mapping[str, str] | None, settings_path: Path
) -> UpdateResult:
    merged: dict[str, str] | None = None
    if settings is not None:
        merged, error = _check_settings(settings, settings_path)
        if error is not None:
            return UpdateResult("refused", None, None, error)

    rc, out, err = await _git(repo, "status", "--porcelain", "--untracked-files=no")
    if rc != 0:
        return UpdateResult("failed", None, None, f"git status failed: {_git_message(out, err)}")
    changed = [line[3:] if len(line) > 3 else line.strip() for line in out.splitlines() if line.strip()]
    if changed:
        listed = ", ".join(changed[:_MAX_LISTED])
        more = len(changed) - _MAX_LISTED
        if more > 0:
            listed += f" (+{more} more)"
        return UpdateResult("refused", None, None, f"folder has uncommitted changes: {listed}")

    rc, out, err = await _git(repo, "symbolic-ref", "--short", "-q", "HEAD")
    branch = out.strip()
    if rc != 0 or not branch:
        return UpdateResult("refused", None, None, "detached HEAD")

    rc, out, err = await _git(repo, "rev-parse", "HEAD")
    if rc != 0:
        return UpdateResult("failed", None, None, f"git rev-parse failed: {_git_message(out, err)}")
    before = out.strip()

    rc, out, err = await _git(repo, "fetch", "origin", branch, timeout=_NET_TIMEOUT)
    if rc != 0:
        return UpdateResult("failed", before, before, f"git fetch failed: {_git_message(out, err)}")

    rc, out, err = await _git(repo, "merge", "--ff-only", f"origin/{branch}")
    if rc != 0:
        return UpdateResult(
            "refused", before, before, f"not a fast-forward: {_git_message(out, err)}"
        )

    rc, out, err = await _git(repo, "rev-parse", "HEAD")
    if rc != 0:
        return UpdateResult("failed", before, None, f"git rev-parse failed: {_git_message(out, err)}")
    after = out.strip()

    if merged is not None:
        try:
            settings_path.parent.mkdir(parents=True, exist_ok=True)
            settings_path.write_text(
                json.dumps(merged, sort_keys=True, indent=2) + "\n", encoding="utf-8"
            )
        except OSError as exc:
            return UpdateResult(
                "failed", before, after, f"pulled but could not write settings: {exc}"
            )

    if before == after:
        message = "already up to date"
    else:
        message = f"pulled {branch} {before[:7]}..{after[:7]}"
    if settings:
        message += "; settings: " + ", ".join(f"{k}={settings[k]}" for k in sorted(settings))
    return UpdateResult("done", before, after, message)


async def apply_update(
    repo: Path,
    settings: Mapping[str, str] | None = None,
    *,
    settings_file: Path | None = None,
) -> UpdateResult:
    """Pull the current branch of ``repo`` fast-forward only and write settings.

    Never raises; unexpected errors return a ``failed`` result.
    """
    settings_path = Path(settings_file) if settings_file is not None else _config.DEFAULT_SETTINGS_FILE
    try:
        return await _apply(Path(repo), settings, settings_path)
    except _GitError as exc:
        return UpdateResult("failed", None, None, str(exc))
    except Exception as exc:
        return UpdateResult("failed", None, None, f"unexpected error: {type(exc).__name__}: {exc}")
