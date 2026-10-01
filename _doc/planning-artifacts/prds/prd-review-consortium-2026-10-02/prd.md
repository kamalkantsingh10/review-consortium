---
title: "PRD: Review Consortium"
status: final
created: 2026-10-02
updated: 2026-10-02
---

# PRD: Review Consortium

## 0. Document Purpose

This PRD defines Review Consortium for its builder and sole operator, Kamal, and for the architecture, epics and stories work that follows. Its primary source is the *AI Rater Board — Repo Spec* (2026-10-02; local copy `input-repo-spec.md`). Decisions made since that spec are in `.memlog.md`. Research and architecture-level detail are in `addendum.md`.

Terms in §3 are used exactly as defined. Features (§4) nest globally numbered FRs. Cross-cutting NFRs are in §5. Every inference not yet confirmed is tagged `[ASSUMPTION]` and indexed in §11.

## 1. Vision

Review Consortium is an open-source command-line tool. It runs a panel of persona-conditioned AI raters over video clips the way a careful researcher runs a human rater panel: recruited, screened, briefed, blinded, randomized and reported. Clips and a Test definition go in; one tidy CSV comes out.

Its reason to exist is **speed and scale**. A human panel study takes weeks of recruiting, scheduling and paying participants, so researchers run few studies and test few variants. With Review Consortium, a researcher builds and screens a Panel once per Study and runs Test after Test against it. Each Test goes from Clips to CSV in hours rather than weeks. A new Study can start from an existing Panel instead of rebuilding one.

Speed is worthless if a reviewer can dismiss the results. Every feature therefore maps to a step a reviewer would expect from a human panel (§4, table). Anything that can't be defended that way stays out, or is an operator convenience that never touches rating data.

The first Study is OLAF "aliveness" (an HRI robot paper). The tool contains no OLAF code; OLAF is just a Study folder.

## 2. Target User

### 2.1 Jobs To Be Done

- **Functional:** run a blinded perception Test on a set of video Clips and get analysis-ready ratings the same day.
- **Functional:** run many variants (Conditions, ablations, Instruments) without a new recruitment or setup cycle each time.
- **Social:** defend the method in peer review. Each Export ships with the evidence a methods reviewer asks for.
- **Contextual:** work alone, on a laptop, on a researcher's budget, with cost known before spending.

### 2.2 Non-Users (v1)

- Teams that need a hosted, multi-user platform.
- Anyone collecting human ratings: Review Consortium rates with AI Agents only.
- Users with no technical background. The interface is a CLI and YAML files.

Others may download and use the tool as is. There is no support or adoption commitment.

### 2.3 Key User Journeys

- **UJ-1. Kamal builds a Panel.** In a new Study folder, Kamal generates 64 Persona cards from a seeded Sampling frame and runs Persona-fidelity and Perception screening against thresholds set in `study.yaml`. Agents that fail are excluded with reasons recorded. He reuses this Panel for every Test in the Study. For his next Study, he copies this Panel rather than rebuilding it.
- **UJ-2. Kamal runs a new Test in a day.** OLAF records three ablation Conditions. Kamal runs `consortium push clip` for each Clip with its Condition and gets anonymized Clip IDs. He pilots on separate Clips, then runs `consortium protocol freeze`. Next he runs `consortium open`, sees the cost estimate and confirms, watches `consortium status`, and runs `consortium export`. By evening he has a CSV with Conditions rejoined, a Rater-flow report and a Reporting manifest.
  - **Edge case:** he edits an Instrument prompt after freezing. `open` refuses the main Test and names the changed file.
- **UJ-3. Lena reruns it from the paper.** Lena, a PhD student in another HRI lab, reads the paper and clones the repo. She installs it, runs the bundled example Study with the Fake rater in minutes at no API cost, then points it at her own Clips and API keys.

## 3. Glossary

