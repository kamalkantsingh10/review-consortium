---
stepsCompleted: [1, 2, 3, 4]
inputDocuments:
  - _doc/planning-artifacts/prds/prd-review-consortium-2026-10-02/prd.md
  - _doc/planning-artifacts/prds/prd-review-consortium-2026-10-02/addendum.md
  - _doc/planning-artifacts/architecture/architecture-review-consortium-2026-10-02/ARCHITECTURE-SPINE.md
---

# Review Consortium - Epic Breakdown

## Overview

This document breaks Review Consortium down into epics and stories, built from the PRD and the Architecture spine (AD-1 to AD-14). There is no UX document, because the tool is a CLI. Where the PRD and the spine differ, the spine's refinement wins and the difference is noted below.

## Requirements Inventory

### Functional Requirements

FR1: `consortium init <path>` creates a Study folder from a template (`study.yaml`, `protocol.md`, an example Test). The result passes validation unedited, using the Fake rater.
FR2: Validate `study.yaml` and Test YAMLs against a published, versioned schema before any work. Reject unknown Instruments, unknown Clip IDs, malformed Pairing plans, unpinned Models and missing thresholds, giving file, field and reason.
FR3: User-defined Instruments in YAML: Likert Items (scale and anchors), pairwise questions, free-text justification and Prompt variants. Each has a response schema. Godspeed (animacy, likeability), pairwise "more alive" and 7-point presence ship built in. A new Instrument runs with no code change.
FR4: `consortium personas generate` creates Persona cards from a seeded Sampling frame. The default is 32 Big Five profiles × 2 NARS bands = 64, with age band, gender, region and robot experience balanced. Output is byte-identical for the same seed, quota counts match the frame, and post-exclusion balance is reported.
FR5: `consortium screen personas` runs a Persona-fidelity questionnaire (short-form Big Five and NARS), scored by directional trait match against the `study.yaml` threshold (default ≥ 80%). It records score, pass or fail, threshold and screening run ID.
FR6: Optional Persona photos via an image-generation model, for human viewing only. No request ever contains a photo. *(Out of MVP.)*
FR7: The Panel is reused across all Tests in a Study until it is rebuilt, a Model pin changes or an Instrument changes, any of which invalidates the relevant screening. `consortium panel copy --from <study>` copies Persona cards and screening results, recording their source and hash.
FR8: One adapter per provider, with pinned version, temperature (> 0, default 0.7), fps, audio handling and seed. The v1 providers are Gemini and hosted `qwen3.8-omni-flash`, plus the always-available Fake rater. Any Test can run on 1..N Models. Pinned settings and the model build are archived per request.
FR9: Transport errors and rate limits are retried with backoff. Refusals are recorded as `refused`. Every Trial ends in exactly one of `valid`, `invalid`, `refused` or `failed`. Refusal and failure rates are reported per Model, Persona attribute and Condition.
FR10: `consortium screen models` runs construct-level Perception screening, with screening Clip pairs of known direction for every construct a main Test uses, plus low-level checks. Models that fail can't rate that Instrument, and `open` refuses a main Test whose constructs lack coverage.
FR11: `consortium push clip <file> --condition factor=level [...]` re-encodes the Clip, strips metadata, stores it under a random Clip ID and writes the Condition only to the Blinding key. A per-Condition leak report covers duration, loudness, resolution and fps.
FR12: `consortium push test <test.yaml>` validates the Test and returns a stable Test ID. Clips can't be shared between pilot or screening Tests and main Tests.
FR13: `consortium protocol freeze` hashes everything that shapes data. `open` refuses a main Test on any mismatch, naming the changed file. Re-freezing creates a new lock version, and Exports record their lock. Pilot and screening rows never appear in main Exports.
FR14: `consortium open <test>` shows the planned Trial count and estimated cost, asks for confirmation, names the providers, assigns Clips to screened Agents and starts the Run. It refuses if the estimate exceeds the ceiling.
FR15: Every Trial is a fresh, independent request. No content from any other Trial appears in a request.
FR16: Trial order is randomized per Session from a recorded seed. Each Clip pair is shown in both orders as separate Trials, and position is recorded.
FR17: Each Trial includes the configured Practice clips (default 2) with their intended answers, which are never exported. Repeats (default 3) each get a new seed and rotate through Prompt variants. The Export records seed and variant.
FR18: Schema failures are retried up to `max_retries` (default 2), then marked `invalid`. Committed spend plus in-flight reservations never exceed the ceiling. The Run pauses at the ceiling, and `open --resume` continues at Trial level without repeating completed Trials. Invalid-answer rates are reported.
FR19: Catch trials have expected answers. Agents above the `study.yaml` failure threshold are excluded; their rows are flagged with a reason code and stay in the Export.
FR20: The Rater-flow report covers: generated, fidelity-screened, perception-screened (per Instrument), refused or failed, excluded, analyzed. It gives counts and reason codes by Persona attribute and Condition, and the counts sum correctly.
FR21: `consortium status [<test>]` shows valid, invalid, refused, failed, retried and cost per Test, Model and Agent. Counts match the Archive.
FR22: `consortium export <test>` runs once all Sessions are terminal and joins the Blinding key at that moment only. It writes one row per Item per Trial to a versioned schema (identifiers, rater, Condition factors, answer and position, repeat, seed, variant, status, exclusion flags, test kind, lock, timestamp).
FR23: A local, read-only status page with Persona cards. *(Out of MVP.)*
FR24: Every raw request and response is archived. A Reporting manifest auto-fills the GUIDE-LLM items the tool knows and lists the ones the author must supply. The Archive plus the Study folder can re-issue every request byte-identically, verified with the Fake rater.
FR25: README, open-source license and a bundled example Study that runs with the Fake rater. Install plus the example works from documented commands on a clean machine.
FR26: A `protocol.md` template covering Panel, thresholds, Instruments, Catch trials, exclusions, Repeats and cross-Model replication.
FR27: `INTERFACE.md` documents every command, the Study folder files, the Export schema version and unit-of-analysis guidance.

