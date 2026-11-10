"""Tests for ``GET /api/ledger``: real HTTP on a free port over a real queue."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from foundry.ledger import Ledger
from foundry.orchestrator_queue import FoundryQueue
from foundry.serve_api import FoundryAPI
from foundry.tests.test_serve_api import FakeService, request

CREATED = "2026-11-10T10:00:00Z"
LATER = "2026-11-10T11:00:00Z"


@pytest.fixture()
def queue(tmp_path):
    db = FoundryQueue(tmp_path / "foundry.db")
    yield db
    db.close()


@pytest.fixture()
def ledger(queue):
    return Ledger(queue)


@pytest.fixture()
def server(queue):
    service = FakeService()
    service.factory = SimpleNamespace(queue=queue)
    with FoundryAPI(service, port=0) as running:
        yield running


def get(server, path):
    resp, text = request(server, "GET", path)
    return resp.status, json.loads(text)


def add_batch(ledger, batch_id, status, created=CREATED, title="A batch"):
    ledger.upsert_batch({"id": batch_id, "createdAt": created, "status": status, "title": title})


def test_open_batches_listed_with_pieces(server, ledger):
    add_batch(ledger, "S-001", "running")
    add_batch(ledger, "S-002", "queued", created=LATER)
    ledger.upsert_piece("S-001", CREATED, "1", title="First", state="building", files=["a.py"])
    ledger.upsert_piece("S-001", CREATED, "2", title="Second", state="queued")

    status, body = get(server, "/api/ledger")

    assert status == 200 and body["ok"] is True
    assert [b["batch_id"] for b in body["batches"]] == ["S-001", "S-002"]
    first, second = body["batches"]
    assert [p["key"] for p in first["pieces"]] == ["1", "2"]
    assert first["pieces"][0]["files"] == ["a.py"]
    assert second["pieces"] == []


def test_finished_batches_not_in_default_list(server, ledger):
    add_batch(ledger, "S-001", "done")
    add_batch(ledger, "S-002", "claimed")

    _, body = get(server, "/api/ledger")

    assert [b["batch_id"] for b in body["batches"]] == ["S-002"]


def test_batch_parameter_returns_that_batch(server, ledger):
    add_batch(ledger, "S-001", "done", title="Finished")
    add_batch(ledger, "S-002", "running")
    ledger.upsert_piece("S-001", CREATED, "1", state="complete")

    status, body = get(server, "/api/ledger?batch=S-001")

    assert status == 200 and body["ok"] is True
    assert [b["title"] for b in body["batches"]] == ["Finished"]
    assert [p["state"] for p in body["batches"][0]["pieces"]] == ["complete"]


def test_created_parameter_picks_the_reused_id(server, ledger):
    add_batch(ledger, "S-001", "done", title="Older")
    add_batch(ledger, "S-001", "running", created=LATER, title="Newer")
    ledger.upsert_piece("S-001", CREATED, "old", state="complete")
    ledger.upsert_piece("S-001", LATER, "new", state="building")

    _, newest = get(server, "/api/ledger?batch=S-001")
    _, older = get(server, f"/api/ledger?batch=S-001&created={CREATED}")

    assert [(b["title"], [p["key"] for p in b["pieces"]]) for b in newest["batches"]] == [("Newer", ["new"])]
    assert [(b["title"], [p["key"] for p in b["pieces"]]) for b in older["batches"]] == [("Older", ["old"])]


def test_blank_created_finds_a_batch_stored_without_a_date(server, ledger):
    ledger.upsert_batch({"id": "S-001", "status": "running", "title": "Undated"})
    ledger.upsert_piece("S-001", None, "1", state="building")

    _, body = get(server, "/api/ledger?batch=S-001&created=")

    assert [b["title"] for b in body["batches"]] == ["Undated"]
    assert [p["key"] for p in body["batches"][0]["pieces"]] == ["1"]


@pytest.mark.parametrize("query", ["batch=S-404", f"batch=S-001&created={LATER}"])
def test_unknown_batch_is_empty(server, ledger, query):
    add_batch(ledger, "S-001", "running")

    status, body = get(server, f"/api/ledger?{query}")

    assert status == 200 and body == {"ok": True, "batches": []}


@pytest.mark.parametrize("bad", ["", "F-001", "S-", "S-1a", "s-001", "S-001%0A", "S-%D9%A1", "x%20S-001"])
def test_bad_batch_id_is_400(server, bad):
    status, body = get(server, f"/api/ledger?batch={bad}")

    assert status == 400
    assert body["ok"] is False and body["code"] == "invalid" and body["error"]


@pytest.mark.parametrize("path", ["/api/ledger", "/api/ledger?batch=S-001"])
def test_no_factory_is_empty(path):
    with FoundryAPI(FakeService(), port=0) as running:
        status, body = get(running, path)

    assert status == 200 and body == {"ok": True, "batches": []}


def test_bad_batch_id_is_400_without_a_factory():
    with FoundryAPI(FakeService(), port=0) as running:
        status, body = get(running, "/api/ledger?batch=nope")

    assert status == 400 and body["code"] == "invalid"


def test_only_get_is_allowed(server):
    resp, _ = request(server, "POST", "/api/ledger", {})

    assert resp.status == 405
