"""Use cases: push Clips into the Study blind (AD-2, AD-11), and register Tests.

The only stage that imports ``board.blinding`` at write time. A Clip's
Condition is written only to ``blinding_key.csv``; ``board.db`` gets neutral
facts; stdout gets the Clip ID. The source path and Condition strings are
never printed, logged or put in an error message.

``push_test`` validates a Test YAML against ``board.db`` and the Instruments,
then copies it to ``tests/<name>.yaml`` (unless it is already there) and
registers it. Nothing is copied or registered on any error.
"""

from __future__ import annotations

import csv
import errno
import hashlib
import io
import logging
import os
import re
import secrets
import sqlite3
from collections.abc import Callable, Sequence
from pathlib import Path
from statistics import fmean

from consortium.board import blinding
from consortium.board.clips import clip_exists, insert_clip, list_clips
from consortium.board.db import DB_FILE, connect, transaction
from consortium.board.tests import get_test, insert_test, target_clip_kinds
from consortium.config.load import load_instruments, load_study, load_test, peek_test
from consortium.config.models import LeakTolerance, MediaProfile, StudyConfig, TestConfig
from consortium.core.errors import ConsortiumError
from consortium.core.ids import new_clip_id
from consortium.core.media_limits import TrialShape, check_media
from consortium.media.canonicalize import canonicalize

log = logging.getLogger(__name__)

CLIPS_DIR = "clips"
EXPORTS_DIR = "exports"
LEAK_REPORT = "leak-report.csv"
LEAK_COLUMNS = ("factor", "metric", "level", "n", "mean", "max_diff", "tolerance", "flagged")
# (metric, exact tolerance): exact metrics must match clip-for-clip across levels;
# None means the tolerance comes from thresholds.leak_tolerance and means are compared.
# fps is stored rounded to 3 decimals, so it gets a small epsilon instead of 0.
FPS_EPSILON = 0.01
_METRICS: tuple[tuple[str, float | None], ...] = (
    ("duration_s", None),
    ("loudness_lufs", None),
    ("width", 0.0),
    ("height", 0.0),
    ("fps", FPS_EPSILON),
)
_ID_ATTEMPTS = 20


def parse_conditions(conditions: Sequence[str]) -> dict[str, str]:
    """``["factor=level", ...]`` to ``{factor: level}``; raises ``bad_condition``.

    Messages give the position only, never the text, so Conditions stay out of logs.
    """
    parsed: dict[str, str] = {}
    for index, raw in enumerate(conditions, start=1):
        factor, sep, level = raw.partition("=")
        factor, level = factor.strip(), level.strip()
        if not sep or not factor or not level:
            raise ConsortiumError(
                "bad_condition", f"condition {index} must be factor=level, both non-empty"
            )
        if any(not ch.isprintable() for ch in factor + level):
            raise ConsortiumError(
                "bad_condition", f"condition {index} contains control characters"
            )
        if factor in parsed:
            raise ConsortiumError(
                "bad_condition", f"condition {index} repeats a factor already given"
            )
        parsed[factor] = level
    return parsed


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _quietly(step: Callable[..., object], *args: object) -> None:
    """Run one cleanup step; its failure is logged, never raised over the original error."""
    try:
        step(*args)
    except Exception as err:  # noqa: BLE001 - cleanup must not mask the original error
        log.warning("cleanup step failed: %s", type(err).__name__)


def _remove_if_moved(final: Path | None, tmp: Path) -> None:
    if final is not None and final.exists() and not tmp.exists():
        final.unlink(missing_ok=True)


def _rmdir_if_empty(path: Path) -> None:
    if path.exists() and not any(path.iterdir()):
        path.rmdir()


