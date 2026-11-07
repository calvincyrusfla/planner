"""End-to-end orchestration tests (the spec's required list).

Every test wires the real modules together (queue, telemetry, rate limiter,
context engine, the real ShopForeman, gates, cache, TeamPools, FloorManager,
BoardBridge in dry mode) and swaps only the two model clients for
``SimulatedClaudeClient`` and ``SimulatedGemmaClient``. Nothing here calls real
Claude, Ollama or the Agent Department board.

1. dynamic sizing                 -> test_s_task_uses_exactly_one_builder
2. context pruning                -> test_builder_prompt_contains_only_the_pruned_slice
3. traceback condensation         -> test_simulated_pytest_failure_condenses_to_one_line*
4. gate short-circuit             -> test_broken_diff_never_reaches_the_reviewer
5. retry limit                    -> (same test) third failure quarantines, no 4th build
6. rate limiter under load        -> test_rate_limiter_*
7. escalation validation/actions  -> test_escalation_* / test_floor_manager_executes_*
8. foreman_probe                  -> test_probe_*
plus the ``python -m foundry.main --simulate`` demo.
"""

from __future__ import annotations

import asyncio
import json
import re
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from foundry.board_bridge import BoardBridge
from foundry.claude_client import ClaudeRateLimited, SimulatedClaudeClient
from foundry.config import FoundryConfig
from foundry.floor_manager import Factory, WorkOrder, build_factory
from foundry.foreman_probe import probe
from foundry.gemma_client import GemmaReply, GemmaRequest, SimulatedGemmaClient
from foundry.main import append_diff, demo_claude, run_demo, write_demo_repo
from foundry.rate_limiter import RateLimiter
from foundry.schemas import ClaudeResult, ForemanEscalation, TaskState


# --------------------------------------------------------------------------- helpers


def _factory(
    tmp_path: Path,
    claude: Any,
    *,
    gemma: Any = None,
    repo: Path | None = None,
    teams: list[str] | None = None,
    limiter: Any = None,
    **cfg: Any,
) -> Factory:
    repo = repo or write_demo_repo(tmp_path / "repo")
    config = FoundryConfig(
        db_path=tmp_path / "foundry.db",
        rpm=1000,
        tpm=10_000_000,
        max_concurrent=5,
        teams=teams or ["alpha", "bravo", "charlie"],
        board_bridge_mode="dry",
        **cfg,
    )
    return build_factory(
        config, claude=claude, gemma=gemma, repo_root=repo, workspace=repo,
        bridge=BoardBridge("dry"), limiter=limiter,
    )


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


async def _submit_and_run(f: Factory, *orders: dict[str, Any], timeout: float = 15) -> tuple[list[str], dict[str, str]]:
    ids = [await f.manager.submit(WorkOrder(**o)) for o in orders]
    async with f.running():
        states = await f.wait(ids, timeout=timeout)
        await asyncio.sleep(0.05)  # give any (wrong) extra worker call a chance to happen
    return ids, states


def _alert(action: str, *, category: str = "LINE_JAM", params: dict[str, Any] | None = None, **target: str) -> ForemanEscalation:
    return ForemanEscalation(
        alert_id="ALT-20261107-001",
        timestamp="2026-11-07T12:00:00Z",
        severity="HIGH",
        category=category,
        target_entity=target,
        metrics_snapshot={"queue_depth": 1, "dwell_seconds": 0.0, "gate_failure_count": 0, "current_tpm": 0.0},
        diagnosis="Test alert.",
        prescribed_action=action,
        action_parameters=params or {},
    )


S_ORDER = {
    "title": "Add clamp helper",
    "description": "Add clamp(x, lo, hi) to the math helpers.",
    "files": ["shop/mathx.py"],
    "acceptance_criteria": ["clamp returns x limited to lo..hi"],
}


# --------------------------------------------------------------------------- 1 dynamic sizing


