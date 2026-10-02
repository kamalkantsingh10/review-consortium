"""SQL for the ``trials`` and ``attempts`` tables (m3); ``begin_attempt`` also reserves (m4).

Inside a dispatching command these helpers run only on the writer task
(``board.writer``), which owns the connection.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal

from consortium.board.db import transaction
from consortium.board.ledger import Spend, reserve
from consortium.core.clock import utc_now_ms
from consortium.core.errors import ConsortiumError
from consortium.core.plan import Trial
from consortium.core.render import canonical_json
from consortium.core.seeds import derive_seed
from consortium.core.validate import invalid_rate

TRIAL_STATES = ("planned", "sent", "valid", "invalid", "refused", "failed")
TERMINAL_STATES = ("valid", "invalid", "refused", "failed")
MODEL_PURPOSE = "model"
TRANSIENT = "transient"
ATTEMPTS_EXHAUSTED = "attempts_exhausted"


@dataclass(frozen=True)
class RetryCaps:
    """A Trial's two retry budgets (story 2.1), counted from its ``attempts`` rows.

    An invalid answer is retried while the Trial's invalid attempts (``valid = 0``)
    are ``<= max_retries``; a transient result while its transient attempts
    (``category = 'transient'``) are ``<= transient_retries``. ``max_attempts`` is
    the hard cap on attempts of any kind (abandoned ones included).
    """

    max_retries: int
    transient_retries: int = 0

    @property
    def max_attempts(self) -> int:
        return 1 + self.max_retries + self.transient_retries

_TRIAL_COLUMNS = (
    "trial_id", "test", "session_id", "trial_index", "instrument", "clip_ids", "pair_id",
    "position", "prompt_variant", "order_seed", "repeat", "agent_id", "persona_id", "model_id",
)


def any_trials(conn: sqlite3.Connection) -> bool:
    """Whether any Trial of any Test is stored; False when there is no ``trials`` table."""
    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'trials'"
    ).fetchone()
    if table is None:
        return False
    return conn.execute("SELECT 1 FROM trials LIMIT 1").fetchone() is not None


def count_trials(conn: sqlite3.Connection, test: str) -> int:
    """How many Trials of ``test`` are stored."""
    return conn.execute("SELECT count(*) FROM trials WHERE test = ?", (test,)).fetchone()[0]


def insert_plan(conn: sqlite3.Connection, test: str, trials: Iterable[Trial]) -> int:
    """Insert every Trial of ``test`` as ``planned`` (attempt 0) in one transaction.

    Raises ``test_already_open`` (nothing written) if ``test`` already has Trials.
    Returns the number of Trials inserted.
    """
    with transaction(conn):
        return insert_trials(conn, test, trials)


def insert_trials(conn: sqlite3.Connection, test: str, trials: Iterable[Trial]) -> int:
    """``insert_plan`` inside the caller's transaction."""
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


def _latest(conn: sqlite3.Connection, trial_id: str, attempt: int) -> tuple | None:
    """``(handle, sent_at, valid, category)`` of the Trial's attempt ``attempt``; None if
    not recorded."""
    return conn.execute(
        "SELECT handle, sent_at, valid, category FROM attempts"
        " WHERE trial_id = ? AND attempt = ?",
        (trial_id, attempt),
    ).fetchone()


def attempt_counts(conn: sqlite3.Connection, trial_id: str) -> tuple[int, int]:
    """``(invalid, transient)``: the Trial's attempts recorded invalid (``valid = 0``) and
    recorded ``transient`` (story 2.1)."""
    invalid, transient = conn.execute(
        "SELECT coalesce(sum(valid = 0), 0), coalesce(sum(category = ?), 0)"
        " FROM attempts WHERE trial_id = ?",
        (TRANSIENT, trial_id),
    ).fetchone()
    return int(invalid), int(transient)