- **Study:** one research question's complete setup and results, held in one Study folder. One install serves many Studies.
- **Study folder:** a directory outside the repo, passed in by path. It holds the Study's config, Panel, Protocol, Tests, board state, Blinding key, Archive and Exports.
- **Panel:** the set of screened Agents a Study rates with. It lives in the Study folder and is reused across all Tests in that Study. It can be copied into a new Study.
- **Persona:** a synthetic rater profile drawn from the Sampling frame (Big Five profile, NARS band, age band, gender, cultural region, robot experience).
- **Sampling frame:** the seeded quota plan that defines which Personas exist.
- **Persona card:** the text description of a Persona given to the Model.
- **Persona photo:** an optional generated portrait of a Persona, for human viewing only. Never sent to any Model.
- **Model:** a pinned vision-language model (provider, version and sampling settings) that accepts video with audio.
- **Agent:** one Persona run on one Model (e.g. `p17-m2`). Agents that share a Model are not independent of each other (see NFR-10).
- **Screening:** a gate passed before rating, with thresholds set in `study.yaml`. Two kinds: **Persona-fidelity screening** checks each Agent answers like its Persona; **Perception screening** checks each Model can perceive the construct each Instrument asks about.
- **Clip:** a video file with audio, re-encoded and stored under an anonymized **Clip ID**.
- **Condition:** the experimental label of a Clip, given as one or more `factor=level` pairs. Stored only in the Blinding key.
- **Blinding key:** the file mapping Clip IDs to Conditions. The rating side never reads it.
- **Instrument:** a questionnaire defined in YAML (e.g. Godspeed, pairwise, presence), made up of **Items**. It may declare **Prompt variants** (reworded or reordered wordings).
- **Test:** a YAML definition of which Clips are rated on which Instruments, with a Pairing plan and Repeats. Kind is `main`, `pilot` or `screening`.
- **Pairing plan:** for pairwise Instruments, which Clip pairs are compared (e.g. `all_pairs`). Each pair is shown in both orders.
- **Trial:** one fresh, independent request to a Model. It carries the Persona card, briefing, Practice clips and one Clip (or one ordered pair), and asks for that Instrument's Items.
- **Session:** all Trials of one Agent for one Test and one Repeat.
- **Repeat:** an independent re-run of a Session with a different recorded seed and, optionally, a different Prompt variant.
- **Practice clip:** a Clip shown with its intended answer inside the briefing. Never exported as data.
- **Catch trial:** a Trial with a known correct answer, used to detect inattentive or non-perceiving Agents.
- **Protocol:** the Study's pre-registration document (`protocol.md`).
- **Protocol lock:** a hash over everything that defines how data is produced (FR-13). A main Test can't open without a valid one.
- **Run:** one execution of an opened Test.
- **Archive:** every raw request and response from a Run.
- **Export:** the tidy CSV of a Test whose Sessions have all ended, with Conditions rejoined.
- **Rater-flow report:** a CONSORT-style account of Personas generated, screened out, failed, excluded and analyzed, with reasons.
- **Reporting manifest:** a file that answers the GUIDE-LLM reporting items the tool can know for a Run.
- **Fake rater:** a built-in Model that returns synthetic answers at no cost, for testing the pipeline.

## 4. Features

Every feature maps to a human-panel step. Conveniences that map to none never touch rating data.

| Feature | Human-panel step |
|---|---|
| 4.1 Study setup | Study design and materials |
| 4.2 Panel building | Recruitment quotas, screening questionnaire |
| 4.3 Models and Perception screening | Vision and hearing test |
| 4.4 Rating board | Study platform, blinding, pre-registration |
| 4.5 Session runner | Briefing, practice, blinded randomized trials |
| 4.6 Data quality and rater flow | Attention checks, exclusions, CONSORT flow |
| 4.7 Status and Export | Data collection monitoring, data release |
| 4.8 Open-source distribution | Materials sharing |
| Persona photos (FR-6), web status page (FR-23) | None: operator conveniences, never seen by a Model |

**Interface rule:** the CLI and the files in the Study folder are the only interface. Outside tools (OLAF, analysis scripts) use nothing else, and no code is imported across that line. Command names are indicative; architecture finalizes them in `INTERFACE.md` (FR-27).

### 4.1 Study setup

**Description:** A Study is input, not code. `study.yaml` sets the Panel, Models, Instruments, Session defaults and all thresholds. Each Test is its own YAML. Instruments are user-defined so many different studies can run without code changes. Realizes UJ-1, UJ-2, UJ-3.

