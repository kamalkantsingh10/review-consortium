# Epic 3 Code Map: shared names for the Epic 3 specs

Epic 3 builds on Epics 1–2 as committed (`4db7aec`). The repo, `docs/INTERFACE.md` and the Epic 1 and 2 code maps describe what exists. Every Epic 3 spec uses the names below exactly. Changes must be additive, and nothing existing may be renamed.

## Existing surface to reuse (do not duplicate)

- **Engine (`engine/dispatch.py`):** the only path to a Model, with the ledger, ceiling, Archive, lease, single writer, retries, transient/refused/fatal handling, validation and resume.
- **Planning and rendering (`core/plan.py`, `core/render.py`, `core/prompt.compose`):** `TrialRequest` supports 0–2 Clips. A clip-less Trial is a valid request.
- **Instruments (`config/load.load_instruments`):** built-ins live in `src/consortium/instruments/*.yaml`. The response schema and the `draft` flag already exist.
- **Tests (`push test`):** these already accept `kind: screening`. Screening and pilot Clips can't be shared with main Tests (`clip_kind_overlap`).
- **Personas (`config/load.load_personas`, `panel/personas/index.json`, `meta.json`):** structured labels per Persona (Big Five poles, NARS band, quota attributes).
- **Panel guard (`board/trials.any_trials`, `panel_in_use`):** no Panel regeneration once Trials exist.
- **Read-only access:** `board.db.read_only(study_dir, fn, allow_older=False)`.
- **Migrations:** `board.db` migrations currently number 5. Epic 3 adds new ones in story order.

## New names (story in brackets)

| Module / name | Owns | Story |
|---|---|---|
| `instruments/fidelity_bfi10.yaml`, `instruments/fidelity_nars.yaml` | Built-in self-report Instruments for Persona-fidelity screening: short-form Big Five (2 items per trait, one reversed) and the NARS short form. Answered with no Clip. Marked as screening-only, so they can't be used in pilot or main Tests. | 3.1 |
| `core/fidelity.py` | `score_fidelity(answers, persona) -> FidelityScore(per_trait, matched, total, ratio)`: directional match per trait (high pole → mean above the scale midpoint, after reversing reversed items), plus the NARS band. Pure. | 3.1 |
| `board` migration 6, `board/screening.py` | Tables `screening_runs(run_id s<n>, kind fidelity\|perception, started_at, status, superseded_by)` and `screening_results(run_id, model_id, agent_id\|NULL, instrument, outcome pass\|fail, score, threshold, settings_hash, instrument_hash, source_study NULL, source_hash NULL)`. Helpers: `new_run`, `record_result`, `supersede`, `current_results`. Superseded runs are kept, never overwritten. | 3.1 (perception columns used by 3.2) |
| `stages/screen.py` | `screen_personas(study_dir, ...)` and `screen_models(study_dir, test, ...)`. Both create Trials and dispatch through `engine.dispatch` exactly like `open`: lease, confirmation with cost estimate, ceiling, Archive, resume. Screening Trials belong to a screening Test or run and never appear in main or pilot Exports. | 3.1, 3.2 |
| CLI | `consortium screen personas [--yes] [--ceiling] [--resume]` and `consortium screen models TEST [--yes] [--ceiling] [--resume]` | 3.1, 3.2 |
| `core/hashes.py` | `settings_hash(model_config)` (canonical JSON of the pinned Model settings and model id) and `instrument_hash(instrument_def)` (canonical JSON of the definition). These are the stamps used for staleness. | 3.1 |
| Screening Test shape (Test YAML, `kind: screening`) | Perception checks: Clip pairs with `expected` direction per construct/Instrument Item (e.g. `{instrument: pairwise_alive, clips: [A, B], expected: A}`), plus low-level checks with expected answers. Defined in the Test YAML; validated by `config.load.load_test`. | 3.2 |
| `core/perception.py` | `score_perception(trials, expectations) -> per (model, instrument) pass ratio`, and `coverage(test_instruments, screening_test) -> missing constructs`. Pure. | 3.2 |
| `core/eligibility.py` | `eligible(agents, instruments, results, stamps) -> Eligibility(agents_ok, excluded: [(agent_or_model, instrument, reason_code)])`. Reason codes are snake_case: `fidelity_fail`, `fidelity_missing`, `perception_fail`, `perception_missing`, `screening_stale`. Pure. | 3.3 |
| `stages/open.py` | Plans only eligible Agents. Refuses with `screening_coverage_missing` or `screening_stale` as the story specifies. Exclusions are written where Epic 4's Rater-flow report can read them (e.g. a `screening_exclusions` view/table or a sidecar). | 3.3 |
| `stages/panel_copy.py` + CLI `consortium panel copy --from SOURCE` | Copies `panel/personas/` (cards, `index.json`, `meta.json`) and imports the source's current screening results into the target `board.db`, with `source_study` and `source_hash`. Read-only on the source. Refuses `panel_exists` / `panel_in_use`. | 3.4 |

## Rules

- **Screening runs on the same machinery as a Run.** Do not build a second runner (AD-6).
- **Fake rater in screening:** offline tests need Agents that pass and Agents that fail. The Fake rater may read the Persona card text it is given and answer in a controlled way, so it can be "faithful" or "unfaithful" by configuration. Specs must design this deterministically.
- **Stamps:** a result counts only while its `settings_hash` and `instrument_hash` equal the current ones.
- **Blinding:** no screening code reads the Blinding key.

