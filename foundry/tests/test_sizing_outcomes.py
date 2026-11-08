"""T-018 sizing outcomes: the queue table, actual_complexity / sizing_accuracy and
the floor manager recording an outcome on completion / quarantine.

Simulated Claude/Gemma and a dry board only.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from foundry.floor_manager import WorkOrder
from foundry.main import demo_claude, write_demo_repo
from foundry.orchestrator_queue import FoundryQueue, normalize_repo_root
from foundry.schemas import TaskSizingDirective
from foundry.schemas import BUILDERS_FOR
from foundry.sizing_outcomes import actual_complexity, effort_score, same_size, size_delta, sizing_accuracy
from foundry.telemetry import Telemetry
from foundry.tests.test_orchestration import S_ORDER, _alert, _factory, _submit_and_run


class Clock:
    def __init__(self) -> None:
        self.t = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.t


def _directive(task_id: str, n: int = 2, complexity: str = "M") -> TaskSizingDirective:
    return TaskSizingDirective.model_validate({
        "task_id": task_id,
        "team_id": "alpha",
        "complexity": complexity,
        "allocated_builders": n,
        "rationale": "test",
        "subtasks": [
            {"subtask_id": f"{task_id}.{i}", "title": f"part {i}", "acceptance_criteria": ["works"],
             "allowed_files": [f"src/f{i}.py"]}
            for i in range(1, n + 1)
        ],
    })


# ------------------------------------------------------------------ pure helpers


def _clean(size: str) -> dict[str, Any]:
    n = BUILDERS_FOR[size]  # one subtask per builder, 1 build + 1 review each, no retries
    return {"predicted": size, "subtasks": n, "builders": n, "attempts": n, "claude_calls": 2 * n}


def test_a_clean_run_of_each_size_scores_that_size() -> None:
    for size in ("S", "M", "L", "XL"):
        assert same_size(actual_complexity(_clean(size)), size), size
    assert actual_complexity(_clean("XL")) == "L"  # L and XL cannot be told apart by effort...
    assert sizing_accuracy([_clean(s) for s in ("S", "M", "L", "XL")])["exact"] == 4  # ...so both are exact


def test_actual_complexity_thresholds() -> None:
    assert actual_complexity({"claude_calls": 6, "attempts": 3, "subtasks": 3}) == "M"
    assert actual_complexity({"claude_calls": 9, "attempts": 3, "subtasks": 2}) == "L"  # 9 + 1 retry
    assert actual_complexity({"claude_calls": 20, "attempts": 8, "subtasks": 5}) == "XL"
    # no telemetry: two calls per attempt are assumed
    assert effort_score({"attempts": 2, "subtasks": 2}) == 4
    # wall time is recorded but not effort: a clean one-subtask task that took 20 minutes is S
    assert actual_complexity({**_clean("S"), "wall_time_s": 1200}) == "S"
    # only exhausted retries floor a quarantined task at L
    q = {**_clean("S"), "final_state": "quarantined"}
    assert actual_complexity({**q, "quarantine_kind": "retries_exhausted"}) == "L"
    assert actual_complexity({**q, "quarantine_kind": "other"}) == "S"
    assert actual_complexity({}) == "S"


def test_accuracy_uses_raw_predicted_and_skips_excluded_rows() -> None:
    rows = [
        {"predicted": "L", "raw_predicted": "M", "actual": "L"},  # calibration was right: raw M was under
        {"predicted": "M", "actual": "M", "excluded_from_calibration": True},
    ]
    acc = sizing_accuracy(rows)
    assert (acc["n"], acc["under"]) == (1, 1) and acc["by_predicted"] == {"M": {"n": 1, "exact": 0, "under": 1, "over": 0}}
    assert size_delta("XL", "L") == 0 and size_delta("M", "L") == 1 and size_delta("L", "S") == -2


def test_sizing_accuracy_counts_exact_under_over() -> None:
    rows = [
        {"predicted": "M", "actual": "M"},
        {"predicted": "M", "actual": "L"},
        {"predicted": "M", "actual": "XL"},
        {"predicted": "L", "actual": "XL"},  # L vs XL: exact
        {"predicted": "L", "actual": "S"},
        {"predicted": "S", "claude_calls": 2, "attempts": 1, "subtasks": 1},  # actual computed: S
        {"predicted": None, "actual": "M"},  # skipped
    ]
    acc = sizing_accuracy(rows)
    assert (acc["n"], acc["exact"], acc["under"], acc["over"]) == (6, 3, 2, 1)
    assert acc["by_predicted"] == {
        "S": {"n": 1, "exact": 1, "under": 0, "over": 0},
        "M": {"n": 3, "exact": 1, "under": 2, "over": 0},
        "L": {"n": 2, "exact": 1, "under": 0, "over": 1},
    }
    assert sizing_accuracy([]) == {"n": 0, "exact": 0, "under": 0, "over": 0, "by_predicted": {}}


# ------------------------------------------------------------------ queue


def test_record_sizing_outcome_counts_effort_and_is_idempotent(tmp_path: Path) -> None:
    clock = Clock()
    q = FoundryQueue(tmp_path / "q.db", clock=clock)
    tm = Telemetry(q)
    q.add_task("F-001", "Add clamp", "desc", "alpha", repo_root=tmp_path)
    tickets = q.apply_sizing(_directive("F-001", 2))
    t1, t2 = tickets
    q.claim_ticket("alpha", 1, "alpha-1")
    q.reopen_ticket(t1.ticket_id, "[pre-review] TODO left in clamp()")
    q.claim_ticket("alpha", 1, "alpha-1")
    q.reopen_ticket(t1.ticket_id, "gate failed: syntax")
    q.record_gate_result({"task_id": "F-001", "subtask_id": "F-001.1", "check_type": "syntax", "passed": False})
    q.record_gate_result({"task_id": "F-001", "subtask_id": "F-001.1", "check_type": "linter", "passed": True})
    q.record_verdict({"task_id": "F-001", "reviewer_id": "alpha-6", "verdict": "REVISE"})
    q.record_verdict({"task_id": "F-001", "reviewer_id": "alpha-6", "verdict": "APPROVED"})
    for _ in range(5):
        tm.record_tokens("F-001", "alpha-1", "builder", 100, 20)
    clock.t += timedelta(minutes=5)
    q.quarantine("F-001", "test")
    clock.t += timedelta(minutes=30)  # recorded later: wall time ends at the quarantine

    row = q.record_sizing_outcome("F-001", sizing_source="gemma", raw_predicted="S")
    assert row is not None
    assert row["task_id"] == "F-001" and row["title"] == "Add clamp"
    assert row["repo"] == normalize_repo_root(tmp_path)
    assert row["files"] == ["src/f1.py", "src/f2.py"]
    assert row["predicted"] == "M" and row["raw_predicted"] == "S" and row["sizing_source"] == "gemma"
    assert row["builders"] == 2 and row["subtasks"] == 2
    assert row["attempts"] == 4  # ticket 1: 3 attempts, ticket 2: 1
    assert row["pre_review_sendbacks"] == 1
    assert row["gate_failures"] == 1
    assert row["reviewer_rejections"] == 1
    assert row["claude_calls"] == 5 and row["tokens"] == 600
    assert row["wall_time_s"] == 300
    assert row["final_state"] == "quarantined"
    assert row["quarantine_kind"] == "other" and row["excluded_from_calibration"] is True
    assert row["actual"] == "M"  # 5 calls + 2 retries; an "other" quarantine is not floored at L

    # idempotent: the first row stays
    again = q.record_sizing_outcome("F-001", sizing_source="heuristic", attempts=99)
    assert again == row
    assert q.sizing_outcomes() == []  # excluded from calibration by default
    assert len(q.sizing_outcomes(include_excluded=True)) == 1
    assert q.record_sizing_outcome("nope") is None
    q.close()


def test_sizing_outcomes_filters_by_repo_newest_first_and_limits(tmp_path: Path) -> None:
    clock = Clock()
    q = FoundryQueue(":memory:", clock=clock)
    for i, repo in enumerate(["/r/a", "/r/b", "/r/a"], start=1):
        tid = f"F-00{i}"
        q.add_task(tid, f"task {i}", "", "alpha", repo_root=repo)
        clock.t += timedelta(seconds=1)
        q.record_sizing_outcome(tid, files=["x.py"], predicted="S", attempts=1, subtasks=1, claude_calls=2)
    assert [r["task_id"] for r in q.sizing_outcomes()] == ["F-003", "F-002", "F-001"]
    a = q.sizing_outcomes(repo="/r/a")
    assert [r["task_id"] for r in a] == ["F-003", "F-001"]
    assert all(r["actual"] == "S" and r["files"] == ["x.py"] for r in a)
    assert [r["task_id"] for r in q.sizing_outcomes(limit=1)] == ["F-003"]
    # keyword signature the Foreman uses
    assert q.sizing_outcomes(repo=None, limit=50)
    q.close()


def test_old_database_gains_the_table(tmp_path: Path) -> None:
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)")
    conn.commit()
    conn.close()
    q = FoundryQueue(db)
    assert q.sizing_outcomes() == []
    q.add_task("F-001", "t", "", "alpha")
    assert q.record_sizing_outcome("F-001")["final_state"] == "backlog"
    q.close()


# ------------------------------------------------------------------ floor manager


def test_completed_task_records_one_outcome_and_the_foreman_sees_it(tmp_path: Path) -> None:
    repo = write_demo_repo(tmp_path / "repo")
    claude = demo_claude(repo, fail_first="alpha-1")
    f = _factory(tmp_path, claude, repo=repo, teams=["alpha"])
    assert f.foreman.outcomes_provider == f.queue.sizing_outcomes
    ids, states = asyncio.run(_submit_and_run(f, S_ORDER))
    tid = ids[0]
    assert states[tid] == "complete"
    rows = f.queue.sizing_outcomes()
    assert len(rows) == 1
    row = rows[0]
    assert row["task_id"] == tid and row["title"] == S_ORDER["title"]
    assert row["files"] == S_ORDER["files"]
    assert row["final_state"] == "complete"
    assert row["predicted"] == f.manager.directives[tid].complexity
    assert row["sizing_source"] == f.manager.sizing_meta[tid].get("source")
    assert row["attempts"] == 2 and row["gate_failures"] >= 1
    assert row["claude_calls"] == 3  # 2 builds + 1 review
    assert row["actual"] in ("S", "M", "L", "XL")
    # calling completion again does not add a row
    asyncio.run(f.manager.on_task_complete(tid))
    assert len(f.queue.sizing_outcomes()) == 1
    assert f.foreman.outcomes_provider(repo=None, limit=50)[0]["task_id"] == tid
    f.close()


def test_quarantined_task_records_an_outcome_once(tmp_path: Path) -> None:
    repo = write_demo_repo(tmp_path / "repo")
    f = _factory(tmp_path, demo_claude(repo, fail_first=None), repo=repo, teams=["alpha"])

    async def go() -> str:
        tid = await f.manager.submit(WorkOrder(**S_ORDER))
        out = await f.manager.apply_escalation(
            _alert("QUARANTINE_TASK", params={"reason": "stuck"}, task_id=tid))
        assert out.applied
        f.manager.report_quarantines()
        return tid

    tid = asyncio.run(go())
    assert f.queue.sizing_outcomes() == []  # an escalation quarantine says nothing about size
    rows = f.queue.sizing_outcomes(include_excluded=True)
    assert [r["task_id"] for r in rows] == [tid]
    assert rows[0]["final_state"] == "quarantined" and rows[0]["quarantine_kind"] == "other"
    assert rows[0]["excluded_from_calibration"] is True
    f.manager.record_outcome(tid)
    assert len(f.queue.sizing_outcomes(include_excluded=True)) == 1
    f.close()


def test_record_outcome_is_best_effort_without_queue_support() -> None:
    from foundry.floor_manager import FloorManager

    class Q:
        def kv_get(self, *a: Any, **k: Any) -> Any:
            return None

    class Bridge:
        def __getattr__(self, name: str) -> Any:
            return lambda *a, **k: None

    m = FloorManager(Q(), object(), {}, None, Bridge())
    assert m.record_outcome("F-001") is None


def test_sizing_passes_the_task_repo_to_the_outcomes_provider(tmp_path: Path) -> None:
    repo = write_demo_repo(tmp_path / "repo")
    f = _factory(tmp_path, demo_claude(repo, fail_first=None), repo=repo, teams=["alpha"])
    q = f.queue
    here = str(repo.resolve())
    # history: 8 M-sized tasks of this repo that were actually L, 8 of another repo that were S
    clock_rows = [("/elsewhere/repo", "S", 2), (here, "L", 9)]
    n = 0
    for root, _actual, calls in clock_rows:
        for _ in range(8):
            n += 1
            tid = f"H-{n:03d}"
            q.add_task(tid, f"history {n}", "", "alpha", repo_root=root)
            q.record_sizing_outcome(tid, files=["shop/mathx.py"], predicted="M",
                                    attempts=3, subtasks=2, claude_calls=calls, final_state="complete")
    seen: list[Any] = []
    real = q.sizing_outcomes

    def provider(repo: Any = None, limit: int = 50) -> list[dict[str, Any]]:
        seen.append(repo)
        return real(repo=repo, limit=limit)

    f.foreman.outcomes_provider = provider
    tid = asyncio.run(f.manager.submit(WorkOrder(**S_ORDER)))
    assert seen and all(r is not None for r in seen)
    assert {normalize_repo_root(r) for r in seen} == {normalize_repo_root(here)}
    rows = provider(repo=seen[0])
    assert len(rows) == 8 and all(r["repo"] == normalize_repo_root(here) for r in rows)
    # newest first
    assert [r["task_id"] for r in rows] == [f"H-{i:03d}" for i in range(16, 8, -1)]
    assert f.manager.directives[tid]
    f.close()


def test_pre_review_sendback_that_hits_the_retry_cap_is_counted(tmp_path: Path) -> None:
    from foundry.schemas import MAX_RETRIES

    q = FoundryQueue(":memory:")
    q.add_task("F-001", "t", "", "alpha")
    (t,) = q.apply_sizing(_directive("F-001", 1, "S"))
    for _ in range(MAX_RETRIES):
        q.claim_ticket("alpha", 1, "alpha-1")
        assert q.reopen_ticket(t.ticket_id, "gate failed: syntax")
    q.claim_ticket("alpha", 1, "alpha-1")
    assert not q.reopen_ticket(t.ticket_id, "[pre-review] TODO left")  # retry_exhausted -> quarantined
    row = q.record_sizing_outcome("F-001")
    assert row["final_state"] == "quarantined"
    assert row["quarantine_kind"] == "retries_exhausted" and not row["excluded_from_calibration"]
    assert row["actual"] in ("L", "XL")  # floored: it needed more than it got
    assert row["pre_review_sendbacks"] == 1
    q.close()


def test_raw_size_flows_from_the_foreman_into_the_outcome(tmp_path: Path) -> None:
    repo = write_demo_repo(tmp_path / "repo")
    f = _factory(tmp_path, demo_claude(repo, fail_first=None), repo=repo, teams=["alpha"])
    real = f.foreman.size_task_scope

    async def sized(task: Any) -> Any:
        out = await real(task) if asyncio.iscoroutinefunction(real) else real(task)
        f.foreman.last_raw_complexity = "XL"  # pretend calibration shifted the size
        return out

    f.foreman.size_task_scope = sized
    ids, states = asyncio.run(_submit_and_run(f, S_ORDER))
    tid = ids[0]
    assert states[tid] == "complete"
    assert f.manager.sizing_meta[tid]["raw"] == "XL"
    row = f.queue.sizing_outcomes()[0]
    assert row["raw_predicted"] == "XL" and row["predicted"] == f.manager.directives[tid].complexity
    f.close()
    g = _factory(tmp_path, demo_claude(repo, fail_first=None), repo=repo, teams=["alpha"])
    assert g.manager.sizing_meta[tid]["raw"] == "XL"  # persisted across a restart
    g.close()


def test_old_outcomes_table_gains_the_new_columns(tmp_path: Path) -> None:
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE sizing_outcomes (task_id TEXT PRIMARY KEY, recorded_at TEXT NOT NULL,"
                 " repo TEXT, title TEXT NOT NULL DEFAULT '', files TEXT NOT NULL DEFAULT '[]',"
                 " predicted TEXT, sizing_source TEXT, builders INTEGER NOT NULL DEFAULT 0,"
                 " subtasks INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,"
                 " gate_failures INTEGER NOT NULL DEFAULT 0, pre_review_sendbacks INTEGER NOT NULL DEFAULT 0,"
                 " reviewer_rejections INTEGER NOT NULL DEFAULT 0, claude_calls INTEGER NOT NULL DEFAULT 0,"
                 " tokens INTEGER NOT NULL DEFAULT 0, wall_time_s REAL NOT NULL DEFAULT 0,"
                 " final_state TEXT NOT NULL, actual TEXT)")
    conn.execute("INSERT INTO sizing_outcomes (task_id, recorded_at, predicted, final_state, actual)"
                 " VALUES ('F-001', '2026-11-01T00:00:00Z', 'M', 'complete', 'M')")
    conn.commit()
    conn.close()
    q = FoundryQueue(db)
    (row,) = q.sizing_outcomes()
    assert row["raw_predicted"] is None and row["excluded_from_calibration"] is False
    q.close()
