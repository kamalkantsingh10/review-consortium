---
title: 'Story 1.5 — Push a Test'
type: 'feature'
created: '2026-10-02'
status: 'done'
baseline_commit: '5d44f3657dffe0c8ddcb7bafca79a6d318868306'
route: 'dispatch'
review_loop_iteration: 0
context:
  - '{project-root}/_doc/implementation-artifacts/epic-1-context.md'
  - '{project-root}/_doc/implementation-artifacts/epic-1-code-map.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** A Test that references missing Clips or Instruments, mixes pilot and main Clips, or sends a Model more media than it accepts must be caught before anything is planned or paid for.

**Approach:** `consortium push test <test.yaml>` loads the file with `load_test`. It checks references against `board.db` and the loaded Instruments, enforces `clip_kind_overlap` and the per-Model media limits, registers the Test under its `test:` name, and prints that name. A `kind: main` Test is stored as not openable.

**Decisions (accepted 2026-10-02):**
- Practice clips are declared per Test YAML in a `practice:` list (Clip IDs + intended answer, per Instrument).
- Practice clips are exempt from `clip_kind_overlap`; the same Practice clips may serve pilot and main Tests.
- `session.practice_clips` = the number of Practice examples included per Trial per Instrument. The `practice:` list must supply at least that many for each Instrument the Test uses, else `bad_practice`. Trials use the first `practice_clips` entries per Instrument, in list order.

## Boundaries & Constraints

**Always:**
- First error wins: `ConsortiumError(code, "<field>: <reason>", path=<test file>)`; nothing registered or copied on error.
- Test name matches `^[a-z0-9][a-z0-9_-]*$` (it is embedded in Session IDs).
- Migration `m2_tests` appended to `MIGRATIONS`: `tests(name PK, kind, path, sha256, openable, registered_at)` (`openable` 0 for `main`) and `test_clips(test, clip_id, role target|practice)`.
- A source outside `tests/` is copied to `tests/<name>.yaml`; one already under `tests/` is recorded in place. Never rewritten (AD-3).
- **Practice** (`TestConfig.practice`, 1.2): each Instrument of the Test has at least the effective `session.practice_clips` examples, and the first `practice_clips` of them (list order) are the ones used; every example names an Instrument of the Test; each example's `clips` count fits the Instrument (1, or 2 for pairwise) and its `answer` passes `response_schema()`. Else `bad_practice`, field `practice[i]`.
- **Media check**, every Model in the Test's `MediaLimits`: per Trial = the used Practice clips of that Trial's Instrument + target(s); worst case per Instrument shape (single: longest target; pairwise: two longest distinct targets). Seconds = sum of `duration_s`; bytes = sum of `size_bytes`, or `4*ceil(size/3)` per Clip when `inline_base64` (1.2) is true.
- Durations and sizes come from the `clips` table (1.4); no ffmpeg here.

**Never:**
- Planning, rendering, or `open`; 1.5 only stores `openable` (`protocol_lock_unavailable` is raised by `open`, 1.6/1.7).
- Importing `board/blinding`; pairing plans other than `all_pairs`; screening checks (Epic 3).

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Pilot OK | Pushed Clips, Godspeed + pairwise, `repeats: 3`, `kind: pilot` | Registered with `openable=1`, file at `tests/<name>.yaml`, name printed | N/A |
| Main | Same, `kind: main` | Registered with `openable=0`. stderr notes "not openable until Protocol lock" | N/A |
| Re-push same | Identical bytes, already registered | No-op, prints name | N/A |
| Re-push changed | Same `test:` name, different bytes | Nothing changes | `test_exists` |
| Unknown Clip | `clips: [c_nothere1]` | -- | `unknown_clip`, field `clips[0]` |
| Unknown Instrument | `instruments: [foo]` | -- | `unknown_instrument` (from `load_test`, 1.2) |
| Bad plan | Pairwise Instrument with fewer than 2 targets, duplicate Clip IDs, or `session.pairing: round_robin` | -- | `bad_pairing` |
| Practice as target | The same ID in `practice` and `clips` | -- | `bad_practice`, field `practice` |
| Practice count/answer | Instrument with fewer than `practice_clips` examples, or an answer failing its schema | -- | `bad_practice`, field `practice[i]` |
| Overlap | A target Clip is already a target of a registered Test where one Test is `main` and the other is pilot/screening | -- | `clip_kind_overlap`, naming the Clip and the other Test |
| Too much media | 2 × 120 s Practice + pairwise 2 × 120 s against `m2` `max_seconds: 300` | -- | `media_limit_exceeded: m2 max_seconds 300 < 480 (2 practice + pairwise c_a,c_b)` |
| Base64 bytes | Raw total 7.6 MB and encoded 10.1 MB against a 10 MB inline limit | -- | `media_limit_exceeded`, naming `max_bytes` |
| Bad name | `test: Pilot/1` | -- | `bad_test_name` |

