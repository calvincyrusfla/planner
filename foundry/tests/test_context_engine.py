"""Tests for foundry.context_engine: slicing, relevance, Gemma ranking, trace condensing."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest

from foundry.context_engine import (
    MAX_TRACE_CHARS,
    ContextEngine,
    condense_error_trace,
    estimate_tokens,
    skeletonize_python,
    skeletonize_script,
    trim_objective,
)
from foundry.schemas import (
    MAX_SNIPPET_LINES,
    ContextSlice,
    SubtaskAllocation,
    count_sentences,
)

# --------------------------------------------------------------------------- fixtures

N_FUNCS = 40
BODY_MARKER = "BODY_SECRET_"


def _big_module() -> str:
    """A ~600-line module: every function body contains a unique marker string."""
    parts = [
        '"""Billing helpers for the fixture project.',
        "",
        "This second docstring line must not appear in a slice.",
        '"""',
        "",
        "from __future__ import annotations",
        "",
        "import math",
        "from dataclasses import dataclass, field",
        "",
        "from pydantic import BaseModel, Field",
        "",
        "TAX_RATE = 0.2",
        "",
        "",
        "class Invoice(BaseModel):",
        '    """An invoice."""',
        "",
        "    invoice_id: str = Field(min_length=1)",
        "    lines: list[float] = Field(default_factory=list)",
        "",
        "    def total(self) -> float:",
        '        """Sum of the lines."""',
        f'        marker = "{BODY_MARKER}invoice_total"',
        "        return sum(self.lines)",
        "",
        "",
        "@dataclass(frozen=True)",
        "class LineItem:",
        '    """One line on an invoice."""',
        "",
        "    sku: str",
        "    qty: int = 1",
        "    tags: list[str] = field(default_factory=list)",
        "",
        "",
    ]
    for i in range(N_FUNCS):
        parts += [
            "@staticmethod" if i % 7 == 0 else f"# helper {i}",
            f"def helper_{i}(value: int,",
            "             scale: float = 1.0) -> float:",
            f'    """Helper number {i} for scaling."""',
            f'    marker = "{BODY_MARKER}{i}"',
            "    total = 0.0",
            "    for k in range(value):",
            "        total += math.sqrt(k) * scale",
            "        if total > 1000:",
            "            break",
            "    return total",
            "",
            "",
        ]
    parts += [
        "def compute_invoice_total(invoice: Invoice, *, include_tax: bool = True) -> float:",
        '    """Compute the grand total of an invoice.',
        "",
        "    Long explanation that must be dropped.",
        '    """',
        f'    marker = "{BODY_MARKER}compute"',
        "    subtotal = invoice.total()",
        "    return subtotal * (1 + TAX_RATE) if include_tax else subtotal",
        "",
        "",
        "async def fetch_rates(currency: str) -> dict[str, float]:",
        f'    return {{"{BODY_MARKER}async": 1.0}}',
        "",
    ]
    return "\n".join(parts)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A tiny repo with a big Python module, a TS module and a broken Python file."""
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "billing.py").write_text(_big_module(), encoding="utf-8")
    (tmp_path / "app" / "other.py").write_text(
        "def unrelated_thing(x):\n    return x  # OTHER_BODY\n\n\ndef parse_config(path: str) -> dict:\n"
        "    return {}  # OTHER_BODY\n",
        encoding="utf-8",
    )
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "api.ts").write_text(
        "import { Invoice } from './types';\n"
        "export interface RateTable {\n"
        "  currency: string;\n"
        "  rate: number;\n"
        "}\n"
        "export type Mode = 'net' | 'gross';\n"
        "export async function loadRates(currency: string,\n"
        "    mode: Mode): Promise<RateTable> {\n"
        "  const TS_BODY_SECRET = await fetch('/rates');\n"
        "  return TS_BODY_SECRET.json();\n"
        "}\n"
        "export const formatTotal = (n: number): string => {\n"
        "  return `TS_BODY_SECRET ${n}`;\n"
        "};\n"
        "export class RateCache {\n"
        "  private store = new Map<string, number>();\n"
        "  get(key: string): number | undefined {\n"
        "    if (key) { return this.store.get('TS_BODY_SECRET'); }\n"
        "    return undefined;\n"
        "  }\n"
        "}\n"
        "function internalHelper() {\n"
        "  return 'TS_BODY_SECRET';\n"
        "}\n",
        encoding="utf-8",
    )
    (tmp_path / "app" / "broken.py").write_text(
        "import os\n\ndef ok(a):\n    return a  # BROKEN_BODY\n\ndef bad(:\n    pass\n", encoding="utf-8"
    )
    return tmp_path


def _all_text(sl: ContextSlice) -> str:
    return "\n".join(s.text for s in sl.snippets)


# --------------------------------------------------------------------------- python slicing


def test_big_fixture_bodies_never_appear(repo: Path) -> None:
    engine = ContextEngine(repo)
    sub = SubtaskAllocation(
        subtask_id="T-1.1",
        title="Fix tax in compute_invoice_total",
        acceptance_criteria=["`compute_invoice_total` applies TAX_RATE only when include_tax is set"],
        allowed_files=["app/billing.py"],
    )
    sl = engine.build_slice(sub, "Fix the invoice total.")
    text = _all_text(sl)
    assert BODY_MARKER not in text
    assert "math.sqrt" not in text
    assert "Long explanation" not in text
    assert "This second docstring line" not in text
    assert all(s.line_count <= MAX_SNIPPET_LINES for s in sl.snippets)
    assert "def compute_invoice_total(invoice: Invoice, *, include_tax: bool = True) -> float:" in text
    assert '"""Compute the grand total of an invoice."""' in text
    assert "TAX_RATE = 0.2" in text
    assert "from pydantic import BaseModel, Field" in text
    source_lines = len(_big_module().splitlines())
    assert sl.source_line_count == source_lines
    assert sl.snippet_line_count < source_lines / 4
    stats = engine.last_stats
    assert stats is not None and stats.tokens_saved > 0
    assert stats.tokens_saved == stats.source_tokens - stats.slice_tokens


def test_line_numbers_preserved(repo: Path) -> None:
    source = _big_module().splitlines()
    engine = ContextEngine(repo)
    sl = engine.build_slice(allowed_files=["app/billing.py"], objective="Touch compute_invoice_total.")
    snippet = next(s for s in sl.snippets if "compute_invoice_total" in s.text)
    def_line = next(i for i, ln in enumerate(source, 1) if ln.startswith("def compute_invoice_total"))
    assert snippet.start_line == def_line
    assert snippet.end_line >= def_line + 7
    for row in snippet.text.splitlines():
        m = re.match(r"^\s*(\d+)\| (.*)$", row)
        if m and '"""' not in m.group(2):  # numbered code rows are the original line verbatim
            assert source[int(m.group(1)) - 1].rstrip() == m.group(2)


