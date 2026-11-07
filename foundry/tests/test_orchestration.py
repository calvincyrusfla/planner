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
from foundry.claude_client import SimulatedClaudeClient
from foundry.config import FoundryConfig
from foundry.floor_manager import Factory, WorkOrder, build_factory
from foundry.foreman_probe import probe
from foundry.gemma_client import GemmaRequest, SimulatedGemmaClient
from foundry.main import append_diff, demo_claude, run_demo, write_demo_repo
from foundry.rate_limiter import RateLimiter
from foundry.schemas import ClaudeResult, DeterministicGateResult, ForemanEscalation, TaskState


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
        bridge=BoardBridge("dry"), limiter=limiter, patches_dir=tmp_path / "patches",
    )


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


async def _submit_and_run(f: Factory, *orders: dict[str, Any], timeout: float = 15) -> tuple[list[str], dict[str, str]]:
    ids = [await f.manager.submit(WorkOrder(**o)) for o in orders]
    async with f.running():
        states = await f.wait(ids, timeout=timeout)
        await asyncio.sleep(0.05)  # give any (wrong) extra worker call a chance to happen
    return ids, states


_ALERT_SEQ = iter(range(1, 1000))


def _alert(
    action: str, *, category: str = "LINE_JAM", params: dict[str, Any] | None = None, alert_id: str | None = None, **target: str
) -> ForemanEscalation:
    return ForemanEscalation(
        alert_id=alert_id or f"ALT-20261107-{next(_ALERT_SEQ):03d}",
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
        assert reopened.attempt == ticket.attempt  # a stall costs no retry (queue.release_ticket)

        # PROMOTE_PEER_REVIEWER (needs the pool running)
        q.quarantine(t2, "test cleanup")
        q.quarantine(t3, "test cleanup")
        # not running -> the pool cannot promote: applied=False
        out = await m.apply_escalation(
            _alert("PROMOTE_PEER_REVIEWER", category="REVIEW_BOTTLENECK", team_id="alpha", params={"minutes": 1})
        )
        assert not out.applied and m.peer_reviewer_for("alpha") is None
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
        "QUARANTINE_TASK", "RESIZE_ALLOCATION", "THROTTLE_DISPATCH", "RESET_BUILDER_CONTEXT",
        "PROMOTE_PEER_REVIEWER", "PROMOTE_PEER_REVIEWER", "NONE",
    ]
    assert [a.applied for a in m.actions] == [True, True, True, True, False, True, True]
    # every escalation is stored and posted to the board in the agreed format
    assert len(f.queue.escalations()) == 7
    posted = [op["args"]["message"] for op in f.bridge.ops if op["type"] == "reportProgress" and op["args"]["message"].startswith("[Foreman]")]
    assert len(posted) == 7 and posted[0].startswith("[Foreman] HIGH LINE_JAM: Test alert.")
    assert claude.total_calls == 0  # none of the actions called Claude
    f.close()


def test_real_foreman_line_jam_drives_quarantine(tmp_path: Path) -> None:
    """The real ShopForeman detects LINE_JAM from telemetry; the manager quarantines."""
    gemma = SimulatedGemmaClient(lambda req: "Task keeps failing its syntax gate. Quarantine it for a human.")
    f = _factory(tmp_path, SimulatedClaudeClient(), gemma=gemma, teams=["alpha"])

    async def go() -> None:
        tid = await f.manager.submit(WorkOrder(**S_ORDER))
        sub = f.manager.directives[tid].subtasks[0].subtask_id
        for _ in range(2):  # the ticket failed its gates twice (D8: counted per ticket)
            f.queue.record_gate_result(
                DeterministicGateResult(task_id=tid, subtask_id=sub, passed=False, check_type="syntax", stderr="SyntaxError")
            )
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