#### FR-1: Initialize a Study

`consortium init <path>` creates a Study folder from a template containing `study.yaml`, `protocol.md` and an example Test.

**Consequences (testable):**
- The initialized folder passes validation without edits, using the Fake rater.

#### FR-2: Validate Study config

The tool validates `study.yaml` and each Test YAML against a published, versioned schema before any work runs.

**Consequences (testable):**
- An unknown Instrument, unknown Clip ID, malformed Pairing plan, unpinned Model version or missing threshold is rejected, with the file, field and reason.

#### FR-3: User-defined Instruments

The operator can define Instruments in YAML: Likert Items (scale size and anchors), pairwise questions, free-text justification fields and Prompt variants. Each Instrument gets a response schema.

**Consequences (testable):**
- Godspeed (animacy, likeability), pairwise "Which one feels more alive?" and a 7-point presence scale ship as built-in Instruments, defined in that same YAML format.
- A new Likert Instrument added in YAML runs end to end with no code change.

**Out of Scope:**
- Branching or skip logic inside an Instrument.

### 4.2 Panel building

**Description:** The Panel is generated from a seeded Sampling frame, screened, and reused for every Test in the Study. Copying a Panel into a new Study keeps starting a new Study cheap. Screening thresholds are written in `study.yaml` before screening runs and are frozen by the Protocol lock. Every screening run, including superseded ones, is logged, so threshold shopping is visible. Realizes UJ-1.

#### FR-4: Generate Personas from a Sampling frame

`consortium personas generate` creates Persona cards from a seeded Sampling frame with quotas. By default: all 32 Big Five high/low profiles × 2 NARS bands = 64 Personas, with age band, gender, cultural region and robot experience assigned to balance marginal counts.

**Consequences (testable):**
- The same seed and frame produce byte-identical Persona cards.
- Marginal quota counts in the output match the frame.
- The Rater-flow report shows quota balance after exclusions.

#### FR-5: Persona-fidelity screening

`consortium screen personas` gives each Agent a short-form Big Five and NARS questionnaire. It scores directional match per trait against the Persona card. Agents below the `study.yaml` threshold `[ASSUMPTION: default ≥ 80% of traits matched]` are excluded.

**Consequences (testable):**
- Each Agent's score, its pass or fail, the threshold and the screening run ID are recorded and appear in the Rater-flow report.

**Notes:** This checks that a Persona is being followed, not that it shapes perception. Construct-level checks belong to Perception screening (FR-10).

#### FR-6: Persona photos (optional)

The operator can generate one Persona photo per Persona with an image-generation model `[ASSUMPTION: Google's image models]`. Photos appear on Persona cards in the status page and can be exported for figures.

**Consequences (testable):**
- No request in the Archive contains a Persona photo or a reference to one.
- A Study without photos runs normally.

#### FR-7: Reuse and copy the Panel

A screened Panel stays valid for every Test in its Study until the operator rebuilds it, a Model's pin changes, or an Instrument changes. `consortium panel copy --from <study>` copies Persona cards and screening results into a new Study and records their source and hash.

**Consequences (testable):**
- Changing a Model pin invalidates that Model's screening; changing an Instrument invalidates Perception screening for it. `open` refuses until screening is re-run.
- A copied Panel records its source Study and hash in the Reporting manifest.

### 4.3 Models and Perception screening

**Description:** Raters are vision-language models that take video with audio natively. v1 uses two providers, Gemini and hosted Qwen (`qwen3.8-omni-flash`), both via cloud API with no local GPU (rationale in addendum A1–A2). Persona supplies rater diversity; the second Model supplies robustness. Perception screening checks that each Model perceives the *construct* each Instrument measures, not just low-level events. Published HRI work found models invert constructs such as "strangeness". Realizes UJ-1.

#### FR-8: Model adapters with pinned settings

The tool has one adapter per provider. Each pins model version, temperature, frame rate, audio handling and seed (where supported). Temperature is above 0 `[ASSUMPTION: default 0.7; the pilot sets the final value]`, so Repeats measure rater stability rather than replay identical output.

