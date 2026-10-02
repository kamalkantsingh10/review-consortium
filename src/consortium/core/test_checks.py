"""Pure Test checks shared by ``push test`` and ``open``: pairing plan, Practice, media limits.

``fail(code, message)`` builds the ``ConsortiumError`` to raise, so each caller
attaches its own path. The first failure wins.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol

from consortium.core.errors import ConsortiumError
from consortium.core.media_limits import ModelLike, TrialShape, check_media

Fail = Callable[[str, str], ConsortiumError]


class _Practice(Protocol):
    instrument: str
    clips: Sequence[str]


class _Test(Protocol):
    instruments: Sequence[str]
    clips: Sequence[str]
    practice: Sequence[_Practice]


class _Instrument(Protocol):
    @property
    def pairwise(self) -> bool: ...


def check_plan(
    test: _Test,
    instruments: Mapping[str, _Instrument],
    practice_per_instrument: int,
    fail: Fail,
) -> list[TrialShape]:
    """Plan (``bad_pairing``) then Practice (``bad_practice``) checks; returns the Trial shapes.

    Every Instrument the Test lists must be in ``instruments`` (else ``unknown_instrument``).
    """
    for i, name in enumerate(test.instruments):
        if name not in instruments:
            raise fail("unknown_instrument", f"instruments.{i}: {name!r} is not enabled")

    # Plan.
    if not test.clips:
        raise fail("bad_pairing", "clips: a Test needs at least 1 target Clip")
    for name in test.instruments:
        if instruments[name].pairwise and len(test.clips) < 2:
            raise fail(
                "bad_pairing",
                f"clips: pairwise Instrument {name!r} needs at least 2 target Clips, "
                f"got {len(test.clips)}",
            )

    # Practice.
    targets = set(test.clips)
    for i, example in enumerate(test.practice):
        if len(set(example.clips)) != len(example.clips):
            raise fail("bad_practice", f"practice[{i}]: lists the same Clip twice")
        for clip in example.clips:
            if clip in targets:
                raise fail(
                    "bad_practice",
                    f"practice: {clip} is both a Practice clip (practice[{i}]) and a target",
                )
    shapes: list[TrialShape] = []
    for name in test.instruments:
        indexes = [i for i, ex in enumerate(test.practice) if ex.instrument == name]
        if len(indexes) < practice_per_instrument:
            field = f"practice[{indexes[-1]}]" if indexes else "practice"
            raise fail(
                "bad_practice",
                f"{field}: Instrument {name!r} has {len(indexes)} Practice example(s); "
                f"session.practice_clips needs {practice_per_instrument}",
            )
        used = tuple(
            c for i in indexes[:practice_per_instrument] for c in test.practice[i].clips
        )
        shapes.append(TrialShape(name, instruments[name].pairwise, used, tuple(test.clips)))
    return shapes


def check_media_limits(
    shapes: Sequence[TrialShape],
    clips: Mapping[str, Mapping[str, Any]],
    models: Sequence[ModelLike],
    fail: Fail,
) -> None:
    """``media_limit_exceeded`` when a worst-case Trial exceeds a Model's limits."""
    violation = check_media(shapes, clips, models)
    if violation is not None:
        raise fail("media_limit_exceeded", violation.message)