def test_main_real_exits_2_when_foreman_is_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from foundry import claude_client
    from foundry import main as main_mod

    fake = tmp_path / "claude"
    fake.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(claude_client, "resolve_claude_bin", lambda name="claude", **kw: str(fake))
    monkeypatch.setenv("FOUNDRY_GEMMA_BASE_URL", f"http://127.0.0.1:{_free_port()}/v1")
    code = main_mod.main(["--real", "--title", "x"])
    assert code == 2
    assert "not reachable" in capsys.readouterr().err


def test_main_real_exits_2_when_claude_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from foundry import main as main_mod

    monkeypatch.setenv("FOUNDRY_CLAUDE_BIN", str(tmp_path / "no-such-claude"))
    monkeypatch.setenv("PATH", str(tmp_path))
    code = main_mod.main(["--real", "--title", "x"])
    assert code == 2
    assert "Claude Code is not ready" in capsys.readouterr().err


def test_claude_args_come_from_config() -> None:
    from foundry.main import claude_args

    args = claude_args(FoundryConfig())
    assert "--append-system-prompt" in args
    joined = " ".join(args)
    for tool in ("Bash", "Edit", "Write"):
        assert tool in joined  # write/exec tools are disallowed for diff-only roles


# --------------------------------------------------------------------------- restart safety, patches, idempotency


def test_two_runs_on_one_db_continue_where_the_first_stopped(tmp_path: Path) -> None:
    repo = write_demo_repo(tmp_path / "repo")
    claude = demo_claude(repo, fail_first=None)
    f1 = _factory(tmp_path, claude, repo=repo, teams=["alpha"])
    ids1, states1 = _run(_submit_and_run(f1, S_ORDER))
    board_id = f1.bridge.board_ids[ids1[0]]
    f1.close()

    f2 = _factory(tmp_path, claude, repo=repo, teams=["alpha"])  # same db, fresh process state
    assert ids1[0] in f2.manager.directives  # manager state reloaded from the queue's kv table
    assert f2.manager.pr_summaries[ids1[0]] == f1.manager.pr_summaries[ids1[0]]
    assert f2.bridge.board_ids[ids1[0]] == board_id
    assert f2.bridge.board_teams[ids1[0]] == ["alpha"]  # who signs off survives the restart
    ids2, states2 = _run(_submit_and_run(f2, {**S_ORDER, "title": "Add wrap helper"}))
    assert ids1 == ["F-001"] and ids2 == ["F-002"]  # no id collision on restart
    assert states1 == {"F-001": "complete"} and states2 == {"F-002": "complete"}
    assert [op["type"] for op in f2.bridge.ops].count("addTask") == 1  # F-001 is not re-registered
    f2.close()


def test_restart_resumes_tickets_a_crashed_run_left_claimed(tmp_path: Path) -> None:
    repo = write_demo_repo(tmp_path / "repo")
    claude = demo_claude(repo, fail_first=None)
    f1 = _factory(tmp_path, claude, repo=repo, teams=["alpha"])
    tid = _run(f1.manager.submit(WorkOrder(**S_ORDER)))
    claimed = f1.queue.claim_ticket("alpha", 1)  # a worker claimed it, then the process died
    assert claimed is not None
    f1.queue.close()

    f2 = _factory(tmp_path, claude, repo=repo, teams=["alpha"])
    assert f2.queue.get_ticket(claimed.ticket_id).status == "open"  # build_factory ran queue.resume()

    async def go() -> dict[str, str]:
        async with f2.running():
            return await f2.wait([tid], timeout=15)

    assert _run(go()) == {tid: "complete"}
    assert claude.calls["alpha-1"] == 1
    f2.close()


