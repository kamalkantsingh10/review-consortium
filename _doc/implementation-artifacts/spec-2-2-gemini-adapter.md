---
title: 'Story 2.2 — Gemini adapter'
type: 'feature'
created: '2026-10-02'
status: 'done'
baseline_commit: 'bb2c0d304daa76e0ab15b733f0618f126d78ba28'
route: 'dispatch'
review_loop_iteration: 0
context:
  - '{project-root}/_doc/implementation-artifacts/epic-2-context.md'
  - '{project-root}/_doc/implementation-artifacts/epic-2-code-map.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** `raters_for` refuses `provider: gemini`, so no real Model can rate Clips. There is also no shared way to turn a `TrialRequest` into provider message parts, so the two adapters could drift apart in their text.

**Approach:** Add pure `core.prompt.compose`, a frozen `ModelSpec`, and `GeminiRater`. `GeminiRater` uploads each Clip once through the File API, calls `generate_content` with pinned settings and static media processing, archives the settings it sent and the reported `model_version`, and maps every outcome to a 2.1 `Category`. Tests use an injected recorded client. A live smoke test is opt-in.

## Boundaries & Constraints

**Always:**
- **`compose(request) -> tuple[Part, ...]`** (pure, deterministic). `Part = Text(text) | Media(clip_id, sha256, role)`, with `role` one of `practice` or `target`. The order is:
  1. persona card
  2. instructions
  3. each Practice example: `Text("Practice example {n}:")`, its Clip(s), then `Text("Intended answer: " + canonical_json(answer))`
  4. targets: a single target follows `Text("Video to rate:")`; a pairwise pair is `Text("Video {opt0}:")`, A, `Text("Video {opt1}:")`, B, using the pairwise Item's option labels
  5. prompt
  6. `Text("Items: " + canonical_json(items))`
  7. `Text("Response schema: " + canonical_json(response_schema))`

  The fixed strings are module constants, and `PROMPT_FORMAT = 1`. Changing any of them bumps it.
- **`ModelSpec`** (frozen; `api_key` uses `field(repr=False)`) holds `model_id, provider, model, temperature, fps, seed_supported, media_resolution, thinking_level, api_key, max_output_tokens, clips_dir`. Only `stages/open.raters_for` builds it.
- **`ModelSettings` additions:**
  - `fps` (float > 0, optional; gemini only)
  - `seed_supported` (default `true`: send the attempt seed)
  - `media_resolution` (`low|medium|high`, optional; gemini only)
  - `thinking_level` (`minimal|low|medium|high`, optional; gemini only). The init template's commented gemini example sets `low`.
  - `api_key_env` (optional; the default comes from a `ModelConfig.api_key_env_name` property: `GEMINI_API_KEY` for gemini, `DASHSCOPE_API_KEY` for qwen, none for fake)

  A setting given on the wrong provider is `config_invalid`.
- **`raters_for(cfg, model_ids, study)`** maps `gemini` to `GeminiRater`. An unset or empty key env var raises `ConsortiumError("api_key_missing", "<model_id>: set <ENV>")`. This happens before confirmation, at the existing call sites, and never in a dry run. Building a Rater makes no network call.
- **`prepare`.** The File name is `files/rc-` plus the Clip ID with `_` replaced by `-`, and `display_name` is the Clip ID. The adapter `get`s that name first:
  - The File is ACTIVE and more than 1 h from `expiration_time` → reuse it.
  - It is missing → upload `clips_dir/<clip_id>.mp4` (`video/mp4`).
  - It is FAILED or near expiry → delete it and upload again.

  Then poll until ACTIVE (every 2 s, 300 s timeout). An upload error is retried 3 times with 2/4/8 s backoff. After that, or on FAILED or timeout, the adapter raises `ConsortiumError("prepare_failed", <summary>)`; the Run stops and can be resumed. `MediaRef.ref` = the File URI. The adapter remembers each File's `expiration_time`.
