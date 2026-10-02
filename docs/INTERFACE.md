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
- Registration stores the name, `kind`, the stored path, the SHA-256 of the file bytes and whether the Test is openable. A `kind: main` Test is registered **not openable**: `push test` logs `not_openable: Test <name> is kind main; not openable until Protocol lock (Epic 4)` to stderr, and `open` refuses it with `protocol_lock_unavailable` until the Protocol lock arrives (Epic 4). Pilot and screening Tests are openable.
- Re-pushing a Test with identical bytes is a no-op that prints the name again, provided the stored `tests/<name>.yaml` still holds the registered bytes (else `test_exists`: the registered file is missing or was edited). The same holds when an identical push by another process registers first. Different bytes under a registered name are refused (`test_exists`); a changed Test needs a new name.
- The file is read once; those bytes are validated, hashed and copied. If the file changes while it is being validated, the push is refused with `test_changed`.
- On success, prints the Test name to stdout and exits `0`.
- **Checks, in order; the first failure wins** and nothing is copied or registered:
  1. **Name.** `test:` matches `^[a-z0-9]([a-z0-9_-]*[a-z0-9])?$` (lowercase letters, digits, `_` and `-`, not starting or ending with `_` or `-`) and is at most 64 characters, since it is part of every Session ID — else `bad_test_name`. The pairing plan's syntax (`session.pairing`, duplicate Clip IDs) is also checked here, before the schema, as `bad_pairing`.
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
| Valid pilot or screening Test | Registered openable, file at `tests/<name>.yaml`, name printed, exit `0` |
| Valid `kind: main` Test | Registered not openable, `not_openable` note on stderr, name printed, exit `0` |
| Same name, identical bytes, already registered | No-op, name printed, exit `0` |
| Same name, different bytes | `test_exists`, exit `1` |
| `test:` not matching `^[a-z0-9]([a-z0-9_-]*[a-z0-9])?$` or longer than 64 characters (for example `Pilot/1`, `pilot-`) | `bad_test_name`, exit `1` |
| A target Clip ID that was never pushed | `unknown_clip: clips[i]: ...`, exit `1` |
| An unknown Instrument | `unknown_instrument`, exit `1` |
| No target Clips, a pairwise Instrument with fewer than 2 targets, duplicate Clip IDs, or `session.pairing` other than `all_pairs` | `bad_pairing`, exit `1` |
| Same name, identical bytes, but the stored `tests/<name>.yaml` is missing or edited | `test_exists`, exit `1` |
| The file changed while being validated | `test_changed`, exit `1` |
| A Clip both in `practice` and `clips`; a Practice Clip never pushed; too few Practice examples | `bad_practice`, exit `1` |
| A Practice answer failing the Instrument's response schema, or the wrong number of Practice Clips | `config_invalid: practice.<i>...` (from the Test schema), exit `1` |
| Target shared between a `main` and a `pilot`/`screening` Test | `clip_kind_overlap`, exit `1` |
| Worst-case Trial above a Model's `max_seconds` or `max_bytes` | `media_limit_exceeded`, exit `1` |
| The file system or `board.db` fails (reading, checking or storing) | `push_failed`, exit `1`, nothing stored |

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

### `consortium open TEST [--dry-run] [--yes] [--ceiling USD] [--resume] [--study PATH]`