def test_approved_task_writes_patch_and_pr_draft_and_never_touches_the_repo(tmp_path: Path) -> None:
    repo = write_demo_repo(tmp_path / "repo")
    before = {p: p.read_text(encoding="utf-8") for p in repo.rglob("*.py")}
    claude = demo_claude(repo, fail_first=None)
    f = _factory(tmp_path, claude, repo=repo, teams=["alpha"])
    ids, states = _run(_submit_and_run(f, S_ORDER))
    assert states[ids[0]] == "complete"
    assert {p: p.read_text(encoding="utf-8") for p in repo.rglob("*.py")} == before  # working tree untouched
    patch = tmp_path / "patches" / f"{ids[0]}.patch"
    md = tmp_path / "patches" / f"{ids[0]}.md"
    assert patch.is_file() and md.is_file()
    text = patch.read_text(encoding="utf-8")
    assert "+++ b/shop/mathx.py" in text and "def mathx_helper(value):" in text
    assert md.read_text(encoding="utf-8").startswith(f.manager.pr_summaries[ids[0]]["commit_message"])
    # the patch applies cleanly to a copy of the repo
    from foundry.validation_gates import apply_diff
    import shutil

    copy = tmp_path / "copy"
    shutil.copytree(repo, copy)
    apply_diff(text, copy, allowed_files=["shop/mathx.py"])
    assert "def mathx_helper(value):" in (copy / "shop/mathx.py").read_text(encoding="utf-8")
    # claude ran in the target repo (read-only role, diff-only output)
    assert {c.cwd for c in claude.history} == {str(repo.resolve())}
    f.close()


def test_apply_escalation_is_idempotent_per_alert_id(tmp_path: Path) -> None:
    f = _factory(tmp_path, SimulatedClaudeClient(), teams=["alpha"])

    async def go() -> None:
        tid = await f.manager.submit(WorkOrder(**S_ORDER))
        alert = _alert("RESIZE_ALLOCATION", category="CAPACITY_REBALANCE", task_id=tid, team_id="alpha", params={"builders": 3})
        first = await f.manager.apply_escalation(alert)
        f.queue.resize(tid, 1)  # someone changed it since
        again = await f.manager.apply_escalation(alert)
        assert again == first and f.queue.get_task(tid)["allocated_builders"] == 1  # not re-applied
        assert len(f.queue.escalations()) == 1
        posted = [op for op in f.bridge.ops if op["args"].get("message", "").startswith("[Foreman]")]
        assert len(posted) == 1

    _run(go())
    f.close()
    # after a restart the stored outcome is still honoured
    f2 = _factory(tmp_path, SimulatedClaudeClient(), teams=["alpha"])
    out = _run(f2.manager.apply_escalation(_alert("NONE", alert_id="ALT-20261107-900", team_id="alpha")))
    assert out.applied
    f2.manager.applied_alerts.clear()  # even with the manager's cache gone, the queue row wins
    again = _run(f2.manager.apply_escalation(_alert("QUARANTINE_TASK", alert_id="ALT-20261107-900", task_id="F-001")))
    assert again.action == "NONE" and f2.queue.get_task("F-001")["state"] != "quarantined"
    f2.close()


def test_quarantine_is_reported_to_the_board_once(tmp_path: Path) -> None:
    f = _factory(tmp_path, SimulatedClaudeClient(), teams=["alpha"])

    async def go() -> None:
        tid = await f.manager.submit(WorkOrder(**S_ORDER))
        await f.manager.apply_escalation(_alert("QUARANTINE_TASK", task_id=tid, params={"reason": "jam"}))
        out = await f.manager.apply_escalation(_alert("QUARANTINE_TASK", task_id=tid, params={"reason": "jam"}))
        assert not out.applied  # already quarantined
        assert f.manager.report_quarantines() == []

    _run(go())
    paused = [op for op in f.bridge.ops if op["type"] == "updateTask" and op["args"]["patch"] == {"status": "paused"}]
    assert len(paused) == 1
    f.close()


# --------------------------------------------------------------------------- --real exits (E3), with simulated clients


def _real_args(repo: Path, timeout: float, title: str = "Add clamp helper") -> Any:
    import argparse

    return argparse.Namespace(repo=str(repo), title=title, desc="Add clamp(x, lo, hi).", files=["shop/mathx.py"], timeout=timeout)