def settlement(
    state: str,
    attempt: int,
    latest: tuple | None,
    caps: RetryCaps | None,
    counts: tuple[int, int] = (0, 0),
) -> str | None:
    """The state a non-terminal Trial is settled to without dispatch, or None.

    ``latest`` is ``_latest``'s row, ``counts`` is ``attempt_counts`` (stories 1.10, 2.1):

    - latest attempt recorded valid (stopped before its state write): ``valid``;
    - latest recorded invalid and invalid attempts > ``max_retries``: ``invalid``;
    - latest recorded ``transient`` and transient attempts > ``transient_retries``:
      ``failed`` (its category stays ``transient``);
    - ``attempt >= max_attempts`` (the hard cap): ``invalid`` if the latest attempt was
      recorded invalid, ``failed`` if it was recorded transient, and ``failed``
      (category ``attempts_exhausted``) if it was never answered (abandoned, not
      collectable), so it is not counted as an invalid answer.
    A ``sent`` latest attempt with a handle and no recorded outcome is collected instead.
    """
    if attempt == 0 or latest is None:
        return None
    handle, sent_at, valid, category = latest
    if valid == 1:
        return "valid"
    if caps is None:
        return None
    invalid, transient = counts
    if valid == 0 and invalid > caps.max_retries:
        return "invalid"
    if category == TRANSIENT and transient > caps.transient_retries:
        return "failed"
    if attempt < caps.max_attempts:
        return None
    if valid == 0:
        return "invalid"
    if category == TRANSIENT:
        return "failed"
    if state == "sent" and sent_at is not None and handle is not None:
        return None  # collectable: it may still be answered
    return "failed"


def load_resumable(
    conn: sqlite3.Connection, test: str, caps: RetryCaps | None = None
) -> list[dict]:
    """Every non-terminal Trial of ``test`` in plan order, for resume (story 1.8).

    Each row is a ``load_trials`` row plus ``handle`` and ``settle``. ``handle``
    is the stored handle (JSON text) of the Trial's latest attempt (``attempt``)
    if that attempt was marked ``sent``, has one and has no recorded outcome yet,
    else ``None``. A ``planned`` Trial's attempt, never marked ``sent``, is never
    collected, so its handle is always ``None``. A latest attempt already
    recorded invalid (``valid = 0``, story 1.10) or ``transient`` (stopped
    mid-backoff, story 2.1) is not collected again: it gets a new attempt at once.
    ``settle`` (see ``settlement``) is the state the Trial is settled to from the
    board without any dispatch, else ``None``; ``caps`` are the retry budgets
    (``None``: no budget and no attempt cap).
    """
    out = []
    for row in load_trials(conn, test):
        if row["state"] in TERMINAL_STATES:
            continue
        tid = row["trial_id"]
        latest = _latest(conn, tid, row["attempt"]) if row["attempt"] else None
        counts = attempt_counts(conn, tid) if latest is not None else (0, 0)
        settle = settlement(row["state"], row["attempt"], latest, caps, counts)
        handle = None
        if settle is None and row["state"] == "sent" and latest is not None:
            stored, sent_at, valid, category = latest
            if sent_at is not None and valid is None and category is None:
                handle = stored
        out.append({**row, "handle": handle, "settle": settle})
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


