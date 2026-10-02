"""QwenRater against the recorded client: the story 2.3 I/O matrix (no network)."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import openai
import pytest

from consortium.core.errors import ConsortiumError
from consortium.core.prompt import Media, Text, compose
from consortium.core.render import ClipRef, PracticeExample, RequestItem, TrialRequest
from consortium.raters.base import MediaRef, ModelSpec, RaterCall
from consortium.raters.gemini import GeminiRater
from consortium.raters.qwen import DOCUMENTED, QwenRater, documented_flag, is_model_studio
from recorded import QWEN_URL, RecordedQwenClient, fixture, qwen_chunks

KEY = "sk-test-SECRET-key-123"
VLLM_URL = "http://gpu:8000/v1"


def _bytes(clip_id: str) -> bytes:
    return f"clip {clip_id}".encode()


def _clip(clip_id: str) -> ClipRef:
    return ClipRef(clip_id, hashlib.sha256(_bytes(clip_id)).hexdigest())


CA, CB, CP = _clip("c_aaaaaaaa"), _clip("c_bbbbbbbb"), _clip("c_pppppppp")
PAIR = RequestItem("alive", "pairwise", "Which seems more alive?", None, None, ("A", "B"))
SCHEMA = {"type": "object", "properties": {"alive": {"type": "string", "enum": ["A", "B"]}},
          "required": ["alive"], "additionalProperties": False}
REQUEST = TrialRequest("card", "instr", (PAIR,), SCHEMA, "prompt",
                       (PracticeExample((CP, CA), {"alive": "A"}),), (CA, CB))


def spec(tmp_path: Path, **over: Any) -> ModelSpec:
    base = ModelSpec(
        model_id="m2", provider="qwen", model="qwen3.8-omni-flash", temperature=0.7,
        fps=None, seed_supported=True, media_resolution=None, thinking_level=None,
        api_key=KEY, max_output_tokens=256, clips_dir=tmp_path, base_url=QWEN_URL,
    )
    return replace(base, **over)


def rater(tmp_path: Path, client: RecordedQwenClient, **over: Any) -> QwenRater:
    for clip in (CA, CB, CP):  # the local Clip files, whose SHA-256 the ClipRefs carry
        (tmp_path / f"{clip.clip_id}.mp4").write_bytes(_bytes(clip.clip_id))
    return QwenRater(spec(tmp_path, **over), client=client)


def run(coro):
    return asyncio.run(coro)


def data_uri(clip: ClipRef, prefix: str = "data:;base64,") -> str:
    return prefix + base64.b64encode(_bytes(clip.clip_id)).decode()


async def prepared_call(r: QwenRater, seed: int = 77) -> RaterCall:
    media = tuple([await r.prepare(c) for c in (CP, CA, CB)])
    return RaterCall("pilot1/p1-m2/r1/t1", 1, seed, REQUEST, media)


async def one(r: QwenRater, seed: int = 77):
    call = await prepared_call(r, seed)
    [handle] = await r.submit([call])
    [result] = await r.collect([handle])
    return handle, result


def sent_kwargs(client: RecordedQwenClient) -> dict[str, Any]:
    [kwargs] = client.ops()
    return kwargs


# --------------------------------------------------------------------------- prepare


def test_prepare_is_an_offline_data_uri(tmp_path: Path) -> None:
    client = RecordedQwenClient()
    r = rater(tmp_path, client)
    ref = run(r.prepare(CA))
    assert ref == MediaRef(CA.clip_id, CA.sha256, data_uri(CA))
    assert ref.ref.startswith("data:;base64,")
    assert client.calls == []


def test_data_uri_carries_the_media_type_off_model_studio(tmp_path: Path) -> None:
    r = rater(tmp_path, RecordedQwenClient(), base_url=VLLM_URL)
    assert run(r.prepare(CA)).ref == data_uri(CA, "data:video/mp4;base64,")


def test_clip_altered_on_disk_is_prepare_failed_nothing_sent(tmp_path: Path) -> None:
    client = RecordedQwenClient([fixture("qwen", "ok")])
    r = rater(tmp_path, client)
    (tmp_path / "c_aaaaaaaa.mp4").write_bytes(b"other bytes")
    with pytest.raises(ConsortiumError) as info:
        run(r.prepare(CA))
    assert info.value.code == "prepare_failed" and "SHA-256" in info.value.message
    assert client.calls == []


def test_missing_local_file_is_prepare_failed(tmp_path: Path) -> None:
    r = rater(tmp_path, RecordedQwenClient())
    (tmp_path / "c_aaaaaaaa.mp4").unlink()
    with pytest.raises(ConsortiumError) as info:
        run(r.prepare(CA))
    assert info.value.code == "prepare_failed" and "unreadable" in info.value.message


# --------------------------------------------------------------------------- request


def test_happy_request_result_and_settings(tmp_path: Path) -> None:
    client = RecordedQwenClient([fixture("qwen", "ok")])
    r = rater(tmp_path, client)
    handle, result = run(one(r))
    assert (result.category, result.raw) == ("ok", '{"alive":"A"}')
    assert result.model_build == "fp-qwen-2026"  # the last non-empty system_fingerprint
    assert result.usage == {"input_tokens": 1830, "output_tokens": 64, "text_tokens": 410,
                            "audio_tokens": 120, "video_tokens": 1300, "reasoning_tokens": 20}
    sent = sent_kwargs(client)
    assert set(sent) == {"model", "messages", "max_tokens", "temperature", "seed", "stream",
                         "stream_options"}
    assert (sent["model"], sent["max_tokens"], sent["temperature"], sent["seed"]) == (
        "qwen3.8-omni-flash", 256, 0.7, 77)
    assert sent["stream"] is True and sent["stream_options"] == {"include_usage": True}
    [message] = sent["messages"]
    assert message["role"] == "user"
    expected = compose(REQUEST)
    assert len(message["content"]) == len(expected)
    clips = {c.clip_id: c for c in (CA, CB, CP)}
    for got, part in zip(message["content"], expected, strict=True):
        if isinstance(part, Text):
            assert got == {"type": "text", "text": part.text}
        else:
            assert isinstance(part, Media)
            assert got == {"type": "video_url", "video_url": {"url": data_uri(clips[part.clip_id])}}
    assert result.settings == {
        "model": {"value": "qwen3.8-omni-flash", "documented": True},
        "max_tokens": {"value": 256, "documented": True},
        "stream": {"value": True, "documented": True},
        "stream_options": {"value": {"include_usage": True}, "documented": True},
        "temperature": {"value": 0.7, "documented": False},
        "seed": {"value": 77, "documented": False},
        "prompt_format": {"value": 1, "documented": None},
    }
    assert set(handle) == {"provider", "raw", "usage", "model_build", "category", "settings"}
    text = json.dumps(handle)
    assert "base64" not in text and KEY not in text


def test_text_parts_identical_to_gemini(tmp_path: Path) -> None:
    client = RecordedQwenClient([fixture("qwen", "ok")])
    r = rater(tmp_path, client)
    run(one(r))
    qwen_texts = [p.get("text") for p in sent_kwargs(client)["messages"][0]["content"]]
    gem = GeminiRater(replace(spec(tmp_path), provider="gemini", base_url=None))
    call = RaterCall("t", 1, 1, REQUEST, ())
    uris = {c.clip_id: "https://files.test/x" for c in (CA, CB, CP)}
    gem_texts = [p.text for p in gem.contents_for(call, uris).parts]
    assert qwen_texts == gem_texts


def test_seed_omitted_when_unsupported(tmp_path: Path) -> None:
    client = RecordedQwenClient([fixture("qwen", "ok")])
    r = rater(tmp_path, client, seed_supported=False)
    _, result = run(one(r))
    assert "seed" not in sent_kwargs(client) and "seed" not in result.settings
    assert "reasoning_effort" not in sent_kwargs(client)
    assert "reasoning_effort" not in result.settings


def test_reasoning_effort_sent_verbatim_and_archived_when_set(tmp_path: Path) -> None:
    client = RecordedQwenClient([fixture("qwen", "ok")])
    r = rater(tmp_path, client, reasoning_effort="low")
    _, result = run(one(r))
    assert sent_kwargs(client)["reasoning_effort"] == "low"
    assert result.settings["reasoning_effort"] == {"value": "low", "documented": True}


def test_nothing_else_is_sent(tmp_path: Path) -> None:
    client = RecordedQwenClient([fixture("qwen", "ok")])
    r = rater(tmp_path, client, reasoning_effort="high")
    run(one(r))
    sent = sent_kwargs(client)
    assert {"modalities", "extra_body", "top_p", "fps", "media_resolution"}.isdisjoint(sent)


def test_vllm_host_same_request_shape_null_flags(tmp_path: Path) -> None:
    hosted, local = RecordedQwenClient([fixture("qwen", "ok")]), RecordedQwenClient(
        [fixture("qwen", "ok")])
    _, r1 = run(one(rater(tmp_path, hosted)))
    _, r2 = run(one(rater(tmp_path, local, base_url=VLLM_URL, model="Qwen/Qwen3-Omni")))
    a, b = sent_kwargs(hosted), sent_kwargs(local)
    hosted_text = json.dumps(a | {"model": "Qwen/Qwen3-Omni"}, sort_keys=True)
    assert hosted_text.replace("data:;base64,", "data:video/mp4;base64,") == json.dumps(
        b, sort_keys=True)
    assert {k: v["value"] for k, v in r1.settings.items()} | {"model": "Qwen/Qwen3-Omni"} == {
        k: v["value"] for k, v in r2.settings.items()}
    assert all(v["documented"] is None for v in r2.settings.values())
    assert (r1.category, r1.raw, r1.usage) == (r2.category, r2.raw, r2.usage)


@pytest.mark.parametrize("url, studio", [
    (QWEN_URL, True),
    ("https://dashscope-intl.aliyuncs.com/compatible-mode/v1", True),
    ("https://aliyuncs.com/v1", True),
    ("https://ws-1.ap-southeast-1.maas.aliyuncs.com./compatible-mode/v1", True),
    ("https://evilaliyuncs.com/v1", False),
    ("https://aliyuncs.com.example.org/v1", False),
    (VLLM_URL, False),
    (None, False),
])
def test_documented_table_by_host(url: str | None, studio: bool) -> None:
    assert is_model_studio(url) is studio
    for name, flag in DOCUMENTED.items():
        assert documented_flag(url, name) is (flag if studio else None)
    assert documented_flag(url, "prompt_format") is None
    assert DOCUMENTED["temperature"] is False and DOCUMENTED["seed"] is False


def test_client_built_lazily_without_sdk_retries(tmp_path: Path) -> None:
    r = QwenRater(spec(tmp_path))
    assert r._client is None
    client = r.client
    assert isinstance(client, openai.AsyncOpenAI)
    assert client.max_retries == 0 and client.timeout == 600.0
    assert str(client.base_url).rstrip("/") == QWEN_URL
    assert client.api_key == KEY


# --------------------------------------------------------------------------- outcomes


@pytest.mark.parametrize("name, category, raw", [
    ("error_400_inspection", "refused",
     "400 data_inspection_failed: Input data may contain inappropriate content."),
    ("error_429_limit", "transient",
     "429 limit_requests: You exceeded your current requests list."),
    ("error_429_throttling", "transient", "429 Throttling: Requests throttling triggered."),
    ("error_429_quota", "fatal", "429 insufficient_quota: You exceeded your current quota, "
                                 "please check your plan and billing details."),
    ("error_403_arrearage", "fatal",
     "403 Arrearage: Access denied, please make sure your account is in good standing."),
    ("error_403_access_denied", "fatal", "403 access_denied: Access denied."),
    ("error_500", "transient",
     "500 internal_error: An internal error has occured, please try again later."),
    ("error_503", "transient", "503 ServiceUnavailable: The service is temporarily unavailable."),
    ("error_400_invalid", "fatal",
     "400 invalid_parameter_error: <400> InternalError.Algo.InvalidParameter: "
     "The video is invalid."),
    ("error_400_internal_algo", "fatal",
     "400 InternalError.Algo.InvalidParameter: The video is invalid."),
    ("error_400_payload", "fatal", "400 payload_too_large: The base64 string exceeds the limit."),
    ("error_401", "fatal", "401 invalid_api_key: Incorrect API key provided."),
    ("error_404", "fatal", "404 model_not_found: The model `qwen-nope` does not exist."),
])
def test_error_matrix(tmp_path: Path, name: str, category: str, raw: str) -> None:
    r = rater(tmp_path, RecordedQwenClient([fixture("qwen", name)]))
    handle, result = run(one(r))
    assert (result.category, result.raw) == (category, raw)
    assert result.usage == {} and result.model_build is None
    assert result.settings["temperature"]["documented"] is False


def test_output_filtered_is_refused(tmp_path: Path) -> None:
    r = rater(tmp_path, RecordedQwenClient([fixture("qwen", "content_filter")]))
    _, result = run(one(r))
    assert (result.category, result.raw) == ("refused", "partial\nfinish_reason: content_filter")


def test_truncated_is_ok(tmp_path: Path) -> None:
    r = rater(tmp_path, RecordedQwenClient([fixture("qwen", "length")]))
    _, result = run(one(r))
    assert (result.category, result.raw) == ("ok", '{"alive":')
    assert result.usage == {"input_tokens": 1830, "output_tokens": 256}


def test_no_usage_chunk_is_empty_usage(tmp_path: Path) -> None:
    r = rater(tmp_path, RecordedQwenClient([fixture("qwen", "no_usage")]))
    _, result = run(one(r))
    assert (result.category, result.raw) == ("ok", '{"alive":"B"}')
    assert result.usage == {} and result.model_build is None


@pytest.mark.parametrize("name, category, raw", [
    ("midstream_inspection", "refused",
     "stream data_inspection_failed: Output data may contain inappropriate content."),
    ("midstream_internal", "transient",
     "stream internal_error: An internal error has occured, please try again later."),
    ("midstream_transport", "transient",
     "APIConnectionError: connection reset mid-stream"),
])
def test_mid_stream_error_drops_partial_text(tmp_path: Path, name: str, category: str,
                                             raw: str) -> None:
    r = rater(tmp_path, RecordedQwenClient([fixture("qwen", name)]))
    _, result = run(one(r))
    assert (result.category, result.raw) == (category, raw)
    assert '{"alive"' not in result.raw and result.usage == {}


def test_unknown_mid_stream_error_is_fatal(tmp_path: Path) -> None:
    doc = fixture("qwen", "midstream_internal")
    doc["chunks"][-1] = {"stream_error": {"code": "something_new", "message": "?"}}
    r = rater(tmp_path, RecordedQwenClient([doc]))
    _, result = run(one(r))
    assert (result.category, result.raw) == ("fatal", "stream something_new: ?")


def test_connection_and_timeout_errors_are_transient(tmp_path: Path) -> None:
    client = RecordedQwenClient([{"connection_error": "Connection error."}, {"timeout": True},
                                 TimeoutError("slow")])
    r = rater(tmp_path, client)
    for want in ("APIConnectionError: Connection error.", "APITimeoutError: Request timed out.",
                 "TimeoutError: slow"):
        _, result = run(one(r))
        assert (result.category, result.raw) == ("transient", want)


def test_invalid_chunk_is_a_fatal_handle(tmp_path: Path) -> None:
    doc = qwen_chunks("x")
    doc["chunks"][0]["choices"] = [{"index": 0, "delta": "oops"}]  # unvalidated, as in the SDK
    r = rater(tmp_path, RecordedQwenClient([doc]))
    handle, result = run(one(r))
    assert result.category == "fatal" and result.raw.startswith("AttributeError")
    assert handle["category"] == "fatal"


def test_non_json_sse_line_is_fatal(tmp_path: Path) -> None:
    doc = qwen_chunks('{"alive":"A"}')
    doc["chunks"].insert(1, {"bad_sse": "not json"})
    r = rater(tmp_path, RecordedQwenClient([doc]))
    _, result = run(one(r))
    assert result.category == "fatal" and result.raw.startswith("JSONDecodeError")


def _stream_error(code: str, message: str = "m") -> dict[str, Any]:
    doc = qwen_chunks('{"alive":"A"}')
    doc["chunks"] = doc["chunks"][:1] + [{"stream_error": {"code": code, "message": message}}]
    return doc


@pytest.mark.parametrize("code, category", [
    ("Throttling.RateQuota", "transient"),
    ("limit_requests", "transient"),
    ("ServiceUnavailable", "transient"),
    ("InternalError", "transient"),
    ("insufficient_quota", "fatal"),
    ("Arrearage", "fatal"),
    ("DataInspectionFailed.Output", "refused"),
])
def test_mid_stream_codes(tmp_path: Path, code: str, category: str) -> None:
    r = rater(tmp_path, RecordedQwenClient([_stream_error(code)]))
    _, result = run(one(r))
    assert (result.category, result.raw) == (category, f"stream {code}: m")


def test_400_data_inspection_output_is_refused(tmp_path: Path) -> None:
    err = {"status_error": {"status": 400, "body": {"error": {
        "code": "DataInspectionFailed.Output", "message": "Output may be inappropriate."}}}}
    r = rater(tmp_path, RecordedQwenClient([err]))
    _, result = run(one(r))
    assert (result.category, result.raw) == (
        "refused", "400 DataInspectionFailed.Output: Output may be inappropriate.")


def test_content_filter_without_text_is_refused(tmp_path: Path) -> None:
    doc = qwen_chunks("", finish="content_filter")
    r = rater(tmp_path, RecordedQwenClient([doc]))
    _, result = run(one(r))
    assert (result.category, result.raw) == ("refused", "finish_reason: content_filter")


def test_stream_without_finish_reason_is_transient(tmp_path: Path) -> None:
    doc = qwen_chunks('{"alive":"A"}', usage={"prompt_tokens": 10, "completion_tokens": 2})
    del doc["chunks"][-2]  # the finish chunk
    r = rater(tmp_path, RecordedQwenClient([doc]))
    _, result = run(one(r))
    assert (result.category, result.raw) == ("transient", "stream ended without finish_reason")
    assert result.usage == {"input_tokens": 10, "output_tokens": 2}


def test_unknown_finish_reason_is_fatal(tmp_path: Path) -> None:
    r = rater(tmp_path, RecordedQwenClient([qwen_chunks("x", finish="tool_calls")]))
    _, result = run(one(r))
    assert (result.category, result.raw) == ("fatal", "x\nfinish_reason: tool_calls")


def test_only_choice_zero_is_read(tmp_path: Path) -> None:
    doc = qwen_chunks('{"alive":"A"}')
    extra = json.loads(json.dumps(doc["chunks"][0]))
    extra["choices"][0].update(index=1, delta={"content": "OTHER"}, finish_reason="content_filter")
    doc["chunks"].insert(1, extra)
    r = rater(tmp_path, RecordedQwenClient([doc]))
    _, result = run(one(r))
    assert (result.category, result.raw) == ("ok", '{"alive":"A"}')


def test_bool_usage_details_are_ignored(tmp_path: Path) -> None:
    usage = {"prompt_tokens": 5, "completion_tokens": 1,
             "prompt_tokens_details": {"text_tokens": 3, "video_tokens": True}}
    r = rater(tmp_path, RecordedQwenClient([qwen_chunks("x", usage=usage)]))
    _, result = run(one(r))
    assert result.usage == {"input_tokens": 5, "output_tokens": 1, "text_tokens": 3}


def test_unexpected_error_is_a_fatal_handle_and_keeps_the_others(tmp_path: Path) -> None:
    client = RecordedQwenClient([RuntimeError(f"boom {KEY}"), fixture("qwen", "ok")])
    r = rater(tmp_path, client)

    async def go():
        a = await prepared_call(r, 1)
        b = replace(a, trial_id="pilot1/p1-m2/r1/t2", seed=2)
        return await r.submit([a, b])

    first, second = run(go())
    assert (first["category"], first["raw"]) == ("fatal", "RuntimeError: boom ***")
    assert second["category"] == "ok"


def test_aclose_closes_only_a_built_client(tmp_path: Path) -> None:
    closed: list[bool] = []
    r = QwenRater(spec(tmp_path))

    async def go():
        client = r.client

        async def close() -> None:
            closed.append(True)

        client.close = close
        await r.aclose()
        await r.aclose()  # idempotent

    run(go())
    assert closed == [True] and r._client is None
    injected = RecordedQwenClient()
    run(QwenRater(spec(tmp_path), client=injected).aclose())  # no close() on the double


def test_key_never_in_raw_handle_or_repr(tmp_path: Path) -> None:
    leaky = {"status_error": {"status": 401, "body": {"error": {
        "code": "invalid_api_key", "message": f"Incorrect API key provided: {KEY}"}}}}
    echo = qwen_chunks(f"echo {KEY}")
    r = rater(tmp_path, RecordedQwenClient([leaky, echo]))
    h1, res1 = run(one(r))
    assert res1.category == "fatal"
    assert res1.raw == "401 invalid_api_key: Incorrect API key provided: ***"
    h2, res2 = run(one(r))
    assert res2.raw == "echo ***"
    assert KEY not in json.dumps([h1, h2])
    assert KEY not in repr(r.spec)


def test_key_straddling_truncation_boundary_is_redacted(tmp_path: Path) -> None:
    err = {"status_error": {"status": 500, "body": {"error": {
        "code": "internal_error", "message": "x" * 490 + KEY + "y"}}}}
    r = rater(tmp_path, RecordedQwenClient([err]))
    _, result = run(one(r))
    assert KEY[:8] not in result.raw
    assert result.raw == "500 internal_error: " + "x" * 490 + "***y"


def test_resumed_handle_collects_without_network(tmp_path: Path) -> None:
    client = RecordedQwenClient([fixture("qwen", "ok")])
    handle, result = run(one(rater(tmp_path, client)))
    stored = json.loads(json.dumps(handle))
    fresh = RecordedQwenClient()
    [again] = run(rater(tmp_path, fresh).collect([stored]))
    assert again == result and fresh.calls == []


def test_submit_calls_are_independent(tmp_path: Path) -> None:
    client = RecordedQwenClient([fixture("qwen", "error_400_invalid"), fixture("qwen", "ok")])
    r = rater(tmp_path, client)

    async def go():
        a = await prepared_call(r, 1)
        b = replace(a, trial_id="pilot1/p1-m2/r1/t2", seed=2)
        return await r.collect(await r.submit([a, b]))

    first, second = run(go())
    assert (first.category, second.category) == ("fatal", "ok")
    assert [kw["seed"] for kw in client.ops()] == [1, 2]


def test_unprepared_media_or_bad_request_is_adapter_error(tmp_path: Path) -> None:
    client = RecordedQwenClient([fixture("qwen", "ok")])
    r = rater(tmp_path, client)

    async def unprepared():
        call = await prepared_call(r)
        await r.submit([replace(call, media=call.media[:1])])

    with pytest.raises(ConsortiumError) as info:
        run(unprepared())
    assert info.value.code == "adapter_error"

    async def three_targets():
        call = await prepared_call(r)
        await r.submit([replace(call, request=replace(REQUEST, clips=(CA, CB, CP)))])

    with pytest.raises(ConsortiumError) as info:
        run(three_targets())
    assert info.value.code == "adapter_error"
    assert client.calls == []
