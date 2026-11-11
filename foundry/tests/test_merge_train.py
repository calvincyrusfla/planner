"""The merge train: rebase open Foundry PRs that fell behind, then run the combined check."""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from foundry import combined_check, merge_train
from foundry.combined_check import CombinedChecker
from foundry.git_publisher import default_runner
from foundry.merge_train import rebase_behind

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

_ID = ("-c", "user.name=T", "-c", "user.email=t@localhost")


def g(cwd: Path, *args: str) -> str:
    done = subprocess.run(["git", *_ID, *args], cwd=cwd, capture_output=True, text=True, check=True)
    return done.stdout.strip()


def commit(cwd: Path, name: str, text: str, message: str) -> str:
    (cwd / name).write_text(text, encoding="utf-8")
    g(cwd, "add", name)
    g(cwd, "commit", "-q", "-m", message)
    return g(cwd, "rev-parse", "HEAD")


class World:
    """origin (bare) plus a clone with five PR branches around a moved ``main``."""

    def __init__(self, tmp: Path) -> None:
        self.origin = tmp / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.origin)], check=True)
        self.clone = tmp / "clone"
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.clone)], check=True, capture_output=True)
        g(self.clone, "checkout", "-q", "-B", "main")
        commit(self.clone, "a.txt", "one\n", "a")
        commit(self.clone, "b.txt", "one\n", "b")
        g(self.clone, "push", "-q", "origin", "main")
        self.heads: dict[str, str] = {}
        self.branch("foundry/clean", "new.txt", "x\n")
        self.branch("foundry/conflict", "a.txt", "mine\n")
        self.branch("foundry/stack", "stack.txt", "s\n", start="foundry/clean")
        # the squash-merged commit that moves the base
        g(self.clone, "checkout", "-q", "main")
        self.moved = commit(self.clone, "a.txt", "theirs\n", "squash merged")
        g(self.clone, "push", "-q", "origin", "main")
        self.branch("foundry/fresh", "fresh.txt", "f\n")
        g(self.clone, "checkout", "-q", "main")

    def branch(self, name: str, file: str, text: str, start: str = "main") -> None:
        g(self.clone, "checkout", "-q", "-B", name, start)
        self.heads[name] = commit(self.clone, file, text, name)
        g(self.clone, "push", "-q", "origin", f"{name}:{name}")

    def prs(self) -> list[dict[str, Any]]:
        rows = [("foundry/clean", 1, "main"), ("foundry/conflict", 2, "main"), ("foundry/fresh", 3, "main"),
                ("foundry/stack", 4, "foundry/clean")]
        return [{"number": n, "title": b, "headRefName": b, "headRefOid": self.heads[b], "baseRefName": base}
                for b, n, base in rows]

    def remote_head(self, branch: str) -> str:
        return g(self.origin, "rev-parse", f"refs/heads/{branch}")


def _runner(calls: list[list[str]]) -> Any:
    async def run(argv: Any, **kw: Any) -> Any:
        calls.append(list(argv))
        return await default_runner(argv, **kw)

    return run


@needs_git
def test_rebases_only_the_prs_that_fell_behind(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world = World(tmp_path)
    calls: list[list[str]] = []
    made: list[Path] = []
    real_mkdtemp = merge_train.tempfile.mkdtemp

    def spy(*a: Any, **k: Any) -> str:
        path = real_mkdtemp(*a, **k)
        made.append(Path(path))
        return path

    monkeypatch.setattr(merge_train.tempfile, "mkdtemp", spy)
    service = SimpleNamespace(repo=world.clone)

    result = asyncio.run(rebase_behind(service, "main", world.prs(), runner=_runner(calls)))

    assert result["rebased"] == [1]
    assert list(result["conflicts"]) == [2]
    assert "a.txt" in result["conflicts"][2]
    assert sorted(result["skipped"]) == [3, 4]
    new_head = world.remote_head("foundry/clean")
    assert new_head != world.heads["foundry/clean"]
    assert g(world.origin, "merge-base", "--is-ancestor", world.moved, new_head) == ""
    assert world.remote_head("foundry/conflict") == world.heads["foundry/conflict"]
    assert world.remote_head("foundry/stack") == world.heads["foundry/stack"]
    pushes = [c for c in calls if "push" in c]
    assert len(pushes) == 1
    assert f"--force-with-lease=foundry/clean:{world.heads['foundry/clean']}" in pushes[0]
    assert made and all(not p.exists() for p in made)
    assert "wt" not in g(world.clone, "worktree", "list")


@needs_git
def test_a_moved_remote_branch_is_not_overwritten(tmp_path: Path) -> None:
    world = World(tmp_path)
    prs = world.prs()
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(world.origin), str(other)], check=True, capture_output=True)
    g(other, "checkout", "-q", "foundry/clean")
    pushed = commit(other, "late.txt", "late\n", "late")
    g(other, "push", "-q", "origin", "foundry/clean")
    prs[0]["headRefOid"] = world.heads["foundry/clean"]  # stale: someone pushed since

    result = asyncio.run(rebase_behind(SimpleNamespace(repo=world.clone), "main", prs))

    assert result["rebased"] == []  # the lease pinned the old head and refused the push
    assert world.remote_head("foundry/clean") == pushed


