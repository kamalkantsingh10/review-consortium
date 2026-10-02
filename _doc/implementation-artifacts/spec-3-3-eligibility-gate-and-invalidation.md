---
title: 'Story 3.3 — Eligibility gate and invalidation'
type: 'feature'
created: '2026-10-02'
status: 'done'
baseline_commit: 'eff67c77ffd66123336969d74e8fedbb989925a5'
route: 'dispatch'
review_loop_iteration: 0
context:
  - '{project-root}/_doc/implementation-artifacts/epic-3-context.md'
  - '{project-root}/_doc/implementation-artifacts/epic-3-code-map.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** `open` plans every Persona × Model, whatever screening says. A failed, missing or stale screening result can still feed results.

**Approach:** Add a pure `core.eligibility.eligible` gate. `open` calls it after loading the Panel and plans only the Agents it lets through. It refuses `screening_coverage_missing`, `screening_stale` or `no_eligible_agents`, and records every exclusion with a reason code in `board.db`, where Epic 4's Rater-flow report reads it.

## Boundaries & Constraints

**Decisions (Kamal, 2026-10-02):**
- **Pilot gating.** Pilots run ungated, with the stderr warning `unscreened_pilot`, until the first screening run exists; from then on they are gated. Main Tests are always gated. Screening Tests are never gated.

**Always:**
- **Which Tests are gated.** `kind: main` is always gated. The gate goes in now, after the existing `protocol_lock_unavailable` refusal, so it applies as soon as Epic 4 makes main openable. `kind: pilot` is gated once `board.db` holds any `screening_runs` row (`board.screening.any_runs`, any kind or status). Before that, a pilot runs ungated: stderr shows `unscreened_pilot: Test <t> runs without screening` and the summary shows `screening: none`. `kind: screening` is never gated: `screen` runs its own Trials ungated, and `open` refuses screening Tests (`screening_test_not_openable`, 3.2).
- **Current result.** A result is current when it is the latest `board.screening.current_results` row for its key. It counts only while its `settings_hash` and `instrument_hash` equal `core.hashes` of the current `study.yaml` Model and Instrument definitions (for fidelity: `instrument_hash` of the list `config.load.load_fidelity_instruments` returns now). Keys are `(model, agent)` with instrument `fidelity` for fidelity, and `(model, instrument)` for perception.
- **Rules, applied to the Test's Models and Instruments only:**
  - **Coverage.** 3.2's rule: a Test Instrument is covered when a current perception result for it has `pair_checks > 0`, for any Model and with any stamps (`core.perception.coverage`). Otherwise `open` refuses `screening_coverage_missing` and names the Instruments.
  - **Perception.** A Model needs a current, unstale `pass` for every Test Instrument. A `fail` excludes all its Agents with `perception_fail`; no result excludes them with `perception_missing`.
  - **Fidelity.** An Agent needs a current, unstale `pass` on its `fidelity` row. Otherwise it is excluded with `fidelity_fail` or `fidelity_missing`.
  - **Stale.** If the only current result for a needed key is stale, the outcome is `screening_stale`, and `open` refuses rather than excluding. The message names each Model or Instrument and says to re-run `screen personas` or `screen models`.
  - **Empty plan.** If no Agent is eligible, `open` refuses `no_eligible_agents` and gives the reason counts.
  - **Refusal order** (after the Panel loads): coverage, then stale, then empty.
- **One reason per excluded Agent.** Precedence is `screening_stale` > `perception_*` > `fidelity_*`, so Rater-flow counts sum.
- **Planning.** `plan_test` gains `agents: Collection[str] | None = None` and skips Agents not listed. Seeds come from the `session_id`, so an eligible Agent's Trials and requests are byte-identical to the ungated plan. The dry run and the Run gate identically, so `requests sha256` still matches. Under the lease, the Run re-gates. A different exclusion set raises `test_changed`.
- **Record (Run only).** The `insert_plan` transaction writes:
  - one `screening_exclusions` row per excluded Agent;
  - one `screening_stamps` row per planned `(model, instrument)`.
