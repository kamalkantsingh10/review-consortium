---
title: 'Story 1.9 — Cost estimate, ceiling, pause and resume'
type: 'feature'
created: '2026-10-02'
status: 'done'
baseline_commit: 'e3770aff6be338fbd7c6d5578c2a45c282fee4b5'
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

- **As built by Stories 1.6–1.8 (commit e3770af); build on these:**
  - `open` parses `--ceiling` already (`bad_ceiling`, Decimal > 0) but doesn't use it yet.
  - The Run prompt shows the requests digest and is asked *before* the lease; under the lease the plan is re-checked (`test_changed`).
  - `--resume` (story 1.8) re-renders stored Trials, runs the re-issue check under the lease, collects sent-with-handle attempts at the same attempt and re-dispatches the rest. The ceiling check and pause must apply to both Run and resume.
  - `board/trials.begin_attempt` creates the attempt row inside the writer. Put the ledger reservation in that same writer op/transaction (the code-map `begin_attempt` rule).
  - Archive response records carry `request_sha256`.
  - Each new table needs a new migration appended to `MIGRATIONS` (currently 3). Update the table and version assertions in existing tests.
- **Already done by Story 1.2 (implemented):** `config.load.load_prices` cross-checks prices against `study.yaml`, so every Model has a price and no stray ids exist (`config_invalid`, field `models.<id>`). A `price_missing` error cannot happen; do not implement it, and rely on `load_prices`.
- `src/consortium/engine/dispatch.py`, `board/writer.py`, `stages/open.py`, `raters/fake.py` (1.7) -- Extended here.
- `src/consortium/config/models.py` (1.2) -- `PricesConfig.models[<model id>]` (`ModelPrice`: `input_usd_per_mtok`, `output_usd_per_mtok`) and `ModelConfig` are extended additively.

## Tasks & Acceptance

**Execution:**
- [x] `src/consortium/core/cost.py` -- `estimate(request, model, prices, clip_seconds) -> Decimal`, `estimate_plan(plan, requests, cfg, prices, clip_seconds) -> PlanEstimate` (total, trial count, providers), and `actual(usage, model, prices) -> Decimal`. Pure. -- AD-13.
- [x] `src/consortium/config/models.py`, `src/consortium/templates/study/{prices,study}.yaml` -- Add `ModelPrice.media_tokens_per_s` and `chars_per_token` (decimal strings), and `ModelConfig.fake` (`input_tokens`, `output_tokens`); template's fake Model priced at 0. -- Formulas live in config.
- [x] `src/consortium/board/db.py` -- Append a migration that adds `ledger(trial_id, attempt, model_id, reserved_usd, actual_usd NULL, PK(trial_id, attempt))`, `ceiling_changes(ts, previous_usd, ceiling_usd)`, and a nullable `paused_reason` column on `tests` (1.5). Add `board` reads for the current ceiling and committed spend, and a `set_ceiling` writer op. -- AD-9, AD-13.
- [x] `src/consortium/board/writer.py` -- Extend `begin_attempt(trial_id, usd, ceiling) -> attempt | None` (atomic check, ledger insert and attempt increment; `None` = refused, nothing written); add `record_actual` and `set_paused` ops. -- No overshoot under concurrency.
- [x] `src/consortium/engine/dispatch.py` -- Before each attempt: reserve, and on refusal stop scheduling, drain in-flight attempts and set the pause. After collect: record the actual cost. -- FR18.
- [x] `src/consortium/raters/fake.py` -- Return usage from the `fake` settings. -- Makes the ceiling testable.
- [x] `src/consortium/stages/open.py`, `src/consortium/cli.py` -- Add the `expected`/`worst_case` printout before 1.7's confirmation, ceiling validation and logging, the `ceiling_required` and `over_ceiling` refusals, and `--resume` (1.8) clearing the pause. -- FR14.
- [x] `docs/INTERFACE.md` -- Document the `prices.yaml` fields, the `open` flags and the reason codes.
- [x] `tests/test_cost.py` -- Covers the matrix rows with a FakeRater at non-zero prices, pure `estimate` cases, and a spy asserting no Rater call before confirmation.

**Acceptance Criteria:**
- Given a finished or paused Run, when the ledger is queried, then there is exactly one row per dispatched `(trial_id, attempt)`, and committed spend never exceeds the ceiling at any moment.
- Given one Plan, when `open --dry-run` and `open` run, then they print identical estimates.

## Implementation Notes

