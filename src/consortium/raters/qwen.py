"""``QwenRater``: the Qwen adapter (story 2.3), OpenAI-compatible Chat Completions.

Speaks only the OpenAI-compatible Chat Completions API through the ``openai`` SDK
against ``ModelSpec.base_url`` (hosted Model Studio, or any OpenAI-compatible
server such as vLLM: moving is a change of ``base_url`` and ``model`` only).

**Clips.** ``prepare`` works offline: it reads ``clips/<clip_id>.mp4``, checks its
SHA-256 against the ``ClipRef`` and returns the Clip inline as a base64 data URI
(``data:;base64,...`` on Model Studio, as its docs show; ``data:video/mp4;base64,...``
on any other host, since vLLM needs the media type). Size is already enforced by ``push test``
(``limits.max_bytes`` with ``inline_base64``), so it is not checked again.

**Calls.** ``submit`` makes the call and returns its outcome as the handle (JSON
only: no data URI, no key), so a resumed handle is collected without being sent
again; each call is independent and ``submit`` never raises for a provider
outcome. Every part comes from ``core.prompt.compose``, one-to-one: text parts as
``text``, Clips as ``video_url``. The request always streams (some omni models
require it) with ``stream_options.include_usage``; nothing else is sent (no
``modalities``, no ``extra_body``). The SDK's own retries are disabled (the
engine's transient retry is the only retry) and a call times out after
``REQUEST_TIMEOUT_S``. ``aclose`` closes the client the Rater built. The settings actually sent are
archived with a per-setting ``documented`` flag from ``DOCUMENTED``.

The adapter never parses or validates the answer and never puts the API key in
``raw``, the handle or an error.
"""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse

import openai

from consortium.core.errors import ConsortiumError
from consortium.core.prompt import PROMPT_FORMAT, Media, Text, compose
from consortium.core.render import ClipRef
from consortium.raters.base import Category, Handle, MediaRef, ModelSpec, RaterCall, RaterResult

PROVIDER = "qwen"
DATA_URI_PREFIX = "data:;base64,"  # Model Studio's documented form
DATA_URI_PREFIX_MP4 = "data:video/mp4;base64,"  # any other host (vLLM needs the media type)
MESSAGE_MAX = 500
REQUEST_TIMEOUT_S = 600.0  # one chat.completions call, as for Gemini
STREAM_OPTIONS = {"include_usage": True}
OK_FINISH = frozenset({"stop", "length"})  # length: left to validation
REFUSED_FINISH = "content_filter"
NO_FINISH = "stream ended without finish_reason"

# Whether each sent setting is documented as honoured, on Alibaba Cloud Model Studio
# (a ``base_url`` host ending in ``aliyuncs.com``). Source: the Model Studio
# Qwen-Omni page (documents ``reasoning_effort``, ``stream``, ``stream_options``,
# ``modalities``; not ``temperature``, ``seed`` or ``top_p``) and the
# OpenAI-compatibility page (lists them without per-model guarantees),
# https://www.alibabacloud.com/help/en/model-studio/qwen-omni, checked 2026-10-02.
# On any other host (vLLM, self-hosted) every flag is None: "not assessed".
# ``prompt_format`` is ours, not a provider setting: its flag is always None.
DOCUMENTED_HOST_SUFFIX = "aliyuncs.com"
DOCUMENTED: Mapping[str, bool] = {
    "model": True,
    "max_tokens": True,
    "stream": True,
    "stream_options": True,
    "temperature": False,
    "seed": False,
    "reasoning_effort": True,
}

# Provider error codes, normalised (lowercase, without "_" and "."), matched by prefix
# (status errors and mid-stream errors alike).
REFUSED_CODE_PREFIXES = ("datainspectionfailed",)
FATAL_CODES = frozenset({"insufficientquota", "arrearage"})
TRANSIENT_CODE_PREFIXES = ("throttling", "limitrequests", "internalerror", "serviceunavailable")
TRANSIENT_STATUS = frozenset({408, 429})
TRANSPORT_ERRORS: tuple[type[BaseException], ...] = (
    openai.APIConnectionError,  # APITimeoutError included; the SDK wraps transport errors
    TimeoutError,
)
# A stream the SDK builds without validation: a malformed chunk or a non-JSON line.
MALFORMED_ERRORS: tuple[type[BaseException], ...] = (
    openai.APIResponseValidationError, ValueError, AttributeError, TypeError,
)
USAGE_INPUT_DETAILS = ("text_tokens", "audio_tokens", "video_tokens", "image_tokens")


def is_model_studio(base_url: str | None) -> bool:
    host = (urlparse(base_url or "").hostname or "").lower().rstrip(".")
    return host == DOCUMENTED_HOST_SUFFIX or host.endswith("." + DOCUMENTED_HOST_SUFFIX)


def documented_flag(base_url: str | None, name: str) -> bool | None:
    """``DOCUMENTED[name]`` on Model Studio; None ("not assessed") on any other host
    and for ``prompt_format``."""
    if name not in DOCUMENTED or not is_model_studio(base_url):
        return None
    return DOCUMENTED[name]


