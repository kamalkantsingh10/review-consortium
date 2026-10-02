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

### `consortium push clip FILE [--condition FACTOR=LEVEL ...] [--study PATH]`

Ingests a video file into the Study **blind**: nothing on the rating side can learn its Condition from the file name, metadata, format or stored state.

- `FILE` must have a video and an audio stream. Only the first video and first audio stream are kept.
- `--condition` / `-c` `factor=level` is repeatable, one per factor. It may be omitted entirely (for example for a Practice clip): such a Clip gets no key rows and is left out of the leak report.
- `--study PATH` is the Study folder (default: the current directory); `study.yaml` must load.
- The file is re-encoded with ffmpeg (>= 6, a system dependency) to the `study.yaml` `media` profile: H.264 scaled to `media.height` (width keeps the aspect ratio, rounded to even), `media.fps`, `media.video_kbps`; AAC at `media.audio_kbps`, 48 kHz stereo; square pixels (`setsar=1`) and fixed colour tags (BT.709, TV range); MP4 with `+faststart`. All global, stream and chapter metadata is dropped (`-map_metadata -1 -map_chapters -1`) and bitexact flags are set, so no source tag or file name survives. Cover-art (`attached_pic`) streams are never taken as the video. The source's aspect ratio does survive (as the stored width); the leak report checks it.
- The result is stored as `clips/<clip_id>.mp4`. The Clip ID is `c_` plus 8 random lowercase base32 characters; it carries nothing about the source. Every push makes a new Clip: there is no duplicate detection.
- Neutral facts go to `board.db`; the Condition goes **only** to `blinding_key.csv`. `exports/leak-report.csv` is then refreshed. If only that refresh fails, the Clip stays stored, the Clip ID is still printed (exit `0`) and `leak_report_failed: <reason>` is logged to stderr as a warning; the next push rewrites the report.
- On success, prints the Clip ID to stdout and exits `0`.
- All-or-nothing: on any failure, `clips/`, the `board.db` rows and `blinding_key.csv` are left as they were (a first push that fails after encoding may leave a migrated `board.db` with no rows).
- ffmpeg and ffprobe runs time out after 600 seconds (`media_unreadable`).
- The source path and the Condition strings never appear in stdout, logs or error messages; errors refer to the "input file" and to conditions by position.

| Situation | Result |
| --- | --- |
| Valid file with audio | Clip stored, Clip ID printed, exit `0` |
| A `--condition` without `=`, with an empty factor or level, with control characters, or repeating a factor | `bad_condition`, exit `1`, nothing stored |
| `ffmpeg` or `ffprobe` not on `PATH`, or a numbered version below 6 | `ffmpeg_missing`, exit `1`, nothing stored |
| `FILE` has no audio stream | `no_audio: input file has no audio stream`, exit `1`, nothing stored |
| `FILE` has no video stream (for example audio with cover art only) | `media_unreadable: input file has no video stream`, exit `1`, nothing stored |
| `FILE` has no duration or frame rate | `media_unreadable: input file has no decodable frames`, exit `1`, nothing stored |
| `FILE` is missing, or ffmpeg cannot read or decode it, or times out | `media_unreadable: input file ...`, exit `1`, nothing stored |
| The file system or `board.db` fails while storing | `push_failed: could not store the Clip: <reason>`, exit `1`, nothing stored |
| Another process holds the `board.db` write lock past the 5 s busy timeout | `board_busy`, exit `1`, nothing stored |
| `board.db` was written by a newer `consortium` | `board_version_mismatch`, exit `1`, nothing stored |

### `consortium personas generate [--study PATH] [--force]`

Generates the Persona Panel from `study.yaml` (`seed` and `personas`) into `panel/personas/`. No network, no LLM: the cards are assembled from the approved wording file.

