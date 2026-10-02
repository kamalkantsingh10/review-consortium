"""``FakeRater``: a deterministic, offline, free Rater.

Each Item is answered in its scale from ``random.Random(call.seed)``, in the
request's Item order: a Likert Item gets ``randint(1, points)``, a pairwise Item
one of its options (a position), a free-text Item a fixed string. The answer is
the canonical JSON of ``{item_id: value}``, which matches the Instrument's
response schema. The handle carries the answer itself, so ``collect`` works
after a restart. Usage is fixed per Model: ``input_tokens`` and
``output_tokens`` from its ``fake`` settings in ``study.yaml`` (default 0), so
the ledger and the ceiling can be exercised offline. No network.

With ``invalid_rate`` (``fake.invalid_rate`` in ``study.yaml``, 0-1) an attempt
answers invalidly when ``random.Random(derive_seed(call.seed, "fake_invalid",
"")).random() < invalid_rate`` (a stream separate from the answer's, so valid
answers stay unbiased); the outcome is fixed by the attempt seed. Invalid answers rotate with the
attempt number through ``INVALID_KINDS``: not JSON, a missing Item, and an
out-of-range value (a Likert value above the scale, a pairwise choice that is
not an option, an empty free text). The Fake rater never validates anything.

Simulated provider outcomes (story 2.1): with ``transient_rate``, ``refusal_rate``
and ``fatal_rate`` (each 0-1) an attempt's outcome is drawn per kind from its own
stream, ``random.Random(derive_seed(call.seed, "fake_<kind>", "")).random() <
rate``, checked in the order fatal, refused, transient, then invalid; the first
hit decides. A transient ``raw`` alternates with the attempt number between a
simulated rate limit (odd attempts) and a simulated transport error (even). The
outcome is decided at submit time and carried in the handle (``category``);
``collect`` returns it, with zero usage for every non-``ok`` result.

Persona fidelity (story 3.1): ``fidelity`` is ``random`` (default: every Item as
above, byte-identical to before), ``faithful`` or ``unfaithful``. With ``cues``
(``FidelityCues``: card sentence -> ``(construct, pole)`` and keyed Item id ->
``(construct, reversed)``), a keyed Likert Item whose construct appears on the
request's Persona card is answered ``points`` when the pole is ``high`` XOR the Item
is reversed and ``1`` otherwise (``faithful``), or the opposite (``unfaithful``).
Every other Item is answered at random as above; the random stream is drawn for
every Item either way, so unkeyed answers do not depend on the mode.

Perception (story 3.2): ``perception`` is ``random`` (default, unchanged), ``faithful``
or ``unfaithful``. Each Clip has a hidden latent per Item, ``fake_latent(clip_sha256,
item_id)`` in ``[0, 1)`` (from ``derive_seed(0, "fake_latent", "<sha>:<item>")``, so the
same for every Study). Only in a perception screening request (the neutral Persona card
``core.perception.NEUTRAL_CARD``; pilot and main Runs are answered as with ``random``)
with target Clips, a faithful rater answers a Likert
Item about one Clip ``1 + floor(latent * points)`` and a pairwise Item with the option of
the Clip whose latent is higher; an unfaithful rater uses ``1 - latent`` instead (capped
at ``points``). Keyed self-report Items and Items of clip-less requests are untouched; the
random stream is still drawn for every Item.
"""

from __future__ import annotations

import json
import math
import random
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

from consortium.core.perception import NEUTRAL_CARD
from consortium.core.render import ClipRef, TrialRequest, canonical_json
from consortium.core.seeds import derive_seed
from consortium.raters.base import Category, Handle, MediaRef, RaterCall, RaterResult

FAKE_BUILD = "fake-1"
FREE_TEXT_ANSWER = "fake answer"
NOT_JSON_ANSWER = "I think it is about a 4."
INVALID_KINDS = ("not_json", "missing_item", "out_of_range")


INVALID_PURPOSE = "fake_invalid"
# Checked in this order; the first hit decides the attempt's category.
OUTCOME_KINDS: tuple[tuple[str, Category], ...] = (
    ("fatal", "fatal"), ("refused", "refused"), ("transient", "transient"),
)
TRANSIENT_RAW = (
    "simulated rate limit: 429 RESOURCE_EXHAUSTED",
    "simulated transport error: connection reset",
)
REFUSED_RAW = "simulated refusal: the request was blocked for safety reasons"
FATAL_RAW = "simulated fatal error: 400 INVALID_ARGUMENT"

