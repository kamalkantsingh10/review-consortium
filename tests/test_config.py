"""Story 1.2: Study config and Instruments: every I/O matrix row plus schema checks."""

from __future__ import annotations

import ast
import logging
import re
from pathlib import Path
from typing import Any

import pytest

from consortium.config.load import (
    load_instruments,
    load_prices,
    load_study,
    load_test,
)
from consortium.config.models import SCHEMA_VERSION, InstrumentDef, StudyConfig
from consortium.config.schema import SCHEMAS, export_schemas
from consortium.core.errors import ConsortiumError
from consortium.stages.init import init_study

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src" / "consortium"


@pytest.fixture
def study(tmp_path: Path) -> Path:
    return init_study(tmp_path / "s")


def _edit(path: Path, old: str, new: str) -> None:
    text = path.read_text()
    assert old in text, f"{old!r} not in {path}"
    path.write_text(text.replace(old, new, 1))


def _err(fn, *args: Any) -> ConsortiumError:
    with pytest.raises(ConsortiumError) as info:
        fn(*args)
    return info.value


# A minimal JSON Schema checker for the subset response_schema() emits.
def _valid(schema: dict[str, Any], value: Any) -> bool:
    kind = schema.get("type")
    if kind == "object":
        if not isinstance(value, dict):
            return False
        props = schema["properties"]
        if any(k not in value for k in schema.get("required", [])):
            return False
        if schema.get("additionalProperties") is False and any(k not in props for k in value):
            return False
        return all(_valid(props[k], v) for k, v in value.items())
    if kind == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            return False
        return schema.get("minimum", value) <= value <= schema.get("maximum", value)
    if kind == "string":
        if not isinstance(value, str):
            return False
        if "enum" in schema and value not in schema["enum"]:
            return False
        return len(value) >= schema.get("minLength", 0)
    raise AssertionError(f"unexpected schema {schema}")


# --------------------------------------------------------------------------- matrix rows


def test_fresh_study_loads(study: Path) -> None:
    cfg = load_study(study)
    assert isinstance(cfg, StudyConfig)
    assert cfg.seed == 1
    m1 = cfg.models[0]
    assert (m1.id, m1.provider, m1.model, m1.max_output_tokens) == ("m1", "fake", "fake-1", 512)
    assert m1.settings.temperature == 0.7
    assert (m1.limits.max_seconds, m1.limits.max_bytes, m1.limits.inline_base64) == (
        600,
        20000000,
        True,
    )
    assert (cfg.media.height, cfg.media.video_kbps, cfg.media.audio_kbps, cfg.media.fps) == (
        480,
        400,
        64,
        25,
    )
    s = cfg.session
    assert (s.practice_clips, s.repeats, s.max_retries, s.pairing) == (2, 3, 2, "all_pairs")
    assert cfg.concurrency == 4
    assert cfg.thresholds.persona_fidelity_min == 0.8
    assert cfg.thresholds.invalid_rate_max == 0.05
    assert cfg.thresholds.leak_tolerance.duration_s == 1.0
    assert cfg.thresholds.leak_tolerance.loudness_lufs == 2.0
    assert (cfg.personas.big_five.fraction, cfg.personas.big_five.replicates) == ("1", 1)
    assert cfg.personas.nars_bands == ["low", "high"]

    instruments = load_instruments(study, cfg)
    assert list(instruments) == ["godspeed", "pairwise_alive", "presence"]

    test = load_test(study / "tests" / "example.yaml")
    assert (test.test, test.kind, test.instruments) == ("example", "pilot", ["godspeed"])
    assert test.model_ids(cfg) == ["m1"]
    assert test.practice == [] and test.clips == []
    assert test.effective_session(cfg) == cfg.session

    prices = load_prices(study)
    assert str(prices.models["m1"].input_usd_per_mtok) == "0"


def test_template_quota_levels_are_marked_placeholder(study: Path) -> None:
    text = (study / "study.yaml").read_text()
    assert text.count("# PLACEHOLDER — Kamal to confirm before the OLAF study") == 4


TRUST = """\
schema_version: 1
name: trust
version: "1"
instructions: Rate how much you trust the robot.
prompt_variants:
  default: Watch the clip and answer.
  formal: Having viewed the clip, please respond to each item.
items:
  - id: q1
    type: likert
    text: I would trust this robot.
    points: 5
    anchors: {low: Strongly disagree, high: Strongly agree}
  - id: why
    type: free_text
    text: Why?
"""


def test_user_instrument(study: Path) -> None:
    (study / "instruments").mkdir()
    (study / "instruments" / "trust.yaml").write_text(TRUST)
    _edit(study / "study.yaml", "instruments: [godspeed,", "instruments: [trust, godspeed,")
    cfg = load_study(study)
    trust = load_instruments(study, cfg)["trust"]
    assert list(trust.prompt_variants) == ["default", "formal"]
    assert trust.draft is False and trust.pairwise is False
    schema = trust.response_schema()
    assert _valid(schema, {"q1": 4, "why": "it moves carefully"})
    assert not _valid(schema, {"q1": 6, "why": "x"})
    assert not _valid(schema, {"q1": 4})
    assert not _valid(schema, {"q1": 4, "why": "x", "extra": 1})