def begin_attempt(
    conn: sqlite3.Connection,
    trial_id: str,
    study_seed: int,
    usd: Decimal,
    ceiling: Decimal | None,
    spend: Spend,
) -> tuple[int, int] | None:
    """Reserve ``usd`` and start the Trial's next attempt, atomically (one transaction).

    If ``ceiling`` is set and committed spend (``spend``, the writer's running
    total) plus ``usd`` exceeds it, the reservation is refused: nothing is written and
    ``None`` is returned. Otherwise ``attempt`` is incremented, the attempt row
    is recorded with its seed (``derive_seed(study_seed, "model",
    "<session_id>:<trial_index>:<attempt>")``) and the ledger row with
    ``reserved_usd = usd``; returns ``(attempt, seed)``. Refuses a Trial in a
    terminal state.
    """
    with transaction(conn):
        row = conn.execute(
            "SELECT session_id, trial_index, state, attempt, model_id FROM trials"
            " WHERE trial_id = ?",
            (trial_id,),
        ).fetchone()
        if row is None:
            raise KeyError(trial_id)
        session_id, trial_index, state, attempt, model_id = row
        if state in TERMINAL_STATES:
            raise ValueError(f"Trial {trial_id} is {state}; terminal states never change")
        if ceiling is not None and spend.committed(conn) + usd > ceiling:
            return None
        attempt += 1
        seed = derive_seed(study_seed, MODEL_PURPOSE, f"{session_id}:{trial_index}:{attempt}")
        conn.execute("UPDATE trials SET attempt = ? WHERE trial_id = ?", (attempt, trial_id))
        conn.execute(
            "INSERT INTO attempts (trial_id, attempt, seed) VALUES (?, ?, ?)",
            (trial_id, attempt, seed),
        )
        reserve(conn, trial_id, attempt, model_id, usd)
    spend.add(conn, usd)
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


def attempt_seed(conn: sqlite3.Connection, trial_id: str, attempt: int) -> int:
    """The recorded seed of the Trial's attempt ``attempt``."""
    row = conn.execute(
        "SELECT seed FROM attempts WHERE trial_id = ? AND attempt = ?", (trial_id, attempt)
    ).fetchone()
    if row is None:
        raise KeyError((trial_id, attempt))
    return row[0]


def record_transient(conn: sqlite3.Connection, trial_id: str, attempt: int) -> None:
    """Record a ``transient`` result of one attempt (story 2.1): category and ``answered_at``.

    ``valid`` stays empty and the Trial's state is not changed (it stays ``sent``
    and gets a new attempt, or ``set_state`` fails it once its budget is spent).
    """
    with transaction(conn):
        cur = conn.execute(
            "UPDATE attempts SET category = ?, answered_at = ?"
            " WHERE trial_id = ? AND attempt = ? AND valid IS NULL",
            (TRANSIENT, utc_now_ms(), trial_id, attempt),
        )
        if cur.rowcount != 1:
            raise KeyError((trial_id, attempt))


def state_counts(conn: sqlite3.Connection, test: str) -> dict[str, int]:
    """Trials of ``test`` by state, in lifecycle order; states with no Trial are omitted."""
    found = dict(
        conn.execute("SELECT state, count(*) FROM trials WHERE test = ? GROUP BY state", (test,))
    )
    return {s: found[s] for s in TRIAL_STATES if s in found}


def record_validation(
    conn: sqlite3.Connection,
    trial_id: str,
    attempt: int,
    valid: bool,
    reason: str | None,
    answer_json: str | None,
    category: str = "ok",
) -> None:
    """Record the validation of one attempt (story 1.10) with its ``category`` and ``answered_at``.

    ``reason`` is the ``invalid_response`` reason of an invalid attempt;
    ``answer_json`` the canonical JSON of a valid attempt's parsed answer.
    The Trial's state is not changed (see ``set_state``).
    """
    if valid and (answer_json is None or reason is not None):
        raise ValueError("a valid attempt has an answer and no invalid reason")
    if not valid and (reason is None or answer_json is not None):
        raise ValueError("an invalid attempt has a reason and no answer")
    with transaction(conn):
        cur = conn.execute(
            "UPDATE attempts SET valid = ?, invalid_reason = ?, answer_json = ?, category = ?,"
            " answered_at = ? WHERE trial_id = ? AND attempt = ?",
            (int(valid), reason, answer_json, category, utc_now_ms(), trial_id, attempt),
        )
        if cur.rowcount != 1:
            raise KeyError((trial_id, attempt))