Fidelity = Literal["random", "faithful", "unfaithful"]
Perception = Literal["random", "faithful", "unfaithful"]
LATENT_PURPOSE = "fake_latent"


def fake_latent(clip_sha256: str, item_id: str) -> float:
    """The hidden latent of a Clip for an Item, in ``[0, 1)`` (deterministic, Study-free)."""
    return random.Random(derive_seed(0, LATENT_PURPOSE, f"{clip_sha256}:{item_id}")).random()


def perceived(latent: float, perception: Perception) -> float:
    """The latent a ``perception`` rater answers from: ``latent``, or ``1 - latent``."""
    return 1.0 - latent if perception == "unfaithful" else latent


def likert_from_latent(latent: float, points: int) -> int:
    """``1 + floor(latent * points)``, capped at ``points``."""
    return min(points, 1 + math.floor(latent * points))


@dataclass(frozen=True)
class FidelityCues:
    """What a faithful or unfaithful Fake rater reads (story 3.1)."""

    sentences: Mapping[str, tuple[str, str]] = field(default_factory=dict)  # -> (construct, pole)
    keys: Mapping[str, tuple[str, bool]] = field(default_factory=dict)  # -> (construct, reversed)


def card_poles(card: str, cues: FidelityCues) -> dict[str, str]:
    """``construct -> pole`` for every card line that is a known sentence."""
    out: dict[str, str] = {}
    for line in card.splitlines():
        found = cues.sentences.get(line)
        if found is not None:
            out[found[0]] = found[1]
    return out


def _hit(seed: int, kind: str, rate: float) -> bool:
    if rate <= 0:
        return False
    return random.Random(derive_seed(seed, f"fake_{kind}", "")).random() < rate


def fake_category(
    seed: int, transient_rate: float = 0.0, refusal_rate: float = 0.0, fatal_rate: float = 0.0
) -> Category:
    """The simulated category of the attempt with ``seed`` (``ok``: answered).

    Each kind is drawn from its own stream, ``Random(derive_seed(seed, "fake_<kind>",
    ""))``, in the order fatal, refused, transient.
    """
    rates = {"fatal": fatal_rate, "refused": refusal_rate, "transient": transient_rate}
    for kind, category in OUTCOME_KINDS:
        if _hit(seed, kind, rates[kind]):
            return category
    return "ok"


def fake_error_raw(category: Category, attempt: int) -> str:
    """The error summary a non-``ok`` simulated result carries as ``raw``."""
    if category == "transient":
        return TRANSIENT_RAW[(attempt - 1) % len(TRANSIENT_RAW)]
    return REFUSED_RAW if category == "refused" else FATAL_RAW


def is_invalid_attempt(seed: int, invalid_rate: float) -> bool:
    """Whether the attempt with ``seed`` answers invalidly at ``invalid_rate``.

    Drawn from its own stream, ``Random(derive_seed(seed, "fake_invalid", ""))``,
    so it never biases the valid answer drawn from ``Random(seed)``.
    """
    if invalid_rate <= 0:
        return False
    return random.Random(derive_seed(seed, INVALID_PURPOSE, "")).random() < invalid_rate


def fake_invalid_answer(
    request: TrialRequest,
    seed: int,
    attempt: int,
    fidelity: Fidelity = "random",
    cues: FidelityCues | None = None,
    perception: Perception = "random",
) -> str:
    """An invalid raw answer, of kind ``INVALID_KINDS[(attempt - 1) % 3]``."""
    kind = INVALID_KINDS[(attempt - 1) % len(INVALID_KINDS)]
    if kind == "not_json":
        return NOT_JSON_ANSWER
    answer = json.loads(fake_answer(request, seed, fidelity, cues, perception))
    first = request.items[0]
    if kind == "missing_item":
        del answer[first.id]
    elif first.type == "likert":
        answer[first.id] = (first.points or 1) + 1
    elif first.type == "pairwise":
        answer[first.id] = "C" if "C" not in (first.options or ()) else "Z"
    else:
        answer[first.id] = ""
    return canonical_json(answer).decode("utf-8")