# ------------------------------------------------------------------ the checker's loop


def _checker(tmp_path: Path, shas: list[str], prs: list[dict[str, Any]]) -> tuple[CombinedChecker, list[Any]]:
    log: list[Any] = []
    events: list[Any] = []

    async def runner(argv: Any, **kw: Any) -> tuple[int, str, str]:
        if list(argv)[:3] == ["gh", "pr", "list"]:
            return 0, json.dumps(prs), ""
        if "ls-remote" in argv:
            return 0, f"{shas[0]}\trefs/heads/main\n", ""
        raise AssertionError(argv)

    queue = SimpleNamespace(record_event=lambda kind, task, payload: events.append((kind, payload)),
                            kv_get=lambda key: None, kv_set=lambda key, value: None)
    service = SimpleNamespace(running=True, repo=tmp_path, factory=SimpleNamespace(queue=queue))
    checker = CombinedChecker(service, interval_s=600, runner=runner, gh="gh", git="git")

    async def fake_rebase(svc: Any, base: str, group: list[dict[str, Any]], *, runner: Any = None) -> dict[str, Any]:
        log.append(("rebase", base))
        return {"rebased": [group[0]["number"]], "conflicts": {}, "skipped": []}

    async def fake_round() -> None:
        log.append("round")

    checker.run_once = fake_round  # type: ignore[method-assign]
    checker._rebase = fake_rebase  # type: ignore[attr-defined]
    log.append(events)
    return checker, log


def _pr(n: int) -> dict[str, Any]:
    return {"number": n, "title": "t", "headRefName": f"foundry/F-{n}", "headRefOid": f"{n:040d}",
            "baseRefName": "main"}


def test_train_runs_when_the_base_sha_changes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FOUNDRY_MERGE_TRAIN", raising=False)
    shas = ["a" * 40]
    checker, log = _checker(tmp_path, shas, [_pr(1), _pr(2)])
    events = log.pop()

    async def fake_rebase(svc: Any, base: str, group: list[dict[str, Any]], *, runner: Any = None) -> dict[str, Any]:
        log.append(("rebase", base))
        return {"rebased": [1], "conflicts": {2: ["a.txt"]}, "skipped": []}

    monkeypatch.setattr(combined_check, "rebase_behind", fake_rebase)

    assert asyncio.run(checker.train_once()) is False  # first poll: baseline
    assert asyncio.run(checker.train_once()) is False  # nothing moved
    assert log == []
    shas[0] = "b" * 40
    assert asyncio.run(checker.train_once()) is True
    assert log == [("rebase", "main"), "round"]
    assert events == [("merge_train", {"base": "main", "old_sha": "a" * 40, "new_sha": "b" * 40,
                                       "rebased": [1], "conflicts": {"2": ["a.txt"]}})]
    assert asyncio.run(checker.train_once()) is False


def test_train_runs_when_the_open_prs_change(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FOUNDRY_MERGE_TRAIN", raising=False)
    prs = [_pr(1)]
    checker, log = _checker(tmp_path, ["a" * 40], prs)
    log.pop()

    async def fake_rebase(svc: Any, base: str, group: list[dict[str, Any]], *, runner: Any = None) -> dict[str, Any]:
        log.append(("rebase", base))
        return {"rebased": [], "conflicts": {}, "skipped": [1]}

    monkeypatch.setattr(combined_check, "rebase_behind", fake_rebase)
    asyncio.run(checker.train_once())
    prs.append(_pr(2))

    assert asyncio.run(checker.train_once()) is True
    assert log == [("rebase", "main"), "round"]


@pytest.mark.parametrize("value", ["off", "0", "false", "no"])
def test_train_off_does_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("FOUNDRY_MERGE_TRAIN", value)
    shas = ["a" * 40]
    checker, log = _checker(tmp_path, shas, [_pr(1)])
    log.pop()

    async def boom(*a: Any, **k: Any) -> dict[str, Any]:
        raise AssertionError("the train must not run")

    monkeypatch.setattr(combined_check, "rebase_behind", boom)
    asyncio.run(checker.train_once())
    shas[0] = "c" * 40

    assert asyncio.run(checker.train_once()) is False
    assert log == []


def test_poll_interval_has_a_floor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOUNDRY_TRAIN_POLL_S", "2")
    assert _checker(tmp_path, ["a" * 40], [])[0].train_poll_s == 15.0
    monkeypatch.delenv("FOUNDRY_TRAIN_POLL_S")
    assert _checker(tmp_path, ["a" * 40], [])[0].train_poll_s == 60.0


def test_status_prints_the_last_events(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from foundry.orchestrator_queue import FoundryQueue

    db = tmp_path / "q.db"
    with FoundryQueue(db) as queue:
        queue.record_event("merge_train", None, {"base": "main", "old_sha": "a" * 40, "new_sha": "b" * 40,
                                                 "rebased": [1], "conflicts": {}})

    assert merge_train.main(["status", "--db", str(db)]) == 0
    assert "main aaaaaaaa->bbbbbbbb rebased=[1]" in capsys.readouterr().out
