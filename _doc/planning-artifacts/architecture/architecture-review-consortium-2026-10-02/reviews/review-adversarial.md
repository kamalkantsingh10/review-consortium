---
review: adversarial (incompatible-pair attack)
target: ARCHITECTURE-SPINE.md (AD-1..AD-12) + .memlog.md
prd: _doc/planning-artifacts/prds/prd-review-consortium-2026-10-02/prd.md
date: 2026-10-02
constraint: "do not over engineer": each fix is the smallest rule that closes the hole
---

# Adversarial Review: Review Consortium Spine

**Verdict:** The spine's boundaries are sound: blinding, the single writer, render/transmit and the lock all hold. But AD-4/5/6 (retry, resume, cost), AD-8 (lock scope) and AD-12 (Export vs Rater-flow) leave room for two compliant stories to build incompatibly. Ten holes are listed below. Closing them takes about 10 short rule edits and adds no new modules or ports.

Method: for each hole, two units (stories or modules) that a different agent could build. Both obey every AD to the letter, and they still clash.

---

## H1 — Raising the cost ceiling breaks the Protocol lock (CRITICAL)

- **Pair:**
  - *Story "cost ceiling / resume" (FR-18).* AD-9 says all cost settings live in `study.yaml`, so the operator raises `cost.ceiling` there and runs `open --resume`.
  - *Story "lock" (FR-13).* AD-8 locks `study.yaml` by raw-byte SHA-256, so `open` refuses the main Test.
- **Result:** the run paths in the PRD (FR-18 "raise the ceiling, then resume") can't both work. An implementer will either skip the lock check on `--resume`, which lets edits made mid-Run escape the lock, or make the ceiling unraisable. Raw-byte hashing means one key can't be excluded.
- **Minimal fix (amend AD-8/AD-9):** the cost ceiling is operator budget, not protocol. It lives in `board.db`, set by `open --ceiling <usd>` (default from `study.yaml` on first open only), and is not read from `study.yaml` after the first open. `open --resume` *does* re-check the lock.

## H2 — The Export can't carry the Rater-flow (HIGH)

- **Pair:**
  - *Story "export" (FR-22).* It writes "one row per Item per Trial", so a `refused` or `failed` Trial (which has no Items) yields 0 rows. Screened-out Agents are never assigned Trials (FR-14), so they also yield 0 rows.
  - *Story "rater-flow" (FR-20).* AD-12 says to compute it *from the Export file only*. Steps 1–4 (generated, fidelity-screened, perception-screened, refused/failed) then count 0, or the story breaks AD-12 with side queries.
- A related split: catch-trial exclusions could be computed once in `stages/run` and stored, and again in `core/exclusions` at export. That makes two owners of `excluded`.
- **Minimal fix (amend AD-12):**
  1. Every Trial yields at least one row. A non-valid Trial yields one row with `item` empty and `status` set.
  2. `export` also writes `exports/<test>.panel.csv`, with one row per Persona × Model including screened-out ones, plus reason codes. The Rater-flow report is computed from those two files only.
  3. Exclusions are computed only by `core/exclusions` at export time and are never persisted.

## H3 — Retry, resume and "attempt" are undefined, so archive keys collide and the answer is ambiguous (HIGH)

- **Pair:**
  - *Story "runner retries" (FR-18).* For an invalid answer, it sets the Trial to `invalid`, increments `retries` and re-dispatches. This breaks terminality in spirit, or else it keeps the Trial `sent`. Either way it reuses `attempt = retries`.
  - *Story "resume" (AD-4).* It re-dispatches a `sent` Trial (the realtime request was lost in the crash) with the **same** attempt number.
- **Result:** `requests.jsonl` holds two lines keyed `(trial_id, attempt)`, possibly two paid responses for one attempt, and nothing says which response is "the" answer that the Export uses.
- **Minimal fix (amend AD-4/AD-5):**
  - `attempt` is a db column, incremented and committed by the writer *before* the request is archived. Every dispatch, including retries and resume re-dispatches, gets a new attempt number.
  - While retries remain, the Trial stays `sent`. It becomes `invalid` only when retries are exhausted.
  - The Trial's answer is the response of its highest attempt.
  - On resume, a realtime `sent` Trial is always re-dispatched as a new attempt, never "collected". Only batch handles are collected.

