"""End-to-end check for T-036: Foundry builds from a clean snapshot of the publish branch.

The scenario is the user's Windows PC that made F-012/F-016 fail with
``apply_failed``:

* ``origin`` (a local bare repository) stores LF blobs on ``main``;
* the user's clone has ``core.autocrlf=true`` and CRLF working files;
* the user's folder sits on ANOTHER branch (``feature``) with an extra,
  unpushed commit that changes the very file the order touches.

``foundry.serve`` runs for that folder with simulated Claude/Gemma and a fake
``gh``. The order completes; the combined patch has LF line endings and applies
``--cached``-clean on ``origin/main``; the branch pushed to the bare remote is
one commit on top of the pinned ``main`` commit with exactly the builder's
change; ``gh pr create`` targets ``main``. The user's folder is untouched:
same bytes, same branch and HEAD, same index.

Safety: simulated Claude and Gemma only, dry board bridge, DEPT_HUB=off plus a
scratch DEPT_DATA, a fake ``gh`` and a local bare "origin" (no GitHub, no
network). Snapshots and publish worktrees go to ``tmp_path``. The git/gh parts
need ``sh``-style executables and are skipped on Windows.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from foundry.claude_client import SimulatedClaudeClient
from foundry.main import _FILES_RE, append_diff
from foundry.schemas import ClaudeResult
from foundry.tests.test_gemma_group3_e2e import (
    _fake_gh,
    _gh_calls,
    _git,
    _git_env,
    _git_repo,
    _http,
    _service,
    _on_loop,
    _wait,
    _wait_pr,
)

ORDER = {
    "title": "Add clamp helper",
    "description": "Add clamp(x, lo, hi) to the math helpers.",
    "files": ["shop/mathx.py"],
}
TARGET = "shop/mathx.py"
FEATURE_LINE = "FEATURE_ONLY = True  # unpushed work on the user's feature branch"

posix_only = pytest.mark.skipif(os.name == "nt", reason="fake gh is a POSIX script")
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


@pytest.fixture(autouse=True)
def _scratch_dept(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEPT_HUB", "off")
    monkeypatch.setenv("DEPT_DATA", str(tmp_path / "dept-data"))


# --------------------------------------------------------------------------- helpers


def _git_bytes(cwd: Path, *args: str) -> bytes:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, timeout=60)
    assert proc.returncode == 0, (args, proc.stderr)
    return proc.stdout


def _tree_bytes(root: Path) -> dict[str, str]:
    """sha256 of every file in the user's folder (``.git`` aside)."""
    out: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if rel == ".git" or rel.startswith(".git/") or not path.is_file():
            continue
        out[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def _windows_like_feature_checkout(repo: Path) -> None:
    """``core.autocrlf=true``, the user on ``feature`` (one unpushed commit on TARGET), CRLF working files."""
    _git(repo, "config", "core.autocrlf", "true")
    _git(repo, "checkout", "--quiet", "-b", "feature")
    target = repo / TARGET
    target.write_bytes(target.read_bytes() + f"\n{FEATURE_LINE}\n".encode())
    _git(repo, "commit", "--quiet", "-am", "feature work (not pushed)")
    # What Git for Windows leaves on disk with autocrlf=true: git itself checks the files out with CRLF.
    for rel in _git(repo, "ls-files").split():
        (repo / rel).unlink()
    _git(repo, "checkout", "--", ".")
    assert _git(repo, "status", "--porcelain").strip() == ""
    assert b"\r\n" in target.read_bytes()
    assert b"\r" not in _git_bytes(repo, "show", f"HEAD:{TARGET}")


def _snapshot_claude(bare: Path) -> SimulatedClaudeClient:
    """Builders append a helper to each allowed file AS STORED on ``origin/main`` (what the snapshot holds).

    A real builder sees the snapshot; the user's folder (feature branch, CRLF)
    would give a diff whose context doesn't match the publish branch.
    """

    def script(prompt: str, agent_id: str) -> ClaudeResult:
        if prompt.startswith("You are reviewer") or "## Unified diff" in prompt:
            text = json.dumps({"verdict": "APPROVED", "directives": []})
        else:
            m = _FILES_RE.search(prompt)
            files = [ln[2:].strip() for ln in m.group(1).splitlines()] if m else []
            diffs = []
            for rel in files:
                src = _git_bytes(bare, "show", f"main:{rel}").decode("utf-8")
                fn = re.sub(r"\W", "_", Path(rel).stem)
                new = ["", "", f"def {fn}_helper(value):", f'    """Helper added by {agent_id}."""', "    return value"]
                diffs.append(append_diff(rel, src, new))
            text = "Here is the change.\n\n```diff\n" + "".join(diffs) + "```\n"
        return ClaudeResult(text=text, input_tokens=len(prompt) // 4 + 1, output_tokens=len(text) // 4 + 1,
                            duration_seconds=0.0)

    return SimulatedClaudeClient(script)


# --------------------------------------------------------------------------- the scenario


@posix_only
@needs_git
def test_crlf_folder_on_another_branch_still_builds_and_publishes_from_origin_main(
    tmp_path: Path, clean_foundry_env: pytest.MonkeyPatch
) -> None:
    from foundry import base_snapshot, git_publisher

    mp = clean_foundry_env
    _git_env(tmp_path, mp)
    worktrees, snapshots = tmp_path / "worktrees", tmp_path / "base"
    mp.setattr(git_publisher, "DEFAULT_WORKTREE_ROOT", worktrees)
    mp.setattr(base_snapshot, "DEFAULT_SNAPSHOT_ROOT", snapshots)
    repo, bare = _git_repo(tmp_path)
    main_sha = _git(bare, "rev-parse", "main").strip()
    main_blob = _git_bytes(bare, "show", f"main:{TARGET}")
    _windows_like_feature_checkout(repo)

    head_before = _git(repo, "rev-parse", "HEAD").strip()
    files_before = _tree_bytes(repo)
    index_before = _git(repo, "ls-files", "--stage")
    branches_before = _git(repo, "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads")
    log = _fake_gh(tmp_path, mp)

    with _service(tmp_path, repo, auto_pr=True, claude=_snapshot_claude(bare)) as (service, base):
        _wait(lambda: _http("GET", f"{base}/api/health").get("pr_ready"), "pr_ready in /api/health")
        tid = _http("POST", f"{base}/api/orders", ORDER)["task_id"]
        row = _wait_pr(base, tid, ("opened", "failed", "skipped"), timeout=90)
        pr = row["pr"]
        assert pr["state"] == "opened", row
        branch = pr["branch"]
        manager = service.factory.manager
        # the pin is forgotten once the PR is settled; the queue keeps the event
        pins = [e["payload"] for e in manager.queue.events(task_id=tid, kind="base_pinned")]
        pin = dict(pins[-1]) if pins else {}
        _on_loop(service, manager.prune_snapshots())  # the finished task's snapshot goes
        patches = sorted((tmp_path / "patches").glob(f"{tid}*.patch"))

    # The task was pinned to origin/main's commit, built in a snapshot under tmp_path, then pruned.
    assert pin == {"base": "main", "sha": main_sha}, pin
    assert snapshots.is_dir()  # it existed: the snapshot root (and the repo's folder in it) was created
    assert not [p for p in snapshots.rglob("*") if p.is_dir() and p.name == main_sha[:12]]
    assert _git(repo, "worktree", "list", "--porcelain").count("worktree ") == 1

    # The combined patch: LF only, and it applies --cached-clean on origin/main.
    assert [p.name for p in patches] == [f"{tid}.patch"], patches
    patch = patches[0].read_bytes()
    assert b"\r" not in patch
    assert FEATURE_LINE.encode() not in patch
    check = tmp_path / "check"
    _git(tmp_path, "clone", "--quiet", "--branch", "main", str(bare), str(check))
    _git(check, "apply", "--check", "--cached", str(patches[0]))

    # gh opened one PR against main for the pushed branch.
    creates = [c for c in _gh_calls(log) if c["argv"][:2] == ["pr", "create"]]
    assert len(creates) == 1, creates
    argv = creates[0]["argv"]
    assert argv[argv.index("--base") + 1] == "main"
    assert argv[argv.index("--head") + 1] == branch

    # The pushed branch is one commit on top of the pinned main with exactly the builder's change, LF only.
    assert _git(bare, "rev-parse", f"{branch}~1").strip() == main_sha
    assert _git(bare, "diff", "--name-only", f"main..{branch}").split() == [TARGET]
    published = _git_bytes(bare, "show", f"{branch}:{TARGET}")
    assert published.startswith(main_blob.rstrip(b"\n"))
    assert b"def mathx_helper(value):" in published
    assert b"\r" not in published and FEATURE_LINE.encode() not in published
    assert _git(bare, "rev-parse", "main").strip() == main_sha  # base branch never pushed to
    assert _git(bare, "branch", "--list", "feature").strip() == ""  # the user's branch never pushed

    # The user's folder is untouched: bytes, branch, HEAD, index, local branches.
    assert _tree_bytes(repo) == files_before
    assert _git(repo, "symbolic-ref", "--short", "HEAD").strip() == "feature"
    assert _git(repo, "rev-parse", "HEAD").strip() == head_before
    assert _git(repo, "ls-files", "--stage") == index_before
    assert _git(repo, "diff", "--cached", "--name-only").strip() == ""
    after = _git(repo, "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads")
    assert set(branches_before.splitlines()) <= set(after.splitlines())
    assert {ln.split()[0] for ln in after.splitlines()} - {ln.split()[0] for ln in branches_before.splitlines()} \
        <= {f"refs/heads/{branch}"}  # at most the kept local foundry/* branch
    assert b"\r\n" in (repo / TARGET).read_bytes()

    # Nothing was written inside the user's folder; publish worktrees are gone.
    assert not (repo / "foundry").exists()
    assert not worktrees.exists() or not any(worktrees.iterdir())
