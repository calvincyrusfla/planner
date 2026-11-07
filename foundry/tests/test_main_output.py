"""End-of-run report of ``python -m foundry.main --real`` (simulated Claude and Gemma, board "dry").

The user's first real run printed only the task state: the Shop Foreman's
commit message draft (a real Gemma call, many seconds) was still running when
``run_real`` shut the line down, so the pools' shutdown grace cancelled it and
neither the patch nor the ``.md`` was written or printed. These tests make the
Foreman slower than the (shrunk) shutdown grace to reproduce that.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from typing import Any

import pytest

from foundry import builder_worker, floor_manager
from foundry.config import FoundryConfig
from foundry.floor_manager import build_factory
from foundry.gemma_client import SimulatedGemmaClient
from foundry.main import (
    EXIT_DONE,
    EXIT_FAILED,
    EXIT_IN_PROGRESS,
    completion_report,
    demo_claude,
    demo_gemma,
    run_real,
    write_demo_repo,
)
from foundry.schemas import ClaudeResult
from foundry.claude_client import SimulatedClaudeClient


def _args(repo: Path, timeout: float = 15) -> argparse.Namespace:
    return argparse.Namespace(
        repo=str(repo), title="Add clamp helper", desc="Add clamp(x, lo, hi).", files=["shop/mathx.py"], timeout=timeout
    )


def _config(tmp_path: Path) -> FoundryConfig:
    return FoundryConfig(
        db_path=tmp_path / "foundry.db", patches_dir=tmp_path / "patches", rpm=1000, tpm=10_000_000,
        teams=["alpha"], board_bridge_mode="dry",
    )


def _slow_gemma(delay: float) -> SimulatedGemmaClient:
    """The demo Gemma, but every reply takes ``delay`` seconds (a real Ollama draft is slow)."""
    inner = demo_gemma().responder

    async def responder(req: Any) -> Any:
        await asyncio.sleep(delay)
        return inner(req)  # type: ignore[misc]

    return SimulatedGemmaClient(responder)


@pytest.fixture
def short_shutdown(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shrink the manager's drain and the pools' stop grace from 5 s to 0.05 s."""
    drain, stop = floor_manager.FloorManager.drain, builder_worker.TeamPool.stop

    async def fast_drain(self: Any, timeout: float = 5.0) -> None:
        await drain(self, 0.05)

    async def fast_stop(self: Any, timeout: float = 5.0) -> None:
        await stop(self, 0.05)

    monkeypatch.setattr(floor_manager.FloorManager, "drain", fast_drain)
    monkeypatch.setattr(builder_worker.TeamPool, "stop", fast_stop)


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_slow_foreman_draft_still_writes_and_prints_the_patch(tmp_path: Path, short_shutdown: None) -> None:
    repo = write_demo_repo(tmp_path / "repo")
    lines: list[str] = []
    code = _run(run_real(_args(repo), _config(tmp_path), demo_claude(repo, fail_first=None), _slow_gemma(0.5),
                         out=lines.append))
    text = "\n".join(lines)
    patch = (tmp_path / "patches" / "F-001.patch").resolve()
    md = (tmp_path / "patches" / "F-001.md").resolve()
    assert code == EXIT_DONE, text
    assert patch.is_file() and md.is_file()
    assert "F-001: complete" in text
    assert "Commit message: feat(shop): add clamp helper" in text
    assert f"Patch:          {patch}" in text
    assert f"Message + PR:   {md}" in text
    assert f'git apply "{patch}"' in text
    assert f'git commit -F "{md}"' in text
    assert str(repo.resolve()) in text  # where to run the commands
    # the commit message's first line only, not the whole PR body
    assert "Drafted by the Gemma Shop Foreman" not in text


def test_windows_report_uses_type_and_survives_a_restart(tmp_path: Path) -> None:
    repo = write_demo_repo(tmp_path / "repo")
    assert _run(run_real(_args(repo), _config(tmp_path), demo_claude(repo, fail_first=None), demo_gemma(),
                         out=lambda _l: None)) == EXIT_DONE
    # a fresh factory (another run) only knows the saved patch name, not this run's paths
    f = build_factory(_config(tmp_path), claude=SimulatedClaudeClient(), gemma=None, repo_root=repo)
    try:
        assert f.manager.patch_paths == {}
        lines = completion_report(f, "F-001", repo, windows=True)
    finally:
        f.close()
    text = "\n".join(lines)
    patch = (tmp_path / "patches" / "F-001.patch").resolve()
    assert f'type "{patch}"' in text and f'git apply "{patch}"' in text
    assert "Commit message: feat(shop): add clamp helper" in text
    unix = completion_report_unix(tmp_path, repo)
    assert f'cat "{patch}"' in unix


