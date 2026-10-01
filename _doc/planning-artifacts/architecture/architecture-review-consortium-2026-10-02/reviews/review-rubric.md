---
reviewer: rubric (good-spine checklist)
target: ARCHITECTURE-SPINE.md (+ .memlog.md)
prd: _doc/planning-artifacts/prds/prd-review-consortium-2026-10-02/prd.md (+ addendum.md)
date: 2026-10-02
---

# Rubric review: Review Consortium architecture spine

## Verdict

The spine is solid and mostly lean. Blinding, single writer, Trial lifecycle, archive-before-state and render/transmit pin the most dangerous divergence points, and the Stack was checked against PyPI on the day. It is not ready for epics yet, for three reasons:

1. **Screening has no defined execution path or state home.** AD-1 forbids `screen` from reusing `run`, and screening results live in `panel/` even though AD-3 names `board.db` as the only mutable state.
2. **The Rater port leaves out provider file upload and token counting.** Both are where the two adapters will diverge most.
3. **The operational envelope is thin.** Concurrent CLI processes, provider region and streaming constraints, and CI-dependent enforcement are not addressed.

Over-engineering is low. A few ADs restate PRD text and could be trimmed.

**Counts:** critical 0, high 4, medium 9, low 6.

---

## Findings

### High

**H1. Screening has no execution path, and AD-1 blocks the obvious one.** *(divergence point missed; FR-5, FR-7, FR-10)*
- `screen personas` and `screen models` both send requests to Models. FR-10 says they run a `screening` Test.
- AD-1 says stages never import another stage, so `stages/screen` cannot reuse the runner in `stages/run`. The same applies to `open`, which per FR-14 "starts the Run" and handles `--resume`.
- The likely result is two runners. One would be correct, with ledger, archive, lifecycle and retries. The other would be ad-hoc and skip the cost ceiling (NFR-5) and the Archive (FR-24).
- FR-5's persona questionnaire has no Clip, and AD-7's `TrialRequest` assumes Clip IDs.

*Fix:* add an AD that names one execution engine, for example a `runner/` infrastructure module, or `core` plus `board` dispatch. Every model call goes through it: screening, pilot and main. Every call gets a Trial row, ledger reservation and Archive entry. Allow `TrialRequest` to have zero Clips. State that `cli` composes `open` and `run`, or that the engine is called from both.

**H2. Screening validity, invalidation and storage are undecided, and conflict with AD-3.** *(FR-7, FR-10, §4.2)*
- FR-7 requires that changing a Model pin invalidates that Model's screening, and changing an Instrument invalidates its Perception screening. It also requires that `open` refuses until screening is re-run.
- FR-10 requires `open` to refuse when a construct has no screening coverage. §4.2 requires every screening run, including superseded ones, to be logged.
- The spine does not say what screening results are keyed by, such as a Model-pin hash or an Instrument hash. It does not say where results live or who computes eligibility.
- Screening results sit in `panel/`, which is a mutable file set written by `screen` and `panel copy`. AD-3 says `board.db` is the only mutable state.
- `panel copy` is missing from the stage list and the package tree.

*Fix:* add an AD for screening results.
- They are written as immutable, versioned files under `panel/screening/<run_id>.json`, so they stay copyable and lockable.
- Each one records the Model-pin hash, the Instrument hash and the thresholds used.
- `core.eligibility(panel, test)` is the single function `open` uses to decide which Agent × Instrument pairs may rate.
- Superseded runs are kept and never overwritten.
- Amend AD-3 to "board.db is the only mutable *run* state; `panel/` holds append-only screening artifacts". Add `stages/panel` (copy).

**H3. The Rater port leaves out the provider file lifecycle and token counting, which are where the adapters will diverge most.** *(AD-6, AD-7, AD-11, FR-14, FR-24, NFR-8)*
- Every Trial carries 2 Practice clips plus 1 or 2 Clips. A 128-Agent, 3-Repeat, all-pairs Test reuses the same Clip videos tens of thousands of times.
- The spine does not say whether adapters upload once and reuse a handle, such as a Gemini Files API URI or a DashScope URL or OSS object, or inline base64 on every request. It also leaves open how handle expiry is handled (Gemini files expire after about 48 h, which is shorter than a paused-and-resumed Run) and who deletes uploaded Clips afterward (NFR-9).
- FR-14 needs a provider token count, such as Gemini `countTokens` or the DashScope equivalent. The port has no method for this.
- FR-24 promises byte-identical re-issue, which a request holding an expired file URI cannot meet.

