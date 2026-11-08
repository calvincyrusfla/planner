"""T-027 (alpha-1): stacked tickets in the queue and the combined patch.

Tickets of one task are built in order (tests/docs last); a ticket waits for
every earlier ticket unless it is ``parallel_ok``; every ticket may touch the
task's files (its own first); approved diffs are kept in the queue and the
combined patch folds the stacked diffs into one diff against the base.
"""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from foundry.floor_manager import diff_paths, stack_diffs
from foundry.orchestrator_queue import FoundryQueue, is_doc_file, is_test_file, plan_tickets
from foundry.schemas import SubtaskAllocation, TaskSizingDirective

HAS_GIT = shutil.which("git") is not None


def sub(i: int, files: list[str], title: str | None = None, criteria: list[str] | None = None) -> SubtaskAllocation:
    return SubtaskAllocation(subtask_id=f"T-1.{i}", title=title or f"part {i}",
                             acceptance_criteria=criteria or [f"c{i}"], allowed_files=files)


def directive(subs: list[SubtaskAllocation], n: int | None = None) -> TaskSizingDirective:
    n = n or len(subs)
    return TaskSizingDirective(task_id="T-1", team_id="alpha", complexity="M", allocated_builders=n,
                               rationale="x", subtasks=subs,
                               override_reason=None if n == 2 else "test")


@pytest.fixture()
def q(tmp_path: Path):  # type: ignore[no-untyped-def]
    queue = FoundryQueue(tmp_path / "q.db")
    yield queue
    queue.close()


# ---------------------------------------------------------------- planning


def test_test_and_doc_files_are_recognised() -> None:
    for p in ("tests/test_a.py", "foundry/tests/x.py", "pkg/test_x.py", "a_test.py", "conftest.py",
              "web/app.test.js", "web\\__tests__\\x.js"):
        assert is_test_file(p), p
    for p in ("foundry/config.py", "testing_tools.py", "contest.py"):
        assert not is_test_file(p), p
    assert is_doc_file("README.md") and is_doc_file("docs/guide.html") and not is_doc_file("a.py")


def test_plan_orders_tests_last_and_gives_every_ticket_the_task_files() -> None:
    subs = [sub(1, ["tests/test_a.py"]), sub(2, ["a.py"]), sub(3, ["README.md"]), sub(4, ["config.py", "a.py"])]
    plan = plan_tickets(subs)
    assert [p.order_index for p in plan] == [2, 0, 3, 1]
    assert plan[1].allowed_files == ["a.py", "config.py", "tests/test_a.py", "README.md"]
    assert plan[3].allowed_files == ["config.py", "a.py", "tests/test_a.py", "README.md"]
    assert plan[0].focus_files == ["tests/test_a.py"]
    assert plan[0].allowed_files[0] == "tests/test_a.py"
    assert not any(p.parallel_ok for p in plan)  # overlap / tests / docs after tests


def test_parallel_ok_only_when_disjoint_without_tests_or_dependencies() -> None:
    plan = plan_tickets([sub(1, ["a.py"]), sub(2, ["b.py"]), sub(3, ["a.py"]), sub(4, ["c.py"])])
    assert [p.parallel_ok for p in plan] == [False, True, False, True]
    # the Foreman marked a dependency on an earlier subtask
    plan = plan_tickets([sub(1, ["a.py"]), sub(2, ["b.py"], criteria=["uses the helper from T-1.1"])])
    assert plan[1].parallel_ok is False
    # a test file anywhere before (or in) the ticket keeps it sequential
    plan = plan_tickets([sub(1, ["a.py", "tests/test_a.py"]), sub(2, ["b.py"])])
    assert plan[1].parallel_ok is False
    # unknown files: sequential, and still unrestricted as before
    plan = plan_tickets([sub(1, ["a.py"]), sub(2, [])])
    assert plan[1].parallel_ok is False and plan[1].allowed_files == []
    # an explicit depends_on (when the directive carries one) is honoured
    class Dep:
        subtask_id, title, acceptance_criteria, allowed_files, depends_on = "T-1.2", "x", [], ["b.py"], ["T-1.1"]
    assert plan_tickets([sub(1, ["a.py"]), Dep()])[1].parallel_ok is False


# ---------------------------------------------------------------- claiming


