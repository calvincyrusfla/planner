"""Order compiler: resolve names in a dispatch batch against the code library.

Run before dispatch: ``python -m foundry.order_compiler BATCH.json [--repo PATH]
[--write OUT.json] [--json]``. Works only from a code library built into a
temporary file: no Claude, no Gemma, no network. Findings are advice; the exit
code is 0 unless the batch cannot be read.
"""

from __future__ import annotations

import argparse
import builtins
import copy
import datetime
import json
import keyword
import re
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from foundry.code_library import Library, build_library
from foundry.code_library import _is_test_path as _is_test_path

__all__ = ["compile_batch", "extract_names", "main"]

FACT_CAP = 3000
MAX_READ_FILES = 5
MAX_CALLERS = 3
MAX_TESTS = 3
SIZE_LIMIT = 600
MIN_NAME = 3

_NAME_RE = re.compile(r"`([A-Za-z_][A-Za-z0-9_.]*(?:\(\))?)`")
_FILE_EXT = {
    "py", "mjs", "js", "cjs", "ts", "json", "md", "html", "css", "bat", "txt",
    "toml", "yml", "yaml", "cfg", "ini", "sh", "sql", "db", "lock", "csv",
}
_CHANGE_RE = re.compile(r"rename|remov|delet|signature|move", re.IGNORECASE)
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+|\n")

MAX_CANDIDATES = 5

_SKIP_WORDS = (
    set(keyword.kwlist)
    | set(keyword.softkwlist)
    | set(dir(builtins))
    | {"self", "cls", "true", "false", "null", "undefined"}
)
_SETTING_RE = re.compile(r"^(?:FOUNDRY_[A-Z0-9_]+|[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)$")

Entry = dict[str, Any]


def _is_file_path(name: str) -> bool:
    return "." in name and name.rsplit(".", 1)[-1].lower() in _FILE_EXT


def extract_names(text: str) -> list[str]:
    """Backticked identifiers in ``text`` (unique, in order), without plain file paths."""
    out: list[str] = []
    for raw in _NAME_RE.findall(text):
        name = raw[:-2] if raw.endswith("()") else raw
        if len(name) < MIN_NAME or _is_file_path(name) or name in out:
            continue
        out.append(name)
    return out


def _qname(entry: Entry) -> str:
    return str(entry["call"]).split("::", 1)[-1]


def _resolve(lib: Library, name: str) -> list[Entry]:
    dotted = "." in name
    found = lib.find(name, limit=1000)
    out: list[Entry] = []
    for e in found:
        q = _qname(e)
        if (q == name) if dotted else (q.rsplit(".", 1)[-1] == name):
            out.append(e)
    return out


def _narrow(hits: list[Entry], files: list[str], read_files: list[str]) -> list[Entry]:
    """Drop test-file candidates (unless the piece edits tests), then prefer its own files."""
    if not any(_is_test_path(f) for f in files):
        kept = [h for h in hits if not _is_test_path(str(h["path"]))]
        hits = kept or hits
    for group in (files, read_files):
        own = [h for h in hits if h["path"] in group]
        if own:
            return own
    return hits


def _as_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, str) and value.strip():
        return [value]
    return []


def _piece_text(sub: dict[str, Any]) -> str:
    acceptance = sub.get("acceptance", sub.get("acceptance_criteria"))
    return "\n".join([str(sub.get("description") or ""), *_as_list(acceptance)])


def _key(sub: dict[str, Any], index: int) -> str:
    return str(sub.get("key") or "").strip() or str(index + 1)


def _fact_sheet(lib: Library, resolved: list[Entry], files: set[str]) -> str:
    lines: list[str] = []
    for e in resolved:
        lines.append(f"{e['call']}  {e['path']}:{e['start']}-{e['end']}  {e['signature']}")
        callers = [c for c in lib.callers(e["call"], limit=200) if c["path"] not in files]
        for c in callers[:MAX_CALLERS]:
            lines.append(f"  caller {c['path']}:{c['line']}: {c['text']}")
        for t in lib.tests_for(e["call"], limit=MAX_TESTS):
            lines.append(f"  test {t['path']}:{t['line']}: {t['text']}")
    text = "\n".join(lines)
    if len(text) > FACT_CAP:
        note = "\n(facts cut at 3000 characters)"
        text = text[: FACT_CAP - len(note)].rstrip() + note
    return text


