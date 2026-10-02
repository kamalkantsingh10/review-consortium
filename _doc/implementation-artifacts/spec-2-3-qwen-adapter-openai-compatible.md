---
title: 'Story 2.3 — Qwen adapter (OpenAI-compatible)'
type: 'feature'
created: '2026-10-02'
status: 'ready-for-dev'
route: 'dispatch'
review_loop_iteration: 0
context:
  - '{project-root}/_doc/implementation-artifacts/epic-2-context.md'
  - '{project-root}/_doc/implementation-artifacts/epic-2-code-map.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** `provider: qwen` has no adapter, so there is no second Model. Moving later to self-hosted Qwen3-Omni weights must not need a code change.

**Approach:** Add `QwenRater`, which speaks only OpenAI-compatible Chat Completions through the `openai` SDK against the configured `base_url`. It sends the same `core.prompt.compose` parts as Gemini, with Clips inline as base64 `video_url` data URIs. Every setting it sends is archived with a per-setting `documented` flag, and every outcome is mapped to a 2.1 `Category`.

## Boundaries & Constraints

**Always:**
- **Config.** `ModelSettings.base_url` (an http(s) URL) is *required* for qwen and `config_invalid` on any other provider. There is no default, because the Model Studio endpoint is workspace-specific. `ModelSettings.reasoning_effort` (non-empty string, optional, qwen only, unset by default) is sent verbatim only when set. `ModelSpec` gains `base_url: str | None = None` and `reasoning_effort: str | None = None`. `raters_for` maps `qwen` to `QwenRater`; the key comes from `api_key_env_name` (default `DASHSCOPE_API_KEY`), and an unset key raises `api_key_missing` (2.2). Moving to vLLM is a change to `base_url` and `model`, plus `api_key_env` pointing at any non-empty token variable.
- **`prepare`** works offline. It reads `clips_dir/<clip_id>.mp4` and checks its SHA-256 against the `ClipRef` (on a mismatch: `ConsortiumError("prepare_failed", ...)`). `MediaRef.ref` = `"data:;base64," + b64`. Size is already enforced by `push test` (`limits.max_bytes` with `inline_base64` counts 4/3 of the file), so the adapter does not check it again.
- **`submit` makes the call, and the handle carries the outcome** (2.2 pattern: `{provider, raw, usage, model_build, category, settings}`, with no data URI and no key). `collect` makes no network call.
- **The request.** `AsyncOpenAI(api_key, base_url, max_retries=0)`, then `chat.completions.create` with:
  - `model`
  - one user message whose content is the `compose` parts in order: `Text` → `{"type":"text","text"}`, `Media` → `{"type":"video_url","video_url":{"url": ref}}`
  - `max_tokens`, `temperature`, `seed=call.seed` if `seed_supported`, `reasoning_effort` if set
  - `stream=True` and `stream_options={"include_usage": true}`

  Nothing else is sent: no `modalities` (text output is the default) and no `extra_body`.
- **The result.**
  - `raw` is the concatenated `delta.content`.
  - `model_build` is the last non-empty `system_fingerprint`, else `None`. This is honest about unknown builds: hosted `model` only echoes the requested name.
  - `usage.input_tokens` is `prompt_tokens` and `usage.output_tokens` is `completion_tokens`, from the final chunk.
  - Extra keys come from `prompt_tokens_details` (`text_tokens`, `audio_tokens`, `video_tokens`, `image_tokens`, read from declared fields or `model_extra`) and `completion_tokens_details.reasoning_tokens`.
  - When the stream has no usage chunk, `usage` is `{}` and the reservation stands.
- **Settings archive.** `settings = {name: {"value", "documented"}}` for `model`, `max_tokens`, `stream`, `temperature`, `seed` (when sent), `reasoning_effort` (when sent) and `prompt_format`; a setting not sent is omitted (2.2 shape). The flags come from one module table: when the `base_url` host ends in `aliyuncs.com`, `temperature` and `seed` are `false` and the rest (incl. `reasoning_effort`) are `true`; for any other host (vLLM, self-hosted) every flag is `null` ("not assessed"). `docs/INTERFACE.md` states that the Reporting manifest (Epic 4) surfaces `false` and `null`.
- **Decisions (Kamal, 2026-10-02):** `reasoning_effort` is optional, sent only when set, unset by default, and archived when sent. On non-Model-Studio hosts the `documented` flag is `null` ("not assessed").
- **Pricing.** `docs/INTERFACE.md` notes that qwen bills video and audio tokens separately, and that `media_tokens_per_s` should be set from Model Studio's billing docs, then checked against the first pilot's archived `video_tokens` + `audio_tokens`.

