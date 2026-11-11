"""Stacked pull requests: what a dependent piece needs to know about its parents' PRs.

A piece that depends on an unmerged piece is built on the parent's PR branch and
opened as a PR against it. :class:`foundry.board_dispatch.BatchWatcher` makes that
decision with the helpers here and stores :func:`stack_record` under
``stack:<task id>`` in the queue's kv store.

Every subprocess is started with an argv list (no shell) and a timeout, and none
of these helpers raises on a failed lookup.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from typing import Any, Mapping

from foundry.git_publisher import Runner, default_runner, github_repo, valid_ref

log = logging.getLogger(__name__)

__all__ = ["PR_DONE", "PR_OPEN", "PR_UNKNOWN", "STACK_PREFIX", "github_origin", "pr_info", "pr_phase", "stack_note",
           "stack_record"]

#: Kv key prefix of the shared stack records.
STACK_PREFIX = "stack:"

PR_OPEN = "open"
PR_DONE = "done"  # merged or closed: a child has nothing to stack on
PR_UNKNOWN = "unknown"

_GH_TIMEOUT_S = 60.0
_GIT_TIMEOUT_S = 10.0
_PR_FIELDS = "state,headRefName,headRefOid,baseRefName,mergedAt"


async def pr_info(url: str, *, gh: str | None = None, runner: Runner | None = None) -> dict[str, Any] | None:
    """``gh pr view <url>``'s state and head as a dict; None on any failure."""
    if not url.startswith(("https://", "http://")):
        return None  # never hand gh anything that could read as an option
    gh = gh or shutil.which("gh")
    if not gh:
        log.debug("stacking: gh is not installed, cannot look up %s", url)
        return None
    run = runner or default_runner
    try:
        rc, out, err = await run([gh, "pr", "view", url, "--json", _PR_FIELDS], timeout=_GH_TIMEOUT_S)
        if rc != 0:
            log.debug("stacking: gh pr view %s failed (%s): %s", url, rc, err.strip())
            return None
        data = json.loads(out)
    except (OSError, ValueError) as exc:
        log.debug("stacking: gh pr view %s failed: %s", url, exc)
        return None
    return data if isinstance(data, dict) else None


def pr_phase(info: Mapping[str, Any] | None) -> str:
    """``PR_OPEN`` with a usable head branch, ``PR_DONE`` when merged or closed, else ``PR_UNKNOWN``."""
    if info is None:
        return PR_UNKNOWN
    state = str(info.get("state") or "").upper()
    if info.get("mergedAt") or state in ("MERGED", "CLOSED"):
        return PR_DONE
    if state == "OPEN" and valid_ref(info.get("headRefName")):
        return PR_OPEN
    return PR_UNKNOWN


async def github_origin(repo: str | os.PathLike[str], *, git: str | None = None,
                        runner: Runner | None = None) -> bool | None:
    """True when ``origin`` is a hosted repo gh can ask about, False when not, None when git could not tell."""
    git = git or shutil.which("git")
    if not git:
        return False  # nothing can have been pushed without git
    run = runner or default_runner
    try:
        rc, out, _ = await run([git, "-C", str(repo), "remote", "get-url", "origin"], timeout=_GIT_TIMEOUT_S)
    except (OSError, ValueError) as exc:
        log.debug("stacking: could not read the origin of %s: %s", repo, exc)
        return None
    if rc is None:
        return None
    return rc == 0 and github_repo(out.strip()) is not None


def stack_note(parent_url: str, parent_branch: str) -> str:
    """The paragraph a stacked piece's order carries."""
    return (f"This piece is stacked on {parent_url} (branch {parent_branch}): that PR's changes are "
            "already in your base. Build on them; do not repeat them.")


def stack_record(*, task_id: str, batch: str, key: str, parent_url: str, parent_branch: str,
                 parent_head: str | None, base: str | None) -> dict[str, Any]:
    """The shared record of one stacked piece, stored under ``stack:<task_id>``."""
    return {"task_id": task_id, "batch": batch, "key": key, "parent_url": parent_url,
            "parent_branch": parent_branch, "parent_head": parent_head, "base": base, "state": "stacked"}
