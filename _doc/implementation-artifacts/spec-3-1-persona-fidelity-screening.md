---
title: 'Story 3.1 — Persona-fidelity screening'
type: 'feature'
created: '2026-10-02'
status: 'done'
baseline_commit: '9092207899e54bdc0821a36e7fc2f021bf3dc566'
route: 'dispatch'
review_loop_iteration: 0
context:
  - '{project-root}/_doc/implementation-artifacts/epic-3-context.md'
  - '{project-root}/_doc/implementation-artifacts/epic-3-code-map.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Nothing checks that an Agent follows its Persona, so an Agent that ignores its card would still rate.

**Approach:** `consortium screen personas` creates a screening run `s<n>`. Each Agent answers a clip-less BFI-10 and (when the frame has NARS bands) a NARS Instrument through the same engine path as `open`. `core.fidelity` scores directional match per trait plus the NARS band, and `board.screening` stores stamped pass/fail results. Superseded runs are kept.

## Boundaries & Constraints

**Decisions (Kamal, 2026-10-02):**
- **NARS text is not redistributed.** The package bundles only the NARS *structure* (`fidelity_nars`: subscales, scoring keys, reversed flags) with neutral placeholder wording, marked `draft: true`. Kamal supplies the real items (Nomura et al., 2006, *Interaction Studies* 7(3), with citation) as a user Instrument in his Study's `instruments/` folder and selects it with `study.yaml` `screening.nars_instrument: <name>` (default `fidelity_nars`, the placeholder). The selected Instrument must meet the same structure/keys contract (validated). A ready-to-fill template `src/consortium/templates/nars_instrument.yaml` (item ids, subscale, reversed flag, empty text, `citation`) ships, and `docs/INTERFACE.md` documents the 3 steps. BFI-10 (GESIS open access, non-commercial research; cite Rammstedt & John, 2007) is bundled as the built-in Big Five fidelity Instrument; TIPI is the noted fallback.
- **No NARS bands → no NARS check.** When `personas.nars_bands` is `[]`, the NARS Instrument is not planned and the score uses only the five traits. The frame allows zero bands (it does not today, so this story adds it): Panel = profiles × replicates × max(1, bands), consistent with Story 2.4's generator, and every Panel with ≥ 1 band stays byte-identical.

**Always:**
- **One runner (AD-6).** Move `open`'s machinery out of `stages/open.py` into `engine/run.py`, unchanged in behaviour. This covers `OpenSummary`, `parse_ceiling`, the ceiling rules, `raters_for`, the Test-context loader, run/resume/re-issue checks and the writer dispatch. It is parametrised by a `Prepared` (run/Test name, kind, Trials, `render`, `estimate`, `budget`, `caps`, `insert(conn)`, `reload()`). `open.py` keeps its public names by re-exporting them. `screen` uses the same functions, so it gets the same estimate, confirmation, ceiling, lease, under-lease recheck (`test_changed`), Archive, retries and resume.
- **A run is a Test row.** Run IDs are `s<n>`, with n = 1 + the highest existing run number. The ID is chosen before confirmation and checked again under the lease. One writer transaction (`board.screening.new_run`) inserts the `screening_runs` row, a `tests` row (name `s<n>`, kind `screening`, openable 0, path `<built-in>/screening/personas`, sha256 = instrument hash) and the Trials. Trials have `test = s<n>`, so the existing ledger, status, pause and resume work unchanged.
- **Plan.** Use `plan_test` with every Persona × every Model, Instruments `[fidelity_bfi10, <screening.nars_instrument>]` (BFI only when the frame has no NARS bands), and repeats = `study.yaml` `screening.fidelity_repeats` (default 1). A `self_report` Instrument plans one Trial per Session with `clip_ids = ()` and no Practice. Each request carries the Persona card exactly as stored.
- **Scoring (pure).** `score_fidelity(answers, persona, keys)`:
  - A reversed item scores as `points + 1 − x`.
  - A construct's score is the mean of its valid answers over all repeats (NARS: over all its items; per-subscale means go in `detail` only).
  - A construct matches when its score is above `(points+1)/2` and the pole is `high`, or below it and the pole is `low`. A score at the midpoint, or no valid answer, does not match.
  - Checks are the five traits plus the NARS band when the Persona has one (6, or 5 with no bands). `ratio = matched / checks`, and the Agent passes when `ratio ≥ thresholds.persona_fidelity_min`.
- **Results.** Scoring runs only once every Trial of the run is terminal. One writer operation then:
  - stores one row per Agent (`instrument = 'fidelity'`, score = ratio, `detail` = JSON of per-construct score, pole and match);
  - stamps each row with `settings_hash` and `instrument_hash` (over the fidelity definitions actually planned);
  - sets the run to `complete`;
  - marks every earlier complete fidelity run `superseded_by = s<n>`.
  Nothing is deleted.
