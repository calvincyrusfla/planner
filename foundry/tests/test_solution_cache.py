"""Tests for foundry.solution_cache (SolutionCache + RetryPolicy)."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from foundry.schemas import CodeSnippet, ContextSlice, TaskAssignment
from foundry.solution_cache import CACHE_KEY_VERSION, RetryPolicy, SolutionCache, cache_key

DIFF = "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n"


def make_assignment(
    criteria: list[str] | None = None,
    files: list[str] | None = None,
    snippet_text: str = "def f():\n    ...\n",
    objective: str = "Make f return 2.",
    **overrides: object,
) -> TaskAssignment:
    crit = criteria if criteria is not None else ["Return 2 from f", "Keep the public API"]
    fields: dict[str, object] = dict(
        task_id="T-1",
        team_id="bravo",
        builder_id="bravo-1",
        subtask_id="T-1.1",
        acceptance_criteria=crit,
        allowed_files=files if files is not None else ["app.py", "lib/util.py"],
        context=ContextSlice(
            objective=objective,
            acceptance_criteria=crit,
            snippets=[CodeSnippet(path="app.py", start_line=1, end_line=2, text=snippet_text)],
        ),
    )
    fields.update(overrides)
    return TaskAssignment(**fields)  # type: ignore[arg-type]


@pytest.fixture()
def cache(tmp_path: Path) -> SolutionCache:
    c = SolutionCache(tmp_path / "foundry.db")
    yield c
    c.close()


def test_same_assignment_hits(cache: SolutionCache) -> None:
    a = make_assignment()
    assert cache.lookup(a) is None
    cache.store(a, DIFF)
    assert cache.lookup(make_assignment()) == DIFF
    entry = cache.get_entry(cache.key_for(a))
    assert entry is not None and entry["hits"] == 1 and entry["approved_at"]
    assert entry["last_hit_at"]


def test_who_and_when_do_not_change_key() -> None:
    base = cache_key(make_assignment())
    other = make_assignment(
        task_id="T-9", builder_id="bravo-5", subtask_id="T-9.3", attempt=2, fix_hint="fix line 3", team_id="alpha"
    )
    assert cache_key(other) == base


@pytest.mark.parametrize(
    "changed",
    [
        {"criteria": ["Return 3 from f", "Keep the public API"]},
        {"criteria": ["Return 2 from f"]},
        {"criteria": ["Return 2 from f", "Keep the public API", "Add a test"]},
        {"files": ["app.py"]},
        {"snippet_text": "def f():\n    return 1\n"},
    ],
)
def test_any_material_change_misses(cache: SolutionCache, changed: dict) -> None:
    cache.store(make_assignment(), DIFF)
    assert cache.lookup(make_assignment(**changed)) is None


def test_reordering_and_whitespace_case_still_hit(cache: SolutionCache) -> None:
    cache.store(make_assignment(), DIFF)
    reordered = make_assignment(
        criteria=["  keep the   PUBLIC api ", "return 2 FROM f"], files=["lib\\util.py", "./app.py"]
    )
    assert cache.lookup(reordered) == DIFF


def test_reviewer_repro_different_objective_misses(cache: SolutionCache) -> None:
    """charlie-6 round 1: task B must not be closed with task A's approved diff."""
    crit = ["Tests pass", "No new dependencies"]
    files = ["foundry/util.py"]
    task_a = make_assignment(criteria=crit, files=files, objective="Add a slug helper.", task_id="F-001")
    task_b = make_assignment(
        criteria=crit, files=files, objective="Rename the module docstring.", task_id="F-002"
    )
    cache.store(task_a, DIFF, title="Add a slug helper", description="slugify(text) -> str")
    assert cache.lookup(task_b, title="Rename the module docstring", description="Docs only") is None
    assert cache.lookup(task_b) is None
    assert cache.lookup(task_a, title="Add a slug helper", description="slugify(text) -> str") == DIFF