def test_s_task_uses_exactly_one_builder(tmp_path: Path) -> None:
    repo = write_demo_repo(tmp_path / "repo")
    claude = demo_claude(repo, fail_first=None)
    f = _factory(tmp_path, claude, repo=repo)
    ids, states = _run(_submit_and_run(f, {**S_ORDER, "team_id": "alpha"}))
    d = f.manager.directives[ids[0]]
    assert (d.complexity, d.allocated_builders) == ("S", 1)
    assert states == {ids[0]: "complete"}
    assert claude.calls["alpha-1"] == 1
    for slot in range(2, 6):
        assert claude.calls[f"alpha-{slot}"] == 0, f"idle alpha-{slot} called Claude"
    for team in ("bravo", "charlie"):  # teams with no work never call Claude either
        assert all(claude.calls[f"{team}-{n}"] == 0 for n in range(1, 7))
    assert claude.calls["alpha-6"] == 1  # one review
    assert claude.role_calls["manager"] == 0  # the Floor Manager makes no Claude calls
    f.close()


def test_m_and_l_tasks_use_two_and_five_builders(tmp_path: Path) -> None:
    repo = write_demo_repo(tmp_path / "repo")
    claude = demo_claude(repo, fail_first=None)
    f = _factory(tmp_path, claude, repo=repo)
    m = {"title": "Add casing helpers", "description": "two files", "files": ["shop/strings.py", "shop/slug.py"], "team_id": "bravo"}
    big = ["shop/inventory.py", "shop/pricing.py", "shop/orders.py", "shop/report.py", "shop/cli.py"]
    l_ = {"title": "Rewrite inventory end-to-end", "description": "five files", "files": big, "team_id": "charlie"}
    ids, states = _run(_submit_and_run(f, m, l_))
    assert [f.manager.directives[t].allocated_builders for t in ids] == [2, 5]
    assert set(states.values()) == {"complete"}
    assert [claude.calls[f"bravo-{n}"] for n in range(1, 6)] == [1, 1, 0, 0, 0]
    assert [claude.calls[f"charlie-{n}"] for n in range(1, 6)] == [1, 1, 1, 1, 1]
    f.close()


# --------------------------------------------------------------------------- 2 context pruning


def _big_module(n_funcs: int = 30, body_lines: int = 12) -> str:
    out = ['"""Big fixture module."""', "", "", "class Ledger:", '    """A ledger."""', "", "    total: int = 0", ""]
    for i in range(n_funcs):
        out += ["", f"def compute_total_{i}(rows, rate):", f'    """Compute total {i} of the rows."""', "    acc = 0"]
        out += [f"    acc += rate * {j}  # SECRET_BODY_LINE_{i}_{j}" for j in range(body_lines)]
        out += ["    return acc", ""]
    return "\n".join(out) + "\n"


def test_builder_prompt_contains_only_the_pruned_slice(tmp_path: Path) -> None:
    repo = write_demo_repo(tmp_path / "repo")
    big = _big_module()
    (repo / "shop" / "ledger.py").write_text(big, encoding="utf-8")
    assert len(big.splitlines()) > 400
    claude = demo_claude(repo, fail_first=None)
    f = _factory(tmp_path, claude, repo=repo, teams=["alpha"])
    order = {
        "title": "Add compute_total_3 rounding",
        "description": "Round compute_total_3 results in the ledger.",
        "files": ["shop/ledger.py"],
        "acceptance_criteria": ["compute_total_3 rounds to 2 decimals"],
    }
    ids, states = _run(_submit_and_run(f, order))
    prompt = claude.prompts_for("alpha-1")[0]
    assert "SECRET_BODY_LINE" not in prompt  # no function bodies of the big file
    assert "def compute_total_3(rows, rate):" in prompt  # the relevant signature is there
    assert len(prompt.splitlines()) < len(big.splitlines()) / 2
    # every snippet block is at most 50 lines
    blocks = re.findall(r"^### \S+ \(lines \d+-\d+\)\n(`{3,})text\n(.*?)\n\1$", prompt, re.M | re.S)
    assert blocks, prompt
    assert all(len(text.splitlines()) <= 50 for _, text in blocks)
    # the reviewer only sees the diff, never the context snippets
    review_prompt = claude.prompts_for("alpha-6")[0]
    assert "compute_total_3(rows, rate)" not in review_prompt and "## Unified diff" in review_prompt
    assert f.telemetry.tokens_saved_by_source().get("pruning", 0) > 0
    f.close()


# --------------------------------------------------------------------------- 3 traceback condensation

