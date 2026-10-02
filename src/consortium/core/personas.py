"""Seeded, quota-balanced Persona generation and behaviour-only card rendering (pure).

The pool is the 32 Big Five profiles (every high/low combination of O, C, E, A,
N, ordered by bit pattern with ``low`` = 0 and O most significant), each crossed
with every NARS band in frame order. Each quota attribute is assigned
independently and stratified by NARS band:

1. The N levels are laid out round-robin in listed order (level 0, 1, ..., k-1,
   0, 1, ...), so the marginal counts are equal with the remainder going one
   each to the earliest-listed levels.
2. The sequence is cut into consecutive blocks of 32, one per band in frame
   order; any 32 consecutive round-robin entries hold each level 32//k or
   32//k + 1 times, so within a band the counts differ by at most 1. A band's
   extras continue the cycle where the previous band's stopped (the first band's
   go to the earliest-listed levels).
3. Each block is shuffled (in band order) with one ``random.Random(derive_seed(
   seed, "personas", <attribute>))`` and dealt to that band's Personas in Persona
   order.

The shuffle is an explicit Fisher-Yates driven only by ``Random.getrandbits``
(Mersenne Twister output, stable across Python versions), never ``random.shuffle``.
"""

from __future__ import annotations

import random
from collections.abc import Mapping, Sequence
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, StrictStr, field_validator

from consortium.core.seeds import derive_seed

Trait = Literal["openness", "conscientiousness", "extraversion", "agreeableness", "neuroticism"]
Pole = Literal["high", "low"]

TRAITS: tuple[Trait, ...] = (
    "openness",
    "conscientiousness",
    "extraversion",
    "agreeableness",
    "neuroticism",
)
QUOTA_ATTRIBUTES = ("age_band", "gender", "cultural_region", "robot_experience")
SEED_PURPOSE = "personas"
PROFILES = 2 ** len(TRAITS)
# Bump when the generation or card algorithm changes; written to panel/personas/meta.json.
GENERATOR_VERSION = "1"


