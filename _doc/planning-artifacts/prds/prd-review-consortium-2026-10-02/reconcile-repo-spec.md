# Reconciliation: input-repo-spec.md vs prd.md + addendum.md

Source: *AI Rater Board — Repo Spec* (rev 9, 2026-10-02), local copy `input-repo-spec.md`.
Memlog overrides were treated as intentional and are not reported as gaps: the rename to Review Consortium and the `consortium` CLI, independence from OLAF epics, the PRD's own NFR IDs replacing PNFR IDs, Gemini plus Qwen3-Omni on cloud, persona photos added, photos and the web page deferred from MVP, and panel reuse across studies.

Severity: **high** means a principle or decision is lost or contradicted and downstream work would build the wrong thing. **Medium** means a meaningful idea is weakened or left implicit. **Low** means a detail or wording issue.

---

## A. Gaps (source content missing, weakened or contradicted)

### G1. Panel scope: the memlog decision is not realized and the PRD contradicts itself (high)
- **Source:** "All state for a study lives in its own folder, so one install of the tool can serve several studies." `study.yaml` holds `panel:` (personas + models), and `board.db` sits in the study folder.
- **Memlog:** "Panel (personas + persona/model screenings) is built once and reused across many studies; only protocol freeze (and pilot) is per study."
- **PRD:** §1 Vision ("builds and screens a panel once, then runs study after study against it") and UJ-1 ("a screened panel he will reuse for every study") follow the memlog. But Glossary *Panel* ("reused across Tests"), FR-7 ("stays valid for every Test **in the Study**"), §4.1 (`study.yaml` holds the Panel) and NFR-6 ("all state lives in the Study folder") keep the source's per-study scope. No FR describes how a second Study folder points to or imports an existing Panel.
- **Fix:** Pick one model and state it everywhere. If the memlog stands, add a *Panel* artifact/folder that Studies reference by path or ID (an FR for "use existing Panel in a new Study", with Consequences such as "screening results and persona cards are reused, not regenerated"). Update the Glossary, FR-7, §4.1 and NFR-6 to match, and say how invalidation (FR-7 model change) works across Studies. Also state whether perception screening is per Panel or per Study's Instruments, because FR-9 ties perception to "what the Instruments ask about", which varies by Study.

### G2. The CLI surface is incomplete, and "four commands are the whole contract" conflicts with the extra commands (high)
- **Source:** "Four commands are the whole contract with the outside world… The panel itself is built with separate setup commands (personas generate, screen personas, screen models) that the study owner runs once before opening tests." Also "The CLI is the only interface."
- **PRD:** §4.4 keeps "Four commands … are the whole external contract." FR-1 (init), FR-4 (generate personas), FR-6 (persona-fidelity screening), FR-9 (perception screening, which has no "screen models" step and only pushes a test), FR-12 (freeze the protocol) and FR-5 (photos) are capabilities with **no command named**. "The CLI is the only interface" is absent. §2.2 only says "The interface is a CLI and YAML files", and the PRD never says there is no Python/library API.
- **Fix:** Add a short CLI-surface table in §4 or an FR. It should separate (a) the 4-command **board contract** used by clip producers such as OLAF from (b) **setup commands**: `init`, `personas generate`, `screen personas`, `screen models`, `protocol freeze`, and optionally `personas photos`. Exact names can be left to architecture. Restate principle 4 as an NFR: "The CLI is the only supported interface; there is no importable API contract."

### G3. The "nothing imports code across those lines" boundary is weakened (medium)
- **Source:** "OLAF produces clips; the paper's study folder defines the tests; the board rates them and returns a table. Nothing imports code across those lines." Also "Epic 12 uses nothing else."
- **PRD:** §6 "No OLAF-specific or paper-specific code" and §1 "OLAF is just a study folder" cover only one direction. Nothing says producers (e.g. a clip recorder) must integrate **only** through the CLI and files, never by importing the package or reading `board.db`.
- **Fix:** Add an NFR (e.g. NFR-9 Integration boundary): "External tools interact only through CLI commands and Study-folder files. No external code imports the package or reads board state directly. Board state is private to the tool." This keeps the independence the memlog wants, now framed generically.

### G4. The `INTERFACE.md` contract document is missing (medium)
- **Source:** `docs/INTERFACE.md  # the push/open/status/export contract`.
- **PRD:** absent. FR-23 lists README, license, protocol template and example Study only.
- **Fix:** Add it to FR-23, or better, a Consequence on the CLI-surface FR from G2: "A versioned interface document specifies command arguments, outputs, exit codes and the Export column schema. Changes to it are breaking changes." This is what lets clip producers integrate without importing code (G3).

