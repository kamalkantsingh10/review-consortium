"""core.render: TrialRequest contents, canonical JSON and isolation (story 1.6)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from consortium.config.load import load_instruments, load_study
from consortium.config.models import PracticeExample, TestConfig
from consortium.core.errors import ConsortiumError
from consortium.core.plan import plan_test
from consortium.core.render import canonical_json, practice_for, render
from consortium.stages.init import init_study

TARGETS = ["c_aaaaaaaa", "c_bbbbbbbb", "c_cccccccc"]
PRACTICE = ["c_pppppppa", "c_pppppppb", "c_pppppppc", "c_pppppppd", "c_pppppppe"]
SHA = {c: f"{i:064x}" for i, c in enumerate(TARGETS + PRACTICE)}
GODSPEED = {f"animacy_{i}": 3 for i in range(1, 7)} | {f"likeability_{i}": 4 for i in range(1, 6)}
CARD = "You are a person.\nYou like robots — ü.\n"


@pytest.fixture(scope="module")
def setup(tmp_path_factory: pytest.TempPathFactory):
    study = init_study(tmp_path_factory.mktemp("render") / "study")
    cfg = load_study(study)
    instruments = load_instruments(study, cfg)
    test = TestConfig.model_validate({
        "schema_version": 1, "test": "pilot1", "kind": "pilot",
        "instruments": ["godspeed", "pairwise_alive"], "clips": TARGETS,
        "practice": [
            {"instrument": "godspeed", "clips": [PRACTICE[0]], "answer": GODSPEED},
            {"instrument": "pairwise_alive", "clips": PRACTICE[1:3], "answer": {"alive": "A"}},
            {"instrument": "godspeed", "clips": [PRACTICE[3]], "answer": GODSPEED},
            {"instrument": "godspeed", "clips": [PRACTICE[4]], "answer": GODSPEED},
            {"instrument": "pairwise_alive", "clips": PRACTICE[3:5], "answer": {"alive": "B"}},
        ],
        "session": {"repeats": 1},
    })
    plan = plan_test(test, cfg, [SimpleNamespace(id="p1")], instruments)
    return test, instruments, plan


def _render(trial, test, instruments):
    return render(
        trial,
        persona_card=CARD,
        instrument=instruments[trial.instrument],
        practice=practice_for(test.practice, trial.instrument, 2),
        clip_sha256=SHA,
    )


def test_practice_for_takes_first_n_of_instrument(setup) -> None:
    test, _, _ = setup
    picked = practice_for(test.practice, "godspeed", 2)
    assert [ex.clips for ex in picked] == [[PRACTICE[0]], [PRACTICE[3]]]
    assert practice_for(test.practice, "pairwise_alive", 0) == []


def test_single_request_contents(setup) -> None:
    test, instruments, plan = setup
    trial = next(t for t in plan.trials if t.instrument == "godspeed")
    req = _render(trial, test, instruments)
    godspeed = instruments["godspeed"]
    assert req.persona_card == CARD
    assert req.instructions == godspeed.instructions
    assert req.prompt == godspeed.prompt_variants["default"]
    assert [i.id for i in req.items] == [i.id for i in godspeed.items]
    assert req.items[0].anchors.low == "Dead" and req.items[0].points == 5
    assert req.response_schema == godspeed.response_schema()
    assert [(c.clip_id, c.sha256) for c in req.clips] == [(trial.clip_ids[0],
                                                           SHA[trial.clip_ids[0]])]
    assert [[c.clip_id for c in ex.clips] for ex in req.practice] == [[PRACTICE[0]],
                                                                       [PRACTICE[3]]]
    assert req.practice[0].clips[0].sha256 == SHA[PRACTICE[0]]
    assert req.practice[0].answer == GODSPEED


def test_pairwise_request_keeps_presentation_order(setup) -> None:
    test, instruments, plan = setup
    for trial in (t for t in plan.trials if t.pairwise):
        req = _render(trial, test, instruments)
        assert tuple(c.clip_id for c in req.clips) == trial.clip_ids
        assert req.items[0].options == ("A", "B")
        assert [ex.answer for ex in req.practice] == [{"alive": "A"}, {"alive": "B"}]


def test_canonical_json_format(setup) -> None:
    test, instruments, plan = setup
    trial = next(iter(plan.trials))
    raw = canonical_json(_render(trial, test, instruments))
    assert raw == canonical_json(_render(trial, test, instruments))
    obj = json.loads(raw.decode("utf-8"))
    assert raw == json.dumps(obj, sort_keys=True, ensure_ascii=False,
                             separators=(",", ":")).encode("utf-8")
    assert "ü".encode() in raw and b"\\u00fc" not in raw
    assert canonical_json({"b": 1, "a": [1, "é"]}) == '{"a":[1,"é"],"b":1}'.encode()
    assert set(obj) == {"persona_card", "instructions", "items", "response_schema", "prompt",
                        "practice", "clips"}


def test_request_carries_no_ids_or_other_clips(setup) -> None:
    test, instruments, plan = setup
    for trial in plan.trials:
        text = canonical_json(_render(trial, test, instruments)).decode("utf-8")
        for forbidden in (trial.trial_id, trial.session_id, "pilot1", trial.agent_id,
                          trial.model_id, trial.persona_id + "-", "/r1", "/t"):
            assert forbidden not in text
        for clip in TARGETS:
            assert (clip in text) == (clip in trial.clip_ids)
        assert trial.instrument not in text  # the pair_id is not sent either


def test_missing_clip_hash_is_unknown_clip(setup) -> None:
    test, instruments, plan = setup
    trial = next(iter(plan.trials))
    with pytest.raises(ConsortiumError) as info:
        render(trial, persona_card=CARD, instrument=instruments[trial.instrument],
               practice=[], clip_sha256={})
    assert info.value.code == "unknown_clip"


def test_practice_example_model_is_accepted(setup) -> None:
    _, instruments, plan = setup
    trial = next(t for t in plan.trials if t.instrument == "godspeed")
    ex = PracticeExample(instrument="godspeed", clips=[PRACTICE[0]], answer=GODSPEED)
    req = render(trial, persona_card=CARD, instrument=instruments["godspeed"],
                 practice=[ex], clip_sha256=SHA)
    assert len(req.practice) == 1


def test_second_repeat_renders_second_variant(setup, tmp_path) -> None:
    test, instruments, _ = setup
    rotating = instruments["godspeed"].model_copy(
        update={"prompt_variants": {"default": "Rate it.", "alt": "Please rate it now."}}
    )
    two = test.model_copy(update={"instruments": ["godspeed"],
                                  "session": test.session.model_copy(update={"repeats": 2})})
    cfg = load_study(init_study(tmp_path / "s"))
    plan = plan_test(two, cfg, [SimpleNamespace(id="p1")], {"godspeed": rotating})
    prompts = {
        t.repeat: _render(t, two, {"godspeed": rotating}).prompt for t in plan.trials
    }
    assert prompts == {1: "Rate it.", 2: "Please rate it now."}


def test_undefined_variant_is_refused(setup) -> None:
    test, instruments, plan = setup
    trial = next(t for t in plan.trials if t.instrument == "godspeed")
    narrowed = instruments["godspeed"].model_copy(update={"prompt_variants": {"other": "x"}})
    with pytest.raises(ConsortiumError) as info:
        render(trial, persona_card=CARD, instrument=narrowed, practice=[], clip_sha256=SHA)
    assert info.value.code == "unknown_prompt_variant"
