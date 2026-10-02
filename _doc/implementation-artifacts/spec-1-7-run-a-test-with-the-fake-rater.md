---
title: 'Story 1.7 — Run a Test with the Fake rater'
type: 'feature'
created: '2026-10-02'
status: 'done'
baseline_commit: 'a516d8bfb4fdbc8c50643c6b775a75740ce92a29'
route: 'dispatch'
review_loop_iteration: 0
context:
  - '{project-root}/_doc/implementation-artifacts/epic-1-context.md'
  - '{project-root}/_doc/implementation-artifacts/epic-1-code-map.md'
  - '{project-root}/_doc/planning-artifacts/architecture/architecture-review-consortium-2026-10-02/ARCHITECTURE-SPINE.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Planned Trials (1.6) can't run; the one execution path (engine, Rater port, writer, lease, Archive) must be proven end to end, offline and free.

**Approach:** `open <test>` confirms, persists the plan, and `engine.dispatch` sends every Trial through the `Rater` port to a deterministic `FakeRater`, Archive before state.

**Decisions:**
- Split 2026-10-02: resume, kill-and-resume, duplicate responses and the re-render check moved to Story 1.8.

## Boundaries & Constraints

**Always:**
- **Open:** refuse `main` (`protocol_lock_unavailable`); acquire the lease; confirm (`Run N Trials on <providers>? [y/N]`, skipped by `--yes`; declined → `not_confirmed`; stdin not a TTY → `confirmation_required`; nothing written). Then insert every planned Trial as `planned` in one transaction and dispatch. A Test that already has Trials is refused (`test_already_open`).
- **Lease:** `acquire_lease` takes `fcntl.flock(LOCK_EX | LOCK_NB)` on `board.lock` for the whole command; failure → `study_busy`. The OS releases it when the process dies.
- **Single writer:** one asyncio task owns the `sqlite3` connection and does every `board.db` write and Archive append, in queue order.
- **Per attempt, in this order:** (1) one writer op `begin_attempt` increments `attempt` and records the attempt with seed `derive_seed(study.seed, "model", f"{session_id}:{trial_index}:{attempt}")`; (2) writer appends the request record; (3) writer marks the Trial `sent`; (4) `rater.submit` → writer stores the handle; (5) `rater.collect` → writer appends the response record; (6) writer sets the terminal state. Each `(trial_id, attempt)` is dispatched at most once.
- **Archive records** are canonical JSON lines keyed by `trial_id` + `attempt`: request = `{trial_id, attempt, seed, model_id, request, request_sha256, ts}`; response = `{trial_id, attempt, raw, usage, model_build, category, ts}`. Media appears only as `clip_id` + SHA-256.
- **Rater port:** `prepare` once per Clip per Rater (cached); `submit` and `collect` awaited separately (never assume a synchronous answer); one `asyncio.Semaphore` per provider, sized by `StudyConfig.concurrency` (1.2).
- **State:** in 1.7, `category == "ok"` → `valid`; any other category → `failed`. Terminal states never change.
- **FakeRater:** answers each Item in its scale (pairwise: a position; free text: a fixed string) from `random.Random(seed)`, as canonical JSON matching the Instrument's response schema; `usage` `{input_tokens: 0, output_tokens: 0}`; `model_build` `"fake-1"`; no network. Its handle carries the answer itself, so `collect` works after a restart.

**Never:** `--resume` or any resume logic (1.8); cost estimate, ledger, ceiling or pause (1.9); response validation or retries (1.10); `status` (1.11); any network; any other path that calls a Rater.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Happy run | pilot Test, Fake Model, `--yes` | all Trials `valid`, `attempt` 1; one request and one response line per Trial; stdout counts by state; exit 0 | N/A |
| Declined | no `--yes`, answer `n` | no Trials, no Archive | `not_confirmed`, exit 1 |
| No TTY | no `--yes`, stdin not a TTY | no Trials, no Archive | `confirmation_required`, exit 1 |
| Second dispatcher | lease held by another process | nothing written | `study_busy`, exit 1 |
| Re-open | Test already has Trials | nothing written | `test_already_open`, exit 1 |
| Main Test | `kind: main` | nothing | `protocol_lock_unavailable`, exit 1 |

</frozen-after-approval>

## Code Map

- **As built by Story 1.6 (commit a516d8b); build on these, don't redo them:**
  - `stages/open.plan_and_render` returns `(plan, requests_generator, instrument_order)`, and requests render lazily in plan order. `open_test` streams a `requests sha256:` digest over the canonical JSON and prints it. The real Run must dispatch exactly the requests that produce that digest.
  - `open` already re-runs the Test checks against the current config (`core/test_checks.py`), parses `--ceiling` (`bad_ceiling`), and refuses main Tests with `protocol_lock_unavailable`.
  - `board.db.read_only(study_dir, fn)` is the read-only path; `connect(study_dir)` is the writer path. SQLite errors map to `board_unreadable` or `board_busy`.
- `core/plan.py`, `core/render.py` (1.6) -- `Trial`, `render`, `canonical_json`.
- `stages/open.py` (1.6) -- replace `run_unavailable` with the Run.
- `board/db.py` (1.4) -- `MIGRATIONS` (append only).

