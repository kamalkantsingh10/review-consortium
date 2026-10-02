# Epic 1 Context: Blinded study pipeline, end to end with the Fake rater

<!-- Compiled from planning artifacts. Edit freely. Regenerate with compile-epic-context if planning docs change. -->

## Goal

Deliver the `consortium` CLI's full blinded pipeline, running offline at zero cost through the Fake rater: create a Study folder, validate config and Instruments, generate a seeded Persona pool, push Clips blind, register a Test, plan and render Trials, estimate cost and enforce a ceiling, run Trials through the one engine with retries and resume, show status, and export a tidy CSV with Conditions rejoined only at export time. This is the foundation every later epic plugs into: real adapters (Epic 2), screening (Epic 3), and the Protocol lock and reports (Epic 4). Until Epic 4 delivers the lock, `kind: main` Tests are registered but refused at `open` (`protocol_lock_unavailable`), so only pilot and screening Tests run.

## Stories

- Story 1.1: Project scaffold and `consortium init`
- Story 1.2: Study config and Instruments
- Story 1.3: Seeded Persona generation
- Story 1.4: Push Clips blind
- Story 1.5: Push a Test
- Story 1.6: Plan and render Trials
- Story 1.7: Run a Test with the Fake rater
- Story 1.8: Resume a Run and verify re-issue
- Story 1.9: Cost estimate, ceiling, pause and resume
- Story 1.10: Response validation, retries and invalid answers
- Story 1.11: Status
- Story 1.12: Export with blinding join

## Requirements & Constraints

- **Definition of done for every story:** each new or changed command and Study-folder file is documented in `docs/INTERFACE.md`, which also states that Agents sharing a Model are not independent and that Agent, Persona, Model and Clip are crossed factors. The tool performs no statistics.
- A freshly initialized Study validates with no edits, using the Fake rater. Config errors name file, field and reason; schemas are versioned and exported as JSON Schema.
- Built-in Instruments: Godspeed (animacy, likeability; 5-point), pairwise "Which one feels more alive?", 7-point presence. User Instruments (Likert Items with anchors, pairwise, free text, Prompt variants) load from the Study folder with no code change; each has a response schema.
- Defaults (overridable in `study.yaml`): 64 Personas (32 Big Five profiles x 2 NARS bands), quota-balanced on age band, gender, cultural region, robot experience; temperature 0.7 (> 0); 2 Practice clips; 3 Repeats; `max_retries` 2; pairing `all_pairs`.
- **Independence:** every Trial is a fresh request; nothing from any other Trial ever appears in a request.
- **Blinding by construction:** Conditions exist only in `blinding_key.csv`; never in `board.db`, the Archive, logs, stored clips or requests. Source filenames and metadata never survive ingest.
- **Reproducibility:** same seed and inputs give byte-identical Persona cards, Trial order and rendered requests; the Archive plus Study folder must re-issue every request byte-identically.
- **Cost control:** estimate shown before any Run; nothing contacts a provider before confirmation (or `--yes`); committed spend plus in-flight reservations never exceed the ceiling; Run pauses at the ceiling.
- **Resumability:** all state is in the Study folder; a killed Run resumes at Trial level without re-sending completed Trials.
- **Local-first:** no telemetry; API keys from environment variables only.
- **Accepted decisions (2026-10-02, recorded in the specs):** Persona cards describe behaviour only (labels live in `index.json`), with wording Kamal approves before 1.3; template quota levels are placeholders until Kamal confirms them; the presence Instrument ships `draft: true` and draft Instruments are refused for `main` (gate in Epic 4); Clips may be pushed with no Condition; Practice clips are listed per Test (`practice:`), exempt from `clip_kind_overlap`, and `session.practice_clips` is the number used per Trial per Instrument; `open` needs `--ceiling` unless every Model is Fake, and compares the expected single-attempt cost to the ceiling; invalid rate = invalid ÷ (valid + invalid).
- Practice clip answers are archived but never exported. Pilot and screening Exports are separate files, never mixed with other Test kinds. Invalid-answer rate per Agent and per Model must be reported (target < 5% after retries).

## Technical Decisions

