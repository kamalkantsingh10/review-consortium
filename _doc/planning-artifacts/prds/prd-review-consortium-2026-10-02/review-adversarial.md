---
title: "Adversarial review: PRD Review Consortium"
reviewed: prd.md (draft, 2026-10-02), addendum.md
date: 2026-10-02
hats: (1) HRI/psychology methods peer reviewer, skeptical of persona-conditioned VLM raters; (2) senior engineer who must build from this PRD
---

# Adversarial review: Review Consortium PRD

## Verdict

The PRD gets the procedure right: blinding, locking, fresh contexts and rater flow. It does not secure the inference those procedures are meant to protect. The parameters a reviewer would attack are left unspecified, sit outside the lock, or contradict each other: the screening thresholds, the meaning of test–retest at temperature 0, the unit of analysis, and leakage through the clip files. As written, a hostile reviewer could reject the method and an engineer would have to guess at roughly a third of the behaviour.

Decisions recorded in `.memlog.md` are taken as given. The two Models via cloud APIs, Panel reuse, photos never reaching the Model, and no stats in the tool are not re-argued. They come up below only where they create a concrete hole.

---

## CRITICAL

### C1. The Protocol lock locks the wrong artifact, and every tunable parameter sits outside it
- **Where:** FR-12, NFR-4, FR-18 ("threshold set in the Protocol"), FR-6, FR-9, §3 "Protocol lock", SM-C1.
- **Problem:** The lock hashes `protocol.md` only. That is free-text Markdown. The things that actually decide the results live elsewhere:
  - the Instrument YAML and its prompt wording;
  - the briefing text, the Persona cards and the Sampling frame;
  - Model settings in `study.yaml`, the Test YAML and the pairing plan;
  - the fidelity, perception and catch-trial thresholds.

  An operator can lock `protocol.md` and then edit an Instrument prompt or a threshold in YAML between pilot and main. The hash still matches. There is a further problem: FR-18 says the catch-trial threshold is "set in the Protocol", but the tool cannot machine-read a threshold out of prose. It will therefore read it from YAML, which is unlocked. SM-C1 ("never tune after seeing data") is an honour-system rule presented as a guarantee.
- **Fix:**
  - Define the Protocol lock as a hash manifest over `protocol.md` plus every input that affects rating: `study.yaml`, all Test YAMLs of kind `main`, Instrument definitions, prompt and briefing templates, Persona cards, the Panel screening results, and every threshold.
  - Require thresholds as structured fields in a locked `analysis_plan.yaml` (or a `protocol.yaml` front-matter block), not in prose.
  - `open` for a main Test recomputes the full manifest and refuses on any drift. Add a testable consequence: "editing any Instrument prompt after lock causes `open` to refuse."

### C2. Temperature 0 plus Repeats makes SM-2 (test–retest) meaningless or misleading
- **Where:** FR-8 (pins temperature), FR-16, SM-2, SM-C3, Risk "Gemini is non-deterministic even with a seed". The source spec sets `temperature: 0` and `repeats: 3`.
- **Problem:**
  - If decoding is deterministic, Repeats are byte-identical copies and ICC = 1 by construction. That is not reliability.
  - If it is not deterministic (Gemini, per A1), the "retest" variance measures provider infrastructure noise (batching, hardware, silent model updates), not rater stability.
  - Either way, an ICC ≥ 0.75 target says nothing about the construct, and SM-C3 already concedes that high values may mean homogenized raters.
  - A reviewer will also ask why a rater at temperature 0 represents a population of humans at all. With temperature 0, all within-cell variance comes from Persona text. That makes Persona-text sensitivity the whole story, and the PRD never tests it (A1 itself flags prompt sensitivity and 9–23% answer flips under option reordering).
