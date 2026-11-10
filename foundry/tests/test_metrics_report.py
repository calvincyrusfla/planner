"""Per-task usage on batch results and the periodic ``reportFoundryMetrics`` report."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from foundry.board_dispatch import BatchWatcher, BoardRefused
from foundry.metrics_report import MetricsUnavailable, build_metrics
from foundry.serve import DEFAULT_METRICS_REPORT_S, Service

CREATED = "2026-01-01T00:00:00Z"


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(":memory:")
    yield connection
    connection.close()


def add_usage(conn: sqlite3.Connection, rows: list[tuple[Any, Any, Any]], table: str = "usage",
              task: str = "task_id") -> None:
    conn.execute(f"CREATE TABLE IF NOT EXISTS {table} ({task} TEXT, input_tokens INTEGER, output_tokens INTEGER)")
    conn.executemany(f"INSERT INTO {table} VALUES (?, ?, ?)", rows)


# --------------------------------------------------------------------------- build_metrics


def test_totals_cover_every_row_and_by_task_only_rows_with_a_task(conn: sqlite3.Connection) -> None:
    add_usage(conn, [("F-1", 10, 1), ("F-1", 20, 2), ("F-2", 5, None), (None, 100, 50)])

    assert build_metrics(conn) == {
        "source": "usage",
        "totals": {"records": 4, "input_tokens": 135, "output_tokens": 53},
        "byTask": {
            "F-1": {"records": 2, "input_tokens": 30, "output_tokens": 3},
            "F-2": {"records": 1, "input_tokens": 5, "output_tokens": 0},
        },
        "savedByGemma": {},
    }


def test_an_empty_usage_table_reports_zero(conn: sqlite3.Connection) -> None:
    add_usage(conn, [])

    metrics = build_metrics(conn)

    assert metrics["totals"] == {"records": 0, "input_tokens": 0, "output_tokens": 0}
    assert metrics["byTask"] == {}


def test_a_column_named_task_is_accepted(conn: sqlite3.Connection) -> None:
    add_usage(conn, [("F-1", 1, 2)], task="task")

    assert build_metrics(conn)["byTask"] == {"F-1": {"records": 1, "input_tokens": 1, "output_tokens": 2}}


@pytest.mark.parametrize("attr", ["conn", "_conn", "connection", "_connection", "db", "_db"])
def test_the_connection_is_found_on_a_telemetry_object(conn: sqlite3.Connection, attr: str) -> None:
    add_usage(conn, [("F-1", 1, 2)])

    assert build_metrics(SimpleNamespace(**{attr: conn}))["source"] == "usage"


@pytest.mark.parametrize("telemetry", [None, object(), SimpleNamespace(conn="not a connection")])
def test_no_connection_is_unavailable(telemetry: Any) -> None:
    with pytest.raises(MetricsUnavailable):
        build_metrics(telemetry)


@pytest.mark.parametrize("schema", [
    "CREATE TABLE notes (task_id TEXT, body TEXT)",
    "CREATE TABLE counters (name TEXT, input_tokens INTEGER)",
    "CREATE TABLE savings (task_id TEXT, saved_tokens INTEGER)",
])
def test_a_schema_mismatch_is_unavailable_not_zero(conn: sqlite3.Connection, schema: str) -> None:
    conn.execute(schema)

    with pytest.raises(MetricsUnavailable):
        build_metrics(conn)


def test_saved_token_columns_are_not_usage(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE usage (task_id TEXT, input_tokens INTEGER, saved_tokens INTEGER)")
    conn.execute("INSERT INTO usage VALUES ('F-1', 4, 900)")

    assert build_metrics(conn)["totals"] == {"records": 1, "input_tokens": 4}


def test_the_table_with_the_most_rows_is_used(conn: sqlite3.Connection) -> None:
    add_usage(conn, [("F-1", 500, 500)], table="a_rollup")
    add_usage(conn, [("F-1", 1, 1), ("F-1", 2, 2)], table="b_raw")

    metrics = build_metrics(conn)

    assert metrics["source"] == "b_raw"
    assert metrics["totals"] == {"records": 2, "input_tokens": 3, "output_tokens": 3}


def test_equal_tables_are_chosen_by_name(conn: sqlite3.Connection) -> None:
    add_usage(conn, [("F-1", 1, 1)], table="b_usage")
    add_usage(conn, [("F-1", 2, 2)], table="a_usage")

    assert build_metrics(conn)["source"] == "a_usage"


def test_unusual_names_are_quoted(conn: sqlite3.Connection) -> None:
    conn.execute('CREATE TABLE "odd ""usage"" table" (task TEXT, "all tokens" INTEGER)')
    conn.execute('INSERT INTO "odd ""usage"" table" VALUES (\'F-1\', 5)')

    metrics = build_metrics(conn)

    assert metrics["source"] == 'odd "usage" table'
    assert metrics["byTask"] == {"F-1": {"records": 1, "all tokens": 5}}


@pytest.mark.parametrize(("extra", "avoided"), [
    ({}, 2),
    ({"claude_reviews_avoided": None}, 2),
    ({"claude_reviews_avoided": 5}, 5),
    ({"claude_reviews_avoided": 0}, 0),
])
def test_saved_by_gemma(conn: sqlite3.Connection, extra: dict[str, Any], avoided: int) -> None:
    add_usage(conn, [])
    raw = {"passed": 3, "sent_back": 2, "advised": 1, "skipped": None, "fallback": 4, **extra}

    metrics = build_metrics(SimpleNamespace(conn=conn, pre_review_counts=lambda: raw))

    assert metrics["savedByGemma"] == {"passed": 3, "sentBack": 2, "advised": 1, "skipped": 0, "fallback": 4,
                                       "claudeReviewsAvoided": avoided}


def test_counters_that_are_not_a_mapping_are_left_out(conn: sqlite3.Connection) -> None:
    add_usage(conn, [])

    assert build_metrics(SimpleNamespace(conn=conn, pre_review_counts=lambda: None))["savedByGemma"] == {}


# --------------------------------------------------------------------------- batch results


class FakeQueue:
    def __init__(self, tasks: dict[str, dict[str, Any]]) -> None:
        self.tasks = tasks
        self.kv: dict[str, str] = {}

    def get_task(self, tid: str) -> dict[str, Any] | None:
        return self.tasks.get(tid)

    def kv_get(self, key: str) -> Any:
        raw = self.kv.get(key)
        return None if raw is None else json.loads(raw)

    def kv_set(self, key: str, value: Any) -> None:
        self.kv[key] = json.dumps(value)


class FakeBoard:
    def __init__(self, *, refuse_usage: bool = False) -> None:
        self.refuse_usage = refuse_usage
        self.batches: list[dict[str, Any]] = []
        self.ops: list[tuple[str, dict[str, Any]]] = []

    def state(self) -> dict[str, Any]:
        return {"foundryBatches": self.batches}

    def op(self, type_: str, args: dict[str, Any]) -> dict[str, Any]:
        self.ops.append((type_, json.loads(json.dumps(args))))
        if self.refuse_usage and any("usage" in r for r in (args.get("results") or {}).values()):
            raise BoardRefused("unknown field: usage", 400)
        return {"ok": True}


class Harness:
    """A watcher already running batch S-1, whose subtask ``a`` is Foundry task F-1."""

    def __init__(self, telemetry: Any, *, refuse_usage: bool = False) -> None:
        self.clock = 1000.0
        self.queue = FakeQueue({"F-1": {"state": "building"}})
        manager = SimpleNamespace(auto_pr=False, publisher=None, orders={})
        factory = SimpleNamespace(queue=self.queue, manager=manager, telemetry=telemetry)
        service = SimpleNamespace(repo="repo", running=True, factory=factory, config=None)
        self.board = FakeBoard(refuse_usage=refuse_usage)
        self.watcher = BatchWatcher(service, self.board, "o/r", "pc", 15.0, enabled=True, now=lambda: self.clock)
        self.board.batches.append({
            "id": "S-1", "repo": "o/r", "status": "running", "createdAt": CREATED,
            "claimedBy": {"service": self.watcher.service_id}, "subtasks": [{"key": "a", "title": "Part a"}],
        })
        self.queue.kv_set("foundry_batch:S-1", {"createdAt": CREATED, "tasks": {"a": "F-1"}})

    def poll(self) -> None:
        assert asyncio.run(self.watcher.poll_once()) == ["S-1"]
        assert self.watcher.last_error is None

    def heartbeat_passes(self) -> None:
        self.clock += self.watcher.heartbeat_s

    @property
    def updates(self) -> list[dict[str, Any]]:
        return [args for type_, args in self.board.ops if type_ == "updateBatch"]


USAGE_ROWS: list[tuple[Any, Any, Any]] = [("F-1", 10, 2), ("F-1", 20, 5)]
USAGE = {"records": 2, "input_tokens": 30, "output_tokens": 7}


def test_a_changed_result_carries_the_usage_of_its_task(conn: sqlite3.Connection) -> None:
    add_usage(conn, [*USAGE_ROWS, ("F-2", 999, 999)])
    h = Harness(conn)

    h.poll()

    assert h.updates[0]["results"] == {
        "a": {"foundryTask": "F-1", "state": "building", "error": None, "usage": USAGE},
    }


def test_usage_alone_never_adds_a_board_write(conn: sqlite3.Connection) -> None:
    add_usage(conn, USAGE_ROWS)
    h = Harness(conn)
    h.poll()

    add_usage(conn, [("F-1", 1, 1)])
    h.poll()

    assert len(h.updates) == 1


def test_usage_that_moved_goes_out_with_the_next_heartbeat(conn: sqlite3.Connection) -> None:
    add_usage(conn, USAGE_ROWS)
    h = Harness(conn)
    h.poll()

    add_usage(conn, [("F-1", 1, 1)])
    h.heartbeat_passes()
    h.poll()

    assert h.updates[1]["results"]["a"]["usage"] == {"records": 3, "input_tokens": 31, "output_tokens": 8}
    assert h.updates[1]["results"]["a"]["state"] == "building"


def test_a_heartbeat_with_unchanged_usage_has_no_results(conn: sqlite3.Connection) -> None:
    add_usage(conn, USAGE_ROWS)
    h = Harness(conn)
    h.poll()

    h.heartbeat_passes()
    h.poll()

    assert len(h.updates) == 2
    assert "results" not in h.updates[1]


def test_a_state_change_carries_usage_even_when_it_did_not_move(conn: sqlite3.Connection) -> None:
    add_usage(conn, USAGE_ROWS)
    h = Harness(conn)
    h.poll()

    h.queue.tasks["F-1"]["state"] = "reviewing"
    h.poll()

    assert h.updates[1]["results"]["a"]["state"] == "reviewing"
    assert h.updates[1]["results"]["a"]["usage"] == USAGE


def test_a_task_without_usage_rows_goes_out_without_usage(conn: sqlite3.Connection) -> None:
    add_usage(conn, [("F-2", 1, 1)])
    h = Harness(conn)

    h.poll()

    assert h.updates[0]["results"]["a"]["state"] == "building"
    assert "usage" not in h.updates[0]["results"]["a"]


def test_unreadable_telemetry_does_not_stop_the_results() -> None:
    h = Harness(None)

    h.poll()

    assert h.updates[0]["results"]["a"]["state"] == "building"
    assert "usage" not in h.updates[0]["results"]["a"]


def test_a_board_refusing_usage_gets_the_results_without_it(conn: sqlite3.Connection) -> None:
    add_usage(conn, USAGE_ROWS)
    h = Harness(conn, refuse_usage=True)

    h.poll()
    h.poll()
    h.poll()

    assert len(h.updates) == 2
    assert "usage" in h.updates[0]["results"]["a"]
    assert h.updates[1]["results"] == {"a": {"foundryTask": "F-1", "state": "building", "error": None}}


# --------------------------------------------------------------------------- periodic report


class RecordingBridge:
    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error
        self.sent: list[tuple[str, dict[str, Any]]] = []

    def op(self, type_: str, args: dict[str, Any]) -> None:
        if self.error is not None:
            raise self.error
        self.sent.append((type_, dict(args)))


class AsyncBridge:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def post_op(self, type_: str, args: dict[str, Any]) -> None:
        self.sent.append(type_)


def make_service(tmp_path: Path, telemetry: Any = None, *, bridge: Any = None, mode: str = "auto",
                 watcher: Any = None, queue: Any = None) -> Service:
    factory = SimpleNamespace(telemetry=telemetry, bridge=bridge, queue=queue)
    return Service(SimpleNamespace(board_bridge_mode=mode), repo=tmp_path, factory=factory, monitor_s=60.0,
                   batch_watcher=watcher)


def test_a_report_goes_through_the_bridge(conn: sqlite3.Connection, tmp_path: Path) -> None:
    add_usage(conn, USAGE_ROWS)
    bridge = RecordingBridge()
    service = make_service(tmp_path, conn, bridge=bridge)

    assert asyncio.run(service.report_metrics()) is True

    assert bridge.sent == [("reportFoundryMetrics", {
        "source": "usage", "totals": USAGE, "byTask": {"F-1": USAGE}, "savedByGemma": {},
        "service": f"foundry:{service.repo.name}",
    })]
    assert service.metrics_reports == 1
    assert service.last_metrics_error is None


def test_an_async_bridge_is_awaited(conn: sqlite3.Connection, tmp_path: Path) -> None:
    add_usage(conn, USAGE_ROWS)
    bridge = AsyncBridge()

    assert asyncio.run(make_service(tmp_path, conn, bridge=bridge).report_metrics()) is True

    assert bridge.sent == ["reportFoundryMetrics"]


def test_without_a_bridge_auto_mode_uses_the_batch_board(conn: sqlite3.Connection, tmp_path: Path) -> None:
    add_usage(conn, USAGE_ROWS)
    board = FakeBoard()
    watcher = SimpleNamespace(board=board, service_id="foundry@pc:o/r")

    assert asyncio.run(make_service(tmp_path, conn, watcher=watcher).report_metrics()) is True

    assert [type_ for type_, _ in board.ops] == ["reportFoundryMetrics"]
    assert board.ops[0][1]["service"] == "foundry@pc:o/r"


def test_outside_auto_mode_the_batch_board_is_not_written(conn: sqlite3.Connection, tmp_path: Path) -> None:
    add_usage(conn, USAGE_ROWS)
    board = FakeBoard()
    watcher = SimpleNamespace(board=board, service_id="foundry@pc:o/r")

    assert asyncio.run(make_service(tmp_path, conn, mode="dry", watcher=watcher).report_metrics()) is False

    assert board.ops == []


def test_unreadable_telemetry_sends_nothing(tmp_path: Path) -> None:
    bridge = RecordingBridge()
    service = make_service(tmp_path, None, bridge=bridge)

    assert asyncio.run(service.report_metrics()) is False

    assert bridge.sent == []
    assert service.metrics_reports == 0
    assert str(service.last_metrics_error).startswith("MetricsUnavailable")


def test_a_failed_send_is_recorded_and_the_next_report_clears_it(conn: sqlite3.Connection, tmp_path: Path) -> None:
    add_usage(conn, USAGE_ROWS)
    bridge = RecordingBridge(RuntimeError("hub down"))
    service = make_service(tmp_path, conn, bridge=bridge)

    assert asyncio.run(service.report_metrics()) is False
    assert service.last_metrics_error == "RuntimeError: hub down"

    bridge.error = None
    assert asyncio.run(service.report_metrics()) is True
    assert service.last_metrics_error is None


def test_cancellation_is_not_swallowed(conn: sqlite3.Connection, tmp_path: Path) -> None:
    add_usage(conn, USAGE_ROWS)
    service = make_service(tmp_path, conn, bridge=RecordingBridge(asyncio.CancelledError()))

    async def go() -> None:
        with pytest.raises(asyncio.CancelledError):
            await service.report_metrics()

    asyncio.run(go())


def test_the_report_interval_comes_from_the_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FOUNDRY_METRICS_REPORT_S", raising=False)
    assert make_service(tmp_path).metrics_s == DEFAULT_METRICS_REPORT_S

    monkeypatch.setenv("FOUNDRY_METRICS_REPORT_S", "900")
    assert make_service(tmp_path).metrics_s == 900.0


def due_service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, finished: set[str]) -> tuple[Service, list[int]]:
    service = make_service(tmp_path)
    service.metrics_s = 100.0
    calls: list[int] = []

    async def report() -> bool:
        calls.append(1)
        return True

    monkeypatch.setattr(service, "report_metrics", report)
    monkeypatch.setattr(service, "_finished_tasks", lambda: set(finished))
    return service, calls


def test_a_report_is_due_once_the_interval_has_passed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service, calls = due_service(tmp_path, monkeypatch, set())

    asyncio.run(service._report_metrics_if_due(60.0))
    assert calls == []

    asyncio.run(service._report_metrics_if_due(40.0))
    assert calls == [1]

    asyncio.run(service._report_metrics_if_due(60.0))
    assert calls == [1]


def test_a_finished_task_brings_the_report_forward_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    finished: set[str] = set()
    service, calls = due_service(tmp_path, monkeypatch, finished)

    asyncio.run(service._report_metrics_if_due(60.0))
    assert calls == []

    finished.add("F-1")
    asyncio.run(service._report_metrics_if_due(1.0))
    assert calls == [1]

    # the early report restarted the interval, and F-1 is not new any more
    asyncio.run(service._report_metrics_if_due(60.0))
    assert calls == [1]


def test_finished_tasks_are_this_repos_terminal_tasks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tasks = [{"task_id": "F-1", "state": "complete"}, {"task_id": "F-2", "state": "building"},
             {"task_id": "F-9", "state": "failed"}]
    service = make_service(tmp_path, queue=SimpleNamespace(all_tasks=lambda: tasks))
    monkeypatch.setattr(service, "_mine", lambda tid: tid != "F-9")

    assert service._finished_tasks() == {"F-1"}


def test_an_unreadable_queue_keeps_the_last_finished_set(tmp_path: Path) -> None:
    def broken() -> list[dict[str, Any]]:
        raise RuntimeError("queue closed")

    service = make_service(tmp_path, queue=SimpleNamespace(all_tasks=broken))
    service._metrics_finished = {"F-1"}

    assert service._finished_tasks() == {"F-1"}


# --------------------------------------------------------------------------- run settings and attempts


class TicketQueue:
    """Answers ``tickets_for_task`` with tickets of the given attempts, or raises ``error``."""

    def __init__(self, attempts: list[int] | None = None, error: Exception | None = None) -> None:
        self.attempts = attempts or []
        self.error = error
        self.asked: list[Any] = []

    def tickets_for_task(self, task_id: Any) -> list[SimpleNamespace]:
        self.asked.append(task_id)
        if self.error is not None:
            raise self.error
        return [SimpleNamespace(attempt=a) for a in self.attempts]


class BrokenConfig:
    @property
    def builders_per_team(self) -> int:
        raise RuntimeError("config is broken")


PILOT_CONFIG = SimpleNamespace(builders_per_team=1, help_after=1, max_retries=2, pre_review="advise")
PILOT_RUN = {"builderTools": "off", "reviewerTools": "off", "buildersPerTeam": 1, "helpAfter": 1, "maxRetries": 2,
             "preReview": "advise"}


def usage_results(telemetry: Any, queue: Any, *, pushed: Any = None, changed: bool = True,
                  **service_fields: Any) -> dict[str, dict[str, Any]]:
    """``_usage_results`` for subtask ``a`` (Foundry task F-1); ``pushed`` is its usage already sent."""
    service = SimpleNamespace(factory=SimpleNamespace(telemetry=telemetry, queue=queue), **service_fields)
    watcher = BatchWatcher(service, object(), repo_slug="o/r", host="pc", enabled=True, interval=1)
    view = {"a": {"foundryTask": "F-1", "state": "building", "error": None}}
    rec = {"tasks": {"a": "F-1"}, "usage": {} if pushed is None else {"a": pushed}}
    return watcher._usage_results(rec, view, view if changed else {})


def test_usage_carries_the_run_settings_with_a_default_for_a_missing_field(conn: sqlite3.Connection) -> None:
    add_usage(conn, USAGE_ROWS)

    out = usage_results(conn, TicketQueue(), config=PILOT_CONFIG)

    assert out["a"]["usage"] == {**USAGE, "run": PILOT_RUN}


def test_usage_reports_the_configured_builder_tools(conn: sqlite3.Connection) -> None:
    add_usage(conn, USAGE_ROWS)
    config = SimpleNamespace(builder_tools="read")

    out = usage_results(conn, TicketQueue(), config=config)

    assert out["a"]["usage"]["run"] == {"builderTools": "read", "buildersPerTeam": None, "helpAfter": None,
                                         "maxRetries": None, "preReview": None, "reviewerTools": "off"}


def test_attempts_is_the_highest_ticket_attempt_plus_one(conn: sqlite3.Connection) -> None:
    add_usage(conn, USAGE_ROWS)
    queue = TicketQueue([0, 2])

    out = usage_results(conn, queue)

    assert out["a"]["usage"] == {**USAGE, "attempts": 3}
    assert queue.asked == ["F-1"]


def test_a_queue_that_raises_gives_usage_without_attempts(conn: sqlite3.Connection) -> None:
    add_usage(conn, USAGE_ROWS)

    out = usage_results(conn, TicketQueue(error=RuntimeError("database is locked")), config=PILOT_CONFIG)

    assert out["a"]["usage"] == {**USAGE, "run": PILOT_RUN}


@pytest.mark.parametrize("queue", [TicketQueue(), object(), None])
def test_no_tickets_or_no_ticket_reader_gives_usage_without_attempts(conn: sqlite3.Connection, queue: Any) -> None:
    add_usage(conn, USAGE_ROWS)

    assert "attempts" not in usage_results(conn, queue)["a"]["usage"]


@pytest.mark.parametrize("fields", [{}, {"config": None}, {"config": BrokenConfig()}])
def test_no_readable_config_gives_usage_without_run(conn: sqlite3.Connection, fields: dict[str, Any]) -> None:
    add_usage(conn, USAGE_ROWS)

    out = usage_results(conn, TicketQueue([1]), **fields)

    assert out["a"]["usage"] == {**USAGE, "attempts": 2}


def test_a_change_in_attempts_counts_as_usage_that_moved(conn: sqlite3.Connection) -> None:
    add_usage(conn, USAGE_ROWS)
    pushed = usage_results(conn, TicketQueue([0]), config=PILOT_CONFIG)["a"]["usage"]

    same = usage_results(conn, TicketQueue([0]), pushed=pushed, changed=False, config=PILOT_CONFIG)
    moved = usage_results(conn, TicketQueue([0, 1]), pushed=pushed, changed=False, config=PILOT_CONFIG)

    assert same == {}
    assert moved["a"]["usage"] == {**USAGE, "run": PILOT_RUN, "attempts": 2}


def test_a_task_without_usage_rows_gets_no_run_or_attempts(conn: sqlite3.Connection) -> None:
    add_usage(conn, [("F-2", 1, 1)])

    assert usage_results(conn, TicketQueue([3]), config=PILOT_CONFIG) == {}
