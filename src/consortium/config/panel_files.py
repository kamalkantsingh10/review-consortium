"""Atomic install of a ``panel/personas/`` folder (``personas generate``, ``panel copy``).

A Panel is written into a ``.personas-new-*`` work folder next to its target and
renamed into place. Stdlib only: no YAML, no Study logic.
"""

from __future__ import annotations

import errno
import os
import shutil
from pathlib import Path

INDEX_FILE = "index.json"
META_FILE = "meta.json"


def write_file(path: Path, data: bytes) -> None:
    with path.open("wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())


def fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def exists(target: Path) -> bool:
    if target.is_dir():
        return any(target.iterdir())
    return target.exists() or target.is_symlink()


def sweep_stale(target: Path) -> None:
    """Remove ``.personas-*`` work folders left behind by a crashed run.

    ``.personas-old-*`` copies are kept while ``panel/personas`` is absent: one may
    hold the only copy of a Panel whose restore failed.
    """
    panel = target.parent
    if not panel.is_dir():
        return
    keep_old = not (target.exists() or target.is_symlink())
    for entry in panel.glob(".personas-*"):
        if keep_old and entry.name.startswith(".personas-old-"):
            continue
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry, ignore_errors=True)


def move_into_place(new: Path, target: Path) -> None:
    """Rename ``new`` to ``target``; ``FileExistsError`` when ``target`` is taken.

    POSIX ``rename`` replaces only an absent target or an empty directory, so a
    Panel that appeared since the first check is never overwritten.
    """
    try:
        os.rename(new, target)
    except OSError as err:
        if err.errno in (errno.ENOTEMPTY, errno.EEXIST, errno.ENOTDIR, errno.EISDIR):
            raise FileExistsError(err.errno, err.strerror, str(target)) from err
        raise