def _real_config(tmp_path: Path) -> FoundryConfig:
    return FoundryConfig(
        db_path=tmp_path / "foundry.db", patches_dir=tmp_path / "patches", rpm=1000, tpm=10_000_000,
        teams=["alpha"], board_bridge_mode="dry",
    )


def test_run_real_done_exits_0_and_prints_the_patch(tmp_path: Path) -> None:
    from foundry.main import EXIT_DONE, run_real

    repo = write_demo_repo(tmp_path / "repo")
    lines: list[str] = []
    code = _run(run_real(_real_args(repo, 15), _real_config(tmp_path), demo_claude(repo, fail_first=None),
                         SimulatedGemmaClient(), out=lines.append))
    assert code == EXIT_DONE
    text = "\n".join(lines)
    assert "F-001: complete" in text and "git apply" in text and (tmp_path / "patches" / "F-001.patch").is_file()


def test_run_real_quarantine_exits_1_with_reason(tmp_path: Path) -> None:
    from foundry.main import EXIT_FAILED, run_real

    repo = write_demo_repo(tmp_path / "repo")
    lines: list[str] = []
    code = _run(run_real(_real_args(repo, 15), _real_config(tmp_path), _always_broken(repo), SimulatedGemmaClient(), out=lines.append))
    assert code == EXIT_FAILED
    text = "\n".join(lines)
    assert "F-001: quarantined" in text and "reason:" in text


def test_run_real_timeout_exits_3_and_rerun_resumes(tmp_path: Path) -> None:
    from foundry.main import EXIT_DONE, EXIT_IN_PROGRESS, run_real

    repo = write_demo_repo(tmp_path / "repo")
    slow = demo_claude(repo, fail_first=None)
    slow.latency = 1.0
    lines: list[str] = []
    code = _run(run_real(_real_args(repo, 0.2), _real_config(tmp_path), slow, SimulatedGemmaClient(), out=lines.append))
    assert code == EXIT_IN_PROGRESS
    text = "\n".join(lines)
    assert "not finished after 0.2s" in text and "rerun the same command to resume" in text
    # the rerun picks the same task up instead of submitting a second one
    lines2: list[str] = []
    code2 = _run(run_real(_real_args(repo, 15), _real_config(tmp_path), demo_claude(repo, fail_first=None),
                          SimulatedGemmaClient(), out=lines2.append))
    assert code2 == EXIT_DONE
    assert "F-001: resuming" in "\n".join(lines2) and "F-002" not in "\n".join(lines2)


def test_run_real_usage_limit_pause_is_explained(tmp_path: Path) -> None:
    from foundry.main import EXIT_IN_PROGRESS, run_real

    repo = write_demo_repo(tmp_path / "repo")
    claude = demo_claude(repo, fail_first=None)
    claude.inject_rate_limit(times=1, retry_after=3600)
    lines: list[str] = []
    code = _run(run_real(_real_args(repo, 0.5), _real_config(tmp_path), claude, SimulatedGemmaClient(), out=lines.append))
    assert code == EXIT_IN_PROGRESS
    text = "\n".join(lines)
    assert re.search(r"paused until \d\d:\d\d \(usage limit\) - rerun the same command to resume", text), text


def test_main_real_preflight_rejects_a_bad_claude_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from foundry import claude_client
    from foundry import main as main_mod

    def bad_launch(self: Any, **kw: Any) -> Any:
        raise claude_client.ClaudeError("argument contains a character claude.cmd cannot pass")

    monkeypatch.setattr(claude_client, "resolve_claude_bin", lambda name="claude", **kw: str(tmp_path / "claude.cmd"))
    monkeypatch.setattr(claude_client.ClaudeCodeClient, "validate_launch", bad_launch)
    assert main_mod.main(["--real", "--title", "x"]) == 2
    assert "Claude Code is not ready: argument contains" in capsys.readouterr().err


# --------------------------------------------------------------------------- combined patch (M2) and scratch copies (M3)


