"""Story 2.1: provider failure handling (transient, refused, fatal) and the retry budgets."""

from __future__ import annotations

import asyncio
import json
import shutil
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from consortium.archive.jsonl import REQUESTS_FILE, RESPONSES_FILE
from consortium.board import trials as board_trials
from consortium.board.clips import insert_clip
from consortium.board.db import connect, transaction
from consortium.board.writer import start_writer
from consortium.config.models import RetryPolicy
from consortium.core.errors import ConsortiumError
from consortium.core.seeds import derive_seed
from consortium.engine.dispatch import Budget, backoff_delay, dispatch
from consortium.raters.base import RaterCall
from consortium.raters.fake import FakeRater, fake_category, fake_invalid_answer, is_invalid_attempt
from consortium.stages import personas as personas_stage
from consortium.stages.init import init_study
from consortium.stages.open import open_test, plan_and_render
from consortium.stages.push import push_test

TARGETS = ["c_aaaaaaaa", "c_bbbbbbbb"]
PRACTICE = ["c_ppppppaa", "c_ppppppab", "c_ppppppac"]
GODSPEED = {f"animacy_{i}": 3 for i in range(1, 7)} | {f"likeability_{i}": 3 for i in range(1, 6)}
STUDY_SEED = 1
RATES = {"invalid_rate": 0.5, "transient_rate": 0.3, "refusal_rate": 0.1, "fatal_rate": 0.05}
NO_WAIT = RetryPolicy(transient_retries=3, backoff_initial_s=0, backoff_max_s=60)


class Killed(Exception):
    """Stands in for the process dying: no writer op runs after it."""


# --------------------------------------------------------------------------- setup


def _add_clips(study: Path, ids: list[str]) -> None:
    conn = connect(study)
    try:
        with transaction(conn):
            for i, clip_id in enumerate(ids):
                info = SimpleNamespace(duration_s=2.0, size_bytes=1000, width=640, height=480,
                                       fps=25.0, loudness_lufs=-23.0)
                insert_clip(conn, clip_id, f"{i + 1:064x}", info)
    finally:
        conn.close()


def _write_test(study: Path, name: str) -> Path:
    doc = {
        "schema_version": 1, "test": name, "kind": "pilot",
        "instruments": ["godspeed", "pairwise_alive"], "clips": TARGETS,
        "practice": [
            {"instrument": "godspeed", "clips": [PRACTICE[0]], "answer": GODSPEED},
            {"instrument": "pairwise_alive", "clips": PRACTICE[1:3], "answer": {"alive": "A"}},
        ],
        "session": {"repeats": 1, "practice_clips": 1, "max_retries": 2},
    }
    src = study.parent / f"{name}.yaml"
    src.write_text(yaml.safe_dump(doc, sort_keys=False))
    return src


