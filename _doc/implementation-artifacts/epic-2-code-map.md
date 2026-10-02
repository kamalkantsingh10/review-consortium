# Epic 2 Code Map: shared names for the Epic 2 specs

Epic 2 builds on Epic 1 as committed (`516af01`). The Epic 1 code map (`epic-1-code-map.md`) and `docs/INTERFACE.md` describe what exists. Every Epic 2 spec uses the names below exactly. Additions must be additive, and specs must not rename anything that exists.

## Existing surface to reuse (do not duplicate)

- **`raters/base.py`:** `Rater` Protocol (`provider`, `prepare(ClipRef)->MediaRef`, `submit(list[RaterCall])->list[Handle]`, `collect(list[Handle])->list[RaterResult]`), plus `RaterCall(trial_id, attempt, seed, request, media)` and `RaterResult(raw, usage, model_build, category)`. A `Handle` is a JSON-serialisable dict stored in `attempts.handle`.
- **`raters/fake.py`:** `FakeRater`, deterministic from the seed, with configurable usage and `invalid_rate`.
- **`engine/dispatch.py`:** the per-attempt sequence (begin_attempt+reserve → append_request → mark_sent → prepare → submit+set_handle (shielded) → collect → append_response → record_actual → record_validation → set_state). Today any category ≠ `ok` → `failed`.
- **`stages/open.py`:** `raters_for(cfg, model_ids)` builds Raters and raises `provider_unavailable` for gemini/qwen. Epic 2 replaces that.
- **`core/render.py`:** `TrialRequest` (persona_card, instructions, items, response_schema, prompt, practice, clips), `ClipRef(clip_id, sha256)` and `canonical_json`.
- **`config/models.py`:** `ModelConfig(id, provider: fake|gemini|qwen, model, settings: ModelSettings(temperature), max_output_tokens, limits: MediaLimits(max_seconds, max_bytes, inline_base64), fake)`.
- **`core/cost.py`:** `estimate`/`actual(usage, ...)`. The `usage` keys today are `input_tokens` and `output_tokens`.

## New names (introduced by the story in brackets)

