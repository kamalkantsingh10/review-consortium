"""Screening stamps (story 3.1): SHA-256 over canonical JSON (pure).

A screening result counts only while its stamps equal the current values
(story 3.3). Canonical JSON is ``core.render.canonical_json``: sorted keys,
UTF-8, no whitespace. Hashes are lowercase hex.

- ``settings_hash``: the Model's id, provider, pinned model, settings (without
  ``api_key_env``), ``max_output_tokens`` and, for a Fake Model, its ``fake`` settings.
- ``instrument_hash``: the Instrument definition(s) (a list in name order) and
  ``core.prompt.PROMPT_FORMAT`` (how a request becomes the text a Model sees).
- ``fidelity_hash``: the fidelity stamp, ``instrument_hash`` of the fidelity Instruments
  plus the SHA-256 of the Persona card wording file (what the cards say).
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from typing import Any, Protocol

from consortium.core.prompt import PROMPT_FORMAT
from consortium.core.render import canonical_json

FAKE_PROVIDER = "fake"


class _Dumpable(Protocol):
    def model_dump(self, **kwargs: Any) -> dict[str, Any]: ...


class _Model(Protocol):
    id: str
    provider: str
    model: str
    settings: _Dumpable
    max_output_tokens: int

    @property
    def fake_settings(self) -> _Dumpable: ...


class _Instrument(Protocol):
    name: str

    def model_dump(self, **kwargs: Any) -> dict[str, Any]: ...


def sha256_json(obj: Any) -> str:
    """SHA-256 (lowercase hex) of the canonical JSON of ``obj``."""
    return hashlib.sha256(canonical_json(obj)).hexdigest()


def settings_view(model: _Model) -> dict[str, Any]:
    """What ``settings_hash`` covers: id, provider, pinned model, settings (without
    ``api_key_env``), ``max_output_tokens`` and, for a Fake Model, its ``fake`` settings."""
    return {
        "id": model.id,
        "provider": model.provider,
        "model": model.model,
        "settings": model.settings.model_dump(mode="json", exclude={"api_key_env"}),
        "max_output_tokens": model.max_output_tokens,
        "fake": (
            model.fake_settings.model_dump(mode="json")
            if model.provider == FAKE_PROVIDER else None
        ),
    }


def settings_hash(model: _Model) -> str:
    """The Model settings stamp of a ``ModelConfig``."""
    return sha256_json(settings_view(model))


def instrument_hash(instrument: _Instrument | Sequence[_Instrument]) -> str:
    """The Instrument stamp: one ``InstrumentDef``, or a list of them in name order, with
    the prompt format."""
    if isinstance(instrument, Sequence):
        ordered = sorted(instrument, key=lambda i: i.name)
        definition: Any = [i.model_dump(mode="json") for i in ordered]
    else:
        definition = instrument.model_dump(mode="json")
    return sha256_json({"instruments": definition, "prompt_format": PROMPT_FORMAT})


def fidelity_hash(instruments: Sequence[_Instrument], wording_sha256: str) -> str:
    """The fidelity stamp: ``instrument_hash(instruments)`` and the card wording SHA-256."""
    return sha256_json({
        "card_wording_sha256": wording_sha256,
        "instrument_hash": instrument_hash(instruments),
    })
