"""With snapshots on, a piece is never built from the user's folder, and starts on a fresh base."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Any

import pytest

from foundry.base_snapshot import SnapshotUnavailable
from foundry.board_bridge import BoardBridge
from foundry.claude_client import ClaudeResult, SimulatedClaudeClient
from foundry.config import FoundryConfig
from foundry.floor_helpers import InfrastructureError
from foundry.floor_manager import BaseUnavailable, WorkOrder, build_factory
from foundry.main import demo_gemma, write_demo_repo

ORDER: dict[str, Any] = {"title": "Add mathx helper", "description": "Add a helper.", "files": ["shop/mathx.py"]}
SHA_A, SHA_B, SHA_C = "a" * 40, "b" * 40, "c" * 40


class FakePublisher:
    base: str | None = None

    async def publish(self, task_id: str, title: str, patch_path: Any, pr: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("nothing is published in these tests")


class FakeSnapshots:
    """``checkout`` copies a fixed tree; ``sha`` is the remote's current commit."""

    def __init__(self, src: Path, root: Path) -> None:
        self.src, self.root, self.sha = src, root, SHA_A
        self.fail_resolve = False
        self.fail_stage = "fetch_failed"
        self.fail_checkout = False
        self.resolves = 0

    async def resolve(self, base: str | None = None) -> tuple[str, str]:
        self.resolves += 1
        if self.fail_resolve:
            raise SnapshotUnavailable(self.fail_stage, "offline")
        return base or "main", self.sha

    async def checkout(self, sha: str) -> Path:
        if self.fail_checkout:
            raise SnapshotUnavailable("worktree_failed", "disk full")
        path = self.root / sha[:12]
        if not path.exists():
            shutil.copytree(self.src, path)
        return path

    async def prune(self, keep: Any = ()) -> list[str]:
        return []


def _claude() -> SimulatedClaudeClient:
    def script(prompt: str, agent_id: str) -> ClaudeResult:
        raise AssertionError("Claude must not be called without a base")

    return SimulatedClaudeClient(script)


def _factory(tmp_path: Path, *, snapshots: bool = True, auto_pr: bool = True) -> tuple[Any, FakeSnapshots]:
    repo = write_demo_repo(tmp_path / "repo")
    snaps = FakeSnapshots(repo, tmp_path / "base")
    config = FoundryConfig(db_path=tmp_path / "foundry.db", rpm=1000, tpm=10_000_000, max_concurrent=5,
                           teams=["alpha"], board_bridge_mode="dry", pre_review="off", auto_pr=auto_pr)
    f = build_factory(config, claude=_claude(), gemma=demo_gemma(), repo_root=repo, workspace=repo,
                      bridge=BoardBridge("dry"), patches_dir=tmp_path / "patches", publisher=FakePublisher(),
                      snapshots=snaps if snapshots else None)
    return f, snaps


def _snapshot_dir(tmp_path: Path, sha: str) -> Path:
    return tmp_path / "base" / sha[:12]


def test_base_unavailable_is_an_infrastructure_error() -> None:
    assert issubclass(BaseUnavailable, InfrastructureError)


def test_checkout_failure_raises_base_unavailable(tmp_path: Path) -> None:
    f, snaps = _factory(tmp_path)
    m = f.manager

    async def go() -> None:
        tid = m._prepare(WorkOrder(**ORDER))
        await m.pin_base(tid)
        shutil.rmtree(_snapshot_dir(tmp_path, SHA_A))  # gone: task_base has to check it out again
        snaps.fail_checkout = True
        with pytest.raises(BaseUnavailable, match="disk full"):
            await m.task_base(tid)
        snaps.fail_checkout = False
        assert await m.task_base(tid) == _snapshot_dir(tmp_path, SHA_A)

    asyncio.run(go())
    f.close()


def test_no_pin_and_offline_raises_base_unavailable(tmp_path: Path) -> None:
    f, snaps = _factory(tmp_path)
    m = f.manager
    snaps.fail_resolve = True

    async def go() -> None:
        tid = m._prepare(WorkOrder(**ORDER))
        assert await m.pin_base(tid) is None  # it only pins
        with pytest.raises(BaseUnavailable, match="offline"):
            await m.task_base(tid)
        with pytest.raises(BaseUnavailable):
            async with m.base_lease(tid):
                raise AssertionError("the lease must not be entered")
        assert m._snapshot_users == {}

    asyncio.run(go())
    f.close()