### G5. The human-panel-step mapping is reduced to one sentence, and FR-5 breaks the stated rule (medium)
- **Source:** "Every feature maps to a step a reviewer would expect from a human panel; anything that can't be defended that way stays out." The source table maps each story to a step: pre-registration, recruitment quotas, screening questionnaire, vision and hearing test, study platform, briefing/practice/blinded trials, attention checks/exclusions, pilot study, human subset.
- **PRD:** §1 restates the principle, but no feature names its human-panel step, so the rule can't be checked. FR-5 (Persona photos), FR-1 (init) and the web page map to no human-panel step, and the PRD doesn't explain why they are allowed in.
- **Fix:** Add a "Human-panel step" line to each feature description, or a traceability table in §4 (Feature → human-panel step → reviewer question it answers). Mark operator-convenience features (photos, init, web page) explicitly as "tooling, not method: never affects ratings" so the principle stays honest.

### G6. Briefing has no requirement (medium)
- **Source:** 11.6 → "Briefing, practice, blinded trials". Principle: "recruited, screened, briefed, blinded…"
- **PRD:** the §4.5 description says "The runner briefs each Agent", but no FR defines the briefing: what the Agent receives (Persona card, task instructions, Instrument wording), whether it is pinned/archived, and whether it is identical across Agents except for the Persona.
- **Fix:** Add a Consequence to FR-16 or a new FR: "Each Session opens with a briefing built from the Persona card plus Study-level instructions. The briefing text is identical across Agents apart from the Persona card, and is archived."

### G7. Retries only cover schema failures; API/transport failures are dropped (medium)
- **Source:** `max_retries: 2`; status reports "done, failed, retried". The runner owns "retries".
- **PRD:** FR-17 retries only "Responses failing the Instrument's response schema". It doesn't say how provider errors, timeouts or rate-limit failures are retried, or what "failed" in FR-20 means.
- **Fix:** Extend FR-17 to cover both kinds: transport/provider errors (retried, never counted as invalid answers) and schema failures (retried, then recorded invalid). Define "failed" for status.