def test_unnumbered_mode_and_skeleton_shape() -> None:
    symbols = skeletonize_python("app/billing.py", _big_module())
    by_name = {s.name: s for s in symbols}
    assert by_name["<imports>"].kind == "imports"
    inv = by_name["Invoice"].text
    assert "invoice_id: str = Field(min_length=1)" in inv
    assert "lines: list[float] = Field(default_factory=list)" in inv
    assert "def total(self) -> float:" in inv and BODY_MARKER not in inv
    li = by_name["LineItem"].text
    assert li.startswith("@dataclass(frozen=True)")
    assert "sku: str" in li and "qty: int = 1" in li
    h0 = by_name["helper_0"].text.splitlines()
    assert h0[0] == "@staticmethod"
    assert h0[1:3] == ["def helper_0(value: int,", "             scale: float = 1.0) -> float:"]
    assert h0[-1] == "    ..."
    assert by_name["fetch_rates"].text.startswith("async def fetch_rates(currency: str)")


def test_large_class_split_into_capped_snippets(tmp_path: Path) -> None:
    fields = "\n".join(f"    field_{i}: int = {i}" for i in range(120))
    (tmp_path / "models.py").write_text(f"class Huge:\n    \"\"\"Many fields.\"\"\"\n{fields}\n", encoding="utf-8")
    sl = ContextEngine(tmp_path).build_slice(allowed_files=["models.py"], objective="Edit Huge.", max_total_lines=500)
    huge = [s for s in sl.snippets if s.path == "models.py"]
    assert len(huge) >= 3
    assert all(s.line_count <= MAX_SNIPPET_LINES for s in huge)
    assert huge[0].start_line == 1 and huge[-1].end_line == 122
    assert [s.start_line for s in huge] == sorted(s.start_line for s in huge)