def test_no_pin_with_approved_work_raises_base_unavailable(tmp_path: Path) -> None:
    f, snaps = _factory(tmp_path)
    m = f.manager

    async def go() -> None:
        tid = m._prepare(WorkOrder(**ORDER))
        m.diffs[tid] = {f"{tid}.1": "--- a/x\n+++ b/x\n"}
        with pytest.raises(BaseUnavailable):
            await m.task_base(tid)
        assert tid not in m.task_bases

    asyncio.run(go())
    f.close()


@pytest.mark.parametrize("stage", ["no_git", "not_a_repo", "no_remote"])
def test_a_folder_with_no_publish_branch_builds_as_before(tmp_path: Path, stage: str) -> None:
    f, snaps = _factory(tmp_path)
    m = f.manager
    snaps.fail_resolve = True
    snaps.fail_stage = stage

    async def go() -> None:
        tid = m._prepare(WorkOrder(**ORDER))
        assert await m.task_base(tid) is None and tid not in m.task_bases

    asyncio.run(go())
    f.close()


def test_snapshots_off_returns_none(tmp_path: Path) -> None:
    f, _ = _factory(tmp_path, snapshots=False)
    m = f.manager

    async def go() -> None:
        tid = m._prepare(WorkOrder(**ORDER))
        assert await m.pin_base(tid) is None
        assert await m.task_base(tid) is None
        async with m.base_lease(tid) as root:
            assert root is None

    asyncio.run(go())
    f.close()


def test_auto_pr_off_returns_none_even_when_the_checkout_fails(tmp_path: Path) -> None:
    f, snaps = _factory(tmp_path, auto_pr=False)
    m = f.manager
    assert not m.snapshots_enabled
    snaps.fail_checkout = True

    async def go() -> None:
        tid = m._prepare(WorkOrder(**ORDER))
        assert await m.task_base(tid) is None
        m.task_bases[tid] = {"base": "main", "sha": SHA_A}  # pinned while snapshots were on
        assert await m.task_base(tid) is None
        assert snaps.resolves == 0

    asyncio.run(go())
    f.close()


def test_a_moved_remote_re_pins_once_before_the_first_build(tmp_path: Path) -> None:
    f, snaps = _factory(tmp_path)
    m = f.manager

    async def go() -> str:
        tid = m._prepare(WorkOrder(**ORDER))
        await m.pin_base(tid)
        snaps.sha = SHA_B
        assert await m.task_base(tid) == _snapshot_dir(tmp_path, SHA_B)
        assert m.task_bases[tid] == {"base": "main", "sha": SHA_B}
        snaps.sha = SHA_C  # moved again: a retry keeps its base
        assert await m.task_base(tid) == _snapshot_dir(tmp_path, SHA_B)
        assert snaps.resolves == 2
        return tid

    tid = asyncio.run(go())
    assert [e["payload"] for e in m.queue.events(task_id=tid, kind="base_repinned")] == [
        {"base": "main", "old_sha": SHA_A, "new_sha": SHA_B}]
    f.close()


def test_concurrent_first_builds_share_one_re_pin(tmp_path: Path) -> None:
    f, snaps = _factory(tmp_path)
    m = f.manager

    async def go() -> None:
        tid = m._prepare(WorkOrder(**ORDER))
        await m.pin_base(tid)
        snaps.sha = SHA_B
        roots = await asyncio.gather(m.task_base(tid), m.task_base(tid), m.task_base(tid))
        assert roots == [_snapshot_dir(tmp_path, SHA_B)] * 3
        assert snaps.resolves == 2

    asyncio.run(go())
    f.close()


def test_an_unmoved_remote_keeps_the_pin_without_an_event(tmp_path: Path) -> None:
    f, snaps = _factory(tmp_path)
    m = f.manager

    async def go() -> str:
        tid = m._prepare(WorkOrder(**ORDER))
        await m.pin_base(tid)
        assert await m.task_base(tid) == _snapshot_dir(tmp_path, SHA_A)
        return tid

    tid = asyncio.run(go())
    assert m.queue.events(task_id=tid, kind="base_repinned") == []
    f.close()


def test_no_re_pin_after_work_is_approved(tmp_path: Path) -> None:
    f, snaps = _factory(tmp_path)
    m = f.manager

    async def go() -> str:
        tid = m._prepare(WorkOrder(**ORDER))
        await m.pin_base(tid)
        m.diffs[tid] = {f"{tid}.1": "--- a/x\n+++ b/x\n"}
        snaps.sha = SHA_B
        assert await m.task_base(tid) == _snapshot_dir(tmp_path, SHA_A)
        assert m.task_bases[tid]["sha"] == SHA_A and snaps.resolves == 1
        return tid

    tid = asyncio.run(go())
    assert m.queue.events(task_id=tid, kind="base_repinned") == []
    f.close()