class Persona(BaseModel):
    """One Persona. Field names become the export's ``persona_*`` columns."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: StrictStr
    big_five: dict[Trait, Pole]
    nars: Literal["low", "high"]
    age_band: StrictStr
    gender: StrictStr
    cultural_region: StrictStr
    robot_experience: StrictStr

    @field_validator("big_five")
    @classmethod
    def _all_traits(cls, value: dict[str, str]) -> dict[str, str]:
        missing = [t for t in TRAITS if t not in value]
        if missing:
            raise ValueError(f"missing trait(s) {', '.join(missing)}")
        return value


# Structural views of the config models, so core never imports config.


class _Quotas(Protocol):
    age_band: list[str]
    gender: list[str]
    cultural_region: list[str]
    robot_experience: list[str]


class _Frame(Protocol):
    nars_bands: list[str]
    quotas: _Quotas


class _Study(Protocol):
    seed: int
    personas: _Frame


class _LevelPhrases(Protocol):
    age_band: dict[str, str]
    gender: dict[str, str]
    cultural_region: dict[str, str]
    robot_experience: dict[str, str]


class _Wording(Protocol):
    demographic: str
    traits: object  # one attribute per TRAITS entry, each with .high and .low
    nars: Mapping[str, str]
    level_phrases: _LevelPhrases


def big_five_profiles() -> list[dict[Trait, Pole]]:
    """The 32 profiles in bit-pattern order (``low`` = 0, openness most significant)."""
    n = len(TRAITS)
    return [
        {
            trait: ("high" if (index >> (n - 1 - bit)) & 1 else "low")
            for bit, trait in enumerate(TRAITS)
        }
        for index in range(2**n)
    ]


def _below(rng: random.Random, n: int) -> int:
    """Uniform integer in ``[0, n)`` from ``getrandbits`` by rejection sampling."""
    bits = n.bit_length()
    while True:
        r = rng.getrandbits(bits)
        if r < n:
            return r


def fisher_yates[T](items: list[T], rng: random.Random) -> None:
    """Shuffle ``items`` in place; depends only on ``rng.getrandbits``."""
    for i in range(len(items) - 1, 0, -1):
        j = _below(rng, i + 1)
        items[i], items[j] = items[j], items[i]


def stratified_levels(
    levels: Sequence[str], bands: int, per_band: int, seed: int
) -> list[list[str]]:
    """One shuffled block of ``per_band`` levels per band (see the module docstring)."""
    if not levels:
        raise ValueError("levels must not be empty")
    k = len(levels)
    rng = random.Random(seed)
    blocks = []
    for b in range(bands):
        block = [levels[j % k] for j in range(b * per_band, (b + 1) * per_band)]
        fisher_yates(block, rng)
        blocks.append(block)
    return blocks


def generate_personas(cfg: _Study) -> list[Persona]:
    """The full Persona pool for ``cfg`` (a ``StudyConfig``): ``p1 ... pN``, N = 32 x bands."""
    frame = cfg.personas
    bands = list(frame.nars_bands)
    grid = [(profile, band) for profile in big_five_profiles() for band in bands]
    blocks = {
        attr: stratified_levels(
            getattr(frame.quotas, attr),
            len(bands),
            PROFILES,
            derive_seed(cfg.seed, SEED_PURPOSE, attr),
        )
        for attr in QUOTA_ATTRIBUTES
    }
    out = []
    for i, (profile, band) in enumerate(grid):
        b, slot = i % len(bands), i // len(bands)
        out.append(
            Persona(
                id=f"p{i + 1}",
                big_five=profile,
                nars=band,
                **{attr: blocks[attr][b][slot] for attr in QUOTA_ATTRIBUTES},
            )
        )
    return out


def render_card(persona: Persona, wording: _Wording) -> str:
    """The behaviour-only card text the Model sees.

    Demographic line, the five trait sentences in O, C, E, A, N order, then the NARS
    sentence; one per line, LF, one trailing newline. No ID and no labels.
    """
    phrases = wording.level_phrases
    values = {}
    for attr in QUOTA_ATTRIBUTES:
        level = getattr(persona, attr)
        try:
            values[attr] = getattr(phrases, attr)[level]
        except KeyError as err:
            raise ValueError(f"no card phrase for {attr} level {level!r}") from err
    lines = [wording.demographic.format(**values)]
    for trait in TRAITS:
        lines.append(getattr(getattr(wording.traits, trait), persona.big_five[trait]))
    try:
        lines.append(wording.nars[persona.nars])
    except KeyError as err:
        raise ValueError(f"no card sentence for NARS band {persona.nars!r}") from err
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- attributes

# Trial counts for differential attrition (story 2.1). ``failed`` is the total of the three
# ``failed_*`` kinds, split by the category of the Trial's last attempt (see ``count_trial``).
ATTRITION_COUNTS = (
    "trials", "invalid", "refused", "failed", "failed_fatal", "failed_transient",
    "failed_exhausted",
)
_FAILED_KIND = {"transient": "failed_transient", "attempts_exhausted": "failed_exhausted"}


def count_trial(acc: dict[str, int], state: str, category: str | None) -> None:
    """Add one Trial in ``state`` (its last attempt's ``category``) to ``acc`` (pure).

    A ``failed`` Trial counts as ``failed_transient`` (transient budget spent),
    ``failed_exhausted`` (``attempts_exhausted``) or ``failed_fatal`` (category ``fatal``,
    or any other or none recorded).
    """
    acc["trials"] += 1
    if state in ("invalid", "refused", "failed"):
        acc[state] += 1
    if state == "failed":
        acc[_FAILED_KIND.get(category or "", "failed_fatal")] += 1


def attribute_names() -> list[str]:
    """``persona_<field>`` per ``Persona`` field except ``id``, in field order; ``big_five``
    gives one ``persona_<trait>`` per trait (the export's Persona columns)."""
    out: list[str] = []
    for name in Persona.model_fields:
        if name == "id":
            continue
        if name == "big_five":
            out.extend(f"persona_{trait}" for trait in TRAITS)
        else:
            out.append(f"persona_{name}")
    return out


def attribute_values(persona: Persona) -> dict[str, str]:
    """``{persona_<field>: value}`` in ``attribute_names`` order."""
    out: dict[str, str] = {}
    for name in Persona.model_fields:
        if name == "id":
            continue
        if name == "big_five":
            out.update({f"persona_{trait}": persona.big_five[trait] for trait in TRAITS})
        else:
            out[f"persona_{name}"] = getattr(persona, name)
    return out


def tally_by_attribute(
    counts_by_persona: Mapping[str, Mapping[str, int]], personas: Mapping[str, Persona]
) -> dict[str, dict[str, dict[str, int]]]:
    """Sum per-Persona counts per Persona attribute value (pure).

    ``counts_by_persona`` maps a Persona ID to its ``ATTRITION_COUNTS`` counts;
    ``personas`` maps a Persona ID to its ``Persona`` (``KeyError`` for an unknown
    ID). Returns ``{persona_<field>: {value: {<ATTRITION_COUNTS>}}}`` with every
    attribute of ``attribute_names`` (in that order) and its values in the order they
    are first seen while walking ``counts_by_persona`` in its order.
    """
    out: dict[str, dict[str, dict[str, int]]] = {name: {} for name in attribute_names()}
    for persona_id, counts in counts_by_persona.items():
        for name, value in attribute_values(personas[persona_id]).items():
            acc = out[name].setdefault(value, dict.fromkeys(ATTRITION_COUNTS, 0))
            for key in ATTRITION_COUNTS:
                acc[key] += int(counts.get(key, 0))
    return out
