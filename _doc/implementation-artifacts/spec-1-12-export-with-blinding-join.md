---
title: 'Story 1.12 — Export with blinding join'
type: 'feature'
created: '2026-10-02'
status: 'done'
baseline_commit: '72fc7b9a1b8da5aa9ac546a79a50d1570240718d'
route: 'dispatch'
review_loop_iteration: 0
context:
  - '{project-root}/_doc/implementation-artifacts/epic-1-context.md'
  - '{project-root}/_doc/implementation-artifacts/epic-1-code-map.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Finished Trials sit in `board.db` without Conditions, so Kamal cannot analyze them in R or Python (FR22).

**Approach:** Add `consortium export TEST`, which refuses unless every Trial is terminal, then writes `exports/<test>.csv`: one tidy row per Item per Trial, versioned schema, with Conditions joined from `blinding_key.csv` at that moment only.

## Boundaries & Constraints

**Always:**
- Read-only, lease-free (AD-3): `connect(study_dir, readonly=True)` (1.6), one read transaction, SQL only in `board/`.
- Refuse with `sessions_running` (naming the count) if any Trial of the Test is `planned` or `sent`; write nothing.
- Conditions come only from `board.blinding.read_key`, called inside `export_test` (AD-2). Never log Condition values.
- Every Trial emits one row per Item, ordered by `session_id`, `trial_index`, Item order. Non-`valid` Trials emit the same rows with `response` empty and `status` set (AD-12).
- `valid` Trials export the highest valid attempt's parsed answer (1.10). Pairwise `response` = the chosen Clip ID (`A` → `clip_ids[0]`, `B` → `clip_ids[1]`).
- Columns, in order: `schema_version`, `agent_id`, `session_id`, `trial_index`, `clip_id`, `pair_id`, `clip_id_a`, `clip_id_b`, `persona_*`, `model`, Condition columns, `instrument`, `item`, `response`, `position`, `repeat`, `seed`, `prompt_variant`, `status`, `excluded`, `exclusion_reason`, `test_kind`, `protocol_lock`, `timestamp`.
  - Single-clip rows fill `clip_id`, leave `pair_id`/`clip_id_a`/`clip_id_b` empty; pairwise rows the reverse (`clip_id_a`/`_b` follow the Trial's ordered `clip_ids`).
  - Condition columns: `<factor>` if the Test has single-clip Trials; `<factor>_a`, `<factor>_b` if it has pairwise Trials; factors sorted by name.
  - `persona_<field>`: one per `Persona` attribute except `id`, in field order, with `big_five` flattened to one column per trait (`persona_openness`, …).
  - `schema_version` = `EXPORT_SCHEMA_VERSION` (1) on every row; `excluded` = `false`, `exclusion_reason` and `protocol_lock` empty (Epic 4 fills them).
  - `seed`, `timestamp` (UTC, `Z`): from the winning attempt, else the last.
- Persona attributes come from `load_personas` (1.3; `panel_missing` if absent); each `render_card(p, load_card_wording())` must equal its stored `panel/personas/<id>.md`, else `panel_mismatch`.
- CSV: UTF-8, `\n` line endings, stdlib `csv`; write `.tmp` then `os.replace`. Same state ⇒ identical bytes.
- stdout: the written path. stderr (logging): invalid rate per Model, and each Agent above `thresholds.invalid_rate_max`, both from `board.trials.invalid_rates` (1.10).

**Never:**
- Export Practice clips or their intended answers in any column.
- Compute exclusions or statistics (Epic 4 / out of scope).
- Write anything but the CSV; mix Tests in one file.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Finished single-clip Test | All Trials terminal | `exports/pilot1.csv`, path on stdout, exit 0 | N/A |
| Finished pairwise Test | Pairwise Instrument | `pair_id`, `clip_id_a/b`, `<factor>_a/_b` filled; `clip_id` empty; `response` ∈ {`clip_id_a`, `clip_id_b`} | N/A |
| Mixed outcomes | Some `invalid`/`refused`/`failed` | Their rows present, `response` empty, `status` set | N/A |
| Still running / paused | Any Trial `planned` or `sent` | No file written | `sessions_running: 12 Trials not terminal`, exit 1 |
| Never opened | Test pushed, no Trials planned | No file | `nothing_to_export: <test>`, exit 1 |
| Unknown Test | `export nope` | No file | `unknown_test: nope`, exit 1 |
| Clip missing from key | A target `clip_id` absent from `blinding_key.csv` (or file absent) | No file | `blinding_key_missing: <clip_id>`, exit 1 |
| Factor name clash | A factor equals a fixed column name (e.g. `model`) | No file | `condition_name_clash: model`, exit 1 |
| Panel edited | Stored card ≠ regenerated card | No file | `panel_mismatch: panel/personas/p3.md`, exit 1 |
| Re-export | Same state, run twice | Identical bytes; old file replaced | N/A |
| Other Test dispatching | `board.lock` held | Export succeeds | N/A |

</frozen-after-approval>

## Code Map

- **As built by Stories 1.4–1.11 (commit 72fc7b9); use these, don't duplicate:**
  - Read-only access goes through `board.db.read_only(study_dir, fn)`. `board/queries.read_transaction` and `registered_tests` exist.
  - `board/trials.chosen_answer(conn, trial_id)` returns the highest valid attempt's stored `answer_json`. `attempts.valid`, `invalid_reason` and `answer_json` exist (migration 5).
  - `board/trials.invalid_rates(conn, test)` and `core/validate.invalid_rate` are the single invalid-rate definition. A failed Trial may have category `attempts_exhausted`.
  - Terminal states: valid, invalid, refused, failed. Export refuses while any Trial is planned or sent (`sessions_running`).
  - `board/blinding.read_key` is importable only from stages/push and stages/export (AST test). `config.load.load_personas` reads index.json; `meta.json` holds provenance.
  - Practice examples come from the Test YAML `practice:` list and are never exported.
  - Status uses natural ID order (p2 before p10); export may reuse it.
- `src/consortium/board/blinding.py` -- `read_key` (1.4): `clip_id` → `{factor: level}`.
- `src/consortium/board/db.py` -- `connect(study_dir, readonly=True)` (1.6); `trials` (1.7), `attempts` (1.7, + `valid`/`answer_json` 1.10), `tests.kind` (1.5).
- `src/consortium/board/trials.py` -- `chosen_answer`, `invalid_rates` (1.10).
- `src/consortium/config/load.py`, `src/consortium/core/personas.py` -- Test, Instruments, `load_personas`, `load_card_wording`, `render_card`.

## Tasks & Acceptance

**Execution:**
- [x] `src/consortium/board/queries.py` -- Add `export_trials(conn, test)`: each Trial with state, Trial fields and exported attempt's answer, seed, timestamp. -- SQL stays in `board/`.
- [x] `src/consortium/stages/export.py` -- `EXPORT_SCHEMA_VERSION = 1`; `export_test(study_dir, test) -> Path` implementing the rules and errors above; logs invalid rates. -- The use case.
- [x] `src/consortium/cli.py` -- `export TEST [--study PATH]`. -- Thin adapter.
- [x] `docs/INTERFACE.md` -- Document `export`, every column, schema version 1, empty-response semantics. -- DoD.
- [x] `tests/test_export.py` -- Every I/O matrix row, Fake rater with invalid rate > 0; refused/failed states seeded directly.

**Acceptance Criteria:**
- Given a finished Fake Test, then the set of `(session_id, trial_index)` in the CSV equals the set of Trials in `board.db`, and row count = Σ Items per Trial.
- Given a finished Test, then no Practice clip ID or Practice intended answer appears anywhere in the CSV.
- Given a spy on `read_key`, then it is called once per successful export and never on a refused one.
- Given an export, then `board.db`, `archive/` and `blinding_key.csv` are byte-unchanged.

## Implementation Notes

## Spec Change Log

## Review Triage Log

| # | Source | Finding | Verdict | Route |
|---|---|---|---|---|
| 1 | BH, EC | A target Clip with no Condition blocks the whole export | medium | patch: empty Condition cells (architect decision) |
| 2 | EC | Items edited after the Run: answers silently dropped or board_unreadable | medium | patch (instrument_changed) |
| 3 | EC | Null/bool/float values exported as strings; bad clip_ids length gives IndexError | low | patch |
| 4 | EC | Missing attempt row exports empty seed/timestamp silently | low | patch |
| 5 | BH, EC | Fixed temp name; no directory fsync | low | patch |
| 6 | BH | Persona columns walked twice; column list rebuilt per Trial | low | patch |
| 7 | BH, EC | Error table incomplete; read_key order claim overstated | low | patch (docs) |
| 8 | BH | CSV formula injection from free text | low | patch (documented; data kept verbatim for analysis fidelity) |
| 9 | VG | Non-valid seed attempt, answered_at, no-attempt, uneven factors, repeat/variant, threshold negative untested | medium | patch (tests) |
| 10 | BH | Zero-Item Instrument drops its Trials | false | The Instrument schema requires ≥1 Item |
| 11 | BH | Two queries per Trial; invalid_rates before refusals | low | Rejected: performance only at pilot scale |
| 12 | BH | Diff missing spec files | false | Planning files are intentionally excluded from the code diff |

## Design Notes

- **Schema version as a column, not a sidecar:** it travels with the CSV when copied.
- **Per-Item rows for non-valid Trials:** a complete Trial × Item grid is the tidy shape R expects and still gives ≥ 1 row per Trial.

## Verification

**Commands:**
- `uv run pytest -q tests/test_export.py` -- expected: all pass
- `uv run pytest -q && uv run ruff check src tests && uv run lint-imports` -- expected: clean
