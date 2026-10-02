---
title: 'Story 1.4 — Push Clips blind'
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

**Problem:** Clips must enter the Study without anything on the rating side being able to learn their Condition from the file name, metadata, format or stored state.

**Approach:** `consortium push clip <file> --condition factor=level [...]` re-encodes the file to the `study.yaml` canonical profile with all metadata stripped. It stores the result as `clips/<clip_id>.mp4`, records only neutral facts in `board.db` (created here), writes the Condition only to `blinding_key.csv`, and refreshes the leak report.

**Decisions (accepted 2026-10-02):**
- A Clip may be pushed with no Condition (Practice clips need none). It gets no key rows and is left out of the leak report.

## Boundaries & Constraints

**Always:**
- `blinding_key.csv` is long CSV `clip_id,factor,level`; `read_key` returns `{clip_id: {factor: level}}`.
- `board.db` (WAL), `MIGRATIONS = [m1_clips]`: table `clips(clip_id PK, sha256, duration_s, size_bytes, width, height, fps, loudness_lufs, pushed_at)`. No Condition, no source name.
- Re-encode maps first video + first audio stream only, with `-map_metadata -1 -map_chapters -1`, bitexact flags, `+faststart`; settings come only from `MediaProfile`.
- All-or-nothing: encode to a temp file in `clips/`, insert row, append key, rename; on failure remove temp and roll back.
- Source path and Condition strings never appear in stdout, logs or error messages (errors say "input file").
- `new_clip_id()`: `c_` + 8 lowercase base32 chars from `secrets` (random by design, AD-11); retry on collision.

**Never:**
- Duplicate detection; each push makes a new Clip.
- Any Condition, source filename or source hash in `board.db`.
- Lease or writer task (single synchronous write).
- Python media libraries (ffmpeg/ffprobe subprocesses only).

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| First push | Valid A/V file, `--condition emotion=happy --condition gait=fast`, no `board.db` | `board.db` created. `clips/c_xxxxxxxx.mp4` stored. Key gets 2 rows. Leak report written. Clip ID printed on stdout. | N/A |
| Tagged source | Source has `title`/`comment` tags and a revealing filename | Stored file has no user tags (checked with ffprobe). Neither the tag text nor the filename occurs in the file bytes, `board.db` or captured logs. | N/A |
| No Condition | `push clip f.mp4` with no `--condition` | Stored with no key rows (for example, a Practice clip). Excluded from the leak report. | N/A |
| Bad Condition | `emotion`, `=x`, `emotion=`, or the same factor twice | Nothing stored | `bad_condition` |
| No ffmpeg | `ffmpeg`/`ffprobe` not on PATH, or version < 6 | Nothing stored | `ffmpeg_missing` |
| No audio | Video-only input | Nothing stored | `no_audio` |
| Unreadable | Missing file, or ffmpeg fails to decode it | Nothing stored | `media_unreadable` |
| Leak flag | 2 levels whose mean duration differs by more than the tolerance | Matching row has `flagged=true` | N/A |

</frozen-after-approval>

## Code Map

- `src/consortium/config/models.py` (1.2) -- `StudyConfig.media` (`MediaProfile`) and `thresholds.leak_tolerance` (`duration_s`, `loudness_lufs`; resolution and fps must match exactly). Use these as defined; add no new tolerance model.
- `src/consortium/board/blinding.py` (stub from 1.1) -- now implemented.
- `src/consortium/stages/push.py` -- new. The only stage allowed to import blinding at write time.

## Tasks & Acceptance

**Execution:**
- [ ] `src/consortium/core/ids.py` -- `new_clip_id()` and `session_id(test, agent, repeat)` (`<test>/<agent>/r<repeat>`). -- Shared ID rules.
- [ ] `src/consortium/media/canonicalize.py` -- `ClipInfo` (duration_s, size_bytes, width, height, fps, loudness_lufs, has_audio); `probe` via ffprobe JSON + `ebur128` loudness; `canonicalize` checks ffmpeg ≥ 6 and audio, re-encodes, probes `dst`. -- AD-11.
- [ ] `src/consortium/board/db.py` -- `connect(study_dir)` and `MIGRATIONS` with `m1_clips`. -- State owner.
- [ ] `src/consortium/board/clips.py` -- `insert_clip(conn, clip_id, sha256, info)` and `list_clips(conn, ids=None)`. -- Keeps all SQL in `board/`.
- [ ] `src/consortium/board/blinding.py` -- `append_conditions` (writes the header if the file is new, then fsyncs) and `read_key`. -- AD-2.
- [ ] `src/consortium/stages/push.py` -- `push_clip(study_dir, src, conditions) -> str` and `write_leak_report(study_dir)`. Leak report `exports/leak-report.csv`: columns `factor,metric,level,n,mean,max_diff,tolerance,flagged`, compared across levels within each factor. -- Use case.
- [ ] `src/consortium/cli.py` -- `push clip FILE --condition/-c (repeatable) --study`. -- Thin adapter.
- [ ] `docs/INTERFACE.md` -- Document `push clip`, `clips/`, `board.db`, `blinding_key.csv`, `leak-report.csv` and the error codes.
- [ ] `tests/test_push_clip.py` -- Every matrix row. Fixtures generated by ffmpeg lavfi (`testsrc` + `sine`, ~2 s, 160x120; video-only for no-audio; unique token in tags and filename); skip if ffmpeg absent; missing-ffmpeg via monkeypatched `PATH`.

**Acceptance Criteria:**
- Given two pushes, when `board.db` is opened, then `PRAGMA journal_mode` is `wal`, `user_version` is 1, and only the `clips` table exists.
- Given any failed push, when the Study is inspected, then `clips/`, `board.db` rows and `blinding_key.csv` are unchanged.
- Given the test suite, when it runs, then `test_architecture` still passes, and `stages/push` is the only stage that imports `blinding`.

## Design Notes

- Loudness is measured once at push and stored, so leak-report refreshes never re-decode.

## Verification

**Commands:**
- `uv run pytest -q tests/test_push_clip.py tests/test_architecture.py` -- expected: pass (ffmpeg tests skipped only if ffmpeg is absent)
- `uv run ruff check src tests` -- expected: clean
