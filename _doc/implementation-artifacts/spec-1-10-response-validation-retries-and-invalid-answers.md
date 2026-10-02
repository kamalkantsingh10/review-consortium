---
title: 'Story 1.10 — Response validation, retries and invalid answers'
type: 'feature'
created: '2026-10-02'
status: 'done'
baseline_commit: '7b7e28e423ecb0fb290c8bf275ef31c9a796dd30'
route: 'dispatch'
review_loop_iteration: 0
context:
  - '{project-root}/_doc/implementation-artifacts/epic-1-context.md'
  - '{project-root}/_doc/implementation-artifacts/epic-1-code-map.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** After 1.7, a raw response is treated as an answer without any check, so malformed output could silently enter the data.

**Approach:** Parse and validate every raw response in `core.validate` against the Instrument's response schema. A failing Trial stays `sent` and is retried with a new attempt and seed until `max_retries` runs out, then it becomes `invalid`. Record per-attempt validity, and compute the invalid-answer rate per Agent and per Model.

**Decisions (accepted 2026-10-02):**
- Invalid-answer rate = invalid ÷ (valid + invalid). Refused and failed Trials are excluded from it and reported separately as counts.

## Boundaries & Constraints

**Always:**
- **Validation in core (AD-7).** `validate_response(raw, instrument) -> ParsedAnswer` is pure. It accepts exactly one JSON object, optionally wrapped in a single fenced code block. It requires every Item of the Instrument and rejects unknown keys. It checks each value against its Item type: a Likert integer within the scale, pairwise `A` or `B`, non-empty free text. On failure it raises `ConsortiumError("invalid_response", <reason>)`. Adapters never parse.
- **Retries (AD-4).** The Archive gets the raw response first. Then:
  - Valid → the Trial becomes `valid`.
  - Invalid and `attempt <= max_retries` → the Trial stays `sent`. The engine re-dispatches through 1.7's `begin_attempt` (which increments `attempt`) with seed `derive_seed(study_seed, "model", f"{session_id}:{trial_index}:{attempt}")`. The rendered request text is unchanged.
  - Invalid with no retries left → the Trial becomes `invalid`.
  - Total attempts never exceed `1 + max_retries`. `max_retries` is the effective `session.max_retries` (Test override, else `study.yaml`; default 2).
  - **Resume** (refines 1.8): a `sent` Trial whose latest attempt is already recorded `valid = 0` gets a new attempt; its old handle is not collected again.
- Each retry goes through the 1.9 ledger reservation and ceiling check like any attempt. A retry blocked by the ceiling leaves the Trial `sent` for resume.
- Each attempt row stores its seed, `valid` (bool), `invalid_reason` and the parsed answer as canonical JSON. **The answer is the highest valid attempt.** This is a board query, not a stored pointer.
- `FakeRater` gets a per-Model `fake.invalid_rate` (0–1). An attempt is invalid when `Random(seed).random() < invalid_rate`, so the outcome is deterministic per attempt seed. Invalid outputs rotate through non-JSON, a missing Item and an out-of-range value.
- **Invalid-answer rate** = Trials `invalid` ÷ (`valid` + `invalid`) (`None` when 0; `refused`/`failed` not in the denominator, reported as separate counts), defined once as pure `core.validate.invalid_rate(counts_by_state)`. `board.trials.invalid_rates` (per Agent and per Model for a Test) and `status` (1.11) both call it; `export` (1.12) uses `invalid_rates`. Target: `thresholds.invalid_rate_max` (1.2).

**Never:**
- Repairing or coercing answers.
- Retrying refused or failed responses (that is Epic 2).
- Changing request text between attempts.
- Validating inside `raters/`.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Valid Likert | `{"animacy_1":4,...}`, all Items present | `ParsedAnswer`, Trial `valid` | N/A |
| Fenced JSON | ```` ```json {...}``` ```` | Parsed like plain JSON | N/A |
| Not JSON | `"I think 4"` | Retry | `invalid_response: not_json` |
| Out of range | Likert 6 on a 5-point scale | Retry | `invalid_response: out_of_range:<item>` |
| Missing or extra Item | a key missing or unknown | Retry | `invalid_response: missing_item` / `unknown_item` |
| Pairwise bad | `{"choice":"C"}` | Retry | `invalid_response: bad_choice` |
| Recovers | attempt 1 invalid, attempt 2 valid | `valid`, answer = attempt 2, two Archive pairs | N/A |
| Exhausted | 3 invalid attempts, `max_retries` 2 | `invalid`, no 4th dispatch | N/A |
| `max_retries: 0` | first attempt invalid | `invalid` immediately | N/A |
| Two valid | attempts 1 and 3 valid (resume late-collect) | Answer = attempt 3, state `valid` unchanged | N/A |
| Kill mid-retry | killed after the attempt-2 request is archived, before submit | Resume re-dispatches as attempt 3 with a new seed, and never re-uses attempt 2 | N/A |

</frozen-after-approval>

## Code Map

