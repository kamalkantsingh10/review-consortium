"""board.db: the only mutable Study state (SQLite, WAL). All SQL lives in ``board/``.

Migrations are applied in order and tracked with ``PRAGMA user_version``.
Each story appends its own migration; never edit an earlier one.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Literal, overload

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


def m2_tests(conn: sqlite3.Connection) -> None:
    """Story 1.5: registered Tests and the Clips each one uses (target or practice)."""
    conn.execute(
        """
        CREATE TABLE tests (
            name          TEXT PRIMARY KEY,
            kind          TEXT NOT NULL CHECK (kind IN ('pilot', 'screening', 'main')),
            path          TEXT NOT NULL,
            sha256        TEXT NOT NULL,
            openable      INTEGER NOT NULL CHECK (openable IN (0, 1)),
            registered_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE test_clips (
            test    TEXT NOT NULL REFERENCES tests(name),
            clip_id TEXT NOT NULL REFERENCES clips(clip_id),
            role    TEXT NOT NULL CHECK (role IN ('target', 'practice')),
            PRIMARY KEY (test, clip_id)
        )
        """
    )
    conn.execute("CREATE INDEX test_clips_by_clip ON test_clips (clip_id, role)")


def m3_trials(conn: sqlite3.Connection) -> None:
    """Story 1.7: planned Trials (every ``core.plan.Trial`` field) and their attempts."""
    conn.execute(
        """
        CREATE TABLE trials (
            trial_id       TEXT PRIMARY KEY,
            test           TEXT NOT NULL REFERENCES tests(name),
            session_id     TEXT NOT NULL,
            trial_index    INTEGER NOT NULL,
            instrument     TEXT NOT NULL,
            clip_ids       TEXT NOT NULL,
            pair_id        TEXT,
            position       INTEGER,
            prompt_variant TEXT NOT NULL,
            order_seed     INTEGER NOT NULL,
            repeat         INTEGER NOT NULL,
            agent_id       TEXT NOT NULL,
            persona_id     TEXT NOT NULL,
            model_id       TEXT NOT NULL,
            state          TEXT NOT NULL DEFAULT 'planned' CHECK (state IN
                               ('planned', 'sent', 'valid', 'invalid', 'refused', 'failed')),
            attempt        INTEGER NOT NULL DEFAULT 0,
            seq            INTEGER NOT NULL,
            UNIQUE (session_id, trial_index)
        )
        """
    )
    conn.execute("CREATE INDEX trials_by_test ON trials (test, seq)")
    conn.execute(
        """
        CREATE TABLE attempts (
            trial_id    TEXT NOT NULL REFERENCES trials(trial_id),
            attempt     INTEGER NOT NULL CHECK (attempt >= 1),
            seed        INTEGER NOT NULL,
            handle      TEXT,
            sent_at     TEXT,
            answered_at TEXT,
            category    TEXT,
            PRIMARY KEY (trial_id, attempt)
        )
        """
    )
    conn.execute("CREATE INDEX attempts_by_trial ON attempts (trial_id)")


def m4_cost(conn: sqlite3.Connection) -> None:
    """Story 1.9: the per-attempt cost ledger, the ceiling change log and a Test's pause.

    USD amounts are decimal strings. ``ceiling_changes`` rows are in insertion
    (rowid) order; the latest is the current ceiling.
    """
    conn.execute(
        """
        CREATE TABLE ledger (
            trial_id     TEXT NOT NULL REFERENCES trials(trial_id),
            attempt      INTEGER NOT NULL CHECK (attempt >= 1),
            model_id     TEXT NOT NULL,
            reserved_usd TEXT NOT NULL,
            actual_usd   TEXT,
            PRIMARY KEY (trial_id, attempt)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE ceiling_changes (
            ts           TEXT NOT NULL,
            previous_usd TEXT,
            ceiling_usd  TEXT NOT NULL,
            test         TEXT NOT NULL,
            command      TEXT NOT NULL CHECK (command IN ('run', 'resume'))
        )
        """
    )
    conn.execute("ALTER TABLE tests ADD COLUMN paused_reason TEXT")


def m5_validation(conn: sqlite3.Connection) -> None:
    """Story 1.10: each attempt's validation (``core.validate``).

    ``valid`` is 1 or 0 once the attempt's response was validated, NULL before;
    ``invalid_reason`` is the ``invalid_response`` reason of an invalid attempt;
    ``answer_json`` is the parsed answer of a valid attempt as canonical JSON.
    """
    conn.execute(
        "ALTER TABLE attempts ADD COLUMN valid INTEGER CHECK (valid IS NULL OR valid IN (0, 1))"
    )
    conn.execute("ALTER TABLE attempts ADD COLUMN invalid_reason TEXT")
    conn.execute("ALTER TABLE attempts ADD COLUMN answer_json TEXT")


def m6_screening(conn: sqlite3.Connection) -> None:
    """Story 3.1: screening runs (``s<n>``) and their stamped results; never overwritten.

    A run's Trials belong to the ``tests`` row named after the run (kind ``screening``).
    ``settings_hashes`` (canonical JSON ``{model_id: settings_hash}``) and
    ``instrument_hash`` are the stamps recorded when the run was created.
    ``superseded_by`` names the newer run once every result key of a complete run is
    covered by later complete runs; ``abandoned`` runs are never scored. Result rows are
    only inserted, one per ``(run_id, instrument, model_id, agent_id)``: ``pair_checks``
    is filled by perception screening (3.2), the
    ``source_*`` columns by a Panel copy (3.4). ``IF NOT EXISTS``: a board.db whose
    ``user_version`` was rolled back by hand keeps its screening tables.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS screening_runs (
            run_id         TEXT PRIMARY KEY,
            kind           TEXT NOT NULL CHECK (kind IN ('fidelity', 'perception')),
            screening_test TEXT,
            started_at     TEXT NOT NULL,
            status         TEXT NOT NULL CHECK (status IN ('open', 'complete', 'abandoned')),
            superseded_by  TEXT,
            settings_hashes TEXT NOT NULL,
            instrument_hash TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS screening_results (
            run_id          TEXT NOT NULL REFERENCES screening_runs(run_id),
            model_id        TEXT NOT NULL,
            agent_id        TEXT,
            instrument      TEXT NOT NULL,
            outcome         TEXT NOT NULL CHECK (outcome IN ('pass', 'fail')),
            score           REAL NOT NULL,
            threshold       REAL NOT NULL,
            settings_hash   TEXT NOT NULL,
            instrument_hash TEXT NOT NULL,
            detail          TEXT NOT NULL,
            pair_checks     INTEGER,
            source_study    TEXT,
            source_hash     TEXT
        )
        """
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS screening_results_key"
        " ON screening_results (run_id, instrument, model_id, ifnull(agent_id, ''))"
    )


def m7_screening_gate(conn: sqlite3.Connection) -> None:
    """Story 3.3: what the eligibility gate decided when a gated Test was opened.

    ``screening_exclusions``: one row per excluded Agent with its one reason code
    (``fidelity_fail``, ``fidelity_missing``, ``perception_fail``, ``perception_missing``;
    a stale result refuses the open, so it is never stored) and, for a perception reason,
    the Instrument (NULL for fidelity reasons). Epic 4's Rater-flow report reads it.
    ``screening_stamps``: one row per planned ``(model_id, instrument)``, plus one per
    planned Model with ``instrument = 'fidelity'`` (the fidelity stamp), holding the
    stamps the gate checked and ``runs`` (canonical JSON list of the screening run IDs
    whose results decided); a resume of the Test must still match the stamps. Both are
    written in the Test's ``insert_plan`` transaction.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS screening_exclusions (
            test       TEXT NOT NULL REFERENCES tests(name),
            agent_id   TEXT NOT NULL,
            persona_id TEXT NOT NULL,
            model_id   TEXT NOT NULL,
            instrument TEXT,
            reason     TEXT NOT NULL CHECK (reason IN ('fidelity_fail', 'fidelity_missing',
                           'perception_fail', 'perception_missing')),
            PRIMARY KEY (test, agent_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS screening_stamps (
            test            TEXT NOT NULL REFERENCES tests(name),
            model_id        TEXT NOT NULL,
            instrument      TEXT NOT NULL,
            settings_hash   TEXT NOT NULL,
            instrument_hash TEXT NOT NULL,
            runs            TEXT NOT NULL,
            PRIMARY KEY (test, model_id, instrument)
        )
        """
    )


MIGRATIONS: list[Callable[[sqlite3.Connection], None]] = [
    m1_clips, m2_tests, m3_trials, m4_cost, m5_validation, m6_screening, m7_screening_gate,
]


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


@overload
def connect(study_dir: Path | str, readonly: Literal[False] = False) -> sqlite3.Connection: ...


@overload
def connect(study_dir: Path | str, readonly: Literal[True]) -> sqlite3.Connection | None: ...


@overload
def connect(study_dir: Path | str, readonly: bool) -> sqlite3.Connection | None: ...


def connect(study_dir: Path | str, readonly: bool = False) -> sqlite3.Connection | None:
    """Open (creating if needed) ``<study>/board.db`` in WAL mode, migrated to the latest version.

    The connection is in autocommit mode; group writes with ``transaction``.
    Raises ``board_busy``, ``board_version_mismatch`` or ``board_wal_unavailable``.

    With ``readonly=True`` (the read-only entry point; ``read_only`` wraps it):
    returns ``None`` when ``board.db`` is absent, never creates, migrates or writes
    Study data, and raises ``board_version_mismatch`` unless ``user_version`` equals
    ``len(MIGRATIONS)`` (``board_unreadable`` for a file SQLite cannot read).
    """
    if readonly:
        return _connect_readonly(Path(study_dir))
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


def _unreadable(err: BaseException) -> ConsortiumError:
    if _is_busy(err):
        return _busy_error(err)
    return ConsortiumError("board_unreadable", f"board.db cannot be read: {err}", path=DB_FILE)


def _wal_exists(study: Path) -> bool:
    return (study / f"{DB_FILE}-wal").exists()


def _connect_readonly(
    study: Path, immutable: bool | None = None, allow_older: bool = False
) -> sqlite3.Connection | None:
    """A ``mode=ro`` connection that writes no Study data.

    SQLite creates ``board.db-wal`` and ``board.db-shm`` when it opens a WAL
    database, even read-only. When no ``-wal`` file exists, no other connection
    has the database open, so it is opened ``immutable=1`` (no side files, no
    locks); otherwise the live ``-wal``/``-shm`` of the other connection are used.
    ``immutable`` forces the choice. Use ``read_only`` to also guard against a
    writer that starts after the check. ``allow_older`` accepts a layout older than
    the current one (never migrated; the reader must cope with missing tables).
    Raises ``board_unreadable``, ``board_busy`` or ``board_version_mismatch``.
    """
    db = study / DB_FILE
    if not db.is_file():
        return None
    if immutable is None:
        immutable = not _wal_exists(study)
    query = "mode=ro" + ("&immutable=1" if immutable else "")
    uri = f"{db.resolve().as_uri()}?{query}"
    try:
        conn = sqlite3.connect(uri, uri=True, isolation_level=None)
    except sqlite3.DatabaseError as err:
        raise _unreadable(err) from err
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()  # detects a corrupt file
    except sqlite3.DatabaseError as err:
        conn.close()
        raise _unreadable(err) from err
    except BaseException:
        conn.close()
        raise
    if version > len(MIGRATIONS) or (version < len(MIGRATIONS) and not allow_older):
        conn.close()
        newer = version > len(MIGRATIONS)
        raise ConsortiumError(
            "board_version_mismatch",
            f"board.db is at version {version}; this consortium "
            + (
                f"knows up to {len(MIGRATIONS)}. Upgrade consortium."
                if newer
                else f"needs {len(MIGRATIONS)}. Run a command that writes board.db to migrate it."
            ),
            path=DB_FILE,
        )
    return conn


def read_only[T](
    study_dir: Path | str,
    read: Callable[[sqlite3.Connection], T],
    allow_older: bool = False,
) -> T | None:
    """Run ``read`` on a read-only connection and return its result; None if no ``board.db``.

    ``allow_older`` accepts an older, unmigrated layout (see ``_connect_readonly``).

    If the connection was ``immutable`` and a ``board.db-wal`` appeared during the
    reads (a writer started), the reads are redone with plain ``mode=ro``.
    SQLite errors during the reads become ``board_unreadable`` (or ``board_busy``).
    """
    study = Path(study_dir)
    immutable = not _wal_exists(study)
    for attempt in (immutable, False):
        conn = _connect_readonly(study, immutable=attempt, allow_older=allow_older)
        if conn is None:
            return None
        try:
            result = read(conn)
        except sqlite3.DatabaseError as err:
            raise _unreadable(err) from err
        finally:
            conn.close()
        if not attempt or not _wal_exists(study):
            return result
    raise AssertionError("unreachable")  # pragma: no cover
