"""PR digest: plain-code digest in the PR description in place of the Gemma brief."""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from foundry.floor_manager import FloorManager, WorkOrder
from foundry.pr_digest import MAX_DIGEST_CHARS, build_digest

PATCH = (
    "diff --git a/shop/mathx.py b/shop/mathx.py\n"
    "--- a/shop/mathx.py\n+++ b/shop/mathx.py\n"
    "@@ -1,1 +1,2 @@\n x = 1\n+y = 2\n"
    "diff --git a/foundry/tests/test_mathx.py b/foundry/tests/test_mathx.py\n"
    "new file mode 100644\n--- /dev/null\n+++ b/foundry/tests/test_mathx.py\n"
    "@@ -0,0 +1,2 @@\n+def test_a():\n+    assert True\n"
)


class FakeQueue:
    def __init__(self, events: list[dict[str, Any]]) -> None:
        self._events = events

    def events(self, **kwargs: Any) -> list[dict[str, Any]]:
        tid = kwargs.get("task_id")
        return [e for e in self._events if tid is None or e["task_id"] == tid]


class FakeTelemetry:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.rows = rows

    def _query(self, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        return self.rows


def _events() -> list[dict[str, Any]]:
    def ev(kind: str, **pl: Any) -> dict[str, Any]:
        return {"id": 1, "ts": "t", "kind": kind, "task_id": "F-1", "payload": pl}

    return [
        ev("gate_result", check_type="syntax", passed=True),
        ev("gate_result", check_type="linter", passed=True),
        ev("gate_result", check_type="typecheck", passed=False),
        ev("builder_session", attempt=1, session=1),
        ev("builder_session", attempt=1, session=2),
        ev("builder_session", attempt=2, session=1),
    ]


def _manager() -> Any:
    return SimpleNamespace(
        queue=FakeQueue(_events()),
        telemetry=FakeTelemetry([("builder", 100, 50, 7), ("reviewer", 10, 5, 0)]),
    )


def test_digest_content() -> None:
    text = build_digest(_manager(), "F-1", PATCH)
    assert "Risk tier: " in text
    assert "2 file(s), +3 -0" in text
    assert "`foundry/tests/test_mathx.py`" in text and "`shop/mathx.py`" not in text
    assert "Gates passed: syntax, linter" in text and "typecheck" not in text
    assert "Attempts: 2; builder sessions: 3" in text
    assert "builder in 100 / out 50 / cache 7" in text
    assert "reviewer in 10 / out 5 / cache 0" in text


def test_digest_is_capped() -> None:
    mgr = SimpleNamespace(
        queue=FakeQueue([]),
        telemetry=FakeTelemetry([(f"role{'x' * 300}{i}", 1, 1, 1) for i in range(30)]),
    )
    assert len(build_digest(mgr, "F-1", PATCH)) <= MAX_DIGEST_CHARS


def _fm(tmp_path: Path, *, digest: bool, template: bool, gemma: Any = None) -> Any:
    patch = tmp_path / "F-1.patch"
    patch.write_text(PATCH, encoding="utf-8")
    fm = object.__new__(FloorManager)
    fm.pr_summaries = {"F-1": {"commit_message": "Add y\n", "pr_body": "Body text"}}
    fm.orders = {"F-1": WorkOrder(title="Add y", description="d")}
    fm.task_bases = {}
    fm.pr_briefs = {}
    fm.pr_template = template
    fm.pr_digest = digest
    fm.pr_draft = False
    fm.gemma_notes = True
    fm.gemma_notes_timeout_s = 1.0
    fm.foreman = SimpleNamespace(gemma=gemma)
    fm.queue = FakeQueue(_events())
    fm.telemetry = FakeTelemetry([("builder", 1, 2, 3)])
    fm._pr_patch = lambda tid: patch  # type: ignore[assignment,method-assign]
    return fm


def test_template_digest_replaces_brief(tmp_path: Path) -> None:
    fm = _fm(tmp_path, digest=True, template=True)
    fm.pr_briefs["F-1"] = "GEMMA BRIEF"
    body = fm.pr_request("F-1")["body"]
    assert "## Digest" in body and "Brief (Gemma" not in body and "GEMMA BRIEF" not in body
    assert body.index("## Digest") < body.index("Opened by Foundry")


def test_plain_digest_before_footer(tmp_path: Path) -> None:
    body = _fm(tmp_path, digest=True, template=False).pr_request("F-1")["body"]
    assert body.index("Body text") < body.index("## Digest") < body.index("Opened by Foundry")


def test_gemma_not_called_with_digest(tmp_path: Path) -> None:
    calls: list[Any] = []

    class Gemma:
        async def chat(self, *a: Any, **k: Any) -> Any:
            calls.append(a)
            return "x"

    fm = _fm(tmp_path, digest=True, template=False, gemma=Gemma())
    asyncio.run(fm._make_brief("F-1", tmp_path / "F-1.patch"))
    assert not calls and not fm.pr_briefs


def test_digest_off_is_todays_body(tmp_path: Path) -> None:
    fm = _fm(tmp_path, digest=False, template=False)
    fm.pr_briefs["F-1"] = "GEMMA BRIEF"
    body = fm.pr_request("F-1")["body"]
    assert "## Digest" not in body
    assert body == "Body text\n\n## Brief (Gemma, advisory)\n\nGEMMA BRIEF\n\n---\nOpened by Foundry (task F-1)\n"
