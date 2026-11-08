"""T-019 (alpha-2): automatic branch + PR integration.

FoundryConfig auto_pr/pr_base/pr_draft, the Floor Manager's background publish
after the patch is written (persisted ``pr`` state, restart rules, retry), the
board note, the service's status ``pr`` field, ``/api/health`` ``pr_ready`` and
``POST /api/tasks/<id>/pr/retry``. A fake publisher only: no git, no gh, no
network; dry board.
"""

from __future__ import annotations

import asyncio
import http.client
import json
from pathlib import Path
from typing import Any

import pytest

from foundry.board_bridge import BoardBridge
from foundry.config import FoundryConfig
from foundry.floor_manager import WorkOrder, build_factory
from foundry.main import append_diff, demo_claude, demo_gemma, write_demo_repo
from foundry.serve import Service
from foundry.serve_api import FoundryAPI

ORDER = {"title": "Add clamp helper", "description": "Add clamp(x, lo, hi).", "files": ["shop/mathx.py"]}


class FakePublisher:
    """Records publish calls; answers with the queued results (default: success)."""

    def __init__(self, *results: dict[str, Any], preflight: dict[str, Any] | None = None, delay: float = 0.0) -> None:
        self.results = list(results)
        self.calls: list[dict[str, Any]] = []
        self.delay = delay
        self._preflight = preflight or {"ok": True, "reason": None, "git": True, "remote": True, "gh": True,
                                        "gh_auth": True}

    async def preflight(self) -> dict[str, Any]:
        return dict(self._preflight)

    async def publish(self, task_id: str, title: str, patch_path: Any, pr: dict[str, Any]) -> dict[str, Any]:
        self.calls.append({"task_id": task_id, "title": title, "patch": Path(patch_path),
                           "patch_text": Path(patch_path).read_text(encoding="utf-8"), "pr": dict(pr)})
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.results:
            return self.results.pop(0)
        n = len(self.calls)
        return {"ok": True, "branch": f"foundry/{task_id.lower()}-x", "commit": "abc123",
                "pr_url": f"https://github.com/me/repo/pull/{n}", "error": None, "stage": None}


def _config(tmp_path: Path, **kw: Any) -> FoundryConfig:
    return FoundryConfig(db_path=tmp_path / "foundry.db", rpm=1000, tpm=10_000_000, max_concurrent=5,
                         teams=["alpha"], board_bridge_mode="dry", **kw)


def _factory(tmp_path: Path, repo: Path, publisher: Any, **kw: Any) -> Any:
    return build_factory(_config(tmp_path, **kw), claude=demo_claude(repo, fail_first=None), gemma=demo_gemma(),
                         repo_root=repo, workspace=repo, bridge=BoardBridge("dry"),
                         patches_dir=tmp_path / "patches", publisher=publisher)


def _seed(m: Any, tid: str, repo: Path) -> None:
    m.diffs[tid] = {f"{tid}.1": append_diff("shop/mathx.py", (repo / "shop/mathx.py").read_text(), ["X = 1"])}


async def _complete(f: Any, repo: Path) -> str:
    tid = await f.manager.submit(WorkOrder(**ORDER))
    _seed(f.manager, tid, repo)
    await f.manager.on_task_complete(tid)
    await f.manager.drain()
    return tid


def _notes(f: Any) -> list[str]:
    return [str(op["args"]) for op in f.bridge.ops if "PR opened" in str(op["args"])]


# --------------------------------------------------------------------------- config


def test_config_defaults_and_env(clean_foundry_env: Any) -> None:
    c = FoundryConfig()
    assert c.auto_pr is True and c.pr_base is None and c.pr_draft is False
    c = FoundryConfig.from_env({"FOUNDRY_AUTO_PR": "off", "FOUNDRY_PR_BASE": " develop ", "FOUNDRY_PR_DRAFT": "yes"})
    assert c.auto_pr is False and c.pr_base == "develop" and c.pr_draft is True
    assert FoundryConfig(pr_base="  ").pr_base is None
    with pytest.raises(ValueError):
        FoundryConfig.from_env({"FOUNDRY_AUTO_PR": "maybe"})


# --------------------------------------------------------------------------- manager


