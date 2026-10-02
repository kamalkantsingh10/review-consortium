"""Story 3.1: ``core.hashes`` screening stamps."""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

from consortium.config.load import card_wording_sha256, load_fidelity_instruments, load_study
from consortium.config.models import ModelConfig
from consortium.core.hashes import (
    fidelity_hash,
    instrument_hash,
    settings_hash,
    settings_view,
    sha256_json,
)
from consortium.core.prompt import PROMPT_FORMAT
from consortium.core.render import canonical_json
from consortium.stages.init import init_study

GEMINI = {
    "id": "m2", "provider": "gemini", "model": "gemini-3-flash-preview",
    "settings": {"temperature": 0.7, "fps": 1.0, "api_key_env": "MY_KEY"},
    "max_output_tokens": 1024, "limits": {"max_seconds": 600, "max_bytes": 2_000_000},
}


def test_sha256_json_is_canonical() -> None:
    assert sha256_json({"b": 1, "a": "é"}) == hashlib.sha256(
        '{"a":"é","b":1}'.encode()).hexdigest()


def test_settings_hash_covers_pinned_settings_not_key_env() -> None:
    model = ModelConfig.model_validate(GEMINI)
    view = settings_view(model)
    assert set(view) == {"id", "provider", "model", "settings", "max_output_tokens", "fake"}
    assert "api_key_env" not in view["settings"] and view["fake"] is None
    assert settings_hash(model) == hashlib.sha256(canonical_json(view)).hexdigest()
    other_env = ModelConfig.model_validate(GEMINI | {"settings": {
        "temperature": 0.7, "fps": 1.0, "api_key_env": "OTHER"}})
    assert settings_hash(other_env) == settings_hash(model)
    for change in ({"model": "gemini-3-pro-preview"}, {"max_output_tokens": 512},
                   {"id": "m3"}, {"settings": {"temperature": 0.5, "fps": 1.0}}):
        assert settings_hash(ModelConfig.model_validate(GEMINI | change)) != settings_hash(model)
    assert len(settings_hash(model)) == 64 and settings_hash(model).islower()


def test_settings_hash_includes_fake_settings(tmp_path: Path) -> None:
    cfg = load_study(init_study(tmp_path / "s"))
    model = cfg.models[0]
    assert settings_view(model)["fake"]["fidelity"] == "random"
    faithful = model.model_copy(update={"fake": model.fake_settings.model_copy(
        update={"fidelity": "faithful"})})
    assert settings_hash(faithful) != settings_hash(model)


def test_instrument_hash_single_and_list(tmp_path: Path) -> None:
    study = init_study(tmp_path / "s")
    bfi, nars = load_fidelity_instruments(study, load_study(study))
    assert instrument_hash(bfi) == sha256_json(
        {"instruments": bfi.model_dump(mode="json"), "prompt_format": PROMPT_FORMAT})
    assert instrument_hash([nars, bfi]) == instrument_hash([bfi, nars]) == sha256_json({
        "instruments": [bfi.model_dump(mode="json"), nars.model_dump(mode="json")],
        "prompt_format": PROMPT_FORMAT})
    assert instrument_hash([bfi]) != instrument_hash(bfi)


def test_instrument_hash_covers_prompt_format(monkeypatch) -> None:
    import consortium.core.hashes as hashes

    instrument = SimpleNamespace(name="x", model_dump=lambda **_: {"name": "x"})
    before = instrument_hash(instrument)
    monkeypatch.setattr(hashes, "PROMPT_FORMAT", PROMPT_FORMAT + 1)
    assert instrument_hash(instrument) != before


def test_fidelity_hash_covers_card_wording(tmp_path: Path) -> None:
    study = init_study(tmp_path / "s")
    instruments = load_fidelity_instruments(study, load_study(study))
    stamp = fidelity_hash(instruments, card_wording_sha256())
    assert stamp == sha256_json({"card_wording_sha256": card_wording_sha256(),
                                 "instrument_hash": instrument_hash(instruments)})
    assert fidelity_hash(instruments, "0" * 64) != stamp
