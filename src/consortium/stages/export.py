"""Use case: ``export TEST`` (story 1.12), a tidy CSV with Conditions joined at export only.

Reads ``board.db`` read-only and lease-free (``board.db.read_only``, one read
transaction), so it works while another Test is dispatching. It refuses
(``sessions_running``) while any Trial of the Test is ``planned`` or ``sent``,
and writes only ``exports/<test>.csv`` (a unique temporary file, then ``os.replace``).

One row per Item per Trial, ordered by ``session_id`` (natural order, so
``p2`` before ``p10``), ``trial_index`` and the Instrument's Item order. A
``valid`` Trial exports its highest valid attempt's answer; every other
terminal Trial exports the same rows with ``response`` empty and ``status`` set.

Conditions come only from ``board.blinding.read_key``, called once here after
the board, Panel and Instrument checks but before the Condition-column and
stored-answer checks (AD-2); they are never logged. A target Clip with no row
in ``blinding_key.csv`` exports empty Condition cells. Practice clips and their
intended answers are never exported (Trials hold target Clips only).
"""

from __future__ import annotations

import csv
import io
import logging
import os
import re
import sqlite3
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from consortium.board import blinding
from consortium.board.db import read_only
from consortium.board.queries import export_trials, read_transaction
from consortium.board.tests import get_test
from consortium.board.trials import invalid_rates
from consortium.config.load import (
    PERSONAS_DIR,
    PERSONAS_INDEX,
    load_card_wording,
    load_instruments,
    load_personas,
    load_study,
)
from consortium.config.models import InstrumentDef, StudyConfig
from consortium.core.errors import ConsortiumError
from consortium.core.personas import TRAITS, Persona, render_card

log = logging.getLogger(__name__)

EXPORT_SCHEMA_VERSION = 1
EXPORTS_DIR = "exports"
RUNNING_STATES = ("planned", "sent")


def _persona_fields() -> list[tuple[str, Callable[[Persona], str]]]:
    """``(column, value of a Persona)`` per ``Persona`` field except ``id``, in field order;
    ``big_five`` gives one ``persona_<trait>`` per trait."""
    out: list[tuple[str, Callable[[Persona], str]]] = []
    for name in Persona.model_fields:
        if name == "id":
            continue
        if name == "big_five":
            out.extend(
                (f"persona_{trait}", lambda p, t=trait: p.big_five[t]) for trait in TRAITS
            )
        else:
            out.append((f"persona_{name}", lambda p, n=name: getattr(p, n)))
    return out


PERSONA_FIELDS = tuple(_persona_fields())


def persona_columns() -> list[str]:
    """``persona_<field>`` per ``Persona`` field except ``id``; ``big_five`` one per trait."""
    return [column for column, _value in PERSONA_FIELDS]


HEAD_COLUMNS = (
    "schema_version", "agent_id", "session_id", "trial_index", "clip_id", "pair_id",
    "clip_id_a", "clip_id_b", *persona_columns(), "model",
)
TAIL_COLUMNS = (
    "instrument", "item", "response", "position", "repeat", "seed", "prompt_variant", "status",
    "excluded", "exclusion_reason", "test_kind", "protocol_lock", "timestamp",
)
FIXED_COLUMNS = frozenset((*HEAD_COLUMNS, *TAIL_COLUMNS))


def _natural(text: str) -> tuple:
    return tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in re.split(r"(\d+)", text))


def _persona_values(persona: Persona) -> dict[str, str]:
    return {column: value(persona) for column, value in PERSONA_FIELDS}


def _read_board(study: Path, test: str) -> tuple[str, list[dict], dict]:
    """``(kind, trials, invalid rates)`` from one read transaction; refuses as documented."""

    def read(conn: sqlite3.Connection) -> tuple[str, list[dict], dict] | None:
        with read_transaction(conn):
            row = get_test(conn, test)
            if row is None:
                return None
            return row["kind"], export_trials(conn, test), invalid_rates(conn, test)

    found = read_only(study, read)
    if found is None:
        raise ConsortiumError("unknown_test", test)
    kind, trials, rates = found
    if not trials:
        raise ConsortiumError("nothing_to_export", test)
    running = sum(t["state"] in RUNNING_STATES for t in trials)
    if running:
        raise ConsortiumError("sessions_running", f"{running} Trials not terminal")
    return kind, trials, rates