*Fix:* extend the port minimally:
- `prepare(clip_id, path) -> ProviderClipRef`: idempotent, cached in `board.db` with an expiry, and re-uploaded when expired.
- `count_tokens(TrialRequest) -> int`.

Rule that the Archive records the provider-neutral `TrialRequest` plus clip hashes, never provider file handles, so re-issue is defined against the neutral form. Add uploaded-file cleanup to the end of `export`, or to a `consortium clean` command.

**H4. Two CLI processes on one Study is undecided (operational envelope).** *(AD-3, NFR-6, UJ-2)*
- UJ-2 has Kamal watching `consortium status` while the Run is going, which means two processes.
- AD-3's single writer is in-process only. Nothing stops a second `open --resume` or `run` from starting against the same Study, which would double-dispatch `sent` and `planned` Trials and double-spend.
- Readers during writes need SQLite WAL mode, or `status` will hit `database is locked`.

*Fix:* add to AD-3:
- `board.db` opens in WAL mode.
- Any command that dispatches Trials takes an exclusive Study lease, for example `board.db`'s `run_lease` row or an `fcntl` lock on `.consortium.lock`, and refuses if one is held.
- Read-only commands (`status`, `export`) never take the lease.

### Medium

**M1. AD-12's rule can't be met as written.** *(FR-20)* Rater-flow steps 1–3 (Personas generated, screened out on fidelity, Models screened out per Instrument) concern Agents that never produce main-Test rows. A Rater-flow report "computed from the Export file" therefore cannot count them, unless the Export carries them.
*Fix:* state how screened-out Agents appear. One option is an `agents` companion table in the Export, versioned with it and keyed by `agent_id`, with screening outcome and reason code. The other is placeholder rows with `status=screened_out`. Then "reproducible from the Export alone" means the Export bundle.

**M2. Provider error classification has no owner.** *(AD-7, FR-9, FR-18)*
- AD-7 says adapters return raw responses and `core` parses them. But telling a safety refusal from a transport error from a rate limit is provider-specific, for example Gemini `finish_reason=SAFETY` or `prompt_feedback.block_reason` versus DashScope `data_inspection_failed`.
- Either `core` learns provider formats, which violates AD-1's spirit, or each adapter invents its own classification.
- AD-4 has one retry counter, but FR-9 transport retries and FR-18 schema retries (`max_retries`) are different budgets.

*Fix:* adapters return `RawResult(outcome: ok | refused | transient | fatal, raw, usage, build)`, and `core` validates only `ok` payloads. Keep two counters on the Trial: `transport_attempts` and `schema_attempts`.

**M3. The tool version is not in the Protocol lock.** *(AD-8, FR-13, NFR-3)*
- `core.render`, built-in Instruments and templates ship inside the package. A `uv tool upgrade` can change the rendered prompts while `open` still accepts the lock.
- Built-in Instruments are not under the Study folder, so AD-8's "relative path" key is undefined for them.

*Fix:* the lock records the `consortium` version. `freeze` copies any built-in Instrument or template in use into the Study (for example `instruments/`) so that it is hashed like user files. `open` refuses a main Test on a version mismatch, or at least warns and records it on the Trial rows.

**M4. Lock history is unspecified.** *(AD-8, FR-13)* "Re-freezing creates a new lock version" and "the manifest lists every lock version", but `protocol.lock` is a single file.
*Fix:* keep `protocol.lock` as current and append each version to `locks/<version>.json`, or to a `locks` table. These are never deleted.

**M5. The worst-case cost reservation is undefined without an output cap.** *(AD-6, FR-18)* "Worst-case cost" needs a bounded output length.
*Fix:* make `max_output_tokens` a required pinned Model setting (validated by FR-2), and define reservation as input count plus `max_output_tokens` times the price.

**M6. AD-10 seeds depend on an autoincrement primary key.** `model:<trial_id>` uses an integer PK whose value depends on insertion order. Re-planning or re-creating a Study can silently change seeds.
*Fix:* derive from a stable natural key, `model:<session_id>:<trial_index>`, and store it.

**M7. AD-10 and AD-11 contradict each other on randomness.** AD-10 says "nothing uses global or unseeded randomness". AD-11 mints Clip IDs from 8 random base32 characters.
*Fix:* state the exception explicitly. Clip IDs use `secrets` (non-reproducible by design, for blinding), and that is the only unseeded randomness allowed.

