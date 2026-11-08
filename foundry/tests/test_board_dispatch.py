"""Tests for foundry/board_dispatch.py (BatchWatcher; owner bravo-1).

A fake board client mirrors the department's claimBatch/updateBatch rules
(department/lib/foundry-batches.mjs); the Foundry service runs with simulated
Claude/Gemma, a dry board bridge and a fake PR publisher.
"""

from __future__ import annotations

import asyncio
import copy
import json
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from foundry.board_bridge import BoardBridge
from foundry.board_dispatch import (
    BatchWatcher, BoardRefused, BoardUnavailable, DashboardBoard, batch_poll_interval, batches_enabled,
    marker, repo_slug_for,
)
from foundry.config import FoundryConfig
from foundry.main import demo_claude, demo_gemma, write_demo_repo
from foundry.serve import Service

SLUG = "me/shop"
TERMINAL = {"complete", "quarantined", "failed"}


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class FakeBoard:
    """In-memory board with the same claim/update rules as the department ops."""

    def __init__(self) -> None:
        self.batches: list[dict[str, Any]] = []
        self.ops: list[tuple[str, dict[str, Any]]] = []
        self.down = False
        self.lock = threading.Lock()

    def add(self, repo: str = SLUG, subtasks: list[dict[str, Any]] | None = None, **kw: Any) -> dict[str, Any]:
        n = len(self.batches) + 1
        subs = subtasks or [
            {"key": "1", "title": "Add clamp helper", "description": "Add clamp(x, lo, hi).", "files": ["shop/mathx.py"]},
            {"key": "2", "title": "Add lerp helper", "description": "Add lerp(a, b, t).", "files": ["shop/mathx.py"]},
        ]
        b = {"id": f"S-{n:03d}", "title": f"Batch {n}", "goal": "", "repo": repo, "base": None, "status": "queued",
             "claimedBy": None, "heartbeatAt": None, "note": "", "subtasks": subs, "results": {}, **kw}
        self.batches.append(b)
        return b

    def get(self, bid: str) -> dict[str, Any]:
        return next(b for b in self.batches if b["id"] == bid)

    def state(self) -> dict[str, Any]:
        if self.down:
            raise BoardUnavailable("down")
        with self.lock:
            return {"tasks": [], "foundryBatches": copy.deepcopy(self.batches)}

    def op(self, type_: str, args: dict[str, Any]) -> Any:
        if self.down:
            raise BoardUnavailable("down")
        with self.lock:
            self.ops.append((type_, copy.deepcopy(args)))
            b = self.get(args["id"])
            now = _iso(datetime.now(timezone.utc))
            if type_ == "claimBatch":
                by = b.get("claimedBy") or {}
                mine = by.get("service") == args["service"]
                stale = b.get("heartbeatAt") and (
                    datetime.now(timezone.utc) - datetime.fromisoformat(b["heartbeatAt"].replace("Z", "+00:00"))
                ).total_seconds() > 600
                if not (b["status"] == "queued" or (b["status"] in ("claimed", "running") and (mine or stale))):
                    return {"ok": False, "reason": f"batch is {b['status']}", "batch": copy.deepcopy(b)}
                b["claimedBy"] = {"host": args["host"], "service": args["service"], "at": now}
                if b["status"] == "queued":
                    b["status"] = "claimed"
                b["heartbeatAt"] = now
                return {"ok": True, "batch": copy.deepcopy(b)}
            if type_ == "updateBatch":
                if (b.get("claimedBy") or {}).get("service") != args["service"]:
                    raise BoardRefused(f"{b['id']} is not claimed by {args['service']}", 409)
                if b["status"] not in ("queued", "claimed", "running"):
                    raise BoardRefused(f"{b['id']} is {b['status']}", 409)
                for key, part in (args.get("results") or {}).items():
                    assert key in {s["key"] for s in b["subtasks"]}
                    assert part.get("state") in (None, "queued", "sizing", "building", "reviewing", "complete",
                                                 "quarantined", "failed")
                    b["results"][key] = {**b["results"].get(key, {}), **part}
                if args.get("note"):
                    b["note"] = args["note"]
                b["heartbeatAt"] = now
                if args.get("status"):
                    b["status"] = args["status"]
                elif args.get("results") and b["status"] == "claimed":
                    b["status"] = "running"
                states = [b["results"].get(s["key"], {}).get("state") for s in b["subtasks"]]
                if all(s in TERMINAL for s in states):
                    b["status"] = "done" if all(s == "complete" for s in states) else "failed"
                return {"ok": True, "batch": copy.deepcopy(b)}
            raise AssertionError(type_)

    def count(self, type_: str) -> int:
        return sum(1 for t, _ in self.ops if t == type_)


