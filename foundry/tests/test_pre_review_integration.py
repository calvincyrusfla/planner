"""BuilderWorker integration of the Gemma pre-review (T-018, feature 1).

Real queue / telemetry / gates over a temp workspace, simulated Claude and a
scripted foreman ``pre_review_diff``: no real Claude, Ollama or board.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from foundry.builder_worker import (
    PRE_REVIEW_KV_PREFIX,
    BuilderSubmission,
    TeamPool,
    build_reviewer_prompt,
    format_pre_review_notes,
)
from foundry.orchestrator_queue import FoundryQueue
from foundry.telemetry import Telemetry
from foundry.tests.test_builder_worker import (
    APPROVE,
    GOOD_DIFF,
    ORIGINAL,
    REVIEWER,
    TEAM,
    ContextEngine,
    FakeForeman,
    Rig,
    fenced,
    is_reviewer,
)

BUILDER = f"{TEAM}-1"

BLOCKING = {
    "verdict": "send_back",
    "issues": [{"severity": "blocking", "note": "TODO left where mul should be"},
               {"severity": "minor", "note": "docstring could be longer"}],
    "confidence": 0.9,
}
PASS = {"verdict": "pass", "issues": [], "confidence": 0.95}


class PreReviewForeman(FakeForeman):
    """FakeForeman plus a scripted ``pre_review_diff`` (sync or async, may raise)."""

    def __init__(self, engine: ContextEngine, results: list[Any], *, source: str = "gemma",
                 is_async: bool = False) -> None:
        super().__init__(engine)
        self.results = list(results)
        self.source = source
        self.is_async = is_async
        self.calls: list[tuple[Any, Any, str, list[Any]]] = []
        self.last_source: dict[str, str] = {}

    def _next(self, task: Any, assignment: Any, diff: str, gate_results: list[Any]) -> Any:
        self.calls.append((task, assignment, diff, gate_results))
        result = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        self.last_source["pre_review_diff"] = self.source
        if isinstance(result, BaseException):
            raise result
        return result

    def pre_review_diff(self, task: Any, assignment: Any, diff: str, gate_results: list[Any]) -> Any:
        if self.is_async:
            async def later() -> Any:
                return self._next(task, assignment, diff, gate_results)
            return later()
        return self._next(task, assignment, diff, gate_results)


def make_rig(tmp_path: Path, results: list[Any], **kwargs: Any) -> Rig:
    rig = Rig(tmp_path, lambda p, agent: APPROVE if is_reviewer(agent) else fenced(GOOD_DIFF))
    rig.foreman = PreReviewForeman(ContextEngine(rig.ws), results, **kwargs)
    return rig


async def run(rig: Rig, task_ids: list[str], **pool_kwargs: Any) -> dict[str, str]:
    pool = rig.pool(1, **pool_kwargs)
    async with pool:
        states = await pool.wait_for_tasks(task_ids, timeout=15.0)
        await asyncio.sleep(0.05)
    return states


# ---------------------------------------------------------------------------
# pure helpers
# ---------------------------------------------------------------------------


def test_reviewer_prompt_contains_the_pre_review_notes() -> None:
    sub = BuilderSubmission(task_id="T-1", subtask_id="T-1.1", builder_id=BUILDER, status="ready_for_review",
                            modified_files=["pkg/mathx.py"], diff=GOOD_DIFF)
    prompt = build_reviewer_prompt(sub, ["works"], [], pre_review_notes="minor: name is vague")
    assert "Gemma pre-review notes (may be wrong): minor: name is vague" in prompt
    assert prompt.index("Gemma pre-review") < prompt.index("## Unified diff")
    assert "Gemma pre-review" not in build_reviewer_prompt(sub, ["works"], [])


def test_format_pre_review_notes() -> None:
    assert format_pre_review_notes([]) is None
    assert format_pre_review_notes([{"severity": "minor", "note": " "}]) is None
    text = format_pre_review_notes(BLOCKING["issues"])
    assert text == "blocking: TODO left where mul should be; minor: docstring could be longer"


# ---------------------------------------------------------------------------
# gate mode
# ---------------------------------------------------------------------------


def test_gate_sends_back_once_then_claude_reviews_with_notes(tmp_path: Path) -> None:
    rig = make_rig(tmp_path, [BLOCKING])  # Gemma (wrongly) flags every submission
    rig.submit("T-G", "S")
    states = asyncio.run(run(rig, ["T-G"]))
    assert states == {"T-G": "complete"}
    assert rig.client.calls[BUILDER] == 2  # sent back once by the pre-review
    assert rig.client.calls[REVIEWER] == 1  # the second submission always reaches Claude
    second_build = rig.client.prompts_for(BUILDER)[1]
    assert "[pre-review] blocking: TODO left where mul should be" in second_build
    assert "docstring" not in second_build.split("## Fix required", 1)[1].split("\n\n", 1)[0]
    review = rig.client.prompts_for(REVIEWER)[0]
    assert "Gemma pre-review notes (may be wrong): blocking: TODO left" in review
    counts = rig.telemetry.pre_review_counts()
    assert counts["sent_back"] == 1 and counts["passed"] == 1
    assert counts["claude_reviews_avoided"] == 1
    assert rig.telemetry.snapshot()["pre_review"] == counts
    assert rig.telemetry.retries("T-G") == 1  # a normal rejected attempt
    ticket_id = rig.queue.tickets_for_task("T-G")[0].ticket_id
    assert rig.queue.kv_get(PRE_REVIEW_KV_PREFIX + ticket_id) == 1
    kinds = [e["payload"]["outcome"] for e in rig.queue.events(task_id="T-G", kind="pre_review")]
    assert kinds == ["sent_back", "passed"]


def test_once_per_ticket_cap_survives_a_restart(tmp_path: Path) -> None:
    rig = make_rig(tmp_path, [BLOCKING])
    rig.submit("T-C", "S")
    ticket_id = rig.queue.tickets_for_task("T-C")[0].ticket_id
    # A previous run already sent this ticket back once (persisted in the queue db).
    rig.queue.kv_set(PRE_REVIEW_KV_PREFIX + ticket_id, 1)
    rig.queue.close()
    rig.queue = FoundryQueue(tmp_path / "foundry.db")
    rig.telemetry = Telemetry(rig.queue)
    states = asyncio.run(run(rig, ["T-C"]))
    assert states == {"T-C": "complete"}
    assert rig.client.calls[BUILDER] == 1 and rig.client.calls[REVIEWER] == 1
    assert rig.telemetry.pre_review_counts()["sent_back"] == 0


@pytest.mark.parametrize(
    "result",
    [
        {"verdict": "send_back", "issues": [{"severity": "blocking", "note": "x"}], "confidence": 0.5},
        {"verdict": "send_back", "issues": [{"severity": "minor", "note": "style"}], "confidence": 0.99},
        {"verdict": "pass", "issues": [{"severity": "blocking", "note": "x"}], "confidence": 0.99},
    ],
    ids=["low-confidence", "no-blocking-issue", "verdict-pass"],
)
def test_gate_does_not_send_back_unless_confident_and_blocking(tmp_path: Path, result: dict[str, Any]) -> None:
    rig = make_rig(tmp_path, [result], is_async=True)
    rig.submit("T-N", "S")
    assert asyncio.run(run(rig, ["T-N"])) == {"T-N": "complete"}
    assert rig.client.calls[BUILDER] == 1 and rig.client.calls[REVIEWER] == 1
    assert "Gemma pre-review notes (may be wrong):" in rig.client.prompts_for(REVIEWER)[0]
    assert rig.telemetry.pre_review_counts()["passed"] == 1


def test_clean_pass_adds_no_notes(tmp_path: Path) -> None:
    rig = make_rig(tmp_path, [PASS])
    rig.submit("T-P", "S")
    assert asyncio.run(run(rig, ["T-P"])) == {"T-P": "complete"}
    assert "Gemma pre-review" not in rig.client.prompts_for(REVIEWER)[0]
    task, assignment, diff, gates = rig.foreman.calls[0]
    assert task["task_id"] == "T-P" and assignment.subtask_id == "T-P.1"
    assert diff == GOOD_DIFF and gates and all(g.passed for g in gates)


def test_pre_review_send_back_counts_toward_quarantine(tmp_path: Path) -> None:
    rig = make_rig(tmp_path, [BLOCKING])
    rig.submit("T-Q", "S")
    ticket_id = rig.queue.tickets_for_task("T-Q")[0].ticket_id
    # Burn the earlier attempts: this submission is the last one allowed.
    assert rig.queue.claim_ticket(TEAM, 1) is not None
    assert rig.queue.reopen_ticket(ticket_id, "earlier gate failure")
    assert rig.queue.claim_ticket(TEAM, 1) is not None
    assert rig.queue.reopen_ticket(ticket_id, "earlier gate failure")
    states = asyncio.run(run(rig, ["T-Q"]))
    assert states == {"T-Q": "quarantined"}
    assert rig.client.calls[REVIEWER] == 0
    assert (rig.ws / "pkg" / "mathx.py").read_text() == ORIGINAL  # rolled back


# ---------------------------------------------------------------------------
# fallback, advise, off
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("how", ["heuristic-source", "raises", "none"])
def test_fallback_always_passes(tmp_path: Path, how: str) -> None:
    if how == "heuristic-source":
        rig = make_rig(tmp_path, [BLOCKING], source="heuristic")
    elif how == "raises":
        rig = make_rig(tmp_path, [RuntimeError("ollama down")])
    else:
        rig = make_rig(tmp_path, [None])
    rig.submit("T-F", "S")
    assert asyncio.run(run(rig, ["T-F"])) == {"T-F": "complete"}
    assert rig.client.calls[BUILDER] == 1 and rig.client.calls[REVIEWER] == 1
    assert "Gemma pre-review" not in rig.client.prompts_for(REVIEWER)[0]
    counts = rig.telemetry.pre_review_counts()
    assert counts["fallback"] == 1 and counts["sent_back"] == 0
    assert rig.telemetry.retries("T-F") == 0  # Gemma errors never consume attempts


def test_advise_never_sends_back_but_shares_notes(tmp_path: Path) -> None:
    rig = make_rig(tmp_path, [BLOCKING])
    rig.submit("T-A", "S")
    assert asyncio.run(run(rig, ["T-A"], pre_review="advise")) == {"T-A": "complete"}
    assert rig.client.calls[BUILDER] == 1 and rig.client.calls[REVIEWER] == 1
    assert "Gemma pre-review notes (may be wrong): blocking: TODO left" in rig.client.prompts_for(REVIEWER)[0]
    counts = rig.telemetry.pre_review_counts()
    assert counts["advised"] == 1 and counts["sent_back"] == 0 and counts["claude_reviews_avoided"] == 0


def test_off_skips_gemma(tmp_path: Path) -> None:
    rig = make_rig(tmp_path, [BLOCKING])
    rig.submit("T-O", "S")
    assert asyncio.run(run(rig, ["T-O"], pre_review="off")) == {"T-O": "complete"}
    assert rig.foreman.calls == []
    assert rig.client.calls[REVIEWER] == 1
    assert rig.telemetry.pre_review_counts()["skipped"] == 1


def test_mode_and_threshold_come_from_config(tmp_path: Path) -> None:
    from types import SimpleNamespace

    rig = make_rig(tmp_path, [BLOCKING])
    pool = rig.pool(1, config=SimpleNamespace(pre_review="advise", pre_review_min_confidence=0.95))
    assert pool.builders[0].pre_review == "advise"
    assert pool.builders[0].pre_review_min_confidence == 0.95
    with pytest.raises(ValueError):
        TeamPool(TEAM, 1, queue=rig.queue, client=rig.client, limiter=None, telemetry=rig.telemetry,
                 pre_review="sometimes")


def test_pre_review_counts_per_task_and_unknown_outcome(tmp_path: Path) -> None:
    tel = Telemetry(FoundryQueue(tmp_path / "t.db"))
    tel.record_pre_review("T-1", "sent_back")
    tel.record_pre_review("T-2", "passed")
    assert tel.pre_review_counts("T-1")["sent_back"] == 1
    assert tel.pre_review_counts("T-1")["passed"] == 0
    with pytest.raises(ValueError):
        tel.record_pre_review("T-1", "maybe")


# ---------------------------------------------------------------------------
# with the real ShopForeman and a simulated Gemma
# ---------------------------------------------------------------------------


def _real_foreman(rig: Rig, responder: Any) -> Any:
    from foundry.config import FoundryConfig
    from foundry.gemma_client import SimulatedGemmaClient
    from foundry.shop_foreman import ShopForeman

    gemma = SimulatedGemmaClient(responder)
    foreman = ShopForeman(gemma, rig.queue, rig.telemetry, ContextEngine(rig.ws), FoundryConfig())
    return foreman, gemma


def test_real_foreman_send_back_once_then_reviewed(tmp_path: Path) -> None:
    def responder(req: Any) -> Any:
        if "Diff:" in str(req.messages[-1].get("content")):
            return BLOCKING
        return "fix it"

    rig = Rig(tmp_path, lambda p, agent: APPROVE if is_reviewer(agent) else fenced(GOOD_DIFF))
    rig.foreman, _gemma = _real_foreman(rig, responder)
    rig.submit("T-RF", "S")
    assert asyncio.run(run(rig, ["T-RF"])) == {"T-RF": "complete"}
    assert rig.client.calls[BUILDER] == 2 and rig.client.calls[REVIEWER] == 1
    assert "[pre-review] blocking: TODO left" in rig.client.prompts_for(BUILDER)[1]
    assert rig.foreman.last_source["pre_review_diff"] == "gemma"


def test_real_foreman_gemma_down_is_fallback_pass(tmp_path: Path) -> None:
    from foundry.gemma_client import GemmaUnavailable

    def responder(req: Any) -> Any:
        raise GemmaUnavailable("down")

    rig = Rig(tmp_path, lambda p, agent: APPROVE if is_reviewer(agent) else fenced(GOOD_DIFF))
    rig.foreman, _gemma = _real_foreman(rig, responder)
    rig.submit("T-RD", "S")
    assert asyncio.run(run(rig, ["T-RD"])) == {"T-RD": "complete"}
    assert rig.client.calls[BUILDER] == 1 and rig.client.calls[REVIEWER] == 1
    assert rig.telemetry.pre_review_counts()["fallback"] == 1