</frozen-after-approval>

## Code Map

- **Already done by Story 1.2 (implemented):** `config.load.load_test` already rejects a Test whose `test:` differs from its file name, practice examples whose Instrument is not in the Test (`unknown_instrument`, field `practice.<i>.instrument`), the wrong clip count (2 for pairwise, else 1), answers failing the response schema (`config_invalid`), and Clip IDs not matching `^c_[a-z2-7]{8}$`. Do not re-implement these checks. `bad_practice` covers only what needs the board: fewer than `session.practice_clips` examples per Instrument, and practice Clip IDs that are not pushed.
- `src/consortium/config/models.py` (1.2) -- `TestConfig` (`test`, `kind`, `instruments`, `models`, `clips`, `practice`, `session` overrides incl. `pairing`, `repeats`), `MediaLimits` (incl. `inline_base64`); use as defined.
- `src/consortium/config/load.py` (1.2) -- `load_test` and `load_instruments` handle the YAML and schema errors.
- `src/consortium/board/db.py`, `board/clips.py` (1.4) -- migration list and Clip lookups.

## Tasks & Acceptance

**Execution:**
- [ ] `src/consortium/board/db.py` -- append `m2_tests`. -- Registration state.
- [ ] `src/consortium/board/tests.py` -- `get_test(conn, name)`, `register_test(conn, row, clips)` (one transaction), and `target_clip_kinds(conn, clip_ids) -> {clip_id: [(test, kind)]}`. -- SQL stays in `board/`.
- [ ] `src/consortium/core/media_limits.py` -- pure `check_media(trial_shapes, clips, models)` returning the first violation or `None`. -- Testable without ffmpeg.
- [ ] `src/consortium/stages/push.py` -- `push_test(study_dir, path) -> str`. Checks name, references, plan, overlap, media; then copies and registers. -- Use case.
- [ ] `src/consortium/cli.py` -- `push test FILE --study`.
- [ ] `docs/INTERFACE.md` -- the Test YAML fields, `push test`, the error codes, and the not-openable rule for `main`.
- [ ] `tests/test_push_test.py` -- every matrix row (Clip rows inserted via `board/clips.py`, no ffmpeg), plus `check_media` unit tests: equal-to-limit passes, base64 rounding.

**Acceptance Criteria:**
- Given any refused push, when `board.db` and `tests/` are inspected, then they are unchanged.
- Given a registered Test, when the Study is reopened, then `get_test` returns its kind, `openable` and SHA-256, and `test_clips` lists every target and Practice clip.
- Given an existing `board.db` at `user_version` 1, when `push test` runs, then `m2` is applied and the version is 2.

## Design Notes

- Overlap counts target Clips only (Practice clips are exempt, by decision), and a Test's kind is read from its registered row. Re-pushing an identical Test therefore never conflicts with itself.

## Verification

**Commands:**
- `uv run pytest -q tests/test_push_test.py tests/test_architecture.py` -- expected: pass
- `uv run ruff check src tests` -- expected: clean
