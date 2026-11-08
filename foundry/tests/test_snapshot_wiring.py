"""T-036 (alpha-2): every task is built from a clean snapshot of its publish branch.

The Floor Manager pins each accepted task to the commit of the branch its PR
targets (persisted), the workers read that snapshot (context engine, Claude's
cwd, gates' base copy, stacked workspace, diff normalisation), the combined
patch is stacked and ``git apply --check``-ed on it, and the publisher branches
from exactly that commit. When snapshots are unavailable the user's folder is
used as before, with one warning per task. No real Claude, Gemma or GitHub.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from foundry.base_snapshot import BaseSnapshots, SnapshotUnavailable
from foundry.board_bridge import BoardBridge
from foundry.claude_client import ClaudeResult, SimulatedClaudeClient
from foundry.config import FoundryConfig
from foundry.floor_manager import WorkOrder, WorkspaceGates, build_factory
from foundry.git_publisher import GitPublisher
from foundry.main import append_diff, demo_gemma, write_demo_repo

HAS_GIT = shutil.which("git") is not None
POSIX = sys.platform != "win32"
needs_git = pytest.mark.skipif(not HAS_GIT, reason="needs git")
needs_sh = pytest.mark.skipif(not (HAS_GIT and POSIX), reason="needs git and a POSIX shell for the fake gh")

ORDER = {"title": "Add mathx helper", "description": "Add a helper to mathx.", "files": ["shop/mathx.py"]}


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class FakePublisher:
    """Records publish calls (with ``base`` / ``base_sha``); always succeeds."""

    def __init__(self, base: str | None = None) -> None:
        self.base = base
        self.calls: list[dict[str, Any]] = []

    async def publish(self, task_id: str, title: str, patch_path: Any, pr: dict[str, Any], *,
                      base: str | None = None, base_sha: str | None = None) -> dict[str, Any]:
        self.calls.append({"task_id": task_id, "patch": Path(patch_path).read_bytes(), "base": base,
                           "base_sha": base_sha})
        return {"ok": True, "branch": f"foundry/{task_id.lower()}-x", "commit": "abc1234",
                "pr_url": "https://github.com/me/repo/pull/1", "base": base}


class OldPublisher:
    """A publisher without ``base_sha`` (older code / third-party fake)."""

    def __init__(self) -> None:
        self.base = None
        self.calls: list[dict[str, Any]] = []

    async def publish(self, task_id: str, title: str, patch_path: Any, pr: dict[str, Any]) -> dict[str, Any]:
        self.calls.append({"task_id": task_id})
        return {"ok": True, "branch": "foundry/x", "pr_url": "u"}


def _claude() -> SimulatedClaudeClient:
    """Builders append a helper to every allowed file, reading it from the cwd Foundry gives Claude."""
    holder: dict[str, SimulatedClaudeClient] = {}

    def script(prompt: str, agent_id: str) -> ClaudeResult:
        if prompt.startswith("You are reviewer") or "## Unified diff" in prompt:
            text = '{"verdict": "APPROVED", "directives": []}'
        else:
            cwd = Path(holder["c"].history[-1].cwd or ".")
            src = (cwd / "shop/mathx.py").read_text(encoding="utf-8")  # universal newlines
            new = ["", "", "def mathx_helper(value):", '    """Helper."""', "    return value"]
            text = "Here is the change.\n\n```diff\n" + append_diff("shop/mathx.py", src, new) + "```\n"
        return ClaudeResult(text=text, input_tokens=10, output_tokens=10, duration_seconds=0.0)

    holder["c"] = SimulatedClaudeClient(script)
    return holder["c"]


def _config(tmp_path: Path) -> FoundryConfig:
    return FoundryConfig(db_path=tmp_path / "foundry.db", rpm=1000, tpm=10_000_000, max_concurrent=5,
                         teams=["alpha"], board_bridge_mode="dry", pre_review="off")


def _factory(tmp_path: Path, repo: Path, publisher: Any, snapshots: Any, claude: Any = None) -> Any:
    return build_factory(_config(tmp_path), claude=claude or _claude(), gemma=demo_gemma(), repo_root=repo,
                         workspace=repo, bridge=BoardBridge("dry"), patches_dir=tmp_path / "patches",
                         publisher=publisher, snapshots=snapshots)


@pytest.fixture
def world(tmp_path: Path) -> dict[str, Any]:
    """Bare origin with the demo repo on main; the user's folder on another branch, CRLF working files."""
    if not HAS_GIT:
        pytest.skip("needs git")
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(origin)], check=True)
    repo = write_demo_repo(tmp_path / "repo")
    _git(repo, "init", "-q", "-b", "main")
    for k, v in (("user.name", "Ada"), ("user.email", "ada@example.com"), ("commit.gpgsign", "false"),
                 ("core.autocrlf", "false")):
        _git(repo, "config", k, v)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-q", "-u", "origin", "main")
    base_sha = _git(repo, "rev-parse", "HEAD")
    # the user's folder: another branch with an extra commit, then CRLF working files (autocrlf-style)
    _git(repo, "checkout", "-q", "-b", "my-work")
    f = repo / "shop/mathx.py"
    f.write_text(f.read_text(encoding="utf-8") + "USER_ONLY = 1\n", encoding="utf-8", newline="\n")
    _git(repo, "commit", "-q", "-am", "user work")
    f.write_bytes(f.read_bytes().replace(b"\n", b"\r\n"))
    return {"tmp": tmp_path, "origin": origin, "repo": repo, "sha": base_sha, "mathx": f.read_bytes(),
            "snapshots": BaseSnapshots(repo, root=tmp_path / "base")}


