"""``GeminiRater``: the Gemini adapter (story 2.2), ``google-genai`` ``generate_content``.

**Files.** ``prepare`` makes each Clip available through the File API under a
content-addressed name, ``files/rc-<first 24 hex of the Clip's SHA-256>`` (display
name the Clip ID), so one key's Files are shared safely across Studies, Models and
Raters. A File is reused only when it is ACTIVE, its ``sha256_hash`` matches the
Clip and it is more than an hour from expiry (a File with no
``expiration_time`` is assumed to expire 47 h after it was created). A File is
never deleted: when the name holds an unusable File (FAILED, other content, or
near expiry) the next versioned name (``-v2``, ``-v3``, ...) is tried, and a
missing name is uploaded (``409`` on upload: someone else just did; it is read
back). A PROCESSING File is polled every 2 s until ACTIVE (300 s at most).
Transient errors (408/429/5xx, transport errors, timeouts) during lookup, upload
or polling are retried 3 times (2/4/8 s); any other error, an upload that FAILED,
a timeout or an ACTIVE File without a URI is ``ConsortiumError("prepare_failed")``.
``prepare`` runs one at a time per Clip.

**Calls.** ``submit`` makes the call and returns its outcome as the handle (JSON
only: no File URI, no key), so a resumed handle is collected without being sent
again; each call is independent and ``submit`` never raises for a provider
outcome (a failed re-prepare becomes a ``transient`` handle, ``fatal`` for an
auth error). Every part comes from ``core.prompt.compose``, one-to-one; video
parts are pinned to static media processing (custom fps works only there). The
SDK's own retries are disabled (the engine's transient retry is the only retry)
and a call times out after ``GENERATE_TIMEOUT_S``. The settings actually sent are
archived with the result.

The adapter never parses or validates the answer, never uses structured output,
and never puts the API key in ``raw``, the handle or an error.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import re
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pydantic
from google import genai
from google.genai import errors, types

from consortium.core.errors import ConsortiumError
from consortium.core.prompt import PROMPT_FORMAT, Media, Text, compose
from consortium.core.render import ClipRef
from consortium.raters.base import Category, Handle, MediaRef, ModelSpec, RaterCall, RaterResult

try:  # the SDK's optional aiohttp transport
    import aiohttp

    _AIOHTTP_ERRORS: tuple[type[BaseException], ...] = (aiohttp.ClientError,)
except ImportError:  # pragma: no cover - depends on the environment
    _AIOHTTP_ERRORS = ()

PROVIDER = "gemini"
MIME = "video/mp4"
EXPIRY_MARGIN = timedelta(hours=1)
ASSUMED_LIFETIME = timedelta(hours=47)  # a File with no expiration_time (kept 48 h)
MAX_VERSIONS = 8
POLL_INTERVAL_S = 2.0
POLL_TIMEOUT_S = 300.0
BACKOFF_S = (2.0, 4.0, 8.0)
GENERATE_TIMEOUT_S = 600  # one generate_content call
FILE_TIMEOUT_S = 600  # one File API request (an upload included)
TRANSIENT_CODES = frozenset({408, 429, 500, 502, 503, 504})
FILE_ACCESS_CODES = frozenset({403, 404})
AUTH_CODES = frozenset({401, 403})
CONFLICT = 409
REFUSAL_FINISH = frozenset({
    "SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "RECITATION",
    "IMAGE_SAFETY", "IMAGE_PROHIBITED_CONTENT",
})
TRANSPORT_ERRORS: tuple[type[BaseException], ...] = (
    httpx.TransportError, TimeoutError, *_AIOHTTP_ERRORS,
)
MESSAGE_MAX = 500
_HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")


def file_name(sha256: str, version: int = 1) -> str:
    """The File API name of a Clip's content: ``files/rc-<24 hex>`` (``-v<n>`` from 2)."""
    base = "files/rc-" + sha256[:24].lower()
    return base if version == 1 else f"{base}-v{version}"


