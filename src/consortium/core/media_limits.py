"""Per-Model media limits: does the worst-case Trial fit what each Model accepts? (pure)

A Trial sends the used Practice clips of its Instrument plus its target(s).
The worst case per Instrument shape is the single largest target, or the two
largest distinct targets for a pairwise Instrument, picked separately for
seconds and for bytes. Bytes count ``4 * ceil(size / 3)`` per Clip when the
Model takes media inline as base64.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol


class _Limits(Protocol):
    max_seconds: float
    max_bytes: int
    inline_base64: bool


class ModelLike(Protocol):
    @property
    def id(self) -> str: ...

    @property
    def limits(self) -> _Limits: ...


@dataclass(frozen=True)
class TrialShape:
    """One Instrument's Trials: the Practice clips every Trial carries, and the target pool."""

    instrument: str
    pairwise: bool
    practice: tuple[str, ...]  # Clip IDs of the used Practice examples, one entry per use
    targets: tuple[str, ...]


@dataclass(frozen=True)
class MediaViolation:
    model: str
    limit: str  # "max_seconds" or "max_bytes"
    allowed: float
    total: float
    shape: TrialShape
    targets: tuple[str, ...]

    @property
    def message(self) -> str:
        kind = "pairwise" if self.shape.pairwise else "single"
        picked = ",".join(self.targets) if self.targets else "no target"
        return (
            f"{self.model} {self.limit} {_num(self.allowed)} < {_num(self.total)} "
            f"({len(self.shape.practice)} practice + {kind} {picked})"
        )


def _num(value: float) -> str:
    value = round(float(value), 3)
    return str(int(value)) if value.is_integer() else repr(value)


def encoded_bytes(size: int, inline_base64: bool) -> int:
    """Bytes a Clip of ``size`` costs in a request: base64 is ``4 * ceil(size / 3)``."""
    return 4 * ((size + 2) // 3) if inline_base64 else size


def _worst(targets: Sequence[str], count: int, metric: Any) -> tuple[str, ...]:
    ranked = sorted(set(targets), key=lambda c: (-metric(c), c))
    return tuple(ranked[:count])


def check_media(
    trial_shapes: Sequence[TrialShape],
    clips: Mapping[str, Mapping[str, Any]],
    models: Sequence[ModelLike],
) -> MediaViolation | None:
    """The first worst-case Trial that exceeds a Model's limit, or None if all fit.

    ``clips`` maps Clip ID to a row with ``duration_s`` and ``size_bytes``.
    Checks Models in order, then shapes in order, seconds before bytes.
    A total equal to the limit fits (totals are rounded to 6 decimals first).
    """
    for model in models:
        limits = model.limits

        def seconds(clip_id: str) -> float:
            return float(clips[clip_id]["duration_s"])

        def size(clip_id: str, b64: bool = limits.inline_base64) -> int:
            return encoded_bytes(int(clips[clip_id]["size_bytes"]), b64)

        for shape in trial_shapes:
            count = 2 if shape.pairwise else 1
            for name, metric, allowed in (
                ("max_seconds", seconds, limits.max_seconds),
                ("max_bytes", size, limits.max_bytes),
            ):
                picked = _worst(shape.targets, count, metric)
                total = sum(metric(c) for c in shape.practice) + sum(metric(c) for c in picked)
                if round(total, 6) > allowed:
                    return MediaViolation(model.id, name, allowed, total, shape, picked)
    return None
