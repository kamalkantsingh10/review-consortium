---
title: 'Story 2.1 — Provider failure handling and refusals'
type: 'feature'
created: '2026-10-02'
status: 'done'
baseline_commit: 'af8626b40083b56f8661e5dd00975bb00508c866'
route: 'dispatch'
review_loop_iteration: 0
context:
  - '{project-root}/_doc/implementation-artifacts/epic-2-context.md'
  - '{project-root}/_doc/implementation-artifacts/epic-2-code-map.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Today any non-`ok` Rater result ends the Trial as `failed`. A rate limit costs a data point, and refusals cannot be told apart from failures.

**Approach:** Adapters classify every outcome as one of four `Category` values. The engine retries `transient` results with deterministic backoff, under a budget separate from `max_retries`, and ends the Trial as `refused` or `failed` otherwise. `status --json` and the export report refusal and failure counts per Persona attribute, and the export also reports them per Condition.

## Boundaries & Constraints

**Always:**
- **Categories (AD-6).** `raters.base.Category = Literal["ok","transient","refused","fatal"]` is the type of `RaterResult.category`. Adapters never raise for a provider outcome. An error at submit time goes into the handle, and `collect` returns its category. `raw` holds the provider text or an error summary. Any other category value stops the Run with `adapter_error`.
- **Engine (AD-4).** The raw response is archived and its actual cost recorded first. Then:
  - `ok` → validate as in 1.10.
  - `refused` → Trial `refused`. It is never validated and never retried.
  - `fatal` → Trial `failed` (attempt category `fatal`).
  - `transient` → the attempt is recorded with category `transient` and `valid` NULL. The Trial stays `sent`. After the backoff it gets a new attempt through the same begin_attempt/reserve/ceiling path (new seed, same request text). Once the Trial has had more than `transient_retries` transient attempts it becomes `failed`, and the attempt category stays `transient`.
- **Separate budgets.** An invalid answer is retried while the Trial's invalid attempts ≤ `max_retries`. A transient result is retried while its transient attempts ≤ `transient_retries`. Both are counted from the `attempts` rows (`valid = 0` and `category = 'transient'`), never from the attempt number. The hard cap on total attempts is `1 + max_retries + transient_retries`, and `settle_exhausted` and resume use it. At the cap, an abandoned latest attempt settles as `failed`/`attempts_exhausted`, as in 1.10.
- **Backoff.** For the k-th transient attempt the delay is `min(backoff_max_s, backoff_initial_s · 2^(k−1)) · u`, where `u = Random(derive_seed(attempt_seed, "backoff", "")).uniform(0.5, 1.0)`. It is a pure function. The provider semaphore is not held while waiting. A ceiling pause during the wait leaves the Trial `sent`.
- **Resume.** A latest attempt recorded `transient` (stopped mid-backoff) is not collected again; the Trial gets a new attempt at once (no wait), within the caps. One archived but not yet recorded is collected again, as today.
- **Config.** `StudyConfig.session.retry: RetryPolicy(transient_retries=3, backoff_initial_s=2, backoff_max_s=60)`. All values are ≥ 0, and `backoff_max_s` ≥ `backoff_initial_s`. It is set in `study.yaml` only, with no per-Test override. The worst-case cost estimate stays at × (1 + `max_retries`), because transient attempts reserve and are checked against the ceiling one at a time.
- **Fake.** `FakeSettings` gains `transient_rate`, `refusal_rate` and `fatal_rate` (each 0–1). Each one is drawn from its own stream, `Random(derive_seed(seed, "fake_<kind>", ""))`, and they are checked in the order fatal, refused, transient, invalid. Transient `raw` alternates by attempt between a simulated rate limit and a simulated transport error. Non-`ok` results report zero usage; the handle carries the category.
- **Reporting.**
  - `status --json` gains `by_persona_attribute`: `{test: {persona_<field>: {value: {trials, refused, failed}}}}`. It uses the export's `persona_*` column names. It is `null` when the Panel cannot be loaded, and status still works. The text table is unchanged. Status never reads `blinding_key.csv` (AD-2).
  - The export writes a sidecar `exports/<test>-attrition.csv` next to the main CSV (atomically, same refusal rules), columns `dimension` (`model`|`persona`|`condition`), `attribute` (`model_id`, the `persona_<field>` name, or the Condition column name), `value`, `trials`, `refused`, `failed`; rows in that dimension order, then attribute order, then value as first seen in the tidy CSV. The tidy CSV is unchanged. Epic 4's rater-flow reuses this file.
- **Decision (Kamal, 2026-10-02):** export attrition is the sidecar `exports/<test>-attrition.csv` (per Model, per Persona attribute, per Condition); `status --json` gets `by_persona_attribute` only, never Conditions.

**Never:**
- Validating or retrying a refused or fatal result.
- Sleeping or backing off inside an adapter.
- A Condition anywhere in `status`.
- A new `board.db` migration (`attempts.category` already exists).
- Network access in tests.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Recovers | attempt 1 transient, attempt 2 ok and valid | `valid`; 2 Archive pairs; attempt 1 category `transient` | N/A |
| Transient exhausted | `transient_retries: 3`, 4 transient attempts | `failed`; no 5th attempt | N/A |
| `transient_retries: 0` | first attempt transient | `failed` at once | N/A |
| Refused | category `refused` | `refused`; no validation row; no retry | N/A |
| Fatal | category `fatal` | `failed`, category `fatal` | N/A |
| Separate budgets | `max_retries: 1`: invalid, transient, invalid | `invalid` after attempt 3 | N/A |
| Killed in backoff | latest attempt recorded `transient` | resume: new attempt, old handle not collected | N/A |
| Ceiling on retry | the reservation of a transient retry is refused | Trial stays `sent`; Run pauses at `ceiling` | N/A |
| Bad category | adapter returns `"oops"` | Run stops | `adapter_error` |