def _make_study(
    root: Path, rates: dict[str, float] | None = None, concurrency: int = 4,
    retry: dict[str, Any] | None = None,
) -> Path:
    study = init_study(root / "study")
    cfg = yaml.safe_load((study / "study.yaml").read_text())
    assert cfg["seed"] == STUDY_SEED
    cfg["models"][0]["fake"].update(rates or {})
    cfg["session"]["retry"]["backoff_initial_s"] = 0  # no real waiting in tests
    cfg["session"]["retry"].update(retry or {})
    cfg["concurrency"] = concurrency
    (study / "study.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False))
    personas_stage.generate(study)
    _add_clips(study, TARGETS + PRACTICE)
    push_test(study, _write_test(study, "pilot1"))
    return study


@pytest.fixture(scope="module")
def template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _make_study(tmp_path_factory.mktemp("failures"))


@pytest.fixture()
def study(tmp_path: Path, template: Path) -> Path:
    copy = tmp_path / "study"
    shutil.copytree(template, copy)
    return copy


@pytest.fixture(scope="module")
def ran(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A completed Run of pilot1 with every Fake rate set; read-only for tests."""
    study = _make_study(tmp_path_factory.mktemp("ran"), RATES)
    open_test(study, "pilot1", dry_run=False, yes=True)
    return study


def _trials(study: Path) -> dict[str, dict]:
    conn = connect(study)
    try:
        return {r["trial_id"]: r for r in board_trials.load_trials(conn, "pilot1")}
    finally:
        conn.close()


def _attempts(study: Path) -> dict[tuple[str, int], dict]:
    conn = connect(study)
    try:
        cols = ("trial_id", "attempt", "seed", "handle", "sent_at", "answered_at", "category",
                "valid")
        rows = conn.execute(f"SELECT {', '.join(cols)} FROM attempts").fetchall()
    finally:
        conn.close()
    return {(r[0], r[1]): dict(zip(cols, r, strict=True)) for r in rows}


def _lines(study: Path, rel: str) -> list[dict]:
    path = study / rel
    return [json.loads(x) for x in path.read_bytes().splitlines()] if path.exists() else []


def _seed(trial: dict, attempt: int) -> int:
    key = f"{trial['session_id']}:{trial['trial_index']}:{attempt}"
    return derive_seed(STUDY_SEED, "model", key)


# --------------------------------------------------------------------------- scripted engine runs


class ScriptedRater(FakeRater):
    """A FakeRater whose outcome per attempt follows ``script`` (``invalid``: an invalid
    answer; anything else: that category, ``ok`` a valid answer)."""

    def __init__(
        self, script: list[str] | dict[str, list[str]], slow: frozenset[str] = frozenset()
    ) -> None:
        super().__init__()
        self.script = script  # one script for every Trial, or one per trial_id
        self.slow = slow  # trial_ids whose collect takes 0.2 s
        self.collected: list[tuple[str, int]] = []

    def _step(self, call: RaterCall) -> str:
        script = self.script if isinstance(self.script, list) else self.script[call.trial_id]
        return script[min(call.attempt, len(script)) - 1]

    def category(self, call: RaterCall):  # type: ignore[override]
        step = self._step(call)
        return "ok" if step in ("ok", "invalid") else step

    def answer(self, call: RaterCall) -> str:
        if self._step(call) == "invalid":
            return fake_invalid_answer(call.request, call.seed, call.attempt)
        return super().answer(call)

    async def collect(self, handles):
        self.collected.extend((h["trial_id"], h["attempt"]) for h in handles)
        if any(h["trial_id"] in self.slow for h in handles):
            await asyncio.sleep(0.2)
        return await super().collect(handles)


class _PricedBudget(Budget):
    def reservation(self, trial, request) -> Decimal:
        return Decimal(1)

    def actual(self, model_id, usage) -> Decimal | None:
        return Decimal(1)


def _engine(
    study: Path,
    rater: FakeRater,
    *,
    n: int = 1,
    max_retries: int = 2,
    retry: RetryPolicy = NO_WAIT,
    budget: Budget | None = None,
    insert: bool = True,
    spy=None,
    collect: dict | None = None,
    only: list[str] | None = None,
) -> tuple[str | None, list[str], list[tuple[str, tuple]]]:
    """Dispatch the first ``n`` Trials (or ``only``) of pilot1 through ``rater``."""
    plan, requests, _ = plan_and_render(study, "pilot1")
    pairs = list(zip(plan.trials, requests, strict=True))
    pairs = [p for p in pairs if p[0].trial_id in only] if only else pairs[:n]
    ops: list[tuple[str, tuple]] = []

    def record(op: str, key: tuple) -> None:
        ops.append((op, key))
        if spy is not None:
            spy(op, key)

    async def run() -> str | None:
        async with start_writer(study, spy=record) as writer:
            if insert:
                await writer.do("insert_plan",
                                lambda c: board_trials.insert_plan(c, "pilot1", plan.trials))
            return await dispatch(
                study, pairs, {"m1": rater}, writer=writer, seed=STUDY_SEED, concurrency=2,
                budget=budget or Budget.zero("pilot1"), max_retries=max_retries, retry=retry,
                collect=collect,
            )

    paused = asyncio.run(run())
    return paused, [p[0].trial_id for p in pairs], ops


def _only(study: Path, tid: str) -> dict:
    return _trials(study)[tid]


def test_recovers(study: Path) -> None:
    _, (tid,), _ = _engine(study, ScriptedRater(["transient", "ok"]))
    t = _only(study, tid)
    assert (t["state"], t["attempt"]) == ("valid", 2)
    attempts = _attempts(study)
    assert attempts[(tid, 1)]["category"] == "transient"
    assert attempts[(tid, 1)]["valid"] is None
    assert attempts[(tid, 1)]["answered_at"] is not None
    assert (attempts[(tid, 2)]["category"], attempts[(tid, 2)]["valid"]) == ("ok", 1)
    requests = [r for r in _lines(study, REQUESTS_FILE) if r["trial_id"] == tid]
    responses = [r for r in _lines(study, RESPONSES_FILE) if r["trial_id"] == tid]
    assert [r["attempt"] for r in requests] == [r["attempt"] for r in responses] == [1, 2]
    assert len({r["request_sha256"] for r in requests}) == 1  # same request text
    assert requests[0]["seed"] != requests[1]["seed"]  # new seed
    assert responses[0]["category"] == "transient"
    assert responses[0]["raw"].startswith("simulated rate limit")
    assert responses[0]["usage"] == {"input_tokens": 0, "output_tokens": 0}


def test_transient_exhausted(study: Path) -> None:
    _, (tid,), _ = _engine(study, ScriptedRater(["transient"]))
    t = _only(study, tid)
    assert (t["state"], t["attempt"]) == ("failed", 4)  # 1 + transient_retries 3
    attempts = _attempts(study)
    assert (tid, 5) not in attempts
    assert [attempts[(tid, a)]["category"] for a in range(1, 5)] == ["transient"] * 4
    raws = [r["raw"] for r in _lines(study, RESPONSES_FILE) if r["trial_id"] == tid]
    assert [r.split(":")[0] for r in raws] == [
        "simulated rate limit", "simulated transport error"] * 2


def test_transient_retries_zero(study: Path) -> None:
    retry = RetryPolicy(transient_retries=0, backoff_initial_s=0)
    _, (tid,), _ = _engine(study, ScriptedRater(["transient", "ok"]), retry=retry)
    t = _only(study, tid)
    assert (t["state"], t["attempt"]) == ("failed", 1)
    assert _attempts(study)[(tid, 1)]["category"] == "transient"


def test_refused(study: Path) -> None:
    _, tids, ops = _engine(study, ScriptedRater(["refused", "ok"]), n=4)
    assert "record_validation" not in [op for op, _ in ops]
    attempts = _attempts(study)
    for tid in tids:
        assert (_only(study, tid)["state"], _only(study, tid)["attempt"]) == ("refused", 1)
        assert (attempts[(tid, 1)]["category"], attempts[(tid, 1)]["valid"]) == ("refused", None)
        assert (tid, 2) not in attempts


def test_fatal(study: Path) -> None:
    _, (tid,), ops = _engine(study, ScriptedRater(["fatal", "ok"]))
    assert "record_validation" not in [op for op, _ in ops]
    t = _only(study, tid)
    assert (t["state"], t["attempt"]) == ("failed", 1)
    assert _attempts(study)[(tid, 1)]["category"] == "fatal"


def test_separate_budgets(study: Path) -> None:
    rater = ScriptedRater(["invalid", "transient", "invalid", "ok"])
    _, (tid,), _ = _engine(study, rater, max_retries=1)
    t = _only(study, tid)
    assert (t["state"], t["attempt"]) == ("invalid", 3)
    attempts = _attempts(study)
    assert [(attempts[(tid, a)]["category"], attempts[(tid, a)]["valid"]) for a in (1, 2, 3)] \
        == [("ok", 0), ("transient", None), ("ok", 0)]
    conn = connect(study)
    try:
        assert board_trials.attempt_counts(conn, tid) == (2, 1)
    finally:
        conn.close()


def test_transient_does_not_use_invalid_budget(study: Path) -> None:
    """max_retries 0: three transient results then an answer is still valid."""
    rater = ScriptedRater(["transient", "transient", "transient", "ok"])
    _, (tid,), _ = _engine(study, rater, max_retries=0)
    assert (_only(study, tid)["state"], _only(study, tid)["attempt"]) == ("valid", 4)


def test_ceiling_on_transient_retry(study: Path) -> None:
    budget = _PricedBudget(test="pilot1", models={}, prices=None, clip_seconds={},
                           ceiling=Decimal(1))
    paused, (tid,), _ = _engine(study, ScriptedRater(["transient", "ok"]), budget=budget)
    assert paused == "ceiling"
    t = _only(study, tid)
    assert (t["state"], t["attempt"]) == ("sent", 1)
    assert _attempts(study)[(tid, 1)]["category"] == "transient"


def test_bad_category_stops_run(study: Path) -> None:
    with pytest.raises(ConsortiumError) as info:
        _engine(study, ScriptedRater(["oops"]))
    assert info.value.code == "adapter_error"
    assert "'oops'" in info.value.message


def test_killed_in_backoff_resumes_with_new_attempt(study: Path) -> None:
    plan, _, _ = plan_and_render(study, "pilot1")
    tid = next(iter(plan.trials)).trial_id

    def killer(op: str, key: tuple) -> None:
        if killer.dead:
            raise Killed(op)
        if op == "record_transient" and key == (tid, 1):
            killer.dead = True

    killer.dead = False
    with pytest.raises(ConsortiumError) as info:
        _engine(study, ScriptedRater(["transient", "ok"]), spy=killer)
    assert info.value.code == "run_failed"
    attempts = _attempts(study)
    assert attempts[(tid, 1)]["category"] == "transient"
    assert attempts[(tid, 1)]["handle"] is not None
    assert _only(study, tid)["state"] == "sent"
    conn = connect(study)
    try:
        caps = board_trials.RetryCaps(2, 3)
        (row,) = [r for r in board_trials.load_resumable(conn, "pilot1", caps)
                  if r["trial_id"] == tid]
    finally:
        conn.close()
    assert (row["handle"], row["settle"]) == (None, None)  # not collected again

    rater = ScriptedRater(["transient", "ok"])
    _engine(study, rater, insert=False, only=[tid], collect={})
    assert (tid, 1) not in rater.collected and rater.collected == [(tid, 2)]
    t = _only(study, tid)
    assert (t["state"], t["attempt"]) == ("valid", 2)
    responses = [r["attempt"] for r in _lines(study, RESPONSES_FILE) if r["trial_id"] == tid]
    assert responses == [1, 2]


def test_killed_before_exhausted_state_settles_failed(study: Path) -> None:
    """The last allowed transient was recorded but its failed state never written."""
    plan, _, _ = plan_and_render(study, "pilot1")
    tid = next(iter(plan.trials)).trial_id
    retry = RetryPolicy(transient_retries=1, backoff_initial_s=0)

    def killer(op: str, key: tuple) -> None:
        if killer.dead:
            raise Killed(op)
        if op == "record_transient" and key == (tid, 2):
            killer.dead = True

    killer.dead = False
    with pytest.raises(ConsortiumError):
        _engine(study, ScriptedRater(["transient"]), retry=retry, spy=killer)
    assert _only(study, tid)["state"] == "sent"
    conn = connect(study)
    try:
        caps = board_trials.RetryCaps(2, 1)
        (row,) = [r for r in board_trials.load_resumable(conn, "pilot1", caps)
                  if r["trial_id"] == tid]
        assert row["settle"] == "failed"
        board_trials.settle(conn, tid, row["attempt"], row["settle"])
    finally:
        conn.close()
    assert (_only(study, tid)["state"], _only(study, tid)["attempt"]) == ("failed", 2)
    assert _attempts(study)[(tid, 2)]["category"] == "transient"  # not attempts_exhausted


def test_total_cap_with_abandoned_attempts() -> None:
    """At the cap an abandoned latest attempt settles failed (attempts_exhausted)."""
    caps = board_trials.RetryCaps(1, 1)
    assert caps.max_attempts == 3
    abandoned = (None, None, None, None)
    assert board_trials.settlement("sent", 2, abandoned, caps, (1, 0)) is None
    assert board_trials.settlement("sent", 3, abandoned, caps, (1, 1)) == "failed"
    collectable = ("{}", "t", None, None)
    assert board_trials.settlement("sent", 3, collectable, caps, (1, 1)) is None
    transient = ("{}", "t", None, "transient")
    assert board_trials.settlement("sent", 2, transient, caps, (0, 1)) is None
    assert board_trials.settlement("sent", 2, transient, caps, (0, 2)) == "failed"
    assert board_trials.settlement("sent", 3, transient, caps, (1, 1)) == "failed"
    invalid = ("{}", "t", 0, "ok")
    assert board_trials.settlement("sent", 2, invalid, caps, (1, 1)) is None
    assert board_trials.settlement("sent", 2, invalid, caps, (2, 0)) == "invalid"


def test_seeded_kill_in_backoff_open_resume(tmp_path: Path) -> None:
    """End to end: a Trial stopped mid-backoff gets a new attempt on ``open --resume``."""
    study = _make_study(tmp_path, {"transient_rate": 0.5})
    plan, _, _ = plan_and_render(study, "pilot1")
    trials = {t.trial_id: {"session_id": t.session_id, "trial_index": t.trial_index}
              for t in plan.trials}
    tid = next(t for t, row in trials.items()
               if fake_category(_seed(row, 1), transient_rate=0.5) == "transient")
    state = {"dead": False}

    def spy(op: str, key: tuple) -> None:
        if state["dead"]:
            raise Killed(op)
        if op == "record_transient" and key == (tid, 1):
            state["dead"] = True

    with pytest.raises(ConsortiumError):
        open_test(study, "pilot1", dry_run=False, yes=True, writer_spy=spy)
    ops: list[tuple[str, tuple]] = []
    summary = open_test(study, "pilot1", dry_run=False, yes=True, resume=True,
                        writer_spy=lambda op, key: ops.append((op, key)))
    assert all(s in ("valid", "failed") for s in summary.states)
    mine = [(op, key) for op, key in ops if key and key[0] == tid]
    assert mine[0] == ("begin_attempt", (tid,))  # a new attempt, attempt 1 not collected
    assert ("append_response", (tid, 1)) not in mine
    assert _only(study, tid)["attempt"] >= 2


# --------------------------------------------------------------------------- backoff


def test_backoff_delay_deterministic_and_bounded() -> None:
    retry = RetryPolicy(transient_retries=10, backoff_initial_s=2, backoff_max_s=60)
    for seed in range(200):
        for k in range(1, 9):
            base = min(60.0, 2.0 * 2 ** (k - 1))
            d = backoff_delay(retry, k, seed)
            assert d == backoff_delay(retry, k, seed)
            assert 0.5 * base <= d <= base
    # The jitter depends on the attempt seed only.
    jitters = {backoff_delay(retry, 1, s) / 2 for s in range(200)}
    assert len(jitters) > 150 and min(jitters) >= 0.5 and max(jitters) <= 1.0
    assert backoff_delay(retry, 1, 7) / 2 == backoff_delay(retry, 3, 7) / 8
    assert backoff_delay(retry, 1000, 7) <= 60
    zero = RetryPolicy(backoff_initial_s=0, backoff_max_s=0)
    assert backoff_delay(zero, 5, 1) == 0
    with pytest.raises(ValueError):
        backoff_delay(retry, 0, 1)


def test_backoff_waits_without_holding_semaphore(study: Path, monkeypatch) -> None:
    """While one Trial waits out its backoff, another Trial on the same provider runs."""
    waits: list[float] = []

    async def fake_wait_for(aw: Any, timeout: float) -> None:  # the backoff wait, skipped
        waits.append(timeout)
        aw.close()
        await asyncio.sleep(0)
        raise TimeoutError

    monkeypatch.setattr(asyncio, "wait_for", fake_wait_for)
    retry = RetryPolicy(transient_retries=2, backoff_initial_s=2, backoff_max_s=3)
    _, tids, _ = _engine(study, ScriptedRater(["transient", "transient", "ok"]), n=2,
                         retry=retry)
    for tid in tids:
        assert (_only(study, tid)["state"], _only(study, tid)["attempt"]) == ("valid", 3)
    attempts = _attempts(study)
    expected = sorted(
        backoff_delay(retry, k, attempts[(tid, k)]["seed"]) for tid in tids for k in (1, 2)
    )
    assert sorted(w for w in waits if w > 0) == expected
    assert all(1 <= w <= 3 for w in expected)


# --------------------------------------------------------------------------- a seeded run


def _simulate(trial: dict, max_retries: int = 2, transient_retries: int = 3) -> tuple:
    """(state, categories per attempt) from the seeds and the rules alone."""
    invalid = transient = 0
    categories = []
    attempt = 0
    while True:
        attempt += 1
        seed = _seed(trial, attempt)
        cat = fake_category(seed, RATES["transient_rate"], RATES["refusal_rate"],
                            RATES["fatal_rate"])
        categories.append(cat)
        if cat == "refused":
            return "refused", categories
        if cat == "fatal":
            return "failed", categories
        if cat == "transient":
            transient += 1
            if transient > transient_retries:
                return "failed", categories
            continue
        if not is_invalid_attempt(seed, RATES["invalid_rate"]):
            return "valid", categories
        invalid += 1
        if invalid > max_retries:
            return "invalid", categories


def _outcomes(study: Path) -> dict[str, tuple]:
    attempts = _attempts(study)
    return {
        tid: (t["state"], t["attempt"],
              [attempts[(tid, a)]["category"] for a in range(1, t["attempt"] + 1)])
        for tid, t in _trials(study).items()
    }


def test_seeded_run_needs_no_operator(ran: Path) -> None:
    trials = _trials(ran)
    states = {t["state"] for t in trials.values()}
    assert states == {"valid", "invalid", "refused", "failed"}
    for tid, (state, attempts, categories) in _outcomes(ran).items():
        want_state, want_categories = _simulate(trials[tid])
        assert state == want_state, tid
        assert attempts == len(want_categories), tid
        assert categories == want_categories, tid


def test_no_refused_trial_has_validated_attempt(ran: Path) -> None:
    """The refused attempt is never validated, and no refused Trial has a valid answer.

    (An earlier attempt of the Trial may have been recorded invalid before the refusal.)
    """
    attempts = _attempts(ran)
    refused = {tid: t for tid, t in _trials(ran).items() if t["state"] == "refused"}
    assert refused
    for tid, t in refused.items():
        last = attempts[(tid, t["attempt"])]
        assert (last["category"], last["valid"]) == ("refused", None)
        assert all(attempts[(tid, a)]["valid"] != 1 for a in range(1, t["attempt"] + 1))
    responses = [r for r in _lines(ran, RESPONSES_FILE) if r["category"] == "refused"]
    assert all(attempts[(r["trial_id"], r["attempt"])]["valid"] is None for r in responses)


def test_every_attempt_archived(ran: Path) -> None:
    attempts = _attempts(ran)
    req = {(r["trial_id"], r["attempt"]) for r in _lines(ran, REQUESTS_FILE)}
    resp = {(r["trial_id"], r["attempt"]): r for r in _lines(ran, RESPONSES_FILE)}
    assert req == set(resp) == set(attempts)
    for key, row in attempts.items():
        assert resp[key]["category"] == row["category"]


@pytest.mark.parametrize("concurrency", [1, 7])
def test_rerun_identical_at_any_concurrency(tmp_path: Path, ran: Path, concurrency: int) -> None:
    fresh = _make_study(tmp_path, RATES, concurrency=concurrency)
    open_test(fresh, "pilot1", dry_run=False, yes=True)
    assert _outcomes(fresh) == _outcomes(ran)


# --------------------------------------------------------------------------- review follow-ups


def _first_ids(study: Path, n: int) -> list[str]:
    plan, _, _ = plan_and_render(study, "pilot1")
    return [t.trial_id for t in plan.trials][:n]


def test_pause_wakes_trials_in_backoff(study: Path) -> None:
    """A ceiling pause ends a backoff wait at once; the waiting Trial stays sent."""
    import time

    a, b = _first_ids(study, 2)
    rater = ScriptedRater({a: ["transient", "ok"], b: ["invalid", "ok"]}, slow=frozenset({b}))
    budget = _PricedBudget(test="pilot1", models={}, prices=None, clip_seconds={},
                           ceiling=Decimal(2))
    retry = RetryPolicy(transient_retries=3, backoff_initial_s=30, backoff_max_s=60)
    start = time.monotonic()
    paused, _, _ = _engine(study, rater, n=2, retry=retry, budget=budget)
    assert time.monotonic() - start < 5  # not the 15-30 s backoff
    assert paused == "ceiling"
    trials = _trials(study)
    assert (trials[a]["state"], trials[a]["attempt"]) == ("sent", 1)
    assert (trials[b]["state"], trials[b]["attempt"]) == ("sent", 1)
    assert _attempts(study)[(a, 1)]["category"] == "transient"


def test_cap_reached_with_mixed_results(study: Path) -> None:
    caps = board_trials.RetryCaps(1, 1)
    assert caps.max_attempts == 3
    t1, t2, t3 = _first_ids(study, 3)
    rater = ScriptedRater({
        t1: ["invalid", "transient", "invalid", "ok"],
        t2: ["transient", "invalid", "transient", "ok"],
        t3: ["invalid", "transient", "ok"],
    })
    retry = RetryPolicy(transient_retries=1, backoff_initial_s=0)
    _engine(study, rater, n=3, max_retries=1, retry=retry)
    trials = _trials(study)
    attempts = _attempts(study)
    assert (trials[t1]["state"], trials[t1]["attempt"]) == ("invalid", 3)
    assert (trials[t2]["state"], trials[t2]["attempt"]) == ("failed", 3)
    assert attempts[(t2, 3)]["category"] == "transient"
    assert (trials[t3]["state"], trials[t3]["attempt"]) == ("valid", 3)
    assert not any(a > 3 for _tid, a in attempts)


@pytest.mark.parametrize(
    ("category", "state", "attempt"),
    [("refused", "refused", 1), ("fatal", "failed", 1), ("transient", "valid", 2)],
)
def test_resume_collects_stored_handle(
    study: Path, category: str, state: str, attempt: int
) -> None:
    (tid,) = _first_ids(study, 1)

    def killer(op: str, key: tuple) -> None:
        if killer.dead:
            raise Killed(op)
        if op == "set_handle" and key == (tid, 1):
            killer.dead = True

    killer.dead = False
    with pytest.raises(ConsortiumError):
        _engine(study, ScriptedRater([category, "ok"]), spy=killer)
    conn = connect(study)
    try:
        (row,) = [r for r in board_trials.load_resumable(conn, "pilot1",
                                                         board_trials.RetryCaps(2, 3))
                  if r["trial_id"] == tid]
    finally:
        conn.close()
    assert row["handle"] is not None and row["settle"] is None
    rater = ScriptedRater([category, "ok"])
    retry = RetryPolicy(transient_retries=3, backoff_initial_s=0.01, backoff_max_s=0.02)
    _, _, ops = _engine(study, rater, insert=False, only=[tid], retry=retry,
                        collect={tid: (1, row["handle"])})
    t = _only(study, tid)
    assert (t["state"], t["attempt"]) == (state, attempt)
    assert rater.collected[0] == (tid, 1)  # collected, never re-sent
    assert ("begin_attempt", (tid,)) in ops if attempt == 2 else ("begin_attempt", (tid,)) \
        not in ops
    att = _attempts(study)
    assert att[(tid, 1)]["category"] == category
    assert att[(tid, 1)]["valid"] is None
    responses = [r["attempt"] for r in _lines(study, RESPONSES_FILE) if r["trial_id"] == tid]
    assert responses == list(range(1, attempt + 1))


def test_study_retry_policy_reaches_engine_run_and_resume(tmp_path: Path) -> None:
    study = _make_study(tmp_path, {"transient_rate": 1.0}, retry={"transient_retries": 1})
    summary = open_test(study, "pilot1", dry_run=False, yes=True)
    assert set(summary.states) == {"failed"}
    assert {(t["state"], t["attempt"]) for t in _trials(study).values()} == {("failed", 2)}
    assert {a["category"] for a in _attempts(study).values()} == {"transient"}

    again = _make_study(tmp_path / "again", {"transient_rate": 1.0},
                        retry={"transient_retries": 1})
    state = {"n": 0}

    def spy(op: str, key: tuple) -> None:  # stop partway through the Run
        if op == "record_transient":
            state["n"] += 1
        if state["n"] > 40:
            raise Killed(op)

    with pytest.raises(ConsortiumError):
        open_test(again, "pilot1", dry_run=False, yes=True, writer_spy=spy)
    open_test(again, "pilot1", dry_run=False, yes=True, resume=True)
    assert {(t["state"], t["attempt"]) for t in _trials(again).values()} == {("failed", 2)}
