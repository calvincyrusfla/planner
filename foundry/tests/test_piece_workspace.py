"""Piece workspaces: throwaway checkouts of a snapshot and the diff of what changed there.

Real git repositories; skipped when git is missing.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from foundry.piece_workspace import (
    WorkspaceError,
    collect_changes,
    make_workspace,
    remove_workspace,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


def run(coro):
    return asyncio.run(coro)


def _env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@e", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@e")
    return env


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, env=_env(), check=True, capture_output=True,
                          text=True).stdout


def git_bytes(cwd: Path, *args: str) -> bytes:
    return subprocess.run(["git", *args], cwd=cwd, env=_env(), check=True, capture_output=True).stdout


def init_repo(root: Path) -> None:
    git(root.parent, "init", "-q", "-b", "main", str(root))
    git(root, "config", "core.autocrlf", "false")


def add_worktree(repo: Path, dest: Path) -> Path:
    git(repo, "-c", "core.autocrlf=false", "worktree", "add", "--detach", "-q", str(dest), "HEAD")
    return dest


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    init_repo(root)
    (root / "pkg").mkdir()
    (root / "pkg" / "app.py").write_bytes(b"A = 1\nB = 2\n")
    (root / "README.md").write_bytes(b"# demo\n")
    (root / "crlf.txt").write_bytes(b"one\r\ntwo\r\n")
    (root / "blob.bin").write_bytes(b"\x00\x01\r\n\x02")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "seed")
    return root


@pytest.fixture()
def snapshot(repo: Path, tmp_path: Path) -> Path:
    return add_worktree(repo, tmp_path / "snapshot")


def assert_matches_commit(workspace: Path, sha: str) -> None:
    names = git_bytes(workspace, "ls-tree", "-r", "-z", "--name-only", sha).decode("utf-8").split("\0")
    for rel in filter(None, names):
        assert (workspace / rel).read_bytes() == git_bytes(workspace, "cat-file", "blob", f"{sha}:{rel}"), rel


def git_apply(cwd: Path, diff: str, *flags: str) -> None:
    res = subprocess.run(["git", "apply", *flags], cwd=cwd, env=_env(), input=diff.encode("utf-8"),
                         capture_output=True)
    assert res.returncode == 0, res.stderr


def test_make_workspace_is_a_checkout_of_the_snapshot_commit(snapshot: Path, tmp_path: Path):
    ws = run(make_workspace(snapshot, tmp_path / "nested" / "ws"))
    assert ws == tmp_path / "nested" / "ws"
    sha = git(snapshot, "rev-parse", "HEAD").strip()
    assert git(ws, "rev-parse", "HEAD").strip() == sha
    assert git(ws, "rev-parse", "--abbrev-ref", "HEAD").strip() == "HEAD"  # detached
    assert_matches_commit(ws, sha)


def test_make_workspace_failure_raises_with_a_stage(tmp_path: Path):
    with pytest.raises(WorkspaceError) as caught:
        run(make_workspace(tmp_path / "missing", tmp_path / "ws"))
    assert caught.value.stage == "worktree_failed"


def test_make_workspace_leaves_an_occupied_destination_alone(snapshot: Path, tmp_path: Path):
    dest = tmp_path / "occupied"
    dest.mkdir()
    (dest / "keep.txt").write_bytes(b"mine")
    with pytest.raises(WorkspaceError):
        run(make_workspace(snapshot, dest))
    assert (dest / "keep.txt").read_bytes() == b"mine"


def test_crlf_in_the_commit_keeps_its_bytes_under_autocrlf(repo: Path, snapshot: Path, tmp_path: Path):
    git(repo, "config", "core.autocrlf", "true")
    ws = run(make_workspace(snapshot, tmp_path / "ws"))
    assert (ws / "crlf.txt").read_bytes() == b"one\r\ntwo\r\n"
    assert (ws / "README.md").read_bytes() == b"# demo\n"
    assert_matches_commit(ws, git(ws, "rev-parse", "HEAD").strip())
    changes = run(collect_changes(ws, []))
    assert (changes.diff, changes.changed, changes.outside) == ("", [], [])


def test_no_edits_gives_an_empty_diff(snapshot: Path, tmp_path: Path):
    ws = run(make_workspace(snapshot, tmp_path / "ws"))
    changes = run(collect_changes(ws, ["pkg/app.py"]))
    assert (changes.diff, changes.changed, changes.outside) == ("", [], [])


def test_edit_and_new_file_give_a_diff_that_applies(repo: Path, snapshot: Path, tmp_path: Path):
    ws = run(make_workspace(snapshot, tmp_path / "ws"))
    (ws / "pkg" / "app.py").write_bytes(b"A = 1\nB = 3\n")
    (ws / "pkg" / "new.py").write_bytes(b"C = 4\n")
    changes = run(collect_changes(ws, ["pkg/app.py", "pkg/new.py"]))
    assert changes.changed == ["pkg/app.py", "pkg/new.py"]
    assert changes.outside == []
    assert "diff --git a/pkg/app.py b/pkg/app.py" in changes.diff
    clean = add_worktree(repo, tmp_path / "clean")
    git_apply(clean, changes.diff, "--check")
    git_apply(clean, changes.diff)
    assert (clean / "pkg" / "app.py").read_bytes() == b"A = 1\nB = 3\n"
    assert (clean / "pkg" / "new.py").read_bytes() == b"C = 4\n"


def test_prefixes_ignore_the_users_diff_config(repo: Path, snapshot: Path, tmp_path: Path):
    git(repo, "config", "diff.noprefix", "true")
    ws = run(make_workspace(snapshot, tmp_path / "ws"))
    (ws / "README.md").write_bytes(b"# changed\n")
    changes = run(collect_changes(ws, ["README.md"]))
    assert "--- a/README.md" in changes.diff and "+++ b/README.md" in changes.diff


def test_outside_lists_paths_not_allowed(snapshot: Path, tmp_path: Path):
    ws = run(make_workspace(snapshot, tmp_path / "ws"))
    (ws / "pkg" / "app.py").write_bytes(b"A = 9\nB = 2\n")
    (ws / "README.md").write_bytes(b"# touched\n")
    changes = run(collect_changes(ws, ["pkg\\app.py"]))
    assert changes.changed == ["README.md", "pkg/app.py"]
    assert changes.outside == ["README.md"]


def test_a_rename_lists_both_paths_and_a_delete_is_listed(snapshot: Path, tmp_path: Path):
    ws = run(make_workspace(snapshot, tmp_path / "ws"))
    (ws / "README.md").rename(ws / "DOCS.md")
    (ws / "crlf.txt").unlink()
    changes = run(collect_changes(ws, ["DOCS.md", "README.md"]))
    assert changes.changed == ["DOCS.md", "README.md", "crlf.txt"]
    assert changes.outside == ["crlf.txt"]


def test_binary_edit_applies(repo: Path, snapshot: Path, tmp_path: Path):
    ws = run(make_workspace(snapshot, tmp_path / "ws"))
    (ws / "blob.bin").write_bytes(b"\x00\xff\xfe\r\n")
    changes = run(collect_changes(ws, ["blob.bin"]))
    assert "GIT binary patch" in changes.diff
    clean = add_worktree(repo, tmp_path / "clean")
    git_apply(clean, changes.diff, "--check")


def test_crlf_edit_stays_crlf(repo: Path, snapshot: Path, tmp_path: Path):
    ws = run(make_workspace(snapshot, tmp_path / "ws"))
    (ws / "crlf.txt").write_bytes(b"one\r\nTWO\r\n")
    changes = run(collect_changes(ws, ["crlf.txt"]))
    assert "-two\r\n" in changes.diff and "+TWO\r\n" in changes.diff
    clean = add_worktree(repo, tmp_path / "clean")
    git_apply(clean, changes.diff, "--check")
    git_apply(clean, changes.diff)
    assert (clean / "crlf.txt").read_bytes() == b"one\r\nTWO\r\n"


def test_eol_attributes_do_not_invent_changes(tmp_path: Path):
    root = tmp_path / "repo"
    init_repo(root)
    (root / "crlf.bat").write_bytes(b"@echo off\r\necho hi\r\n")
    (root / "lf.bat").write_bytes(b"a\nb\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "scripts")
    (root / ".gitattributes").write_bytes(b"*.bat text eol=crlf\n")
    git(root, "add", ".gitattributes")
    git(root, "commit", "-q", "-m", "attributes")
    snapshot = add_worktree(root, tmp_path / "snapshot")

    ws = run(make_workspace(snapshot, tmp_path / "ws"))
    assert_matches_commit(ws, git(ws, "rev-parse", "HEAD").strip())
    changes = run(collect_changes(ws, []))
    assert (changes.diff, changes.changed, changes.outside) == ("", [], [])

    (ws / "crlf.bat").write_bytes(b"@echo off\r\necho bye\r\n")
    changes = run(collect_changes(ws, ["crlf.bat"]))
    assert changes.changed == ["crlf.bat"]
    assert "-echo hi\r\n" in changes.diff and "+echo bye\r\n" in changes.diff
    assert "@echo off\r\n" in changes.diff and "-@echo off" not in changes.diff
    clean = add_worktree(root, tmp_path / "clean")
    git_apply(clean, changes.diff, "--check")


def test_a_diff_that_is_not_utf8_is_refused(snapshot: Path, tmp_path: Path):
    ws = run(make_workspace(snapshot, tmp_path / "ws"))
    (ws / "latin.txt").write_bytes(b"caf\xe9\n")
    with pytest.raises(WorkspaceError) as caught:
        run(collect_changes(ws, ["latin.txt"]))
    assert caught.value.stage == "not_utf8"


def test_collect_changes_on_a_missing_folder_raises(tmp_path: Path):
    with pytest.raises(WorkspaceError) as caught:
        run(collect_changes(tmp_path / "missing", []))
    assert caught.value.stage == "collect_failed"


def test_remove_workspace_deletes_it_and_can_repeat(repo: Path, snapshot: Path, tmp_path: Path):
    ws = run(make_workspace(snapshot, tmp_path / "piece-attempt-ws"))
    (ws / "README.md").write_bytes(b"dirty\n")
    (ws / "untracked.txt").write_bytes(b"new\n")
    run(remove_workspace(ws))
    assert not ws.exists()
    assert "piece-attempt-ws" not in git(repo, "worktree", "list", "--porcelain")
    run(remove_workspace(ws))
    assert not ws.exists()


def test_remove_workspace_clears_a_leftover_directory(tmp_path: Path):
    leftover = tmp_path / "leftover"
    leftover.mkdir()
    (leftover / "file.txt").write_bytes(b"x")
    run(remove_workspace(leftover))
    assert not leftover.exists()


def test_remove_workspace_refuses_a_full_repository(repo: Path):
    run(remove_workspace(repo))
    assert (repo / ".git").is_dir()
    assert (repo / "README.md").exists()
