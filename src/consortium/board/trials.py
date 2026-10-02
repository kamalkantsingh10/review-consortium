"""SQL for the ``trials`` and ``attempts`` tables (m3).

Inside a dispatching command these helpers run only on the writer task
(``board.writer``), which owns the connection.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable

from consortium.board.db import transaction
from consortium.core.clock import utc_now_ms
from consortium.core.errors import ConsortiumError
from consortium.core.plan import Trial
from consortium.core.render import canonical_json
from consortium.core.seeds import derive_seed

TRIAL_STATES = ("planned", "sent", "valid", "invalid", "refused", "failed")
TERMINAL_STATES = ("valid", "invalid", "refused", "failed")
MODEL_PURPOSE = "model"

_TRIAL_COLUMNS = (
    "trial_id", "test", "session_id", "trial_index", "instrument", "clip_ids", "pair_id",
    "position", "prompt_variant", "order_seed", "repeat", "agent_id", "persona_id", "model_id",
)


def count_trials(conn: sqlite3.Connection, test: str) -> int:
    """How many Trials of ``test`` are stored."""
    return conn.execute("SELECT count(*) FROM trials WHERE test = ?", (test,)).fetchone()[0]


def insert_plan(conn: sqlite3.Connection, test: str, trials: Iterable[Trial]) -> int:
    """Insert every Trial of ``test`` as ``planned`` (attempt 0) in one transaction.

    Raises ``test_already_open`` (nothing written) if ``test`` already has Trials.
    Returns the number of Trials inserted.
    """
    with transaction(conn):
        if count_trials(conn, test):
            raise ConsortiumError(
                "test_already_open",
                f"Test {test!r} already has Trials; use --resume to continue it",
            )
        rows = []
        for seq, t in enumerate(trials):
            if t.test != test:
                raise ValueError(f"Trial {t.trial_id} belongs to Test {t.test!r}, not {test!r}")
            rows.append((
                t.trial_id, t.test, t.session_id, t.trial_index, t.instrument,
                canonical_json(list(t.clip_ids)).decode("utf-8"), t.pair_id, t.position,
                t.prompt_variant,
                t.order_seed, t.repeat, t.agent_id, t.persona_id, t.model_id, seq,
            ))
        conn.executemany(
            f"INSERT INTO trials ({', '.join(_TRIAL_COLUMNS)}, seq)"
            f" VALUES ({', '.join('?' * (len(_TRIAL_COLUMNS) + 1))})",
            rows,
        )
    return len(rows)


def load_trials(conn: sqlite3.Connection, test: str) -> list[dict]:
    """Every stored Trial of ``test`` in plan order, with ``state`` and ``attempt``.

    ``clip_ids`` is a tuple; the other columns are as stored.
    """
    cols = (*_TRIAL_COLUMNS, "state", "attempt")
    out = []
    for row in conn.execute(
        f"SELECT {', '.join(cols)} FROM trials WHERE test = ? ORDER BY seq", (test,)
    ):
        d = dict(zip(cols, row, strict=True))
        d["clip_ids"] = tuple(json.loads(d["clip_ids"]))
        out.append(d)
    return out


def trial_from_row(row: dict) -> Trial:
    """The ``core.plan.Trial`` stored in a ``load_trials`` / ``load_resumable`` row."""
    return Trial(**{name: row[name] for name in _TRIAL_COLUMNS})


def load_resumable(conn: sqlite3.Connection, test: str) -> list[dict]:
    """Every non-terminal Trial of ``test`` in plan order, for resume (story 1.8).

    Each row is a ``load_trials`` row plus ``handle``: the stored handle (JSON
    text) of the Trial's latest attempt (``attempt``) if that attempt was marked
    ``sent`` and has one, else ``None``. A ``planned`` Trial's attempt, never
    marked ``sent``, is never collected, so its handle is always ``None``.
    """
    out = []
    for row in load_trials(conn, test):
        if row["state"] in TERMINAL_STATES:
            continue
        handle = None
        if row["state"] == "sent":
            found = conn.execute(
                "SELECT handle FROM attempts WHERE trial_id = ? AND attempt = ?"
                " AND sent_at IS NOT NULL",
                (row["trial_id"], row["attempt"]),
            ).fetchone()
            handle = found[0] if found else None
        out.append({**row, "handle": handle})
    return out


def attempt_seeds(conn: sqlite3.Connection, test: str) -> dict[tuple[str, int], tuple[int, bool]]:
    """``(trial_id, attempt) -> (seed, sent)`` for every recorded attempt of ``test``.

    ``sent`` is true when the attempt was marked ``sent`` (it has ``sent_at``).
    """
    return {
        (trial_id, attempt): (seed, sent_at is not None)
        for trial_id, attempt, seed, sent_at in conn.execute(
            "SELECT a.trial_id, a.attempt, a.seed, a.sent_at FROM attempts a"
            " JOIN trials t ON t.trial_id = a.trial_id WHERE t.test = ?",
            (test,),
        )
    }


def begin_attempt(conn: sqlite3.Connection, trial_id: str, study_seed: int) -> tuple[int, int]:
    """Increment the Trial's ``attempt`` and record the new attempt row, in one transaction.

    The attempt's seed is ``derive_seed(study_seed, "model",
    "<session_id>:<trial_index>:<attempt>")``. Returns ``(attempt, seed)``.
    Refuses a Trial in a terminal state.
    """
    with transaction(conn):
        row = conn.execute(
            "SELECT session_id, trial_index, state, attempt FROM trials WHERE trial_id = ?",
            (trial_id,),
        ).fetchone()
        if row is None:
            raise KeyError(trial_id)
        session_id, trial_index, state, attempt = row
        if state in TERMINAL_STATES:
            raise ValueError(f"Trial {trial_id} is {state}; terminal states never change")
        attempt += 1
        seed = derive_seed(study_seed, MODEL_PURPOSE, f"{session_id}:{trial_index}:{attempt}")
        conn.execute("UPDATE trials SET attempt = ? WHERE trial_id = ?", (attempt, trial_id))
        conn.execute(
            "INSERT INTO attempts (trial_id, attempt, seed) VALUES (?, ?, ?)",
            (trial_id, attempt, seed),
        )
    return attempt, seed


def mark_sent(conn: sqlite3.Connection, trial_id: str, attempt: int) -> None:
    """Set the Trial ``sent`` and stamp the attempt's ``sent_at``."""
    with transaction(conn):
        cur = conn.execute(
            "UPDATE trials SET state = 'sent'"
            " WHERE trial_id = ? AND attempt = ? AND state IN ('planned', 'sent')",
            (trial_id, attempt),
        )
        if cur.rowcount != 1:
            raise ValueError(f"Trial {trial_id} attempt {attempt} cannot be marked sent")
        conn.execute(
            "UPDATE attempts SET sent_at = ? WHERE trial_id = ? AND attempt = ?",
            (utc_now_ms(), trial_id, attempt),
        )


