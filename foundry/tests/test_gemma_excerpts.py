"""Gemma picks the gate-output lines a retry needs (foundry/gemma_excerpts.py)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from foundry.config import FoundryConfig
from foundry.gemma_excerpts import error_excerpt
from foundry.tests.test_builder_worker import Rig
from foundry.tests.test_retry_wiring import GATE_OUTPUT, LONG_HINT, TASK, FailOnce, LongForeman, _reply, _run, _stored_hint


class _Reply:
    def __init__(self, data: Any) -> None:
        self._data = data

    def parse_json(self) -> Any:
        if isinstance(self._data, Exception):
            raise self._data
        return self._data


class _Gemma:
    def __init__(self, data: Any = None, error: Exception | None = None, delay: float = 0.0) -> None:
        self.data = data
        self.error = error
        self.delay = delay
        self.calls = 0
        self.last_user = ""

    async def chat(self, messages: Any, **kw: Any) -> _Reply:
        self.calls += 1
        self.last_user = messages[-1]["content"]
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return _Reply(self.data)


LINES = [f"line {i}" for i in range(1, 21)]
OUTPUT = "\n".join(LINES)


def _excerpt(gemma: Any, output: str = OUTPUT, max_chars: int = 1000, timeout: float = 5) -> str | None:
    return asyncio.run(error_excerpt(gemma, output, timeout=timeout, max_chars=max_chars))


def test_ranges_become_original_lines_with_dots_between() -> None:
    gemma = _Gemma({"ranges": [{"start": 3, "end": 4}, {"start": 12, "end": 12}]})
    assert _excerpt(gemma) == "\n".join(LINES[1:5]) + "\n...\n" + "\n".join(LINES[10:13])
    assert "3: line 3" in gemma.last_user


def test_overlapping_and_out_of_range_ranges() -> None:
    gemma = _Gemma({"ranges": [{"start": 5, "end": 8}, {"start": 7, "end": 10}, {"start": 50, "end": 60},
                               {"start": 9, "end": 2}, {"start": -5, "end": 1}, "x"]})
    assert _excerpt(gemma) == "\n".join(LINES[0:2]) + "\n...\n" + "\n".join(LINES[3:11])


def test_adjacent_ranges_merge_without_dots() -> None:
    gemma = _Gemma({"ranges": [{"start": 5, "end": 6}, {"start": 8, "end": 9}]})
    assert _excerpt(gemma) == "\n".join(LINES[3:10])


def test_only_last_400_lines_are_numbered() -> None:
    big = "\n".join(f"row {i}" for i in range(1, 501))
    gemma = _Gemma({"ranges": [{"start": 50, "end": 60}, {"start": 101, "end": 101}, {"start": 480, "end": 480}]})
    assert _excerpt(gemma, big) == "row 101\nrow 102\n...\nrow 479\nrow 480\nrow 481"
    assert gemma.last_user.count("\n") < 410 and "401: row 401" in gemma.last_user and "100: row" not in gemma.last_user


def test_garbage_timeout_and_errors_give_none() -> None:
    assert _excerpt(_Gemma("nonsense")) is None
    assert _excerpt(_Gemma({"ranges": []})) is None
    assert _excerpt(_Gemma({"ranges": [{"start": "a", "end": 2}]})) is None
    assert _excerpt(_Gemma(error=RuntimeError("boom"))) is None
    assert _excerpt(_Gemma({"ranges": [{"start": 1, "end": 2}]}, delay=1.0), timeout=0.05) is None
    assert _excerpt(_Gemma({"ranges": [{"start": 1, "end": 2}]}), output="") is None


def test_excerpt_over_max_chars_drops_last_ranges_then_none() -> None:
    gemma = _Gemma({"ranges": [{"start": 3, "end": 3}, {"start": 15, "end": 15}]})
    first_only = "\n".join(LINES[1:4])
    assert _excerpt(gemma, max_chars=len(first_only)) == first_only
    assert _excerpt(gemma, max_chars=5) is None


# -- worker level ----------------------------------------------------------------


def _gemma_rig(tmp_path: Path, gemma: Any) -> Rig:
    rig = Rig(tmp_path, _reply, gates=FailOnce(GATE_OUTPUT))
    rig.foreman = LongForeman(rig.foreman.engine)
    assert rig.engine is not None
    rig.engine.gemma = gemma
    rig.submit(TASK, "S")
    return rig


def _config_notes(tmp_path: Path, enabled: bool) -> FoundryConfig:
    return FoundryConfig(db_path=tmp_path / "foundry.db", gate_error_chars=3000, gemma_notes=enabled)


GOOD = {"ranges": [{"start": 100, "end": 101}]}
PICKED = "\n".join(GATE_OUTPUT.splitlines()[98:102])


def test_worker_uses_excerpt_when_on(tmp_path: Path) -> None:
    rig = _gemma_rig(tmp_path, _Gemma(GOOD))
    _run(rig, config=_config_notes(tmp_path, True))
    assert _stored_hint(rig) == f"[syntax] {LONG_HINT}\n\nGate output (the lines that matter, copied exactly):\n{PICKED}"


def test_worker_keeps_tail_when_off(tmp_path: Path) -> None:
    gemma = _Gemma(GOOD)
    rig = _gemma_rig(tmp_path, gemma)
    _run(rig, config=_config_notes(tmp_path, False))
    assert _stored_hint(rig) == f"[syntax] {LONG_HINT}\n\nGate output (last 3000 characters):\n{GATE_OUTPUT[-3000:]}"
    assert gemma.calls == 0


def test_worker_keeps_tail_when_gemma_fails(tmp_path: Path) -> None:
    rig = _gemma_rig(tmp_path, _Gemma(error=RuntimeError("down")))
    _run(rig, config=_config_notes(tmp_path, True))
    assert _stored_hint(rig) == f"[syntax] {LONG_HINT}\n\nGate output (last 3000 characters):\n{GATE_OUTPUT[-3000:]}"