class FakePublisher:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def preflight(self) -> dict[str, Any]:
        return {"ok": True, "reason": None}

    async def publish(self, task_id: str, title: str, patch_path: Any, pr: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(task_id)
        return {"ok": True, "branch": f"foundry/{task_id.lower()}-x", "commit": "abc123",
                "pr_url": f"https://github.com/me/shop/pull/{len(self.calls)}", "error": None, "stage": None}


def _service(tmp_path: Path, *, publisher: Any = None, repo: Path | None = None, **kw: Any) -> Service:
    repo = repo or write_demo_repo(tmp_path / "repo")
    cfg = FoundryConfig(db_path=tmp_path / "foundry.db", rpm=1000, tpm=10_000_000, max_concurrent=5,
                        teams=["alpha", "bravo"], board_bridge_mode="dry", auto_pr=publisher is not None)
    fk: dict[str, Any] = {"bridge": BoardBridge("dry"), "patches_dir": tmp_path / "patches"}
    if publisher is not None:
        fk["publisher"] = publisher
    return Service(cfg, repo=repo, claude=demo_claude(repo, fail_first=None), gemma=demo_gemma(),
                   monitor_s=3600, factory_kwargs=fk, **kw)


def _watcher(svc: Service, board: FakeBoard, **kw: Any) -> BatchWatcher:
    kw.setdefault("repo_slug", SLUG)
    kw.setdefault("host", "pc1")
    return BatchWatcher(svc, board, enabled=kw.pop("enabled", True), interval=1, **kw)


async def _pump(w: BatchWatcher, pred: Any, timeout: float = 20.0) -> None:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while True:
        await w.poll_once()
        if pred():
            return
        assert loop.time() < end, "condition not reached in time"
        await asyncio.sleep(0.02)


# --------------------------------------------------------------------------- settings


def test_settings_from_env() -> None:
    assert batches_enabled({}) is True
    assert batches_enabled({"FOUNDRY_BATCHES": "off"}) is False
    assert batch_poll_interval({}) == 15.0
    assert batch_poll_interval({"FOUNDRY_BATCH_POLL_S": "5"}) == 5.0
    assert batch_poll_interval({"FOUNDRY_BATCH_POLL_S": "nope"}) == 15.0
    assert repo_slug_for("/nowhere", {"FOUNDRY_BATCH_REPO": "Me/Shop"}) == "Me/Shop"


def test_repo_slug_from_origin(tmp_path: Path) -> None:
    import shutil
    import subprocess

    if shutil.which("git") is None:
        pytest.skip("git not installed")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "remote", "add", "origin", "git@github.com:Me/Shop.git"], check=True)
    assert repo_slug_for(tmp_path, {}) == "Me/Shop"
    assert repo_slug_for(tmp_path / "missing", {}) is None


# --------------------------------------------------------------------------- the route


def test_claims_own_repo_runs_subtasks_and_reports_prs(tmp_path: Path) -> None:
    board = FakeBoard()
    other = board.add(repo="someone/else")
    mine = board.add(repo="ME/Shop")  # case-insensitive match
    pub = FakePublisher()

    async def go() -> None:
        svc = _service(tmp_path, publisher=pub)
        await svc.start()
        try:
            w = _watcher(svc, board)
            await _pump(w, lambda: board.get(mine["id"])["status"] == "done")
        finally:
            await svc.stop()

    asyncio.run(go())
    b = board.get(mine["id"])
    assert b["claimedBy"]["host"] == "pc1"
    assert b["claimedBy"]["service"] == "foundry@pc1:me/shop"
    tids = [b["results"][k]["foundryTask"] for k in ("1", "2")]
    assert all(t and t.startswith("F-") for t in tids) and len(set(tids)) == 2
    for k in ("1", "2"):
        r = b["results"][k]
        assert r["state"] == "complete"
        assert r["pr"]["state"] == "opened" and r["pr"]["url"].startswith("https://github.com/me/shop/pull/")
    assert sorted(pub.calls) == sorted(tids)
    # the other repo's batch is never touched
    assert board.get(other["id"])["status"] == "queued"
    assert all(a["id"] != other["id"] for _, a in board.ops)
    # every update is a heartbeat from the claimer
    ups = [a for t, a in board.ops if t == "updateBatch"]
    assert ups and all(a["heartbeat"] is True and a["service"] == "foundry@pc1:me/shop" for a in ups)


