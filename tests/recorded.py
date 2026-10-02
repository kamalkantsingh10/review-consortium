"""Recorded-response doubles for the real adapters (no network).

``RecordedQwenClient`` (story 2.3, below) stands in for ``openai.AsyncOpenAI``.

``RecordedGeminiClient`` stands in for ``genai.Client``: ``aio.files``
(``get``/``upload``/``delete``) keeps an in-memory File store, and
``aio.models.generate_content`` replays a queue of recorded outcomes: a
``GenerateContentResponse`` JSON object, an ``{"api_error": {code, status,
message}}`` object (raised as the SDK's ``ClientError``/``ServerError``) or
``{"transport_error": "<message>"}`` (raised as ``httpx.ConnectError``); once the
queue is empty, an optional ``responder(contents)`` builds the response. Every call
is logged in ``calls`` as ``(op, kwargs)``.
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import openai
from google.genai import errors, types
from openai._base_client import httpx2 as _sdk_http  # the SDK's HTTP types, via the SDK
from openai._models import construct_type
from openai.types.chat import ChatCompletionChunk

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)


def fixture(provider: str, name: str) -> Any:
    return json.loads((FIXTURES / provider / f"{name}.json").read_text(encoding="utf-8"))


def api_error(code: int, status: str, message: str = "recorded error") -> errors.APIError:
    body = {"error": {"code": code, "status": status, "message": message}}
    cls = errors.ServerError if code >= 500 else errors.ClientError
    return cls(code, body)


def _raise_or_return(item: Any) -> Any:
    if isinstance(item, BaseException):
        raise item
    if isinstance(item, dict) and "api_error" in item:
        e = item["api_error"]
        raise api_error(e["code"], e["status"], e.get("message", "recorded error"))
    if isinstance(item, dict) and "transport_error" in item:
        raise httpx.ConnectError(item["transport_error"])
    return item


def provider_hash(sha256_hex: str) -> str:
    """How the File API reports ``sha256_hash``: base64 of the hex digest text."""
    return base64.b64encode(sha256_hex.encode("ascii")).decode("ascii")


def file_entry(sha256_hex: str, states: list[str], expires: datetime | None) -> dict[str, Any]:
    """A pre-existing File for ``RecordedGeminiClient(store=...)``."""
    return {"states": states, "expires": expires, "sha": provider_hash(sha256_hex)}


class _Files:
    def __init__(self, owner: RecordedGeminiClient) -> None:
        self.o = owner

    def _file(self, name: str, state: str, entry: dict[str, Any]) -> types.File:
        return types.File(
            name=name, uri=entry.get("uri", f"https://files.test/{name}"), state=state,
            expiration_time=entry["expires"], create_time=entry.get("created"),
            sha256_hash=entry.get("sha"), mime_type="video/mp4",
        )

    async def get(self, *, name: str, config: Any = None) -> types.File:
        self.o.calls.append(("files.get", {"name": name}))
        if self.o.get_errors:
            err = self.o.get_errors.pop(0)
            if err is not None:
                _raise_or_return(err)
        entry = self.o.store.get(name)
        if entry is None:
            raise api_error(self.o.missing_code, "NOT_FOUND", f"File {name} not found")
        states = entry["states"]
        state = states.pop(0) if len(states) > 1 else states[0]
        return self._file(name, state, entry)

    async def upload(self, *, file: Any, config: Any = None) -> types.File:
        name = config.name
        self.o.calls.append(("files.upload", {"file": str(file), "name": name,
                                              "display_name": config.display_name,
                                              "mime_type": config.mime_type}))
        if self.o.upload_errors:
            _raise_or_return(self.o.upload_errors.pop(0))
        if name in self.o.store:
            raise api_error(409, "ALREADY_EXISTS", f"File {name} already exists")
        sha = hashlib.sha256(Path(file).read_bytes()).hexdigest()
        states = list(self.o.upload_states)
        expires = self.o.expires or self.o.clock() + timedelta(hours=48)
        entry = {"states": states[1:] or states, "expires": expires,
                 "sha": provider_hash(sha)}
        self.o.store[name] = entry
        return self._file(name, states[0], entry)

    async def delete(self, *, name: str, config: Any = None) -> None:
        self.o.calls.append(("files.delete", {"name": name}))
        self.o.store.pop(name, None)


class _Models:
    def __init__(self, owner: RecordedGeminiClient) -> None:
        self.o = owner

    async def generate_content(self, *, model: str, contents: Any, config: Any = None) -> Any:
        self.o.calls.append(("generate_content",
                             {"model": model, "contents": contents, "config": config}))
        if self.o.responses:
            item = _raise_or_return(self.o.responses.pop(0))
        elif self.o.responder is not None:
            item = self.o.responder(contents)
        else:
            raise AssertionError("no recorded response left")
        return types.GenerateContentResponse.model_validate(item)


class RecordedGeminiClient:
    def __init__(
        self,
        responses: list[Any] | None = None,
        *,
        upload_states: tuple[str, ...] = ("PROCESSING", "ACTIVE"),
        expires: datetime | None = None,
        store: dict[str, dict[str, Any]] | None = None,
        responder: Callable[[Any], Any] | None = None,
    ) -> None:
        self.responder = responder  # answers once the recorded queue is empty
        self.missing_code = 403  # what files.get answers for a missing File
        self.responses = list(responses or [])
        self.upload_states = upload_states
        self.expires = expires  # None: 48 h after the upload, by ``clock``
        self.clock: Callable[[], datetime] = lambda: NOW
        self.store: dict[str, dict[str, Any]] = store or {}
        self.upload_errors: list[Any] = []
        self.get_errors: list[Any] = []  # None entries: that get succeeds
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.aio = SimpleNamespace(files=_Files(self), models=_Models(self))

    def ops(self, op: str) -> list[dict[str, Any]]:
        return [kw for o, kw in self.calls if o == op]


# --------------------------------------------------------------------------- qwen (story 2.3)

QWEN_URL = "https://ws-test.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1"


def _qwen_request(base_url: str = QWEN_URL) -> _sdk_http.Request:
    return _sdk_http.Request("POST", f"{base_url}/chat/completions")


def qwen_status_error(status: int, body: Any) -> openai.APIStatusError:
    """The SDK's own status error for an HTTP ``status`` with JSON ``body`` (the
    exception class and ``code`` come from the SDK's mapping, as on the wire)."""
    response = _sdk_http.Response(status, json=body, request=_qwen_request())
    sdk = openai.AsyncOpenAI(api_key="recorded", base_url=QWEN_URL, max_retries=0)
    return sdk._make_status_error_from_response(response)


