"""Stacked PRs: a piece whose parent's PR is still open is built on that PR's branch."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from foundry import board_dispatch
from foundry.board_dispatch import BatchWatcher
from foundry.stacking import (
    PR_DONE, PR_OPEN, PR_UNKNOWN, github_origin, pr_info, pr_phase, stack_note, stack_record,
)
from foundry.tests.test_board_dispatch import SLUG, FakeBoard, FakePublisher, _pump, _service, _watcher

PR1 = "https://github.com/me/shop/pull/1"
PR2 = "https://github.com/me/shop/pull/2"
SHA = "a" * 40


def _open(branch: str = "foundry/parent") -> dict[str, Any]:
    return {"state": "OPEN", "headRefName": branch, "headRefOid": SHA, "baseRefName": "main", "mergedAt": None}


MERGED = {"state": "MERGED", "headRefName": "foundry/parent", "headRefOid": SHA, "baseRefName": "main",
          "mergedAt": "2026-11-10T00:00:00Z"}
CLOSED = {"state": "CLOSED", "headRefName": "foundry/parent", "headRefOid": SHA, "baseRefName": "main",
          "mergedAt": None}


class FakeGh:
    """A gh runner answering ``pr view`` from ``prs``; a url it does not know fails."""

    def __init__(self, prs: dict[str, dict[str, Any]] | None = None) -> None:
        self.prs = prs or {}
        self.calls: list[list[str]] = []

    async def __call__(self, argv: list[str], **_: Any) -> tuple[int | None, str, str]:
        self.calls.append(list(argv))
        pr = self.prs.get(argv[3])
        return (0, json.dumps(pr), "") if pr is not None else (1, "", "no such PR")

    async def lookup(self, url: str) -> dict[str, Any] | None:
        return await pr_info(url, gh="gh", runner=self)


def _child_batch(board: FakeBoard, deps: list[str] | None = None, **kw: Any) -> dict[str, Any]:
    return board.add(subtasks=[
        {"key": "1", "title": "Add clamp helper", "description": "clamp", "files": ["shop/mathx.py"]},
        {"key": "2", "title": "Use clamp", "description": "use it", "files": ["shop/mathx.py"],
         "dependsOn": ["1"] if deps is None else deps},
    ], **kw)


def _complete(board: FakeBoard, bid: str, *keys: str) -> bool:
    results = board.get(bid)["results"]
    return all((results.get(k) or {}).get("state") == "complete" for k in keys)


# --------------------------------------------------------------------------- pr_info and friends


def test_pr_info_runs_gh_with_the_fields_and_parses_them() -> None:
    gh = FakeGh({PR1: _open()})
    assert asyncio.run(pr_info(PR1, gh="gh", runner=gh)) == _open()
    assert gh.calls == [["gh", "pr", "view", PR1, "--json", "state,headRefName,headRefOid,baseRefName,mergedAt"]]


@pytest.mark.parametrize("answer", [(1, "", "boom"), (None, "", "timed out"), (0, "not json", ""), (0, "[1]", "")])
def test_pr_info_is_none_on_a_failed_answer(answer: tuple[int | None, str, str]) -> None:
    async def runner(argv: list[str], **_: Any) -> tuple[int | None, str, str]:
        return answer

    assert asyncio.run(pr_info(PR1, gh="gh", runner=runner)) is None


def test_pr_info_is_none_when_gh_cannot_start() -> None:
    async def runner(argv: list[str], **_: Any) -> tuple[int | None, str, str]:
        raise OSError("no such file")

    assert asyncio.run(pr_info(PR1, gh="gh", runner=runner)) is None


def test_pr_info_is_none_without_gh(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("foundry.stacking.shutil.which", lambda _: None)
    assert asyncio.run(pr_info(PR1, runner=FakeGh({PR1: _open()}))) is None


def test_pr_info_never_passes_an_option_like_url() -> None:
    gh = FakeGh()
    assert asyncio.run(pr_info("--web", gh="gh", runner=gh)) is None
    assert gh.calls == []


@pytest.mark.parametrize("info, phase", [
    (_open(), PR_OPEN), (MERGED, PR_DONE), (CLOSED, PR_DONE), (None, PR_UNKNOWN), ({}, PR_UNKNOWN),
    ({"state": "DRAFTY", "headRefName": "x"}, PR_UNKNOWN), (_open("--bad"), PR_UNKNOWN), ({"state": "OPEN"}, PR_UNKNOWN),
])
def test_pr_phase(info: dict[str, Any] | None, phase: str) -> None:
    assert pr_phase(info) == phase


@pytest.mark.parametrize("answer, hosted", [
    ((0, "git@github.com:me/shop.git\n", ""), True),
    ((0, "https://github.com/me/shop\n", ""), True),
    ((0, "/tmp/origin.git\n", ""), False),
    ((2, "", "error: No such remote 'origin'"), False),
    ((None, "", "timed out"), None),
])
def test_github_origin(answer: tuple[int | None, str, str], hosted: bool | None) -> None:
    async def runner(argv: list[str], **_: Any) -> tuple[int | None, str, str]:
        assert argv == ["git", "-C", "/repo", "remote", "get-url", "origin"]
        return answer

    assert asyncio.run(github_origin("/repo", git="git", runner=runner)) is hosted


def test_stack_record_is_the_shared_shape() -> None:
    assert stack_record(task_id="F-2", batch="S-1", key="2", parent_url=PR1, parent_branch="foundry/p",
                        parent_head=SHA, base=None) == {
        "task_id": "F-2", "batch": "S-1", "key": "2", "parent_url": PR1, "parent_branch": "foundry/p",
        "parent_head": SHA, "base": None, "state": "stacked"}


# --------------------------------------------------------------------------- through the watcher


def test_one_open_parent_stacks_the_piece(tmp_path: Path) -> None:
    board = FakeBoard()
    b = _child_batch(board, base="develop")
    pub = FakePublisher()
    gh = FakeGh({PR1: _open()})

    async def go() -> None:
        svc = _service(tmp_path, publisher=pub)
        await svc.start()
        try:
            w = _watcher(svc, board, stack_lookup=gh.lookup)
            await _pump(w, lambda: board.get(b["id"])["status"] == "done")
            tasks = w._record(b["id"])["tasks"]
            parent, child = tasks["1"], tasks["2"]
            orders = svc.factory.manager.orders
            assert orders[parent].pr_base == "develop"
            assert orders[child].pr_base == "foundry/parent"
            assert stack_note(PR1, "foundry/parent") in orders[child].description
            assert orders[child].description.index("use it") < orders[child].description.index("stacked on")
            assert stack_note(PR1, "foundry/parent") == (
                f"This piece is stacked on {PR1} (branch foundry/parent): that PR's changes are already in "
                "your base. Build on them; do not repeat them.")
            assert "stacked on" not in orders[parent].description
            assert pub.bases == {parent: "develop", child: "foundry/parent"}
            assert svc.factory.queue.kv_items("stack:") == {f"stack:{child}": {
                "task_id": child, "batch": b["id"], "key": "2", "parent_url": PR1, "parent_branch": "foundry/parent",
                "parent_head": SHA, "base": "develop", "state": "stacked"}}
        finally:
            await svc.stop()

    asyncio.run(go())


@pytest.mark.parametrize("answer", [MERGED, CLOSED])
def test_a_finished_parent_builds_on_the_batch_base(tmp_path: Path, answer: dict[str, Any]) -> None:
    board = FakeBoard()
    b = _child_batch(board, base="develop")
    pub = FakePublisher()
    gh = FakeGh({PR1: answer})

    async def go() -> None:
        svc = _service(tmp_path, publisher=pub)
        await svc.start()
        try:
            w = _watcher(svc, board, stack_lookup=gh.lookup)
            await _pump(w, lambda: board.get(b["id"])["status"] == "done")
            child = w._record(b["id"])["tasks"]["2"]
            assert svc.factory.manager.orders[child].pr_base == "develop"
            assert "stacked on" not in svc.factory.manager.orders[child].description
            assert svc.factory.queue.kv_items("stack:") == {}
        finally:
            await svc.stop()

    asyncio.run(go())


def test_two_open_parents_wait_until_one_is_merged(tmp_path: Path) -> None:
    board = FakeBoard()
    b = board.add(subtasks=[
        {"key": "1", "title": "Add clamp helper", "description": "clamp", "files": ["shop/mathx.py"]},
        {"key": "2", "title": "Add lerp helper", "description": "lerp", "files": ["shop/mathx.py"]},
        {"key": "3", "title": "Use both", "description": "use", "files": ["shop/mathx.py"], "dependsOn": ["1", "2"]},
    ])
    gh = FakeGh({PR1: _open("foundry/one"), PR2: _open("foundry/two")})

    async def go() -> None:
        svc = _service(tmp_path, publisher=FakePublisher())
        await svc.start()
        try:
            w = _watcher(svc, board, stack_lookup=gh.lookup)
            await _pump(w, lambda: _complete(board, b["id"], "1", "2"))
            for _ in range(3):
                await w.poll_once()
            assert set(w._record(b["id"])["tasks"]) == {"1", "2"}
            assert board.get(b["id"])["results"]["3"]["state"] == "queued"
            assert svc.factory.queue.kv_items("stack:") == {}

            gh.prs[PR1] = MERGED
            await _pump(w, lambda: "3" in w._record(b["id"])["tasks"])
            child = w._record(b["id"])["tasks"]["3"]
            assert svc.factory.manager.orders[child].pr_base == "foundry/two"
            assert svc.factory.queue.kv_items("stack:")[f"stack:{child}"]["parent_url"] == PR2
        finally:
            await svc.stop()

    asyncio.run(go())


def test_a_failing_lookup_waits(tmp_path: Path) -> None:
    board = FakeBoard()
    b = _child_batch(board)
    gh = FakeGh()  # knows no PR: every lookup fails

    async def go() -> None:
        svc = _service(tmp_path, publisher=FakePublisher())
        await svc.start()
        try:
            w = _watcher(svc, board, stack_lookup=gh.lookup)
            await _pump(w, lambda: _complete(board, b["id"], "1"))
            for _ in range(3):
                await w.poll_once()
            assert set(w._record(b["id"])["tasks"]) == {"1"}
            assert board.get(b["id"])["results"]["2"]["state"] == "queued"

            gh.prs[PR1] = _open()  # the lookup recovers: the piece goes on
            await _pump(w, lambda: "2" in w._record(b["id"])["tasks"])
            child = w._record(b["id"])["tasks"]["2"]
            assert svc.factory.manager.orders[child].pr_base == "foundry/parent"
        finally:
            await svc.stop()

    asyncio.run(go())


def test_pieces_without_depends_on_are_unchanged(tmp_path: Path) -> None:
    board = FakeBoard()
    b = board.add(base="develop")
    pub = FakePublisher()
    gh = FakeGh()

    async def go() -> None:
        svc = _service(tmp_path, publisher=pub)
        await svc.start()
        try:
            w = _watcher(svc, board, stack_lookup=gh.lookup)
            await _pump(w, lambda: board.get(b["id"])["status"] == "done")
            assert set(pub.bases.values()) == {"develop"}
            assert svc.factory.queue.kv_items("stack:") == {}
        finally:
            await svc.stop()

    asyncio.run(go())
    assert gh.calls == []


def test_a_repo_gh_cannot_ask_about_never_runs_gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A fake PR url and no hosted origin (the demo repo): builds as before, whatever gh is installed."""
    asked: list[str] = []

    async def spy(url: str, **_: Any) -> dict[str, Any] | None:
        asked.append(url)
        return None

    monkeypatch.setattr(board_dispatch, "pr_info", spy)
    board = FakeBoard()
    b = _child_batch(board)
    pub = FakePublisher()

    async def go() -> None:
        svc = _service(tmp_path, publisher=pub)
        await svc.start()
        try:
            w = _watcher(svc, board)
            await _pump(w, lambda: board.get(b["id"])["status"] == "done")
            child = w._record(b["id"])["tasks"]["2"]
            assert svc.factory.manager.orders[child].pr_base is None
            assert svc.factory.queue.kv_items("stack:") == {}
        finally:
            await svc.stop()

    asyncio.run(go())
    assert asked == []


