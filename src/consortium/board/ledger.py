"""SQL for the cost ledger and the ceiling (m4, story 1.9; AD-9, AD-13).

``ledger`` has one row per ``(trial_id, attempt)``: ``reserved_usd`` is written
by ``trials.begin_attempt`` in the same transaction as the attempt row, and
``actual_usd`` once the attempt's usage is known. Committed spend is the sum
over the whole Study of ``actual_usd``, or ``reserved_usd`` where it is NULL.
The current ceiling is the latest ``ceiling_changes`` row (none if empty).
USD amounts are stored as decimal strings and summed as ``Decimal``.

Inside a dispatching command the writer keeps a running total in a ``Spend``:
it is read once (one SQL aggregate) on first use, then updated by every
reservation and actual cost, so a reservation never re-sums the ledger.
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal, InvalidOperation

from consortium.board.db import DB_FILE, transaction
from consortium.core.clock import utc_now_ms
from consortium.core.errors import ConsortiumError


def _text(value: Decimal) -> str:
    return format(value, "f")


def _amount(text: object) -> Decimal:
    try:
        value = Decimal(str(text))
    except InvalidOperation as err:
        raise ConsortiumError(
            "board_unreadable", f"ledger amount {text!r} is not a decimal", path=DB_FILE
        ) from err
    if not value.is_finite():
        raise ConsortiumError(
            "board_unreadable", f"ledger amount {text!r} is not a decimal", path=DB_FILE
        )
    return value


class _DecimalSum:
    """SQLite aggregate summing decimal strings exactly (as text)."""

    def __init__(self) -> None:
        self.total = Decimal(0)
        self.bad: object | None = None

    def step(self, value: object) -> None:
        if value is None or self.bad is not None:
            return
        try:
            self.total += _amount(value)
        except ConsortiumError:
            self.bad = value

    def finalize(self) -> str:
        return f"bad:{self.bad}" if self.bad is not None else _text(self.total)


def register_decimal_sum(conn: sqlite3.Connection) -> None:
    """Register the ``decimal_sum(text)`` aggregate on ``conn`` (read it with ``summed``)."""
    conn.create_aggregate("decimal_sum", 1, _DecimalSum)


def summed(total: str | None) -> Decimal:
    """A ``decimal_sum`` result as a ``Decimal`` (0 for no rows); ``board_unreadable`` if bad."""
    if total is None:  # no rows
        return Decimal(0)
    if total.startswith("bad:"):
        _amount(total[4:])  # raises board_unreadable
    return _amount(total)


def committed_usd(conn: sqlite3.Connection) -> Decimal:
    """Study-wide committed spend: Σ(actual, or reserved where actual is NULL).

    A ledger amount that is not a decimal raises ``board_unreadable``.
    """
    register_decimal_sum(conn)
    (total,) = conn.execute(
        "SELECT decimal_sum(coalesce(actual_usd, reserved_usd)) FROM ledger"
    ).fetchone()
    return summed(total)


class Spend:
    """The writer's running committed total (``committed`` is read lazily, once)."""

    def __init__(self) -> None:
        self._committed: Decimal | None = None

    def committed(self, conn: sqlite3.Connection) -> Decimal:
        if self._committed is None:
            self._committed = committed_usd(conn)
        return self._committed

    def add(self, conn: sqlite3.Connection, delta: Decimal) -> None:
        self._committed = self.committed(conn) + delta


def current_ceiling(conn: sqlite3.Connection) -> Decimal | None:
    """The latest ceiling set with ``open --ceiling``; ``None`` if none was ever set."""
    row = conn.execute(
        "SELECT ceiling_usd FROM ceiling_changes ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    return _amount(row[0]) if row else None


def set_ceiling(
    conn: sqlite3.Connection, ceiling: Decimal, test: str, command: str
) -> Decimal | None:
    """Append a ceiling change (UTC timestamp, previous, new, Test, ``run``/``resume``).

    Returns the previous ceiling.
    """
    if command not in ("run", "resume"):
        raise ValueError(f"unknown command {command!r}")
    with transaction(conn):
        previous = current_ceiling(conn)
        conn.execute(
            "INSERT INTO ceiling_changes (ts, previous_usd, ceiling_usd, test, command)"
            " VALUES (?, ?, ?, ?, ?)",
            (utc_now_ms(), None if previous is None else _text(previous), _text(ceiling),
             test, command),
        )
    return previous


def reserve(
    conn: sqlite3.Connection, trial_id: str, attempt: int, model_id: str, usd: Decimal
) -> None:
    """Insert the attempt's reservation; call it inside the caller's transaction."""
    conn.execute(
        "INSERT INTO ledger (trial_id, attempt, model_id, reserved_usd) VALUES (?, ?, ?, ?)",
        (trial_id, attempt, model_id, _text(usd)),
    )


def record_actual(
    conn: sqlite3.Connection,
    trial_id: str,
    attempt: int,
    model_id: str,
    usd: Decimal | None,
    reserved: Decimal,
    spend: Spend,
) -> None:
    """Store the attempt's actual cost ``usd`` (``None``: no usage, the reservation stands).

    An attempt with no ledger row (recorded before the ledger existed, on a
    ``board.db`` migrated mid-Run) gets one, reserving ``reserved`` (the
    attempt's estimate). ``spend`` is updated by the change in committed spend.
    """
    before = spend.committed(conn)
    with transaction(conn):
        row = conn.execute(
            "SELECT reserved_usd, actual_usd FROM ledger WHERE trial_id = ? AND attempt = ?",
            (trial_id, attempt),
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO ledger (trial_id, attempt, model_id, reserved_usd, actual_usd)"
                " VALUES (?, ?, ?, ?, ?)",
                (trial_id, attempt, model_id, _text(reserved),
                 None if usd is None else _text(usd)),
            )
            delta = usd if usd is not None else reserved
        else:
            old = _amount(row[1] if row[1] is not None else row[0])
            if usd is None:
                delta = Decimal(0)
            else:
                conn.execute(
                    "UPDATE ledger SET actual_usd = ? WHERE trial_id = ? AND attempt = ?",
                    (_text(usd), trial_id, attempt),
                )
                delta = usd - old
    spend._committed = before + delta