def _check_panel(study: Path, trials: Sequence[Mapping[str, Any]]) -> dict[str, Persona]:
    """The Panel by id; every stored card must equal its regenerated card."""
    personas = load_personas(study)
    wording = load_card_wording()
    for p in personas:
        rel = f"{PERSONAS_DIR}/{p.id}.md"
        try:
            stored = (study / rel).read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError) as err:
            raise ConsortiumError("panel_invalid", f"cannot read {rel}: {err}", path=rel) from err
        try:
            card = render_card(p, wording)
        except ValueError as err:
            raise ConsortiumError("panel_mismatch", f"{rel}: {err}", path=rel) from err
        if card != stored:
            raise ConsortiumError("panel_mismatch", rel, path=rel)
    by_id = {p.id: p for p in personas}
    for t in trials:
        if t["persona_id"] not in by_id:
            raise ConsortiumError(
                "panel_mismatch",
                f"{PERSONAS_INDEX}: Persona {t['persona_id']} of the Trials is not in the Panel",
                path=PERSONAS_INDEX,
            )
    return by_id


def _instruments(
    study: Path, cfg: StudyConfig, trials: Sequence[Mapping[str, Any]]
) -> dict[str, InstrumentDef]:
    loaded = load_instruments(study, cfg)
    for name in sorted({t["instrument"] for t in trials}):
        if name not in loaded:
            raise ConsortiumError(
                "unknown_instrument", f"{name!r} (used by the Test's Trials) is not loaded"
            )
    return loaded


def _condition_columns(
    key: Mapping[str, Mapping[str, str]], trials: Sequence[Mapping[str, Any]]
) -> list[tuple[str, str, int | None]]:
    """``(column, factor, side)``: side ``None`` for single-clip, 0/1 for pairwise ``_a``/``_b``.

    A target Clip with no key row (pushed with no Condition) adds no factor.
    Raises ``condition_name_clash`` (a Condition column equals another column).
    """
    clips = sorted({c for t in trials for c in t["clip_ids"]})
    factors = sorted({f for clip in clips for f in key.get(clip, {})})
    has_single = any(t["pair_id"] is None for t in trials)
    has_pair = any(t["pair_id"] is not None for t in trials)
    out: list[tuple[str, str, int | None]] = []
    for factor in factors:
        if has_single:
            out.append((factor, factor, None))
        if has_pair:
            out.extend(((f"{factor}_a", factor, 0), (f"{factor}_b", factor, 1)))
    seen = set(FIXED_COLUMNS)
    for column, factor, _side in out:
        if column in seen:
            raise ConsortiumError("condition_name_clash", factor)
        seen.add(column)
    return out


def _response(item_type: str, options: list[str] | None, value: Any, clip_ids: tuple) -> str:
    """The exported ``response``; ``ValueError`` when ``value`` does not fit the Item type."""
    if item_type == "likert":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("likert answer is not an integer")
        return str(value)
    if not isinstance(value, str):
        raise ValueError(f"{item_type} answer is not a string")
    if item_type == "pairwise":
        return clip_ids[list(options or []).index(value)]
    return value


def _cell(value: Any) -> str:
    return "" if value is None else str(value)