def test_complete_waits_for_the_pr_before_the_board_closes_the_batch(tmp_path: Path) -> None:
    """The board refuses updates once a batch is done, so complete is only sent with a settled PR."""
    board = FakeBoard()
    b = board.add(subtasks=[{"key": "1", "title": "Add clamp helper", "description": "clamp", "files": ["shop/mathx.py"]}])
    pub = FakePublisher()

    async def go() -> None:
        svc = _service(tmp_path, publisher=pub)
        await svc.start()
        try:
            w = _watcher(svc, board)
            await _pump(w, lambda: board.get(b["id"])["status"] == "done")
            assert w._record(b["id"]).get("closed") is True
        finally:
            await svc.stop()

    asyncio.run(go())
    for _, a in board.ops:
        for r in (a.get("results") or {}).values():
            if r.get("state") == "complete":
                assert r["pr"]["state"] not in (None, "pending")


def test_depends_on_waits_for_completion(tmp_path: Path) -> None:
    board = FakeBoard()
    b = board.add(subtasks=[
        {"key": "1", "title": "Add clamp helper", "description": "clamp", "files": ["shop/mathx.py"]},
        {"key": "2", "title": "Use clamp", "description": "use it", "files": ["shop/mathx.py"], "dependsOn": ["1"]},
    ])
    submitted_while_waiting: list[dict[str, Any]] = []

    async def go() -> None:
        svc = _service(tmp_path)
        await svc.start()
        try:
            w = _watcher(svc, board)
            await w.poll_once()
            rec = w._record(b["id"])
            assert set(rec["tasks"]) == {"1"}
            assert board.get(b["id"])["results"]["2"]["state"] == "queued"
            submitted_while_waiting.append(dict(rec["tasks"]))
            await _pump(w, lambda: board.get(b["id"])["status"] == "done")
            assert set(w._record(b["id"])["tasks"]) == {"1", "2"}
        finally:
            await svc.stop()

    asyncio.run(go())
    assert submitted_while_waiting == [{"1": board.get(b["id"])["results"]["1"]["foundryTask"]}]


def test_unsubmittable_subtask_fails_with_reason_and_dependents_fail(tmp_path: Path) -> None:
    board = FakeBoard()
    b = board.add(subtasks=[
        {"key": "1", "title": "Bad paths", "description": "x", "files": ["../outside.py"]},
        {"key": "2", "title": "After bad", "description": "y", "dependsOn": ["1"]},
    ])

    async def go() -> None:
        svc = _service(tmp_path)
        await svc.start()
        try:
            w = _watcher(svc, board)
            await w.poll_once()
            assert not svc.factory.manager.orders
        finally:
            await svc.stop()

    asyncio.run(go())
    got = board.get(b["id"])
    assert got["status"] == "failed"
    assert got["results"]["1"]["state"] == "failed" and "repo-relative" in got["results"]["1"]["error"]
    assert got["results"]["2"]["state"] == "failed" and "dependency 1" in got["results"]["2"]["error"]


def test_restart_resumes_without_resubmitting(tmp_path: Path) -> None:
    board = FakeBoard()
    b = board.add()
    repo = write_demo_repo(tmp_path / "repo")
    first: dict[str, str] = {}

    async def run_once() -> None:
        svc = _service(tmp_path, repo=repo)
        await svc.start()
        try:
            w = _watcher(svc, board)
            await w.poll_once()
            first.update(w._record(b["id"])["tasks"])
        finally:
            await svc.stop()

    async def run_again() -> None:
        svc = _service(tmp_path, repo=repo)
        await svc.start()
        try:
            before = set(svc.factory.manager.orders)
            w = _watcher(svc, board)
            await _pump(w, lambda: board.get(b["id"])["status"] == "done")
            assert set(svc.factory.manager.orders) == before  # nothing submitted again
            assert w._record(b["id"])["tasks"] == first
        finally:
            await svc.stop()

    asyncio.run(run_once())
    assert set(first) == {"1", "2"}
    asyncio.run(run_again())
    assert board.count("claimBatch") == 1  # the second run recognised its own claim


