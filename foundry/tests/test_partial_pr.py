"""A quarantined task's approved parts reach a draft PR; its normal PR still opens later."""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from foundry.floor_manager import FloorManager

TASK = "F-001"
REASON = "gates kept failing"
FIRST_DIFF = (
    "diff --git a/approved.py b/approved.py\nnew file mode 100644\n"
    "--- /dev/null\n+++ b/approved.py\n@@ -0,0 +1 @@\n+X = 1\n"
)
SECOND_DIFF = (
    "diff --git a/later.py b/later.py\nnew file mode 100644\n"
    "--- /dev/null\n+++ b/later.py\n@@ -0,0 +1 @@\n+Y = 2\n"
)


class FakeOrder:
    """The parts of a work order the manager reads here; every other field is None."""

    def __init__(self, title: str) -> None:
        self.title = title

    def __getattr__(self, name: str) -> None:
        return None

    def model_dump(self, mode: str = "python") -> dict[str, Any]:
        return {"title": self.title}


class FakePublisher:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.error = error

    async def publish(
        self,
        task_id: str,
        title: str,
        patch_path: Any,
        pr: Any = None,
        *,
        draft: bool | None = None,
        on_pushed: Any = None,
        base: str | None = None,
        base_sha: str | None = None,
    ) -> dict[str, Any]:
        if self.error is not None:
            raise self.error
        path = Path(patch_path)
        self.calls.append({
            "task_id": task_id, "title": title, "path": path, "patch": path.read_text(encoding="utf-8"),
            "pr": dict(pr or {}), "draft": draft, "base": base, "base_sha": base_sha,
        })
        n = len(self.calls)
        return {"ok": True, "branch": f"foundry/f-001-{n}", "pr_url": f"https://example.test/pr/{n}", "commit": "abc1234"}


def _manager(tmp_path: Path, publisher: FakePublisher) -> Any:
    queue = MagicMock()
    queue.kv_get.return_value = None
    queue.approved_diffs.return_value = []
    queue.get_task.return_value = {"task_id": TASK, "title": "Add thing", "repo_root": None}
    queue.tickets_in_order.return_value = [
        SimpleNamespace(subtask_id="F-001.1", title="First part"),
        SimpleNamespace(subtask_id="F-001.2", title="Second part"),
    ]
    foreman = MagicMock()
    foreman.draft_pr_summary.return_value = {"commit_message": "Add thing", "pr_body": "Body"}
    bridge = MagicMock()
    bridge.board_ids = {}
    bridge.board_teams = {}
    fm: Any = FloorManager(queue, foreman, {}, MagicMock(), bridge, patches_dir=tmp_path / "patches", publisher=publisher)
    # no sizing data, board progress or git checkout in these tests
    fm.record_outcome = MagicMock(return_value=None)
    fm.report_task_progress = MagicMock()
    fm._leased_root = AsyncMock(return_value=None)
    fm.orders[TASK] = FakeOrder("Add thing")
    fm.task_bases[TASK] = {"base": "main", "sha": "abc1234"}
    return fm


@pytest.fixture(autouse=True)
def _default_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FOUNDRY_PARTIAL_PR", raising=False)


def test_quarantine_with_an_approved_part_opens_a_draft_of_only_that_part(tmp_path: Path) -> None:
    publisher = FakePublisher()

    async def scenario() -> Any:
        fm = _manager(tmp_path, publisher)
        fm.diffs[TASK] = {"F-001.1": FIRST_DIFF}
        fm._report_quarantine(TASK, REASON)
        await fm._partial_tasks[TASK]
        return fm

    fm = asyncio.run(scenario())

    assert len(publisher.calls) == 1
    call = publisher.calls[0]
    assert call["title"] == "[partial] Add thing"
    assert call["pr"]["title"] == "[partial] Add thing"
    assert call["draft"] is True
    assert (call["base"], call["base_sha"]) == ("main", "abc1234")
    assert call["path"].name == f"{TASK}.partial.patch"
    assert "approved.py" in call["patch"]
    assert "later.py" not in call["patch"]
    assert not (tmp_path / "patches" / f"{TASK}.patch").exists()

    body = call["pr"]["body"]
    for text in ("F-001.1", "First part", "F-001.2", "Second part", REASON):
        assert text in body
    assert body.endswith(f"Opened by Foundry (task {TASK}, partial: the task was quarantined)")

    pr = fm.partial_prs[TASK]
    assert pr["state"] == "opened"
    assert pr["url"] == "https://example.test/pr/1"
    assert pr["partial"] is True
    assert TASK not in fm.pr_states
    notes = [str(c) for c in fm.bridge.progress.call_args_list]
    assert any("[Foundry] partial PR (draft): https://example.test/pr/1" in n for n in notes)