def test_combined_patch_is_keyed_by_subtask_and_checked_with_git(tmp_path: Path) -> None:
    import shutil as _sh
    from types import SimpleNamespace

    repo = write_demo_repo(tmp_path / "repo")
    f = _factory(tmp_path, SimulatedClaudeClient(), repo=repo, teams=["alpha"])
    src = (repo / "shop/mathx.py").read_text(encoding="utf-8")
    good = append_diff("shop/mathx.py", src, ["", "", "def clamp(x, lo, hi):", "    return x"])

    async def go() -> None:
        tid = await f.manager.submit(WorkOrder(**S_ORDER))
        sub = f.manager.directives[tid].subtasks[0].subtask_id
        item = SimpleNamespace(
            assignment=SimpleNamespace(task_id=tid, subtask_id=sub, team_id="alpha", builder_id="alpha-1"),
            submission=SimpleNamespace(diff=good), ticket_id="none",
        )
        verdict = SimpleNamespace(reviewer_id="alpha-6")
        await f.manager.on_approved(item, verdict)
        await f.manager.on_approved(item, verdict)  # re-approved after a resume: not duplicated
        assert list(f.manager.diffs[tid]) == [sub]
        await f.manager.on_task_complete(tid)

    _run(go())
    text = (tmp_path / "patches" / "F-001.patch").read_text(encoding="utf-8")
    assert text.count("+++ b/shop/mathx.py") == 1
    f.close()

    if _sh.which("git") is None:
        pytest.skip("git not installed: the --check step is skipped")
    # two subtasks appending at the same place conflict -> .patch.rejected, no .patch
    f2 = _factory(tmp_path / "two", SimulatedClaudeClient(), repo=repo, teams=["alpha"])
    (tmp_path / "two").mkdir(exist_ok=True)

    async def conflict() -> None:
        tid = await f2.manager.submit(WorkOrder(**S_ORDER))
        f2.manager.diffs[tid] = {"F-001.1": good, "F-001.2": good}
        await f2.manager.on_task_complete(tid)

    _run(conflict())
    assert (tmp_path / "two" / "patches" / "F-001.patch.rejected").is_file()
    assert not (tmp_path / "two" / "patches" / "F-001.patch").exists()
    assert "F-001" in f2.manager.rejected_patches
    f2.close()


def test_workspace_copy_failure_is_an_infrastructure_error(tmp_path: Path) -> None:
    from foundry.floor_manager import InfrastructureError, WorkspaceGates, _copy_workspace

    target = tmp_path / "exists"
    target.mkdir()
    with pytest.raises(InfrastructureError):
        _copy_workspace(write_demo_repo(tmp_path / "repo"), target)  # destination already exists

    class NeverCalled:
        async def validate_async(self, *a: Any, **k: Any) -> Any:
            raise AssertionError("gates must not run without a workspace")

    gates = WorkspaceGates(NeverCalled(), tmp_path / "repo")

    async def go() -> None:
        import foundry.floor_manager as fm

        real = fm._copy_workspace
        fm._copy_workspace = lambda s, d: (_ for _ in ()).throw(InfrastructureError("disk full"))  # type: ignore[assignment]
        try:
            from types import SimpleNamespace

            with pytest.raises(InfrastructureError, match="disk full"):
                await gates.run(SimpleNamespace(), SimpleNamespace(allowed_files=[]))
        finally:
            fm._copy_workspace = real

    _run(go())


# --------------------------------------------------------------------------- round 3: repo scoping (G1), patch files (G2), symlinks (G4), explanations (G5)


