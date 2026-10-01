# Source input: "AI Rater Board — Repo Spec" (claude.ai doc QwWAMeRqqHBoNpWb4QCHGv, rev 9, 2026-10-02, author Kamal)
# Local text copy for reconciliation. The 7-part data-flow diagram is summarized by its caption and the next paragraph.

## Purpose and scope
The AI rater board is a standalone, reusable tool that runs a panel of persona-conditioned AI agents over video clips the way a human rater panel would be run: recruited, screened, briefed, blinded, randomized, and reported. The OLAF paper is its first study, not its only one. Working repo name: ai-rater-panel.

| Lives in | Contents |
|---|---|
| ai-rater-panel (new repo) | Personas, screening, the rating board, the session runner, quality checks, export. Generic: no OLAF or paper code. |
| OLAF repo | The robot, plus the hardware side of Epic 12: ablation switches (12.1), turn player (12.2), clip recorder (12.3). |
| Paper (Overleaf, already outside OLAF git) | LaTeX, references, and this study's config, test specs, blinding key, results and analysis. |

OLAF produces clips; the paper's study folder defines the tests; the board rates them and returns a table. Nothing imports code across those lines.
[Diagram: rating board data flow · 7 parts]
Condition labels go straight into the blinding key and only rejoin the ratings at export, after every rater has finished.

## Design principles
Every feature maps to a step a reviewer would expect from a human panel; anything that can't be defended that way stays out.
1. Independent raters. Every session starts from a fresh context. Agents never see each other's answers and share no memory (PNFR5).
2. Blinded by construction. The board renames every clip to an anonymized ID on push. Condition labels live only in a separate blinding key that the rating side never reads (PNFR5).
3. A study is input, not code. Instruments, tests, persona quotas, models and repeats come from a study config. The tool has no knowledge of OLAF.
4. Files in, files out. Clips plus a test spec go in; one tidy CSV comes out. The CLI is the only interface.
5. Protocol before data. A main-study test is refused until the study's protocol is frozen and hashed. Pilot and screening tests are allowed earlier and never mix with results (PNFR7).
6. Reproducible. Model versions, sampling settings, frame rate, prompts, persona cards and seeds are pinned. Every raw prompt and response is archived so a run can be re-executed (PNFR6).
7. Video and audio native. Raters are vision-language models that take clips with sound, from at least two providers.

## Repo layout
One Python package with a CLI, plus a study folder that lives outside the repo and is passed in by path.
```
ai-rater-panel/
  raterpanel/
    cli.py            # push / open / status / export (+ panel commands)
    board/            # clip store, blinding key, tests, assignments (SQLite)
    personas/         # sampling frame + persona card generator (11.2)
    screening/        # persona fidelity (11.3), model perception battery (11.4)
    runner/           # blinded randomized sessions, retries, cost ceiling (11.6)
    models/           # one adapter per VLM provider, pinned settings
    instruments/      # Godspeed, pairwise, presence: prompts + response schemas
    quality/          # catch trials, exclusions, rater-flow report (11.7)
    web/              # local read-only status page
  docs/
    INTERFACE.md      # the push/open/status/export contract
    protocol-template.md
  tests/

<study>/              # e.g. the OLAF paper's study folder, NOT in this repo
  study.yaml          # instruments, models, persona quotas, repeats
  protocol.md         # frozen -> protocol.lock (hash)
  tests/*.yaml        # test definitions
  board.db            # the board's state for this study
  blinding_key.csv    # condition labels, never read by the runner
  archive/            # raw prompts + responses
  exports/            # results CSVs
```
All state for a study lives in its own folder, so one install of the tool can serve several studies.