### NonFunctional Requirements

NFR1: Independence — no shared memory or context between Agents or Trials.
NFR2: Blinding by construction — the rating side has no code path to the Blinding key.
NFR3: Reproducibility — Model versions and builds, settings, fps, prompts, Persona cards and seeds are pinned and archived. Both v1 Models are closed, so this means pinned and archived, not re-runnable on weights.
NFR4: Protocol before data — enforced by the Protocol lock.
NFR5: Cost control — an estimate before each Run, and a hard ceiling that includes in-flight requests.
NFR6: Resumability — all state is in the Study folder, and Runs survive crashes and restarts.
NFR7: Local-first — no telemetry; data leaves the machine only for the configured providers.
NFR8: Throughput — Trials run concurrently within rate limits (assumption: a 128-Agent main Test on ~20 Clips finishes in ≤ 4 h).
NFR9: Data handling — providers receiving Clips are named at `open` and in the manifest. Consent for Clip content is the Study's responsibility.
NFR10: Unit of analysis — Agents sharing a Model are not independent. `INTERFACE.md` names the crossed factors. No statistics in the tool.

### Additional Requirements

From the Architecture spine (each story cites the ADs it must obey):

- **Scaffold (Epic 1, Story 1):** there is no third-party starter. Use `uv init --package` with Python ≥ 3.12, Typer 0.27, Pydantic 2.13, PyYAML 6, stdlib sqlite3, pytest 9, ruff 0.16 and import-linter 2.15. Install with `uv tool install`. ffmpeg ≥ 6 is a documented system dependency.
- **AD-1:** the module layout is `core/`, `engine/`, `stages/`, `raters/`, `board/`, `archive/`, `media/`, `config/`, `instruments/`, `templates/`. Dependency direction is enforced by import-linter contracts run as a pytest test.
- **AD-2:** only `board/blinding.py` touches `blinding_key.csv`, and only `stages/push` and `stages/export` may import it.
- **AD-3:** `board.db` (WAL) is the only mutable state. A single writer task handles writes, and dispatching commands hold the `board.lock` lease.
- **AD-4:** the Trial state machine, with `attempt` incremented before every dispatch and the highest valid attempt winning.
- **AD-5:** Archive before state. Records are append-only JSONL keyed by `(trial_id, attempt)` and reference media by clip_id and SHA-256.
- **AD-6:** every Model call, screening included, goes through `engine.dispatch`. The `Rater` port is `prepare / submit / collect`, with a per-provider `asyncio.Semaphore`.
- **AD-7:** `core.render` builds a provider-neutral `TrialRequest` with 0–2 Clips. Adapters never change text, and parsing and validation happen in core.
- **AD-8:** the lock is a per-file SHA-256 map plus a combined hash. It covers protocol, study, tests, prices, the Instruments in use, `panel/` (screening snapshot) and the tool version, but not the cost ceiling.
- **AD-9:** only `config/` reads YAML. Models are keyed by explicit `id`. The cost ceiling lives in `board.db` (`open --ceiling`), with every change logged.
- **AD-10:** seeds are derived from `study.seed` using natural keys, masked to 31 bits, with the attempt included in the Model seed.
- **AD-11:** ffmpeg canonical profile (about 480p, ≤ 400 kbps, so a 120 s Clip stays under ~7 MB). IDs are `c_` plus 8 base32 characters. Per-Model media limits are enforced at `push test`.
- **AD-12:** every Trial gets ≥ 1 Export row. Pairwise rows carry `pair_id` and `clip_id_a`/`clip_id_b`. Exclusions are computed at export. The Rater-flow report takes steps 1–3 from the screening snapshot and 4–6 from the Export. *This refines PRD FR20's "Export alone".*
- **AD-13:** one offline `core.cost` function (formulas in `prices.yaml` plus the pinned `max_output_tokens`). The ledger has one row per attempt, and nothing is uploaded before confirmation. *This refines PRD FR14's "provider token-count method".*
- **AD-14:** the Qwen adapter uses the OpenAI-compatible API, with `base_url` and `model` set in `study.yaml`. Media is sent as base64. Undocumented settings are flagged in the manifest.
- **Conventions:**
  - ID formats; UTC ISO 8601 timestamps; SHA-256 hex hashes.
  - `ConsortiumError(code, message, path?)`, with snake_case reason codes.
  - Data to stdout, logs to stderr, no telemetry.
  - API keys from environment variables only.
  - `--study <path>` on every command.
  - USD as decimal strings.
