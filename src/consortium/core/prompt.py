"""Compose a ``TrialRequest`` into provider-neutral message parts (pure: no I/O).

``compose`` is the single, deterministic way a request becomes the parts every
adapter sends, so the text is identical across providers. Adapters map each
part one-to-one onto their SDK's part type and add pinned settings only.

Order: the Persona card, the instructions, each Practice example (a label, its
Clip(s), its intended answer as canonical JSON), the target Clip(s) (one target
after ``Video to rate:``). A Clip pair, target or Practice, is labelled with the
pairwise Items' option labels (``Video A:`` ...). Then the Prompt variant text,
the Items and the response schema as canonical JSON. Changing any fixed string
below, or the order, bumps ``PROMPT_FORMAT``.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Literal

from consortium.core.render import TrialRequest, canonical_json

PROMPT_FORMAT = 1

PRACTICE_LABEL = "Practice example {n}:"
INTENDED_ANSWER = "Intended answer: "
SINGLE_TARGET_LABEL = "Video to rate:"
PAIR_TARGET_LABEL = "Video {label}:"
ITEMS_PREFIX = "Items: "
SCHEMA_PREFIX = "Response schema: "


@dataclass(frozen=True)
class Text:
    text: str


@dataclass(frozen=True)
class Media:
    clip_id: str
    sha256: str
    role: Literal["practice", "target"]


Part = Text | Media


def _json(obj: object) -> str:
    return canonical_json(obj).decode("utf-8")


def _pair_labels(request: TrialRequest) -> tuple[str, str]:
    """The pairwise Items' two option labels; ValueError when there are none or they differ."""
    labels = {tuple(i.options or ()) for i in request.items if i.type == "pairwise"}
    if len(labels) != 1:
        raise ValueError("a Clip pair needs pairwise Items sharing one set of option labels")
    found = labels.pop()
    if len(found) != 2:
        raise ValueError(f"pairwise Item options must be exactly 2 labels, not {list(found)}")
    return found[0], found[1]


def _clips(request: TrialRequest, clips: tuple, role: Literal["practice", "target"]) -> list[Part]:
    if len(clips) > 2:
        raise ValueError(f"at most 2 Clips per {role} example or Trial, not {len(clips)}")
    if len(clips) == 2:
        out: list[Part] = []
        for label, clip in zip(_pair_labels(request), clips, strict=True):
            out += [Text(PAIR_TARGET_LABEL.format(label=label)),
                    Media(clip.clip_id, clip.sha256, role)]
        return out
    if role == "target" and clips:
        return [Text(SINGLE_TARGET_LABEL), Media(clips[0].clip_id, clips[0].sha256, role)]
    return [Media(c.clip_id, c.sha256, role) for c in clips]


def compose(request: TrialRequest) -> tuple[Part, ...]:
    """The message parts of ``request``, in the fixed order (see the module docstring).

    ValueError for more than 2 Clips in a Trial or Practice example, or a Clip pair
    whose pairwise Items do not share exactly 2 option labels.
    """
    parts: list[Part] = [Text(request.persona_card), Text(request.instructions)]
    for n, example in enumerate(request.practice, start=1):
        parts.append(Text(PRACTICE_LABEL.format(n=n)))
        parts.extend(_clips(request, example.clips, "practice"))
        parts.append(Text(INTENDED_ANSWER + _json(dict(example.answer))))
    parts.extend(_clips(request, request.clips, "target"))
    parts.append(Text(request.prompt))
    parts.append(Text(ITEMS_PREFIX + _json([dataclasses.asdict(i) for i in request.items])))
    parts.append(Text(SCHEMA_PREFIX + _json(dict(request.response_schema))))
    return tuple(parts)
