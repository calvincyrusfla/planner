"""BaseSnapshots: byte-exact snapshots of the publish branch.

Real git repos (a bare "origin" plus a client clone whose working files are
CRLF like a Windows checkout with core.autocrlf=true, while the blobs are LF).
Skipped when git is missing. Injected-runner tests run everywhere.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from foundry.base_snapshot import (
    DEFAULT_SNAPSHOT_ROOT,
    BaseSnapshots,
    Snapshot,
    SnapshotUnavailable,
)

HAS_GIT = shutil.which("git") is not None
needs_git = pytest.mark.skipif(not HAS_GIT, reason="needs git")


def run(coro):
    return asyncio.run(coro)


def git(cwd: Path, *args: str) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@e", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@e")
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True,
                          text=True).stdout


def git_bytes(cwd: Path, *args: str) -> bytes:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True).stdout


@pytest.fixture()
def repos(tmp_path: Path) -> dict[str, Path]:
    """origin (bare, default main) <- seed commit; client clone with CRLF working files."""
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    client = tmp_path / "client"
    git(tmp_path, "init", "--bare", "-q", "-b", "main", str(origin))
    git(tmp_path, "init", "-q", "-b", "main", str(seed))
    git(seed, "config", "core.autocrlf", "false")
    (seed / "pkg").mkdir()
    (seed / "pkg" / "config.py").write_bytes(b"A = 1\nB = 2\n\ndef f():\n    return A + B\n")
    (seed / "README.md").write_bytes(b"# demo\nline two\n")
    (seed / "bin.dat").write_bytes(b"\x00\x01\r\n\x02")
    (seed / ".gitattributes").write_bytes(b"*.dat binary\ncrlf.txt text eol=crlf\n")
    (seed / "crlf.txt").write_bytes(b"one\ntwo\n")
    git(seed, "add", "-A")
    git(seed, "commit", "-q", "-m", "seed")
    git(seed, "remote", "add", "origin", str(origin))
    git(seed, "push", "-q", "origin", "main")
    git(tmp_path, "clone", "-q", str(origin), str(client))
    git(client, "config", "core.autocrlf", "true")
    # Windows-style checkout: working files CRLF, blobs LF
    for rel in ("pkg/config.py", "README.md"):
        p = client / rel
        p.write_bytes(p.read_bytes().replace(b"\n", b"\r\n"))
    return {"origin": origin, "seed": seed, "client": client, "tmp": tmp_path}


def snaps(repos: dict[str, Path], **kw) -> BaseSnapshots:
    kw.setdefault("root", repos["tmp"] / "snaps")
    return BaseSnapshots(repos["client"], **kw)


def tracked(path: Path, sha: str) -> list[str]:
    return [p for p in git(path, "ls-tree", "-r", "--name-only", sha).splitlines() if p]


@needs_git
def test_snapshot_files_are_byte_identical_to_blobs(repos):
    s = snaps(repos)
    base, sha = run(s.resolve())
    assert base == "main"
    assert sha == git(repos["seed"], "rev-parse", "HEAD").strip()
    path = run(s.checkout(sha))
    assert path == s.path_for(sha) and path.name == sha[:12]
    assert path.parent.parent == repos["tmp"] / "snaps"
    for rel in tracked(repos["client"], sha):
        assert (path / rel).read_bytes() == git_bytes(repos["client"], "show", f"{sha}:{rel}"), rel
    assert b"\r\n" not in (path / "pkg" / "config.py").read_bytes()
    assert (path / "crlf.txt").read_bytes() == b"one\ntwo\n"   # in-tree eol=crlf ignored
    # the user's folder is untouched: still CRLF, same branch, not detached
    assert (repos["client"] / "README.md").read_bytes() == b"# demo\r\nline two\r\n"
    assert git(repos["client"], "symbolic-ref", "--short", "HEAD").strip() == "main"


@needs_git
def test_snapshot_one_call_and_explicit_base(repos):
    git(repos["seed"], "checkout", "-q", "-b", "dev")
    (repos["seed"] / "dev.txt").write_bytes(b"dev\n")
    git(repos["seed"], "add", "-A")
    git(repos["seed"], "commit", "-q", "-m", "dev")
    git(repos["seed"], "push", "-q", "origin", "dev")
    snap = run(snaps(repos).snapshot("dev"))
    assert isinstance(snap, Snapshot) and snap.base == "dev"
    assert snap.sha == git(repos["seed"], "rev-parse", "dev").strip()
    assert (snap.path / "dev.txt").read_bytes() == b"dev\n"
    assert snap.to_dict()["path"] == str(snap.path)


@needs_git
def test_checkout_is_idempotent_and_repairs_dirty(repos):
    s = snaps(repos)
    _, sha = run(s.resolve())
    path = run(s.checkout(sha))
    marker = path / "pkg" / "config.py"
    before = marker.stat().st_mtime_ns
    assert run(s.checkout(sha)) == path
    assert marker.stat().st_mtime_ns == before          # reused, not recreated
    marker.write_bytes(b"dirty\r\n")
    (path / "junk.txt").write_text("x")
    assert run(s.checkout(sha)) == path
    assert marker.read_bytes() == git_bytes(repos["client"], "show", f"{sha}:pkg/config.py")
    assert not (path / "junk.txt").exists()


@needs_git
def test_prune_keeps_referenced_and_release(repos):
    s = snaps(repos)
    _, sha1 = run(s.resolve())
    p1 = run(s.checkout(sha1))
    (repos["seed"] / "README.md").write_bytes(b"# demo v2\n")
    git(repos["seed"], "commit", "-qam", "v2")
    git(repos["seed"], "push", "-q", "origin", "main")
    _, sha2 = run(s.resolve())
    assert sha2 != sha1
    p2 = run(s.checkout(sha2))
    stray = s.repo_dir / "leftover"
    stray.mkdir()
    removed = run(s.prune(keep={sha2}))
    assert set(removed) == {sha1[:12], "leftover"}
    assert not p1.exists() and p2.is_dir()
    listed = git(repos["client"], "worktree", "list", "--porcelain")
    assert str(p1) not in listed
    assert run(s.release(sha2)) is True
    assert not p2.exists()
    assert run(s.release(sha2)) is False
    assert run(s.prune()) == []
    assert "worktree " + str(repos["client"]) in git(repos["client"], "worktree", "list", "--porcelain")
    # the user's branches are untouched
    assert git(repos["client"], "branch", "--format=%(refname:short)").split() == ["main"]


@needs_git
def test_fetch_failure_raises_unavailable(repos):
    s = snaps(repos)
    with pytest.raises(SnapshotUnavailable) as exc:
        run(s.resolve("no-such-branch"))
    assert exc.value.stage == "fetch_failed"
    shutil.rmtree(repos["origin"])
    with pytest.raises(SnapshotUnavailable) as exc:
        run(s.resolve("main"))
    assert exc.value.stage == "fetch_failed"


@needs_git
def test_no_remote_and_not_a_repo(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(SnapshotUnavailable) as exc:
        run(BaseSnapshots(plain, root=tmp_path / "s").resolve())
    assert exc.value.stage == "not_a_repo"
    git(tmp_path, "init", "-q", str(plain))
    with pytest.raises(SnapshotUnavailable) as exc:
        run(BaseSnapshots(plain, root=tmp_path / "s").resolve())
    assert exc.value.stage == "no_remote"


@needs_git
def test_refuses_root_inside_tree_unless_ignored(repos):
    _, sha = run(snaps(repos).resolve())
    inside = BaseSnapshots(repos["client"], root=repos["client"] / "snaps")
    with pytest.raises(SnapshotUnavailable) as exc:
        run(inside.checkout(sha))
    assert exc.value.stage == "unsafe_root"
    (repos["client"] / ".git" / "info" / "exclude").write_text("/snaps/\n")
    ok = BaseSnapshots(repos["client"], root=repos["client"] / "snaps")
    path = run(ok.checkout(sha))
    assert path.is_dir()
    assert git(repos["client"], "status", "--porcelain", "--untracked-files=all", "--", "snaps") == ""


def test_bad_inputs_and_missing_git(tmp_path):
    s = BaseSnapshots(tmp_path, root=tmp_path / "s")
    with pytest.raises(SnapshotUnavailable) as exc:
        s.path_for("abc")
    assert exc.value.stage == "bad_ref"
    assert s.exists("nope") is False
    with pytest.raises(SnapshotUnavailable):
        run(s.checkout("--upload-pack=evil"))
    nogit = BaseSnapshots(tmp_path, root=tmp_path / "s", git="definitely-not-git-xyz")
    with pytest.raises(SnapshotUnavailable) as exc:
        run(nogit.resolve())
    assert exc.value.stage == "no_git"
    assert DEFAULT_SNAPSHOT_ROOT.parts[-2:] == ("data", "base")


def test_injected_runner_resolve_and_bad_base(tmp_path):
    calls: list[list[str]] = []
    sha = "a" * 40

    async def runner(argv, *, cwd=None, timeout=None, env=None):
        calls.append(list(argv))
        assert env is not None and "GIT_DIR" not in env and timeout
        if argv[1:3] == ["symbolic-ref", "--quiet"] and argv[-1].endswith("/HEAD") and "refs/remotes" in argv[-1]:
            return 0, "origin/trunk\n", ""
        if argv[1] == "rev-parse" and argv[2] == "--verify":
            return 0, sha + "\n", ""
        return 0, "", ""

    os.environ["GIT_DIR"] = "/elsewhere"
    try:
        s = BaseSnapshots(tmp_path, runner=runner, root=tmp_path / "s")
        assert run(s.resolve()) == ("trunk", sha)
    finally:
        del os.environ["GIT_DIR"]
    fetch = next(c for c in calls if c[1] == "fetch")
    assert fetch[-3:] == ["--", "origin", "+refs/heads/trunk:refs/remotes/origin/trunk"]
    with pytest.raises(SnapshotUnavailable) as exc:
        run(s.resolve("-oops"))
    assert exc.value.stage == "bad_ref"