- **Pilot assumption:** with 60–120 s Clips, 2 Practice clips per Trial roughly triples the media per request, so the pilot should test one short excerpt.

### UX Design Requirements

None. Review Consortium is a CLI, and there is no UX design contract.

### FR Coverage Map

FR1: Epic 1 - Study init from template
FR2: Epic 1 - Config and Test schema validation
FR3: Epic 1 - User-defined and built-in Instruments
FR4: Epic 1 - Seeded Persona generation (Sampling frame, quotas)
FR5: Epic 3 - Persona-fidelity screening
FR6: Backlog - Persona photos (post-MVP)
FR7: Epic 3 - Panel reuse, invalidation, panel copy
FR8: Epic 2 - Gemini and Qwen adapters with pinned settings (Fake rater in Epic 1)
FR9: Epic 2 - Provider failure handling, refusals, attrition breakdown
FR10: Epic 3 - Construct-level Perception screening and the eligibility gate
FR11: Epic 1 - push clip: canonicalize, blind, leak report
FR12: Epic 1 - push test: validation, Clip disjointness
FR13: Epic 4 - Protocol lock and the main-Test gate
FR14: Epic 1 - open: plan, offline cost estimate, confirmation, ceiling
FR15: Epic 1 - Independent fresh-context Trials
FR16: Epic 1 - Randomization and pairwise counterbalancing
FR17: Epic 1 - Briefing, Practice clips, Repeats, Prompt variants
FR18: Epic 1 - Retries, invalid answers, ceiling pause and resume
FR19: Epic 4 - Catch trials and exclusions
FR20: Epic 4 - Rater-flow report
FR21: Epic 1 - status
FR22: Epic 1 - export with blinding join
FR23: Backlog - Local status page (post-MVP)
FR24: Epic 1 (Archive and byte-identical re-issue) + Epic 4 (Reporting manifest)
FR25: Epic 4 - README, license, example Study
FR26: Epic 4 - Protocol template (with the lock)
FR27: Epic 1 - INTERFACE.md, created in 1.1 and extended by every command story

## Epic List

### Epic 1: Blinded study pipeline, end to end with the Fake rater
Kamal can create a Study, generate a Persona pool, push Clips blind, define a Test, see a cost estimate, run it, watch status, and export a blinded-then-rejoined CSV. All of this runs offline at zero cost through the Fake rater. Until Epic 4 delivers the Protocol lock, `kind: main` Tests are refused outright, so pilot and screening Tests only.
**FRs covered:** FR1, FR2, FR3, FR4, FR11, FR12, FR14, FR15, FR16, FR17, FR18, FR21, FR22, FR24 (Archive), FR27

### Epic 2: Real AI raters (Gemini and Qwen)
Kamal can run the same pipeline against real models, Gemini and hosted `qwen3.8-omni-flash`, with pinned settings, per-Model media limits, real cost, and robust handling of rate limits, errors and refusals.
**FRs covered:** FR8, FR9

### Epic 3: Screened, reusable Panel
Kamal can screen Agents for Persona fidelity and Models for construct-level perception. Only eligible Agents rate. The Panel is reused across Tests, invalidated when a Model or Instrument changes, and can be copied into a new Study.
**FRs covered:** FR5, FR7, FR10

### Epic 4: Defensible results and release
Kamal can freeze the Protocol and run main Tests, apply Catch-trial exclusions, and ship each Export with a Rater-flow report and a Reporting manifest. Lena can install the tool and run the bundled example Study.
**FRs covered:** FR13, FR19, FR20, FR24 (manifest), FR25, FR26

### Backlog (post-MVP)
- FR6: Persona photos (human viewing only, never on the rating path)
- FR23: Local, read-only status page

## Epic 1: Blinded study pipeline, end to end with the Fake rater

Kamal can create a Study, generate a Persona pool, push Clips blind, define a Test, see a cost estimate, run it, watch status, and export a blinded-then-rejoined CSV. All of this runs offline at zero cost through the Fake rater. Until Epic 4, `kind: main` Tests are refused, so only pilot and screening Tests run.

**Definition of done for every story in every epic:** each new or changed command and Study-folder file is documented in `docs/INTERFACE.md` (FR27).

### Story 1.1: Project scaffold and `consortium init`

As Kamal,
I want an installable `consortium` CLI that creates a ready-to-use Study folder,
So that every later command has a home and the architecture rules are enforced from day one.

**Acceptance Criteria:**