def test_unknown_instrument(study: Path) -> None:
    (study / "tests" / "x.yaml").write_text(
        "schema_version: 1\ntest: x\nkind: pilot\ninstruments: [foo]\n"
    )
    err = _err(load_test, study / "tests" / "x.yaml")
    assert err.code == "unknown_instrument"
    assert err.path == "tests/x.yaml"
    assert err.message.startswith("instruments.0:")


def test_instrument_not_enabled_in_study(study: Path) -> None:
    (study / "tests" / "x.yaml").write_text(
        "schema_version: 1\ntest: x\nkind: pilot\ninstruments: [godspeed]\n"
    )
    _edit(study / "study.yaml", "instruments: [godspeed, ", "instruments: [")
    err = _err(load_test, study / "tests" / "x.yaml")
    assert err.code == "unknown_instrument"
    assert "not enabled" in err.message


def test_unknown_instrument_in_study(study: Path) -> None:
    _edit(study / "study.yaml", "instruments: [godspeed,", "instruments: [nope, godspeed,")
    err = _err(load_study, study)
    assert (err.code, err.path) == ("unknown_instrument", "study.yaml")
    assert err.message.startswith("instruments.0:")


def test_model_without_id(study: Path) -> None:
    _edit(study / "study.yaml", "  - id: m1\n    provider: fake", "  - provider: fake")
    err = _err(load_study, study)
    assert (err.code, err.path) == ("config_invalid", "study.yaml")
    assert err.message.startswith("models.0.id: field required")


@pytest.mark.parametrize(
    "replacement",
    ["model: gemini-latest", "model: gemini-2.5-pro-latest", 'model: ""', "model: '  '"],
)
def test_unpinned_model(study: Path, replacement: str) -> None:
    _edit(study / "study.yaml", "model: fake-1", replacement)
    err = _err(load_study, study)
    assert err.code == "config_invalid"
    assert err.message.startswith("models.0.model:")


def test_missing_model_field(study: Path) -> None:
    text = (study / "study.yaml").read_text()
    line = next(ln for ln in text.splitlines(keepends=True) if "model: fake-1" in ln)
    _edit(study / "study.yaml", line, "")
    err = _err(load_study, study)
    assert err.message.startswith("models.0.model: field required")


def test_missing_thresholds_block(study: Path) -> None:
    text = (study / "study.yaml").read_text()
    start = text.index("thresholds:")
    end = text.index("# Persona frame")
    (study / "study.yaml").write_text(text[:start] + text[end:])
    err = _err(load_study, study)
    assert err.code == "config_invalid"
    assert err.message.startswith("thresholds: field required")


@pytest.mark.parametrize(
    ("old", "field"),
    [
        ("  invalid_rate_max: 0.05\n", "thresholds.invalid_rate_max"),
        ("    loudness_lufs: 2.0\n", "thresholds.leak_tolerance.loudness_lufs"),
    ],
)
def test_missing_threshold_key(study: Path, old: str, field: str) -> None:
    _edit(study / "study.yaml", old, "")
    err = _err(load_study, study)
    assert err.message.startswith(f"{field}: field required")


@pytest.mark.parametrize(
    ("old", "new", "field"),
    [
        ("concurrency: 4", "concurrency: 4\ncolour: blue", "colour"),
        ("temperature: 0.7", "temperature: 0", "models.0.settings.temperature"),
        ("max_output_tokens: 512", "max_output_tokens: 0", "models.0.max_output_tokens"),
        ("provider: fake ", "provider: openai ", "models.0.provider"),
        ("seed: 1", "seed: -1", "seed"),
        ("schema_version: 1", "schema_version: 2", "schema_version"),
        ("[none, some, regular]", "[none, none]", "personas.quotas.robot_experience"),
        ("[none, some, regular]", "[]", "personas.quotas.robot_experience"),
        ("nars_bands: [low, high]", "nars_bands: []", "personas.nars_bands"),
    ],
)
def test_invalid_study_fields(study: Path, old: str, new: str, field: str) -> None:
    _edit(study / "study.yaml", old, new)
    err = _err(load_study, study)
    assert (err.code, err.path) == ("config_invalid", "study.yaml")
    assert err.message.startswith(f"{field}:"), err.message


def test_missing_schema_version(study: Path) -> None:
    _edit(study / "prices.yaml", "schema_version: 1\n", "")
    err = _err(load_prices, study)
    assert (err.code, err.path) == ("config_invalid", "prices.yaml")
    assert err.message.startswith("schema_version: field required")


def test_duplicate_model_id(study: Path) -> None:
    text = (study / "study.yaml").read_text()
    block = text[text.index("  - id: m1") : text.index("# Canonical media")]
    _edit(study / "study.yaml", block, block + block)
    err = _err(load_study, study)
    assert err.code == "config_invalid"
    assert err.message.startswith("models:") and "m1" in err.message


@pytest.mark.parametrize("bad_id", ["model1", "m0", "M1", "m"])
def test_bad_model_id(study: Path, bad_id: str) -> None:
    _edit(study / "study.yaml", "id: m1", f"id: {bad_id}")
    err = _err(load_study, study)
    assert err.message.startswith("models.0.id:")


def test_bad_yaml(study: Path) -> None:
    (study / "study.yaml").write_text("seed: [1, 2\n")
    err = _err(load_study, study)
    assert (err.code, err.path) == ("config_invalid", "study.yaml")
    assert "line" in err.message