- **As built by Stories 1.7–1.9 (commit 7b7e28e); build on these:**
  - `engine/dispatch.py` requires a `Budget`. The per-attempt sequence is begin_attempt (with ledger reservation and running `Spend`) → append_request → mark_sent → prepare → submit+set_handle (shielded) → collect → append_response(request_sha256) → record_actual (overshoot check) → set_state. A retry is a new attempt through that same sequence, so it reserves cost and can pause at the ceiling.
  - Resume (1.8) collects a `sent` attempt that has a handle. Add the 1.10 rule in `board/trials.load_resumable`: an attempt already recorded `valid = 0` gets a new attempt instead of being re-collected.
  - Today `ok` → `valid` and anything else → `failed` (1.7). 1.10 replaces this with response-schema validation in core.
  - Migrations are at 4; add 5 if you need new columns.
  - Error contract: dispatch unwraps errors to ConsortiumError.
- `src/consortium/config/models.py` (1.2) -- `InstrumentDef`/`ItemDef` give the Item types and scales; `session.max_retries` defaults to 2.
- `src/consortium/engine/dispatch.py`, `board/writer.py`, `board/trials.py` (1.7, 1.9) -- Extended here.

## Tasks & Acceptance

**Execution:**
- [x] `src/consortium/core/validate.py` -- `ParsedAnswer` (frozen: `instrument`, `answers: dict[item_id, int|str]`), `validate_response(raw, instrument)` with stable snake_case reasons, and `invalid_rate(counts)`. -- AD-7.
- [x] `src/consortium/board/db.py`, `board/trials.py` -- Append a migration adding nullable `valid`, `invalid_reason` and `answer_json` to `attempts` (1.7). In `board/trials.py` add `chosen_answer(conn, trial_id)` (the highest valid attempt) and `invalid_rates(conn, test) -> {"by_agent": ..., "by_model": ...}`. -- All SQL stays in `board/`.
- [x] `src/consortium/board/writer.py` -- Add a `record_validation(trial_id, attempt, valid, reason, answer_json)` op. Give the terminal-state op a guard so it never overwrites a terminal state. -- AD-4.
- [x] `src/consortium/engine/dispatch.py` -- After archiving the response: validate, then mark valid, re-queue the Trial for a new attempt, or mark invalid. Re-queued Trials pass through the reservation step. -- FR18.
- [x] `src/consortium/raters/fake.py`, `src/consortium/config/models.py` -- Add `invalid_rate` and the deterministic invalid outputs. -- Testable retries.
- [x] `docs/INTERFACE.md` -- Document `max_retries`, `fake.invalid_rate`, the `invalid_response` reasons, the "highest valid attempt" rule, and the invalid-rate definition.
- [x] `tests/test_validate.py` -- Pure validator cases from the matrix for all three built-in Instruments.
- [x] `tests/test_retries.py` -- Engine runs with the FakeRater: recover, exhaust, `max_retries: 0`, two valid, kill mid-retry, and `invalid_rates` on a seeded run with a known expected count.

**Acceptance Criteria:**
- Given any completed Run, when the Archive is compared with `board.db`, then every attempt row has a matching request and response keyed by `(trial_id, attempt)`, and the seeds differ across attempts of one Trial.
- Given the same `study.seed` and `invalid_rate`, when the Run is repeated in a fresh Study, then the same Trials end up `invalid` with the same attempt counts.

## Implementation Notes

## Spec Change Log

## Review Triage Log

| # | Source | Finding | Verdict | Route |
|---|---|---|---|---|
| 1 | BH | Fake invalid draw shares a random stream with fake_answer, so valid answers are skewed | medium | patch (separate seed) |
| 2 | BH, EC, VG | Abandoned last attempt counted as an invalid answer; a crash changes results | medium | patch: settle as `failed`/attempts_exhausted, excluded from the invalid rate (architect decision), documented |
| 3 | BH | A recorded-valid attempt killed before set_state is re-collected | medium | patch (settle from the board) |
| 4 | BH, EC | Fenced free text containing ``` rejected | medium | patch |
| 5 | EC | Duplicate-key check runs before the not_json/not_object checks | low | patch |
| 6 | BH | bad_choice has no item; pairwise wrong type misreported | low | patch |
| 7 | BH, EC | Likert without points or an unknown item type blamed on the Model | low | patch (programming error) |
| 8 | BH | max_retries default and a bare ValueError in dispatch; docstring step numbers stale | low | patch |
| 9 | BH, EC, VG | invalid_rate schema emits non-standard ge/le | low | patch |
| 10 | BH | Docs overpromise status/export; misleading several-valid statement; check-order wording | low | patch (docs) |
| 11 | EC | Resume summary counts settled Trials as new attempts | low | patch |
| 12 | VG | Untested: resume with a max_retries override, abandoned last attempt, invalid_rate bounds, non-ok category, late-collect retry | medium | patch (tests) |
| 13 | EC | Fake with zero items gives an IndexError | false | The Instrument schema requires ≥1 Item, and render always carries them |
| 14 | EC | Pairwise with empty options | false | The schema requires exactly two distinct options |

## Design Notes

- **Retries are sequential per Trial.** Attempt n+1 is only dispatched after attempt n is collected and found invalid. Several valid attempts therefore arise only when resume late-collects an older handle. The answer query handles that case, and the state stays `valid`.

## Verification

**Commands:**
- `uv run pytest -q tests/test_validate.py tests/test_retries.py` -- expected: all pass
- `uv run pytest -q && uv run ruff check src tests && uv run lint-imports` -- expected: clean
