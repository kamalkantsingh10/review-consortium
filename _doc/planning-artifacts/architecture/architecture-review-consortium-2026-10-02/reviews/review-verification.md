---
review: verification (technology reality-check)
target: ../ARCHITECTURE-SPINE.md
date: 2026-10-02
method: PyPI JSON API, local install + introspection in a scratch venv, official docs (ai.google.dev, alibabacloud.com, import-linter docs), web search
---

# Verification review: Architecture Spine

## Verdict

**Mostly verified. Two provider-integration risks block a straightforward Qwen adapter.** Every Stack version is current and checked. The Gemini assumptions hold, confirmed against the installed SDK. import-linter and `uv tool install` work as the spine expects. However, the Qwen path depends on two facts that were never checked: how a local video file reaches Model Studio through the openai SDK, and what clip length `qwen3-omni-flash` accepts. The open question in the Deferred section (whether the hosted model is the open-weight model) can now be answered: it is not.

## Already verified in .memlog.md (re-confirmed today)

| Item | Memlog | Re-check 2026-10-02 | Status |
| --- | --- | --- | --- |
| typer / pydantic / google-genai / openai / import-linter / pytest / ruff / pyyaml | 0.27.2 / 2.13.5 / 2.27.0 / 3.23.0 / 2.15 / 9.1.1 / 0.16.10 / 6.0.3 | Same on PyPI. google-genai 2.27.0 and openai 3.23.0 were uploaded 2026-10-01, so expect frequent minor bumps. | OK |
| Python 3.12.3, uv 0.11.9, ffmpeg 6.1.1 (local) | yes | Same. **PyPI latest uv is 0.12.21** (2026-09-29). | Minor: the spine pins uv 0.11, one minor release behind |
| Gemini Batch = 50% | morphllm.com (third party) | Official docs: "Batch API usage is priced at 50% of the standard interactive API cost", with a 24h target. They don't say explicitly whether File API **video** works in batch. | OK. Video-in-batch is unconfirmed; this is deferred anyway |
| DashScope batch = 50%, omni batch varies by region | Model Studio docs | Not re-checked (batch is deferred) | Accepted |

## New checks

### 1. google-genai: video + audio, fps, File API upload (AD-6, AD-7, AD-11)

- **Verified by introspecting google-genai 2.27.0:**
  - `types.Part` has `file_data`, `video_metadata` and `media_resolution`.
  - `types.VideoMetadata` has `fps`, `start_offset` and `end_offset`.
  - `Files.upload(file=..., config=UploadFileConfig(name, mime_type, display_name))` exists, so AD-11's "Clip ID as file name" maps to `display_name`/`name`.
  - `GenerateContentConfig` has `temperature`, `seed`, `media_resolution`, `response_json_schema` and `thinking_config`.
- **Docs:** video understanding processes "both the audio and visual streams". The default sampling rate is 1 fps. Static mode costs about 100 tok/s at low resolution and about 300 tok/s at high resolution, and audio is 32 tok/s. The File API takes files up to 20 GB on the paid tier.
- **New since training data (risk: medium):** the docs now describe a `processing` mode, **static vs agentic**. Custom fps "is only supported in static mode". In agentic mode, video tokens are reported as thought tokens and tool-use tokens. SDK 2.27 has a `Part.media_processing` field. The Gemini adapter must pin static processing explicitly as part of the "pinned settings" (AD-7). Otherwise fps, cost estimates and reproducibility could change silently if a model defaults to agentic mode.

### 2. Model Studio / Qwen3-Omni via openai SDK (AD-6, AD-7, AD-11, Stack)

- **Verified:** Qwen-Omni models are served on the OpenAI-compatible Chat Completions endpoint. Video goes in a `video_url` content part, and the audio track is processed and billed separately (as `video_tokens` and `audio_tokens`). Several files per request are allowed. `qwen3-omni-flash` is offered in Singapore, US (Virginia), Frankfurt, Tokyo, Hong Kong and Beijing.
- **RISK HIGH: clip length.** The docs state "Qwen3-Omni-Flash series: audio and video input up to **150 seconds**" (Qwen3.8-Omni-Flash allows 2 h, Qwen3.5-Omni 1 h). The PRD defaults to 2 Practice clips plus the target per Trial. Whether the 150 s applies per file or per request isn't stated. Either way it caps Clip duration and should become a validation rule at `push` or `open`, alongside the "canonical ffmpeg profile" deferral.
- **RISK HIGH: how local files reach the API.** The openai SDK accepts only a public URL or a base64 data URI, and "the encoded Base64 string must be smaller than **10 MB**". Model Studio's temporary-upload service (`oss://` URLs) states that "The OpenAI SDK is not supported" for it. It is also "available only in the China (Beijing) region", is model-bound, and deletes files after 48 h. So in international regions the spine's choice of "openai SDK against DashScope compatible mode" works only if canonical clips stay under about 7.5 MB raw (the 10 MB base64 limit), or if clips are hosted on a public URL. Hosting is a privacy and blinding consideration (NFR-2). The canonical ffmpeg profile must be sized for this, or the `dashscope` SDK and the Beijing region must be considered.
- **Streaming (risk: low-medium):** the docs require `stream=True` in some modes (thinking mode and audio output), but it is optional for text output. The adapter should be written to handle streaming, and usage then arrives in the final chunk via `stream_options={"include_usage": true}`.
- **Determinism params (risk: medium):** the Qwen-Omni page documents `reasoning_effort` / `enable_thinking`, but **does not document `temperature`, `seed` or `top_p`** for omni models. The openai SDK will send them, but whether they are honoured is unconfirmed. The PRD relies on a pinned temperature above 0 (A2/A3).