def test_quarantine_without_approved_parts_publishes_nothing(tmp_path: Path) -> None:
    publisher = FakePublisher()

    async def scenario() -> Any:
        fm = _manager(tmp_path, publisher)
        fm._report_quarantine(TASK, REASON)
        await asyncio.sleep(0)
        return fm

    fm = asyncio.run(scenario())

    assert publisher.calls == []
    assert fm.pr_state(TASK) is None
    assert TASK not in fm._partial_tasks


@pytest.mark.parametrize("value", ["off", "OFF", "0", "false", "no"])
def test_switched_off_publishes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("FOUNDRY_PARTIAL_PR", value)
    publisher = FakePublisher()

    async def scenario() -> Any:
        fm = _manager(tmp_path, publisher)
        fm.diffs[TASK] = {"F-001.1": FIRST_DIFF}
        fm._report_quarantine(TASK, REASON)
        await asyncio.sleep(0)
        return fm

    fm = asyncio.run(scenario())

    assert publisher.calls == []
    assert fm.pr_state(TASK) is None


def test_later_normal_completion_still_opens_the_normal_pr(tmp_path: Path) -> None:
    publisher = FakePublisher()

    async def scenario() -> Any:
        fm = _manager(tmp_path, publisher)
        fm.diffs[TASK] = {"F-001.1": FIRST_DIFF}
        fm._report_quarantine(TASK, REASON)
        await fm._partial_tasks[TASK]
        assert TASK not in fm.pr_states
        # retried: the failed part is approved too and the task completes
        fm.diffs[TASK]["F-001.2"] = SECOND_DIFF
        await fm._finish(TASK, None)
        await fm._pr_tasks[TASK]
        return fm

    fm = asyncio.run(scenario())

    assert len(publisher.calls) == 2
    normal = publisher.calls[1]
    assert normal["title"] == "Add thing"
    assert normal["draft"] is False
    assert normal["path"].name == f"{TASK}.patch"
    assert "approved.py" in normal["patch"] and "later.py" in normal["patch"]
    pr = fm.pr_state(TASK)
    assert pr["state"] == "opened"
    assert pr["url"] == "https://example.test/pr/2"
    assert not pr.get("partial")
    assert fm.partial_prs[TASK]["url"] == "https://example.test/pr/1"


def test_publisher_exception_is_recorded_and_never_raised(tmp_path: Path) -> None:
    publisher = FakePublisher(error=RuntimeError("boom"))

    async def scenario() -> Any:
        fm = _manager(tmp_path, publisher)
        fm.diffs[TASK] = {"F-001.1": FIRST_DIFF}
        fm._report_quarantine(TASK, REASON)
        await fm._partial_tasks[TASK]
        return fm

    fm = asyncio.run(scenario())

    pr = fm.partial_prs[TASK]
    assert pr["state"] == "failed"
    assert pr["partial"] is True
    assert "boom" in pr["error"]
    assert TASK not in fm.pr_states


def test_second_quarantine_does_not_publish_a_second_draft(tmp_path: Path) -> None:
    publisher = FakePublisher()

    async def scenario() -> None:
        fm = _manager(tmp_path, publisher)
        fm.diffs[TASK] = {"F-001.1": FIRST_DIFF}
        fm._report_quarantine(TASK, REASON)
        await fm._partial_tasks[TASK]
        fm._reported_quarantine.discard(TASK)  # retried, then quarantined again
        fm._report_quarantine(TASK, REASON)
        await asyncio.sleep(0)

    asyncio.run(scenario())

    assert len(publisher.calls) == 1


def test_partial_record_is_saved_and_loaded_with_its_flag(tmp_path: Path) -> None:
    publisher = FakePublisher()

    async def scenario() -> Any:
        fm = _manager(tmp_path, publisher)
        fm.diffs[TASK] = {"F-001.1": FIRST_DIFF}
        fm._report_quarantine(TASK, REASON)
        await fm._partial_tasks[TASK]
        return fm

    fm = asyncio.run(scenario())
    fm.save()
    saved = fm.queue.kv_set.call_args.args[1]
    assert saved["partial_prs"][TASK]["partial"] is True
    assert TASK not in saved["pr_states"]

    fm.partial_prs.clear()
    fm._kv_get = MagicMock(return_value={"partial_prs": saved["partial_prs"]})
    fm._load()

    assert fm.partial_prs == saved["partial_prs"]