def test_duplicate_yaml_key(study: Path) -> None:
    _edit(study / "study.yaml", "seed: 1", "seed: 1\nseed: 2")
    err = _err(load_study, study)
    assert err.code == "config_invalid"
    assert "duplicate key 'seed'" in err.message


@pytest.mark.parametrize("text", ["", "- a\n- b\n"])
def test_empty_or_non_mapping(study: Path, text: str) -> None:
    (study / "study.yaml").write_text(text)
    assert _err(load_study, study).code == "config_invalid"


def test_missing_study_file(tmp_path: Path) -> None:
    err = _err(load_study, tmp_path)
    assert (err.code, err.path, err.message) == ("config_invalid", "study.yaml", "file not found")


# --------------------------------------------------------------------------- Test files


def _write_test(study: Path, body: str) -> Path:
    path = study / "tests" / "t.yaml"
    path.write_text("schema_version: 1\ntest: t\nkind: pilot\n" + body)
    return path


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ("instruments: []\n", "instruments"),
        ("instruments: [godspeed]\nkind2: x\n", "kind2"),
        ("instruments: [godspeed]\nsession: {repeats: 0}\n", "session.repeats"),
        ("instruments: [godspeed]\nsession: {colour: 1}\n", "session.colour"),
        ("instruments: [godspeed]\nmodels: []\n", "models"),
        ("instruments: [godspeed]\nclips: [c_aaaaaaaa, c_aaaaaaaa]\n", "clips"),
        ("instruments: [godspeed]\nclips: [clip1]\n", "clips.0"),
        ("instruments: [godspeed]\nclips: [c_AAAAAAAA]\n", "clips.0"),
        ("instruments: [godspeed]\nsession: {pairing: random}\n", "session.pairing"),
        (
            "instruments: [godspeed]\npractice: [{instrument: godspeed, clips: [], answer: {}}]\n",
            "practice.0.clips",
        ),
    ],
)
def test_invalid_test_fields(study: Path, body: str, field: str) -> None:
    err = _err(load_test, _write_test(study, body))
    assert (err.code, err.path) == ("config_invalid", "tests/t.yaml")
    assert err.message.startswith(f"{field}:"), err.message


def test_test_unknown_model_id(study: Path) -> None:
    err = _err(load_test, _write_test(study, "instruments: [godspeed]\nmodels: [m2]\n"))
    assert err.code == "config_invalid"
    assert err.message.startswith("models.0:")


def test_test_bad_kind(study: Path) -> None:
    path = study / "tests" / "t.yaml"
    path.write_text("schema_version: 1\ntest: t\nkind: final\ninstruments: [godspeed]\n")
    assert _err(load_test, path).message.startswith("kind:")


def test_test_full_fields_and_overrides(study: Path) -> None:
    path = _write_test(
        study,
        "instruments: [godspeed, pairwise_alive]\n"
        "models: [m1]\n"
        "clips: [c_aaaaaaaa, c_bbbbbbbb]\n"
        "practice:\n"
        f"  - {{instrument: godspeed, clips: [c_pppppppp], answer: {GODSPEED_ANSWER}}}\n"
        "  - {instrument: pairwise_alive, clips: [c_pppppppp, c_qqqqqqqq], answer: {alive: A}}\n"
        "session: {repeats: 1, pairing: all_pairs}\n",
    )
    test = load_test(path)
    cfg = load_study(study)
    eff = test.effective_session(cfg)
    assert (eff.repeats, eff.practice_clips, eff.max_retries) == (1, 2, 2)
    assert test.practice[1].answer == {"alive": "A"}
    assert cfg.session.repeats == 3  # study config untouched


def test_draft_instrument_warning(study: Path, caplog: pytest.LogCaptureFixture) -> None:
    path = _write_test(study, "instruments: [godspeed, presence]\n")
    with caplog.at_level(logging.WARNING):
        load_test(path)
    messages = [r.getMessage() for r in caplog.records]
    assert messages == ["draft_instrument: presence"]