## H4 — The cost ledger double counts or leaks reservations (HIGH)

- **Pair:**
  - *Story "runner".* It reserves worst-case cost per request (AD-6) and adds actual cost on response.
  - *Story "status" (FR-21, "cost so far").* It sums every ledger row, counting reservation and actual for the same request.
- *Resume story:* it re-reserves the re-dispatched `sent` Trials, while the pre-crash reservation rows are still in the ledger. The ceiling then trips early, or spend is under-counted if a story deletes those rows.
- **Minimal fix (amend AD-6):**
  - The ledger holds one row per `(trial_id, attempt)`, with `reserved_usd` and `actual_usd`.
  - Committed spend is `Σ coalesce(actual_usd, reserved_usd)` over rows that aren't abandoned.
  - Resume marks the previous attempt's unresolved row `abandoned` and counts it at `reserved_usd`. The request may have been billed, so this is conservative.
  - `status`, the ceiling check and the Reporting manifest all use this one query from `board/`.

## H5 — Screening results have two owners, and Model IDs are positional (HIGH)

- **Pair:**
  - *Story "screen".* Screening is a Test that runs through the Runner, so its results are Trial rows in `board.db`. AD-3 says `board.db` is the only mutable state.
  - *Story "panel copy" (FR-7).* AD-8 says screening results live in `panel/` (copied and locked). It copies `panel/` files, which have no Trial rows behind them.
  - *Story "open".* It checks screen-pass from the db, and a copied Panel shows as unscreened. Or it checks `panel/`, and a pin change (FR-7) can't be detected because nothing records which pin a result came from.
