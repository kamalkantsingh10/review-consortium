"""GeminiRater against the recorded client: the story 2.2 I/O matrix (no network)."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from google.genai import errors, types

from consortium.core.errors import ConsortiumError
from consortium.core.prompt import Media, Text, compose
from consortium.core.render import ClipRef, PracticeExample, RequestItem, TrialRequest
from consortium.raters.base import MediaRef, ModelSpec, RaterCall
from consortium.raters.gemini import GeminiRater, file_name, provider_sha256
from recorded import NOW, RecordedGeminiClient, file_entry, fixture, provider_hash

KEY = "sk-test-SECRET-key-123"


def _clip(clip_id: str) -> ClipRef:
    return ClipRef(clip_id, hashlib.sha256(f"clip {clip_id}".encode()).hexdigest())


CA, CB, CP = _clip("c_aaaaaaaa"), _clip("c_bbbbbbbb"), _clip("c_pppppppp")
PAIR = RequestItem("alive", "pairwise", "Which seems more alive?", None, None, ("A", "B"))
SCHEMA = {"type": "object", "properties": {"alive": {"type": "string", "enum": ["A", "B"]}},
          "required": ["alive"], "additionalProperties": False}
REQUEST = TrialRequest("card", "instr", (PAIR,), SCHEMA, "prompt",
                       (PracticeExample((CP, CA), {"alive": "A"}),), (CA, CB))


def spec(tmp_path: Path, **over: Any) -> ModelSpec:
    base = ModelSpec(
        model_id="m2", provider="gemini", model="gemini-3-flash-preview", temperature=0.7,
        fps=1.0, seed_supported=True, media_resolution="low", thinking_level=None,
        api_key=KEY, max_output_tokens=256, clips_dir=tmp_path,
    )
    return replace(base, **over)


class Sleeps:
    def __init__(self) -> None:
        self.waits: list[float] = []

    async def __call__(self, s: float) -> None:
        self.waits.append(s)


def rater(tmp_path: Path, client: RecordedGeminiClient, now=None, **over: Any):
    for clip in (CA, CB, CP):  # the local Clip files, whose SHA-256 the ClipRefs carry
        (tmp_path / f"{clip.clip_id}.mp4").write_bytes(f"clip {clip.clip_id}".encode())
    sleeps = Sleeps()
    r = GeminiRater(spec(tmp_path, **over), client=client, now=now or (lambda: NOW),
                    sleep=sleeps)
    return r, sleeps


def run(coro):
    return asyncio.run(coro)


async def prepared_call(r: GeminiRater, seed: int = 77) -> RaterCall:
    media = tuple([await r.prepare(c) for c in (CP, CA, CB)])
    return RaterCall("pilot1/p1-m2/r1/t1", 1, seed, REQUEST, media)


async def one(r: GeminiRater, seed: int = 77):
    call = await prepared_call(r, seed)
    [handle] = await r.submit([call])
    [result] = await r.collect([handle])
    return handle, result


# --------------------------------------------------------------------------- prepare


NAME_A = file_name(CA.sha256)


def test_file_names_are_content_addressed() -> None:
    assert file_name("AB" * 32) == "files/rc-" + "ab" * 12
    assert file_name("ab" * 32, 3) == "files/rc-" + "ab" * 12 + "-v3"
    assert len(file_name("a" * 64, 99).removeprefix("files/")) <= 40


def test_provider_sha256_decodings() -> None:
    hexd = CA.sha256
    assert provider_sha256(hexd.upper()) == hexd
    assert provider_sha256(provider_hash(hexd)) == hexd  # base64 of the hex text
    assert provider_sha256(base64.b64encode(bytes.fromhex(hexd)).decode()) == hexd
    assert provider_sha256(None) is None and provider_sha256("%%%") is None


def test_prepare_uploads_once_and_polls(tmp_path: Path) -> None:
    client = RecordedGeminiClient()
    r, sleeps = rater(tmp_path, client)
    ref = run(r.prepare(CA))
    assert ref == MediaRef("c_aaaaaaaa", CA.sha256, f"https://files.test/{NAME_A}")
    [up] = client.ops("files.upload")
    assert up == {"file": str(tmp_path / "c_aaaaaaaa.mp4"), "name": NAME_A,
                  "display_name": "c_aaaaaaaa", "mime_type": "video/mp4"}
    assert sleeps.waits == [2.0]  # PROCESSING once, then ACTIVE
    run(r.prepare(CA))  # a 2nd prepare reuses the ACTIVE File
    assert len(client.ops("files.upload")) == 1


def test_prepare_reuses_active_file_from_earlier_run(tmp_path: Path) -> None:
    store = {NAME_A: file_entry(CA.sha256, ["ACTIVE"], NOW + timedelta(hours=40))}
    client = RecordedGeminiClient(store=store)
    r, _ = rater(tmp_path, client)
    run(r.prepare(CA))
    assert client.ops("files.upload") == [] and client.ops("files.delete") == []


def test_processing_file_reused_without_delete_or_upload(tmp_path: Path) -> None:
    store = {NAME_A: file_entry(CA.sha256, ["PROCESSING", "PROCESSING", "ACTIVE"],
                                NOW + timedelta(hours=47))}
    client = RecordedGeminiClient(store=store)
    r, sleeps = rater(tmp_path, client)
    run(r.prepare(CA))
    assert client.ops("files.upload") == [] and client.ops("files.delete") == []
    assert sleeps.waits == [2.0, 2.0]


@pytest.mark.parametrize("state, expires", [("FAILED", timedelta(hours=40)),
                                            ("ACTIVE", timedelta(minutes=30))])
def test_failed_or_expiring_file_gets_a_new_version(tmp_path: Path, state, expires) -> None:
    store = {NAME_A: file_entry(CA.sha256, [state], NOW + expires)}
    client = RecordedGeminiClient(store=store)
    r, _ = rater(tmp_path, client)
    ref = run(r.prepare(CA))
    assert client.ops("files.delete") == []
    assert [kw["name"] for kw in client.ops("files.upload")] == [file_name(CA.sha256, 2)]
    assert ref.ref.endswith("-v2") and NAME_A in client.store


def test_no_expiration_time_assumes_47h_from_creation(tmp_path: Path) -> None:
    created = NOW - timedelta(hours=46, minutes=30)
    old = file_entry(CA.sha256, ["ACTIVE"], None) | {"created": created}
    client = RecordedGeminiClient(store={NAME_A: old})
    r, _ = rater(tmp_path, client)
    assert run(r.prepare(CA)).ref.endswith("-v2")  # created 46.5 h ago: expires in 0.5 h
    young = file_entry(CA.sha256, ["ACTIVE"], None) | {"created": NOW - timedelta(hours=1)}
    client = RecordedGeminiClient(store={NAME_A: young})
    r, _ = rater(tmp_path, client)
    assert run(r.prepare(CA)).ref.endswith(NAME_A) and client.ops("files.upload") == []


def test_content_hash_mismatch_reuploads(tmp_path: Path) -> None:
    store = {NAME_A: file_entry("f" * 64, ["ACTIVE"], NOW + timedelta(hours=40))}
    client = RecordedGeminiClient(store=store)
    r, _ = rater(tmp_path, client)
    ref = run(r.prepare(CA))
    assert ref.ref.endswith("-v2") and client.ops("files.delete") == []
    assert [kw["name"] for kw in client.ops("files.upload")] == [file_name(CA.sha256, 2)]


def test_local_file_changed_is_prepare_failed(tmp_path: Path) -> None:
    client = RecordedGeminiClient()
    r, _ = rater(tmp_path, client)
    (tmp_path / "c_aaaaaaaa.mp4").write_bytes(b"other bytes")
    with pytest.raises(ConsortiumError) as info:
        run(r.prepare(CA))
    assert info.value.code == "prepare_failed" and "SHA-256" in info.value.message


def test_missing_local_file_is_prepare_failed_without_upload(tmp_path: Path) -> None:
    client = RecordedGeminiClient()
    r, _ = rater(tmp_path, client)
    (tmp_path / "c_aaaaaaaa.mp4").unlink()
    with pytest.raises(ConsortiumError) as info:
        run(r.prepare(CA))
    assert info.value.code == "prepare_failed" and "local Clip file" in info.value.message
    assert client.ops("files.upload") == []


def test_404_on_get_is_missing(tmp_path: Path) -> None:
    client = RecordedGeminiClient()
    client.missing_code = 404
    r, _ = rater(tmp_path, client)
    run(r.prepare(CA))
    assert len(client.ops("files.upload")) == 1


def test_409_on_upload_reads_the_file_back(tmp_path: Path) -> None:
    client = RecordedGeminiClient()
    r, _ = rater(tmp_path, client)
    client.upload_errors = [{"api_error": {"code": 409, "status": "ALREADY_EXISTS"}}]
    client.store[NAME_A] = file_entry(CA.sha256, ["PROCESSING", "ACTIVE"], NOW + timedelta(48))
    client.get_errors = [{"api_error": {"code": 404, "status": "NOT_FOUND"}}]  # raced
    ref = run(r.prepare(CA))
    assert ref.ref.endswith(NAME_A)


def test_active_without_uri_is_prepare_failed(tmp_path: Path) -> None:
    entry = file_entry(CA.sha256, ["ACTIVE"], NOW + timedelta(hours=40)) | {"uri": None}
    r, _ = rater(tmp_path, RecordedGeminiClient(store={NAME_A: entry}))
    with pytest.raises(ConsortiumError) as info:
        run(r.prepare(CA))
    assert info.value.code == "prepare_failed" and "URI" in info.value.message


def test_two_models_sharing_clips_never_delete_each_others_file(tmp_path: Path) -> None:
    clock = {"now": NOW}
    client = RecordedGeminiClient()
    client.clock = lambda: clock["now"]
    r1, _ = rater(tmp_path, client, now=lambda: clock["now"])
    r2, _ = rater(tmp_path, client, now=lambda: clock["now"], model_id="m3")
    first = run(r1.prepare(CA))
    assert run(r2.prepare(CA)) == first  # shared, one upload
    clock["now"] = NOW + timedelta(hours=47, minutes=30)
    second = run(r2.prepare(CA))
    assert second.ref.endswith("-v2")
    assert client.ops("files.delete") == [] and NAME_A in client.store
    assert len(client.ops("files.upload")) == 2


def test_concurrent_prepares_of_one_clip_upload_once(tmp_path: Path) -> None:
    client = RecordedGeminiClient()
    r, _ = rater(tmp_path, client)

    async def go():
        return await asyncio.gather(*(r.prepare(CA) for _ in range(5)))

    assert len(set(run(go()))) == 1
    assert len(client.ops("files.upload")) == 1


def test_prepare_retries_transient_upload_then_fails(tmp_path: Path) -> None:
    client = RecordedGeminiClient()
    client.upload_errors = [{"api_error": {"code": 503, "status": "UNAVAILABLE"}}] * 2
    r, sleeps = rater(tmp_path, client)
    run(r.prepare(CA))
    assert sleeps.waits == [2.0, 4.0, 2.0]  # two upload backoffs, then one poll
    client2 = RecordedGeminiClient()
    client2.upload_errors = [{"transport_error": f"boom {KEY}"}] * 4
    r2, sleeps2 = rater(tmp_path, client2)
    with pytest.raises(ConsortiumError) as info:
        run(r2.prepare(CA))
    assert info.value.code == "prepare_failed"
    assert sleeps2.waits == [2.0, 4.0, 8.0]
    assert KEY not in str(info.value)


@pytest.mark.parametrize("code", [400, 401, 403])
def test_prepare_does_not_retry_client_errors(tmp_path: Path, code: int) -> None:
    client = RecordedGeminiClient()
    client.upload_errors = [{"api_error": {"code": code, "status": "X"}}]
    r, sleeps = rater(tmp_path, client)
    with pytest.raises(ConsortiumError) as info:
        run(r.prepare(CA))
    assert info.value.code == "prepare_failed"
    assert sleeps.waits == [] and len(client.ops("files.upload")) == 1


def test_get_503_during_existence_check_is_retried(tmp_path: Path) -> None:
    client = RecordedGeminiClient()
    client.get_errors = [{"api_error": {"code": 503, "status": "UNAVAILABLE"}}]
    r, sleeps = rater(tmp_path, client)
    run(r.prepare(CA))
    assert sleeps.waits[0] == 2.0 and len(client.ops("files.upload")) == 1


def test_polling_errors_retried_then_prepare_failed_redacted(tmp_path: Path) -> None:
    client = RecordedGeminiClient(upload_states=("PROCESSING", "ACTIVE"))
    err = {"api_error": {"code": 500, "status": "INTERNAL", "message": f"bad {KEY}"}}
    client.get_errors = [None, err, None]  # lookup ok; 1st poll fails once, then succeeds
    r, sleeps = rater(tmp_path, client)
    run(r.prepare(CA))
    assert sleeps.waits == [2.0, 2.0]  # poll interval, one backoff
    client2 = RecordedGeminiClient(upload_states=("PROCESSING", "ACTIVE"))
    client2.get_errors = [None] + [err] * 4
    r2, sleeps2 = rater(tmp_path, client2)
    with pytest.raises(ConsortiumError) as info:
        run(r2.prepare(CA))
    assert info.value.code == "prepare_failed" and "polling" in info.value.message
    assert KEY not in str(info.value) and "***" in info.value.message
    assert sleeps2.waits == [2.0, 2.0, 4.0, 8.0]


def test_prepare_failed_state_and_timeout(tmp_path: Path) -> None:
    r, _ = rater(tmp_path, RecordedGeminiClient(upload_states=("PROCESSING", "FAILED")))
    with pytest.raises(ConsortiumError) as info:
        run(r.prepare(CA))
    assert info.value.code == "prepare_failed" and "FAILED" in info.value.message
    r, sleeps = rater(tmp_path, RecordedGeminiClient(upload_states=("PROCESSING",)))
    with pytest.raises(ConsortiumError) as info:
        run(r.prepare(CA))
    assert info.value.code == "prepare_failed"
    assert sum(sleeps.waits) == 300.0


# --------------------------------------------------------------------------- request


def test_happy_parts_config_and_settings(tmp_path: Path) -> None:
    client = RecordedGeminiClient([fixture("gemini", "ok")])
    r, _ = rater(tmp_path, client)
    handle, result = run(one(r))
    assert (result.category, result.raw) == ("ok", '{"alive":"A"}')
    assert result.model_build == "gemini-3-flash-preview-09-2026"
    assert result.usage == {"input_tokens": 1205, "output_tokens": 100, "text_tokens": 400,
                            "video_tokens": 700, "audio_tokens": 100, "thoughts_tokens": 60}
    [sent] = client.ops("generate_content")
    assert sent["model"] == "gemini-3-flash-preview"
    [content] = sent["contents"]
    assert content.role == "user"
    expected = compose(REQUEST)
    assert len(content.parts) == len(expected)
    for got, part in zip(content.parts, expected, strict=True):
        if isinstance(part, Text):
            assert got == types.Part(text=part.text)
        else:
            assert isinstance(part, Media)
            sha = next(c.sha256 for c in (CA, CB, CP) if c.clip_id == part.clip_id)
            assert got.file_data.file_uri == f"https://files.test/{file_name(sha)}"
            assert got.file_data.mime_type == "video/mp4"
            assert got.media_processing == types.MediaProcessing.STATIC
            assert got.video_metadata == types.VideoMetadata(fps=1.0)
    cfg = sent["config"]
    assert (cfg.temperature, cfg.max_output_tokens, cfg.seed) == (0.7, 256, 77)
    assert cfg.media_resolution == types.MediaResolution.MEDIA_RESOLUTION_LOW
    assert cfg.thinking_config is None
    assert cfg.http_options.retry_options.attempts == 1
    assert cfg.response_mime_type is None and cfg.response_schema is None
    assert result.settings == {
        "model": {"value": "gemini-3-flash-preview", "documented": True},
        "temperature": {"value": 0.7, "documented": True},
        "seed": {"value": 77, "documented": True},
        "fps": {"value": 1.0, "documented": True},
        "media_processing": {"value": "static", "documented": True},
        "media_resolution": {"value": "low", "documented": True},
        "max_output_tokens": {"value": 256, "documented": True},
        "prompt_format": {"value": 1, "documented": True},
    }
    text = json.dumps(handle)
    assert "files.test" not in text and KEY not in text
    assert set(handle) == {"provider", "raw", "usage", "model_build", "category", "settings"}


def test_seed_omitted_and_no_fps(tmp_path: Path) -> None:
    client = RecordedGeminiClient([fixture("gemini", "ok")])
    r, _ = rater(tmp_path, client, seed_supported=False, fps=None, media_resolution=None)
    _, result = run(one(r))
    cfg = client.ops("generate_content")[0]["config"]
    assert cfg.seed is None and cfg.media_resolution is None
    media = [p for p in client.ops("generate_content")[0]["contents"][0].parts if p.file_data]
    assert all(p.video_metadata is None for p in media)
    assert {"seed", "fps", "media_resolution", "thinking_level"}.isdisjoint(result.settings)


def test_thinking_level_sent_and_archived(tmp_path: Path) -> None:
    client = RecordedGeminiClient([fixture("gemini", "ok")])
    r, _ = rater(tmp_path, client, thinking_level="low")
    _, result = run(one(r))
    cfg = client.ops("generate_content")[0]["config"]
    assert cfg.thinking_config == types.ThinkingConfig(thinking_level=types.ThinkingLevel.LOW)
    assert result.settings["thinking_level"] == {"value": "low", "documented": True}


# --------------------------------------------------------------------------- outcomes


@pytest.mark.parametrize("name, category, raw", [
    ("prompt_blocked", "refused", "blocked: PROHIBITED_CONTENT"),
    ("output_safety", "refused", "partial\nfinish_reason: SAFETY"),
    ("max_tokens", "ok", '{"alive":'),
    ("empty", "ok", ""),
    ("error_429", "transient",
     "429 RESOURCE_EXHAUSTED: Resource has been exhausted (e.g. check quota)."),
    ("error_503", "transient", "503 UNAVAILABLE: The model is overloaded. Please try again later."),
    ("error_400", "fatal", "400 INVALID_ARGUMENT: Request contains an invalid argument."),
])
def test_outcome_matrix(tmp_path: Path, name: str, category: str, raw: str) -> None:
    r, _ = rater(tmp_path, RecordedGeminiClient([fixture("gemini", name)]))
    _, result = run(one(r))
    assert (result.category, result.raw) == (category, raw)
    if name == "empty":
        assert result.usage == {} and result.model_build == "gemini-3-flash-preview-09-2026"
    if name.startswith("error"):
        assert result.usage == {} and result.model_build is None


@pytest.mark.parametrize("finish", ["PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "RECITATION",
                                    "IMAGE_SAFETY", "IMAGE_PROHIBITED_CONTENT"])
def test_other_output_blocks_refused(tmp_path: Path, finish: str) -> None:
    doc = fixture("gemini", "output_safety")
    doc["candidates"][0]["finish_reason"] = finish
    del doc["candidates"][0]["content"]
    r, _ = rater(tmp_path, RecordedGeminiClient([doc]))
    _, result = run(one(r))
    assert (result.category, result.raw) == ("refused", f"finish_reason: {finish}")


def test_transport_error_and_long_message(tmp_path: Path) -> None:
    long = {"api_error": {"code": 500, "status": "INTERNAL", "message": "x" * 900}}
    r, _ = rater(tmp_path, RecordedGeminiClient([{"transport_error": "reset"}, long]))
    _, result = run(one(r))
    assert result.category == "transient" and result.raw == "ConnectError: reset"
    _, result = run(one(r))
    assert result.category == "transient" and result.raw == "500 INTERNAL: " + "x" * 500


def test_no_build_is_none(tmp_path: Path) -> None:
    r, _ = rater(tmp_path, RecordedGeminiClient([fixture("gemini", "no_build")]))
    _, result = run(one(r))
    assert result.model_build is None and result.usage == {}


def test_file_403_reprepares_once_and_retries(tmp_path: Path) -> None:
    client = RecordedGeminiClient([fixture("gemini", "error_403_file"), fixture("gemini", "ok")])
    r, _ = rater(tmp_path, client)

    async def go():
        call = await prepared_call(r)
        client.store.clear()  # the Files are gone
        return (await r.collect(await r.submit([call])))[0]

    result = run(go())
    assert result.category == "ok"
    assert len(client.ops("generate_content")) == 2
    assert len(client.ops("files.upload")) == 6  # 3 Clips, then 3 again


def test_file_403_twice_is_fatal(tmp_path: Path) -> None:
    err = fixture("gemini", "error_403_file")
    client = RecordedGeminiClient([err, err])
    r, _ = rater(tmp_path, client)
    _, result = run(one(r))
    assert result.category == "fatal" and result.raw.startswith("403 PERMISSION_DENIED")
    assert len(client.ops("generate_content")) == 2


def test_expiring_file_reuploaded_before_call(tmp_path: Path) -> None:
    clock = {"now": NOW}
    client = RecordedGeminiClient([fixture("gemini", "ok")])
    client.clock = lambda: clock["now"]
    r, _ = rater(tmp_path, client, now=lambda: clock["now"])

    async def go():
        call = await prepared_call(r)
        assert len(client.ops("files.upload")) == 3
        clock["now"] = NOW + timedelta(hours=47, minutes=30)  # expiry < now + 1 h
        return await r.submit([call])

    run(go())
    uploads = [o for o, _ in client.calls if o in ("files.upload", "generate_content")]
    assert uploads == ["files.upload"] * 6 + ["generate_content"]


def test_resumed_handle_collects_without_network(tmp_path: Path) -> None:
    client = RecordedGeminiClient([fixture("gemini", "ok")])
    r, _ = rater(tmp_path, client)
    handle, result = run(one(r))
    stored = json.loads(json.dumps(handle))
    fresh_client = RecordedGeminiClient()
    r2, _ = rater(tmp_path, fresh_client)
    [again] = run(r2.collect([stored]))
    assert again == result
    assert fresh_client.calls == []


def test_key_never_in_raw_handle_or_repr(tmp_path: Path) -> None:
    leaky = {"api_error": {"code": 401, "status": "UNAUTHENTICATED",
                           "message": f"API key {KEY} is invalid"}}
    doc = fixture("gemini", "ok")
    doc["candidates"][0]["content"]["parts"][0]["text"] = f"echo {KEY}"
    r, _ = rater(tmp_path, RecordedGeminiClient([leaky, doc]))
    h1, res1 = run(one(r))
    assert res1.category == "fatal" and res1.raw == "401 UNAUTHENTICATED: API key *** is invalid"
    h2, res2 = run(one(r))
    assert res2.raw == "echo ***"
    assert KEY not in json.dumps([h1, h2])
    assert KEY not in repr(r.spec)


def test_key_straddling_truncation_boundary_is_redacted(tmp_path: Path) -> None:
    err = {"api_error": {"code": 500, "status": "INTERNAL", "message": "x" * 490 + KEY + "y"}}
    r, _ = rater(tmp_path, RecordedGeminiClient([err]))
    _, result = run(one(r))
    assert KEY[:8] not in result.raw
    assert result.raw == "500 INTERNAL: " + "x" * 490 + "***y"


def test_403_not_about_a_file_does_not_reprepare(tmp_path: Path) -> None:
    err = {"api_error": {"code": 403, "status": "PERMISSION_DENIED",
                         "message": "Method doesn't allow unregistered callers."}}
    client = RecordedGeminiClient([err])
    r, _ = rater(tmp_path, client)
    _, result = run(one(r))
    assert result.category == "fatal"
    assert len(client.ops("generate_content")) == 1 and len(client.ops("files.upload")) == 3


def test_failed_reprepare_becomes_a_handle(tmp_path: Path) -> None:
    client = RecordedGeminiClient([fixture("gemini", "error_403_file")])
    r, _ = rater(tmp_path, client)

    async def go(code: int):
        call = await prepared_call(r)
        client.store.clear()
        client.responses = [fixture("gemini", "error_403_file")]
        client.upload_errors = [{"api_error": {"code": code, "status": "X",
                                               "message": f"no {KEY}"}}] * 4
        return (await r.collect(await r.submit([call])))[0]

    result = run(go(503))
    assert result.category == "transient" and result.raw.startswith("m2: Clip")
    assert KEY not in result.raw
    result = run(go(401))
    assert result.category == "fatal"


def test_timeouts_and_bad_responses(tmp_path: Path) -> None:
    client = RecordedGeminiClient([TimeoutError("slow"), httpx.ReadTimeout("read timed out"),
                                   errors.UnknownApiResponseError("garbled")])
    r, _ = rater(tmp_path, client)
    assert run(one(r))[1].category == "transient"
    assert run(one(r))[1].category == "transient"
    _, result = run(one(r))
    assert (result.category, result.raw) == ("fatal", "UnknownApiResponseError: garbled")
    cfg = client.ops("generate_content")[0]["config"]
    assert cfg.http_options.timeout == 600_000


def test_submit_calls_are_independent(tmp_path: Path) -> None:
    client = RecordedGeminiClient([fixture("gemini", "error_400"), fixture("gemini", "ok")])
    r, _ = rater(tmp_path, client)

    async def go():
        a = await prepared_call(r, 1)
        b = replace(a, trial_id="pilot1/p1-m2/r1/t2", seed=2)
        return await r.collect(await r.submit([a, b]))

    first, second = run(go())
    assert (first.category, second.category) == ("fatal", "ok")


def test_unprepared_media_is_adapter_error_before_any_call(tmp_path: Path) -> None:
    client = RecordedGeminiClient([fixture("gemini", "ok")])
    r, _ = rater(tmp_path, client)

    async def go():
        call = await prepared_call(r)
        await r.submit([replace(call, media=call.media[:1])])

    with pytest.raises(ConsortiumError) as info:
        run(go())
    assert info.value.code == "adapter_error"
    assert client.ops("generate_content") == []


def test_aclose_closes_only_a_built_client(tmp_path: Path) -> None:
    closed: list[bool] = []
    r = GeminiRater(spec(tmp_path))

    async def go():
        client = r.client  # built lazily by the Rater

        async def aclose() -> None:
            closed.append(True)

        client.aio.aclose = aclose
        await r.aclose()
        await r.aclose()  # idempotent
        return list(closed)  # before the SDK's own __del__ may schedule another aclose

    assert run(go()) == [True] and r._client is None
    closed.clear()
    injected = RecordedGeminiClient()
    injected.aio.aclose = lambda: closed.append(False)  # must not be called
    run(GeminiRater(spec(tmp_path), client=injected).aclose())
    assert False not in closed
