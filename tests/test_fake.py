"""FakeRater: deterministic, schema-conforming, restart-safe, offline."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from consortium.config.models import InstrumentDef
from consortium.core.render import ClipRef, canonical_json, render
from consortium.raters.base import MediaRef, RaterCall
from consortium.raters.fake import FAKE_BUILD, FREE_TEXT_ANSWER, FakeRater, fake_answer

CLIPS = {"c_aaaaaaaa": "a" * 64, "c_bbbbbbbb": "b" * 64}


def _instrument(items: list[dict]) -> InstrumentDef:
    return InstrumentDef.model_validate({
        "schema_version": 1, "name": "inst", "version": "1", "instructions": "Rate.",
        "prompt_variants": {"default": "Go."}, "items": items,
    })


def _request(instrument: InstrumentDef, clip_ids: tuple[str, ...]):
    trial = SimpleNamespace(prompt_variant="default", clip_ids=clip_ids)
    return render(trial, persona_card="You are calm.\n", instrument=instrument, practice=[],
                  clip_sha256=CLIPS)


LIKERT = _instrument([
    {"id": "q1", "type": "likert", "text": "Dead / Alive", "points": 5,
     "anchors": {"low": "Dead", "high": "Alive"}},
    {"id": "q2", "type": "likert", "text": "x", "points": 7, "anchors": {"low": "a", "high": "b"}},
    {"id": "note", "type": "free_text", "text": "Why?"},
])
PAIRWISE = _instrument([{"id": "alive", "type": "pairwise", "text": "Which?"}])


def test_answers_match_schema_and_are_deterministic() -> None:
    req = _request(LIKERT, ("c_aaaaaaaa",))
    answers = {fake_answer(req, seed) for seed in range(200)}
    assert len(answers) > 10
    for seed in range(200):
        raw = fake_answer(req, seed)
        assert raw == fake_answer(req, seed)
        parsed = json.loads(raw)
        assert canonical_json(parsed).decode() == raw
        assert LIKERT.answer_problem(parsed) is None
        assert parsed["note"] == FREE_TEXT_ANSWER
    values = {json.loads(fake_answer(req, s))["q2"] for s in range(200)}
    assert values == set(range(1, 8))


def test_pairwise_answers_a_position() -> None:
    req = _request(PAIRWISE, ("c_aaaaaaaa", "c_bbbbbbbb"))
    picks = {json.loads(fake_answer(req, s))["alive"] for s in range(100)}
    assert picks == {"A", "B"}
    for s in range(20):
        assert PAIRWISE.answer_problem(json.loads(fake_answer(req, s))) is None


def test_submit_then_collect_after_restart() -> None:
    req = _request(LIKERT, ("c_aaaaaaaa",))

    async def run():
        rater = FakeRater()
        ref = await rater.prepare(ClipRef("c_aaaaaaaa", CLIPS["c_aaaaaaaa"]))
        assert ref == MediaRef("c_aaaaaaaa", CLIPS["c_aaaaaaaa"], "fake:c_aaaaaaaa")
        (handle,) = await rater.submit([RaterCall("t/x/r1/t1", 1, 42, req, (ref,))])
        stored = canonical_json(handle).decode()  # as in attempts.handle
        (result,) = await FakeRater().collect([json.loads(stored)])  # a fresh process
        return result

    result = asyncio.run(run())
    assert result.raw == fake_answer(req, 42)
    assert result.usage == {"input_tokens": 0, "output_tokens": 0}
    assert result.model_build == FAKE_BUILD == "fake-1"
    assert result.category == "ok"


def test_fake_module_has_no_network_imports() -> None:
    import consortium.raters.fake as fake

    text = Path(fake.__file__).read_text()
    for name in ("socket", "http", "urllib", "requests", "httpx"):
        assert f"import {name}" not in text
