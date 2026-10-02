---
title: 'Story 1.11 — Status'
type: 'feature'
created: '2026-10-02'
status: 'done'
baseline_commit: '5b6c4f8cd93fac21eaa78463b03655c7a314b9d9'
route: 'dispatch'
review_loop_iteration: 0
context:
  - '{project-root}/_doc/implementation-artifacts/epic-1-context.md'
  - '{project-root}/_doc/implementation-artifacts/epic-1-code-map.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Kamal cannot see what a Run has done, failed, retried or cost without opening `board.db` by hand (FR21).

**Approach:** Add `consortium status [TEST]`, which reads `board.db` read-only (no lease) and prints per Test, Model and Agent the Trial counts by state, retries, invalid rate and cost so far, plus a footer with committed spend against the ceiling and the paused state.

## Boundaries & Constraints

**Always:**
- Read-only, lease-free (AD-3): no `acquire_lease`, no migrations, no writes; `connect(study_dir, readonly=True)` (1.6; `mode=ro` over WAL), so it works mid-run. SQL only in `board/`.
- Row columns: `test`, `model`, `agent`, `planned`, `sent` (in flight), `valid`, `invalid`, `refused`, `failed`, `retried`, `invalid_rate`, `cost_usd`. Rows: Test total (`model`, `agent` = `*`), Model total (`agent` = `*`), each Agent; sorted.
- `retried` = attempts beyond the first, summed (`Σ max(attempt − 1, 0)`), so `requests.jsonl` records per Agent = Trials with ≥ 1 attempt + `retried`.
- `invalid_rate` = `core.validate.invalid_rate(counts)` (1.10's single definition: invalid ÷ (valid + invalid); `refused` and `failed` stay separate columns) applied to each row's counts; empty when it returns `None`.
- `cost_usd` = ledger committed spend (actual, else reserved), decimal string.
- Footer (study-wide): `committed <usd> / ceiling <usd|none>` and `state: paused: ceiling` when any Test has a `tests.paused_reason` (1.9), else `state: ok`.
- Data to stdout as a fixed-width table; `--json` prints `{"rows":[…],"committed","ceiling","state"}` instead.

**Never:**
- Import `consortium.board.blinding` or show any Condition breakdown (AD-2; the 1.1 AST test enforces it).
- Read the Archive at runtime; watch mode, colours, status page (FR23).

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Completed Run | `status` after a Fake Run | Table with Test, Model, Agent rows; footer; exit 0 | N/A |
| One Test | `status pilot1` | Only `pilot1` rows; footer still study-wide | N/A |
| Mid-run | Another process holds `board.lock` and is writing | Current counts incl. `sent` > 0; no `study_busy` | N/A |
| Paused | Story 1.9 pause recorded | Footer shows `state: paused: ceiling` | N/A |
| No ceiling set | Ledger empty / ceiling null | `ceiling none`, `cost_usd` `0` | N/A |
| Unknown Test | `status nope` | Nothing printed to stdout | stderr `unknown_test: nope`, exit 1 |
| No board yet | Fresh `init` folder, no `board.db` | Empty table (header only) and footer `committed 0 / ceiling none`, exit 0 | N/A |
| Board schema newer/older | `user_version` ≠ `len(MIGRATIONS)` | Nothing printed | stderr `board_version_mismatch`, exit 1 |

</frozen-after-approval>

## Code Map

- **As built by Stories 1.6–1.10 (commit 5b6c4f8); use these, don't duplicate:**
  - Read-only access goes through `board.db.read_only(study_dir, fn)` (it re-reads if a writer starts, and maps errors to `board_unreadable`/`board_busy`). It returns None when there is no board.db.
  - `board/trials.invalid_rates(conn, test)` already returns valid/invalid/refused/failed counts plus `rate` per Agent and per Model; `core/validate.invalid_rate(counts)` is the single rate formula (invalid / (valid + invalid)).
  - Committed spend and the ceiling come from `board/ledger.py`. The pause comes from `board/tests.paused_reason`.
  - Trial states: planned, sent, valid, invalid, refused, failed. `attempts_exhausted` is a failed category.
  - Migrations are at 5.
- `src/consortium/board/db.py` -- `connect(study_dir, readonly=True)` (1.6), `MIGRATIONS` (1.4); `trials`/`attempts` (1.7), `ledger`, `ceiling_changes`, `tests.paused_reason` (1.9), attempt validity (1.10).
- `src/consortium/core/validate.py` -- `invalid_rate` (1.10).
- `src/consortium/archive/jsonl.py` -- Archive format (1.7); consistency test only.

## Tasks & Acceptance

**Execution:**
- [x] `src/consortium/board/queries.py` -- `status_counts(conn, test=None)` (grouped by test, model, agent: state counts, `retried`, committed cost) and `cost_footer(conn)` (committed, ceiling, paused state), each in one read transaction. -- Keeps SQL in `board/`.
- [x] `src/consortium/stages/status.py` -- `status(study_dir, test=None) -> rows`: builds Agent rows, uses `connect(..., readonly=True)` (`None` → empty table), rolls them up to Model and Test totals, adds `invalid_rate`, raises `unknown_test`; plus `format_table(rows, footer)`. -- The use case.
- [x] `src/consortium/cli.py` -- `status [TEST] [--json] [--study PATH]`. -- Thin adapter.
- [x] `docs/INTERFACE.md` -- Document `status`, columns, `retried`, `--json`. -- DoD.
- [x] `tests/test_status.py` -- Every I/O matrix row; mid-run case holds `acquire_lease` and an open write transaction while calling `status`.

**Acceptance Criteria:**
- Given a completed Fake Run with a non-zero invalid rate (1.10), when the test recomputes per-Agent counts from the Archive (request records per Trial → `retried`; highest-attempt response per Trial (last line per key, `read_responses`, 1.8) re-validated with `core.validate.validate_response` → valid/invalid), then they equal `status` counts exactly.
- Given any status row, then `planned + sent + valid + invalid + refused + failed` equals the Trial count for that row, and Model/Test totals equal the sum of their children.
- Given `status` runs, then `board.db` bytes and mtime are unchanged afterwards.

## Implementation Notes

- `status()` returns a `StatusReport` (`rows` plus the Study-wide `committed`, `ceiling`, `state`; `footer()`, `to_json()`) rather than bare rows, so rows and footer come from one read snapshot (`board.queries.read_transaction` around `status_counts` and `cost_footer` inside one `board.db.read_only` call). `format_table(rows, footer)` is as specified.
- `board/ledger.py` gained `register_decimal_sum` and `summed` (factored out of `committed_usd`) so per-row cost uses the same exact decimal sum.
- USD strings use `core.cost.usd` (trailing zeros dropped: `5.00` prints `5`), matching the I/O matrix's `committed 0 / ceiling none` and INTERFACE.md's USD convention rather than the `0.00` in the Design Notes example.
- Rows are sorted with totals (`*`) first and IDs in natural order (`p2-m1` before `p10-m1`).
- Review round 1 (coordinator): footer `state: ok` or `state: paused <test> (<reason>)[, ...]` listing every paused Test (supersedes the frozen `state: paused: ceiling` wording); JSON adds `schema_version: 1`, `test` (the filter), `state` `ok`|`paused` and `paused: [{test, reason}]`; new `trials` and `paused` columns (`paused` set on Test-total rows only); every registered Test gets a Test-total row, zeros if never opened; `cost_footer` uses `ledger.committed_usd`; `read_transaction` rolls back on error and commits only on success.

## Spec Change Log

## Review Triage Log

| # | Source | Finding | Verdict | Route |
|---|---|---|---|---|
| 1 | BH, EC | Footer hides which Test is paused; state takes undocumented values | medium | patch |
| 2 | BH | Unopened registered Tests invisible | medium | patch |
| 3 | BH | trials count computed but dropped | low | patch |
| 4 | BH | cost_footer duplicates committed_usd | low | patch |
| 5 | BH | read_transaction commits on failure | low | patch |
| 6 | BH | --json has no schema_version or filter echo | low | patch |
| 7 | BH, EC | retried reconciliation claim false after a crash; test collapses duplicate lines | low | patch (docs + raw-line test) |
| 8 | BH | Docs example single-Model; board_busy row wording | low | patch (docs) |
| 9 | VG | Table text format (4 decimals, alignment) untested | low | patch (test) |
| 10 | BH | board_busy path untested | low | Rejected: same read_only path, covered in story 1.6 tests |
| 11 | BH | immutable re-read path untested here | low | Rejected: covered by the read_only tests in story 1.6 |
| 12 | BH | No compact --totals view | low | Rejected: feature, not a defect; revisit if large Studies need it |
| 13 | EC | Orphan ledger rows break the cost roll-up | false | connect() enforces foreign keys (PRAGMA foreign_keys=ON) |

## Design Notes

- **Why `retried` counts extra attempts, not retried Trials:** it makes the Archive reconciliation exact (one request record per attempt) and also counts resume re-dispatches, which do cost money.
- Table via f-strings, no dependency. Footer example: `committed 0.00 / ceiling 5.00   state: ok`.

## Verification

**Commands:**
- `uv run pytest -q tests/test_status.py` -- expected: all pass
- `uv run pytest -q && uv run ruff check src tests && uv run lint-imports` -- expected: clean
