"""``FakeRater``: a deterministic, offline, free Rater.

Each Item is answered in its scale from ``random.Random(call.seed)``, in the
request's Item order: a Likert Item gets ``randint(1, points)``, a pairwise Item
one of its options (a position), a free-text Item a fixed string. The answer is
the canonical JSON of ``{item_id: value}``, which matches the Instrument's
response schema. The handle carries the answer itself, so ``collect`` works
after a restart. Usage is fixed per Model: ``input_tokens`` and
``output_tokens`` from its ``fake`` settings in ``study.yaml`` (default 0), so
the ledger and the ceiling can be exercised offline. No network.
"""

from __future__ import annotations

import random

from consortium.core.render import ClipRef, TrialRequest, canonical_json
from consortium.raters.base import Handle, MediaRef, RaterCall, RaterResult

FAKE_BUILD = "fake-1"
FREE_TEXT_ANSWER = "fake answer"


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

    def __init__(self, input_tokens: int = 0, output_tokens: int = 0) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens

    async def prepare(self, clip: ClipRef) -> MediaRef:
        return MediaRef(clip.clip_id, clip.sha256, f"fake:{clip.clip_id}")

    async def submit(self, calls: list[RaterCall]) -> list[Handle]:
        return [
            {"provider": self.provider, "trial_id": c.trial_id, "attempt": c.attempt,
             "raw": fake_answer(c.request, c.seed)}
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
