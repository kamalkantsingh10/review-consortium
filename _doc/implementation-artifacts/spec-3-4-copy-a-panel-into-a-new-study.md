---
title: 'Story 3.4 — Copy a Panel into a new Study'
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

**Problem:** A new Study has to regenerate its Panel and pay for screening again, even when an earlier Study already screened the same Panel.

**Approach:** Add `consortium panel copy --from SOURCE [--study PATH]`. It copies the source's `panel/personas/` byte for byte and imports the source's current screening results into the target `board.db`, recording their source Study and hash. The source is only ever read. Story 3.3's stamp check then decides whether the imported results still count.

## Boundaries & Constraints

**Decisions (Kamal, 2026-10-02):**
- **Frame mismatch is refused.** A target whose `personas` frame differs from the source's is refused with `panel_frame_mismatch`; seeds may differ.

**Always:**
- **Checks, in order. The first error wins, and nothing is written on refusal:**
  1. Target `study.yaml` (`load_study`).
  2. Source `study.yaml`, then the source Panel (`load_personas(source)`; `panel_missing` / `panel_invalid` name the source).
  3. **Frame.** The canonical JSON of `personas` (defaults filled) must be equal in both Studies, else `panel_frame_mismatch`. Seeds may differ: the copied `meta.json` keeps the source seed as provenance.
  4. **Cards.** Every card must equal `render_card(index persona, load_card_wording(target cfg))`, else `panel_mismatch`, the same check `export` makes.
  5. Target `panel_in_use`: the target `board.db` holds a Trial or a screening result.
  6. Target `panel_exists`, after `_sweep_stale`.
- **The source is read only.** File bytes are read once. `board.db` is opened with `read_only(source, ..., allow_older=True)`: no lease and no migration. If the screening tables are absent, there is nothing to import. A newer source layout fails with `board_version_mismatch`. No source file's bytes or mtime change, and no `-wal`/`-shm` file is created when none existed.
- **What is imported.** The source's current (non-superseded) `screening_runs` and their `screening_results`.
  - Runs get target IDs `s1..sn` in source run order, with `superseded_by` NULL. All other fields are copied unchanged, stamps, `detail` and `pair_checks` included.
  - Every imported row gets `source_study`, the source folder as a POSIX path relative to the target folder (never absolute), and one `source_hash`: the SHA-256 of the canonical JSON `{"panel": {file: sha256}, "results": [rows without source columns, source order]}`.
  - Earlier provenance is replaced by the immediate source.
- **Atomic.** The bytes go into a `.personas-new-*` folder under `panel/`. Then, under the target `board.lock` lease (`connect` migrates or creates `board.db`):
  - re-check `panel_in_use` and `panel_exists`;
  - insert runs and results in one transaction;
  - `_move_into_place`;
  - commit.

  If the rename fails, roll back. If the commit fails, remove the moved folder. Afterwards there is either both the Panel and the results, or neither.
- **`panel_in_use` widens.** It is `board.trials.any_trials` or `board.screening.any_results` (any `screening_results` row, imported or not), so `personas generate --force` can't re-label imported results. Screening runs already create Trials, so this only adds the imported case.
- **Output.** stdout gets `copied <N> personas and <R> screening results (<n> runs) from <source_study> -> panel/personas`. With no results, stderr gets `no_screening_results` and the copy still succeeds.