def _detail(details: Any, name: str) -> int | None:
    """A token count from a usage details object: a declared field or ``model_extra``."""
    if details is None:
        return None
    value = getattr(details, name, None)
    if value is None:
        value = (getattr(details, "model_extra", None) or {}).get(name)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _usage(usage: Any) -> dict[str, int]:
    if usage is None:
        return {}
    out = {"input_tokens": usage.prompt_tokens or 0,
           "output_tokens": usage.completion_tokens or 0}
    for name in USAGE_INPUT_DETAILS:
        value = _detail(usage.prompt_tokens_details, name)
        if value is not None:
            out[name] = value
    reasoning = _detail(usage.completion_tokens_details, "reasoning_tokens")
    if reasoning is not None:
        out["reasoning_tokens"] = reasoning
    return out


def _error_code(err: openai.APIError) -> str:
    code = err.code or (err.body.get("code") if isinstance(err.body, dict) else None)
    return str(code or err.type or "")


def _error_message(err: openai.APIError) -> str:
    if isinstance(err.body, dict) and isinstance(err.body.get("message"), str):
        return err.body["message"]
    return str(err.message or "")


def normalise_code(code: str) -> str:
    return code.lower().replace("_", "").replace(".", "")


def _category(err: openai.APIError) -> Category:
    """The ``Category`` of an API error (status or mid-stream): by code, then status."""
    code = normalise_code(_error_code(err))
    if code.startswith(REFUSED_CODE_PREFIXES):
        return "refused"
    if code in FATAL_CODES:
        return "fatal"
    if isinstance(err, openai.APIStatusError):  # any other 4xx stays fatal, whatever its code
        status = err.status_code
        return "transient" if status in TRANSIENT_STATUS or status >= 500 else "fatal"
    if code.startswith(TRANSIENT_CODE_PREFIXES):  # mid-stream: no status, the code decides
        return "transient"
    return "fatal"  # a mid-stream error with an unknown code