def _qwen_raise(item: Any) -> None:
    if isinstance(item, BaseException):
        raise item
    if not isinstance(item, dict):
        return
    if "status_error" in item:
        e = item["status_error"]
        raise qwen_status_error(e["status"], e["body"])
    if "stream_error" in item:  # how the SDK raises an ``{"error": ...}`` event mid-stream
        raise openai.APIError(item["stream_error"].get("message", "stream error"),
                              _qwen_request(), body=item["stream_error"])
    if "transport_error" in item:  # the SDK wraps a transport error as APIConnectionError
        raise openai.APIConnectionError(message=item["transport_error"],
                                        request=_qwen_request())
    if "bad_sse" in item:  # a non-JSON SSE data line: the SDK's json.loads fails
        json.loads(item["bad_sse"])
    if "connection_error" in item:
        raise openai.APIConnectionError(message=item["connection_error"],
                                        request=_qwen_request())
    if "timeout" in item:
        raise openai.APITimeoutError(_qwen_request())


class _QwenStream:
    """An async stream of ``ChatCompletionChunk`` built as the SDK builds them
    (``construct_type``: no validation); a non-chunk entry raises mid-stream."""

    def __init__(self, entries: list[Any]) -> None:
        self.entries = list(entries)

    def __aiter__(self) -> _QwenStream:
        return self

    async def __anext__(self) -> ChatCompletionChunk:
        if not self.entries:
            raise StopAsyncIteration
        entry = self.entries.pop(0)
        if isinstance(entry, dict) and not ({"stream_error", "transport_error", "bad_sse"}
                                            & entry.keys()):
            return construct_type(type_=ChatCompletionChunk, value=entry)
        _qwen_raise(entry)
        raise AssertionError(f"unknown recorded stream entry {entry!r}")


class _QwenCompletions:
    def __init__(self, owner: RecordedQwenClient) -> None:
        self.o = owner

    async def create(self, **kwargs: Any) -> _QwenStream:
        self.o.calls.append(("chat.completions.create", kwargs))
        if self.o.responses:
            item = self.o.responses.pop(0)
        elif self.o.responder is not None:
            item = self.o.responder(kwargs)
        else:
            raise AssertionError("no recorded response left")
        _qwen_raise(item)  # an error before the stream starts
        return _QwenStream(item["chunks"])


class RecordedQwenClient:
    """Stands in for ``openai.AsyncOpenAI``: ``chat.completions.create(**kwargs)``
    replays a queue of recorded outcomes. Each is ``{"chunks": [...]}`` (an async
    stream of ``ChatCompletionChunk`` JSON objects, in which a ``{"stream_error":
    {code, message}}``, ``{"transport_error": msg}`` (an ``APIConnectionError``) or
    ``{"bad_sse": text}`` (a ``JSONDecodeError``) entry raises mid-stream as the SDK
    would), or an error raised by ``create``: ``{"status_error": {status,
    body}}`` (the SDK's status error for that HTTP response), ``{"connection_error":
    msg}``, ``{"timeout": true}``, or an exception. Once the queue is empty, an
    optional ``responder(kwargs)`` builds the outcome. Calls are logged in ``calls``.
    """

    def __init__(self, responses: list[Any] | None = None, *,
                 responder: Callable[[dict[str, Any]], Any] | None = None) -> None:
        self.responses = list(responses or [])
        self.responder = responder
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.chat = SimpleNamespace(completions=_QwenCompletions(self))

    def ops(self, op: str = "chat.completions.create") -> list[dict[str, Any]]:
        return [kw for o, kw in self.calls if o == op]


def qwen_chunks(text: str, *, finish: str = "stop", usage: dict[str, Any] | None = None,
                fingerprint: str | None = None, model: str = "qwen3.8-omni-flash") -> dict:
    """A recorded stream answering ``text`` (one delta per 8 characters)."""
    base = {"id": "chatcmpl-rec", "object": "chat.completion.chunk", "created": 1790000000,
            "model": model, "system_fingerprint": fingerprint}
    pieces = [text[i:i + 8] for i in range(0, len(text), 8)] or [""]
    chunks = [base | {"choices": [{"index": 0, "delta": {"role": "assistant", "content": p},
                                   "finish_reason": None}]} for p in pieces]
    chunks.append(base | {"choices": [{"index": 0, "delta": {}, "finish_reason": finish}]})
    if usage is not None:
        chunks.append(base | {"choices": [], "usage": usage})
    return {"chunks": chunks}