PYTEST_FAILURE = """\
============================= test session starts ==============================
collected 3 items

tests/test_mathx.py ..F                                                  [100%]

=================================== FAILURES ===================================
_________________________________ test_clamp ___________________________________

    def test_clamp():
>       assert clamp(5, 0, 3) == 3
E       assert 5 == 3
E        +  where 5 = clamp(5, 0, 3)

tests/test_mathx.py:12: AssertionError
----------------------------- Captured stdout call -----------------------------
debug noise line 1
debug noise line 2
=========================== short test summary info ============================
FAILED tests/test_mathx.py::test_clamp - assert 5 == 3
========================= 1 failed, 2 passed in 0.03s ==========================
"""


def test_simulated_pytest_failure_condenses_to_one_line(tmp_path: Path) -> None:
    f = _factory(tmp_path, SimulatedClaudeClient(), gemma=None, teams=["alpha"])
    hint = _run(f.foreman.condense_error_trace(PYTEST_FAILURE))
    assert "\n" not in hint and len(hint) <= 160
    assert "tests/test_mathx.py:12" in hint
    f.close()


def test_simulated_pytest_failure_condensed_by_gemma(tmp_path: Path) -> None:
    def responder(req: GemmaRequest) -> Any:
        return "tests/test_mathx.py:12: clamp(5, 0, 3) returned 5; cap x at hi."

    gemma = SimulatedGemmaClient(responder)
    f = _factory(tmp_path, SimulatedClaudeClient(), gemma=gemma, teams=["alpha"])
    hint = _run(f.foreman.condense_error_trace(PYTEST_FAILURE, task_id="F-001"))
    assert hint == "tests/test_mathx.py:12: clamp(5, 0, 3) returned 5; cap x at hi."
    assert f.foreman.last_source["condense_error_trace"] == "gemma"
    assert f.telemetry.tokens_saved_by_source().get("triage", 0) > 0
    f.close()


def test_simulated_pytest_failure_with_gemma_down_falls_back(tmp_path: Path) -> None:
    f = _factory(tmp_path, SimulatedClaudeClient(), gemma=SimulatedGemmaClient(unavailable=True), teams=["alpha"])
    hint = _run(f.foreman.condense_error_trace(PYTEST_FAILURE))
    assert "tests/test_mathx.py:12" in hint and "\n" not in hint
    f.close()


# --------------------------------------------------------------------------- 4+5 gate short-circuit and retry limit


def _always_broken(repo: Path) -> SimulatedClaudeClient:
    def script(prompt: str, agent_id: str) -> str:
        if "## Unified diff" in prompt:
            return json.dumps({"verdict": "APPROVED", "directives": []})
        src = (repo / "shop/mathx.py").read_text(encoding="utf-8")
        return "```diff\n" + append_diff("shop/mathx.py", src, ["", "", "def clamp(x, lo, hi:", "    return x"]) + "```\n"

    return SimulatedClaudeClient(script)


def test_broken_diff_never_reaches_the_reviewer_and_third_failure_quarantines(tmp_path: Path) -> None:
    repo = write_demo_repo(tmp_path / "repo")
    claude = _always_broken(repo)
    f = _factory(tmp_path, claude, repo=repo, teams=["alpha"])
    ids, states = _run(_submit_and_run(f, S_ORDER))
    tid = ids[0]
    # 4: the reviewer never saw the broken diff
    assert claude.calls["alpha-6"] == 0
    assert claude.role_calls["reviewer"] == 0
    # 5: attempts 1, 2, 3 -> quarantined, no 4th build call
    assert states[tid] == TaskState.QUARANTINED.value
    assert claude.calls["alpha-1"] == 3
    prompts = claude.prompts_for("alpha-1")
    assert "## Fix required" not in prompts[0]
    assert all("## Fix required" in p and "[syntax]" in p for p in prompts[1:])
    retries = [e for e in f.queue.events() if e["kind"] == "retry"]
    assert len(retries) == 2
    for e in retries:  # every hint is one line naming file:line
        hint = e["payload"]["fix_hint"]
        assert "\n" not in hint and re.search(r"shop/mathx\.py:\d+", hint)
    # the quarantine reaches the board
    paused = [op for op in f.bridge.ops if op["type"] == "updateTask" and op["args"]["patch"] == {"status": "paused"}]
    assert len(paused) == 1
    f.close()