## Tasks & Acceptance

**Execution:**
- [x] `src/consortium/board/db.py` -- append one migration: `trials` (all `Trial` fields, `test`, `state`, `attempt`) and `attempts` (`trial_id`, `attempt`, `seed`, `handle`, `sent_at`, `answered_at`, `category`; PK `(trial_id, attempt)`).
- [x] `src/consortium/board/trials.py` -- insert plan, load Trials, next attempt, mark sent, set handle, set state.
- [x] `src/consortium/board/writer.py` -- writer task over an `asyncio.Queue`; owns the connection and Archive appends.
- [x] `src/consortium/board/lease.py` -- `acquire_lease(study_dir)`.
- [x] `src/consortium/archive/jsonl.py` -- `append_request`, `append_response` (append + flush + fsync).
- [x] `src/consortium/raters/base.py` -- `Rater` Protocol, `MediaRef`, `Handle` (JSON-serialisable, stored as text in `attempts.handle`), `RaterResult(raw, usage, model_build, category)`, and `RaterCall(trial_id, attempt, seed, request, media)` as `submit`'s input.
- [x] `src/consortium/raters/fake.py` -- `FakeRater`.
- [x] `src/consortium/engine/dispatch.py` -- `async dispatch(study_dir, trials, rater_by_model, *, writer)`, `trials` = list of `(Trial, TrialRequest)`.
- [x] `src/consortium/stages/open.py`, `src/consortium/cli.py` -- Run flow; confirmation prompt; a `provider → Rater` factory that knows only `fake` (other providers → `provider_unavailable`).
- [x] `docs/INTERFACE.md` -- `open`, `--yes`, `board.lock`, Archive record formats, Trial states.
- [x] `tests/test_dispatch.py`, `tests/test_lease.py`, `tests/test_fake.py` -- every matrix row; per-attempt order via a writer-queue spy.

**Acceptance Criteria:**
- Given a completed Fake Run, when the Archive and `board.db` are compared, then every attempt has exactly one request line and one response line, and every request line precedes its Trial's `sent` write.
- Given the code, then only `engine/dispatch.py` calls `submit`/`collect`, and within `open` only `board/writer.py` writes to `board.db`.
- Given the Archive and `board.db`, when searched for Condition values or source filenames, then none appear.

## Implementation Notes

## Spec Change Log

## Review Triage Log

| # | Source | Finding | Verdict | Route |
|---|---|---|---|---|
| 1 | BH, EC, VG | TaskGroup ExceptionGroup escapes the CLI (traceback, not `code: message`); failure path untested | high | patch (unwrap + run_failed + test) |
| 2 | BH | Cancel between append_request and mark_sent leaves an undocumented state | medium | patch (document for 1.8) |
| 3 | EC | Handle lost if cancelled after submit, before set_handle | medium | patch (shield) |
| 4 | EC, BH | Wrong-length adapter result, cached failed prepare, concurrency < 1, missing Rater | low | patch |
| 5 | BH, EC, VG | A Run can't migrate an older board.db | medium | patch (migrate under the lease) |
| 6 | BH, EC | Prompt held while leased; planning outside the lease; digest not shown at confirm | medium | patch |
| 7 | BH, EC | EOF/Ctrl-C prints "Aborted!" | low | patch (not_confirmed) |
| 8 | BH | Exit status with failed Trials undefined | low | patch (warning, exit 0) |
| 9 | EC | --dry-run --resume now refused | low | patch |
| 10 | BH, EC | Partial last Archive line merges; archive dir not fsynced | medium | patch |
| 11 | BH | Two timestamp precisions; non-canonical clip_ids; response not linked to request | low | patch |
| 12 | BH | No attempts index | low | patch |
| 13 | VG | Response raw never checked against request and seed | medium | patch (test) |
| 14 | BH | Untested: provider_unavailable lock, --yes with TTY, dry run on an open Test | low | patch (tests) |
| 15 | BH | Foreign keys not enforced | false | connect() sets PRAGMA foreign_keys=ON (story 1.5) |
| 16 | EC | Empty plan reopenable | false | open's Test checks require ≥1 target and Personas/Models, so a plan can't be empty |
| 17 | EC | Fake likert with points None | false | The schema requires points on Likert items |
| 18 | BH | Run holds all requests in memory | low | Rejected: fine at pilot scale; revisit with batch adapters |
| 19 | EC | asyncio.run inside a running loop | low | Rejected: CLI-only entry point |
| 20 | BH | writer_spy public hook; brittle substring test | low | Rejected: test-only, harmless |
| 21 | BH | Story diff missing the spec | false | Planning files are intentionally excluded from the code diff |

## Design Notes

- **Plan persisted after confirmation,** so a declined Run writes nothing and resume (1.8) never re-plans.
- **Archive appends go through the writer,** so ordering comes from the queue, not locks.
- **Seams:** 1.8 adds resume; 1.9's reservation joins `begin_attempt` (same transaction); 1.10's validation replaces `ok → valid` at step 6.

## Verification

**Commands:**
- `uv run pytest -q && uv run ruff check src tests && uv run lint-imports` -- expected: all pass, no network
- `consortium open example --yes --study <fresh study>` -- expected: all Trials `valid`
