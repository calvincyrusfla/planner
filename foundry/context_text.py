"""Search helpers for named text in non-Python files.

A BLOCKED reply can name something that is not a symbol the outliner knows, such
as an element id, a CSS selector or a config key. These helpers turn such requests
into literal search terms and order the hits, so the context engine can show the
lines around them on the next attempt.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence

__all__ = ["request_terms", "spread_hits"]

_MIN_TERM_CHARS = 3  # shorter requests match too many unrelated lines


def request_terms(names: Iterable[str], source: str) -> list[str]:
    """Literal terms to search ``source`` for, one per requested name that occurs in it.

    A name is tried as written first. If it is absent, its last dotted part without
    a leading ``#`` is tried, so ``Widget.render`` finds ``render`` and the selectors
    ``.card`` and ``#list`` find ``class="card"`` and ``id="list"``.
    """
    terms: list[str] = []
    for raw in names:
        name = raw.strip().strip("`").removesuffix("()").strip()
        for candidate in (name, name.rsplit(".", 1)[-1].lstrip("#")):
            if len(candidate) >= _MIN_TERM_CHARS and candidate in source:
                terms.append(candidate)
                break
    return list(dict.fromkeys(terms))


def spread_hits(hits: Sequence[tuple[str, int]]) -> list[tuple[str, int]]:
    """Reorder ``(name, line)`` hits so each name's first hit comes before any name's second.

    Order within each round is kept, so a window cap cannot starve a commoner name.
    """
    turns: Counter[str] = Counter()
    keyed: list[tuple[int, tuple[str, int]]] = []
    for hit in hits:
        keyed.append((turns[hit[0]], hit))
        turns[hit[0]] += 1
    keyed.sort(key=lambda item: item[0])
    return [hit for _, hit in keyed]