def test_gate_failure_then_retry_succeeds(tmp_path: Path) -> None:
    repo = write_demo_repo(tmp_path / "repo")
    claude = demo_claude(repo, fail_first="alpha-1")
    f = _factory(tmp_path, claude, repo=repo, teams=["alpha"])
    ids, states = _run(_submit_and_run(f, S_ORDER))
    assert states[ids[0]] == "complete"
    assert claude.calls["alpha-1"] == 2 and claude.calls["alpha-6"] == 1
    summary = f.manager.pr_summaries[ids[0]]
    assert summary["commit_message"] and summary["pr_body"]
    done = [op for op in f.bridge.ops if op["type"] == "updateTask" and op["args"]["patch"] == {"status": "done"}]
    assert len(done) == 1
    f.close()


# --------------------------------------------------------------------------- 6 rate limiter under load


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.t

    async def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += max(0.0, s)
        await asyncio.sleep(0)


def test_rate_limiter_caps_in_flight_under_load() -> None:
    clock = _Clock()
    limiter = RateLimiter(rpm=1000, tpm=10_000_000, max_concurrent=3, clock=clock, sleep=clock.sleep)
    claude = SimulatedClaudeClient(lambda p, a: "ok", latency=0.005)

    async def one(i: int) -> ClaudeResult:
        async with limiter.slot(100):
            return await claude.run("p", agent_id=f"alpha-{i % 5 + 1}", role="builder")

    async def go() -> None:
        await asyncio.gather(*(one(i) for i in range(20)))

    _run(go())
    assert claude.total_calls == 20
    assert claude.max_in_flight <= 3 and limiter.peak_in_flight == 3


def test_rate_limiter_respects_rpm_with_fake_clock() -> None:
    clock = _Clock()
    limiter = RateLimiter(rpm=6, tpm=10_000_000, max_concurrent=3, clock=clock, sleep=clock.sleep)
    starts: list[float] = []

    async def one() -> None:
        async with limiter.slot(10):
            starts.append(clock.t)

    async def go() -> None:
        await asyncio.gather(*(one() for _ in range(20)))

    _run(go())
    assert len(starts) == 20
    # Token bucket: a burst of rpm requests, then one every 60/rpm seconds.
    assert starts[:6] == [1000.0] * 6
    gaps = [b - a for a, b in zip(starts[6:], starts[7:])]
    assert all(g >= 10 - 1e-6 for g in gaps)
    assert starts[-1] - 1000.0 == pytest.approx(60 * (20 - 6) / 6)
    for t in starts:  # never more than burst + one minute of refill in any 60 s window
        assert sum(1 for s in starts if t <= s < t + 60) <= 6 + 6


def test_rate_limiter_backs_off_on_429_with_jitter() -> None:
    clock = _Clock()
    rolls = iter([0.5, 0.25, 0.75])
    limiter = RateLimiter(rpm=1000, tpm=10_000_000, max_concurrent=3, clock=clock, sleep=clock.sleep, rng=lambda: next(rolls))
    claude = SimulatedClaudeClient(lambda p, a: "ok")
    claude.inject_rate_limit(times=3)

    async def call() -> ClaudeResult:
        async with limiter.slot(10):
            return await claude.run("p", agent_id="alpha-1", role="builder")

    result = _run(limiter.call_with_backoff(call, base=1.0, cap=60.0))
    assert result.text == "ok" and claude.calls["alpha-1"] == 4
    backoffs = clock.sleeps[:3]
    assert backoffs == pytest.approx([0.5 * 1, 0.25 * 2, 0.75 * 4])  # full jitter: rng * base * 2**n


def test_rate_limited_builders_still_finish_through_the_factory(tmp_path: Path) -> None:
    repo = write_demo_repo(tmp_path / "repo")
    claude = demo_claude(repo, fail_first=None)
    claude.inject_rate_limit(agent_id="alpha-1", times=2, retry_after=0.01)
    clock = _Clock()
    limiter = RateLimiter(rpm=1000, tpm=10_000_000, max_concurrent=3, clock=clock, sleep=clock.sleep)
    f = _factory(tmp_path, claude, repo=repo, teams=["alpha"], limiter=limiter)
    ids, states = _run(_submit_and_run(f, S_ORDER))
    assert states[ids[0]] == "complete"
    assert claude.calls["alpha-1"] == 3 and limiter.rate_limit_hits == 2
    f.close()


# --------------------------------------------------------------------------- 7 escalations