- **Fix:**
  - Decide and state the sampling regime as a Protocol-level choice, with a rationale. Either (a) T=0 with Repeats dropped and replaced by **prompt-perturbation retest** (paraphrased Items, reversed anchor order, re-ordered options), or (b) T>0 with Repeats as true resampling.
  - Redefine SM-2 as stability under perturbation, and record which kind of retest was run.
  - Add a requirement that `open` warns when Repeats > 1 at T=0 on a provider documented as deterministic.
  - Name the ICC form (e.g. ICC(3,1) or ICC(2,k)) and say which Item types it applies to. ICC is undefined for pairwise choices, so use a kappa or agreement rate there.

### C3. The core validity objection is neither addressed nor scoped out
- **Where:** §1 Vision ("the way a careful researcher runs a human rater panel"), §2.1 Social JTBD ("every methods question a reviewer asks has a recorded answer"), §6 Non-Goals ("a human validity anchor belongs to the Study"), §9 Risk row 1, Q4.
- **Problem:**
  - The first question any HRI reviewer asks is "why should I believe this stands in for humans?" The PRD's mitigations (Screenings, Catch trials, Rater-flow, manifest) all establish that the procedure was followed. None establishes criterion validity.
  - The Risk table lists the "optional human anchor" as a mitigation while the Non-Goals push it out of the tool. So nothing in v1 helps the user compare against humans: no import format and no agreement report.
  - The Social JTBD is untestable and, as written, false.
  - The Vision's framing ("the way a human panel is run") invites the reply that procedure is not equivalence.
- **Fix:** Either
  - (a) add an explicit Non-Goal: "The tool does not establish that Agents are valid proxies for human raters; validity claims are the Study's burden", and rewrite the Social JTBD to "every *procedural* methods question has a recorded answer"; or
  - (b) add a small FR: import a human-ratings CSV in the Export schema and emit per-Item human–Agent distribution comparisons (e.g. mean difference, variance ratio, rank correlation).

  Option (b) is cheap and makes the lessons in addendum A1 operational. Also have the Reporting manifest carry a mandatory "human validation: none / description" field, since GUIDE-LLM asks for it.

---

## HIGH

### H1. Screening thresholds: no owner, no metric, no timing, and they are set before any Protocol exists
- **Where:** FR-6, FR-9, FR-18, UJ-1 vs UJ-2, FR-7.
- **Problem:**
  - FR-6 says "below a threshold" and FR-9 says "against a threshold", with no metric, default, owner or timing given.
  - UJ-1 builds and screens the Panel once, before any Study's Protocol is written (UJ-2). The Panel is then reused across Studies (memlog). That means the exclusion decisions are made outside, and before, every preregistration that relies on them. Each Study inherits exclusions it never preregistered.
  - Nothing stops the operator from re-screening with a looser threshold after seeing how many Agents fail.
  - FR-6's fidelity check is also close to circular. Asking an Agent to self-report Big Five right after handing it a Big Five card tests instruction following, not whether the Persona shows up in perceptual judgments.