def _dep_graph(subs: list[dict[str, Any]], keys: list[str]) -> dict[int, set[int]]:
    graph: dict[int, set[int]] = {}
    for i, sub in enumerate(subs):
        deps: set[int] = set()
        for d in _as_list(sub.get("dependsOn", sub.get("depends_on"))):
            for j, other in enumerate(subs):
                if j != i and d in (keys[j], str(j + 1), str(other.get("title") or "\0")):
                    deps.add(j)
        graph[i] = deps
    return graph


def _reaches(graph: dict[int, set[int]], start: int, goal: int) -> bool:
    seen: set[int] = set()
    todo = [start]
    while todo:
        n = todo.pop()
        if n == goal:
            return True
        if n in seen:
            continue
        seen.add(n)
        todo.extend(graph.get(n, ()))
    return False


def _overlap(a: Entry, b: Entry) -> bool:
    return bool(a["call"] == b["call"] or (a["start"] <= b["end"] and b["start"] <= a["end"]))


def compile_batch(batch: dict[str, Any], lib: Library) -> dict[str, Any]:
    """Findings and suggested facts for every subtask of ``batch``."""
    subs = [s for s in batch.get("subtasks", []) if isinstance(s, dict)]
    keys = [_key(s, i) for i, s in enumerate(subs)]
    pieces: list[dict[str, Any]] = []
    resolved_all: list[list[Entry]] = []
    for i, sub in enumerate(subs):
        files = _as_list(sub.get("files"))
        read_files = _as_list(sub.get("readFiles", sub.get("read_files")))
        text = _piece_text(sub)
        names: list[dict[str, Any]] = []
        resolved: list[Entry] = []
        findings: list[str] = []
        unresolved: list[str] = []
        settings = 0
        for name in extract_names(text):
            if name in _SKIP_WORDS:
                continue
            hits = _resolve(lib, name)
            if not hits and _SETTING_RE.match(name):
                settings += 1
                continue
            total = len(hits)
            if total > 1:
                hits = _narrow(hits, files, read_files)
            if len(hits) == 1:
                item: dict[str, Any] = {"name": name, "status": "resolved", "calls": [hits[0]["call"]]}
                if total > 1:
                    item["alsoDefined"] = total - 1
                names.append(item)
                resolved.append(hits[0])
            elif hits:
                calls = [h["call"] for h in hits]
                more = max(0, len(calls) - MAX_CANDIDATES)
                names.append({"name": name, "status": "ambiguous", "calls": calls[:MAX_CANDIDATES], "more": more})
                tail = f" +{more} more" if more else ""
                findings.append(f"`{name}` is ambiguous: {', '.join(calls[:MAX_CANDIDATES])}{tail}")
            else:
                names.append({"name": name, "status": "unresolved", "calls": []})
                unresolved.append(name)
        if unresolved:
            findings.append(f"not found (fine if this piece creates them): {', '.join(unresolved)}")
        if settings:
            findings.append(f"settings or constants not checked: {settings}")
        known = set(files) | set(read_files)
        suggested: list[str] = []
        for e in resolved:
            if e["path"] not in known and e["path"] not in suggested:
                suggested.append(e["path"])
        suggested = suggested[:MAX_READ_FILES]
        resolved_all.append(resolved)
        pieces.append(
            {
                "key": keys[i],
                "title": str(sub.get("title") or ""),
                "names": names,
                "unresolved": unresolved,
                "settingsSkipped": settings,
                "findings": findings,
                "facts": _fact_sheet(lib, resolved, set(files)),
                "suggestedReadFiles": suggested,
            }
        )

    # Size and callers outside the piece's files.
    for i, sub in enumerate(subs):
        files_set = set(_as_list(sub.get("files")))
        own = {e["call"]: e for e in resolved_all[i] if e["path"] in files_set}
        lines = sum(int(e["end"]) - int(e["start"]) + 1 for e in own.values())
        if lines > SIZE_LIMIT:
            pieces[i]["findings"].append(
                f"#{keys[i]} touches ~{lines} lines of existing code; consider a codemod or splitting"
            )
        shown: set[str] = set()
        for sentence in _SENTENCE_RE.split(_piece_text(sub)):
            if not _CHANGE_RE.search(sentence):
                continue
            for name in extract_names(sentence):
                for e in resolved_all[i]:
                    if e["call"] in shown or (_qname(e) != name and _qname(e).rsplit(".", 1)[-1] != name):
                        continue
                    outside = [c for c in lib.callers(e["call"], limit=200) if c["path"] not in files_set]
                    if outside:
                        shown.add(e["call"])
                        sites = ", ".join(f"{c['path']}:{c['line']}" for c in outside[:5])
                        pieces[i]["findings"].append(
                            f"#{keys[i]} changes {e['call']}; callers outside its files: {sites}"
                        )

    # Conflict risk across the batch.
    graph = _dep_graph(subs, keys)
    conflicts: list[str] = []
    for a in range(len(subs)):
        for b in range(a + 1, len(subs)):
            if _reaches(graph, a, b) or _reaches(graph, b, a):
                continue
            shared = sorted(set(_as_list(subs[a].get("files"))) & set(_as_list(subs[b].get("files"))))
            for path in shared:
                both = [
                    (x, y)
                    for x in resolved_all[a]
                    for y in resolved_all[b]
                    if x["path"] == path and y["path"] == path and _overlap(x, y)
                ]
                if both:
                    done: set[str] = set()
                    for x, _y in both:
                        if x["call"] in done:
                            continue
                        done.add(x["call"])
                        conflicts.append(
                            f"#{keys[a]} and #{keys[b]} both change {x['call']} ({path}:{x['start']}-{x['end']})"
                        )
                else:
                    conflicts.append(
                        f"#{keys[a]} and #{keys[b]} share {path}; keep their edits apart "
                        "and tell each where its imports go"
                    )
    return {"title": str(batch.get("title") or ""), "subtasks": pieces, "conflicts": conflicts}