def test_no_warning_without_draft(study: Path, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        load_test(study / "tests" / "example.yaml")
    assert not caplog.records


# --------------------------------------------------------------------------- Instruments


def test_user_instrument_shadowing_builtin(study: Path) -> None:
    cfg = load_study(study)
    (study / "instruments").mkdir()
    (study / "instruments" / "godspeed.yaml").write_text(TRUST.replace("trust", "godspeed"))
    for err in (_err(load_study, study), _err(load_instruments, study, cfg)):
        assert (err.code, err.path) == ("config_invalid", "instruments/godspeed.yaml")
        assert err.message.startswith("name:")


@pytest.mark.parametrize(
    ("old", "new", "field"),
    [
        ("  default: Watch the clip and answer.\n", "", "prompt_variants"),
        ("    points: 5\n", "", "items.0"),
        ("    type: free_text\n", "    type: free_text\n    points: 3\n", "items.1"),
        ("id: why", "id: q1", "items"),
        ("version: \"1\"\n", "", "version"),
        ("    type: likert\n", "    type: slider\n", "items.0.type"),
    ],
)
def test_invalid_user_instrument(study: Path, old: str, new: str, field: str) -> None:
    (study / "instruments").mkdir()
    text = TRUST
    assert old in text
    (study / "instruments" / "trust.yaml").write_text(text.replace(old, new, 1))
    _edit(study / "study.yaml", "instruments: [godspeed,", "instruments: [trust, godspeed,")
    cfg = load_study(study)
    err = _err(load_instruments, study, cfg)
    assert (err.code, err.path) == ("config_invalid", "instruments/trust.yaml")
    assert err.message.startswith(f"{field}:"), err.message


def test_user_instrument_name_must_match_file(study: Path) -> None:
    (study / "instruments").mkdir()
    (study / "instruments" / "trust.yaml").write_text(TRUST.replace("name: trust", "name: other"))
    _edit(study / "study.yaml", "instruments: [godspeed,", "instruments: [trust, godspeed,")
    err = _err(load_instruments, study, load_study(study))
    assert err.message.startswith("name:")


def test_mixed_pairwise_instrument_rejected(study: Path) -> None:
    (study / "instruments").mkdir()
    mixed = TRUST + "  - {id: pick, type: pairwise, text: Pick one}\n"
    (study / "instruments" / "trust.yaml").write_text(mixed)
    _edit(study / "study.yaml", "instruments: [godspeed,", "instruments: [trust, godspeed,")
    err = _err(load_instruments, study, load_study(study))
    assert err.message.startswith("items:")


BUILTIN_CASES = {
    "godspeed": (
        {f"animacy_{i}": 3 for i in range(1, 7)} | {f"likeability_{i}": 5 for i in range(1, 6)},
        "animacy_1",
        6,
    ),
    "pairwise_alive": ({"alive": "B"}, "alive", "C"),
    "presence": ({"presence_1": 7}, "presence_1", 8),
}


@pytest.mark.parametrize("name", sorted(BUILTIN_CASES))
def test_builtin_response_schema(study: Path, name: str) -> None:
    instrument = load_instruments(study, load_study(study))[name]
    valid, item, bad_value = BUILTIN_CASES[name]
    schema = instrument.response_schema()
    assert _valid(schema, valid)
    assert not _valid(schema, valid | {item: bad_value})
    if instrument.items[0].type == "likert":
        assert not _valid(schema, valid | {item: 0})


def test_builtin_contents_and_draft_flags(study: Path) -> None:
    instruments = load_instruments(study, load_study(study))
    godspeed = instruments["godspeed"]
    assert godspeed.draft is False
    ids = [i.id for i in godspeed.items]
    assert sum(i.startswith("animacy_") for i in ids) == 6
    assert sum(i.startswith("likeability_") for i in ids) == 5
    assert all(i.type == "likert" and i.points == 5 for i in godspeed.items)

    alive = instruments["pairwise_alive"]
    assert alive.draft is False and alive.pairwise
    assert [(i.text, i.options) for i in alive.items] == [
        ("Which one feels more alive?", ["A", "B"])
    ]

    presence = instruments["presence"]
    assert presence.draft is True
    assert [(i.type, i.points) for i in presence.items] == [("likert", 7)]
    first = (SRC / "instruments" / "presence.yaml").read_text().splitlines()[0]
    assert first == "# DRAFT — replace with a published presence scale before any main Test"


def test_instrument_def_is_versioned() -> None:
    assert "schema_version" in InstrumentDef.model_fields


# --------------------------------------------------------------------------- schemas


def test_committed_schemas_match_regenerated(tmp_path: Path) -> None:
    written = export_schemas(tmp_path)
    assert {p.name for p in written} == {f"{n}.schema.json" for n in SCHEMAS}
    for path in written:
        committed = REPO / "docs" / "schema" / path.name
        assert committed.read_text() == path.read_text(), f"{path.name} is stale"
        assert f'"x-schema-version": {SCHEMA_VERSION}' in path.read_text()


# --------------------------------------------------------------------------- YAML boundary


_YAML_CALL = re.compile(r"(?<![\w.])yaml\.")


def test_yaml_only_read_in_config() -> None:
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        if path.is_relative_to(SRC / "config"):
            continue
        text = path.read_text()
        if _YAML_CALL.search(text):
            offenders.append(str(path))
        for node in ast.walk(ast.parse(text)):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            if any(n == "yaml" or n.startswith("yaml.") for n in names):
                offenders.append(str(path))
    assert offenders == []


# --------------------------------------------------------------------------- review fixes


def test_unhashable_yaml_key(study: Path) -> None:
    (study / "study.yaml").write_text("? [a, b]\n: 1\n")
    err = _err(load_study, study)
    assert (err.code, err.path) == ("config_invalid", "study.yaml")


def test_merge_key_may_be_overridden(study: Path) -> None:
    # Overriding a key pulled in by a merge is allowed (explicit duplicates stay refused).
    _edit(
        study / "study.yaml",
        "media:\n  height: 480\n",
        "media:\n  <<: {height: 360, fps: 25}\n  height: 480\n",
    )
    assert load_study(study).media.height == 480


@pytest.mark.parametrize(
    ("old", "new", "field"),
    [
        ("temperature: 0.7", "temperature: .inf", "models.0.settings.temperature"),
        ("max_seconds: 600", "max_seconds: .nan", "models.0.limits.max_seconds"),
        ("duration_s: 1.0", "duration_s: .inf", "thresholds.leak_tolerance.duration_s"),
        ("seed: 1", f"seed: {2**63}", "seed"),
        ("pairing: all_pairs", "pairing: random", "session.pairing"),
    ],
)
def test_more_invalid_study_values(study: Path, old: str, new: str, field: str) -> None:
    _edit(study / "study.yaml", old, new)
    assert _err(load_study, study).message.startswith(f"{field}:")


def test_several_errors_joined(study: Path) -> None:
    _edit(study / "study.yaml", "seed: 1", "seed: -1")
    _edit(study / "study.yaml", "concurrency: 4", "concurrency: 0")
    msg = _err(load_study, study).message
    assert msg.startswith("seed:")
    assert "; concurrency:" in msg


def test_test_name_must_match_file(study: Path) -> None:
    path = study / "tests" / "other.yaml"
    path.write_text("schema_version: 1\ntest: example\nkind: pilot\ninstruments: [godspeed]\n")
    err = _err(load_test, path)
    assert (err.code, err.path) == ("config_invalid", "tests/other.yaml")
    assert err.message.startswith("test:")


GODSPEED_ANSWER = (
    "{" + ", ".join(f"animacy_{i}: 3" for i in range(1, 7))
    + ", " + ", ".join(f"likeability_{i}: 3" for i in range(1, 6)) + "}"
)


@pytest.mark.parametrize(
    ("practice", "code", "field"),
    [
        ("{instrument: presence, clips: [c_pppppppp], answer: {presence_1: 4}}",
         "unknown_instrument", "practice.0.instrument"),
        ("{instrument: pairwise_alive, clips: [c_pppppppp], answer: {alive: A}}",
         "config_invalid", "practice.0.clips"),
        (f"{{instrument: godspeed, clips: [c_pppppppp, c_qqqqqqqq], answer: {GODSPEED_ANSWER}}}",
         "config_invalid", "practice.0.clips"),
        ("{instrument: pairwise_alive, clips: [c_pppppppp, c_qqqqqqqq], answer: {alive: C}}",
         "config_invalid", "practice.0.answer.alive"),
        ("{instrument: godspeed, clips: [c_pppppppp], answer: {animacy_1: 3}}",
         "config_invalid", "practice.0.answer.animacy_2"),
        (f"{{instrument: godspeed, clips: [c_pppppppp], answer: {GODSPEED_ANSWER[:-1]}, x: 1}}}}",
         "config_invalid", "practice.0.answer.x"),
        ("{instrument: godspeed, clips: [bad], answer: {}}",
         "config_invalid", "practice.0.clips.0"),
    ],
)
def test_bad_practice(study: Path, practice: str, code: str, field: str) -> None:
    path = _write_test(
        study, f"instruments: [godspeed, pairwise_alive]\npractice:\n  - {practice}\n"
    )
    err = _err(load_test, path)
    assert (err.code, err.path) == (code, "tests/t.yaml")
    assert err.message.startswith(f"{field}:"), err.message


def test_valid_practice(study: Path) -> None:
    path = _write_test(
        study,
        "instruments: [godspeed, pairwise_alive]\npractice:\n"
        f"  - {{instrument: godspeed, clips: [c_pppppppp], answer: {GODSPEED_ANSWER}}}\n"
        "  - {instrument: pairwise_alive, clips: [c_pppppppp, c_qqqqqqqq], answer: {alive: B}}\n",
    )
    assert len(load_test(path).practice) == 2


def test_main_test_with_draft_only_warns(study: Path, caplog: pytest.LogCaptureFixture) -> None:
    path = study / "tests" / "t.yaml"
    path.write_text("schema_version: 1\ntest: t\nkind: main\ninstruments: [presence]\n")
    with caplog.at_level(logging.WARNING):
        assert load_test(path).kind == "main"
    assert [r.getMessage() for r in caplog.records] == ["draft_instrument: presence"]


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ('  m1: {input_usd_per_mtok: 0.30, output_usd_per_mtok: "0"}\n',
         "models.m1.input_usd_per_mtok"),
        ('  m1: {input_usd_per_mtok: "-1", output_usd_per_mtok: "0"}\n',
         "models.m1.input_usd_per_mtok"),
        ('  model1: {input_usd_per_mtok: "0", output_usd_per_mtok: "0"}\n', "models.model1"),
        ('  m1: {input_usd_per_mtok: "0", output_usd_per_mtok: "0"}\n'
         '  m2: {input_usd_per_mtok: "0", output_usd_per_mtok: "0"}\n', "models.m2"),
        ("  {}\n", "models.m1"),
    ],
)
def test_invalid_prices(study: Path, body: str, field: str) -> None:
    head = "schema_version: 1\nmodels:\n"
    if body == "  {}\n":
        (study / "prices.yaml").write_text("schema_version: 1\nmodels: {}\n")
    else:
        (study / "prices.yaml").write_text(head + body)
    err = _err(load_prices, study)
    assert (err.code, err.path) == ("config_invalid", "prices.yaml")
    assert err.message.startswith(f"{field}:"), err.message