Opens the registered Test `TEST`. With `--dry-run`, before anything is sent or spent, it prints the counts of what a Run would send plus a digest of every request, and writes nothing. Without `--dry-run` it **runs** the Test (see [Run](#run)): every Trial is sent once through its Model's Rater. In this version only the `fake` provider has an adapter. `--ceiling USD` sets the Study's cost ceiling (a decimal amount greater than 0, for example `5.00`, else `invalid_ceiling`; see [Cost and ceiling](#cost-and-ceiling)). Without `--dry-run`, `--resume` continues a stopped Run (see [Resume](#resume)); with `--dry-run` it is ignored.

- `--study PATH` is the Study folder (default: the current directory); `study.yaml` must load.
- **Reads and checks, in order:** `--ceiling`; `study.yaml`; the Test's registration and its Clips' rows in `board.db` (opened read-only); a `kind: main` Test is refused here with `protocol_lock_unavailable`, before anything is planned (as is any other Test registered not openable: `Test '<name>' is registered as not openable`); the registered `tests/<name>.yaml` (its bytes must still match the registered SHA-256, else `test_exists`; if they change while it is validated, `test_changed`), validated as by `push test`'s schema step; then `push test`'s plan, Practice, Clip-reference and media-limit checks are run again against the current config (`bad_pairing`, `bad_practice`, `unknown_clip`, `media_limit_exceeded`, `unknown_instrument`), since `study.yaml` or an Instrument may have changed since the push; the Persona Panel (`panel/personas/index.json` and every `p<n>.md` card).
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
  cost estimate: expected <usd> USD, worst case <usd> USD (max_retries 2)
  cost covers: 3072 Trials; Clips go to: fake
  ceiling: none, committed before: 0 USD
  ```

  Models are listed in the Test's order, Instruments in the Test's order; `by type` counts single-Clip and pairwise Trials.

| Situation | Result |
| --- | --- |
| Registered pilot or screening Test, `--dry-run` | Counts and requests digest printed, exit `0`, Study folder unchanged |
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
3. Prints the summary lines to stdout, exactly as a dry run does (without `dry run`), including the cost estimate and the ceiling it will run under. Then applies the ceiling rules (see [Cost and ceiling](#cost-and-ceiling)): `ceiling_required` or `over_ceiling`, exit `1`, nothing written. Then refuses any Model whose provider has no adapter yet (`provider_unavailable`; only `fake` exists in this version). Nothing is written and no `board.lock` is created.
4. Asks on stderr, before taking the lease (default no):

   ```text
   requests sha256: <64 lowercase hex digits>
   Run N Trials on <providers>? [y/N]:
   ```

   With `--ceiling X` the question reads `Run N Trials on <providers>, ceiling X USD (study-wide)? [y/N]:` (and likewise for `Resume`).

   `--yes` skips the question. Answering anything but yes, or end of input / Ctrl-C at the prompt: `not_confirmed`; stdin not a terminal and no `--yes`: `confirmation_required`. Either way no Trial is stored, no Archive line, ledger row or ceiling change is written. Nothing reaches a Rater (not even a Clip upload, `prepare`) before this point.
5. Takes the exclusive lease on `board.lock` (`fcntl.flock`, held for the rest of the command; the OS releases it if the process dies). If another dispatching command holds it: `study_busy`, nothing written. Under the lease it re-checks that the Test has no Trials (`test_already_open`) and that the registered Test file's SHA-256, the requests digest, the cost estimate and the providers are unchanged since the confirmation (else `test_changed`), nothing written; the ceiling rules are applied again against the committed spend now in `board.db`.
6. Stores every planned Trial as `planned` (attempt `0`) in one transaction, logs `--ceiling` if given (see [Cost and ceiling](#cost-and-ceiling)), then sends every Trial through the engine.

Per attempt, in this order: (1) the attempt's estimated cost is reserved in the ledger, `attempt` is incremented and the attempt is recorded with its seed (the derived seed for purpose `model` and key `<session_id>:<trial_index>:<attempt>`, see [Seeds](#seeds)), all in one transaction, which is refused (nothing written) when the reservation would cross the ceiling (see [Cost and ceiling](#cost-and-ceiling)); (2) the attempt's Clips are prepared (uploaded) for the Rater, only after a successful reservation, and the request is appended to `archive/requests.jsonl`; (3) the Trial is marked `sent`; (4) the request is submitted to the Rater and its handle stored; (5) the answer is collected and appended to `archive/responses.jsonl`; (6) the attempt's actual cost, computed from the returned usage, is recorded in the ledger (when the answer has no usage the reservation stands; an attempt with no ledger row, recorded before the ledger existed, gets one reserving its estimate); (7) an answer with category `ok` is parsed and validated against the Instrument's response schema (see [Response validation and retries](#response-validation-and-retries)) and the attempt's `valid`, `invalid_reason` and parsed answer are recorded; (8) the Trial takes its state: a valid answer gives `valid`; an invalid one is retried (the Trial stays `sent` and gets a new attempt through steps 1 to 8) while `attempt <= max_retries`, else gives `invalid`; any other category gives `failed` (never validated, never retried). Each `(trial_id, attempt)` is dispatched at most once. Inside the process a single writer task performs every `board.db` write and Archive append, in order. Each Clip is prepared once per Rater; at most `concurrency` (from `study.yaml`) calls are in flight per provider. The provider's handle is stored even if the Run is stopped while it is being submitted. If an adapter (or anything else) fails, the Run stops: the other Trials are cancelled, and the error is reported as `code: message` (exit `1`): a `ConsortiumError` from the adapter unchanged, any other error as `run_failed: <type>: <message>`; a Rater that returns the wrong number of results is `adapter_error`.

**State after a stopped Run (resume contract).** Every Trial is in one of these states, and `--resume` handles each (see [Resume](#resume)):

- `planned` with `attempt` `0`: never dispatched; dispatch it.
- `planned` with `attempt >= 1`: stopped between recording the attempt and marking it `sent`. Its attempt row has no `sent_at`, and the request may already be archived. Re-dispatch it with a new attempt (the old attempt number is never reused).
- `sent` with a handle: submitted; collect it at the same attempt (unless that attempt is already validated, see below).
- `sent` whose latest attempt is already recorded valid: stopped before its state write; settle it `valid` from the board (no second collect).
- `sent` whose latest attempt is already recorded invalid (`valid = 0`): stopped between validating an invalid answer and its retry; re-dispatch it with a new attempt (its handle is not collected again).
- `sent` without a handle: stopped while submitting (the request is archived); re-dispatch it with a new attempt.
- terminal (`valid`, `invalid`, `refused`, `failed`): done; it always has its response line.

On success it adds the Trials by state and a cost footer (Study-wide committed spend and the ceiling, `none` for an uncapped Run) to the summary and exits `0`, even when some Trials ended `failed`; then it also prints `warning: N Trials did not end valid` to stderr:

```text
test: pilot1 (pilot)
...
requests sha256: <64 lowercase hex digits>
cost estimate: expected <usd> USD, worst case <usd> USD (max_retries 2)
cost covers: 3072 Trials; Clips go to: fake
ceiling: 5 USD, committed before: 0 USD
states: valid 3072
cost: committed <usd> USD, ceiling 5 USD
```

If the Run pauses at the ceiling, the summary ends with `paused: ceiling`, and the command exits `1` with `ceiling_reached: ...` on stderr (see [Cost and ceiling](#cost-and-ceiling)).

**Trial states.** `planned` (stored, not yet sent), `sent` (submitted; not necessarily answered), then one of the terminal states `valid`, `invalid`, `refused`, `failed`. Terminal states never change. This version produces `valid`, `invalid` and `failed`.

**Fake rater** (`provider: fake`). Deterministic, offline and free. It answers each Item from `random.Random(<attempt seed>)` in the request's Item order: a Likert Item `randint(1, points)`, a pairwise Item one of its options (a position, `A` or `B`), a free-text Item the fixed string `fake answer`. The raw answer is the canonical JSON `{item_id: value}`, which matches the Instrument's response schema. Usage is `{"input_tokens": I, "output_tokens": O}` from the Model's `fake` settings in `study.yaml` (default `0` and `0`), so its cost is priced from `prices.yaml` like any Model's; model build `fake-1`, category `ok`. Its handle carries the answer itself, so it can be collected after a restart.

With `fake.invalid_rate` (`0` to `1`, default `0`) an attempt answers invalidly when `random.Random(<attempt seed>).random() < invalid_rate`, so whether an attempt is invalid is fixed by its seed: the same `study.seed` and `invalid_rate` give the same `invalid` Trials and attempt counts in a fresh Study. Invalid answers rotate with the attempt number: attempt 1, 4, ... is not JSON (`I think it is about a 4.`); attempt 2, 5, ... omits the first Item; attempt 3, 6, ... gives the first Item an out-of-range value (a Likert value `points + 1`, a pairwise choice `C`, an empty free text). The Fake rater never validates; the engine does.

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

**Retries.** The raw answer is always archived first. A valid answer makes the Trial `valid`. An invalid one leaves the Trial `sent` and, while `attempt <= max_retries`, the engine sends a new attempt: `attempt` is incremented, the seed is the derived seed for key `<session_id>:<trial_index>:<attempt>`, the request text is unchanged (same `request_sha256`), and the attempt reserves its cost and can pause at the ceiling like any other (a retry refused by the ceiling leaves the Trial `sent` for `--resume`). An invalid answer with no retries left makes the Trial `invalid`. `max_retries` is the Test's effective `session.max_retries` (the Test's `session.max_retries`, else `study.yaml`'s, default `2`), so a Trial has at most `1 + max_retries` attempts; with `max_retries: 0` the first invalid answer is final. Retries are sequential per Trial: attempt `n+1` is sent only after attempt `n` was collected and found invalid. A Trial never gets more than `1 + max_retries` attempts, also across a resume. When a resumed Trial has already used them all, it is settled from the board without another attempt: `invalid` if its last attempt was recorded invalid; `failed`, with category `attempts_exhausted`, if its last attempt was abandoned before it was answered (a crash during the last allowed attempt). Such a `failed` Trial is not an invalid answer: it is outside the invalid-answer rate and counted with `failed`. So a resumed Run can differ from an uninterrupted one only for Trials whose last allowed attempt was interrupted. A Trial whose latest attempt was already recorded valid (stopped before its state was written) is settled `valid` from the board, without collecting it again. Abandoned attempts keep their attempt row (and possibly a request line) but have no response line. Refused and failed answers are never retried.

**The answer** of a Trial is its **highest valid attempt** (a query, `board.trials.chosen_answer`, not a stored pointer).

**Invalid-answer rate** = Trials `invalid` ÷ (`valid` + `invalid`), empty (`None`) when both are `0`. `refused` and `failed` Trials are not in the denominator and are reported separately as counts. It is defined once (`core.validate.invalid_rate`) and reported per Agent and per Model for a Test (`board.trials.invalid_rates`: for each Agent and each Model the `valid`, `invalid`, `refused` and `failed` counts and the `rate`); `status` (story 1.11) and `export` (story 1.12) will use the same definition. The target is `thresholds.invalid_rate_max` in `study.yaml`.

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

| Situation | Result |
| --- | --- |
| Pilot or screening Test on Fake Models, `--yes` | Every Trial `valid` at attempt `1`; one request and one response line per Trial; counts by state printed; exit `0` |
| No `--yes`, answer `n` (or anything but yes) | `not_confirmed`, exit `1`; no Trial stored, no Archive written |
| No `--yes`, stdin not a terminal | `confirmation_required`, exit `1`; no Trial stored, no Archive written |
| Another dispatching command holds `board.lock` | `study_busy`, exit `1`; nothing written |
| The Test already has Trials | `test_already_open` (`...; use --resume to continue it`), exit `1`; nothing written |
| `kind: main` Test | `protocol_lock_unavailable`, exit `1`; nothing written |
| A Model of the Test uses a provider other than `fake` | `ceiling_required` without a ceiling, else `provider_unavailable`; exit `1`; nothing written |

#### Resume

`open TEST --resume` continues a stopped Run (killed, crashed or failed) at Trial level, through the same engine. It never re-plans and never re-orders: every stored Trial is **re-rendered** from its `board.db` row plus the current Study folder (Persona card, Instrument, Practice examples, Clip hashes), which also proves the Archive can re-issue its requests. In order:

1. The same checks as a Run up to planning (a `kind: main` Test is refused with `protocol_lock_unavailable`; `test_exists`, config, Panel and Clip checks). An older `board.db` layout is migrated under the lease first.
2. Refuses a Test with no Trials in `board.db`: `test_not_open`, nothing written.
3. Takes Raters only for the Models of the non-terminal Trials (a Model whose Trials are all terminal never blocks a resume): `unknown_model` if one is no longer in `study.yaml`, `provider_unavailable` if its provider has no adapter. Before that it prints the summary lines with the cost estimate of the **non-terminal Trials only** and the ceiling, and applies `ceiling_required` and the `--ceiling` below committed spend check (but not the `expected` check of `over_ceiling`: a resume runs until the ceiling pauses it).
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
   - settled (latest attempt recorded valid: `valid`; all `1 + max_retries` attempts used: `invalid` or `failed`): its state is written, nothing is sent or collected;
   - `sent` whose latest attempt has a stored handle and is not yet validated: collected at that same attempt (steps 5 to 8 only, then retried if invalid with retries left: no new attempt, no new request line); its response line carries the archived `request_sha256` (a stored handle that is not a JSON object stops the resume with `adapter_error: stored handle unreadable for <trial_id>`);
   - `planned` (attempt `0`, or `>= 1` after a stop between recording the attempt and marking it `sent`), `sent` without a handle, and `sent` whose latest attempt is already recorded invalid: a new attempt (steps 1 to 8, new seed, same request text, new request line). Attempt numbers are never reused, and an attempt that was never marked `sent` is never collected.

   The Test's pause (`paused_reason`) is cleared only when the resume ends without pausing again.

It ends like a Run: Trials by state and the cost footer, exit `0` (with the `warning:` line if some did not end `valid`), or `paused: ceiling` and `ceiling_reached`, exit `1`, if it paused again. With nothing to resume it dispatches nothing and writes nothing (no ceiling change, the pause is kept), prints the counts and exits `0`.

#### Cost and ceiling

**One cost formula.** Every figure comes from one offline function (`core.cost.estimate`); no provider is ever asked for token counts or prices. For one attempt of one Trial on Model `m` with `p = prices.yaml models.<m>`:

- media tokens = ceil(Σ `duration_s` of every Clip the request shows, Practice examples and targets, each appearance counted, × `p.media_tokens_per_s`);
- text tokens = ceil(characters of the request's canonical JSON ÷ `p.chars_per_token`);
- cost (USD) = (media + text tokens) × `p.input_usd_per_mtok` ÷ 10^6 + the Model's `max_output_tokens` × `p.output_usd_per_mtok` ÷ 10^6.

USD amounts are exact decimals, stored and printed as decimal strings (no exponent, trailing zeros dropped: `5.00` prints as `5`).

**Estimate.** `expected` is the sum of that cost over every Trial the open would send (all planned Trials for a Run or dry run, covering both pairwise orders, every Repeat and the Practice clips; the non-terminal Trials for `--resume`). `worst_case` = `expected` × (1 + `max_retries`), with the Test's effective `session.max_retries`. Both are printed side by side with the number of Trials and the providers that will receive Clips:

```text
cost estimate: expected 0.4183 USD, worst case 1.2549 USD (max_retries 2)
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
| `board_busy` | any command that writes `board.db` | Another process holds the `board.db` lock past the busy timeout. Try again. |
| `board_version_mismatch` | any command that opens `board.db` | `board.db` has a newer layout version (`PRAGMA user_version`) than this `consortium` knows. Any read-only command (one that never migrates, such as `open --dry-run`) also raises it for an older version. |
| `board_unreadable` | any read-only command (`open --dry-run`), `open` | `board.db` is corrupt or SQLite cannot open or read it, or a ledger amount is not a decimal. |
| `board_wal_unavailable` | any command that opens `board.db` | SQLite could not put `board.db` in WAL mode (for example on some network file systems). |
| `panel_exists` | `personas generate` | `panel/personas/` already holds files; pass `--force` to replace them. |
| `personas_failed` | `personas generate` | The file system failed while writing the Panel; no partial Panel is left. |
| `panel_missing` | any command that needs Personas | `panel/personas/index.json` does not exist; run `consortium personas generate`. |
| `panel_invalid` | any command that needs Personas | `panel/personas/index.json` cannot be read as a non-empty list of Personas (all five traits with `high`/`low`, `nars` `low`/`high`, ids exactly `p1 ... pN` in order), or a `p<n>.md` card is missing. |
| `bad_test_name` | `push test` | The Test name does not match `^[a-z0-9]([a-z0-9_-]*[a-z0-9])?$` or is longer than 64 characters. |
| `test_changed` | `push test`, `open` | The Test file changed while it was being validated, or (for a Run or `--resume`) the Test file, its requests, its non-terminal Trials or its providers changed between the confirmation and the lease; nothing was registered, planned or stored. |
| `test_exists` | `push test`, `open` | A Test of that name is registered with different bytes, its registered `tests/<name>.yaml` is missing or was edited, or `tests/<name>.yaml` already exists unregistered with different bytes. |
| `unknown_clip` | `push test`, `open` | A target Clip ID is not in `board.db` (field `clips[i]`), or a Clip a registered Test uses is missing when its Trials are rendered. |
| `bad_pairing` | `push test`, `open` | The pairing plan cannot be built: no target Clips, a pairwise Instrument with fewer than 2 targets, duplicate target Clip IDs, or a `session.pairing` other than `all_pairs`. |
| `bad_practice` | `push test`, `open` | A Practice Clip is not pushed or is also a target, a pairwise example lists the same Clip twice, or an Instrument has fewer than `session.practice_clips` Practice examples. |
| `clip_kind_overlap` | `push test` | A target Clip is already a target of a registered Test of the other side (`main` vs `pilot`/`screening`). |
| `media_limit_exceeded` | `push test`, `open` | A worst-case Trial exceeds a Model's `limits.max_seconds` or `limits.max_bytes`. |
| `unknown_test` | `open` | The Test is not registered (or there is no `board.db` yet). |
| `protocol_lock_unavailable` | `open` | The Test is `kind: main` (main Tests open only once the Protocol lock exists, Epic 4), or is otherwise registered as not openable. |
| `invalid_response` | `open`, `open --resume` (recorded, not printed) | A Model's raw answer failed the Instrument's response schema; the reason (`not_json`, `missing_item`, `out_of_range:<item>`, ...) is stored as the attempt's `invalid_reason` and the Trial is retried or becomes `invalid` (see [Response validation and retries](#response-validation-and-retries)). Never an exit code. |
| `invalid_ceiling` | `open` | `--ceiling` is not a decimal USD amount greater than 0; nothing changed. |
| `ceiling_required` | `open` | No cost ceiling has ever been set and none was given, and a Model the open sends to is not `provider: fake` priced `0`; nothing was sent. |
| `over_ceiling` | `open` | Committed spend plus the Run's expected cost exceeds the ceiling (`[committed C + ]expected X > ceiling Y`), or `--ceiling` is below committed spend (`committed C > ceiling Y`); nothing was sent or logged. |
| `ceiling_overshoot` | `open` (stderr warning) | An attempt's actual cost exceeded its estimate and pushed committed spend over the ceiling; the Run paused. |
| `ceiling_reached` | `open`, `open --resume` | The Run paused because the next attempt's reservation would cross the ceiling; in-flight attempts were collected. Continue with `--resume --ceiling <higher>`. |
| `unknown_prompt_variant` | `open` | A Trial's Prompt variant is not defined by its Instrument (an internal consistency check). |
| `provider_unavailable` | `open` | A Model of the Test uses a provider with no adapter yet (only `fake` exists in this version). |
| `study_busy` | `open` | Another dispatching command holds the `board.lock` lease of this Study. |
| `test_already_open` | `open` | The Test already has Trials in `board.db`; it cannot be opened again (message ends `; use --resume to continue it`). |
| `not_confirmed` | `open` | The Run was declined at the confirmation prompt; nothing was stored or sent. |
| `confirmation_required` | `open` | No `--yes` and stdin is not a terminal, so the Run cannot be confirmed; nothing was stored or sent. |
| `unknown_model` | `open --resume` | A Model of the Test's non-terminal Trials is no longer in `study.yaml`. |
| `archive_corrupt` | `open --resume` | An Archive line is valid JSON but not a record with a string `trial_id` and an integer `attempt`; the message names `<file>:<line>`. |
| `test_not_open` | `open --resume` | The Test has no Trials in `board.db`; open it without `--resume` first. |
| `reissue_mismatch` | `open --resume` | An archived request no longer re-renders byte-identically from its Trial row and the Study folder (a Study input was edited after open), or its `request_sha256`, seed or Model does not match; nothing was sent. |
| `run_failed` | `open` | The Run stopped on an unexpected error (`<type>: <message>`); see the resume contract under [Run](#run). |
| `adapter_error` | `open` | A Rater broke the port contract (for example returned the wrong number of results), or `--resume` found a stored handle that is not a JSON object. The Run stopped. |
| `bad_concurrency` | `open` | The engine was given a concurrency below 1 (an internal check; `study.yaml` already requires at least 1). |
| `bad_max_retries` | `open` | The engine was given `max_retries` below 0 (an internal check; the config already requires at least 0). |
| `unknown_instrument` | any command that loads config | An Instrument name in `study.yaml` or a Test does not resolve, or a Test lists an Instrument not enabled in `study.yaml`. |

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

SQLite in WAL mode; the only mutable Study state, created by the first `push clip` or successful `push test`. Its layout version is `PRAGMA user_version` (currently `5`; older files are migrated forward when opened). Table `clips` (version 1), one row per Clip:

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

Table `attempts` (version 3), one row per `(trial_id, attempt)` (primary key): `seed` (the attempt's derived Model seed), `handle` (the Rater's handle as canonical JSON, once submitted), `sent_at`, `answered_at` (UTC ISO 8601 with milliseconds and `Z`) and `category` (the Rater's result category, `ok` or a snake_case reason). Indexed by `trial_id`. Columns added in version 5: `valid` (`1` or `0` once the answer was validated, empty before and for a category other than `ok`), `invalid_reason` (the `invalid_response` reason of an invalid attempt) and `answer_json` (a valid attempt's parsed answer `{item_id: value}` as canonical JSON). An abandoned last allowed attempt (never answered) that ended its Trial `failed` on resume has category `attempts_exhausted`.

Table `ledger` (version 4), one row per `(trial_id, attempt)` (primary key) that was reserved: `model_id`, `reserved_usd` (the attempt's estimated cost, written in the same transaction as its `attempts` row) and `actual_usd` (from the returned usage; empty until known, or when the answer had no usage). USD as decimal strings.

Table `ceiling_changes` (version 4), one row per `open --ceiling`: `ts` (UTC ISO 8601 with milliseconds and `Z`), `previous_usd` (empty for the first), `ceiling_usd`, `test` (the Test opened) and `command` (`run` or `resume`). The latest row (insertion order) is the current, Study-wide ceiling.

`board.db` never holds a Condition, the source file name or a hash of the source file.

Read-only commands (`open --dry-run`) open it with SQLite `mode=ro` and never migrate it or write Study data; when no `board.db-wal` exists they add `immutable=1`, so no `board.db-wal`/`board.db-shm` side files are created (reads are redone without it if a writer starts meanwhile). A stale `board.db-wal` left by a crashed writer can make SQLite create `board.db-shm`, which holds no Study data.

### `board.lock`

An empty file; `open` and `open --resume` (dispatching commands) hold an exclusive `fcntl.flock` on it for the whole command. A second dispatcher refuses with `study_busy`. The lock is released by the OS when the holding process ends, so a leftover file never blocks.

### `archive/requests.jsonl`, `archive/responses.jsonl`

Append-only; never rewritten. One canonical JSON object per line (sorted keys, UTF-8, no whitespace, LF), keyed by `trial_id` + `attempt`; each line is flushed and fsynced before the Run moves on. A request line is written before its Trial is marked `sent`; a response line before the Trial changes state. The `archive/` directory is fsynced when it or a file in it is first created. A crash during an append can leave an unterminated final line: readers ignore it, and the next append first ends it with a newline so it never merges into a new record (readers also skip and count such a fragment; a valid JSON line that is not a keyed record is `archive_corrupt`). A key can appear on more than one line (a response collected again by `--resume`, see [Resume](#resume)); every reader takes the **last line per key**. Timestamps (`ts`, and `sent_at`/`answered_at` in `board.db`) are UTC ISO 8601 with milliseconds and `Z`.

- **Request:** `{"attempt", "model_id", "request", "request_sha256", "seed", "trial_id", "ts"}`. `request` is the rendered request object (see [Trial requests](#trial-requests)); `request_sha256` is the SHA-256 of its canonical JSON; `seed` the attempt's Model seed; `ts` UTC ISO 8601 with milliseconds and `Z`.
- **Response:** `{"attempt", "category", "model_build", "raw", "request_sha256", "trial_id", "ts", "usage"}`. `request_sha256` repeats that of the attempt's request line; `raw` is the Model's raw text, `usage` `{"input_tokens", "output_tokens"}`, `model_build` the provider-reported build (or `null`), `category` `ok` or a snake_case reason.

Media appear only as Clip ID + SHA-256. The Archive never holds a Condition, a source file name or a provider file handle.

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
| `models[].fake` | Fake rater settings, only for `provider: fake` (on another provider: `config_invalid`, field `models.<i>`): `input_tokens` and `output_tokens` (integers >= 0, template and default `0`), the usage it reports for every answer; `invalid_rate` (a number from `0` to `1`, template and default `0`), the share of attempts it answers invalidly, decided per attempt seed (see [Run](#run)). |
| `models[].limits` | *Required.* What the Model accepts per request: `max_seconds` (> 0, template `600`), `max_bytes` (integer > 0, template `20000000`), `inline_base64` (template `true`; media is sent base64-inline, which counts 4/3 of the file size). |
| `media` | Canonical Clip encoding: `height` 480 (must be even), `video_kbps` 400, `audio_kbps` 64, `fps` 25. |
| `session.practice_clips` | Practice examples included per Trial per Instrument (>= 0); template `2`. |
| `session.repeats` | Repeats per Agent (>= 1); template `3`. |
| `session.max_retries` | Retries after an invalid answer (>= 0); template and default `2`. A Trial gets at most `1 + max_retries` attempts (see [Response validation and retries](#response-validation-and-retries)). |
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
| `session` | Optional overrides of any `study.yaml` `session` key (`practice_clips`, `repeats`, `max_retries`, `pairing`). |

Prompt variants are not chosen in the Test; they rotate by Repeat (see [Sessions and Trials](#sessions-and-trials)).

### `prices.yaml`

| Field | Meaning |
| --- | --- |
| `schema_version` | *Required.* `1`. |
| `models.<model id>` | Per Model id: `input_usd_per_mtok` and `output_usd_per_mtok`, USD per million tokens, as quoted non-negative decimal strings (for example `"0.30"`; an unquoted `0.30` is refused). Every Model id in `study.yaml` must have an entry, and no other ids may appear (`config_invalid`, field `models.<id>`). The template prices the fake Model `m1` at `"0"`. |
| `models.<model id>.media_tokens_per_s` | Input tokens per second of Clip media, a quoted non-negative decimal string; default and template `"300"`. |
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