- **`submit` makes the call and returns the outcome as the handle.** The handle is `{provider, raw, usage, model_build, category, settings}`, JSON only, with no File URI and no key. `collect` turns it back into a `RaterResult`, so a resumed handle is never sent again. Before the call, the adapter re-prepares any Clip within 1 h of expiry. If the call fails with 403 or 404 on a File, it re-prepares once and retries once in the same attempt (no answer was produced).
- **Request.** `client.aio.models.generate_content` with a single user `Content` built from `compose` in order:
  - `Text` → `Part(text=...)`
  - `Media` → `Part(file_data=FileData(file_uri, "video/mp4"), media_processing=STATIC, video_metadata=VideoMetadata(fps=spec.fps) if fps)`

  The config is `GenerateContentConfig(temperature, max_output_tokens, seed=call.seed if seed_supported, media_resolution if set, thinking_config=ThinkingConfig(thinking_level=ThinkingLevel[level.upper()]) if thinking_level set)`, plus `HttpRetryOptions(attempts=1)`, so that the engine's 2.1 transient retry is the only retry.
- **`RaterResult.settings`** (new, optional, default `None`) is archived as a `settings` field on the response line only when it is not `None`. Fake lines stay unchanged. Each setting is recorded as `{name: {"value": v, "documented": true}}`, covering `model`, `temperature`, `seed`, `fps`, `media_resolution`, `thinking_level`, `media_processing: "static"`, `max_output_tokens` and `prompt_format`. A setting that is not sent is omitted.
- **Result mapping:**
  - `model_build` = `response.model_version`, or `None`.
  - `usage.input_tokens` = `prompt_token_count` + `tool_use_prompt_token_count`.
  - `usage.output_tokens` = `candidates_token_count` + `thoughts_token_count` (thinking is billed as output).
  - Extra keys are added: `text_tokens`, `video_tokens` and `audio_tokens` from `prompt_tokens_details`, plus `thoughts_tokens`. Missing counts are 0. With no `usage_metadata`, usage is `{}`.
- **Decisions (Kamal, 2026-10-02):** thinking is pinned through `settings.thinking_level` (gemini only), sent as `thinking_config` and archived; the init template default is `low`. Story 2.2 stays one story (no split).
- **`pricing`**: `docs/INTERFACE.md` gives the gemini derivation `media_tokens_per_s ≈ per-frame tokens(media_resolution) × fps + 32` (audio). Per the docs, a frame costs 66 tokens at low resolution and 258 at high, at 1 fps by default (low ≈ 100/s). No cost-code change.

**Never:** structured-output or JSON-mode settings (text must be identical across providers), the Interactions API, provider token counting, adapter-side parsing or validation, importing config, board or engine from `raters/`, or a key in `raw`, the handle, logs or errors (`raw` replaces any occurrence of the key with `***`). Batch mode is out of scope.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Happy | text candidate, finish `STOP` | `ok`, raw = joined text parts, `model_build` = `model_version` | N/A |
| Prompt blocked | `prompt_feedback.block_reason` set | `refused`, raw `blocked: <REASON>` | N/A |
| Output blocked | finish `SAFETY`/`PROHIBITED_CONTENT`/`BLOCKLIST`/`SPII`/`RECITATION` | `refused`, raw = any text + `finish_reason: <R>` | N/A |
| Truncated | finish `MAX_TOKENS` | `ok` (validation decides) | N/A |
| Empty | no candidate, no block reason | `ok`, raw `""` (→ invalid retry) | N/A |
| Rate/server | `APIError` code 408/429/500/502/503/504, httpx transport error or timeout | `transient`, raw `<code> <status>: <message ≤500 chars>` | N/A |
| Client error | 400/401/403/404 (after the one File retry) | `fatal` | N/A |
| File expired | remembered expiry < now+1 h | re-uploaded before the call | N/A |
| Resumed handle | `collect(handle)` after restart | same `RaterResult`, no network | N/A |
| Missing key | `GEMINI_API_KEY` unset | nothing sent or uploaded | `api_key_missing` |

