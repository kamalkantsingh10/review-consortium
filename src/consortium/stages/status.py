"""Use case: ``status [TEST]`` (story 1.11), what a Run has done, failed, retried and cost.

Reads ``board.db`` read-only and lease-free (``board.db.read_only``: ``mode=ro``
over WAL, no migration, no write), so it works while another process holds
``board.lock`` and is dispatching. Rows and footer come from one read
transaction. It never reads the Archive or ``blinding_key.csv`` and shows no
Condition (AD-2).

Rows, per Test (``model`` and ``agent`` = ``*``), per Model (``agent`` = ``*``)
and per Agent: Trial counts by state, ``retried`` (attempts beyond the first,
summed), ``invalid_rate`` (``core.validate.invalid_rate`` of the row's counts)
and ``cost_usd`` (committed spend of the row's ledger rows), plus ``trials``
and, on a Test-total row, ``paused`` (the Test's pause reason). Every registered
Test has a Test-total row, all zeros if it was never opened. The footer is
Study-wide: committed spend, the ceiling and ``state: ok`` or
``state: paused <test> (<reason>)[, ...]`` listing every paused Test.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from consortium.board.db import read_only
from consortium.board.queries import (
    cost_footer,
    read_transaction,
    registered_tests,
    status_counts,
)
from consortium.board.trials import TRIAL_STATES
from consortium.core.cost import usd
from consortium.core.errors import ConsortiumError
from consortium.core.validate import invalid_rate

ALL = "*"
SCHEMA_VERSION = 1
COUNTS = ("trials", *TRIAL_STATES, "retried")
COLUMNS = ("test", "model", "agent", *COUNTS, "invalid_rate", "cost_usd", "paused")


@dataclass(frozen=True)
class StatusReport:
    """``rows`` (one dict per row, keys ``COLUMNS``) and the Study-wide footer.

    ``test`` is the Test filter (``None``: every Test); ``paused`` lists every
    paused Test of the Study as ``(test, reason)``, by Test name.
    """

    rows: list[dict[str, Any]] = field(default_factory=list)
    committed: Decimal = Decimal(0)
    ceiling: Decimal | None = None
    paused: list[tuple[str, str]] = field(default_factory=list)
    test: str | None = None

    @property
    def state(self) -> str:
        return "paused" if self.paused else "ok"

    def footer(self) -> str:
        spend = f"committed {usd(self.committed)} / ceiling {usd(self.ceiling)}"
        if not self.paused:
            return f"{spend}   state: ok"
        listed = ", ".join(f"{test} ({reason})" for test, reason in self.paused)
        return f"{spend}   state: paused {listed}"

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "test": self.test,
            "rows": self.rows,
            "committed": usd(self.committed),
            "ceiling": None if self.ceiling is None else usd(self.ceiling),
            "state": self.state,
            "paused": [{"test": test, "reason": reason} for test, reason in self.paused],
        }


def _natural(text: str) -> tuple:
    if text == ALL:
        return (0,)
    return (1, tuple((0, int(p)) if p.isdigit() else (1, p) for p in re.split(r"(\d+)", text)))


def _row(
    test: str, model: str, agent: str, counts: dict[str, Any], cost: Decimal,
    paused: str | None = None,
) -> dict:
    return {
        "test": test, "model": model, "agent": agent,
        **{name: int(counts[name]) for name in COUNTS},
        "invalid_rate": invalid_rate(counts),
        "cost_usd": usd(cost),
        "paused": paused,
    }


def _zero() -> dict[str, Any]:
    return {name: 0 for name in COUNTS} | {"cost_usd": Decimal(0)}


def _rollup(
    agent_rows: list[dict[str, Any]], tests: list[str], paused: dict[str, str]
) -> list[dict[str, Any]]:
    """Agent rows plus their Model and Test totals, sorted (totals first).

    Every Test in ``tests`` gets a Test-total row (zeros if it has no Trials);
    a Test-total row carries the Test's pause reason from ``paused``.
    """
    totals: dict[tuple[str, str, str], dict[str, Any]] = {
        (name, ALL, ALL): _zero() for name in tests
    }
    out = []
    for r in agent_rows:
        out.append(_row(r["test"], r["model"], r["agent"], r, r["cost_usd"]))
        for key in ((r["test"], r["model"], ALL), (r["test"], ALL, ALL)):
            acc = totals.setdefault(key, _zero())
            for name in COUNTS:
                acc[name] += r[name]
            acc["cost_usd"] += r["cost_usd"]
    out.extend(
        _row(*key, acc, acc["cost_usd"], paused.get(key[0]) if key[1] == ALL else None)
        for key, acc in totals.items()
    )
    return sorted(out, key=lambda r: tuple(_natural(r[c]) for c in ("test", "model", "agent")))


def status(study_dir: Path | str, test: str | None = None) -> StatusReport:
    """The status rows (``test`` only, if given) and the Study-wide footer.

    No ``board.db`` yet: an empty report (``unknown_test`` if ``test`` was given).
    Raises ``unknown_test``, ``board_version_mismatch``, ``board_unreadable`` or
    ``board_busy``.
    """

    def read(conn: sqlite3.Connection) -> tuple[list[str], list[dict], dict]:
        with read_transaction(conn):
            tests = registered_tests(conn, test)
            if test is not None and not tests:
                return [], [], {}
            return tests, status_counts(conn, test), cost_footer(conn)

    found = read_only(study_dir, read)
    if found is None:
        found = ([], [], {"committed": Decimal(0), "ceiling": None, "paused": []})
    tests, agent_rows, footer = found
    if test is not None and not tests:
        raise ConsortiumError("unknown_test", f"{test} is not a registered Test")
    return StatusReport(
        rows=_rollup(agent_rows, tests, dict(footer["paused"])),
        committed=footer["committed"],
        ceiling=footer["ceiling"],
        paused=list(footer["paused"]),
        test=test,
    )


def _cell(row: dict[str, Any], column: str) -> str:
    value = row[column]
    if column == "invalid_rate":
        return "" if value is None else f"{value:.4f}"
    if column == "paused":
        return value or ""
    return str(value)


def format_table(rows: list[dict[str, Any]], footer: str) -> str:
    """A fixed-width table (header, one line per row) and the footer line.

    Text columns are left-aligned, numbers right-aligned; columns are separated
    by two spaces.
    """
    cells = [[_cell(r, c) for c in COLUMNS] for r in rows]
    widths = [max([len(c), *(len(line[i]) for line in cells)]) for i, c in enumerate(COLUMNS)]
    text = {"test", "model", "agent", "paused"}

    def line(values: list[str] | tuple[str, ...]) -> str:
        parts = [
            v.ljust(w) if c in text else v.rjust(w)
            for c, v, w in zip(COLUMNS, values, widths, strict=True)
        ]
        return "  ".join(parts).rstrip()

    return "\n".join([line(COLUMNS), *(line(c) for c in cells), footer])
