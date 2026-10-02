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

### `consortium push test FILE [--study PATH]`

Validates a Test YAML file (see [`tests/<name>.yaml`](#testsnameyaml)) against `board.db` and the Study's Instruments, then registers it under its `test:` name and prints that name.

- `--study PATH` is the Study folder (default: the current directory); `study.yaml` must load.
- A `FILE` outside the Study's `tests/` folder is copied byte for byte to `tests/<name>.yaml`; a file already at `tests/<name>.yaml` is recorded in place. Registered Test files are never rewritten.
- Registration stores the name, `kind`, the stored path, the SHA-256 of the file bytes and whether the Test is openable. A `kind: main` Test is registered **not openable**: `push test` logs `not_openable: Test <name> is kind main; not openable until Protocol lock (Epic 4)` to stderr, and `open` refuses it with `protocol_lock_unavailable` until the Protocol lock arrives (Epic 4). A `kind: screening` Test is also registered **not openable** (story 3.2): `open` refuses it with `screening_test_not_openable`, since a screening Test is run by `consortium screen models`. Pilot Tests are openable.
- Re-pushing a Test with identical bytes is a no-op that prints the name again, provided the stored `tests/<name>.yaml` still holds the registered bytes (else `test_exists`: the registered file is missing or was edited). The same holds when an identical push by another process registers first. Different bytes under a registered name are refused (`test_exists`); a changed Test needs a new name.
- The file is read once; those bytes are validated, hashed and copied. If the file changes while it is being validated, the push is refused with `test_changed`.
- On success, prints the Test name to stdout and exits `0`.
- **Checks, in order; the first failure wins** and nothing is copied or registered:
  1. **Name.** `test:` matches `^[a-z0-9]([a-z0-9_-]*[a-z0-9])?$` (lowercase letters, digits, `_` and `-`, not starting or ending with `_` or `-`) and is at most 64 characters, since it is part of every Session ID, does not end in `-attrition` (reserved for `exports/<test>-attrition.csv`) and is not `s<n>` (`^s[0-9]+$`, reserved for screening run IDs, story 3.1) — else `bad_test_name`. The pairing plan's syntax (`session.pairing`, duplicate Clip IDs) is also checked here, before the schema, as `bad_pairing`.
  2. **Schema** (`load_test`, as for every config file): `config_invalid`, `unknown_instrument`.
  3. **Registration.** Already registered with other bytes, or `tests/<name>.yaml` exists unregistered with other bytes — `test_exists`.
  4. **References.** Every target Clip is pushed (`unknown_clip`, field `clips[i]`); every Practice Clip is pushed (`bad_practice`, field `practice[i]`).
  5. **Plan.** The Test has at least 1 target Clip, and every pairwise Instrument has at least 2 — else `bad_pairing` (`clips: a Test needs at least 1 target Clip`). Pairing is always `all_pairs`.
  6. **Practice.** A pairwise example may not list the same Clip twice (`bad_practice`, field `practice[i]`). No Clip is both a Practice clip and a target (`bad_practice`, field `practice`). Each Instrument of the Test has at least the effective `session.practice_clips` examples in `practice:` (`bad_practice`, field `practice[i]` naming that Instrument's last example, or `practice` when it has none). Trials use the first `session.practice_clips` examples of their Instrument, in list order; extra examples are allowed but unused.
  7. **Overlap.** A target Clip may not be a target of both a `main` Test and a `pilot`/`screening` Test (`clip_kind_overlap`, naming the Clip and the other Test). Practice clips are exempt; pilot and screening Tests may share targets. A registered Test's kind is read from its registration.
  8. **Media limits.** For every Model the Test uses and each Instrument, the worst-case Trial must fit the Model's `limits`: the used Practice clips of that Instrument plus the longest target (single-clip Instrument) or the two longest distinct targets (pairwise), chosen separately for seconds and for bytes. Seconds are the sum of the stored durations; bytes the sum of the stored sizes, or `4 * ceil(size / 3)` per Clip when `inline_base64` is true. A total equal to the limit fits. Else `media_limit_exceeded: <model> <limit> <allowed> < <total> (<n> practice + <single|pairwise> <clip ids>)`, for example `media_limit_exceeded: m2 max_seconds 300 < 480 (2 practice + pairwise c_aaaaaaaa,c_bbbbbbbb)`. `<n> practice` counts Practice Clips, not examples (one pairwise example adds 2). Totals are rounded to 6 decimals before the comparison. Durations and sizes come from `board.db`; no ffmpeg is run.
- Errors name the Test file (relative to the Study folder) as their path. Every file-system or SQLite failure is `push_failed`. If `board.db` does not exist yet, a refused push does not create it. When `board.db` is at an older layout version, it is migrated on open (for example `1` to `2`) even if the push is then refused; no rows change.

| Situation | Result |
| --- | --- |
| Valid pilot Test | Registered openable, file at `tests/<name>.yaml`, name printed, exit `0` |
| Valid screening Test | Registered not openable (run by `screen models`), file at `tests/<name>.yaml`, name printed, exit `0` |
| Valid `kind: main` Test | Registered not openable, `not_openable` note on stderr, name printed, exit `0` |
| Same name, identical bytes, already registered | No-op, name printed, exit `0` |
| Same name, different bytes | `test_exists`, exit `1` |
| `test:` not matching `^[a-z0-9]([a-z0-9_-]*[a-z0-9])?$`, longer than 64 characters, ending in `-attrition` or of the form `s<n>` (for example `Pilot/1`, `pilot-`, `pilot1-attrition`, `s3`) | `bad_test_name`, exit `1` |
| The Test lists a self-report screening Instrument (`fidelity_bfi10`, `fidelity_nars` or any `self_report: true` Instrument), enabled in `study.yaml` or not | `instrument_not_allowed`, exit `1` |
| A target Clip ID that was never pushed | `unknown_clip: clips[i]: ...`, exit `1` |
| An unknown Instrument | `unknown_instrument`, exit `1` |
| No target Clips, a pairwise Instrument with fewer than 2 targets, duplicate Clip IDs, or `session.pairing` other than `all_pairs` | `bad_pairing`, exit `1` |
| Same name, identical bytes, but the stored `tests/<name>.yaml` is missing or edited | `test_exists`, exit `1` |
| The file changed while being validated | `test_changed`, exit `1` |
| A Clip both in `practice` and `clips`; a Practice Clip never pushed; too few Practice examples | `bad_practice`, exit `1` |
| A Practice answer failing the Instrument's response schema, or the wrong number of Practice Clips | `config_invalid: practice.<i>...` (from the Test schema), exit `1` |
| `checks` in a pilot or main Test; a check whose Instrument is not the Test's, whose Item is not the Instrument's, whose Clips are not among the Test's `clips`, or whose `expected` is wrong for its kind (see [`tests/<name>.yaml`](#testsnameyaml)) | `config_invalid: checks...`, exit `1` |
| Target shared between a `main` and a `pilot`/`screening` Test | `clip_kind_overlap`, exit `1` |
| Worst-case Trial above a Model's `max_seconds` or `max_bytes` | `media_limit_exceeded`, exit `1` |
| The file system or `board.db` fails (reading, checking or storing) | `push_failed`, exit `1`, nothing stored |

### `consortium personas generate [--study PATH] [--force]`

Generates the Persona Panel from `study.yaml` (`seed` and `personas`) into `panel/personas/`. No network, no LLM: the cards are assembled from the approved wording file.

- `--study PATH` is the Study folder (default: the current directory); `study.yaml` must load.
- **Pool.** The 32 Big Five profiles are every high/low combination of openness (O), conscientiousness (C), extraversion (E), agreeableness (A) and neuroticism (N), ordered by bit pattern (`low` = 0, O most significant: profile 1 is all low, profile 32 all high). `personas.big_five.fraction` keeps all of them (`1`) or a principal fractional factorial subset (`1/2`, `1/4`, see [Panel design](#panel-design)), in the same order. Each kept profile is repeated `personas.big_five.replicates` times, and each copy is crossed with every `personas.nars_bands` entry in the order listed. Order is profile, then replicate, then band (band fastest). Persona IDs run `p1 ... pN`, N = profiles x replicates x max(1, bands) (64 by default; `p1` is all-low with the first band, `p2` all-low with the second). With `personas.nars_bands: []` (story 3.1) the Panel is one band-less block: every Persona has `nars: null` in `index.json`, its card has 6 lines (no NARS sentence), order is profile then replicate, and fidelity screening checks only the five traits. A Panel with at least one band is byte-identical to before. Every copy of a profile gets its own demographic draw from the quotas below.
- **Cost lever.** Every Test plans its Trials per Persona, so the number of Trials (and the cost) scales linearly with N: a half fraction halves it, a quarter fraction quarters it, and each extra replicate adds one full multiple.
- **Quotas.** Each quota attribute (`age_band`, `gender`, `cultural_region`, `robot_experience`) is assigned independently and stratified by NARS band. The N levels are laid out round-robin in listed order (so the marginal counts are equal, with any remainder going one each to the earliest-listed levels: 3 regions over 64 give 22/21/21). That sequence is cut into consecutive blocks of profiles x replicates (32 for the full grid once), one per band in frame order, so within each band every level appears B // k or B // k + 1 times (B the block size, k levels; counts per band differ by at most 1). A band's extra units continue the cycle where the previous band's stopped: the first band's go to the earliest-listed levels, later bands' to the next levels in turn, which is what keeps the marginal counts exact. Each block is shuffled with that attribute's seed (see [Seeds](#seeds)), bands in frame order, and dealt to that band's Personas in Persona order. The shuffle is an explicit Fisher-Yates using only `random.Random(seed).getrandbits` (rejection sampling for each index), not `random.shuffle`, so the result does not depend on the Python version. Joint balance across attributes is not attempted. **Limit:** a band holds only profiles x replicates Personas (8 for a quarter fraction once), so an attribute with more levels than that leaves some levels without any Persona in that band; each such case is logged as a warning `quota_levels_empty: <attribute>: band <band> has no Persona at <levels> ...`.
- **Distinct replicates.** With `replicates` 2 or 3, copies of one profile in one band whose four quota levels all coincide (which would give identical cards) are made distinct after dealing: walking the band's Personas in order, a colliding copy swaps one attribute's level (attributes in the order `age_band`, `gender`, `cultural_region`, `robot_experience`) with the first other Persona of the same band, from a different profile, for which both profiles' copies then all differ. Swaps stay within a band, so per-band counts and marginals are unchanged, and the result is deterministic. When the level combinations do not allow it, the warning `replicates_indistinct: band <band>: ...` is logged and the copies stay identical.
- **In use.** Once `board.db` holds any Trial (of any Test), `personas generate` refuses with `panel_in_use`, with or without `--force` and even when `panel/personas` was deleted, because a new Panel would re-label the Personas those Trials reference. It only reads `board.db` (no lease, no migration), and accepts an older, unmigrated layout (one without a `trials` table holds no Trials). A `board.db` with only Clips or registered Tests does not block it. The check runs before anything is touched (including the sweep of leftover work folders) and again just before the final rename; a Trial planned by an `open` between that second check and the rename is not detected (the window is a single rename). A `board.db` that cannot be read fails closed: `board_unreadable` or `board_busy`, nothing written. To change the Panel after a Run, start a new Study folder.
- On success, prints `<N> personas -> panel/personas` to stdout and exits `0`.

#### Panel design

Coding: factors O, C, E, A, N in that order, `high` = +1, `low` = -1. Each fraction is the principal one (every generator holds with the + sign).

| `fraction` | Profiles | Generators | Defining relation | Resolution | Aliasing (main effects and two-way interactions) |
| --- | --- | --- | --- | --- | --- |
| `1` | 32 | none | none | full | none |
| `1/2` | 16 (profiles 2, 3, 5, 8, 9, 12, 14, 15, 17, 20, 22, 23, 26, 27, 29, 32) | N = O·C·E·A | I = OCEAN | V | Each main effect is aliased with a four-way interaction (O = CEAN, ...), each two-way with a three-way (OC = EAN, ...). Nothing below three-way is aliased with a main effect or a two-way interaction. |
| `1/4` | 8 (profiles 4, 7, 10, 13, 17, 22, 27, 32) | A = O·C, N = O·E | I = OCA = OEN = CEAN | III | O = CA = EN, C = OA, E = ON, A = OC, N = OE, and CE = AN, CN = EA (higher-order terms omitted; `meta.json` lists the full chains). Main effects are estimable only if two-way interactions are negligible. |

In every design each trait column is balanced (half `high`) and the five main-effect columns are pairwise orthogonal.
- **Reproducible.** The same `study.yaml` gives byte-identical `panel/` trees in any folder, on any run.
- **Atomic.** The set is built in a temporary folder `panel/.personas-new-*` and renamed into place; a failure leaves no partial Panel. Without `--force`, the final rename only succeeds onto an absent or empty `panel/personas`, so a Panel created by another process meanwhile is never overwritten (`panel_exists`). With `--force`, the old Panel is first renamed to `panel/.personas-old-*/personas`, the new one renamed in, then the old copy deleted (a failure to delete it is logged as a warning). If the second rename fails, the old Panel is renamed back; if that also fails, `personas_failed` names the folder that holds the old Panel.
- **Crash window.** With `--force`, a crash between the two renames leaves no `panel/personas`; the old Panel is then in `panel/.personas-old-*/personas`. Move it back by hand or run `personas generate` again. Each run first removes leftover `panel/.personas-new-*` folders, and `panel/.personas-old-*` folders only while `panel/personas` exists, so a preserved old Panel is never swept.

| Situation | Result |
| --- | --- |
| Fresh Study | `p1.md ... p64.md` and `index.json` written, `64 personas -> panel/personas`, exit `0` |
| `panel/personas/` exists and is not empty | `panel_exists: panel/personas already exists (use --force)`, exit `1`, nothing changes |
| `panel/personas/` exists, `--force` | The old set is replaced atomically by the regenerated set, exit `0` |
| `board.db` holds any Trial (with or without `--force`) | `panel_in_use`, exit `1`, nothing changes |
| `board.db` holds only Clips (no Trials), `--force` | The Panel is regenerated, exit `0` |
| `board.db` at an older layout version without Trials | The Panel is generated; `board.db` is not migrated, exit `0` |
| `board.db` corrupt, or locked past the busy timeout | `board_unreadable` / `board_busy`, exit `1`, nothing changes |
| `personas.big_five` `fraction` not 1, 0.5, 0.25, `"1"`, `"1/2"`, `"1/4"`, or `replicates` not 1-3 | `config_invalid` (field `personas.big_five...`), exit `1`, nothing written |
| `personas.nars_bands` or a quota list is empty, or `study.yaml` is otherwise invalid | `config_invalid` (for example `personas.nars_bands: ...`), exit `1`, nothing written |
| The wording file has no sentence for a band in the frame | `config_invalid: nars.<band>: no card sentence for this NARS band`, exit `1`, nothing written |
| The wording file has no phrase for a quota level in the frame | `config_invalid: level_phrases.<attribute>.<level>: no card phrase for this quota level`, exit `1`, nothing written |
| The file system fails while writing | `personas_failed: could not write panel/personas: <reason>`, exit `1`, no partial Panel (if restoring the old Panel under `--force` also fails, the message names where it is preserved) |

Later commands read the Panel from `index.json` (never by re-deriving it); if it is absent they fail with `panel_missing`.

### `consortium open TEST [--dry-run] [--yes] [--ceiling USD] [--resume] [--study PATH]`

Opens the registered Test `TEST`. With `--dry-run`, before anything is sent or spent, it prints the counts of what a Run would send plus a digest of every request, and writes nothing. Without `--dry-run` it **runs** the Test (see [Run](#run)): every Trial is sent once through its Model's Rater. In this version only the `fake` provider has an adapter. `--ceiling USD` sets the Study's cost ceiling (a decimal amount greater than 0, for example `5.00`, else `invalid_ceiling`; see [Cost and ceiling](#cost-and-ceiling)). Without `--dry-run`, `--resume` continues a stopped Run (see [Resume](#resume)); with `--dry-run` it is ignored.

- `--study PATH` is the Study folder (default: the current directory); `study.yaml` must load.
- **Reads and checks, in order:** `--ceiling`; `study.yaml`; the Test's registration and its Clips' rows in `board.db` (opened read-only); any `kind: screening` registration (a user screening Test or a screening run `s<n>`) is refused with `screening_test_not_openable` (story 3.2), before the next check; a `kind: main` Test is refused here with `protocol_lock_unavailable`, before anything is planned (as is any other Test registered not openable: `Test '<name>' is registered as not openable`); the registered `tests/<name>.yaml` (its bytes must still match the registered SHA-256, else `test_exists`; if they change while it is validated, `test_changed`), validated as by `push test`'s schema step; then `push test`'s plan, Practice, Clip-reference and media-limit checks are run again against the current config (`bad_pairing`, `bad_practice`, `unknown_clip`, `media_limit_exceeded`, `unknown_instrument`), since `study.yaml` or an Instrument may have changed since the push; the Persona Panel (`panel/personas/index.json` and every `p<n>.md` card).
- **Plans** every Session and Trial (see [Sessions and Trials](#sessions-and-trials)) and **renders every request** (see [Trial requests](#trial-requests)), one at a time, so a broken Instrument, card or missing Clip fails here rather than mid-Run. `requests sha256` is the SHA-256 of the canonical JSON of every request concatenated in plan order (Sessions in order, Trials by `trial_index`); the same seed and inputs give the same digest in any folder.
- **Estimates the cost** of every planned Trial with the one offline cost formula (see [Cost and ceiling](#cost-and-ceiling)); nothing is sent to a provider for token counts or prices.
- **A dry run writes no Study data.** `board.db` is opened read-only (SQLite `mode=ro`; when no `board.db-wal` exists, also `immutable=1`, so no `board.db-wal` or `board.db-shm` is created; if a writer starts during the reads, they are redone with plain `mode=ro`). Normally no file in the Study folder is created, changed or touched (bytes and modification times are unchanged). One exception: a stale `board.db-wal` left by a crashed writer can make SQLite create `board.db-shm`, SQLite's own side file, which holds no Study data. No Trial is stored, no provider is contacted and no ceiling is logged (a `--ceiling` given with `--dry-run` is only shown).
- On success, a dry run prints the counts to stdout and exits `0`. For a pilot Test with 64 Personas, 1 Model, 3 Repeats, `godspeed` over 4 Clips and `pairwise_alive` over the same 4 Clips:

  ```text
  test: pilot1 (pilot) dry run
  sessions: 192
  trials per session: 4 + 12 = 16 (godspeed 4, pairwise_alive 12)
  trials: 3072
  by model: m1 3072
  by instrument: godspeed 768, pairwise_alive 2304
  by type: single 768, pairwise 2304
  requests sha256: <64 lowercase hex digits>
  cost estimate: expected <usd> USD, worst case <usd> USD (max_retries 2, transient_retries 3)
  cost covers: 3072 Trials; Clips go to: fake
  ceiling: none, committed before: 0 USD
  ```

  Models are listed in the Test's order, Instruments in the Test's order; `by type` counts single-Clip and pairwise Trials.

| Situation | Result |
| --- | --- |
| Registered pilot Test, `--dry-run` | Counts and requests digest printed, exit `0`, Study folder unchanged |
| Registered `kind: screening` Test or screening run `s<n>` (with or without `--dry-run`) | `screening_test_not_openable`, exit `1`, nothing planned (run it with `consortium screen models TEST`) |
| `kind: main` Test | `protocol_lock_unavailable`, exit `1`, nothing planned (until the Protocol lock, Epic 4) |
| `TEST` not registered, or no `board.db` | `unknown_test`, exit `1` |
| `panel/personas/` empty or missing | `panel_missing`, exit `1` (`panel_invalid` if the index or a card is unreadable) |
| The registered `tests/<name>.yaml` is missing or was edited | `test_exists`, exit `1` |
| A Clip the Test uses is no longer in `board.db` | `unknown_clip`, exit `1` |
| `board.db` is at another layout version than this `consortium` | `board_version_mismatch`, exit `1` (a read-only open never migrates; any writing command migrates an older file) |
| `board.db` is corrupt or SQLite cannot read it | `board_unreadable`, exit `1` |
| The registered file changed while it was being validated | `test_changed`, exit `1` |
| Config changed since `push test` so the Test no longer passes its checks (for example `session.practice_clips` raised, a Model's `limits` tightened, an Instrument disabled or now pairwise with fewer than 2 targets) | `bad_practice`, `media_limit_exceeded`, `unknown_instrument` or `bad_pairing`, exit `1` |
| `--ceiling` not a decimal amount greater than 0 (for example `-1`) | `invalid_ceiling`, exit `1`; nothing changes |
| `--resume` without `--dry-run` | Continues a stopped Run; see [Resume](#resume) |

#### Run

`open TEST` without `--dry-run` does everything a dry run does (same checks, same plan, same requests, so the Run dispatches exactly the requests behind the dry run's `requests sha256`), then, in order:

1. Refuses a `kind: main` Test (`protocol_lock_unavailable`, see above). Nothing is written and no `board.lock` is created. If `board.db` is at an older layout version, the Run first takes the lease, migrates it, and releases the lease (a dry run never migrates).
2. Refuses a Test that already has Trials in `board.db`: `test_already_open`, nothing written (continue a stopped Run with `--resume`). A dry run of an open Test still works.
3. Prints the summary lines to stdout, exactly as a dry run does (without `dry run`), including the cost estimate and the ceiling it will run under. Then applies the ceiling rules (see [Cost and ceiling](#cost-and-ceiling)): `ceiling_required` or `over_ceiling`, exit `1`, nothing written. Then builds a Rater for every Model of the Test: `api_key_missing` when a `gemini` or `qwen` Model's key env var (see [`study.yaml`](#studyyaml) `settings.api_key_env`) is unset or empty. Building a Rater makes no network call. Nothing is written, uploaded or sent and no `board.lock` is created. A dry run never needs a key.
4. Asks on stderr, before taking the lease (default no):

   ```text
   requests sha256: <64 lowercase hex digits>
   Run N Trials on <providers>? [y/N]:
   ```

   With `--ceiling X` the question reads `Run N Trials on <providers>, ceiling X USD (study-wide)? [y/N]:` (and likewise for `Resume`).

   `--yes` skips the question. Answering anything but yes, or end of input / Ctrl-C at the prompt: `not_confirmed`; stdin not a terminal and no `--yes`: `confirmation_required`. Either way no Trial is stored, no Archive line, ledger row or ceiling change is written. Nothing reaches a Rater (not even a Clip upload, `prepare`) before this point.
5. Takes the exclusive lease on `board.lock` (`fcntl.flock`, held for the rest of the command; the OS releases it if the process dies). If another dispatching command holds it: `study_busy`, nothing written. Under the lease it re-checks that the Test has no Trials (`test_already_open`) and that the registered Test file's SHA-256, the requests digest, the cost estimate and the providers are unchanged since the confirmation (else `test_changed`), nothing written; the ceiling rules are applied again against the committed spend now in `board.db`.
6. Stores every planned Trial as `planned` (attempt `0`) in one transaction, logs `--ceiling` if given (see [Cost and ceiling](#cost-and-ceiling)), then sends every Trial through the engine.

Per attempt, in this order: (1) the attempt's estimated cost is reserved in the ledger, `attempt` is incremented and the attempt is recorded with its seed (the derived seed for purpose `model` and key `<session_id>:<trial_index>:<attempt>`, see [Seeds](#seeds)), all in one transaction, which is refused (nothing written) when the reservation would cross the ceiling (see [Cost and ceiling](#cost-and-ceiling)); (2) the attempt's Clips are prepared (uploaded) for the Rater, only after a successful reservation, and the request is appended to `archive/requests.jsonl`; (3) the Trial is marked `sent`; (4) the request is submitted to the Rater and its handle stored; (5) the answer is collected and appended to `archive/responses.jsonl`; (6) the attempt's actual cost, computed from the returned usage, is recorded in the ledger (when the answer has no usage the reservation stands; an attempt with no ledger row, recorded before the ledger existed, gets one reserving its estimate); (7) the result's category (one of `ok`, `transient`, `refused`, `fatal`, see [Provider failures](#provider-failures)) decides the rest: an answer with category `ok` is parsed and validated against the Instrument's response schema (see [Response validation and retries](#response-validation-and-retries)) and the attempt's `valid`, `invalid_reason` and parsed answer are recorded; a `transient` result is recorded as the attempt's category with `valid` left empty; `refused` and `fatal` results are never validated; (8) the Trial takes its state: a valid answer gives `valid`; an invalid one is retried (the Trial stays `sent` and gets a new attempt through steps 1 to 8) while the Trial's invalid attempts are `<= max_retries`, else gives `invalid`; a `transient` result is retried the same way, after a backoff, while the Trial's transient attempts are `<= session.retry.transient_retries`, else gives `failed` (the attempt keeps category `transient`); `refused` gives `refused` and `fatal` gives `failed` (never validated, never retried). Any other category stops the Run with `adapter_error`, after its response line was archived and its actual cost recorded (the call may have been billed). Each `(trial_id, attempt)` is dispatched at most once. Inside the process a single writer task performs every `board.db` write and Archive append, in order. Each Clip is prepared once per Rater; at most `concurrency` (from `study.yaml`) calls are in flight per provider. The provider's handle is stored even if the Run is stopped while it is being submitted. If an adapter (or anything else) fails, the Run stops: the other Trials are cancelled, and the error is reported as `code: message` (exit `1`): a `ConsortiumError` from the adapter unchanged, any other error as `run_failed: <type>: <message>`; a Rater that returns the wrong number of results is `adapter_error`.

**State after a stopped Run (resume contract).** Every Trial is in one of these states, and `--resume` handles each (see [Resume](#resume)):

- `planned` with `attempt` `0`: never dispatched; dispatch it.
- `planned` with `attempt >= 1`: stopped between recording the attempt and marking it `sent`. Its attempt row has no `sent_at`, and the request may already be archived. Re-dispatch it with a new attempt (the old attempt number is never reused).
- `sent` with a handle: submitted; collect it at the same attempt (unless that attempt is already validated, see below).
- `sent` whose latest attempt is already recorded valid: stopped before its state write; settle it `valid` from the board (no second collect).
- `sent` whose latest attempt is already recorded invalid (`valid = 0`): stopped between validating an invalid answer and its retry; re-dispatch it with a new attempt (its handle is not collected again).
- `sent` whose latest attempt is already recorded `transient`: stopped during the backoff before its retry; re-dispatch it with a new attempt at once, with no wait (its handle is not collected again).
- `sent` without a handle: stopped while submitting (the request is archived); re-dispatch it with a new attempt.
- terminal (`valid`, `invalid`, `refused`, `failed`): done; it always has its response line.

On success it adds the Trials by state and a cost footer (Study-wide committed spend and the ceiling, `none` for an uncapped Run) to the summary and exits `0`, even when some Trials ended `failed`; then it also prints `warning: N Trials did not end valid` to stderr:

```text
test: pilot1 (pilot)
...
requests sha256: <64 lowercase hex digits>
cost estimate: expected <usd> USD, worst case <usd> USD (max_retries 2, transient_retries 3)
cost covers: 3072 Trials; Clips go to: fake
ceiling: 5 USD, committed before: 0 USD
states: valid 3072
cost: committed <usd> USD, ceiling 5 USD
```

If the Run pauses at the ceiling, the summary ends with `paused: ceiling`, and the command exits `1` with `ceiling_reached: ...` on stderr (see [Cost and ceiling](#cost-and-ceiling)).

**Trial states.** `planned` (stored, not yet sent), `sent` (submitted; not necessarily answered, or waiting out a backoff before a retry), then one of the terminal states `valid`, `invalid`, `refused`, `failed`. Terminal states never change. `refused`: the provider refused the request (a safety refusal, category `refused`); `failed`: a fatal provider error, transient errors past `session.retry.transient_retries`, or an abandoned last allowed attempt (`attempts_exhausted`).

**Fake rater** (`provider: fake`). Deterministic, offline and free. It answers each Item from `random.Random(<attempt seed>)` in the request's Item order: a Likert Item `randint(1, points)`, a pairwise Item one of its options (a position, `A` or `B`), a free-text Item the fixed string `fake answer`. The raw answer is the canonical JSON `{item_id: value}`, which matches the Instrument's response schema. Usage is `{"input_tokens": I, "output_tokens": O}` from the Model's `fake` settings in `study.yaml` (default `0` and `0`), so its cost is priced from `prices.yaml` like any Model's; model build `fake-1`, category `ok`. Its handle carries the answer itself (and its category), so it can be collected after a restart.

With `fake.invalid_rate` (`0` to `1`, default `0`) an attempt answers invalidly when `random.Random(<attempt seed>).random() < invalid_rate`, so whether an attempt is invalid is fixed by its seed: the same `study.seed` and `invalid_rate` give the same `invalid` Trials and attempt counts in a fresh Study. Invalid answers rotate with the attempt number: attempt 1, 4, ... is not JSON (`I think it is about a 4.`); attempt 2, 5, ... omits the first Item; attempt 3, 6, ... gives the first Item an out-of-range value (a Likert value `points + 1`, a pairwise choice `C`, an empty free text). The Fake rater never validates; the engine does.

With `fake.transient_rate`, `fake.refusal_rate` and `fake.fatal_rate` (each `0` to `1`, default `0`) it simulates provider failures. Each kind is drawn from its own stream, `random.Random(derive_seed(<attempt seed>, "fake_<kind>", "")).random() < rate` (kinds `fatal`, `refused`, `transient`), checked in the order fatal, refused, transient, then invalid; the first hit decides the attempt's category. A `transient` result's `raw` alternates with the attempt number between a simulated rate limit (odd attempts) and a simulated transport error (even attempts). Every non-`ok` result reports zero usage. So the same `study.seed` and rates give the same final states, attempt categories and attempt counts in a fresh Study, at any `concurrency`.

**Gemini rater** (`provider: gemini`, story 2.2). Calls `google-genai` `generate_content` (never the Interactions API, never structured output or JSON mode, so the text is identical across providers) with one user message built by `core.prompt.compose` (see [Trial requests](#trial-requests)), each part mapped one-to-one: text parts as text, each Clip as a File API `file_data` part (`video/mp4`) pinned to **static** media processing, with `video_metadata.fps` when `settings.fps` is set. The request config pins `temperature`, `max_output_tokens`, the attempt seed (unless `settings.seed_supported: false`), `media_resolution` and `thinking_config.thinking_level` when set; the SDK's own HTTP retries are off (`attempts: 1`), so the engine's transient retry is the only retry. The key is read from `GEMINI_API_KEY` (or `settings.api_key_env`) and never appears in `raw`, the handle, the Archive, logs or errors (`raw` replaces it with `***`).

- **Clips.** Each Clip is made available through the File API under a content-addressed name, `files/rc-<first 24 hex characters of the Clip's SHA-256>` (display name the Clip ID), from `clips/<clip_id>.mp4` (an unreadable local file is `prepare_failed` before any upload), so one key's Files are shared safely across Studies, Models and Raters. The provider keeps a File 48 h. A File is reused (also across Runs and resumes) only when it is ACTIVE, its `sha256_hash` (hex, or base64 of the digest or of its hex text) matches the Clip, and it is more than 1 h from its `expiration_time` (none: 47 h after its creation). A File is never deleted: when the name holds a FAILED File, other content or a File near expiry, the next name `-v2`, `-v3`, ... (up to 8) is tried; a missing name is uploaded (`409` on upload: it was just uploaded elsewhere and is read back). A PROCESSING File is polled every 2 s until ACTIVE (300 s at most). Transient errors (`408`, `429`, `5xx`, transport errors, timeouts) while looking up, uploading or polling are retried 3 times (2, 4 and 8 s apart). After that, on any other error, on an upload that FAILED or does not match the Clip, on a timeout or an ACTIVE File without a URI, preparing fails with `prepare_failed`: the Run stops and can be resumed. Clips are prepared one at a time per Clip. Before each call a Clip within 1 h of its expiry is prepared again. If the call fails with `403` or `404` whose message refers to a file or URI, the Clips are prepared again and the call retried once in the same attempt (no answer was produced). A failed re-prepare inside a call does not stop the Run: the attempt gets category `transient` (`fatal` for a `401`/`403`) with the failure summary as `raw`. Uploads happen only after confirmation and a successful reservation. Each `generate_content` call times out after 600 s (each File API request too); a timeout is `transient`.
- **Handle.** `submit` makes the call and stores its outcome as the handle `{provider, raw, usage, model_build, category, settings}` (no File URI, no key), so a resumed handle is collected without being sent again.
- **Outcomes.** A text answer (finish `STOP`, or `MAX_TOKENS`, left to validation): `ok`, `raw` the joined text parts. No candidate and no block reason: `ok` with `raw` `""` (invalid, so retried). `prompt_feedback.block_reason` set: `refused`, `raw` `blocked: <REASON>`. Finish `SAFETY`, `PROHIBITED_CONTENT`, `BLOCKLIST`, `SPII`, `RECITATION`, `IMAGE_SAFETY`, `IMAGE_PROHIBITED_CONTENT` (or any other reason containing `SAFETY`): `refused`, `raw` any text plus `finish_reason: <R>` on its own line. An API error `408`, `429`, `500`, `502`, `503` or `504`, a transport error or a timeout: `transient`; any other API error (`400`, `401`, `403`, `404`, ...) or a response the SDK cannot read: `fatal`. An error's `raw` is `<code> <status>: <message, at most 500 characters>` (the key is redacted before cutting) (a transport error: `<type>: <message>`), with empty usage.
- **Build and usage.** `model_build` is the response's `model_version` (or `null`). `usage.input_tokens` = `prompt_token_count` + `tool_use_prompt_token_count`; `usage.output_tokens` = `candidates_token_count` + `thoughts_token_count` (thinking is billed as output); extra keys `text_tokens`, `video_tokens`, `audio_tokens` (from `prompt_tokens_details`) and `thoughts_tokens`, a missing count being `0`. A response without usage metadata gives `usage` `{}` (the reservation stands as its cost).
- **Archived settings.** Each response line carries `settings`: `{name: {"value": v, "documented": true}}` (`documented`: `true` = the provider documents the setting as honoured for this model; `false` = sent, but not documented as honoured; `null` = not assessed) for `model`, `temperature`, `seed`, `fps`, `media_processing` (`"static"`), `media_resolution`, `thinking_level`, `max_output_tokens` and `prompt_format` (`core.prompt.PROMPT_FORMAT`, currently `1`). A setting not sent (an unset optional setting, the seed with `seed_supported: false`, `fps` and `media_processing` for a request without Clips) is omitted.

**Qwen rater** (`provider: qwen`, story 2.3). Speaks only the OpenAI-compatible Chat Completions API, through the `openai` SDK, against `settings.base_url` (never the `dashscope` SDK, never `oss://` temporary uploads or public Clip URLs). The same code serves hosted Model Studio and any OpenAI-compatible server: moving to a self-hosted vLLM (open-weight Qwen3-Omni) is meant to be a change of `base_url` and `model` only (plus `api_key_env` naming any non-empty token variable). vLLM compatibility rests on the API shape and has not yet been verified against a live vLLM server. The client is built with `max_retries: 0`, so the engine's transient retry is the only retry, and a call times out after 600 s (a timeout is `transient`). The client is closed when the Run ends.

- **Clips.** `prepare` works offline: it reads `clips/<clip_id>.mp4`, checks its SHA-256 against the Clip (a mismatch or unreadable file is `prepare_failed`, and nothing is sent) and sends the Clip inline as a base64 data URI: `data:;base64,<base64>` on Model Studio (its documented form), `data:video/mp4;base64,<base64>` on any other host (vLLM needs the media type). Size is enforced by `push test` (`limits.max_bytes` with `inline_base64: true` counts 4/3 of the file); hosted Model Studio requires the base64 string to be under 10 MB, so a hosted qwen Model (a `base_url` host ending in `aliyuncs.com`) needs `max_bytes` at most `9900000` and `inline_base64: true` (else `config_invalid`).
- **Request.** One user message whose content is the `core.prompt.compose` parts in order, one-to-one: each text part as `{"type": "text", "text": ...}`, each Clip as `{"type": "video_url", "video_url": {"url": <data URI>}}` (so the text is identical to Gemini's). Sent: `model`, `max_tokens` (`max_output_tokens`), `temperature`, `seed` (the attempt seed, unless `settings.seed_supported: false`), `reasoning_effort` (only when set), `stream: true` and `stream_options: {"include_usage": true}` (some omni models require streaming, so every call streams). Nothing else: no `modalities` (text output is the default), no `extra_body`.
- **Handle.** `submit` makes the call and stores its outcome as the handle `{provider, raw, usage, model_build, category, settings}` (no data URI, no key); `collect` makes no network call.
- **Outcomes.** `raw` is the concatenated `delta.content` of choice 0 (other choices are ignored). Finish `stop` or `length` (left to validation): `ok`. Finish `content_filter`: `refused`, `raw` any text plus `finish_reason: content_filter` on its own line. Any other finish reason: `fatal` (same `raw` form). A stream that ends without a finish reason: `transient`, `raw` `stream ended without finish_reason`. An API error is mapped by its code first, normalised (lowercase, `_` and `.` removed) and matched by prefix, the same for errors before and inside the stream: `datainspectionfailed*` (moderation, e.g. `data_inspection_failed`, `DataInspectionFailed.Output`): `refused`; `insufficientquota` or `arrearage`: `fatal`; then by HTTP status: `408`, `429` and `5xx` `transient`, any other status (`400`, `401`, `403`, `404`, ...) `fatal` whatever its code (e.g. `400` `InternalError.Algo.InvalidParameter`). An error inside the stream has no status: `throttling*`, `limitrequests`, `internalerror*` or `serviceunavailable*` is `transient`, any other code `fatal`. A connection error (also inside the stream) or a timeout: `transient`. A response the SDK cannot read (a malformed chunk, a non-JSON stream line): `fatal`. Any other unexpected error in a call becomes a `fatal` handle for that call only. After an error inside the stream the partial text is dropped. An error's `raw` is `<status> <code>: <message, at most 500 characters>` (a mid-stream error: `stream <code>: <message>`; a transport error: `<type>: <message>`), the key redacted before cutting, with empty usage.
- **Build and usage.** `model_build` is the last non-empty `system_fingerprint` of the stream, else `null` (the hosted response's `model` only echoes the requested name, so the build is honestly unknown). `usage.input_tokens` = `prompt_tokens`, `usage.output_tokens` = `completion_tokens`, from the final usage chunk; extra keys, when reported: `text_tokens`, `audio_tokens`, `video_tokens`, `image_tokens` (from `prompt_tokens_details`) and `reasoning_tokens` (from `completion_tokens_details`). A stream without a usage chunk gives `usage` `{}` (the reservation stands as its cost).
- **Archived settings.** `settings` holds `model`, `max_tokens`, `stream`, `stream_options`, `temperature`, `seed` (when sent), `reasoning_effort` (when sent) and `prompt_format`, each `{"value", "documented"}`. The flags come from one table in `raters/qwen.py` (Model Studio docs, checked 2026-10-02): when the `base_url` host (a trailing dot ignored) is `aliyuncs.com` or ends in `.aliyuncs.com`, `temperature` and `seed` are `false` (sent, but not documented as honoured for the omni models) and the other provider settings, `stream_options` and `reasoning_effort` included, `true`; on any other host (vLLM, self-hosted) every flag is `null` (not assessed). `prompt_format` is not a provider setting: its flag is always `null`. Usage counts that are not integers (or are booleans) are ignored. The Reporting manifest (Epic 4) surfaces every `false` and `null` setting.

#### Response validation and retries

**Validation** (`core.validate.validate_response`, never in an adapter). Every raw answer with category `ok` is parsed and checked against the Trial's Instrument. It must be exactly one JSON object, optionally wrapped in a single fenced code block (```` ```json {...}``` ````; surrounding whitespace is ignored), with a key for every Item of the Instrument and no other key. Each value must fit its Item: a Likert value an integer from `1` to `points` (`4.0`, `"4"` and `true` are not integers), a pairwise value one of the Item's options (`A` or `B`), a free-text value a non-empty string. Answers are never repaired or coerced. A failing answer gets one `invalid_response` reason: the structural checks run in this order (`not_json` to `missing_item`), then each Item's value is checked in Item order:

| Reason | Meaning |
| --- | --- |
| `not_json` | Not one JSON value or one fenced block holding one (prose, two objects, trailing text, `NaN`); a fenced body is handed to the JSON parser as is, so backticks inside a JSON string are fine |
| `not_object` | Valid JSON but not an object (a list, a number, a string, `null`) |
| `duplicate_item` | The top-level object repeats a key |
| `unknown_item` | A key that is not an Item of the Instrument |
| `missing_item` | An Item of the Instrument has no key |
| `wrong_type:<item>` | A Likert value that is not an integer, or a pairwise or free-text value that is not a string |
| `out_of_range:<item>` | A Likert integer outside `1`..`points` |
| `bad_choice:<item>` | A pairwise string that is not one of the options |
| `empty_text:<item>` | An empty (or whitespace-only) free-text answer |

**Retries.** The raw answer is always archived first. A valid answer makes the Trial `valid`. An invalid one leaves the Trial `sent` and, while the Trial's invalid attempts (attempts recorded with `valid = 0`) are `<= max_retries`, the engine sends a new attempt: `attempt` is incremented, the seed is the derived seed for key `<session_id>:<trial_index>:<attempt>`, the request text is unchanged (same `request_sha256`), and the attempt reserves its cost and can pause at the ceiling like any other (a retry refused by the ceiling leaves the Trial `sent` for `--resume`). An invalid answer with no retries left makes the Trial `invalid`. `max_retries` is the Test's effective `session.max_retries` (the Test's `session.max_retries`, else `study.yaml`'s, default `2`), with `max_retries: 0` the first invalid answer is final. Transient results have their own budget (`session.retry.transient_retries`, see [Provider failures](#provider-failures)); both budgets are counted from the Trial's `attempts` rows, never from the attempt number, so a transient result never uses up an invalid-answer retry and the other way round. Retries are sequential per Trial: attempt `n+1` is sent only after attempt `n` was collected. A Trial never gets more than `1 + max_retries + transient_retries` attempts (the hard cap, abandoned attempts included), also across a resume. When a resumed Trial has spent a budget or reached the cap, it is settled from the board without another attempt: `invalid` if its last attempt was recorded invalid (and its invalid attempts exceed `max_retries`, or it is at the cap); `failed`, keeping category `transient`, if its last attempt was recorded transient (and its transient attempts exceed `transient_retries`, or it is at the cap); `failed`, with category `attempts_exhausted`, if at the cap its last attempt was abandoned before it was answered (a crash during the last allowed attempt). Such a `failed` Trial is not an invalid answer: it is outside the invalid-answer rate and counted with `failed`. So a resumed Run can differ from an uninterrupted one only for Trials whose last allowed attempt was interrupted. A Trial whose latest attempt was already recorded valid (stopped before its state was written) is settled `valid` from the board, without collecting it again. Abandoned attempts keep their attempt row (and possibly a request line) but have no response line. Refused and fatal results are never retried.

**The answer** of a Trial is its **highest valid attempt** (a query, `board.trials.chosen_answer`, not a stored pointer).

**Invalid-answer rate** = Trials `invalid` ÷ (`valid` + `invalid`), empty (`None`) when both are `0`. `refused` and `failed` Trials are not in the denominator and are reported separately as counts. It is defined once (`core.validate.invalid_rate`) and reported per Agent and per Model for a Test (`board.trials.invalid_rates`: for each Agent and each Model the `valid`, `invalid`, `refused` and `failed` counts and the `rate`); `status` (story 1.11) uses the same definition, and `export` (story 1.12) will. The target is `thresholds.invalid_rate_max` in `study.yaml`.

| Situation | Result |
| --- | --- |
| `{"animacy_1": 4, ...}` with every Item | Trial `valid` |
| The answer in a ```` ```json ```` fence | Parsed like plain JSON |
| `I think 4` | Retried; reason `not_json` |
| Likert `6` on a 5-point Item | Retried; reason `out_of_range:<item>` |
| An Item missing, or an unknown key | Retried; reason `missing_item` / `unknown_item` |
| A pairwise answer `C` | Retried; reason `bad_choice:<item>` |
| Attempt 1 invalid, attempt 2 valid | `valid`; the answer is attempt 2; two request and two response lines |
| 3 invalid attempts, `max_retries: 2` | `invalid`; no 4th attempt |
| `max_retries: 0`, first attempt invalid | `invalid` at once |
| Stopped after attempt 2's request was archived, before it was sent | `--resume` sends attempt 3 with a new seed; attempt 2 is never sent or collected |

#### Provider failures

Every Rater result has exactly one category (AD-6): `ok` (an answer, validated as above), `transient` (a rate limit or transport error), `refused` (a safety refusal) or `fatal` (any other provider error). Adapters never raise for a provider outcome: an error at submit time goes into the handle and `collect` returns its category; `raw` holds the provider text or an error summary (never a secret). Adapters never sleep or back off; the engine does. Any other category stops the Run with `adapter_error`, after its response line was archived and its actual cost recorded (the call may have been billed). The raw response is always archived and its actual cost recorded first.

- `refused`: the Trial becomes `refused`. Never validated, never retried. The refusing attempt is never validated; earlier attempts of the same Trial may have been recorded invalid before it.
- `fatal`: the Trial becomes `failed` (attempt category `fatal`).
- `transient`: the attempt is recorded with category `transient` and `valid` empty, and the Trial stays `sent`. After a backoff it gets a new attempt through the same steps 1 to 8 (attempt incremented, new seed, same request text, its own reservation and ceiling check; a retry refused by the ceiling leaves the Trial `sent` and pauses the Run). Once the Trial has had more than `session.retry.transient_retries` transient attempts it becomes `failed` and the attempt keeps category `transient`.

**Backoff.** After the Trial's k-th transient attempt the engine waits `min(backoff_max_s, backoff_initial_s × 2^(k−1)) × u` seconds, where `u = random.Random(derive_seed(<attempt seed>, "backoff", "")).uniform(0.5, 1.0)`, a deterministic jitter in [0.5, 1.0]. The provider's concurrency slot is not held while waiting. A ceiling pause wakes every Trial waiting out a backoff at once: it starts no new attempt and stays `sent` for `--resume`. On `--resume` a Trial whose latest attempt was recorded `transient` gets its new attempt at once, without waiting.

The worst-case cost estimate is `expected × (1 + max_retries + transient_retries)` (see [Cost and ceiling](#cost-and-ceiling)); each transient retry also reserves and is checked against the ceiling one attempt at a time.

| Situation | Result |
| --- | --- |
| Attempt 1 transient, attempt 2 valid | `valid`; two request and two response lines; attempt 1 category `transient` |
| `transient_retries: 3`, 4 transient attempts | `failed`; no 5th attempt |
| `transient_retries: 0`, first attempt transient | `failed` at once |
| Category `refused` | `refused`; not validated; no retry |
| Category `fatal` | `failed`, category `fatal` |
| `max_retries: 1`: invalid, transient, invalid | `invalid` after attempt 3 |
| Stopped during a backoff (latest attempt recorded `transient`) | `--resume` sends a new attempt at once; the old handle is not collected |
| The reservation of a transient retry would cross the ceiling | The Trial stays `sent`; the Run pauses at `ceiling` |
| A Rater returns category `oops` | The Run stops: `adapter_error` |

| Situation | Result |
| --- | --- |
| Pilot or screening Test on Fake Models, `--yes` | Every Trial `valid` at attempt `1`; one request and one response line per Trial; counts by state printed; exit `0` |
| No `--yes`, answer `n` (or anything but yes) | `not_confirmed`, exit `1`; no Trial stored, no Archive written |
| No `--yes`, stdin not a terminal | `confirmation_required`, exit `1`; no Trial stored, no Archive written |
| Another dispatching command holds `board.lock` | `study_busy`, exit `1`; nothing written |
| The Test already has Trials | `test_already_open` (`...; use --resume to continue it`), exit `1`; nothing written |
| `kind: main` Test | `protocol_lock_unavailable`, exit `1`; nothing written |
| A Model of the Test uses a provider other than `fake` | `ceiling_required` without a ceiling; then `api_key_missing` (gemini or qwen, key env var unset or empty); exit `1`; nothing written or uploaded |

#### Resume

`open TEST --resume` continues a stopped Run (killed, crashed or failed) at Trial level, through the same engine. It never re-plans and never re-orders: every stored Trial is **re-rendered** from its `board.db` row plus the current Study folder (Persona card, Instrument, Practice examples, Clip hashes), which also proves the Archive can re-issue its requests. In order:

1. The same checks as a Run up to planning (a `kind: main` Test is refused with `protocol_lock_unavailable`; `test_exists`, config, Panel and Clip checks). An older `board.db` layout is migrated under the lease first.
2. Refuses a Test with no Trials in `board.db`: `test_not_open`, nothing written.
3. Takes Raters only for the Models of the non-terminal Trials (a Model whose Trials are all terminal never blocks a resume): `unknown_model` if one is no longer in `study.yaml`, `provider_unavailable` if its provider has no adapter, `api_key_missing` if its key env var is unset or empty. Before that it prints the summary lines with the cost estimate of the **non-terminal Trials only** and the ceiling, and applies `ceiling_required` and the `--ceiling` below committed spend check (but not the `expected` check of `over_ceiling`: a resume runs until the ceiling pauses it).
4. If any Trial is not terminal, asks on stderr as a Run does, counting only the non-terminal Trials (default no; `--yes` skips it; `not_confirmed` / `confirmation_required` as for a Run, nothing written):

   ```text
   requests sha256: <64 lowercase hex digits>
   Resume N Trials on <providers>? [y/N]:
   ```

   `requests sha256` is over every stored Trial's re-rendered request in plan order, so it equals the Run's (and the dry run's) digest when nothing changed. When every Trial is terminal there is nothing to confirm.
5. Takes the `board.lock` lease (`study_busy` if another dispatcher holds it, nothing written; a killed Run never blocks, since the OS releases its lock). Under the lease it re-checks the Test has Trials (`test_not_open`) and that the Test file's bytes (hashed again), the requests digest, the non-terminal Trials (state, attempt, handle), the cost estimate and the providers are unchanged since the confirmation (else `test_changed`, nothing written).
6. Runs the **re-issue check** (below); a mismatch is `reissue_mismatch`, nothing written.
7. Prints `resume: collect C, new attempt A, settled S, terminal T, archive fragments F` (S = Trials settled from the board without dispatch, see [Response validation and retries](#response-validation-and-retries); F = crash fragments skipped in the two Archive files), settles those Trials, logs `--ceiling` if given, then sends the non-terminal Trials through the engine (each new attempt reserves as in a Run; a collected attempt was reserved when it was sent):
   - terminal (`valid`, `invalid`, `refused`, `failed`): untouched, never re-sent;
   - settled (latest attempt recorded valid: `valid`; a spent budget or the `1 + max_retries + transient_retries` cap: `invalid` or `failed`): its state is written, nothing is sent or collected;
   - `sent` whose latest attempt has a stored handle and no recorded outcome yet (not validated, not recorded `transient`): collected at that same attempt (steps 5 to 8 only, then retried if invalid or transient with its budget left: no new attempt, no new request line); its response line carries the archived `request_sha256` (a stored handle that is not a JSON object stops the resume with `adapter_error: stored handle unreadable for <trial_id>`);
   - `planned` (attempt `0`, or `>= 1` after a stop between recording the attempt and marking it `sent`), `sent` without a handle, and `sent` whose latest attempt is already recorded invalid or `transient`: a new attempt (at once, no backoff) (steps 1 to 8, new seed, same request text, new request line). Attempt numbers are never reused, and an attempt that was never marked `sent` is never collected.

   The Test's pause (`paused_reason`) is cleared only when the resume ends without pausing again.

It ends like a Run: Trials by state and the cost footer, exit `0` (with the `warning:` line if some did not end `valid`), or `paused: ceiling` and `ceiling_reached`, exit `1`, if it paused again. With nothing to resume it dispatches nothing and writes nothing (no ceiling change, the pause is kept), prints the counts and exits `0`.

#### Cost and ceiling

**One cost formula.** Every figure comes from one offline function (`core.cost.estimate`); no provider is ever asked for token counts or prices. For one attempt of one Trial on Model `m` with `p = prices.yaml models.<m>`:

- media tokens = ceil(Σ `duration_s` of every Clip the request shows, Practice examples and targets, each appearance counted, × `p.media_tokens_per_s`);
- text tokens = ceil(characters of the request's canonical JSON ÷ `p.chars_per_token`);
- cost (USD) = (media + text tokens) × `p.input_usd_per_mtok` ÷ 10^6 + the Model's `max_output_tokens` × `p.output_usd_per_mtok` ÷ 10^6.

USD amounts are exact decimals, stored and printed as decimal strings (no exponent, trailing zeros dropped: `5.00` prints as `5`).

**Estimate.** `expected` is the sum of that cost over every Trial the open would send (all planned Trials for a Run or dry run, covering both pairwise orders, every Repeat and the Practice clips; the non-terminal Trials for `--resume`). `worst_case` = `expected` × (1 + `max_retries` + `transient_retries`), every Trial taking its maximum number of attempts, with the Test's effective `session.max_retries` and `study.yaml`'s `session.retry.transient_retries`. Both are printed side by side with the number of Trials and the providers that will receive Clips:

```text
cost estimate: expected 0.4183 USD, worst case 2.5098 USD (max_retries 2, transient_retries 3)
cost covers: 256 Trials; Clips go to: fake
ceiling: 5 USD, committed before: 0 USD
```

A dry run and a Run of the same Plan print identical `cost estimate` and `cost covers` lines.

**Ceiling.** The ceiling is Study-wide (it covers every Test) and lives in `board.db`, never in a file. `--ceiling USD` appends a row to `ceiling_changes` (UTC timestamp, previous ceiling, new ceiling, Test, `run` or `resume`) once a Run or resume with something to send is confirmed, under the lease (never for a dry run, a refused or declined open); the current ceiling is the latest row. A `--ceiling` below the committed spend is refused: `over_ceiling: committed C > ceiling Y; nothing was sent`. Committed spend is the Study-wide sum over the ledger of each attempt's actual cost, or its reservation where the actual cost is unknown.

- No ceiling has ever been set and none is given: `ceiling_required`, exit `1`, unless every Model the open sends to has `provider: fake` **and** is priced `0` (input and output) in `prices.yaml`; such a Run runs uncapped (it still reserves and records costs, and never pauses; its footer reads `ceiling none`).
- A Run refuses `over_ceiling: expected X > ceiling Y; nothing was sent` (prefixed `committed C + ` when earlier spend exists), exit `1`, when committed spend + `expected` > ceiling. Only `expected` is compared; when `expected` <= ceiling < `worst_case` both are printed and the Run starts, and the per-attempt pause guards retries.
- **Per-attempt reservation.** Before each attempt the engine reserves that attempt's estimated cost. The check (committed + reservation <= ceiling), the ledger row and the attempt increment are one transaction on the single writer, so concurrent dispatch never lets a *reservation* cross the ceiling, an exact fit is dispatched, and a refused reservation leaves no attempt and no ledger row. The writer keeps committed spend as a running total (read once per Run). Clips are prepared (uploaded) only after the reservation succeeds.
- **Reservations are estimates.** An attempt's actual cost replaces its reservation once known and can be higher. If it pushes committed spend over the ceiling, the Run pauses at once (no new attempt starts, in-flight attempts are collected) and stderr shows `ceiling_overshoot: committed X > ceiling Y (actual cost exceeded the estimate)` before `ceiling_reached`. So the ceiling is never crossed by a reservation; an under-estimate can cross it by at most the attempts in flight.
- **Pause.** When the next reservation would cross the ceiling, that attempt is not reserved or sent, no new attempt starts, attempts already in flight are still collected, and the Test's `paused_reason` becomes `ceiling` (`paused: ceiling`; stored even if the Run then stops on an error). The command prints `paused: ceiling` as the last summary line, the `warning:` line, and exits `1` with `ceiling_reached: ...`. Untried Trials stay `planned`. A dry run of a paused Test ends with `paused: ceiling`; opening it again without `--resume` is `test_already_open: ...; it is paused at the ceiling, use --resume --ceiling <higher>`.
- **Dry run.** A dry run applies the same rules as a Run and, if the Run would be refused, adds `would refuse: ceiling_required` or `would refuse: over_ceiling` after the ceiling line; it still exits `0`.
- **Resume.** `open TEST --resume --ceiling <higher>` logs the new ceiling, shows the estimate of the remaining Trials and continues without re-sending a completed Trial. `--resume` without a higher ceiling pauses again before any send it cannot afford (`ceiling_reached`).

| Situation | Result |
| --- | --- |
| Estimate shown, answer `y` | Runs |
| Answer `n` | `not_confirmed`, exit `1`; nothing dispatched, no ledger row, no ceiling change |
| committed + `expected` > ceiling | `over_ceiling: expected X > ceiling Y`, exit `1`; nothing dispatched |
| `expected` <= ceiling < `worst_case` | Both printed; runs; the pause guards retries |
| No ceiling ever set, a non-Fake Model, no `--ceiling` | `ceiling_required`, exit `1`; nothing dispatched |
| No ceiling, every Model `provider: fake` and priced `0` | Runs uncapped; footer `ceiling none` |
| No ceiling, a Fake Model priced above `0` | `ceiling_required`, exit `1` |
| `--ceiling` below committed spend | `over_ceiling: committed C > ceiling Y`, exit `1`; nothing logged |
| An actual cost pushes committed spend over the ceiling | `ceiling_overshoot: ...` warning, pause, `ceiling_reached`, exit `1` |
| `--ceiling -1` | `invalid_ceiling`, exit `1`; nothing changes |
| committed + reservation == ceiling | Dispatched (`<=`) |
| The next reservation crosses the ceiling mid-Run | In-flight attempts collected, `paused: ceiling`; `ceiling_reached`, exit `1` |
| Paused Run, `--resume --ceiling <higher>` | Change logged, remaining estimate shown, continues with no re-send |
| Paused Run, `--resume` at the same ceiling | Pauses again before any send; `ceiling_reached`, exit `1` |
| An attempt ends without usage | Its reservation counts as its cost |
| A Model has no `prices.yaml` entry | `config_invalid` (field `models.<id>`, file `prices.yaml`) on every `open`; nothing dispatched |

**Re-issue check.** It runs under the `board.lock` lease (as `--resume` does), so no dispatcher writes meanwhile. Every attempt marked `sent` must have a request line and no `(trial_id, attempt)` may have more than one (else `reissue_mismatch`). For every `archive/requests.jsonl` line of the Test, the canonical JSON of the request re-rendered from the stored Trial row and the current Study folder must equal the archived `request` bytes, its SHA-256 must equal `request_sha256`, and the line's `seed` and `model_id` must be those of its attempt (the derived seed for purpose `model` and key `<session_id>:<trial_index>:<attempt>`) and Trial. If a Study input that affects requests (a Persona card, an Instrument, a Clip row) was edited after the Test was opened, the check fails with `reissue_mismatch` instead of mixing request versions.

**Duplicate responses; last line wins.** If a Run dies after a response was appended but before the Trial's state was written, `--resume` collects that attempt again and appends a second response line for the same `(trial_id, attempt)`. This is allowed. Every reader of the Archive keys lines by `(trial_id, attempt)` and takes the **last line per key**; it ignores an unterminated final line and skips (and counts) any line that is not valid JSON (a fragment a crash can leave). A line that is valid JSON but not a record with a string `trial_id` and an integer `attempt` is corruption: `archive_corrupt`. Request lines are the exception to last-line-wins: the re-issue check refuses a repeated request key.

| Situation | Result |
| --- | --- |
| Run stopped with Trials `valid`, `planned`, `sent` with handle and `sent` without handle | `valid` untouched; handled `sent` collected at the same attempt; the others get a new attempt; all end `valid`; exit `0` |
| Stopped after a response was appended, before the state write | Collected again; a second response line for the same key; readers take the last |
| Stopped after an attempt was recorded, before its request was sent | New attempt `n+1`; attempt `n` is never sent or collected |
| Every Trial terminal | Nothing dispatched; counts printed; exit `0` |
| The Test has no Trials | `test_not_open`, exit `1`; nothing written |
| Another dispatching command holds `board.lock` | `study_busy`, exit `1`; nothing written |
| An archived request no longer re-renders identically | `reissue_mismatch`, exit `1`; nothing written |
| `kind: main` Test | `protocol_lock_unavailable`, exit `1` |

#### Sessions and Trials

- **Agents.** Every Persona of the Panel (in ID order) x every Model of the Test (the Test's `models`, in its order; default every Model in `study.yaml`). Agent ID `p<n>-m<n>`, for example `p12-m1`.
- **Sessions.** One per Agent and Repeat `r1 ... rN`, where N is the effective `session.repeats` (the Test's override, else `study.yaml`). Session ID `<test>/<agent>/r<repeat>`, for example `pilot1/p12-m1/r2`. Sessions are listed Persona-major, then Model, then Repeat.
- **Trials per Session.** For each Instrument of the Test, in the Test's order:
  - a single-Clip Instrument gives one Trial per target Clip;
  - a pairwise Instrument (`pairing: all_pairs`) gives, for every unordered pair of target Clips, two Trials, one per presentation order. Both share `pair_id = <instrument>:<clip_lo>:<clip_hi>` (the two Clip IDs sorted). `position` `1` shows `clip_lo` first (as `A`), `position` `2` shows `clip_hi` first. With k target Clips that is k(k-1) pairwise Trials.
- **Order.** The canonical list (Instruments in Test order; then Clips in the Test's `clips` order, pairs in `itertools.combinations` order of that list; then position 1 before 2) is shuffled with `random.Random(order_seed)`, `order_seed` = the derived seed for purpose `order` and key `<session_id>` (see [Seeds](#seeds)), using the same explicit Fisher-Yates as the Personas. The shuffled Trials are numbered `trial_index` `1 ... n`; Trial ID `<session_id>/t<trial_index>`, for example `pilot1/p12-m1/r2/t7`. Each Trial keeps its `order_seed`.
- **Prompt variant.** Repeat `r` uses the `((r - 1) mod n)`-th of the Instrument's `prompt_variants` in declared order (n = number of variants), so with variants `default`, `alt` and 3 Repeats: `r1` `default`, `r2` `alt`, `r3` `default`. The Trial keeps the variant name.
- Same seed and inputs give the same Trials in the same order, in any folder.

#### Trial requests

Each Trial is rendered into a provider-neutral request. It holds **only**:

| Field | Content |
| --- | --- |
| `persona_card` | The Persona's `p<n>.md` text, exactly as stored. |
| `instructions` | The Instrument's `instructions`. |
| `items` | The Instrument's Items: `id`, `type`, `text`, `points`, `anchors` (`low`, `high`) and `options`; `points`, `anchors` and `options` are `null` when not applicable to the Item type. |
| `response_schema` | The Instrument's response schema (see [Instruments](#instruments)). |
| `prompt` | The text of the Trial's Prompt variant. |
| `practice` | The Instrument's Practice examples: the first effective `session.practice_clips` entries of the Test's `practice:` list for that Instrument, in list order; each `{clips: [{clip_id, sha256}, ...], answer: {item_id: value}}`. |
| `clips` | The 1 or 2 target Clips as `{clip_id, sha256}`, in presentation order (the first is `A` in a pairwise Trial). |

A request contains no Trial, Session, Test, Agent or Model ID, no Instrument name or `pair_id` field, nothing from any other Trial, no Condition and no provider setting. Media are referenced by Clip ID and SHA-256 only. Its canonical JSON (sorted keys; UTF-8, where control characters are `\u`-escaped and all other text is literal UTF-8; separators `,` and `:` with no whitespace) is byte-identical for the same inputs.

**Message parts** (`core.prompt.compose`, story 2.2). Every real adapter sends a request as the same ordered parts, so the text is identical across providers: (1) the Persona card; (2) the instructions; (3) each Practice example: `Practice example <n>:` (n from 1), its Clip(s), then `Intended answer: <canonical JSON of answer>`; (4) the targets: one target after `Video to rate:`; a pair as `Video <opt0>:`, Clip A, `Video <opt1>:`, Clip B, with the pairwise Item's option labels; (5) the Prompt variant text; (6) `Items: <canonical JSON of items>`; (7) `Response schema: <canonical JSON of response_schema>`. A Practice pair is labelled exactly like a target pair. Each Clip part carries its Clip ID, SHA-256 and role (`practice` or `target`). More than 2 Clips in a Trial or Practice example, or a Clip pair whose pairwise Items do not share exactly 2 option labels, cannot be composed (`adapter_error`). A self-report (screening) Trial has no target part (4) and no Practice (3). Changing any fixed string or the order bumps `PROMPT_FORMAT` (currently `1`), which is archived with each response's settings.

### `consortium screen personas [--yes] [--ceiling USD] [--resume] [--dry-run] [--abandon] [--study PATH]`

Persona-fidelity screening (story 3.1): checks that every Agent answers a self-report questionnaire the way its Persona card says, before the Panel rates anything. It checks only that the Persona is followed; construct perception is a separate check (story 3.2).

- **Run.** Creates a screening run `s<n>` (n = 1 + the highest run number in use; chosen before the confirmation and checked again under the lease, `test_changed` if it was taken). Every Agent (every Persona x every Model of `study.yaml`) answers the fidelity Instruments: `fidelity_bfi10`, then the NARS Instrument named by `screening.nars_instrument` (default `fidelity_nars`) when `personas.nars_bands` is not empty. Each is one clip-less Trial per Session (`clip_ids` `[]`, no Practice, the Persona card exactly as stored); an Agent has `screening.fidelity_repeats` Sessions (`s<n>/<agent>/r<repeat>`), each shuffled like any Session. The Trials go through the same runner as `open` (`engine.run`): the summary, the cost estimate and the confirmation (unless `--yes`), the ceiling rules (`--ceiling`, `ceiling_required`, `over_ceiling`), the `board.lock` lease (`study_busy`), the Archive, retries, refusals, the ceiling pause and resume all behave exactly as for `open`. The summary's first line is `test: s<n> (screening)`.
- **Storage.** One transaction stores the `screening_runs` row (`open`), a `tests` row named `s<n>` (kind `screening`, not openable, path `<built-in>/screening/personas`, `sha256` = the fidelity `instrument_hash`) and the Trials (`test = s<n>`), so `status`, the ledger, pause and `--resume` work unchanged. `open s<n>` refuses (`screening_test_not_openable`) and `export s<n>` refuses (`screening_not_exportable`). Because the run stores Trials, `personas generate` then refuses with `panel_in_use`, which freezes the Panel the results describe.
- **Scoring** (`core.fidelity`, only once every Trial of the run is terminal). A reverse-keyed answer `x` counts as `points + 1 - x`. A construct's score is the mean of its valid answers over every repeat (NARS: over all its Items; the per-subscale means are detail only). A construct matches when its score is above `(points + 1) / 2` and the card's pole is `high`, or below it and the pole is `low`; a score at the midpoint, or no valid answer (for example a `refused` Trial), does not match. A check also needs at least half of its planned valid answers (its keyed Items x the Agent's Trials of that Instrument); with fewer it does not match and `detail` marks it `insufficient_data`. The checks are the five Big Five traits plus the NARS band when the Persona has one (6 checks, or 5 with no band). `score = matched / checks`; the Agent passes when `score >= thresholds.persona_fidelity_min` (with `0.8`: 5 of 6, or 4 of 5).
- **Results.** One writer transaction stores one row per Agent in `screening_results` (`instrument` `fidelity`, `outcome` `pass`/`fail`, `score`, `threshold`, `settings_hash`, `instrument_hash`, `detail`), sets the run `complete` and marks each earlier complete fidelity run whose every result key (`instrument`, `model_id`, `agent_id`) is now covered by later complete runs `superseded_by = s<n>`. Supersession is per key: the current result of a key is the one from the highest-numbered complete run that has it, so a later run that covers fewer Models leaves the other Models' earlier results current. Nothing is deleted or overwritten, so threshold shopping stays visible. `detail` also records `insufficient_data` (any check short of answers) and `draft` (a draft Instrument was used). **Stamps** (`core.hashes`, SHA-256 lowercase hex over canonical JSON), recorded on the run when it is created and copied to every result row: `settings_hash` covers the Model's `{id, provider, model, settings (without api_key_env), max_output_tokens, fake}` (`fake` is the Fake settings for `provider: fake`, else `null`); `instrument_hash` (`core.hashes.instrument_hash`) covers Instrument definitions (a list in name order) and `PROMPT_FORMAT`; the fidelity stamp stored as a fidelity row's `instrument_hash` (`core.hashes.fidelity_hash`) covers the `instrument_hash` of the fidelity Instruments planned plus the SHA-256 of the Persona card wording file.
- **Output.** The `open`-style summary, then `screening run: s<n> (fidelity) complete` (or `open (no results yet)` when it paused or stopped), one line per Model `fidelity <model>: pass <n>, fail <n> (insufficient data <k>), threshold <t>` (k: Agents with a check short of answers), and `superseded: <runs>` when it replaced earlier runs. A run that completed and was scored although it paused at the ceiling (an overshoot on its last attempts) prints its results and the `ceiling_overshoot` warning and exits `0`. Logs `draft_instrument: fidelity_nars` (stderr) while the placeholder NARS Instrument is used.
- **`--resume`** continues the open fidelity run exactly like `open --resume` and then scores it. `screening_not_open` when no fidelity run is open; `test_changed` when the current fidelity stamp (Instruments, prompt format or card wording) or any run Model's current `settings_hash` differs from the stamps recorded on the run (nothing written; `--abandon` it).
- **`--abandon`** takes the lease (`study_busy`) and marks the open fidelity run `abandoned`: no results are written and its Trials and Archive lines are kept; a fresh run is then allowed. Prints `screening run: s<n> (fidelity) abandoned`. `screening_not_open` when no fidelity run is open.
- **`--dry-run`** plans and renders the next run and prints the summary, the requests digest, the cost estimate and `screening run: s<n> (fidelity) dry run`; it writes nothing. `--resume`, `--dry-run` and `--abandon` cannot be combined (`bad_option`).
- **Refusals** (nothing written): `panel_missing` / `panel_invalid` (before anything is planned), `panel_frame_mismatch` (`personas.nars_bands` is empty but the Panel's Personas have NARS bands, or the reverse), `board_unreadable` (an open run has no `tests` row), `screening_run_open` (a fidelity run is not complete; continue it with `--resume` or drop it with `--abandon`), `draft_instrument_not_allowed` (a fidelity Instrument in use is `draft: true`, such as the placeholder `fidelity_nars`, and a Model is not `provider: fake`; the Fake rater may use it), `config_invalid` (field `screening.nars_instrument`) or `unknown_instrument` for a bad NARS Instrument, and every refusal of `open`. A run that pauses at the ceiling exits `1` with `ceiling_reached` (continue with `consortium screen personas --resume --ceiling <higher USD>`).
- **Fake rater.** With `fake.fidelity: faithful` the Fake rater reads the card it is sent and answers every keyed Likert Item whose construct is on the card with `points` when the pole is `high` XOR the Item is reversed, else `1`; `unfaithful` gives the opposite; `random` (default) answers as before. Deterministic, offline.

#### NARS Instrument

The published NARS item wording (Nomura, Suzuki, Kanda & Kato, 2006, *Interaction Studies* 7(3), 437-454) is not redistributed. The built-in `fidelity_nars` holds only its structure (Items `nars_1`-`nars_14`, 5-point; subscales S1: 4, 7, 8, 9, 10, 12; S2: 1, 2, 11, 13, 14; S3: 3, 5, 6, reverse-keyed) with neutral placeholder text, and is `draft: true`. To use the real items:

1. Copy the package template `src/consortium/templates/nars_instrument.yaml` to `<study>/instruments/<name>.yaml` and set `name: <name>` (any name except `fidelity_nars`).
2. Fill each Item's `text` and the `citation` from Nomura et al. (2006); check the subscale mapping against the paper.
3. Set `screening.nars_instrument: <name>` in `study.yaml`.

The selected Instrument must be `self_report: true`, every Item keyed with construct `nars`, a `subscale` and a `reversed` flag, and at least one of the subscales S1, S2, S3 present (an S1-only short form is valid; scoring uses the subscales present), with no Item id shared with `fidelity_bfi10`; else `config_invalid`, field `screening.nars_instrument`. The placeholder `fidelity_nars` is `draft: true`, so only the Fake rater may be screened with it (`draft_instrument_not_allowed` for any other Model). Changing it changes the fidelity `instrument_hash`, so earlier fidelity results go stale (story 3.3).

### `consortium screen models TEST [--yes] [--ceiling USD] [--resume] [--abandon] [--study PATH]`

Perception screening (story 3.2): checks that each Model perceives what an Instrument measures, using the known answers (`checks`, see [`tests/<name>.yaml`](#testsnameyaml)) of the registered `kind: screening` Test `TEST`. A Model that inverts "aliveness" fails.

- **Trials.** Only the Trials the checks need (`core.perception.check_shapes`), in check order and de-duplicated: a pairwise check's pair in both positions, a single-Clip check's one Trial per Clip. With 4 Clips and one pairwise check a Session has 2 Trials, not 12. They are answered by one neutral Persona `p0` (never a Panel Persona; no Panel is needed), whose card is `You are an adult taking part in a study about robots. Answer every question carefully and honestly.`; one Agent `p0-m<n>` per Model of the Test, with the Test's effective `session.repeats` Sessions (`s<n>/p0-m<n>/r<repeat>`, shuffled like any Session) and its Practice examples (`session.practice_clips`).
- **Run.** A screening run `s<n>` (kind `perception`, `screening_test = TEST`; numbered as for `screen personas`) goes through the same runner as `open` (`engine.run`): summary (first line `test: s<n> (screening)`), cost estimate, confirmation (unless `--yes`), ceiling rules, `board.lock` lease (`study_busy`), under-lease recheck (including the screening Test file), Archive, retries, ceiling pause and resume. One transaction stores the `screening_runs` row, a `tests` row `s<n>` that copies the screening Test's `path`, `sha256` and Clips (`test_clips`), not openable, and the Trials. Only one open perception run per screening Test is allowed (`screening_run_open`); `--resume` continues it (`screening_not_open` when none is open; `test_changed` when an Instrument of the Test, the prompt format or a Model's `settings_hash` differs from the stamps recorded on the run, or, for a fresh run, from those confirmed). Every Model of the run's Trials must have a recorded settings stamp before a resume dispatches (`board_unreadable` otherwise), so scoring never fails after the Trials are terminal.
- **`--abandon`** takes the lease (`study_busy`) and marks the open perception run of `TEST` `abandoned`: no results are written and its Trials and Archive lines are kept; a fresh run of `TEST` is then allowed. Prints `screening run: s<n> (perception) abandoned`. `screening_not_open` when no perception run of `TEST` is open; `--resume` and `--abandon` cannot be combined (`bad_option`).
- **Scoring** (`core.perception.score_perception`, once every Trial is terminal). Units are counted per repeat: a pairwise check gives one unit per position Trial, passing when the chosen option's Clip is `expected`; a two-Clip single-Clip check gives one unit, passing when `expected`'s answer is strictly greater than the other Clip's (a tie fails); a low-level check gives one unit, passing when the answer equals `expected`. A Trial that did not end `valid` fails its units. `ratio = passed / units`; the Model passes the Instrument when it has at least one unit and `ratio >= thresholds.perception_min` (default `0.8`); 0 units always fails.
- **Results.** One `screening_results` row per Model x Instrument with checks (`agent_id` empty, `score` the ratio, `threshold`, `settings_hash`, `instrument_hash` = `core.hashes.instrument_hash` of that Instrument's definition, `pair_checks` = the number of pair checks for it in `TEST`, 0 when it has only low-level checks, `detail` `{checks, passed, units, draft}`). Completing the run supersedes earlier complete perception runs of the same screening Test (per result key, as for fidelity); nothing is deleted.
- **Coverage** (`core.perception.covered_instruments` and `coverage`, the rule story 3.3 also uses). Perception results are current per screening Test: for each screening Test, the highest-numbered complete run of that Test per `(instrument, model)`, so runs of different screening Tests never hide each other's results. An Instrument is covered when any screening Test has a current result for it with `pair_checks > 0` whose `instrument_hash` equals the current definition's hash and whose `settings_hash` equals that Model's current `settings_hash` (a stale result never covers); low-level checks never cover. Before confirmation, every Instrument of a registered `main` or `pilot` Test (not `self_report`) that neither `TEST`'s own pair checks nor such a result covers is written to stderr as `coverage_gap: <instrument> (tests: a, b)`, and the same lines close the stdout summary (intentionally both: stderr before confirmation, stdout as part of the result). A registered `main` or `pilot` Test whose file cannot be read is written to stderr as `coverage_unknown: <test>`. A gap is reported, never refused.
- **Output.** The `open`-style summary, then `screening run: s<n> (perception) complete` (or `open (no results yet)`), one line per result `perception <model> <instrument>: <pass|fail> <ratio> (<passed>/<units>), pair checks <n>, threshold <t>`, the `coverage_gap: ...` lines and `superseded: <runs>` when it replaced earlier runs. A run that pauses at the ceiling exits `1` with `ceiling_reached` (continue with `consortium screen models TEST --resume --ceiling <higher USD>`).
- **Refusals** (nothing written): `unknown_test`, `not_a_screening_test` (`TEST` is a pilot or main Test, or a screening run `s<n>`), `no_screening_checks` (the screening Test declares no `checks`), `screening_run_open`, `screening_not_open`, `test_changed`, and every refusal of `open`. `open TEST` refuses a screening Test (`screening_test_not_openable`), and `export` refuses it and its runs (`screening_not_exportable`): perception Trials never appear in a pilot or main Export. No screening code reads the Blinding key.
- **Fake rater.** Each Clip has a hidden latent per Item, `fake_latent(clip_sha256, item_id)` in `[0, 1)` (`random.Random(derive_seed(0, "fake_latent", "<sha256>:<item>")).random()`, the same in every Study). With `fake.perception: faithful`, in a perception screening request (the neutral `p0` card; pilot and main Runs are answered as with `random`) with target Clips, a Likert Item about one Clip is answered `1 + floor(latent * points)` and a pairwise Item with the option of the Clip whose latent is higher; `unfaithful` uses `1 - latent` (capped at `points`); `random` (default) answers as before. Deterministic, offline.

### `consortium status [TEST] [--json] [--study PATH]`

Shows what Runs have done, failed, retried and cost so far, per Test, Model and Agent, without opening `board.db` by hand. `TEST` limits the rows to one registered Test; the footer is always Study-wide.

`status` only reads: it opens `board.db` read-only (see [`board.db`](#boarddb)), never takes the `board.lock` lease, never migrates and never writes, so it works while another command is dispatching (it shows the last committed state, including `sent` Trials in flight) and leaves `board.db` byte-for-byte unchanged. It reads neither the Archive nor `blinding_key.csv` and shows no Condition.

Rows, sorted by Test, Model, Agent (numbers in natural order, so `p2-m1` before `p10-m1`), totals before their parts:

- one Test total per registered Test (`model` and `agent` are `*`), all zeros for a Test that was never opened;
- one Model total per Model of the Test (`agent` is `*`);
- one row per Agent.

| Column | Meaning |
| --- | --- |
| `test`, `model`, `agent` | The row's Test name, Model ID and Agent ID, or `*` for a total. |
| `trials` | The row's Trial count. |
| `planned`, `sent`, `valid`, `invalid`, `refused`, `failed` | Trials in each state (`sent` = in flight). They add up to `trials`; a total is the sum of its rows. |
| `retried` | Attempts beyond the first, summed over the row's Trials: Σ max(`attempt` − 1, 0). It counts retries after invalid answers and re-dispatches on resume (both cost money). After a completed Run, the row's lines in `archive/requests.jsonl` equal Trials with at least one attempt + `retried`. After a crash there can be fewer lines than that: an attempt is recorded in `board.db` before its request line is written, and an attempt the crash stopped in between has no line. |
| `invalid_rate` | invalid ÷ (valid + invalid) of the row's counts (the one definition, see [Response validation and retries](#response-validation-and-retries)); `refused` and `failed` are separate columns. Empty (JSON `null`) when the row has no valid or invalid Trial. Printed with 4 decimals. |
| `cost_usd` | Committed spend of the row's ledger rows: each attempt's actual cost, or its reservation where the actual cost is unknown. A decimal string (`0` when nothing was spent). |
| `paused` | On a Test-total row: why that Test's Run is paused (`ceiling`); empty (JSON `null`) otherwise and on every Model and Agent row. |

The footer, Study-wide: `committed <USD> / ceiling <USD|none>   state: <state>`, where committed is the Study's committed spend, the ceiling is the latest one set with `open --ceiling` (`none` if never set) and the state is `ok`, or `paused <test> (<reason>)` for every paused Test by name, separated by `, ` (for example `state: paused pilot1 (ceiling), pilot2 (ceiling)`).

```text
$ consortium status pilot1      # a single-Model Study (m1 only), so the Model total equals the Test total
test    model  agent   trials  planned  sent  valid  invalid  refused  failed  retried  invalid_rate  cost_usd  paused
pilot1  *      *          256        0     0    226       30        0       0      194        0.1172         0
pilot1  m1     *          256        0     0    226       30        0       0      194        0.1172         0
pilot1  m1     p1-m1        4        0     0      4        0        0       0        1        0.0000         0
pilot1  m1     p2-m1        4        0     0      3        1        0       0        2        0.2500         0
...
committed 0 / ceiling none   state: ok
```

`--json` prints, instead of the table, one JSON object `{"schema_version": 1, "test": "<TEST>" | null, "rows": [...], "committed": "<USD>", "ceiling": "<USD>" | null, "state": "ok" | "paused", "paused": [{"test": "<name>", "reason": "<reason>"}, ...], "by_persona_attribute": {...} | null}`. `test` echoes the `TEST` filter (`null` without one); `paused` lists every paused Test of the Study by name (empty when `state` is `ok`). Each row has the columns above as keys, counts as integers, `invalid_rate` as a number or `null`, `cost_usd` as a decimal string, `paused` as a string or `null`.

`by_persona_attribute` (story 2.1) shows differential attrition per Persona attribute: `{"<test>": {"persona_<field>": {"<value>": {"trials": n, "invalid": n, "refused": n, "failed": n, "failed_fatal": n, "failed_transient": n, "failed_exhausted": n}}}}` for every listed Test (the counts are those of the [Attrition](#attrition) sidecar), with the export's `persona_*` column names in export column order (values in natural Persona order of first appearance; a Test never opened has every attribute with `{}`). The neutral Persona `p0` of perception screening (story 3.2) is not a Panel Persona and is skipped, so a perception run's Test shows empty tallies. It is `null` when the Panel cannot be loaded (`panel_missing` or `panel_invalid`) and Panel Personas have Trials, or the Panel lacks a Persona of the Trials; the rest of the status still works. New keys are additive: `schema_version` stays `1`. It never holds a Condition, and the text table is unchanged. Refusals and failures per Model and Agent are the rows' `refused` and `failed` columns.

| Situation | Result |
| --- | --- |
| After a Run | Test, Model and Agent rows and the footer; exit `0` |
| `status TEST` | Only that Test's rows (just a zero Test total if it was never opened); footer still Study-wide |
| A registered Test never opened | A Test-total row of zeros |
| During a Run (another command holds `board.lock`) | Current committed counts, `sent` included; never `study_busy` |
| A Test paused at the ceiling | Footer ends `state: paused <test> (ceiling)`; its Test-total row shows `ceiling` under `paused`; JSON `state` `paused` |
| No ceiling ever set | Footer `ceiling none` (`null` in JSON) |
| No `board.db` yet (fresh `init`) | Header only and `committed 0 / ceiling none   state: ok`; exit `0` |
| `TEST` not registered | Nothing on stdout; `unknown_test: TEST is not a registered Test`, exit `1` |
| `board.db` layout version not this tool's | Nothing on stdout; `board_version_mismatch`, exit `1` |

### `consortium export TEST [--study PATH]`

Writes `exports/<TEST>.csv`, a tidy CSV for analysis in R or Python, plus its attrition sidecar `exports/<TEST>-attrition.csv` (see [Attrition](#attrition)), and prints the CSV's path on stdout. Conditions are joined from `blinding_key.csv` at this moment only (they never enter `board.db`, the Archive or the logs).

`export` only reads: like `status` it opens `board.db` read-only in one read transaction, never takes the `board.lock` lease (so it works while another Test is dispatching) and writes nothing but the CSV and its sidecar (each to a uniquely named temporary file in `exports/`, fsynced, renamed over any previous export, then the `exports/` folder is fsynced). `board.db`, `archive/` and `blinding_key.csv` stay byte-for-byte unchanged. Exporting the same state twice gives identical bytes. One file per Test; Tests are never mixed.

It refuses unless every Trial of the Test is terminal (`valid`, `invalid`, `refused` or `failed`): any `planned` or `sent` Trial (a Run in progress, stopped or paused) gives `sessions_running`. It also checks that every stored Persona card `panel/personas/p<n>.md` still equals the card regenerated from `index.json` and the built-in wording (`panel_mismatch` otherwise).

Order of checks: the `board.db` checks, then the Panel and Instrument checks; only then is `blinding_key.csv` read (once), followed by the Condition-column checks (`condition_name_clash`) and the stored-answer checks (`instrument_changed`, `board_unreadable`). A refusal at any step writes no file (neither the CSV nor the sidecar).

**Rows.** One row per Item per Trial, so every Trial has at least one row: ordered by `session_id` (natural order, so `p2` before `p10`), then `trial_index`, then the Instrument's Item order. A `valid` Trial exports the answer of its highest valid attempt. Every other Trial (`invalid`, `refused`, `failed`) exports the same rows with `response` **empty** and `status` set: an empty `response` always means "no valid answer", never a value. Practice clips and their intended answers never appear. The tool computes no exclusions and no statistics.

**Columns**, in this order (schema version `1`; a later epic fills its columns without changing the version):

| Column | Meaning |
| --- | --- |
| `schema_version` | `1` on every row (the export schema version, so it travels with the file). |
| `agent_id` | `p<n>-m<n>`. |
| `session_id` | `<test>/<agent>/r<repeat>`. |
| `trial_index` | The Trial's 1-based position in its Session. |
| `clip_id` | Single-clip Trials: the rated Clip. Empty on pairwise rows. |
| `pair_id` | Pairwise Trials: `<instrument>:<clip_lo>:<clip_hi>`. Empty on single-clip rows. |
| `clip_id_a`, `clip_id_b` | Pairwise Trials: the Clips in the order shown (`A` first, then `B`). Empty on single-clip rows. |
| `persona_openness`, `persona_conscientiousness`, `persona_extraversion`, `persona_agreeableness`, `persona_neuroticism` | The Persona's Big Five poles (`high` / `low`). |
| `persona_nars`, `persona_age_band`, `persona_gender`, `persona_cultural_region`, `persona_robot_experience` | The Persona's other attributes from `index.json` (one `persona_<field>` per Persona field except `id`, in field order). |
| `model` | Model ID (`m<n>`). |
| Condition columns | Factors from `blinding_key.csv` of the Test's target Clips, sorted by name (none when no target Clip has a Condition). Per factor: `<factor>` (if the Test has single-clip Trials; filled on single-clip rows) then `<factor>_a`, `<factor>_b` (if it has pairwise Trials; the levels of `clip_id_a` / `clip_id_b`, filled on pairwise rows). Empty where the row's kind does not apply or the Clip has no level for that factor (a Clip pushed with no Condition has empty cells for every factor). |
| `instrument` | Instrument name. |
| `item` | Item ID. |
| `response` | `valid` Trials: the Likert integer, the free text, or for a pairwise Item the **chosen Clip ID** (option `A` → `clip_id_a`, `B` → `clip_id_b`). Empty for every other status. |
| `position` | Pairwise: `1` (`clip_lo` shown first) or `2`. Empty for single-clip. |
| `repeat` | Repeat number. |
| `seed` | The exported attempt's Model seed: the winning (highest valid) attempt, else the last attempt; empty if none was started. |
| `prompt_variant` | Prompt variant name. |
| `status` | The Trial's terminal state: `valid`, `invalid`, `refused` or `failed`. |
| `excluded` | `false` (exclusions arrive in Epic 4). |
| `exclusion_reason` | Empty (Epic 4). |
| `test_kind` | `pilot`, `screening` or `main`. |
| `protocol_lock` | Empty (Epic 4). |
| `timestamp` | When the exported attempt was answered (else sent), UTC ISO 8601 with milliseconds and `Z`; empty if none. |

UTF-8, `\n` line endings, standard CSV quoting (Python `csv`), header row first.

#### Attrition

`exports/<TEST>-attrition.csv` (story 2.1) makes differential attrition visible: Trial counts by outcome per Model, per Persona attribute, per Condition and per Instrument. Columns `schema_version,dimension,attribute,value,trials,invalid,refused,failed,failed_fatal,failed_transient,failed_exhausted`, one row per value:

| `dimension` | `attribute` | `value` |
| --- | --- | --- |
| `model` | `model_id` | The Model ID |
| `persona` | The `persona_<field>` column name (export column order) | The Persona's value |
| `condition` | The Condition column name of the tidy CSV (`<factor>`, `<factor>_a`, `<factor>_b`) | The level, empty where the tidy CSV cell is empty |
| `instrument` | `instrument` | The Instrument name |

`schema_version` is `1` on every row. `trials` counts Trials (not Item rows); `invalid`, `refused` and `failed` count Trials in that state. `failed` is the total of `failed_fatal` (category `fatal`, or any other or none recorded on the last attempt), `failed_transient` (transient budget spent) and `failed_exhausted` (`attempts_exhausted`). Rows are in that dimension order, then attribute order, then value as first seen in the tidy CSV. For every attribute the `trials` sum to the Test's Trials. Same encoding as the tidy CSV; the tidy CSV itself is unchanged. Both files are written together: each to its temporary file (fsynced), then both renamed, then the folder fsynced. Conditions appear here (and in the tidy CSV) only, never in `status`.

Free-text responses are exported verbatim and may start with `=`, `+`, `-` or `@`. A spreadsheet may run such a cell as a formula: import the file as text, or analyse it in R or Python.

**stderr** (logging, warnings): the invalid-answer rate per Model (`invalid_rate: model m1 0.1172 (valid 226, invalid 30, refused 0, failed 0)`, `n/a` with no valid or invalid Trial) and each Agent whose rate is above `thresholds.invalid_rate_max` (`invalid_rate_above_max: agent p2-m1 0.2500 > 0.05`), both from the one invalid-rate definition. Condition values are never logged.

| Situation | Result |
| --- | --- |
| Every Trial terminal | `exports/<TEST>.csv`, its path on stdout; exit `0` |
| Any Trial `planned` or `sent` (running, stopped or paused) | No file; `sessions_running: <n> Trials not terminal`, exit `1` |
| Test registered, never opened | No file; `nothing_to_export: <TEST>`, exit `1` |
| `TEST` not registered (or no `board.db`) | No file; `unknown_test: <TEST>`, exit `1` |
| A target Clip has no row in `blinding_key.csv` (pushed with no Condition, or no key file at all) | Exported; its Condition cells are empty; exit `0` |
| `blinding_key.csv` exists but cannot be read | No file; `blinding_key_missing: blinding_key.csv cannot be read: <reason>`, exit `1` |
| A Condition column would equal another column (e.g. factor `model`, or `clip_id` in a pairwise Test) | No file; `condition_name_clash: <factor>`, exit `1` |
| A stored Persona card differs from its regenerated card | No file; `panel_mismatch: panel/personas/p<n>.md`, exit `1` |
| `panel/personas/index.json` absent | No file; `panel_missing`, exit `1` |
| The Panel cannot be read (bad `index.json`, a card missing or unreadable) | No file; `panel_invalid`, exit `1` |
| An Instrument of the Trials is no longer loaded (config edited) | No file; `unknown_instrument`, exit `1` |
| A valid Trial's stored answers do not have exactly its Instrument's Items (Items added or removed after the Run) | No file; `instrument_changed: <instrument>: stored answers do not match its Items`, exit `1` |
| `board.db` unreadable, an attempt row missing, or a stored answer or `clip_ids` of the wrong type or length (a Likert value that is not an integer, a free-text or pairwise value that is not a string, a pairwise value not among the options, `clip_ids` not 1 or 2 long) | No file; `board_unreadable`, exit `1` |
| Run again on the same state | Identical bytes; the old file is replaced |
| Another Test is dispatching (`board.lock` held) | Export succeeds |

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
| `push_failed` | `push clip`, `push test` | The file system or SQLite failed while storing the Clip or Test; nothing was stored. For `push clip` the message names no source path. |
| `board_busy` | any writing command; any read-only command (`open --dry-run`, `status`, `export`, `personas generate`) | Another process holds the `board.db` lock past the busy timeout. Try again. |
| `board_version_mismatch` | any command that opens `board.db` | `board.db` has a newer layout version (`PRAGMA user_version`) than this `consortium` knows. Any read-only command (one that never migrates: `open --dry-run`, `status`, `export`) also raises it for an older version. |
| `board_unreadable` | any read-only command (`open --dry-run`, `status`, `export`, `personas generate`), `open` | `board.db` is corrupt or SQLite cannot open or read it, or a ledger amount is not a decimal; for `export` also a missing attempt row or a stored answer or `clip_ids` of the wrong type or length. |
| `board_wal_unavailable` | any command that opens `board.db` | SQLite could not put `board.db` in WAL mode (for example on some network file systems). |
| `panel_exists` | `personas generate` | `panel/personas/` already holds files; pass `--force` to replace them. |
| `panel_in_use` | `personas generate` | `board.db` already holds Trials of Panel Personas (of any Test or fidelity screening run; Trials of perception screening's neutral Persona `p0` do not count), which reference the current Panel's Personas; regenerating (even with `--force`) would re-label them. Start a new Study folder to change the Panel. |
| `personas_failed` | `personas generate` | The file system failed while writing the Panel; no partial Panel is left. |
| `panel_missing` | any command that needs Personas (`open`, `export`) | `panel/personas/index.json` does not exist; run `consortium personas generate`. |
| `panel_invalid` | any command that needs Personas (`open`, `export`) | `panel/personas/index.json` cannot be read as a non-empty list of Personas (all five traits with `high`/`low`, `nars` `low`/`high` or `null` for every Persona or `null` for none, ids exactly `p1 ... pN` in order), or a `p<n>.md` card is missing (or, for `export`, unreadable). |
| `bad_test_name` | `push test` | The Test name does not match `^[a-z0-9]([a-z0-9_-]*[a-z0-9])?$`, is longer than 64 characters, ends in `-attrition`, or is a reserved screening run ID `s<n>`. |
| `instrument_not_allowed` | `push test`, `open` | A Test lists a self-report screening Instrument (`self_report: true`, such as `fidelity_bfi10`); those are used only by `screen personas`. |
| `screening_run_open` | `screen personas`, `screen models` | A fidelity screening run (or a perception run of the same screening Test) is not complete (paused or stopped); continue it with `--resume` (fidelity: or drop it with `--abandon`). Nothing was written. |
| `screening_not_open` | `screen personas --resume`, `--abandon`, `screen models --resume`, `--abandon` | No fidelity screening run (or no perception run of the screening Test) is open. |
| `screening_test_not_openable` | `open` | The name is a `kind: screening` Test (run it with `screen models`) or a screening run `s<n>` (run and resumed by `screen personas` or `screen models`). |
| `not_a_screening_test` | `screen models` | The Test is not a registered `kind: screening` Test (a pilot or main Test, or a screening run `s<n>`). Nothing was written. |
| `no_screening_checks` | `screen models` | The screening Test declares no `checks`. Nothing was written. |
| `draft_instrument_not_allowed` | `screen personas` | A fidelity Instrument in use is `draft: true` (the placeholder `fidelity_nars`) and a Model is not `provider: fake`; select a filled-in NARS Instrument (see [NARS Instrument](#nars-instrument)). |
| `panel_frame_mismatch` | `screen personas` | `study.yaml` `personas.nars_bands` and the Panel disagree on whether Personas have a NARS band. |
| `bad_option` | `screen personas`, `screen models` | `--resume`, `--dry-run` and `--abandon` (`screen models`: `--resume` and `--abandon`) were combined. |
| `test_changed` | `push test`, `open`, `screen personas` | The Test file changed while it was being validated, or (for a Run or `--resume`) the Test file, its requests, its non-terminal Trials or its providers changed between the confirmation and the lease; nothing was registered, planned or stored. |
| `test_exists` | `push test`, `open` | A Test of that name is registered with different bytes, its registered `tests/<name>.yaml` is missing or was edited, or `tests/<name>.yaml` already exists unregistered with different bytes. |
| `unknown_clip` | `push test`, `open` | A target Clip ID is not in `board.db` (field `clips[i]`), or a Clip a registered Test uses is missing when its Trials are rendered. |
| `bad_pairing` | `push test`, `open` | The pairing plan cannot be built: no target Clips, a pairwise Instrument with fewer than 2 targets, duplicate target Clip IDs, or a `session.pairing` other than `all_pairs`. |
| `bad_practice` | `push test`, `open` | A Practice Clip is not pushed or is also a target, a pairwise example lists the same Clip twice, or an Instrument has fewer than `session.practice_clips` Practice examples. |
| `clip_kind_overlap` | `push test` | A target Clip is already a target of a registered Test of the other side (`main` vs `pilot`/`screening`). |
| `media_limit_exceeded` | `push test`, `open` | A worst-case Trial exceeds a Model's `limits.max_seconds` or `limits.max_bytes`. |
| `unknown_test` | `open`, `status`, `export` | The Test is not registered (or there is no `board.db` yet). |
| `protocol_lock_unavailable` | `open` | The Test is `kind: main` (main Tests open only once the Protocol lock exists, Epic 4), or is otherwise registered as not openable. |
| `invalid_response` | `open`, `open --resume` (recorded, not printed) | A Model's raw answer failed the Instrument's response schema; the reason (`not_json`, `missing_item`, `out_of_range:<item>`, ...) is stored as the attempt's `invalid_reason` and the Trial is retried or becomes `invalid` (see [Response validation and retries](#response-validation-and-retries)). Never an exit code. |
| `invalid_ceiling` | `open` | `--ceiling` is not a decimal USD amount greater than 0; nothing changed. |
| `ceiling_required` | `open`, `screen personas` | No cost ceiling has ever been set and none was given, and a Model the open sends to is not `provider: fake` priced `0`; nothing was sent. |
| `over_ceiling` | `open`, `screen personas` | Committed spend plus the Run's expected cost exceeds the ceiling (`[committed C + ]expected X > ceiling Y`), or `--ceiling` is below committed spend (`committed C > ceiling Y`); nothing was sent or logged. |
| `ceiling_overshoot` | `open` (stderr warning) | An attempt's actual cost exceeded its estimate and pushed committed spend over the ceiling; the Run paused. |
| `ceiling_reached` | `open`, `open --resume`, `screen personas` | The Run paused because the next attempt's reservation would cross the ceiling; in-flight attempts were collected. Continue with `--resume --ceiling <higher>`. |
| `unknown_prompt_variant` | `open` | A Trial's Prompt variant is not defined by its Instrument (an internal consistency check). |
| `provider_unavailable` | `open` | A Model of the Test uses a provider with no adapter (every provider has one in this version: `fake`, `gemini`, `qwen`). |
| `api_key_missing` | `open`, `open --resume`, `screen personas` | A Model's API key env var (`GEMINI_API_KEY`, `DASHSCOPE_API_KEY` or `settings.api_key_env`) is unset or empty: `<model id>: set <ENV>`. Raised before confirmation; nothing was sent or uploaded. Never for a dry run. |
| `prepare_failed` | `open`, `open --resume`, `screen personas` | A Clip could not be made available to a provider (the local Clip file is unreadable; a Gemini lookup, upload or poll failed, after 3 retries for a transient error; the upload FAILED, did not match the Clip's SHA-256 or was not ACTIVE within 300 s). The Run stops; continue with `--resume`. |
| `study_busy` | `open`, `screen personas` | Another dispatching command holds the `board.lock` lease of this Study. |
| `test_already_open` | `open` | The Test already has Trials in `board.db`; it cannot be opened again (message ends `; use --resume to continue it`). |
| `not_confirmed` | `open`, `screen personas` | The Run was declined at the confirmation prompt; nothing was stored or sent. |
| `confirmation_required` | `open`, `screen personas` | No `--yes` and stdin is not a terminal, so the Run cannot be confirmed; nothing was stored or sent. |
| `unknown_model` | `open --resume` | A Model of the Test's non-terminal Trials is no longer in `study.yaml`. |
| `archive_corrupt` | `open --resume` | An Archive line is valid JSON but not a record with a string `trial_id` and an integer `attempt`; the message names `<file>:<line>`. |
| `test_not_open` | `open --resume` | The Test has no Trials in `board.db`; open it without `--resume` first. |
| `reissue_mismatch` | `open --resume` | An archived request no longer re-renders byte-identically from its Trial row and the Study folder (a Study input was edited after open), or its `request_sha256`, seed or Model does not match; nothing was sent. |
| `run_failed` | `open` | The Run stopped on an unexpected error (`<type>: <message>`); see the resume contract under [Run](#run). |
| `adapter_error` | `open` | A Rater broke the port contract (for example returned the wrong number of results, or a category other than `ok`, `transient`, `refused`, `fatal`), or `--resume` found a stored handle that is not a JSON object. The Run stopped. |
| `bad_concurrency` | `open` | The engine was given a concurrency below 1 (an internal check; `study.yaml` already requires at least 1). |
| `bad_max_retries` | `open` | The engine was given `max_retries` below 0 (an internal check; the config already requires at least 0). |
| `screening_not_exportable` | `export` | The Test is a `kind: screening` Test or a screening run `s<n>`; screening Trials never appear in an Export. Nothing was written. |
| `sessions_running` | `export` | A Trial of the Test is still `planned` or `sent` (`<n> Trials not terminal`); finish or resume the Run first. Nothing was written. |
| `nothing_to_export` | `export` | The Test is registered but has no Trials (never opened). |
| `blinding_key_missing` | `export` | `blinding_key.csv` exists but cannot be read or parsed. (A Clip with no row, or no key file at all, is not an error: its Condition cells are empty.) |
| `instrument_changed` | `export` | A valid Trial's stored answers do not have exactly the Items of its Instrument as now configured (Items were added or removed after the Run); the message names the Instrument. |
| `condition_name_clash` | `export` | A Condition factor would produce a column that equals another export column (for example a factor named `model`); the message is the factor. |
| `panel_mismatch` | `export` | A stored Persona card `panel/personas/p<n>.md` differs from the card regenerated from `index.json` and the built-in wording, or a Trial's Persona is not in the Panel. |
| `unknown_instrument` | any command that loads config (including `export`) | An Instrument name in `study.yaml` (including `screening.nars_instrument`) or a Test does not resolve, or a Test lists an Instrument not enabled in `study.yaml`; for `export`, an Instrument of the Test's Trials is no longer loaded. |

## Study folder layout

```text
<study>/
  study.yaml           Study configuration: seed, Models (by id), defaults.        (init; schema in story 1.2)
  protocol.md          Study protocol.                                              (init; full template arrives in story 4.1)
  prices.yaml          Per-Model prices in USD.                                     (init; schema in story 1.2)
  tests/*.yaml         Test definitions; init writes a pilot Test, example.yaml.    (init; schema in story 1.2; push test)
  instruments/*.yaml   Optional user Instruments.                                   (story 1.2)
  panel/personas/      Persona cards (p<n>.md), index.json and meta.json.            (personas generate)
  panel/               Screening snapshot.                                          (arrives in story 4.1)
  clips/<clip_id>.mp4  Canonicalized, metadata-free Clips.                          (push clip)
  blinding_key.csv     The only place Conditions exist.                             (push clip)
  board.db             All mutable Study state (SQLite).                            (push clip)
  board.lock           Exclusive lease held by a dispatching command.               (open)
  archive/requests.jsonl   Append-only rendered requests.                           (open)
  archive/responses.jsonl  Append-only raw responses.                               (open)
  exports/             <test>.csv and <test>-attrition.csv from export; leak-report.csv from push clip.      (export, push clip)
  protocol.lock        Hashes of every file that affects the data.                  (arrives in story 4.1)
```

All state lives in the Study folder. Paths stored inside it are relative to it.

### `panel/personas/`

Written only by `personas generate`; never edited in place once used.

- **`p<n>.md`** — the Persona card the Model sees. UTF-8, LF line endings, one trailing newline; no timestamps, paths, versions, Persona ID or labels. It describes behaviour only: it never contains a trait name, `high`, `low` or `NARS`. Seven lines (six without a NARS band), in this order: the demographic line; the openness, conscientiousness, extraversion, agreeableness and neuroticism sentences for the Persona's poles; the NARS-band sentence (six lines, no NARS sentence, when `personas.nars_bands` is `[]`). For example (`p1` of the template Study):

  ```text
  You are a man, aged 18 to 29, from the Middle East or North Africa, with regular experience of robots.
  You prefer familiar things and practical, well-tried ways of doing them.
  You take things as they come and do not worry much about plans or details.
  You are quiet and reserved, and you prefer calm settings or small groups.
  You say what you think plainly, and you trust others once they have shown they are reliable.
  You stay calm under pressure and rarely worry for long.
  You feel comfortable around robots and would be at ease interacting with one.
  ```

- **`index.json`** — the labels, for screening and export (the export's `persona_*` columns). Canonical JSON (sorted keys, UTF-8, no whitespace, no trailing newline): a list of Persona objects in ID order, each `{"age_band", "big_five": {"agreeableness", "conscientiousness", "extraversion", "neuroticism", "openness"}, "cultural_region", "gender", "id", "nars", "robot_experience"}`, where every `big_five` value is `"high"` or `"low"` and `nars` is `"low"`, `"high"` or `null` (no NARS band; the export's `persona_nars` is then empty).
- **`meta.json`** — provenance, canonical JSON: `{"design", "frame_sha256", "generator_version", "seed", "wording_sha256"}`. `design` is `{"aliasing", "defining_relation", "fraction", "generators", "profiles", "replicates", "resolution"}`: `fraction` (`"1"`, `"1/2"`, `"1/4"`), `replicates`, `profiles` (the number of distinct profiles: 32, 16, 8), `resolution` (`"V"`, `"III"`, `null` for the full grid), `generators` (for example `["A=OC", "N=OE"]`; `[]` for the full grid), `defining_relation` (for example `"I=OCA=OEN=CEAN"`; `null` for the full grid) and `aliasing` (one `"="`-joined chain per alias class other than I, every effect in trait letters O C E A N, lowest order first, for example `"O=CA=EN=OCEAN"`; `[]` for the full grid). `seed` is `study.yaml` `seed`; `frame_sha256` is the SHA-256 of the canonical JSON of `study.yaml` `personas` (with defaults filled in); `wording_sha256` is the SHA-256 of the packaged `wording.yaml` bytes; `generator_version` (currently `"1"`) changes whenever the generation or card algorithm changes. Story 2.4 kept `"1"`: for the full grid once, the cards and `index.json` are byte-identical to before, and a `meta.json` without a `design` key (written before Story 2.4) means that full grid. It is not yet checked against the Study.

### `clips/<clip_id>.mp4`

One canonical MP4 per Clip, written by `push clip`. The file name is the Clip ID; the file holds no user metadata. Temporary files named `clips/.push-*.mp4` exist only while a push runs.

### `board.db`

SQLite in WAL mode; the only mutable Study state, created by the first `push clip` or successful `push test`. Its layout version is `PRAGMA user_version` (currently `6`; older files are migrated forward when opened). Table `clips` (version 1), one row per Clip:

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

Table `tests` (version 2), one row per registered Test:

| Column | Meaning |
| --- | --- |
| `name` | Primary key, the Test's `test:` name. |
| `kind` | `pilot`, `screening` or `main`. |
| `path` | The registered file, `tests/<name>.yaml`. |
| `sha256` | SHA-256 (lowercase hex) of the file bytes. |
| `openable` | `1`, or `0` for `kind: main` (not openable until the Protocol lock, Epic 4). |
| `registered_at` | UTC ISO 8601 time with `Z`. |

Column `paused_reason` (version 4) on `tests`: why the Test's Run is paused (`ceiling`), or empty; cleared by `open --resume`.

Table `test_clips` (version 2), one row per Test and Clip it uses: `test`, `clip_id`, `role` (`target` or `practice`; every Practice Clip listed in `practice:` is recorded, used or not).

Table `trials` (version 3), one row per planned Trial, written by `open`: every Trial field (`trial_id` primary key, `test`, `session_id`, `trial_index`, `instrument`, `clip_ids` as a canonical JSON list in presentation order, `pair_id`, `position`, `prompt_variant`, `order_seed`, `repeat`, `agent_id`, `persona_id`, `model_id`), plus `state` (see [Trial states](#run)), `attempt` (`0` until first dispatched, then the latest attempt number) and `seq` (plan order).

Table `attempts` (version 3), one row per `(trial_id, attempt)` (primary key): `seed` (the attempt's derived Model seed), `handle` (the Rater's handle as canonical JSON, once submitted), `sent_at`, `answered_at` (UTC ISO 8601 with milliseconds and `Z`) and `category` (the Rater's result category: `ok`, `transient`, `refused` or `fatal`; or `attempts_exhausted`, see below). Indexed by `trial_id`. Columns added in version 5: `valid` (`1` or `0` once the answer was validated, empty before and for a category other than `ok`), `invalid_reason` (the `invalid_response` reason of an invalid attempt) and `answer_json` (a valid attempt's parsed answer `{item_id: value}` as canonical JSON). An abandoned last allowed attempt (never answered) that ended its Trial `failed` on resume has category `attempts_exhausted`.

Table `ledger` (version 4), one row per `(trial_id, attempt)` (primary key) that was reserved: `model_id`, `reserved_usd` (the attempt's estimated cost, written in the same transaction as its `attempts` row) and `actual_usd` (from the returned usage; empty until known, or when the answer had no usage). USD as decimal strings.

Table `ceiling_changes` (version 4), one row per `open --ceiling`: `ts` (UTC ISO 8601 with milliseconds and `Z`), `previous_usd` (empty for the first), `ceiling_usd`, `test` (the Test opened) and `command` (`run` or `resume`). The latest row (insertion order) is the current, Study-wide ceiling.

Table `screening_runs` (version 6, story 3.1), one row per screening run: `run_id` (primary key, `s<n>`), `kind` (`fidelity` or `perception`), `screening_test` (the screening Test of a perception run; empty for fidelity), `started_at` (UTC ISO 8601 with milliseconds and `Z`), `status` (`open` until scored, then `complete`; `abandoned` by `--abandon`, never scored), `superseded_by` (the newer complete run once every result key of this run is covered by later complete runs; empty otherwise), `settings_hashes` (canonical JSON `{model_id: settings_hash}`) and `instrument_hash` (the stamps recorded when the run was created). A run's Trials belong to the `tests` row named `s<n>` (kind `screening`, not openable).

Table `screening_results` (version 6), one row per result, only ever inserted: `run_id`, `model_id`, `agent_id` (the Agent of a fidelity row), `instrument` (`fidelity` for fidelity rows), `outcome` (`pass` or `fail`), `score` (the match ratio), `threshold` (`thresholds.persona_fidelity_min` when scored), `settings_hash`, `instrument_hash` (see [`screen personas`](#consortium-screen-personas---yes---ceiling-usd---resume---dry-run---abandon---study-path)), `detail` (canonical JSON: per construct its `score`, `midpoint`, `pole`, `match`, `n`, `expected` and `insufficient_data`, plus NARS `subscales`; `matched`, `total`, `insufficient_data`, `draft`), `pair_checks` (perception, story 3.2), `source_study` and `source_hash` (a copied Panel, story 3.4). At most one row per `(run_id, instrument, model_id, agent_id)` (unique). The current result of each `(instrument, model_id, agent_id)` is the one from the highest-numbered complete run that has it.

`board.db` never holds a Condition, the source file name or a hash of the source file.

Read-only commands (`open --dry-run`, `status`, `export`) open it with SQLite `mode=ro` and never migrate it or write Study data; when no `board.db-wal` exists they add `immutable=1`, so no `board.db-wal`/`board.db-shm` side files are created (reads are redone without it if a writer starts meanwhile). A stale `board.db-wal` left by a crashed writer can make SQLite create `board.db-shm`, which holds no Study data.

### `board.lock`

An empty file; `open`, `open --resume`, `screen personas` and `screen models` (dispatching commands) hold an exclusive `fcntl.flock` on it for the whole command. A second dispatcher refuses with `study_busy`. The lock is released by the OS when the holding process ends, so a leftover file never blocks.

### `archive/requests.jsonl`, `archive/responses.jsonl`

Append-only; never rewritten. One canonical JSON object per line (sorted keys, UTF-8, no whitespace, LF), keyed by `trial_id` + `attempt`; each line is flushed and fsynced before the Run moves on. A request line is written before its Trial is marked `sent`; a response line before the Trial changes state. The `archive/` directory is fsynced when it or a file in it is first created. A crash during an append can leave an unterminated final line: readers ignore it, and the next append first ends it with a newline so it never merges into a new record (readers also skip and count such a fragment; a valid JSON line that is not a keyed record is `archive_corrupt`). A key can appear on more than one line (a response collected again by `--resume`, see [Resume](#resume)); every reader takes the **last line per key**. Timestamps (`ts`, and `sent_at`/`answered_at` in `board.db`) are UTC ISO 8601 with milliseconds and `Z`.

- **Request:** `{"attempt", "model_id", "request", "request_sha256", "seed", "trial_id", "ts"}`. `request` is the rendered request object (see [Trial requests](#trial-requests)); `request_sha256` is the SHA-256 of its canonical JSON; `seed` the attempt's Model seed; `ts` UTC ISO 8601 with milliseconds and `Z`.
- **Response:** `{"attempt", "category", "model_build", "raw", "request_sha256", "trial_id", "ts", "usage"}`. `request_sha256` repeats that of the attempt's request line; `raw` is the Model's raw text, `usage` `{"input_tokens", "output_tokens"}`, `model_build` the provider-reported build (or `null`), `category` `ok` or a snake_case reason. A real adapter's line also has `settings`, the settings it sent as `{name: {"value", "documented"}}`; `documented` is `true` when the provider documents the setting as honoured for this model, `false` when it was sent but is not documented as honoured, `null` when not assessed (see [Run](#run)). A Fake line has none. `usage` may hold extra keys (`text_tokens`, `video_tokens`, `audio_tokens`, `image_tokens`, `thoughts_tokens`, `reasoning_tokens`), which cost ignores, or be `{}`.

Media appear only as Clip ID + SHA-256. The Archive never holds a Condition, a source file name or a provider file handle.

### `blinding_key.csv`

The only place Conditions exist. Long CSV with header `clip_id,factor,level`, one row per Clip and factor, appended (and fsynced) by `push clip`. A Clip pushed with no Condition has no rows. The file is created by the first push that has a Condition. Only the push and export stages read it.

### `exports/<test>.csv`

Written only by `consortium export` (see [the command](#consortium-export-test---study-path) for every column). Replaced whole on each export; never mixes Tests.

### `exports/<test>-attrition.csv`

Written by `consortium export` next to `exports/<test>.csv`: Trial counts by outcome per Model, Persona attribute, Condition and Instrument (see [Attrition](#attrition)). Test names ending in `-attrition` are refused (`bad_test_name`), so a sidecar never collides with another Test's CSV.

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
| `models[].settings.seed_supported` | Not for `fake`. `true` (default) sends each attempt's seed; `false` omits it. |
| `models[].settings.fps` | Optional, finite, > 0, `gemini` only: frames sampled per second of each Clip (sent as `video_metadata.fps`). |
| `models[].settings.media_resolution` | Optional, `low`, `medium` or `high`, `gemini` only. |
| `models[].settings.thinking_level` | Optional, `minimal`, `low`, `medium` or `high`, `gemini` only; sent as `thinking_config` and archived. The template's commented gemini example sets `low` (and shows its matching `prices.yaml` entry). |
| `models[].settings.base_url` | `qwen` only, and *required* for `qwen` (no default: the Model Studio endpoint is workspace-specific): the OpenAI-compatible endpoint, an `http(s)` URL with a valid host name and port. Hosted Model Studio URLs end in `/compatible-mode/v1`, e.g. `https://<WorkspaceId>.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1`; vLLM example: `http://gpu:8000/v1`. |
| `models[].settings.reasoning_effort` | Optional, a non-empty string, `qwen` only; unset by default. Sent verbatim as `reasoning_effort` and archived only when set. |
| `models[].settings.api_key_env` | Optional env var name holding the key; default `GEMINI_API_KEY` for `gemini`, `DASHSCOPE_API_KEY` for `qwen`; not allowed for `fake`. Keys are only ever read from the environment. |
| | A provider-only setting on another provider is `config_invalid`. |
| `models[].max_output_tokens` | *Required.* Integer > 0; template `512`. |
| `models[].fake` | Fake rater settings, only for `provider: fake` (on another provider: `config_invalid`, field `models.<i>`): `input_tokens` and `output_tokens` (integers >= 0, template and default `0`), the usage it reports for every answer; `invalid_rate` (a number from `0` to `1`, template and default `0`), the share of attempts it answers invalidly, decided per attempt seed (see [Run](#run)); `transient_rate`, `refusal_rate` and `fatal_rate` (each a number from `0` to `1`, template and default `0`), the share of attempts that simulate a transient error, a refusal or a fatal error, each decided per attempt seed from its own stream (see [Run](#run)); `fidelity` (`random`, `faithful` or `unfaithful`, template and default `random`), how it answers keyed self-report Items in fidelity screening (see [`screen personas`](#consortium-screen-personas---yes---ceiling-usd---resume---dry-run---abandon---study-path)); `perception` (`random`, `faithful` or `unfaithful`, template and default `random`), how it answers Items about Clips in perception screening requests only (the neutral `p0` card; pilot and main Runs are answered as with `random`; see [`screen models`](#consortium-screen-models-test---yes---ceiling-usd---resume---abandon---study-path)). |
| `models[].limits` | *Required.* What the Model accepts per request: `max_seconds` (> 0, template `600`), `max_bytes` (integer > 0, template `20000000`), `inline_base64` (template `true`; media is sent base64-inline, which counts 4/3 of the file size). Hosted `qwen` (a `base_url` host ending in `aliyuncs.com`): `max_bytes` at most `9900000` and `inline_base64: true`, since the base64 string must be under 10 MB (else `config_invalid`). |
| `media` | Canonical Clip encoding: `height` 480 (must be even), `video_kbps` 400, `audio_kbps` 64, `fps` 25. |
| `session.practice_clips` | Practice examples included per Trial per Instrument (>= 0); template `2`. |
| `session.repeats` | Repeats per Agent (>= 1); template `3`. |
| `session.max_retries` | Retries after an invalid answer (>= 0); template and default `2`. Counted separately from transient retries (see [Response validation and retries](#response-validation-and-retries)). |
| `session.retry` | Retries after a `transient` result (see [Provider failures](#provider-failures)); `study.yaml` only, no per-Test override. `transient_retries` (integer >= 0, template and default `3`), `backoff_initial_s` (finite, >= 0, template and default `2`), `backoff_max_s` (finite, >= `backoff_initial_s`, template and default `60`; else `config_invalid`, field `session.retry`: `backoff_max_s must be at least backoff_initial_s`). A Trial gets at most `1 + max_retries + transient_retries` attempts. |
| `session.pairing` | Pairing rule for pairwise Instruments; only `all_pairs` is accepted. |
| `concurrency` | Max in-flight calls per provider (>= 1); template `4`. |
| `thresholds` | *Required*, every key below except `perception_min`, no code defaults: `persona_fidelity_min` (0-1, template `0.8`), `invalid_rate_max` (0-1, template `0.05`), `leak_tolerance.duration_s` (template `1.0`), `leak_tolerance.loudness_lufs` (template `2.0`); `perception_min` (0-1, default and template `0.8`, story 3.2: a Model passes an Instrument in `screen models` when its pass ratio is at least this). Resolution and fps must match exactly. |
| `personas.big_five` | `{fraction, replicates}` (see [Panel design](#panel-design)). `fraction`: `1`, `0.5` or `0.25`, or the strings `"1"`, `"1/2"`, `"1/4"` (stored as the string form; default and template `1`). Numerically equal values are accepted too (`1.0`, `0.50`); other strings (for example `"0.5"`, `"1/2 "`, `"½"`) are not. `replicates`: `1`, `2` or `3` (default and template `1`). The legacy value `all_32` is accepted and means `{fraction: 1, replicates: 1}` (its `frame_sha256` then changes, since the frame is written as the mapping). Anything else is `config_invalid`. |
| `personas.nars_bands` | Unique subset of `low`, `high`; template and default (when absent) `[low, high]`. `[]` means no NARS band: N = profiles x replicates, 6-line cards, `nars: null`, an empty `persona_nars` export column, and no NARS fidelity check. |
| `screening.fidelity_repeats` | Sessions per Agent in a fidelity screening run (integer >= 1); template and default `1`. |
| `screening.nars_instrument` | The NARS self-report Instrument for fidelity screening: an Instrument name, resolved like any Instrument (user folder first) but not gated by `instruments`; template and default `fidelity_nars` (the placeholder). See [NARS Instrument](#nars-instrument). |
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

`study.yaml` `seed` is the only seed. Every other seed is derived as `int(sha256("<seed>:<purpose>:<key>").hexdigest()[:8], 16) & 0x7FFFFFFF` (the first 8 hex digits of the SHA-256, masked to 31 bits) and used only through Python's `random.Random(seed)`. Persona quotas use purpose `personas` with the attribute name as key (for example `1:personas:gender`). Trial order uses purpose `order` with the Session ID as key (for example `1:order:pilot1/p12-m1/r2`). Each attempt of a Trial uses purpose `model` with key `<session_id>:<trial_index>:<attempt>` (for example `1:model:pilot1/p12-m1/r2:7:1`); the Fake rater answers from it.

### `tests/<name>.yaml`

| Field | Meaning |
| --- | --- |
| `schema_version` | *Required.* `1`. |
| `test` | *Required.* The Test name; must equal the file name without `.yaml`. The schema accepts letters, digits, `_` and `-`; `push test` requires `^[a-z0-9]([a-z0-9_-]*[a-z0-9])?$`, at most 64 characters (lowercase, no leading or trailing `_`/`-`; `bad_test_name`), since the name is part of every Session ID. |
| `kind` | *Required.* `pilot`, `screening` or `main`. |
| `instruments` | *Required.* Non-empty list of Instrument names; each must resolve and be enabled in `study.yaml` `instruments`. |
| `models` | Optional list of Model ids from `study.yaml`; omitted means every Model. |
| `clips` | Target Clip IDs, each `c_` plus 8 lowercase base32 characters, unique (default `[]`; existence is checked by `push test`). |
| `practice` | Practice examples (default `[]`): each `{instrument, clips, answer}`. `instrument` must be one of the Test's `instruments` (else `unknown_instrument`, field `practice.<i>.instrument`); `clips` is exactly 1 Clip ID, or 2 for a pairwise Instrument; `answer` maps Item id to the intended value and must pass the Instrument's response schema (else `config_invalid`, field `practice.<i>.clips` or `practice.<i>.answer.<item>`). Each Instrument needs at least `session.practice_clips` examples; the first ones in list order are used (count and Clip existence checked by `push test`, `bad_practice`). Practice clips may be shared between pilot and main Tests. |
| `session` | Optional overrides of these `study.yaml` `session` keys: `practice_clips`, `repeats`, `max_retries`, `pairing` (not `retry`). |
| `checks` | Perception checks (story 3.2), allowed only in a `kind: screening` Test (else `config_invalid`, field `checks`); default `[]`. Each is `{instrument, item, clips, expected}`: `instrument` one of the Test's `instruments`, `item` one of its Item ids, `clips` 1-2 distinct Clip IDs among the Test's `clips`. A **pair** check is a pairwise Instrument's check (exactly 2 Clips, a pairwise Item) or a single-Clip Instrument's check given 2 Clips (its Item must be Likert with at least 3 points, since a 2-point Item ties too often): `expected` is the Clip ID that should win. A **low-level** check has 1 Clip and a Likert Item (not `free_text`): `expected` is a valid answer to the Item (for `perception_cues`, `2` = Yes). The same instrument, Item and Clip set may appear in one check only: a repeat is refused as a duplicate, a different `expected` as a contradiction. Anything else is `config_invalid`, field `checks.<i>...`. See [`screen models`](#consortium-screen-models-test---yes---ceiling-usd---resume---abandon---study-path). |

Prompt variants are not chosen in the Test; they rotate by Repeat (see [Sessions and Trials](#sessions-and-trials)).

### `prices.yaml`

| Field | Meaning |
| --- | --- |
| `schema_version` | *Required.* `1`. |
| `models.<model id>` | Per Model id: `input_usd_per_mtok` and `output_usd_per_mtok`, USD per million tokens, as quoted non-negative decimal strings (for example `"0.30"`; an unquoted `0.30` is refused). Every Model id in `study.yaml` must have an entry, and no other ids may appear (`config_invalid`, field `models.<id>`). The template prices the fake Model `m1` at `"0"`. |
| `models.<model id>.media_tokens_per_s` | Input tokens per second of Clip media, a quoted non-negative decimal string; default and template `"300"`. For `gemini` derive it as per-frame tokens(`media_resolution`) × `fps` + 32 (audio tokens per second of Clip): a frame costs 66 tokens at `low`, 258 at `medium`, 258 at `high` and 258 when `media_resolution` is unset; `fps` is 1 when unset. So `low` at 1 fps is `"98"`, unset at 1 fps `"290"`, `low` at 2 fps `"164"`. These per-frame figures are Google's published ones as of 2026-10-02; check them for the pinned model. `qwen` bills video and audio tokens separately: set `media_tokens_per_s` from Model Studio's billing docs for the pinned model (video plus audio tokens per second of Clip), then check it against the first pilot's archived `usage.video_tokens` + `usage.audio_tokens`. |
| `models.<model id>.chars_per_token` | Characters of rendered request text per input token, a quoted decimal string greater than 0 (`"0"` is refused); default and template `"4"`. |

These fields feed the one cost formula (see [Cost and ceiling](#cost-and-ceiling)).

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
| `self_report` | `true` or `false` (default `false`). A self-report Instrument is a clip-less questionnaire about the Persona itself, answered only in fidelity screening (one Trial per Session, no Clip, no Practice); a Test that lists it is refused (`instrument_not_allowed`). Every Item must be keyed. |
| `citation` | Optional: the published source of the Items. |
| `keys` | Optional scoring keys by Item id (`{construct, reversed, subscale}`), for Likert Items only; never shown to a Model. `construct` is a Big Five trait or `nars`; `reversed` is `true` or `false`; `subscale` (`s1`, `s2`, `s3`) is required for `nars` and not allowed otherwise. |
| `items[]` | *Required*, non-empty, unique `id`s. Each Item has `id`, `type` and `text`. `likert` Items also need `points` (2-11) and `anchors: {low, high}`; `pairwise` Items take `options` (two distinct labels, default `[A, B]`); `free_text` Items take nothing else. An Instrument is either all pairwise or has no pairwise Items. |

Each Instrument has a response schema (a JSON Schema): an object with exactly one key per Item id; Likert values are integers from 1 to `points`, pairwise values are one of `options`, free text is a non-empty string.

**Built-in Instruments.**

| Name | Items | Draft |
| --- | --- | --- |
| `godspeed` | Godspeed animacy (6) and likeability (5), 5-point semantic differentials, ids `animacy_1`-`animacy_6`, `likeability_1`-`likeability_5`. | no |
| `pairwise_alive` | One pairwise Item `alive`: "Which one feels more alive?", options `A`, `B`. | no |
| `presence` | One 7-point Item `presence_1`. A placeholder until a published presence scale is chosen. | **yes** |
| `perception_cues` | Low-level perception checks for screening Tests (story 3.2): `moving` ("Is the robot moving?") and `speech` ("Is there speech?"), each a 2-point Likert Item with anchors `No` (1) and `Yes` (2). Enabled in the template's `study.yaml` `instruments`. | no |
| `fidelity_bfi10` | Self-report, screening only: BFI-10 (Rammstedt & John, 2007; open access via GESIS for non-commercial research), Items `bfi_1`-`bfi_10` completing "I see myself as someone who...", 5-point, keyed E 1R/6, A 2/7R, C 3R/8, N 4R/9, O 5R/10 (R = reversed). TIPI is the noted fallback. | no |
| `fidelity_nars` | Self-report, screening only: the NARS structure (`nars_1`-`nars_14`, subscales S1/S2/S3, S3 reversed) with placeholder text; supply the real items (see [NARS Instrument](#nars-instrument)). | **yes** |

## Unit of analysis

The tool produces data; it performs **no statistics**. When you analyse an Export:

- **Agents sharing a Model are not independent.** An Agent is a Persona run on a Model (`p<n>-m<n>`). All Agents on the same Model share its weights, so treat them as repeated measures of that Model, not as independent raters. Do not count Agents as if they were human participants.
- **Agent, Persona, Model and Clip are crossed factors.** Every Persona is run on every Model and rates the same Clips, so model them as crossed (for example, crossed random effects for Persona and Clip, with Model as a fixed or grouping factor), not as nested.
