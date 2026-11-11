"""Tests for review by risk tier: the two args functions and the reviewer's client choice."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from foundry.builder_worker import ReviewerWorker
from foundry.config import FoundryConfig
from foundry.main import reviewer_claude_args, reviewer_light_args, reviewer_strict_args

TEST_DIFF = "--- a/tests/test_x.py\n+++ b/tests/test_x.py\n@@ -1 +1 @@\n-a\n+b\n"
HIGH_DIFF = "--- a/netlify/f.js\n+++ b/netlify/f.js\n@@ -1 +1 @@\n-a\n+b\n"
LOW_DIFF = "--- a/shop/m.py\n+++ b/shop/m.py\n@@ -1 +1 @@\n-a\n+b\n"


class FakeClient:
    def __init__(self, claude_bin: str = "claude", extra_args: Any = (), env: Any = None) -> None:
        self.claude_bin = claude_bin
        self.extra_args = list(extra_args)
        self.env = env


class Events:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def record_event(self, kind: str, task_id: str, payload: dict[str, Any]) -> None:
        self.events.append((kind, payload))


class _Config:
    """A real config plus an optional ``review_by_tier`` (absent when ``on`` is None)."""

    def __init__(self, base: FoundryConfig, on: Any) -> None:
        self._base = base
        if on is not None:
            self.review_by_tier = on

    def __getattr__(self, name: str) -> Any:
        return getattr(self._base, name)


def _worker(on: Any = True) -> tuple[ReviewerWorker, Events]:
    events = Events()
    client: Any = FakeClient("claude", ["--x"])
    worker = ReviewerWorker("alpha", events, client, None, None, None, inbox=asyncio.Queue())
    setattr(worker, "config", _Config(FoundryConfig(), on))
    return worker, events


def _item(path: str, diff: str) -> Any:
    return SimpleNamespace(
        ticket_id="T1", submission=SimpleNamespace(modified_files=[path], diff=diff),
    )


def _args(client: Any) -> list[str]:
    return list(client.extra_args)


def test_light_args_have_no_repo_tools() -> None:
    args = reviewer_light_args(FoundryConfig())
    assert "--allowedTools" not in args
    assert args[args.index("--max-turns") + 1] == "3"
    assert args.count("--max-turns") == 1


def test_strict_args_use_opus() -> None:
    args = reviewer_strict_args(FoundryConfig())
    assert args[args.index("--model") + 1] == "opus"
    assert args.count("--model") == 1
    assert reviewer_claude_args(FoundryConfig()) is not args


def test_client_per_tier() -> None:
    worker, events = _worker(True)
    assert worker._tier_client(_item("tests/test_x.py", TEST_DIFF), "T") is not None
    light = worker._tier_client(_item("tests/test_x.py", TEST_DIFF), "T")
    low = worker._tier_client(_item("shop/m.py", LOW_DIFF), "T")
    assert light is low and "--allowedTools" not in _args(light)
    strict = worker._tier_client(_item("netlify/f.js", HIGH_DIFF), "T")
    assert strict is not None and strict is not light
    assert _args(strict)[_args(strict).index("--model") + 1] == "opus"
    assert [p["tier"] for k, p in events.events if k == "review_tier"][:3] == ["tests_docs", "tests_docs", "low"]


def test_normal_tier_uses_usual_client() -> None:
    worker, _ = _worker(True)
    paths = [f"shop/f{i}.py" for i in range(5)]
    item: Any = SimpleNamespace(ticket_id="T", submission=SimpleNamespace(modified_files=paths, diff=LOW_DIFF))
    assert worker._tier_client(item, "T") is None


def test_off_by_default() -> None:
    worker, events = _worker(None)
    assert worker._tier_client(_item("netlify/f.js", HIGH_DIFF), "T") is None
    assert events.events == []


def test_fallback_on_problem() -> None:
    worker, _ = _worker(True)
    bad: Any = SimpleNamespace(ticket_id="T", submission=None)
    assert worker._tier_client(bad, "T") is None