- **Fix:**
  - Specify the fidelity metric (e.g. a per-trait directional match on the BFI-10 and NARS short forms, scored against the card's high/low labels), give a default threshold, and require the threshold in a locked Panel-level `panel_lock` with its own hash.
  - Every Study's Protocol lock references that Panel lock hash.
  - Log every screening run, including superseded ones, and show re-screens in the Rater-flow report so threshold shopping is visible.
  - Name the circularity as a known limitation, or add a behavioural fidelity probe (e.g. text vignettes with known trait-dependent responses).

### H2. Blinding leaks through clip bytes, metadata, filenames sent to providers, and clip content
- **Where:** FR-10, NFR-2, §3 "Blinding key".
- **Problem:** FR-10 "copies the Clip". A byte copy keeps:
  - container metadata (MP4 `©nam` title, `creation_time`, encoder tags, GPS, and the original filename, which is often embedded by editors);
  - systematic differences by Condition in duration, resolution, bitrate, loudness or codec when ablations are recorded or exported differently.

  Models can read some of this (duration and audio level certainly). Providers also receive a filename or display name on upload; the Gemini File API takes a `display_name`, and it must not be derived from the original file.

  The Condition can also leak through content, for example an ablation that visibly removes a component, or a lab whiteboard reading "condition B". The tool cannot fix that, but the PRD should name it.

  Finally, "blinding" covers the Model only. The operator knows Conditions at push time and runs pilots, so operator blinding is not addressed, and a reviewer will ask about it.
- **Fix:**
  - FR-10 must **re-mux and normalize** each Clip rather than copy it: strip all metadata, standardize the container and codec, and optionally normalize loudness and resolution under a Study setting.
  - Emit a per-Condition **leak report** at push or lock time covering duration, resolution, bitrate and loudness, with a warning when they differ significantly by Condition.
  - Testable consequence: "No byte sequence of the original filename or Condition label appears in the stored Clip or any provider upload request."
  - Add a §6 or §9 line that content-level Condition cues are the Study's responsibility.

### H3. "Session" is undefined at the request level, so carryover, context growth and pairwise/Likert order effects are unbounded
- **Where:** §3 "Session", FR-14, FR-15, FR-16, FR-3.
- **Problem:** It is unclear whether a Session is one multi-turn conversation in which all Clips accumulate, or a set of independent single-Clip requests that share a briefing.
  - **If it is a conversation:**
    - Clip k is rated with Clips 1..k-1 in context. That causes anchoring and contrast effects.
    - Context and cost grow quadratically: 20 clips of video at about 100 tokens/s, re-sent every turn.
    - It may exceed context limits.
    - Mixing pairwise and Likert in the same Session means pairwise exposure contaminates the later Likert ratings, and vice versa.
  - **If they are independent requests:** "Practice clips brief the Agent" (FR-16) does nothing unless the practice turns are prefixed into every request.

  The PRD says nothing about Instrument order within a Session, pairwise-before or after Likert, or Item order within Godspeed (A1: option reordering flips 9–23% of answers).
- **Fix:**
  - Define a Session explicitly. Recommended: each trial is an independent request containing the fixed briefing, the Practice exemplars, and one Clip or one pair.
  - Make Instrument block order and Item order randomized or counterbalanced from the recorded seed, with the order recorded in the Export.
  - If multi-turn is ever allowed, make it an explicit, locked Protocol setting with `trial_index` exported.
  - Add a testable consequence: "Each Archive request contains at most the briefing, Practice exemplars and the current trial's Clip(s)."

### H4. Cost estimate method is unspecified; SM-3 (±20%) and the hard ceiling are not buildable as written
- **Where:** FR-13, FR-17, NFR-5, SM-3.
- **Problem:**
  - Nothing says how the estimate is computed. Video token counts depend on fps, resolution, audio tokenization, clip duration and provider pricing tiers, and pairwise trials double the video input. Retries, invalid responses, Practice clips, Catch trials and Repeats multiply it.
  - Provider pricing changes, and neither provider bills in real time, so "spend" during a Run is the tool's own estimate. "No request is sent after the ceiling" is impossible with concurrent in-flight requests unless spend is **reserved** before dispatch.
  - SM-3 compares the estimate with an "actual cost" that the tool cannot observe.
- **Fix:**
  - Estimate equals the sum over planned requests of the provider's token-count endpoint result (Gemini `countTokens`; DashScope's equivalent or a documented formula) times a pinned price table versioned in the repo, plus a retry allowance.
  - The ceiling check reserves a worst-case cost per request before dispatch.
  - Define "actual cost" as the sum of the `usage` fields returned by the provider, logged per request in the Archive.
  - SM-3 then compares the estimate against the summed usage.

### H5. Missing failure modes: refusals, safety filters, rate limits, upload processing, and runs that can never finish
- **Where:** FR-17, FR-21 ("Export refuses while any Session is incomplete"), NFR-8, FR-19.
- **Problem:**
  - **Safety refusals.** Video of robots interacting with people, possibly minors, can trigger safety blocks, empty candidates or `finishReason=SAFETY`. These are not schema failures. The PRD folds everything into "invalid".
  - **Differential refusal.** Refusals may correlate with the Persona text (age, gender, region) or with the Condition. That is differential attrition: a confound a reviewer will catch, and the Rater-flow report has no category for it.
  - **Rate limits and quotas.** 429 errors, daily quotas, and the Gemini File API's upload processing state (`PROCESSING` → `ACTIVE`, plus file expiry after 48h) are absent.
  - **Mid-run changes.** Model deprecation mid-run and a DashScope regional or endpoint outage are absent.
  - **Runs that can't finish.** FR-21 blocks Export until every Session is complete. A Session that fails permanently, or a Run paused at the ceiling the operator won't raise, makes the Study **unexportable forever**.