def settle(conn: sqlite3.Connection, trial_id: str, attempt: int, state: str) -> None:
    """Settle a non-terminal Trial at its latest ``attempt`` to ``state`` (story 1.10).

    ``state`` comes from ``settlement``. ``failed`` records category
    ``attempts_exhausted`` on an attempt that has none (never answered) and keeps a
    recorded one (``transient``); ``valid``/``invalid`` keep the category recorded
    with the attempt's validation. Never changes a terminal state.
    """
    if state not in ("valid", "invalid", "failed"):
        raise ValueError(f"cannot settle a Trial to {state!r}")
    with transaction(conn):
        cur = conn.execute(
            "UPDATE trials SET state = ? WHERE trial_id = ? AND attempt = ?"
            f" AND state NOT IN ({', '.join('?' * len(TERMINAL_STATES))})",
            (state, trial_id, attempt, *TERMINAL_STATES),
        )
        if cur.rowcount != 1:
            raise ValueError(f"Trial {trial_id} attempt {attempt} cannot be settled {state}")
        category = ATTEMPTS_EXHAUSTED if state == "failed" else None
        conn.execute(
            "UPDATE attempts SET category = coalesce(category, ?),"
            " answered_at = coalesce(answered_at, ?) WHERE trial_id = ? AND attempt = ?",
            (category, utc_now_ms(), trial_id, attempt),
        )


def settle_exhausted(conn: sqlite3.Connection, trial_id: str, caps: RetryCaps) -> bool:
    """Settle the Trial (see ``settlement``) if it can take no new attempt; True if settled.

    The engine's guard before every new attempt, so a Trial never exceeds its
    budgets or ``caps.max_attempts`` attempts. A terminal Trial is left unchanged
    (False).
    """
    row = conn.execute(
        "SELECT state, attempt FROM trials WHERE trial_id = ?", (trial_id,)
    ).fetchone()
    if row is None:
        raise KeyError(trial_id)
    state, attempt = row
    if state in TERMINAL_STATES or attempt == 0:
        return False
    latest = _latest(conn, trial_id, attempt)
    target = settlement(state, attempt, latest, caps, attempt_counts(conn, trial_id))
    if target is None:
        if attempt < caps.max_attempts:
            return False
        target = "failed"  # a collectable attempt at the cap: never dispatched past it
    settle(conn, trial_id, attempt, target)
    return True


def chosen_answer(conn: sqlite3.Connection, trial_id: str) -> dict | None:
    """The Trial's answer: its highest valid attempt, as ``{"attempt", "answers"}``.

    ``answers`` is the parsed ``{item_id: value}``; ``None`` when no attempt is valid.
    """
    row = conn.execute(
        "SELECT attempt, answer_json FROM attempts WHERE trial_id = ? AND valid = 1"
        " ORDER BY attempt DESC LIMIT 1",
        (trial_id,),
    ).fetchone()
    if row is None:
        return None
    return {"attempt": row[0], "answers": json.loads(row[1])}


_RATE_STATES = ("valid", "invalid", "refused", "failed")


def invalid_rates(conn: sqlite3.Connection, test: str) -> dict[str, dict[str, dict]]:
    """The invalid-answer rate of ``test`` per Agent and per Model (story 1.10).

    Returns ``{"by_agent": {agent_id: entry}, "by_model": {model_id: entry}}``,
    each entry ``{"valid", "invalid", "refused", "failed", "rate"}`` (Trial
    counts; ``rate`` from ``core.validate.invalid_rate``, ``None`` when no Trial
    is valid or invalid). Keys are sorted.
    """
    out: dict[str, dict[str, dict]] = {}
    for key, column in (("by_agent", "agent_id"), ("by_model", "model_id")):
        counts: dict[str, dict[str, int]] = {}
        for group, state, n in conn.execute(
            f"SELECT {column}, state, count(*) FROM trials WHERE test = ?"
            f" GROUP BY {column}, state",
            (test,),
        ):
            counts.setdefault(group, {s: 0 for s in _RATE_STATES})
            if state in _RATE_STATES:
                counts[group][state] = n
        out[key] = {
            group: {**c, "rate": invalid_rate(c)} for group, c in sorted(counts.items())
        }
    return out