def test_sequential_ticket_waits_until_every_earlier_ticket_is_done(q: FoundryQueue) -> None:
    q.apply_sizing(directive([sub(1, ["a.py"]), sub(2, ["a.py"]), sub(3, ["tests/test_a.py"])]))
    by_order = q.tickets_in_order("T-1")
    assert [t.subtask_id for t in by_order] == ["T-1.1", "T-1.2", "T-1.3"]
    assert [t.order_index for t in by_order] == [0, 1, 2]
    assert by_order[1].allowed_files == ["a.py", "tests/test_a.py"]
    assert by_order[2].focus_files == ["tests/test_a.py"]
    assert q.claim_ticket("alpha", 2) is None and q.claim_ticket("alpha", 3) is None
    first = q.claim_ticket("alpha", 1)
    assert first is not None and first.subtask_id == "T-1.1"
    assert q.claim_ticket("alpha", 2) is None  # T-1.1 claimed, not done
    q.set_in_review(first.ticket_id, True)
    assert q.claim_ticket("alpha", 2) is None  # in review is not approved
    q.complete_ticket(first.ticket_id, diff="--- a/a.py\n+++ b/a.py\n")
    second = q.claim_ticket("alpha", 2)
    assert second is not None and second.subtask_id == "T-1.2"
    assert q.claim_ticket("alpha", 3) is None
    q.complete_ticket(second.ticket_id)
    assert q.claim_ticket("alpha", 3).subtask_id == "T-1.3"  # type: ignore[union-attr]


def test_parallel_ok_tickets_run_alongside_earlier_ones(q: FoundryQueue) -> None:
    q.apply_sizing(directive([sub(1, ["a.py"]), sub(2, ["b.py"]), sub(3, ["a.py"])]))
    t1 = q.claim_ticket("alpha", 1)
    t2 = q.claim_ticket("alpha", 2)
    assert t1 is not None and t2 is not None and t2.parallel_ok
    assert q.claim_ticket("alpha", 3) is None
    q.complete_ticket(t1.ticket_id)
    assert q.claim_ticket("alpha", 3) is None  # T-1.2 is earlier too
    q.complete_ticket(t2.ticket_id)
    assert q.claim_ticket("alpha", 3) is not None


def test_one_slot_takes_tickets_in_build_order(q: FoundryQueue) -> None:
    q.apply_sizing(directive([sub(1, ["tests/test_a.py"]), sub(2, ["a.py"])]))
    q.resize("T-1", 1)  # both tickets now share slot 1
    t = q.claim_ticket("alpha", 1)
    assert t is not None and t.subtask_id == "T-1.2"  # the tests subtask moved last
    assert q.claim_ticket("alpha", 1) is None
    q.complete_ticket(t.ticket_id)
    assert q.claim_ticket("alpha", 1).subtask_id == "T-1.1"  # type: ignore[union-attr]


def test_old_database_tickets_keep_running_in_parallel(tmp_path: Path) -> None:
    db = tmp_path / "old.db"
    old = FoundryQueue(db)
    old.apply_sizing(directive([sub(1, ["a.py"]), sub(2, ["a.py"])]))
    old.close()
    c = sqlite3.connect(db)
    # what an older Foundry left behind: no ordering columns
    for col in ("order_index", "parallel_ok", "focus_files", "approved_diff"):
        c.execute(f"ALTER TABLE tickets DROP COLUMN {col}")
    c.commit()
    c.close()
    q = FoundryQueue(db)
    try:
        ts = q.tickets_in_order("T-1")
        assert all(t.parallel_ok and t.order_index == 0 for t in ts)
        assert q.claim_ticket("alpha", 1) is not None and q.claim_ticket("alpha", 2) is not None
    finally:
        q.close()


# ---------------------------------------------------------------- approved diffs


def test_approved_diffs_before_lists_earlier_done_diffs_in_order(q: FoundryQueue) -> None:
    q.apply_sizing(directive([sub(1, ["tests/test_a.py"]), sub(2, ["a.py"]), sub(3, ["b.py"])]))
    tid = {t.subtask_id: t.ticket_id for t in q.tickets_for_task("T-1")}
    assert q.approved_diffs_before(tid["T-1.1"]) == []
    q.record_approved_diff(tid["T-1.2"], "D2")  # reviewer approved; not done yet
    assert q.approved_diffs_before(tid["T-1.1"]) == []
    q.complete_ticket(tid["T-1.2"])
    q.complete_ticket(tid["T-1.3"], diff="D3")
    assert q.approved_diffs_before(tid["T-1.1"]) == [("T-1.2", "D2"), ("T-1.3", "D3")]
    assert q.approved_diffs_before(tid["T-1.3"]) == [("T-1.2", "D2")]
    assert q.approved_diff(tid["T-1.3"]) == "D3"
    assert q.approved_diffs("T-1") == [("T-1.2", "D2"), ("T-1.3", "D3")]
    with pytest.raises(KeyError):
        q.approved_diffs_before("T-1:nope")