# --------------------------------------------------------------------------- the decision on its own


def _bare_watcher(tmp_path: Path, *, auto_pr: bool = True, **kw: Any) -> BatchWatcher:
    manager = SimpleNamespace(auto_pr=auto_pr, publisher=object() if auto_pr else None)
    service = SimpleNamespace(running=True, factory=SimpleNamespace(manager=manager, queue=SimpleNamespace()),
                              repo=tmp_path)
    return BatchWatcher(service, board=FakeBoard(), repo_slug=SLUG, host="pc", interval=1.0, enabled=True, **kw)


def _done(url: str | None, state: str = "opened") -> dict[str, Any]:
    return {"state": "complete", "pr": {"state": state, "url": url}}


def test_a_parent_pr_still_being_opened_waits(tmp_path: Path) -> None:
    gh = FakeGh()
    w = _bare_watcher(tmp_path, stack_lookup=gh.lookup)
    results = {"1": _done(None, "pending")}
    assert asyncio.run(w._stack_parent("S-1", "2", ["1"], results, {})) == (False, None)
    assert gh.calls == []


def test_a_parent_without_a_pr_url_builds_on_the_base(tmp_path: Path) -> None:
    gh = FakeGh()
    w = _bare_watcher(tmp_path, stack_lookup=gh.lookup)
    assert asyncio.run(w._stack_parent("S-1", "2", ["1"], {"1": _done(None, "failed")}, {})) == (True, None)
    assert gh.calls == []