## Additions agreed after spec writing (2026-10-02, binding)

- **Layering (3.1).** `engine/run.py` holds `open`'s moved machinery (`Prepared`, `OpenSummary`, `parse_ceiling`, ceiling rules, `raters_for`, Test-context loader, run/resume/re-issue, writer dispatch). It imports core, board, archive, raters, `engine.dispatch` and `config.models` types only (as `engine/dispatch.py` already does), never `config.load`: stages pass a `ConfigReader` (frozen dataclass of `config.load` callables). New import-linter forbidden contract: `consortium.engine` must not import `consortium.config.load` or `yaml`.
- **`Prepared` hooks (3.1, used by 3.3).** `insert(conn)`, `reload()` (under-lease recheck → `test_changed`), `resume_check(conn)` (no-op default, called under the lease before a resume dispatches). The eligibility gate lives in `stages/open.py`, never in `engine`.
- **NARS (Kamal decision, 3.1).** Built-in `fidelity_nars` is structure only (`nars_1..nars_14`, subscales S1/S2/S3, S3 reversed), placeholder wording, `draft: true`. `study.yaml` `screening.nars_instrument` (default `fidelity_nars`) selects a user Instrument from `<study>/instruments/`. Template: `src/consortium/templates/nars_instrument.yaml`. `InstrumentDef` gains `self_report`, `keys: {item: ItemKey(construct, reversed, subscale)}`, `citation`. `config.load.load_fidelity_instruments(study, cfg)` returns BFI-10 plus the selected NARS (BFI only with no bands).
- **Zero NARS bands (Kamal decision, 3.1).** `personas.nars_bands: []` is allowed (absent still means `[low, high]`). Panel = profiles × replicates × max(1, bands); `Persona.nars` may be `None`; cards are 6 lines; fidelity uses 5 checks. Panels with ≥ 1 band are byte-identical.
- **`StudyConfig.screening` (3.1).** `ScreeningConfig(fidelity_repeats = 1, nars_instrument = "fidelity_nars")`. `FakeSettings.fidelity` / `FakeSettings.perception`: `random|faithful|unfaithful`. `Thresholds.perception_min = 0.8` (3.2).
- **Migration 6 `m6_screening` (3.1, the only one creating screening tables).** `screening_runs(run_id, kind fidelity|perception, screening_test NULL, started_at, status open|complete, superseded_by NULL)`; `screening_results(run_id, model_id, agent_id NULL, instrument, outcome, score, threshold, settings_hash, instrument_hash, detail, pair_checks NULL, source_study NULL, source_hash NULL)`. Fidelity rows use `instrument = 'fidelity'`.
- **Migration 7 `m7_screening_gate` (3.3).** `screening_exclusions(test, agent_id, persona_id, model_id, instrument NULL, reason)`, `screening_stamps(test, model_id, instrument, settings_hash, instrument_hash)`. 3.2 and 3.4 add no migration.
- **`board/screening.py` helpers.** 3.1: `next_run_id`, `new_run`, `open_run(kind, screening_test)`, `record_result`, `complete_run`, `current_results` (complete, non-superseded runs; highest run wins per key), `any_runs`. 3.3: `record_gate`, `test_stamps`. 3.4: `current_runs`, `any_results`, `import_runs`.
- **Coverage (3.2 = 3.3).** An Instrument is covered when a current perception result for it has `pair_checks > 0`. `core.perception.coverage(needed, covered)` is the single function; `screen models` reports gaps (stderr `coverage_gap`), `open` refuses (`screening_coverage_missing`).
- **Neutral Persona (3.2).** `core.perception.NEUTRAL_CARD`, `NEUTRAL_PERSONA_ID = "p0"`; Agents `p0-m<n>`; perception rows have `agent_id` NULL; `status` tallies skip `p0`.
- **Gating (Kamal decision, 3.3).** Main always gated; pilot ungated with stderr `unscreened_pilot` until any `screening_runs` row exists, then gated; screening never gated (`open` refuses screening Tests with `screening_test_not_openable`).
- **Panel files (3.4).** `config/panel_files.py`: `INDEX_FILE`, `META_FILE`, `write_file`, `fsync_dir`, `exists`, `sweep_stale`, `move_into_place` (stdlib + core only). `personas generate` refuses `panel_in_use` on `any_panel_trials` (p0 excluded) or any fidelity result; `panel copy` checks `any_trials` or `any_results`.
- **CLI.** `consortium screen personas [--yes] [--ceiling USD] [--resume] [--study PATH]`; `consortium screen models TEST [--yes] [--ceiling USD] [--resume] [--study PATH]`; `consortium panel copy --from SOURCE [--study PATH]`. Test names `^s[0-9]+$` are reserved.
- **Codes.** Errors: `screening_run_open`, `screening_not_open`, `instrument_not_allowed`, `screening_not_exportable`, `not_a_screening_test`, `no_screening_checks`, `screening_test_not_openable`, `screening_coverage_missing`, `screening_stale`, `no_eligible_agents`, `panel_frame_mismatch` (plus existing `bad_test_name`, `panel_in_use`, `panel_exists`, `panel_mismatch`, `config_invalid`). Warnings (stderr): `draft_instrument`, `coverage_gap`, `unscreened_pilot`, `no_screening_results`. Exclusion reasons: `fidelity_fail`, `fidelity_missing`, `perception_fail`, `perception_missing`, `screening_stale`.