- `--study PATH` is the Study folder (default: the current directory); `study.yaml` must load.
- **Pool.** The 32 Big Five profiles are every high/low combination of openness (O), conscientiousness (C), extraversion (E), agreeableness (A) and neuroticism (N), ordered by bit pattern (`low` = 0, O most significant: profile 1 is all low, profile 32 all high). Each profile is crossed with every `personas.nars_bands` entry in the order listed (profile-major). Persona IDs run `p1 ... pN`, N = 32 x the number of bands (64 by default; `p1` is all-low with the first band, `p2` all-low with the second).
- **Quotas.** Each quota attribute (`age_band`, `gender`, `cultural_region`, `robot_experience`) is assigned independently and stratified by NARS band. The N levels are laid out round-robin in listed order (so the marginal counts are equal, with any remainder going one each to the earliest-listed levels: 3 regions over 64 give 22/21/21). That sequence is cut into consecutive blocks of 32, one per band in frame order, so within each band every level appears 32 // k or 32 // k + 1 times (k levels; counts per band differ by at most 1). A band's extra units continue the cycle where the previous band's stopped: the first band's go to the earliest-listed levels, later bands' to the next levels in turn, which is what keeps the marginal counts exact. Each block is shuffled with that attribute's seed (see [Seeds](#seeds)), bands in frame order, and dealt to that band's Personas in Persona order. The shuffle is an explicit Fisher-Yates using only `random.Random(seed).getrandbits` (rejection sampling for each index), not `random.shuffle`, so the result does not depend on the Python version. Joint balance across attributes is not attempted.
- On success, prints `<N> personas -> panel/personas` to stdout and exits `0`.
- **Reproducible.** The same `study.yaml` gives byte-identical `panel/` trees in any folder, on any run.
- **Atomic.** The set is built in a temporary folder `panel/.personas-new-*` and renamed into place; a failure leaves no partial Panel. Without `--force`, the final rename only succeeds onto an absent or empty `panel/personas`, so a Panel created by another process meanwhile is never overwritten (`panel_exists`). With `--force`, the old Panel is first renamed to `panel/.personas-old-*/personas`, the new one renamed in, then the old copy deleted (a failure to delete it is logged as a warning). If the second rename fails, the old Panel is renamed back; if that also fails, `personas_failed` names the folder that holds the old Panel.
- **Crash window.** With `--force`, a crash between the two renames leaves no `panel/personas`; the old Panel is then in `panel/.personas-old-*/personas`. Move it back by hand or run `personas generate` again. Each run first removes leftover `panel/.personas-new-*` folders, and `panel/.personas-old-*` folders only while `panel/personas` exists, so a preserved old Panel is never swept.

| Situation | Result |
| --- | --- |
| Fresh Study | `p1.md ... p64.md` and `index.json` written, `64 personas -> panel/personas`, exit `0` |
| `panel/personas/` exists and is not empty | `panel_exists: panel/personas already exists (use --force)`, exit `1`, nothing changes |
| `panel/personas/` exists, `--force` | The old set is replaced atomically by the regenerated set, exit `0` |
| `personas.nars_bands` or a quota list is empty, or `study.yaml` is otherwise invalid | `config_invalid` (for example `personas.nars_bands: ...`), exit `1`, nothing written |
| The wording file has no sentence for a band in the frame | `config_invalid: nars.<band>: no card sentence for this NARS band`, exit `1`, nothing written |
| The wording file has no phrase for a quota level in the frame | `config_invalid: level_phrases.<attribute>.<level>: no card phrase for this quota level`, exit `1`, nothing written |
| The file system fails while writing | `personas_failed: could not write panel/personas: <reason>`, exit `1`, no partial Panel (if restoring the old Panel under `--force` also fails, the message names where it is preserved) |

Later commands read the Panel from `index.json` (never by re-deriving it); if it is absent they fail with `panel_missing`.

## Error codes

| Code | Raised by | Meaning |
| --- | --- | --- |
| `study_exists` | `init` | The target path is a file or a non-empty directory. |
| `study_create_failed` | `init` | The operating system refused to create or write the Study folder; the message gives the reason. |
| `config_invalid` | any command that loads config | A config or Instrument file is missing, is not valid YAML, or fails its schema. The message is `<field>: <reason>`; the error's path is the file, relative to the Study folder. |
| `bad_condition` | `push clip` | A `--condition` is not `factor=level` with both parts non-empty, contains control characters, or repeats a factor. |
| `ffmpeg_missing` | `push clip` | `ffmpeg` or `ffprobe` is not on `PATH`, cannot be run, or is older than version 6. |
| `no_audio` | `push clip` | The input file has no audio stream. |
| `media_unreadable` | `push clip` | The input file is missing, or ffmpeg cannot read or decode it. |
| `push_failed` | `push clip` | The file system or SQLite failed while storing the Clip; nothing was stored. The message names no source path. |
| `board_busy` | any command that writes `board.db` | Another process holds the `board.db` lock past the busy timeout. Try again. |
| `board_version_mismatch` | any command that opens `board.db` | `board.db` has a newer layout version (`PRAGMA user_version`) than this `consortium` knows. |
| `board_wal_unavailable` | any command that opens `board.db` | SQLite could not put `board.db` in WAL mode (for example on some network file systems). |
| `panel_exists` | `personas generate` | `panel/personas/` already holds files; pass `--force` to replace them. |
| `personas_failed` | `personas generate` | The file system failed while writing the Panel; no partial Panel is left. |
| `panel_missing` | any command that needs Personas | `panel/personas/index.json` does not exist; run `consortium personas generate`. |
| `panel_invalid` | any command that needs Personas | `panel/personas/index.json` cannot be read as a non-empty list of Personas (all five traits with `high`/`low`, `nars` `low`/`high`, ids exactly `p1 ... pN` in order), or a `p<n>.md` card is missing. |
| `unknown_instrument` | any command that loads config | An Instrument name in `study.yaml` or a Test does not resolve, or a Test lists an Instrument not enabled in `study.yaml`. |