### 3. Is hosted `qwen3-omni-flash` the open-weight Qwen3-Omni? (Deferred item, PRD A2)

- **Answer: no (risk: HIGH for the A2 rationale).**
  - The QwenLM/Qwen3-Omni README lists "Qwen3-Omni-Flash-Instruct/Thinking" only in benchmark tables, with no weights, separate from the Apache-2.0 `Qwen3-Omni-30B-A3B` checkpoints.
  - Third-party sources (HN thread on Qwen3-Omni-Flash-2025-12-01, comparison sites) describe Flash as a closed-weight, further-trained derivative of the 30B-A3B model.
  - Model Studio docs make no equivalence claim. The response `model` field echoes `"qwen3-omni-flash"` with no snapshot.
- The PRD's reason for choosing Qwen ("if the cloud model is retired, the weights stay available") therefore does not hold for the hosted v1 path. Running the same weights would need self-hosted vLLM or another host serving `Qwen3-Omni-30B-A3B-Instruct`.
- Note also that `qwen3.8-omni-flash` (released September 2026) is now the model the docs recommend for text analysis. That suggests `qwen3-omni-flash` is heading for maintenance mode, though no deprecation notice was found for it.

### 4. import-linter `forbidden` and `layers` (AD-1, AD-2)

- **Verified:** import-linter 2.15 ships the `forbidden`, `layers`, `independence`, `protected` and `acyclic_siblings` contracts.
- **Reality-checked:** a scratch package with a `layers` contract and a `forbidden` contract (`core` must not import `board.blinding`) correctly reported the violation (`demo.core.x -> demo.board.blinding`).
- **Suggestions:**
  - AD-1's "stages never import another stage" maps naturally to an `independence` contract over `consortium.stages.*`.
  - AD-2's "only push/export may import blinding" maps more directly to a `protected` contract with an allow-list than to a forbidden list that has to name every other module.

### 5. `uv tool install` with a console script (FR-25, Stack)

- **Reality-checked:** with local uv 0.11.9, `uv tool install .` on a hatchling package with `[project.scripts] demo = "demo.cli:app"` (a Typer app) installed the package, and the `demo` executable ran.

### 6. Provider-reported model build (AD-7)

- **Gemini, verified:** `GenerateContentResponse.model_version` exists, documented as "The model version used to generate the response". Sources say it resolves aliases such as `gemini-flash-latest` to a concrete model id. That id is a version string, not a weights hash.
- **Gemini caveat:** a developer-forum thread reports that the newer **Interactions API** does not return a concrete version. Use `generate_content`, not the Interactions API, in the adapter.
- **Qwen, risk medium:** the response `model` field just echoes the requested name. No build or snapshot is reported. Pin a dated snapshot name if Model Studio offers one; none is documented for `qwen3-omni-flash` on the current page. Otherwise record "unknown build" honestly in the Archive.

### 7. Token counting for the cost reservation (AD-6)

- **Gemini, verified:** `client.models.count_tokens` exists and the docs state that it covers video and audio. `usage_metadata` includes `prompt_tokens_details` (per modality), `thoughts_token_count` and `tool_use_prompt_token_count`.
- **DashScope, risk medium:** no count-tokens endpoint for video was found on the OpenAI-compatible endpoint. Usage is reported only after the call. The pre-submit worst-case reservation for Qwen must come from a local formula: frames × tokens per 32×32 pixel tile, plus audio at about 427 tokens/min, per the Model Studio billing docs. That formula should live in `prices.yaml` or the price table (AD-8, AD-9), not in code.

## Possibly out of date / unconfirmed

- uv pin 0.11: current is 0.12.21.
- Gemini static/agentic processing mode: new. Pin it.
- `qwen3-omni-flash`: the newer `qwen3.8-omni-flash` exists and the docs steer users to it. The status of the older model is unconfirmed.
- Qwen `temperature` / `seed` support on omni models: undocumented.
- Gemini Batch with File API video: undocumented (deferred).

## Sources

- PyPI JSON API, `https://pypi.org/pypi/<pkg>/json`, queried 2026-10-02
- https://ai.google.dev/gemini-api/docs/video-understanding
- https://ai.google.dev/gemini-api/docs/tokens
- https://ai.google.dev/gemini-api/docs/batch-mode
- https://github.com/googleapis/python-genai/issues/2019 (VideoMetadata fps)
- https://discuss.ai.google.dev/t/interactions-api-response-doesnt-report-the-concrete-model-version-no-equivalent-of-generatecontents-modelversion/185217
- https://dev.to/ai_changewatch/a-model-google-lists-as-shut-down-answered-me-today-read-modelversion-before-you-trust-it-2bbf
- https://www.alibabacloud.com/help/en/model-studio/qwen-omni
- https://www.alibabacloud.com/help/en/model-studio/get-temporary-file-url
- https://www.alibabacloud.com/help/en/model-studio/vision
- https://www.alibabacloud.com/help/en/model-studio/model-pricing
- https://github.com/QwenLM/Qwen3-Omni
- https://huggingface.co/Qwen/Qwen3-Omni-30B-A3B-Instruct
- https://news.ycombinator.com/item?id=46219538 (Qwen3-Omni-Flash-2025-12-01, closed weights)
- https://import-linter.readthedocs.io/en/stable/contract_types/
- Local checks: google-genai 2.27.0, openai 3.23.0 and import-linter 2.15 installed in a scratch venv and introspected. A `uv tool install` and `lint-imports` run were made on a throwaway package.
