---
title: 'Story 1.3 — Seeded Persona generation'
type: 'feature'
created: '2026-10-02'
status: 'ready-for-dev'
route: 'dispatch'
review_loop_iteration: 0
context:
  - '{project-root}/_doc/implementation-artifacts/epic-1-context.md'
  - '{project-root}/_doc/implementation-artifacts/epic-1-code-map.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Trials need Persona cards, and the pool must be reproducible from one seed and quota-balanced, or the study cannot be replicated.

**Approach:** Pure `core.personas` enumerates the Big Five × NARS grid, assigns quota attributes by seeded balanced shuffles and renders deterministic, behaviour-only cards from an approved wording file; a thin `personas generate` stage writes cards plus a JSON index.

**Decisions (accepted 2026-10-02):**
- Card wording: Claude drafts one sentence per Big Five pole and per NARS band plus a short demographic line (`_doc/implementation-artifacts/persona-card-wording-draft.md`). Kamal approves the wording before Story 1.3 is implemented. The approved text lives in the package file `src/consortium/templates/persona_card/wording.yaml`.
- The card the Model sees describes behaviour only: no trait labels (no "high/low", trait names or "NARS"). Labels are stored only as structured fields in `index.json`, for screening and export.

## Boundaries & Constraints

**Always:**
- **Seeds.** `core.seeds.derive_seed(study_seed, purpose, key)` returns `int(sha256(f"{study_seed}:{purpose}:{key}").hexdigest()[:8], 16) & 0x7FFFFFFF`. Here: `derive_seed(s, "personas", <attribute>)`, used only via `random.Random(seed)`.
- **Grid.** The 32 Big Five profiles cover every high/low combination of openness, conscientiousness, extraversion, agreeableness and neuroticism, ordered by bit pattern (`low`=0, O most significant). Profile-major order then takes each NARS band in frame order. IDs run `p1…pN`, where N = 32 × len(`nars_bands`) (64 by default).
- **Quotas, per attribute independently:** a list of N levels in equal counts (remainder one each to the earliest-listed levels), shuffled with that attribute's seed, assigned in Persona order. Marginals match the frame exactly.
- **`Persona` (pydantic, in core).** Fields are `id`, `big_five: dict[trait, "high"|"low"]`, `nars`, `age_band`, `gender`, `cultural_region`, `robot_experience`. These field names become the export's `persona_*` columns in 1.12.
- **Wording file** `templates/persona_card/wording.yaml` (read only via `config.load.load_card_wording()` into `CardWording`): `voice: second_person`, `traits.<trait>.{high,low}` (one sentence each), `nars.<band>` (one sentence per band in the frame; a band without a sentence → `config_invalid`), `demographic` (a line template with `{age_band}`, `{gender}`, `{cultural_region}`, `{robot_experience}`), optional `level_phrases.<attribute>.<level>` (levels not listed are inserted verbatim).
- **Card (`render_card(persona, wording)`):** UTF-8, LF, one trailing newline, no timestamps/paths/versions, no Persona ID, no trait or NARS labels. Fixed order: demographic line; the five trait sentences in O, C, E, A, N order; the NARS sentence. Only these sentences, nothing else.
- **Outputs.** The stage writes `panel/personas/p<n>.md` and `panel/personas/index.json` (canonical JSON: sorted keys, no whitespace, a list of Persona dicts). Later stories load it via `config.load.load_personas(study_dir) -> list[Persona]` (added here), never by re-deriving.
- **Atomic:** build in a temp dir under `panel/`, then rename into place; failure leaves no partial Panel.

**Never:** Persona photos, screening, `panel copy`, non-uniform quota proportions, LLM-generated card text, unapproved wording, trait labels in the card, network, YAML outside `config/`.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Default | Fresh Study, `consortium personas generate` | 64 cards `p1.md…p64.md` and `index.json`; prints `64 personas -> panel/personas`. Exit 0 | N/A |
| Reproducible | Same `study.yaml` in two folders | Both `panel/` trees are byte-identical | N/A |
| Uneven quota | 3 regions over 64 | Counts 22/21/21; the extra goes to the first-listed region | N/A |
| Panel exists | `panel/personas/` is non-empty | Nothing changes | `panel_exists: panel/personas already exists (use --force)`, exit 1 |
| Force | Panel exists, `--force` | Old cards replaced atomically with the regenerated set | N/A |
| Bad frame | Empty `nars_bands` or empty quota list | Nothing written | `config_invalid` from `load_study` (1.2) |

</frozen-after-approval>

## Code Map

- **Approved input (Kamal, 2026-10-02):** `_doc/implementation-artifacts/persona-card-wording-draft.md` is APPROVED. Copy its `wording.yaml` block verbatim. Gender quota is two levels, `woman` and `man`: also change `src/consortium/templates/study/study.yaml` line `gender: [woman, man, non-binary]` to `gender: [woman, man]`, and update any test or doc that lists three genders. The 8 region placeholders stay.
- `src/consortium/config/{models,load}.py` -- `StudyConfig.personas`, `seed`, `load_study` (1.2)
- `src/consortium/cli.py`, `core/errors.py` -- from 1.1

## Tasks & Acceptance

**Execution:**
- [ ] `src/consortium/core/seeds.py` -- `derive_seed` -- the single seed rule, AD-10.
- [ ] `src/consortium/templates/persona_card/wording.yaml` -- the approved wording, copied verbatim from the approved draft -- research content stays out of code.
- [ ] `src/consortium/config/models.py` -- `CardWording` -- typed wording.
- [ ] `src/consortium/core/personas.py` -- `Persona`, `generate_personas`, `render_card(persona, wording)` -- pure.
- [ ] `src/consortium/stages/personas.py` -- `generate(study_dir, force)` -- load config, refuse `panel_exists`, write atomically.
- [ ] `src/consortium/config/load.py` -- `load_personas(study_dir)` (`panel_missing` if absent) and `load_card_wording()` -- for 1.6 and 1.12.
- [ ] `src/consortium/cli.py` -- the `personas generate [--study PATH] [--force]` subcommand group -- a thin adapter.
- [ ] `docs/INTERFACE.md` -- the command, the `panel/` files, the card layout (behaviour only; labels in `index.json`), the wording file, and the seed rule.
- [ ] `tests/test_personas.py` -- every matrix row; a fixed `derive_seed` vector; 32 profiles × each band; marginal counts; golden `p1.md` for the template seed; no card contains a trait name, `high`, `low`, `NARS` or its Persona ID.

**Acceptance Criteria:**
- Given the template Study, when generation runs twice into separate folders, then a recursive byte comparison of the two `panel/` dirs is equal.
- Given `uv run pytest` and `uv run lint-imports`, then both pass. `core/personas.py` and `core/seeds.py` import only stdlib and pydantic.

## Implementation Notes

## Spec Change Log

## Review Triage Log

## Design Notes

- **`index.json`:** cards are behaviour-only prose for the Model; screening (Epic 3) and export (1.12) need the labels as typed attributes, without parsing Markdown.
- **Independent per-attribute shuffles** balance marginals exactly (FR-4); joint balance is not required.

## Verification

**Commands:**
- `uv run pytest -q tests/test_personas.py` -- expected: all pass
- `consortium init /tmp/a && consortium init /tmp/b && consortium personas generate --study /tmp/a && consortium personas generate --study /tmp/b && diff -r /tmp/a/panel /tmp/b/panel` -- expected: no output
