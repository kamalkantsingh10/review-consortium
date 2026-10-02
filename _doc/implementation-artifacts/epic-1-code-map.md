# Epic 1 Code Map: shared names for all Epic 1 specs

These are the planned modules, functions and commands. Every Epic 1 spec uses exactly these names, so stories written in parallel fit together. A story may add private helpers, but it must not rename or duplicate anything listed here. The package root is `src/consortium/` (Story 1.1).

| Module | Owns | Introduced by |
|---|---|---|
| `core/errors.py` | `ConsortiumError(code, message, path=None)` | 1.1 |
| `stages/init.py`, `templates/study/` | `init_study(path)` | 1.1 |
| `config/models.py` | Pydantic models: `StudyConfig`, `ModelConfig` (keyed by `id`), `TestConfig`, `PracticeExample`, `InstrumentDef` (incl. `draft: bool`, default false), `ItemDef`, `CardWording` (1.3), `MediaProfile`, `MediaLimits`, `PricesConfig`/`ModelPrice`. Thresholds and defaults live in `StudyConfig`. Field names (1.2): `seed`, `instruments`, `models[]` (`id`, `provider`, `model`, `settings.temperature`, `max_output_tokens`, `limits.{max_seconds,max_bytes,inline_base64}`, `fake.*` 1.9/1.10), `media`, `session.{practice_clips,repeats,max_retries,pairing}` (`practice_clips` = Practice examples included per Trial per Instrument), `concurrency`, `thresholds.{persona_fidelity_min,invalid_rate_max,leak_tolerance}`, `personas`. `TestConfig`: `test`, `kind`, `instruments`, `models`, `clips`, `practice[]` (`instrument`, `clips`, `answer`; at least `practice_clips` per Instrument used, the first `practice_clips` in list order are used), `session` overrides. | 1.2 (extended by later stories, additively only) |
| `config/load.py` | `load_study(study_dir) -> StudyConfig`, `load_test(path) -> TestConfig`, `load_instruments(study_dir, cfg) -> dict[str, InstrumentDef]`, `load_prices(study_dir) -> PricesConfig`, `load_personas(study_dir) -> list[Persona]` (reads `panel/personas/index.json`; `panel_missing`, added in 1.3), `load_card_wording() -> CardWording` (reads the package `templates/persona_card/wording.yaml`, added in 1.3). This is the only module that reads YAML. | 1.2 |
| `config/schema.py` | `export_schemas(out_dir)`, writes `docs/schema/*.schema.json` | 1.2 |
| `instruments/*.yaml` | `godspeed.yaml`, `pairwise_alive.yaml`, `presence.yaml` | 1.2 |
| `core/seeds.py` | `derive_seed(study_seed, purpose, key) -> int` (SHA-256, first 8 hex digits, masked to 31 bits) | 1.3 |
| `core/personas.py` | `generate_personas(cfg) -> list[Persona]`, `render_card(persona, wording) -> str` (behaviour-only text the Model sees; no trait labels) | 1.3 |
| `templates/persona_card/wording.yaml` | Kamal-approved card wording: one sentence per Big Five pole and NARS band, a demographic line template | 1.3 |
| `stages/personas.py` | Writes `panel/personas/p<n>.md` (card text) and `panel/personas/index.json` (structured labels) | 1.3 |
| `core/ids.py` | `new_clip_id()` (`c_` + 8 base32 characters), `session_id(test, agent, repeat)` | 1.4 |
| `board/db.py` | `connect(study_dir, readonly=False)` (WAL; `readonly=True` opens `mode=ro`, never migrates, returns `None` if absent, raises `board_version_mismatch`; kwarg added in 1.6), `MIGRATIONS: list[callable]` applied in order, tracked with `PRAGMA user_version`. **Each story appends its own migration; never edit an earlier one.** All SQL lives in `board/`. | 1.4 |
| `board/clips.py` | `insert_clip`, `list_clips` (table `clips`, m1) | 1.4 |
| `board/tests.py` | `get_test`, `register_test`, `target_clip_kinds` (tables `tests`, `test_clips`, m2; `tests.paused_reason` added in 1.9) | 1.5 |
| `board/trials.py` | Trial/attempt SQL helpers (tables `trials`, `attempts`, m3); `chosen_answer`, `invalid_rates` (1.10) | 1.7 |
| `board/queries.py` | `status_counts`, `cost_footer` (1.11), `export_trials` (1.12) | 1.11 |
| `core/media_limits.py` | `check_media(trial_shapes, clips, models)` | 1.5 |
| `board/blinding.py` | `append_conditions(study_dir, clip_id, conditions)`, `read_key(study_dir) -> dict` | 1.4 |
| `media/canonicalize.py` | `canonicalize(src, dst, profile) -> ClipInfo`, `probe(path) -> ClipInfo` (duration, size, fps, resolution, loudness) | 1.4 |
| `stages/push.py` | `push_clip(...)`, `push_test(...)`, `write_leak_report(...)` | 1.4 (clip), 1.5 (test) |
| `core/plan.py` | `plan_test(test, cfg, personas, instruments) -> Plan` (Sessions and Trials). `trial_id = <session_id>/t<trial_index>`; `pair_id = <instrument>:<clip_lo>:<clip_hi>`; `position` 1 (lo first) or 2; `prompt_variant` = variant name | 1.6 |
| `core/render.py` | `TrialRequest` (frozen dataclass), `render(trial, ...) -> TrialRequest`, `canonical_json(obj) -> bytes` | 1.6 |
| `stages/open.py` | `open_test(study_dir, test, *, dry_run, yes, ceiling, resume)` | 1.6 (dry-run), 1.7 (run), 1.8 (resume), 1.9 (cost) |
| `raters/base.py` | `Rater` Protocol: `prepare(clip) -> MediaRef`, `async submit(calls: list[RaterCall]) -> list[Handle]`, `async collect(handles) -> list[RaterResult]`. `RaterCall(trial_id, attempt, seed, request, media)`; `Handle` JSON-serialisable; `RaterResult(raw, usage, model_build, category)`, `usage = {input_tokens, output_tokens}`. | 1.7 |
| `raters/fake.py` | `FakeRater`: deterministic from seed, no network, configurable invalid rate | 1.7 (invalid rate in 1.10) |
| `archive/jsonl.py` | `append_request(...)`, `append_response(...)`: append-only, keyed by `(trial_id, attempt)` (1.7); `read_requests`, `read_responses`: last line per key wins (1.8) | 1.7 |
| `board/writer.py` | The single writer task: an asyncio queue of write ops, including every Archive append. `begin_attempt` increments `attempt` (and, from 1.9, reserves in the ledger in the same transaction) | 1.7 |
| `board/lease.py` | `acquire_lease(study_dir)`, a context manager for the exclusive `board.lock` that raises `study_busy` | 1.7 |
| `engine/dispatch.py` | `async dispatch(study_dir, trials, rater_by_model, ...)`: attempts, Archive before state, semaphore | 1.7 (resume 1.8, ceiling 1.9, retries 1.10) |
| `core/cost.py` | `estimate(request, model, prices, clip_seconds) -> Decimal`, `estimate_plan(plan, ...)`, `actual(usage, model, prices)`. Tables `ledger`, `ceiling_changes` (m4) | 1.9 |
| `core/validate.py` | `validate_response(raw, instrument) -> ParsedAnswer`, raising `invalid_response`; `invalid_rate(counts)`, the single invalid-rate definition: invalid ÷ (valid + invalid), `None` when 0; refused and failed are reported separately. Attempt columns `valid`, `invalid_reason`, `answer_json` (m5) | 1.10 |
| `stages/status.py` | `status(study_dir, test=None) -> rows` (read-only, no lease) | 1.11 |
| `stages/export.py` | `export_test(study_dir, test) -> Path`. `EXPORT_SCHEMA_VERSION = 1` | 1.12 |

**CLI** (`cli.py`, thin): `init`, `personas generate`, `push clip`, `push test`, `open [--dry-run] [--yes] [--ceiling] [--resume]`, `status`, `export`. Every command except `init` takes `--study PATH` (default: the current directory).

**Shared error codes** (one meaning each): `unknown_test` (Test not registered), `unknown_clip`, `unknown_instrument`, `panel_missing`, `not_confirmed` (declined), `confirmation_required` (no TTY, no `--yes`), `board_version_mismatch`, `bad_pairing`, `bad_practice`, `test_already_open` (1.7), `test_not_open` (1.8), `ceiling_required` (1.9).

**Trial states:** `planned`, `sent`, `valid`, `invalid`, `refused`, `failed`. The last four are terminal.

**Tests** live in `tests/`, one file per story (`tests/test_<module>.py`). The Fake rater is always used; no tests touch the network.
