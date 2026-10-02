"""core.validate: the story 1.10 response-validation matrix for the three built-in Instruments."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from consortium.config.load import load_instruments, load_study
from consortium.config.models import InstrumentDef
from consortium.core.errors import ConsortiumError
from consortium.core.validate import ParsedAnswer, invalid_rate, validate_response
from consortium.stages.init import init_study

GODSPEED = {f"animacy_{i}": 4 for i in range(1, 7)} | {f"likeability_{i}": 2 for i in range(1, 6)}


@pytest.fixture(scope="module")
def instruments(tmp_path_factory: pytest.TempPathFactory) -> dict[str, InstrumentDef]:
    study = init_study(tmp_path_factory.mktemp("validate") / "study")
    return load_instruments(study, load_study(study))


def _reason(raw: str, instrument: InstrumentDef) -> str:
    with pytest.raises(ConsortiumError) as info:
        validate_response(raw, instrument)
    assert info.value.code == "invalid_response"
    return info.value.message


def test_valid_likert(instruments) -> None:
    parsed = validate_response(json.dumps(GODSPEED), instruments["godspeed"])
    assert parsed == ParsedAnswer("godspeed", GODSPEED)
    assert list(parsed.answers) == [item.id for item in instruments["godspeed"].items]


def test_valid_presence_and_pairwise(instruments) -> None:
    assert validate_response('{"presence_1": 7}', instruments["presence"]).answers == {
        "presence_1": 7}
    for choice in ("A", "B"):
        parsed = validate_response(json.dumps({"alive": choice}), instruments["pairwise_alive"])
        assert parsed == ParsedAnswer("pairwise_alive", {"alive": choice})


@pytest.mark.parametrize("raw", [
    "```json " + json.dumps(GODSPEED) + "```",
    "```json\n" + json.dumps(GODSPEED, indent=2) + "\n```",
    "  ```\n" + json.dumps(GODSPEED) + "\n```\n",
    "\n" + json.dumps(GODSPEED) + "\n",
])
def test_fenced_or_padded_json(instruments, raw: str) -> None:
    assert validate_response(raw, instruments["godspeed"]).answers == GODSPEED


@pytest.mark.parametrize("raw", [
    "I think 4",
    "",
    "Sure! " + json.dumps(GODSPEED),
    json.dumps(GODSPEED) + " hope that helps",
    json.dumps(GODSPEED) + json.dumps(GODSPEED),
    "```json\n" + json.dumps(GODSPEED) + "\n```\n```json\n" + json.dumps(GODSPEED) + "\n```",
    "```json\n" + json.dumps(GODSPEED),
    json.dumps(GODSPEED).replace("4", "NaN", 1),
])
def test_not_json(instruments, raw: str) -> None:
    assert _reason(raw, instruments["godspeed"]) == "not_json"


@pytest.mark.parametrize("raw", ["[4, 4]", "4", '"A"', "null"])
def test_not_object(instruments, raw: str) -> None:
    assert _reason(raw, instruments["presence"]) == "not_object"


@pytest.mark.parametrize(("name", "answer", "item"), [
    ("godspeed", GODSPEED | {"animacy_3": 6}, "animacy_3"),
    ("godspeed", GODSPEED | {"likeability_5": 0}, "likeability_5"),
    ("presence", {"presence_1": 8}, "presence_1"),
    ("presence", {"presence_1": -1}, "presence_1"),
])
def test_out_of_range(instruments, name: str, answer: dict, item: str) -> None:
    assert _reason(json.dumps(answer), instruments[name]) == f"out_of_range:{item}"


@pytest.mark.parametrize("value", ["4", 4.0, True, None, [4]])
def test_likert_wrong_type_is_not_coerced(instruments, value) -> None:
    raw = json.dumps(GODSPEED | {"animacy_1": value})
    assert _reason(raw, instruments["godspeed"]) == "wrong_type:animacy_1"


def test_missing_and_unknown_items(instruments) -> None:
    missing = dict(GODSPEED)
    del missing["likeability_2"]
    assert _reason(json.dumps(missing), instruments["godspeed"]) == "missing_item"
    assert _reason("{}", instruments["presence"]) == "missing_item"
    assert _reason('{"choice": "A"}', instruments["pairwise_alive"]) == "unknown_item"
    extra = GODSPEED | {"animacy_7": 3}
    assert _reason(json.dumps(extra), instruments["godspeed"]) == "unknown_item"
    assert _reason('{"presence_1": 3, "presence_1": 4}', instruments["presence"]) == \
        "duplicate_item"


@pytest.mark.parametrize("value", ["C", "a", ""])
def test_pairwise_bad_choice(instruments, value) -> None:
    raw = json.dumps({"alive": value})
    assert _reason(raw, instruments["pairwise_alive"]) == "bad_choice:alive"


@pytest.mark.parametrize("value", [1, None, ["A"], True])
def test_pairwise_wrong_type(instruments, value) -> None:
    raw = json.dumps({"alive": value})
    assert _reason(raw, instruments["pairwise_alive"]) == "wrong_type:alive"


def test_duplicate_key_rules(instruments) -> None:
    dup = '{"presence_1": 3, "presence_1": 4}'
    assert _reason(dup, instruments["presence"]) == "duplicate_item"
    assert _reason(dup + " thanks", instruments["presence"]) == "not_json"
    assert _reason('[{"a": 1, "a": 2}]', instruments["presence"]) == "not_object"


def test_backticks_inside_fenced_free_text() -> None:
    inst = InstrumentDef.model_validate({
        "schema_version": 1, "name": "notes", "version": "1",
        "instructions": "x", "prompt_variants": {"default": "y"},
        "items": [{"id": "why", "type": "free_text", "text": "Why?"}],
    })
    raw = '```json\n{"why": "it typed ``` then stopped"}\n```'
    assert validate_response(raw, inst).answers == {"why": "it typed ``` then stopped"}


def test_bad_instrument_is_a_programming_error() -> None:
    item = SimpleNamespace(id="x", type="likert", points=None, options=None)
    with pytest.raises(ValueError):
        validate_response('{"x": 1}', SimpleNamespace(name="i", items=[item]))
    item = SimpleNamespace(id="x", type="slider", points=None, options=None)
    with pytest.raises(ValueError):
        validate_response('{"x": 1}', SimpleNamespace(name="i", items=[item]))


def test_matrix_bad_choice_with_unknown_key(instruments) -> None:
    # The matrix's {"choice": "C"}: "choice" is not this Instrument's Item.
    assert _reason('{"choice":"C"}', instruments["pairwise_alive"]) == "unknown_item"


def test_free_text_and_custom_options() -> None:
    inst = InstrumentDef.model_validate({
        "schema_version": 1, "name": "notes", "version": "1",
        "instructions": "x", "prompt_variants": {"default": "y"},
        "items": [{"id": "why", "type": "free_text", "text": "Why?"}],
    })
    assert validate_response('{"why": "it moves"}', inst).answers == {"why": "it moves"}
    assert _reason('{"why": "  "}', inst) == "empty_text:why"
    assert _reason('{"why": 3}', inst) == "wrong_type:why"
    pair = InstrumentDef.model_validate({
        "schema_version": 1, "name": "pick", "version": "1",
        "instructions": "x", "prompt_variants": {"default": "y"},
        "items": [{"id": "pick", "type": "pairwise", "text": "?", "options": ["L", "R"]}],
    })
    assert validate_response('{"pick": "R"}', pair).answers == {"pick": "R"}
    assert _reason('{"pick": "A"}', pair) == "bad_choice:pick"


def test_invalid_rate() -> None:
    assert invalid_rate({}) is None
    assert invalid_rate({"refused": 3, "failed": 2}) is None
    assert invalid_rate({"valid": 3, "invalid": 1, "refused": 5, "failed": 7}) == 0.25
    assert invalid_rate({"invalid": 2}) == 1.0
    assert invalid_rate({"valid": 9, "sent": 4, "planned": 1}) == 0.0


def test_validate_is_pure() -> None:
    src = Path(__file__).resolve().parents[1] / "src" / "consortium" / "core" / "validate.py"
    text = src.read_text()
    for forbidden in ("open(", "sqlite3", "import yaml", "pathlib", "consortium.config"):
        assert forbidden not in text
