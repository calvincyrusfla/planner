"""End-to-end checks for T-018 (Gemma group 2a: pre-review + sizing that learns).

* **Pre-review over the real service.** ``foundry.serve`` runs on a free
  loopback port with a simulated Claude (builders write a clean diff,
  reviewers approve) and a simulated Gemma whose pre-review flags *every*
  diff as blocking. The first submission goes back to the builder with a
  ``[pre-review]`` hint, the builder resubmits, and the second submission goes
  on to the Claude reviewer (once-per-ticket cap): the reviewer is called once,
  not twice, and its prompt carries Gemma's notes. ``GET /api/status`` shows
  ``foreman.pre_review`` with one send-back / one Claude review avoided.
* **Outcomes are recorded.** After a few completed tasks, ``sizing_outcomes``
  rows exist (one per task) and ``foreman.calibration_summary()`` reports them.
* **Calibration.** A seeded history where M tasks always turned out L makes a
  new order that Gemma sizes M come out L, with "calibrated" in the rationale,
  and the sizing prompt carries "Past task ..." examples.

Safety: simulated Claude and Gemma only, dry board bridge, DEPT_HUB=off via
the ``clean_foundry_env`` fixture plus a scratch DEPT_DATA.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

ORDER = {
    "title": "Add clamp helper",
    "description": "Add clamp(x, lo, hi) to the math helpers.",
    "files": ["shop/mathx.py"],
}
PRICING_FILES = ["shop/pricing.py", "shop/orders.py"]
BLOCKING_NOTE = "placeholder left where the clamp logic should be"
_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


@pytest.fixture(autouse=True)
def _scratch_dept(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEPT_HUB", "off")
    monkeypatch.setenv("DEPT_DATA", str(tmp_path / "dept-data"))


# --------------------------------------------------------------------------- simulated Gemma


def _is_pre_review(req: Any) -> bool:
    schema = req.json_schema or {}
    props = schema.get("properties") if isinstance(schema, dict) else None
    if isinstance(props, dict) and "verdict" in props and "confidence" in props:
        return True
    system = str(req.messages[0].get("content", "")) if req.messages else ""
    return "pre-review" in system.lower()


def _is_sizing(req: Any) -> bool:
    system = str(req.messages[0].get("content", "")) if req.messages else ""
    return "Size the work order" in system


class SimGemma:
    """demo_gemma plus: a pre-review that always flags the diff, a fixed sizing size."""

    def __init__(self, size: str = "S") -> None:
        from foundry.gemma_client import SimulatedGemmaClient
        from foundry.main import demo_gemma

        self.size = size
        self.pre_reviews = 0
        self.sizing_prompts: list[str] = []
        self._base = demo_gemma()
        self.client = SimulatedGemmaClient(self._respond)

    def _respond(self, req: Any) -> Any:
        if _is_pre_review(req):
            self.pre_reviews += 1
            return {
                "verdict": "send_back",
                "issues": [{"severity": "blocking", "note": BLOCKING_NOTE}],
                "confidence": 0.95,
            }
        if _is_sizing(req):
            user = str(req.messages[-1].get("content", ""))
            self.sizing_prompts.append(user)
            files = re.search(r"Files: (.*)", user)
            listed = [f.strip() for f in (files.group(1) if files else "").split(",") if f.strip().startswith("shop/")]
            return {
                "complexity": self.size,
                "rationale": f"Gemma thinks this is {self.size}.",
                "subtasks": [
                    {"title": f"Part {k + 1}", "acceptance_criteria": ["works"], "allowed_files": [f]}
                    for k, f in enumerate(listed or ["shop/mathx.py"])
                ],
            }
        return self._base.responder(req)


# --------------------------------------------------------------------------- helpers


def _http(method: str, url: str, body: Any = None, timeout: float = 15.0) -> dict[str, Any]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with _NO_PROXY.open(req, timeout=timeout) as resp:
            code, text = resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        code, text = exc.code, exc.read().decode("utf-8")
    assert code == 200, (code, text)
    return json.loads(text)


@contextmanager
def _service(tmp_path: Path, claude: Any, gemma: Any, repo: Path) -> Iterator[tuple[Any, str]]:
    from foundry import serve
    from foundry.board_bridge import BoardBridge
    from foundry.config import FoundryConfig

    config = FoundryConfig(
        db_path=tmp_path / "foundry.db", patches_dir=tmp_path / "patches", rpm=600, tpm=10_000_000,
        max_concurrent=3, board_bridge_mode="dry", pre_review="gate", pre_review_min_confidence=0.7,
    )
    service = serve.Service(
        config, repo=repo, claude=claude, gemma=gemma, monitor_s=3600,
        factory_kwargs={"bridge": BoardBridge("dry"), "patches_dir": tmp_path / "patches"},
    )
    lines: list[str] = []
    holder: dict[str, Any] = {}
    ready = threading.Event()

    def out(line: str = "") -> None:
        lines.append(str(line))
        if "running for" in str(line):
            ready.set()

    async def main() -> None:
        holder["loop"] = asyncio.get_running_loop()
        holder["stop"] = asyncio.Event()
        try:
            await serve.run_service(service, port=0, stop_event=holder["stop"], out=out)
        except BaseException as exc:
            holder["error"] = exc
            ready.set()
            raise

    thread = threading.Thread(target=lambda: asyncio.run(main()), name="foundry-g2a-e2e", daemon=True)
    thread.start()
    try:
        assert ready.wait(20), f"service did not start: {lines}"
        assert "error" not in holder, holder.get("error")
        m = re.search(r"http://127\.0\.0\.1:(\d+)", " ".join(lines))
        assert m, lines
        yield service, f"http://127.0.0.1:{m.group(1)}"
    finally:
        loop = holder.get("loop")
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(holder["stop"].set)
        thread.join(30)
        assert not thread.is_alive(), "service did not stop"


def _wait_state(base: str, tid: str, timeout: float = 30.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    row: dict[str, Any] = {}
    while time.monotonic() < deadline:
        status = _http("GET", f"{base}/api/status")
        row = next((t for t in status["tasks"] if t["id"] == tid), {})
        if row.get("state") in ("complete", "quarantined", "failed"):
            return row
        time.sleep(0.1)
    raise AssertionError(f"{tid} did not finish in {timeout}s: {row}")


def _factory(tmp_path: Path, gemma: Any, claude: Any = None) -> Any:
    from foundry.board_bridge import BoardBridge
    from foundry.config import FoundryConfig
    from foundry.floor_manager import build_factory
    from foundry.main import demo_claude, write_demo_repo

    repo = write_demo_repo(tmp_path / "repo")
    config = FoundryConfig(
        db_path=tmp_path / "foundry.db", rpm=600, tpm=10_000_000, max_concurrent=3,
        board_bridge_mode="dry", pre_review="off",
    )
    return build_factory(
        config, claude=claude or demo_claude(repo, fail_first=None), gemma=gemma, repo_root=repo,
        workspace=repo, bridge=BoardBridge("dry"), patches_dir=tmp_path / "patches",
    )


# --------------------------------------------------------------------------- tests


def test_pre_review_sends_back_once_then_claude_reviews_once(
    tmp_path: Path, clean_foundry_env: pytest.MonkeyPatch
) -> None:
    from foundry.main import demo_claude, write_demo_repo

    repo = write_demo_repo(tmp_path / "repo")
    claude = demo_claude(repo, fail_first=None)
    gemma = SimGemma(size="S")
    with _service(tmp_path, claude, gemma.client, repo) as (service, base):
        tid = _http("POST", f"{base}/api/orders", ORDER)["task_id"]
        row = _wait_state(base, tid)
        assert row["state"] == "complete", row

        builders = [c for c in claude.history if c.role == "builder"]
        reviewers = [c for c in claude.history if c.role == "reviewer"]
        assert len(builders) == 2, [(c.agent_id, c.role) for c in claude.history]
        assert len(reviewers) == 1, [(c.agent_id, c.role) for c in claude.history]  # once, not twice
        assert "[pre-review]" in builders[1].prompt, builders[1].prompt[-2000:]
        assert BLOCKING_NOTE in builders[1].prompt
        assert "Gemma pre-review notes" in reviewers[0].prompt, reviewers[0].prompt[-2000:]
        assert BLOCKING_NOTE in reviewers[0].prompt
        assert gemma.pre_reviews == 2  # flagged both times; the cap let the second through

        status = _http("GET", f"{base}/api/status")
        pre = status["foreman"].get("pre_review")
        assert isinstance(pre, dict) and pre, status["foreman"]
        assert pre["sent_back"] == 1, pre
        assert pre["claude_reviews_avoided"] == 1, pre
        assert service.factory.foreman.last_source.get("pre_review_diff") == "gemma"


def test_completed_tasks_record_sizing_outcomes(tmp_path: Path, clean_foundry_env: pytest.MonkeyPatch) -> None:
    from foundry.floor_manager import WorkOrder
    from foundry.main import DEMO_ORDERS

    gemma = SimGemma(size="S")
    f = _factory(tmp_path, gemma.client)
    orders = [dict(o) for o in DEMO_ORDERS[:2]] + [
        {"title": "Add slug helper", "description": "Add a slug helper.", "files": ["shop/slug.py"]},
    ]

    async def run() -> list[str]:
        ids = [await f.manager.submit(WorkOrder(**o)) for o in orders]
        async with f.running():
            states = await f.wait(ids, timeout=30)
        assert set(states.values()) == {"complete"}, states
        await f.manager.drain()
        return ids

    try:
        ids = asyncio.run(run())
        rows = f.queue.sizing_outcomes()
        assert sorted(r["task_id"] for r in rows) == sorted(ids), rows  # one row per task (idempotent)
        for r in rows:
            assert r["predicted"] in ("S", "M", "L", "XL"), r
            assert r["sizing_source"] == "gemma", r
            assert r["final_state"] in ("complete", "approved"), r
            assert int(r["claude_calls"]) >= 2, r  # at least one builder + one reviewer call
        summary = f.foreman.calibration_summary()
        assert summary["n"] == len(ids), summary
        assert summary["under"] + summary["over"] <= summary["n"], summary
        assert 0 <= float(summary["accuracy_pct"]) <= 100, summary
    finally:
        f.close()


async def _seed_m_was_l(f: Any, n: int = 9) -> list[str]:
    """``n`` finished M-sized tasks whose recorded effort was clearly L (same repo, same files)."""
    from foundry.floor_manager import WorkOrder
    from foundry.sizing_outcomes import actual_complexity

    ids = []
    for k in range(n):
        ids.append(await f.manager.submit(WorkOrder(
            title=f"Add pricing rule {k}", description="Add a pricing rule.", files=PRICING_FILES,
        )))
    for tid in ids:
        assert f.manager.directives[tid].complexity == "M", f.manager.directives[tid]
        # 9 Claude calls + 2 retries: effort 11 -> L (see foundry/sizing_outcomes.py)
        row = f.queue.record_sizing_outcome(tid, sizing_source="gemma", attempts=4, claude_calls=9,
                                            final_state="complete")
        assert row is not None and row["predicted"] == "M", row
        assert row["actual"] == "L" == actual_complexity(row), row
    return ids


def test_history_of_m_tasks_that_were_l_shifts_new_m_order_to_l(
    tmp_path: Path, clean_foundry_env: pytest.MonkeyPatch
) -> None:
    from foundry.floor_manager import WorkOrder

    gemma = SimGemma(size="M")
    f = _factory(tmp_path, gemma.client)

    async def run() -> str:
        await _seed_m_was_l(f)
        return await f.manager.submit(WorkOrder(
            title="Add pricing rule for bulk orders", description="Add a bulk discount rule.", files=PRICING_FILES,
        ))

    try:
        tid = asyncio.run(run())
        d = f.manager.directives[tid]
        assert d.complexity == "L", d
        assert "calibrated" in d.rationale.lower(), d.rationale
        prompt = gemma.sizing_prompts[-1]
        assert "Past task" in prompt and "you predicted M, it actually took L" in prompt, prompt
        summary = f.foreman.calibration_summary()
        assert summary["n"] >= 9, summary
        assert summary["under"] >= 9, summary  # predicted M, actually bigger
        assert summary["shift_rules"], summary
    finally:
        f.close()