def push_clip(study_dir: Path | str, src: Path | str, conditions: Sequence[str]) -> str:
    """Canonicalize ``src`` into ``clips/<clip_id>.mp4`` and return the new Clip ID.

    All-or-nothing: on any failure ``clips/``, ``board.db`` rows and
    ``blinding_key.csv`` are left as they were. Operating-system and SQLite
    failures are reported as ``push_failed`` with a message that names no
    source path. Refreshes the leak report; if only that refresh fails, the
    Clip stays stored, ``leak_report_failed`` is logged and the ID is returned.
    """
    study = Path(study_dir)
    parsed = parse_conditions(conditions)
    cfg = load_study(study)
    try:
        clip_id = _store_clip(study, Path(src), parsed, cfg.media)
    except sqlite3.Error as err:
        raise ConsortiumError("push_failed", f"could not store the Clip: {err}") from err
    except OSError as err:
        reason = err.strerror or type(err).__name__
        raise ConsortiumError("push_failed", f"could not store the Clip: {reason}") from err

    log.info("pushed clip %s", clip_id)
    try:
        write_leak_report(study)
    except (ConsortiumError, OSError, sqlite3.Error) as err:
        reason = err.message if isinstance(err, ConsortiumError) else type(err).__name__
        log.warning("leak_report_failed: %s", reason)
    return clip_id


def _store_clip(study: Path, src: Path, parsed: dict[str, str], media: MediaProfile) -> str:
    clips_dir = study / CLIPS_DIR
    created_dir = not clips_dir.exists()
    clips_dir.mkdir(exist_ok=True)
    tmp = clips_dir / f".push-{secrets.token_hex(8)}.mp4"
    final: Path | None = None
    mark: int | None = None
    key_pending = False  # key rows appended but the transaction not yet committed
    conn = None
    try:
        info = canonicalize(src, tmp, media)
        sha = _sha256(tmp)
        conn = connect(study)
        for _ in range(_ID_ATTEMPTS):
            clip_id = new_clip_id()
            if not clip_exists(conn, clip_id) and not (clips_dir / f"{clip_id}.mp4").exists():
                break
        else:  # pragma: no cover - 2^40 ID space
            raise ConsortiumError("clip_id_exhausted", "could not draw an unused Clip ID")
        final = clips_dir / f"{clip_id}.mp4"
        with transaction(conn):
            insert_clip(conn, clip_id, sha, info)
            # Marked under the write lock, so a rollback never cuts another push's rows.
            mark = blinding.key_mark(study)
            key_pending = True
            try:
                blinding.append_conditions(study, clip_id, parsed)
                os.replace(tmp, final)
                _fsync_dir(clips_dir)
            except BaseException:
                _quietly(blinding.restore_key, study, mark)
                key_pending = False
                raise
        key_pending = False
    except BaseException:
        _quietly(_remove_if_moved, final, tmp)
        _quietly(tmp.unlink, True)
        if key_pending:  # COMMIT itself failed
            _quietly(blinding.restore_key, study, mark)
        if created_dir:
            _quietly(_rmdir_if_empty, clips_dir)
        raise
    finally:
        if conn is not None:
            _quietly(conn.close)
    return clip_id


def _fmt(value: float | None) -> str:
    if value is None:
        return ""
    return repr(round(float(value), 4))


def _leak_rows(
    key: dict[str, dict[str, str]],
    clips: dict[str, dict],
    tolerance: LeakTolerance,
) -> list[tuple[str, ...]]:
    # factor -> level -> [clip rows]
    groups: dict[str, dict[str, list[dict]]] = {}
    for clip_id, factors in key.items():
        clip = clips.get(clip_id)
        if clip is None:
            continue
        for factor, level in factors.items():
            groups.setdefault(factor, {}).setdefault(level, []).append(clip)

    rows: list[tuple[str, ...]] = []
    for factor in sorted(groups):
        levels = groups[factor]
        for metric, exact_tol in _METRICS:
            exact = exact_tol is not None
            tol = exact_tol if exact_tol is not None else float(getattr(tolerance, metric))
            values = {
                level: [c[metric] for c in members if c[metric] is not None]
                for level, members in levels.items()
            }
            means = {level: fmean(v) if v else None for level, v in values.items()}
            for level in sorted(levels):
                others = [o for o in levels if o != level]
                diffs: list[float] = []
                for other in others:
                    if exact:
                        diffs.extend(abs(a - b) for a in values[level] for b in values[other])
                    elif means[level] is not None and means[other] is not None:
                        diffs.append(abs(means[level] - means[other]))
                max_diff = max(diffs) if diffs else None
                flagged = max_diff is not None and max_diff > tol
                rows.append((
                    factor, metric, level, str(len(values[level])), _fmt(means[level]),
                    _fmt(max_diff), _fmt(tol), "true" if flagged else "false",
                ))
    return rows