- **Fix:**
  - Add a terminal Session state `failed` with reason codes: `refused_safety`, `schema_invalid`, `rate_limited_exhausted`, `provider_error`, `model_unavailable`.
  - Export proceeds once every Session is in a terminal state. `failed` Sessions are reported in the Rater-flow report, broken down by Persona attributes and Condition so differential attrition is visible.
  - Specify retry and backoff for 429/5xx separately from `max_retries` for schema failures.
  - Re-upload expired provider files automatically.
  - Optionally add `export --partial` producing a clearly marked non-final file.

### H6. Pilot/main separation can be gamed, and nothing prevents piloting on main Clips
- **Where:** FR-12, UJ-2, SM-C1, SM-C2.
- **Problem:**
  - Test kind is self-declared, so a main analysis can be run as `pilot`, inspected, and then repeated as `main`.
  - Pilots can open after the lock and can use the main Clips, so prompts can be tuned against the very stimuli the main Test rates, and the lock hash still matches (see C1).
  - SM-C2 ("don't skip the pilot") cannot be measured, because nothing records whether a pilot happened.
- **Fix:**
  - Record every opened Test, of every kind, with timestamps in an append-only ledger.
  - The Reporting manifest lists all pilot and screening Tests run before and after the lock, with their Clip IDs.
  - Option, set by the Study: forbid pilot Tests from using Clip IDs that appear in any main Test, or at least flag that overlap in the manifest.
  - After the lock, `open` on a pilot warns that it will be disclosed.

### H7. The reproducibility claims contradict v1 scope and are untestable as phrased
- **Where:** FR-22 ("a Run can be re-executed from the Archive and Study folder alone"), NFR-3, §4.3, Risk row 4 ("reproducible by self-hosting").
- **Problem:**
  - Re-executing against a cloud API is not reproducing. The output will differ (Gemini nondeterminism, A1).
  - The "open weights" argument depends on self-hosting, but v1 has no vLLM or local adapter. DashScope's hosted "Qwen3-Omni" may be quantized, re-versioned or served with different frame sampling than the released weights, and the PRD does not establish that the DashScope model ID maps to a specific weights checkpoint.
  - Pinned "frame rate and audio handling" cannot be verified for server-side preprocessing.
- **Fix:**
  - Split FR-22's consequence into **re-executable** (the same requests can be re-sent from the Archive; testable with the Fake rater) and **re-analyzable** (the Export is regenerated byte-identically from the Archive). Drop any implied output reproducibility.
  - Record the DashScope model ID and the claimed weights revision, and state in §9 that self-hosted reproduction is unverified in v1.
  - Either add an OpenAI-compatible endpoint adapter (vLLM serves one) so self-hosting needs no new code, or remove "reproducible by self-hosting" from the Risk table.

### H8. Unit of analysis and pseudo-replication are never named
- **Where:** §3 "Agent", FR-4, FR-13, FR-21 Export schema, §6 ("no statistical analysis").
- **Problem:**
  - 64 Agents on one Model are 64 prompts to one set of weights. They are not 64 independent participants.
  - A reviewer will reject any analysis treating `agent_id` as N=64 subjects, with Repeats as further replication.
  - "No stats in the tool" is a decided scope, but the Export and Rater-flow report still frame Agents as participants ("CONSORT-style"), and the PRD never says what N is. That invites exactly the misuse a reviewer will punish.
- **Fix:** Add a §9 risk and a manifest field stating the nesting structure (Repeat within Agent, Agent within Persona and Model, Model) and recommending mixed models with Model as a fixed effect or a cross-Model replication. Make the Export carry `persona_id` and `model` as separate columns so the nesting can be modelled. The tool still does no stats.