class QwenRater:
    provider = PROVIDER

    def __init__(self, spec: ModelSpec, client: Any = None) -> None:
        self.spec = spec
        self._client = client
        self._owns_client = client is None

    # ------------------------------------------------------------------ helpers

    @property
    def client(self) -> Any:
        if self._client is None:  # lazily: building a Rater makes no network call
            self._client = openai.AsyncOpenAI(
                api_key=self.spec.api_key, base_url=self.spec.base_url, max_retries=0,
                timeout=REQUEST_TIMEOUT_S,
            )
        return self._client

    async def aclose(self) -> None:
        """Close the client this Rater built (an injected client is left alone)."""
        if self._owns_client and self._client is not None:
            client, self._client = self._client, None
            await client.close()

    def _redact(self, text: str) -> str:
        key = self.spec.api_key
        return text.replace(key, "***") if key else text

    def _summary(self, err: BaseException) -> str:
        """``<status> <code>: <message>`` (or ``<type>: <message>``): redacted, then cut."""
        if isinstance(err, openai.APIStatusError):
            head = f"{err.status_code} {_error_code(err) or 'error'}: "
            message = _error_message(err)
        elif type(err) is openai.APIError:  # how the SDK raises an error inside the stream
            head = f"stream {_error_code(err) or 'error'}: "
            message = _error_message(err)
        else:
            head, message = f"{type(err).__name__}: ", str(err)
        return self._redact(head) + self._redact(message)[:MESSAGE_MAX]

    # ------------------------------------------------------------------ prepare

    async def prepare(self, clip: ClipRef) -> MediaRef:
        """The Clip inline as a base64 data URI, after checking its SHA-256 (offline)."""
        fail = f"{self.spec.model_id}: Clip {clip.clip_id}"
        path = self.spec.clips_dir / f"{clip.clip_id}.mp4"
        try:
            data = path.read_bytes()
        except OSError as err:
            raise ConsortiumError(
                "prepare_failed", f"{fail}: local Clip file {path.name} unreadable: "
                f"{err.strerror or err}"
            ) from None
        if hashlib.sha256(data).hexdigest() != clip.sha256.lower():
            raise ConsortiumError(
                "prepare_failed", f"{fail}: local Clip file does not match the Clip's SHA-256"
            )
        prefix = DATA_URI_PREFIX if is_model_studio(self.spec.base_url) else DATA_URI_PREFIX_MP4
        ref = prefix + base64.b64encode(data).decode("ascii")
        return MediaRef(clip.clip_id, clip.sha256, ref)

    # ------------------------------------------------------------------ submit

    def settings_for(self, call: RaterCall) -> dict[str, dict[str, Any]]:
        """The settings sent for ``call``, as ``{name: {"value", "documented"}}``."""
        spec = self.spec
        sent: dict[str, Any] = {
            "model": spec.model, "max_tokens": spec.max_output_tokens, "stream": True,
            "stream_options": dict(STREAM_OPTIONS), "temperature": spec.temperature,
        }
        if spec.seed_supported:
            sent["seed"] = call.seed
        if spec.reasoning_effort is not None:
            sent["reasoning_effort"] = spec.reasoning_effort
        sent["prompt_format"] = PROMPT_FORMAT
        return {name: {"value": value, "documented": documented_flag(spec.base_url, name)}
                for name, value in sent.items()}

    def messages_for(self, call: RaterCall) -> list[dict[str, Any]]:
        refs = {ref.clip_id: ref.ref for ref in call.media}
        content: list[dict[str, Any]] = []
        for part in compose(call.request):
            if isinstance(part, Text):
                content.append({"type": "text", "text": part.text})
            elif isinstance(part, Media):
                content.append({"type": "video_url", "video_url": {"url": refs[part.clip_id]}})
        return [{"role": "user", "content": content}]

    def request_for(self, call: RaterCall) -> dict[str, Any]:
        """The keyword arguments of ``chat.completions.create`` for ``call``."""
        spec = self.spec
        kwargs: dict[str, Any] = {
            "model": spec.model,
            "messages": self.messages_for(call),
            "max_tokens": spec.max_output_tokens,
            "temperature": spec.temperature,
        }
        if spec.seed_supported:
            kwargs["seed"] = call.seed
        if spec.reasoning_effort is not None:
            kwargs["reasoning_effort"] = spec.reasoning_effort
        kwargs["stream"] = True
        kwargs["stream_options"] = dict(STREAM_OPTIONS)
        return kwargs

    def _check_call(self, call: RaterCall) -> None:
        """``adapter_error`` for a request ``compose`` refuses or a Clip not in ``call.media``."""
        try:
            parts = compose(call.request)
        except ValueError as err:
            raise ConsortiumError(
                "adapter_error", f"{PROVIDER} Rater: {call.trial_id}: {err}"
            ) from None
        prepared = {ref.clip_id for ref in call.media}
        missing = sorted({p.clip_id for p in parts if isinstance(p, Media)} - prepared)
        if missing:
            raise ConsortiumError(
                "adapter_error",
                f"{PROVIDER} Rater: {call.trial_id}: Clip(s) {', '.join(missing)} not prepared",
            )

    async def _call(self, call: RaterCall) -> Handle:
        settings = self.settings_for(call)
        texts: list[str] = []
        usage: Any = None
        build: str | None = None
        finish: str | None = None
        try:
            stream = await self.client.chat.completions.create(**self.request_for(call))
            async for chunk in stream:
                if chunk.system_fingerprint:
                    build = chunk.system_fingerprint
                if chunk.usage is not None:
                    usage = chunk.usage
                for choice in chunk.choices or []:
                    if choice.index != 0:  # only the first choice is read
                        continue
                    if choice.delta is not None and choice.delta.content:
                        texts.append(choice.delta.content)
                    if choice.finish_reason:
                        finish = choice.finish_reason
        except TRANSPORT_ERRORS as err:  # before APIError: a connection error is one
            return self._handle(self._summary(err), {}, None, "transient", settings)
        except MALFORMED_ERRORS as err:  # before APIError: a validation error is one
            return self._handle(self._summary(err), {}, None, "fatal", settings)
        except openai.APIError as err:  # a status error or a mid-stream error
            return self._handle(self._summary(err), {}, None, _category(err), settings)
        raw = "".join(texts)
        if finish is None:
            return self._handle(NO_FINISH, _usage(usage), build, "transient", settings)
        category: Category = "ok"
        if finish not in OK_FINISH:
            tail = f"finish_reason: {finish}"
            raw = f"{raw}\n{tail}" if raw else tail
            category = "refused" if finish == REFUSED_FINISH else "fatal"
        return self._handle(self._redact(raw), _usage(usage), build, category, settings)

    async def _safe_call(self, call: RaterCall) -> Handle:
        """``_call``; any unexpected error becomes a ``fatal`` handle (never raised)."""
        try:
            return await self._call(call)
        except Exception as err:  # noqa: BLE001 - one bad call never loses the others
            return self._handle(self._summary(err), {}, None, "fatal", self.settings_for(call))

    def _handle(
        self, raw: str, usage: dict[str, int], build: str | None, category: Category,
        settings: dict[str, Any],
    ) -> Handle:
        return {"provider": PROVIDER, "raw": raw, "usage": usage, "model_build": build,
                "category": category, "settings": settings}

    async def submit(self, calls: list[RaterCall]) -> list[Handle]:
        for call in calls:  # before any call: a request the adapter cannot send is a bug
            self._check_call(call)
        return [await self._safe_call(c) for c in calls]

    async def collect(self, handles: list[Handle]) -> list[RaterResult]:
        return [
            RaterResult(raw=h["raw"], usage=dict(h.get("usage") or {}),
                        model_build=h.get("model_build"), category=h["category"],
                        settings=h.get("settings"))
            for h in handles
        ]