def test_syntax_error_falls_back_to_outline(repo: Path) -> None:
    sl = ContextEngine(repo).build_slice(allowed_files=["app/broken.py"], objective="Fix bad.")
    text = _all_text(sl)
    assert "def ok(a):" in text and "def bad(:" in text
    assert "BROKEN_BODY" not in text


# --------------------------------------------------------------------------- ts / js


def test_typescript_signatures(repo: Path) -> None:
    symbols = skeletonize_script("web/api.ts", (repo / "web" / "api.ts").read_text())
    names = {s.name: s for s in symbols}
    assert {"RateTable", "Mode", "loadRates", "formatTotal", "RateCache", "internalHelper"} <= set(names)
    assert "currency: string;" in names["RateTable"].text
    assert names["loadRates"].text.endswith("mode: Mode): Promise<RateTable> { ... }")
    assert names["formatTotal"].text == "export const formatTotal = (n: number): string => ..."
    assert "get(key: string): number | undefined { ... }" in names["RateCache"].text
    assert (names["loadRates"].start_line, names["loadRates"].end_line) == (7, 11)
    sl = ContextEngine(repo).build_slice(allowed_files=["web/api.ts"], objective="Change loadRates and RateCache.")
    text = _all_text(sl)
    assert "TS_BODY_SECRET" not in text
    assert "import { Invoice } from './types';" in text


def test_js_department_core_is_sliced() -> None:
    root = Path(__file__).resolve().parents[2]
    core = root / "department" / "lib" / "core.mjs"
    if not core.exists():
        pytest.skip("department/lib/core.mjs not present")
    symbols = skeletonize_script("department/lib/core.mjs", core.read_text(encoding="utf-8"))
    assert any(s.name == "crewFor" and s.kind == "function" for s in symbols)
    assert all("{ ... }" in s.text or s.kind != "function" or "=>" in s.text for s in symbols)


# --------------------------------------------------------------------------- relevance & limits


def test_relevance_prefers_matching_symbols(repo: Path) -> None:
    engine = ContextEngine(repo)
    sl = engine.build_slice(
        objective="Make parse_config read TOML.",
        acceptance_criteria=["parse_config returns a dict of settings"],
        allowed_files=["app/billing.py", "app/other.py"],
        max_snippets=3,
    )
    text = _all_text(sl)
    assert "def parse_config(path: str) -> dict:" in text
    assert "helper_" not in text
    assert "OTHER_BODY" not in text


def test_no_match_gives_outline_within_budget(repo: Path) -> None:
    engine = ContextEngine(repo)
    sl = engine.build_slice(objective="Do something vague.", allowed_files=["app/billing.py"], max_snippets=5, max_total_lines=60)
    assert 1 <= len([s for s in sl.snippets if "<imports>" not in s.text]) <= 6
    assert sl.snippet_line_count <= 60 + MAX_SNIPPET_LINES


def test_paths_outside_repo_and_missing_files_are_ignored(repo: Path, tmp_path_factory: pytest.TempPathFactory) -> None:
    outside = tmp_path_factory.mktemp("outside") / "secret.py"
    outside.write_text("def leak():\n    return 'LEAK'\n", encoding="utf-8")
    engine = ContextEngine(repo)
    sl = engine.build_slice(objective="x.", allowed_files=[str(outside), "../secret.py", "app/new_file.py"])
    assert sl.snippets == []
    assert sl.source_line_count == 0


