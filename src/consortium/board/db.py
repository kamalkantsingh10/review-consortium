"""board.db: the only mutable Study state (SQLite, WAL). All SQL lives in ``board/``.

Migrations are applied in order and tracked with ``PRAGMA user_version``.
Each story appends its own migration; never edit an earlier one.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from pathlib import Path

from consortium.core.errors import ConsortiumError

DB_FILE = "board.db"


def m1_clips(conn: sqlite3.Connection) -> None:
    """Story 1.4: neutral Clip facts only. No Condition, no source name, no source hash."""
    conn.execute(
        """
        CREATE TABLE clips (
            clip_id       TEXT PRIMARY KEY,
            sha256        TEXT NOT NULL,
            duration_s    REAL NOT NULL,
            size_bytes    INTEGER NOT NULL,
            width         INTEGER NOT NULL,
            height        INTEGER NOT NULL,
            fps           REAL NOT NULL,
            loudness_lufs REAL,
            pushed_at     TEXT NOT NULL
        )
        """
    )


MIGRATIONS: list[Callable[[sqlite3.Connection], None]] = [m1_clips]


def _is_busy(err: BaseException) -> bool:
    if not isinstance(err, sqlite3.OperationalError):
        return False
    text = str(err).lower()
    return "locked" in text or "busy" in text


def _busy_error(err: BaseException) -> ConsortiumError:
    return ConsortiumError("board_busy", "board.db is locked by another process; try again")


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """``BEGIN IMMEDIATE`` ... ``COMMIT``; rolls back on any exception.

    A busy or locked database raises ``board_busy``.
    """
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as err:
        if _is_busy(err):
            raise _busy_error(err) from err
        raise
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException as err:
        if conn.in_transaction:
            with suppress(sqlite3.Error):
                conn.execute("ROLLBACK")
        if _is_busy(err):
            raise _busy_error(err) from err
        raise


def _migrate(conn: sqlite3.Connection) -> None:
    with transaction(conn):
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version > len(MIGRATIONS):
            raise ConsortiumError(
                "board_version_mismatch",
                f"board.db is at version {version}; this consortium knows up to "
                f"{len(MIGRATIONS)}. Upgrade consortium.",
                path=DB_FILE,
            )
        for index in range(version, len(MIGRATIONS)):
            MIGRATIONS[index](conn)
            conn.execute(f"PRAGMA user_version = {index + 1}")


def connect(study_dir: Path | str) -> sqlite3.Connection:
    """Open (creating if needed) ``<study>/board.db`` in WAL mode, migrated to the latest version.

    The connection is in autocommit mode; group writes with ``transaction``.
    Raises ``board_busy``, ``board_version_mismatch`` or ``board_wal_unavailable``.
    """
    conn = sqlite3.connect(Path(study_dir) / DB_FILE, isolation_level=None)
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        if str(mode).lower() != "wal":
            raise ConsortiumError(
                "board_wal_unavailable",
                f"board.db could not be put in WAL mode (got {mode})",
                path=DB_FILE,
            )
        conn.execute("PRAGMA foreign_keys=ON")
        _migrate(conn)
    except sqlite3.OperationalError as err:
        conn.close()
        if _is_busy(err):
            raise _busy_error(err) from err
        raise
    except BaseException:
        conn.close()
        raise
    return conn
