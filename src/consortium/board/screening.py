"""SQL for screening runs and results (m6, story 3.1).

A screening run ``s<n>`` is also a ``tests`` row (kind ``screening``, not openable)
that owns its Trials, so the ledger, status, pause and resume work unchanged. Its
stamps (per-Model ``settings_hash`` and the ``instrument_hash``) are recorded when it
is created. Results are only ever inserted, never deleted or overwritten.

Supersession is per result key ``(instrument, model_id, agent_id)``: the current
result of a key is the one from the highest-numbered complete run that has the key.
A complete run is marked ``superseded_by`` a newer run only once every one of its keys
is covered by later complete runs. An ``abandoned`` run is never scored.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Mapping
from typing import Any

from consortium.board.db import transaction
from consortium.board.tests import insert_test
from consortium.board.trials import insert_trials
from consortium.core.clock import utc_now_ms
from consortium.core.errors import ConsortiumError
from consortium.core.ids import SCREENING_RUN_ID_PATTERN, run_number
from consortium.core.plan import Trial

RUN_ID_PATTERN = SCREENING_RUN_ID_PATTERN
SCREENING_KIND = "screening"
_RUN_COLUMNS = (
    "run_id", "kind", "screening_test", "started_at", "status", "superseded_by",
    "settings_hashes", "instrument_hash",
)
RESULT_COLUMNS = (
    "run_id", "model_id", "agent_id", "instrument", "outcome", "score", "threshold",
    "settings_hash", "instrument_hash", "detail", "pair_checks", "source_study", "source_hash",
)



def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone() is not None


def any_runs(conn: sqlite3.Connection) -> bool:
    """Whether any screening run exists; False on a layout without the table."""
    if not _has_table(conn, "screening_runs"):
        return False
    return conn.execute("SELECT 1 FROM screening_runs LIMIT 1").fetchone() is not None


def any_results(conn: sqlite3.Connection, instrument: str | None = None) -> bool:
    """Whether any screening result (of ``instrument`` when given, e.g. ``fidelity``)
    exists, imported or not; False on a layout without the table."""
    if not _has_table(conn, "screening_results"):
        return False
    return conn.execute(
        "SELECT 1 FROM screening_results WHERE ? IS NULL OR instrument = ? LIMIT 1",
        (instrument, instrument),
    ).fetchone() is not None


def current_runs(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every complete, not superseded run in run-number order, each column as stored
    (``settings_hashes`` stays canonical JSON); empty on a layout without the table
    (story 3.4: what ``panel copy`` imports)."""
    if not _has_table(conn, "screening_runs"):
        return []
    rows = [
        dict(zip(_RUN_COLUMNS, r, strict=True))
        for r in conn.execute(
            f"SELECT {', '.join(_RUN_COLUMNS)} FROM screening_runs"
            " WHERE status = 'complete' AND superseded_by IS NULL"
        )
    ]
    return sorted(rows, key=lambda r: run_number(r["run_id"]))


def import_runs(
    conn: sqlite3.Connection,
    runs: Iterable[Mapping[str, Any]],
    results: Iterable[Mapping[str, Any]],
    source_study: str,
    source_hash: str,
) -> None:
    """Insert ``runs`` (every run column, as stored) and ``results`` as given, each
    result stamped with ``source_study`` and ``source_hash`` (story 3.4), inside the
    caller's transaction. No ``tests`` row and no Trials: imported runs are results only."""
    conn.executemany(
        f"INSERT INTO screening_runs ({', '.join(_RUN_COLUMNS)})"
        f" VALUES ({', '.join('?' * len(_RUN_COLUMNS))})",
        [tuple(run[c] for c in _RUN_COLUMNS) for run in runs],
    )
    for row in results:
        record_result(
            conn, row["run_id"], dict(row, source_study=source_study, source_hash=source_hash)
        )


def next_run_id(conn: sqlite3.Connection) -> str:
    """``s<n>`` with n = 1 + the highest run number in use (runs and ``tests`` names)."""
    names = [r[0] for r in conn.execute("SELECT name FROM tests")] if _has_table(
        conn, "tests") else []
    if _has_table(conn, "screening_runs"):
        names += [r[0] for r in conn.execute("SELECT run_id FROM screening_runs")]
    return f"s{max((run_number(n) for n in names), default=0) + 1}"


