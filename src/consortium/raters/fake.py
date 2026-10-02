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
"""

from __future__ import annotations

import json
import random

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


def fake_invalid_answer(request: TrialRequest, seed: int, attempt: int) -> str:
    """An invalid raw answer, of kind ``INVALID_KINDS[(attempt - 1) % 3]``."""
    kind = INVALID_KINDS[(attempt - 1) % len(INVALID_KINDS)]
    if kind == "not_json":
        return NOT_JSON_ANSWER
    answer = json.loads(fake_answer(request, seed))
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


def fake_answer(request: TrialRequest, seed: int) -> str:
    """The raw answer the Fake rater gives to ``request`` with ``seed``."""
    rng = random.Random(seed)
    answer: dict[str, int | str] = {}
    for item in request.items:
        if item.type == "likert":
            answer[item.id] = rng.randint(1, item.points or 1)
        elif item.type == "pairwise":
            answer[item.id] = rng.choice(list(item.options or ("A", "B")))
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
    ) -> None:
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
            return fake_invalid_answer(call.request, call.seed, call.attempt)
        return fake_answer(call.request, call.seed)

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
