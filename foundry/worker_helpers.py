"""Small helpers shared by the builder and reviewer workers and their prompt and reply layers.

Split out of ``foundry/builder_worker.py``, which imports these names back.
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from foundry.schemas import TaskAssignment

#: Longest fix hint stored on a reopened ticket (REVISE directives can be long).
MAX_FIX_HINT_CHARS = 1200

#: Longest one-line hint produced by the local fallback condenser.
_ONE_LINE_CHARS = 160


def _get(obj: Any, name: str, default: Any = None) -> Any:
    """Read ``name`` from a mapping or an object."""
    if obj is None:
        return default
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


_KW_CACHE: dict[tuple[Any, str], bool] = {}


def _accepts_kw(fn: Callable[..., Any], name: str) -> bool:
    """Whether ``fn`` accepts keyword ``name`` (inspected once per function, then cached)."""
    key = (getattr(fn, "__func__", fn), name)
    cached = _KW_CACHE.get(key)
    if cached is None:
        try:
            params = inspect.signature(fn).parameters
        except (TypeError, ValueError):  # builtins without a signature
            cached = False
        else:
            cached = name in params or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())
        _KW_CACHE[key] = cached
    return cached


def _supported_kwargs(fn: Callable[..., Any], kwargs: Mapping[str, Any]) -> dict[str, Any]:
    """The subset of ``kwargs`` that ``fn`` accepts (older collaborators lack newer keywords)."""
    return {k: v for k, v in kwargs.items() if _accepts_kw(fn, k)}


async def _maybe_await(value: Any) -> Any:
    """Await ``value`` when it is awaitable, else return it."""
    if inspect.isawaitable(value):
        return await value
    return value


def _estimate_tokens(text: str) -> int:
    """``ceil(len(text) / 4)``, the shared token estimate."""
    return -(-len(text) // 4)


def _state_value(state: Any) -> str:
    """Normalize a TaskState / string / None to its string value."""
    if state is None:
        return ""
    return str(getattr(state, "value", state))


def _one_line(text: str, limit: int = _ONE_LINE_CHARS) -> str:
    """Collapse ``text`` to one line of at most ``limit`` characters."""
    line = " ".join(text.split())
    return line if len(line) <= limit else line[: limit - 3].rstrip() + "..."


def _fallback_condense(trace: str) -> str:
    """Deterministic 1-line condensation used when no foreman is configured.

    Picks the last ``File "x", line N`` frame (or ``path:N:`` location) and the
    last line that looks like an exception/error.
    """
    lines = [ln.strip() for ln in trace.splitlines() if ln.strip()]
    if not lines:
        return "gate failed with no output"
    location = ""
    for ln in reversed(lines):
        m = re.search(r'File "([^"]+)", line (\d+)', ln)
        if m:
            location = f"{m.group(1)}:{m.group(2)}"
            break
        m = re.match(r"([\w./\\-]+\.\w+):(\d+)", ln)
        if m:
            location = f"{m.group(1)}:{m.group(2)}"
            break
    error = next(
        (ln for ln in reversed(lines) if re.search(r"(Error|Exception|FAILED|failed|assert)", ln)),
        lines[-1],
    )
    return _one_line(f"{location} {error}" if location and location not in error else error)


def _fence_for(text: str) -> str:
    """A backtick fence longer than any backtick run inside ``text``."""
    longest = max((len(m.group(0)) for m in re.finditer(r"`+", text)), default=0)
    return "`" * max(3, longest + 1)


def _normalize_path(path: str) -> str:
    """Repo-relative, forward-slash path without a leading ``./``."""
    p = path.strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def _read_only_path(path: Any) -> str:
    """A repo-relative path in the form diffs use (forward slashes), so paths compare reliably."""
    return _normalize_path(str(path).strip().replace("\\", "/"))


def _read_only_touched(files: Iterable[str], assignment: TaskAssignment) -> list[str]:
    """The ``files`` the assignment marks read-only, whatever ``allowed_files`` says."""
    protected = {_read_only_path(f) for f in getattr(assignment, "read_files", ())}
    return [f for f in files if _read_only_path(f) in protected]