async def _run(f: Any) -> str:
    async with f.running():
        tid = await f.manager.submit(WorkOrder(**ORDER))
        await f.wait([tid], timeout=60)
    await f.manager.drain()
    return tid


# --------------------------------------------------------------------------- end to end (manager + workers)


@needs_git
def test_task_is_built_checked_and_published_from_the_pinned_snapshot(world: dict[str, Any]) -> None:
    tmp, repo = world["tmp"], world["repo"]
    pub = FakePublisher()
    claude = _claude()
    f = _factory(tmp, repo, pub, world["snapshots"], claude)
    tid = asyncio.run(_run(f))
    m = f.manager
    pinned = [e["payload"] for e in m.queue.events(task_id=tid, kind="base_pinned")]
    assert pinned == [{"base": "main", "sha": world["sha"]}]
    snap = world["snapshots"].path_for(world["sha"])
    # Claude (builder and reviewer) read the snapshot, never the user's folder
    cwds = {c.cwd for c in claude.history}
    assert cwds == {str(snap)}, cwds
    # the combined patch was checked on the snapshot: written as .patch, LF, without the user's commit
    patch = tmp / "patches" / f"{tid}.patch"
    assert patch.is_file() and not (tmp / "patches" / f"{tid}.patch.rejected").exists()
    data = patch.read_bytes()
    assert b"\r" not in data and b"USER_ONLY" not in data and b"mathx_helper" in data
    # it applies --cached clean on the base commit
    clone = tmp / "clone"
    subprocess.run(["git", "clone", "-q", str(world["origin"]), str(clone)], check=True)
    _git(clone, "apply", "--check", "--cached", str(patch))
    # build == publish: the publisher branches from exactly the pinned commit
    assert pub.calls == [{"task_id": tid, "patch": data, "base": "main", "base_sha": world["sha"]}]
    assert m.pr_state(tid)["state"] == "opened"
    # the finished task's snapshot is pruned; the user's folder is untouched
    assert not snap.exists()
    assert (repo / "shop/mathx.py").read_bytes() == world["mathx"]
    assert _git(repo, "symbolic-ref", "--short", "HEAD") == "my-work"
    assert _git(repo, "status", "--porcelain") == "M shop/mathx.py"  # only the CRLF bytes we wrote
    # the PR is open: nothing reads the pin again, so it is forgotten
    assert tid not in m.task_bases
    f.close()