def test_lost_mapping_is_recovered_from_the_order_marker(tmp_path: Path) -> None:
    board = FakeBoard()
    b = board.add()

    async def go() -> None:
        svc = _service(tmp_path)
        await svc.start()
        try:
            w = _watcher(svc, board)
            await w.poll_once()
            tasks = dict(w._record(b["id"])["tasks"])
            svc.factory.queue.kv_delete("foundry_batch:" + b["id"])  # cut off before the kv write
            n = len(svc.factory.manager.orders)
            await w.poll_once()
            assert len(svc.factory.manager.orders) == n
            assert w._record(b["id"])["tasks"] == tasks
            order = svc.factory.manager.orders[tasks["1"]]
            assert marker(b["id"], "1") in order.description
        finally:
            await svc.stop()

    asyncio.run(go())


def test_sizes_and_acceptance_reach_the_order(tmp_path: Path) -> None:
    board = FakeBoard()
    b = board.add(goal="Make math nicer", subtasks=[
        {"key": "1", "title": "Add clamp helper", "description": "clamp", "files": ["shop/mathx.py"], "size": "M",
         "acceptance": ["clamp(5, 0, 3) == 3"]},
    ])

    async def go() -> None:
        svc = _service(tmp_path)
        await svc.start()
        try:
            w = _watcher(svc, board)
            await w.poll_once()
            tid = w._record(b["id"])["tasks"]["1"]
            o = svc.factory.manager.orders[tid]
            assert o.files == ["shop/mathx.py"]
            assert o.acceptance_criteria == ["clamp(5, 0, 3) == 3"]
            assert "Requested size: M" in o.description and "Make math nicer" in o.description
            assert Path(o.repo_root) == svc.repo
        finally:
            await svc.stop()

    asyncio.run(go())


# --------------------------------------------------------------------------- never claims


def test_never_claims_when_off_or_not_running(tmp_path: Path) -> None:
    board = FakeBoard()
    board.add()

    async def go() -> None:
        svc = _service(tmp_path)
        w_off = _watcher(svc, board, enabled=False)
        assert await w_off.poll_once() == []
        w = _watcher(svc, board)
        assert await w.poll_once() == []  # not started
        await svc.start()
        try:
            assert await w_off.poll_once() == []
            assert w_off.start() is None
        finally:
            await svc.stop()
        assert await w.poll_once() == []  # stopped

    asyncio.run(go())
    assert board.ops == []


def test_fresh_claim_of_another_foundry_is_left_alone_stale_one_is_taken_over(tmp_path: Path) -> None:
    board = FakeBoard()
    now = datetime.now(timezone.utc)
    fresh = board.add(status="running", claimedBy={"host": "pc2", "service": "foundry@pc2:me/shop"},
                      heartbeatAt=_iso(now - timedelta(seconds=30)))
    stale = board.add(status="running", claimedBy={"host": "pc2", "service": "foundry@pc2:me/shop"},
                      heartbeatAt=_iso(now - timedelta(minutes=30)))

    async def go() -> None:
        svc = _service(tmp_path)
        await svc.start()
        try:
            w = _watcher(svc, board)
            handled = await w.poll_once()
            assert handled == [stale["id"]]
        finally:
            await svc.stop()

    asyncio.run(go())
    assert board.get(fresh["id"])["claimedBy"]["host"] == "pc2"
    assert board.get(stale["id"])["claimedBy"]["host"] == "pc1"
    assert all(a["id"] != fresh["id"] for _, a in board.ops)