@pytest.mark.parametrize(
    ("item", "reason"),
    [
        ("{id: a, type: likert, text: T, points: 5}", "anchors"),
        ("{id: a, type: pairwise, text: T, options: [A, A]}", "options"),
        ("{id: a, type: pairwise, text: T, options: [A, B, C]}", "options"),
        ("{id: a, type: likert, text: T, points: 5, anchors: {low: L, high: H}, options: [A, B]}",
         "options"),
    ],
)
def test_item_rules(study: Path, item: str, reason: str) -> None:
    (study / "instruments").mkdir()
    (study / "instruments" / "trust.yaml").write_text(
        'schema_version: 1\nname: trust\nversion: "1"\ninstructions: I\n'
        f"prompt_variants: {{default: D}}\nitems:\n  - {item}\n"
    )
    _edit(study / "study.yaml", "instruments: [godspeed,", "instruments: [trust, godspeed,")
    err = _err(load_instruments, study, load_study(study))
    assert (err.code, err.path) == ("config_invalid", "instruments/trust.yaml")
    assert err.message.startswith("items.0:") and reason in err.message, err.message


def test_prices_schema_forbids_other_keys() -> None:
    import json

    schema = json.loads((REPO / "docs" / "schema" / "prices.schema.json").read_text())
    assert schema["properties"]["models"]["additionalProperties"] is False


