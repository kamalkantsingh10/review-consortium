---
title: 'Story 1.2 — Study config and Instruments'
type: 'feature'
created: '2026-10-02'
status: 'done'
baseline_commit: '23bb18a7e09055a16b658e5ecfd3317ef400a311'
route: 'dispatch'
review_loop_iteration: 0
context:
  - '{project-root}/_doc/implementation-artifacts/epic-1-context.md'
  - '{project-root}/_doc/implementation-artifacts/epic-1-code-map.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Config has no schema, so mistakes surface mid-run, after spend; later stories need one typed config object.

**Approach:** Versioned Pydantic models loaded only via `config/load.py`; built-in Instruments as package YAML in the user format; JSON Schemas exported to `docs/schema/`; the 1.1 template validates unchanged.

**Decisions (accepted 2026-10-02):**
- Default quota levels: Claude drafts clearly marked placeholder levels in the `init` template (from `persona-card-wording-draft.md`); Kamal confirms them before the OLAF study. They are template defaults only, never code defaults.
- Presence item: ship one 7-point item marked `draft: true`, with a visible note, until Kamal names a published scale. A draft Instrument must be refused for `kind: main` Tests; Epic 1 only records the flag and the rule (the main gate arrives in Epic 4).

## Boundaries & Constraints

**Always:**
- `yaml.safe_load` into Pydantic models with `extra="forbid"`. Every file carries `schema_version: 1` (`SCHEMA_VERSION = 1`).
- Any failure raises `ConsortiumError("config_invalid", "<field>: <reason>", path=<file relative to study>)`. An unresolved Instrument name raises `unknown_instrument`.
- `study.yaml` fields (later stories rely on these names; defaults written explicitly in the template):
  - `seed` (int ≥ 0); `instruments` (enabled names)
  - `models[]` (`ModelConfig`): `id` (`m<n>`, unique), `provider` (`fake|gemini|qwen`), `model` (pinned; reject empty or ending in `latest`), `settings.temperature` (> 0, 0.7), `max_output_tokens` (> 0, required), `limits` (`MediaLimits`: `max_seconds`, `max_bytes`, `inline_base64` true)
  - `media` (`MediaProfile`): `height` 480, `video_kbps` 400, `audio_kbps` 64, `fps` 25
  - `session`: `practice_clips` 2 (Practice examples included per Trial per Instrument), `repeats` 3, `max_retries` 2, `pairing` `all_pairs` (a string; 1.5 rejects others with `bad_pairing`)
  - `concurrency` 4 (max in-flight calls per provider; used by 1.7)
  - `thresholds` (required, no code defaults; later epics add keys): `persona_fidelity_min` 0.8, `invalid_rate_max` 0.05, `leak_tolerance` (`duration_s` 1.0, `loudness_lufs` 2.0; resolution/fps exact)
  - `personas` frame: `big_five: all_32`, `nars_bands: [low, high]`, `quotas` (`age_band`, `gender`, `cultural_region`, `robot_experience`: non-empty unique level lists; the template's levels carry a `# PLACEHOLDER — Kamal to confirm before the OLAF study` comment)
- `TestConfig`: `test`, `kind` (`pilot|screening|main`), `instruments` (non-empty name list), `models` (optional ids that exist in `study.yaml`; default all), `clips` (target Clip IDs; existence checked in 1.5), `practice` (list of `PracticeExample`: `instrument`, `clips` (1 ID, or 2 for pairwise), `answer` `{item_id: value}`; default `[]`; at least `practice_clips` per Instrument, checked in 1.5), optional `session` overrides (same keys as `study.yaml` `session`). Prompt variants are not chosen here; 1.6 rotates them by Repeat.
- `InstrumentDef`: `name`, `version`, `draft` (bool, default false; a draft Instrument is not allowed in `kind: main` Tests, enforced by the Epic 4 main gate; `load_test` logs a stderr warning `draft_instrument: <name>` for each draft Instrument the Test lists), `instructions`, `prompt_variants` (name → text mapping, declared order kept, must contain `default`), `items[]` (`ItemDef`: `id`, `type` `likert|pairwise|free_text`, `text`; Likert `points` + `anchors` low/high; pairwise `options: [A, B]`). `response_schema()` returns the JSON Schema of a valid answer object keyed by item id.
- Lookup order for Instruments: `<study>/instruments/<name>.yaml` (user), then the package `instruments/` (built-in). A user file with a built-in's name raises `config_invalid` (no silent shadowing).

**Never:**
- Clip existence checks or media checks (1.5), cost formulas (1.9 extends `PricesConfig` additively), any new CLI command, network access.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Fresh Study | `init` output, no edits | `load_study`, `load_test(tests/example.yaml)`, `load_instruments` and `load_prices` all succeed | N/A |
| User Instrument | `instruments/trust.yaml`: Likert + free text + 2 Prompt variants | Loads; `response_schema()` accepts `{q1: 4, why: "..."}` | N/A |
| Unknown Instrument | Test `instruments: [foo]` | — | `unknown_instrument`, path `tests/x.yaml`, field `instruments.0` |
| Model without id | `models: [{provider: fake, ...}]` | — | `config_invalid`, `models.0.id: field required` |
| Unpinned Model | `model: gemini-latest` or missing | — | `config_invalid`, `models.0.model` |
| Missing thresholds | no `thresholds` block or key | — | `config_invalid`, `thresholds...: field required` |
| Unknown field / bad YAML / temperature 0 / duplicate or absent Model id | — | — | `config_invalid` with file and field |

</frozen-after-approval>

## Code Map

- `src/consortium/templates/study/`, `src/consortium/core/errors.py` -- from 1.1; template must now validate

## Tasks & Acceptance

**Execution:**
- [x] `src/consortium/config/models.py` -- the code-map models, `PricesConfig` as `models: {<model id>: ModelPrice}` with `ModelPrice(input_usd_per_mtok, output_usd_per_mtok)` decimal strings (1.9 adds media/text token fields), `SCHEMA_VERSION`, cross-field validators -- typed config.
- [x] `src/consortium/config/load.py` -- the four loaders; map `YAMLError`/`ValidationError` to `config_invalid` with dotted field path -- one error choke point.
- [x] `src/consortium/config/schema.py` -- `export_schemas(out_dir)` writes `{study,test,instrument,prices}.schema.json` (`"x-schema-version": 1`); `python -m` runnable; output committed to `docs/schema/`.
- [x] `src/consortium/instruments/{godspeed,pairwise_alive,presence}.yaml` -- Godspeed animacy (6) + likeability (5), 5-point; one pairwise item "Which one feels more alive?"; one 7-point presence item with `draft: true` and a top-of-file note `# DRAFT — replace with a published presence scale before any main Test` -- built-ins.
- [x] `src/consortium/templates/study/{study.yaml,tests/example.yaml,prices.yaml}` -- full schema: `m1` fake, `model: fake-1`, `max_output_tokens` 512, limits 600 s / 20000000 bytes -- FR1.
- [x] `docs/INTERFACE.md` -- document all fields, the user-Instrument folder, error format, the `draft` flag (presence is draft) and the placeholder quota levels.
- [x] `tests/test_config.py` -- every matrix row; committed schemas equal regenerated ones; each built-in `response_schema()` accepts a valid and rejects an out-of-range answer; presence loads with `draft` true and Godspeed with `draft` false.

**Acceptance Criteria:**
- Given the source tree, when grepping for `yaml.` outside `src/consortium/config/`, then nothing is found (asserted in `tests/test_config.py`).

## Implementation Notes

## Spec Change Log

## Review Triage Log

| # | Source | Finding | Verdict | Route |
|---|---|---|---|---|
| 1 | BH, EC | Unhashable YAML key gives a TypeError traceback | medium | patch |
| 2 | EC | Merge-key override rejected as a duplicate | medium | patch |
| 3 | EC | `.inf`/`.nan` accepted for limits and temperature | medium | patch |
| 4 | EC | `seed` unbounded | low | patch (direct constraint) |
| 5 | BH, EC | `pairing` accepts any string | medium | patch (Literal) |
| 6 | BH, EC | Practice instrument, clip count and answer unchecked | medium | patch |
| 7 | BH, EC | `test:` name not tied to the file name, so two files can collide | medium | patch |
| 8 | BH, EC | Prices not cross-checked against study Models | medium | patch |
| 9 | EC | Prices JSON Schema allows non-`m<n>` keys | low | patch (direct) |
| 10 | BH | Clip ID fields accept any string | low | patch (pattern) |
| 11 | BH | `unknown_instrument` has no path in `_load_instrument` | low | patch |
| 12 | VG | Price validation and ItemDef options/anchors rules untested | medium | patch (tests) |
| 13 | BH | JSON Schemas looser than the loader; pinning docs overpromise | low | patch (docs) |
| 14 | BH | Duplicate-key line reported twice | low | Rejected: cosmetic |
| 15 | BH, EC | Case-insensitive FS shadowing; `.yml` ignored | low | Rejected: Linux target; an ignored file surfaces as a loud `unknown_instrument` |
| 16 | BH | `load_study` doesn't parse Instrument files | low | Rejected: every consumer calls `load_instruments` |
| 17 | BH | Pairwise option labels not templated into prompts | low | Rejected: built-ins are consistent; a doc note can come with user Instruments |
| 18 | BH | Temperature upper bound; quota whitespace duplicates | low | Rejected: provider-specific / unlikely |
| 19 | EC | `load_test` outside `tests/` gives a misleading error | low | Rejected: CLI always passes study_dir |
| 20 | VG | Built-in Instrument files untracked | false | Committed with this story |

## Verification

**Commands:**
- `uv run pytest -q tests/test_config.py` -- expected: all pass
- `uv run python -m consortium.config.schema docs/schema && git diff --exit-code docs/schema` -- expected: no diff
- `uv run ruff check src tests && uv run lint-imports` -- expected: clean