---

## MEDIUM

### M1. The Panel's scope contradicts itself: per Study or across Studies?
- **Where:**
  - §3 "Study folder" (holds board state) and "Panel" ("the set of screened Agents a Study rates with … reused across Tests");
  - FR-7 ("valid for every Test in the Study");
  - UJ-1 ("in a new study folder … reuse for every study");
  - §1 ("then runs study after study against it");
  - memlog ("built once and reused across many studies").
- **Problem:** The engineer cannot decide where Panel state lives or how a second Study references it. The speed thesis (SM-1, "existing Panel") depends on cross-Study reuse, which the FRs do not provide.
- **Fix:** Introduce a first-class `panel/` artifact, either outside Study folders or exportable, with its own ID and lock hash. Add an FR for `consortium panel use <panel-id>`, which records the Panel hash in the Study. FR-7 then refers to that.

### M2. Sampling-frame balance is arithmetically underspecified, and exclusions break it
- **Where:** FR-4, FR-6, FR-19.
- **Problem:**
  - 32 × 2 = 64 cells fully use the budget. "Balanced across age band, gender, cultural region and robot experience" cannot be fully crossed. It can only be marginally balanced, by an unstated algorithm, and the result depends on how many levels each factor has, which is not given.
  - After fidelity exclusions, cells empty out and the design is no longer balanced. There is no replacement rule.
  - The FR-4 consequence ("quota counts match the frame") is untestable without the quota algorithm.
- **Fix:** Specify the level counts for each attribute, the assignment algorithm (e.g. a seeded Latin-square or marginal-balance assignment), and a replacement policy: none, resample within cell, or oversample at generation. Report post-exclusion balance in the Rater-flow report.

### M3. Perception screening tests the wrong construct
- **Where:** FR-9, §4.3 description, Risk row 3.
- **Problem:**
  - The FR-9 examples ("is the robot moving?", "is there speech?") test low-level detection.
  - Risk row 3 claims construct-relevant screening against inversion of constructs like "strangeness". Passing "is there speech" says nothing about whether the Model's "aliveness" or "presence" ratings track the construct.
  - Screening is pass/fail per Model with only two Models, so one failure removes the robustness arm entirely, and there is no rule for what happens then.
  - Screening Clips also need ground truth from somewhere, and nobody is named as the source of the known answers.
- **Fix:**
  - Require Perception screening Items to be declared per Instrument construct. Make a known-groups check the default for each construct (e.g. Clips manipulated to be clearly more or less animate, from which the Model must recover the direction).
  - Say who authors the ground truth, and lock it.
  - State the behaviour when a Model fails: Study-level decision, recorded in the manifest.

### M4. The Export schema is too thin for the analyses the PRD itself demands
- **Where:** FR-21, FR-15, FR-17, FR-18, FR-3, SM-C3.
- **Problem:** The listed columns lack:
  - `persona_id` separate from `agent_id`;
  - `session_id`, `trial_index` and `instrument_order`;
  - `is_catch` (Catch trials are mixed in with data), `excluded` and `exclusion_reason`;
  - `invalid` and `retry_count`, `refusal`;
  - for pairwise, the *pair* (left and right clip IDs, rather than a single `clip_id`) and the counterbalance arm;
  - the free-text justification (FR-3 defines it; the `response` type is unstated);
  - `model_version` separate from `model`, the seed, and the Protocol lock hash.

  It is also unclear whether excluded Agents' rows are dropped or flagged. Dropping them silently prevents sensitivity analysis.
- **Fix:** Publish the Export schema as a versioned artifact with these columns. Flag exclusions rather than deleting rows. Add a consequence: "the Export can reproduce every count in the Rater-flow report."