def test_title_and_description_are_part_of_key(cache: SolutionCache) -> None:
    a = make_assignment()
    cache.store(a, DIFF, title="Add slug helper", description="Adds slugify")
    assert cache.lookup(a, title="Add slug helper", description="Something else") is None
    assert cache.lookup(a, title="Other title", description="Adds slugify") is None
    assert cache.lookup(a) is None
    # whitespace/case-insensitive, and None == ""
    assert cache.lookup(a, title="  add SLUG   helper", description="adds slugify ") == DIFF
    assert cache_key(a, title=None, description=None) == cache_key(a, title="", description="")


def test_objective_whitespace_case_still_hits(cache: SolutionCache) -> None:
    cache.store(make_assignment(), DIFF)
    assert cache.lookup(make_assignment(objective="  make F   return 2. ")) == DIFF


def test_key_version_bumped() -> None:
    assert CACHE_KEY_VERSION == "v2"


def test_persistence_across_reopen(tmp_path: Path) -> None:
    db = tmp_path / "foundry.db"
    with SolutionCache(db) as c1:
        c1.store(make_assignment(), DIFF)
        assert c1.lookup(make_assignment()) == DIFF
    with SolutionCache(db) as c2:
        assert c2.lookup(make_assignment()) == DIFF
        stats = c2.stats()
        assert stats["entries"] == 1
        assert stats["total_hits"] == 2
        assert stats["lookups"] == 1 and stats["hits"] == 1 and stats["misses"] == 0
    mode = sqlite3.connect(db).execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_store_replaces_diff_keeps_hits(cache: SolutionCache) -> None:
    a = make_assignment()
    cache.store(a, DIFF)
    cache.lookup(a)
    cache.store(a, DIFF + "+y = 3\n")
    entry = cache.get_entry(cache.key_for(a))
    assert entry is not None and entry["hits"] == 1 and entry["diff"].endswith("+y = 3\n")


def test_empty_diff_rejected(cache: SolutionCache) -> None:
    with pytest.raises(ValueError):
        cache.store(make_assignment(), "   ")


def test_stats_counts_misses(cache: SolutionCache) -> None:
    cache.lookup(make_assignment())
    cache.store(make_assignment(), DIFF)
    cache.lookup(make_assignment())
    s = cache.stats()
    assert s == {
        "entries": 1, "total_hits": 1, "lookups": 2, "hits": 1, "misses": 1, "stores": 1, "hit_rate": 0.5,
    }


def test_concurrent_stores_shared_and_separate_connections(tmp_path: Path) -> None:
    db = tmp_path / "foundry.db"
    shared = SolutionCache(db)
    errors: list[BaseException] = []

    def worker(i: int) -> None:
        try:
            own = SolutionCache(db) if i % 2 else None
            target = own or shared
            for j in range(10):
                a = make_assignment(criteria=[f"criterion {i}-{j}"])
                target.store(a, DIFF)
                assert target.lookup(a) == DIFF
                target.store(make_assignment(), DIFF)  # same key from every thread
            if own:
                own.close()
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert shared.stats()["entries"] == 8 * 10 + 1
    shared.close()


def test_coexists_with_other_tables(tmp_path: Path) -> None:
    db = tmp_path / "foundry.db"
    other = sqlite3.connect(db)
    other.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY)")
    other.commit()
    with SolutionCache(db) as c:
        c.store(make_assignment(), DIFF)
    names = {r[0] for r in other.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"tasks", "solution_cache"} <= names
    other.close()


def test_retry_policy_default() -> None:
    p = RetryPolicy()
    assert p.max_retries == 2
    assert [p.should_retry(a) for a in (0, 1, 2)] == [True, True, False]
    assert [p.next_attempt(a) for a in (0, 1, 2)] == [1, 2, None]
    assert p.exhausted(2) and not p.exhausted(1)


def test_retry_policy_custom_and_invalid() -> None:
    assert RetryPolicy(max_retries=0).next_attempt(0) is None
    with pytest.raises(ValueError):
        RetryPolicy(max_retries=-1)
    with pytest.raises(ValueError):
        RetryPolicy().should_retry(-1)