**Given** a clean checkout
**When** I run `uv tool install .` and then `consortium --help`
**Then** the CLI lists its commands (Typer, Python ≥ 3.12, the stack versions from the spine)
**And** the package has the AD-1 layout: `core/`, `engine/`, `stages/`, `raters/`, `board/`, `archive/`, `media/`, `config/`, `instruments/`, `templates/`

**Given** the repo's pytest suite
**When** it runs
**Then** an import-linter test enforces the AD-1 layers and the AD-2 forbidden contract: only `stages/push` and `stages/export` may import `board.blinding`
**And** adding a forbidden import makes the test fail

**Given** an empty directory
**When** I run `consortium init <path>`
**Then** it creates `study.yaml`, `protocol.md`, `prices.yaml` and an example `tests/example.yaml`, configured for the Fake rater
**And** running it on a non-empty Study folder fails with a `ConsortiumError` (`study_exists`) and changes nothing

**Given** the repo
**When** this story is done
**Then** `docs/INTERFACE.md` exists, documents `init` and the Study folder layout, and states the unit-of-analysis guidance: Agents sharing a Model are not independent, and Agent, Persona, Model and Clip are crossed factors (FR27, NFR10)

**Given** any command
**When** it fails
**Then** it prints `code: message` to stderr and exits non-zero, sending logs to stderr and data to stdout

### Story 1.2: Study config and Instruments

As Kamal,
I want `study.yaml`, Test YAMLs and Instruments validated against a published schema,
So that mistakes are caught before any work or spending starts.

**Acceptance Criteria:**

**Given** a Study folder
**When** any command loads config
**Then** only `config/` reads YAML (`yaml.safe_load` into Pydantic models), and it rejects unknown Instruments, unpinned Models, Models without an explicit `id`, and missing thresholds, naming the file, field and reason (FR2, AD-9)
**And** the JSON Schema for each file is exported under `docs/schema/` with a schema version

**Given** the package
**When** Instruments are loaded
**Then** the built-in Godspeed (animacy, likeability; 5-point), pairwise "Which one feels more alive?" and 7-point presence Instruments load from package YAML, each with a response schema (FR3)
**And** a user Instrument in the Study folder, with Likert Items, anchors, a free-text field and Prompt variants, loads by the same path with no code change

**Given** a freshly initialized Study
**When** its config is loaded
**Then** it validates with no edits (FR1)

### Story 1.3: Seeded Persona generation

As Kamal,
I want `consortium personas generate` to build a Persona pool from a seeded Sampling frame,
So that the Panel is reproducible and quota-balanced.

**Acceptance Criteria:**

**Given** a `study.yaml` with `seed`, a Big Five spec, NARS bands and quota attributes
**When** I run `consortium personas generate`
**Then** it writes one Persona card per Persona to `panel/personas/` (default 32 × 2 = 64), with IDs `p1…p64` (FR4)
**And** age band, gender, cultural region and robot experience are assigned so that marginal counts match the frame, using seeds derived per AD-10

**Given** the same seed and frame
**When** I regenerate in another folder
**Then** the Persona cards are byte-identical

**Given** existing Persona cards
**When** I run the command again without `--force`
**Then** it refuses (`panel_exists`), because `panel/` inputs are never rewritten in place (AD-3)

### Story 1.4: Push Clips blind

As Kamal,
I want `consortium push clip <file> --condition factor=level [...]` to store an anonymized, canonical copy,
So that nothing on the rating side can learn a Clip's Condition.

**Acceptance Criteria:**

**Given** a video file with audio, and ffmpeg ≥ 6 on PATH
**When** I push it with one or more `factor=level` Conditions
**Then** `media.canonicalize` re-encodes it to the canonical profile from `study.yaml`, strips all metadata, and stores it as `clips/<clip_id>.mp4` with a `c_` + 8 base32 ID, which the command prints (FR11, AD-11)
**And** `board.db` is created on first use (WAL) with only the tables this story needs, and the Clip row holds the Clip ID, SHA-256, duration and size, but no Condition

**Given** a pushed Clip
**When** I inspect the stored file, `board.db` and the logs
**Then** neither the source filename, any metadata tag, nor any Condition string appears in any of them
**And** the Condition exists only in `blinding_key.csv`, written by `board/blinding.py` (AD-2)

**Given** several pushed Clips
**When** a push completes
**Then** `exports/leak-report.csv` is refreshed, comparing duration, loudness, resolution and fps per Condition and flagging differences above a configured tolerance

**Given** ffmpeg is missing or the file has no audio track
**When** I push
**Then** the command fails with `ffmpeg_missing` or `no_audio` and stores nothing

### Story 1.5: Push a Test

As Kamal,
I want `consortium push test <test.yaml>` to validate and register a Test,
So that only well-formed, runnable Tests ever reach a Model.

**Acceptance Criteria:**

**Given** a Test YAML referencing pushed Clip IDs, Instruments, a Pairing plan (`all_pairs`), Repeats and a kind
**When** I push it
**Then** it is validated (unknown Clips or Instruments, or a malformed plan, are rejected with file, field and reason) and registered under its `test:` name (FR12)

