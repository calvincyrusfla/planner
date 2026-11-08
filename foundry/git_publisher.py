"""Publish an approved task's patch as its own branch and pull request.

``GitPublisher.publish`` takes the ``.patch`` file Foundry wrote for a task and:

1. resolves the base branch (explicit ``base``; else the remote's default
   branch, ``<remote>/HEAD``; else the repo's current branch);
2. fetches it from the remote;
3. adds a temporary ``git worktree`` under ``foundry/data/worktrees`` on a new
   branch ``foundry/<task-id>-<slug>`` (``-2``, ``-3`` ... when that name is
   taken locally or on the remote) started from ``<remote>/<base>``;
4. ``git apply --index`` the patch there and commits it (author/committer
   from the repo's git config; hooks run, no ``--no-verify``);
5. ``git push -u <remote> <branch>`` (never forced);
6. ``gh pr create`` with the body passed through ``--body-file``;
7. always removes the worktree again.

The user's working tree and current branch are never touched: every change
happens inside the temporary worktree. Every subprocess is started with an
argv list (no shell), with a timeout, and the executables are resolved with
``shutil.which`` so ``git.exe`` / ``gh.exe`` work on Windows.

Failures never raise: ``publish`` returns a ``PublishResult`` with ``ok=False``
and a ``stage`` naming what went wrong (see ``STAGES``).
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import os
import re
import shutil
import tempfile
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: A full or abbreviated commit id (``publish(base_sha=)``); never an option or a ref expression.
_SHA_RE = re.compile(r"[0-9a-fA-F]{7,64}")

#: Where temporary worktrees live by default (inside foundry/data, git-ignored).
DEFAULT_WORKTREE_ROOT = Path(__file__).resolve().parent / "data" / "worktrees"

#: Branch name prefix for every branch Foundry publishes.
BRANCH_PREFIX = "foundry/"

#: Error stages a failed ``PublishResult`` can carry.
STAGES: tuple[str, ...] = (
    "no_git",           # git is not installed
    "not_a_repo",       # repo_root is not a git repository
    "no_remote",        # the remote (default "origin") is not configured
    "no_base",          # no base branch could be determined (detached HEAD, no default branch)
    "fetch_failed",     # git fetch of the base branch failed
    "no_patch",         # the patch file is missing or empty
    "worktree_failed",  # git worktree add failed
    "apply_failed",     # git apply --index failed
    "commit_failed",    # git commit failed (e.g. a hook rejected it)
    "push_failed",      # git push failed
    "gh_missing",       # the GitHub CLI is not installed
    "gh_not_logged_in", # gh auth status failed
    "pr_failed",        # gh pr create failed (the branch is pushed; see open_pr)
    "error",            # anything unexpected
)

#: (returncode, stdout, stderr). returncode is None when the command timed out.
RunResult = tuple["int | None", str, str]
#: Injected subprocess runner: ``await runner(argv, cwd=..., timeout=..., env=...)``.
Runner = Callable[..., Awaitable[RunResult]]
#: ``on_pushed(branch, commit)``, sync or async.
OnPushed = Callable[[str, "str | None"], Any]

_TIMEOUT = 120.0
_NET_TIMEOUT = 300.0
_URL_RE = re.compile(r"https?://\S+")
#: Inherited git location variables would point every command at another repository.
_GIT_LOCATION_VARS = frozenset({
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_COMMON_DIR",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE",
})


@dataclass
class PublishResult:
    """Outcome of one ``publish`` / ``open_pr`` call."""

    ok: bool
    branch: str | None = None
    commit: str | None = None
    pr_url: str | None = None
    error: str | None = None
    stage: str | None = None
    base: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def slugify(text: str, limit: int = 40) -> str:
    """Lowercase ASCII slug for branch names (``"Add login!"`` -> ``"add-login"``)."""
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    slug = slug[:limit].rstrip("-")
    return slug or "change"


def branch_name(task_id: str, title: str) -> str:
    """``foundry/<task-id>-<slug>``, lowercase and safe for git refs on every OS."""
    return f"{task_prefix(task_id)}{slugify(title)}"


def commit_message(title: str, body: str, limit: int = 2000,
                   trailers: Mapping[str, str] | None = None) -> str:
    """PR title as the subject line, then a summary of the body, then git trailers."""
    subject = " ".join((title or "Foundry change").split())[:200]
    text = (body or "").strip()
    if len(text) > limit:
        text = text[:limit].rstrip() + "\n..."
    msg = f"{subject}\n\n{text}\n" if text else f"{subject}\n"
    if trailers:
        msg += "\n" + "".join(f"{k}: {' '.join(str(v).split())}\n" for k, v in trailers.items())
    return msg


def patch_digest(text: str) -> str:
    """sha256 of the patch with line endings normalised (CRLF/CR -> LF, one final newline)."""
    norm = text.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n") + "\n"
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def parse_trailers(message: str) -> dict[str, str]:
    """``Key: value`` lines of a commit message's last paragraph."""
    paras = [p for p in (message or "").strip().split("\n\n") if p.strip()]
    out: dict[str, str] = {}
    if paras:
        for line in paras[-1].splitlines():
            m = re.match(r"^([A-Za-z0-9-]+):\s*(.*?)\s*$", line)
            if m:
                out[m.group(1)] = m.group(2)
    return out


