"""Use case: copy a screened Panel from another Study into this one (story 3.4).

Copies the source's ``panel/personas/`` byte for byte and imports its current (complete,
not superseded) screening runs and results into this Study's ``board.db``, each result
stamped with ``source_study`` and ``source_hash``. The source is only read: its files
once, its ``board.db`` read-only (no lease, no migration). Story 3.3's stamp check then
decides whether the imported results still count here.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import sqlite3
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from consortium.board.db import DB_FILE, connect, read_only, transaction
from consortium.board.lease import acquire_lease
from consortium.board.screening import (
    RESULT_COLUMNS,
    any_results,
    current_runs,
    import_runs,
    run_results,
)
from consortium.board.trials import any_trials
from consortium.config.load import (
    PERSONAS_DIR,
    STUDY_FILE,
    load_card_wording,
    load_personas,
    load_study,
)
from consortium.config.models import StudyConfig
from consortium.config.panel_files import (
    INDEX_FILE,
    META_FILE,
    exists,
    fsync_dir,
    move_into_place,
    sweep_stale,
    write_file,
)
from consortium.core.errors import ConsortiumError
from consortium.core.personas import render_card

log = logging.getLogger(__name__)

_SOURCE_COLUMNS = ("source_study", "source_hash")


@dataclass(frozen=True)
class CopySummary:
    personas: int
    results: int
    runs: int
    source_study: str  # the source folder, POSIX, relative to the target folder


def copy_panel(study_dir: Path | str, source_dir: Path | str) -> CopySummary:
    """Copy the Panel and current screening results of ``source_dir`` into ``study_dir``.

    Checks in order, nothing written on refusal: the target ``study.yaml``; the source
    ``study.yaml`` and Panel (errors name the source); ``panel_frame_mismatch``;
    ``panel_mismatch`` (a card differs from its re-render); ``panel_in_use`` (the target
    ``board.db`` holds a Trial or a screening result); ``panel_exists``. The source
    ``board.db`` is read before the target checks (5, 6). The Panel goes
    into a ``.personas-new-*`` work folder; then, under the target lease, one transaction
    re-checks, inserts the runs and results and renames the folder into place, so there
    is either both the Panel and the results, or neither.
    """
    study_dir, source_dir = Path(study_dir), Path(source_dir)
    cfg = load_study(study_dir)
    source = Path(os.path.relpath(source_dir.resolve(), study_dir.resolve())).as_posix()
    source_cfg = _from_source(source, lambda: load_study(source_dir))
    personas = _from_source(source, lambda: load_personas(source_dir))
    if _frame(source_cfg) != _frame(cfg):
        raise ConsortiumError(
            "panel_frame_mismatch",
            f"the personas frame differs from the one of {source}/{STUDY_FILE}",
            path=STUDY_FILE,
        )
    files = _from_source(source, lambda: _read_files(source_dir, [p.id for p in personas]))
    wording = load_card_wording(cfg)
    for p in personas:
        rel = f"{PERSONAS_DIR}/{p.id}.md"
        try:
            card = render_card(p, wording).encode("utf-8")
        except ValueError as err:
            raise ConsortiumError(
                "panel_mismatch", f"{source}/{rel}: {err}", path=f"{source}/{rel}"
            ) from err
        if files.get(f"{p.id}.md") != card:
            raise ConsortiumError("panel_mismatch", f"{source}/{rel}", path=f"{source}/{rel}")
    runs, results = _from_source(
        source, lambda: read_only(source_dir, _current, allow_older=True) or ([], [])
    )
    if read_only(study_dir, _in_use, allow_older=True):
        raise _panel_in_use()
    target = study_dir / PERSONAS_DIR
    sweep_stale(target)
    if exists(target):
        raise _panel_exists()

    ids = {run["run_id"]: f"s{n}" for n, run in enumerate(runs, 1)}
    runs = [dict(run, run_id=ids[run["run_id"]], superseded_by=None) for run in runs]
    rows = [dict(row, run_id=ids[row["run_id"]]) for row in results]
    source_hash = _source_hash(files, rows)

    panel = target.parent
    try:
        panel.mkdir(parents=True, exist_ok=True)
        tmp = Path(tempfile.mkdtemp(prefix=".personas-new-", dir=panel))
        try:
            for name, data in files.items():
                write_file(tmp / name, data)
            os.chmod(tmp, 0o755)
            fsync_dir(tmp)
            _install(study_dir, tmp, target, runs, rows, source, source_hash)
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
    except OSError as err:
        raise ConsortiumError(
            "personas_failed", f"could not write {PERSONAS_DIR}: {err}", path=PERSONAS_DIR
        ) from err
    except sqlite3.Error as err:
        raise ConsortiumError(
            "personas_failed", f"could not import the screening results: {err}", path=DB_FILE
        ) from err
    log.info("copied %d personas and %d screening results from %s", len(personas),
             len(rows), source)
    return CopySummary(len(personas), len(rows), len(runs), source)


def _source_hash(files: Mapping[str, bytes], rows: Sequence[Mapping[str, Any]]) -> str:
    """SHA-256 of the canonical JSON ``{"panel": {file: sha256}, "results": rows}``, the
    rows (target run IDs, source order) without the source columns."""
    data = {
        "panel": {name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()},
        "results": [{c: row[c] for c in RESULT_COLUMNS if c not in _SOURCE_COLUMNS}
                    for row in rows],
    }
    text = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _install(
    study_dir: Path, tmp: Path, target: Path, runs: list[dict[str, Any]],
    rows: list[dict[str, Any]], source: str, source_hash: str,
) -> None:
    """Under the lease, in one transaction: re-check, import, rename ``tmp`` into place,
    commit. A failed rename rolls back; a failed commit moves the Panel back to ``tmp``
    (or, failing that, removes it)."""
    with acquire_lease(study_dir):
        conn = connect(study_dir)
        moved = False
        try:
            with transaction(conn):
                if _in_use(conn):
                    raise _panel_in_use()
                if exists(target):
                    raise _panel_exists()
                import_runs(conn, runs, rows, source, source_hash)
                try:
                    move_into_place(tmp, target)
                except FileExistsError as err:
                    raise _panel_exists() from err
                moved = True
                fsync_dir(target.parent)  # the rename is durable before the results are
        except BaseException:
            if moved:
                try:
                    os.rename(target, tmp)
                except OSError:
                    shutil.rmtree(target, ignore_errors=True)
            raise
        finally:
            conn.close()


def _frame(cfg: StudyConfig) -> str:
    return json.dumps(cfg.personas.model_dump(mode="json"), sort_keys=True,
                      separators=(",", ":"))


def _read_files(source_dir: Path, ids: list[str]) -> dict[str, bytes]:
    """The source Panel files (the cards of ``ids``, ``index.json`` and, when present,
    ``meta.json``), each read once, by name."""
    folder = source_dir / PERSONAS_DIR
    files: dict[str, bytes] = {}
    try:
        for name in [*(f"{i}.md" for i in ids), INDEX_FILE, META_FILE]:
            if name == META_FILE and not (folder / name).exists():
                continue
            files[name] = (folder / name).read_bytes()
    except OSError as err:
        raise ConsortiumError(
            "panel_invalid", f"cannot read {PERSONAS_DIR}: {err}", path=PERSONAS_DIR
        ) from err
    return files


def _current(conn: sqlite3.Connection) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    runs = current_runs(conn)
    return runs, [row for run in runs for row in run_results(conn, run["run_id"])]


def _in_use(conn: sqlite3.Connection) -> bool:
    return any_trials(conn) or any_results(conn)


def _from_source[T](source: str, load: Callable[[], T]) -> T:
    """``load()``, with an error's message and path naming the source Study."""
    try:
        return load()
    except ConsortiumError as err:
        path = f"{source}/{err.path}" if err.path else source
        raise ConsortiumError(err.code, f"source {source}: {err.message}", path=path) from err


def _panel_in_use() -> ConsortiumError:
    return ConsortiumError(
        "panel_in_use",
        "board.db already holds Trials or screening results; copy a Panel into a new Study",
        path=DB_FILE,
    )


def _panel_exists() -> ConsortiumError:
    return ConsortiumError("panel_exists", f"{PERSONAS_DIR} already exists", path=PERSONAS_DIR)
