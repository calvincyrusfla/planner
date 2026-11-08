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
        self.busy = 0  # updateBatch answers 409 "busy" this many times (hub CAS exhaustion)
        self.reject_results = 0  # updateBatch with results answers 400 this many times
        self.lock = threading.Lock()

    def add(self, repo: str = SLUG, subtasks: list[dict[str, Any]] | None = None, **kw: Any) -> dict[str, Any]:
        n = len(self.batches) + 1
        subs = subtasks or [
            {"key": "1", "title": "Add clamp helper", "description": "Add clamp(x, lo, hi).", "files": ["shop/mathx.py"]},
            {"key": "2", "title": "Add lerp helper", "description": "Add lerp(a, b, t).", "files": ["shop/mathx.py"]},
        ]
        b = {"id": f"S-{n:03d}", "createdAt": f"2026-11-08T10:00:{n:02d}.000Z", "title": f"Batch {n}", "goal": "", "repo": repo, "base": None, "status": "queued",
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
                if self.busy:
                    self.busy -= 1
                    raise BoardRefused("busy", 409)
                if self.reject_results and args.get("results"):
                    self.reject_results -= 1
                    raise BoardRefused("pr error is too long", 400)
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
        self.bases: dict[str, str | None] = {}

    async def preflight(self) -> dict[str, Any]:
        return {"ok": True, "reason": None}

    async def publish(self, task_id: str, title: str, patch_path: Any, pr: dict[str, Any], *,
                      base: str | None = None) -> dict[str, Any]:
        self.calls.append(task_id)
        self.bases[task_id] = base
        return {"ok": True, "branch": f"foundry/{task_id.lower()}-x", "commit": "abc123", "base": base or "main",
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


def test_batch_base_is_the_pr_base(tmp_path: Path) -> None:
    board = FakeBoard()
    one = [{"key": "1", "title": "Add clamp helper", "description": "clamp", "files": ["shop/mathx.py"]}]
    with_base = board.add(subtasks=one, base="develop")
    without = board.add(subtasks=[{**one[0], "title": "Add lerp helper"}])
    pub = FakePublisher()

    async def go() -> None:
        svc = _service(tmp_path, publisher=pub)
        await svc.start()
        try:
            w = _watcher(svc, board)
            await _pump(w, lambda: all(board.get(b["id"])["status"] == "done" for b in (with_base, without)))
            t1 = w._record(with_base["id"])["tasks"]["1"]
            t2 = w._record(without["id"])["tasks"]["1"]
            assert svc.factory.manager.orders[t1].pr_base == "develop"
            assert svc.factory.manager.orders[t2].pr_base is None
            assert pub.bases == {t1: "develop", t2: None}  # None: the publisher's own base
            assert svc.factory.manager.pr_state(t1)["base"] == "develop"
        finally:
            await svc.stop()

    asyncio.run(go())


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
            assert marker(b["id"], "1", b["createdAt"]) in order.description
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




# --------------------------------------------------------------------------- review round 1


def test_forward_dependency_is_submitted_once_its_dependency_completes(tmp_path: Path) -> None:
    board = FakeBoard()
    b = board.add(subtasks=[
        {"key": "1", "title": "Use clamp", "description": "use it", "files": ["shop/mathx.py"], "dependsOn": ["2"]},
        {"key": "2", "title": "Add clamp helper", "description": "clamp", "files": ["shop/mathx.py"]},
    ])

    async def go() -> None:
        svc = _service(tmp_path)
        await svc.start()
        try:
            w = _watcher(svc, board)
            await w.poll_once()
            assert set(w._record(b["id"])["tasks"]) == {"2"}
            await _pump(w, lambda: board.get(b["id"])["status"] == "done")
            assert set(w._record(b["id"])["tasks"]) == {"1", "2"}
        finally:
            await svc.stop()

    asyncio.run(go())


def test_failed_forward_dependency_and_cycles_fail_without_submitting(tmp_path: Path) -> None:
    board = FakeBoard()
    b = board.add(subtasks=[
        {"key": "1", "title": "After bad", "description": "y", "dependsOn": ["2"]},
        {"key": "2", "title": "Bad paths", "description": "x", "files": ["../outside.py"]},
        {"key": "3", "title": "Loop a", "description": "a", "dependsOn": ["4"]},
        {"key": "4", "title": "Loop b", "description": "b", "dependsOn": ["3"]},
    ])

    async def go() -> None:
        svc = _service(tmp_path)
        await svc.start()
        try:
            await _watcher(svc, board).poll_once()
            assert not svc.factory.manager.orders
        finally:
            await svc.stop()

    asyncio.run(go())
    got = board.get(b["id"])
    assert got["status"] == "failed"
    assert "dependency 2" in got["results"]["1"]["error"]
    assert "cycle" in got["results"]["3"]["error"] and "cycle" in got["results"]["4"]["error"]


def test_busy_409_is_transient_not_the_end_of_the_batch(tmp_path: Path) -> None:
    board = FakeBoard()
    b = board.add()

    async def go() -> None:
        svc = _service(tmp_path)
        await svc.start()
        try:
            w = _watcher(svc, board)
            board.busy = 2
            await w.poll_once()
            await w.poll_once()
            rec = w._record(b["id"])
            assert not rec.get("closed") and set(rec["tasks"]) == {"1", "2"}
            n = len(svc.factory.manager.orders)
            await _pump(w, lambda: board.get(b["id"])["status"] == "done")
            assert len(svc.factory.manager.orders) == n == 2
        finally:
            await svc.stop()

    asyncio.run(go())


def test_rejected_results_still_send_the_heartbeat(tmp_path: Path) -> None:
    board = FakeBoard()
    b = board.add()

    async def go() -> None:
        svc = _service(tmp_path)
        await svc.start()
        try:
            w = _watcher(svc, board)
            board.reject_results = 1
            await w.poll_once()
            assert not w._record(b["id"]).get("closed")
            assert board.ops[-1][0] == "updateBatch" and "results" not in board.ops[-1][1]
            assert board.ops[-1][1]["heartbeat"] is True
            await _pump(w, lambda: board.get(b["id"])["status"] == "done")  # pushed again later
        finally:
            await svc.stop()

    asyncio.run(go())


def test_pr_fields_are_capped_to_the_board_limits() -> None:
    from foundry.board_dispatch import subtask_state

    class Q:
        def get_task(self, tid: str) -> dict[str, Any]:
            return {"state": "failed", "quarantine_reason": "q" * 5000}

    class M:
        sizing_errors: dict[str, str] = {}

        def pr_state(self, tid: str) -> dict[str, Any]:
            return {"state": "s" * 100, "url": "https://x/" + "u" * 2000, "branch": "b" * 500, "error": "e" * 5000}

    r = subtask_state(M(), Q(), "F-001")
    assert len(r["error"]) == 2000
    assert len(r["pr"]["state"]) == 40 and len(r["pr"]["url"]) == 1000
    assert len(r["pr"]["branch"]) == 300 and len(r["pr"]["error"]) == 2000


def test_record_of_another_batch_with_the_same_id_is_ignored(tmp_path: Path) -> None:
    board = FakeBoard()
    b = board.add()

    async def go() -> None:
        svc = _service(tmp_path)
        await svc.start()
        try:
            w = _watcher(svc, board)
            await w.poll_once()
            old = dict(w._record(b["id"])["tasks"])
            # the board was reset: a new S-001 (other createdAt) arrives
            board.batches = []
            nb = board.add(subtasks=[{"key": "1", "title": "Add lerp helper", "description": "lerp",
                                      "files": ["shop/mathx.py"]}])
            nb["createdAt"] = "2026-11-09T00:00:00.000Z"
            assert nb["id"] == b["id"]
            await w.poll_once()
            rec = w._record(nb["id"], nb["createdAt"])
            assert rec["createdAt"] == nb["createdAt"]
            assert rec["tasks"]["1"] != old["1"]  # submitted fresh, not mistaken for the old order
            assert svc.factory.manager.orders[rec["tasks"]["1"]].title == "Add lerp helper"
        finally:
            await svc.stop()

    asyncio.run(go())


def test_stale_claim_without_heartbeat_uses_the_claim_time(tmp_path: Path) -> None:
    board = FakeBoard()
    old = _iso(datetime.now(timezone.utc) - timedelta(minutes=30))
    b = board.add(status="claimed", claimedBy={"host": "pc2", "service": "foundry@pc2:me/shop", "at": old},
                  heartbeatAt=None)

    async def go() -> None:
        svc = _service(tmp_path)
        await svc.start()
        try:
            w = _watcher(svc, board)
            board_claim = board.op  # the fake board judges staleness by heartbeatAt; mirror the real rule
            def op(type_: str, args: dict[str, Any]) -> Any:
                if type_ == "claimBatch":
                    board.get(b["id"])["heartbeatAt"] = old
                return board_claim(type_, args)
            board.op = op  # type: ignore[method-assign]
            assert await w.poll_once() == [b["id"]]
        finally:
            await svc.stop()

    asyncio.run(go())
    assert board.get(b["id"])["claimedBy"]["host"] == "pc1"


def test_heartbeat_only_updates_are_rate_limited(tmp_path: Path) -> None:
    """Every poll reads the state, but an unchanged batch is written at most every 2 minutes."""
    board = FakeBoard()
    b = board.add(subtasks=[
        {"key": "1", "title": "Add clamp helper", "description": "clamp", "files": ["shop/mathx.py"]},
        {"key": "2", "title": "Use clamp", "description": "use", "dependsOn": ["1"]},
    ])
    clock = {"t": 1000.0}

    async def go() -> None:
        svc = _service(tmp_path)
        await svc.start()
        try:
            # stop the builders: once sized, the order's state stays put, so only heartbeats are due
            for pool in svc.factory.pools.values():
                await pool.stop()
            w = _watcher(svc, board, now=lambda: clock["t"])
            await w.poll_once()
            await _until_sized(svc)
            await w.poll_once()  # pushes the sized state
            sent = board.count("updateBatch")
            for _ in range(5):
                clock["t"] += 15
                await w.poll_once()
            assert board.count("updateBatch") == sent  # 75 s: nothing new, no write
            clock["t"] += 60
            await w.poll_once()
            assert board.count("updateBatch") == sent + 1  # > 2 min: one heartbeat
            assert board.ops[-1][1] == {"id": b["id"], "service": "foundry@pc1:me/shop", "heartbeat": True}
        finally:
            await svc.stop()

    asyncio.run(go())


async def _until_sized(svc: Service, timeout: float = 15.0) -> None:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    m = svc.factory.manager
    while not m.orders or any(m.is_sizing(t) for t in m.orders):
        assert loop.time() < end, "sizing did not finish"
        await asyncio.sleep(0.02)


def test_option_like_batch_base_fails_the_subtask_without_submitting(tmp_path: Path) -> None:
    board = FakeBoard()
    b = board.add(base="--upload-pack=touch X;git-upload-pack")

    async def go() -> None:
        svc = _service(tmp_path, publisher=FakePublisher())
        await svc.start()
        try:
            await _watcher(svc, board).poll_once()
            assert not svc.factory.manager.orders
        finally:
            await svc.stop()

    asyncio.run(go())
    got = board.get(b["id"])
    assert got["status"] == "failed"
    assert all("not a plain branch name" in got["results"][k]["error"] for k in ("1", "2"))
