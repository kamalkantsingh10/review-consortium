"""Story 3.1: ``core.fidelity.score_fidelity`` (pure scoring) edge cases."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from consortium.core.fidelity import ScoredItem, fidelity_keys, score_fidelity
from consortium.core.personas import TRAITS

# One item per trait (one reversed), plus two NARS items on a 5-point scale.
KEYS = {
    "o": ScoredItem("openness", False, None, 5),
    "c": ScoredItem("conscientiousness", True, None, 5),
    "e": ScoredItem("extraversion", False, None, 5),
    "a": ScoredItem("agreeableness", False, None, 5),
    "n": ScoredItem("neuroticism", False, None, 5),
    "n1": ScoredItem("nars", False, "s1", 5),
    "n3": ScoredItem("nars", True, "s3", 5),
}


def _persona(pole: str = "high", nars: str | None = "high") -> SimpleNamespace:
    return SimpleNamespace(big_five=dict.fromkeys(TRAITS, pole), nars=nars)


def test_all_match() -> None:
    answers = [{"o": 5, "c": 1, "e": 4, "a": 5, "n": 4, "n1": 5, "n3": 1}]
    score = score_fidelity(answers, _persona(), KEYS)
    assert (score.matched, score.total, score.ratio) == (6, 6, 1.0)
    assert score.per_trait["conscientiousness"]["score"] == 5  # reversed: 5 + 1 - 1
    assert score.per_trait["nars"]["subscales"] == {"s1": 5.0, "s3": 5.0}


def test_reversal_and_low_pole() -> None:
    answers = [{"o": 1, "c": 5, "e": 2, "a": 1, "n": 2, "n1": 1, "n3": 5}]
    score = score_fidelity(answers, _persona("low", "low"), KEYS)
    assert score.ratio == 1.0
    assert score.per_trait["conscientiousness"]["score"] == 1
    flipped = score_fidelity(answers, _persona("high", "high"), KEYS)
    assert flipped.matched == 0


def test_midpoint_does_not_match() -> None:
    answers = [{"o": 3, "c": 3, "e": 3, "a": 3, "n": 3, "n1": 3, "n3": 3}]
    for persona in (_persona("high"), _persona("low", "low")):
        score = score_fidelity(answers, persona, KEYS)
        assert score.matched == 0
        assert all(e["score"] == e["midpoint"] == 3 for e in score.per_trait.values())


def test_mean_over_repeats() -> None:
    # 5 and 2 -> 3.5 > 3 (match); 4 and 2 -> 3.0 (midpoint, no match).
    answers = [{"o": 5, "e": 4}, {"o": 2, "e": 2}]
    score = score_fidelity(answers, _persona(), KEYS)
    assert score.per_trait["openness"] == {
        "score": 3.5, "midpoint": 3.0, "pole": "high", "match": True, "n": 2}
    assert score.per_trait["extraversion"]["match"] is False


def test_partial_and_invalid_answers() -> None:
    answers = [{"o": 5, "c": 9, "e": True, "a": "5", "unknown": 5}]
    score = score_fidelity(answers, _persona(), KEYS)
    assert score.matched == 1
    assert score.per_trait["conscientiousness"] == {
        "score": None, "midpoint": None, "pole": "high", "match": False, "n": 0}
    assert score.per_trait["nars"]["subscales"] == {}
    assert score_fidelity([], _persona(), KEYS).ratio == 0.0


def test_no_nars_band_gives_five_checks() -> None:
    answers = [{"o": 5, "c": 1, "e": 5, "a": 5, "n": 1, "n1": 1}]
    score = score_fidelity(answers, _persona(nars=None), KEYS)
    assert (score.matched, score.total) == (4, 5)
    assert score.ratio == pytest.approx(0.8)
    assert "nars" not in score.per_trait


def test_six_checks_need_five_matches() -> None:
    answers = [{"o": 5, "c": 1, "e": 5, "a": 5, "n": 1, "n1": 5, "n3": 1}]
    score = score_fidelity(answers, _persona(), KEYS)
    assert (score.matched, score.total) == (5, 6) and score.ratio >= 0.8


def test_fidelity_keys_from_instruments() -> None:
    key = SimpleNamespace(construct="openness", reversed=True, subscale=None)
    instrument = SimpleNamespace(
        items=[SimpleNamespace(id="x", points=7), SimpleNamespace(id="free", points=None)],
        keys={"x": key},
    )
    assert fidelity_keys([instrument]) == {"x": ScoredItem("openness", True, None, 7)}
    assert fidelity_keys([SimpleNamespace(items=[], keys=None)]) == {}


def test_insufficient_data_is_not_matched() -> None:
    # Planned 4 answers per check: 1 answer is too few, 2 is enough.
    expected = dict.fromkeys([*TRAITS, "nars"], 4)
    answers = [{"o": 5, "c": 1, "e": 5, "a": 5, "n": 5, "n1": 5, "n3": 1},
               {"o": 5, "c": 1, "e": 5, "n1": 5}]
    score = score_fidelity(answers, _persona(), KEYS, expected)
    assert score.per_trait["openness"]["match"] is True
    assert score.per_trait["openness"]["insufficient_data"] is False
    for construct in ("agreeableness", "neuroticism"):
        entry = score.per_trait[construct]
        assert entry["insufficient_data"] is True and entry["match"] is False
        assert (entry["n"], entry["expected"]) == (1, 4)
    assert (score.matched, score.insufficient, score.insufficient_data) == (4, 2, True)


def test_four_of_five_is_exactly_the_threshold() -> None:
    answers = [{"o": 5, "c": 1, "e": 5, "a": 5, "n": 1}]
    score = score_fidelity(answers, _persona(nars=None), KEYS, dict.fromkeys(TRAITS, 1))
    assert score.ratio == 0.8 and score.ratio >= 0.8
