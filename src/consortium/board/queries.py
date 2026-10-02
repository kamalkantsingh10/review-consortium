"""Read-only queries over ``board.db`` for ``status`` (story 1.11) and ``export`` (1.12).

Every function here only reads. Each runs in one read transaction (a single
snapshot) unless the caller already opened one with ``read_transaction``, so
``status`` can read its rows and its footer from the same snapshot while a
dispatcher is writing (WAL).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from decimal import Decimal
from typing import Any

from consortium.board.ledger import (
    committed_usd,
    current_ceiling,
    register_decimal_sum,
    summed,
)
from consortium.board.trials import TRIAL_STATES, chosen_answer, load_trials
from consortium.core.errors import ConsortiumError
from consortium.core.personas import ATTRITION_COUNTS, count_trial


@contextmanager
def read_transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """A deferred ``BEGIN`` ... ``COMMIT`` (one snapshot); reuses an open transaction.

    For read-only connections (``board.db.read_only``): it groups reads into one
    snapshot and never writes. On an exception it rolls back (and re-raises);
    it commits only when the block succeeds, and a failing ``COMMIT`` propagates.
    """
    if conn.in_transaction:
        yield conn
        return
    conn.execute("BEGIN")
    try:
        yield conn
    except BaseException:
        if conn.in_transaction:
            with suppress(sqlite3.Error):
                conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def registered_tests(conn: sqlite3.Connection, test: str | None = None) -> list[str]:
    """Names of the registered Tests (only ``test``, if given and registered), by name."""
    if test is None:
        return [n for (n,) in conn.execute("SELECT name FROM tests ORDER BY name")]
    return [n for (n,) in conn.execute("SELECT name FROM tests WHERE name = ?", (test,))]


def is_registered(conn: sqlite3.Connection, test: str) -> bool:
    """Whether ``test`` is a registered Test."""
    return conn.execute("SELECT 1 FROM tests WHERE name = ?", (test,)).fetchone() is not None


def status_counts(conn: sqlite3.Connection, test: str | None = None) -> list[dict[str, Any]]:
    """Per ``(test, model, agent)``: Trial counts by state, ``retried`` and committed cost.

    Each row is ``{"test", "model", "agent", <one count per Trial state>, "trials",
    "retried", "cost_usd"}``: ``retried`` = Σ max(attempt − 1, 0) over the
    group's Trials (attempts beyond the first); ``cost_usd`` (``Decimal``) = Σ over
    the group's ledger rows of the actual cost, or the reservation where it is
    unknown. ``test`` limits the rows to one Test. Rows are ordered by Test,
    Model, Agent (SQL text order).
    """
    args: tuple[str, ...] = (test,) if test is not None else ()
    where = "WHERE test = ?" if test is not None else ""
    ledger_where = "WHERE t.test = ?" if test is not None else ""
    with read_transaction(conn):
        register_decimal_sum(conn)
        counts = conn.execute(
            "SELECT test, model_id, agent_id, "
            + ", ".join(f"sum(state = '{s}')" for s in TRIAL_STATES)
            + ", count(*), sum(max(attempt - 1, 0))"
            f" FROM trials {where} GROUP BY test, model_id, agent_id"
            " ORDER BY test, model_id, agent_id",
            args,
        ).fetchall()
        costs = {
            (t, m, a): summed(total)
            for t, m, a, total in conn.execute(
                "SELECT t.test, t.model_id, t.agent_id,"
                " decimal_sum(coalesce(l.actual_usd, l.reserved_usd))"
                " FROM ledger l JOIN trials t ON t.trial_id = l.trial_id"
                f" {ledger_where}"
                " GROUP BY t.test, t.model_id, t.agent_id",
                args,
            )
        }
    out = []
    for row in counts:
        key = tuple(row[:3])
        out.append({
            "test": row[0], "model": row[1], "agent": row[2],
            **dict(zip(TRIAL_STATES, row[3:3 + len(TRIAL_STATES)], strict=True)),
            "trials": row[3 + len(TRIAL_STATES)],
            "retried": row[4 + len(TRIAL_STATES)],
            "cost_usd": costs.get(key, Decimal(0)),
        })
    return out


def persona_counts(
    conn: sqlite3.Connection, test: str | None = None
) -> dict[str, dict[str, dict[str, int]]]:
    """Per Test, per Persona: ``core.personas.ATTRITION_COUNTS`` Trial counts (story 2.1).

    Returns ``{test: {persona_id: counts}}``; Tests by name, Personas in natural ID
    order (``p2`` before ``p10``). A ``failed`` Trial is split by its last attempt's
    category (``core.personas.count_trial``). ``test`` limits it to one Test.
    """
    args: tuple[str, ...] = (test,) if test is not None else ()
    where = "WHERE t.test = ?" if test is not None else ""
    out: dict[str, dict[str, dict[str, int]]] = {}
    with read_transaction(conn):
        rows = conn.execute(
            "SELECT t.test, t.persona_id, t.state, a.category FROM trials t"
            " LEFT JOIN attempts a ON a.trial_id = t.trial_id AND a.attempt = t.attempt"
            f" {where}",
            args,
        ).fetchall()
    for name, persona_id, state, category in sorted(
        rows, key=lambda r: (r[0], _persona_order(r[1]))
    ):
        acc = out.setdefault(name, {}).setdefault(
            persona_id, dict.fromkeys(ATTRITION_COUNTS, 0))
        count_trial(acc, state, category)
    return out


def _persona_order(persona_id: str) -> tuple:
    digits = persona_id[1:]
    return (0, int(digits), "") if digits.isdigit() else (1, 0, persona_id)


def cost_footer(conn: sqlite3.Connection) -> dict[str, Any]:
    """Study-wide ``{"committed": Decimal, "ceiling": Decimal | None, "paused": [(test, reason)]}``.

    ``committed`` is ``ledger.committed_usd`` (actual, else reserved), ``ceiling``
    the latest logged ceiling (``None`` if never set), ``paused`` every Test with a
    ``tests.paused_reason``, by Test name (empty when none is paused).
    """
    with read_transaction(conn):
        committed = committed_usd(conn)
        ceiling = current_ceiling(conn)
        paused = [
            (name, reason)
            for name, reason in conn.execute(
                "SELECT name, paused_reason FROM tests WHERE paused_reason IS NOT NULL"
                " ORDER BY name"
            )
        ]
    return {"committed": committed, "ceiling": ceiling, "paused": paused}


def export_trials(conn: sqlite3.Connection, test: str) -> list[dict[str, Any]]:
    """Every Trial of ``test`` with its state and exported attempt, for ``export`` (story 1.12).

    Each row is a ``trials.load_trials`` row (``clip_ids`` a tuple, plus ``state``
    and ``attempt``) and:

    - ``answers``: the parsed ``{item_id: value}`` of the highest valid attempt
      (``trials.chosen_answer``) for a ``valid`` Trial, else ``None``;
    - ``exported_attempt``: that attempt for a ``valid`` Trial, else the last
      attempt (``attempt``; 0 when none was ever started);
    - ``seed``, ``timestamp``, ``category``: the exported attempt's seed, ``answered_at``
      (falling back to ``sent_at``) and category; ``None`` when no attempt was started.
      ``board_unreadable`` when ``attempt`` > 0 has no ``attempts`` row.

    Rows are in plan order; one read transaction.
    """
    with read_transaction(conn):
        rows = load_trials(conn, test)
        out = []
        for row in rows:
            chosen = chosen_answer(conn, row["trial_id"]) if row["state"] == "valid" else None
            exported = chosen["attempt"] if chosen is not None else row["attempt"]
            found = conn.execute(
                "SELECT seed, coalesce(answered_at, sent_at), category FROM attempts"
                " WHERE trial_id = ? AND attempt = ?",
                (row["trial_id"], exported),
            ).fetchone()
            if found is None and exported > 0:
                raise ConsortiumError(
                    "board_unreadable",
                    f"Trial {row['trial_id']}: attempt {exported} has no attempts row",
                )
            seed, timestamp, category = found if found is not None else (None, None, None)
            out.append({
                **row,
                "answers": chosen["answers"] if chosen is not None else None,
                "exported_attempt": exported,
                "seed": seed,
                "timestamp": timestamp,
                "category": category,
            })
    return out
