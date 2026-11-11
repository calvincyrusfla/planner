"""Tests for the Foreman actions routes (GET /api/actions, POST /api/actions/<id>/undo).

Real HTTP on a free loopback port with a simulated service, plus the
``Service.actions`` / ``Service.undo_action`` wrappers over a fake manager.
No Claude, Ollama, board or GitHub.
"""

from __future__ import annotations

import asyncio
import http.client
import json
from enum import Enum
from types import SimpleNamespace

import pytest

from foundry.serve import Service
from foundry.serve_api import FoundryAPI

UNDO_GONE = "undo is gone: the Foreman only logs and never acts"


class FakeService:
    def __init__(self) -> None:
        self.repo = "/tmp/repo"
        self.loop = None
        self.undone: list[tuple[str, str]] = []
        self.limits: list[int] = []
        self.rows = [
            {"action_id": "A-2", "at": 200.0, "action": "REASSIGN_TEAM", "task_id": "F-012", "team": "bravo",
             "detail": "stuck 14 min", "undoable": True, "undone_at": None},
            {"action_id": "A-1", "at": 100.0, "action": "RESET_BUILDER_CONTEXT", "task_id": "F-011",
             "undoable": False, "undone_at": None},
        ]

    def health(self):
        return {}

    def status(self):
        return {"tasks": []}

    def actions(self, limit=50):
        self.limits.append(limit)
        return self.rows[:limit]

    async def undo_action(self, action_id, by="user"):
        self.undone.append((action_id, by))
        return {"ok": False, "reason": UNDO_GONE}


@pytest.fixture()
def svc():
    return FakeService()


@pytest.fixture()
def api(svc):
    server = FoundryAPI(svc, port=0).start()
    yield server
    server.stop()