**Never:**
- copying Trials, attempts, the ledger, the Archive, Clips, Tests or `blinding_key.csv`;
- importing superseded runs;
- re-stamping or re-scoring results;
- a new `board.db` migration (`connect` may still bring the target to the current version);
- `stages.panel_copy` importing `stages.personas`.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Happy path | screened source, fresh target | the Panel is byte-identical; current results are imported with source columns; the source is unchanged | N/A |
| Superseded | source has s1 (superseded) and s2 | only s2 is imported, as target s1 | N/A |
| Unscreened source | Panel only, or a pre-Epic-3 `board.db` | Panel copied, 0 results | `no_screening_results` warning |
| Target has a Panel | `panel/personas/` non-empty | nothing written | `panel_exists` |
| Target used | target `board.db` has a Trial or a screening result | nothing written | `panel_in_use` |
| Frame differs | target quotas differ | nothing written | `panel_frame_mismatch` |
| Wording drift | card ≠ re-render | nothing written | `panel_mismatch` |
| No source Panel | source never ran `personas generate` | nothing written | `panel_missing` |
| Target hashes differ | target m1 has a different pinned model | the copy succeeds; a later `open` refuses | `screening_stale` (3.3) |
| Same Model, new m2 | target adds m2 | `*-m2` excluded as missing (3.3) | N/A |

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
- `src/consortium/stages/personas.py` -- `_write`, `_fsync_dir`, `_exists`, `_sweep_stale`, `_move_into_place`, `INDEX_FILE`, `META_FILE`. Move these into a shared module, with no behaviour change (`_meta`, `canonical_index` and `_swap_into_place` stay). `_refuse_if_in_use` uses the widened check.
- `src/consortium/config/load.py` -- `PERSONAS_DIR`, `load_personas`, `load_card_wording`.
- `src/consortium/core/personas.py` -- `render_card`.
- `src/consortium/board/db.py` -- `read_only(allow_older=True)`, `connect`. `src/consortium/board/lease.py` -- `acquire_lease`.
- `src/consortium/board/screening.py` -- 3.1 tables. The source columns and `pair_checks` are already in migration 6; migration 7 (3.3) holds no screening results, so it is not copied.
- `src/consortium/stages/export.py` -- the `panel_mismatch` check to mirror, not import.

## Tasks & Acceptance

**Execution:**
- [ ] `src/consortium/config/panel_files.py` -- new: `INDEX_FILE`, `META_FILE` and the atomic-install helpers `write_file`, `fsync_dir`, `exists`, `sweep_stale`, `move_into_place`, moved from `stages/personas.py`. Layer-legal: `config` sits below `stages` and may import only stdlib and `core` (it is independent of `board`); `config/load.py` already owns `PERSONAS_DIR`. The module reads no YAML and holds no Study logic.
- [ ] `src/consortium/stages/personas.py` -- use the moved helpers. `panel_in_use` = `any_trials` or `any_results` (still `read_only(..., allow_older=True)`).
- [ ] `src/consortium/board/screening.py` -- `current_runs(conn)` (works on a migration-6 layout), `any_results(conn)` (false when the table is absent), and `import_runs(conn, runs, results, source_study, source_hash)`.
- [ ] `src/consortium/stages/panel_copy.py` -- `copy_panel(study_dir, source_dir) -> CopySummary`, following the order and atomicity above.
- [ ] `src/consortium/cli.py` -- the `panel` sub-app with `copy --from`.
- [ ] `docs/INTERFACE.md` -- the command, `source_study` / `source_hash`, the widened `panel_in_use`, the error codes `panel_frame_mismatch` and `panel_mismatch` (with `panel copy` added as a raiser), and the warning `no_screening_results`.
- [ ] `tests/test_panel_copy.py` -- one test per Matrix row; a test of the source tree's bytes, mtimes and absent side files; rollback when the rename fails (monkeypatched); and a copy-then-`open` test with the Fake rater that hits 3.3's stale and eligible paths.

**Acceptance Criteria:**
- Given a copy and an unchanged `models:` / Instruments in the target, when a Test is dry-run in the target, then 3.3 yields the same eligible Agents and exclusion reasons as the source's current results imply.
- Given an imported result, then its `source_hash` recomputes from the target's Panel files and imported rows.
- Given a copy, when `personas generate --force` runs in the target, then it refuses with `panel_in_use`.

## Verification

**Commands:**
- `uv run pytest -q tests/test_panel_copy.py tests/test_personas.py` -- expected: all pass
- `uv run pytest -q && uv run ruff check src tests && uv run lint-imports` -- expected: clean