## Study folder layout

```text
<study>/
  study.yaml           Study configuration: seed, Models (by id), defaults.        (init; schema in story 1.2)
  protocol.md          Study protocol.                                              (init; full template arrives in story 4.1)
  prices.yaml          Per-Model prices in USD.                                     (init; schema in story 1.2)
  tests/*.yaml         Test definitions; init writes a pilot Test, example.yaml.    (init; schema in story 1.2, registration in 1.5)
  instruments/*.yaml   Optional user Instruments.                                   (story 1.2)
  panel/personas/      Persona cards (p<n>.md), index.json and meta.json.            (personas generate)
  panel/               Screening snapshot.                                          (arrives in story 4.1)
  clips/<clip_id>.mp4  Canonicalized, metadata-free Clips.                          (push clip)
  blinding_key.csv     The only place Conditions exist.                             (push clip)
  board.db             All mutable Study state (SQLite).                            (push clip)
  board.lock           Exclusive lease held by a dispatching command.               (arrives in story 1.7)
  archive/requests.jsonl   Append-only rendered requests.                           (arrives in story 1.7)
  archive/responses.jsonl  Append-only raw responses.                               (arrives in story 1.7)
  exports/             Export CSVs and reports; leak-report.csv from push clip.     (exports in story 1.12)
  protocol.lock        Hashes of every file that affects the data.                  (arrives in story 4.1)
```

All state lives in the Study folder. Paths stored inside it are relative to it.

### `panel/personas/`

Written only by `personas generate`; never edited in place once used.

- **`p<n>.md`** — the Persona card the Model sees. UTF-8, LF line endings, one trailing newline; no timestamps, paths, versions, Persona ID or labels. It describes behaviour only: it never contains a trait name, `high`, `low` or `NARS`. Exactly seven lines, in this order: the demographic line; the openness, conscientiousness, extraversion, agreeableness and neuroticism sentences for the Persona's poles; the NARS-band sentence. For example (`p1` of the template Study):

  ```text
  You are a man, aged 18 to 29, from the Middle East or North Africa, with regular experience of robots.
  You prefer familiar things and practical, well-tried ways of doing them.
  You take things as they come and do not worry much about plans or details.
  You are quiet and reserved, and you prefer calm settings or small groups.
  You say what you think plainly, and you trust others once they have shown they are reliable.
  You stay calm under pressure and rarely worry for long.
  You feel comfortable around robots and would be at ease interacting with one.
  ```

