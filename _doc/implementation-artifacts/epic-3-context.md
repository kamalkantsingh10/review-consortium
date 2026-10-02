# Epic 3 Context: Screened, reusable Panel

<!-- Compiled from planning artifacts. Edit freely. Regenerate with compile-epic-context if planning docs change. -->

## Goal

Make the Panel trustworthy and reusable. Agents are screened for Persona fidelity: does each one answer a self-report questionnaire like its Persona card? Models are screened for construct-level perception: does each one rank known-direction Clip pairs correctly for every construct an Instrument measures? `open` then assigns only Agents and Models with a current pass. A pass stays valid across every Test in the Study until a Model pin or an Instrument changes, which makes it stale. A screened Panel can be copied into a new Study, so starting a new Study stays cheap. This is the procedural evidence that answers the two main validity risks: Models stereotyping or ignoring Personas, and Models inverting constructs such as "aliveness" or "strangeness".

## Stories

- Story 3.1: Persona-fidelity screening
- Story 3.2: Perception screening
- Story 3.3: Eligibility gate and invalidation
- Story 3.4: Copy a Panel into a new Study

## Requirements & Constraints

- **Persona fidelity (`screen personas`).** Each Agent answers short-form Big Five (BFI-10 style, 2 items per trait, one reversed) and NARS short-form items. These Trials carry no Clip. Scoring is a directional match per trait against the card's high/low labels, plus the NARS band. An Agent passes when its match ratio is at or above `thresholds.persona_fidelity_min` in `study.yaml` (template 0.8). The board records score, pass or fail, threshold, screening run ID, Model settings hash and Instrument hash. The check is only that the Persona is followed. Construct perception is a separate check.
- **Perception (`screen models TEST`).** This runs a `kind: screening` Test made of Clip pairs with a known `expected` direction per construct or Instrument, plus low-level checks with expected answers ("is the robot moving?", "is there speech?"). Results are pass or fail per Model × Instrument, against a threshold set in `study.yaml`. Any construct used by a main or pilot Test that no screening pair covers is reported as a coverage gap.
- **Runs are logged, never overwritten.** Each screening run gets an ID `s<n>`. A newer run marks older ones superseded and keeps them. This makes threshold shopping visible. Thresholds sit in `study.yaml` before screening and are frozen by the Protocol lock (Epic 4).
- **Eligibility.** `open` plans only Agents with a current fidelity pass, on Models with a current perception pass for each Instrument the Test uses. Excluded Agents and Models are listed with snake_case reason codes: `fidelity_fail`, `fidelity_missing`, `perception_fail`, `perception_missing` and `screening_stale`. Epic 4's Rater-flow report must be able to read them.
- **Refusals.** `open` refuses with `screening_coverage_missing` when a Test's constructs lack perception coverage, and with `screening_stale` when the current settings or Instrument hashes no longer match the stamps. It keeps refusing until screening is re-run.
- **Invalidation.** Changing a Model pin invalidates that Model's screening. Changing an Instrument invalidates perception screening for that Instrument. Rebuilding the Panel invalidates everything, but the existing `panel_in_use` guard already blocks regeneration once Trials exist.
- **Panel copy (`panel copy --from SOURCE`).** Copies Persona cards, `index.json` and `meta.json`, and imports the source's current stamped screening results with `source_study` and `source_hash`. The source is only read, never modified. Refuses with `panel_exists` or `panel_in_use`. Copied results whose stamps differ from the target's current hashes count as stale.
- **Offline testing.** Every eligibility rule must be covered by offline tests using the Fake rater. The Fake rater may read the Persona card it receives and answer faithfully or unfaithfully depending on its configuration, deterministically.

## Technical Decisions

- **One engine (AD-6).** Every screening call goes through `engine.dispatch`, with the same lease, cost estimate and confirmation, ceiling, ledger, Archive-before-state, retries, refusal handling, validation and resume as `open`. Do not build a second runner. Stages create Trials and only the engine sends them.
- **Provider-neutral rendering (AD-7).** `core.render` builds a `TrialRequest` with 0–2 Clips. A clip-less questionnaire Trial is valid. Adapters never change text, and parsing and scoring happen in `core`.
- **Single state owner (AD-3).** Screening runs and results live in `board.db` (WAL), and only `board/` executes SQL. One writer task handles writes. `screen` is a dispatching command, so it holds the `board.lock` lease. Files in `panel/` are inputs and are never rewritten in place once used. Read the source Study only through `board.db.read_only`.
- **Migrations.** These are additive and numbered after the current version 5, in story order. Never rename an existing table or column.
- **Stamps.** `settings_hash` is canonical JSON of the pinned Model settings plus the model id. `instrument_hash` is canonical JSON of the Instrument definition. Both are SHA-256 lowercase hex over canonical JSON (sorted keys, UTF-8, no whitespace). A result counts only while both stamps equal the current values.
- **Pure scoring.** `core/fidelity`, `core/perception` (score plus coverage) and `core/eligibility` are pure functions. Stages handle I/O.
- **Instruments.** The fidelity Instruments are built-in YAML, answered with no Clip and marked screening-only, so they can't be used in pilot or main Tests. Screening Test expectations are validated by `config.load.load_test`, because only `config/` reads YAML.
- **Lock interplay (AD-8).** The Protocol lock (Epic 4) will hash `panel/`, including a screening snapshot written at freeze. Steps 1–3 of the Rater-flow report come from that snapshot.
- **Blinding.** No screening code reads the Blinding key. Screening Trials and rows never appear in pilot or main Exports. Screening and pilot Clips can't also be main-Test targets (`clip_kind_overlap`).
- **IDs and conventions.** Screening run `s<n>`, Agent `p<n>-m<n>`, Model is its `study.yaml` id. Errors use `ConsortiumError(code, message, path?)` with snake_case codes. Data goes to stdout and logs to stderr. Every command takes `--study`. Each new command is documented in `docs/INTERFACE.md`.
- **Binding names.** Module, table and CLI names are fixed by `epic-3-code-map.md` (in this folder). Use them exactly.

## Cross-Story Dependencies

- 3.1 creates the screening tables, the `screen` stage and CLI group, and `core/hashes`. 3.2 reuses all of them, adding the perception columns, the screening Test shape and `core/perception`.
- 3.3 depends on 3.1 and 3.2 results, and changes `stages/open` (from Epic 1). Its exclusion records are an input to Epic 4's Rater-flow report (4.3), and screening runs ahead of the main-Test gate (4.1).
- 3.4 depends on the result schema and stamps from 3.1 and 3.2, and on the staleness rules from 3.3.
- Builds on Epic 1 (Personas and `index.json`, `push test` with `kind: screening`, the engine, the `panel_in_use` guard) and Epic 2 (real adapters with pinned settings, which are what `settings_hash` stamps).
