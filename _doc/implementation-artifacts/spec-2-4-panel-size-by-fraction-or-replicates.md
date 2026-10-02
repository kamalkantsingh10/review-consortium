---
title: 'Story 2.4 — Panel size by fraction or replicates'
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

**Problem:** The Panel is always 32 profiles × NARS bands. Cost cannot be traded against design resolution, and `personas generate --force` can silently re-label Personas that past Trials reference.

**Approach:** `personas.big_five` becomes `{fraction, replicates}`. The profile set is the full grid, a resolution-V half or a resolution-III quarter fraction, crossed with every band and repeated with fresh demographic draws through the existing stratified dealing. `meta.json` and `INTERFACE.md` state the design and its aliasing. Regeneration refuses with `panel_in_use` once `board.db` holds a Trial.

## Boundaries & Constraints

**Always:**
- **Config.** `big_five: {fraction: 1 | 0.5 | 0.25 (default 1), replicates: 1 | 2 | 3 (default 1)}`. The strings `"1"`, `"1/2"`, `"1/4"` are also accepted; the model stores the canonical string (`"1"`, `"1/2"`, `"1/4"`). The legacy value `all_32` is accepted and normalised to `{fraction: "1", replicates: 1}`. The init template moves to the mapping form. Anything else is `config_invalid`.
- **Coding.** Factors A–E = O, C, E, A, N (TRAITS order); `high` = +1, `low` = −1. Every fraction is the *principal* one (all words +), and its profiles keep full-grid bit-pattern order (filter of `big_five_profiles()`).
  - `"1"`: 32 profiles.
  - `"1/2"`: 2^(5−1), generator N = O·C·E·A, defining relation I = OCEAN; resolution V. Main effects alias 4-way, two-way alias 3-way: nothing below 3-way is aliased with a main effect or two-way.
  - `"1/4"`: 2^(5−2), generators A(agreeableness) = O·C and N = O·E, defining relation I = OCA = OEN = CEAN; resolution III. Aliases: O = CA = EN, C = OA, E = ON, A = OC, N = OE, and CE = AN, CN = EA.
- **Pool and IDs.** N = profiles × bands × replicates. Order: profile, then replicate, then band (band fastest), IDs `p1..pN`. Quotas use the existing `stratified_levels` with `per_band = profiles × replicates`, same seeds; each replicate gets its own slot, hence its own draw, with exact marginals and within-band counts differing by at most 1.
- **Golden.** For fraction `"1"`, replicates 1, cards and `index.json` are byte-identical to today (`GOLDEN_INDEX_SHA256` unchanged). `GENERATOR_VERSION` stays `"1"`.
- **meta.json** gains `design`: `{fraction, replicates, profiles, resolution, generators, defining_relation, aliasing}` (strings/lists in trait letters O C E A N; `null`/empty for the full grid).
- **`panel_in_use`.** `personas generate` (with or without `--force`) refuses before writing anything when `board.db` holds any Trial, via `board.db.read_only` + a new `board/trials.any_trials(conn)` (false when there is no `board.db` or no `trials` table). The stage never takes the lease and never migrates.

**Never:** a `board.db` migration; changing the Persona fields, card text or `persona_*` export columns; a stage importing another stage; blocking regeneration when only Archive files exist.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Template | `{fraction: 1, replicates: 1}` | 64 Personas, golden bytes | N/A |
| Legacy | `big_five: all_32` | same 64 Personas | N/A |
| Half | `fraction: 1/2`, 2 bands | 32 Personas; each row N = O·C·E·A | N/A |
| Quarter × 3 | `fraction: 0.25`, `replicates: 3`, 2 bands | 48 Personas | N/A |
| Bad value | `fraction: 0.3` or `replicates: 4` | nothing written | `config_invalid` |
| Panel used | `--force`, `board.db` has a Trial | nothing changes | `panel_in_use` |
| Board without Trials | `--force`, Clips only | regenerated | N/A |

</frozen-after-approval>

## Code Map

- `src/consortium/core/personas.py` -- `big_five_profiles`, `stratified_levels`, `generate_personas` (`b, slot = i % bands, i // bands` already fits the band-fastest order), `GENERATOR_VERSION`.
- `src/consortium/stages/personas.py` -- `generate`, `_meta`; check order: config, sweep, `panel_in_use`, `panel_exists`.
- `src/consortium/config/models.py` -- `PersonaFrame.big_five` (`Literal["all_32"]` today).
- `src/consortium/board/db.py` -- `read_only`; `MIGRATIONS` stays at 5.

## Tasks & Acceptance

**Execution:**
- [ ] `src/consortium/config/models.py`, `templates/study/study.yaml`, `docs/schema/` -- `BigFiveDesign` model with legacy normaliser; template mapping form; regenerate schemas.
- [ ] `src/consortium/core/personas.py` -- Pure `design_profiles(fraction) -> (profiles, design_meta)`; `generate_personas` takes fraction and replicates.
- [ ] `src/consortium/board/trials.py` -- `any_trials(conn) -> bool`.
- [ ] `src/consortium/stages/personas.py` -- `panel_in_use`, `design` in `meta.json`.
- [ ] `docs/INTERFACE.md` -- `personas generate` (pool = profiles × bands × replicates, ordering, aliasing table, cost lever: Trials scale linearly with N, e.g. half-fraction halves cost), `meta.json`, `personas.big_five`, error `panel_in_use`.
- [ ] `tests/test_personas.py`, `tests/test_config.py` -- Matrix rows; half rows satisfy N = O·C·E·A and quarter rows A = O·C, N = O·E in ±1 coding; every trait column balanced and all main-effect pairs orthogonal (zero inner product); per-band exact marginals with replicates; determinism; golden unchanged; meta `design`.

**Acceptance Criteria:**
- Given the same `study.yaml`, when generated twice in different folders, then the `panel/` trees are byte-identical for every fraction and replicates value.
- Given any design, then `open` and `export` accept the Panel unchanged (`p1..pN` contiguous).

## Design Notes

Verified: the half fraction is profiles 2, 3, 5, 8, 9, 12, … (16; `p1` all-low is excluded), the quarter is profiles 4, 7, 10, 13, 17, 22, 27, 32; both have balanced, pairwise-orthogonal main-effect columns. A legacy Study's `frame_sha256` changes (the frame now dumps as a mapping); `meta.json` is not checked, so nothing breaks. `panel_in_use` also covers regeneration without `--force` onto a deleted Panel, since a changed frame would re-label stored Trials.

## Verification

**Commands:**
- `uv run pytest -q tests/test_personas.py tests/test_config.py` -- expected: all pass, golden test unchanged
- `uv run pytest -q && uv run ruff check src tests && uv run lint-imports` -- expected: clean