- **`index.json`** — the labels, for screening and export (the export's `persona_*` columns). Canonical JSON (sorted keys, UTF-8, no whitespace, no trailing newline): a list of Persona objects in ID order, each `{"age_band", "big_five": {"agreeableness", "conscientiousness", "extraversion", "neuroticism", "openness"}, "cultural_region", "gender", "id", "nars", "robot_experience"}`, where every `big_five` value is `"high"` or `"low"`.
- **`meta.json`** — provenance, canonical JSON: `{"frame_sha256", "generator_version", "seed", "wording_sha256"}`. `seed` is `study.yaml` `seed`; `frame_sha256` is the SHA-256 of the canonical JSON of `study.yaml` `personas` (with defaults filled in); `wording_sha256` is the SHA-256 of the packaged `wording.yaml` bytes; `generator_version` (currently `"1"`) changes whenever the generation or card algorithm changes. It is not yet checked against the Study.

### `clips/<clip_id>.mp4`

One canonical MP4 per Clip, written by `push clip`. The file name is the Clip ID; the file holds no user metadata. Temporary files named `clips/.push-*.mp4` exist only while a push runs.

### `board.db`

SQLite in WAL mode; the only mutable Study state, created by the first `push clip`. Its layout version is `PRAGMA user_version` (currently `1`). Table `clips`, one row per Clip:

| Column | Meaning |
| --- | --- |
| `clip_id` | Primary key, `c_` + 8 lowercase base32 characters. |
| `sha256` | SHA-256 (lowercase hex) of the stored canonical file. |
| `duration_s` | Duration in seconds. |
| `size_bytes` | Size of the stored file. |
| `width`, `height` | Resolution of the stored file. |
| `fps` | Frame rate of the stored file. |
| `loudness_lufs` | Integrated loudness (EBU R128), measured once at push; empty when it is -inf (digital silence). |
| `pushed_at` | UTC ISO 8601 time with `Z`. |

`board.db` never holds a Condition, the source file name or a hash of the source file.

### `blinding_key.csv`

The only place Conditions exist. Long CSV with header `clip_id,factor,level`, one row per Clip and factor, appended (and fsynced) by `push clip`. A Clip pushed with no Condition has no rows. The file is created by the first push that has a Condition. Only the push and export stages read it.

### `exports/leak-report.csv`

Rewritten after every `push clip` (written to a temporary file, fsynced, then renamed). Checks whether a Condition could be guessed from the media itself. Columns `factor,metric,level,n,mean,max_diff,tolerance,flagged`; one row per factor, metric and level. Levels are compared within each factor; Clips with no Condition are left out.

| Metric | Compared | `tolerance` |
| --- | --- | --- |
| `duration_s` | `max_diff` = largest difference between this level's mean and another level's mean | `thresholds.leak_tolerance.duration_s` |
| `loudness_lufs` | as for `duration_s` (Clips with empty loudness are not counted in `n`) | `thresholds.leak_tolerance.loudness_lufs` |
| `width`, `height`, `fps` | must match: `max_diff` = largest difference between any Clip of this level and any Clip of another level | `0` (`fps`: `0.01`) |

`n` is the number of Clips in the level, `mean` its mean. `max_diff` is empty when the factor has only one level. `flagged` is `true` when `max_diff > tolerance`, else `false`.

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
| `media` | Canonical Clip encoding: `height` 480 (must be even), `video_kbps` 400, `audio_kbps` 64, `fps` 25. |
| `session.practice_clips` | Practice examples included per Trial per Instrument (>= 0); template `2`. |
| `session.repeats` | Repeats per Agent (>= 1); template `3`. |
| `session.max_retries` | Retries after an invalid answer (>= 0); template `2`. |
| `session.pairing` | Pairing rule for pairwise Instruments; only `all_pairs` is accepted. |
| `concurrency` | Max in-flight calls per provider (>= 1); template `4`. |
| `thresholds` | *Required*, every key, no code defaults: `persona_fidelity_min` (0-1, template `0.8`), `invalid_rate_max` (0-1, template `0.05`), `leak_tolerance.duration_s` (template `1.0`), `leak_tolerance.loudness_lufs` (template `2.0`). Resolution and fps must match exactly. |
| `personas.big_five` | `all_32` (every high/low combination of the five traits). |
| `personas.nars_bands` | Non-empty, unique subset of `low`, `high`; template `[low, high]`. |
| `personas.quotas` | *Required*: `age_band`, `gender`, `cultural_region`, `robot_experience`, each a non-empty list of unique levels. |

**Placeholder quota levels.** The template's quota levels are placeholders, each marked `# PLACEHOLDER — Kamal to confirm before the OLAF study`: age bands `18-29`, `30-44`, `45-59`, `60+`; genders `woman`, `man`; eight broad cultural regions; robot experience `none`, `some`, `regular`. They are template defaults only, never code defaults. Confirm or replace them before the OLAF study.

### Persona card wording (built in)

The card text comes from the package file `src/consortium/templates/persona_card/wording.yaml`, approved by Kamal on 2026-10-02. It is not part of the Study folder and is not user-editable without a code change; changing it changes every card.

| Field | Meaning |
| --- | --- |
| `voice` | `second_person`. |
| `demographic` | A one-line template using exactly the placeholders `{age_band}`, `{gender}`, `{cultural_region}`, `{robot_experience}`, each plain (no `!conversion` or `:format` spec). |
| `traits.<trait>.{high,low}` | One sentence per pole for each of `openness`, `conscientiousness`, `extraversion`, `agreeableness`, `neuroticism`. |
| `nars.<band>` | One sentence per NARS band; every band in `personas.nars_bands` needs one (else `config_invalid`). |
| `level_phrases.<attribute>.<level>` | The phrase inserted for a quota level (for example `60+` -> `aged 60 or over`). Every level in the Study's `personas.quotas` needs one (else `config_invalid`); a level is never inserted verbatim. |

Every sentence and phrase must be a single line (no line break of any kind, including `\u2028`, `\x85`, vertical tab and form feed), have no leading or trailing whitespace, and must not contain (case-insensitive) a trait name, `high`, `low`, `NARS`, or a trait stem: `agreeab*`, `conscientious*`, `neurotic*`, `extravert*`/`extrovert*` (and `extravers*`/`extrovers*`), `introvert*`, `open-minded` (`config_invalid`).

### Seeds

`study.yaml` `seed` is the only seed. Every other seed is derived as `int(sha256("<seed>:<purpose>:<key>").hexdigest()[:8], 16) & 0x7FFFFFFF` (the first 8 hex digits of the SHA-256, masked to 31 bits) and used only through Python's `random.Random(seed)`. Persona quotas use purpose `personas` with the attribute name as key (for example `1:personas:gender`).

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
