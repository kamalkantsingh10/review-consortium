# consortium — Interface

This document is the contract for the `consortium` command-line tool and the Study folder it works in. Every story that adds or changes a command or a Study-folder file updates it.

## Conventions

- **Output.** Data goes to stdout. Logs go to stderr through Python's `logging` (pass `-v` / `--verbose` before the command for debug logs). The tool sends no telemetry.
- **Errors.** Every failure is reported as one line on stderr:

  ```text
  <code>: <message>
  ```

  `<code>` is a stable snake_case reason code; `<message>` is human-readable. The exit code is `1`. Scripts should match on `<code>`, not on the message.
- **Exit codes.** `0` on success, `1` on any `consortium` error. Usage errors (unknown option, missing argument) exit with `2`.

## Commands

### `consortium init PATH`

Creates a new Study folder at `PATH` from the built-in template. The template uses the Fake rater, so a fresh Study runs offline at zero cost.

- `PATH` must not exist, or must be an empty directory. Missing parent directories are created.
- On success, prints the created path to stdout and exits `0`.
- Creates `study.yaml`, `protocol.md`, `prices.yaml` and `tests/example.yaml`. The created Study validates with no edits (see [Configuration files](#configuration-files)).

| Situation | Result |
| --- | --- |
| `PATH` does not exist | Study created, exit `0` |
| `PATH` is an empty directory | Study created, exit `0` |
| `PATH` is a directory with any content | `study_exists: <PATH> is not empty`, exit `1`, nothing written |
| `PATH` is a file | `study_exists: <PATH> exists and is not an empty directory`, exit `1`, nothing written |
| `PATH` cannot be created or written (for example a parent is a file, or permission denied) | `study_create_failed: <reason>`, exit `1` |

## Error codes

| Code | Raised by | Meaning |
| --- | --- | --- |
| `study_exists` | `init` | The target path is a file or a non-empty directory. |
| `study_create_failed` | `init` | The operating system refused to create or write the Study folder; the message gives the reason. |
| `config_invalid` | any command that loads config | A config or Instrument file is missing, is not valid YAML, or fails its schema. The message is `<field>: <reason>`; the error's path is the file, relative to the Study folder. |
| `unknown_instrument` | any command that loads config | An Instrument name in `study.yaml` or a Test does not resolve, or a Test lists an Instrument not enabled in `study.yaml`. |

## Study folder layout

```text
<study>/
  study.yaml           Study configuration: seed, Models (by id), defaults.        (init; schema in story 1.2)
  protocol.md          Study protocol.                                              (init; full template arrives in story 4.1)
  prices.yaml          Per-Model prices in USD.                                     (init; schema in story 1.2)
  tests/*.yaml         Test definitions; init writes a pilot Test, example.yaml.    (init; schema in story 1.2, registration in 1.5)
  instruments/*.yaml   Optional user Instruments.                                   (story 1.2)
  panel/               Persona cards and the screening snapshot.                    (arrives in story 1.3)
  clips/<clip_id>.mp4  Canonicalized, metadata-free Clips.                          (arrives in story 1.4)
  blinding_key.csv     The only place Conditions exist.                             (arrives in story 1.4)
  board.db             All mutable Study state (SQLite).                            (arrives in story 1.4)
  board.lock           Exclusive lease held by a dispatching command.               (arrives in story 1.7)
  archive/requests.jsonl   Append-only rendered requests.                           (arrives in story 1.7)
  archive/responses.jsonl  Append-only raw responses.                               (arrives in story 1.7)
  exports/             Export CSVs and reports.                                     (arrives in story 1.12)
  protocol.lock        Hashes of every file that affects the data.                  (arrives in story 4.1)
```

All state lives in the Study folder. Paths stored inside it are relative to it.

## Configuration files

Every config file is YAML, read with `yaml.safe_load` semantics into a versioned schema. Duplicate keys and list or mapping keys are refused; a key pulled in by a `<<` merge may be overridden. Numbers must be finite (`.inf` and `.nan` are refused). Every file carries `schema_version: 1`. Unknown fields are refused. The JSON Schemas are committed in `docs/schema/` (`study`, `test`, `instrument`, `prices`; each carries `"x-schema-version": 1`) and are regenerated with `python -m consortium.config.schema docs/schema`. The JSON Schemas describe each file on its own and are looser than the loader: cross-file and cross-field checks (Model ids shared by `study.yaml`, Tests and `prices.yaml`; Instrument resolution; Practice examples; file-name matching; Item type rules) are not expressed in them. The loader is authoritative.

**Errors.** Any problem is reported as `config_invalid: <field>: <reason>`, where `<field>` is a dotted path into the file (list items by index, for example `models.0.id: field required`) and the error names the file relative to the Study folder (for example `study.yaml`, `tests/x.yaml`, `instruments/trust.yaml`). When several fields are wrong, all are listed, separated by `; `. A YAML syntax error gives the line and column. An unresolved Instrument name raises `unknown_instrument` (for example `instruments.0: 'foo' not found`, file `tests/x.yaml`).

### `study.yaml`

All values below are what `init` writes. Fields marked *required* have no default in code.

| Field | Meaning |
| --- | --- |
| `schema_version` | *Required.* `1`. |
| `seed` | *Required.* Integer, 0 <= seed < 2^63; the one Study seed. |
| `instruments` | *Required.* Names of the Instruments Tests may use (unique). Template: `[godspeed, pairwise_alive, presence]`. |
| `models[]` | *Required*, at least one. Each Model: |
| `models[].id` | *Required.* `m<n>` (`m1`, `m2`, ...), unique. Models are referenced by id, never by list position. |
| `models[].provider` | *Required.* `fake`, `gemini` or `qwen`. |
| `models[].model` | *Required.* The model version to call (template: `fake-1`). Refused: an empty or blank name, and any name ending in `latest` (case-insensitive, for example `gemini-latest`). No other check is made that the name is pinned. |
| `models[].settings.temperature` | > 0; template `0.7`. |
| `models[].max_output_tokens` | *Required.* Integer > 0; template `512`. |
| `models[].limits` | *Required.* What the Model accepts per request: `max_seconds` (> 0, template `600`), `max_bytes` (integer > 0, template `20000000`), `inline_base64` (template `true`; media is sent base64-inline, which counts 4/3 of the file size). |
| `media` | Canonical Clip encoding: `height` 480, `video_kbps` 400, `audio_kbps` 64, `fps` 25. |
| `session.practice_clips` | Practice examples included per Trial per Instrument (>= 0); template `2`. |
| `session.repeats` | Repeats per Agent (>= 1); template `3`. |
| `session.max_retries` | Retries after an invalid answer (>= 0); template `2`. |
| `session.pairing` | Pairing rule for pairwise Instruments; only `all_pairs` is accepted. |
| `concurrency` | Max in-flight calls per provider (>= 1); template `4`. |
| `thresholds` | *Required*, every key, no code defaults: `persona_fidelity_min` (0-1, template `0.8`), `invalid_rate_max` (0-1, template `0.05`), `leak_tolerance.duration_s` (template `1.0`), `leak_tolerance.loudness_lufs` (template `2.0`). Resolution and fps must match exactly. |
| `personas.big_five` | `all_32` (every high/low combination of the five traits). |
| `personas.nars_bands` | Non-empty, unique subset of `low`, `high`; template `[low, high]`. |
| `personas.quotas` | *Required*: `age_band`, `gender`, `cultural_region`, `robot_experience`, each a non-empty list of unique levels. |

**Placeholder quota levels.** The template's quota levels are placeholders, each marked `# PLACEHOLDER — Kamal to confirm before the OLAF study`: age bands `18-29`, `30-44`, `45-59`, `60+`; genders `woman`, `man`, `non-binary`; eight broad cultural regions; robot experience `none`, `some`, `regular`. They are template defaults only, never code defaults. Confirm or replace them before the OLAF study.

### `tests/<name>.yaml`

| Field | Meaning |
| --- | --- |
| `schema_version` | *Required.* `1`. |
| `test` | *Required.* The Test name (letters, digits, `_`, `-`); must equal the file name without `.yaml`. |
| `kind` | *Required.* `pilot`, `screening` or `main`. |
| `instruments` | *Required.* Non-empty list of Instrument names; each must resolve and be enabled in `study.yaml` `instruments`. |
| `models` | Optional list of Model ids from `study.yaml`; omitted means every Model. |
| `clips` | Target Clip IDs, each `c_` plus 8 lowercase base32 characters (default `[]`; existence is checked by `push test`, story 1.5). |
| `practice` | Practice examples (default `[]`): each `{instrument, clips, answer}`. `instrument` must be one of the Test's `instruments` (else `unknown_instrument`, field `practice.<i>.instrument`); `clips` is exactly 1 Clip ID, or 2 for a pairwise Instrument; `answer` maps Item id to the intended value and must pass the Instrument's response schema (else `config_invalid`, field `practice.<i>.clips` or `practice.<i>.answer.<item>`). Each Instrument needs at least `session.practice_clips` examples; the first ones in list order are used (count checked in story 1.5). |
| `session` | Optional overrides of any `study.yaml` `session` key (`practice_clips`, `repeats`, `max_retries`, `pairing`). |

Prompt variants are not chosen in the Test; they rotate by Repeat (story 1.6).

### `prices.yaml`

| Field | Meaning |
| --- | --- |
| `schema_version` | *Required.* `1`. |
| `models.<model id>` | Per Model id: `input_usd_per_mtok` and `output_usd_per_mtok`, USD per million tokens, as quoted non-negative decimal strings (for example `"0.30"`; an unquoted `0.30` is refused). Every Model id in `study.yaml` must have an entry, and no other ids may appear (`config_invalid`, field `models.<id>`). The template prices the fake Model `m1` at `"0"`. |

### Instruments

An Instrument is a YAML file. Names are looked up first in the Study's `instruments/<name>.yaml` (user Instruments; no code change needed), then among the built-ins. A user Instrument with a built-in's name is refused with `config_invalid` (no silent shadowing). The `name` field must match the file name.

| Field | Meaning |
| --- | --- |
| `schema_version` | *Required.* `1`. |
| `name` | *Required.* Lowercase letters, digits and `_`; equal to the file name. |
| `version` | *Required.* A string, for example `"1"`. |
| `draft` | `true` or `false` (default `false`). A draft Instrument may not be used in a `kind: main` Test (enforced from Epic 4). Loading a Test that lists a draft Instrument logs `draft_instrument: <name>` to stderr; until Epic 4 this is only a warning, even for a `kind: main` Test. |
| `instructions` | *Required.* Text shown to the Model. |
| `prompt_variants` | *Required.* Name to text mapping, in declared order; must contain `default`. |
| `items[]` | *Required*, non-empty, unique `id`s. Each Item has `id`, `type` and `text`. `likert` Items also need `points` (2-11) and `anchors: {low, high}`; `pairwise` Items take `options` (two distinct labels, default `[A, B]`); `free_text` Items take nothing else. An Instrument is either all pairwise or has no pairwise Items. |

Each Instrument has a response schema (a JSON Schema): an object with exactly one key per Item id; Likert values are integers from 1 to `points`, pairwise values are one of `options`, free text is a non-empty string.

**Built-in Instruments.**

| Name | Items | Draft |
| --- | --- | --- |
| `godspeed` | Godspeed animacy (6) and likeability (5), 5-point semantic differentials, ids `animacy_1`-`animacy_6`, `likeability_1`-`likeability_5`. | no |
| `pairwise_alive` | One pairwise Item `alive`: "Which one feels more alive?", options `A`, `B`. | no |
| `presence` | One 7-point Item `presence_1`. A placeholder until a published presence scale is chosen. | **yes** |

## Unit of analysis

The tool produces data; it performs **no statistics**. When you analyse an Export:

- **Agents sharing a Model are not independent.** An Agent is a Persona run on a Model (`p<n>-m<n>`). All Agents on the same Model share its weights, so treat them as repeated measures of that Model, not as independent raters. Do not count Agents as if they were human participants.
- **Agent, Persona, Model and Clip are crossed factors.** Every Persona is run on every Model and rates the same Clips, so model them as crossed (for example, crossed random effects for Persona and Clip, with Model as a fixed or grouping factor), not as nested.