def write_leak_report(study_dir: Path | str) -> Path:
    """Write ``exports/leak-report.csv``: per factor, metric and level, compared across levels.

    Clips without key rows (for example Practice clips) are left out.
    ``max_diff`` is the largest difference between this level and any other
    level of the factor: of means for ``duration_s`` and ``loudness_lufs``, of
    individual Clips for ``width``, ``height`` (which must match exactly) and
    ``fps`` (within ``FPS_EPSILON``). Empty when the factor has one level. ``flagged`` is
    ``max_diff > tolerance``.
    """
    study = Path(study_dir)
    cfg = load_study(study)
    key = blinding.read_key(study)
    conn = connect(study)
    try:
        clips = {c["clip_id"]: c for c in list_clips(conn, ids=key)}
    finally:
        conn.close()
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(LEAK_COLUMNS)
    writer.writerows(_leak_rows(key, clips, cfg.thresholds.leak_tolerance))

    out_dir = study / EXPORTS_DIR
    out_dir.mkdir(exist_ok=True)
    out = out_dir / LEAK_REPORT
    tmp = out_dir / f".{LEAK_REPORT}.{secrets.token_hex(8)}.tmp"
    try:
        with tmp.open("w", encoding="utf-8", newline="") as fh:
            fh.write(buf.getvalue())
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, out)
    finally:
        tmp.unlink(missing_ok=True)
    return out


# --------------------------------------------------------------------------- push test

TESTS_DIR = "tests"
TEST_NAME_MAX = 64
TEST_NAME_PATTERN = r"^[a-z0-9]([a-z0-9_-]*[a-z0-9])?$"
_TEST_NAME = re.compile(TEST_NAME_PATTERN)
_NO_HARD_LINKS = {errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP}

Fail = Callable[[str, str], ConsortiumError]


def _display_path(path: Path, study: Path) -> str:
    try:
        return Path(os.path.relpath(path.resolve(), study.resolve())).as_posix()
    except ValueError:  # different drive on Windows
        return str(path)


def _check_name(name: str, fail: Fail) -> None:
    if len(name) > TEST_NAME_MAX or not _TEST_NAME.fullmatch(name):
        raise fail(
            "bad_test_name",
            f"test: {name!r} must match {TEST_NAME_PATTERN} and be at most {TEST_NAME_MAX} "
            "characters (it is part of every Session ID)",
        )


def _check_raw(raw: dict, fail: Fail) -> None:
    """Name rule and pairing plan, checked before schema validation would hide them."""
    name = raw.get("test")
    if isinstance(name, str):
        _check_name(name, fail)
    session = raw.get("session")
    if isinstance(session, dict):
        pairing = session.get("pairing")
        if pairing is not None and pairing != "all_pairs":
            raise fail(
                "bad_pairing", f"session.pairing: {pairing!r} is not supported; use all_pairs"
            )
    clips = raw.get("clips")
    if isinstance(clips, list):
        seen: set[str] = set()
        for i, clip in enumerate(clips):
            if isinstance(clip, str):
                if clip in seen:
                    raise fail("bad_pairing", f"clips[{i}]: duplicate Clip ID {clip!r}")
                seen.add(clip)


def _check_overlap(conn: sqlite3.Connection | None, test: TestConfig, fail: Fail) -> None:
    """``clip_kind_overlap``: main and pilot/screening Tests never share a target Clip."""
    if conn is None:
        return
    uses = target_clip_kinds(conn, test.clips)
    for i, clip in enumerate(test.clips):
        for other, kind in uses.get(clip, []):
            if other != test.test and (kind == "main") != (test.kind == "main"):
                raise fail(
                    "clip_kind_overlap",
                    f"clips[{i}]: {clip} is already a target of {kind} Test {other!r}; "
                    f"a {test.kind} Test may not share target Clips with it",
                )


