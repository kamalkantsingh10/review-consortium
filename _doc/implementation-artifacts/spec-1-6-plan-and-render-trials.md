---
title: 'Story 1.6 — Plan and render Trials'
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

**Problem:** A registered Test can't yet be turned into concrete Trials. Kamal needs to see exactly what would run before anything is sent or spent, and 1.7 needs a deterministic plan and request renderer to dispatch.

**Approach:**
- Plan Sessions and Trials in pure `core/plan.py`, and render each Trial into a provider-neutral `TrialRequest` in pure `core/render.py`.
- Add `consortium open <test> --dry-run`, which loads inputs, plans, renders every request (to prove it renders), prints counts and writes nothing.

## Boundaries & Constraints

**Always:**
- `core/plan.py` and `core/render.py` are pure: no I/O, no unseeded randomness. `stages/open.py` does all reading.
- **Agents** = every Persona from `load_personas` (1.3) × every Model in the Test's `models` (default all in `study.yaml`). Agent ID `p<n>-m<n>`. One Session per Agent × Repeat (`r1..rN`, N = effective `session.repeats`: Test override, else `study.yaml`), `session_id` from `core.ids.session_id` = `<test>/<agent>/r<repeat>`.
- **Trials per Session:** for each Instrument in the Test, a single-Clip Instrument gives one Trial per Clip; a pairwise Instrument with `all_pairs` gives, for every unordered Clip pair, two Trials (both orders) sharing `pair_id = <instrument>:<clip_lo>:<clip_hi>` (sorted IDs), with ordered `clip_ids` and `position` 1 (lo first) or 2 (hi first).
- **Order:** build the canonical list (Instrument order as in the Test, then Clip order, then position), shuffle it with `random.Random(derive_seed(study.seed, "order", session_id))`, then number `trial_index` from 1. `order_seed` is kept on each Trial.
- **Prompt variant:** Repeat `r` uses the `((r - 1) mod n)`-th of the Instrument's `prompt_variants` in declared order (`default` always exists, 1.2). The variant name is kept on the Trial.
- **`TrialRequest`** (frozen dataclass) holds only: Persona card text, Instrument instructions and Items (with anchors and response schema), Prompt variant text, the Instrument's Practice examples (the first effective `session.practice_clips` entries of the Test's `practice:` list for that Instrument, in list order; each as clip_id + SHA-256 + intended answer), and 0–2 target Clips as `clip_id` + SHA-256. No Trial, Session, Test, Agent or Model IDs; nothing from any other Trial; no Conditions; no provider settings.
- `canonical_json(obj)`: sorted keys, UTF-8 (`ensure_ascii=False`), separators `(",", ":")`. Same inputs → byte-identical bytes.
- `open` refuses `kind: main` with `protocol_lock_unavailable` before planning.
- `--dry-run` writes nothing: `board.db` is opened with `connect(study_dir, readonly=True)` (see Tasks), no files are created or touched.

**Never:**
- Persisting Trials or any board table (that is 1.7). No dispatch, no Rater, no cost estimate (1.9), no eligibility (Epic 3).
- Importing `board.blinding` anywhere on this path.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Dry run | pilot Test, 64 Personas, 1 Model, 3 Repeats, Godspeed over 4 Clips + pairwise over 4 Clips | stdout: Sessions 192; Trials per Session 4 + 12 = 16; total 3072; counts per Model, Instrument and Trial type (single/pairwise). Exit 0. Study folder unchanged (bytes and mtimes). | N/A |
| Pairs | pairwise over 3 Clips | 6 Trials/Session; 3 `pair_id`s, positions 1 and 2 | N/A |
| Same seed | plan twice | identical Trial order and identical `canonical_json` of every request | N/A |
| Repeats > variants | 3 Repeats, variants `default`, `alt` | r1→`default`, r2→`alt`, r3→`default` | N/A |
| Main Test | `kind: main` | nothing planned | `protocol_lock_unavailable`, exit 1 |
| Unknown Test | name not registered, or no `board.db` | nothing planned | `unknown_test`, exit 1 |
| No Personas | `panel/personas/` empty or missing | nothing planned | `panel_missing` (from `load_personas`), exit 1 |
| No dry-run flag | `open <test>` | nothing happens | `run_unavailable: dispatch arrives in story 1.7`, exit 1 (removed by 1.7) |

