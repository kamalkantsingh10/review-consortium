"""Persona-fidelity scoring (story 3.1): does an Agent answer like its Persona card? (pure)

Each keyed self-report answer is scored on its construct: a reverse-keyed answer
``x`` on a ``points``-point scale counts as ``points + 1 - x``. A construct's score
is the mean of its valid answers over every repeat (NARS: over all its items; the
per-subscale means are detail only). A construct matches when its score is above
the scale midpoint ``(points + 1) / 2`` and the card's pole is ``high``, or below it
and the pole is ``low``; a score at the midpoint, or no valid answer, does not match.

A check needs at least half of its planned valid answers (``expected``, Items x
Trials planned for the Agent): with fewer it counts as not matched and its detail
says ``insufficient_data``.

The checks are the five Big Five traits plus the NARS band when the Persona has one
(6 checks, or 5 with no band). ``ratio = matched / total``; the stage compares it with
``thresholds.persona_fidelity_min``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from consortium.core.personas import TRAITS

NARS = "nars"


class _Persona(Protocol):
    @property
    def big_five(self) -> Mapping[str, str]: ...

    @property
    def nars(self) -> str | None: ...


class _Key(Protocol):
    construct: str
    reversed: bool
    subscale: str | None


class _Item(Protocol):
    id: str
    points: int | None


class _Instrument(Protocol):
    items: Sequence[_Item]
    keys: Mapping[str, _Key] | None


@dataclass(frozen=True)
class ScoredItem:
    """How one keyed Item is scored."""

    construct: str
    reversed: bool
    subscale: str | None
    points: int


@dataclass(frozen=True)
class FidelityScore:
    # construct -> {"score", "midpoint", "pole", "match", "n"} (+ "subscales" for nars)
    per_trait: dict[str, dict[str, Any]]
    matched: int
    total: int
    ratio: float
    insufficient: int = 0  # checks with fewer than half their planned valid answers

    @property
    def insufficient_data(self) -> bool:
        return self.insufficient > 0


def fidelity_keys(instruments: Iterable[_Instrument]) -> dict[str, ScoredItem]:
    """``item_id -> ScoredItem`` over every keyed Likert Item of ``instruments``."""
    out: dict[str, ScoredItem] = {}
    for instrument in instruments:
        keys = instrument.keys or {}
        for item in instrument.items:
            key = keys.get(item.id)
            if key is None or item.points is None:
                continue
            out[item.id] = ScoredItem(key.construct, key.reversed, key.subscale, item.points)
    return out


def _value(raw: Any, key: ScoredItem) -> int | None:
    if isinstance(raw, bool) or not isinstance(raw, int) or not 1 <= raw <= key.points:
        return None
    return key.points + 1 - raw if key.reversed else raw


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def score_fidelity(
    answers: Iterable[Mapping[str, Any]],
    persona: _Persona,
    keys: Mapping[str, ScoredItem],
    expected: Mapping[str, int] | None = None,
) -> FidelityScore:
    """Score one Agent's valid answers (``{item_id: value}`` per Trial, every repeat).

    ``expected`` maps a construct to its planned answer count (None: no minimum).
    """
    values: dict[str, list[int]] = {}
    midpoints: dict[str, list[float]] = {}
    subscales: dict[str, list[int]] = {}
    for answer in answers:
        for item_id, raw in answer.items():
            key = keys.get(item_id)
            if key is None:
                continue
            value = _value(raw, key)
            if value is None:
                continue
            values.setdefault(key.construct, []).append(value)
            midpoints.setdefault(key.construct, []).append((key.points + 1) / 2)
            if key.subscale is not None:
                subscales.setdefault(key.subscale, []).append(value)
    checks: list[tuple[str, str]] = [(t, persona.big_five[t]) for t in TRAITS]
    if persona.nars is not None:
        checks.append((NARS, persona.nars))
    per: dict[str, dict[str, Any]] = {}
    matched = 0
    insufficient = 0
    for construct, pole in checks:
        score = _mean(values.get(construct, []))
        midpoint = _mean(midpoints.get(construct, []))
        n = len(values.get(construct, []))
        planned = None if expected is None else expected.get(construct, 0)
        short = planned is not None and 2 * n < planned
        match = not short and score is not None and midpoint is not None and (
            (pole == "high" and score > midpoint) or (pole == "low" and score < midpoint)
        )
        matched += match
        insufficient += short
        entry: dict[str, Any] = {
            "score": score, "midpoint": midpoint, "pole": pole, "match": match, "n": n,
        }
        if planned is not None:
            entry["expected"] = planned
            entry["insufficient_data"] = short
        if construct == NARS:
            entry["subscales"] = {s: _mean(v) for s, v in sorted(subscales.items())}
        per[construct] = entry
    total = len(checks)
    return FidelityScore(per, matched, total, matched / total, insufficient)