def request(api, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", api.port, timeout=10)
    hdrs = {"Host": f"127.0.0.1:{api.port}"}
    hdrs.update(headers or {})
    data = json.dumps(body).encode() if body is not None else None
    if data is not None:
        hdrs["Content-Type"] = "application/json"
    conn.request(method, path, body=data, headers=hdrs)
    resp = conn.getresponse()
    text = resp.read().decode()
    conn.close()
    return resp, (json.loads(text) if text else None)


def test_get_actions(api, svc):
    resp, out = request(api, "GET", "/api/actions")
    assert resp.status == 200
    assert out["ok"] is True
    assert [a["action_id"] for a in out["actions"]] == ["A-2", "A-1"]
    assert svc.limits[-1] == 50
    resp, out = request(api, "GET", "/api/actions?limit=1")
    assert resp.status == 200 and len(out["actions"]) == 1 and svc.limits[-1] == 1
    resp, _ = request(api, "GET", "/api/actions?limit=9999")
    assert resp.status == 200 and svc.limits[-1] == 200
    resp, out = request(api, "GET", "/api/actions?limit=x")
    assert resp.status == 400 and out["code"] == "invalid"


def test_undo_is_refused(api, svc):
    resp, out = request(api, "POST", "/api/actions/A-2/undo", body={})
    assert resp.status == 409
    assert out == {"ok": False, "reason": UNDO_GONE, "error": UNDO_GONE, "action_id": "A-2", "code": "refused"}
    assert svc.undone[-1] == ("A-2", "user")


def test_undo_routes_refuse_bad_ids_methods_and_foreign_callers(api, svc):
    assert request(api, "GET", "/api/actions/A-2/undo")[0].status == 405
    assert request(api, "POST", "/api/actions")[0].status == 405
    assert request(api, "POST", "/api/actions/a%20b/undo")[0].status == 404
    assert request(api, "POST", "/api/actions/../undo")[0].status == 404
    assert request(api, "POST", "/api/actions/A-2/undo/x")[0].status == 404
    resp, out = request(api, "POST", "/api/actions/A-2/undo", body={}, headers={"Origin": "http://evil.example"})
    assert resp.status == 403
    assert request(api, "GET", "/api/actions", headers={"Host": "evil.example"})[0].status == 403
    assert svc.undone == []


def test_service_without_action_log():
    class Bare:
        loop = None

        def status(self):
            return {}

    server = FoundryAPI(Bare(), port=0).start()
    try:
        resp, out = request(server, "GET", "/api/actions")
        assert resp.status == 200 and out == {"ok": True, "actions": []}
        assert request(server, "POST", "/api/actions/A-1/undo")[0].status == 404
    finally:
        server.stop()


# -- Service wrappers over the manager ------------------------------------------------


class Kind(str, Enum):
    REASSIGN_TEAM = "REASSIGN_TEAM"


class FakeManager:
    def __init__(self, repos):
        self.repos = repos
        self.calls = []

    def task_repo(self, tid):
        return self.repos.get(tid)

    def actions(self, limit=50):
        rows = [
            {"action_id": "A-1", "at": "2026-11-08T10:00:00Z", "action": Kind.REASSIGN_TEAM, "task_id": "F-1"},
            {"action_id": "A-3", "at": "2026-11-08T12:00:00Z", "action": "THROTTLE_DISPATCH", "task_id": None},
            {"action_id": "A-2", "at": "2026-11-08T11:00:00Z", "action": "QUARANTINE_TASK", "task_id": "F-2"},
            {"action_id": "A-9", "at": "2026-11-08T13:00:00Z", "action": "QUARANTINE_TASK", "task_id": "F-9"},
        ]
        return rows[:limit]

    def undo_action(self, action_id, by="user"):
        self.calls.append((action_id, by))
        return {"ok": False, "reason": UNDO_GONE}


def _service(tmp_path, manager):
    svc = Service(SimpleNamespace(), repo=tmp_path, factory=SimpleNamespace(manager=manager))
    return svc


def test_service_actions_newest_first_jsonable_and_this_repo_only(tmp_path):
    other = tmp_path.parent / "elsewhere"
    m = FakeManager({"F-1": str(tmp_path), "F-2": None, "F-9": str(other)})
    svc = _service(tmp_path, m)
    rows = svc.actions()
    assert [r["action_id"] for r in rows] == ["A-3", "A-2", "A-1"]  # F-9 is another repo's task
    assert rows[-1]["action"] == "REASSIGN_TEAM"
    json.dumps(rows)
    assert len(svc.actions(limit=1)) == 1


def test_service_undo_action(tmp_path):
    m = FakeManager({})
    svc = _service(tmp_path, m)
    assert asyncio.run(svc.undo_action("A-1")) == {"ok": False, "reason": UNDO_GONE, "action_id": "A-1"}
    out = asyncio.run(svc.undo_action("A-2", by="dashboard"))
    assert out["ok"] is False and out["reason"] == UNDO_GONE
    assert m.calls == [("A-1", "user"), ("A-2", "dashboard")]


def test_service_without_manager_log(tmp_path):
    svc = _service(tmp_path, SimpleNamespace())
    assert svc.actions() == []
    out = asyncio.run(svc.undo_action("A-1"))
    assert out["ok"] is False and out["code"] == "unsupported"


def test_service_actions_filters_before_limit(tmp_path):
    other = tmp_path.parent / "elsewhere"
    m = FakeManager({"F-1": str(tmp_path), "F-2": None, "F-9": str(other)})
    seen = []
    orig = m.actions
    m.actions = lambda limit=50: (seen.append(limit), orig(limit))[1]
    svc = _service(tmp_path, m)
    # the newest entry (A-9) is another repo's: limit=1 must still return this repo's newest
    assert [r["action_id"] for r in svc.actions(limit=1)] == ["A-3"]
    assert seen[-1] > 1


def test_service_undo_refuses_another_repos_action(tmp_path):
    other = tmp_path.parent / "elsewhere"
    m = FakeManager({"F-1": str(tmp_path), "F-9": str(other)})
    svc = _service(tmp_path, m)
    out = asyncio.run(svc.undo_action("A-9"))
    assert out == {"ok": False, "action_id": "A-9", "reason": "unknown action", "code": "not_found"}
    assert m.calls == []
    assert asyncio.run(svc.undo_action("A-1"))["reason"] == UNDO_GONE
    assert m.calls == [("A-1", "user")]


def test_undo_not_found_and_not_running(api, svc):
    async def foreign(action_id, by="user"):
        return {"ok": False, "reason": "unknown action", "code": "not_found"}

    svc.undo_action = foreign
    resp, out = request(api, "POST", "/api/actions/A-9/undo")
    assert resp.status == 404 and out["code"] == "not_found"
    svc.running = False
    resp, out = request(api, "POST", "/api/actions/A-2/undo")
    assert resp.status == 503 and out["code"] == "not_running"