def test_glob_allowed_files(repo: Path) -> None:
    engine = ContextEngine(repo)
    assert engine.expand_files(["app/*.py"]) == ["app/billing.py", "app/broken.py", "app/other.py"]


def test_objective_trimmed_to_two_sentences(repo: Path) -> None:
    long = "First sentence here. Second one! Third? Fourth sentence."
    assert trim_objective(long) == "First sentence here. Second one!"
    assert trim_objective("  one   line  ") == "one line"
    sl = ContextEngine(repo).build_slice(objective=long, allowed_files=["app/other.py"])
    assert count_sentences(sl.objective) == 2
    sub = SubtaskAllocation(subtask_id="s", title="Title only", acceptance_criteria=["c1"])
    sl2 = ContextEngine(repo).build_slice(sub)
    assert sl2.objective == "Title only" and sl2.acceptance_criteria == ["c1"]


def test_estimate_tokens() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcde") == 2


# --------------------------------------------------------------------------- gemma ranking


def _sub() -> SubtaskAllocation:
    return SubtaskAllocation(
        subtask_id="T-1.2", title="Tune helpers", acceptance_criteria=["helpers scale"], allowed_files=["app/billing.py"]
    )


def test_gemma_ranking_is_used_when_available(repo: Path) -> None:
    from foundry.gemma_client import SimulatedGemmaClient

    def responder(request):  # picks helper_33 by its catalogue id
        listing = request.messages[-1]["content"]
        sid = re.search(r"^(S\d+) \S+ function helper_33:", listing, re.MULTILINE).group(1)
        return json.dumps({"ranked": [sid, "S999"]})

    gemma = SimulatedGemmaClient(responder)
    engine = ContextEngine(repo, gemma)
    sl = asyncio.run(engine.abuild_slice(_sub(), max_snippets=1))
    assert gemma.calls == 1
    assert engine.last_stats is not None and engine.last_stats.ranked_by_gemma
    assert "def helper_33(" in _all_text(sl)
    assert BODY_MARKER not in _all_text(sl)
    assert gemma.requests[0].json_schema is not None


@pytest.mark.parametrize("mode", ["unavailable", "garbage", "none"])
def test_gemma_failure_falls_back(repo: Path, mode: str) -> None:
    from foundry.gemma_client import SimulatedGemmaClient

    gemma = None
    if mode == "unavailable":
        gemma = SimulatedGemmaClient(unavailable=True)
    elif mode == "garbage":
        gemma = SimulatedGemmaClient(lambda request: "not json at all")
    engine = ContextEngine(repo, gemma)
    expected = engine.build_slice(_sub())
    got = asyncio.run(engine.abuild_slice(_sub()))
    assert got == expected
    assert engine.last_stats is not None and not engine.last_stats.ranked_by_gemma


# --------------------------------------------------------------------------- error traces

PYTEST_LONG = """\
============================= test session starts ==============================
collected 2 items

tests/test_calc.py F.                                                    [100%]

=================================== FAILURES ===================================
___________________________________ test_neg ___________________________________

    def test_neg():
>       add(-1, 2)

tests/test_calc.py:5:
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _

a = -1, b = 2

    def add(a, b):
        if a < 0:
>           raise ValueError("negative input not allowed")
E           ValueError: negative input not allowed

pkg/calc.py:3: ValueError
=========================== short test summary info ============================
FAILED tests/test_calc.py::test_neg - ValueError: negative input not allowed
1 failed, 1 passed in 0.02s
"""

PYTEST_SHORT_TWO = """\
=================================== FAILURES ===================================
___________________________________ test_add ___________________________________
tests/test_calc.py:3: in test_add
    assert add(1, 2) == 3
E   assert 4 == 3
E    +  where 4 = add(1, 2)
___________________________________ test_neg ___________________________________
tests/test_calc.py:5: in test_neg
    add(-1, 2)
pkg/calc.py:3: in add
    raise ValueError("negative input not allowed")
E   ValueError: negative input not allowed
=========================== short test summary info ============================
FAILED tests/test_calc.py::test_add - assert 4 == 3
FAILED tests/test_calc.py::test_neg - ValueError: negative input not allowed
"""

