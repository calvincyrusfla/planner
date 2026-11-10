"""Private, throwaway checkouts for one builder attempt.

A piece workspace is a detached ``git worktree`` of the commit checked out in a
base snapshot (:mod:`foundry.base_snapshot`). A builder edits files in it, then
:func:`collect_changes` turns those edits into a git-style diff for the existing
gates, review and PR path, and :func:`remove_workspace` throws the checkout away.

Workspace files are byte-identical to the commit, and the collected diff holds
exactly the bytes in the workspace: neither the user's ``core.autocrlf`` nor an
in-tree ``text eol=...`` attribute changes them. Without that, a CRLF blob under
``*.bat text eol=crlf`` would show up as a whole-file change nobody made.
"""

from __future__ import annotations

import asyncio
import logging
import os
import posixpath
import shutil
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .base_snapshot import (
    _EOL_CONFIG,
    _SHA_RE,
    _TIMEOUT,
    BaseSnapshots,
    SnapshotUnavailable,
    _rmtree,
)
from .git_publisher import _kill

log = logging.getLogger(__name__)

_GitResult = tuple["int | None", bytes, str]


class WorkspaceError(Exception):
    """A workspace operation failed; ``stage`` says which step."""

    def __init__(self, stage: str, message: str) -> None:
        super().__init__(message)
        self.stage = stage
        self.message = message


@dataclass
class WorkspaceChanges:
    """What a builder changed: the diff, every changed path, and those not allowed."""

    diff: str
    changed: list[str] = field(default_factory=list)
    outside: list[str] = field(default_factory=list)


async def _git(git: str, *args: str, cwd: Path) -> _GitResult:
    """Run git (no shell); stdout stays bytes because a diff must not be altered by decoding."""
    try:
        proc = await asyncio.create_subprocess_exec(
            git, *args, cwd=str(cwd), env=BaseSnapshots._env(),
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)
    except (OSError, ValueError) as exc:
        return 127, b"", str(exc)
    done = False
    try:
        out, err = await asyncio.wait_for(proc.communicate(), _TIMEOUT)
        done = True
    except asyncio.TimeoutError:
        return None, b"", f"timed out after {_TIMEOUT:.0f}s"
    finally:
        if not done:  # timeout or cancellation: never leave the child running
            _kill(proc)
            try:
                await asyncio.shield(asyncio.wait_for(proc.wait(), 10))
            except (asyncio.TimeoutError, asyncio.CancelledError, ProcessLookupError):
                pass
    return proc.returncode, out, err.decode("utf-8", errors="replace")


def _require_git() -> str:
    git = shutil.which("git")
    if not git:
        raise WorkspaceError("no_git", "git is not installed")
    return git


def _detail(err: str, fallback: str) -> str:
    return (err.strip() or fallback)[-1500:]


async def make_workspace(snapshot: Path, dest: Path) -> Path:
    """A detached worktree at ``dest`` of the commit checked out in ``snapshot``.

    Raises :class:`WorkspaceError` on failure. A ``dest`` that already holds files
    is refused and left as it was.
    """
    snapshot = Path(os.path.abspath(snapshot))
    dest = Path(os.path.abspath(dest))
    git = _require_git()
    rc, out, err = await _git(git, "rev-parse", "HEAD", cwd=snapshot)
    sha = out.decode("ascii", errors="replace").strip().lower()
    if rc != 0 or not _SHA_RE.match(sha):
        raise WorkspaceError("worktree_failed", f"no commit checked out in {snapshot}: "
                             + _detail(err, "rev-parse failed"))
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise WorkspaceError("worktree_failed", f"could not create {dest.parent}: {exc}") from exc
    await _git(git, "worktree", "prune", cwd=snapshot)  # forget a crashed attempt at this path
    rc, _, err = await _git(git, *_EOL_CONFIG, "worktree", "add", "--detach", "--no-checkout",
                            str(dest), sha, cwd=snapshot)
    if rc != 0:
        raise WorkspaceError("worktree_failed", "git worktree add failed: "
                             + _detail(err, "worktree add failed"))
    try:
        rc, _, err = await _git(git, *_EOL_CONFIG, "reset", "--hard", "--quiet", sha, cwd=dest)
        if rc != 0:
            raise WorkspaceError("worktree_failed", "checkout failed: " + _detail(err, "reset failed"))
        # in-tree attributes (eol=crlf, a filter) can still change files during the reset
        await BaseSnapshots(snapshot, root=dest.parent)._force_blob_bytes(git, dest, sha)
    except (WorkspaceError, SnapshotUnavailable) as exc:
        await remove_workspace(dest)
        raise WorkspaceError("worktree_failed", str(exc)) from exc
    return dest


