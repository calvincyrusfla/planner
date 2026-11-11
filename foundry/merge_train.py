"""Merge train: keep open Foundry PRs rebased on a base that moved.

``rebase_behind`` looks at the open Foundry PRs of one base. A PR whose head does not
contain the current ``origin/<base>`` is rebased onto it in a temporary detached
worktree and pushed with ``--force-with-lease`` pinned to the head that was rebased.
A conflicting rebase is aborted and the PR is left untouched (the combined check
already comments on conflicts). PRs stacked on a ``foundry/`` branch and PRs that are
up to date are skipped.

The user's folder is never touched: all git work happens in a temporary worktree that
is always removed. Every subprocess gets an argv list (no shell) and a timeout.

``python -m foundry.merge_train status [--db PATH]`` prints the last train events.
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from foundry.git_publisher import GitPublisher, default_runner, valid_ref

__all__ = ["EVENT_KIND", "merge_train_enabled", "rebase_behind", "main"]

log = logging.getLogger(__name__)

EVENT_KIND = "merge_train"
_GIT_TIMEOUT_S = 300
_IDENTITY = ("-c", "user.name=Foundry", "-c", "user.email=foundry@localhost")


def merge_train_enabled() -> bool:
    """False when FOUNDRY_MERGE_TRAIN switches the train off (it is on by default)."""
    return os.environ.get("FOUNDRY_MERGE_TRAIN", "1").strip().lower() not in ("0", "false", "no", "off")


async def rebase_behind(service: Any, base: str, prs: list[dict[str, Any]], *, runner: Any = None) -> dict[str, Any]:
    """Rebase the PRs of ``base`` that fell behind ``origin/<base>``.

    Returns ``{"rebased": [numbers], "conflicts": {number: [files]}, "skipped": [numbers]}``.
    Never raises for a git failure: such a PR is skipped.
    """
    run = runner or default_runner
    git = shutil.which("git") or "git"
    repo = str(service.repo)
    result: dict[str, Any] = {"rebased": [], "conflicts": {}, "skipped": []}

    async def sh(argv: list[str], cwd: str = repo) -> tuple[int | None, str, str]:
        try:
            return await run(argv, cwd=cwd, timeout=_GIT_TIMEOUT_S, env=GitPublisher._env())
        except (OSError, ValueError) as exc:
            return 127, "", str(exc)

    candidates: list[dict[str, Any]] = []
    for pr in prs:
        number = pr.get("number")
        if not isinstance(number, int):
            continue
        pr_base = pr.get("baseRefName")
        if pr_base != base or str(pr_base).startswith("foundry/"):
            result["skipped"].append(number)
            continue
        candidates.append(pr)
    if not candidates or not valid_ref(base):
        result["skipped"].extend(pr["number"] for pr in candidates)
        return result

    rc, _out, err = await sh([git, "fetch", "--quiet", "origin", base])
    sha = ""
    if rc == 0:
        rc, out, err = await sh([git, "rev-parse", "--verify", f"refs/remotes/origin/{base}"])
        sha = out.strip() if rc == 0 else ""
    if not sha:
        log.warning("merge train: could not read origin/%s: %s", base, (err or f"exit {rc}").strip()[-300:])
        result["skipped"].extend(pr["number"] for pr in candidates)
        return result

    for pr in sorted(candidates, key=lambda row: row["number"]):
        number = pr["number"]
        branch, head = pr.get("headRefName"), str(pr.get("headRefOid") or "")
        if not valid_ref(branch) or not head:
            result["skipped"].append(number)
            continue
        try:
            outcome = await _rebase_one(sh, git, base, sha, str(branch), head)
        except Exception as exc:  # noqa: BLE001 - one bad PR must not stop the others
            log.warning("merge train: #%s failed: %s: %s", number, type(exc).__name__, exc)
            outcome = None
        if outcome is None:
            result["skipped"].append(number)
        elif outcome == []:
            result["rebased"].append(number)
        else:
            result["conflicts"][number] = outcome
    return result


async def _rebase_one(sh: Any, git: str, base: str, sha: str, branch: str, head: str) -> list[str] | None:
    """Rebase one branch; ``[]`` when pushed, the conflicting files on a conflict, None when skipped."""
    rc, _o, _e = await sh([git, "fetch", "--quiet", "origin", f"refs/heads/{branch}"])
    if rc != 0:
        return None
    rc, _o, _e = await sh([git, "merge-base", "--is-ancestor", sha, head])
    if rc != 1:  # 0: up to date, anything else: unknown
        return None
    tmp = Path(tempfile.mkdtemp(prefix="merge-train-"))
    tree = tmp / "wt"
    try:
        rc, _o, err = await sh([git, "worktree", "add", "--quiet", "--detach", str(tree), head])
        if rc != 0:
            log.warning("merge train: worktree failed: %s", err.strip()[-300:])
            return None
        where = ["-C", str(tree)]
        rc, _o, _e = await sh([git, *_IDENTITY, *where, "rebase", f"refs/remotes/origin/{base}"])
        if rc != 0:
            _, out, _ = await sh([git, *where, "diff", "--name-only", "--diff-filter=U"])
            files = out.splitlines()
            await sh([git, *where, "rebase", "--abort"])
            return files or ["?"]
        rc, _o, err = await sh([git, *where, "push", "--quiet", f"--force-with-lease={branch}:{head}",
                                "origin", f"HEAD:refs/heads/{branch}"])
        if rc != 0:
            log.warning("merge train: push of %s failed: %s", branch, err.strip()[-300:])
            return None
        return []
    finally:
        await sh([git, "worktree", "remove", "--force", str(tree)])
        shutil.rmtree(tmp, ignore_errors=True)
        await sh([git, "worktree", "prune"])


def main(argv: Sequence[str] | None = None) -> int:
    """``status [--db PATH]``: print the last merge train events."""
    parser = argparse.ArgumentParser(prog="python -m foundry.merge_train")
    sub = parser.add_subparsers(dest="cmd", required=True)
    status = sub.add_parser("status", help="print the last train events")
    status.add_argument("--db", help="queue database (default: the configured one)")
    args = parser.parse_args(list(argv) if argv is not None else sys.argv[1:])

    from foundry.config import FoundryConfig
    from foundry.orchestrator_queue import FoundryQueue

    db = Path(args.db) if args.db else Path(FoundryConfig.from_env().db_path)
    if not db.is_file():
        print(f"merge_train: no queue database at {db}", file=sys.stderr)
        return 2
    with FoundryQueue(db) as queue:
        events = queue.events(kind=EVENT_KIND)[-10:]
    if not events:
        print("no merge train events")
        return 0
    for event in events:
        p = event.get("payload") or {}
        print(f"{event.get('ts')} {p.get('base')} {str(p.get('old_sha', ''))[:8]}->{str(p.get('new_sha', ''))[:8]} "
              f"rebased={p.get('rebased', [])} conflicts={p.get('conflicts', {})}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