# --------------------------------------------------------------------------- story 2.1


def test_retry_policy_and_fake_rates_defaults(study: Path) -> None:
    cfg = load_study(study)
    retry = cfg.session.retry
    assert (retry.transient_retries, retry.backoff_initial_s, retry.backoff_max_s) == (3, 2, 60)
    fake = cfg.models[0].fake_settings
    assert (fake.transient_rate, fake.refusal_rate, fake.fatal_rate) == (0, 0, 0)


@pytest.mark.parametrize(
    ("old", "new", "field"),
    [
        ("    transient_retries: 3 ", "    transient_retries: -1 ",
         "session.retry.transient_retries"),
        ("    transient_retries: 3 ", "    transient_retries: 1.5 ",
         "session.retry.transient_retries"),
        ("    backoff_initial_s: 2 ", "    backoff_initial_s: -0.5 ",
         "session.retry.backoff_initial_s"),
        ("    backoff_max_s: 60 ", "    backoff_max_s: -1 ", "session.retry.backoff_max_s"),
        ("    backoff_max_s: 60 ", '    backoff_max_s: "60" ', "session.retry.backoff_max_s"),
        ("    backoff_max_s: 60 ", "    backoff_max_s: 60\n    jitter: 1 ",
         "session.retry.jitter"),
        ("      transient_rate: 0 ", "      transient_rate: 1.5 ",
         "models.0.fake.transient_rate"),
        ("      refusal_rate: 0 ", "      refusal_rate: -0.1 ", "models.0.fake.refusal_rate"),
        ("      fatal_rate: 0 ", '      fatal_rate: "0.5" ', "models.0.fake.fatal_rate"),
    ],
)
def test_retry_policy_and_fake_rate_bounds(study: Path, old: str, new: str, field: str) -> None:
    _edit(study / "study.yaml", old, new)
    err = _err(load_study, study)
    assert err.code == "config_invalid"
    assert err.message.startswith(f"{field}:")


def test_retry_backoff_max_below_initial_message(study: Path) -> None:
    _edit(study / "study.yaml", "    backoff_max_s: 60 ", "    backoff_max_s: 1 ")
    err = _err(load_study, study)
    assert err.code == "config_invalid"
    assert err.message == "session.retry: backoff_max_s must be at least backoff_initial_s"


@pytest.mark.parametrize("field", ["backoff_initial_s", "backoff_max_s"])
@pytest.mark.parametrize("value", [".inf", ".nan"])
def test_retry_backoff_must_be_finite(study: Path, field: str, value: str) -> None:
    default = {"backoff_initial_s": 2, "backoff_max_s": 60}[field]
    _edit(study / "study.yaml", f"    {field}: {default} ", f"    {field}: {value} ")
    err = _err(load_study, study)
    assert err.code == "config_invalid"
    assert err.message.startswith(f"session.retry.{field}:")


@pytest.mark.parametrize("value", [float("inf"), float("nan")])
def test_retry_policy_model_forbids_inf_nan(value: float) -> None:
    from pydantic import ValidationError

    from consortium.config.models import RetryPolicy

    with pytest.raises(ValidationError):
        RetryPolicy(backoff_initial_s=0, backoff_max_s=value)
    with pytest.raises(ValidationError):
        RetryPolicy(backoff_initial_s=value, backoff_max_s=value)


def test_retry_policy_accepts_equal_and_zero_backoff(study: Path) -> None:
    _edit(study / "study.yaml", "    backoff_initial_s: 2 ", "    backoff_initial_s: 0 ")
    _edit(study / "study.yaml", "    backoff_max_s: 60 ", "    backoff_max_s: 0 ")
    assert load_study(study).session.retry.backoff_max_s == 0


def test_retry_policy_has_no_per_test_override() -> None:
    from consortium.config.models import SessionOverrides

    assert "retry" not in SessionOverrides.model_fields


# --------------------------------------------------------------------------- story 2.2 settings


@pytest.mark.parametrize(
    "setting", ["fps: 1", "media_resolution: low", "thinking_level: low", "api_key_env: MY_KEY",
                "seed_supported: true"]
)
def test_gemini_settings_on_fake_are_config_invalid(study: Path, setting: str) -> None:
    _edit(study / "study.yaml", "      temperature: 0.7      # > 0\n",
          f"      temperature: 0.7      # > 0\n      {setting}\n")
    with pytest.raises(ConsortiumError) as info:
        load_study(study)
    assert info.value.code == "config_invalid"
    assert setting.split(":")[0] in info.value.message


