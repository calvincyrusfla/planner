"""T-012 follow-ups: completion work (PR draft + patch) is recorded only once the
patch is written, a missing/unwritten patch is retried on the next call, and
``drain()`` / ``Factory.running()`` wait (bounded) for in-flight completions.

Simulated Claude/Gemma and a dry board only.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from foundry.floor_manager import DEFAULT_FINISH_TIMEOUT_S, WorkOrder, _finish_timeout
from foundry.main import append_diff, demo_claude, write_demo_repo
from foundry.tests.test_orchestration import S_ORDER, _factory, _slow_draft_gemma


def _dones(f: Any) -> int:
    return sum(1 for op in f.bridge.ops if op["type"] == "updateTask" and op["args"]["patch"] == {"status": "done"})


def _seed_diff(m: Any, tid: str, repo: Path) -> None:
    m.diffs[tid] = {f"{tid}.1": append_diff("shop/mathx.py", (repo / "shop/mathx.py").read_text(), ["X = 1"])}


def test_unwritable_patch_is_not_recorded_and_the_next_call_writes_it_without_a_new_draft(tmp_path: Path) -> None:
    repo = write_demo_repo(tmp_path / "repo")
    f = _factory(tmp_path, demo_claude(repo, fail_first=None), gemma=_slow_draft_gemma(0.0), repo=repo, teams=["alpha"])
    m = f.manager
    good_dir = m.patches_dir
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("a file where the patches folder should be")

    async def go() -> None:
        tid = await m.submit(WorkOrder(**S_ORDER))
        _seed_diff(m, tid, repo)
        m.patches_dir = blocker  # mkdir fails: the patch cannot be written
        await m.on_task_complete(tid)
        assert tid not in m.pr_summaries  # nothing recorded without a patch
        assert _dones(f) == 0  # and the board was not told it is done
        calls = f.gemma.calls
        m.patches_dir = good_dir  # the disk is back
        summary = await m.on_task_complete(tid)
        assert m.pr_summaries[tid] == summary
        assert (good_dir / f"{tid}.patch").is_file() and (good_dir / f"{tid}.md").is_file()
        assert f.gemma.calls == calls  # the earlier draft was reused
        assert _dones(f) == 1
        assert await m.on_task_complete(tid) == summary and _dones(f) == 1  # idempotent afterwards

    asyncio.run(go())
    f.close()


def test_rewritten_patch_after_restart_uses_the_stored_summary(tmp_path: Path) -> None:
    repo = write_demo_repo(tmp_path / "repo")
    f = _factory(tmp_path, demo_claude(repo, fail_first=None), gemma=_slow_draft_gemma(0.0), repo=repo, teams=["alpha"])

    async def first() -> str:
        tid = await f.manager.submit(WorkOrder(**S_ORDER))
        _seed_diff(f.manager, tid, repo)
        await f.manager.on_task_complete(tid)
        return tid

    tid = asyncio.run(first())
    summary = f.manager.pr_summaries[tid]
    f.close()
    (tmp_path / "patches" / f"{tid}.patch").unlink()

    g = _factory(tmp_path, demo_claude(repo, fail_first=None), gemma=_slow_draft_gemma(0.0), repo=repo, teams=["alpha"])
    assert g.manager.pr_summaries[tid] == summary  # loaded from the saved state
    assert asyncio.run(g.manager.on_task_complete(tid)) == summary
    assert (tmp_path / "patches" / f"{tid}.patch").is_file()
    assert g.gemma.calls == 0 and _dones(g) == 0  # no new draft, no repeated board ops
    g.close()


def test_drain_is_bounded_and_never_cancels(tmp_path: Path) -> None:
    repo = write_demo_repo(tmp_path / "repo")
    f = _factory(tmp_path, demo_claude(repo, fail_first=None), gemma=_slow_draft_gemma(0.4), repo=repo, teams=["alpha"])
    m = f.manager

    async def go() -> None:
        tid = await m.submit(WorkOrder(**S_ORDER))
        _seed_diff(m, tid, repo)
        m._spawn(m.on_task_complete(tid))  # an in-flight completion, like the pool callback
        await asyncio.sleep(0)
        assert await m.drain(0.05) is False  # timed out ...
        assert tid not in m.pr_summaries
        assert await m.drain(5.0) is True  # ... but the work was not cancelled
        assert (tmp_path / "patches" / f"{tid}.patch").is_file() and tid in m.pr_summaries
        assert await m.drain(0) is True  # nothing left in flight

    asyncio.run(go())
    f.close()


def test_running_waits_for_completion_before_pools_stop(tmp_path: Path) -> None:
    repo = write_demo_repo(tmp_path / "repo")
    f = _factory(tmp_path, demo_claude(repo, fail_first=None), gemma=_slow_draft_gemma(0.3), repo=repo, teams=["alpha"])
    m = f.manager
    order: list[str] = []
    for pool in f.pools.values():
        stop = pool.stop

        async def tracked_stop(*a: Any, _stop: Any = stop, **k: Any) -> None:
            order.append("stop:" + ("written" if m.pr_summaries else "pending"))
            await _stop(*a, **k)

        pool.stop = tracked_stop  # type: ignore[method-assign]

    async def go() -> str:
        tid = await m.submit(WorkOrder(**S_ORDER))
        _seed_diff(m, tid, repo)
        async with f.running(finish_timeout=5.0):
            m._spawn(m.on_task_complete(tid))
        return tid

    tid = asyncio.run(go())
    assert order == ["stop:written"]  # the patch was written before any pool stopped
    assert (tmp_path / "patches" / f"{tid}.patch").is_file()
    f.close()


def test_finish_timeout_comes_from_the_argument_then_the_config() -> None:
    assert _finish_timeout(SimpleNamespace(), None) == DEFAULT_FINISH_TIMEOUT_S
    assert _finish_timeout(SimpleNamespace(finish_timeout_s=12), None) == 12.0
    assert _finish_timeout(SimpleNamespace(finish_timeout_s=12), 3) == 3.0
    assert _finish_timeout(SimpleNamespace(finish_timeout_s="bad"), None) == DEFAULT_FINISH_TIMEOUT_S
    assert _finish_timeout(SimpleNamespace(finish_timeout_s=-1), None) == DEFAULT_FINISH_TIMEOUT_S