- **Stamps (`core.hashes`).** SHA-256 over canonical JSON. `settings_hash` covers `{id, provider, model, settings without api_key_env, max_output_tokens, fake}`. `instrument_hash` takes a definition, or a list of definitions in name order.
- **Fake fidelity.** `FakeSettings.fidelity` is `random` (default, today's answers byte-identical), `faithful` or `unfaithful`. `raters_for` gives the FakeRater a map from card sentence to `(construct, pole)`, built from `load_card_wording()`, and the item keys of the fidelity Instruments. For a keyed Likert item whose construct appears on the card, the faithful rater answers `points` when the pole is high XOR the item is reversed, and `1` otherwise. The unfaithful rater gives the opposite. All other items are answered at random, as today.

**Never:**
- A second runner, or copying `open` code into `screen`.
- Reading the Blinding key.
- Deleting or overwriting results.
- Rendering `keys` to a Model.
- Shipping the published NARS item wording in the repo.
- Network access in tests.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Pass/fail split | m1 `faithful`, m2 `unfaithful`, `--yes` | `s1` complete. Every m1 Agent passes (1.0) and every m2 Agent fails (0.0). Rows are stamped. | N/A |
| Second run | `s1` complete, run again | `s2` complete. `s1` is `superseded_by s2`, and its rows are kept. | N/A |
| Run already open | `s1` paused or interrupted | Refused, nothing written | `screening_run_open` (hint `--resume`) |
| Ceiling pause | Ceiling hit mid-run | Run stays `open` with no results. `--resume` finishes it and scores. | N/A |
| Nothing to resume | `--resume`, no open run | Refused | `screening_not_open` |
| Refused Trial | The Agent's BFI Trial is `refused` | Its traits do not match, so the Agent fails | N/A |
| No Panel | No `index.json` | Refused before anything is planned | `panel_missing` |
| Misuse | A Test lists `fidelity_bfi10`; `push test` names `s3`; `export s1` | Refused | `instrument_not_allowed` / `bad_test_name` / `screening_not_exportable` |
| No NARS bands | `personas.nars_bands: []` | Panel of profiles × replicates, 6-line cards, `nars: null`; only BFI Trials; ratio over 5 | N/A |
| User NARS | `screening.nars_instrument: nars_s1` (filled template) | NARS Trials use `nars_s1`; its hash is in the stamp | N/A |
| Bad NARS | selected Instrument not `self_report`, an Item unkeyed, a key not `nars`, a subscale absent | Refused before planning | `config_invalid` (`screening.nars_instrument`) / `unknown_instrument` |
| Placeholder NARS | default `fidelity_nars` | Runs; stderr `draft_instrument: fidelity_nars` | N/A |

</frozen-after-approval>

## Code Map

- **Architect note (overrides the NARS-instrument check wherever it says all three subscales are required):** a selected NARS Instrument must contain at least one known subscale (S1, S2, S3), with every item keyed to a subscale and a reversed flag. S1-only short forms are valid. Scoring uses the subscales that are present. Absent `personas.nars_bands` still means `[low, high]`; only an explicit `[]` turns NARS off.
- `stages/open.py` -- the source of the machinery to move. Stages may not import each other, so shared code goes to `engine/run.py`.
- `engine/dispatch.py` -- already imports `config.models` (types); `engine/run.py` follows that and never imports `config.load`.
- `core/plan.py` -- `canonical_trials` (gains the clip-less shape). `core/render.py` and `core/prompt.py` already handle 0 Clips.
- `config/models.py` -- `InstrumentDef`, `FakeSettings`, `Thresholds.persona_fidelity_min` (exists), `StudyConfig`, `PersonaFrame.nars_bands` (`min_length=1` today).
- `config/load.py` -- `_load_instrument`, `load_test`, `load_card_wording`, the wording/band check. `templates/persona_card/wording.yaml` -- the card sentences.
- `core/personas.py` -- the generator (`bands = list(frame.nars_bands)`), `Persona.nars`, `render_card` (NARS line), attribute tallies.
- `board/db.py` (`MIGRATIONS` = 5), `board/trials.py` (`insert_plan`, `any_trials`), `board/tests.py` (`insert_test`).
- `stages/push.py` (`_check_name`), `stages/export.py` (`_read_board`), `pyproject.toml` (import-linter contracts).

## Tasks & Acceptance

**Execution:**
- [x] `src/consortium/engine/run.py`, `stages/open.py` -- Move the machinery and parametrise it by `Prepared`. `open.py` becomes a thin stage and re-exports its existing names. Layering: `engine/run.py` imports core, board, archive, raters, `engine.dispatch` and `config.models` types only. Every `config.load` call (study, prices, Test file, Instruments, Personas, cards, card wording) reaches it through a `ConfigReader` (a frozen dataclass of those callables) that the stage builds from `config.load` and passes in; `raters_for` takes the Fake cue map as an argument. `Prepared` also carries `resume_check(conn)` (no-op by default; called under the lease before a resume dispatches), which 3.3 uses.
- [x] `pyproject.toml` -- Add a forbidden contract: `consortium.engine` must not import `consortium.config.load` or `yaml`.
- [x] `src/consortium/config/models.py` -- Add these fields:
  - `InstrumentDef.self_report: bool = False`, `InstrumentDef.citation: str | None = None`.
  - `InstrumentDef.keys: dict[item_id, ItemKey(construct, reversed, subscale: s1|s2|s3 | None)] | None`. Each key must name a Likert Item of the Instrument. A `self_report` Instrument must have a key for every Item, and each construct must be one of `TRAITS ∪ {nars}`; `subscale` is required for `nars` keys and forbidden otherwise.
  - `FakeSettings.fidelity`.
  - `StudyConfig.screening: ScreeningConfig(fidelity_repeats ≥ 1 = 1, nars_instrument: InstrumentName = "fidelity_nars")`.
  - `PersonaFrame.nars_bands`: allow `[]` (an absent key keeps today's default `[low, high]`).
- [x] `src/consortium/config/load.py` -- Add `load_fidelity_instruments(study, cfg) -> list[InstrumentDef]` (BFI-10, plus the selected NARS Instrument when the frame has bands; resolved like any Instrument, user folder first, not gated by `study.instruments`). The NARS one must be `self_report`, every key `nars`, each of `s1`, `s2`, `s3` keyed at least once (else `config_invalid`, field `screening.nars_instrument`); `draft: true` logs `draft_instrument: <name>`. `load_test` refuses a `self_report` Instrument with `instrument_not_allowed`. Skip the NARS-sentence check when there are no bands.
- [x] `src/consortium/instruments/fidelity_bfi10.yaml`, `fidelity_nars.yaml` -- Self-report Instruments, 5-point. BFI-10 Items `bfi_1..bfi_10` with the stem "I see myself as someone who…", keyed as in Rammstedt & John (2007): E 1R/6, A 2/7R, C 3R/8, N 4R/9, O 5R/10, with `citation`. `fidelity_nars`: `draft: true`, Items `nars_1..nars_14` keyed `nars` with the Nomura et al. (2006) subscale mapping (S1: 4, 7, 8, 9, 10, 12; S2: 1, 2, 11, 13, 14; S3: 3, 5, 6, reversed), neutral placeholder text (not the published items).
- [x] `src/consortium/templates/nars_instrument.yaml` -- Same ids, subscales and reversed flags as `fidelity_nars`, empty `text` placeholders, a `citation` field, `name` to set, `draft: false`. A test asserts its structure equals `fidelity_nars`'s.
- [x] `src/consortium/core/personas.py` -- Zero bands: generate over one band-less block (`nars = None`), Persona order profile then replicate; `render_card` omits the NARS line (6 lines); tallies treat `None` as an empty value. Non-empty bands: unchanged bytes.
- [x] `src/consortium/core/plan.py`, `core/hashes.py`, `core/fidelity.py` -- The clip-less shape, the stamps, and `score_fidelity` returning `FidelityScore(per_trait, matched, total, ratio)`.
- [x] `src/consortium/raters/fake.py` -- The fidelity modes (see Always).
- [x] `src/consortium/board/db.py`, `board/screening.py` -- Migration 6 (`m6_screening`) adds:
  - `screening_runs(run_id, kind fidelity|perception, screening_test NULL, started_at, status open|complete, superseded_by NULL)`;
  - `screening_results(run_id, model_id, agent_id NULL, instrument, outcome pass|fail, score, threshold, settings_hash, instrument_hash, detail, pair_checks NULL, source_study NULL, source_hash NULL)` — `pair_checks` is filled by 3.2, the source columns by 3.4.
  Helpers: `next_run_id`, `new_run` (run, tests row and Trials in one transaction), `open_run(kind, screening_test)`, `record_result`, `complete_run` (results, status and supersession in one transaction), `current_results` (rows of complete, non-superseded runs; on a duplicate key the highest run number wins), `any_runs`.
- [x] `src/consortium/stages/screen.py`, `cli.py` -- `screen_personas(study, yes, ceiling, resume, confirm, announce)` and `consortium screen personas [--yes] [--ceiling USD] [--resume] [--study PATH]`. Print the run summary and the pass/fail counts per Model.
- [x] `src/consortium/stages/push.py`, `stages/export.py` -- Reserve `^s[0-9]+$` (`bad_test_name`). Export refuses a `screening` Test with `screening_not_exportable`.
- [x] `docs/INTERFACE.md`, `templates/study/study.yaml` -- Document the command, `screening.fidelity_repeats`, `screening.nars_instrument`, `fake.fidelity`, `nars_bands: []` (6-line cards, `nars: null`, empty `persona_nars`), the new codes, and the 3 NARS steps: (1) copy `templates/nars_instrument.yaml` to `<study>/instruments/<name>.yaml` and set `name`; (2) fill each Item's `text` and the `citation` from Nomura et al. (2006); (3) set `screening.nars_instrument: <name>`.
- [x] `tests/test_screen_personas.py`, `test_fidelity.py`, `test_hashes.py`, `test_personas.py` -- Every matrix row. Scoring edge cases: midpoint, reversal, partial answers, 5-check ratio. Zero-band Panel; full-grid Panel bytes unchanged.

**Acceptance Criteria:**
- Given a screening run, then every Archive request has no Clip and carries the Persona card, and a concurrent `open` or `screen` refuses with `study_busy`.
- Given any screening Trials, when `personas generate` runs, then it refuses with `panel_in_use`.
- Given the move to `engine/run.py`, then every existing `open`, resume and cost test passes unmodified and `lint-imports` is clean.
- Given the same seed and Fake settings, when screening is repeated in a fresh Study, then the scores and outcomes are identical.

## Implementation Notes

## Spec Change Log

## Review Triage Log

| # | Source | Finding | Verdict | Route |
|---|---|---|---|---|
| 1 | BH | settings_hash computed at scoring time, so a resume after a settings change mis-stamps | high | patch (record at run creation; refuse resume on change) |
| 2 | BH, EC, VG | Stuck open run can never be abandoned; all-terminal unscored path untested | high | patch (--abandon + tests) |
| 3 | BH | Real Models screened against placeholder NARS bias every Agent to fail; not recorded | high | patch (refuse non-fake + draft) |
| 4 | BH | Placeholder item text leaks subscale keys to the Model | medium | patch |
| 5 | BH, EC | Panel/frame NARS mismatch and mixed index.json unchecked | medium | patch |
| 6 | EC | Duplicate item ids across fidelity Instruments overwrite keys | medium | patch |
| 7 | BH | A partial re-run supersedes all earlier results | medium | patch (per-key current results, UNIQUE, ms timestamps) |
| 8 | BH | Stamps ignore the prompt composer and card wording | medium | patch |
| 9 | BH | Pass/fail on almost no valid data | medium | patch (insufficient_data) |
| 10 | BH | No --dry-run for screening; no results view | low | patch (--dry-run); results view deferred to Epic 4 rater-flow |
| 11 | BH, VG | open s<n> gives a misleading refusal | low | patch |
| 12 | EC | Completed-but-paused run exits 1 asking to resume | low | patch |
| 13 | EC | Missing tests row skips the sha check | low | patch |
| 14 | BH | ItemKey duplicates trait names; getattr default | low | patch |
| 15 | BH, VG | INTERFACE stale rows and missing tags | low | patch (docs) |
| 16 | VG | Boundary, resume test_changed, finish-only, ItemKey validators untested | medium | patch (tests) |
| 17 | EC | NARS check allows missing subscales | false | Architect note in the Code Map: S1-only short forms are valid by design |

## Design Notes

- With the 0.8 default: 6 checks need 5 matches; 5 checks need 4.
- `screen` creates Trials, so the existing `panel_in_use` guard freezes the Panel the results describe. This is why the Panel needs no stamp of its own.
- The NARS subscale mapping in `fidelity_nars` follows the common citation of Nomura et al. (2006); Kamal checks it against the paper when filling the template. Changing `screening.nars_instrument` changes the fidelity `instrument_hash`, so earlier fidelity results go stale (3.3).
- Layering: the enforced import-linter layers already allow `engine → config`, and `engine/dispatch.py` imports `config.models`. The AD-1 prose lists only core, board, archive and raters, so `engine/run.py` takes config *types* only and receives all loading through `ConfigReader`.

## Verification

**Commands:**
- `uv run pytest -q tests/test_screen_personas.py tests/test_fidelity.py tests/test_hashes.py tests/test_personas.py` -- expected: all pass
- `uv run pytest -q && uv run ruff check src tests && uv run lint-imports` -- expected: clean