</frozen-after-approval>

## Code Map

- `src/consortium/raters/base.py` -- `Rater`, `RaterResult` (gets `settings`), `MediaRef`; add `ModelSpec`.
- `src/consortium/raters/fake.py` -- Pattern for a handle that carries the outcome.
- `src/consortium/engine/dispatch.py` -- `prepare` runs after reservation (no upload before confirmation); passes `result.settings` to the Archive.
- `src/consortium/archive/jsonl.py` -- `append_response` gains an optional `settings`.
- `src/consortium/stages/open.py` -- `raters_for` (4 call sites; add `study`).
- `src/consortium/config/models.py` -- `ModelSettings`, `ModelConfig`.
- `src/consortium/core/render.py` -- `TrialRequest`, `canonical_json`.

## Tasks & Acceptance

**Execution:**
- [x] `pyproject.toml` -- Add `google-genai>=2.27,<2.28`. Register the pytest marker `live` and add `-m "not live"` to `addopts`. Add `google`/`openai` to core's forbidden import contract.
- [x] `src/consortium/core/prompt.py` -- `Text`, `Media`, `PROMPT_FORMAT`, `compose`.
- [x] `src/consortium/raters/base.py` -- `ModelSpec`, `RaterResult.settings`.
- [x] `src/consortium/raters/gemini.py` -- `GeminiRater(spec, client=None, now=None)` per the rules above. Lazily builds `genai.Client(api_key=...)`.
- [x] `src/consortium/config/models.py`, `docs/schema/`, `src/consortium/templates/study/study.yaml` -- Settings additions (incl. `thinking_level`), validators and `api_key_env_name`; a commented gemini Model example with `thinking_level: low`. Regenerate the schemas.
- [x] `src/consortium/stages/open.py` -- Gemini branch, `ModelSpec` from config plus env, `api_key_missing`.
- [x] `src/consortium/archive/jsonl.py`, `src/consortium/engine/dispatch.py` -- Pass `settings` through.
- [x] `docs/INTERFACE.md` -- New settings (incl. `thinking_level`), key env vars, the response `settings` field, `api_key_missing`/`prepare_failed`, the gemini price derivation, the File naming and 48 h reuse.
- [x] `tests/recorded.py`, `tests/fixtures/gemini/*.json` -- A fake `aio.files`/`aio.models` client that replays recorded responses and raises recorded `APIError`s, logging the calls made.
- [x] `tests/test_prompt.py` -- Golden `compose` output for a single, pairwise and practice request. Purity: the same input gives an equal tuple.
- [x] `tests/test_gemini.py` -- Every matrix row. Also: the sent parts equal `compose` mapped one-to-one, with static processing and fps; seed omitted when `seed_supported: false`; `thinking_config` sent and archived only when `thinking_level` is set; a 2nd `prepare` of a Clip makes no upload; the key never appears in `raw` or the handle.
- [x] `tests/test_open.py` -- `api_key_missing` before confirmation; a dry run needs no key; Archive response lines carry `settings`.
- [x] `tests/test_live_gemini.py` -- `@pytest.mark.live`, skipped without `GEMINI_API_KEY`. One 2 s ffmpeg `testsrc` Clip, one Trial, end to end: it reaches a terminal state and has a `model_build`.

**Acceptance Criteria:**
- Given a gemini Model and the key, when a Test runs with the recorded client, then each distinct Clip is uploaded once and every attempt's response line holds `raw`, `usage`, `model_build` and `settings`, with an actual cost in the ledger.
- Given the same `TrialRequest`, when it is composed twice or in another process, then the parts are equal.

## Implementation Notes

## Spec Change Log

## Review Triage Log