</frozen-after-approval>

## Code Map

- `engine/dispatch.py` -- `outcome_for` (non-ok → failed; `attempt <= max_retries`), `attempt_once`/`finish`, `max_attempts`.
- `board/trials.py` -- `settlement`, `load_resumable` (collects any sent, unvalidated attempt today), `settle_exhausted`.
- `stages/open.py` -- `_load_resumable(..., 1 + ctx.max_retries)`, the `dispatch` call, `raters_for` (builds `FakeRater`).
- `stages/export.py` -- `PERSONA_FIELDS`, `_condition_columns`, `_write`. `stages/status.py` -- `to_json`. Stages may not import each other.

## Tasks & Acceptance

**Execution:**
- [x] `src/consortium/raters/base.py`, `config/models.py`, `raters/fake.py` -- `Category`; `RetryPolicy`; the Fake rates and simulated outcomes.
- [x] `src/consortium/board/trials.py` -- Add `record_transient(trial_id, attempt)` (category plus `answered_at`) and `attempt_counts(trial_id) -> (invalid, transient)`. Make `settlement`, `settle_exhausted` and `load_resumable` count-based with the total cap, and skip collecting a recorded-transient latest attempt.
- [x] `src/consortium/engine/dispatch.py`, `stages/open.py` -- Category handling and a pure `backoff_delay`. `dispatch` takes `retry: RetryPolicy`. `open` passes the policy and the Fake rates.
- [x] `src/consortium/core/personas.py` -- Move the `persona_<field>` value mapping here as `attribute_values(persona)`, and add a pure `tally_by_attribute(counts_by_persona, personas)`. Export reuses it.
- [x] `src/consortium/board/queries.py`, `stages/status.py` -- Add a per-persona `{trials, refused, failed}` query, and add `by_persona_attribute` to the JSON output.
- [x] `src/consortium/stages/export.py` -- Write `exports/<test>-attrition.csv` with `tally_by_attribute` (persona) plus Model and Condition tallies.
- [x] `docs/INTERFACE.md` -- Update Run step 8, retries, Resume, Trial states, `session.retry`, the Fake rates, the status JSON and the export.
- [x] `tests/test_failures.py` -- Every matrix row, using the FakeRater and the `backoff_initial_s: 0` setting, plus `backoff_delay` determinism and bounds.
- [x] `tests/test_fake.py`, `test_config.py`, `test_status.py`, `test_export.py` -- Independent rate draws, config bounds, `by_persona_attribute` (no Condition key), the attrition sidecar (columns, counts, no file on refusal).

**Acceptance Criteria:**
- Given Fake rates for transient, refusal and fatal results, when a Run completes, then it needs no operator action and every Trial is `valid`, `invalid`, `refused` or `failed`.
- Given the same `study.seed` and rates, when the Run is repeated in a fresh Study at any `concurrency`, then every Trial has the same final state, the same attempt categories and the same attempt count.
- Given any completed Run, then no `refused` Trial has an attempt with `valid` set.

## Implementation Notes

## Spec Change Log

## Review Triage Log

| # | Source | Finding | Verdict | Route |
|---|---|---|---|---|
| 1 | BH, EC | Unknown category skips the Archive and cost record | medium | patch (archive first, then adapter_error) |
| 2 | BH | Worst-case estimate ignores transient retries | medium | patch |
| 3 | BH, EC | Ceiling pause waits out every backoff sleep | medium | patch (pause event) |
| 4 | EC | Infinite backoff accepted, so the Run hangs | medium | patch |
| 5 | BH, EC | CSV and sidecar replaced non-atomically | low | patch |
| 6 | BH | Sidecar name can collide with an `<x>-attrition` Test | low | patch (refuse such names) |
| 7 | BH | Attrition lacks invalid, a failed-cause split and an Instrument dimension | medium | patch |
| 8 | BH, VG | Bare KeyError hides bugs as "no Panel" | low | patch |
| 9 | BH | retry_left("ok") naming; jitter range inconsistent; refused-invariant wording | low | patch |
| 10 | BH | Status JSON additive key / sidecar version | low | patch (docs + version column) |
| 11 | VG, BH | Retry config through open/resume, resume categories, mixed cap and value order untested; weak config test; template-order slicing | medium | patch (tests) |
| 12 | BH | Two queries per Trial on resume | low | Rejected: performance only |

## Design Notes

- "Never sleep inside an adapter" covers `submit`/`collect` outcomes only. 2.2's File-API upload retries and ACTIVE polling happen in `prepare` (no Category; failure is `prepare_failed`), so they do not compete with `transient_retries`. SDK-internal request retries are disabled in 2.2/2.3, so the engine's transient retry is the only request retry.
- No migration: `board.db` stays at 5 (`m5_validation`); Epic 2 adds none.
- A Test named `<x>-attrition` would share a file name with Test `<x>`'s sidecar (the same pre-existing class as `leak-report.csv`); not addressed here.

## Verification

**Commands:**
- `uv run pytest -q tests/test_failures.py tests/test_fake.py tests/test_retries.py tests/test_resume.py` -- expected: all pass
- `uv run pytest -q && uv run ruff check src tests && uv run lint-imports` -- expected: clean
