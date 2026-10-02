---
title: 'Story 1.8 — Resume a Run and verify re-issue'
type: 'feature'
created: '2026-10-02'
status: 'done'
baseline_commit: '2277f21287757dc666859be32964d30be4b8b1d6'
route: 'dispatch'
review_loop_iteration: 0
context:
  - '{project-root}/_doc/implementation-artifacts/epic-1-context.md'
  - '{project-root}/_doc/implementation-artifacts/epic-1-code-map.md'
  - '{project-root}/_doc/planning-artifacts/architecture/architecture-review-consortium-2026-10-02/ARCHITECTURE-SPINE.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** After 1.7, a killed Run cannot continue (`open` refuses with `test_already_open`), and nothing proves the Archive can re-issue its requests byte-identically (FR18, FR24, NFR6).

**Approach:** Add `open <test> --resume`, which re-renders the stored Trials and continues at Trial level through the same `engine.dispatch`, never re-sending a completed Trial. Prove re-issue by re-rendering every archived request from its Trial row plus the Study folder.

**Decisions:**
- Split out of Story 1.7 (2026-10-02). The resume rules, kill-and-resume tests, the duplicate-response rule and the re-render check live here.

## Boundaries & Constraints

**Always:**
- **Resume entry:** `open <test> --resume` refuses `main` (`protocol_lock_unavailable`), acquires the lease (`study_busy`), requires existing Trials (`test_not_open`), and never re-plans. Each non-terminal Trial is re-rendered from its stored row plus the Study folder with `core.render`. Confirmation works as in 1.7 (`--yes`, `not_confirmed`, `confirmation_required`), counting only non-terminal Trials.
- **Per Trial state on resume:**
  - terminal → untouched, never re-sent;
  - `sent` with a stored handle → `collect` at the same attempt (no new request line);
  - `planned`, or `sent` without a handle → a new attempt through 1.7's `begin_attempt` (new seed, new request line).
  - 1.10 adds one rule: a `sent` Trial whose latest attempt is already recorded `valid = 0` gets a new attempt, and its old handle is not collected again.
- **Duplicate responses:** if a Run dies after the response append but before the state write, resume collects again and appends a second line for the same `(trial_id, attempt)`. This is allowed; every reader takes the **last line per key**.
- **No orphans:** an attempt created by `begin_attempt` but never marked `sent` is never collected; the Trial gets a new attempt. Attempt numbers are never reused.
- **Lease after a kill:** the OS releases `flock`, so a killed Run never blocks `--resume`.
- **Re-issue check:** for every request line, `canonical_json(render(...))` from the stored Trial row and the current Study folder equals the archived `request` bytes, and its SHA-256 equals `request_sha256`.

**Never:** cost, ceiling or pause handling (1.9; `--resume --ceiling` arrives there); validation or retries (1.10); a new CLI command; re-planning or re-ordering Trials; any network.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Resume after kill | mix of `valid`, `planned`, `sent` with handle, `sent` without handle | `valid` untouched; handle → collected at same attempt; `planned` and handle-less `sent` → new attempt; all end `valid` | N/A |
| Killed after response append | response archived, state still `sent` | resume collects again; a duplicate identical response line is allowed; readers take the last line per key | N/A |
| Killed after `begin_attempt` | attempt row exists, no request line, Trial `planned` | new attempt (n+1); attempt n never sent | N/A |
| All terminal | completed Test, `--resume` | nothing dispatched; counts printed; exit 0 | N/A |
| Resume, nothing open | no Trials for Test | nothing | `test_not_open`, exit 1 |
| Resume, busy | lease held elsewhere | nothing written | `study_busy`, exit 1 |
| Re-render | completed Fake Run | every archived request byte-identical to its re-render | N/A |

</frozen-after-approval>

## Code Map

- **As built by Story 1.7 (commit 2277f21); build on these:**
  - `docs/INTERFACE.md` already defines the resume contract for every state a stopped Run can leave, including **`planned` with attempt ≥ 1** (re-dispatch with a new attempt; the request may or may not be archived). Implement exactly that contract.
  - `--resume` currently raises `resume_unavailable` for a Run; replace that.
  - Each response record carries `request_sha256`.
  - A Run plans twice (before the prompt, and under the lease) and compares digests (`test_changed`).
  - `engine/dispatch.py` shields submit+set_handle, unwraps errors to ConsortiumError (`run_failed`/`adapter_error`), and keeps the writer alive until shielded tasks finish.
  - `board.writer.migrate()` upgrades old layouts.
  - Timestamps come from `core/clock.utc_now_ms()`.