def set_handle(conn: sqlite3.Connection, trial_id: str, attempt: int, handle: str) -> None:
    """Store the Rater's handle (JSON text) for the attempt."""
    with transaction(conn):
        cur = conn.execute(
            "UPDATE attempts SET handle = ? WHERE trial_id = ? AND attempt = ?",
            (handle, trial_id, attempt),
        )
        if cur.rowcount != 1:
            raise KeyError((trial_id, attempt))


def set_state(
    conn: sqlite3.Connection, trial_id: str, attempt: int, state: str, category: str
) -> None:
    """Move the Trial to ``state`` and record the attempt's ``category`` and ``answered_at``.

    Only a non-terminal Trial changes; terminal states never change.
    """
    if state not in TRIAL_STATES:
        raise ValueError(f"unknown Trial state {state!r}")
    with transaction(conn):
        cur = conn.execute(
            "UPDATE trials SET state = ? WHERE trial_id = ? AND attempt = ?"
            f" AND state NOT IN ({', '.join('?' * len(TERMINAL_STATES))})",
            (state, trial_id, attempt, *TERMINAL_STATES),
        )
        if cur.rowcount != 1:
            raise ValueError(f"Trial {trial_id} attempt {attempt} cannot become {state}")
        conn.execute(
            "UPDATE attempts SET category = ?, answered_at = ? WHERE trial_id = ? AND attempt = ?",
            (category, utc_now_ms(), trial_id, attempt),
        )


def state_counts(conn: sqlite3.Connection, test: str) -> dict[str, int]:
    """Trials of ``test`` by state, in lifecycle order; states with no Trial are omitted."""
    found = dict(
        conn.execute("SELECT state, count(*) FROM trials WHERE test = ? GROUP BY state", (test,))
    )
    return {s: found[s] for s in TRIAL_STATES if s in found}
