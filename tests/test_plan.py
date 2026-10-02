"""core.plan: Sessions, Trials, pairs, order and Prompt-variant rotation (story 1.6)."""

from __future__ import annotations

import random
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from consortium.config.load import load_instruments, load_study
from consortium.config.models import InstrumentDef, StudyConfig, TestConfig
from consortium.core.personas import fisher_yates
from consortium.core.plan import canonical_trials, pair_id, plan_test
from consortium.core.seeds import derive_seed
from consortium.stages.init import init_study

CLIPS = ["c_dddddddd", "c_aaaaaaaa", "c_cccccccc", "c_bbbbbbbb"]


@pytest.fixture(scope="module")
def study(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return init_study(tmp_path_factory.mktemp("plan") / "study")


@pytest.fixture(scope="module")
def cfg(study: Path) -> StudyConfig:
    return load_study(study)


@pytest.fixture(scope="module")
def instruments(study: Path, cfg: StudyConfig) -> dict[str, InstrumentDef]:
    return load_instruments(study, cfg)


def _personas(n: int) -> list[SimpleNamespace]:
    return [SimpleNamespace(id=f"p{i}") for i in range(1, n + 1)]


def _test(**fields: Any) -> TestConfig:
    doc: dict[str, Any] = {
        "schema_version": 1, "test": "pilot1", "kind": "pilot",
        "instruments": ["godspeed", "pairwise_alive"], "clips": CLIPS,
    }
    doc.update(fields)
    return TestConfig.model_validate(doc)


def test_pilot_counts(cfg: StudyConfig, instruments: dict) -> None:
    plan = plan_test(_test(session={"repeats": 3}), cfg, _personas(64), instruments)
    assert len(plan.sessions) == 192
    assert all(len(s.trials) == 16 for s in plan.sessions)
    assert len(plan) == 3072
    per = Counter(t.instrument for t in plan.sessions[0].trials)
    assert per == {"godspeed": 4, "pairwise_alive": 12}
    assert Counter(t.model_id for t in plan.trials) == {"m1": 3072}


def test_session_and_trial_ids(cfg: StudyConfig, instruments: dict) -> None:
    plan = plan_test(_test(session={"repeats": 2}), cfg, _personas(2), instruments)
    assert [s.session_id for s in plan.sessions] == [
        "pilot1/p1-m1/r1", "pilot1/p1-m1/r2", "pilot1/p2-m1/r1", "pilot1/p2-m1/r2",
    ]
    for session in plan.sessions:
        assert session.order_seed == derive_seed(cfg.seed, "order", session.session_id)
        assert [t.trial_index for t in session.trials] == list(range(1, 17))
        for t in session.trials:
            assert t.trial_id == f"{session.session_id}/t{t.trial_index}"
            assert (t.test, t.session_id, t.repeat) == ("pilot1", session.session_id,
                                                        session.repeat)
            assert (t.agent_id, t.persona_id, t.model_id) == (
                session.agent_id, session.persona_id, "m1"
            )
            assert t.agent_id == f"{t.persona_id}-m1"
            assert t.order_seed == session.order_seed


def test_pairs_over_three_clips(cfg: StudyConfig, instruments: dict) -> None:
    clips = CLIPS[:3]
    plan = plan_test(
        _test(instruments=["pairwise_alive"], clips=clips, session={"repeats": 1}),
        cfg, _personas(1), instruments,
    )
    trials = plan.sessions[0].trials
    assert len(trials) == 6
    by_pair: dict[str, list] = {}
    for t in trials:
        by_pair.setdefault(t.pair_id, []).append(t)
    assert len(by_pair) == 3
    for pid, pair in by_pair.items():
        _, lo, hi = pid.split(":")
        assert lo < hi
        assert sorted(t.position for t in pair) == [1, 2]
        for t in pair:
            assert t.clip_ids == ((lo, hi) if t.position == 1 else (hi, lo))
    assert set(by_pair) == {
        pair_id("pairwise_alive", a, b)
        for i, a in enumerate(clips) for b in clips[i + 1:]
    }


def test_single_clip_trials(cfg: StudyConfig, instruments: dict) -> None:
    plan = plan_test(_test(instruments=["godspeed"], session={"repeats": 1}),
                     cfg, _personas(1), instruments)
    trials = plan.sessions[0].trials
    assert sorted(t.clip_ids for t in trials) == sorted((c,) for c in CLIPS)
    assert all(t.pair_id is None and t.position is None for t in trials)


def test_order_is_seeded_shuffle_of_canonical(cfg: StudyConfig, instruments: dict) -> None:
    test = _test(session={"repeats": 1})
    plan = plan_test(test, cfg, _personas(1), instruments)
    session = plan.sessions[0]
    canonical = canonical_trials(test.instruments, test.clips, instruments)
    # Canonical: Instrument order, then Clip order, then position.
    assert [c[0] for c in canonical] == ["godspeed"] * 4 + ["pairwise_alive"] * 12
    assert [c[1] for c in canonical[:4]] == [(c,) for c in CLIPS]
    expected = list(canonical)
    fisher_yates(expected, random.Random(derive_seed(cfg.seed, "order", session.session_id)))
    got = [(t.instrument, t.clip_ids, t.pair_id, t.position) for t in session.trials]
    assert got == expected


def test_same_seed_same_plan(cfg: StudyConfig, instruments: dict) -> None:
    test = _test(session={"repeats": 3})
    a = plan_test(test, cfg, _personas(4), instruments)
    b = plan_test(test, cfg, _personas(4), instruments)
    assert a == b
    other = plan_test(test, cfg.model_copy(update={"seed": cfg.seed + 1}), _personas(4),
                      instruments)
    assert [t.clip_ids for t in a.trials] != [t.clip_ids for t in other.trials]
    # Sessions are shuffled independently.
    orders = {tuple(t.clip_ids for t in s.trials) for s in a.sessions}
    assert len(orders) > 1


def test_prompt_variant_rotation(cfg: StudyConfig, instruments: dict) -> None:
    rotating = instruments["godspeed"].model_copy(
        update={"prompt_variants": {"default": "Rate it.", "alt": "Please rate it."}}
    )
    plan = plan_test(
        _test(instruments=["godspeed"], session={"repeats": 3}),
        cfg, _personas(1), {"godspeed": rotating},
    )
    variants = {s.repeat: {t.prompt_variant for t in s.trials} for s in plan.sessions}
    assert variants == {1: {"default"}, 2: {"alt"}, 3: {"default"}}


def test_test_models_and_repeats_override(cfg: StudyConfig, instruments: dict) -> None:
    two = cfg.model_copy(update={"models": [cfg.models[0], cfg.models[0].model_copy(
        update={"id": "m2"})]})
    plan = plan_test(_test(instruments=["godspeed"]), two, _personas(2), instruments)
    assert len(plan.sessions) == 2 * 2 * cfg.session.repeats
    assert {s.agent_id for s in plan.sessions} == {"p1-m1", "p1-m2", "p2-m1", "p2-m2"}
    only = plan_test(_test(instruments=["godspeed"], models=["m2"], session={"repeats": 1}),
                     two, _personas(2), instruments)
    assert [s.session_id for s in only.sessions] == ["pilot1/p1-m2/r1", "pilot1/p2-m2/r1"]
