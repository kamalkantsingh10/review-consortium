"""Story 3.2: perception screening, pure parts (shapes, scoring, coverage, Fake latent)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from consortium.config.load import load_instruments, load_test
from consortium.config.models import PerceptionCheck, TestConfig, Thresholds
from consortium.core.errors import ConsortiumError
from consortium.core.perception import (
    NEUTRAL_CARD,
    NEUTRAL_PERSONA_ID,
    NeutralPersona,
    PerceptionTrial,
    check_shapes,
    coverage,
    covered_instruments,
    pair_checks_by_instrument,
    passes,
    score_perception,
)
from consortium.core.plan import plan_test
from consortium.core.render import ClipRef, RequestItem, TrialRequest
from consortium.raters.fake import fake_answer, fake_latent, likert_from_latent
from consortium.stages.init import init_study

A, B, C, D = "c_aaaaaaaa", "c_bbbbbbbb", "c_cccccccc", "c_dddddddd"


@pytest.fixture(scope="module")
def instruments(tmp_path_factory: pytest.TempPathFactory) -> dict:
    study = init_study(tmp_path_factory.mktemp("p") / "study")
    from consortium.config.load import load_study

    return load_instruments(study, load_study(study))


def _check(instrument: str, item: str, clips: list[str], expected) -> PerceptionCheck:
    return PerceptionCheck(instrument=instrument, item=item, clips=clips, expected=expected)


def _t(model: str, instrument: str, clips: tuple, answers, repeat: int = 1,
       state: str = "valid") -> PerceptionTrial:
    return PerceptionTrial(model, repeat, instrument, clips, state,
                           answers if state == "valid" else None)


# --------------------------------------------------------------------------- shapes


def test_check_shapes_dedup_and_order(instruments: dict) -> None:
    checks = [
        _check("pairwise_alive", "alive", [B, A], A),
        _check("godspeed", "animacy_1", [C, A], C),
        _check("pairwise_alive", "alive", [A, B], B),  # same pair: no new Trials
        _check("godspeed", "animacy_2", [A], 3),  # same Clip: no new Trial
        _check("perception_cues", "moving", [D], 2),
    ]
    assert check_shapes(checks, instruments) == [
        ("pairwise_alive", (A, B), f"pairwise_alive:{A}:{B}", 1),
        ("pairwise_alive", (B, A), f"pairwise_alive:{A}:{B}", 2),
        ("godspeed", (C,), None, None),
        ("godspeed", (A,), None, None),
        ("perception_cues", (D,), None, None),
    ]
    assert pair_checks_by_instrument(checks, instruments) == {
        "pairwise_alive": 2, "godspeed": 1, "perception_cues": 0}


def test_plan_with_shapes_uses_neutral_persona(instruments: dict) -> None:
    view = SimpleNamespace(
        test="s1", kind="screening", instruments=["pairwise_alive"], clips=[A, B, C, D],
        model_ids=lambda cfg: ["m1", "m2"],
        effective_session=lambda cfg: SimpleNamespace(repeats=2),
    )
    shapes = check_shapes([_check("pairwise_alive", "alive", [A, B], A)], instruments)
    plan = plan_test(view, SimpleNamespace(seed=1), [NeutralPersona()], instruments,
                     shapes=shapes)
    assert [s.agent_id for s in plan.sessions] == ["p0-m1", "p0-m1", "p0-m2", "p0-m2"]
    assert all(len(s.trials) == 2 for s in plan.sessions)  # not 12 (all 6 pairs x 2)
    assert {t.persona_id for t in plan.trials} == {NEUTRAL_PERSONA_ID}
    assert {t.clip_ids for t in plan.trials} == {(A, B), (B, A)}
    assert NEUTRAL_CARD == ("You are an adult taking part in a study about robots. "
                            "Answer every question carefully and honestly.")


# --------------------------------------------------------------------------- scoring


def test_pairwise_units_per_position_and_repeat(instruments: dict) -> None:
    checks = [_check("pairwise_alive", "alive", [A, B], A)]
    trials = [
        _t("m1", "pairwise_alive", (A, B), {"alive": "A"}),  # A shown first, chosen: pass
        _t("m1", "pairwise_alive", (B, A), {"alive": "A"}),  # B shown first, chosen: fail
        _t("m1", "pairwise_alive", (A, B), {"alive": "A"}, repeat=2),
        _t("m1", "pairwise_alive", (B, A), {"alive": "B"}, repeat=2),
    ]
    assert score_perception(trials, checks, instruments) == {
        ("m1", "pairwise_alive"): (3, 4, 0.75)}


def test_invalid_position_trial_fails_its_unit(instruments: dict) -> None:
    checks = [_check("pairwise_alive", "alive", [A, B], B)]
    trials = [
        _t("m1", "pairwise_alive", (A, B), None, state="invalid"),
        _t("m1", "pairwise_alive", (B, A), {"alive": "A"}),
    ]
    assert score_perception(trials, checks, instruments) == {
        ("m1", "pairwise_alive"): (1, 2, 0.5)}


def test_two_clip_single_check_is_strict(instruments: dict) -> None:
    checks = [_check("godspeed", "animacy_1", [A, B], A)]

    def score(a, b, state="valid") -> tuple:
        trials = [_t("m1", "godspeed", (A,), {"animacy_1": a}, state=state),
                  _t("m1", "godspeed", (B,), {"animacy_1": b})]
        return score_perception(trials, checks, instruments)[("m1", "godspeed")]

    assert score(4, 2) == (1, 1, 1.0)
    assert score(3, 3) == (0, 1, 0.0)  # a tie fails
    assert score(2, 4) == (0, 1, 0.0)
    assert score(4, 2, state="refused") == (0, 1, 0.0)


def test_low_level_equality(instruments: dict) -> None:
    checks = [_check("perception_cues", "moving", [A], 2),
              _check("perception_cues", "speech", [A], 1)]
    trials = [_t("m1", "perception_cues", (A,), {"moving": 2, "speech": 2}),
              _t("m2", "perception_cues", (A,), {"moving": 2, "speech": 1})]
    assert score_perception(trials, checks, instruments) == {
        ("m1", "perception_cues"): (1, 2, 0.5),
        ("m2", "perception_cues"): (2, 2, 1.0),
    }


def test_missing_trial_fails(instruments: dict) -> None:
    checks = [_check("perception_cues", "moving", [A], 2),
              _check("perception_cues", "moving", [B], 2)]
    trials = [_t("m1", "perception_cues", (A,), {"moving": 2, "speech": 1})]
    assert score_perception(trials, checks, instruments) == {
        ("m1", "perception_cues"): (1, 2, 0.5)}


# --------------------------------------------------------------------------- coverage


def test_coverage() -> None:
    needed = {"presence": ["pilot1", "main1", "pilot1"], "godspeed": ["pilot1"],
              "pairwise_alive": ["main1"]}
    assert coverage(needed, {"godspeed"}) == {
        "pairwise_alive": ["main1"], "presence": ["main1", "pilot1"]}
    assert coverage(needed, ["godspeed", "presence", "pairwise_alive"]) == {}
    assert coverage({}, set()) == {}


# --------------------------------------------------------------------------- Fake rater


def _request(items: list[RequestItem], shas: list[str]) -> TrialRequest:
    return TrialRequest(
        persona_card=NEUTRAL_CARD, instructions="x", items=tuple(items), response_schema={},
        prompt="p", practice=(), clips=tuple(ClipRef(f"c{i}", s) for i, s in enumerate(shas)),
    )


LIKERT = RequestItem("animacy_1", "likert", "t", 5, None, None)
PAIR = RequestItem("alive", "pairwise", "t", None, None, ("A", "B"))


def test_fake_latent_is_deterministic_and_in_range() -> None:
    values = [fake_latent(f"{i:064x}", "alive") for i in range(50)]
    assert values == [fake_latent(f"{i:064x}", "alive") for i in range(50)]
    assert all(0 <= v < 1 for v in values)
    assert len(set(values)) == 50
    assert fake_latent("1" * 64, "alive") != fake_latent("1" * 64, "moving")


def test_fake_faithful_and_unfaithful_answers() -> None:
    sha_a, sha_b = "1" * 64, "2" * 64
    la, lb = fake_latent(sha_a, "alive"), fake_latent(sha_b, "alive")
    higher_first = "A" if la > lb else "B"
    pair = _request([PAIR], [sha_a, sha_b])
    assert json.loads(fake_answer(pair, 7, perception="faithful")) == {"alive": higher_first}
    lower_first = "B" if higher_first == "A" else "A"
    assert json.loads(fake_answer(pair, 7, perception="unfaithful")) == {"alive": lower_first}

    single = _request([LIKERT], [sha_a])
    latent = fake_latent(sha_a, "animacy_1")
    assert json.loads(fake_answer(single, 7, perception="faithful")) == {
        "animacy_1": likert_from_latent(latent, 5)}
    assert json.loads(fake_answer(single, 7, perception="unfaithful")) == {
        "animacy_1": likert_from_latent(1 - latent, 5)}
    # random (the default) is unchanged and ignores the latent.
    assert fake_answer(single, 7) == fake_answer(single, 7, perception="random")
    assert likert_from_latent(0.0, 5) == 1 and likert_from_latent(0.999, 5) == 5
    assert likert_from_latent(1.0, 5) == 5  # 1 - latent may reach 1: capped


def test_fake_perception_ignores_clipless_requests() -> None:
    clipless = _request([LIKERT], [])
    assert fake_answer(clipless, 3, perception="faithful") == fake_answer(clipless, 3)


# --------------------------------------------------------------------------- config


def test_thresholds_default_perception_min() -> None:
    t = Thresholds.model_validate({"persona_fidelity_min": 0.8, "invalid_rate_max": 0.05,
                                   "leak_tolerance": {"duration_s": 1.0,
                                                      "loudness_lufs": 2.0}})
    assert t.perception_min == 0.8


def _test_doc(**over) -> dict:
    doc = {"schema_version": 1, "test": "scr", "kind": "screening",
           "instruments": ["pairwise_alive", "godspeed", "perception_cues", "note"],
           "clips": [A, B], "session": {"practice_clips": 0}, "checks": []}
    doc.update(over)
    return doc


@pytest.fixture()
def cues_study(tmp_path: Path) -> Path:
    study = init_study(tmp_path / "study")
    note = {"schema_version": 1, "name": "note", "version": "1", "instructions": "x",
            "prompt_variants": {"default": "p"},
            "items": [{"id": "words", "type": "free_text", "text": "Describe it."}]}
    (study / "instruments").mkdir(exist_ok=True)
    (study / "instruments" / "note.yaml").write_text(yaml.safe_dump(note))
    doc = yaml.safe_load((study / "study.yaml").read_text())
    doc["instruments"].append("note")
    (study / "study.yaml").write_text(yaml.safe_dump(doc))
    return study


@pytest.mark.parametrize(("check", "fragment"), [
    ({"instrument": "presence", "item": "presence_1", "clips": [A], "expected": 3},
     "checks.0.instrument: 'presence' is not one of the Test's instruments"),
    ({"instrument": "godspeed", "item": "alive", "clips": [A], "expected": 3},
     "checks.0.item: 'alive' is not an item of godspeed"),
    ({"instrument": "godspeed", "item": "animacy_1", "clips": [C], "expected": 3},
     "checks.0.clips.0: c_cccccccc is not one of the Test's clips"),
    ({"instrument": "pairwise_alive", "item": "alive", "clips": [A], "expected": A},
     "checks.0.clips: pairwise Instrument pairwise_alive needs 2 clips"),
    ({"instrument": "pairwise_alive", "item": "alive", "clips": [A, B], "expected": "A"},
     "checks.0.expected: must be one of the check's clips"),
    ({"instrument": "godspeed", "item": "animacy_1", "clips": [A, B], "expected": C},
     "checks.0.expected: must be one of the check's clips"),
    ({"instrument": "perception_cues", "item": "moving", "clips": [A], "expected": 3},
     "checks.0.expected: must be from 1 to 2"),
    ({"instrument": "perception_cues", "item": "moving", "clips": [A], "expected": "yes"},
     "checks.0.expected: must be an integer"),
    ({"instrument": "perception_cues", "item": "moving", "clips": [A, A], "expected": A},
     "checks.0.clips"),
    ({"instrument": "perception_cues", "item": "moving", "clips": [A, B], "expected": A},
     "checks.0.item: a 2-clip check needs a likert item of at least 3 points"),
    ({"instrument": "note", "item": "words", "clips": [A], "expected": "x"},
     "checks.0.item: words is a free_text item"),
    ({"instrument": "note", "item": "words", "clips": [A, B], "expected": A},
     "checks.0.item: a 2-clip check of a single-clip Instrument needs a likert item"),
])
def test_bad_checks_are_config_invalid(cues_study: Path, check: dict, fragment: str) -> None:
    path = cues_study / "tests" / "scr.yaml"
    path.parent.mkdir(exist_ok=True)
    path.write_text(yaml.safe_dump(_test_doc(checks=[check])))
    with pytest.raises(ConsortiumError) as info:
        load_test(path, cues_study)
    assert info.value.code == "config_invalid"
    assert fragment in info.value.message


@pytest.mark.parametrize(("second", "fragment"), [
    ({"instrument": "pairwise_alive", "item": "alive", "clips": [B, A], "expected": B},
     "checks.1: duplicates checks.0 (same instrument, item and clips)"),
    ({"instrument": "pairwise_alive", "item": "alive", "clips": [A, B], "expected": A},
     "checks.1: contradicts checks.0 (same instrument, item and clips)"),
])
def test_duplicate_and_contradictory_checks(cues_study: Path, second: dict,
                                            fragment: str) -> None:
    path = cues_study / "tests" / "scr.yaml"
    path.parent.mkdir(exist_ok=True)
    first = {"instrument": "pairwise_alive", "item": "alive", "clips": [A, B], "expected": B}
    path.write_text(yaml.safe_dump(_test_doc(checks=[first, second])))
    with pytest.raises(ConsortiumError) as info:
        load_test(path, cues_study)
    assert info.value.code == "config_invalid" and fragment in info.value.message


def test_zero_units_fail() -> None:
    assert passes(0, 0.0, 0.0) is False
    assert passes(4, 0.75, 0.7) is True and passes(4, 0.75, 0.8) is False


def test_covered_instruments_needs_current_stamps() -> None:
    row = {"instrument": "godspeed", "model_id": "m1", "pair_checks": 1,
           "instrument_hash": "i", "settings_hash": "s"}
    assert covered_instruments([row], {"godspeed": "i"}, {"m1": "s"}) == {"godspeed"}
    assert covered_instruments([row], {"godspeed": "x"}, {"m1": "s"}) == set()  # stale
    assert covered_instruments([row], {"godspeed": "i"}, {"m1": "x"}) == set()
    assert covered_instruments([row | {"pair_checks": 0}], {"godspeed": "i"},
                               {"m1": "s"}) == set()
    assert covered_instruments([row], {}, {"m1": "s"}) == set()


def test_good_checks_load(cues_study: Path) -> None:
    path = cues_study / "tests" / "scr.yaml"
    path.parent.mkdir(exist_ok=True)
    checks = [
        {"instrument": "pairwise_alive", "item": "alive", "clips": [A, B], "expected": B},
        {"instrument": "godspeed", "item": "animacy_1", "clips": [A, B], "expected": A},
        {"instrument": "perception_cues", "item": "speech", "clips": [B], "expected": 2},
    ]
    path.write_text(yaml.safe_dump(_test_doc(checks=checks)))
    test = load_test(path, cues_study)
    assert [c.expected for c in test.checks] == [B, A, 2]


def test_checks_only_in_screening_tests() -> None:
    with pytest.raises(ValueError, match="checks: only allowed in a kind: screening Test"):
        TestConfig.model_validate(_test_doc(kind="pilot", checks=[
            {"instrument": "perception_cues", "item": "moving", "clips": [A], "expected": 2}]))


def test_fake_perception_only_for_perception_screening() -> None:
    """A Panel Persona's request (a pilot or main Run) is answered as with ``random``."""
    import dataclasses

    single = dataclasses.replace(_request([LIKERT, PAIR], ["1" * 64]), persona_card="card")
    pair = dataclasses.replace(_request([PAIR], ["1" * 64, "2" * 64]), persona_card="card")
    for request in (single, pair):
        for mode in ("faithful", "unfaithful"):
            assert fake_answer(request, 5, perception=mode) == fake_answer(request, 5)
