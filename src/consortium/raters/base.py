"""The ``Rater`` port (AD-6): the only way a Model is called, always from ``engine.dispatch``.

``prepare`` makes a Clip available to the provider once (the engine caches the
result per Rater); ``submit`` sends calls and returns handles; ``collect`` turns
handles into results. ``submit`` and ``collect`` are awaited separately: no code
assumes a synchronous answer. Adapters import ``core`` only, add pinned
settings only and never change the request text.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol, get_args

from consortium.core.render import ClipRef, TrialRequest

# A handle is JSON-serialisable (stored as canonical JSON text in ``attempts.handle``),
# so ``collect`` can be called with it after a restart.
Handle = dict[str, Any]

# AD-6: every provider outcome is exactly one of these. ``ok``: an answer (validated by
# the engine); ``transient``: a rate limit or transport error (retried with backoff);
# ``refused``: a safety refusal (terminal, never validated or retried); ``fatal``: any
# other provider error (the Trial fails). Adapters never raise for a provider outcome.
Category = Literal["ok", "transient", "refused", "fatal"]
CATEGORIES: tuple[str, ...] = get_args(Category)


@dataclass(frozen=True)
class MediaRef:
    """A prepared Clip: its ID and SHA-256 plus the provider's reference (never archived)."""

    clip_id: str
    sha256: str
    ref: str


@dataclass(frozen=True)
class RaterCall:
    """``submit``'s input: one attempt of one Trial."""

    trial_id: str
    attempt: int
    seed: int
    request: TrialRequest
    media: tuple[MediaRef, ...]  # every Clip of the request, in order of first appearance


@dataclass(frozen=True)
class RaterResult:
    """The raw answer of one attempt and its ``Category``.

    ``raw`` is the provider text, or an error summary for a non-``ok`` result (never a
    secret). An error at submit time goes into the handle; ``collect`` returns it.
    """

    raw: str
    usage: dict[str, int]  # {input_tokens, output_tokens}
    model_build: str | None
    category: Category


class Rater(Protocol):
    provider: str  # one asyncio.Semaphore per provider caps concurrency

    async def prepare(self, clip: ClipRef) -> MediaRef: ...

    async def submit(self, calls: list[RaterCall]) -> list[Handle]: ...

    async def collect(self, handles: list[Handle]) -> list[RaterResult]: ...