def _rows(
    trials: Sequence[Mapping[str, Any]],
    personas: Mapping[str, Persona],
    instruments: Mapping[str, InstrumentDef],
    key: Mapping[str, Mapping[str, str]],
    conditions: Sequence[tuple[str, str, int | None]],
    kind: str,
) -> list[list[str]]:
    out = []
    columns = [*HEAD_COLUMNS, *(c for c, _f, _s in conditions), *TAIL_COLUMNS]
    ordered = sorted(trials, key=lambda t: (_natural(t["session_id"]), t["trial_index"]))
    for t in ordered:
        clip_ids = tuple(t["clip_ids"])
        pairwise = t["pair_id"] is not None
        if len(clip_ids) != (2 if pairwise else 1) or not all(isinstance(c, str) for c in clip_ids):
            raise ConsortiumError(
                "board_unreadable", f"Trial {t['trial_id']}: stored clip_ids do not fit the Trial"
            )
        base: dict[str, Any] = {
            "schema_version": EXPORT_SCHEMA_VERSION,
            "agent_id": t["agent_id"],
            "session_id": t["session_id"],
            "trial_index": t["trial_index"],
            "clip_id": None if pairwise else clip_ids[0],
            "pair_id": t["pair_id"],
            "clip_id_a": clip_ids[0] if pairwise else None,
            "clip_id_b": clip_ids[1] if pairwise else None,
            **_persona_values(personas[t["persona_id"]]),
            "model": t["model_id"],
            "instrument": t["instrument"],
            "position": t["position"],
            "repeat": t["repeat"],
            "seed": t["seed"],
            "prompt_variant": t["prompt_variant"],
            "status": t["state"],
            "excluded": "false",
            "exclusion_reason": None,
            "test_kind": kind,
            "protocol_lock": None,
            "timestamp": t["timestamp"],
        }
        for column, factor, side in conditions:
            if (side is None) == pairwise:
                base[column] = None
            else:
                clip = clip_ids[0] if side is None else clip_ids[side]
                base[column] = key.get(clip, {}).get(factor)
        answers = t["answers"] if t["state"] == "valid" else None
        if t["state"] == "valid" and answers is None:
            raise ConsortiumError(
                "board_unreadable", f"Trial {t['trial_id']} is valid but has no valid attempt"
            )
        items = instruments[t["instrument"]].items
        if answers is not None and not isinstance(answers, dict):
            raise ConsortiumError(
                "board_unreadable", f"Trial {t['trial_id']}: stored answer is not an object"
            )
        if answers is not None and set(answers) != {item.id for item in items}:
            raise ConsortiumError(
                "instrument_changed",
                f"{t['instrument']}: stored answers do not match its Items",
            )
        for item in items:
            response = None
            if answers is not None:
                try:
                    response = _response(item.type, item.options, answers[item.id], clip_ids)
                except (ValueError, IndexError) as err:
                    raise ConsortiumError(
                        "board_unreadable",
                        f"Trial {t['trial_id']}: stored answer does not fit item {item.id}",
                    ) from err
            row = {**base, "item": item.id, "response": response}
            out.append([_cell(row[c]) for c in columns])
    return out


def _log_rates(rates: Mapping[str, Mapping[str, Any]], threshold: float) -> None:
    def fmt(rate: float | None) -> str:
        return "n/a" if rate is None else f"{rate:.4f}"

    for model, entry in rates["by_model"].items():
        log.warning(
            "invalid_rate: model %s %s (valid %d, invalid %d, refused %d, failed %d)",
            model, fmt(entry["rate"]), entry["valid"], entry["invalid"], entry["refused"],
            entry["failed"],
        )
    for agent, entry in rates["by_agent"].items():
        if entry["rate"] is not None and entry["rate"] > threshold:
            log.warning(
                "invalid_rate_above_max: agent %s %s > %s", agent, fmt(entry["rate"]), threshold
            )


def _write(path: Path, header: Sequence[str], rows: Sequence[Sequence[str]]) -> None:
    """Write via a unique temporary file in the same folder, fsync it, rename, fsync the folder."""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(buf.getvalue())
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def _read_key(study: Path) -> dict[str, dict[str, str]]:
    """``blinding.read_key``; ``blinding_key_missing`` if the file exists but cannot be read."""
    try:
        return blinding.read_key(study)
    except (OSError, UnicodeDecodeError, csv.Error, KeyError) as err:
        raise ConsortiumError(
            "blinding_key_missing", f"{blinding.KEY_FILE} cannot be read: {err}",
            path=blinding.KEY_FILE,
        ) from err


def export_test(study_dir: Path | str, test: str) -> Path:
    """Write ``exports/<test>.csv`` for the finished Test ``test`` and return its path.

    Raises, in this order and before writing anything: ``unknown_test`` (not
    registered, or no ``board.db``), ``nothing_to_export`` (no Trials),
    ``sessions_running`` (a Trial is planned or sent), ``board_unreadable``
    (``board.db`` unreadable, or an attempt row missing), ``panel_missing`` /
    ``panel_invalid`` / ``panel_mismatch``, ``unknown_instrument``; then
    ``blinding.read_key`` runs and ``blinding_key_missing`` (the key file exists
    but cannot be read), ``condition_name_clash``, ``instrument_changed`` (a
    valid Trial's stored answers do not match its Instrument's Items) and
    ``board_unreadable`` (a stored answer or ``clip_ids`` of the wrong type or
    length) can still follow.
    """
    study = Path(study_dir)
    kind, trials, rates = _read_board(study, test)
    cfg = load_study(study)
    personas = _check_panel(study, trials)
    instruments = _instruments(study, cfg, trials)
    key = _read_key(study)
    conditions = _condition_columns(key, trials)
    rows = _rows(trials, personas, instruments, key, conditions, kind)
    header = [*HEAD_COLUMNS, *(c for c, _f, _s in conditions), *TAIL_COLUMNS]
    path = study / EXPORTS_DIR / f"{test}.csv"
    _write(path, header, rows)
    _log_rates(rates, cfg.thresholds.invalid_rate_max)
    return path