PY_TRACEBACK = """\
Traceback (most recent call last):
  File "/repo/foundry/main.py", line 10, in <module>
    run()
  File "/repo/foundry/worker.py", line 42, in run
    json.loads(payload)
  File "/usr/lib/python3.13/json/__init__.py", line 346, in loads
    return _default_decoder.decode(s)
  File "/usr/lib/python3.13/json/decoder.py", line 355, in raw_decode
    raise JSONDecodeError("Expecting value", s, err.value) from None
json.decoder.JSONDecodeError: Expecting value: line 1 column 1 (char 0)
"""

NODE_STACK = """\
/repo/web/n.js:2
function g() { return o.prop; }
                        ^

TypeError: Cannot read properties of undefined (reading 'prop')
    at g (/repo/web/n.js:2:25)
    at Object.<anonymous> (/repo/web/n.js:3:1)
    at Module._compile (node:internal/modules/cjs/loader:1781:14)

Node.js v22.23.4
"""


@pytest.mark.parametrize(
    ("trace", "expected"),
    [
        (PYTEST_LONG, "pkg/calc.py:3: ValueError: negative input not allowed"),
        (PYTEST_SHORT_TWO, "tests/test_calc.py:3: AssertionError: assert 4 == 3"),
        (PY_TRACEBACK, "foundry/worker.py:42: json.decoder.JSONDecodeError: Expecting value: line 1 column 1 (char 0)"),
        (NODE_STACK, "web/n.js:2: TypeError: Cannot read properties of undefined (reading 'prop')"),
        (
            "app/x.py:1:8: F401 [*] `os` imported but unused\napp/x.py:9:1: E722 Do not use bare `except`\nFound 2 errors.\n",
            "app/x.py:1: F401 `os` imported but unused (+1 more)",
        ),
        ("F401 [*] `os` imported but unused\n --> app/x.py:1:8\n  |\n1 | import os\n", "app/x.py:1: F401 `os` imported but unused"),
        (
            "FAILED tests/test_a.py::test_one - KeyError: 'x'\nFAILED tests/test_a.py::test_two\n",
            "tests/test_a.py::test_one: KeyError: 'x' (+1 more)",
        ),
        (
            '  File "app/x.py", line 3\n    def bad(:\n            ^\nSyntaxError: invalid syntax\n',
            "app/x.py:3: SyntaxError: invalid syntax",
        ),
    ],
)
def test_condense_error_trace(trace: str, expected: str) -> None:
    root = "/repo"
    assert condense_error_trace(trace, repo_root=root) == expected


def test_condense_is_single_line_and_capped() -> None:
    trace = PY_TRACEBACK.replace("Expecting value", "Expecting value " + "x" * 400).replace("\n", "\r\n")
    out = condense_error_trace("\x1b[31m" + trace + "\x1b[0m")
    assert "\n" not in out and "\r" not in out
    assert len(out) <= MAX_TRACE_CHARS
    assert out.startswith("/repo/foundry/worker.py:42: json.decoder.JSONDecodeError")


@pytest.mark.parametrize("trace", ["", "   \n", "something went wrong somewhere", "\x00\x01 weird ::: bytes"])
def test_condense_never_raises(trace: str) -> None:
    out = condense_error_trace(trace)
    assert out and "\n" not in out and len(out) <= MAX_TRACE_CHARS


def test_engine_condense_relativises_paths(tmp_path: Path) -> None:
    trace = f'Traceback (most recent call last):\n  File "{tmp_path}/pkg/m.py", line 7, in f\n    x()\nNameError: name \'x\' is not defined\n'
    out = ContextEngine(tmp_path).condense_error_trace(trace)
    assert out == "pkg/m.py:7: NameError: name 'x' is not defined"
