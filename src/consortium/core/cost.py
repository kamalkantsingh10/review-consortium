"""The one cost formula (AD-13): pure, offline, in ``Decimal`` USD.

For one attempt of one Trial on Model ``m`` with price ``p = prices.models[m.id]``:

* media tokens = ceil(sum of ``duration_s`` of every Clip the request shows,
  Practice examples first then targets, each appearance counted) x
  ``p.media_tokens_per_s``);
* text tokens = ceil(characters of the request's canonical JSON / ``p.chars_per_token``);
* output tokens = the Model's pinned ``max_output_tokens``;
* cost = (media + text tokens) x ``p.input_usd_per_mtok`` / 1e6
  + output tokens x ``p.output_usd_per_mtok`` / 1e6.

``estimate`` is used both for the pre-Run estimate and for every per-attempt
reservation; ``actual`` prices the usage a Rater reports. Nothing here calls a
provider. USD amounts are ``Decimal``; ``usd`` formats one as a decimal string.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

from consortium.core.plan import Trial
from consortium.core.render import TrialRequest, canonical_json

MTOK = Decimal(1_000_000)

# Structural views of the config models, so core never imports config.


class _Price(Protocol):
    input_usd_per_mtok: Decimal
    output_usd_per_mtok: Decimal
    media_tokens_per_s: Decimal
    chars_per_token: Decimal


class _Prices(Protocol):
    @property
    def models(self) -> Mapping[str, _Price]: ...


class _Model(Protocol):
    id: str
    provider: str
    max_output_tokens: int


class _Study(Protocol):
    def model_by_id(self, model_id: str) -> Any: ...


def _ceil(value: Decimal) -> int:
    return math.ceil(value)


def _media_seconds(request: TrialRequest, clip_seconds: Mapping[str, float]) -> Decimal:
    clips = [c for example in request.practice for c in example.clips] + list(request.clips)
    return sum((Decimal(str(clip_seconds[c.clip_id])) for c in clips), Decimal(0))


def input_tokens(
    request: TrialRequest, price: _Price, clip_seconds: Mapping[str, float]
) -> int:
    """Estimated input tokens of ``request``: media tokens plus text tokens."""
    media = _ceil(_media_seconds(request, clip_seconds) * price.media_tokens_per_s)
    chars = len(canonical_json(request).decode("utf-8"))
    text = _ceil(Decimal(chars) / price.chars_per_token)
    return media + text


def _price(model: _Model, prices: _Prices) -> _Price:
    return prices.models[model.id]


def estimate(
    request: TrialRequest, model: _Model, prices: _Prices, clip_seconds: Mapping[str, float]
) -> Decimal:
    """USD cost of one attempt of ``request`` on ``model``, before it is sent.

    ``clip_seconds`` maps every Clip ID the request shows to its ``duration_s``.
    """
    price = _price(model, prices)
    tokens_in = input_tokens(request, price, clip_seconds)
    return (
        tokens_in * price.input_usd_per_mtok + model.max_output_tokens * price.output_usd_per_mtok
    ) / MTOK


def actual(usage: Mapping[str, Any] | None, model: _Model, prices: _Prices) -> Decimal | None:
    """USD cost of an answered attempt from the usage its Rater reported.

    ``None`` when the usage lacks a non-negative integer ``input_tokens`` or
    ``output_tokens`` (the attempt's reservation then stands as its cost).
    """
    if not isinstance(usage, Mapping):
        return None
    tokens = [usage.get("input_tokens"), usage.get("output_tokens")]
    if not all(isinstance(t, int) and not isinstance(t, bool) and t >= 0 for t in tokens):
        return None
    price = _price(model, prices)
    return (tokens[0] * price.input_usd_per_mtok + tokens[1] * price.output_usd_per_mtok) / MTOK


@dataclass(frozen=True)
class PlanEstimate:
    """The cost of sending a set of Trials once (``expected``) and with every retry used."""

    expected: Decimal
    worst_case: Decimal
    trials: int
    providers: tuple[str, ...]  # providers that receive Clips, in first-Trial order
    max_retries: int

    def lines(self) -> list[str]:
        """The estimate as printed by ``consortium open`` (identical for a dry run and a Run)."""
        return [
            f"cost estimate: expected {usd(self.expected)} USD, worst case "
            f"{usd(self.worst_case)} USD (max_retries {self.max_retries})",
            f"cost covers: {self.trials} Trials; Clips go to: "
            f"{', '.join(self.providers) or 'none'}",
        ]


def estimate_plan(
    plan: Any,
    requests: Iterable[TrialRequest],
    cfg: _Study,
    prices: _Prices,
    clip_seconds: Mapping[str, float],
    *,
    max_retries: int,
) -> PlanEstimate:
    """Sum ``estimate`` over every Trial of ``plan`` (a ``Plan`` or a sequence of Trials).

    ``requests`` are the Trials' rendered requests in the same order (consumed
    once). ``worst_case`` is ``expected x (1 + max_retries)``.
    """
    trials: Sequence[Trial] = plan.trials if hasattr(plan, "trials") else plan
    total = Decimal(0)
    count = 0
    providers: dict[str, None] = {}
    for trial, request in zip(trials, requests, strict=True):
        model = cfg.model_by_id(trial.model_id)
        total += estimate(request, model, prices, clip_seconds)
        count += 1
        if request.clips or request.practice:
            providers.setdefault(model.provider)
    return PlanEstimate(
        expected=total,
        worst_case=total * (1 + max_retries),
        trials=count,
        providers=tuple(providers),
        max_retries=max_retries,
    )


def usd(value: Decimal | None) -> str:
    """A USD amount as a plain decimal string (no exponent, no trailing zeros)."""
    if value is None:
        return "none"
    text = format(value.normalize(), "f")
    return "0" if text in ("-0", "") else text
