"""The exclusive ``board.lock`` lease held by a dispatching command (AD-3)."""

from __future__ import annotations

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from consortium.core.errors import ConsortiumError

LOCK_FILE = "board.lock"


@contextmanager
def acquire_lease(study_dir: Path | str) -> Iterator[None]:
    """Hold ``fcntl.flock(LOCK_EX | LOCK_NB)`` on ``<study>/board.lock`` for the block.

    Raises ``study_busy`` when another dispatcher holds it. The OS releases the
    lock when the process dies, so a stale ``board.lock`` file never blocks.
    """
    path = Path(study_dir) / LOCK_FILE
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as err:
        os.close(fd)
        raise ConsortiumError(
            "study_busy", "another consortium command is dispatching in this Study",
            path=LOCK_FILE,
        ) from err
    except BaseException:
        os.close(fd)
        raise
    try:
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