def test_real_mode_only_dispatches_tasks_of_the_current_repo(tmp_path: Path) -> None:
    """charlie-6's real2.py: an unfinished task of repo A must not be built against repo B."""
    from foundry.main import EXIT_DONE, EXIT_IN_PROGRESS, run_real

    a = write_demo_repo(tmp_path / "repoA")
    b = write_demo_repo(tmp_path / "repoB")
    (b / "shop/mathx.py").write_text('"""repo B mathx - different project."""\n\n\ndef other(value):\n    return value\n', encoding="utf-8")
    cfg = _real_config(tmp_path)
    slow = demo_claude(a, fail_first=None)
    slow.latency = 1.0
    out1: list[str] = []
    assert _run(run_real(_real_args(a, 0.2, "Add clamp helper to A"), cfg, slow, SimulatedGemmaClient(), out=out1.append)) == EXIT_IN_PROGRESS

    cb = demo_claude(b, fail_first=None)
    out2: list[str] = []
    args_b = _real_args(b, 15, "Add slug helper to B")
    args_b.files = ["shop/slug.py"]
    assert _run(run_real(args_b, cfg, cb, SimulatedGemmaClient(), out=out2.append)) == EXIT_DONE
    text = "\n".join(out2)
    assert re.search(r"F-001 \(repo .*repoA\) is unfinished - rerun with --repo \".*repoA\" to resume it", text), text
    assert cb.history and all("task f-002" in c.prompt.lower() for c in cb.history), "B's run touched A's task"
    assert {c.cwd for c in cb.history} == {str(b.resolve())}
    patches = sorted(p.name for p in (tmp_path / "patches").glob("*.patch"))
    assert patches == ["F-002.patch"]
    assert "+++ b/shop/slug.py" in (tmp_path / "patches" / "F-002.patch").read_text(encoding="utf-8")

    # back on repo A, the same command resumes F-001 there
    out3: list[str] = []
    assert _run(run_real(_real_args(a, 15, "Add clamp helper to A"), cfg, demo_claude(a, fail_first=None),
                         SimulatedGemmaClient(), out=out3.append)) == EXIT_DONE
    assert "F-001: resuming" in "\n".join(out3)


def _manager_only(tmp_path: Path) -> Factory:
    return _factory(tmp_path, SimulatedClaudeClient(), teams=["alpha"])