- Also, the Agent ID `p<n>-m<n>` gives no rule for `m<n>`. If it is positional in `study.yaml`, reordering Models or copying the Panel into a Study with a different Model list silently attaches screening results to the wrong Model.
- **Minimal fix (new AD or amend AD-3/AD-8):**
  - `panel/screening.json` is the sole authority for screen-pass. Only `stages/screen` writes it, derived from its Trial rows. It is a declared exception to AD-3, as an immutable-per-run artifact.
  - Each record carries `model_pin_sha256` (canonical JSON of the Model's pinned settings) and `instrument_sha256`. `open` refuses on a mismatch, which covers FR-7.
  - Model IDs are explicit keys in `study.yaml` (`id: m1`), never list positions.

## H6 — The Seed int range overflows the provider APIs, and retries reuse the same seed (HIGH)

- **Pair:**
  - *Story "seeds" (AD-10).* It takes the first 8 hex digits as an int, giving a range of 0..2^32−1.
  - *Story "gemini/qwen adapter" (FR-8).* It passes `model:<trial_id>` as the provider `seed`. Both providers document seed as a signed 32-bit / [0, 2^31−1] range (verify at onboarding), so about half of all Trials get a 400 error. The Fake rater never catches this.
- In addition, `model:<trial_id>` is the same for every attempt. A retry at the same seed, settings and prompt can reproduce the same invalid answer, which defeats FR-18 retries on seed-honoring providers.
- **Minimal fix (amend AD-10):**
  - Mask the derived seed to 31 bits (`& 0x7FFFFFFF`).
  - The provider seed purpose is `model:<trial_id>:<attempt>`.
  - IDs inside the seed string are joined with `|` (Session IDs already contain `/`, and test names are user text).

## H7 — The lock's file set is ambiguous: built-ins, "in use", pilot YAMLs and lock-vs-hash (MEDIUM)

- **Pair:**
  - *Story "freeze".* It computes "Instruments in use" from **all** `tests/*.yaml` and hashes the built-in Instrument YAMLs, which live in the installed package and not the Study folder, so they have no Study-relative path.
  - *Story "open".* It recomputes the set from **the Test being opened**, so the file sets differ and the lock reports a false mismatch. Or it skips built-ins, so a `pip upgrade` silently changes an Instrument.
  - Adding a *pilot* Test YAML after freezing changes `tests/*` and blocks every main Test, though FR-13 says pilots can open at any time.
  - The Export `protocol_lock` column is the version in one story and the combined hash in another.
- **Minimal fix (amend AD-8):**
  - The lock set is all Study-folder inputs, plus the built-in Instruments, which are hashed under the path `builtin:<name>.yaml`.
  - The set is always computed as "everything referenced by any `main` Test YAML, plus fixed files". `pilot` and `screening` Test YAMLs are excluded.
  - Combined hash = SHA-256 of the canonical JSON of the path→hash map.
  - Trial rows and the Export store the combined hash. Version numbers are only for the manifest.

## H8 — The Test definition has two owners: the push snapshot and `tests/*.yaml` (MEDIUM)

- **Pair:**
  - *Story "push test" (FR-12).* It validates the YAML and stores the parsed Test (Clips, pairing, kind) in `board.db`, returning a stable ID.
  - *Story "open".* It plans Trials from `tests/<name>.yaml` through `config/` (AD-9: only config reads YAML).
- **Result:** if the YAML is edited after push (a pilot Test isn't locked), the plan and the validated Test diverge. The FR-12 pilot/main Clip-exclusivity check runs against stale db data.
- **Minimal fix:** the YAML file is the only definition. `push test` records only `(name, kind, file_sha256, clip_ids)` in `board.db`. `open` refuses if the file's SHA differs from the pushed one ("re-push").

## H9 — `open` and `run` can't chain under AD-1 (MEDIUM)

- **Pair:**
  - *Story "open" (FR-14).* It must estimate, confirm, plan **and start the Run**.
  - *Story "run".* It is a separate stage, and AD-1 forbids stage→stage imports.
- **Result:** one agent duplicates the runner inside `open`. Another has `open` insert planned rows and exit, so nothing runs. A third adds `consortium run`, which conflicts with FR-18's `open --resume`.
- **Minimal fix (amend AD-1 or the Conventions):** `cli` composes stages. `open` = `stages.open.plan()` (writes `planned` Trials and the ceiling), then `stages.run.execute()`. `open --resume` = lock check, then `stages.run.execute()`. Only `cli` sequences stages.

## H10 — Who writes the Archive, and the crash ordering for `push` (LOW)

- **Pair (archive):**
  - *Story "adapter/worker".* It appends to `responses.jsonl` from concurrent workers. The google-genai sync calls can run in threads, so lines can interleave.
  - *Story "writer".* It marks the Trial terminal. The order AD-5 requires holds only if the append is flushed before the db commit.
- **Pair (push):**
  - *Story "push clip".* It inserts the Clip row into `board.db`, then crashes before appending to `blinding_key.csv`.
  - *Story "export".* It finds a Clip with no Condition and either drops its rows (breaking AD-12) or crashes.
- **Minimal fix:**
  - The single writer task also owns Archive appends: append, then flush and fsync, then the db commit.
  - `push` writes the Blinding key row (fsync) before the db row.
  - `export` refuses if any db Clip lacks a key row, and ignores orphan key rows.

---

## Considered and rejected (no hole)

- **Blinding (AD-2):** the import contract plus "Conditions never in db" holds. The leak report runs inside `push`, and Condition breakdowns go through `export`. No compliant pair leaks.
- **Clip ID collisions:** 40 random bits per Study is negligible. `push` can retry on a uniqueness error without a new rule.
- **Prompt drift across adapters:** AD-7 closes it.
- **A dedicated Session/Test state table:** it isn't needed. Session status is derived from Trials (all terminal means ended), which is consistent with AD-3. Adding states would be over-engineering.

## Priority order to apply

H1, H2, H3, H4 and H5 before the first Run story. H6 before the first real-provider adapter story. H7 and H8 before the lock and `push test` stories. H9 and H10 can be one-line convention edits made any time before the runner story.
