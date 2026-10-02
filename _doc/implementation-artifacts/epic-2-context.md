# Epic 2 Context: Real AI raters (Gemini and Qwen)

<!-- Compiled from planning artifacts. Edit freely. Regenerate with compile-epic-context if planning docs change. -->

## Goal

Run the blinded pipeline that Epic 1 built with the Fake rater against two real video+audio models: Gemini and hosted `qwen3.8-omni-flash`. Settings are pinned and archived, per-Model media limits are enforced, cost is real, and rate limits, transport errors and safety refusals are handled without operator help. This makes the tool usable for actual pilot data. It also keeps differential attrition visible (refusals by Model, Persona attribute and Condition), so that refusals cannot quietly bias results.

## Stories

- Story 2.1: Provider failure handling and refusals
- Story 2.2: Gemini adapter
- Story 2.3: Qwen adapter (OpenAI-compatible)
- Story 2.4: Panel size by fraction or replicates

## Requirements & Constraints

- Every Trial ends in exactly one terminal state: `valid`, `invalid`, `refused` or `failed`. Transport errors and rate limits are retried with backoff. Safety refusals are recorded as `refused` and never retried. Exhausted transient retries or fatal errors end the Trial as `failed`.
- Refusal and failure counts are reported per Model, per Agent and per Persona attribute (`status --json` and the Export). The per-Condition breakdown appears **only in the Export**, because Conditions must never reach the rating side.
- A long Run must finish unattended while the Fake rater simulates rate limits, transport errors and refusals.
- Each adapter pins the model version, temperature (> 0, default 0.7), fps, audio handling, and the seed where it is supported. The pinned settings and the provider-reported model build are archived for every request.
- Any Test can run on 1..N Models. The Fake rater stays available offline.
- Reproducibility for these closed hosted models means "pinned and archived", not re-runnable on weights. If the provider reports no build, record that honestly as unknown.
- Moving Qwen to a self-hosted vLLM (open-weight Qwen3-Omni) must be a config-only change: `base_url` and `model`.
- Local-first: data leaves the machine only for the configured providers, and `open` names them before any upload. Nothing is uploaded before confirmation and a successful cost reservation.
- API keys come only from env vars (`GEMINI_API_KEY`, `DASHSCOPE_API_KEY`). Never put them in Study files, the Archive, `raw`, logs or error messages. A missing key fails before confirmation.
- Tests never touch the network. Adapters are tested against recorded-response doubles. Live smoke tests are opt-in (`@pytest.mark.live`) and run only when the key is set.

## Technical Decisions

- **Port and engine:** every Model call goes through `engine.dispatch` and the `Rater` port (`prepare` / `submit` / `collect`), with a per-provider `asyncio.Semaphore`. Adapters import only `core` and `raters.base`, never config, board or engine. They receive a frozen `ModelSpec` built by `stages/open.raters_for` (the only provider-to-class mapping), plus an optional injected SDK client.
- **Categories:** adapters map every outcome to exactly one of `ok | transient | refused | fatal`. `raw` holds the provider text or an error summary.
- **Engine handling:** a `transient` result gets a new attempt after exponential backoff, with jitter derived deterministically from the attempt seed, up to `transient_retries`, then `failed`. Each retry runs the full attempt sequence (attempt increment, reservation, ceiling check, archive). `refused` is terminal and never validated. `fatal` ends as `failed`. Transient retries are counted separately from `max_retries`, which covers invalid answers. A `RetryPolicy` on the session config holds `transient_retries=3`, `backoff_initial_s=2` and `backoff_max_s=60`.
- **Trial lifecycle:** `planned → sent → terminal`. Terminal states never change. Each `(trial_id, attempt)` is dispatched at most once, and the highest valid attempt wins. Resume collects any `sent` attempt that has a handle, so adapter handles must be JSON-serialisable and collectable after a restart.
- **Archive before state:** the request is archived before `sent`, and the raw response before any state change. Media is referenced by `clip_id` plus SHA-256, never by a provider file handle.
- **Core renders, adapters transmit:** `core.prompt.compose(request)` is the one deterministic conversion from a `TrialRequest` to message parts (persona card, instructions, practice examples with answers, targets labelled A/B for pairwise, prompt, Items and schema as canonical JSON). Adapters add only the pinned settings and never change text. Parsing and validation stay in core.
- **Cost:** there is one offline cost function, and no provider token-count calls. The ledger has one row per attempt, with reserved and actual cost. Usage keys stay `input_tokens` / `output_tokens`, and extra detail (`video_tokens`, `audio_tokens`) goes in additional keys.
- **Gemini (verified facts):** use `google-genai` `generate_content`, not the Interactions API, which reports no concrete version. `response.model_version` gives the model build. Custom fps works only in **static** processing mode, so pin static explicitly (agentic mode silently changes tokens and fps). Upload through the File API in `prepare`, once per Clip, named by Clip ID; reuse the reference and re-prepare on expiry. Pin temperature, seed and `media_resolution`. Gemini processes both audio and video. `usage_metadata` gives a per-modality breakdown.
- **Qwen (verified facts):** use the `openai` SDK, Chat Completions only, against `base_url`. Clips go inline as base64 `video_url` data URIs, and the encoded string must be under 10 MB (the temp-upload/`oss://` path doesn't work with the openai SDK outside Beijing). Audio and video are billed separately as `audio_tokens` and `video_tokens`. `temperature`, `seed` and `top_p` are **undocumented** for omni models: send them, but archive each setting with `documented: false` for the manifest. The response `model` only echoes the requested name, so there is no build. Streaming may be required in some modes; usage then arrives in the final chunk (`stream_options.include_usage`).
- **Stack:** google-genai 2.27, openai 3.23 (both release often).

## Cross-Story Dependencies

- 2.1 comes first. It defines `Category`, `RetryPolicy`, the engine's category handling, the Fake rater's `transient_rate` / `refusal_rate` / `fatal_rate`, and the per-Persona-attribute counts. 2.2 and 2.3 only map provider errors onto those categories.
- 2.2 introduces `core.prompt.compose`, `ModelSpec`, the `ModelSettings` additions (`fps`, `seed_supported`, `media_resolution`, `api_key_env`), the `api_key_missing` check, and the recorded-double test pattern. 2.3 reuses all of these and adds `base_url` and the Qwen branch in `raters_for`.
- Builds on Epic 1: `raters_for` currently raises `provider_unavailable` for gemini/qwen, and Epic 2 replaces that. `push test` already enforces `max_seconds` / `max_bytes` / `inline_base64`. Status and export already count refused and failed per Agent and Model.
- Feeds later epics. Epic 3 screening runs through these adapters, and screening is stamped with the Model settings hash, so pinned settings must hash stably. Epic 4's Reporting manifest surfaces the `documented: false` settings and the providers that received Clips.