### M5. Several success metrics cannot be measured by anything the PRD specifies
- **Where:** SM-1, SM-2, SM-4, SM-5, NFR-8.
- **Problem:**
  - SM-1 measures "operator time", but nothing records it, and wall-clock time (NFR-8, 4h) is a different quantity.
  - SM-4 needs a stranger and a stopwatch, with no protocol for either.
  - SM-5 sets a "< 5%" target with no consequence for missing it: is it a gate or an observation?
  - SM-2 is undermined by C2.
  - NFR-8's 4h for 64×2 Agents on ~20 Clips gives no request count. With pairwise in both orders (190 pairs × 2 for 20 Clips) plus 3 Repeats, it could be about 100k video requests. That is untestable against unknown rate limits, and probably wrong by an order of magnitude.
- **Fix:**
  - Have the tool log Test timestamps (push, lock, open, complete, export) so SM-1 is the lock → export span.
  - Define SM-4 as a scripted CI job on a clean container with a time budget.
  - Make SM-5 a reported metric with a Protocol-declared exclusion rule.
  - Require the pairing plan to state how pairs are selected (all pairs, a within-Condition subset, or a balanced incomplete block), and restate NFR-8 in requests per hour at a stated quota.

### M6. Uploading participant video to third-party APIs is unaddressed (ethics and data residency)
- **Where:** NFR-7, FR-8, Q4.
- **Problem:** NFR-7 says data leaves the machine "only for the configured Model providers". That is exactly where an ethics board will push.
  - HRI clips often contain identifiable humans.
  - Google may retain or use data depending on API tier.
  - Alibaba Cloud Model Studio processes data in regions that may conflict with the participant consent or GDPR terms of the originating lab (Lena, UJ-3).
  - Reviewers increasingly ask for this.
- **Fix:** Add an NFR or Non-Goal stating that the tool sends Clips to providers and that consent and data-processing compliance is the Study's responsibility. Have `push` or the first `open` per provider print a one-time data-egress notice, record the provider region and endpoint in the manifest, and record the acknowledgment.

---

## LOW

### L1. Leftover ambiguities and loose ends in individual FRs
- **Where:** FR-13, FR-20, FR-10, FR-5, FR-11.
- **Problem:**
  - FR-13 "assigns every Clip to every screened Agent". That contradicts Tests that select Clips and Instruments, and ignores pairwise plans.
  - FR-20 specifies a web page that §7.2 removes from the MVP, so the FR is half out of scope.
  - FR-10 allows a single `--condition` label. Factorial designs (e.g. ablation × robot) need multiple factors.
  - FR-11 has no testable consequences at all.
  - FR-5 generates demographic portraits from Persona attributes. That is a stereotype-rendering risk in a published figure, and nothing guards it beyond "humans only".
- **Fix:**
  - FR-13 should read "assigns per the Test's plan".
  - Move the web page into a deferred FR.
  - Allow `--condition key=value` multiple times.
  - Give FR-11 consequences (rejects unknown Clip ID; idempotent re-push).
  - Note in Q6 that photo prompts are logged and the figure caption must disclose that the photos are synthetic.

---

## Contradictions index (quick reference)

| # | Location A | Location B | Conflict |
|---|---|---|---|
| 1 | FR-18 "threshold set in the Protocol" | FR-12 Protocol is free-text `protocol.md` | The tool can't read a threshold out of prose |
| 2 | Risk row 1 "optional human anchor" as mitigation | §6 Non-Goal: human anchor not the tool's job | The mitigation is out of scope |
| 3 | Risk row 4 "reproducible by self-hosting" | §4.3 / FR-8: cloud APIs only, no local adapter | The claimed path doesn't exist in v1 |
| 4 | §3 Panel per Study; FR-7 "every Test in the Study" | §1, UJ-1, memlog: reused across many Studies | The Panel's storage scope is undefined |
| 5 | FR-13 "every Clip to every Agent" | §3 Test defines which Clips and a pairing plan | Assignment rule |
| 6 | FR-21 Export blocks on incomplete Sessions | FR-17 pause at ceiling; FR-18 exclusions | A permanently failed or paused Run can never export |
| 7 | FR-20 includes the web page | §7.2 web page out of MVP | Scope |
| 8 | SM-2 test–retest target | Source spec temperature 0 | Retest is degenerate or measures provider noise |
