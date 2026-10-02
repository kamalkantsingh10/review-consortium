---
title: 'Story 1.9 — Cost estimate, ceiling, pause and resume'
type: 'feature'
created: '2026-10-02'
status: 'ready-for-dev'
route: 'dispatch'
review_loop_iteration: 0
context:
  - '{project-root}/_doc/implementation-artifacts/epic-1-context.md'
  - '{project-root}/_doc/implementation-artifacts/epic-1-code-map.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** After 1.7/1.8, `open` dispatches Trials with no cost estimate and no spend limit, so a researcher can't see or cap what a Run will cost.

**Approach:** Add one pure, offline cost function (`core.cost`) used for both the pre-Run estimate and per-attempt reservations. Store the ceiling and a per-attempt ledger in `board.db`. The engine reserves before every dispatch and pauses the Run when the next reservation would cross the ceiling. `open --resume --ceiling <higher>` continues.

**Decisions (accepted 2026-10-02):**
- No ceiling set: `open` requires `--ceiling` (`ceiling_required`) unless every Model in the Test is the Fake rater (`provider: fake`, cost 0), which may run with no ceiling.
- `open` compares the expected single-attempt cost to the ceiling; the worst case including retries is printed alongside. The mid-run pause (a reservation per attempt) is the hard limit.

## Boundaries & Constraints

**Always:**
- **One cost function (AD-13).** `estimate(request, model, prices, clip_seconds)` is the only cost math: media tokens = Σ `duration_s` of the request's target + Practice clips (`clip_seconds` from the `clips` table) × `media_tokens_per_s`, text tokens = rendered text length ÷ `chars_per_token`, plus the pinned `max_output_tokens`, priced per 1M input/output tokens. `core/cost.py` imports stdlib and pydantic only. USD is `Decimal`, stored and printed as decimal strings.
- **Estimate:** `expected` = sum over every planned Trial in the Plan (both pairwise orders, all Repeats, Practice clips included) of `estimate(...)`; `worst_case` = `expected` × `(1 + max_retries)` (effective `session.max_retries`). On resume both cover only non-terminal Trials.
- `open` prints `expected` and `worst_case` side by side, the Trial count and the distinct providers that will receive Clips to stdout, then asks for confirmation unless `--yes`. Nothing reaches a Rater before that, including `prepare()`. `--dry-run` prints the same figures and exits.
- **Ceiling (AD-9).** `--ceiling <usd>` appends a row to `ceiling_changes` (UTC timestamp, previous, new). The current ceiling is the latest row. The ceiling covers the whole Study. `open` refuses with `over_ceiling` when committed spend + `expected` > ceiling. If no ceiling has ever been set, `open` without `--ceiling` refuses with `ceiling_required` unless every Model in the Test has `provider: fake`; a Fake-only Run with no ceiling reserves and records costs but never pauses.
- **Ledger.** There is one row per `(trial_id, attempt)`: `reserved_usd` is inserted by 1.7's `begin_attempt` writer op (same transaction as the attempt row, before the request is archived and marked `sent`), and `actual_usd` is computed from the returned usage. Committed = Σ(actual, or reserved where actual is NULL). Check, reserve and attempt increment are one writer op, so concurrent dispatch can't overshoot and a refused reservation leaves no attempt.
- **Pause.** If committed + the next reservation > ceiling, that attempt is not reserved or sent. No new dispatches start, in-flight attempts are still collected, and the Test's run status becomes `paused: ceiling`.
- `FakeRater` reports deterministic usage from its Model's `fake` settings (`input_tokens`, `output_tokens`). Its price comes from `prices.yaml` like any Model.

**Never:**
- Calling a provider for token counts or prices.
- A second cost formula anywhere.
- Locking the ceiling in any file.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Confirm | `open t1`, no ceiling problem, user answers y | Prints estimate, then runs | N/A |
| Decline | user answers n | Nothing is dispatched or written to the ledger | `not_confirmed` (1.7), exit code 1 |
| Over ceiling | committed + `expected` > current ceiling | Nothing is dispatched | `over_ceiling: expected X > ceiling Y`, exit code 1 |
| Only worst case over | `expected` ≤ ceiling < `worst_case` | Both printed; runs; the pause guards retries | N/A |
| No ceiling, real Model | no ceiling ever set, a non-Fake Model, no `--ceiling` | Nothing is dispatched | `ceiling_required`, exit code 1 |
| No ceiling, Fake only | every Model `provider: fake`, no ceiling | Runs uncapped; footer `ceiling none` | N/A |
| Bad ceiling | `--ceiling -1` | Nothing changes | `invalid_ceiling` |
| Exact fit | committed + reservation == ceiling | Dispatched (`<=`) | N/A |
| Mid-run cap | the next reservation crosses the ceiling | In-flight attempts are collected, and the status is `paused: ceiling` | `ceiling_reached`, exit code 1 |
| Resume higher | paused run, `open t1 --resume --ceiling <higher>` | Change logged, remaining estimate shown, continues with no re-send | N/A |
| Resume, same cap | `--resume` with no higher ceiling | Pauses again before any send | `ceiling_reached` |
| No usage | the attempt ends without usage | Reserved cost counts | N/A |
| Missing price | the Model has no `prices.yaml` entry | Nothing is dispatched | `price_missing: <model_id>` |

