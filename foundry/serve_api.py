"""Local HTTP API of the long-running Foundry service (``python -m foundry.serve``).

Stdlib only (``http.server``), JSON in and out, no CORS.  Every request must
come from this computer: a loopback socket *and* a local ``Host`` header
(blocks DNS rebinding), the same rule the department dashboard uses for
``/api/usage/refresh``.  POSTs that carry an ``Origin`` header must name a
local host as well, so a web page elsewhere cannot submit orders.

Routes
------
``GET  /api/health``  -> ``{ok, repo, model, gemma_ok, claude_ok, version}``
``GET  /api/status``  -> ``{ok, tasks, foreman, paused_until, last_account_error}``
``POST /api/orders``  ``{title, description?, files?, size?}`` -> ``{ok, task_id}``
``POST /api/ask``     ``{question}`` -> ``text/event-stream`` of
``data: {"text": "..."}`` chunks, then ``event: done`` (``event: error`` first
if the answer fails mid-stream).

Errors are JSON ``{ok: false, error, code}``: 400 ``invalid``, 403 ``not_local``,
404, 405 ``method``, 413 ``too_large``, 503 ``not_running`` (orders/ask while
``service.running`` is False), 500 ``internal`` (details only in the log).

The API talks to a *service* object (``foundry.serve.Service``) and only uses
duck-typed methods, so tests can pass a simulated one:

* ``health()`` -> dict (optional; filled from ``repo``/``model`` attributes),
* ``status()`` -> dict,
* ``submit(title=, description=, files=, size=)`` -> task id (str, or a dict
  with ``task_id``); raises ``ValueError`` for an invalid order,
* ``ask(question)`` -> (async) iterator of text chunks.

Any of them may be a coroutine / async generator.  When the service exposes a
running asyncio loop as ``service.loop`` the coroutine runs on that loop
(``run_coroutine_threadsafe``); otherwise it runs on a private loop in the
request thread.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

from foundry import __version__

__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "LOOPBACK",
    "LOCAL_HOSTS",
    "MAX_BODY_BYTES",
    "SIZES",
    "FoundryAPI",
    "OrderError",
    "is_local_request",
    "validate_order",
]

log = logging.getLogger("foundry.serve_api")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 4310
LOOPBACK = frozenset({"127.0.0.1", "::1", "::ffff:127.0.0.1"})
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]", "::1"})
MAX_BODY_BYTES = 64 * 1024
CALL_TIMEOUT_S = 30.0
ASK_CHUNK_TIMEOUT_S = 180.0

MAX_TITLE = 200
MAX_DESCRIPTION = 20_000
MAX_FILES = 200
MAX_QUESTION = 4_000
SIZES = {"S": "S", "M": "M", "L": "L", "XL": "XL", "SMALL": "S", "MEDIUM": "M", "LARGE": "L"}


class OrderError(ValueError):
    """An invalid request body (answered with HTTP 400)."""


# ---------------------------------------------------------------------------
# Local checks
# ---------------------------------------------------------------------------


def _host_name(value: str | None) -> str:
    """Hostname of a ``Host`` header value, lower-case, without the port."""
    host = str(value or "").strip().lower()
    if host.startswith("["):  # [::1]:4310
        end = host.find("]")
        return host[: end + 1] if end != -1 else host
    if host.count(":") == 1:
        host = host.split(":", 1)[0]
    return host


def is_local_request(client_ip: str, host_header: str | None, origin: str | None = None) -> bool:
    """True when a request comes from this computer.

    Loopback socket, local ``Host`` header and, if an ``Origin`` is sent, a
    local origin (any port: the dashboard proxy on :4300 is local too).
    """
    if client_ip not in LOOPBACK:
        return False
    if _host_name(host_header) not in LOCAL_HOSTS:
        return False
    if origin is not None and origin != "":
        try:
            name = (urlsplit(origin).hostname or "").lower()
        except ValueError:
            return False
        if name not in LOCAL_HOSTS:
            return False
    return True


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_order(body: Any) -> dict[str, Any]:
    """Check a ``POST /api/orders`` body; return ``{title, description, files, size}``.

    ``files`` may be a list of strings or one comma separated string; paths
    must be repo-relative (no absolute paths, no ``..``).  ``size`` is
    optional: S/M/L/XL or small/medium/large (case-insensitive).
    """
    if not isinstance(body, dict):
        raise OrderError("Send a JSON object.")
    unknown = set(body) - {"title", "description", "files", "size"}
    if unknown:
        raise OrderError(f"Unknown field(s): {', '.join(sorted(unknown))}.")
    title = body.get("title")
    if not isinstance(title, str) or not title.strip():
        raise OrderError("A title is required.")
    title = title.strip()
    if len(title) > MAX_TITLE:
        raise OrderError(f"The title is longer than {MAX_TITLE} characters.")
    description = body.get("description")
    if description is None:
        description = ""
    if not isinstance(description, str):
        raise OrderError("The description must be text.")
    if len(description) > MAX_DESCRIPTION:
        raise OrderError(f"The description is longer than {MAX_DESCRIPTION} characters.")
    files_in = body.get("files")
    if files_in is None:
        files_in = []
    if isinstance(files_in, str):
        files_in = files_in.split(",")
    if not isinstance(files_in, list) or not all(isinstance(f, str) for f in files_in):
        raise OrderError("Files must be a list of paths.")
    files: list[str] = []
    for raw in files_in:
        path = raw.strip().replace("\\", "/")
        if not path:
            continue
        parts = path.split("/")
        if path.startswith("/") or (len(path) > 1 and path[1] == ":") or ".." in parts:
            raise OrderError(f"Files must be paths inside the repo: {raw.strip()!r}.")
        if path not in files:
            files.append(path)
    if len(files) > MAX_FILES:
        raise OrderError(f"At most {MAX_FILES} files.")
    size = body.get("size")
    if size is not None and size != "":
        if not isinstance(size, str) or size.strip().upper() not in SIZES:
            raise OrderError("Size must be one of S, M, L, XL (or small, medium, large).")
        size = SIZES[size.strip().upper()]
    else:
        size = None
    return {"title": title, "description": description.strip(), "files": files, "size": size}


def _validate_question(body: Any) -> str:
    if not isinstance(body, dict):
        raise OrderError("Send a JSON object.")
    question = body.get("question")
    if not isinstance(question, str) or not question.strip():
        raise OrderError("A question is required.")
    question = question.strip()
    if len(question) > MAX_QUESTION:
        raise OrderError(f"The question is longer than {MAX_QUESTION} characters.")
    return question


# ---------------------------------------------------------------------------
# Sync/async bridging
# ---------------------------------------------------------------------------


def _service_loop(service: Any) -> asyncio.AbstractEventLoop | None:
    loop = getattr(service, "loop", None)
    if isinstance(loop, asyncio.AbstractEventLoop) and loop.is_running() and not loop.is_closed():
        return loop
    return None


def _resolve(service: Any, value: Any, timeout: float = CALL_TIMEOUT_S) -> Any:
    """Return ``value``, awaiting it first when it is awaitable."""
    if not inspect.isawaitable(value):
        return value
    loop = _service_loop(service)
    if loop is not None:
        return asyncio.run_coroutine_threadsafe(_as_coro(value), loop).result(timeout)
    return asyncio.run(asyncio.wait_for(_as_coro(value), timeout))


async def _as_coro(awaitable: Any) -> Any:
    return await awaitable


def _call(service: Any, fn: Any, *args: Any, **kwargs: Any) -> Any:
    """Call a service method; on the service loop when it runs (keeps factory state single-threaded)."""
    loop = _service_loop(service)
    if loop is None:
        return _resolve(service, fn(*args, **kwargs))

    async def run() -> Any:
        value = fn(*args, **kwargs)
        return await value if inspect.isawaitable(value) else value

    return asyncio.run_coroutine_threadsafe(run(), loop).result(CALL_TIMEOUT_S)


def _not_running(service: Any) -> bool:
    """True when the service says it is not running (``running`` attribute False)."""
    return getattr(service, "running", True) is False


class _ChunkPump:
    """Iterate a sync or async iterator of chunks from a request thread."""

    def __init__(self, service: Any, source: Any) -> None:
        self.service = service
        self.source = source
        self.is_async = hasattr(source, "__anext__") or hasattr(source, "__aiter__")
        self._own_loop: asyncio.AbstractEventLoop | None = None
        if self.is_async:
            if not hasattr(source, "__anext__"):
                self.source = source.__aiter__()
            if _service_loop(service) is None:
                self._own_loop = asyncio.new_event_loop()
        else:
            self.source = iter(source)

    def _run(self, coro: Any) -> Any:
        loop = _service_loop(self.service)
        if self._own_loop is None and loop is not None:
            return asyncio.run_coroutine_threadsafe(coro, loop).result(ASK_CHUNK_TIMEOUT_S)
        assert self._own_loop is not None
        return self._own_loop.run_until_complete(asyncio.wait_for(coro, ASK_CHUNK_TIMEOUT_S))

    def __iter__(self) -> "_ChunkPump":
        return self

    def __next__(self) -> Any:
        if not self.is_async:
            return next(self.source)
        try:
            return self._run(_as_coro(self.source.__anext__()))
        except StopAsyncIteration:
            raise StopIteration from None

    def close(self) -> None:
        try:
            if self.is_async:
                aclose = getattr(self.source, "aclose", None)
                if aclose is not None:
                    try:
                        self._run(_as_coro(aclose()))
                    except Exception:  # noqa: BLE001 - best effort cleanup
                        log.debug("ask stream close failed", exc_info=True)
            else:
                close = getattr(self.source, "close", None)
                if close is not None:
                    close()
        finally:
            if self._own_loop is not None:
                self._own_loop.close()
                self._own_loop = None


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    server_version = "Foundry/" + __version__
    protocol_version = "HTTP/1.1"

    # set on the subclass made by FoundryAPI
    service: Any = None

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        log.debug("%s - %s", self.address_string(), format % args)

    # -- helpers ----------------------------------------------------------

    def _send_json(self, code: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _error(self, code: int, message: str, error_code: str) -> None:
        self._send_json(code, {"ok": False, "error": message, "code": error_code})

    def _read_json(self) -> Any:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise OrderError("Bad Content-Length.") from None
        if length < 0:
            raise OrderError("Bad Content-Length.")
        if length > MAX_BODY_BYTES:
            raise _TooLarge()
        raw = self.rfile.read(length) if length else b""
        if not raw.strip():
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise OrderError("The body is not valid JSON.") from None

    def _local(self) -> bool:
        origin = self.headers.get("Origin") if self.command == "POST" else None
        return is_local_request(self.client_address[0], self.headers.get("Host"), origin)

    def _route(self) -> tuple[str, Any] | None:
        path = urlsplit(self.path).path.rstrip("/") or "/"
        routes = {
            "/api/health": ("GET", self._health),
            "/api/status": ("GET", self._status),
            "/api/orders": ("POST", self._orders),
            "/api/ask": ("POST", self._ask),
        }
        return routes.get(path)

    def _dispatch(self) -> None:
        if not self._local():
            # Drain nothing; just refuse and close.
            self.close_connection = True
            return self._error(403, "Only this computer may use the Foundry API.", "not_local")
        route = self._route()
        if route is None:
            self.close_connection = True
            return self._error(404, "Not found", "not_found")
        method, handler = route
        if self.command != method:
            self.close_connection = True
            return self._error(405, f"Use {method}", "method")
        try:
            handler()
        except _TooLarge:
            self.close_connection = True
            self._error(413, "The request body is too large.", "too_large")
        except OrderError as exc:
            self._error(400, str(exc), "invalid")
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception:  # noqa: BLE001 - never leak internals to the client
            log.exception("Foundry API %s %s failed", self.command, self.path)
            self.close_connection = True
            try:
                self._error(500, "Foundry could not handle that request.", "internal")
            except OSError:
                pass

    def do_GET(self) -> None:  # noqa: N802 - stdlib name
        self._dispatch()

    def do_POST(self) -> None:  # noqa: N802 - stdlib name
        self._dispatch()

    def do_PUT(self) -> None:  # noqa: N802
        self._dispatch()

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch()

    def do_OPTIONS(self) -> None:  # noqa: N802 - no CORS: preflights get 405
        self._dispatch()

    # -- routes -----------------------------------------------------------

    def _health(self) -> None:
        svc = self.service
        data: dict[str, Any] = {}
        fn = getattr(svc, "health", None)
        if callable(fn):
            data = dict(_call(svc, fn) or {})
        repo = data.get("repo", getattr(svc, "repo", None))
        out = {
            "ok": bool(data.get("ok", True)),
            "repo": str(repo) if repo is not None else None,
            "model": data.get("model", getattr(svc, "model", None)),
            "gemma_ok": bool(data.get("gemma_ok", getattr(svc, "gemma_ok", False))),
            "claude_ok": bool(data.get("claude_ok", getattr(svc, "claude_ok", False))),
            "version": data.get("version") or __version__,
        }
        for key, value in data.items():
            out.setdefault(key, value)
        self._send_json(200, out)

    def _status(self) -> None:
        svc = self.service
        data = dict(_call(svc, svc.status) or {})
        out: dict[str, Any] = {
            "ok": bool(data.get("ok", True)),
            "tasks": list(data.get("tasks") or []),
            "foreman": data.get("foreman") or {},
            "paused_until": data.get("paused_until"),
            "last_account_error": data.get("last_account_error"),
        }
        for key, value in data.items():
            out.setdefault(key, value)
        self._send_json(200, out)

    def _orders(self) -> None:
        order = validate_order(self._read_json())
        svc = self.service
        if _not_running(svc):
            return self._error(503, "Foundry is not running.", "not_running")
        try:
            result = _call(svc, svc.submit, **order)
        except OrderError:
            raise
        except ValueError as exc:  # incl. pydantic ValidationError
            raise OrderError(_short(exc)) from None
        task_id = result.get("task_id") if isinstance(result, dict) else result
        if not task_id:
            raise RuntimeError("service.submit returned no task id")
        self._send_json(200, {"ok": True, "task_id": str(task_id)})

    def _ask(self) -> None:
        question = _validate_question(self._read_json())
        svc = self.service
        if _not_running(svc):
            return self._error(503, "Foundry is not running.", "not_running")
        source = svc.ask(question)
        if inspect.isawaitable(source) and not hasattr(source, "__anext__"):
            source = _resolve(svc, source)
        pump = _ChunkPump(svc, source)
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            try:
                for chunk in pump:
                    if not chunk:
                        continue
                    self._event(None, {"text": str(chunk)})
            except (BrokenPipeError, ConnectionResetError):
                raise
            except Exception:  # noqa: BLE001 - report on the stream, keep internals out
                log.exception("Foreman answer failed")
                self._event("error", {"error": "The Foreman could not finish the answer."})
            self._event("done", {})
        finally:
            pump.close()

    def _event(self, name: str | None, payload: dict[str, Any]) -> None:
        text = ""
        if name:
            text += f"event: {name}\n"
        text += "data: " + json.dumps(payload) + "\n\n"
        self.wfile.write(text.encode("utf-8"))
        self.wfile.flush()


class _TooLarge(Exception):
    pass


def _short(exc: Exception) -> str:
    text = str(exc).strip().splitlines()
    return (text[0] if text else "Invalid order.")[:300]


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class _Server6(_Server):
    address_family = socket.AF_INET6


class FoundryAPI:
    """The Foundry HTTP API bound to one service; runs in a background thread.

    >>> api = FoundryAPI(service, port=0); api.start(); api.url  # doctest: +SKIP
    """

    def __init__(self, service: Any, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
        if host not in LOOPBACK and host != "localhost":
            raise ValueError("The Foundry API only listens on loopback (127.0.0.1).")
        self.service = service
        self.host = host
        self.requested_port = port
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        if self._server is None:
            return self.requested_port
        return int(self._server.server_address[1])

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self) -> "FoundryAPI":
        """Bind and serve in a daemon thread (raises ``OSError`` if the port is taken)."""
        if self._server is not None:
            return self
        handler = type("FoundryHandler", (_Handler,), {"service": self.service})
        server_cls = _Server6 if ":" in self.host else _Server
        self._server = server_cls((self.host, self.requested_port), handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="foundry-api", kwargs={"poll_interval": 0.2}, daemon=True
        )
        self._thread.start()
        log.info("Foundry API listening on %s", self.url)
        return self

    def stop(self) -> None:
        """Stop serving and close the socket (idempotent)."""
        server, self._server = self._server, None
        if server is None:
            return
        server.shutdown()
        server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def __enter__(self) -> "FoundryAPI":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.stop()