**Given** a Clip already used in a `pilot` or `screening` Test
**When** I push a `main` Test that includes it, or the reverse
**Then** the push is refused (`clip_kind_overlap`)

**Given** a Model with media limits in `study.yaml` (seconds and bytes)
**When** any planned Trial's total media (Practice clips + target Clips) would exceed a Model's limit
**Then** the push is refused, naming the Model, limit and Trial shape (AD-11)

**Given** a Test with `kind: main`
**When** I push it before the Protocol lock exists (Epic 4)
**Then** it is registered but marked not openable, and `open` refuses it with `protocol_lock_unavailable`

### Story 1.6: Plan and render Trials

As Kamal,
I want `consortium open <test> --dry-run` to show exactly which Trials would run,
So that I can check the design before spending anything.

**Acceptance Criteria:**

**Given** a pushed Test, Personas and the Models in `study.yaml`
**When** I run `open --dry-run`
**Then** it plans Sessions (`<test>/<agent>/r<repeat>`) and Trials (`session_id` + `trial_index`) for every Agent × Repeat, and prints counts per Model, Instrument and Trial type, writing nothing (FR14)

**Given** a pairwise Instrument with `all_pairs`
**When** Trials are planned
**Then** every Clip pair produces two Trials, one per order, sharing a `pair_id` and an ordered `clip_ids`, with position recorded (FR16, AD-12)

**Given** a Session
**When** Trial order is planned
**Then** it is shuffled with the seed `order:<session_id>` (AD-10), and the same seed gives the same order

**Given** a Trial
**When** `core.render` builds its `TrialRequest`
**Then** the request contains the Persona card, the Instrument instructions, the configured Practice clips with their intended answers, and 0–2 target Clip IDs, and nothing from any other Trial (FR15, FR17, AD-7)
**And** Repeats rotate through the Instrument's Prompt variants, and rendering the same inputs gives byte-identical output

### Story 1.7: Run a Test with the Fake rater

As Kamal,
I want `consortium open <test>` to run every planned Trial through the engine using the Fake rater,
So that the whole pipeline works end to end with no API cost.

**Acceptance Criteria:**

**Given** a planned pilot Test and the Fake rater as Model
**When** I run `open` and confirm
**Then** `engine.dispatch` sends every Trial through the `Rater` port (`prepare / submit / collect`), using a per-provider `asyncio.Semaphore` (AD-6)
**And** the Fake rater returns schema-valid synthetic answers deterministically from its seed, with no network access

**Given** a dispatching Run
**When** it writes state
**Then** only the single writer task writes to `board.db`, and the command holds the `board.lock` lease, so a second dispatching command refuses with `study_busy` (AD-3)

**Given** a Trial being dispatched
**When** its attempt is sent and then answered
**Then** `attempt` is incremented before dispatch, the rendered request is appended to `archive/requests.jsonl` before the attempt is marked `sent`, and the response is appended to `archive/responses.jsonl` before any state change (AD-4, AD-5)
**And** Archive records are keyed by `(trial_id, attempt)` and reference media by `clip_id` and SHA-256 only

### Story 1.8: Resume a Run and verify re-issue

As Kamal,
I want a killed or interrupted Run to resume exactly where it stopped, and proof that every archived request can be re-issued byte-identically,
So that crashes never cost data or money, and the Archive is a trustworthy record.

**Acceptance Criteria:**

**Given** a Run killed mid-way
**When** I run `open <test> --resume`
**Then** completed Trials are not re-sent, and `planned` or orphaned `sent` Trials are dispatched with a new attempt (FR18, NFR6)

**Given** a completed Fake Run
**When** I re-render every request from the Study folder
**Then** each is byte-identical to its archived request (FR24)

### Story 1.9: Cost estimate, ceiling, pause and resume

As Kamal,
I want to see the cost before a Run and never exceed my ceiling,
So that I control spend on a researcher's budget.

**Acceptance Criteria:**

**Given** `prices.yaml` with per-Model token formulas and prices, and a pinned `max_output_tokens`
**When** I run `open <test>`
**Then** `core.cost` estimates the worst-case cost of every planned request offline, covering both pairwise orders, Practice clips and Repeats plus a retry allowance, and prints it with the Trial count and the providers that will receive Clips (FR14, AD-13, NFR9)
**And** no provider is contacted before I confirm (or pass `--yes`)

**Given** a ceiling set with `open --ceiling <usd>`
**When** the estimate exceeds it
**Then** `open` refuses (`over_ceiling`)
**And** the ceiling is stored in `board.db`, and every change is logged in the cost ledger (AD-9)

**Given** a running Test
**When** the next reservation would push committed spend (actual cost, or reserved where actual is unknown) over the ceiling
**Then** that request is not sent, the Run pauses, and status shows `paused: ceiling` (FR18)
**And** the ledger has exactly one row per `(trial_id, attempt)`, with reserved and actual cost

**Given** a paused Run
**When** I run `open <test> --resume --ceiling <higher>`
**Then** it continues at Trial level