- **Resume freezes eligibility.** No re-gating and no new exclusions. For a gated Test, the Models and Instruments of non-terminal Trials must still match `screening_stamps`, otherwise resume refuses `screening_stale`.

**Never:** changing screening results; reading the Blinding key or Archive; dropping Trials of an already-open Test; a gate in `engine` or `export`; one stage importing another.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| All pass | pilot; m1 passes every Instrument and all Agents pass fidelity | plan identical to ungated; 0 exclusions | N/A |
| Fidelity fail | p3-m1 fails, p5-m1 has no result | 2 Agents excluded (`fidelity_fail`, `fidelity_missing`); dry run shows counts | N/A |
| Model fails one Instrument | m2 fails `pairwise_alive` | every `*-m2` Agent excluded, `perception_fail`, with the Instrument named | N/A |
| No coverage | no current perception result for `presence` | nothing planned | `screening_coverage_missing` |
| Settings changed | m1 `temperature` edited after screening | nothing planned | `screening_stale` |
| Instrument edited | `godspeed` Item changed | nothing planned | `screening_stale` |
| Superseded | old run failed, new run passes | the new pass counts | N/A |
| None eligible | every Agent excluded | nothing written | `no_eligible_agents` |
| Pre-screening pilot | no `screening_runs` | ungated plan, digest unchanged from Epic 1 | `unscreened_pilot` warning |
| Resume after edit | gated Test open, m1 settings edited | nothing sent | `screening_stale` |

</frozen-after-approval>

## Code Map

- **As built by Stories 3.1–3.2; reuse these, don't redo them:**
  - Coverage is `core.perception.covered_instruments(results, instrument_hashes, settings_hashes)`: an Instrument counts as covered only via current, non-stale results (`pair_checks > 0`, matching Instrument and settings hashes).
  - `board/screening.current_results` keys perception results per (screening Test, instrument, model) and fidelity results per (instrument, model, agent).
  - Stamps are recorded at run creation (`screening_runs.settings_hashes`, `instrument_hash`).
  - `core/hashes`: `instrument_hash` includes `PROMPT_FORMAT`; `fidelity_hash` adds the card wording.
  - `board/trials.any_panel_trials` ignores `p0` Trials and is the Panel guard used by `personas generate`. **Imported screening results (3.4) are not yet counted by the guard; 3.4 must make the guard also count fidelity results with a `source_study`.**
  - Screening Tests are registered not openable, and `open` refuses them with `screening_test_not_openable`.
  - `screen personas` and `screen models` support `--dry-run`, `--abandon` and `--resume`; `bad_option` covers conflicting flags.
- `src/consortium/core/eligibility.py` -- new and pure: `eligible(...) -> Eligibility(agents_ok, excluded)`, with the reason codes from the code map.
- `src/consortium/core/plan.py` -- `plan_test` gets the `agents` filter.
- `src/consortium/core/hashes.py`, `src/consortium/board/screening.py` -- from 3.1: stamps, `current_results`, `any_runs`. `src/consortium/core/perception.py` -- from 3.2: `coverage`.
- `src/consortium/config/load.py` -- from 3.1: `load_fidelity_instruments` (the current fidelity hash input).
- `src/consortium/board/db.py` -- migration 7 (`m7_screening_gate`): `screening_exclusions(test, agent_id, persona_id, model_id, instrument NULL, reason)` and `screening_stamps(test, model_id, instrument, settings_hash, instrument_hash)`.
- `src/consortium/engine/run.py` (from 3.1) -- holds the moved run/resume machinery; the gate is never put here. `src/consortium/stages/open.py` -- builds the gate into its `Prepared`: filter the plan after `load_personas`; `insert(conn)` records the gate; `reload()` re-gates under the lease (a different plan → `test_changed`); `resume_check(conn)` does the stamp check. `OpenSummary`: the screening lines.