- `--ceiling` errors now use `invalid_ceiling` (matrix), replacing 1.6's `bad_ceiling`.
- `ModelPrice.media_tokens_per_s` / `chars_per_token` default to `"300"` / `"4"` (additive: older `prices.yaml` files still load); the template writes them out. `chars_per_token` must be > 0. `ModelConfig.fake` is optional (`input_tokens`/`output_tokens`, default 0).
- Text tokens use the character length of the request's canonical JSON; media seconds count every Clip appearance (Practice + targets). Token counts are rounded up.
- Ledger SQL lives in new `board/ledger.py`; `set_paused`/`paused_reason` in `board/tests.py`. `begin_attempt` (board/trials) takes `usd`, `ceiling` and returns `None` on refusal. Writer op order per attempt is now `begin_attempt, append_request, mark_sent, set_handle, append_response, record_actual, set_state`; the refusal flag is checked inside the writer op, so no attempt starts after a refusal even when several are queued.
- `over_ceiling` applies to a fresh Run only; a resume applies `ceiling_required` but otherwise runs until the engine pauses it (matrix row "Resume, same cap" -> `ceiling_reached`).
- `ceiling_required` is checked before `provider_unavailable`, so it is reachable while only the Fake adapter exists.
- The summary (incl. estimate and ceiling) is printed before the confirmation prompt; after a Run a footer `cost: committed X USD, ceiling Y|none` follows `states:`, and `paused: ceiling` when paused (CLI then exits 1 with `ceiling_reached`).
- FakeRater is now one instance per Model (it carries that Model's usage); they share the provider semaphore.
- `--ceiling` is logged (always appended, even if equal) only after confirmation, under the lease; never for a dry run or a refused/declined open.
- Review follow-ups: an actual cost that pushes committed over the ceiling pauses the Run at once (`ceiling_overshoot` warning); committed spend is a running total on the writer (`board.ledger.Spend`, one SQL aggregate per Run); Clips are prepared only after a successful reservation; `dispatch` requires a `Budget` (`Budget.zero` for zero-cost tests); the pause is stored in a `finally` and cleared on resume only when it did not pause again; uncapped only when every Model is Fake and priced 0; `--ceiling` below committed is `over_ceiling`; a resume with nothing to send writes nothing; dry run shows `would refuse: <code>` and `paused: ceiling`; `ceiling_changes` has `test` and `command` (m4 edited in place, unreleased); `fake` on a non-fake Model is `config_invalid`.

## Spec Change Log

## Review Triage Log

| # | Source | Finding | Verdict | Route |
|---|---|---|---|---|
| 1 | BH, EC | Actual > reserved lets committed spend exceed the ceiling; docs claim it can't | high | patch (pause on overshoot, warn, honest docs). The AC "never exceeds" holds for reservations only; an exact hard cap is impossible while provider cost is only estimated. Kamal informed. |
| 2 | BH, EC | Ledger re-summed per attempt (quadratic) | medium | patch (running total) |
| 3 | BH, EC, VG | Pre-m4 attempts can have no ledger row; corrupt amounts give a traceback | medium | patch |
| 4 | EC | prepare (upload) runs before the reservation | medium | patch |
| 5 | BH | budget=None silently disables cost control | medium | patch (required) |
| 6 | BH, EC | Pause lost on exception; resume clears the pause before dispatch | medium | patch |
| 7 | EC | Non-zero-priced Fake runs uncapped | medium | patch |
| 8 | BH, EC | --ceiling below committed spend accepted | medium | patch (over_ceiling) |
| 9 | BH, EC | Resume with nothing to send writes the ceiling/pause unconfirmed | medium | patch |
| 10 | BH | Dry run doesn't show refusal; pause invisible | medium | patch |
| 11 | BH, EC | Lease re-check reuses providers; second ceiling line | low | patch (test_changed) |
| 12 | BH, EC | Prompt silent on ceiling; not_valid warning dropped on pause | low | patch |
| 13 | BH | ceiling_changes lacks test/command context | low | patch |
| 14 | EC | fake settings on a non-fake Model; schema allows chars_per_token 0 | low | patch |
| 15 | VG | Untested: price change at confirm, spend change at confirm, ceiling_required on resume, v3 migration | medium | patch (tests) |
| 16 | BH | estimate_plan typing; substring purity test; brittle template replace | low | Rejected: test hygiene, no defect |
| 17 | VG | One FakeRater per Model changes the prepare-once scope | low | Rejected: harmless for fake; revisit per adapter in Epic 2 |

## Design Notes

- **Study-wide ceiling.** One researcher budget covers all Tests. Committed spend sums the whole ledger.
- **Pause exits 1 with `ceiling_reached`** so scripts notice. `status` (1.11) reads the stored pause.
- **Per-attempt reservation is the single-attempt estimate,** the same figure `open` checks. The `(1 + max_retries)` worst case is information only; the per-attempt pause is what enforces the ceiling.
- **Committed spend counts at `open`** because the ceiling is study-wide: an earlier Test's spend reduces what a new Run may use.

## Verification

**Commands:**
- `uv run pytest -q tests/test_cost.py` -- expected: all pass
- `uv run pytest -q && uv run ruff check src tests && uv run lint-imports` -- expected: clean