def test_a_hosted_origin_asks_gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def hosted(repo: Any) -> bool | None:
        return True

    async def answer(url: str) -> dict[str, Any] | None:
        return _open()

    monkeypatch.setattr(board_dispatch, "github_origin", hosted)
    monkeypatch.setattr(board_dispatch, "pr_info", answer)
    w = _bare_watcher(tmp_path)
    assert asyncio.run(w._stack_parent("S-1", "2", ["1"], {"1": _done(PR1)}, {})) == (
        True, {"url": PR1, "branch": "foundry/parent", "head": SHA})


def test_an_origin_git_could_not_read_waits_and_is_asked_again(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    answers: list[bool | None] = [None, False]

    async def origin(repo: Any) -> bool | None:
        return answers.pop(0)

    monkeypatch.setattr(board_dispatch, "github_origin", origin)
    w = _bare_watcher(tmp_path)
    results = {"1": _done(PR1)}
    assert asyncio.run(w._stack_parent("S-1", "2", ["1"], results, {})) == (False, None)
    assert asyncio.run(w._stack_parent("S-1", "2", ["1"], results, {})) == (True, None)
    assert answers == []


def test_a_round_looks_each_pr_up_once(tmp_path: Path) -> None:
    gh = FakeGh({PR1: _open()})
    w = _bare_watcher(tmp_path, stack_lookup=gh.lookup)
    infos: dict[str, dict[str, Any] | None] = {}
    results = {"1": _done(PR1)}

    async def go() -> None:
        await w._stack_parent("S-1", "2", ["1"], results, infos)
        await w._stack_parent("S-1", "3", ["1"], results, infos)

    asyncio.run(go())
    assert len(gh.calls) == 1


def test_each_decision_is_logged_at_info_once(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    gh = FakeGh({PR1: _open()})
    w = _bare_watcher(tmp_path, stack_lookup=gh.lookup)
    results = {"1": _done(PR1)}

    async def go() -> None:
        await w._stack_parent("S-1", "2", ["1"], results, {})
        await w._stack_parent("S-1", "2", ["1"], results, {})

    with caplog.at_level("INFO", logger="foundry.board_dispatch"):
        asyncio.run(go())
    assert [r.getMessage() for r in caplog.records] == [
        f"board dispatch: S-1 subtask 2 stacks on {PR1} (branch foundry/parent)"]
