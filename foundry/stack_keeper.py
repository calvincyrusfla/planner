"""Stack keeper: rebase and retarget a stacked PR when its parent PR merges.

A piece that depends on an unmerged piece is published as a PR against the parent's
branch and recorded in the queue as ``stack:<task id>`` (``state: "stacked"``). Every
round this keeper looks at each such record:

* parent still open: nothing happens;
* parent closed without a merge: the record becomes ``gone`` and the child PR gets
  one comment;
* parent merged: the child branch is rebased onto the parent's base in a temporary
  worktree, pushed with ``--force-with-lease`` and retargeted (``restacked``). A
  conflict leaves the child untouched except for one comment (``conflict``).

The user's working folder is never touched: all git work happens in a temporary
worktree that is always removed. Every subprocess gets an argv list (no shell) and a
timeout; ``git`` and ``gh`` are resolved with ``shutil.which``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Sequence

from foundry.git_publisher import default_runner, valid_ref

log = logging.getLogger("foundry.stack_keeper")

__all__ = ["StackKeeper", "stack_keeper_interval"]

STACK_PREFIX = "stack:"
_GIT_TIMEOUT_S = 120.0
_GH_TIMEOUT_S = 60.0
_IDENTITY = ("-c", "user.name=Foundry", "-c", "user.email=foundry@localhost")

Runner = Callable[..., Awaitable[tuple[Any, str, str]]]


def stack_keeper_interval() -> float:
    """Seconds between rounds: FOUNDRY_STACK_KEEPER_S (default 120, at least 30)."""
    try:
        value = float(os.environ.get("FOUNDRY_STACK_KEEPER_S", "").strip() or 120.0)
    except ValueError:
        return 120.0
    return max(value, 30.0) if math.isfinite(value) else 120.0


def _env() -> dict[str, str]:
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GH_PROMPT_DISABLED"] = "1"
    env["GH_NO_UPDATE_NOTIFIER"] = "1"
    env.setdefault("LC_ALL", "C")
    return env


def _tail(err: str, out: str, rc: Any) -> str:
    return (err or out or f"exit {rc}").strip()[-300:]


class StackKeeper:
    """Restacks child PRs once their parent PR merged.

    Args:
        service: the running :class:`~foundry.serve.Service` (``repo``, ``running``, ``factory``).
        interval_s: seconds between rounds (default FOUNDRY_STACK_KEEPER_S).
        runner / gh / git: how commands are run (tests pass fakes).
    """

    def __init__(self, service: Any, *, interval_s: float | None = None, runner: Runner | None = None,
                 gh: str | None = None, git: str | None = None) -> None:
        self.service = service
        self.interval_s = stack_keeper_interval() if interval_s is None else interval_s
        self.runner: Runner = runner or default_runner
        self.gh = gh or shutil.which("gh") or "gh"
        self.git = git or shutil.which("git") or "git"
        self.rounds = 0
        self.last_error: str | None = None
        self._task: asyncio.Task | None = None

    # ------------------------------------------------------------------ loop

    def start(self) -> None:
        """Start the background loop (no-op when it is already running)."""
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._loop())

    async def stop(self) -> None:
        """Stop the background loop."""
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self.interval_s)
                await self.round()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the keeper must outlive a bad round
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.warning("stack keeper: round failed: %s", self.last_error)

    # ------------------------------------------------------------------ round

    def _queue(self) -> Any:
        factory = getattr(self.service, "factory", None)
        return getattr(factory, "queue", None)

    def _child_pr(self, task_id: str) -> tuple[str, str] | None:
        factory = getattr(self.service, "factory", None)
        states = getattr(getattr(factory, "manager", None), "pr_states", None) or {}
        pr = states.get(task_id)
        if not isinstance(pr, Mapping):
            return None
        url, branch = pr.get("url"), pr.get("branch")
        if isinstance(url, str) and url and isinstance(branch, str) and branch:
            return url, branch
        return None

    async def round(self) -> None:
        """One pass over the ``stacked`` records."""
        queue = self._queue()
        if queue is None:
            return
        self.rounds += 1
        failed = False
        for key, rec in sorted(queue.kv_items(STACK_PREFIX).items()):
            if not isinstance(rec, dict) or rec.get("state") != "stacked":
                continue
            if not getattr(self.service, "running", True):
                return
            try:
                await self._handle(queue, key, rec)
            except Exception as exc:  # noqa: BLE001 - one bad record must not stop the round
                failed = True
                self.last_error = f"{key}: {type(exc).__name__}: {exc}"
                log.warning("stack keeper: %s", self.last_error)
        if not failed:
            self.last_error = None

    async def _handle(self, queue: Any, key: str, rec: dict[str, Any]) -> None:
        child = self._child_pr(str(rec.get("task_id") or ""))
        parent_url = rec.get("parent_url")
        if child is None or not isinstance(parent_url, str) or not parent_url:
            return
        child_url, child_branch = child
        rc, out, err = await self._run([self.gh, "pr", "view", parent_url, "--json",
                                        "state,mergedAt,headRefOid,baseRefName"], _GH_TIMEOUT_S)
        if rc != 0:
            log.warning("stack keeper: could not read %s: %s", parent_url, _tail(err, out, rc))
            return
        try:
            parent = json.loads(out or "{}")
        except ValueError:
            return
        if not isinstance(parent, dict):
            return
        state = str(parent.get("state") or "").upper()
        merged = state == "MERGED" or bool(parent.get("mergedAt"))
        if merged:
            await self._restack(queue, key, rec, parent, child_url, child_branch)
        elif state == "CLOSED":
            note = f"parent {parent_url} was closed without merging"
            self._save(queue, key, rec, "gone", note)
            await self._comment(child_url, f"The parent PR {parent_url} was closed without merging, "
                                           "so this stacked PR needs re-planning.")

    # ------------------------------------------------------------------ restack

    async def _restack(self, queue: Any, key: str, rec: dict[str, Any], parent: dict[str, Any],
                       child_url: str, child_branch: str) -> None:
        base = parent.get("baseRefName")
        parent_head = parent.get("headRefOid")
        if not valid_ref(base) or not valid_ref(child_branch) or not isinstance(parent_head, str) or not parent_head:
            log.warning("stack keeper: %s: unusable branch names or parent commit", key)
            return
        assert isinstance(base, str)
        repo = Path(self.service.repo)
        tmp_root = Path(tempfile.mkdtemp(prefix="stack-keeper-"))
        tree = tmp_root / "wt"
        added = False
        try:
            rc, out, err = await self._git(repo, "fetch", "--quiet", "origin")
            if rc != 0:
                log.warning("stack keeper: git fetch failed: %s", _tail(err, out, rc))
                return
            rc, out, err = await self._git(repo, "worktree", "add", "--detach", str(tree),
                                           f"origin/{child_branch}")
            if rc != 0:
                log.warning("stack keeper: could not add a worktree: %s", _tail(err, out, rc))
                return
            added = True
            rc, old_sha, err = await self._git(tree, "rev-parse", "HEAD")
            old_sha = old_sha.strip()
            if rc != 0 or not old_sha:
                log.warning("stack keeper: could not read %s: %s", child_branch, _tail(err, "", rc))
                return
            rc, upstream, err = await self._git(tree, "merge-base", f"origin/{child_branch}", parent_head)
            upstream = upstream.strip()
            if rc != 0 or not upstream:
                log.warning("stack keeper: no common commit of %s and the parent: %s", child_branch,
                            _tail(err, "", rc))
                return
            rc, out, err = await self._git(tree, *_IDENTITY, "rebase", "--onto", f"origin/{base}", upstream)
            if rc != 0:
                _, files_out, _ = await self._git(tree, "diff", "--name-only", "--diff-filter=U")
                files = [line.strip() for line in files_out.splitlines() if line.strip()]
                await self._git(tree, "rebase", "--abort")
                listed = ", ".join(files) or "unknown files"
                self._save(queue, key, rec, "conflict", f"conflicts in {listed}", files=files)
                await self._comment(child_url, f"Could not restack onto {base} after {rec.get('parent_url')} "
                                               f"merged: conflicts in {listed}. It needs a rebuild on the new base.")
                return
            rc, new_sha, err = await self._git(tree, "rev-parse", "HEAD")
            new_sha = new_sha.strip()
            if rc != 0 or not new_sha:
                log.warning("stack keeper: could not read the rebased commit: %s", _tail(err, "", rc))
                return
            rc, out, err = await self._git(tree, "push", f"--force-with-lease={child_branch}:{old_sha}", "origin",
                                           f"HEAD:{child_branch}")
            if rc != 0:
                log.warning("stack keeper: push of %s failed: %s", child_branch, _tail(err, out, rc))
                return
            note = f"restacked onto {base}"
            rc, out, err = await self._run([self.gh, "pr", "edit", child_url, "--base", base], _GH_TIMEOUT_S)
            if rc != 0:  # the branch is already rebased: asking again later would replay the wrong commits
                note += f" (retarget failed: {_tail(err, out, rc)})"
                log.warning("stack keeper: could not retarget %s: %s", child_url, _tail(err, out, rc))
            self._save(queue, key, rec, "restacked", note, old_sha=old_sha, new_sha=new_sha)
        finally:
            await self._cleanup(repo, tmp_root, tree, added)

    async def _cleanup(self, repo: Path, tmp_root: Path, tree: Path, added: bool) -> None:
        try:
            if added:
                await self._git(repo, "worktree", "remove", "--force", str(tree))
        except Exception as exc:  # noqa: BLE001 - cleanup must not hide the real outcome
            log.warning("stack keeper: could not remove the worktree: %s", exc)
        shutil.rmtree(tmp_root, ignore_errors=True)
        try:
            await self._git(repo, "worktree", "prune")
        except Exception as exc:  # noqa: BLE001
            log.warning("stack keeper: worktree prune failed: %s", exc)

    # ------------------------------------------------------------------ helpers

    def _save(self, queue: Any, key: str, rec: dict[str, Any], state: str, note: str, **extra: Any) -> None:
        queue.kv_set(key, {**rec, "state": state, "note": note, **extra})

    async def _comment(self, url: str, body: str) -> None:
        try:
            rc, out, err = await self._run([self.gh, "pr", "comment", url, "--body", body], _GH_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001 - the record already holds the outcome
            log.warning("stack keeper: could not comment on %s: %s", url, exc)
            return
        if rc != 0:
            log.warning("stack keeper: could not comment on %s: %s", url, _tail(err, out, rc))

    async def _git(self, cwd: Path, *args: str) -> tuple[Any, str, str]:
        return await self._run([self.git, *args], _GIT_TIMEOUT_S, cwd=cwd)

    async def _run(self, argv: Sequence[str], timeout: float, cwd: Path | None = None) -> tuple[Any, str, str]:
        where = cwd if cwd is not None else Path(self.service.repo)
        return await self.runner(list(argv), cwd=str(where), timeout=timeout, env=_env())