| Module / name | Owns | Story |
|---|---|---|
| `raters/base.py`: `Category` | Adapter result categories: `ok`, `transient`, `refused`, `fatal`. Adapters map every provider outcome to exactly one of them; `raw` holds the provider text or the error summary, never a secret. | 2.1 |
| `config/models.py`: `RetryPolicy` on `StudyConfig.session` | `transient_retries` (default 3), `backoff_initial_s` (default 2), `backoff_max_s` (default 60). Transient retries are separate from `max_retries` (invalid answers). | 2.1 |
| `engine/dispatch.py` | Category handling: `transient` → new attempt after exponential backoff with jitter derived from the attempt seed (deterministic), up to `transient_retries`, then `failed`; `refused` → `refused` (terminal, never retried, never validated); `fatal` → `failed`. Each transient retry is a new attempt through the same sequence (reserved cost, ceiling check). | 2.1 |
| `raters/fake.py`: `FakeSettings.transient_rate`, `refusal_rate`, `fatal_rate` | Simulated outcomes, each decided from its own derived seed. | 2.1 |
| `board/trials.invalid_rates` / `stages/status` / `stages/export` | Refusal and failure counts already exist per Agent and Model. 2.1 adds per Persona attribute counts to `status --json` and the export (the Condition breakdown appears only in the export). | 2.1 |
| `core/prompt.py`: `compose(request) -> tuple[Part, ...]` with `Text(text)` and `Media(clip_id, sha256, role)` | **The single, deterministic way a `TrialRequest` becomes provider message parts.** Both adapters use it, so the text is identical across providers. Order: persona card, instructions, practice examples (each Clip followed by its intended answer as canonical JSON), target Clip(s) (pairwise: A then B, labelled with the option labels), prompt, Items and response schema as canonical JSON. Pure. | 2.2 |
| `config/models.py`: `ModelSettings` additions | `fps` (optional), `seed_supported: bool`, `media_resolution` (optional; gemini low/medium/high), `base_url` (optional; qwen/OpenAI-compatible), `api_key_env` (default per provider: `GEMINI_API_KEY`, `DASHSCOPE_API_KEY`). | 2.2 / 2.3 |
| `raters/base.py`: `ModelSpec` (frozen dataclass) | Everything an adapter needs, as plain values: `model_id`, `provider`, `model`, `temperature`, `fps`, `seed_supported`, `media_resolution`, `base_url`, `api_key` (read from the env by `stages/open`), `max_output_tokens`, `clips_dir`. `stages/open` builds it from `ModelConfig`, so adapters never import config. | 2.2 |
| `raters/gemini.py`: `GeminiRater(spec: ModelSpec, client=None)` | `google-genai` `generate_content`, pinned settings, File API upload in `prepare`, re-prepare on expiry, error → Category mapping, `model_version` → `model_build`. | 2.2 |
| `raters/qwen.py`: `QwenRater(spec: ModelSpec, client=None)` | `openai` SDK Chat Completions against `base_url`; Clips inline as base64 `video_url` data URIs; error → Category mapping; audio/video token usage; settings archived with a `documented` flag. | 2.3 |
| `stages/open.raters_for` (existing, extended) | The one place that maps a provider to an adapter class and builds its `ModelSpec`. A missing API key env var raises `ConsortiumError("api_key_missing", ...)` before confirmation. (It can't live in `raters/`, because adapters may not import config.) | 2.2 (Qwen added in 2.3) |
| `raters/recorded.py` (tests only, under `tests/`) | Recorded-response doubles: adapters take an injectable SDK client so tests run without network. Live smoke tests are marked `@pytest.mark.live` and skipped unless the key env var is set. | 2.2 / 2.3 |

## Rules

- Adapters import `core` and `raters.base` only (AD-1), and never import config, board or engine. They receive a `ModelSpec`, and an optional injected SDK client for tests.
- Adapters never change text. All text comes from `core.prompt.compose`.
- API keys come only from environment variables. Never put them in config, the Archive, `raw`, logs or error messages.
- Usage keys stay `input_tokens` and `output_tokens`. Extra provider detail (e.g. `video_tokens`, `audio_tokens`) goes in additional keys, which `core.cost.actual` ignores.
- Tests never touch the network. Live tests are opt-in only.

## Additions agreed after spec writing (binding)

- **Retry settings:** `session.retry` (`RetryPolicy`) is set in `study.yaml` only; there is no per-Test override.
- **Retry budgets:** invalid answers and transient errors have separate budgets, counted from `attempts` rows. The total cap is `1 + max_retries + transient_retries`.
- **Board helpers (2.1):** `board/trials.record_transient` and `board/trials.attempt_counts`.
- **Persona helpers (2.1):** `core/personas.attribute_values` and `core/personas.tally_by_attribute`, shared by status and export.
- **status (2.1):** `status --json` gains `by_persona_attribute`. The text table is unchanged.
- **Archived settings (2.2):** `RaterResult.settings: Mapping | None` is archived as an optional `settings` field on response lines, as `{name: {value, documented}}`.
- **Rater builder (2.2):** `stages/open.raters_for(cfg, model_ids, study)` takes the Study folder, for `ModelSpec.clips_dir`.
- **`ModelSpec.base_url`** arrives in 2.3, default `None`.
- **`core/prompt.py` (2.2):** `Media.role` is `practice` or `target`. Fixed label strings and `PROMPT_FORMAT = 1` live here, and the format number is archived with the settings.
- **New error codes:** `prepare_failed` (2.2) and `api_key_missing` (2.2).
- **`ModelConfig.api_key_env_name`:** a property giving the default env var name per provider.
- **Provider-only settings:** `fps` and `media_resolution` are gemini-only; `base_url` is qwen-only and required for qwen. A setting on the wrong provider gives `config_invalid`.
- **Tests:** doubles in `tests/recorded.py` and `tests/fixtures/{gemini,qwen}/`; pytest marker `live`, with `-m "not live"` in addopts.
- **Dependencies:** `google-genai>=2.27,<2.28` and `openai>=3.23,<3.24`. `google` and `openai` are forbidden in `core` by the import contract.
- **SDK retries:** each SDK's internal retries are disabled, so the engine's transient retry is the only retry.

## Additions from Kamal's decisions and Story 2.4 (2026-10-02, binding)

- **Attrition export (2.1):** `exports/<test>-attrition.csv` (`dimension`, `attribute`, `value`, `trials`, `refused`, `failed`).
- **`ModelSettings.thinking_level` / `ModelSpec.thinking_level` (2.2):** gemini-only, `minimal|low|medium|high`, sent as `ThinkingConfig(thinking_level=...)`; template example `low`.
- **`ModelSettings.reasoning_effort` / `ModelSpec.reasoning_effort` (2.3):** qwen-only, optional, sent and archived only when set.
- **Archived settings:** a setting not sent is omitted from `settings` (2.2 and 2.3).
- **Panel design (2.4):** `PersonaFrame.big_five: BigFiveDesign(fraction "1"|"1/2"|"1/4", replicates 1..3)`, legacy `all_32` accepted; `core/personas.design_profiles`; `board/trials.any_trials`; error `panel_in_use`; `meta.json` `design`.
- **Migrations:** none in Epic 2; `MIGRATIONS` stays at 5.
