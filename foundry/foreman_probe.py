"""Readiness probe for the Gemma Shop Foreman (Ollama).

Before the orchestrator starts in real mode it must know that the local
Ollama server answers and that the Foreman model has been pulled.  This
module asks ``GET <base_url>/models`` (Ollama's OpenAI-compatible model list)
and checks the requested tag is present.  It never raises: every failure
(connection refused, timeout, bad JSON, HTTP error) becomes
``ProbeResult(ok=False, message=...)`` with an actionable message.

Command line::

    python -m foundry.foreman_probe [--base-url URL] [--model TAG] [--timeout S] [--json]

Exit code 0 when the Foreman is ready, 2 otherwise (the message goes to
stderr).  Defaults come from ``FOUNDRY_GEMMA_BASE_URL`` /
``FOUNDRY_GEMMA_MODEL`` (falling back to ``FOREMAN_MODEL``, the variable the
setup scripts use), then ``http://localhost:11434/v1`` and ``gemma4:e4b``.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_MODEL",
    "EXIT_NOT_READY",
    "EXIT_OK",
    "ProbeResult",
    "main",
    "model_matches",
    "normalize_base_url",
    "offline_message",
    "probe",
]

DEFAULT_BASE_URL = "http://localhost:11434/v1"
DEFAULT_MODEL = "gemma4:e4b"
EXIT_OK = 0
EXIT_NOT_READY = 2

_SETUP_HINT = (
    "Run ./setup_foreman.sh (macOS/Linux) or setup_foreman.bat (Windows) "
    "to start Gemma 4 before launching the orchestrator."
)


@dataclass(frozen=True)
class ProbeResult:
    """Outcome of one probe.

    Attributes:
        ok: True when the server answered and the model is available.
        models: Model ids the server reported (empty when unreachable).
        message: Human-readable, actionable summary.
        base_url: The normalized ``.../v1`` URL that was probed.
        model: The model tag that was looked for.
    """

    ok: bool
    models: list[str] = field(default_factory=list)
    message: str = ""
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-able dict of this result."""
        return {
            "ok": self.ok,
            "models": list(self.models),
            "message": self.message,
            "base_url": self.base_url,
            "model": self.model,
        }


def offline_message(base_url: str) -> str:
    """Return the standard "Foreman not reachable" message for ``base_url``."""
    return f"Gemma Shop Foreman is not reachable at {base_url}. {_SETUP_HINT}"


def normalize_base_url(base_url: str) -> str:
    """Return ``base_url`` without a trailing slash and ending in ``/v1``.

    Accepts either the server root (``http://localhost:11434``) or the
    OpenAI-compatible root (``http://localhost:11434/v1``).
    """
    url = (base_url or DEFAULT_BASE_URL).strip().rstrip("/")
    if "://" not in url:
        url = "http://" + url
    if not url.endswith("/v1"):
        url += "/v1"
    return url


def _split_tag(name: str) -> tuple[str, str]:
    """Split ``repo:tag`` into ``(repo, tag)``; the tag defaults to ``latest``."""
    name = name.strip().lower()
    # A ':' inside a registry host/port ("host:5000/x") is not a tag separator.
    head, sep, tail = name.rpartition(":")
    if sep and "/" not in tail:
        return head, tail
    return name, "latest"


def model_matches(wanted: str, available: str) -> bool:
    """True when ``available`` is the ``wanted`` model.

    Matches the exact tag, and treats a missing tag as ``:latest`` on either
    side (``gemma4`` matches ``gemma4:latest`` and vice versa).  Comparison is
    case-insensitive.
    """
    return _split_tag(wanted) == _split_tag(available)