def test_approved_task_is_published_in_the_background_with_gemmas_text(tmp_path: Path) -> None:
    repo = write_demo_repo(tmp_path / "repo")
    pub = FakePublisher()
    f = _factory(tmp_path, repo, pub, pr_draft=True)
    tid = asyncio.run(_complete(f, repo))
    m = f.manager
    assert len(pub.calls) == 1
    call = pub.calls[0]
    summary = m.pr_summaries[tid]
    assert call["task_id"] == tid and call["title"] == ORDER["title"]
    assert call["patch"] == tmp_path / "patches" / f"{tid}.patch" and "X = 1" in call["patch_text"]
    assert call["pr"]["title"] == summary["commit_message"].strip().splitlines()[0]
    assert summary["pr_body"].strip()[:40] in call["pr"]["body"]
    assert call["pr"]["body"].rstrip().endswith(f"Opened by Foundry (task {tid})")
    assert call["pr"]["draft"] is True
    pr = m.pr_state(tid)
    assert pr["state"] == "opened" and pr["url"] == "https://github.com/me/repo/pull/1"
    assert pr["branch"].startswith("foundry/")
    assert len(_notes(f)) == 1 and "pull/1" in _notes(f)[0]
    # idempotent: completing again never opens a second PR
    asyncio.run(m.on_task_complete(tid))
    assert len(pub.calls) == 1
    f.close()

    # restart: the opened PR is remembered and not published again
    g = _factory(tmp_path, repo, FakePublisher())
    assert g.manager.pr_state(tid)["state"] == "opened"
    assert g.manager.resume_publishing() == []
    assert g.manager.publisher.calls == []
    g.close()


def test_no_publisher_or_auto_pr_off_publishes_nothing(tmp_path: Path) -> None:
    repo = write_demo_repo(tmp_path / "repo")
    f = _factory(tmp_path / "a", repo, None)
    tid = asyncio.run(_complete(f, repo))
    assert f.manager.pr_state(tid) is None
    f.close()
    pub = FakePublisher()
    g = _factory(tmp_path / "b", repo, pub, auto_pr=False)
    tid = asyncio.run(_complete(g, repo))
    assert pub.calls == [] and g.manager.pr_state(tid) is None
    g.close()


def test_failed_publish_is_kept_not_retried_on_restart_and_retry_opens_it(tmp_path: Path) -> None:
    repo = write_demo_repo(tmp_path / "repo")
    pub = FakePublisher({"ok": False, "error": "gh: not logged in", "stage": "gh_not_logged_in"})
    f = _factory(tmp_path, repo, pub)
    tid = asyncio.run(_complete(f, repo))
    pr = f.manager.pr_state(tid)
    assert pr["state"] == "failed" and pr["stage"] == "gh_not_logged_in" and "not logged in" in pr["error"]
    assert _notes(f) == []
    f.close()

    pub2 = FakePublisher()
    g = _factory(tmp_path, repo, pub2)

    async def go() -> None:
        assert g.manager.resume_publishing() == []  # failed: never retried on its own
        assert pub2.calls == []
        out = await g.manager.retry_pr(tid)
        assert out["ok"] is True and out["pr"]["state"] == "pending"
        await g.manager.drain()
        assert g.manager.pr_state(tid)["state"] == "opened"
        again = await g.manager.retry_pr(tid)
        assert again["ok"] is False and "already open" in again["reason"]
        assert (await g.manager.retry_pr("F-999"))["ok"] is False

    asyncio.run(go())
    assert len(pub2.calls) == 1
    g.close()


def test_publish_cut_off_is_retried_once_on_restart(tmp_path: Path) -> None:
    repo = write_demo_repo(tmp_path / "repo")
    pub = FakePublisher(delay=30)
    f = _factory(tmp_path, repo, pub)

    async def first() -> str:
        tid = await f.manager.submit(WorkOrder(**ORDER))
        _seed(f.manager, tid, repo)
        await f.manager.on_task_complete(tid)
        await asyncio.sleep(0.05)
        assert f.manager.is_publishing(tid) and f.manager.pr_state(tid)["state"] == "pending"
        f.manager._pr_tasks[tid].cancel()  # the service died mid-push
        await asyncio.gather(f.manager._pr_tasks[tid], return_exceptions=True)
        return tid

    tid = asyncio.run(first())
    f.close()

    g = _factory(tmp_path, repo, FakePublisher())

    async def second() -> None:
        assert g.manager.pr_state(tid)["state"] == "pending"
        assert g.manager.resume_publishing() == [tid]
        await g.manager.drain()

    asyncio.run(second())
    assert g.manager.pr_state(tid)["state"] == "opened" and len(g.manager.publisher.calls) == 1
    g.close()

    # pending after two attempts: marked failed instead of trying forever
    h = _factory(tmp_path, repo, FakePublisher())
    h.manager.pr_states[tid] = {"state": "pending", "attempts": 2}
    assert h.manager.resume_publishing() == []
    assert h.manager.pr_state(tid)["state"] == "failed" and h.manager.pr_state(tid)["stage"] == "interrupted"
    h.close()