**Consequences (testable):**
- The Gemini API and hosted `qwen3.8-omni-flash` both work in v1. Switching Qwen to self-hosted open weights is a configuration change.
- Any Test can run on one Model or several. Whether to replicate a finding across Models is a Protocol decision, not a tool behavior.
- The Fake rater is always available and needs no network.
- Pinned settings and the provider's reported model build are written to the Archive for every request.

#### FR-9: Provider failure handling

Transport errors and rate limits are retried with backoff. Safety refusals are recorded as `refused`, not retried indefinitely.

**Consequences (testable):**
- Every Trial ends in exactly one terminal state: `valid`, `invalid`, `refused` or `failed`.
- Refusal and failure rates are reported per Model, per Persona attribute and per Condition, so differential attrition is visible.

#### FR-10: Perception screening

`consortium screen models` runs a `screening` Test. Every construct used by a main Test's Instruments must have at least one screening Clip pair with a known direction (e.g. clearly more vs. less "alive"). A Model passes for an Instrument only if it ranks those pairs in the right direction at or above the `study.yaml` threshold. Low-level checks ("is the robot moving?", "is there speech?") are also included.

**Consequences (testable):**
- A Model that fails for an Instrument can't rate that Instrument in a main Test.
- `open` refuses a main Test whose constructs lack screening coverage.
- Results per Model and Instrument appear in the Rater-flow report.

### 4.4 Rating board

**Description:** The board holds Clips, the Blinding key, Tests and assignments for a Study. It enforces blinding and "protocol before data" structurally, not by convention. Realizes UJ-2.

#### FR-11: Push a Clip

`consortium push clip <file> --condition <factor=level> [...]` re-encodes the Clip to a canonical format, strips all metadata, stores it under a random Clip ID, and writes the Condition only to the Blinding key.

**Consequences (testable):**
- No source filename, metadata tag or Condition string appears in the stored file, in board state outside the Blinding key, or in any provider upload name.
- A per-Condition leak report flags differences in duration, loudness, resolution and frame rate across Conditions.

**Out of Scope:**
- Leaks in Clip *content* (e.g. a visible lab sign). The Study is responsible for that.

#### FR-12: Push a Test

`consortium push test <test.yaml>` validates Clip IDs, Instruments and the Pairing plan, then returns a Test ID.

**Consequences (testable):**
- An invalid Test is rejected with file, field and reason. A valid one returns a stable Test ID.
- A Clip used in a `pilot` or `screening` Test can't be used in a `main` Test, and vice versa.

#### FR-13: Protocol lock

`consortium protocol freeze` hashes everything that defines how data is produced:
- `protocol.md`
- `study.yaml`
- Test YAMLs
- Instrument definitions
- prompt templates and Prompt variants
- Persona cards
- screening results
- Model pins
- all thresholds
- the price table

`consortium open` refuses a `main` Test unless the current files match a valid lock. `pilot` and `screening` Tests can open at any time.

**Consequences (testable):**
- Editing any locked file after freezing makes `open` refuse main Tests, naming the changed file.
- Re-freezing creates a new lock version. Each Export records the lock it ran under, and the Reporting manifest lists every lock version.
- Pilot and screening rows never appear in a main Export.

#### FR-14: Open a Test with a cost estimate

`consortium open <test>` shows the planned Trial count and estimated cost, asks for confirmation, assigns every Clip to every screened Agent, and starts the Run. The estimate counts every planned request (both pairwise orders, Practice clips, Repeats) using the provider's token-count method and a versioned price table, plus a retry allowance.

**Consequences (testable):**
- `open` refuses if the estimate exceeds the Study's cost ceiling.
- `open` names which providers will receive Clips.

### 4.5 Session runner

**Description:** Each Trial is its own fresh request. It briefs the Agent with its Persona card, the Instrument's instructions and the Practice clips, then presents one Clip or one ordered pair. Nothing carries over between Trials, so order and carryover effects can't build up. Realizes UJ-2.

#### FR-15: Independent Trials

Every Trial is a fresh request. No Agent sees another Agent's answers or any earlier Trial's output.

