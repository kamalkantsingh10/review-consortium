"""Render a Trial into a provider-neutral ``TrialRequest`` (pure: no I/O).

A request holds only what the Model is shown: the Persona card, the
Instrument's instructions and Items (with anchors and response schema), the
Prompt variant text, the Instrument's Practice examples (Clip ID + SHA-256 +
intended answer) and 0-2 target Clips (Clip ID + SHA-256). It carries no Trial,
Session, Test, Agent or Model ID, nothing from any other Trial, no Condition and
no provider setting; the Archive keys it by ``(trial_id, attempt)`` from outside.
Adapters add pinned settings only and never change its text.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from consortium.core.errors import ConsortiumError
from consortium.core.plan import Trial

# Structural views of the config models, so core never imports config.


class _Anchors(Protocol):
    low: str
    high: str


class _Item(Protocol):
    id: str
    type: str
    text: str
    points: int | None
    anchors: _Anchors | None
    options: list[str] | None


class _Instrument(Protocol):
    instructions: str
    prompt_variants: Mapping[str, str]
    items: Sequence[_Item]

    def response_schema(self) -> dict[str, Any]: ...


class _Practice(Protocol):
    instrument: str
    clips: Sequence[str]
    answer: Mapping[str, Any]


@dataclass(frozen=True)
class ClipRef:
    clip_id: str
    sha256: str


@dataclass(frozen=True)
class Anchors:
    low: str
    high: str


@dataclass(frozen=True)
class RequestItem:
    id: str
    type: str  # likert | pairwise | free_text
    text: str
    points: int | None
    anchors: Anchors | None
    options: tuple[str, ...] | None


@dataclass(frozen=True)
class PracticeExample:
    clips: tuple[ClipRef, ...]
    answer: Mapping[str, Any]  # Item id -> intended answer


@dataclass(frozen=True)
class TrialRequest:
    """Everything a Model sees for one Trial, fully determined by its inputs."""

    persona_card: str
    instructions: str
    items: tuple[RequestItem, ...]
    response_schema: Mapping[str, Any]
    prompt: str  # the Prompt variant text
    practice: tuple[PracticeExample, ...]
    clips: tuple[ClipRef, ...]  # 0-2 target Clips, in presentation order


def canonical_json(obj: Any) -> bytes:
    """Sorted keys, UTF-8 (``ensure_ascii=False``), no whitespace; dataclasses as objects."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        obj = dataclasses.asdict(obj)
    return json.dumps(
        obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def practice_for(
    practice: Sequence[_Practice], instrument: str, count: int
) -> list[_Practice]:
    """The first ``count`` Practice examples of ``instrument``, in list order."""
    return [ex for ex in practice if ex.instrument == instrument][:count]


def _clip(clip_id: str, clip_sha256: Mapping[str, str]) -> ClipRef:
    sha = clip_sha256.get(clip_id)
    if sha is None:
        raise ConsortiumError("unknown_clip", f"{clip_id!r} is not a pushed Clip")
    return ClipRef(clip_id, sha)


def _prompt(instrument: _Instrument, variant: str) -> str:
    text = instrument.prompt_variants.get(variant)
    if text is None:
        raise ConsortiumError(
            "unknown_prompt_variant", f"prompt variant {variant!r} is not defined by the Instrument"
        )
    return text


def _item(item: _Item) -> RequestItem:
    anchors = Anchors(item.anchors.low, item.anchors.high) if item.anchors else None
    options = tuple(item.options) if item.options is not None else None
    return RequestItem(item.id, item.type, item.text, item.points, anchors, options)


def render(
    trial: Trial,
    *,
    persona_card: str,
    instrument: _Instrument,
    practice: Sequence[_Practice],
    clip_sha256: Mapping[str, str],
) -> TrialRequest:
    """The ``TrialRequest`` for ``trial``.

    ``instrument`` is the Trial's ``InstrumentDef``; ``practice`` the Practice
    examples to include (already selected, see ``practice_for``); ``clip_sha256``
    maps Clip ID to SHA-256 for at least the target and Practice Clips (else
    ``unknown_clip``). ``persona_card`` is the card text exactly as stored.
    A Trial whose Prompt variant the Instrument does not define raises
    ``unknown_prompt_variant``.
    """
    return TrialRequest(
        persona_card=persona_card,
        instructions=instrument.instructions,
        items=tuple(_item(item) for item in instrument.items),
        response_schema=instrument.response_schema(),
        prompt=_prompt(instrument, trial.prompt_variant),
        practice=tuple(
            PracticeExample(
                clips=tuple(_clip(c, clip_sha256) for c in ex.clips),
                answer=dict(ex.answer),
            )
            for ex in practice
        ),
        clips=tuple(_clip(c, clip_sha256) for c in trial.clip_ids),
    )