def test_reopening_an_earlier_ticket_releases_later_claimed_ones(q: FoundryQueue) -> None:
    q.apply_sizing(directive([sub(1, ["a.py"]), sub(2, ["a.py"])]))
    t1 = q.claim_ticket("alpha", 1)
    assert t1 is not None
    q.complete_ticket(t1.ticket_id, diff="D1")
    t2 = q.claim_ticket("alpha", 2)
    assert t2 is not None
    # the earlier ticket goes back (e.g. a late REVISE): T-1.2 was built on stale code
    q.conn.execute("UPDATE tickets SET status='claimed' WHERE ticket_id=?", (t1.ticket_id,))
    assert q.reopen_ticket(t1.ticket_id, "fix it") is True
    later = q.get_ticket(t2.ticket_id)
    assert later is not None and later.status == "open" and later.attempt == 0 and later.claimed_by is None
    assert q.claim_ticket("alpha", 2) is None  # waits for T-1.1 again
    assert any(e["kind"] == "ticket_released" for e in q.events(task_id="T-1"))


# ---------------------------------------------------------------- combined patch

BASE = "def a():\n    return 1\n"


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "a.py").write_text(BASE, encoding="utf-8")
    (repo / "pkg" / "b.py").write_text("B = 1\n", encoding="utf-8")
    return repo


D1 = (  # adds helper() to a.py and a new config.py
    "--- a/pkg/a.py\n+++ b/pkg/a.py\n@@ -1,2 +1,5 @@\n def a():\n     return 1\n+\n+def helper():\n+    return 2\n"
    "--- /dev/null\n+++ b/pkg/config.py\n@@ -0,0 +1 @@\n+X = 1\n"
)
D2 = (  # relative to D1: changes helper() and config.py
    "--- a/pkg/a.py\n+++ b/pkg/a.py\n@@ -3,3 +3,3 @@\n \n def helper():\n-    return 2\n+    return 3\n"
    "--- a/pkg/config.py\n+++ b/pkg/config.py\n@@ -1 +1,2 @@\n X = 1\n+Y = 2\n"
)
D3 = "--- a/pkg/b.py\n+++ b/pkg/b.py\n@@ -1 +1,2 @@\n B = 1\n+C = 2\n"


def test_diff_paths() -> None:
    assert diff_paths(D1) == ["pkg/a.py", "pkg/config.py"]
    assert diff_paths("diff --git a/x.py b/y.py\nrename from x.py\nrename to y.py\n") == ["x.py", "y.py"]


@pytest.mark.skipif(not HAS_GIT, reason="git not installed")
def test_stacked_diffs_fold_into_one_patch_that_applies_on_the_base(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    combined = stack_diffs([D1, D2, D3], repo)
    assert combined is not None
    (tmp_path / "c.patch").write_text(combined, encoding="utf-8", newline="\n")
    subprocess.run(["git", "apply", str(tmp_path / "c.patch")], cwd=repo, check=True)
    assert (repo / "pkg" / "a.py").read_text(encoding="utf-8") == BASE + "\ndef helper():\n    return 3\n"
    assert (repo / "pkg" / "config.py").read_text(encoding="utf-8") == "X = 1\nY = 2\n"
    assert (repo / "pkg" / "b.py").read_text(encoding="utf-8") == "B = 1\nC = 2\n"


@pytest.mark.skipif(not HAS_GIT, reason="git not installed")
def test_disjoint_diffs_are_concatenated_unchanged_and_bad_stacks_give_none(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    assert stack_diffs([D1, D3], repo) == D1 + D3
    assert stack_diffs([D2], repo) is None  # D2 does not apply without D1
    assert stack_diffs([], repo) == ""


@pytest.mark.skipif(not HAS_GIT, reason="git not installed")
def test_manager_combined_patch_follows_ticket_order(tmp_path: Path) -> None:
    import asyncio

    from foundry.tests.test_orchestration import _factory
    from foundry.main import demo_claude

    repo = _repo(tmp_path)
    f = _factory(tmp_path, demo_claude(repo, fail_first=None), repo=repo, teams=["alpha"])
    try:
        m = f.manager
        f.queue.apply_sizing(directive([sub(1, ["pkg/a.py", "pkg/config.py"]), sub(2, ["pkg/a.py", "pkg/config.py"])]))
        m.diffs["T-1"] = {"T-1.2": D2, "T-1.1": D1}
        assert m._ordered_diffs("T-1") == [D1, D2]
        combined = asyncio.run(m._combined_diff("T-1"))
        (tmp_path / "c.patch").write_text(combined, encoding="utf-8", newline="\n")
        r = subprocess.run(["git", "apply", "--check", str(tmp_path / "c.patch")], cwd=repo, capture_output=True)
        assert r.returncode == 0, r.stderr
        # the manager's saved diffs are gone: the queue's stored approved diffs are used
        m.diffs.clear()
        for t in f.queue.tickets_in_order("T-1"):
            f.queue.complete_ticket(t.ticket_id, diff={"T-1.1": D1, "T-1.2": D2}[t.subtask_id])
        assert m._ordered_diffs("T-1") == [D1, D2]
    finally:
        f.close()