**Consequences (testable):**
- No request in the Archive contains content from any other Trial.

#### FR-16: Randomization and counterbalancing

Trial order is randomized per Session from a recorded seed. Each Clip pair is presented in both orders as separate Trials, and position is recorded.

**Consequences (testable):**
- The same seed reproduces the same order.
- The Export includes position, so position bias can be measured.

#### FR-17: Briefing, Practice clips, Repeats and Prompt variants

Each Trial includes the configured Practice clips with their intended answers `[ASSUMPTION: default 2; the pilot weighs their cost]`. Each Session runs the configured number of Repeats `[ASSUMPTION: default 3]`, each with a new recorded seed. When an Instrument declares Prompt variants, Repeats rotate through them.

**Consequences (testable):**
- Practice clip answers are archived but never exported.
- The Export records seed and Prompt variant per row.

#### FR-18: Retries, invalid answers and cost ceiling

Responses failing the response schema are retried up to `max_retries` `[ASSUMPTION: default 2]`, then marked `invalid`. Spend plus the reserved cost of in-flight requests never exceeds the cost ceiling. When the ceiling is reached, the Run pauses. `consortium open --resume` continues at Trial level after the operator raises the ceiling.

**Consequences (testable):**
- No request is sent that could push committed spend over the ceiling.
- A paused or crashed Run resumes without repeating completed Trials.
- The invalid-answer rate per Agent and per Model is reported.

### 4.6 Data quality and rater flow

**Description:** The checks a human panel would get: attention checks, pre-declared exclusions, and an auditable account of who was dropped and why. Rows are flagged, never deleted. Realizes UJ-2.

#### FR-19: Catch trials

The operator can mark Clips in a Test as Catch trials with expected answers. Agents failing more than the `study.yaml` threshold are excluded.

**Consequences (testable):**
- An excluded Agent's rows stay in the Export, flagged with the reason code. They also appear in the Rater-flow report.

#### FR-20: Rater-flow report

For each Test, the tool produces a Rater-flow report:

1. Personas generated
2. Screened out on fidelity
3. Models screened out on perception, per Instrument
4. Trials refused or failed
5. Agents excluded (Catch trials, invalid answers)
6. Analyzed

Each step shows counts and reason codes, broken down by Persona attribute and Condition.

**Consequences (testable):**
- Counts at each step sum correctly, and every exclusion has a reason code.
- Every count can be reproduced from the Export alone.

### 4.7 Status and Export

**Description:** Progress is visible while a Run is going. Once it ends: one tidy CSV plus the evidence a reviewer will ask for. Realizes UJ-2, UJ-3.

#### FR-21: Status

`consortium status [<test>]` shows progress per Test, Model and Agent: valid, invalid, refused, failed, retried, and cost so far.

**Consequences (testable):**
- Status counts match the Archive.

#### FR-22: Export

`consortium export <test>` runs once every Session is in a terminal state. It joins responses with the Blinding key at that moment only. It writes one row per Item per Trial, to a versioned schema with these columns:
- identifiers: `agent_id`, `session_id`, `trial_index`, `clip_id`
- the rater: `persona_*`, `model`
- the Condition: one column per factor
- the answer: `instrument`, `item`, `response` (Likert value, or the chosen Clip ID for pairwise), `position`
- run details: `repeat`, `seed`, `prompt_variant`, `status`
- exclusions: `excluded`, `exclusion_reason`
- provenance: `test_kind`, `protocol_lock`, `timestamp` (ISO 8601)

**Consequences (testable):**
- Export refuses while any Session is still running.
- Condition columns come from the Blinding key at export time only.

#### FR-23: Local status page

A local, read-only web page shows the `status` view and Persona cards, with photos if they were generated.

#### FR-24: Archive and Reporting manifest

Every raw request and response is archived. Each Export ships with a Reporting manifest. The manifest auto-fills every GUIDE-LLM item the tool can know:
- Model, version, build and access date
- sampling settings and seeds
- number of Runs
- exact prompts
- the Protocol lock and Panel source
- Model providers that received Clips

It lists the items the author must supply, such as human validation.

