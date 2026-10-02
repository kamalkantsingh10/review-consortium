"""Perception screening (story 3.2): Trial shapes, scoring and coverage (pure).

A ``kind: screening`` Test declares ``checks``: known answers about 1-2 of its Clips.

- A **pair** check is a pairwise Instrument's check, or a single-Clip Instrument's check
  given 2 Clips; ``expected`` is the Clip that should win.
- A **low-level** check is a single-Clip check; ``expected`` is the Item's answer.

``check_shapes`` turns the checks into the Trials one Session needs (only those), in check
order, de-duplicated: a pairwise check's pair in both positions, a single-Clip check's one
Trial per Clip. Every Trial is answered by the neutral Persona ``p0`` (``NEUTRAL_CARD``),
one Agent ``p0-m<n>`` per Model, never by a Panel Persona.

``score_perception`` counts units per repeat (per Session): a pairwise check gives one unit
per position Trial, passing when the chosen option's Clip is ``expected``; a two-Clip
single-Clip check gives one unit, passing when ``expected``'s answer is strictly greater
than the other Clip's; a low-level check gives one unit, passing when the answer equals
``expected``. A Trial that is not ``valid`` (or is missing) fails its units.

``passes``: a Model x Instrument passes when it has at least one unit and its ratio is at
least ``thresholds.perception_min`` (0 units always fails).

Coverage is the one rule shared with story 3.3: ``covered_instruments`` takes the current
perception results (per screening Test: the highest complete run of that Test per
``(instrument, model)``, ``board.screening.current_results``) and counts an Instrument as
covered when any of them has ``pair_checks > 0``, an ``instrument_hash`` equal to the
Instrument's current definition hash and a ``settings_hash`` equal to its Model's current
settings hash (stale results never cover); low-level checks never cover. ``coverage``
then lists the needed Instruments that are not covered.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from consortium.core.plan import pair_id

NEUTRAL_PERSONA_ID = "p0"
NEUTRAL_CARD = (
    "You are an adult taking part in a study about robots. "
    "Answer every question carefully and honestly."
)

# (instrument, clip_ids, pair_id, position), as ``core.plan.canonical_trials``
Shape = tuple[str, tuple[str, ...], str | None, int | None]


class _Check(Protocol):
    instrument: str
    item: str
    clips: Sequence[str]
    expected: Any


class _Item(Protocol):
    id: str
    options: list[str] | None


class _Instrument(Protocol):
    items: Sequence[_Item]

    @property
    def pairwise(self) -> bool: ...


@dataclass(frozen=True)
class NeutralPersona:
    """The neutral Persona ``p0`` (only its ``id`` is planned)."""

    id: str = NEUTRAL_PERSONA_ID


@dataclass(frozen=True)
class PerceptionTrial:
    """What scoring needs from one Trial of a perception run."""

    model_id: str
    repeat: int
    instrument: str
    clip_ids: tuple[str, ...]
    state: str
    answers: Mapping[str, Any] | None  # the chosen valid answer; None if not valid


def is_pair_check(check: _Check, instruments: Mapping[str, _Instrument]) -> bool:
    """A pairwise Instrument's check, or any 2-Clip check."""
    return instruments[check.instrument].pairwise or len(check.clips) == 2


def check_shapes(checks: Iterable[_Check], instruments: Mapping[str, _Instrument]) -> list[Shape]:
    """The Trials one Session needs for ``checks``, in check order, de-duplicated."""
    out: list[Shape] = []
    seen: set[Shape] = set()

    def add(shape: Shape) -> None:
        if shape not in seen:
            seen.add(shape)
            out.append(shape)

    for check in checks:
        name = check.instrument
        if instruments[name].pairwise:
            lo, hi = sorted(check.clips)
            pid = pair_id(name, lo, hi)
            add((name, (lo, hi), pid, 1))
            add((name, (hi, lo), pid, 2))
        else:
            for clip in check.clips:
                add((name, (clip,), None, None))
    return out


def pair_checks_by_instrument(
    checks: Iterable[_Check], instruments: Mapping[str, _Instrument]
) -> dict[str, int]:
    """``{instrument: number of pair checks}`` for every Instrument with a check (0 when it
    has only low-level checks), in first-check order."""
    out: dict[str, int] = {}
    for check in checks:
        out.setdefault(check.instrument, 0)
        out[check.instrument] += is_pair_check(check, instruments)
    return out


def _answer(trial: PerceptionTrial | None, item: str) -> Any:
    if trial is None or trial.state != "valid" or trial.answers is None:
        return None
    return trial.answers.get(item)