def fake_answer(
    request: TrialRequest,
    seed: int,
    fidelity: Fidelity = "random",
    cues: FidelityCues | None = None,
    perception: Perception = "random",
) -> str:
    """The raw answer the Fake rater gives to ``request`` with ``seed``."""
    rng = random.Random(seed)
    # Only perception screening requests (the neutral card): pilot and main Runs unchanged.
    screening = perception != "random" and request.persona_card == NEUTRAL_CARD
    clips = request.clips if screening else ()
    poles = card_poles(request.persona_card, cues) if fidelity != "random" and cues else {}
    answer: dict[str, int | str] = {}
    for item in request.items:
        if item.type == "likert":
            points = item.points or 1
            answer[item.id] = rng.randint(1, points)
            key = cues.keys.get(item.id) if poles and cues else None
            if key is not None and key[0] in poles:
                high = (poles[key[0]] == "high") != key[1]
                if fidelity == "unfaithful":
                    high = not high
                answer[item.id] = points if high else 1
            elif len(clips) == 1:
                latent = perceived(fake_latent(clips[0].sha256, item.id), perception)
                answer[item.id] = likert_from_latent(latent, points)
        elif item.type == "pairwise":
            options = list(item.options or ("A", "B"))
            answer[item.id] = rng.choice(options)
            if len(clips) == 2:
                a, b = (perceived(fake_latent(c.sha256, item.id), perception) for c in clips)
                answer[item.id] = options[0] if a > b else options[1]
        else:
            answer[item.id] = FREE_TEXT_ANSWER
    return canonical_json(answer).decode("utf-8")


class FakeRater:
    provider = "fake"

    async def aclose(self) -> None:
        """Nothing to release."""

    def __init__(
        self,
        input_tokens: int = 0,
        output_tokens: int = 0,
        invalid_rate: float = 0.0,
        transient_rate: float = 0.0,
        refusal_rate: float = 0.0,
        fatal_rate: float = 0.0,
        fidelity: Fidelity = "random",
        cues: FidelityCues | None = None,
        perception: Perception = "random",
    ) -> None:
        self.fidelity = fidelity
        self.perception = perception
        self.cues = cues
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.invalid_rate = invalid_rate
        self.transient_rate = transient_rate
        self.refusal_rate = refusal_rate
        self.fatal_rate = fatal_rate

    def category(self, call: RaterCall) -> Category:
        """The simulated category of ``call`` (see the module docstring)."""
        return fake_category(call.seed, self.transient_rate, self.refusal_rate, self.fatal_rate)

    def answer(self, call: RaterCall) -> str:
        """The raw answer to ``call`` (valid, or invalid at ``invalid_rate``)."""
        if is_invalid_attempt(call.seed, self.invalid_rate):
            return fake_invalid_answer(
                call.request, call.seed, call.attempt, self.fidelity, self.cues, self.perception
            )
        return fake_answer(call.request, call.seed, self.fidelity, self.cues, self.perception)

    async def prepare(self, clip: ClipRef) -> MediaRef:
        return MediaRef(clip.clip_id, clip.sha256, f"fake:{clip.clip_id}")

    def _handle(self, call: RaterCall) -> Handle:
        category = self.category(call)
        raw = self.answer(call) if category == "ok" else fake_error_raw(category, call.attempt)
        return {"provider": self.provider, "trial_id": call.trial_id, "attempt": call.attempt,
                "category": category, "raw": raw}

    async def submit(self, calls: list[RaterCall]) -> list[Handle]:
        return [self._handle(c) for c in calls]

    async def collect(self, handles: list[Handle]) -> list[RaterResult]:
        out = []
        for h in handles:
            category = h.get("category", "ok")  # handles from before story 2.1 are answers
            ok = category == "ok"
            out.append(RaterResult(
                raw=h["raw"],
                usage={"input_tokens": self.input_tokens if ok else 0,
                       "output_tokens": self.output_tokens if ok else 0},
                model_build=FAKE_BUILD,
                category=category,
            ))
        return out