async def _staged(git: str, workspace: Path) -> list[tuple[str, str, str, str]]:
    """``(new_mode, new_oid, status, path)`` for every path whose index entry differs from HEAD."""
    rc, out, err = await _git(git, "diff-index", "--cached", "-z", "--no-renames", "HEAD", cwd=workspace)
    if rc != 0:
        raise WorkspaceError("collect_failed", "git diff-index failed: " + _detail(err, "diff-index failed"))
    fields = out.decode("utf-8", errors="surrogateescape").split("\0")
    entries: list[tuple[str, str, str, str]] = []
    for meta, rel in zip(fields[0::2], fields[1::2]):
        parts = meta.split()  # ":<old mode> <new mode> <old oid> <new oid> <status>"
        if len(parts) == 5:
            entries.append((parts[1], parts[3], parts[4], rel))
    return entries


async def _restage_exact_bytes(git: str, workspace: Path) -> None:
    """Make the index hold each staged file's exact bytes, undoing attribute normalization."""
    for mode, oid, status, rel in await _staged(git, workspace):
        if status == "D" or not mode.startswith("100"):
            continue
        rc, out, err = await _git(git, "hash-object", "-w", "--no-filters", "--", rel, cwd=workspace)
        exact = out.decode("ascii", errors="replace").strip()
        if rc != 0 or not exact:
            raise WorkspaceError("collect_failed", f"could not hash {rel}: " + _detail(err, "hash-object failed"))
        if exact == oid:
            continue
        rc, _, err = await _git(git, "update-index", "--cacheinfo", f"{mode},{exact},{rel}", cwd=workspace)
        if rc != 0:
            raise WorkspaceError("collect_failed", f"could not stage {rel}: " + _detail(err, "update-index failed"))


def _normalize(path: str) -> str:
    return posixpath.normpath(path.replace("\\", "/").strip())


async def collect_changes(workspace: Path, allowed_files: Sequence[str]) -> WorkspaceChanges:
    """Stage everything in ``workspace`` and return its diff against HEAD.

    ``changed`` and ``outside`` are sorted repo-relative paths. Raises
    :class:`WorkspaceError` when git fails, and when the diff is not valid UTF-8
    (stage ``not_utf8``), because Foundry handles diffs as UTF-8 text and a lossy
    decode would produce a patch that no longer applies.
    """
    workspace = Path(os.path.abspath(workspace))
    git = _require_git()
    rc, _, err = await _git(git, *_EOL_CONFIG, "add", "-A", cwd=workspace)
    if rc != 0:
        raise WorkspaceError("collect_failed", "git add failed: " + _detail(err, "add failed"))
    await _restage_exact_bytes(git, workspace)
    rc, out, err = await _git(git, *_EOL_CONFIG, "diff", "--cached", "--binary", "--no-color", "--no-ext-diff",
                              "--no-textconv", "--no-renames", "--src-prefix=a/", "--dst-prefix=b/", "HEAD",
                              cwd=workspace)
    if rc != 0:
        raise WorkspaceError("collect_failed", "git diff failed: " + _detail(err, "diff failed"))
    try:
        diff = out.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WorkspaceError("not_utf8", "the diff is not valid UTF-8: a changed text file "
                             "uses another encoding") from exc
    changed = sorted(entry[3] for entry in await _staged(git, workspace))
    allowed = {_normalize(path) for path in allowed_files}
    return WorkspaceChanges(diff=diff, changed=changed,
                            outside=[path for path in changed if _normalize(path) not in allowed])


async def _common_dir(git: str, workspace: Path) -> Path | None:
    rc, out, _ = await _git(git, "rev-parse", "--git-common-dir", cwd=workspace)
    text = out.decode("utf-8", errors="surrogateescape").strip()
    if rc != 0 or not text:
        return None
    return Path(os.path.abspath(workspace / text))  # git may answer relative to the workspace


async def _remove(workspace: Path) -> None:
    workspace = Path(os.path.abspath(workspace))
    marker = workspace / ".git"
    if marker.is_dir():  # a linked worktree has a .git file; this is a whole repository
        log.info("piece workspace: refusing to remove %s (it is a full repository)", workspace)
        return
    if not workspace.exists():
        return
    git = shutil.which("git")
    common = await _common_dir(git, workspace) if git and marker.exists() else None
    if git and common:
        # run from the repository: Windows cannot delete a process's current directory
        rc, _, err = await _git(git, "worktree", "remove", "--force", str(workspace), cwd=common)
        if rc != 0:
            log.info("piece workspace: git worktree remove failed for %s: %s", workspace, err.strip())
    if workspace.is_dir():
        await _rmtree(workspace)
    elif workspace.exists():
        workspace.unlink()
    if git and common:
        await _git(git, "worktree", "prune", cwd=common)


async def remove_workspace(workspace: Path) -> None:
    """Remove a workspace and any leftover directory. Safe to repeat; never raises."""
    try:
        await _remove(Path(workspace))
    except Exception as exc:  # noqa: BLE001 - cleanup must never fail the caller
        log.info("piece workspace: could not remove %s: %s", workspace, exc)


__all__ = ["WorkspaceChanges", "WorkspaceError", "collect_changes", "make_workspace", "remove_workspace"]