- **Stack:** `uv init --package`, Python >= 3.12, Typer 0.27, Pydantic 2.13, PyYAML 6, stdlib sqlite3 (WAL), pytest 9.1, ruff 0.16, import-linter 2.15; installed via `uv tool install`; ffmpeg >= 6 is a system dependency. No third-party starter.
- **Layout and dependency direction (enforced by import-linter contracts run as a pytest test):** `cli.py` (thin Typer, imports stages only) -> `stages/` (init, personas, push, open, status, export; never import each other) -> `engine/`, `core/`, `board/`, `media/`, `config/`, `archive/`. `core` is pure (stdlib + pydantic, no I/O). Adapters in `raters/` import `core` only. `engine` imports core, board, archive, raters. Plus `instruments/` (built-in YAMLs) and `templates/`.
- **Blinding boundary:** only `board/blinding.py` reads/writes `blinding_key.csv`; only `stages/push` and `stages/export` may import it (forbidden contract).
- **State:** `board.db` is the only mutable state; only `board/` runs SQL. Inside a process, one writer task does all writes. Dispatching commands hold an exclusive `board.lock` lease (second dispatcher refuses with `study_busy`); `status` and `export` read without the lease. Files in `panel/` and `tests/` are never rewritten in place once used. Table layout is owned by `board/`; create only the tables each story needs.
- **Trial lifecycle:** `planned -> sent -> valid | invalid | refused | failed` (terminal states never change). `attempt` is incremented by the writer before every dispatch; each `(trial_id, attempt)` is dispatched at most once. A Trial stays `sent` across retries; highest valid attempt wins. Never assume a synchronous answer. Resume collects `sent` attempts with a handle and re-dispatches `planned` and handle-less `sent` Trials.
- **Archive before state:** append request to `archive/requests.jsonl` before marking `sent`; append raw response to `archive/responses.jsonl` before any state change. Append-only, keyed by `(trial_id, attempt)`, media referenced by `clip_id` + SHA-256 only.
- **Engine and port:** every Model call goes through `engine.dispatch(trials)`; stages create Trials, only the engine sends them. `Rater` port: `prepare(clip) -> media_ref`, `submit(requests) -> handles`, `collect(handles) -> results`; concurrency capped by a per-provider `asyncio.Semaphore`. The Fake rater is deterministic from its seed, needs no network, and can be configured to emit invalid answers (later also errors/refusals).
- **Rendering:** `core.render` builds a provider-neutral `TrialRequest` (Persona card, Instrument instructions, Prompt variant, Practice clips with intended answers, 0-2 Clip IDs), fully determined by its inputs. Adapters add pinned settings only and never change text. Parsing and response-schema validation happen in `core`, never in adapters.
- **Config:** only `config/` reads YAML (`yaml.safe_load` into Pydantic). Thresholds, defaults, pinned `max_output_tokens`, Model settings and media limits live in `study.yaml`. Models keyed by explicit `id` (`m1`, `m2`), never list position; unpinned or id-less Models are rejected. The cost ceiling lives in `board.db` (set via `open --ceiling <usd>`), every change logged in the ledger.
- **Seeds:** one `study.seed`; every other seed is `sha256("<study.seed>:<purpose>:<natural key>")`, first 8 hex digits as int, masked to 31 bits. Keys: `personas`, `order:<session_id>`, `model:<session_id>:<trial_index>:<attempt>`. No unseeded randomness; seeds stored on Trial rows.
- **Clip ingest:** `media.canonicalize` ffmpeg re-encode to one profile from `study.yaml` (about 480p, <= 400 kbps, so a 120 s Clip stays under ~7 MB), all metadata stripped, stored as `clips/<clip_id>.mp4`, ID `c_` + 8 random base32 chars; Clip row holds ID, SHA-256, duration, size. Leak report `exports/leak-report.csv` compares duration, loudness, resolution, fps per Condition against a tolerance. `push test` refuses if any Trial's total media (Practice + targets) exceeds a Model's declared seconds/bytes limit. Clips cannot overlap between pilot/screening and main Tests (`clip_kind_overlap`).
- **Cost:** a single offline `core.cost(request)` estimates worst case from per-Model formulas in `prices.yaml` plus pinned `max_output_tokens`, covering both pairwise orders, Practice clips, Repeats and a retry allowance. Ledger: one row per `(trial_id, attempt)` with reserved and actual cost; committed spend = actual, or reserved where actual unknown. Dispatch only while committed + next reservation <= ceiling.
- **Export:** one row per Item per Trial, at least one row per Trial (refused/failed get `status` and empty `response`). Pairwise rows carry `pair_id`, `clip_id_a`/`clip_id_b` and Condition columns per side. Columns: `agent_id`, `session_id`, `trial_index`, `clip_id`, `persona_*`, `model`, Condition factors, `instrument`, `item`, `response`, `position`, `repeat`, `seed`, `prompt_variant`, `status`, `excluded`, `exclusion_reason`, `test_kind`, `protocol_lock`, `timestamp`. Later-epic columns ship now with defaults (`false`, empty, empty) so the schema version doesn't change.
- **Conventions:** IDs: Persona `p<n>`, Agent `p<n>-m<n>`, Test = YAML `test:` name, Session `<test>/<agent>/r<repeat>`, Trial = `session_id` + `trial_index`. UTC ISO 8601 with `Z`. SHA-256 lowercase hex; canonical JSON = sorted keys, UTF-8, no whitespace. One `ConsortiumError(code, message, path?)`; CLI prints `code: message` to stderr and exits non-zero; snake_case reason codes (reused later in the Rater-flow report). Data to stdout, logs to stderr via stdlib `logging`. Every command takes `--study <path>` (default cwd); stored paths relative to the Study folder. USD as decimal strings.
- **Study folder:** `study.yaml`, `protocol.md`, `prices.yaml`, `tests/*.yaml`, `panel/` (Persona cards), `clips/`, `blinding_key.csv`, `board.db`, `board.lock`, `archive/requests.jsonl`, `archive/responses.jsonl`, `exports/` (`protocol.lock` arrives in Epic 4).

## Cross-Story Dependencies

- 1.1 provides the package layout, error/output conventions, import-linter test and `INTERFACE.md` that all others extend; 1.2's config loaders and Instruments are used by every later story.
- 1.3 (Personas) and 1.4 (Clips, `board.db` creation, blinding writer) feed 1.5 (Test validation, media limits), which feeds 1.6 (planning, ordering, rendering).
- 1.7 (engine, writer, lease, Archive) is the runtime; 1.8 adds resume and the re-issue check on top of it. 1.9 (cost/ceiling) and 1.10 (validation/retries) extend the engine and rely on 1.8's resume; 1.11 and 1.12 read the state and Archive they produce.
- Downstream: Epic 2 adds Gemini/Qwen adapters and transient/refused/fatal handling behind the same `Rater` port and engine; Epic 3 adds screening via `engine.dispatch` and an eligibility gate in `open`; Epic 4 retires `protocol_lock_unavailable`, fills `excluded`/`exclusion_reason`/`protocol_lock`, and adds Catch trials, Rater-flow report and manifest. Design Epic 1 seams so these land without schema or port changes.