- `src/consortium/engine/dispatch.py`, `board/writer.py`, `board/trials.py`, `board/lease.py`, `archive/jsonl.py` (1.7) -- extended here.
- `src/consortium/stages/open.py`, `cli.py` (1.6, 1.7) -- `--resume` flag already in the signature.
- `src/consortium/core/render.py` (1.6) -- `render`, `canonical_json`.

## Tasks & Acceptance

**Execution:**
- [x] `src/consortium/board/trials.py` -- load non-terminal Trials with their latest attempt and handle. -- Resume input.
- [x] `src/consortium/archive/jsonl.py` -- `read_requests(study_dir)` and `read_responses(study_dir)`, each keyed by `(trial_id, attempt)` with last-line-wins. -- Single reader rule.
- [x] `src/consortium/engine/dispatch.py` -- accept resumed Trials: collect-at-same-attempt for handled `sent`, new attempt otherwise. -- FR18.
- [x] `src/consortium/stages/open.py`, `src/consortium/cli.py` -- `--resume` flow: refusals, re-render from stored rows, confirmation, dispatch. -- Use case.
- [x] `docs/INTERFACE.md` -- `--resume`, the resume rules, last-line-wins. -- Definition of done.
- [x] `tests/test_resume.py` -- every matrix row; kill simulated by stopping dispatch after a chosen writer op (each op in turn), then resuming.

**Acceptance Criteria:**
- Given a Run killed after any writer op and then resumed, when the Archive and `board.db` are compared, then every `sent` attempt has a request line, every terminal Trial has a response line for its final attempt, and no completed Trial got a new attempt.

## Implementation Notes

## Spec Change Log

## Review Triage Log

| # | Source | Finding | Verdict | Route |
|---|---|---|---|---|
| 1 | BH, EC | A sent attempt with no request line passes re-issue | medium | patch |
| 2 | BH | Duplicate request lines hidden by last-wins | medium | patch |
| 3 | BH, EC | Mid-file corruption silently skipped; bool attempt | medium | patch (archive_corrupt; fragments counted) |
| 4 | EC | Removed Model gives a KeyError; terminal-only Models block resume | medium | patch (unknown_model) |
| 5 | EC | Test edit between confirm and lease reports test_exists, not test_changed | low | patch |
| 6 | EC | Unreadable stored handle gives a generic run_failed | low | patch (adapter_error) |
| 7 | EC | AssertionError on inconsistent Sessions | low | patch (board_unreadable) |
| 8 | BH | check_reissue has no lease; docs' error order differs | low | patch (docs) |
| 9 | BH | test_already_open gives no --resume hint | low | patch |
| 10 | VG, BH | Seed, sha, model_id and missing-line mismatches; double-resume; old layout; Trials changed after confirm; collect failure — all untested | medium | patch (tests) |
| 11 | BH, EC | Dead or expired handle has no fallback, so resume is stuck forever | medium | defer (real adapters, Epic 2) |
| 12 | VG | Collect path not covered by a concurrency test | low | defer (first real adapter) |
| 13 | BH | No CLI command to run the re-issue check on its own | medium | defer (Epic 4 defensibility, alongside the manifest) |
| 14 | BH | Warnings on stderr | false | Logs go to stderr by design (INTERFACE.md conventions); the code: message contract is for errors |
| 15 | BH | N+1 queries / Trials loaded twice | low | Rejected: performance only at pilot scale |
| 16 | BH | _settle callback untested | low | Rejected: exercised by the kill tests |

## Design Notes

- **Re-render, don't replay:** resume rebuilds requests from the Study folder, which is also the re-issue proof. If a Study input was edited after open, the re-issue check fails loudly instead of silently mixing request versions.

## Verification

**Commands:**
- `uv run pytest -q tests/test_resume.py` -- expected: all pass, no network
- `uv run pytest -q && uv run ruff check src tests && uv run lint-imports` -- expected: clean
