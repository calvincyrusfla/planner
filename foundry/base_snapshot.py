"""Clean, byte-exact snapshots of the publish branch for Foundry to build from.

Foundry used to read the user's working folder to build context, run gates and
produce patches. On Windows with ``core.autocrlf=true`` that folder holds CRLF
files while the repository stores LF blobs, so patches carried CRLF context and
failed to apply in the publisher's fresh worktree (``apply_failed``). When the
folder sat on another branch, or behind, the patch was built against the wrong
code altogether.

``BaseSnapshots`` fixes both: for each task it

1. :meth:`~BaseSnapshots.resolve` s the publish base (an explicit branch, else the
   remote's default branch, like :class:`~foundry.git_publisher.GitPublisher`)
   to an exact commit with ``git fetch`` + ``rev-parse refs/remotes/<remote>/<base>``;
2. :meth:`~BaseSnapshots.checkout` s that commit as a detached ``git worktree``
   under ``foundry/data/base/<repo>-<hash8>/<sha12>`` with ``core.autocrlf=false``
   / ``core.eol=lf`` and no smudge filters, so every file is byte-identical to the
   stored blob (``git show <sha>:<path>``);
3. :meth:`~BaseSnapshots.release` s / :meth:`~BaseSnapshots.prune` s snapshots no
   task references any more.

The user's branches, index and working tree are never touched: the only writes
are the snapshot worktrees (and their admin entries under ``.git/worktrees``)
plus the remote-tracking ref the fetch updates. Every subprocess is argv-only
with a timeout. Anything that makes a snapshot impossible (no git, not a
repository, no remote, fetch failed ...) raises :class:`SnapshotUnavailable`, so
the caller can fall back to the user's folder.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import shutil
import sys
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .git_publisher import (
    _GIT_LOCATION_VARS,
    Runner,
    RunResult,
    default_runner,
    slugify,
    valid_ref,
)

log = logging.getLogger(__name__)

#: Fallback for callers that pass no root (inside foundry/data, git-ignored);
#: the service passes ``<work_root>/base``, outside the repository.
DEFAULT_SNAPSHOT_ROOT = Path(__file__).resolve().parent / "data" / "base"

#: Stages a :class:`SnapshotUnavailable` can carry.
STAGES: tuple[str, ...] = (
    "no_git",           # git is not installed
    "not_a_repo",       # repo_root is not a git repository
    "no_remote",        # the remote is not configured
    "bad_ref",          # the base is not a plain branch name / the sha is not a full hex id
    "no_base",          # no base branch could be determined
    "fetch_failed",     # git fetch of the base failed
    "no_commit",        # the commit is not in the repository
    "unsafe_root",      # the snapshot root is inside the user's tree and not git-ignored
    "worktree_failed",  # git worktree add / checkout failed
)

_TIMEOUT = 120.0
_NET_TIMEOUT = 300.0
_SHA_RE = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")
#: Config for every git call that writes snapshot files: no EOL conversion, no LFS smudge.
_EOL_CONFIG: tuple[str, ...] = (
    "-c", "core.autocrlf=false",
    "-c", "core.eol=lf",
    "-c", "core.safecrlf=false",
    "-c", "filter.lfs.smudge=",
    "-c", "filter.lfs.process=",
    "-c", "filter.lfs.required=false",
)


class SnapshotUnavailable(Exception):
    """A snapshot cannot be made; ``stage`` is one of :data:`STAGES`. Callers fall back."""

    def __init__(self, stage: str, error: str) -> None:
        super().__init__(error)
        self.stage = stage
        self.error = error


@dataclass(frozen=True)
class Snapshot:
    """One pinned base: branch name, exact commit and the worktree holding it."""

    base: str
    sha: str
    path: Path

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["path"] = str(self.path)
        return d


def _norm(path: str | os.PathLike[str]) -> str:
    return os.path.normcase(os.path.abspath(path))


def _inside(child: Path, parent: Path) -> bool:
    c, p = _norm(child), _norm(parent)
    return c == p or c.startswith(p.rstrip("\\/") + os.sep)


class BaseSnapshots:
    """Pins publish bases to commits and keeps one clean worktree per pinned commit."""

    def __init__(
        self,
        repo_root: str | os.PathLike[str],
        *,
        remote: str = "origin",
        root: str | os.PathLike[str] | None = None,
        runner: Runner | None = None,
        git: str | None = None,
        timeout: float = _TIMEOUT,
        net_timeout: float = _NET_TIMEOUT,
    ) -> None:
        self.repo_root = Path(repo_root)
        self.remote = remote or "origin"
        self.root = Path(root) if root else DEFAULT_SNAPSHOT_ROOT
        self.runner: Runner = runner or default_runner
        self._injected = runner is not None
        self.git_name = git or "git"
        self.timeout = timeout
        self.net_timeout = net_timeout
        self._lock = asyncio.Lock()
        self._root_checked = False
        key = hashlib.sha1(_norm(self.repo_root).encode("utf-8")).hexdigest()[:8]
        #: this repository's folder under ``root`` (prune never looks outside it)
        self.repo_dir = self.root / f"{slugify(self.repo_root.name, 20)}-{key}"

    # -- helpers ---------------------------------------------------------

    def _which(self) -> str | None:
        found = shutil.which(self.git_name)
        if found:
            return found
        return self.git_name if self._injected else None

    @staticmethod
    def _env() -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k not in _GIT_LOCATION_VARS}
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_LFS_SKIP_SMUDGE"] = "1"
        env.setdefault("LC_ALL", "C")
        return env

    async def _git(self, git: str, *args: str, cwd: Path | None = None,
                   timeout: float | None = None) -> RunResult:
        try:
            return await self.runner([git, *args], cwd=str(cwd or self.repo_root),
                                     timeout=timeout or self.timeout, env=self._env())
        except (OSError, ValueError) as exc:
            return 127, "", str(exc)

    @staticmethod
    def _msg(res: RunResult, fallback: str) -> str:
        _rc, out, err = res
        text = (err or "").strip() or (out or "").strip()
        return (text or fallback)[-1500:]

    async def _require_repo(self, *, remote: bool = True) -> str:
        git = self._which()
        if not git:
            raise SnapshotUnavailable("no_git", "git is not installed")
        if not self.repo_root.is_dir() or (await self._git(git, "rev-parse", "--git-dir"))[0] != 0:
            raise SnapshotUnavailable("not_a_repo", f"{self.repo_root} is not a git repository")
        if remote and (await self._git(git, "remote", "get-url", self.remote))[0] != 0:
            raise SnapshotUnavailable("no_remote", f"the repository has no '{self.remote}' remote")
        return git

    def path_for(self, sha: str) -> Path:
        """Where the snapshot of ``sha`` lives (whether or not it exists yet)."""
        return self.repo_dir / _check_sha(sha)[:12]

    def exists(self, sha: str) -> bool:
        """True when a snapshot folder for ``sha`` is present (not checked for cleanliness)."""
        try:
            return self.path_for(sha).is_dir()
        except SnapshotUnavailable:
            return False

    # -- resolve ---------------------------------------------------------

    async def resolve(self, base: str | None = None) -> tuple[str, str]:
        """Fetch ``base`` (None: the remote's default branch) and return ``(base, sha)``."""
        git = await self._require_repo()
        if base is not None and not valid_ref(base):
            raise SnapshotUnavailable("bad_ref", f"refusing the base branch {str(base)[:80]!r}")
        name = base or await self._default_base(git)
        res = await self._git(git, "fetch", "--quiet", "--no-tags", "--", self.remote,
                              f"+refs/heads/{name}:refs/remotes/{self.remote}/{name}",
                              timeout=self.net_timeout)
        if res[0] != 0:
            raise SnapshotUnavailable("fetch_failed", f"git fetch {self.remote} {name} failed: "
                                      + self._msg(res, "fetch failed"))
        rc, out, _ = await self._git(git, "rev-parse", "--verify", "--quiet",
                                     f"refs/remotes/{self.remote}/{name}^{{commit}}")
        sha = out.strip().lower()
        if rc != 0 or not _SHA_RE.match(sha):
            raise SnapshotUnavailable("fetch_failed", f"{self.remote}/{name} did not resolve to a commit")
        return name, sha

    async def _default_base(self, git: str) -> str:
        # same order as GitPublisher._resolve_base, so build and publish agree
        rc, out, _ = await self._git(git, "symbolic-ref", "--quiet", "--short",
                                     f"refs/remotes/{self.remote}/HEAD")
        name, prefix = out.strip(), f"{self.remote}/"
        if rc == 0 and name.startswith(prefix) and valid_ref(name[len(prefix):]):
            return name[len(prefix):]
        rc, out, _ = await self._git(git, "ls-remote", "--symref", self.remote, "HEAD",
                                     timeout=self.net_timeout)
        if rc == 0:
            for line in out.splitlines():
                m = re.match(r"ref:\s+refs/heads/(\S+)\s+HEAD", line)
                if m and valid_ref(m.group(1)):
                    return m.group(1)
        rc, out, _ = await self._git(git, "symbolic-ref", "--quiet", "--short", "HEAD")
        if rc == 0 and valid_ref(out.strip()):
            return out.strip()
        raise SnapshotUnavailable("no_base", "could not determine the base branch "
                                  "(no remote default, detached HEAD); set FOUNDRY_PR_BASE")

    # -- checkout --------------------------------------------------------

    async def snapshot(self, base: str | None = None) -> Snapshot:
        """:meth:`resolve` then :meth:`checkout` in one call."""
        name, sha = await self.resolve(base)
        return Snapshot(name, sha, await self.checkout(sha))

    async def checkout(self, sha: str) -> Path:
        """A clean detached worktree of exactly ``sha`` with LF files as stored. Idempotent."""
        sha = _check_sha(sha)
        async with self._lock:
            git = await self._require_repo(remote=False)
            await self._check_root(git)
            path = self.path_for(sha)
            if path.exists():
                if await self._is_clean(git, path, sha):
                    return path
                log.info("base snapshot: %s is stale or dirty; recreating", path)
                await self._remove(git, path)
            if (await self._git(git, "cat-file", "-e", f"{sha}^{{commit}}"))[0] != 0:
                raise SnapshotUnavailable("no_commit", f"commit {sha[:12]} is not in the repository")
            self.repo_dir.mkdir(parents=True, exist_ok=True)
            await self._git(git, "worktree", "prune")
            res = await self._git(git, *_EOL_CONFIG, "worktree", "add", "--detach", "--no-checkout",
                                  str(path), sha)
            if res[0] != 0:
                await self._remove(git, path)
                raise SnapshotUnavailable("worktree_failed", "git worktree add failed: "
                                          + self._msg(res, "worktree add failed"))
            res = await self._git(git, *_EOL_CONFIG, "reset", "--hard", "--quiet", sha, cwd=path)
            if res[0] != 0:
                await self._remove(git, path)
                raise SnapshotUnavailable("worktree_failed", "checkout of the snapshot failed: "
                                          + self._msg(res, "checkout failed"))
            try:
                await self._force_blob_bytes(git, path, sha)
            except SnapshotUnavailable:
                await self._remove(git, path)
                raise
            return path

    async def _check_root(self, git: str) -> None:
        """Refuse a snapshot root inside the user's tree unless git ignores it."""
        if self._root_checked:
            return
        rc, out, _ = await self._git(git, "rev-parse", "--show-toplevel")
        top = Path(out.strip()) if rc == 0 and out.strip() else self.repo_root
        if _inside(self.repo_dir, top):
            rel = os.path.relpath(self.repo_dir, top).replace(os.sep, "/") + "/"
            if (await self._git(git, "check-ignore", "-q", "--no-index", "--", rel, cwd=top))[0] != 0:
                raise SnapshotUnavailable("unsafe_root", f"the snapshot folder {self.root} is inside "
                                          "the repository and not git-ignored")
        self._root_checked = True

    async def _force_blob_bytes(self, git: str, path: Path, sha: str) -> None:
        """Rewrite any file an in-tree attribute (eol=crlf, a filter) changed from the blob bytes."""
        rc, out, _ = await self._git(git, *_EOL_CONFIG, "ls-files", "-z", "--eol", "-s", cwd=path)
        if rc != 0:
            return
        for entry in out.split("\0"):
            # "-s --eol" gives: "<mode> <oid> <stage>\ti/<eol> w/<eol> attr/<...>\t<path>"
            parts = entry.split("\t", 2)
            if len(parts) != 3:
                continue
            head, eol, rel = parts[0].split(), parts[1].split(), parts[2]
            if len(head) < 2 or not head[0].startswith("100") or len(eol) < 2:
                continue
            i_eol, w_eol = eol[0], eol[1]
            attr = " ".join(eol[2:])
            if i_eol[2:] == w_eol[2:] and "filter" not in attr and "eol=crlf" not in attr:
                continue
            target = path / rel
            if not _inside(target, path):
                continue
            data = await self._blob(git, head[1])
            if data is None:
                raise SnapshotUnavailable("worktree_failed", f"could not read the blob of {rel}")
            try:
                target.write_bytes(data)
            except OSError as exc:
                raise SnapshotUnavailable("worktree_failed", f"could not write {rel}: {exc}") from exc

    async def _blob(self, git: str, oid: str) -> bytes | None:
        try:
            proc = await asyncio.create_subprocess_exec(
                git, "cat-file", "blob", oid, cwd=str(self.repo_root), env=self._env(),
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL)
        except OSError:
            return None
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), self.timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return None
        return out if proc.returncode == 0 else None

    async def _is_clean(self, git: str, path: Path, sha: str) -> bool:
        if not (path / ".git").exists():
            return False
        rc, out, _ = await self._git(git, "rev-parse", "HEAD", cwd=path)
        if rc != 0 or out.strip().lower() != sha:
            return False
        rc, out, _ = await self._git(git, *_EOL_CONFIG, "status", "--porcelain", "-z", "--untracked-files=all",
                                     "--ignored=no", cwd=path)
        if rc != 0:
            return False
        for entry in out.split("\0"):
            if not entry:
                continue
            # a file _force_blob_bytes rewrote (in-tree eol=crlf / filter) reads as " M" but holds
            # exactly the blob bytes: still clean; anything else is dirty
            code, rel = entry[:2], entry[3:]
            if code != " M" or not _inside(path / rel, path):
                return False
            try:
                data = (path / rel).read_bytes()
            except OSError:
                return False
            if data != await self._blob(git, f"{sha}:{rel}"):
                return False
        return True

    # -- release / prune -------------------------------------------------

    async def release(self, sha: str) -> bool:
        """Remove the snapshot of ``sha``; True when something was removed. Never raises."""
        try:
            path = self.path_for(sha)
        except SnapshotUnavailable:
            return False
        async with self._lock:
            if not path.exists():
                return False
            git = self._which()
            await self._remove(git, path)
            return not path.exists()

    async def prune(self, keep: Iterable[str] = ()) -> list[str]:
        """Remove every snapshot of this repo whose commit is not in ``keep``. Never raises.

        Returns the folder names (``sha12``) removed.
        """
        kept = {str(s).strip().lower()[:12] for s in keep if s}
        removed: list[str] = []
        async with self._lock:
            if not self.repo_dir.is_dir():
                return removed
            git = self._which()
            for entry in sorted(self.repo_dir.iterdir()):
                if entry.name in kept:
                    continue
                await self._remove(git, entry)
                if not entry.exists():
                    removed.append(entry.name)
            if git:
                await self._git(git, "worktree", "prune")
            try:
                self.repo_dir.rmdir()  # only when empty
            except OSError:
                pass
        return removed

    async def _remove(self, git: str | None, path: Path) -> None:
        # only ever under this repo's snapshot folder
        if not _inside(path, self.repo_dir) or _norm(path) == _norm(self.repo_dir):
            log.warning("base snapshot: refusing to remove %s (outside %s)", path, self.repo_dir)
            return
        if git and path.is_dir() and (path / ".git").exists():
            await self._git(git, "worktree", "remove", "--force", str(path))
        if path.is_dir():
            await _rmtree(path)
        elif path.exists():
            try:
                path.unlink()
            except OSError:
                pass
        if git:
            await self._git(git, "worktree", "prune")


def _check_sha(sha: Any) -> str:
    s = str(sha or "").strip().lower()
    if not _SHA_RE.match(s):
        raise SnapshotUnavailable("bad_ref", f"not a full commit id: {str(sha)[:80]!r}")
    return s


async def _rmtree(path: Path, attempts: int = 5) -> None:
    """``shutil.rmtree`` with retries; clears read-only bits (Windows git objects)."""
    def _retry(func: Any, p: str, _exc: Any) -> None:
        try:
            os.chmod(p, 0o700)
            func(p)
        except OSError:
            pass

    for i in range(attempts):
        if sys.version_info >= (3, 12):
            shutil.rmtree(path, onexc=_retry)
        else:  # pragma: no cover - older Pythons
            shutil.rmtree(path, onerror=_retry)
        if not path.exists():
            return
        await asyncio.sleep(0.2 * (i + 1))
    log.warning("base snapshot: could not delete %s", path)


__all__ = ["BaseSnapshots", "DEFAULT_SNAPSHOT_ROOT", "STAGES", "Snapshot", "SnapshotUnavailable"]
