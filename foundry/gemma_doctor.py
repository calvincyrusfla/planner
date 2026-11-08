"""Gemma speed check ("doctor") for the Shop Foreman.

Tells the user, in plain English, whether Ollama runs the Foreman's Gemma
model on the GPU, how fast it generates, and what to change when it is slow.

It asks the local Ollama server (native API, not the OpenAI ``/v1`` one):

1. ``GET /api/tags``   - is the model installed?
2. ``POST /api/generate`` - a tiny timed generation (this also loads the
   model); tokens/s = ``eval_count / eval_duration``.
3. ``GET /api/ps``     - how much of the loaded model sits in VRAM
   (``size_vram / size`` -> % on the GPU).

Command line::

    python -m foundry.gemma_doctor [--base-url URL] [--model TAG] [--timeout S] [--json]

Exit codes: 0 Gemma is fast enough, 1 slow or (partly) on the CPU,
2 Ollama unreachable or the model is not installed.

Defaults: ``FOUNDRY_GEMMA_BASE_URL`` then ``FOREMAN_OLLAMA_URL`` (the setup
scripts' variable), then ``http://localhost:11434``; the model is
``FOUNDRY_GEMMA_MODEL`` then ``FOREMAN_MODEL``, then ``gemma4:e4b``.
Loopback calls never go through HTTP(S)_PROXY.

The verdict logic is pure (:func:`gpu_percent`, :func:`tokens_per_second`,
:func:`assess`) so it can be tested with canned JSON.  :func:`quick_report`
returns a small dict for other modules (dashboard / health).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from foundry.foreman_probe import _is_loopback, model_matches

__all__ = [
    "DEFAULT_MODEL",
    "DEFAULT_ROOT_URL",
    "EXIT_OK",
    "EXIT_SLOW",
    "EXIT_UNREACHABLE",
    "FAST_TOKENS_PER_S",
    "GPU_OK_PCT",
    "Assessment",
    "assess",
    "diagnose",
    "gpu_percent",
    "main",
    "quick_report",
    "root_url",
    "smaller_model",
    "tokens_per_second",
]

DEFAULT_ROOT_URL = "http://localhost:11434"
DEFAULT_MODEL = "gemma4:e4b"

EXIT_OK = 0
EXIT_SLOW = 1
EXIT_UNREACHABLE = 2

#: At least this share of the model in VRAM counts as "on the GPU".
GPU_OK_PCT = 90
#: Generation speed (tokens/s) below which sizing will often time out.
FAST_TOKENS_PER_S = 10.0

_README_HINT = "see foundry/README.md 'Make Gemma fast'"
_PROMPT = "Reply with the single word OK."

#: Next smaller tag for known Gemma families (largest first in each chain).
_SMALLER: dict[str, str] = {
    "gemma4:31b": "gemma4:26b",
    "gemma4:26b": "gemma4:e4b",
    "gemma4:latest": "gemma4:e2b",
    "gemma4:e4b": "gemma4:e2b",
    "gemma3:27b": "gemma3:12b",
    "gemma3:12b": "gemma3:4b",
    "gemma3:latest": "gemma3:1b",
    "gemma3:4b": "gemma3:1b",
    "gemma3n:latest": "gemma3n:e2b",
    "gemma3n:e4b": "gemma3n:e2b",
    "gemma2:27b": "gemma2:9b",
    "gemma2:latest": "gemma2:2b",
    "gemma2:9b": "gemma2:2b",
}


# --------------------------------------------------------------------------- pure helpers


def root_url(base_url: str | None) -> str:
    """Return the Ollama server root (no trailing slash, no ``/v1``)."""
    url = (base_url or DEFAULT_ROOT_URL).strip().rstrip("/")
    if "://" not in url:
        url = "http://" + url
    if url.endswith("/v1"):
        url = url[: -len("/v1")]
    return url


def _norm_tag(name: str) -> str:
    name = name.strip().lower()
    head, sep, tail = name.rpartition(":")
    if sep and "/" not in tail:
        return name
    return name + ":latest"


def smaller_model(model: str) -> str | None:
    """Next smaller Gemma tag for ``model``, or None when unknown/already smallest."""
    return _SMALLER.get(_norm_tag(model))


def _installed(tags_payload: Any) -> list[str]:
    names: list[str] = []
    if isinstance(tags_payload, dict) and isinstance(tags_payload.get("models"), list):
        for item in tags_payload["models"]:
            if isinstance(item, dict):
                name = item.get("name") or item.get("model")
                if isinstance(name, str) and name not in names:
                    names.append(name)
    return names


def gpu_percent(ps_payload: Any, model: str) -> int | None:
    """Percent of ``model`` held in VRAM per ``/api/ps``; None if not loaded/unknown."""
    if not isinstance(ps_payload, dict) or not isinstance(ps_payload.get("models"), list):
        return None
    for item in ps_payload["models"]:
        if not isinstance(item, dict):
            continue
        name = item.get("name") or item.get("model") or ""
        if not isinstance(name, str) or not model_matches(model, name):
            continue
        size, vram = item.get("size"), item.get("size_vram")
        if not isinstance(size, (int, float)) or not isinstance(vram, (int, float)) or size <= 0:
            return None
        return max(0, min(100, round(100 * vram / size)))
    return None


def tokens_per_second(generate_payload: Any) -> float | None:
    """Generation speed from an ``/api/generate`` reply (``eval_count/eval_duration``)."""
    if not isinstance(generate_payload, dict):
        return None
    count, duration = generate_payload.get("eval_count"), generate_payload.get("eval_duration")
    if not isinstance(count, (int, float)) or not isinstance(duration, (int, float)):
        return None
    if count <= 0 or duration <= 0:
        return None
    return round(count / (duration / 1e9), 1)


@dataclass(frozen=True)
class Assessment:
    """Verdict on a measured Gemma.

    Attributes:
        ok: True when Gemma is on the GPU (or unknown) and fast enough.
        exit_code: 0 ok, 1 slow/CPU, 2 unreachable/missing model.
        verdict: One plain-English headline.
        advice: Extra lines with what to change (may be empty).
    """

    ok: bool
    exit_code: int
    verdict: str
    advice: list[str] = field(default_factory=list)


def _speed(tps: float | None) -> str:
    return "an unknown speed" if tps is None else f"{tps:g} tokens/s"


def assess(model: str, gpu_pct: int | None, tps: float | None) -> Assessment:
    """Turn GPU share and speed into a verdict and advice (pure)."""
    smaller = smaller_model(model)
    slow = tps is not None and tps < FAST_TOKENS_PER_S
    advice: list[str] = []

    if gpu_pct is None:
        where = "Gemma is running (GPU use unknown)"
    elif gpu_pct == 0:
        where = "Gemma is running on the CPU (0% GPU)"
    elif gpu_pct < GPU_OK_PCT:
        where = f"Gemma is running only {gpu_pct}% on the GPU (the rest on the CPU)"
    else:
        where = f"Gemma is running {gpu_pct}% on the GPU"

    on_gpu = gpu_pct is None or gpu_pct >= GPU_OK_PCT
    if on_gpu and not slow:
        return Assessment(True, EXIT_OK, f"{where} at {_speed(tps)} - good.", advice)

    verdict = f"{where} at {_speed(tps)} - sizing will often fall back; {_README_HINT}."
    if gpu_pct == 0:
        advice.append(
            "Ollama is not using your GPU: update Ollama and your GPU driver, "
            "close other apps that use the GPU, then restart Ollama."
        )
    elif gpu_pct is not None and gpu_pct < GPU_OK_PCT:
        advice.append(
            "The model does not fit in your GPU memory (VRAM): close other GPU apps, "
            "lower OLLAMA_CONTEXT_LENGTH, or use a smaller Gemma."
        )
    if smaller and (slow or (gpu_pct is not None and gpu_pct < GPU_OK_PCT)):
        advice.append(
            f"Try a smaller Gemma: ollama pull {smaller}, then set FOUNDRY_GEMMA_MODEL={smaller}."
        )
    elif slow and not smaller:
        advice.append("Gemma is slow even so: a GPU with more VRAM would help most.")
    advice.append(
        "Slow Gemma is safe: tasks are still sized by the built-in fallback. "
        "You can raise FOUNDRY_GEMMA_TIMEOUT_S to let sizing wait longer; "
        "Ask the Foreman waits up to about 100 s (FOUNDRY_ASK_TIMEOUT_S, default 90) "
        "and then answers with the built-in status."
    )
    return Assessment(False, EXIT_SLOW, verdict, advice)


# --------------------------------------------------------------------------- HTTP


def _request_json(url: str, timeout: float, body: dict[str, Any] | None = None) -> Any:
    handlers: list[urllib.request.BaseHandler] = []
    if _is_loopback(url):
        handlers.append(urllib.request.ProxyHandler({}))
    opener = urllib.request.build_opener(*handlers)
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers)
    with opener.open(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def diagnose(
    base_url: str | None = None,
    model: str = DEFAULT_MODEL,
    timeout: float = 120.0,
) -> dict[str, Any]:
    """Run the full check against Ollama; never raises.

    Returns a dict with ``ok``, ``exit_code``, ``model``, ``base_url``,
    ``installed``, ``gpu_pct``, ``tokens_per_s``, ``load_s``, ``total_s``,
    ``verdict`` and ``advice`` (list of lines).
    """
    root = root_url(base_url)
    model = (model or DEFAULT_MODEL).strip()
    report: dict[str, Any] = {
        "ok": False,
        "exit_code": EXIT_UNREACHABLE,
        "model": model,
        "base_url": root,
        "installed": [],
        "gpu_pct": None,
        "tokens_per_s": None,
        "load_s": None,
        "total_s": None,
        "verdict": "",
        "advice": [],
    }

    try:
        tags = _request_json(root + "/api/tags", min(timeout, 10.0))
    except Exception:  # noqa: BLE001 - the doctor must never raise
        report["verdict"] = f"Ollama is not reachable at {root}."
        report["advice"] = [
            "Start Ollama (open the Ollama app, or run `ollama serve`), or run "
            "foundry\\setup_foreman.bat (Windows) / ./foundry/setup_foreman.sh."
        ]
        return report

    installed = _installed(tags)
    report["installed"] = installed
    if not any(model_matches(model, name) for name in installed):
        listed = ", ".join(installed) if installed else "none"
        report["verdict"] = f"Gemma model {model} is not installed in Ollama (installed: {listed})."
        report["advice"] = [f"Run: ollama pull {model}  (or set FOUNDRY_GEMMA_MODEL to an installed tag)."]
        return report

    started = time.monotonic()
    try:
        gen = _request_json(
            root + "/api/generate",
            timeout,
            {"model": model, "prompt": _PROMPT, "stream": False, "options": {"num_predict": 16}},
        )
    except Exception as exc:  # noqa: BLE001
        elapsed = round(time.monotonic() - started, 1)
        report["total_s"] = elapsed
        report["exit_code"] = EXIT_SLOW
        report["verdict"] = (
            f"Gemma did not answer a tiny test prompt within {elapsed:g} s "
            f"({type(exc).__name__}) - sizing will fall back; {_README_HINT}."
        )
        smaller = smaller_model(model)
        if smaller:
            report["advice"] = [
                f"Try a smaller Gemma: ollama pull {smaller}, then set FOUNDRY_GEMMA_MODEL={smaller}."
            ]
        return report
    report["total_s"] = round(time.monotonic() - started, 1)
    report["tokens_per_s"] = tokens_per_second(gen)
    if isinstance(gen, dict) and isinstance(gen.get("load_duration"), (int, float)):
        report["load_s"] = round(gen["load_duration"] / 1e9, 1)

    try:
        ps = _request_json(root + "/api/ps", min(timeout, 10.0))
    except Exception:  # noqa: BLE001
        ps = None
    report["gpu_pct"] = gpu_percent(ps, model)

    result = assess(model, report["gpu_pct"], report["tokens_per_s"])
    report.update(ok=result.ok, exit_code=result.exit_code, verdict=result.verdict, advice=list(result.advice))
    return report


def quick_report(config: Any, timeout: float = 60.0) -> dict[str, Any]:
    """Short speed report for other modules; never raises.

    ``config`` is a :class:`foundry.config.FoundryConfig` (anything with
    ``gemma_base_url`` and ``gemma_model``).  Returns
    ``{gpu_pct, tokens_per_s, model, ok, verdict}``.
    """
    base = getattr(config, "gemma_base_url", None) or DEFAULT_ROOT_URL
    model = getattr(config, "gemma_model", None) or DEFAULT_MODEL
    full = diagnose(base, model, timeout=timeout)
    return {key: full[key] for key in ("gpu_pct", "tokens_per_s", "model", "ok", "verdict")}


# --------------------------------------------------------------------------- CLI


def _env(*names: str) -> str | None:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return None


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point; returns 0 ok, 1 slow/CPU, 2 unreachable/model missing."""
    parser = argparse.ArgumentParser(
        prog="python -m foundry.gemma_doctor",
        description="Check whether Ollama runs the Foreman's Gemma on the GPU, and how fast.",
    )
    parser.add_argument(
        "--base-url",
        default=_env("FOUNDRY_GEMMA_BASE_URL", "FOREMAN_OLLAMA_URL") or DEFAULT_ROOT_URL,
    )
    parser.add_argument("--model", default=_env("FOUNDRY_GEMMA_MODEL", "FOREMAN_MODEL") or DEFAULT_MODEL)
    parser.add_argument("--timeout", type=float, default=120.0, help="seconds for the test prompt")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args(argv)

    if not args.json:
        print(f"[gemma doctor] Checking {args.model} at {root_url(args.base_url)} (this can take a minute)...")
    report = diagnose(args.base_url, args.model, timeout=args.timeout)
    if args.json:
        print(json.dumps(report))
    else:
        print(f"[gemma doctor] {report['verdict']}")
        for line in report["advice"]:
            print(f"[gemma doctor]   - {line}")
    return int(report["exit_code"])


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