@needs_git
def test_the_users_folder_would_have_rejected_the_patch(world: dict[str, Any]) -> None:
    """Control: without snapshots the CRLF folder on another branch yields a patch that fails on the base."""
    tmp, repo = world["tmp"], world["repo"]
    pub = FakePublisher()
    claude = _claude()
    f = _factory(tmp, repo, pub, None, claude)
    tid = asyncio.run(_run(f))
    assert f.manager.task_bases == {}
    assert {c.cwd for c in claude.history} == {str(repo.resolve())}
    patch = tmp / "patches" / f"{tid}.patch"
    clone = tmp / "clone"
    subprocess.run(["git", "clone", "-q", str(world["origin"]), str(clone)], check=True)
    proc = subprocess.run(["git", "apply", "--check", "--cached", str(patch)], cwd=clone,
                          capture_output=True, text=True)
    assert proc.returncode != 0  # built on the wrong code: the bug this task fixes
    assert pub.calls and pub.calls[0]["base_sha"] is None
    f.close()


class BrokenSnapshots:
    """Snapshots that are never available (no git / no remote / fetch failed)."""

    def __init__(self) -> None:
        self.pruned: list[set[str]] = []

    async def resolve(self, base: str | None = None) -> tuple[str, str]:
        raise SnapshotUnavailable("fetch_failed", "git fetch origin main failed: offline")

    async def checkout(self, sha: str) -> Path:  # pragma: no cover - never reached
        raise SnapshotUnavailable("no_commit", "nope")

    async def prune(self, keep: Any = ()) -> list[str]:
        self.pruned.append(set(keep))
        return []