## Tasks & Acceptance

**Execution:**
- [x] `src/consortium/core/eligibility.py` -- the rules, precedence and coverage above. Inputs are plain mappings, with no I/O.
- [x] `src/consortium/core/plan.py` -- the `agents` filter. `None` means today's behaviour.
- [x] `src/consortium/board/db.py`, `src/consortium/board/screening.py` -- migration 7, plus `record_gate(conn, test, excluded, stamps)` and `test_stamps(conn, test)`.
- [x] `src/consortium/stages/open.py` -- wire the gate, the refusals, the warning, the dry-run lines, the record and the resume check through the `Prepared` hooks (no gate code in `engine/run.py`).
- [x] `docs/INTERFACE.md` -- in `open`: the gate, the check order, the dry-run lines and the resume freeze. Add the new tables to `board.db`. Add the error codes `screening_coverage_missing`, `screening_stale`, `no_eligible_agents` and the warning `unscreened_pilot`.
- [x] `tests/test_eligibility.py`, `tests/test_open_gate.py` -- one test per Matrix row, using the Fake rater. Results are seeded either through `board.screening.record_result` or through 3.1/3.2's faithful and unfaithful Fake configs.

**Acceptance Criteria:**
- Given a gated Test, when it is dry-run and then Run, then both print the same digest and exclusion counts, and the stored `screening_exclusions` match the dry run.
- Given every Agent eligible, then the requests digest equals the ungated digest.
- Given a dry run, then `board.db` is unchanged, as today.

## Review Triage Log

| # | Source | Finding | Verdict | Route |
|---|---|---|---|---|
| 1 | Architect | Gate counts stale perception rows as coverage, contradicting 3.2's rule | high | patch (covered_instruments) |
| 2 | BH | Self-report Instruments can never be covered | high | patch |
| 3 | BH, EC | A newer stale row hides an older fresh pass; tests use impossible inputs | medium | patch |
| 4 | BH | Stale keys refuse even for already-excluded Models | medium | patch |
| 5 | BH, VG | screening_stale exclusion unreachable but listed | low | patch |
| 6 | BH | Deciding runs and fidelity stamp not persisted for audit | medium | patch |
| 7 | BH, EC, VG | Resume freeze ignores fidelity drift, checks settle-only Trials, KeyError risk, runs only after confirm | medium | patch |
| 8 | BH, EC | Re-gate refusal under the lease is not test_changed; key ignores run IDs | medium | patch |
| 9 | BH, EC | unscreened_pilot after confirmation / only on success; non-pilot mislabel | low | patch |
| 10 | BH | One abandoned run gates pilots forever, unexplained | medium | patch (complete runs only + context) |
| 11 | BH | Plural, NULL wording, non-pass outcomes, duplicate run parsing | low | patch |
| 12 | VG, BH | Main gate, Instrument/fidelity resume, terminal-only resume, CLI detail lines untested | medium | patch (tests) |
| 13 | EC | Fidelity config errors surface as config_invalid at open | low | Rejected: a broken config should fail loudly with its own code |

## Design Notes

Dry-run lines, printed after `by type`:

```text
screening: fidelity s3, perception s4
eligible agents: 58 of 128
excluded: perception_fail 64 (m2: pairwise_alive), fidelity_fail 4, fidelity_missing 2
```

Coverage is taken from current results (`pair_checks > 0`, stored by 3.2 and carried over by 3.4's import), not from screening Test files, so `open` needs no screening-Test lookup and imported results cover too. A Model with one failed Instrument is dropped from the whole Test, because every Session must keep the same shape (`_summarize_trials`).

## Verification

**Commands:**
- `uv run pytest -q tests/test_eligibility.py tests/test_open_gate.py` -- expected: all pass
- `uv run pytest -q && uv run ruff check src tests && uv run lint-imports` -- expected: clean, with Epic 1–2 open tests unchanged