### Story 1.10: Response validation, retries and invalid answers

As Kamal,
I want malformed answers retried and then marked invalid,
So that bad output never silently enters my data.

**Acceptance Criteria:**

**Given** a response
**When** `core` parses it
**Then** it is validated against the Instrument's response schema in core, never in an adapter (AD-7)

**Given** a response failing the schema
**When** retries remain (`max_retries`, default 2, from `study.yaml`)
**Then** the Trial stays `sent` and is re-dispatched with a new attempt and a new seed (`model:<session_id>:<trial_index>:<attempt>`)
**And** when retries run out the Trial becomes `invalid` (FR18, AD-4)

**Given** several valid attempts for one Trial
**When** the answer is chosen
**Then** the highest valid attempt wins

**Given** the Fake rater configured to emit a set rate of invalid answers
**When** a Run completes
**Then** the invalid-answer rate per Agent and per Model is computed and available to `status` and `export`

### Story 1.11: Status

As Kamal,
I want `consortium status [<test>]` to show progress while a Run is going,
So that I can see what's done, failing and costing.

**Acceptance Criteria:**

**Given** any Study, during or after a Run
**When** I run `status`
**Then** it shows, per Test, Model and Agent: valid, invalid, refused, failed, retried, `sent` in flight, and cost so far against the ceiling (FR21)
**And** it reads without taking the lease, so it works while another process dispatches (AD-3)

**Given** a completed Run
**When** I compare status counts with the Archive
**Then** they match

### Story 1.12: Export with blinding join

As Kamal,
I want `consortium export <test>` to produce one tidy, versioned CSV with Conditions rejoined only at that moment,
So that I can analyze the results in R or Python.

**Acceptance Criteria:**

**Given** a Test with any Session not yet terminal
**When** I export
**Then** it refuses (`sessions_running`) (FR22)

**Given** a finished Test
**When** I export
**Then** `exports/<test>.csv` has one row per Item per Trial, with at least one row per Trial (refused or failed Trials get a row with `status` and an empty `response`), following the versioned schema (AD-12)
**And** the columns are: `agent_id`, `session_id`, `trial_index`, `clip_id` (or `clip_id_a`/`clip_id_b` and `pair_id` for pairwise), `persona_*`, `model`, one column per Condition factor (per side for pairwise), `instrument`, `item`, `response`, `position`, `repeat`, `seed`, `prompt_variant`, `status`, `excluded`, `exclusion_reason`, `test_kind`, `protocol_lock`, `timestamp` (UTC ISO 8601)

**Given** columns whose features arrive in later epics (`excluded`, `exclusion_reason`, `protocol_lock`)
**When** this story exports
**Then** they are present with defaults (`false`, empty, empty), so the schema version never changes when those epics land

**Given** the export runs
**When** Conditions are filled in
**Then** they are read from `blinding_key.csv` by `board/blinding.py` at that moment only (AD-2)
**And** Practice clip answers never appear

**Given** pilot and screening Tests
**When** they are exported
**Then** they go to separate files and are never mixed with any other Test kind

## Epic 2: Real AI raters (Gemini and Qwen)

Kamal can run the same pipeline against real models, Gemini and hosted `qwen3.8-omni-flash`, with pinned settings, per-Model media limits, real cost, and robust handling of rate limits, errors and refusals.

### Story 2.1: Provider failure handling and refusals

As Kamal,
I want transient errors retried and refusals recorded rather than retried forever,
So that a long Run survives provider hiccups and attrition stays visible.

**Acceptance Criteria:**

**Given** a `Rater` result categorized by the adapter as `transient` (transport error or rate limit), `refused` (safety filter) or `fatal`
**When** the engine handles it
**Then** transient results are re-dispatched with exponential backoff and a new attempt, `refused` ends the Trial as `refused`, and exhausted transient or `fatal` results end it as `failed` (FR9, AD-4)
**And** every Trial ends in exactly one of `valid`, `invalid`, `refused` or `failed`

**Given** the Fake rater configured to simulate rate limits, transport errors and refusals
**When** a Run completes
**Then** it finishes without operator help, and `status` and the Export show refusal and failure counts per Model, per Persona attribute and per Condition (the Condition breakdown appears only in the Export, AD-2)

### Story 2.2: Gemini adapter

As Kamal,
I want a Gemini `Rater` adapter with pinned settings,
So that I can rate Clips with a real model reproducibly.

**Acceptance Criteria:**

**Given** a Model in `study.yaml` with `provider: gemini`, a pinned model version, temperature, fps, seed and media limits, and `GEMINI_API_KEY` in the environment
**When** the engine dispatches a Trial
**Then** `prepare` uploads each Clip once under its Clip ID through the File API, reuses the reference and re-prepares on expiry, and `submit`/`collect` call `generate_content` with static processing mode and the pinned settings (FR8, AD-6)
**And** the request text is exactly the `TrialRequest` text, unchanged (AD-7)