def test_escalation_validation_good_and_bad() -> None:
    good = _alert("QUARANTINE_TASK", task_id="F-001")
    assert good.prescribed_action.value == "QUARANTINE_TASK"
    base = good.model_dump(mode="json")
    bad_payloads = [
        {**base, "alert_id": "ALT-2026-1"},
        {**base, "timestamp": "2026-11-07T12:00:00"},  # no UTC offset
        {**base, "timestamp": "2026-11-07T12:00:00+02:00"},
        {**base, "severity": "URGENT"},
        {**base, "category": "SLOW"},
        {**base, "prescribed_action": "REBOOT"},
        {**base, "target_entity": {}},
        {**base, "diagnosis": "One. Two. Three."},
        {**base, "metrics_snapshot": {"queue_depth": 1}},
        {**base, "surprise": True},
    ]
    for payload in bad_payloads:
        with pytest.raises(ValidationError):
            ForemanEscalation.model_validate(payload)
    # extra metrics are allowed
    ok = ForemanEscalation.model_validate({**base, "metrics_snapshot": {**base["metrics_snapshot"], "tpm_cap": 5}})
    assert ok.metrics_snapshot.model_dump()["tpm_cap"] == 5


def test_floor_manager_rejects_invalid_escalation_dict(tmp_path: Path) -> None:
    f = _factory(tmp_path, SimulatedClaudeClient(), teams=["alpha"])
    with pytest.raises(ValidationError):
        _run(f.manager.apply_escalation({"alert_id": "nope"}))
    f.close()


def test_floor_manager_executes_each_action(tmp_path: Path) -> None:
    repo = write_demo_repo(tmp_path / "repo")
    claude = SimulatedClaudeClient()
    f = _factory(tmp_path, claude, repo=repo, teams=["alpha"])
    m = f.manager

    async def go() -> None:
        t1 = await m.submit(WorkOrder(**{**S_ORDER, "title": "quarantine me"}))
        t2 = await m.submit(WorkOrder(title="resize me", description="d", files=["shop/strings.py", "shop/slug.py"]))
        t3 = await m.submit(WorkOrder(title="stall me", description="d", files=["shop/pricing.py"]))
        q = f.queue

        # QUARANTINE_TASK
        out = await m.apply_escalation(_alert("QUARANTINE_TASK", task_id=t1, params={"reason": "2 gate failures"}))
        assert out.applied and q.get_task(t1)["state"] == "quarantined"

        # RESIZE_ALLOCATION
        out = await m.apply_escalation(
            _alert("RESIZE_ALLOCATION", category="CAPACITY_REBALANCE", task_id=t2, team_id="alpha", params={"builders": 4, "from": 2})
        )
        assert out.applied and q.get_task(t2)["allocated_builders"] == 4

        # THROTTLE_DISPATCH
        out = await m.apply_escalation(
            _alert("THROTTLE_DISPATCH", category="TOKEN_VELOCITY_SPIKE", team_id="alpha", params={"factor": 0.5, "seconds": 60})
        )
        assert out.applied and f.limiter.is_throttled and f.limiter.throttle_factor == 0.5

        # RESET_BUILDER_CONTEXT: a claimed ticket on slot 1 of t3 (t2's slot-1 ticket comes first; claim until we hit t3)
        pool = f.pools["alpha"]
        ticket = None
        while ticket is None or ticket.task_id != t3:
            ticket = q.claim_ticket("alpha", 1)
            assert ticket is not None
        pool.builder(1).scratch["last_prompt"] = "old"
        out = await m.apply_escalation(
            _alert(
                "RESET_BUILDER_CONTEXT", category="AGENT_STALLED", task_id=t3, team_id="alpha", agent_id="alpha-1",
                params={"agent_id": "alpha-1", "ticket_id": ticket.ticket_id, "slot": 1},
            )
        )
        assert out.applied and pool.builder(1).scratch == {}
        reopened = q.get_ticket(ticket.ticket_id)
        assert reopened.status == "open" and reopened.fix_hint is None

        # PROMOTE_PEER_REVIEWER (needs the pool running)
        q.quarantine(t2, "test cleanup")
        q.quarantine(t3, "test cleanup")
        async with f.running():
            out = await m.apply_escalation(
                _alert("PROMOTE_PEER_REVIEWER", category="REVIEW_BOTTLENECK", team_id="alpha", params={"minutes": 1, "slot": 5})
            )
            assert out.applied
            assert "alpha-5" in pool.peer_reviewers
            grant = m.peer_reviewer_for("alpha")
            assert grant is not None and grant.agent_id == "alpha-5"

        # NONE
        out = await m.apply_escalation(_alert("NONE", team_id="alpha"))
        assert out.applied and out.detail == "no action"

    _run(go())
    assert [a.action for a in m.actions] == [
        "QUARANTINE_TASK", "RESIZE_ALLOCATION", "THROTTLE_DISPATCH", "RESET_BUILDER_CONTEXT", "PROMOTE_PEER_REVIEWER", "NONE",
    ]
    # every escalation is stored and posted to the board in the agreed format
    assert len(f.queue.escalations()) == 6
    posted = [op["args"]["message"] for op in f.bridge.ops if op["type"] == "reportProgress" and op["args"]["message"].startswith("[Foreman]")]
    assert len(posted) == 6 and posted[0].startswith("[Foreman] HIGH LINE_JAM: Test alert.")
    assert claude.total_calls == 0  # none of the actions called Claude
    f.close()