def test_cancelled_batch_stops_submitting(tmp_path: Path) -> None:
    board = FakeBoard()
    b = board.add(subtasks=[
        {"key": "1", "title": "Add clamp helper", "description": "clamp", "files": ["shop/mathx.py"]},
        {"key": "2", "title": "Use clamp", "description": "use it", "dependsOn": ["1"]},
    ])

    async def go() -> None:
        svc = _service(tmp_path)
        await svc.start()
        try:
            w = _watcher(svc, board)
            await w.poll_once()
            board.get(b["id"])["status"] = "cancelled"
            n = len(svc.factory.manager.orders)
            for _ in range(3):
                await w.poll_once()
                await asyncio.sleep(0.05)
            assert len(svc.factory.manager.orders) == n == 1
            assert w._record(b["id"]).get("closed") is True
        finally:
            await svc.stop()

    asyncio.run(go())


def test_board_down_is_survived(tmp_path: Path) -> None:
    board = FakeBoard()
    board.add()
    board.down = True

    async def go() -> None:
        svc = _service(tmp_path)
        await svc.start()
        try:
            w = _watcher(svc, board)
            assert await w.poll_once() == []
            assert "down" in (w.last_error or "")
            board.down = False
            assert await w.poll_once() == ["S-001"]
            assert w.last_error is None
        finally:
            await svc.stop()

    asyncio.run(go())


def test_no_repo_slug_means_no_watching(tmp_path: Path) -> None:
    board = FakeBoard()
    board.add()

    async def go() -> None:
        svc = _service(tmp_path)
        await svc.start()
        try:
            w = BatchWatcher(svc, board, repo_slug="", host="pc1", enabled=True)
            w.repo_slug = None
            assert w.start() is None
            assert await w.poll_once() == []
        finally:
            await svc.stop()

    asyncio.run(go())
    assert board.ops == []


# --------------------------------------------------------------------------- serve wiring


def test_service_runs_the_watcher_with_a_board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOUNDRY_BATCH_REPO", SLUG)
    monkeypatch.setenv("FOUNDRY_BATCH_POLL_S", "1")
    monkeypatch.delenv("FOUNDRY_BATCHES", raising=False)
    board = FakeBoard()
    b = board.add()

    async def go() -> None:
        svc = _service(tmp_path, batch_board=board)
        await svc.start()
        try:
            loop = asyncio.get_running_loop()
            end = loop.time() + 20
            while board.get(b["id"])["status"] != "done":
                assert loop.time() < end
                await asyncio.sleep(0.05)
            health = svc.health()
            assert health["batches"]["repo"] == SLUG and health["batches"]["enabled"] is True
            assert b["id"] in health["batches"]["claimed"]
        finally:
            await svc.stop()
        assert svc.batch_watcher._task is None

    asyncio.run(go())


def test_service_without_board_in_dry_mode_has_no_watcher(tmp_path: Path) -> None:
    async def go() -> None:
        svc = _service(tmp_path)
        await svc.start()
        try:
            assert svc.batch_watcher is None
            assert "batches" not in svc.health()
        finally:
            await svc.stop()

    asyncio.run(go())


# --------------------------------------------------------------------------- HTTP client


class _Handler(BaseHTTPRequestHandler):
    calls: list[dict[str, Any]] = []

    def log_message(self, *a: Any) -> None:
        pass

    def _send(self, code: int, body: Any) -> None:
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        self._send(200, {"tasks": [], "foundryBatches": [{"id": "S-001"}]})

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        type(self).calls.append(body)
        if body["type"] == "updateBatch":
            self._send(409, {"ok": False, "error": "S-001 is done"})
        else:
            self._send(200, {"ok": True, "result": {"ok": True, "batch": {"id": "S-001"}}})


def test_dashboard_board_client() -> None:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        c = DashboardBoard(f"http://127.0.0.1:{srv.server_address[1]}", timeout=5)
        assert c.state()["foundryBatches"][0]["id"] == "S-001"
        assert c.op("claimBatch", {"id": "S-001", "host": "h", "service": "s"}) == {"ok": True, "batch": {"id": "S-001"}}
        with pytest.raises(BoardRefused) as err:
            c.op("updateBatch", {"id": "S-001", "service": "s"})
        assert err.value.code == 409 and "done" in str(err.value)
        assert _Handler.calls[0] == {"type": "claimBatch", "args": {"id": "S-001", "host": "h", "service": "s"}}
    finally:
        srv.shutdown()
        srv.server_close()
    with pytest.raises(BoardUnavailable):
        DashboardBoard(f"http://127.0.0.1:{srv.server_address[1]}", timeout=1).state()


