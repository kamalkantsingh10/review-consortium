"""Seeded, quota-balanced Persona generation and behaviour-only card rendering (pure).

The profile set is the 32 Big Five profiles (every high/low combination of O, C,
E, A, N, ordered by bit pattern with ``low`` = 0 and O most significant), or the
principal half or quarter fraction of them (``design_profiles``; the kept profiles
stay in that order). Each profile is repeated ``replicates`` times and each copy is
crossed with every NARS band in frame order: Persona order is profile, then
replicate, then band (band fastest). Each quota attribute is assigned independently
and stratified by NARS band:

1. The N levels are laid out round-robin in listed order (level 0, 1, ..., k-1,
   0, 1, ...), so the marginal counts are equal with the remainder going one
   each to the earliest-listed levels.
2. The sequence is cut into consecutive blocks of ``profiles x replicates`` (32 for
   the full grid), one per band in frame order; any such run of round-robin entries
   holds each level within 1 of the others, so within a band the counts differ by at
   most 1. A band's extras continue the cycle where the previous band's stopped (the
   first band's go to the earliest-listed levels).
3. Each block is shuffled (in band order) with one ``random.Random(derive_seed(
   seed, "personas", <attribute>))`` and dealt to that band's Personas in Persona
   order, so every replicate gets its own slot and its own demographic draw.
4. Replicate copies of one profile in one band whose four quota levels coincide are
   made distinct by swapping one attribute's level with a Persona of the same band
   (see ``_separate_replicates``); counts per band and marginals are unchanged.

The shuffle is an explicit Fisher-Yates driven only by ``Random.getrandbits``
(Mersenne Twister output, stable across Python versions), never ``random.shuffle``.
"""

from __future__ import annotations

import logging
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
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
# Bump when the generation or card algorithm changes; written to panel/personas/meta.json.
# Story 2.4 kept "1": the full grid once gives byte-identical cards and index.json, and a
# meta.json without ``design`` means that full grid.
GENERATOR_VERSION = "1"

log = logging.getLogger(__name__)


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


class _BigFive(Protocol):
    fraction: str
    replicates: int


class _Frame(Protocol):
    big_five: _BigFive
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


# Fractional designs (story 2.4). Factors are O, C, E, A, N (TRAITS order); high = +1,
# low = -1. Each fraction is the principal one: every generator word multiplies to +1.
FRACTIONS = ("1", "1/2", "1/4")
_LETTERS = "OCEAN"
# fraction -> (generators as (generated trait index, (generating trait indices)), resolution)
_DESIGNS: dict[str, tuple[tuple[tuple[int, tuple[int, ...]], ...], str | None]] = {
    "1": ((), None),
    "1/2": (((4, (0, 1, 2, 3)),), "V"),  # N = O*C*E*A; I = OCEAN
    "1/4": (((3, (0, 1)), (4, (0, 2))), "III"),  # A = O*C, N = O*E; I = OCA = OEN = CEAN
}


@dataclass(frozen=True)
class PanelDesign:
    """The Panel's profile design, written once to ``meta.json`` as ``design``.

    ``resolution`` is ``"V"``, ``"III"`` or None (full grid); ``generators`` e.g.
    ``("N=OCEA",)``; ``defining_relation`` e.g. ``"I=OCEAN"`` (None for the full grid);
    ``aliasing`` one ``"X=Y=..."`` chain per alias class other than I, every effect in
    trait letters, lowest order first (empty for the full grid).
    """

    fraction: str
    replicates: int
    profiles: int
    resolution: str | None
    generators: tuple[str, ...]
    defining_relation: str | None
    aliasing: tuple[str, ...]

    def as_json(self) -> dict[str, object]:
        return {
            "fraction": self.fraction,
            "replicates": self.replicates,
            "profiles": self.profiles,
            "resolution": self.resolution,
            "generators": list(self.generators),
            "defining_relation": self.defining_relation,
            "aliasing": list(self.aliasing),
        }


def _word(indices: frozenset[int]) -> str:
    return "".join(_LETTERS[i] for i in sorted(indices))


def _effect_key(effect: frozenset[int]) -> tuple[int, tuple[int, ...]]:
    return (len(effect), tuple(sorted(effect)))


