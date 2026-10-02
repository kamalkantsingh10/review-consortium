---
title: 'Story 1.11 — Status'
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

- `src/consortium/board/db.py` -- `connect(study_dir, readonly=True)` (1.6), `MIGRATIONS` (1.4); `trials`/`attempts` (1.7), `ledger`, `ceiling_changes`, `tests.paused_reason` (1.9), attempt validity (1.10).
- `src/consortium/core/validate.py` -- `invalid_rate` (1.10).
- `src/consortium/archive/jsonl.py` -- Archive format (1.7); consistency test only.

## Tasks & Acceptance

**Execution:**
- [ ] `src/consortium/board/queries.py` -- `status_counts(conn, test=None)` (grouped by test, model, agent: state counts, `retried`, committed cost) and `cost_footer(conn)` (committed, ceiling, paused state), each in one read transaction. -- Keeps SQL in `board/`.
- [ ] `src/consortium/stages/status.py` -- `status(study_dir, test=None) -> rows`: builds Agent rows, uses `connect(..., readonly=True)` (`None` → empty table), rolls them up to Model and Test totals, adds `invalid_rate`, raises `unknown_test`; plus `format_table(rows, footer)`. -- The use case.
- [ ] `src/consortium/cli.py` -- `status [TEST] [--json] [--study PATH]`. -- Thin adapter.
- [ ] `docs/INTERFACE.md` -- Document `status`, columns, `retried`, `--json`. -- DoD.
- [ ] `tests/test_status.py` -- Every I/O matrix row; mid-run case holds `acquire_lease` and an open write transaction while calling `status`.

**Acceptance Criteria:**
- Given a completed Fake Run with a non-zero invalid rate (1.10), when the test recomputes per-Agent counts from the Archive (request records per Trial → `retried`; highest-attempt response per Trial (last line per key, `read_responses`, 1.8) re-validated with `core.validate.validate_response` → valid/invalid), then they equal `status` counts exactly.
- Given any status row, then `planned + sent + valid + invalid + refused + failed` equals the Trial count for that row, and Model/Test totals equal the sum of their children.
- Given `status` runs, then `board.db` bytes and mtime are unchanged afterwards.

## Implementation Notes

## Spec Change Log

## Review Triage Log

## Design Notes

- **Why `retried` counts extra attempts, not retried Trials:** it makes the Archive reconciliation exact (one request record per attempt) and also counts resume re-dispatches, which do cost money.
- Table via f-strings, no dependency. Footer example: `committed 0.00 / ceiling 5.00   state: ok`.

## Verification

**Commands:**
- `uv run pytest -q tests/test_status.py` -- expected: all pass
- `uv run pytest -q && uv run ruff check src tests && uv run lint-imports` -- expected: clean
