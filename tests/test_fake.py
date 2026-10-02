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


# --------------------------------------------------------------------------- story 2.1 outcomes


def test_outcome_rates_draw_from_independent_streams() -> None:
    from consortium.raters.fake import fake_category, is_invalid_attempt

    seeds = range(4000)
    transient = {s for s in seeds if fake_category(s, transient_rate=0.5) == "transient"}
    refused = {s for s in seeds if fake_category(s, refusal_rate=0.5) == "refused"}
    fatal = {s for s in seeds if fake_category(s, fatal_rate=0.5) == "fatal"}
    invalid = {s for s in seeds if is_invalid_attempt(s, 0.5)}
    for hits in (transient, refused, fatal, invalid):
        assert 1800 < len(hits) < 2200
    # Independent: each pair overlaps about a quarter of the seeds, never identically.
    for a, b in ((transient, refused), (transient, fatal), (refused, fatal),
                 (transient, invalid), (refused, invalid), (fatal, invalid)):
        assert a != b
        assert 800 < len(a & b) < 1200
    # Order fatal, refused, transient: a higher-priority hit wins, the others are unchanged.
    for s in seeds:
        got = fake_category(s, transient_rate=0.5, refusal_rate=0.5, fatal_rate=0.5)
        want = ("fatal" if s in fatal else "refused" if s in refused
                else "transient" if s in transient else "ok")
        assert got == want
    assert {fake_category(s) for s in seeds} == {"ok"}
    assert {fake_category(s, fatal_rate=1) for s in seeds} == {"fatal"}


def test_simulated_outcomes_in_handle_and_collect() -> None:
    req = _request(LIKERT, ("c_aaaaaaaa",))

    async def run(rater: FakeRater, attempt: int):
        (handle,) = await rater.submit([RaterCall("t/x/r1/t1", attempt, 42, req, ())])
        stored = json.loads(canonical_json(handle).decode())
        (result,) = await FakeRater(input_tokens=5, output_tokens=7).collect([stored])
        return handle, result

    for kind, kw in (("transient", {"transient_rate": 1.0}), ("refused", {"refusal_rate": 1.0}),
                     ("fatal", {"fatal_rate": 1.0})):
        handle, result = asyncio.run(run(FakeRater(input_tokens=5, output_tokens=7, **kw), 1))
        assert handle["category"] == result.category == kind
        assert result.usage == {"input_tokens": 0, "output_tokens": 0}
        assert result.model_build == FAKE_BUILD
        assert result.raw.startswith("simulated ")
    _, odd = asyncio.run(run(FakeRater(transient_rate=1.0), 1))
    _, even = asyncio.run(run(FakeRater(transient_rate=1.0), 2))
    assert odd.raw.startswith("simulated rate limit")
    assert even.raw.startswith("simulated transport error")
    handle, ok = asyncio.run(run(FakeRater(input_tokens=5, output_tokens=7), 1))
    assert (handle["category"], ok.category, ok.raw) == ("ok", "ok", fake_answer(req, 42))
    assert ok.usage == {"input_tokens": 5, "output_tokens": 7}
    legacy = {k: v for k, v in handle.items() if k != "category"}  # a handle from before 2.1
    (old,) = asyncio.run(FakeRater().collect([legacy]))
    assert old.category == "ok"