def test_rejected_patch_is_skipped(tmp_path: Path) -> None:
    repo = write_demo_repo(tmp_path / "repo")
    pub = FakePublisher()
    f = _factory(tmp_path, repo, pub)

    async def go() -> str:
        tid = await f.manager.submit(WorkOrder(**ORDER))
        f.manager.diffs[tid] = {f"{tid}.1": "--- a/nope.py\n+++ b/nope.py\n@@ -1,1 +1,1 @@\n-old\n+new\n"}
        await f.manager.on_task_complete(tid)
        await f.manager.drain()
        return tid

    tid = asyncio.run(go())
    pr = f.manager.pr_state(tid)
    if (tmp_path / "patches" / f"{tid}.patch.rejected").is_file():  # git installed: the patch was rejected
        assert pr["state"] == "skipped" and pub.calls == []
    f.close()


# --------------------------------------------------------------------------- service + HTTP


def _service(tmp_path: Path, pub: Any, **cfg: Any) -> Service:
    repo = write_demo_repo(tmp_path / "repo")
    kwargs: dict[str, Any] = {"bridge": BoardBridge("dry"), "patches_dir": tmp_path / "patches"}
    if pub is not None:
        kwargs["publisher"] = pub
    return Service(_config(tmp_path, **cfg), repo=repo, claude=demo_claude(repo, fail_first=None),
                   gemma=demo_gemma(), monitor_s=3600, factory_kwargs=kwargs)


async def _until(pred: Any, timeout: float = 15.0) -> None:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not pred():
        assert loop.time() < end, "condition not reached in time"
        await asyncio.sleep(0.01)


def _row(svc: Service, tid: str) -> dict[str, Any]:
    return next(t for t in svc.status()["tasks"] if t["id"] == tid)


def test_service_status_health_and_retry(tmp_path: Path) -> None:
    pub = FakePublisher({"ok": False, "error": "push rejected", "stage": "push_failed"},
                        preflight={"ok": False, "reason": "the GitHub CLI is not logged in (run: gh auth login)"})

    async def go() -> None:
        svc = _service(tmp_path, pub)
        await svc.start()
        try:
            await _until(lambda: svc.pr_preflight is not None)
            h = svc.health()
            assert h["pr_ready"] is False and "gh auth login" in h["pr_reason"]
            tid = await svc.submit(**ORDER)
            await _until(lambda: (_row(svc, tid).get("pr") or {}).get("state") == "failed")
            row = _row(svc, tid)
            assert row["pr"] == {"state": "failed", "url": None, "branch": None, "error": "push rejected",
                                 "stage": "push_failed"}
            out = await svc.retry_pr(tid)
            assert out["ok"] is True
            await _until(lambda: _row(svc, tid)["pr"]["state"] == "opened")
            assert _row(svc, tid)["pr"]["url"].endswith("/pull/2")
            assert (await svc.retry_pr("F-999"))["code"] == "not_found"
        finally:
            await svc.stop()

    asyncio.run(go())


def test_health_pr_ready_when_off_or_ready(tmp_path: Path) -> None:
    async def go(sub: str, pub: Any, **cfg: Any) -> dict[str, Any]:
        svc = _service(tmp_path / sub, pub, **cfg)
        await svc.start()
        try:
            await _until(lambda: svc.pr_preflight is not None)
            return svc.health()
        finally:
            await svc.stop()

    ready = asyncio.run(go("a", FakePublisher()))
    assert ready["pr_ready"] is True and ready["pr_reason"] is None
    off = asyncio.run(go("b", None, auto_pr=False))
    assert off["pr_ready"] is False and "FOUNDRY_AUTO_PR" in off["pr_reason"]


def _post(port: int, path: str) -> tuple[int, dict[str, Any]]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    conn.request("POST", path, body=b"{}", headers={"Content-Type": "application/json"})
    resp = conn.getresponse()
    data = json.loads(resp.read().decode("utf-8"))
    conn.close()
    return resp.status, data


def test_http_pr_retry_route(tmp_path: Path) -> None:
    pub = FakePublisher({"ok": False, "error": "gh missing", "stage": "gh_missing"})

    async def go() -> None:
        svc = _service(tmp_path, pub)
        await svc.start()
        api = FoundryAPI(svc, port=0).start()
        try:
            tid = await svc.submit(**ORDER)
            await _until(lambda: (_row(svc, tid).get("pr") or {}).get("state") == "failed")
            code, body = await asyncio.to_thread(_post, api.port, f"/api/tasks/{tid}/pr/retry")
            assert code == 200 and body["ok"] is True and body["task_id"] == tid
            await _until(lambda: _row(svc, tid)["pr"]["state"] == "opened")
            code, body = await asyncio.to_thread(_post, api.port, f"/api/tasks/{tid}/pr/retry")
            assert code == 409 and body["ok"] is False and "already open" in body["reason"]
            code, body = await asyncio.to_thread(_post, api.port, "/api/tasks/F-404/pr/retry")
            assert code == 404 and body["code"] == "not_found"
        finally:
            api.stop()
            await svc.stop()

    asyncio.run(go())