**Given** a response
**When** it is collected
**Then** the raw response, usage, and the `model_version` the provider reports are archived for that attempt, and actual cost is written to that attempt's ledger row (AD-5, AD-13)

**Given** Gemini errors, rate limits or safety blocks
**When** they occur
**Then** the adapter maps them to `transient`, `refused` or `fatal` for Story 2.1's handling

**Given** a recorded-response test double
**When** the test suite runs
**Then** the adapter is tested without network access. A live smoke test runs only when an API key is present.

### Story 2.3: Qwen adapter (OpenAI-compatible)

As Kamal,
I want a Qwen `Rater` adapter that speaks the OpenAI-compatible API,
So that I have a second Model now and can move to self-hosted weights later by changing config only.

**Acceptance Criteria:**

**Given** a Model with `provider: qwen`, a `base_url`, `model: qwen3.8-omni-flash`, settings and media limits, and `DASHSCOPE_API_KEY` set
**When** a Trial is dispatched
**Then** each Clip is sent inline as a base64 `video_url` part using the openai SDK, and Clips whose encoded size exceeds the Model's byte limit are rejected beforehand by `push test` (AD-11, AD-14)
**And** pointing `base_url` at a vLLM server needs no code change

**Given** settings the provider doesn't document as honoured (e.g. `temperature`, `seed`)
**When** they are sent
**Then** they are archived with a `documented: false` flag that the Reporting manifest can surface (AD-14, NFR3)

**Given** a response
**When** it is collected
**Then** the raw response, the audio and video token usage, and the actual cost are recorded as in Story 2.2, and errors map to `transient`, `refused` or `fatal`
**And** tests use a recorded-response double, with a live smoke test only when a key is present

## Epic 3: Screened, reusable Panel

Kamal can screen Agents for Persona fidelity and Models for construct-level perception. Only eligible Agents rate. The Panel is reused across Tests, invalidated when a Model or Instrument changes, and can be copied into a new Study.

### Story 3.1: Persona-fidelity screening

As Kamal,
I want `consortium screen personas` to check each Agent answers like its Persona,
So that Agents that ignore their Persona never rate.

**Acceptance Criteria:**

**Given** generated Personas and configured Models
**When** I run `screen personas`
**Then** it creates a screening run `s<n>` whose Trials carry no Clip (short-form Big Five and NARS questionnaire), dispatched through `engine.dispatch` with the ledger, Archive and lease, like any Run (FR5, AD-6, AD-7)

**Given** the responses
**When** they are scored
**Then** each Agent gets a per-trait directional-match score and passes or fails against the `study.yaml` threshold (default ≥ 80%)
**And** the score, the outcome, the threshold, the run ID, the Model settings hash and the Instrument hash are stored in `board.db`

**Given** a second screening run
**When** it completes
**Then** earlier runs are kept and marked superseded, never overwritten

### Story 3.2: Perception screening

As Kamal,
I want `consortium screen models` to check each Model perceives the constructs my Instruments measure,
So that a Model that inverts "aliveness" can't rate it.

**Acceptance Criteria:**

**Given** a `screening` Test listing Clip pairs with a known direction per construct, plus low-level checks with expected answers ("is the robot moving?", "is there speech?")
**When** I run `screen models`
**Then** each Model is dispatched through the engine, and passes or fails per Instrument against the `study.yaml` threshold (FR10)
**And** results are stored per Model × Instrument with the run ID and the settings and Instrument hashes, and superseded runs are kept

**Given** a main or pilot Test using a construct
**When** no screening pair covers that construct
**Then** `screen models` reports the coverage gap

### Story 3.3: Eligibility gate and invalidation

As Kamal,
I want `open` to use only Agents and Instruments with a current screening pass,
So that stale or failed screening can never feed results.

**Acceptance Criteria:**

**Given** screening results in `board.db`
**When** `open` plans a Test
**Then** `core.eligibility` assigns only Agents with a current fidelity pass, on Models with a current perception pass for each Instrument used (FR7, FR10)
**And** excluded Agents and Models are listed with reason codes for the Rater-flow report

**Given** a Test whose constructs lack perception coverage
**When** I `open` it
**Then** it refuses (`screening_coverage_missing`)

**Given** a Model whose pinned settings hash changed, or an Instrument whose hash changed
**When** I `open` a Test
**Then** results stamped with the old hashes no longer count, and `open` refuses until screening is re-run (`screening_stale`)

**Given** the Fake rater
**When** eligibility is tested
**Then** every rule above is covered by offline tests

### Story 3.4: Copy a Panel into a new Study

As Kamal,
I want `consortium panel copy --from <study>` to reuse a screened Panel,
So that starting a new Study is cheap.

**Acceptance Criteria:**

**Given** a source Study with Persona cards and screening results
**When** I run `panel copy --from <source>` in a freshly initialized Study
**Then** the Persona cards are copied into `panel/` and the stamped screening results are imported into the new `board.db` with their source Study and hash (FR7)
**And** the source Study is never modified

**Given** the new Study's Model or Instrument hashes differ from the stamps
**When** I `open` a Test
**Then** the copied results count as stale, per Story 3.3