**M8. Provider strategy is silent on region, endpoint and streaming (operational envelope).**
- The memlog notes that omni batch support varies by DashScope region, but the spine does not pin a region or base URL (international `dashscope-intl` versus mainland). Region changes model availability and data residency (NFR-9).
- Alibaba's docs state that Qwen-Omni models in compatible mode support **streaming output only**. If that holds for `qwen3-omni-flash`, the adapter must stream and assemble, which affects how `collect` and usage capture work.
- Rate-limit and quota tiers are not mentioned, and NFR-8 implies sustained high request rates.

*Fix:* put `region` and `base_url` in the per-Model `study.yaml` pin, recorded in the Archive and manifest. Add "verify streaming-only and per-minute quotas" to the onboarding check alongside the existing Qwen equivalence item. Add a NFR-8 feasibility check (request volume × rate limit) as an open question.

**M9. Enforcement depends on a CI that is deferred.** AD-1 and AD-2 are "enforced by import-linter contracts in CI", and Deferred says the CI provider is "decided at release. Not needed to build".
*Fix:* run `lint-imports` as a pytest test, or through a `uv run` check script, from the first commit. Add a test that greps for `blinding_key` and `sqlite3` outside their owning modules, because import-linter cannot stop a module from opening the CSV by path. Also add a test asserting no Condition string appears in `board.db` after the example Study runs.

### Low

**L1. The excluded-Agent replacement policy is undecided.** Addendum A3 asks for one: none, resample within cell, or oversample. This affects persona generation, quota balance after exclusions (FR-4) and the Rater-flow report.
*Fix:* decide "none (v1)" in one line, or list it under Deferred with the story that owns it.

**L2. `board.db` schema evolution is undecided.** A Study lives across tool upgrades, and Deferred hands the table layout to `board/`.
*Fix:* add one line: `board.db` carries a `schema_version`, and `board` refuses or migrates on mismatch.

**L3. AD-5 needs a torn-write rule.** A crash mid-append can leave a partial last JSONL line.
*Fix:* readers ignore a final line that fails to parse, and writers `fsync` before the state transition.

**L4. Over-engineering or obvious items to trim.**
- AD-7's last bullet ("photos are never an input") is a no-op while photos are deferred, and FR-6 already says it.
- AD-9's "built-in Instruments are YAML" restates FR-3.
- AD-12's "pilot and screening Exports go to separate files" restates FR-13.

None is harmful, but each adds review surface.
*Fix:* drop these bullets or fold them into the Capability map.

**L5. AD-6's submit/collect split is speculative but defensible.** Batch is deferred, and the user asked for no over-engineering. Given NFR-8 volume and the 50% batch discount, keeping the shape is reasonable.
*Fix:* add one line of rationale to the AD (crash-safe `sent` semantics are needed anyway), so it doesn't read as gold-plating. Keep the real-time `submit` trivial: start tasks and return futures.

**L6. Small consistency gaps.**
- `prices.yaml` is a separate file, but AD-9 says cost settings live in `study.yaml`. Name `prices.yaml` explicitly as a config input.
- The Capability map routes FR-7 to `stages/personas` and `stages/screen`, but neither covers `panel copy`.
- ffmpeg is a non-pip system dependency, so FR-25 and SM-5 need a startup check with a clear error code.

---

## Checklist summary

| Criterion | Result |
| --- | --- |
| Fixes the real divergence points, misses none | Partial: screening path (H1, H2), provider upload and token counting (H3), error classification (M2) |
| Each Rule enforceable and prevents its divergence | Mostly: AD-12 unsatisfiable as written (M1), AD-6 unbounded (M5), AD-2 file reads not covered by import-linter (M9) |
| Nothing Deferred allows divergence | Mostly: CI deferral removes enforcement (M9); schema layout is fine with L2 |
| Tech verified-current | Yes for packages (PyPI checked 2026-10-02). Provider behavior is not verified: streaming-only and region (M8) |
| Covers the PRD | Gaps: FR-7 copy and invalidation, FR-10 eligibility, FR-14 token count, FR-20 steps 1–3, FR-24 lock history |
| Every dimension decided, deferred or open | Operational envelope is thin: multi-process (H4), provider region and quotas (M8), schema versioning (L2) |
| Not over-engineered | Good overall. Minor restatements (L4); AD-6 is justified (L5) |