def design_profiles(
    fraction: str, replicates: int = 1
) -> tuple[list[dict[Trait, Pole]], PanelDesign]:
    """The profiles of ``fraction`` (``"1"``, ``"1/2"`` or ``"1/4"``) and the design (pure).

    The profiles are a filter of ``big_five_profiles()`` (bit-pattern order kept).
    """
    try:
        generators, resolution = _DESIGNS[fraction]
    except KeyError:
        raise ValueError(f"unknown fraction {fraction!r}; expected one of {FRACTIONS}") from None
    profiles = [
        p
        for p in big_five_profiles()
        if all(
            _sign(p, generated) == _product(p, generating)
            for generated, generating in generators
        )
    ]
    words = {frozenset((generated, *generating)) for generated, generating in generators}
    group = {frozenset()}
    for w in words:
        group |= {g ^ w for g in group}
    defining = sorted(group - {frozenset()}, key=_effect_key)
    classes: list[list[frozenset[int]]] = []
    if defining:
        seen: set[frozenset[int]] = set()
        effects = [
            frozenset(i for i in range(len(TRAITS)) if (mask >> i) & 1)
            for mask in range(1, 2 ** len(TRAITS))
        ]
        for effect in sorted(effects, key=_effect_key):
            if effect in seen:
                continue
            chain = sorted({effect ^ g for g in group}, key=_effect_key)
            seen.update(chain)
            if frozenset() not in chain:
                classes.append(chain)
    design = PanelDesign(
        fraction=fraction,
        replicates=replicates,
        profiles=len(profiles),
        resolution=resolution,
        generators=tuple(
            f"{_LETTERS[generated]}={_word(frozenset(generating))}"
            for generated, generating in generators
        ),
        defining_relation="=".join(["I", *(_word(w) for w in defining)]) if defining else None,
        aliasing=tuple("=".join(_word(e) for e in chain) for chain in classes),
    )
    return profiles, design


def _sign(profile: Mapping[Trait, Pole], index: int) -> int:
    return 1 if profile[TRAITS[index]] == "high" else -1


def _product(profile: Mapping[Trait, Pole], indices: Sequence[int]) -> int:
    out = 1
    for i in indices:
        out *= _sign(profile, i)
    return out


def _separate_replicates(
    blocks: Mapping[str, list[list[str]]], replicates: int, band_names: Sequence[str]
) -> None:
    """Make replicate copies of each profile within a band differ in some quota level.

    Within a band, slot ``q * replicates + r`` is replicate ``r`` of profile ``q``.
    Walking the band's slots in order, a slot whose level tuple equals another copy's
    of its profile swaps one attribute's level (attributes in ``QUOTA_ATTRIBUTES``
    order) with the first other slot of the band (in slot order) for which both groups
    then hold distinct tuples. Swaps stay within the band, so every count is kept.
    Logs ``replicates_indistinct`` when no such swap exists. Deterministic.
    """
    if replicates < 2:
        return
    for b, band in enumerate(band_names):
        cols = [blocks[attr][b] for attr in QUOTA_ATTRIBUTES]
        size = len(cols[0])

        def tup(i: int, cols: list[list[str]] = cols) -> tuple[str, ...]:
            return tuple(col[i] for col in cols)

        def mates(i: int) -> range:
            q = i // replicates
            return range(q * replicates, (q + 1) * replicates)

        def distinct(i: int) -> bool:
            return all(tup(i) != tup(m) for m in mates(i) if m != i)

        for i in range(size):
            if distinct(i):
                continue
            fixed = False
            for col in cols:
                for j in range(size):
                    if j // replicates == i // replicates or col[i] == col[j]:
                        continue
                    col[i], col[j] = col[j], col[i]
                    if distinct(i) and distinct(j) and all(
                        distinct(m) for m in mates(i) if m < i
                    ):
                        fixed = True
                        break
                    col[i], col[j] = col[j], col[i]
                if fixed:
                    break
            if not fixed:
                log.warning(
                    "replicates_indistinct: band %s: replicates of profile %d share every "
                    "quota level (too few level combinations)",
                    band, i // replicates + 1,
                )


def _warn_empty_levels(
    blocks: Mapping[str, list[list[str]]], levels: Mapping[str, Sequence[str]],
    band_names: Sequence[str],
) -> None:
    for attr in QUOTA_ATTRIBUTES:
        for b, band in enumerate(band_names):
            missing = [lv for lv in levels[attr] if lv not in blocks[attr][b]]
            if missing:
                log.warning(
                    "quota_levels_empty: %s: band %s has no Persona at %s (a band holds "
                    "profiles x replicates = %d Personas)",
                    attr, band, ", ".join(missing), len(blocks[attr][b]),
                )


def generate_personas(cfg: _Study) -> list[Persona]:
    """The Persona pool for ``cfg`` (a ``StudyConfig``): ``p1 ... pN``.

    N = profiles x replicates x bands, where the profiles come from
    ``design_profiles(cfg.personas.big_five.fraction)``; order is profile, then
    replicate, then band.
    """
    frame = cfg.personas
    bands = list(frame.nars_bands)
    replicates = frame.big_five.replicates
    profiles, _ = design_profiles(frame.big_five.fraction, replicates)
    grid = [
        (profile, band)
        for profile in profiles
        for _replicate in range(replicates)
        for band in bands
    ]
    levels = {attr: list(getattr(frame.quotas, attr)) for attr in QUOTA_ATTRIBUTES}
    blocks = {
        attr: stratified_levels(
            levels[attr],
            len(bands),
            len(profiles) * replicates,
            derive_seed(cfg.seed, SEED_PURPOSE, attr),
        )
        for attr in QUOTA_ATTRIBUTES
    }
    _warn_empty_levels(blocks, levels, bands)
    _separate_replicates(blocks, replicates, bands)
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
