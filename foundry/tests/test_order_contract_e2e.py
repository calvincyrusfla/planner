"""End-to-end contract test: a real board batch reaches the builder whole.

The board side is the real department code, run with node. The Foundry side is the
real BatchWatcher. The test fails if any order field is lost or cut short on the way.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from foundry.builder_worker import MAX_ORDER_DETAILS_CHARS, _order_details_section
from foundry.floor_manager import WorkOrder
from foundry.order_schema import LIMIT_BUILDER_ORDER
from foundry.tests.test_board_dispatch import SLUG, FakeBoard, _service, _watcher

REPO_ROOT = Path(__file__).resolve().parents[2]
MODULE_URL = (REPO_ROOT / "department" / "lib" / "foundry-batches.mjs").as_uri()

DESCRIPTION_HEAD = "First line of the order.\n    Indented detail: `keep_me(x, y)` stays as written.\n"
BATCH_TITLE = "Order contract batch"
GOAL = "g" * 4500  # long enough that the padded description stays under its own limit
FIRST: dict[str, Any] = {
    "key": "1",
    "title": "Add clamp helper",
    "description": DESCRIPTION_HEAD,
    "files": ["shop/mathx.py"],
    "size": "S",
    "acceptance": ["clamp returns lo below the range", "clamp returns hi above the range"],
    "readFiles": ["shop/strings.py"],
}
SECOND: dict[str, Any] = {
    "key": "2",
    "title": "Add slug helper",
    "description": "Add slugify_full(text).",
    "files": ["shop/slug.py"],
    "size": "S",
    "acceptance": ["spaces become dashes", "the result is lower case"],
    "readFiles": ["shop/report.py"],
    "dependsOn": ["1"],
}
SPEC: dict[str, Any] = {"repo": SLUG, "base": "main", "title": BATCH_TITLE, "goal": GOAL, "subtasks": [FIRST, SECOND]}

SCRIPT = """
import { dispatchBatch, orderTextLength } from '__MODULE__';

const spec = JSON.parse(process.env.ORDER_SPEC);
const target = Number(process.env.ORDER_TARGET);
const first = spec.subtasks[0];
first.description += 'x'.repeat(target - orderTextLength(spec, first));
const state = {};
const batch = dispatchBatch(state, spec);
const length = orderTextLength(batch, batch.subtasks[0]);
if (length !== target) throw new Error('order text is ' + length + ' characters, wanted ' + target);
console.log(JSON.stringify(state.foundryBatches[0]));
"""


def _node() -> str:
    found = shutil.which("node")
    if found is None:
        pytest.skip("node is not on PATH")
    return str(found)


def _run_node(node: str, target: int) -> subprocess.CompletedProcess[str]:
    """Dispatch ``SPEC`` on the real board code, subtask 1 padded to an order text of ``target`` characters."""
    env = {**os.environ, "ORDER_SPEC": json.dumps(SPEC), "ORDER_TARGET": str(target)}
    script = SCRIPT.replace("__MODULE__", MODULE_URL)
    return subprocess.run([node, "--input-type=module", "-e", script], cwd=REPO_ROOT, env=env, capture_output=True,
                          text=True, encoding="utf-8", timeout=60, check=False)


def _accepted_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stored: dict[str, Any]) -> WorkOrder:
    """Run one real watcher round on ``stored`` and return the order the manager accepted."""
    board = FakeBoard()
    board.batches.append(copy.deepcopy(stored))
    orders: list[WorkOrder] = []

    async def go() -> None:
        svc = _service(tmp_path)
        await svc.start()
        try:
            manager = svc.factory.manager
            real_accept = manager.accept

            def accept(order: WorkOrder) -> str:
                orders.append(order)
                return str(real_accept(order))

            monkeypatch.setattr(manager, "accept", accept)
            assert await _watcher(svc, board).poll_once() == [stored["id"]]
        finally:
            await svc.stop()

    asyncio.run(go())
    results = board.get(stored["id"])["results"]
    assert str(results["1"].get("foundryTask")).startswith("F-"), results["1"]
    assert results["2"]["state"] == "queued"  # waits for subtask 1, so only one order is submitted
    assert len(orders) == 1
    return orders[0]


def test_largest_order_reaches_the_builder_whole(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    proc = _run_node(_node(), LIMIT_BUILDER_ORDER)
    assert proc.returncode == 0, proc.stderr
    stored: dict[str, Any] = json.loads(proc.stdout)

    assert (stored["repo"], stored["base"], stored["title"], stored["goal"]) == (SLUG, "main", BATCH_TITLE, GOAL)
    first, second = stored["subtasks"]
    assert first["description"].startswith(DESCRIPTION_HEAD)
    for key in ("key", "title", "files", "size", "acceptance", "readFiles"):
        assert first[key] == FIRST[key]
    for key in ("key", "title", "description", "files", "size", "acceptance", "readFiles", "dependsOn"):
        assert second[key] == SECOND[key]

    order = _accepted_order(tmp_path, monkeypatch, stored)

    assert order.title == FIRST["title"]
    assert first["description"] in order.description
    assert "Requested size: S" in order.description
    assert order.files == FIRST["files"]
    assert FIRST["readFiles"][0] in order.read_files
    assert order.acceptance_criteria == FIRST["acceptance"]
    assert order.pr_base == "main"
    assert len(order.description) <= MAX_ORDER_DETAILS_CHARS
    section = "\n".join(_order_details_section(order.description))
    assert first["description"] in section
    assert "truncated" not in section


def test_one_character_more_is_refused_by_the_board() -> None:
    proc = _run_node(_node(), LIMIT_BUILDER_ORDER + 1)
    assert proc.returncode != 0
    assert f"order is {LIMIT_BUILDER_ORDER + 1} characters" in proc.stderr, proc.stderr