def write_batch(batch: dict[str, Any], report: dict[str, Any], out: Path) -> None:
    """Write a copy of ``batch`` with fact sheets and suggested readFiles; ``batch`` is untouched."""
    result = copy.deepcopy(batch)
    date = datetime.date.today().isoformat()
    subs = [s for s in result.get("subtasks", []) if isinstance(s, dict)]
    for sub, piece in zip(subs, report["subtasks"], strict=False):
        if piece["facts"]:
            desc = str(sub.get("description") or "")
            sub["description"] = f"{desc}\n\n## Facts (from the code library, {date})\n{piece['facts']}"
        if piece["suggestedReadFiles"]:
            current = _as_list(sub.get("readFiles", sub.get("read_files")))
            sub["readFiles"] = current + [p for p in piece["suggestedReadFiles"] if p not in current]
    out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")


def format_report(report: dict[str, Any]) -> str:
    lines: list[str] = []
    for piece in report["subtasks"]:
        lines.append(f"#{piece['key']} {piece['title']}".rstrip())
        for n in piece["names"]:
            if n["status"] == "resolved":
                extra = f" (also defined in {n['alsoDefined']} other places)" if n.get("alsoDefined") else ""
                lines.append(f"  resolved: `{n['name']}` -> {n['calls'][0]}{extra}")
        for f in piece["findings"]:
            lines.append(f"  {f}")
        for p in piece["suggestedReadFiles"]:
            lines.append(f"  suggest readFiles: {p}")
    if report["conflicts"]:
        lines.append("Conflict risk")
        lines.extend(f"  {c}" for c in report["conflicts"])
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m foundry.order_compiler", description="Order compiler.")
    parser.add_argument("batch")
    parser.add_argument("--repo", default=".")
    parser.add_argument("--write")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)
    try:
        batch = json.loads(Path(args.batch).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"cannot read batch: {exc}", file=sys.stderr)
        return 1
    if not isinstance(batch, dict):
        print("cannot read batch: not a JSON object", file=sys.stderr)
        return 1
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "library.db"
        build_library(args.repo, db)
        report = compile_batch(batch, Library(db))
    if args.write:
        write_batch(batch, report, Path(args.write))
    print(json.dumps(report, indent=2) if args.as_json else format_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