def completion_report_unix(tmp_path: Path, repo: Path) -> str:
    f = build_factory(_config(tmp_path), claude=SimulatedClaudeClient(), gemma=None, repo_root=repo)
    try:
        return "\n".join(completion_report(f, "F-001", repo, windows=False))
    finally:
        f.close()


def test_rejected_patch_is_reported_with_its_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(floor_manager, "_git_check", lambda patch, root: "error: patch failed: shop/mathx.py:1")
    repo = write_demo_repo(tmp_path / "repo")
    lines: list[str] = []
    code = _run(run_real(_args(repo), _config(tmp_path), demo_claude(repo, fail_first=None), demo_gemma(),
                         out=lines.append))
    text = "\n".join(lines)
    rejected = (tmp_path / "patches" / "F-001.patch.rejected").resolve()
    assert code == EXIT_DONE
    assert rejected.is_file() and not (tmp_path / "patches" / "F-001.patch").exists()
    assert "REJECTED" in text and str(rejected) in text
    assert "git apply" not in text  # nothing to apply automatically
    assert str((tmp_path / "patches" / "F-001.md").resolve()) in text


def test_patch_not_written_in_time_exits_3_and_rerun_finishes_it(tmp_path: Path, short_shutdown: None) -> None:
    repo = write_demo_repo(tmp_path / "repo")
    lines: list[str] = []
    code = _run(run_real(_args(repo), _config(tmp_path), demo_claude(repo, fail_first=None), _slow_gemma(1.5),
                         out=lines.append, finish_timeout=0.2))
    text = "\n".join(lines)
    assert code == EXIT_IN_PROGRESS, text
    assert "rerun the same command to finish it" in text
    assert not (tmp_path / "patches" / "F-001.patch").exists()

    lines2: list[str] = []
    code2 = _run(run_real(_args(repo), _config(tmp_path), demo_claude(repo, fail_first=None), demo_gemma(),
                          out=lines2.append))
    text2 = "\n".join(lines2)
    assert code2 == EXIT_DONE, text2
    assert "F-001: resuming" in text2 and "F-002" not in text2
    patch = (tmp_path / "patches" / "F-001.patch").resolve()
    assert patch.is_file() and f'git apply "{patch}"' in text2
    assert "+++ b/shop/mathx.py" in patch.read_text(encoding="utf-8")


def test_quarantine_is_still_explained_without_patch_lines(tmp_path: Path) -> None:
    repo = write_demo_repo(tmp_path / "repo")

    def broken(prompt: str, agent_id: str) -> ClaudeResult:
        if prompt.startswith("You are reviewer") or "## Unified diff" in prompt:
            text = '{"verdict": "APPROVED", "directives": []}'
        else:
            text = "```diff\n--- a/shop/mathx.py\n+++ b/shop/mathx.py\n@@ -1,1 +1,2 @@\n-nope\n+def broken(:\n```\n"
        return ClaudeResult(text=text, input_tokens=10, output_tokens=10, duration_seconds=0.0)

    lines: list[str] = []
    code = _run(run_real(_args(repo), _config(tmp_path), SimulatedClaudeClient(broken), demo_gemma(), out=lines.append))
    text = "\n".join(lines)
    assert code == EXIT_FAILED, text
    assert "F-001: quarantined" in text and "reason:" in text
    assert "Patch:" not in text and "git apply" not in text


def test_still_in_progress_is_explained(tmp_path: Path) -> None:
    repo = write_demo_repo(tmp_path / "repo")
    slow = demo_claude(repo, fail_first=None)
    slow.latency = 1.0
    lines: list[str] = []
    code = _run(run_real(_args(repo, 0.2), _config(tmp_path), slow, demo_gemma(), out=lines.append))
    text = "\n".join(lines)
    assert code == EXIT_IN_PROGRESS
    assert "rerun the same command to resume" in text and "Patch:" not in text