## Study config
A study is two kinds of file: one study.yaml that sets up the panel, and one YAML per test. Values are the OLAF paper's current plan.
```yaml
# study.yaml
study: olaf-aliveness
protocol: protocol.md          # frozen -> protocol.lock
panel:
  personas:
    seed: 42
    big_five: all_32_profiles  # high/low on each trait
    nars: [low, high]          # 32 x 2 = 64 persona cards
    quotas: [age_band, gender, cultural_region, robot_experience]
  models:                      # pinned; each must pass perception screening
    - {provider: <provider-a>, model: <id>, temperature: 0, fps: <n>}
    - {provider: <provider-b>, model: <id>, temperature: 0, fps: <n>}
instruments:
  godspeed:  {subscales: [animacy, likeability], scale: 5}
  pairwise:  {question: "Which one feels more alive?"}
  presence:  {scale: 7}
session:
  practice_clips: 2
  repeats: 3
  max_retries: 2
  cost_ceiling_usd: <limit>
```
```yaml
# tests/finding2-at-rest.yaml
test: finding2-at-rest
kind: main                     # main | pilot | screening
clips: [c_7f3a, c_91bd, ...]   # anonymized IDs returned by push
instruments: [godspeed, pairwise]
pairing: all_pairs
repeats: 3
```
Placeholders in angle brackets are open until the models are onboarded (11.4) and the pilot sets the budget (11.8).

## Interface
Four commands are the whole contract with the outside world; Epic 12 uses nothing else.
| Command | Takes | Does | Returns |
|---|---|---|---|
| raterpanel push clip <file> --condition <label> | A clip file and its condition label | Copies the clip under a new anonymized ID; writes the label to the blinding key only | The clip ID |
| raterpanel push test <test.yaml> | A test definition | Validates clip IDs, instruments and pairing plan | The test ID |
| raterpanel open <test> | A pushed test | Assigns every clip to every screened agent and starts the runner. Refuses kind: main if the protocol is not frozen | Run started |
| raterpanel status [<test>] | Optional test | Progress per test, model and agent: done, failed, retried, cost so far. Same view on the local web page | A status table |
| raterpanel export <test> | A completed test | Joins responses with the blinding key | One tidy CSV |

The export has one row per answered item: agent_id (p17-m2), persona_* (Big Five profile, NARS band, age band, gender, region, robot experience), model (provider and pinned version), clip_id (c_7f3a), condition (read from the blinding key at export time only), instrument, item (godspeed, animacy_3), response (4, or chosen clip ID for pairwise), repeat (1–3), timestamp (ISO 8601).
The panel itself is built with separate setup commands (personas generate, screen personas, screen models) that the study owner runs once before opening tests.

## Epic 11 stories in the new repo
Six of the nine Epic 11 stories become code in the tool; the protocol, pilot and human anchor stay with the study.
| Story | Human-panel step | Where it lands |
|---|---|---|
| 11.1 Rater study protocol | Pre-registration | Study folder (protocol.md); the tool ships a template |
| 11.2 Sampling frame & personas | Recruitment quotas | personas/ |
| 11.3 Persona fidelity screening | Screening questionnaire | screening/ |
| 11.4 Model onboarding & perception | Vision and hearing test | models/, screening/ |
| 11.5 Rating board | Study platform | board/, cli.py, web/ |
| 11.6 Rating session runner | Briefing, practice, blinded trials | runner/, instruments/ |
| 11.7 Data quality & rater flow | Attention checks, exclusions | quality/ |
| 11.8 Pilot & protocol freeze | Pilot study | Study folder: a pilot run plus protocol.lock |
| 11.9 Human validity anchor | Human subset | Study folder, if agreed on 4 Oct |
The perception screening clips (12.5) are recorded on OLAF and pushed as a screening test, like any other study input.

## Open questions and build plan
The skeleton can start now; the instruments and panel size wait for the 4 Oct meeting.
To settle with Dr. Alaa on 4 Oct:
- [ ] Does "AI agents as approvers" mean the AI panel is the primary instrument, or a pre-screen before humans?
- [ ] Are Godspeed (animacy, likeability), pairwise "more alive" and presence the final instruments?
- [ ] Is 64 personas (32 Big Five profiles × 2 NARS bands) the right pool size?
- [ ] Is a small human validity anchor (11.9) wanted, and is ethics approval needed for it?
- [ ] Should the tool be released with the paper as a citable artifact?
Build order after the meeting:
1. Board core: clip store, blinding key, push, status, export, with a fake rater so it runs end to end without API cost.
2. Instruments and one model adapter, then the runner with fresh-context sessions, retries and the cost ceiling.
3. Persona generator and both screenings.
4. Quality checks and the rater-flow report.
5. Pilot on screening clips, then freeze the protocol and open the main tests.