def _validate_test(
    conn: sqlite3.Connection | None,
    study: Path,
    test: TestConfig,
    cfg: StudyConfig,
    fail: Fail,
) -> None:
    """References, plan, Practice, overlap and media checks, in that order."""
    instruments = load_instruments(study, cfg)
    wanted = set(test.clips) | {c for ex in test.practice for c in ex.clips}
    clips = {r["clip_id"]: r for r in list_clips(conn, ids=wanted)} if conn else {}

    # References.
    for i, clip in enumerate(test.clips):
        if clip not in clips:
            raise fail("unknown_clip", f"clips[{i}]: {clip!r} is not a pushed Clip")
    for i, example in enumerate(test.practice):
        for clip in example.clips:
            if clip not in clips:
                raise fail("bad_practice", f"practice[{i}]: {clip!r} is not a pushed Clip")

    # Plan.
    if not test.clips:
        raise fail("bad_pairing", "clips: a Test needs at least 1 target Clip")
    for name in test.instruments:
        if instruments[name].pairwise and len(test.clips) < 2:
            raise fail(
                "bad_pairing",
                f"clips: pairwise Instrument {name!r} needs at least 2 target Clips, "
                f"got {len(test.clips)}",
            )

    # Practice.
    targets = set(test.clips)
    for i, example in enumerate(test.practice):
        if len(set(example.clips)) != len(example.clips):
            raise fail("bad_practice", f"practice[{i}]: lists the same Clip twice")
        for clip in example.clips:
            if clip in targets:
                raise fail(
                    "bad_practice",
                    f"practice: {clip} is both a Practice clip (practice[{i}]) and a target",
                )
    per_trial = test.effective_session(cfg).practice_clips
    shapes: list[TrialShape] = []
    for name in test.instruments:
        indexes = [i for i, ex in enumerate(test.practice) if ex.instrument == name]
        if len(indexes) < per_trial:
            field = f"practice[{indexes[-1]}]" if indexes else "practice"
            raise fail(
                "bad_practice",
                f"{field}: Instrument {name!r} has {len(indexes)} Practice example(s); "
                f"session.practice_clips needs {per_trial}",
            )
        used = tuple(c for i in indexes[:per_trial] for c in test.practice[i].clips)
        shapes.append(TrialShape(name, instruments[name].pairwise, used, tuple(test.clips)))

    # Overlap.
    _check_overlap(conn, test, fail)

    # Media.
    models = [cfg.model_by_id(m) for m in test.model_ids(cfg)]
    violation = check_media(shapes, clips, models)
    if violation is not None:
        raise fail("media_limit_exceeded", violation.message)


def _note_kind(name: str, kind: str) -> None:
    if kind == "main":
        log.warning(
            "not_openable: Test %s is kind main; not openable until Protocol lock (Epic 4)", name
        )


def push_test(study_dir: Path | str, path: Path | str) -> str:
    """Validate the Test file at ``path``, register it and return its name.

    A file outside ``tests/`` is copied to ``tests/<name>.yaml``; one already
    there is recorded in place. Re-pushing identical bytes is a no-op; changed
    bytes under a registered name raise ``test_exists``. A ``kind: main`` Test
    is registered as not openable. First error wins; on any error nothing is
    copied or registered. File-system and SQLite failures are ``push_failed``.
    """
    study = Path(study_dir)
    src = Path(path)
    rel = _display_path(src, study)

    def fail(code: str, message: str) -> ConsortiumError:
        return ConsortiumError(code, message, path=rel)

    try:
        return _push_test(study, src, fail)
    except ConsortiumError:
        raise
    except sqlite3.Error as err:
        raise fail("push_failed", f"could not register the Test: {err}") from err
    except OSError as err:
        reason = err.strerror or type(err).__name__
        raise fail("push_failed", f"could not store the Test: {reason}") from err