TASK_TRAILER = "Foundry-Task"
PATCH_TRAILER = "Foundry-Patch"


async def default_runner(
    argv: Sequence[str],
    *,
    cwd: str | os.PathLike[str] | None = None,
    timeout: float = _TIMEOUT,
    env: Mapping[str, str] | None = None,
) -> RunResult:
    """Run ``argv`` (no shell); returns (returncode, stdout, stderr), returncode None on timeout."""
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(cwd) if cwd is not None else None,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=dict(env) if env is not None else None,
    )
    done = False
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
        done = True
    except asyncio.TimeoutError:
        return None, "", f"timed out after {timeout:.0f}s"
    finally:
        if not done:  # timeout or cancellation: never leave the child running
            _kill(proc)
            try:
                await asyncio.shield(asyncio.wait_for(proc.wait(), 10))
            except (asyncio.TimeoutError, asyncio.CancelledError, ProcessLookupError):
                pass
    return (
        proc.returncode,
        out.decode("utf-8", errors="replace"),
        err.decode("utf-8", errors="replace"),
    )


def _kill(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is None:
        try:
            proc.kill()
        except (ProcessLookupError, OSError):
            pass


_REF_RE = re.compile(r"^[A-Za-z0-9_./-]+$")


def valid_ref(name: Any) -> bool:
    """A branch name safe to hand to git/gh (the board's rule for a batch ``base``).

    Only ``[A-Za-z0-9_./-]``, at most 200 characters, no ``..``, and no leading
    ``-`` or ``/`` or trailing ``/`` (so it can never be read as an option).
    """
    return (isinstance(name, str) and 0 < len(name) <= 200 and bool(_REF_RE.match(name)) and ".." not in name
            and not name.startswith(("-", "/")) and not name.endswith("/"))


def github_repo(url: str) -> str | None:
    """``owner/name`` (``host/owner/name`` off github.com) from a remote URL; None if unparsable.

    Handles ``https://[user@]host/owner/name(.git)``, ``ssh://git@host[:port]/owner/name(.git)``
    and scp-style ``git@host:owner/name(.git)``. Local paths give None.
    """
    url = (url or "").strip()
    m = re.match(r"^(?:https?|ssh|git)://(?:[^@/]+@)?([^/:]+)(?::\d+)?/([^/]+)/([^/]+?)(?:\.git)?/?$", url)
    if not m:
        m = re.match(r"^(?:[^@/\s]+@)?([A-Za-z0-9.-]+\.[A-Za-z]+):/?([^/]+)/([^/]+?)(?:\.git)?/?$", url)
    if not m:
        return None
    host, owner, name = m.group(1).lower(), m.group(2), m.group(3)
    if not owner or not name:
        return None
    return f"{owner}/{name}" if host in ("github.com", "www.github.com") else f"{host}/{owner}/{name}"


class _Fail(Exception):
    def __init__(self, stage: str, error: str) -> None:
        super().__init__(error)
        self.stage = stage
        self.error = error


def _check_ref(name: Any, what: str) -> str:
    """``name`` when it is a :func:`valid_ref`; else stops the publish before any git/gh call."""
    if not valid_ref(name):
        raise _Fail("bad_ref", f"refusing the {what} {str(name)[:80]!r}: not a plain branch name")
    return str(name)


class GitPublisher:
    """Turns a task's patch into a pushed branch plus an open pull request."""

    def __init__(
        self,
        repo_root: str | os.PathLike[str],
        *,
        base: str | None = None,
        remote: str = "origin",
        runner: Runner | None = None,
        gh: str | None = None,
        git: str | None = None,
        draft: bool = False,
        worktree_root: str | os.PathLike[str] | None = None,
        timeout: float = _TIMEOUT,
        net_timeout: float = _NET_TIMEOUT,
    ) -> None:
        self.repo_root = Path(repo_root)
        self.base = base or None
        self.remote = remote or "origin"
        self.runner: Runner = runner or default_runner
        self._injected = runner is not None
        self.gh_name = gh or "gh"
        self.git_name = git or "git"
        self.draft = draft
        self.worktree_root = Path(worktree_root) if worktree_root else DEFAULT_WORKTREE_ROOT
        self.timeout = timeout
        self.net_timeout = net_timeout
        self._lock = asyncio.Lock()
        self._gh_repo: str | None = None
        # one folder per repository, so cleaning up stale worktrees never touches another repo's
        key = hashlib.sha1(os.path.normcase(os.path.abspath(self.repo_root)).encode("utf-8")).hexdigest()[:8]
        self._wt_dir = self.worktree_root / f"{slugify(self.repo_root.name, 20)}-{key}"

    # -- helpers ---------------------------------------------------------

    def _which(self, name: str) -> str | None:
        found = shutil.which(name)
        if found:
            return found
        # an injected runner may fake executables that are not on PATH
        return name if self._injected else None

    @staticmethod
    def _env() -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k not in _GIT_LOCATION_VARS}
        env["GIT_TERMINAL_PROMPT"] = "0"   # never hang on a credentials prompt
        env["GH_PROMPT_DISABLED"] = "1"
        env["GH_NO_UPDATE_NOTIFIER"] = "1"
        env.setdefault("LC_ALL", "C")      # stable, parseable git output
        return env

    async def _run(self, argv: Sequence[str], *, cwd: Path | None = None, timeout: float | None = None) -> RunResult:
        try:
            return await self.runner(
                list(argv), cwd=str(cwd or self.repo_root), timeout=timeout or self.timeout, env=self._env()
            )
        except (OSError, ValueError) as exc:
            return 127, "", str(exc)

    async def _git(self, git: str, *args: str, cwd: Path | None = None, timeout: float | None = None) -> RunResult:
        return await self._run([git, *args], cwd=cwd, timeout=timeout)

    @staticmethod
    def _msg(res: RunResult, fallback: str) -> str:
        rc, out, err = res
        text = (err or "").strip() or (out or "").strip()
        return (text or fallback)[-1500:]

    # -- preflight -------------------------------------------------------

    async def preflight(self) -> dict[str, Any]:
        """Check git, the remote, gh and gh's login. Never raises.

        Returns ``{git, repo, remote, gh, gh_auth, ok, reason}`` (bools, plus a
        plain-words ``reason`` for the first thing missing, None when ready).
        """
        report: dict[str, Any] = {
            "git": False, "repo": False, "remote": False, "gh": False, "gh_auth": False,
            "ok": False, "reason": None,
        }
        reasons: list[str] = []
        git = self._which(self.git_name)
        if git:
            report["git"] = True
            if (await self._git(git, "rev-parse", "--git-dir"))[0] == 0:
                report["repo"] = True
                if (await self._git(git, "remote", "get-url", self.remote))[0] == 0:
                    report["remote"] = True
                else:
                    reasons.append(f"the repository has no '{self.remote}' remote")
            else:
                reasons.append(f"{self.repo_root} is not a git repository")
        else:
            reasons.append("git is not installed")
        gh = self._which(self.gh_name)
        if gh:
            report["gh"] = True
            res = await self._run([gh, "auth", "status"], timeout=60)
            if res[0] == 0:
                report["gh_auth"] = True
            else:
                reasons.append("the GitHub CLI is not logged in (run: gh auth login)")
        else:
            reasons.append("the GitHub CLI (gh) is not installed")
        report["ok"] = not reasons
        report["reason"] = reasons[0] if reasons else None
        return report

    # -- publish ---------------------------------------------------------

    async def publish(
        self,
        task_id: str,
        title: str,
        patch_path: str | os.PathLike[str],
        pr: Mapping[str, Any] | None = None,
        *,
        draft: bool | None = None,
        on_pushed: OnPushed | None = None,
        base: str | None = None,
        base_sha: str | None = None,
    ) -> PublishResult:
        """Branch, commit, push and open the PR for one task. Never raises.

        ``base`` (e.g. a board batch's branch) overrides the publisher's base for this task.
        ``base_sha`` (the commit the task was built on, see :mod:`foundry.base_snapshot`):
        the branch starts from exactly that commit instead of the base's current tip, so
        what was built and checked is what is published (the PR still targets ``base``).

        Idempotent per task: when a branch ``foundry/<task-id>-*`` from an earlier
        call already exists on the remote, no new branch is pushed; its existing
        PR is returned, or a PR is opened for it. ``on_pushed(branch, commit)``
        (sync or async) is called right after a successful push, so the caller
        can persist it before the PR step.
        """
        async with self._lock:
            try:
                for ref, what in ((base, "base branch"), (self.base, "base branch (FOUNDRY_PR_BASE)")):
                    if ref:
                        _check_ref(ref, what)
                if base_sha is not None and not _SHA_RE.fullmatch(str(base_sha)):
                    raise _Fail("bad_ref", f"invalid base commit: {str(base_sha)[:80]!r}")
                return await self._publish(task_id, title, Path(patch_path), dict(pr or {}),
                                           self.draft if draft is None else bool(draft), on_pushed, base=base,
                                           base_sha=base_sha)
            except _Fail as f:
                return PublishResult(ok=False, error=f.error, stage=f.stage)
            except Exception as exc:  # noqa: BLE001 - publish must never break the caller
                log.exception("git publisher: unexpected failure for %s", task_id)
                return PublishResult(ok=False, error=str(exc) or type(exc).__name__, stage="error")

    async def _require_repo(self) -> str:
        git = self._which(self.git_name)
        if not git:
            raise _Fail("no_git", "git is not installed")
        if not self.repo_root.is_dir() or (await self._git(git, "rev-parse", "--git-dir"))[0] != 0:
            raise _Fail("not_a_repo", f"{self.repo_root} is not a git repository")
        rc, out, _ = await self._git(git, "remote", "get-url", self.remote)
        if rc != 0:
            raise _Fail("no_remote", f"the repository has no '{self.remote}' remote")
        self._gh_repo = github_repo(out.strip().splitlines()[0] if out.strip() else "")
        return git

    async def _require_gh(self) -> str:
        gh = self._which(self.gh_name)
        if not gh:
            raise _Fail("gh_missing", "the GitHub CLI (gh) is not installed")
        res = await self._run([gh, "auth", "status"], timeout=60)
        if res[0] != 0:
            raise _Fail("gh_not_logged_in", "the GitHub CLI is not logged in (run: gh auth login): "
                        + self._msg(res, "gh auth status failed"))
        return gh

    def _repo_args(self) -> list[str]:
        return ["--repo", self._gh_repo] if self._gh_repo else []

    async def _resolve_base(self, git: str) -> str:
        if self.base:
            return self.base
        rc, out, _ = await self._git(git, "symbolic-ref", "--quiet", "--short", f"refs/remotes/{self.remote}/HEAD")
        name = out.strip()
        prefix = f"{self.remote}/"
        if rc == 0 and name.startswith(prefix) and len(name) > len(prefix):
            return name[len(prefix):]
        rc, out, _ = await self._git(git, "ls-remote", "--symref", self.remote, "HEAD", timeout=self.net_timeout)
        if rc == 0:
            for line in out.splitlines():
                m = re.match(r"ref:\s+refs/heads/(\S+)\s+HEAD", line)
                if m:
                    return m.group(1)
        rc, out, _ = await self._git(git, "symbolic-ref", "--quiet", "--short", "HEAD")
        if rc == 0 and out.strip():
            return out.strip()
        raise _Fail("no_base", "could not determine the base branch (detached HEAD and no remote default); "
                    "set FOUNDRY_PR_BASE")

    async def _branch_taken(self, git: str, name: str) -> bool:
        if (await self._git(git, "rev-parse", "--verify", "--quiet", f"refs/heads/{name}"))[0] == 0:
            return True
        rc, out, _ = await self._git(git, "ls-remote", "--heads", "--", self.remote, f"refs/heads/{name}",
                                     timeout=self.net_timeout)
        return rc == 0 and bool(out.strip())

    async def _free_branch(self, git: str, task_id: str, title: str) -> str:
        stem = branch_name(task_id, title)
        for n in range(1, 100):
            cand = stem if n == 1 else f"{stem}-{n}"
            if not await self._branch_taken(git, cand):
                return cand
        raise _Fail("worktree_failed", f"no free branch name for {stem}")

    async def _identity_args(self, git: str) -> list[str]:
        """``-c user.name/-c user.email`` only when the repo's config lacks them."""
        args: list[str] = []
        if (await self._git(git, "config", "user.name"))[1].strip() == "":
            args += ["-c", "user.name=Foundry"]
        if (await self._git(git, "config", "user.email"))[1].strip() == "":
            args += ["-c", "user.email=foundry@localhost"]
        return args

    async def _task_branches(self, git: str, task_id: str) -> tuple[dict[str, str], list[str]]:
        """This task's earlier branches: ({remote branch: sha}, [local-only branches])."""
        pattern = f"refs/heads/{task_prefix(task_id)}*"
        remote: dict[str, str] = {}
        rc, out, _ = await self._git(git, "ls-remote", "--heads", self.remote, pattern, timeout=self.net_timeout)
        if rc == 0:
            for line in out.splitlines():
                parts = line.split()
                if len(parts) == 2 and parts[1].startswith("refs/heads/"):
                    remote[parts[1][len("refs/heads/"):]] = parts[0]
        local: list[str] = []
        rc, out, _ = await self._git(git, "for-each-ref", "--format=%(refname)", pattern)
        if rc == 0:
            for line in out.splitlines():
                name = line.strip()[len("refs/heads/"):]
                if name and name not in remote:
                    local.append(name)
        return remote, local

    async def _matching_branches(self, git: str, remote: Mapping[str, str],
                                 trailers: Mapping[str, str]) -> list[str]:
        """Remote branches whose tip commit message has every one of ``trailers``."""
        if not remote:
            return []
        names = sorted(remote)
        await self._git(git, "fetch", "--quiet", "--no-tags", self.remote,
                        *[f"refs/heads/{n}" for n in names], timeout=self.net_timeout)
        out: list[str] = []
        for name in names:
            rc, msg, _ = await self._git(git, "log", "-1", "--format=%B", remote[name])
            if rc != 0:
                continue
            found = parse_trailers(msg)
            if all(found.get(k) == v for k, v in trailers.items()):
                out.append(name)
        return out

    async def _existing_pr(self, gh: str, branch: str) -> str | None:
        """URL of a PR (any state) whose head is ``branch``; None when there is none or gh fails."""
        res = await self._run([gh, "pr", "list", *self._repo_args(), "--head", branch, "--state", "all",
                               "--json", "url,number", "--limit", "1"], timeout=self.net_timeout)
        if res[0] != 0:
            return None
        try:
            data = json.loads(res[1] or "[]")
        except ValueError:
            return None
        if isinstance(data, list) and data and isinstance(data[0], Mapping):
            url = data[0].get("url")
            return str(url) if url else None
        return None

    async def _notify_pushed(self, on_pushed: OnPushed | None, branch: str, commit: str | None) -> None:
        if on_pushed is None:
            return
        try:
            out = on_pushed(branch, commit)
            if inspect.isawaitable(out):
                await out
        except Exception as exc:  # noqa: BLE001 - the caller's bookkeeping never breaks publishing
            log.warning("git publisher: on_pushed callback failed: %s", exc)

    async def _publish(self, task_id: str, title: str, patch: Path, pr: dict[str, Any], draft: bool,
                       on_pushed: OnPushed | None, *, base: str | None = None,
                       base_sha: str | None = None) -> PublishResult:
        git = await self._require_repo()
        await self._clean_stale(git)
        if not patch.is_file() or patch.stat().st_size == 0:
            raise _Fail("no_patch", f"patch file missing or empty: {patch}")
        # check gh before pushing anything, so a missing CLI leaves no orphan branch
        gh = await self._require_gh()
        base = _check_ref(base or await self._resolve_base(git), "base branch")
        pr_title = str(pr.get("title") or title or task_id).strip()
        pr_body = str(pr.get("body") or "")

        # resume: an earlier call already pushed this task's branch (crash before the state was saved)
        # local-only branches of this task are left alone (the user may have committed there by
        # hand): _free_branch skips their names and picks the next free -2, -3 ...
        remote, _local_only = await self._task_branches(git, task_id)
        # only a branch whose tip carries this task's id AND this exact patch is "ours" (a reset
        # or second database can reuse task ids for different work)
        trailers = {TASK_TRAILER: task_id, PATCH_TRAILER: patch_digest(patch.read_text(encoding="utf-8",
                                                                                     errors="replace"))}
        mine = await self._matching_branches(git, remote, trailers)
        if mine:
            ordered = sorted(mine, key=_suffix_order)
            for name in ordered:
                url = await self._existing_pr(gh, name)
                if url:
                    log.info("git publisher: %s already has a PR for %s: %s", task_id, name, url)
                    return PublishResult(ok=True, branch=name, commit=remote[name], pr_url=url, base=base)
            name = ordered[-1]
            log.info("git publisher: %s branch %s already pushed; opening its PR", task_id, name)
            await self._notify_pushed(on_pushed, name, remote[name])
            result = await self._create_pr(gh, name, base, pr_title, pr_body, draft)
            result.commit = remote[name]
            return result

        res = await self._git(git, "fetch", "--quiet", "--", self.remote, base, timeout=self.net_timeout)
        if res[0] != 0:
            raise _Fail("fetch_failed", f"git fetch {self.remote} {base} failed: " + self._msg(res, "fetch failed"))
        start = f"refs/remotes/{self.remote}/{base}"
        if (await self._git(git, "rev-parse", "--verify", "--quiet", start))[0] != 0:
            start = "FETCH_HEAD"
        if base_sha:
            # build == publish: branch from the exact commit the task was built and checked on
            if (await self._git(git, "rev-parse", "--verify", "--quiet", f"{base_sha}^{{commit}}"))[0] != 0:
                raise _Fail("fetch_failed", f"the commit the task was built on ({base_sha[:12]}) is not in "
                            f"the repository; fetch {self.remote} {base} and retry")
            start = base_sha
        branch = _check_ref(await self._free_branch(git, task_id, title), "branch")

        wt_dir = self._wt_dir
        wt_dir.mkdir(parents=True, exist_ok=True)
        wt = wt_dir / f"{slugify(task_id, 20)}-{uuid.uuid4().hex[:8]}"
        pushed = False
        commit: str | None = None
        added = False
        try:
            res = await self._git(git, "worktree", "add", "--quiet", "--no-track", "-b", branch, "--", str(wt), start)
            if res[0] != 0:
                raise _Fail("worktree_failed", self._msg(res, "git worktree add failed"))
            added = True
            res = await self._git(git, "apply", "--index", "--whitespace=nowarn", str(patch.resolve()), cwd=wt)
            if res[0] != 0:
                raise _Fail("apply_failed", self._msg(res, "git apply failed"))
            msg_file = wt_dir / f"{wt.name}.msg"
            msg_file.write_text(commit_message(pr_title, pr_body, trailers=trailers), encoding="utf-8", newline="\n")
            try:
                ident = await self._identity_args(git)
                res = await self._git(git, *ident, "commit", "--quiet", "-F", str(msg_file), cwd=wt)
            finally:
                msg_file.unlink(missing_ok=True)
            if res[0] != 0:
                raise _Fail("commit_failed", self._msg(res, "git commit failed"))
            commit = (await self._git(git, "rev-parse", "HEAD", cwd=wt))[1].strip() or None
            res = await self._git(git, "push", "--quiet", "-u", "--", self.remote, f"refs/heads/{branch}:refs/heads/{branch}",
                                  cwd=wt, timeout=self.net_timeout)
            if res[0] != 0:
                raise _Fail("push_failed", self._msg(res, "git push failed"))
            pushed = True
        except _Fail as f:
            return PublishResult(ok=False, branch=branch if pushed else None, commit=commit,
                                 error=f.error, stage=f.stage, base=base)
        finally:
            if added:
                await self._remove_worktree(git, wt)
            else:
                await _rmtree(wt)
            if added and not pushed:
                # nothing reached the remote: drop the local branch so a retry reuses the name
                await self._git(git, "branch", "-D", branch)
            _rmdir_quiet(wt_dir)

        await self._notify_pushed(on_pushed, branch, commit)
        result = await self._create_pr(gh, branch, base, pr_title, pr_body, draft)
        result.commit = commit
        return result

    async def _remove_worktree(self, git: str, wt: Path) -> None:
        res = await self._git(git, "worktree", "remove", "--force", str(wt))
        if res[0] != 0 or wt.exists():
            log.warning("git publisher: worktree remove failed (%s); deleting %s", self._msg(res, "?"), wt)
            await _rmtree(wt)
            await self._git(git, "worktree", "prune")

    async def _clean_stale(self, git: str) -> None:
        """Remove worktrees an interrupted earlier run left under this repo's worktree folder."""
        await self._git(git, "worktree", "prune")
        wt_dir = self._wt_dir
        if not wt_dir.is_dir():
            return
        rc, out, _ = await self._git(git, "worktree", "list", "--porcelain")
        registered = set()
        if rc == 0:
            for line in out.splitlines():
                if line.startswith("worktree "):
                    registered.add(os.path.normcase(os.path.abspath(line[len("worktree "):].strip())))
        for entry in list(wt_dir.iterdir()):
            if os.path.normcase(os.path.abspath(entry)) in registered:
                await self._git(git, "worktree", "remove", "--force", str(entry))
            if entry.is_dir():
                await _rmtree(entry)
            else:
                try:
                    entry.unlink()
                except OSError:
                    pass
        await self._git(git, "worktree", "prune")
        _rmdir_quiet(wt_dir)

    # -- pull request ----------------------------------------------------

    async def open_pr(self, branch: str, pr: Mapping[str, Any], *, base: str | None = None,
                      draft: bool | None = None) -> PublishResult:
        """(Re)open the PR for an already pushed ``branch`` (after a ``pr_failed``). Never raises.

        Returns the existing PR when the branch already has one.
        """
        async with self._lock:
            try:
                _check_ref(branch, "branch")
                for ref, what in ((base, "base branch"), (self.base, "base branch (FOUNDRY_PR_BASE)")):
                    if ref:
                        _check_ref(ref, what)
                git = await self._require_repo()
                gh = await self._require_gh()
                base = _check_ref(base or await self._resolve_base(git), "base branch")
                url = await self._existing_pr(gh, branch)
                if url:
                    return PublishResult(ok=True, branch=branch, base=base, pr_url=url)
                return await self._create_pr(gh, branch, base, str(pr.get("title") or branch),
                                             str(pr.get("body") or ""), self.draft if draft is None else draft)
            except _Fail as f:
                return PublishResult(ok=False, branch=branch, error=f.error, stage=f.stage, base=base)
            except Exception as exc:  # noqa: BLE001
                log.exception("git publisher: unexpected failure opening PR for %s", branch)
                return PublishResult(ok=False, branch=branch, error=str(exc), stage="error", base=base)

    async def _create_pr(self, gh: str, branch: str, base: str, title: str, body: str,
                         draft: bool = False) -> PublishResult:
        _check_ref(branch, "branch")
        _check_ref(base, "base branch")
        fd, body_path = tempfile.mkstemp(prefix="foundry-pr-", suffix=".md")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(body)
            argv = [gh, "pr", "create", *self._repo_args(), "--base", base, "--head", branch,
                    "--title", title or branch, "--body-file", body_path]
            if draft:
                argv.append("--draft")
            res = await self._run(argv, timeout=self.net_timeout)
        finally:
            try:
                os.unlink(body_path)
            except OSError:
                pass
        urls = _URL_RE.findall(res[1]) or _URL_RE.findall(res[2])
        if res[0] != 0:
            if "already exists" in (res[1] + res[2]) and urls:
                return PublishResult(ok=True, branch=branch, base=base, pr_url=urls[-1])
            return PublishResult(ok=False, branch=branch, base=base, stage="pr_failed",
                                 error=self._msg(res, "gh pr create failed"))
        return PublishResult(ok=True, branch=branch, base=base, pr_url=urls[-1] if urls else None)