def provider_sha256(value: str | None) -> str | None:
    """A File's ``sha256_hash`` as lowercase hex: hex as is, or base64 of the digest
    or of its hex text. None when absent or in an unknown encoding."""
    if not value:
        return None
    value = value.strip()
    if _HEX64.match(value):
        return value.lower()
    try:
        raw = base64.b64decode(value + "=" * (-len(value) % 4))
    except (binascii.Error, ValueError):
        return None
    if len(raw) == 32:
        return raw.hex()
    text = raw.decode("ascii", errors="replace")
    return text.lower() if _HEX64.match(text) else None


def _http_options(timeout_s: int) -> types.HttpOptions:
    return types.HttpOptions(
        timeout=timeout_s * 1000, retry_options=types.HttpRetryOptions(attempts=1)
    )


def _name(value: Any) -> str | None:
    """An SDK enum (or a plain string) as its name, e.g. ``"STOP"``."""
    if value is None:
        return None
    return str(getattr(value, "value", value))


def _usage(response: Any) -> dict[str, int]:
    um = getattr(response, "usage_metadata", None)
    if um is None:
        return {}
    by_modality: dict[str, int] = {}
    for d in um.prompt_tokens_details or []:
        key = (_name(d.modality) or "").upper()
        by_modality[key] = by_modality.get(key, 0) + (d.token_count or 0)
    thoughts = um.thoughts_token_count or 0
    return {
        "input_tokens": (um.prompt_token_count or 0) + (um.tool_use_prompt_token_count or 0),
        "output_tokens": (um.candidates_token_count or 0) + thoughts,
        "text_tokens": by_modality.get("TEXT", 0),
        "video_tokens": by_modality.get("VIDEO", 0),
        "audio_tokens": by_modality.get("AUDIO", 0),
        "thoughts_tokens": thoughts,
    }


def _text(candidate: Any) -> str:
    content = getattr(candidate, "content", None)
    parts = getattr(content, "parts", None) or []
    return "".join(p.text for p in parts if p.text and not p.thought)


def _is_refusal(finish: str | None) -> bool:
    return finish is not None and (finish in REFUSAL_FINISH or "SAFETY" in finish)


def _mentions_file(err: errors.APIError) -> bool:
    text = f"{err.message or ''} {err.status or ''}".lower()
    return "file" in text or "uri" in text


class _PrepareError(ConsortiumError):
    """``prepare_failed``, with the category a failed re-prepare inside ``submit`` maps to."""

    def __init__(self, message: str, category: Category = "transient") -> None:
        super().__init__("prepare_failed", message)
        self.category = category


