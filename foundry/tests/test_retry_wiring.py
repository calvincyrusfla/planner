"""A retry gets the failed gate's own output and, when one is configured, the retry client."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from foundry import serve
from foundry.builder_worker import MAX_FIX_HINT_CHARS, TeamPool
from foundry.claude_client import SimulatedClaudeClient
from foundry.config import FoundryConfig
from foundry.main import builder_retry_claude_args
from foundry.schemas import BuilderSubmission, DeterministicGateResult, TaskAssignment
from foundry.tests.test_builder_client_wiring import FakeClaude, _factory
from foundry.tests.test_builder_tests_args import FakeCodeClient, _serve_main
from foundry.tests.test_builder_worker import APPROVE, GOOD_DIFF, REVIEWER, TEAM, FakeForeman, Rig, fenced, is_reviewer

BUILDER = f"{TEAM}-1"
TASK = "T-R"
LONG_HINT = "condensed " + "x" * 1090
GATE_OUTPUT = "\n".join(f"pkg/mathx.py:{i}: error: something is wrong here" for i in range(300))


class LongForeman(FakeForeman):
    """Condenses every trace to the same long one-line hint."""

    def condense_error_trace(self, trace: str) -> str:
        self.traces.append(trace)
        return LONG_HINT


class FailOnce:
    """Gates that fail the first submission with ``output`` and pass the rest."""

    def __init__(self, output: str) -> None:
        self.output = output
        self.calls = 0

    def __call__(self, _submission: BuilderSubmission, a: TaskAssignment) -> list[DeterministicGateResult]:
        self.calls += 1
        failed = self.calls == 1
        return [
            DeterministicGateResult(
                task_id=a.task_id,
                subtask_id=a.subtask_id,
                passed=not failed,
                check_type="syntax",
                stderr=self.output if failed else "",
            )
        ]


def _reply(_prompt: str, agent: str) -> str:
    return APPROVE if is_reviewer(agent) else fenced(GOOD_DIFF)


def _rig(tmp_path: Path, output: str = GATE_OUTPUT) -> Rig:
    rig = Rig(tmp_path, _reply, gates=FailOnce(output))
    rig.foreman = LongForeman(rig.foreman.engine)
    rig.submit(TASK, "S")
    return rig


def _config(tmp_path: Path, chars: int) -> FoundryConfig:
    return FoundryConfig(db_path=tmp_path / "foundry.db", gate_error_chars=chars)


def _run(rig: Rig, **pool_kwargs: Any) -> None:
    async def scenario() -> dict[str, str]:
        async with rig.pool(**pool_kwargs) as pool:
            return await pool.wait_for_tasks([TASK], timeout=15)

    assert asyncio.run(scenario()) == {TASK: "complete"}


def _stored_hint(rig: Rig) -> str:
    (ticket,) = rig.queue.tickets_for_task(TASK)
    assert ticket.fix_hint is not None
    return ticket.fix_hint


# -- gate output in the retry hint ---------------------------------------------


def test_without_gate_error_chars_the_hint_is_unchanged(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    _run(rig, config=_config(tmp_path, 0))
    assert _stored_hint(rig) == f"[syntax] {LONG_HINT}"


def test_without_a_config_the_hint_is_unchanged(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    _run(rig)
    assert _stored_hint(rig) == f"[syntax] {LONG_HINT}"


@pytest.mark.parametrize("chars", [500, 3000])
def test_retry_hint_carries_the_end_of_the_gate_output(tmp_path: Path, chars: int) -> None:
    rig = _rig(tmp_path)
    _run(rig, config=_config(tmp_path, chars))
    tail = GATE_OUTPUT[-chars:]
    assert "\n" in tail
    stored = _stored_hint(rig)
    assert stored == f"[syntax] {LONG_HINT}\n\nGate output (last {chars} characters):\n{tail}"
    assert len(stored) > MAX_FIX_HINT_CHARS
    retry_prompt = rig.client.prompts_for(BUILDER)[1]
    assert stored in retry_prompt


def test_empty_gate_output_adds_nothing(tmp_path: Path) -> None:
    rig = _rig(tmp_path, output="  \n")
    _run(rig, config=_config(tmp_path, 500))
    assert _stored_hint(rig) == "[syntax] gate failed with no output"


def test_other_send_backs_keep_the_cap(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    pool = rig.pool(config=_config(tmp_path, 500))
    ticket = rig.queue.claim_ticket(TEAM, 1)
    assert ticket is not None
    assert pool.builder(1)._send_back(ticket, "y" * 5000, reason="revise") == "retry"
    stored = _stored_hint(rig)
    assert len(stored) == MAX_FIX_HINT_CHARS and stored.endswith("...")


# -- retry client ----------------------------------------------------------------


def test_first_attempt_uses_builder_client_and_retry_the_retry_client(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    builder, retry = SimulatedClaudeClient(_reply), SimulatedClaudeClient(_reply)
    _run(rig, builder_client=builder, builder_retry_client=retry)
    assert builder.calls[BUILDER] == 1
    assert retry.calls[BUILDER] == 1
    assert "Fix required" not in builder.prompts_for(BUILDER)[0]
    assert "Fix required" in retry.prompts_for(BUILDER)[0]
    assert rig.client.calls[BUILDER] == 0
    assert rig.client.calls[REVIEWER] == 1
    assert retry.calls[REVIEWER] == 0


def test_without_retry_client_every_attempt_uses_the_builder_client(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    builder = SimulatedClaudeClient(_reply)
    _run(rig, builder_client=builder)
    assert builder.calls[BUILDER] == 2
    assert rig.client.calls[BUILDER] == 0


def _pool(**clients: Any) -> TeamPool:
    return TeamPool("alpha", 2, queue=object(), client=clients.pop("client"), limiter=None, telemetry=None, **clients)


def test_pool_stores_the_retry_client_on_every_builder() -> None:
    client, builder_client, retry_client = FakeClaude(), FakeClaude(), FakeClaude()
    pool = _pool(client=client, builder_client=builder_client, builder_retry_client=retry_client)
    assert all(builder.retry_client is retry_client for builder in pool.builders)
    assert all(builder.client is builder_client for builder in pool.builders)
    assert pool.reviewer.client is client


def test_pool_retry_client_defaults_to_the_builder_client() -> None:
    client, builder_client = FakeClaude(), FakeClaude()
    pool = _pool(client=client, builder_client=builder_client)
    assert all(builder.retry_client is builder_client for builder in pool.builders)


# -- build_factory and Service ---------------------------------------------------


def test_build_factory_passes_the_retry_client_to_every_pool(tmp_path: Path) -> None:
    claude, retry = FakeClaude(), FakeClaude()
    factory = _factory(tmp_path, claude, builder_retry_claude=retry)
    try:
        assert set(factory.pools) == {"alpha", "bravo"}
        for pool in factory.pools.values():
            assert all(builder.retry_client is retry for builder in pool.builders)
            assert all(builder.client is claude for builder in pool.builders)
    finally:
        factory.close()


def test_build_factory_without_retry_client_uses_the_builder_client(tmp_path: Path) -> None:
    claude, builder_claude = FakeClaude(), FakeClaude()
    factory = _factory(tmp_path, claude, builder_claude=builder_claude)
    try:
        for pool in factory.pools.values():
            assert all(builder.retry_client is builder_claude for builder in pool.builders)
    finally:
        factory.close()


def test_service_passes_the_retry_client_to_build_factory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: dict[str, Any] = {}

    def fake_build_factory(_config: Any, **kwargs: Any) -> str:
        seen.update(kwargs)
        return "factory"

    monkeypatch.setattr("foundry.floor_manager.build_factory", fake_build_factory)
    retry = FakeClaude()
    config = FoundryConfig(db_path=tmp_path / "foundry.db", auto_pr=False)
    service = serve.Service(config, repo=tmp_path, claude=FakeClaude(), builder_retry_claude=retry)
    assert service._build() == "factory"
    assert seen["builder_retry_claude"] is retry


def test_serve_main_creates_the_retry_client_when_a_retry_model_is_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("FOUNDRY_BUILDER_RETRY_MODEL", "opus")
    service = _serve_main(monkeypatch, tmp_path)
    config = FoundryConfig.from_env()
    assert isinstance(service.builder_retry_claude, FakeCodeClient)
    assert service.builder_retry_claude.extra_args == builder_retry_claude_args(config)
    assert "opus" in service.builder_retry_claude.extra_args


def test_serve_main_has_no_retry_client_without_a_retry_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("FOUNDRY_BUILDER_RETRY_MODEL", raising=False)
    service = _serve_main(monkeypatch, tmp_path)
    assert service.builder_retry_claude is None


def test_serve_main_gives_the_retry_client_the_builder_options(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("FOUNDRY_BUILDER_RETRY_MODEL", "opus")
    monkeypatch.setenv("FOUNDRY_BUILDER_MODE", "edit")
    monkeypatch.setenv("FOUNDRY_BUILDER_TESTS", "1")
    monkeypatch.setenv("PATH", "some-other-dir")
    service = _serve_main(monkeypatch, tmp_path)
    assert service.builder_retry_claude.options == {
        "env": {"PATH": os.path.dirname(sys.executable) + os.pathsep + "some-other-dir"}
    }
    assert service.builder_retry_claude.options == service.builder_claude.options