**Given** a target that already has a Panel
**When** I copy
**Then** it refuses (`panel_exists`)

## Epic 4: Defensible results and release

Kamal can freeze the Protocol and run main Tests, apply Catch-trial exclusions, and ship each Export with a Rater-flow report and a Reporting manifest. Lena can install the tool and run the bundled example Study.

### Story 4.1: Protocol freeze and main-Test gate

As Kamal,
I want `consortium protocol freeze` to lock everything that shapes data,
So that main Tests run only on a pre-registered, unaltered setup.

**Acceptance Criteria:**

**Given** a Study with current screening
**When** I run `protocol freeze`
**Then** it writes the screening snapshot to `panel/`, then `protocol.lock`: a per-file SHA-256 map plus a combined hash and a lock version (FR13, AD-8). The map covers:
- `protocol.md`, `study.yaml`, `tests/*.yaml` and `prices.yaml`
- every Instrument file in use, built-ins included
- `panel/`
- the `consortium` package version

**And** the cost ceiling is not included

**Given** a valid lock
**When** I `open` or `open --resume` a `main` Test
**Then** it re-hashes and runs, and each Trial stores the lock version. The Epic 1 `protocol_lock_unavailable` refusal is retired.

**Given** any locked file edited after freezing
**When** I `open` a main Test
**Then** it refuses (`lock_mismatch`), naming the changed file

**Given** a re-freeze
**When** it completes
**Then** a new lock version is created, and earlier versions are kept for the manifest

**Given** `consortium init`
**When** it creates `protocol.md`
**Then** it uses the shipped template, whose sections cover Panel, Screening thresholds, Instruments, Catch trials, exclusions, Repeats and cross-Model replication (FR26)

### Story 4.2: Catch trials and exclusions

As Kamal,
I want Catch trials with expected answers to exclude inattentive Agents,
So that my data has the attention checks a human panel would.

**Acceptance Criteria:**

**Given** a Test YAML marking Clips as Catch trials with expected answers
**When** the Test runs
**Then** Catch trials are planned and randomized like any other Trial, and are indistinguishable from others in the request (FR19)

**Given** a finished Test
**When** I export
**Then** `core.exclusions` excludes Agents whose Catch-trial failure rate exceeds the `study.yaml` threshold, and Agents whose invalid-answer rate exceeds its threshold
**And** their rows stay in the Export with `excluded=true` and an `exclusion_reason` code (AD-12)

### Story 4.3: Rater-flow report

As Kamal,
I want a CONSORT-style Rater-flow report with every Export,
So that a reviewer can see exactly who was dropped and why.

**Acceptance Criteria:**

**Given** an Export
**When** it is written
**Then** `exports/<test>-rater-flow.csv` and `.md` are written with these steps (FR20, AD-12):
1. Personas generated
2. Fidelity-screened out
3. Models perception-screened out, per Instrument
4. Trials refused or failed
5. Agents excluded
6. Analyzed

**And** steps 1–3 come from the frozen screening snapshot (or, for pilot Tests run before any freeze, the current screening results in `board.db`), and steps 4–6 from the Export file

**Given** the report
**When** it is checked
**Then** the counts at each step sum correctly, every exclusion has a reason code, and counts are broken down by Persona attribute and Condition, including quota balance after exclusions (FR4)

### Story 4.4: Reporting manifest

As Kamal,
I want a Reporting manifest generated with every Export,
So that my methods section can be written from evidence, not memory.

**Acceptance Criteria:**

**Given** an Export
**When** it is written
**Then** `exports/<test>-manifest.md` auto-fills (FR24):
- Model, pinned version, reported build and access dates
- sampling settings and seeds, with undocumented settings flagged
- number of Runs and attempts
- the exact prompt templates
- every lock version and the one this Export ran under
- the Panel source (copied or generated)
- the providers that received Clips (NFR9)
- every cost-ceiling change

**And** it lists the GUIDE-LLM items the author must supply, such as human validation

**Given** any tool-owned item
**When** the manifest is generated
**Then** no tool-owned field is left for manual entry (PRD SM-2)

### Story 4.5: README, license and example Study

As Lena,
I want to install Review Consortium and run a complete example in minutes,
So that I can evaluate it and start my own Study.

**Acceptance Criteria:**

**Given** a clean machine with Python ≥ 3.12, uv and ffmpeg
**When** I follow the README
**Then** `uv tool install` works, and the bundled example Study runs every stage end to end with the Fake rater using only the documented commands (FR25):
- generate Personas
- screen
- push Clips and a Test
- freeze
- open
- export, producing the CSV, Rater-flow report and manifest

**And** it finishes in under 30 minutes (PRD SM-5)

**Given** the repo
**When** it is published
**Then** it contains an open-source license file (the choice is confirmed by Kamal at release; MIT or Apache-2.0 assumed) and a README covering install, the ffmpeg dependency, API-key environment variables, the data-handling note (NFR9) and a pointer to `docs/INTERFACE.md`