class GeminiRater:
    provider = PROVIDER

    def __init__(
        self,
        spec: ModelSpec,
        client: Any = None,
        now: Callable[[], datetime] | None = None,
        sleep: Callable[[float], Awaitable[Any]] | None = None,
    ) -> None:
        self.spec = spec
        self._client = client
        self._owns_client = client is None
        self._now = now or (lambda: datetime.now(UTC))
        self._sleep = sleep or asyncio.sleep
        self._expiry: dict[str, datetime] = {}  # clip_id -> effective expiration
        self._uri: dict[str, str] = {}  # clip_id -> current File URI
        self.file_names: dict[str, str] = {}  # clip_id -> current File name
        self._locks: dict[str, asyncio.Lock] = {}  # Clip SHA-256 -> prepare lock

    # ------------------------------------------------------------------ helpers

    @property
    def client(self) -> Any:
        if self._client is None:  # lazily: building a Rater makes no network call
            self._client = genai.Client(
                api_key=self.spec.api_key, http_options=_http_options(FILE_TIMEOUT_S)
            )
        return self._client

    async def aclose(self) -> None:
        """Close the client this Rater built (an injected client is left alone)."""
        if self._owns_client and self._client is not None:
            client, self._client = self._client, None
            await client.aio.aclose()

    def _redact(self, text: str) -> str:
        key = self.spec.api_key
        return text.replace(key, "***") if key else text

    def _summary(self, err: BaseException) -> str:
        """``<code> <status>: <message>`` (or ``<type>: <message>``): redacted, then cut."""
        if isinstance(err, errors.APIError):
            head, message = f"{err.code} {err.status}: ", str(err.message or "")
        else:
            head, message = f"{type(err).__name__}: ", str(err)
        return self._redact(head) + self._redact(message)[:MESSAGE_MAX]

    def _near_expiry(self, expiration: datetime | None) -> bool:
        return expiration is not None and expiration < self._now() + EXPIRY_MARGIN

    def _expiry_of(self, file: Any) -> datetime:
        if file.expiration_time is not None:
            return file.expiration_time
        return (file.create_time or self._now()) + ASSUMED_LIFETIME

    # ------------------------------------------------------------------ prepare

    async def _retry(self, what: str, fail: str, fn: Callable[[], Awaitable[Any]]) -> Any:
        """``fn()``, retried on transient errors (2/4/8 s); else ``_PrepareError``."""
        for attempt in range(len(BACKOFF_S) + 1):
            try:
                return await fn()
            except errors.APIError as err:
                if err.code not in TRANSIENT_CODES:
                    category: Category = "fatal" if err.code in AUTH_CODES else "transient"
                    raise _PrepareError(
                        f"{fail}: {what} failed: {self._summary(err)}", category
                    ) from None
                last: BaseException = err
            except TRANSPORT_ERRORS as err:
                last = err
            if attempt == len(BACKOFF_S):
                raise _PrepareError(f"{fail}: {what} failed: {self._summary(last)}") from None
            await self._sleep(BACKOFF_S[attempt])
        raise AssertionError("unreachable")

    async def _get(self, name: str) -> Any | None:
        try:
            return await self.client.aio.files.get(name=name)
        except errors.APIError as err:
            if err.code in FILE_ACCESS_CODES:  # the File API answers 403 for a missing File
                return None
            raise

    async def _upload(self, path: Path, name: str, clip: ClipRef, fail: str) -> Any | None:
        """The uploaded File, or None when ``name`` already exists (409)."""
        try:
            return await self.client.aio.files.upload(
                file=str(path),
                config=types.UploadFileConfig(
                    name=name, display_name=clip.clip_id, mime_type=MIME,
                    http_options=_http_options(FILE_TIMEOUT_S),
                ),
            )
        except errors.APIError as err:
            if err.code == CONFLICT:
                return None
            raise
        except OSError as err:
            raise _PrepareError(f"{fail}: local Clip file unreadable: {err}", "fatal") from None

    @staticmethod
    def _check_local(path: Path, fail: str) -> None:
        try:
            with path.open("rb") as fh:
                fh.read(1)
        except OSError as err:
            raise _PrepareError(
                f"{fail}: local Clip file {path.name} unreadable: {err.strerror or err}", "fatal"
            ) from None

    async def _poll(self, file: Any, name: str, fail: str) -> Any | None:
        """``file`` once ACTIVE; None when it FAILED."""
        waited = 0.0
        while (state := _name(file.state)) != "ACTIVE":
            if state == "FAILED":
                return None
            if waited >= POLL_TIMEOUT_S:
                raise _PrepareError(f"{fail}: not ACTIVE after {POLL_TIMEOUT_S:.0f} s")
            await self._sleep(POLL_INTERVAL_S)
            waited += POLL_INTERVAL_S
            file = await self._retry("polling", fail, lambda: self._get(name))
            if file is None:
                raise _PrepareError(f"{fail}: File {name} disappeared while processing")
        return file

    async def prepare(self, clip: ClipRef) -> MediaRef:
        """Make ``clip`` available (reuse or upload its File) and wait until it is ACTIVE."""
        lock = self._locks.setdefault(clip.sha256, asyncio.Lock())
        async with lock:
            return await self._prepare(clip)

    async def _prepare(self, clip: ClipRef) -> MediaRef:
        fail = f"{self.spec.model_id}: Clip {clip.clip_id}"
        path = self.spec.clips_dir / f"{clip.clip_id}.mp4"
        for version in range(1, MAX_VERSIONS + 1):
            name = file_name(clip.sha256, version)
            file = await self._retry("lookup", fail, lambda n=name: self._get(n))
            fresh = file is None
            if fresh:
                self._check_local(path, fail)
                file = await self._retry(
                    "upload", fail, lambda n=name: self._upload(path, n, clip, fail)
                )
                if file is None:  # 409: another uploader got there first; read it back
                    file = await self._retry("lookup", fail, lambda n=name: self._get(n))
                    if file is None:
                        raise _PrepareError(f"{fail}: File {name} exists but cannot be read")
            file = await self._poll(file, name, fail)
            if file is None:
                if fresh:
                    raise _PrepareError(f"{fail}: File processing FAILED")
                continue  # an older copy FAILED: never deleted, try the next name
            got = provider_sha256(file.sha256_hash)
            if got is not None and got != clip.sha256.lower():
                if fresh:
                    raise _PrepareError(
                        f"{fail}: uploaded content does not match the Clip's SHA-256", "fatal"
                    )
                continue  # other content under this name: treated as missing
            expiry = self._expiry_of(file)
            if self._near_expiry(expiry):
                continue  # never delete an ACTIVE File: upload a fresh copy under a new name
            if not file.uri:
                raise _PrepareError(f"{fail}: ACTIVE File {name} has no URI")
            self._expiry[clip.clip_id] = expiry
            self._uri[clip.clip_id] = file.uri
            self.file_names[clip.clip_id] = name
            return MediaRef(clip.clip_id, clip.sha256, file.uri)
        raise _PrepareError(f"{fail}: no usable File under {MAX_VERSIONS} names")

    # ------------------------------------------------------------------ submit

    def settings_for(self, call: RaterCall, has_media: bool) -> dict[str, dict[str, Any]]:
        """The settings sent for ``call``, as ``{name: {"value", "documented"}}``."""
        spec = self.spec
        sent: dict[str, Any] = {"model": spec.model, "temperature": spec.temperature}
        if spec.seed_supported:
            sent["seed"] = call.seed
        if has_media:
            if spec.fps is not None:
                sent["fps"] = spec.fps
            sent["media_processing"] = "static"
        if spec.media_resolution is not None:
            sent["media_resolution"] = spec.media_resolution
        if spec.thinking_level is not None:
            sent["thinking_level"] = spec.thinking_level
        sent["max_output_tokens"] = spec.max_output_tokens
        sent["prompt_format"] = PROMPT_FORMAT
        return {name: {"value": value, "documented": True} for name, value in sent.items()}

    def config_for(self, call: RaterCall) -> types.GenerateContentConfig:
        spec = self.spec
        kwargs: dict[str, Any] = {
            "temperature": spec.temperature,
            "max_output_tokens": spec.max_output_tokens,
            "http_options": _http_options(GENERATE_TIMEOUT_S),
        }
        if spec.seed_supported:
            kwargs["seed"] = call.seed
        if spec.media_resolution is not None:
            kwargs["media_resolution"] = types.MediaResolution[
                f"MEDIA_RESOLUTION_{spec.media_resolution.upper()}"
            ]
        if spec.thinking_level is not None:
            kwargs["thinking_config"] = types.ThinkingConfig(
                thinking_level=types.ThinkingLevel[spec.thinking_level.upper()]
            )
        return types.GenerateContentConfig(**kwargs)

    def contents_for(self, call: RaterCall, uris: Mapping[str, str]) -> types.Content:
        parts: list[types.Part] = []
        for part in compose(call.request):
            if isinstance(part, Text):
                parts.append(types.Part(text=part.text))
            elif isinstance(part, Media):
                parts.append(types.Part(
                    file_data=types.FileData(file_uri=uris[part.clip_id], mime_type=MIME),
                    media_processing=types.MediaProcessing.STATIC,
                    video_metadata=(
                        types.VideoMetadata(fps=self.spec.fps)
                        if self.spec.fps is not None else None
                    ),
                ))
        return types.Content(role="user", parts=parts)

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

    async def _reprepare(self, call: RaterCall, uris: dict[str, str], only_expiring: bool) -> None:
        for ref in call.media:
            if only_expiring and not self._near_expiry(self._expiry.get(ref.clip_id)):
                continue
            fresh = await self.prepare(ClipRef(ref.clip_id, ref.sha256))
            uris[ref.clip_id] = fresh.ref

    def _outcome(self, response: Any) -> tuple[str, Category]:
        feedback = getattr(response, "prompt_feedback", None)
        block = _name(getattr(feedback, "block_reason", None))
        if block:
            return f"blocked: {block}", "refused"
        candidates = getattr(response, "candidates", None) or []
        if not candidates:
            return "", "ok"
        candidate = candidates[0]
        text = _text(candidate)
        finish = _name(candidate.finish_reason)
        if _is_refusal(finish):
            tail = f"finish_reason: {finish}"
            return (f"{text}\n{tail}" if text else tail), "refused"
        return text, "ok"

    async def _call(self, call: RaterCall) -> Handle:
        settings = self.settings_for(call, has_media=bool(call.media))
        uris = {ref.clip_id: self._uri.get(ref.clip_id, ref.ref) for ref in call.media}
        try:
            await self._reprepare(call, uris, only_expiring=True)
            config = self.config_for(call)
            retried = False
            while True:
                try:
                    response = await self.client.aio.models.generate_content(
                        model=self.spec.model, contents=[self.contents_for(call, uris)],
                        config=config,
                    )
                    break
                except errors.APIError as err:
                    if (err.code in FILE_ACCESS_CODES and call.media and not retried
                            and _mentions_file(err)):
                        retried = True  # no answer was produced: re-prepare once, retry once
                        await self._reprepare(call, uris, only_expiring=False)
                        continue
                    category: Category = "transient" if err.code in TRANSIENT_CODES else "fatal"
                    return self._handle(self._summary(err), {}, None, category, settings)
                except TRANSPORT_ERRORS as err:  # transport errors and timeouts
                    return self._handle(self._summary(err), {}, None, "transient", settings)
                except (errors.UnknownApiResponseError, pydantic.ValidationError) as err:
                    return self._handle(self._summary(err), {}, None, "fatal", settings)
        except _PrepareError as err:  # a failed re-prepare: no answer was produced
            return self._handle(self._redact(err.message), {}, None, err.category, settings)
        raw, category = self._outcome(response)
        return self._handle(
            self._redact(raw), _usage(response), getattr(response, "model_version", None),
            category, settings,
        )

    def _handle(
        self, raw: str, usage: dict[str, int], build: str | None, category: Category,
        settings: dict[str, Any],
    ) -> Handle:
        return {"provider": PROVIDER, "raw": raw, "usage": usage, "model_build": build,
                "category": category, "settings": settings}

    async def submit(self, calls: list[RaterCall]) -> list[Handle]:
        for call in calls:  # before any call: a request the adapter cannot send is a bug
            self._check_call(call)
        return [await self._call(c) for c in calls]

    async def collect(self, handles: list[Handle]) -> list[RaterResult]:
        return [
            RaterResult(raw=h["raw"], usage=dict(h.get("usage") or {}),
                        model_build=h.get("model_build"), category=h["category"],
                        settings=h.get("settings"))
            for h in handles
        ]