**Consequences (testable):**
- The Archive and Study folder contain everything needed to re-issue every request byte-identically. This is verified with the Fake rater. Identical Model *outputs* are not promised.

### 4.8 Open-source distribution

**Description:** Anyone can download the tool and run it unaided. Realizes UJ-3.

#### FR-25: Install and example Study

The repo ships a README, an open-source license `[ASSUMPTION: permissive, e.g. MIT or Apache-2.0]` and a bundled example Study that runs with the Fake rater.

**Consequences (testable):**
- On a clean machine, install plus the example Study finishes using documented commands only.

#### FR-26: Protocol template

The repo ships a `protocol.md` template. Its sections cover the method choices a reviewer checks: Panel, Screening thresholds, Instruments, Catch trials, exclusions, Repeats and cross-Model replication.

#### FR-27: Interface contract

The repo ships `INTERFACE.md`, which documents every command, the Study folder files and the Export schema version. It also states the unit-of-analysis guidance (NFR-10).

## 5. Cross-Cutting NFRs

- **NFR-1 Independence:** no shared memory or context between Agents or Trials (FR-15).
- **NFR-2 Blinding by construction:** the rating side has no code path that reads the Blinding key (FR-11, FR-22).
- **NFR-3 Reproducibility:** Model versions and builds, sampling settings, frame rate, prompts, Persona cards and seeds are pinned and archived (FR-8, FR-24). Both v1 Models are closed and hosted, so reproducibility means pinned and archived, not re-runnable on weights.
- **NFR-4 Protocol before data:** enforced by the Protocol lock (FR-13).
- **NFR-5 Cost control:** an estimate before every Run and a hard ceiling that includes in-flight requests (FR-14, FR-18).
- **NFR-6 Resumability:** all state lives in the Study folder. Runs survive crashes and restarts (FR-18).
- **NFR-7 Local-first:** no telemetry. Data leaves the machine only for the configured Model providers.
- **NFR-8 Throughput:** Trials run concurrently within provider rate limits `[ASSUMPTION: a 64-Persona × 2-Model (128-Agent) main Test on ~20 Clips finishes within 4 hours]`.
- **NFR-9 Data handling:** Clips are uploaded to third-party providers. `open` names them (FR-14) and the Reporting manifest records them (FR-24). Consent and ethics for the Clip content are the Study's responsibility.
- **NFR-10 Unit of analysis:** Agents sharing a Model are not independent participants. `INTERFACE.md` says so and names Agent, Persona, Model and Clip as crossed factors for downstream analysis. The tool performs no statistics.

## 6. Non-Goals

- No hosted service, accounts or multi-user access.
- No collecting human ratings, and no claim that AI ratings stand in for humans. Criterion validity, such as a human anchor, belongs to the Study.
- No statistical analysis beyond the Export and its descriptive reports.
- No stimuli other than video with audio in v1 (images, audio-only and text are future work).
- No Persona photo or other Persona image is ever given to a rating Model.
- No detection of Condition leaks in Clip content.
- No OLAF-specific or paper-specific code.

## 7. MVP Scope

### 7.1 In Scope

Build order, each step usable on its own:

1. Board core: `init`, Clip store with re-encoding, Blinding key, `push`, `status`, `export`, with the Fake rater running end to end at no API cost.
2. Instruments and the first Model adapter, then the runner: fresh-context Trials, failure handling, retries and the cost ceiling.
3. Persona generator, both Screenings, Panel copy, and the second Model adapter.
4. Catch trials, exclusions, the Rater-flow report, the Protocol lock and the Reporting manifest.
5. Pilot on screening and pilot Clips, then freeze the Protocol and run the OLAF main Tests.

### 7.2 Out of Scope for MVP

- Persona photos (FR-6): not on the critical path to the first Study. **[NOTE FOR PM]** Pull in if time allows; it helps the paper figure.
- The local status page (FR-23): the CLI `status` comes first.
- Providers beyond two.

## 8. Success Metrics

**Primary**
- **SM-1 Turnaround:** with an existing Panel, a new main Test goes from Clips to Export in ≤ 1 day of operator time `[ASSUMPTION]`. Validates FR-11–FR-22.
- **SM-2 Defensibility:** every Export ships with a complete Reporting manifest and a Rater-flow report. The manifest has zero manual fields for tool-owned items, and every Rater-flow count reconciles with the Export. Validates FR-13, FR-20, FR-24.