</frozen-after-approval>

## Code Map

- `src/consortium/core/seeds.py` (1.3) -- `derive_seed`.
- `src/consortium/config/load.py`, `config/models.py` (1.2, 1.3) -- `load_study`, `load_test`, `load_instruments`, `load_personas`; `TestConfig` carries `instruments`, `models`, `clips`, `practice`, `session` overrides, `kind`.
- `src/consortium/core/ids.py` (1.4) -- `session_id`.
- `src/consortium/board/db.py` (1.4), `board/clips.py` `list_clips` (1.4), `board/tests.py` `get_test` (1.5) -- registered Tests and Clip SHA-256s.

## Tasks & Acceptance

**Execution:**
- [ ] `src/consortium/core/plan.py` -- `Session`, `Trial` (frozen dataclasses: `trial_id = <session_id>/t<trial_index>`, `test`, `session_id`, `trial_index`, `instrument`, `clip_ids`, `pair_id`, `position`, `prompt_variant`, `order_seed`, `repeat`, `agent_id`, `persona_id`, `model_id`), `Plan`, and `plan_test(test, cfg, personas, instruments) -> Plan` per the rules above. -- FR14, FR16, AD-10.
- [ ] `src/consortium/core/render.py` -- `TrialRequest`, `canonical_json`, and `render(trial, *, persona_card, instrument, practice, clip_sha256) -> TrialRequest`. -- FR15, FR17, AD-7.
- [ ] `src/consortium/board/db.py` -- `connect(study_dir, readonly=False)`: with `readonly=True` it opens a `file:…?mode=ro` URI, never migrates or writes, returns `None` if `board.db` is absent, and raises `board_version_mismatch` if `user_version` ≠ `len(MIGRATIONS)`. The one read-only entry point; 1.11 and 1.12 reuse it. -- Dry run must not write.
- [ ] `src/consortium/stages/open.py` -- `open_test(study_dir, test, *, dry_run, yes, ceiling, resume)`: load config, Test (from its registered path), Instruments, Personas (`load_personas`) and their cards (`panel/personas/<id>.md`); refuse `main`; plan; render every Trial; return a summary. Non-dry-run raises `run_unavailable`. -- Use case.
- [ ] `src/consortium/cli.py` -- `open TEST [--dry-run] [--yes] [--ceiling] [--resume] [--study]`; prints the summary to stdout. -- Thin adapter.
- [ ] `docs/INTERFACE.md` -- document `open --dry-run`, Session/Trial/pair ID formats, position, variant rotation, and `TrialRequest` contents. -- Definition of done.
- [ ] `tests/test_plan.py`, `tests/test_render.py`, `tests/test_open.py` -- every matrix row; render excludes other Trials' Clips and any ID/Condition string; folder snapshot equal before and after dry run.

**Acceptance Criteria:**
- Given two Study folders with the same seed and inputs, when each is dry-run planned, then Trial order and every rendered request are byte-identical.
- Given any rendered request, when its `canonical_json` is searched, then no `blinding_key.csv` Condition value, no other Trial's Clip ID and no Session/Test ID appears.

## Implementation Notes

## Spec Change Log

## Review Triage Log

## Design Notes

- **Why no IDs in the request.** The Test name or Session ID could hint at the design; the Archive keys the request by `(trial_id, attempt)` from outside, so the request itself needs none.
- **Practice examples** come from the Test YAML's `practice:` list (1.2/1.5); a Trial gets the first `session.practice_clips` examples whose `instrument` matches its own (1.5 guarantees at least that many).
- **Dry run renders everything** so a broken Instrument or missing Clip fails here, not mid-Run.

## Verification

**Commands:**
- `uv run pytest -q` -- expected: all pass
- `uv run ruff check src tests && uv run lint-imports` -- expected: clean