def test_patch_writer_removes_the_stale_variant(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import foundry.floor_manager as fm

    f = _manager_only(tmp_path)
    pd = tmp_path / "patches"
    summary = {"commit_message": "feat: x", "pr_body": "body"}
    monkeypatch.setattr(fm, "_git_check", lambda text, repo: "error: patch does not apply")
    _run(f.manager._write_patch("F-001", "--- a/x\n+++ b/x\n", summary))
    assert sorted(p.name for p in pd.iterdir()) == ["F-001.md", "F-001.patch.rejected"]
    monkeypatch.setattr(fm, "_git_check", lambda text, repo: None)
    _run(f.manager._write_patch("F-001", "--- a/y\n+++ b/y\n", summary))
    assert sorted(p.name for p in pd.iterdir()) == ["F-001.md", "F-001.patch"]  # no stale .rejected
    assert f.manager.patch_paths["F-001"].name == "F-001.patch" and "F-001" not in f.manager.rejected_patches
    f.close()


def test_patch_writer_never_overwrites_someone_elses_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import foundry.floor_manager as fm

    monkeypatch.setattr(fm, "_git_check", lambda text, repo: None)
    pd = tmp_path / "patches"
    pd.mkdir()
    (pd / "F-001.patch").write_text("an older project's patch\n", encoding="utf-8")
    f = _manager_only(tmp_path)
    summary = {"commit_message": "feat: x", "pr_body": "body"}
    _run(f.manager._write_patch("F-001", "--- a/x\n+++ b/x\n", summary))
    assert (pd / "F-001.patch").read_text(encoding="utf-8") == "an older project's patch\n"
    assert f.manager.patch_paths["F-001"] == pd / "F-001-2.patch"
    assert (pd / "F-001-2.md").read_text(encoding="utf-8").startswith("feat: x")
    # identical content is reused, not duplicated
    _run(f.manager._write_patch("F-001", "--- a/x\n+++ b/x\n", summary))
    assert sorted(p.name for p in pd.iterdir()) == ["F-001-2.md", "F-001-2.patch", "F-001.patch"]
    f.close()


def test_two_subtasks_on_one_file_give_one_honest_output(tmp_path: Path) -> None:
    """charlie-6's patch.py: two subtasks append to the same file; whatever is written must apply or be marked rejected."""
    import shutil as _sh
    import subprocess as _sp

    from foundry.schemas import SubtaskAllocation, TaskSizingDirective

    repo = write_demo_repo(tmp_path / "repo")
    f = _factory(tmp_path, demo_claude(repo, fail_first=None), repo=repo, teams=["alpha"])

    async def size(task: Any) -> TaskSizingDirective:
        return TaskSizingDirective(
            task_id=task["task_id"], team_id=task["team_id"], complexity="M", allocated_builders=2, rationale="x",
            subtasks=[SubtaskAllocation(subtask_id=f"{task['task_id']}.{i}", title=f"part {i}", acceptance_criteria=[f"c{i}"],
                                        allowed_files=["shop/mathx.py"]) for i in (1, 2)],
        )

    f.foreman.size_task_scope = size  # type: ignore[method-assign]
    ids, states = _run(_submit_and_run(f, {"title": "two parts one file", "files": ["shop/mathx.py"]}))
    assert states[ids[0]] == "complete"
    names = sorted(p.name for p in (tmp_path / "patches").iterdir())
    assert names in (["F-001.md", "F-001.patch"], ["F-001.md", "F-001.patch.rejected"])
    if "F-001.patch" in names and _sh.which("git"):
        r = _sp.run(["git", "apply", "--check", str(tmp_path / "patches" / "F-001.patch")], cwd=repo, capture_output=True)
        assert r.returncode == 0
    if _sh.which("git"):
        assert names == ["F-001.md", "F-001.patch.rejected"]  # both append at the same line: conflict
    f.close()


def test_default_patches_dir_is_next_to_the_db(tmp_path: Path) -> None:
    repo = write_demo_repo(tmp_path / "repo")
    config = FoundryConfig(db_path=tmp_path / "db" / "foundry.db", teams=["alpha"], board_bridge_mode="dry")
    f = build_factory(config, claude=SimulatedClaudeClient(), gemma=None, repo_root=repo, bridge=BoardBridge("dry"))
    assert f.manager.patches_dir == tmp_path / "db" / "patches"
    f.close()


def test_scratch_copy_keeps_symlinks_as_links(tmp_path: Path) -> None:
    from foundry.floor_manager import _copy_workspace

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("not part of the repo", encoding="utf-8")
    repo = write_demo_repo(tmp_path / "repo")
    try:
        (repo / "link").symlink_to(outside, target_is_directory=True)
        (repo / "loop").symlink_to(repo, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not available")
    _copy_workspace(repo, tmp_path / "copy")
    assert (tmp_path / "copy" / "link").is_symlink() and (tmp_path / "copy" / "loop").is_symlink()


def test_explanations_name_the_real_cause(tmp_path: Path) -> None:
    from datetime import datetime as _dt

    from foundry.main import explain_task, format_local_time

    assert format_local_time(_dt(2026, 11, 7, 15, 0), now=_dt(2026, 11, 7, 9, 0)) == "15:00"
    assert format_local_time(_dt(2026, 11, 8, 15, 0), now=_dt(2026, 11, 7, 9, 0)) == "Sun 8 Nov 15:00"
    f = _manager_only(tmp_path)
    tid = _run(f.manager.submit(WorkOrder(**S_ORDER)))
    f.queue.kv_set("last_account_error", {"type": "ClaudeAuthError", "message": "Please run /login", "at": "x"})
    state, lines = explain_task(f, tid)
    assert state == "building" and any("not logged in - run claude and /login" in ln for ln in lines)
    f.queue.kv_set("last_account_error", {"type": "ClaudeRateLimited", "message": "limit", "at": "x"})
    f.limiter.pause_until(seconds=3600)
    _, lines = explain_task(f, tid)
    assert any(re.search(r"paused until .*\(usage limit\) - rerun", ln) for ln in lines)
    f.close()
