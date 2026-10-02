"""Use case: push Clips into the Study blind (AD-2, AD-11).

The only stage that imports ``board.blinding`` at write time. A Clip's
Condition is written only to ``blinding_key.csv``; ``board.db`` gets neutral
facts; stdout gets the Clip ID. The source path and Condition strings are
never printed, logged or put in an error message.
"""

from __future__ import annotations

import csv
import hashlib
import io
import logging
import os
import secrets
import sqlite3
from collections.abc import Callable, Sequence
from pathlib import Path
from statistics import fmean

from consortium.board import blinding
from consortium.board.clips import clip_exists, insert_clip, list_clips
from consortium.board.db import connect, transaction
from consortium.config.load import load_study
from consortium.config.models import LeakTolerance, MediaProfile
from consortium.core.errors import ConsortiumError
from consortium.core.ids import new_clip_id
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
