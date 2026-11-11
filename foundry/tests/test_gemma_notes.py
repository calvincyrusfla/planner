"""Tests for the advisory Gemma pitfall notes and PR briefs."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from foundry.gemma_client import GemmaReply, GemmaUnavailable
from foundry.gemma_notes import Hit, find_related, pitfall_notes, pr_brief


class FakeGemma:
    """Answers with fixed text, raises ``error`` or sleeps past the timeout."""

    def __init__(self, answer: Any = "", *, error: Exception | None = None, delay: float = 0.0) -> None:
        self.text = answer if isinstance(answer, str) else json.dumps(answer)
        self.error = error
        self.delay = delay
        self.requests: list[dict[str, Any]] = []

    async def chat(
        self,
        messages: list[dict],
        *,
        json_schema: dict | None = None,
        temperature: float = 0.1,
        timeout: float | None = None,
    ) -> GemmaReply:
        self.requests.append({"messages": messages, "json_schema": json_schema, "timeout": timeout})
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return GemmaReply(text=self.text)

    @property
    def prompt(self) -> str:
        return "\n".join(str(m["content"]) for m in self.requests[-1]["messages"])


class NoTimeoutGemma:
    """A client whose ``chat`` predates the ``timeout`` keyword."""

    def __init__(self, answer: Any) -> None:
        self.text = json.dumps(answer)

    async def chat(
        self, messages: list[dict], *, json_schema: dict | None = None, temperature: float = 0.1
    ) -> GemmaReply:
        return GemmaReply(text=self.text)


def write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def by_line(hits: list[Hit], path: str) -> dict[int, str]:
    return {hit.line: hit.text for hit in hits if hit.path == path}


class TestFindRelated:
    def test_importing_test_fake_definition_and_exact_list(self, tmp_path: Path) -> None:
        write(tmp_path, "pkg/mod.py", "def run():\n    pass\n")
        write(
            tmp_path,
            "pkg/tests/test_mod.py",
            "from pkg.mod import other\n"
            "\n"
            "class Fake:\n"
            "    async def run(self, a):\n"
            "        return a\n"
            "\n"
            "def helper(x):\n"
            "    return x\n"
            "\n"
            "def test_it():\n"
            "    assert other() == ['run', 'stop']\n",
        )
        hits = find_related(tmp_path, ["pkg/mod.py"], ["run"])
        assert by_line(hits, "pkg/tests/test_mod.py") == {
            1: "from pkg.mod import other",
            4: "async def run(self, a):",
            11: "assert other() == ['run', 'stop']",
        }

    def test_only_test_files_are_searched(self, tmp_path: Path) -> None:
        write(tmp_path, "pkg/other.py", "from pkg.mod import run\ndef run():\n    pass\n")
        write(tmp_path, "node_modules/tests/test_x.py", "def run():\n    pass\n")
        assert find_related(tmp_path, ["pkg/mod.py"], ["run"]) == []

    def test_unrelated_test_file_gives_no_hits(self, tmp_path: Path) -> None:
        write(tmp_path, "tests/test_a.py", "import json\n\ndef running():\n    assert json.loads('[]') == ['runner']\n")
        assert find_related(tmp_path, ["pkg/mod.py"], ["run"]) == []

    def test_import_forms(self, tmp_path: Path) -> None:
        write(tmp_path, "tests/test_a.py", "import pkg.mod as m\n")
        write(tmp_path, "tests/test_b.py", "from pkg import (\n    mod,\n)\n")
        write(tmp_path, "pkg/tests/test_c.py", "from ..mod import thing\n")
        hits = find_related(tmp_path, ["pkg/mod.py"], [])
        assert [(hit.path, hit.line) for hit in hits] == [
            ("pkg/tests/test_c.py", 1),
            ("tests/test_a.py", 1),
            ("tests/test_b.py", 1),
        ]

    def test_exact_tuple_multiline_list_and_unittest_call(self, tmp_path: Path) -> None:
        write(
            tmp_path,
            "tests/test_a.py",
            "def test_one(self):\n"
            "    assert names() == ('alpha',)\n"
            "    assert names() == [\n"
            "        'alpha',\n"
            "        'beta',\n"
            "    ]\n"
            "    self.assertEqual(names(), ['alpha'])\n",
        )
        found = by_line(find_related(tmp_path, [], ["alpha"]), "tests/test_a.py")
        assert found == {
            2: "assert names() == ('alpha',)",
            3: "assert names() == [ 'alpha', 'beta', ]",
            7: "self.assertEqual(names(), ['alpha'])",
        }

    def test_calls_membership_and_inequality_are_not_exact_lists(self, tmp_path: Path) -> None:
        write(
            tmp_path,
            "tests/test_a.py",
            "x = 1\nassert 'alpha' in ['alpha']\nassert f(alpha) == 3\nassert y != ['alpha']\n",
        )
        hits = find_related(tmp_path, [], ["alpha"])
        assert [hit.line for hit in hits] == [2]  # only the first mention of the name

    def test_mjs_file_import_definition_and_exact_lists(self, tmp_path: Path) -> None:
        write(
            tmp_path,
            "department/tests/core.test.mjs",
            "import assert from 'node:assert';\n"
            "import { assign } from '../lib/core.mjs';\n"
            "function assignTeam(a) { return a; }\n"
            "\n"
            "assert.deepStrictEqual(assigned(), ['assignTeam', 'other']);\n"
            "assert.deepEqual(assigned(), [\n"
            "  'assignTeam',\n"
            "]);\n"
            "expect(assigned()).toEqual(['assignTeam']);\n"
            "assert.deepStrictEqual(lookup[assignTeam], 1);\n",
        )
        hits = find_related(tmp_path, ["department/lib/core.mjs"], ["assignTeam"])
        assert by_line(hits, "department/tests/core.test.mjs") == {
            2: "import { assign } from '../lib/core.mjs';",
            3: "function assignTeam(a) { return a; }",
            5: "assert.deepStrictEqual(assigned(), ['assignTeam', 'other']);",
            6: "assert.deepEqual(assigned(), [ 'assignTeam', ]);",
            9: "expect(assigned()).toEqual(['assignTeam']);",
        }
        assert [hit.line for hit in hits] == [2, 3, 5, 6, 9]

    def test_cap_keeps_the_strongest_hits_in_path_order(self, tmp_path: Path) -> None:
        for number in range(30):
            write(tmp_path, f"tests/test_a{number:02}.py", "x = 'run'\n")
        write(tmp_path, "tests/test_z.py", "def run():\n    pass\n" * 20)
        hits = find_related(tmp_path, [], ["run"])
        assert len(hits) == 40
        assert sum(hit.path == "tests/test_z.py" for hit in hits) == 20
        assert hits == sorted(hits, key=lambda hit: (hit.path, hit.line))

    def test_invalid_utf8_and_broken_python_are_tolerated(self, tmp_path: Path) -> None:
        path = tmp_path / "tests" / "test_a.py"
        path.parent.mkdir()
        path.write_bytes(b"# \xff\xfe\ndef run(:\ndef run():\n    pass\n")
        hits = find_related(tmp_path, [], ["run"])
        assert [hit.line for hit in hits] == [2, 3]


HITS = [Hit("tests/test_a.py", 4, "def run(self):"), Hit("tests/test_a.py", 9, "assert x == ['run']")]


def notes(gemma: Any, hits: list[Hit] = HITS) -> list[str]:
    return asyncio.run(pitfall_notes(gemma, "change run", hits, timeout=1.0))


class TestPitfallNotes:
    def test_keeps_grounded_notes_and_drops_the_rest(self) -> None:
        gemma = FakeGemma(
            {
                "notes": [
                    {"path": "tests/test_a.py", "line": 4, "note": "older\nsignature"},
                    {"path": "tests/test_a.py", "line": 5, "note": "wrong line"},
                    {"path": "tests/other.py", "line": 9, "note": "wrong file"},
                    {"path": "tests/test_a.py", "line": "9", "note": "line is not a number"},
                    {"path": "tests/test_a.py", "line": 9, "note": "   "},
                    "not an object",
                ]
            }
        )
        assert notes(gemma) == ["tests/test_a.py:4: older signature"]
        assert gemma.requests[0]["timeout"] == 1.0
        assert "notes" in gemma.requests[0]["json_schema"]["properties"]
        assert "tests/test_a.py:9: assert x == ['run']" in gemma.prompt

    def test_cuts_notes_and_limits_their_number(self) -> None:
        hits = [Hit("t.py", number, "x") for number in range(1, 9)]
        gemma = FakeGemma({"notes": [{"path": "t.py", "line": n, "note": "n" * 300} for n in range(1, 9)]})
        result = notes(gemma, hits)
        assert len(result) == 5
        assert result[0] == "t.py:1: " + "n" * 200

    def test_client_without_timeout_keyword(self) -> None:
        answer = {"notes": [{"path": "tests/test_a.py", "line": 4, "note": "n"}]}
        assert notes(NoTimeoutGemma(answer)) == ["tests/test_a.py:4: n"]

    def test_no_hits_skips_gemma(self) -> None:
        gemma = FakeGemma({"notes": []})
        assert notes(gemma, []) == []
        assert gemma.requests == []

    def test_failures_return_nothing(self) -> None:
        assert notes(FakeGemma(error=GemmaUnavailable("down"))) == []
        assert notes(FakeGemma(error=RuntimeError("boom"))) == []
        assert notes(FakeGemma("")) == []
        assert notes(FakeGemma("not json")) == []
        assert notes(FakeGemma("[1]")) == []
        assert notes(None) == []

    def test_timeout_returns_nothing(self) -> None:
        gemma = FakeGemma({"notes": []}, delay=5.0)
        assert asyncio.run(pitfall_notes(gemma, "o", HITS, timeout=0.01)) == []


CRITERIA = ["first criterion", "second criterion", "third criterion"]
CHANGED = ["a.py", "b.py"]


def brief(gemma: Any, diff: str = "diff", criteria: list[str] = CRITERIA) -> str:
    return asyncio.run(pr_brief(gemma, "order", criteria, CHANGED, diff, timeout=1.0))


class TestPrBrief:
    def test_maps_criteria_marks_unlocated_and_drops_unknown_files(self) -> None:
        gemma = FakeGemma(
            {
                "criteria": [
                    {"index": 0, "files": ["a.py", "ghost.py", "b.py", "a.py"]},
                    {"index": 1, "files": ["ghost.py"]},
                    {"index": 7, "files": ["a.py"]},
                    {"index": True, "files": ["b.py"]},
                ],
                "unusual": [],
            }
        )
        assert brief(gemma).split("\n") == [
            "✓ first criterion — a.py, b.py",
            "? second criterion — not located",
            "? third criterion — not located",
        ]

    def test_cuts_criteria_and_unusual_lines(self) -> None:
        gemma = FakeGemma(
            {"criteria": [{"index": 0, "files": ["a.py"]}], "unusual": ["u" * 200, "", "two", "three", "four"]}
        )
        assert brief(gemma, criteria=["c" * 100]).split("\n") == [
            f"✓ {'c' * 80} — a.py",
            f"! {'u' * 150}",
            "! two",
            "! three",
        ]

    def test_sends_at_most_12000_characters_of_diff(self) -> None:
        gemma = FakeGemma({"criteria": [{"index": 0, "files": ["a.py"]}], "unusual": []})
        brief(gemma, diff="§" * 20_000)
        assert gemma.prompt.count("§") == 12_000

    def test_failures_return_empty(self) -> None:
        assert brief(FakeGemma(error=GemmaUnavailable("down"))) == ""
        assert brief(FakeGemma(error=RuntimeError("boom"))) == ""
        assert brief(FakeGemma("")) == ""
        assert brief(FakeGemma("not json")) == ""
        assert brief(FakeGemma("{}")) == ""
        assert brief(FakeGemma({"criteria": [], "unusual": []})) == ""
        assert brief(None) == ""

    def test_timeout_returns_empty(self) -> None:
        gemma = FakeGemma({"criteria": [], "unusual": []}, delay=5.0)
        assert asyncio.run(pr_brief(gemma, "o", CRITERIA, CHANGED, "d", timeout=0.01)) == ""
