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
"""

from __future__ import annotations

import json
import random

from consortium.core.render import ClipRef, TrialRequest, canonical_json
from consortium.core.seeds import derive_seed
from consortium.raters.base import Handle, MediaRef, RaterCall, RaterResult

FAKE_BUILD = "fake-1"
FREE_TEXT_ANSWER = "fake answer"
NOT_JSON_ANSWER = "I think it is about a 4."
INVALID_KINDS = ("not_json", "missing_item", "out_of_range")


INVALID_PURPOSE = "fake_invalid"


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

    def __init__(
        self, input_tokens: int = 0, output_tokens: int = 0, invalid_rate: float = 0.0
    ) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.invalid_rate = invalid_rate

    def answer(self, call: RaterCall) -> str:
        """The raw answer to ``call`` (valid, or invalid at ``invalid_rate``)."""
        if is_invalid_attempt(call.seed, self.invalid_rate):
            return fake_invalid_answer(call.request, call.seed, call.attempt)
        return fake_answer(call.request, call.seed)

    async def prepare(self, clip: ClipRef) -> MediaRef:
        return MediaRef(clip.clip_id, clip.sha256, f"fake:{clip.clip_id}")

    async def submit(self, calls: list[RaterCall]) -> list[Handle]:
        return [
            {"provider": self.provider, "trial_id": c.trial_id, "attempt": c.attempt,
             "raw": self.answer(c)}
            for c in calls
        ]

    async def collect(self, handles: list[Handle]) -> list[RaterResult]:
        return [
            RaterResult(
                raw=h["raw"],
                usage={"input_tokens": self.input_tokens, "output_tokens": self.output_tokens},
                model_build=FAKE_BUILD,
                category="ok",
            )
            for h in handles
        ]