def test_offline_at_the_re_check_keeps_the_pin(tmp_path: Path) -> None:
    f, snaps = _factory(tmp_path)
    m = f.manager

    async def go() -> None:
        tid = m._prepare(WorkOrder(**ORDER))
        await m.pin_base(tid)
        snaps.fail_resolve = True
        assert await m.task_base(tid) == _snapshot_dir(tmp_path, SHA_A)
        snaps.fail_resolve = False
        snaps.sha = SHA_B  # the first build started on A: its retries stay there
        assert await m.task_base(tid) == _snapshot_dir(tmp_path, SHA_A)

    asyncio.run(go())
    f.close()


def test_a_moved_remote_without_a_snapshot_is_unavailable_then_retried(tmp_path: Path) -> None:
    f, snaps = _factory(tmp_path)
    m = f.manager

    async def go() -> None:
        tid = m._prepare(WorkOrder(**ORDER))
        await m.pin_base(tid)
        snaps.sha = SHA_B
        snaps.fail_checkout = True
        with pytest.raises(BaseUnavailable, match="disk full"):
            await m.task_base(tid)
        assert m.task_bases[tid]["sha"] == SHA_A
        snaps.fail_checkout = False
        assert await m.task_base(tid) == _snapshot_dir(tmp_path, SHA_B)

    asyncio.run(go())
    f.close()


@pytest.mark.parametrize("lease", [True, False], ids=["base_lease", "resolve_base"])
def test_worker_releases_the_ticket_as_infrastructure(tmp_path: Path, lease: bool) -> None:
    f, snaps = _factory(tmp_path)
    m = f.manager
    snaps.fail_resolve = True

    async def go() -> tuple[str, str]:
        tid = await m.submit(WorkOrder(**ORDER))
        slot = m.queue.tickets_for_task(tid)[0].slot
        worker = f.pools["alpha"].builder(slot)
        if not lease:
            worker.base_lease = None
        ticket = m.queue.claim_ticket("alpha", slot)
        assert ticket is not None
        return tid, await worker.process_ticket(ticket)

    tid, outcome = asyncio.run(go())
    assert outcome == "released"
    ticket = m.queue.tickets_for_task(tid)[0]
    assert ticket.status == "open" and ticket.attempt == 0
    assert f.claude.history == []
    reasons = [e["payload"]["reason"] for e in m.queue.events(task_id=tid, kind="ticket_released")]
    assert reasons and all(r.startswith("infrastructure: BaseUnavailable:") for r in reasons)
    f.close()


def test_other_base_errors_still_fall_back(tmp_path: Path) -> None:
    f, _ = _factory(tmp_path, snapshots=False)
    worker = f.pools["alpha"].builder(1)

    def broken(task_id: str) -> Path:
        raise ValueError("a bug, not an unavailable base")

    worker.base_for = broken
    assert asyncio.run(worker._resolve_base("F-001")) is None
    f.close()


def test_a_patch_is_not_checked_on_the_users_folder(tmp_path: Path) -> None:
    f, snaps = _factory(tmp_path)
    m = f.manager
    snaps.fail_resolve = True

    async def go() -> None:
        tid = m._prepare(WorkOrder(**ORDER))
        assert await m._write_patch(tid, "--- a/x\n+++ b/x\n", {"commit_message": "x"}) is False
        assert m.patch_paths == {} and m.rejected_patches == {}

    asyncio.run(go())
    f.close()


@pytest.mark.parametrize("template", [False, True], ids=["plain", "template"])
def test_pr_body_shows_the_base_above_the_footer(tmp_path: Path, template: bool) -> None:
    f, _ = _factory(tmp_path)
    m = f.manager
    m.pr_template = template
    tid = m._prepare(WorkOrder(**ORDER))
    m.pr_summaries[tid] = {"commit_message": "Add helper", "pr_body": "Adds it."}
    assert "Built on" not in m.pr_request(tid)["body"]
    m.task_bases[tid] = {"base": "main", "sha": SHA_A}
    body = m.pr_request(tid)["body"]
    assert body.endswith(f"Built on main @ {'a' * 12}\nOpened by Foundry (task {tid})\n")
    f.close()


def test_pr_body_without_text_still_shows_the_base(tmp_path: Path) -> None:
    f, _ = _factory(tmp_path)
    m = f.manager
    tid = m._prepare(WorkOrder(**ORDER))
    m.task_bases[tid] = {"base": "main", "sha": SHA_A}
    assert m.pr_request(tid)["body"] == f"Built on main @ {'a' * 12}\nOpened by Foundry (task {tid})\n"
    f.close()