**Never:** a provider-specific text path, the `dashscope` SDK, `oss://` temporary uploads, public Clip URLs, SDK-internal retries, `fps`/`media_resolution` (gemini-only, rejected by 2.2's validators), or a key in `raw`, the handle, logs or errors.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Happy | chunks with text, final `finish_reason: stop`, usage chunk | `ok`, raw = joined text, usage mapped | N/A |
| Moderation | `BadRequestError`, code `data_inspection_failed` | `refused`, raw `400 data_inspection_failed: <msg>` | N/A |
| Output filtered | `finish_reason: content_filter` | `refused` | N/A |
| Truncated | `finish_reason: length` | `ok` (validation decides) | N/A |
| Rate limit | 429 `limit_requests` / `Throttling` | `transient` | N/A |
| Quota or billing | 429 `insufficient_quota`, 403 `Arrearage` / `access_denied` | `fatal` | N/A |
| Server or network | 5xx, `APIConnectionError`, `APITimeoutError` | `transient` | N/A |
| Other client error | 400 `invalid_request_error`/`payload_too_large`, 401, 404 | `fatal` | N/A |
| Mid-stream error | error after some text | the error's mapping; partial text dropped | N/A |
| vLLM host | `base_url` `http://gpu:8000/v1` | same request shape, flags `null` | N/A |
| Clip altered on disk | SHA-256 mismatch | nothing sent | `prepare_failed` |

</frozen-after-approval>

## Code Map

- `src/consortium/raters/gemini.py`, `tests/recorded.py` (2.2) -- Patterns to mirror: the handle carries the outcome, injected client, redaction.
- `src/consortium/core/prompt.py` (2.2) -- `compose`, `PROMPT_FORMAT`.
- `src/consortium/core/media_limits.py` -- The base64 byte check, already enforced by `push test`.
- `src/consortium/stages/open.py` -- `raters_for` qwen branch.
- `src/consortium/config/models.py`, `src/consortium/raters/base.py` -- `base_url` in settings and `ModelSpec`.

## Tasks & Acceptance

**Execution:**
- [ ] `pyproject.toml` -- Add `openai>=3.23,<3.24`.
- [ ] `src/consortium/raters/qwen.py` -- `QwenRater(spec, client=None)` and the `DOCUMENTED` table (with a doc URL and date).
- [ ] `src/consortium/raters/base.py`, `src/consortium/config/models.py`, `docs/schema/` -- `base_url` and `reasoning_effort` plus their qwen-only validators. Regenerate the schemas.
- [ ] `src/consortium/stages/open.py` -- Qwen branch.
- [ ] `docs/INTERFACE.md` -- `base_url` (a workspace URL example and a vLLM example), `limits.max_bytes` ≤ 10000000 for hosted qwen, `reasoning_effort`, the `documented` semantics (`null` off Model Studio), the usage keys, the pricing note.
- [ ] `tests/recorded.py`, `tests/fixtures/qwen/*.json` -- A recorded `chat.completions.create` async stream double that can raise SDK errors mid-stream.
- [ ] `tests/test_qwen.py` -- Every matrix row. The sent content equals `compose` mapped one-to-one, and is identical in text to Gemini's for the same request. `seed` is omitted when `seed_supported: false`; `reasoning_effort` is sent and archived only when set. The key is absent from `raw` and the handle.
- [ ] `tests/test_live_qwen.py` -- `@pytest.mark.live`, skipped unless `DASHSCOPE_API_KEY` and `DASHSCOPE_BASE_URL` are set. Runs one Trial on a 2 s ffmpeg Clip to a terminal state.

**Acceptance Criteria:**
- Given a Study with a gemini and a qwen Model, when a Test runs on recorded clients, then both adapters receive identical text parts and every qwen response line holds `settings` with `temperature.documented == false`.
- Given only `base_url` and `model` changed to a vLLM endpoint, when the suite runs, then no code path differs except the `null` flags.

## Implementation Notes

## Spec Change Log

## Review Triage Log

## Design Notes

Verified 2026-10-02 (PyPI, an introspected SDK, alibabacloud.com Model Studio docs):
- openai 3.23.0 (2026-10-01) defaults to `max_retries=2`, so 0 must be set.
- The international endpoint is now workspace-specific, `https://{WorkspaceId}.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1`, and `dashscope-intl.aliyuncs.com` is marked deprecated. That is why `base_url` is required.
- `stream=True` is mandatory for Qwen3.5-Omni and Qwen3-Omni-Flash but optional for qwen3.8-omni-flash. Always streaming covers every Model and vLLM.
- Local video is sent as `data:;base64,...`; the base64 string must be under 10 MB. qwen3.8-omni-flash accepts up to 2 h and up to 250 base64 files.
- Error codes: `400 data_inspection_failed`, `429 limit_requests`/`insufficient_quota`, `403 Arrearage`, `500 internal_error`.
- Retry and error handling assume 2.1: categories map onto `Category`, and `max_retries=0` leaves the engine's transient retry as the only retry. No `board.db` migration.
- The omni page documents `reasoning_effort`, `stream`, `stream_options` and `modalities`, but not `temperature`, `seed` or `top_p`. The generic compatibility page lists them without per-model guarantees, hence `false`.
- The usage example shows `prompt_tokens_details.text_tokens` and `completion_tokens_details.audio_tokens/text_tokens`. `video_tokens` is not shown in the examples, so it is read when present.

## Verification

**Commands:**
- `uv run pytest -q tests/test_qwen.py` -- expected: all pass, no network
- `uv run pytest -q && uv run ruff check src tests && uv run lint-imports` -- expected: clean
- `DASHSCOPE_API_KEY=... DASHSCOPE_BASE_URL=... uv run pytest -m live tests/test_live_qwen.py` -- expected: pass (manual)