def _push_test(study: Path, src: Path, fail: Fail) -> str:
    try:
        data = src.read_bytes()  # the bytes that are validated, hashed and copied
    except OSError:
        load_test(src, study)  # reports a missing or unreadable file as config_invalid
        raise
    sha = hashlib.sha256(data).hexdigest()
    _check_raw(peek_test(src), fail)
    cfg = load_study(study)
    test = load_test(src, study, cfg=cfg)
    name = test.test
    _check_name(name, fail)

    tests_dir = study / TESTS_DIR
    final = tests_dir / f"{name}.yaml"
    stored = f"{TESTS_DIR}/{name}.yaml"
    in_place = src.resolve() == final.resolve()

    conn = connect(study) if (study / DB_FILE).exists() else None
    try:
        existing = get_test(conn, name) if conn else None
        if existing is not None:
            if existing["sha256"] != sha:
                raise fail(
                    "test_exists", f"test: {name!r} is already registered with other content"
                )
            return _noop(existing, final, fail)
        if not in_place and final.exists() and _stored_sha256(final) != sha:
            raise fail("test_exists", f"test: {stored} already exists with other content")

        _validate_test(conn, study, test, cfg, fail)
        if hashlib.sha256(src.read_bytes()).hexdigest() != sha:
            raise fail("test_changed", "the Test file changed while it was being validated")

        row = {
            "name": name, "kind": test.kind, "path": stored, "sha256": sha,
            "openable": test.kind != "main",
        }
        clip_rows = [(c, "target") for c in test.clips]
        practice = dict.fromkeys(c for ex in test.practice for c in ex.clips)
        clip_rows += [(c, "practice") for c in practice]
        if conn is None:
            conn = connect(study)
        raced = _register(conn, test, row, clip_rows, data, final, in_place, fail)
        if raced is not None:
            return _noop(raced, final, fail)
    finally:
        if conn is not None:
            _quietly(conn.close)
    log.info("registered test %s", name)
    _note_kind(name, test.kind)
    return name


def _noop(existing: dict, final: Path, fail: Fail) -> str:
    """Identical re-push: the stored file must still hold the registered bytes."""
    if _stored_sha256(final) != existing["sha256"]:
        raise fail(
            "test_exists",
            f"test: {existing['name']!r} is registered, but its registered file is missing "
            "or was edited",
        )
    log.info("test %s already registered; nothing to do", existing["name"])
    _note_kind(existing["name"], existing["kind"])
    return existing["name"]


def _stored_sha256(path: Path) -> str | None:
    """SHA-256 of ``path``; None when it does not exist. Other read errors raise ``OSError``."""
    try:
        return _sha256(path)
    except FileNotFoundError:
        return None


def _register(
    conn: sqlite3.Connection,
    test: TestConfig,
    row: dict,
    clip_rows: list[tuple[str, str]],
    data: bytes,
    final: Path,
    in_place: bool,
    fail: Fail,
) -> dict | None:
    """Copy and register in one transaction.

    Returns the existing row (and registers nothing) when an identical Test was
    registered meanwhile; None when this call registered it.
    """
    tests_dir = final.parent
    created_dir = False
    tmp: Path | None = None
    copied = False
    try:
        if not in_place and not final.exists():
            created_dir = not tests_dir.exists()
            tests_dir.mkdir(exist_ok=True)
            tmp = tests_dir / f".push-{secrets.token_hex(8)}.yaml"
            with tmp.open("xb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
        with transaction(conn):
            existing = get_test(conn, test.test)  # registered meanwhile?
            if existing is not None:
                if existing["sha256"] == row["sha256"]:
                    return existing
                raise fail("test_exists", f"test: {test.test!r} is already registered")
            _check_overlap(conn, test, fail)
            insert_test(conn, row, clip_rows)
            if tmp is not None:
                copied = _place(tmp, final, data, fail)
                if copied:
                    _fsync_dir(tests_dir)
    except BaseException:
        if copied:
            _quietly(final.unlink, True)
        if tmp is not None:
            _quietly(tmp.unlink, True)
        if created_dir:
            _quietly(_rmdir_if_empty, tests_dir)
        raise
    finally:
        if tmp is not None:
            _quietly(tmp.unlink, True)
    return None


def _place(tmp: Path, final: Path, data: bytes, fail: Fail) -> bool:
    """Put ``data`` at ``final`` without ever overwriting; True if a file was placed."""
    try:
        os.link(tmp, final)
        return True
    except FileExistsError:
        pass
    except OSError as err:
        if err.errno not in _NO_HARD_LINKS:
            raise
        try:  # file system without hard links: exclusive create, never overwrite
            fd = os.open(final, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            pass
        else:
            try:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(data)
                    fh.flush()
                    os.fsync(fh.fileno())
            except BaseException:
                _quietly(final.unlink, True)
                raise
            return True
    if final.read_bytes() != data:
        raise fail("test_exists", f"test: {final.parent.name}/{final.name} already exists")
    return False