</frozen-after-approval>

## Code Map

- **Already done by Story 1.2 (implemented):** `config.load.load_prices` cross-checks prices against `study.yaml`, so every Model has a price and no stray ids exist (`config_invalid`, field `models.<id>`). A `price_missing` error cannot happen; do not implement it, and rely on `load_prices`.
- `src/consortium/engine/dispatch.py`, `board/writer.py`, `stages/open.py`, `raters/fake.py` (1.7) -- Extended here.
- `src/consortium/config/models.py` (1.2) -- `PricesConfig.models[<model id>]` (`ModelPrice`: `input_usd_per_mtok`, `output_usd_per_mtok`) and `ModelConfig` are extended additively.

## Tasks & Acceptance

**Execution:**
- [ ] `src/consortium/core/cost.py` -- `estimate(request, model, prices, clip_seconds) -> Decimal`, `estimate_plan(plan, requests, cfg, prices, clip_seconds) -> PlanEstimate` (total, trial count, providers), and `actual(usage, model, prices) -> Decimal`. Pure. -- AD-13.
- [ ] `src/consortium/config/models.py`, `src/consortium/templates/study/{prices,study}.yaml` -- Add `ModelPrice.media_tokens_per_s` and `chars_per_token` (decimal strings), and `ModelConfig.fake` (`input_tokens`, `output_tokens`); template's fake Model priced at 0. -- Formulas live in config.
- [ ] `src/consortium/board/db.py` -- Append a migration that adds `ledger(trial_id, attempt, model_id, reserved_usd, actual_usd NULL, PK(trial_id, attempt))`, `ceiling_changes(ts, previous_usd, ceiling_usd)`, and a nullable `paused_reason` column on `tests` (1.5). Add `board` reads for the current ceiling and committed spend, and a `set_ceiling` writer op. -- AD-9, AD-13.
- [ ] `src/consortium/board/writer.py` -- Extend `begin_attempt(trial_id, usd, ceiling) -> attempt | None` (atomic check, ledger insert and attempt increment; `None` = refused, nothing written); add `record_actual` and `set_paused` ops. -- No overshoot under concurrency.
- [ ] `src/consortium/engine/dispatch.py` -- Before each attempt: reserve, and on refusal stop scheduling, drain in-flight attempts and set the pause. After collect: record the actual cost. -- FR18.
- [ ] `src/consortium/raters/fake.py` -- Return usage from the `fake` settings. -- Makes the ceiling testable.
- [ ] `src/consortium/stages/open.py`, `src/consortium/cli.py` -- Add the `expected`/`worst_case` printout before 1.7's confirmation, ceiling validation and logging, the `ceiling_required` and `over_ceiling` refusals, and `--resume` (1.8) clearing the pause. -- FR14.
- [ ] `docs/INTERFACE.md` -- Document the `prices.yaml` fields, the `open` flags and the reason codes.
- [ ] `tests/test_cost.py` -- Covers the matrix rows with a FakeRater at non-zero prices, pure `estimate` cases, and a spy asserting no Rater call before confirmation.

**Acceptance Criteria:**
- Given a finished or paused Run, when the ledger is queried, then there is exactly one row per dispatched `(trial_id, attempt)`, and committed spend never exceeds the ceiling at any moment.
- Given one Plan, when `open --dry-run` and `open` run, then they print identical estimates.

## Implementation Notes

## Spec Change Log

## Review Triage Log

## Design Notes

- **Study-wide ceiling.** One researcher budget covers all Tests. Committed spend sums the whole ledger.
- **Pause exits 1 with `ceiling_reached`** so scripts notice. `status` (1.11) reads the stored pause.
- **Per-attempt reservation is the single-attempt estimate,** the same figure `open` checks. The `(1 + max_retries)` worst case is information only; the per-attempt pause is what enforces the ceiling.
- **Committed spend counts at `open`** because the ceiling is study-wide: an earlier Test's spend reduces what a new Run may use.

## Verification

**Commands:**
- `uv run pytest -q tests/test_cost.py` -- expected: all pass
- `uv run pytest -q && uv run ruff check src tests && uv run lint-imports` -- expected: clean
