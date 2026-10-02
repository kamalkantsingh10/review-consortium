"""Blinding boundary (AD-2).

Only this module reads or writes ``blinding_key.csv``, the one place where
Conditions exist. Only ``consortium.stages.push``, ``consortium.stages.export``
and ``consortium.board`` itself may import it; ``tests/test_architecture.py``
enforces that. Conditions never enter ``board.db``, the Archive, logs or any
request.

The key is long CSV, one row per Clip and factor: ``clip_id,factor,level``.
"""

from __future__ import annotations

import csv
import io
import os
from collections.abc import Mapping
from pathlib import Path

KEY_FILE = "blinding_key.csv"
HEADER = ("clip_id", "factor", "level")


def _key_path(study_dir: Path | str) -> Path:
    return Path(study_dir) / KEY_FILE


def key_mark(study_dir: Path | str) -> int | None:
    """The key file's current size (``None`` if absent), for ``restore_key``."""
    path = _key_path(study_dir)
    return path.stat().st_size if path.exists() else None


def restore_key(study_dir: Path | str, mark: int | None) -> None:
    """Undo appends made since ``key_mark`` returned ``mark``."""
    path = _key_path(study_dir)
    if mark is None:
        path.unlink(missing_ok=True)
    elif path.exists():
        with path.open("r+b") as fh:
            fh.truncate(mark)
            fh.flush()
            os.fsync(fh.fileno())


def append_conditions(study_dir: Path | str, clip_id: str, conditions: Mapping[str, str]) -> None:
    """Append one row per factor for ``clip_id``; write the header if the file is new; fsync.

    No Conditions means no rows (the file is left untouched).
    """
    if not conditions:
        return
    path = _key_path(study_dir)
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    if not path.exists() or path.stat().st_size == 0:
        writer.writerow(HEADER)
    for factor, level in conditions.items():
        writer.writerow((clip_id, factor, level))
    with path.open("a", encoding="utf-8", newline="") as fh:
        fh.write(buf.getvalue())
        fh.flush()
        os.fsync(fh.fileno())


def read_key(study_dir: Path | str) -> dict[str, dict[str, str]]:
    """``{clip_id: {factor: level}}``; empty when the key file does not exist."""
    path = _key_path(study_dir)
    if not path.exists():
        return {}
    key: dict[str, dict[str, str]] = {}
    with path.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            key.setdefault(row["clip_id"], {})[row["factor"]] = row["level"]
    return key