def task_prefix(task_id: str) -> str:
    """Branch prefix every branch of ``task_id`` starts with (``foundry/f-012-``)."""
    tid = re.sub(r"[^a-z0-9]+", "-", (task_id or "task").lower()).strip("-") or "task"
    return f"{BRANCH_PREFIX}{tid}-"


def _suffix_order(name: str) -> tuple[int, str]:
    m = re.search(r"-(\d+)$", name)
    return (int(m.group(1)) if m and int(m.group(1)) < 100 else 1, name)


async def _rmtree(path: Path, attempts: int = 5) -> None:
    """``shutil.rmtree`` with a few short retries (Windows keeps files locked briefly)."""
    for i in range(attempts):
        shutil.rmtree(path, ignore_errors=True)
        if not path.exists():
            return
        await asyncio.sleep(0.2 * (i + 1))
    log.warning("git publisher: could not delete %s", path)


def _rmdir_quiet(path: Path) -> None:
    try:
        path.rmdir()  # only when empty
    except OSError:
        pass


__all__ = [
    "BRANCH_PREFIX", "DEFAULT_WORKTREE_ROOT", "GitPublisher", "PublishResult", "STAGES",
    "PATCH_TRAILER", "TASK_TRAILER", "branch_name", "commit_message", "parse_trailers", "patch_digest", "default_runner", "github_repo", "slugify", "task_prefix",
]