def _is_loopback(url: str) -> bool:
    """True when ``url`` points at this machine (so proxies must be skipped)."""
    host = urllib.parse.urlsplit(url).hostname or ""
    if host in ("localhost", ""):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _fetch_json(url: str, timeout: float) -> Any:
    """GET ``url`` and decode JSON; raises on any network/decoding problem."""
    handlers: list[urllib.request.BaseHandler] = []
    if _is_loopback(url):
        # A local Ollama must never be reached through HTTP(S)_PROXY.
        handlers.append(urllib.request.ProxyHandler({}))
    opener = urllib.request.build_opener(*handlers)
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with opener.open(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _model_ids(payload: Any) -> list[str]:
    """Extract model ids from an OpenAI (``data[].id``) or Ollama (``models[].name``) list."""
    ids: list[str] = []
    if not isinstance(payload, dict):
        return ids
    for key, attr in (("data", "id"), ("models", "name"), ("models", "model")):
        items = payload.get(key)
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict) and isinstance(item.get(attr), str):
                    if item[attr] not in ids:
                        ids.append(item[attr])
    return ids


def probe(base_url: str = DEFAULT_BASE_URL, model: str = DEFAULT_MODEL, timeout: float = 3) -> ProbeResult:
    """Check that Ollama answers at ``base_url`` and serves ``model``.

    Never raises.  Returns ``ok=False`` with an actionable message when the
    server is unreachable, times out, returns garbage, or lacks the model.

    Args:
        base_url: Ollama root or OpenAI-compatible root (``.../v1``).
        model: Model tag to look for, e.g. ``gemma4:e4b``.
        timeout: Socket timeout in seconds.
    """
    url = normalize_base_url(base_url)
    model = (model or DEFAULT_MODEL).strip()

    def fail(message: str, models: list[str] | None = None) -> ProbeResult:
        return ProbeResult(ok=False, models=models or [], message=message, base_url=url, model=model)

    try:
        payload = _fetch_json(url + "/models", timeout)
    except urllib.error.HTTPError as exc:
        return fail(
            f"Gemma Shop Foreman at {url} answered HTTP {exc.code} for /models "
            f"(is this an Ollama server?). {_SETUP_HINT}"
        )
    except (urllib.error.URLError, OSError, TimeoutError, ValueError):
        # URLError: refused / DNS; OSError & TimeoutError: socket timeouts
        # and resets; ValueError: JSON / unicode decoding problems.
        return fail(offline_message(url))
    except Exception as exc:  # noqa: BLE001 - the probe must never raise
        return fail(f"{offline_message(url)} ({type(exc).__name__}: {exc})")

    models = _model_ids(payload)
    if any(model_matches(model, name) for name in models):
        return ProbeResult(
            ok=True,
            models=models,
            message=f"Gemma Shop Foreman is ready at {url} (model {model}).",
            base_url=url,
            model=model,
        )
    listed = ", ".join(models) if models else "none"
    return fail(
        f"Gemma Shop Foreman at {url} is running but model {model} is not available "
        f"(installed: {listed}). Run `ollama pull {model}`, or {_SETUP_HINT[0].lower()}{_SETUP_HINT[1:]}",
        models,
    )


def _env(*names: str) -> str | None:
    """First non-empty environment value among ``names``."""
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return None


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point; returns 0 when ready, 2 otherwise."""
    parser = argparse.ArgumentParser(
        prog="python -m foundry.foreman_probe",
        description="Check that the local Gemma Shop Foreman (Ollama) is up and has the model.",
    )
    parser.add_argument("--base-url", default=_env("FOUNDRY_GEMMA_BASE_URL") or DEFAULT_BASE_URL)
    parser.add_argument("--model", default=_env("FOUNDRY_GEMMA_MODEL", "FOREMAN_MODEL") or DEFAULT_MODEL)
    parser.add_argument("--timeout", type=float, default=3.0)
    parser.add_argument("--json", action="store_true", help="print the result as JSON on stdout")
    args = parser.parse_args(argv)

    result = probe(args.base_url, args.model, timeout=args.timeout)
    if args.json:
        print(json.dumps(result.to_dict()))
    elif result.ok:
        print(result.message)
    if not result.ok and not args.json:
        print(result.message, file=sys.stderr)
    return EXIT_OK if result.ok else EXIT_NOT_READY


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
