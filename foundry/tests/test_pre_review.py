"""Tests for ShopForeman.pre_review_diff and the pre_review config fields (simulated Gemma only)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pydantic
import pytest

from foundry.config import FoundryConfig
from foundry.gemma_client import GemmaUnavailable, SimulatedGemmaClient
from foundry.orchestrator_queue import FoundryQueue
from foundry.schemas import DeterministicGateResult, SubtaskAllocation
from foundry.shop_foreman import PRE_REVIEW_TIMEOUT_CAP_S, PreReview, PreReviewIssue, ShopForeman

DIFF = """\
diff --git a/foundry/widget.py b/foundry/widget.py
new file mode 100644
--- /dev/null
+++ b/foundry/widget.py
@@ -0,0 +1,3 @@
+def widget() -> int:
+    # TODO: implement
+    pass
"""

TASK = {"task_id": "F-001", "title": "Add widget", "description": "Return the widget count."}
SUB = SubtaskAllocation(
    subtask_id="F-001.1", title="widget()", acceptance_criteria=["widget() returns 1"], allowed_files=["foundry/widget.py"]
)
GATES = [
    DeterministicGateResult(task_id="F-001", subtask_id="F-001.1", passed=True, check_type="syntax"),
    DeterministicGateResult(task_id="F-001", subtask_id="F-001.1", passed=True, check_type="tests"),
]


def foreman(tmp_path: Path, gemma: Any, **cfg: Any) -> ShopForeman:
    queue = FoundryQueue(tmp_path / "foundry.db")
    return ShopForeman(gemma, queue, None, None, FoundryConfig(db_path=tmp_path / "foundry.db", **cfg))


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_gemma_send_back_blocking(tmp_path: Path) -> None:
    gemma = SimulatedGemmaClient(lambda req: {
        "verdict": "send_back",
        "issues": [{"severity": "minor", "note": "name could be clearer"},
                   {"severity": "blocking", "note": "TODO stub left in widget()"}],
        "confidence": 0.9,
    })
    fm = foreman(tmp_path, gemma)
    result = run(fm.pre_review_diff(TASK, SUB, DIFF, GATES))
    assert isinstance(result, PreReview)
    assert result.source == "gemma" and result.verdict == "send_back"
    assert [i.note for i in result.blocking] == ["TODO stub left in widget()"]
    assert result.should_send_back(0.7)
    assert not result.should_send_back(0.95)
    assert result.notes_text().startswith("blocking: TODO stub left in widget(); minor:")
    assert fm.last_source["pre_review_diff"] == "gemma"
    req = gemma.requests[0]
    assert req.json_schema is not None and req.temperature == 0.0
    user = req.messages[-1]["content"]
    for bit in ("Add widget", "widget() returns 1", "Allowed files: foundry/widget.py", "Gates passed: syntax, tests", "TODO: implement"):
        assert bit in user
    assert "never blocking" in req.messages[0]["content"].lower()
    assert gemma.timeouts[0] == pytest.approx(PRE_REVIEW_TIMEOUT_CAP_S)
    json.dumps(result.to_dict())


def test_send_back_without_blocking_or_low_confidence_does_not_send_back(tmp_path: Path) -> None:
    replies = iter([
        {"verdict": "send_back", "issues": [{"severity": "minor", "note": "style"}], "confidence": 0.99},
        {"verdict": "send_back", "issues": [{"severity": "blocking", "note": "secret key"}], "confidence": 0.5},
        {"verdict": "pass", "issues": [{"severity": "blocking", "note": "odd"}], "confidence": 1.0},
    ])
    fm = foreman(tmp_path, SimulatedGemmaClient(lambda req: next(replies)))
    for _ in range(3):
        result = run(fm.pre_review_diff(TASK, SUB, DIFF))
        assert result.source == "gemma"
        assert not result.should_send_back(0.7)


@pytest.mark.parametrize(
    "gemma",
    [
        None,
        SimulatedGemmaClient(unavailable=True),
        SimulatedGemmaClient(lambda req: "not json"),
        SimulatedGemmaClient(lambda req: {"verdict": "maybe", "issues": [], "confidence": 1}),
        SimulatedGemmaClient(lambda req: {"verdict": "send_back", "issues": []}),
        SimulatedGemmaClient(lambda req: {"verdict": "send_back", "issues": "x", "confidence": 1}),
    ],
)
def test_fallback_is_pass(tmp_path: Path, gemma: Any) -> None:
    fm = foreman(tmp_path, gemma)
    result = run(fm.pre_review_diff(TASK, SUB, DIFF, GATES))
    assert result.verdict == "pass" and result.source == "fallback" and result.is_fallback
    assert result.error
    assert not result.should_send_back(0.0)
    assert fm.last_source["pre_review_diff"] == "fallback"
    if gemma is not None:
        assert fm.gemma_stats()["by_hook"]["pre_review_diff"]["fallbacks"] == 1


def test_timeout_falls_back(tmp_path: Path) -> None:
    async def slow(req: Any) -> str:
        await asyncio.sleep(5)
        return "{}"

    fm = foreman(tmp_path, SimulatedGemmaClient(slow), gemma_timeout_s=0.05)
    result = run(fm.pre_review_diff(TASK, None, DIFF))
    assert result.source == "fallback" and result.verdict == "pass"


def test_raising_responder_never_raises(tmp_path: Path) -> None:
    def boom(req: Any) -> Any:
        raise GemmaUnavailable("down")

    fm = foreman(tmp_path, SimulatedGemmaClient(boom))
    assert run(fm.pre_review_diff(object(), object(), "", None)).verdict == "pass"


def test_clamps_and_normalises(tmp_path: Path) -> None:
    gemma = SimulatedGemmaClient(lambda req: "```json\n" + json.dumps({
        "verdict": "Send-Back",
        "issues": [{"severity": "CRITICAL", "note": "  x  "}, {"severity": "blocking", "note": ""}, "junk"],
        "confidence": 3,
    }) + "\n```")
    result = run(foreman(tmp_path, gemma).pre_review_diff(TASK, SUB, DIFF))
    assert result.verdict == "send_back" and result.confidence == 1.0
    assert result.issues == (PreReviewIssue("minor", "x"),)
    assert not result.should_send_back(0.7)  # no blocking issue survived


def test_config_defaults_and_env() -> None:
    cfg = FoundryConfig()
    assert cfg.pre_review == "gate" and cfg.pre_review_min_confidence == 0.7
    env = FoundryConfig.from_env({"FOUNDRY_PRE_REVIEW": "ADVISE", "FOUNDRY_PRE_REVIEW_MIN_CONFIDENCE": "0.85"})
    assert env.pre_review == "advise" and env.pre_review_min_confidence == 0.85
    assert FoundryConfig.from_env({"FOUNDRY_PRE_REVIEW": "off"}).pre_review == "off"
    with pytest.raises(pydantic.ValidationError):
        FoundryConfig.from_env({"FOUNDRY_PRE_REVIEW": "sometimes"})
    with pytest.raises(pydantic.ValidationError):
        FoundryConfig(pre_review_min_confidence=1.5)