def _run_row(raw: tuple) -> dict[str, Any]:
    row = dict(zip(_RUN_COLUMNS, raw, strict=True))
    row["settings_hashes"] = json.loads(row["settings_hashes"])
    return row


def get_run(conn: sqlite3.Connection, run_id: str) -> dict[str, Any] | None:
    """The run row (``settings_hashes`` parsed to a dict); None if absent."""
    row = conn.execute(
        f"SELECT {', '.join(_RUN_COLUMNS)} FROM screening_runs WHERE run_id = ?", (run_id,)
    ).fetchone()
    return _run_row(row) if row else None


def list_runs(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every run, in run-number order."""
    rows = [_run_row(r)
            for r in conn.execute(f"SELECT {', '.join(_RUN_COLUMNS)} FROM screening_runs")]
    return sorted(rows, key=lambda r: run_number(r["run_id"]))


def open_run(
    conn: sqlite3.Connection, kind: str, screening_test: str | None = None
) -> dict[str, Any] | None:
    """The open (not yet complete) run of ``kind`` (and ``screening_test``); None if none."""
    if not _has_table(conn, "screening_runs"):
        return None
    found = [r for r in list_runs(conn) if r["kind"] == kind and r["status"] == "open"
             and r["screening_test"] == screening_test]
    return found[-1] if found else None


def new_run(
    conn: sqlite3.Connection,
    run_id: str,
    kind: str,
    screening_test: str | None,
    path: str,
    sha256: str,
    trials: Iterable[Trial],
    settings_hashes: Mapping[str, str],
    *,
    clips: Iterable[tuple[str, str]] = (),
    instrument_hash: str | None = None,
) -> int:
    """In one transaction: the ``screening_runs`` row (``open``, with its stamps
    ``settings_hashes`` and ``instrument_hash``, default ``sha256``), its ``tests`` row
    (kind ``screening``, not openable, ``path``, ``sha256`` and the ``(clip_id, role)``
    rows ``clips``: a perception run copies its screening Test's) and its Trials
    (``planned``).

    Refuses (nothing written) with ``test_changed`` when ``run_id`` is no longer the next
    run ID and ``screening_run_open`` when a run of ``kind`` is still open. Returns the
    number of Trials inserted.
    """
    with transaction(conn):
        if next_run_id(conn) != run_id:
            raise ConsortiumError(
                "test_changed", f"screening run {run_id} was taken since the confirmation"
            )
        running = open_run(conn, kind, screening_test)
        if running is not None:
            raise run_open_error(running["run_id"])
        conn.execute(
            f"INSERT INTO screening_runs ({', '.join(_RUN_COLUMNS)})"
            f" VALUES ({', '.join('?' * len(_RUN_COLUMNS))})",
            (run_id, kind, screening_test, utc_now_ms(), "open", None,
             json.dumps(dict(settings_hashes), sort_keys=True, separators=(",", ":")),
             sha256 if instrument_hash is None else instrument_hash),
        )
        insert_test(
            conn,
            {"name": run_id, "kind": SCREENING_KIND, "path": path, "sha256": sha256,
             "openable": False},
            list(clips),
        )
        return insert_trials(conn, run_id, trials)


def run_open_error(run_id: str) -> ConsortiumError:
    return ConsortiumError(
        "screening_run_open",
        f"screening run {run_id} is not complete; continue it with --resume",
    )


def record_result(conn: sqlite3.Connection, run_id: str, row: Mapping[str, Any]) -> None:
    """Insert one result row of ``run_id`` (inside the caller's transaction)."""
    values = {name: row.get(name) for name in RESULT_COLUMNS} | {"run_id": run_id}
    conn.execute(
        f"INSERT INTO screening_results ({', '.join(RESULT_COLUMNS)})"
        f" VALUES ({', '.join('?' * len(RESULT_COLUMNS))})",
        tuple(values[name] for name in RESULT_COLUMNS),
    )


def abandon_run(conn: sqlite3.Connection, run_id: str) -> None:
    """Mark the open run ``abandoned`` (no results; its Trials and Archive lines are kept).
    ``screening_not_open`` when it is absent or not open."""
    with transaction(conn):
        run = get_run(conn, run_id)
        if run is None or run["status"] != "open":
            raise ConsortiumError("screening_not_open", f"screening run {run_id} is not open")
        conn.execute("UPDATE screening_runs SET status = 'abandoned' WHERE run_id = ?", (run_id,))


def _key(row: Mapping[str, Any]) -> tuple:
    return (row["instrument"], row["model_id"], row["agent_id"])


def complete_run(
    conn: sqlite3.Connection, run_id: str, results: Iterable[Mapping[str, Any]]
) -> list[str]:
    """In one transaction: insert ``results``, set the run ``complete`` and mark each
    earlier complete, not yet superseded run of its kind (and screening Test) whose every
    result key is now covered by later complete runs ``superseded_by = run_id``. Returns
    those run IDs. ``screening_not_open`` when the run is absent or not open (nothing
    written)."""
    with transaction(conn):
        run = get_run(conn, run_id)
        if run is None or run["status"] != "open":
            raise ConsortiumError("screening_not_open", f"screening run {run_id} is not open")
        for row in results:
            record_result(conn, run_id, row)
        conn.execute("UPDATE screening_runs SET status = 'complete' WHERE run_id = ?", (run_id,))
        same = [
            r for r in list_runs(conn)
            if r["kind"] == run["kind"] and r["screening_test"] == run["screening_test"]
            and r["status"] == "complete"
        ]
        keys = {r["run_id"]: {_key(x) for x in run_results(conn, r["run_id"])} for r in same}
        older = []
        for r in same:
            n = run_number(r["run_id"])
            if r["superseded_by"] is not None or n >= run_number(run_id):
                continue
            later: set[tuple] = set()
            for other in same:
                if run_number(other["run_id"]) > n:
                    later |= keys[other["run_id"]]
            if keys[r["run_id"]] <= later:
                older.append(r["run_id"])
        conn.executemany(
            "UPDATE screening_runs SET superseded_by = ? WHERE run_id = ?",
            [(run_id, old) for old in older],
        )
        return older


def run_results(conn: sqlite3.Connection, run_id: str) -> list[dict[str, Any]]:
    """Every result row of ``run_id``, in insertion order."""
    return [
        dict(zip(RESULT_COLUMNS, r, strict=True))
        for r in conn.execute(
            f"SELECT {', '.join(RESULT_COLUMNS)} FROM screening_results WHERE run_id = ?"
            " ORDER BY rowid",
            (run_id,),
        )
    ]


def complete_results(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every result row of every complete run (both kinds, superseded runs included), with
    its run's ``screening_test``, in run-number then insertion order. The eligibility gate
    (story 3.3) reads these so that the latest row whose stamps still match decides, even
    when a newer, stale row of the same key exists."""
    if not _has_table(conn, "screening_results"):
        return []
    rows = conn.execute(
        f"SELECT {', '.join('r.' + c for c in RESULT_COLUMNS)}, s.screening_test"
        " FROM screening_results r"
        " JOIN screening_runs s ON s.run_id = r.run_id"
        " WHERE s.status = 'complete' ORDER BY r.rowid"
    ).fetchall()
    out = [dict(zip(RESULT_COLUMNS, raw[:-1], strict=True)) | {"screening_test": raw[-1]}
           for raw in rows]
    return sorted(out, key=lambda r: run_number(r["run_id"]))


def complete_run_ids(conn: sqlite3.Connection) -> list[str]:
    """The IDs of every complete screening run, in run-number order (empty without the
    table). Open and abandoned runs are left out."""
    if not _has_table(conn, "screening_runs"):
        return []
    return [r["run_id"] for r in list_runs(conn) if r["status"] == "complete"]


def current_results(conn: sqlite3.Connection, kind: str | None = None) -> list[dict[str, Any]]:
    """Per result key ``(screening_test, instrument, model_id, agent_id)``, the row from
    the highest-numbered complete run (of ``kind`` when given) that has that key. A
    perception result is current per screening Test (story 3.2): runs of different
    screening Tests never hide each other's results; fidelity runs have no screening Test.

    Rows are in run-number, then insertion order.
    """
    if not _has_table(conn, "screening_results"):
        return []
    rows = conn.execute(
        f"SELECT {', '.join('r.' + c for c in RESULT_COLUMNS)}, s.screening_test"
        " FROM screening_results r"
        " JOIN screening_runs s ON s.run_id = r.run_id"
        " WHERE s.status = 'complete'"
        + (" AND s.kind = ?" if kind is not None else "")
        + " ORDER BY r.rowid",
        (kind,) if kind is not None else (),
    ).fetchall()
    best: dict[tuple, dict[str, Any]] = {}
    for raw in rows:
        row = dict(zip(RESULT_COLUMNS, raw[:-1], strict=True))
        key = (raw[-1], *_key(row))
        held = best.get(key)
        if held is None or run_number(row["run_id"]) > run_number(held["run_id"]):
            best[key] = row
    return sorted(best.values(), key=lambda r: run_number(r["run_id"]))


_EXCLUSION_COLUMNS = ("agent_id", "persona_id", "model_id", "instrument", "reason")
_STAMP_COLUMNS = ("model_id", "instrument", "settings_hash", "instrument_hash", "runs")


def record_gate(
    conn: sqlite3.Connection,
    test: str,
    excluded: Iterable[Mapping[str, Any]],
    stamps: Iterable[Mapping[str, Any]],
) -> None:
    """Store a gated Test's eligibility decision (story 3.3, m7), inside the caller's
    transaction (the Test's ``insert_plan``): one ``screening_exclusions`` row per
    excluded Agent (``agent_id``, ``persona_id``, ``model_id``, ``instrument``,
    ``reason``) and one ``screening_stamps`` row per planned ``(model_id, instrument)``,
    including ``instrument = 'fidelity'`` (``settings_hash``, ``instrument_hash``, ``runs``:
    the screening run IDs whose results decided, a list stored as canonical JSON)."""
    conn.executemany(
        f"INSERT INTO screening_exclusions (test, {', '.join(_EXCLUSION_COLUMNS)})"
        f" VALUES (?, {', '.join('?' * len(_EXCLUSION_COLUMNS))})",
        [(test, *(row[c] for c in _EXCLUSION_COLUMNS)) for row in excluded],
    )
    conn.executemany(
        f"INSERT INTO screening_stamps (test, {', '.join(_STAMP_COLUMNS)})"
        f" VALUES (?, {', '.join('?' * len(_STAMP_COLUMNS))})",
        [(test, row["model_id"], row["instrument"], row["settings_hash"],
          row["instrument_hash"],
          json.dumps(list(row["runs"]), separators=(",", ":"))) for row in stamps],
    )


def insert_gated_plan(
    conn: sqlite3.Connection,
    test: str,
    trials: Iterable[Trial],
    excluded: Iterable[Mapping[str, Any]],
    stamps: Iterable[Mapping[str, Any]],
) -> int:
    """``board.trials.insert_plan`` for a gated Test (story 3.3): in one transaction, the
    Trials (``planned``) and ``record_gate``. Raises ``test_already_open`` (nothing
    written) if ``test`` already has Trials. Returns the number of Trials inserted."""
    with transaction(conn):
        count = insert_trials(conn, test, trials)
        record_gate(conn, test, excluded, stamps)
        return count


def test_stamps(conn: sqlite3.Connection, test: str) -> dict[tuple[str, str], tuple[str, str]]:
    """``{(model_id, instrument): (settings_hash, instrument_hash)}`` recorded when the
    gated Test ``test`` was opened (``instrument`` ``fidelity``: the fidelity stamp); empty
    for an ungated Test (or a layout before m7)."""
    if not _has_table(conn, "screening_stamps"):
        return {}
    return {
        (r[0], r[1]): (r[2], r[3])
        for r in conn.execute(
            "SELECT model_id, instrument, settings_hash, instrument_hash"
            " FROM screening_stamps WHERE test = ?"
            " ORDER BY rowid",
            (test,),
        )
    }


def test_exclusions(conn: sqlite3.Connection, test: str) -> list[dict[str, Any]]:
    """The ``screening_exclusions`` rows of ``test`` in insertion order (empty before m7)."""
    if not _has_table(conn, "screening_exclusions"):
        return []
    return [
        dict(zip(_EXCLUSION_COLUMNS, r, strict=True))
        for r in conn.execute(
            f"SELECT {', '.join(_EXCLUSION_COLUMNS)} FROM screening_exclusions WHERE test = ?"
            " ORDER BY rowid",
            (test,),
        )
    ]


test_stamps.__test__ = False  # type: ignore[attr-defined]  # not a pytest test when imported
test_exclusions.__test__ = False  # type: ignore[attr-defined]