| # | Source | Finding | Verdict | Route |
|---|---|---|---|---|
| 1 | BH, EC | Reused File never checked against Clip content; names collide across Studies/Models; one Rater deletes another's File; concurrent re-prepare races | high | patch (content-addressed names, hash check, never delete ACTIVE, per-Clip lock, 409=exists) |
| 2 | BH, EC | prepare retries non-transient errors; poll errors not retried; OSError conflates network and local | medium | patch |
| 3 | BH, EC | 403 from a bad key re-prepares all Clips; prepare_failed escapes submit after mark_sent; partial multi-call loss; missing uri KeyError | high | patch |
| 4 | BH, EC | No request timeout; non-httpx SDK errors escape | medium | patch |
| 5 | EC | IMAGE_SAFETY and other safety finish reasons classed ok | medium | patch (refused) |
| 6 | EC | Key straddling the truncation boundary survives redaction | high | patch |
| 7 | BH, EC | Practice pairs unlabelled; >2 Clips dropped; label fallback mismatch | medium | patch |
| 8 | EC | Whitespace-only key passes | low | patch |
| 9 | EC, BH | fps inf accepted; seed_supported on fake | low | patch |
| 10 | BH | httpx undeclared | low | patch (pin narrowness kept for reproducibility) |
| 11 | BH | Template example incomplete; media_tokens guidance; documented flag undefined | low | patch (docs) |
| 12 | BH, EC | Live test too weak; leaves its File | low | patch |
| 13 | VG | _model_spec mapping, resume api_key_missing, prepare_failed stop/resume, poll/404/PROCESSING branches, Fake settings shape untested | medium | patch (tests) |
| 14 | EC | ACTIVE without uri; no expiration_time | low | patch |

## Design Notes

Verified 2026-10-02 (PyPI, an introspected SDK, ai.google.dev):
- google-genai 2.27.0 was released 2026-10-01.
- `Part.media_processing` is a `MediaProcessing` enum (`STATIC`/`AGENTIC`), and the docs call static the default. "Static processing mode" is therefore a real setting; it is pinned per Part and not left to defaults, because custom fps works only in static mode.
- `VideoMetadata(fps, start_offset, end_offset)` and `GenerateContentConfig.{temperature, seed, media_resolution}` exist.
- `File` has `state` (`PROCESSING`/`ACTIVE`/`FAILED`) and `expiration_time`. Files are kept 48 h, with 20 GB per project and 2 GB per file.
- A File ID allows only lowercase letters, digits and `-` (up to 40 characters), so the Clip ID's `_` cannot be used as is.
- `usage_metadata` has `prompt_token_count`, `candidates_token_count`, `thoughts_token_count`, `tool_use_prompt_token_count` and `prompt_tokens_details[{modality, token_count}]`.
- Errors are `errors.APIError(code, status, message)`, with subclasses `ClientError` (4xx) and `ServerError` (5xx). Block signals are `prompt_feedback.block_reason` and `Candidate.finish_reason`.

- `ThinkingConfig(thinking_level: ThinkingLevel)` with `ThinkingLevel` `MINIMAL|LOW|MEDIUM|HIGH` (case-insensitive enum) and `GenerateContentConfig.thinking_config` exist in 2.27.0.

The handle carries the outcome because `generate_content` is synchronous. Resume then never sends the same `(trial_id, attempt)` twice.

Settings archive (shared with 2.3): `{name: {"value", "documented"}}`, listing only the settings actually sent plus `prompt_format`; unset optional settings and an omitted seed do not appear. Upload retries and ACTIVE polling in `prepare` are preparation, not a Category outcome, so they do not conflict with 2.1's "no backoff inside an adapter" (which covers `submit`/`collect`). No `board.db` migration.

## Verification

**Commands:**
- `uv run pytest -q tests/test_prompt.py tests/test_gemini.py tests/test_open.py` -- expected: all pass, no network
- `uv run pytest -q && uv run ruff check src tests && uv run lint-imports` -- expected: clean
- `GEMINI_API_KEY=... uv run pytest -m live tests/test_live_gemini.py` -- expected: pass (manual, opt-in)
