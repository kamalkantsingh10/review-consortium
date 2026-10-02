---
title: 'Story 3.2 — Perception screening'
type: 'feature'
created: '2026-10-02'
status: 'ready-for-dev'
route: 'dispatch'
review_loop_iteration: 0
context:
  - '{project-root}/_doc/implementation-artifacts/epic-3-context.md'
  - '{project-root}/_doc/implementation-artifacts/epic-3-code-map.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Nothing checks that a Model perceives what an Instrument measures. A Model that inverts "aliveness" would still rate it.

**Approach:** A `kind: screening` Test declares `checks` with known answers. `consortium screen models TEST` runs only the Trials those checks need, on every Model of the Test, with a neutral Persona, through `engine/run.py` (from 3.1). It then scores a pass ratio per Model × Instrument against `thresholds.perception_min` and reports the constructs that main or pilot Tests use but no check covers.

## Boundaries & Constraints

**Always:**
- **Test shape.** `checks` is allowed only when `kind: screening`. Each entry is `{instrument, item, clips: [1–2 Clip IDs], expected}`. `load_test` validates every check:
  - The Instrument is one of the Test's, and the Item is one of its Items.
  - The check's Clips are among the Test's `clips`.
  - A pairwise Instrument, or a single-Clip Instrument given 2 Clips, needs `expected` to be one of the check's Clips. These are the *pair* checks.
  - A single Clip needs `expected` to be a valid answer to the Item. This is a *low-level* check.
  - Otherwise the check is refused with `config_invalid`.
  `screen models` refuses a Test that has no checks (`no_screening_checks`) or is not `kind: screening` (`not_a_screening_test`).
- **Plan.** `plan_test` gains an optional `shapes` argument. `core.perception.check_shapes(checks, instruments)` builds the shapes, de-duplicated in check order:
  - pairwise: both positions of the pair;
  - single-Clip: one Trial per Clip of the check.
  There is one neutral Persona, `p0`, whose card is `core.perception.NEUTRAL_CARD`: "You are an adult taking part in a study about robots. Answer every question carefully and honestly." Agents are `p0-m<n>` for the Test's Models. Repeats and Practice come from the Test's effective session.
- **Run.** The run is `s<n>`, kind `perception`, with `screening_test = TEST`. Its `tests` row copies the screening Test's path, sha256 and `test_clips`, with openable 0. The run/resume/lease/ceiling/confirmation flow is the 3.1 `Prepared` path, and the under-lease recheck also covers the screening Test file. Only one open run per screening Test is allowed (`screening_run_open`), and `--resume` continues it.
- **Scoring (pure).** `score_perception(trials, checks) -> {(model, instrument): (passed, units, ratio)}`. Units are counted per repeat:
  - pairwise check: each of the 2 position Trials is one unit; it passes when the chosen option's Clip is `expected`;
  - two-Clip single-Clip check: one unit, which passes when `expected`'s answer is strictly greater than the other Clip's;
  - low-level check: one unit, which passes when the answer equals `expected`.
  A non-`valid` Trial fails its units. The result is a pass when `ratio ≥ thresholds.perception_min`, which defaults to 0.8 and is written in the template.
- **Results.** One row per Model × Instrument with checks (`agent_id` NULL), stamped with `settings_hash` and `instrument_hash(def)`, with `pair_checks` = the number of *pair* checks for that Instrument in the screening Test (0 for low-level only). Completing the run supersedes earlier complete perception runs of the same screening Test. Nothing is deleted.
- **Coverage (one rule, shared with 3.3).** An Instrument is *covered* when a current perception result for it has `pair_checks > 0`. `coverage(needed, covered) -> {instrument: tests}` returns every Instrument of a registered `main` or `pilot` Test (ignoring `self_report` Instruments) not in `covered`. `screen models` passes `covered` = this Test's pair-checked Instruments ∪ those covered by current runs of other screening Tests, and writes `coverage_gap: <instrument> (tests: a, b)` to stderr before confirmation and lists the gaps in the stdout summary. A gap is reported, never refused. 3.3 refuses, using the same function over current results.
- **Fake perception.** `FakeSettings.perception` is `random` (default, unchanged), `faithful` or `unfaithful`. The latent is `fake_latent(clip_sha256, item_id) ∈ [0,1)`, drawn from `derive_seed(0, "fake_latent", f"{sha}:{item}")`. A faithful rater answers a Likert Item with `1 + floor(latent·points)` and a pairwise Item with the option of the Clip that has the higher latent. An unfaithful rater uses `1 − latent`. Tests derive their `expected` values from the same function.
- **Built-in low-level Instrument.** `perception_cues` has the Items `moving` ("Is the robot moving?") and `speech` ("Is there speech?"). Each is a 2-point Likert Item with anchors No and Yes, so `expected: 2` means yes.

