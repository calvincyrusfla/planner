"""Gemma (Shop Foreman) chat clients.

The Shop Foreman talks to a local Gemma model through Ollama's
OpenAI-compatible API (``http://localhost:11434/v1``). This module provides:

* :class:`GemmaClient` -- the protocol every Foreman component depends on.
* :class:`GemmaReply` / :class:`GemmaToolCall` / :class:`GemmaUsage` -- the
  normalised reply model.
* :class:`OllamaGemmaClient` -- the real HTTP client (standard library
  ``urllib`` executed in a worker thread via :func:`asyncio.to_thread`).
* :class:`SimulatedGemmaClient` -- a deterministic stand-in used by tests and
  the ``--simulate`` demo; it records every request it receives.
* :class:`GemmaUnavailable` -- raised when the Foreman cannot be reached or
  returns something unusable. Callers fall back to deterministic heuristics.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import socket
import urllib.error
import urllib.request
from typing import Any, Awaitable, Callable, Protocol, Union, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "GemmaClient",
    "GemmaReply",
    "GemmaRequest",
    "GemmaToolCall",
    "GemmaUnavailable",
    "GemmaUsage",
    "OllamaGemmaClient",
    "SimulatedGemmaClient",
    "parse_chat_completion",
]

DEFAULT_BASE_URL = "http://localhost:11434/v1"
DEFAULT_MODEL = "gemma4:e4b"


class GemmaUnavailable(RuntimeError):
    """The Gemma Shop Foreman could not be reached or gave an unusable reply.

    Attributes:
        status: HTTP status code when the server answered with an error, else ``None``.
    """

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class GemmaToolCall(BaseModel):
    """One tool call requested by the model (arguments already JSON-decoded)."""

    model_config = ConfigDict(extra="forbid")

    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    id: str | None = None


class GemmaUsage(BaseModel):
    """Token usage reported by the server (0 when not reported)."""

    model_config = ConfigDict(extra="forbid")

    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        """Prompt plus completion tokens."""
        return self.prompt_tokens + self.completion_tokens


class GemmaReply(BaseModel):
    """Normalised chat reply: text, tool calls and usage."""

    model_config = ConfigDict(extra="forbid")

    text: str = ""
    tool_calls: list[GemmaToolCall] = Field(default_factory=list)
    usage: GemmaUsage = Field(default_factory=GemmaUsage)
    finish_reason: str | None = None

    def parse_json(self) -> Any:
        """Decode :attr:`text` as JSON (tolerating a ```json fence).

        Raises:
            GemmaUnavailable: if the text is not valid JSON.
        """
        return decode_json_text(self.text)


class GemmaRequest(BaseModel):
    """A request as seen by :class:`SimulatedGemmaClient` (kept for assertions)."""

    model_config = ConfigDict(extra="forbid")

    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]] | None = None
    json_schema: dict[str, Any] | None = None
    temperature: float = 0.1


@runtime_checkable
class GemmaClient(Protocol):
    """Anything that can hold a chat with the Shop Foreman model."""

    async def chat(
        self,
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
        json_schema: dict | None = None,
        temperature: float = 0.1,
    ) -> GemmaReply:
        """Send ``messages`` (OpenAI chat format) and return the reply.

        Args:
            messages: ``[{"role": ..., "content": ...}, ...]``.
            tools: OpenAI tool definitions, passed through unchanged.
            json_schema: when given, the reply text must be JSON matching it.
            temperature: sampling temperature.
        """
        ...


def decode_json_text(text: str) -> Any:
    """Decode JSON from model text, stripping an optional Markdown code fence."""
    body = text.strip()
    if body.startswith("```"):
        lines = body.splitlines()
        lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        body = "\n".join(lines).strip()
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise GemmaUnavailable(f"Gemma reply is not valid JSON: {exc}") from exc


def _decode_arguments(raw: Any) -> dict[str, Any]:
    """Tool-call arguments arrive as a JSON string (OpenAI) or already as a dict (Ollama)."""
    if raw is None or raw == "":
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise GemmaUnavailable(f"Gemma tool-call arguments are not valid JSON: {raw[:200]!r}") from exc
        if isinstance(value, dict):
            return value
        return {"value": value}
    raise GemmaUnavailable(f"Unexpected tool-call arguments type: {type(raw).__name__}")


def parse_chat_completion(payload: dict[str, Any]) -> GemmaReply:
    """Turn an OpenAI-style ``chat.completion`` response into a :class:`GemmaReply`.

    Raises:
        GemmaUnavailable: when the payload has no usable choice.
    """
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise GemmaUnavailable("Gemma response has no choices")
    choice = choices[0]
    message = choice.get("message") or {}
    if not isinstance(message, dict):
        raise GemmaUnavailable("Gemma response choice has no message")
    text = message.get("content") or ""
    if not isinstance(text, str):
        text = json.dumps(text)
    calls: list[GemmaToolCall] = []
    for raw_call in message.get("tool_calls") or []:
        if not isinstance(raw_call, dict):
            continue
        fn = raw_call.get("function") or {}
        name = fn.get("name") if isinstance(fn, dict) else None
        if not name:
            continue
        calls.append(
            GemmaToolCall(
                name=str(name),
                arguments=_decode_arguments(fn.get("arguments")),
                id=raw_call.get("id"),
            )
        )
    usage_raw = payload.get("usage") or {}
    usage = GemmaUsage(
        prompt_tokens=int(usage_raw.get("prompt_tokens") or 0),
        completion_tokens=int(usage_raw.get("completion_tokens") or 0),
    )
    return GemmaReply(text=text, tool_calls=calls, usage=usage, finish_reason=choice.get("finish_reason"))


class OllamaGemmaClient:
    """Real client for Gemma served by Ollama's OpenAI-compatible endpoint.

    The blocking ``urllib`` call runs in a worker thread so the event loop
    stays free. When ``json_schema`` is given the request uses
    ``response_format={"type": "json_schema", ...}``; if the server rejects
    that with HTTP 400 the client retries once with ``{"type": "json_object"}``
    (and remembers, so later calls go straight to ``json_object``).
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        model: str = DEFAULT_MODEL,
        timeout: float = 60,
        *,
        api_key: str = "ollama",
    ) -> None:
        """Args:
        base_url: API root including ``/v1``.
        model: Ollama model tag.
        timeout: per-request timeout in seconds.
        api_key: placeholder bearer token (Ollama ignores it; no real key is needed).
        """
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.api_key = api_key
        self._json_schema_supported = True

    @property
    def endpoint(self) -> str:
        """Full URL of the chat completions endpoint."""
        return f"{self.base_url}/chat/completions"

    def build_payload(
        self,
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
        json_schema: dict | None = None,
        temperature: float = 0.1,
        use_json_schema: bool = True,
    ) -> dict[str, Any]:
        """Build the OpenAI-format request body."""
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "stream": False,
        }
        if tools:
            payload["tools"] = tools
        if json_schema is not None:
            if use_json_schema:
                payload["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {
                        "name": str(json_schema.get("title") or "response"),
                        "schema": json_schema,
                        "strict": True,
                    },
                }
            else:
                payload["response_format"] = {"type": "json_object"}
        return payload

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Blocking POST; maps every transport problem to :class:`GemmaUnavailable`."""
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.endpoint,
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read()
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8", "replace")[:300]
            except Exception:  # noqa: BLE001 - best-effort detail only
                detail = ""
            raise GemmaUnavailable(
                f"Gemma Shop Foreman at {self.base_url} returned HTTP {exc.code}: {detail}".rstrip(": "),
                status=exc.code,
            ) from exc
        except (TimeoutError, socket.timeout) as exc:
            raise GemmaUnavailable(
                f"Gemma Shop Foreman at {self.base_url} timed out after {self.timeout:g}s"
            ) from exc
        except urllib.error.URLError as exc:
            reason = exc.reason
            if isinstance(reason, (TimeoutError, socket.timeout)):
                raise GemmaUnavailable(
                    f"Gemma Shop Foreman at {self.base_url} timed out after {self.timeout:g}s"
                ) from exc
            raise GemmaUnavailable(
                f"Gemma Shop Foreman is not reachable at {self.base_url} ({reason}). "
                "Run ./setup_foreman.sh (macOS/Linux) or setup_foreman.bat (Windows) to start it."
            ) from exc
        except (ConnectionError, OSError) as exc:
            raise GemmaUnavailable(f"Gemma Shop Foreman connection to {self.base_url} failed: {exc}") from exc
        try:
            decoded = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GemmaUnavailable(f"Gemma Shop Foreman at {self.base_url} returned non-JSON body") from exc
        if not isinstance(decoded, dict):
            raise GemmaUnavailable("Gemma response is not a JSON object")
        if "error" in decoded and "choices" not in decoded:
            err = decoded["error"]
            msg = err.get("message") if isinstance(err, dict) else err
            raise GemmaUnavailable(f"Gemma Shop Foreman error: {msg}")
        return decoded

    async def chat(
        self,
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
        json_schema: dict | None = None,
        temperature: float = 0.1,
    ) -> GemmaReply:
        """POST to ``/chat/completions`` and parse the reply (see :class:`GemmaClient`)."""
        use_schema = self._json_schema_supported
        payload = self.build_payload(
            messages, tools=tools, json_schema=json_schema, temperature=temperature, use_json_schema=use_schema
        )
        try:
            raw = await asyncio.to_thread(self._post, payload)
        except GemmaUnavailable as exc:
            if json_schema is None or not use_schema or exc.status != 400:
                raise
            self._json_schema_supported = False
            payload = self.build_payload(
                messages, tools=tools, json_schema=json_schema, temperature=temperature, use_json_schema=False
            )
            raw = await asyncio.to_thread(self._post, payload)
        return parse_chat_completion(raw)


ResponderResult = Union[GemmaReply, str, dict, list, None]
Responder = Callable[[GemmaRequest], Union[ResponderResult, Awaitable[ResponderResult]]]


class SimulatedGemmaClient:
    """Deterministic Gemma stand-in.

    ``responder`` receives a :class:`GemmaRequest` and returns one of:

    * a :class:`GemmaReply` (used as is),
    * a ``str`` (becomes the reply text),
    * a ``dict`` with a ``"tool_calls": [{"name", "arguments"}, ...]`` key (and an
      optional ``"text"``) -> a tool-call reply; any other dict is JSON-encoded as text,
    * a ``list`` -> JSON-encoded as text,
    * ``None`` -> empty text.

    It may also be async, or raise (e.g. :class:`GemmaUnavailable`) to simulate
    an outage. With no responder the reply text is ``"{}"`` when a JSON schema
    is requested, else ``"ok"``. Every request is appended to :attr:`requests`.
    Usage is estimated as ``ceil(chars/4)``.
    """

    def __init__(self, responder: Responder | None = None, *, unavailable: bool = False) -> None:
        """Args:
        responder: callable producing the reply (see class docstring).
        unavailable: when True every call raises :class:`GemmaUnavailable`.
        """
        self.responder = responder
        self.unavailable = unavailable
        self.requests: list[GemmaRequest] = []

    @property
    def calls(self) -> int:
        """Number of chat calls received."""
        return len(self.requests)

    @staticmethod
    def _estimate(text: str) -> int:
        return (len(text) + 3) // 4

    def _coerce(self, result: ResponderResult) -> GemmaReply:
        if isinstance(result, GemmaReply):
            return result
        if result is None:
            return GemmaReply()
        if isinstance(result, str):
            return GemmaReply(text=result)
        if isinstance(result, dict) and "tool_calls" in result:
            calls = [
                c if isinstance(c, GemmaToolCall) else GemmaToolCall(
                    name=c["name"], arguments=_decode_arguments(c.get("arguments")), id=c.get("id")
                )
                for c in result.get("tool_calls") or []
            ]
            return GemmaReply(text=str(result.get("text") or ""), tool_calls=calls)
        if isinstance(result, (dict, list)):
            return GemmaReply(text=json.dumps(result))
        raise TypeError(f"SimulatedGemmaClient responder returned unsupported {type(result).__name__}")

    async def chat(
        self,
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
        json_schema: dict | None = None,
        temperature: float = 0.1,
    ) -> GemmaReply:
        """Record the request and return the responder's (deterministic) reply."""
        request = GemmaRequest(
            messages=[dict(m) for m in messages],
            tools=tools,
            json_schema=json_schema,
            temperature=temperature,
        )
        self.requests.append(request)
        if self.unavailable:
            raise GemmaUnavailable("Simulated Gemma Shop Foreman is unavailable")
        if self.responder is None:
            result: ResponderResult = "{}" if json_schema is not None else "ok"
        else:
            result = self.responder(request)
            if inspect.isawaitable(result):
                result = await result
        reply = self._coerce(result)
        if reply.usage.prompt_tokens == 0 and reply.usage.completion_tokens == 0:
            prompt_chars = sum(len(str(m.get("content") or "")) for m in messages)
            reply = reply.model_copy(
                update={
                    "usage": GemmaUsage(
                        prompt_tokens=(prompt_chars + 3) // 4,
                        completion_tokens=self._estimate(reply.text),
                    )
                }
            )
        return reply