def test_gemini_settings_and_key_env_name(study: Path) -> None:
    text = (study / "study.yaml").read_text()
    text = text.replace("provider: fake ", "provider: gemini ", 1)
    text = text.replace("      temperature: 0.7      # > 0\n",
                        "      temperature: 0.7      # > 0\n      fps: 2\n"
                        "      media_resolution: high\n      thinking_level: minimal\n", 1)
    text = text[: text.index("    fake:")] + text[text.index("# Canonical media"):]
    (study / "study.yaml").write_text(text)
    model = load_study(study).models[0]
    assert (model.settings.fps, model.settings.media_resolution,
            model.settings.thinking_level, model.settings.seed_supported) == (
        2.0, "high", "minimal", True)
    assert model.api_key_env_name == "GEMINI_API_KEY"
    assert model.model_copy(update={"provider": "qwen"}).api_key_env_name == "DASHSCOPE_API_KEY"
    assert model.model_copy(update={"provider": "fake"}).api_key_env_name is None


@pytest.mark.parametrize("setting", ["fps: 0", "fps: .inf", "fps: .nan",
                                     "media_resolution: ultra", "thinking_level: max",
                                     "api_key_env: 'bad name'", "seed_supported: 'yes'"])
def test_gemini_setting_values_checked(study: Path, setting: str) -> None:
    text = (study / "study.yaml").read_text().replace("provider: fake ", "provider: gemini ", 1)
    text = text[: text.index("    fake:")] + text[text.index("# Canonical media"):]
    text = text.replace("      temperature: 0.7      # > 0\n",
                        f"      temperature: 0.7      # > 0\n      {setting}\n", 1)
    (study / "study.yaml").write_text(text)
    with pytest.raises(ConsortiumError) as info:
        load_study(study)
    assert info.value.code == "config_invalid"


# --------------------------------------------------------------------------- story 2.3 settings

QWEN_URL = "https://ws-1.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1"


def _as_provider(study: Path, provider: str, *settings: str) -> None:
    text = (study / "study.yaml").read_text()
    text = text.replace("provider: fake ", f"provider: {provider} ", 1)
    if provider == "qwen":  # within hosted qwen's 10 MB base64 limit
        text = text.replace("      max_bytes: 20000000\n", "      max_bytes: 9000000\n", 1)
    text = text[: text.index("    fake:")] + text[text.index("# Canonical media"):]
    extra = "".join(f"      {s}\n" for s in settings)
    text = text.replace("      temperature: 0.7      # > 0\n",
                        f"      temperature: 0.7      # > 0\n{extra}", 1)
    (study / "study.yaml").write_text(text)


def test_qwen_settings_load(study: Path) -> None:
    original = (study / "study.yaml").read_text()
    _as_provider(study, "qwen", f"base_url: {QWEN_URL}", "reasoning_effort: low")
    model = load_study(study).models[0]
    assert (model.settings.base_url, model.settings.reasoning_effort) == (QWEN_URL, "low")
    assert model.api_key_env_name == "DASHSCOPE_API_KEY"
    (study / "study.yaml").write_text(original)
    _as_provider(study, "qwen", "base_url: http://gpu:8000/v1")
    model = load_study(study).models[0]
    assert (model.settings.base_url, model.settings.reasoning_effort) == (
        "http://gpu:8000/v1", None)


def test_qwen_needs_base_url(study: Path) -> None:
    _as_provider(study, "qwen")
    with pytest.raises(ConsortiumError) as info:
        load_study(study)
    assert info.value.code == "config_invalid"
    assert "base_url: required for provider: qwen" in info.value.message


@pytest.mark.parametrize("provider", ["gemini", "fake"])
@pytest.mark.parametrize("setting", [f"base_url: {QWEN_URL}", "reasoning_effort: low"])
def test_qwen_settings_on_other_providers_are_config_invalid(
    study: Path, provider: str, setting: str
) -> None:
    if provider == "fake":
        _edit(study / "study.yaml", "      temperature: 0.7      # > 0\n",
              f"      temperature: 0.7      # > 0\n      {setting}\n")
    else:
        _as_provider(study, provider, setting)
    with pytest.raises(ConsortiumError) as info:
        load_study(study)
    assert info.value.code == "config_invalid"
    assert setting.split(":")[0] in info.value.message


@pytest.mark.parametrize("setting", ["base_url: ftp://x/v1", "base_url: gpu:8000/v1",
                                     "base_url: 'https://'", "base_url: 'http://a b/v1'",
                                     "base_url: 3", "reasoning_effort: ''",
                                     "reasoning_effort: '  '", "reasoning_effort: 1",
                                     "fps: 1", "media_resolution: low", "thinking_level: low"])
def test_qwen_setting_values_checked(study: Path, setting: str) -> None:
    extra = [] if setting.startswith("base_url") else [f"base_url: {QWEN_URL}"]
    _as_provider(study, "qwen", *extra, setting)
    with pytest.raises(ConsortiumError) as info:
        load_study(study)
    assert info.value.code == "config_invalid"