**Never:**
- Using Panel Personas for perception.
- Opening a screening Test with `open`, which now refuses `kind: screening` with `screening_test_not_openable`.
- Refusing on a coverage gap.
- Reading the Blinding key.
- Network access in tests.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Pass/fail split | m1 `faithful`, m2 `unfaithful`; pair and low-level checks from `fake_latent` | m1 passes every Instrument (1.0) and m2 fails every pair-checked Instrument | N/A |
| Only needed Trials | 4 Clips, one pairwise check | 2 Trials per Session, not 12 | N/A |
| Gap | Pilot Test uses `presence`, with no pair check for it | `coverage_gap: presence (tests: pilot1)`; the run proceeds | N/A |
| Second run | Same Test again | The older run is superseded and kept | N/A |
| Bad check | `expected` not among the check's Clips; Item not in the Instrument; `checks` on a pilot Test | `push test` refuses | `config_invalid` |
| Wrong Test | `screen models pilot1` / no checks | Refused, nothing written | `not_a_screening_test` / `no_screening_checks` |
| Invalid Trial | One position Trial `invalid` | That unit fails, and the ratio drops | N/A |
| Nothing to resume | `--resume`, no open run for the Test | Refused | `screening_not_open` |
| Status | A perception run exists | `status --json` `by_persona_attribute` skips `p0` and is not null | N/A |

</frozen-after-approval>

## Code Map

- `engine/run.py`, `stages/screen.py`, `board/screening.py`, `core/hashes.py` -- from 3.1; reuse them. Migration 6 (3.1) already has `screening_runs.screening_test` and `screening_results.pair_checks`; this story adds no migration.
- `core/plan.py` -- `plan_test`, `canonical_trials`. `config/models.py` -- `TestConfig`, `Thresholds`, `FakeSettings`. `config/load.py` -- `load_test`.
- `stages/status.py` -- `_by_persona_attribute` returns None for an unknown Persona today. `stages/open.py` -- the refusal for a screening Test.

## Tasks & Acceptance

**Execution:**
- [ ] `src/consortium/config/models.py`, `config/load.py` -- Add `PerceptionCheck`, `TestConfig.checks` and `Thresholds.perception_min = 0.8`, plus the check validation.
- [ ] `src/consortium/instruments/perception_cues.yaml` -- The built-in low-level Instrument.
- [ ] `src/consortium/core/plan.py`, `core/perception.py` -- The `shapes` override, `check_shapes`, `NEUTRAL_CARD`/`NEUTRAL_PERSONA_ID = "p0"`, `score_perception`, `pair_checks_by_instrument(checks, instruments)` and `coverage(needed, covered)`.
- [ ] `src/consortium/raters/fake.py` -- `fake_latent` and the perception modes.
- [ ] `src/consortium/engine/run.py` -- Let the Test-context loader take a run name (the `tests` row `s<n>` vs the registered screening Test it reads), extra cards (`p0`) and check shapes. No `config.load` import: loading still arrives through 3.1's `ConfigReader`.
- [ ] `src/consortium/stages/screen.py`, `cli.py` -- `screen_models(study, test, ...)` and `consortium screen models TEST [--yes] [--ceiling USD] [--resume] [--study PATH]`. Print the pass/fail result per Model × Instrument, plus the gaps. `--resume` with no open run of TEST refuses `screening_not_open` (3.1's code).
- [ ] `src/consortium/stages/open.py`, `stages/status.py` -- Add `screening_test_not_openable` for any registered `kind: screening` Test (a user screening Test or a run row `s<n>`), checked before the `openable` check so it wins over `protocol_lock_unavailable`. Skip `p0` when tallying.
- [ ] `docs/INTERFACE.md`, `templates/study/study.yaml` -- Document `checks`, `perception_min`, `fake.perception`, `perception_cues`, the command and the codes.
- [ ] `tests/test_screen_models.py`, `test_perception.py` -- Every matrix row, plus the scoring unit rules.

**Acceptance Criteria:**
- Given a perception run, then every Trial went through `engine.dispatch` with the ledger, Archive and lease, and its request carries `NEUTRAL_CARD`.
- Given any perception run, when a pilot Test is exported, then no row comes from a screening run.
- Given the same seed and Fake settings, when the run is repeated in a fresh Study, then the results are identical.

## Implementation Notes

## Spec Change Log

## Review Triage Log

## Design Notes

- Perception is a property of the Model, so a neutral Persona keeps the cost at one Agent per Model and avoids confounding it with Persona effects. The PRD's "construct" is taken per Instrument, because results, eligibility and invalidation are all per Instrument.
- A check names its `item`, so a Godspeed pair can target `animacy_1`, for example. Low-level checks count toward their own Instrument's result (usually `perception_cues`) and never count as coverage, because FR-10 requires a Clip pair for that.

## Verification

**Commands:**
- `uv run pytest -q tests/test_screen_models.py tests/test_perception.py` -- expected: all pass
- `uv run pytest -q && uv run ruff check src tests && uv run lint-imports` -- expected: clean