def test_fallback_to_the_users_folder_warns_once_per_task(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    repo = write_demo_repo(tmp_path / "repo")
    pub = FakePublisher()
    claude = _claude()
    f = _factory(tmp_path, repo, pub, BrokenSnapshots(), claude)
    with caplog.at_level(logging.WARNING, logger="foundry.floor_manager"):
        tid = asyncio.run(_run(f))
    assert f.manager.task_bases == {}
    warns = [r for r in caplog.records if "built from your working folder" in r.getMessage()]
    assert len(warns) == 1 and tid in warns[0].getMessage() and "offline" in warns[0].getMessage()
    assert {c.cwd for c in claude.history} == {str(repo.resolve())}
    assert (tmp_path / "patches" / f"{tid}.patch").is_file()
    assert pub.calls[0]["base_sha"] is None and pub.calls[0]["base"] is None
    f.close()


def test_auto_pr_off_never_pins(tmp_path: Path) -> None:
    repo = write_demo_repo(tmp_path / "repo")
    snaps = BrokenSnapshots()
    f = build_factory(_config(tmp_path).model_copy(update={"auto_pr": False}), claude=_claude(),
                      gemma=demo_gemma(), repo_root=repo, workspace=repo, bridge=BoardBridge("dry"),
                      patches_dir=tmp_path / "patches", publisher=FakePublisher(), snapshots=snaps)
    assert not f.manager.snapshots_enabled
    tid = asyncio.run(_run(f))
    assert f.manager.task_bases == {} and (tmp_path / "patches" / f"{tid}.patch").is_file()
    f.close()


class FakeSnapshots:
    """In-memory snapshots: ``checkout`` copies a fixed tree; records every call."""

    def __init__(self, src: Path, root: Path, sha: str = "a" * 40) -> None:
        self.src, self.root, self.sha = src, root, sha
        self.calls: list[tuple[str, Any]] = []

    async def resolve(self, base: str | None = None) -> tuple[str, str]:
        self.calls.append(("resolve", base))
        return base or "main", self.sha

    async def checkout(self, sha: str) -> Path:
        self.calls.append(("checkout", sha))
        path = self.root / sha[:12]
        if not path.exists():
            shutil.copytree(self.src, path)
        return path

    async def prune(self, keep: Any = ()) -> list[str]:
        keep = {str(k)[:12] for k in keep}
        self.calls.append(("prune", keep))
        out = []
        for p in sorted(self.root.iterdir()) if self.root.is_dir() else []:
            if p.name not in keep:
                shutil.rmtree(p)
                out.append(p.name)
        return out


def test_pin_uses_the_orders_pr_base_and_is_idempotent(tmp_path: Path) -> None:
    repo = write_demo_repo(tmp_path / "repo")
    snaps = FakeSnapshots(repo, tmp_path / "base")
    f = _factory(tmp_path, repo, FakePublisher(base="develop"), snaps)
    m = f.manager

    async def go() -> None:
        tid = m._prepare(WorkOrder(**ORDER, pr_base="release"))
        assert await m.pin_base(tid) == {"base": "release", "sha": "a" * 40}
        assert await m.pin_base(tid) == {"base": "release", "sha": "a" * 40}
        assert [c for c in snaps.calls if c[0] == "resolve"] == [("resolve", "release")]
        tid2 = m._prepare(WorkOrder(title="other", description="x", files=["shop/mathx.py"]))
        assert (await m.pin_base(tid2))["base"] == "develop"  # the publisher's base
        assert await m.task_base(tid) == tmp_path / "base" / ("a" * 12)
        # an unknown snapshot is pruned; open tasks keep theirs
        (tmp_path / "base" / "deadbeefdead").mkdir()
        assert await m.prune_snapshots() == ["deadbeefdead"]
        assert (tmp_path / "base" / ("a" * 12)).is_dir()
        # quarantined: its snapshot goes (both tasks share the commit, so close both)
        for t in (tid, tid2):
            m.queue.quarantine(t, "test")
        assert await m.prune_snapshots() == ["a" * 12]
        # undo/retry: the snapshot comes back at the pinned commit
        assert await m.task_base(tid) == tmp_path / "base" / ("a" * 12)
        assert (tmp_path / "base" / ("a" * 12) / "shop/mathx.py").is_file()

    asyncio.run(go())
    f.close()

    # pins survive a restart
    g = _factory(tmp_path, repo, FakePublisher(), FakeSnapshots(repo, tmp_path / "base"))
    assert set(g.manager.task_bases) == {"F-001", "F-002"}
    g.close()


def _mark_complete(m: Any, tid: str) -> None:
    with m.queue.transaction() as conn:
        conn.execute("UPDATE tasks SET state='complete' WHERE task_id=?", (tid,))


def test_a_task_still_finishing_keeps_its_snapshot(tmp_path: Path) -> None:
    """The queue marks a task complete before _finish stacks and checks its patch: never prune then."""
    repo = write_demo_repo(tmp_path / "repo")
    f = _factory(tmp_path, repo, FakePublisher(), FakeSnapshots(repo, tmp_path / "base"))
    m = f.manager

    async def go() -> None:
        tid = m._prepare(WorkOrder(**ORDER))
        await m.pin_base(tid)
        root = await m.build_root(tid)
        _mark_complete(m, tid)
        assert tid not in m._completed
        assert await m.prune_snapshots() == []
        assert root.is_dir()
        # a pending PR keeps it too, even once the patch is written
        m._completed.add(tid)
        m._set_pr(tid, "pending")
        assert await m.prune_snapshots() == []
        m._set_pr(tid, "opened")
        assert await m.prune_snapshots() == ["a" * 12]
        assert tid not in m.task_bases  # settled: the pin is forgotten

    asyncio.run(go())
    f.close()


def test_a_leased_snapshot_is_never_pruned(tmp_path: Path) -> None:
    repo = write_demo_repo(tmp_path / "repo")
    f = _factory(tmp_path, repo, FakePublisher(), FakeSnapshots(repo, tmp_path / "base"))
    m = f.manager

    async def go() -> None:
        tid = m._prepare(WorkOrder(**ORDER))
        await m.pin_base(tid)
        async with m.base_lease(tid) as root:
            m.queue.quarantine(tid, "test")  # e.g. quarantined while a worker copies its snapshot
            assert await m.prune_snapshots() == []
            assert root is not None and root.is_dir()
        assert m._snapshot_users == {}
        assert await m.prune_snapshots() == ["a" * 12]
        assert tid in m.task_bases  # quarantined: kept for an undo / retry

    asyncio.run(go())
    f.close()


class FlakySnapshots(FakeSnapshots):
    """Offline at first (resolve fails), online later."""

    def __init__(self, *a: Any, **kw: Any) -> None:
        super().__init__(*a, **kw)
        self.online = False

    async def resolve(self, base: str | None = None) -> tuple[str, str]:
        if not self.online:
            raise SnapshotUnavailable("fetch_failed", "offline")
        return await super().resolve(base)


def test_a_failed_pin_is_retried_until_work_is_approved(tmp_path: Path) -> None:
    repo = write_demo_repo(tmp_path / "repo")
    snaps = FlakySnapshots(repo, tmp_path / "base")
    f = _factory(tmp_path, repo, FakePublisher(), snaps)
    m = f.manager

    async def go() -> None:
        tid = m._prepare(WorkOrder(**ORDER))
        assert await m.pin_base(tid) is None
        assert await m.task_base(tid) is None  # still offline
        snaps.online = True
        assert await m.task_base(tid) == tmp_path / "base" / ("a" * 12)
        assert m.task_bases[tid]["sha"] == "a" * 40
        # once work is approved on the user's folder, it never switches base
        tid2 = m._prepare(WorkOrder(title="two", description="x", files=["shop/mathx.py"]))
        m.diffs[tid2] = {f"{tid2}.1": "--- a/x\n+++ b/x\n"}
        assert await m.task_base(tid2) is None and tid2 not in m.task_bases

    asyncio.run(go())
    f.close()


def test_checkout_runs_once_per_commit_per_run(tmp_path: Path) -> None:
    repo = write_demo_repo(tmp_path / "repo")
    snaps = FakeSnapshots(repo, tmp_path / "base")
    f = _factory(tmp_path, repo, FakePublisher(), snaps)
    m = f.manager

    async def go() -> None:
        tid = m._prepare(WorkOrder(**ORDER))
        await m.pin_base(tid)
        for _ in range(3):
            await m.task_base(tid)
        assert [c for c in snaps.calls if c[0] == "checkout"] == [("checkout", "a" * 40)]
        shutil.rmtree(tmp_path / "base" / ("a" * 12))  # gone: checked out (and verified) again
        assert (await m.task_base(tid)).is_dir()
        assert len([c for c in snaps.calls if c[0] == "checkout"]) == 2

    asyncio.run(go())
    f.close()


def test_scratch_copies_skip_foundry_data(tmp_path: Path) -> None:
    from foundry.floor_manager import _copy_workspace
    from foundry.validation_gates import prepare_stacked_workspace

    src = tmp_path / "src"
    (src / "foundry/data/base/x").mkdir(parents=True)
    (src / "foundry/data/base/x/big.py").write_text("x = 1\n", encoding="utf-8")
    (src / "foundry/core.py").write_text("y = 1\n", encoding="utf-8")
    (src / "data").mkdir()
    (src / "data/keep.txt").write_text("k\n", encoding="utf-8")
    _copy_workspace(src, tmp_path / "a")
    out = prepare_stacked_workspace(src, tmp_path / "b", [])
    for d in (tmp_path / "a", Path(out)):
        assert (d / "foundry/core.py").is_file() and (d / "data/keep.txt").is_file()
        assert not (d / "foundry/data").exists()


def test_old_publisher_without_base_sha_still_publishes(tmp_path: Path) -> None:
    repo = write_demo_repo(tmp_path / "repo")
    pub = OldPublisher()
    f = _factory(tmp_path, repo, pub, FakeSnapshots(repo, tmp_path / "base"))
    tid = asyncio.run(_run(f))
    assert pub.calls == [{"task_id": tid}] and f.manager.pr_state(tid)["state"] == "opened"
    f.close()


# --------------------------------------------------------------------------- gates


class RecordingGates:
    def __init__(self) -> None:
        self.seen: list[tuple[Path, str]] = []

    async def validate_async(self, submission: Any, workspace: Path, allowed_files: Any = None) -> Any:
        self.seen.append((workspace, (workspace / "f.txt").read_text(encoding="utf-8")))
        return []


def test_workspace_gates_copy_the_given_base(tmp_path: Path) -> None:
    user, snap = tmp_path / "user", tmp_path / "snap"
    for d, text in ((user, "user\r\n"), (snap, "snap\n")):
        d.mkdir()
        (d / "f.txt").write_bytes(text.encode())
    rec = RecordingGates()
    gates = WorkspaceGates(rec, user)

    class A:
        allowed_files = ["f.txt"]

    asyncio.run(gates.run(object(), A(), base=snap))
    asyncio.run(gates.run(object(), A()))
    assert [t for _, t in rec.seen] == ["snap\n", "user\n"]
    assert all(not p.exists() for p, _ in rec.seen)  # scratch copies are removed
    assert (snap / "f.txt").read_bytes() == b"snap\n"  # the snapshot is only read


# --------------------------------------------------------------------------- publisher

FAKE_GH = """#!/bin/sh
if [ "$1" = "auth" ]; then exit 0; fi
if [ "$1" = "pr" ] && [ "$2" = "list" ]; then echo "[]"; exit 0; fi
if [ "$1" = "pr" ] && [ "$2" = "create" ]; then echo "https://github.com/acme/widget/pull/3"; exit 0; fi
exit 2
"""


@needs_sh
def test_publish_branches_from_the_pinned_commit_even_when_the_base_moved(tmp_path: Path) -> None:
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(origin)], check=True)
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    for k, v in (("user.name", "Ada"), ("user.email", "ada@example.com"), ("commit.gpgsign", "false")):
        _git(repo, "config", k, v)
    (repo / "hello.txt").write_text("hello\n", encoding="utf-8")
    _git(repo, "add", "hello.txt")
    _git(repo, "commit", "-q", "-m", "init")
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-q", "-u", "origin", "main")
    pinned = _git(repo, "rev-parse", "HEAD")
    # someone else moves main on the remote after the task was pinned
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(origin), str(other)], check=True)
    for k, v in (("user.name", "Bob"), ("user.email", "bob@example.com"), ("commit.gpgsign", "false")):
        _git(other, "config", k, v)
    (other / "later.txt").write_text("later\n", encoding="utf-8")
    _git(other, "add", "later.txt")
    _git(other, "commit", "-q", "-m", "later")
    _git(other, "push", "-q", "origin", "main")
    gh = tmp_path / "gh"
    gh.write_text(FAKE_GH, encoding="utf-8", newline="\n")
    gh.chmod(0o755)
    patch = tmp_path / "F-001.patch"
    patch.write_text("diff --git a/hello.txt b/hello.txt\n--- a/hello.txt\n+++ b/hello.txt\n"
                     "@@ -1 +1,2 @@\n hello\n+world\n", encoding="utf-8", newline="\n")
    pub = GitPublisher(repo, gh=str(gh), worktree_root=tmp_path / "wt")
    res = asyncio.run(pub.publish("F-001", "Add world", patch, {"title": "Add world"}, base_sha=pinned))
    assert res.ok, res
    assert res.base == "main"
    assert _git(origin, "rev-parse", f"{res.branch}~1") == pinned  # not the moved tip
    assert _git(origin, "show", f"{res.branch}:hello.txt") == "hello\nworld"
    # an unknown commit fails clearly; an option-like one is refused before git runs
    bad = asyncio.run(pub.publish("F-002", "x", patch, {}, base_sha="0" * 40))
    assert not bad.ok and bad.stage == "fetch_failed" and "000000000000" in bad.error
    worse = asyncio.run(pub.publish("F-003", "x", patch, {}, base_sha="--upload-pack=evil"))
    assert not worse.ok and worse.stage == "bad_ref"