**Secondary**
- **SM-3 Stability reporting:** within-Agent stability across Repeats (ICC) and variance across Personas are computed and reported for every main Test. The acceptable level is a Study threshold declared in the Protocol, not a tool target. Validates FR-17.
- **SM-4 Cost predictability:** actual Run cost is within ±20% of the `open` estimate. Validates FR-14.
- **SM-5 Stranger setup:** a new user runs the example Study from the README in ≤ 30 minutes. Validates FR-25.
- **SM-6 Data quality:** the invalid-answer rate is < 5% after retries. Validates FR-18.

**Counter-metrics (do not optimize)**
- **SM-C1 Agreement with expected or human results:** never tune prompts, Personas or thresholds after seeing main-Test data to raise agreement. That is p-hacking; the Protocol lock exists to stop it. Counterbalances SM-1.
- **SM-C2 Skipped safeguards:** don't hit turnaround by skipping the pilot, Screening or Catch trials. Counterbalances SM-1.
- **SM-C3 Artificial reliability:** very high stability with near-zero variance across Personas signals homogenized raters, not quality. Counterbalances SM-3.

## 9. Risks

| Risk | Mitigation |
|---|---|
| Reviewers reject AI raters as stand-ins for humans | Out of the tool's scope by design (§6). The tool supplies procedural evidence (Screenings, Catch trials, Rater-flow report, Reporting manifest). The Study decides on a human anchor. |
| Models stereotype Personas or exaggerate identity effects | Persona-fidelity screening; variance across Personas reported (SM-3, SM-C3); refusal rates by Persona (FR-9); no Persona photos to Models. |
| A Model can't perceive the construct (e.g. inverted "strangeness") | Construct-level Perception screening per Instrument (FR-10). |
| Clips leak their Condition | Re-encode, strip metadata, randomize names, per-Condition leak report (FR-11). |
| Thresholds loosened after seeing results | Thresholds in `study.yaml` under the Protocol lock; superseded screening runs logged (FR-13, §4.2). |
| Cloud Models drift or are deprecated | Pin versions and builds; archive every request; self-hosting open-weight Qwen3-Omni stays available as a config change (NFR-3). |
| Findings specific to one Model | The same Test can run on both Models; the Protocol decides whether to replicate. |

## 10. Open Questions

**For the tool**
1. Persona photos: a `consortium personas photos` command, or a separate Claude Code skill?
2. What is the full GUIDE-LLM core item list, and which items does the tool fill versus the author? (Feeds FR-24; see addendum A1.)
3. Is there a paper deadline that fixes the MVP date?
4. Should the tool be released with the paper as a citable artifact (DOI, e.g. Zenodo)? *(4 Oct)*

**For the OLAF Study (4 Oct, Dr. Alaa). These don't block the tool.**
5. Is the AI Panel the primary instrument, or a pre-screen before humans?
6. Are Godspeed (animacy, likeability), pairwise "more alive" and presence the final Instruments?
7. Is 64 Personas the right pool size?
8. Is a human validity anchor wanted, and does it need ethics approval?
9. Will the headline finding be replicated on the second Model?

## 11. Assumptions Index

- §4.2 FR-5: Persona-fidelity default threshold is ≥ 80% of traits matched.
- §4.2 FR-6: Persona photos use Google's image models.
- §4.3 FR-8: Default temperature is 0.7, with the final value set by the pilot.
- §4.5 FR-17: Defaults are 2 Practice clips per Trial and 3 Repeats.
- §4.5 FR-18: Default `max_retries` is 2.
- §4.8 FR-25: Permissive open-source license (MIT or Apache-2.0).
- §5 NFR-8: A 64-Persona × 2-Model (128-Agent) main Test on ~20 Clips finishes within 4 hours.
- §8 SM-1: ≤ 1 operator-day from Clips to Export with an existing Panel.
- Memlog: open-source scope means a README, install path, license and example Study, with no support commitment.
