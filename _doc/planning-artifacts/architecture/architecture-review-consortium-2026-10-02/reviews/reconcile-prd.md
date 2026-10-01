# Reconcile: Architecture Spine vs PRD

- **Spine:** `../ARCHITECTURE-SPINE.md` (draft, 2026-10-02)
- **Sources:** `prd.md` (final), `addendum.md` (A2, A3), spine `.memlog.md`
- **Filter:** the user asked for no over-engineering, so this lists only gaps where two separately built stories could plausibly diverge. Each fix is the smallest rule that closes the gap.

## Verdict

The spine covers most quiet PRD requirements. Five gaps are worth a line in the spine before stories are cut: gaps 1–3 are high severity and gaps 4–5 are medium. Gaps 6–9 are low; settle them inside a story or ignore them.

## Already covered (no action)

| PRD item | Where the spine covers it |
|---|---|
| NFR-7 local-first, no telemetry | Conventions "Output: No telemetry"; Structural Seed "only network traffic goes to the configured Model providers" |
| NFR-2 / FR-22 Condition joined at export only | AD-2 |
| FR-13 pilot and screening rows never in a main Export | AD-12 (separate files) |
| FR-17 Practice clip answers never exported | AD-12 builds Export from Trial rows, and Practice clips exist only inside the rendered request (AD-7), so they never reach it |
| FR-22 export refuses while Sessions are running | AD-4 terminal states give one check: every Trial is terminal |
| FR-24 byte-identical re-issue | AD-5 + AD-7 (deterministic render, archive before state) |
| FR-6 / §6 no photos to Models | AD-7 |
| Multi-factor Conditions in the Blinding key | AD-2 (one module owns the format, so it can't diverge) |

## Gaps

| # | Gap | PRD ref | Severity | Suggested minimal fix |
|---|---|---|---|---|
| 1 | **Raising the cost ceiling breaks the Protocol lock.** FR-18 says the operator raises the ceiling and runs `open --resume`. AD-9 puts cost settings in `study.yaml`, and AD-8 locks `study.yaml`. So a resumed main Test either fails the lock check or gets re-frozen part-way through, which leaves one Test's Trials under two lock versions. The resume story and the lock story will settle this differently: one might skip the check on resume, while the other might leave the ceiling out of the lock. | FR-18, FR-13, NFR-5, A3 ("cost ceiling is set by the pilot") | High | Add a rule to AD-8. Either (a) `cost.ceiling` is excluded from the lock: it is stored in `study.yaml` but not in the lock, and every change is recorded in the cost ledger and the manifest; or (b) it sits in a separate unlocked file. Pick one. |
| 2 | **No record format for screening results.** Nothing says where results live. They could sit in `panel/` files (which are copyable and locked) or in `board.db` (AD-3 makes it the only mutable state). Nothing says that superseded runs are kept rather than overwritten. Nothing stamps a result with what it was valid for, so FR-7 invalidation can't work: after a Model pin or Instrument change plus a re-freeze, the lock passes again while the screening is stale. `screen`, `panel copy` and `open` are separate stories, and each will guess differently. | FR-5 (screening run ID), FR-7 (invalidation, copy), FR-10 (per Model × Instrument pass), §4.2 / §9 (superseded runs logged) | High | Add one rule: screening results are append-only records in `panel/screening.jsonl`, keyed by `run_id`, one per Agent or per Model×Instrument. Each record stamps the Model pin hash and the Instrument definition hash. "Current" means the latest run whose stamps match the current config. `open` refuses a main Test if any assigned (Agent, Instrument) has no current pass. |
| 3 | **"Rater-flow from the Export alone" vs screened-out Agents having no Export rows.** AD-12 and FR-20 require every Rater-flow count to come from the Export file. But Steps 1–3 (Personas generated, fidelity screen-outs, Models screened out per Instrument) are never assigned in a main Test, so they have no rows. The Export story and the Rater-flow story will each make a different choice: placeholder rows, or reading `panel/` directly. | FR-20, SM-2, AD-12 | High | Amend AD-12 to say that Steps 1–3 come from the screening records (gap 2) as frozen in the lock, and Steps 4–6 come from the Export. Or, if FR-20's "from the Export alone" must hold literally, the Export carries one `status=not_assigned` row per screened-out Agent with its reason code. Pick one. |
| 4 | **Pairwise Trials don't fit the data shape.** The ERD shows one Clip per Trial (`CLIP ||--o{ TRIAL`), and the Export has a single `clip_id` plus "one column per factor". A pairwise Trial has two Clips, two Conditions and a `position`, and the PRD needs both orders as separate Trials. Nothing says how a pair is identified, how the two orders link, or which Clip's Condition fills the factor columns. The renderer, the cost estimate, the randomizer and the Export are separate stories. | FR-16, FR-22, Glossary "Pairing plan", A1 (position bias) | Medium | Add a convention: a Trial holds an ordered `clip_ids` (length 1 or 2) and a `pair_id`, and the two orders are two Trials with the same `pair_id` and opposite `position`. In the Export, pairwise rows carry `clip_id_a` and `clip_id_b`, with factor columns suffixed `_a` and `_b`; `response` is the chosen Clip ID. |
| 5 | **The cost estimate and the ceiling reservation can use different math.** `open` estimates with the provider's token-count method (A3). The runner reserves "worst-case cost" (AD-6). With no shared function, SM-4 (±20%) and the "refuse if over ceiling" check can disagree. A second issue: Gemini `countTokens` on video needs the file uploaded, which would send Clips to a provider *before* the operator confirms, against NFR-9's intent that `open` names providers before anything is sent. | FR-14, FR-18, NFR-5, NFR-9, SM-4, A3 | Medium | Add to AD-6: one pure `core.cost` function maps a `TrialRequest`, Model and price table to an (expected, worst-case) cost. The estimate sums expected cost plus a retry allowance; the ledger reserves worst-case cost. Token counts come from an offline formula (tokens per second × duration plus text tokens). Nothing goes to a provider before confirmation. Calibrate the formula in the pilot. |

## Low (settle inside a story; don't add to the spine)

| # | Gap | PRD ref | Note |
|---|---|---|---|
| 6 | Persona-fidelity screening has no Clip. AD-4 and AD-7 assume Clip IDs, so `screen personas` could bypass the Trial, Archive and ledger path. | FR-5, FR-24, NFR-5 | One sentence fixes it: "All provider calls, including screening, are Trials (Clips optional) and go through AD-4 to AD-6." |
| 7 | Pilot and main Clip disjointness has no recorded owner. It could be a Clip attribute set at push, or derived from Test usage. | FR-12 | Derive it in `push test` from existing Test↔Clip rows in `board.db`. One story owns it. |
| 8 | The Panel copy hash isn't defined for a directory. AD-8 defines only file and JSON hashes. | FR-7 | Reuse the AD-8 lock scheme: a per-file map plus a combined hash over `panel/`. |
| 9 | The replacement policy for excluded Agents (none, resample within cell, or oversample) is neither decided nor Deferred. | A3, FR-4 | Add it to Deferred, to be decided in the `personas generate` story. |