def _option_clip(trial: PerceptionTrial, item: _Item, value: Any) -> str | None:
    options = list(item.options or ())
    if value not in options:
        return None
    index = options.index(value)
    return trial.clip_ids[index] if index < len(trial.clip_ids) else None


def score_perception(
    trials: Iterable[PerceptionTrial],
    checks: Sequence[_Check],
    instruments: Mapping[str, _Instrument],
) -> dict[tuple[str, str], tuple[int, int, float]]:
    """``{(model, instrument): (passed units, units, ratio)}`` for every Model with Trials
    and every Instrument with a check (see the module docstring). ``ratio`` is
    ``passed / units`` (0.0 when there are no units)."""
    by_key: dict[tuple[str, int, str, tuple[str, ...]], PerceptionTrial] = {}
    repeats: dict[str, set[int]] = {}
    for t in trials:
        by_key[(t.model_id, t.repeat, t.instrument, tuple(t.clip_ids))] = t
        repeats.setdefault(t.model_id, set()).add(t.repeat)
    names = list(dict.fromkeys(c.instrument for c in checks))
    out: dict[tuple[str, str], tuple[int, int, float]] = {}
    for model in repeats:
        for name in names:
            passed = units = 0
            for repeat in sorted(repeats[model]):
                for check in (c for c in checks if c.instrument == name):
                    p, u = _units(check, instruments[name], model, repeat, by_key)
                    passed += p
                    units += u
            out[(model, name)] = (passed, units, passed / units if units else 0.0)
    return out


def _units(
    check: _Check,
    instrument: _Instrument,
    model: str,
    repeat: int,
    by_key: Mapping[tuple[str, int, str, tuple[str, ...]], PerceptionTrial],
) -> tuple[int, int]:
    """``(passed, units)`` of one check in one repeat."""
    name = check.instrument

    def trial(clips: tuple[str, ...]) -> PerceptionTrial | None:
        return by_key.get((model, repeat, name, clips))

    if instrument.pairwise:
        item = next(i for i in instrument.items if i.id == check.item)
        lo, hi = sorted(check.clips)
        passed = 0
        for clips in ((lo, hi), (hi, lo)):
            t = trial(clips)
            value = _answer(t, check.item)
            if t is not None and value is not None:
                passed += _option_clip(t, item, value) == check.expected
        return passed, 2
    if len(check.clips) == 2:
        other = next(c for c in check.clips if c != check.expected)
        a = _answer(trial((check.expected,)), check.item)
        b = _answer(trial((other,)), check.item)
        ok = isinstance(a, int) and isinstance(b, int) and a > b
        return int(ok), 1
    value = _answer(trial((check.clips[0],)), check.item)
    return int(value is not None and value == check.expected), 1


def passes(units: int, ratio: float, threshold: float) -> bool:
    """A Model x Instrument's outcome: at least one unit and ``ratio >= threshold``."""
    return units > 0 and ratio >= threshold


def covered_instruments(
    results: Iterable[Mapping[str, Any]],
    instrument_hashes: Mapping[str, str],
    settings_hashes: Mapping[str, str],
) -> set[str]:
    """Instruments with a current, non-stale perception result with ``pair_checks > 0``.

    ``results`` are the current perception rows (``instrument``, ``model_id``,
    ``pair_checks``, ``instrument_hash``, ``settings_hash``); ``instrument_hashes`` maps
    each Instrument to its current definition's hash and ``settings_hashes`` each Model
    of ``study.yaml`` to its current settings hash. A row of an Instrument or Model not in
    those maps does not count.
    """
    out: set[str] = set()
    for row in results:
        name = row["instrument"]
        if (
            (row.get("pair_checks") or 0) > 0
            and instrument_hashes.get(name) is not None
            and row.get("instrument_hash") == instrument_hashes[name]
            and settings_hashes.get(row["model_id"]) is not None
            and row.get("settings_hash") == settings_hashes[row["model_id"]]
        ):
            out.add(name)
    return out


def coverage(
    needed: Mapping[str, Iterable[str]], covered: Iterable[str]
) -> dict[str, list[str]]:
    """``{instrument: tests}`` for every needed Instrument not ``covered``.

    ``needed`` maps each Instrument a registered ``main`` or ``pilot`` Test uses (the
    caller leaves out ``self_report`` Instruments) to those Tests. Instruments in
    name order, Tests sorted.
    """
    have = set(covered)
    return {
        name: sorted(set(tests)) for name, tests in sorted(needed.items()) if name not in have
    }