def test_real_foreman_line_jam_drives_quarantine(tmp_path: Path) -> None:
    """The real ShopForeman detects LINE_JAM from telemetry; the manager quarantines."""
    gemma = SimulatedGemmaClient(lambda req: "Task keeps failing its syntax gate. Quarantine it for a human.")
    f = _factory(tmp_path, SimulatedClaudeClient(), gemma=gemma, teams=["alpha"])

    async def go() -> None:
        tid = await f.manager.submit(WorkOrder(**S_ORDER))
        f.telemetry.record_gate_failure(tid)
        f.telemetry.record_gate_failure(tid)
        alert = await f.foreman.evaluate_line_anomaly()
        assert alert is not None and alert.category.value == "LINE_JAM"
        assert alert.prescribed_action.value == "QUARANTINE_TASK"
        assert alert.diagnosis == "Task keeps failing its syntax gate. Quarantine it for a human."
        out = await f.manager.apply_escalation(alert)
        assert out.applied and f.queue.get_task(tid)["state"] == "quarantined"
        assert await f.foreman.evaluate_line_anomaly() is None  # terminal task: line healthy again

    _run(go())
    f.close()


# --------------------------------------------------------------------------- 8 foreman probe


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_probe_fails_gracefully_when_nothing_listens() -> None:
    port = _free_port()
    t0 = time.monotonic()
    r = probe(f"http://127.0.0.1:{port}/v1", "gemma4:e4b", timeout=1)
    assert time.monotonic() - t0 < 3
    assert r.ok is False and r.models == []
    assert "not reachable" in r.message and "setup_foreman" in r.message


def test_probe_succeeds_against_local_fake_ollama() -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            body = json.dumps({"object": "list", "data": [{"id": "gemma4:e4b", "object": "model"}]}).encode()
            self.send_response(200 if self.path.rstrip("/") == "/v1/models" else 404)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a: Any) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        r = probe(f"http://127.0.0.1:{server.server_port}/v1", "gemma4:e4b", timeout=2)
    finally:
        server.shutdown()
    assert r.ok is True and "gemma4:e4b" in r.models


# --------------------------------------------------------------------------- demo


def test_simulated_demo_runs_end_to_end_quickly(tmp_path: Path) -> None:
    t0 = time.monotonic()
    result = _run(run_demo(workdir=tmp_path, quiet=True))
    assert time.monotonic() - t0 < 10
    assert [d["allocated_builders"] for d in result["directives"].values()] == [1, 2, 5]
    assert set(result["states"].values()) == {"complete"}
    calls = result["calls"]
    assert [calls.get(f"alpha-{n}", 0) for n in range(1, 6)] == [1, 0, 0, 0, 0]
    assert calls.get("bravo-1") == 2 and result["retries"] == 1  # forced gate failure + retry
    assert result["alert"] is not None and result["action"]["applied"]
    assert len(result["pr_summaries"]) == 3
    out = result["output"]
    for needle in ("Sizing", "Selective dispatch", "gate failed -> hint", "Review", "Floor Manager ->", "PR summaries"):
        assert needle in out
    assert calls.get("alpha-6", 0) == 1 and "manager" not in json.dumps(calls)


def test_main_real_exits_2_when_foreman_is_offline(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    from foundry import main as main_mod

    monkeypatch.setenv("FOUNDRY_GEMMA_BASE_URL", f"http://127.0.0.1:{_free_port()}/v1")
    code = main_mod.main(["--real", "--title", "x"])
    assert code == 2
    assert "not reachable" in capsys.readouterr().err