### G8. Pairing plan semantics are undefined, and counterbalancing changes the source plan (medium)
- **Source:** `pairing: all_pairs` in the Test YAML. `push test` "validates … pairing plan".
- **PRD:** "pairing plan" appears in FR-2, FR-11 and the Glossary *Test* entry but is never defined. Allowed values (`all_pairs`, …) are absent. FR-15 adds "each pair is presented in both orders", which doubles pairwise trials, but neither the cost estimate (FR-13) nor the pairing definition says so.
- **Fix:** Add *Pairing plan* to the Glossary with at least `all_pairs` (every unordered pair of the Test's Clips, presented in both orders per FR-15). Add a Consequence to FR-13: the estimate counts both orders.

### G9. Repeats: the test-level override and the Session/Repeat semantics are inconsistent (low)
- **Source:** `session.repeats: 3` in `study.yaml`, **and** `repeats: 3` per Test. Export `repeat (1–3)`.
- **PRD:** the Glossary says a *Repeat* is "an independent re-run of the same Session", but FR-16 says "Each Session runs the configured number of Repeats" (repeats inside a Session). The per-Test override is absent.
- **Fix:** Pick one definition. The source and independence (FR-14) favor each Repeat being a separate fresh-context Session. Allow a Test to override the Study default.

### G10. Pinned-setting defaults are dropped: temperature 0 (low)
- **Source:** `temperature: 0, fps: <n>` for every model.
- **PRD:** FR-8 pins "temperature, frame rate, audio handling and seed" but drops the temperature 0 default. FR-2 rejects an "unpinned Model version" but says nothing about unpinned temperature or fps.
- **Fix:** In FR-2, also reject Models missing temperature or fps. State temperature 0 as the default in the Study template (FR-1), with any deviation recorded in the Reporting manifest.

### G11. Export column semantics are lost (low)
- **Source:** `agent_id (p17-m2)`, `persona_*` = Big Five profile, NARS band, age band, gender, region, robot experience; `model` = provider **and pinned version**; `response` = Likert value **or the chosen clip ID for pairwise**; `timestamp` is **ISO 8601**.
- **PRD:** FR-21 lists the column names only. It drops the pairwise response encoding, the model-version content, the persona_* expansion and the timestamp format.
- **Fix:** Add a column table to FR-21 (or to the INTERFACE.md from G4) with type/encoding per column. Pairwise `response` = chosen Clip ID, together with the `position` the PRD already added.

### G12. The default Session parameters from the source plan are not recorded (low)
- **Source:** `practice_clips: 2`, `repeats: 3`, `max_retries: 2`, Godspeed `scale: 5`, presence `scale: 7`.
- **PRD:** FR-16/17 say only "configured". FR-3 keeps presence at 7 points but doesn't give Godspeed's 5-point scale.
- **Fix:** Put these as template defaults in FR-1 (example Study) or the addendum. Add Godspeed 5-point to FR-3.

### G13. How a Study folder is selected ("passed in by path") is unspecified (low)
- **Source:** "a study folder that lives outside the repo and is passed in by path."
- **PRD:** the Glossary says the folder is outside the repo, but no FR says how commands select it (flag, env var, cwd).
- **Fix:** One Consequence on the CLI-surface FR (G2): every command operates on an explicitly given Study folder. Leave the mechanism to architecture.

### G14. Screening-kind data isolation is only partly stated (low)
- **Source:** "Pilot and screening tests are allowed earlier and never mix with results."
- **PRD:** FR-12 says "their data never mixes with main results", but its Consequence covers only pilot ("pilot rows never appear in a main Export").
- **Fix:** Make the Consequence cover both: "No pilot or screening response appears in any main Export or main-Test statistic."

### G15. Placeholder ownership is lost (low)
- **Source:** "Placeholders in angle brackets are open until the models are onboarded (11.4) and the pilot sets the budget (11.8)." Also: "The skeleton can start now; the instruments and panel size wait for the 4 Oct meeting."
- **PRD:** the cost ceiling and per-model fps have no named owner or decision point (§10 has no question for them), and no note says build can start before 4 Oct.
- **Fix:** Add an Open Question or an assumption: the cost ceiling is set by the pilot, and fps by model onboarding. Note in §7.1 that step 1 does not depend on the 4 Oct answers.

### Covered (checked, no gap)
Blinding on push and the separate key (FR-10, NFR-2); conditions rejoined only at export after completion (FR-21); fresh context and no shared memory (FR-14, NFR-1); protocol freeze/hash gate and `kind: main|pilot|screening` (FR-12, Glossary); reproducibility and archive (NFR-3, FR-22); ≥2 video+audio providers (FR-8); "assigns every clip to every screened agent" (FR-13); perception clips pushed as a `screening` Test like any other input (FR-9); quotas, 32×2 = 64 and seed (FR-4); protocol template (FR-23); human anchor stays with the Study (§6); build order (§7.1); open questions 1–5 (§10 Q1–5); one install serving many Studies (Glossary).

---

## B. Source detail that belongs in architecture (not lost, but needs a handoff home)

The PRD is right to keep these out of its body. The addendum, however, has no "carry to architecture" section, so they currently exist only in the source file:
- SQLite board state (`board.db`) inside the Study folder; board = clip store, blinding key, tests, assignments.
- Package layout and module boundaries (`board/`, `personas/`, `screening/`, `runner/`, `models/`, `instruments/`, `quality/`, `web/`). In particular, the runner must have no import path to the blinding key, which is how NFR-2 is meant to be enforced.
- The Study folder layout (`study.yaml`, `protocol.md` → `protocol.lock`, `tests/*.yaml`, `blinding_key.csv`, `archive/`, `exports/`).
- `docs/INTERFACE.md` and `docs/protocol-template.md` as repo deliverables.

**Fix:** Add addendum section A2 "Architecture handoff from the repo spec" that lists these items, so the architecture step picks them up.

## C. PRD content that is architecture-level, or rationale better placed in the addendum

- **§4.3 description:** the argument for Qwen3-Omni over newer cloud-only Qwen versions, and the "Published HRI work found models invert constructs" rationale. This is research rationale; move it to the addendum and keep only a one-line pointer. (Low)
- **FR-8 Consequence "via Alibaba Cloud Model Studio":** the hosting endpoint is an architecture/deployment choice. The PRD-level requirement is "Qwen3-Omni open weights, no local GPU required". (Low)
- **FR-5 "[ASSUMPTION: Google's image models]" and Q6 (CLI command vs Claude Code skill):** delivery mechanism; fine as an open question, but it is architecture-level. (Low)
- **FR-21 exact column list:** keep it, because it is the external contract. Ideally it is defined once in INTERFACE.md (G4) and referenced from the PRD so the two cannot drift. (Info)
- **FR-18 "threshold set in the Protocol":** `protocol.md` is prose. The machine-readable threshold must live in `study.yaml` or the Test YAML and be covered by the lock hash. This is an architecture detail, but the PRD wording implies the tool parses the Protocol. (Low)