@pytest.mark.parametrize("limits, ok", [
    ("max_bytes: 9900000", True),
    ("max_bytes: 9900001", False),
    ("max_bytes: 20000000", False),
])
def test_hosted_qwen_max_bytes(study: Path, limits: str, ok: bool) -> None:
    _as_provider(study, "qwen", f"base_url: {QWEN_URL}")
    _edit(study / "study.yaml", "      max_bytes: 9000000\n", f"      {limits}\n")
    if ok:
        assert load_study(study).models[0].limits.max_bytes == 9_900_000
        return
    with pytest.raises(ConsortiumError) as info:
        load_study(study)
    assert info.value.code == "config_invalid" and "10 MB" in info.value.message


def test_hosted_qwen_needs_inline_base64(study: Path) -> None:
    _as_provider(study, "qwen", f"base_url: {QWEN_URL}")
    text = (study / "study.yaml").read_text()
    (study / "study.yaml").write_text(text.replace("inline_base64: true", "inline_base64: false",
                                                   1))
    with pytest.raises(ConsortiumError) as info:
        load_study(study)
    assert info.value.code == "config_invalid" and "10 MB" in info.value.message


def test_self_hosted_qwen_has_no_size_rule(study: Path) -> None:
    _as_provider(study, "qwen", "base_url: http://gpu:8000/v1")
    _edit(study / "study.yaml", "      max_bytes: 9000000\n", "      max_bytes: 20000000\n")
    assert load_study(study).models[0].limits.max_bytes == 20_000_000


@pytest.mark.parametrize("url", ["http://gpu:99999/v1", "http://gpu:port/v1", "https://:8000/v1",
                                 "https://./v1"])
def test_qwen_base_url_needs_valid_host_and_port(study: Path, url: str) -> None:
    _as_provider(study, "qwen", f"base_url: '{url}'")
    with pytest.raises(ConsortiumError) as info:
        load_study(study)
    assert info.value.code == "config_invalid" and "base_url" in info.value.message


# --------------------------------------------------------------------------- story 2.4: big_five

_BIG_FIVE = "  big_five:\n    fraction: 1\n    replicates: 1\n"


@pytest.mark.parametrize(
    ("value", "fraction", "replicates"),
    [
        ("{fraction: 1, replicates: 1}", "1", 1),
        ("all_32", "1", 1),
        ("{}", "1", 1),
        ("{fraction: 0.5}", "1/2", 1),
        ("{fraction: 1/2, replicates: 2}", "1/2", 2),
        ('{fraction: "1/2"}', "1/2", 1),
        ("{fraction: 0.25, replicates: 3}", "1/4", 3),
        ("{fraction: 1/4}", "1/4", 1),
        ('{fraction: "1"}', "1", 1),
        ("{fraction: 1.0}", "1", 1),
        ("{fraction: 0.50}", "1/2", 1),
    ],
)
def test_big_five_design_forms(study: Path, value: str, fraction: str, replicates: int) -> None:
    _edit(study / "study.yaml", _BIG_FIVE, f"  big_five: {value}\n")
    design = load_study(study).personas.big_five
    assert (design.fraction, design.replicates) == (fraction, replicates)
    assert design.model_dump(mode="json") == {"fraction": fraction, "replicates": replicates}


def test_big_five_default_when_omitted(study: Path) -> None:
    _edit(study / "study.yaml", _BIG_FIVE, "")
    design = load_study(study).personas.big_five
    assert (design.fraction, design.replicates) == ("1", 1)


@pytest.mark.parametrize(
    ("value", "field"),
    [
        ("{fraction: 0.3}", "personas.big_five.fraction"),
        ("{fraction: 0}", "personas.big_five.fraction"),
        ("{fraction: 2}", "personas.big_five.fraction"),
        ('{fraction: "0.5"}', "personas.big_five.fraction"),
        ("{fraction: 1/3}", "personas.big_five.fraction"),
        ("{fraction: true}", "personas.big_five.fraction"),
        ('{fraction: "1/2 "}', "personas.big_five.fraction"),
        ('{fraction: "½"}', "personas.big_five.fraction"),
        ("{replicates: 4}", "personas.big_five.replicates"),
        ("{replicates: 0}", "personas.big_five.replicates"),
        ('{replicates: "2"}', "personas.big_five.replicates"),
        ("{replicates: 1.0}", "personas.big_five.replicates"),
        ("{fraction: 1, extra: 1}", "personas.big_five.extra"),
        ("all_64", "personas.big_five"),
        ("1", "personas.big_five"),
    ],
)
def test_big_five_design_invalid(study: Path, value: str, field: str) -> None:
    _edit(study / "study.yaml", _BIG_FIVE, f"  big_five: {value}\n")
    err = _err(load_study, study)
    assert (err.code, err.path) == ("config_invalid", "study.yaml")
    assert err.message.startswith(f"{field}"), err.message


def test_big_five_schema_accepts_mapping_and_legacy() -> None:
    schema = StudyConfig.model_json_schema()
    big_five = schema["$defs"]["PersonaFrame"]["properties"]["big_five"]
    assert big_five["anyOf"][1] == {"const": "all_32", "type": "string"}
    assert big_five["default"] == {"fraction": "1", "replicates": 1}
    design = schema["$defs"]["BigFiveDesign"]["properties"]
    assert design["fraction"]["enum"] == [1, 0.5, 0.25, "1", "1/2", "1/4"]
    assert (design["replicates"]["minimum"], design["replicates"]["maximum"]) == (1, 3)
