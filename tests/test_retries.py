"""Story 1.10: validation in the engine, retries, invalid Trials and the invalid-answer rate."""

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

from consortium.archive.jsonl import REQUESTS_FILE, RESPONSES_FILE, read_responses
from consortium.board import trials as board_trials
from consortium.board.clips import insert_clip
from consortium.board.db import connect, transaction
from consortium.board.writer import start_writer
from consortium.config.load import load_study
from consortium.core.errors import ConsortiumError
from consortium.core.seeds import derive_seed
from consortium.core.validate import invalid_rate
from consortium.engine.dispatch import Budget, dispatch
from consortium.raters.base import RaterCall, RaterResult
from consortium.raters.fake import FakeRater, is_invalid_attempt
from consortium.stages import personas as personas_stage
from consortium.stages.init import init_study
from consortium.stages.open import open_test, plan_and_render
from consortium.stages.push import push_test

TARGETS = ["c_aaaaaaaa", "c_bbbbbbbb"]
PRACTICE = ["c_ppppppaa", "c_ppppppab", "c_ppppppac"]
GODSPEED = {f"animacy_{i}": 3 for i in range(1, 7)} | {f"likeability_{i}": 3 for i in range(1, 6)}
TRIALS = 64 * (2 + 2)  # 64 Personas x 1 Model x 1 Repeat x (2 godspeed + 2 pairwise)
RATE = 0.5
STUDY_SEED = 1


class Killed(Exception):
    """Stands in for the process dying: no writer op runs after it."""


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


def _write_test(study: Path, name: str, max_retries: int | None) -> Path:
    session: dict[str, Any] = {"repeats": 1, "practice_clips": 1}
    if max_retries is not None:
        session["max_retries"] = max_retries
    doc = {
        "schema_version": 1, "test": name, "kind": "pilot",
        "instruments": ["godspeed", "pairwise_alive"], "clips": TARGETS,
        "practice": [
            {"instrument": "godspeed", "clips": [PRACTICE[0]], "answer": GODSPEED},
            {"instrument": "pairwise_alive", "clips": PRACTICE[1:3], "answer": {"alive": "A"}},
        ],
        "session": session,
    }
    src = study.parent / f"{name}.yaml"
    src.write_text(yaml.safe_dump(doc, sort_keys=False))
    return src


def _make_study(root: Path, rate: float = RATE) -> Path:
    study = init_study(root / "study")
    cfg = yaml.safe_load((study / "study.yaml").read_text())
    assert cfg["seed"] == STUDY_SEED
    cfg["models"][0]["fake"]["invalid_rate"] = rate
    (study / "study.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False))
    personas_stage.generate(study)
    _add_clips(study, TARGETS + PRACTICE)
    push_test(study, _write_test(study, "pilot1", None))  # study default: max_retries 2
    push_test(study, _write_test(study, "zero", 0))
    return study


@pytest.fixture(scope="module")
def template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _make_study(tmp_path_factory.mktemp("retries"))


@pytest.fixture(scope="module")
def ran(tmp_path_factory: pytest.TempPathFactory, template: Path) -> Path:
    """A completed Run of pilot1 (max_retries 2) at invalid_rate 0.5; read-only for tests."""
    copy = tmp_path_factory.mktemp("ran") / "study"
    shutil.copytree(template, copy)
    open_test(copy, "pilot1", dry_run=False, yes=True)
    return copy


@pytest.fixture()
def study(tmp_path: Path, template: Path) -> Path:
    copy = tmp_path / "study"
    shutil.copytree(template, copy)
    return copy


def _trials(study: Path, test: str = "pilot1") -> dict[str, dict]:
    conn = connect(study)
    try:
        return {r["trial_id"]: r for r in board_trials.load_trials(conn, test)}
    finally:
        conn.close()


def _attempts(study: Path) -> dict[tuple[str, int], dict]:
    conn = connect(study)
    try:
        cols = ("trial_id", "attempt", "seed", "handle", "sent_at", "category", "valid",
                "invalid_reason", "answer_json")
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


def _expected(trial: dict, max_retries: int, rate: float = RATE) -> tuple[str, int]:
    """(state, attempts) a Trial must end with, computed from the seeds alone."""
    for attempt in range(1, max_retries + 2):
        if not is_invalid_attempt(_seed(trial, attempt), rate):
            return "valid", attempt
    return "invalid", max_retries + 1


def _with_pattern(trials: dict[str, dict], pattern: list[bool]) -> str:
    """A Trial whose first attempts are invalid (True) / valid (False) as in ``pattern``."""
    for tid, t in trials.items():
        if [is_invalid_attempt(_seed(t, a), RATE) for a in range(1, len(pattern) + 1)] == pattern:
            return tid
    raise AssertionError(f"no Trial with pattern {pattern}")


# --------------------------------------------------------------------------- a seeded run


def test_states_and_attempts_follow_the_seeds(ran: Path) -> None:
    trials = _trials(ran)
    assert len(trials) == TRIALS
    for t in trials.values():
        assert (t["state"], t["attempt"]) == _expected(t, 2), t["trial_id"]
    states = {t["state"] for t in trials.values()}
    assert states == {"valid", "invalid"}
    assert max(t["attempt"] for t in trials.values()) == 3  # never a 4th attempt


def test_every_attempt_archived_and_recorded(ran: Path) -> None:
    trials = _trials(ran)
    attempts = _attempts(ran)
    requests = _lines(ran, REQUESTS_FILE)
    responses = _lines(ran, RESPONSES_FILE)
    req_keys = [(r["trial_id"], r["attempt"]) for r in requests]
    resp_keys = [(r["trial_id"], r["attempt"]) for r in responses]
    assert len(req_keys) == len(set(req_keys))
    assert set(req_keys) == set(resp_keys) == set(attempts)
    by_key = {(r["trial_id"], r["attempt"]): r for r in requests}
    raw = {(r["trial_id"], r["attempt"]): r["raw"] for r in responses}
    for tid, t in trials.items():
        keys = [(tid, a) for a in range(1, t["attempt"] + 1)]
        # Seeds differ across attempts; the request text never changes.
        seeds = [attempts[k]["seed"] for k in keys]
        assert seeds == [_seed(t, a) for a in range(1, t["attempt"] + 1)]
        assert len(set(seeds)) == len(seeds)
        assert len({by_key[k]["request_sha256"] for k in keys}) == 1
        assert [by_key[k]["seed"] for k in keys] == seeds
        for k in keys:
            a = attempts[k]
            invalid = is_invalid_attempt(a["seed"], RATE)
            assert a["valid"] == (0 if invalid else 1)
            assert a["category"] == "ok"
            if invalid:
                assert a["answer_json"] is None
                assert a["invalid_reason"] == _rotation_reason(t, k[1])
            else:
                assert a["invalid_reason"] is None
                assert json.loads(a["answer_json"]) == json.loads(raw[k])


def _rotation_reason(trial: dict, attempt: int) -> str:
    kind = ("not_json", "missing_item", "out_of_range")[(attempt - 1) % 3]
    if kind != "out_of_range":
        return kind
    return "out_of_range:animacy_1" if trial["instrument"] == "godspeed" else "bad_choice:alive"


def test_recovers(ran: Path) -> None:
    trials = _trials(ran)
    tid = _with_pattern(trials, [True, False])
    assert (trials[tid]["state"], trials[tid]["attempt"]) == ("valid", 2)
    conn = connect(ran)
    try:
        chosen = board_trials.chosen_answer(conn, tid)
    finally:
        conn.close()
    assert chosen is not None and chosen["attempt"] == 2
    responses = read_responses(ran)
    assert chosen["answers"] == json.loads(responses[(tid, 2)]["raw"])
    assert {k for k in responses if k[0] == tid} == {(tid, 1), (tid, 2)}
    assert sum(r["trial_id"] == tid for r in _lines(ran, REQUESTS_FILE)) == 2


def test_exhausted(ran: Path) -> None:
    trials = _trials(ran)
    tid = _with_pattern(trials, [True, True, True])
    assert (trials[tid]["state"], trials[tid]["attempt"]) == ("invalid", 3)
    assert sum(r["trial_id"] == tid for r in _lines(ran, REQUESTS_FILE)) == 3
    conn = connect(ran)
    try:
        assert board_trials.chosen_answer(conn, tid) is None
    finally:
        conn.close()


def test_invalid_rates_known_count(ran: Path) -> None:
    trials = _trials(ran)
    expected_invalid = sum(_expected(t, 2)[0] == "invalid" for t in trials.values())
    # 0.5 ** 3 of 256 Trials is 32 on average; the seeded count is fixed.
    assert expected_invalid == 30
    conn = connect(ran)
    try:
        rates = board_trials.invalid_rates(conn, "pilot1")
    finally:
        conn.close()
    m1 = rates["by_model"]["m1"]
    assert m1 == {"valid": TRIALS - 30, "invalid": 30, "refused": 0, "failed": 0,
                  "rate": 30 / TRIALS}
    assert m1["rate"] == invalid_rate(m1)
    assert len(rates["by_agent"]) == 64
    assert sum(e["invalid"] for e in rates["by_agent"].values()) == 30
    for agent, entry in rates["by_agent"].items():
        mine = [t for t in trials.values() if t["agent_id"] == agent]
        n_invalid = sum(t["state"] == "invalid" for t in mine)
        assert entry["invalid"] == n_invalid
        assert entry["rate"] == n_invalid / len(mine)


def test_rerun_in_fresh_study_is_identical(tmp_path: Path, ran: Path) -> None:
    fresh = _make_study(tmp_path)
    open_test(fresh, "pilot1", dry_run=False, yes=True)
    a = {tid: (t["state"], t["attempt"]) for tid, t in _trials(ran).items()}
    b = {tid: (t["state"], t["attempt"]) for tid, t in _trials(fresh).items()}
    assert a == b


def test_max_retries_zero(study: Path) -> None:
    open_test(study, "zero", dry_run=False, yes=True)
    trials = _trials(study, "zero")
    for t in trials.values():
        invalid = is_invalid_attempt(_seed(t, 1), RATE)
        assert (t["state"], t["attempt"]) == ("invalid" if invalid else "valid", 1)
    assert {t["state"] for t in trials.values()} == {"valid", "invalid"}


def test_invalid_rate_zero_never_retries(tmp_path: Path) -> None:
    study = _make_study(tmp_path, rate=0)
    open_test(study, "pilot1", dry_run=False, yes=True)
    assert {(t["state"], t["attempt"]) for t in _trials(study).values()} == {("valid", 1)}


# --------------------------------------------------------------------------- answer rule


def test_two_valid_attempts_highest_wins(ran: Path, tmp_path: Path) -> None:
    study = tmp_path / "study"
    shutil.copytree(ran, study)
    trials = _trials(study)
    tid = _with_pattern(trials, [True, True, False])
    conn = connect(study)
    try:
        # Resume late-collects attempt 1's handle: it turns out valid too.
        board_trials.record_validation(conn, tid, 1, True, None, json.dumps(GODSPEED))
        chosen = board_trials.chosen_answer(conn, tid)
        assert chosen is not None and chosen["attempt"] == 3
        with pytest.raises(ValueError):  # the terminal state is never overwritten
            board_trials.set_state(conn, tid, 3, "invalid", "ok")
        with pytest.raises(ValueError):
            board_trials.set_state(conn, tid, 1, "valid", "ok")
        assert not board_trials.settle_exhausted(conn, tid, 3)
    finally:
        conn.close()
    assert (_trials(study)[tid]["state"], _trials(study)[tid]["attempt"]) == ("valid", 3)


# --------------------------------------------------------------------------- kill and resume


def _killer(stop):
    state = {"dead": False}

    def spy(op: str, key: tuple) -> None:
        if state["dead"]:
            raise Killed(f"killed before {op} {key}")
        if stop(op, key):
            state["dead"] = True  # this op still runs; every later one does not

    return spy


def _kill_run(study: Path, stop, test: str = "pilot1") -> None:
    with pytest.raises(ConsortiumError) as info:
        open_test(study, test, dry_run=False, yes=True, writer_spy=_killer(stop))
    assert info.value.code == "run_failed"


def _resume(study: Path, test: str = "pilot1", **kw: Any):
    return open_test(study, test, dry_run=False, yes=True, resume=True, **kw)


def test_kill_mid_retry_redispatches_new_attempt(study: Path) -> None:
    trials = _trials_planned(study)
    tid = _with_pattern(trials, [True, True, False])  # needs attempt 3 normally
    _kill_run(study, lambda op, key: op == "append_request" and key == (tid, 2))
    before = _attempts(study)
    assert (tid, 2) in before and before[(tid, 2)]["sent_at"] is None
    assert before[(tid, 1)]["valid"] == 0
    assert _trials(study)[tid]["state"] == "sent"

    _resume(study)
    after = _attempts(study)
    t = _trials(study)[tid]
    # Attempt 2 is never re-used or collected; attempt 3 has a new seed and is the last allowed.
    assert t["attempt"] == 3
    assert after[(tid, 2)]["valid"] is None and after[(tid, 2)]["handle"] is None
    assert (tid, 2) not in read_responses(study)
    assert after[(tid, 3)]["seed"] == _seed(t, 3) != after[(tid, 2)]["seed"]
    assert t["state"] == ("invalid" if is_invalid_attempt(_seed(t, 3), RATE) else "valid")
    assert max(x["attempt"] for x in _trials(study).values()) <= 3
    # Every other Trial with no abandoned attempt ended as an uninterrupted Run would.
    responses = read_responses(study)
    abandoned = {k[0] for k in after if k not in responses}
    for other, x in _trials(study).items():
        if other not in abandoned:
            assert (x["state"], x["attempt"]) == _expected(x, 2), other


def test_kill_after_invalid_recorded_gets_new_attempt(study: Path) -> None:
    trials = _trials_planned(study)
    tid = _with_pattern(trials, [True, False])
    _kill_run(study, lambda op, key: op == "record_validation" and key == (tid, 1))
    assert _attempts(study)[(tid, 1)]["valid"] == 0
    conn = connect(study)
    try:
        row = next(r for r in board_trials.load_resumable(conn, "pilot1") if r["trial_id"] == tid)
    finally:
        conn.close()
    assert row["state"] == "sent" and row["handle"] is None  # not collected again
    responses_before = sum(r["trial_id"] == tid for r in _lines(study, RESPONSES_FILE))
    _resume(study)
    t = _trials(study)[tid]
    assert (t["state"], t["attempt"]) == ("valid", 2)
    lines = [r for r in _lines(study, RESPONSES_FILE) if r["trial_id"] == tid]
    assert responses_before == 1 and [r["attempt"] for r in lines] == [1, 2]
    for other, x in _trials(study).items():
        assert (x["state"], x["attempt"]) == _expected(x, 2), other


def test_kill_before_last_invalid_state_settles_without_4th_attempt(study: Path) -> None:
    trials = _trials_planned(study)
    tid = _with_pattern(trials, [True, True, True])
    _kill_run(study, lambda op, key: op == "record_validation" and key == (tid, 3))
    assert _trials(study)[tid]["state"] == "sent"
    _resume(study)
    t = _trials(study)[tid]
    assert (t["state"], t["attempt"]) == ("invalid", 3)
    assert (tid, 4) not in _attempts(study)
    assert sum(r["trial_id"] == tid for r in _lines(study, REQUESTS_FILE)) == 3


def _trials_planned(study: Path, test: str = "pilot1") -> dict[str, dict]:
    """The Plan's Trials (with session_id and trial_index), before any Run."""
    plan, _, _ = plan_and_render(study, test)
    return {
        t.trial_id: {"trial_id": t.trial_id, "session_id": t.session_id,
                     "trial_index": t.trial_index}
        for t in plan.trials
    }


# --------------------------------------------------------------------------- ceiling


class _PricedBudget(Budget):
    def reservation(self, trial, request) -> Decimal:
        return Decimal(1)

    def actual(self, model_id, usage) -> Decimal | None:
        return Decimal(1)


def test_retry_blocked_by_ceiling_stays_sent(study: Path) -> None:
    plan, requests, _ = plan_and_render(study, "pilot1")
    pairs = list(zip(plan.trials, requests, strict=True))
    planned = _trials_planned(study)
    tid = _with_pattern(planned, [True, False])
    pair = next(p for p in pairs if p[0].trial_id == tid)

    async def run(ceiling: str, insert: bool) -> str | None:
        async with start_writer(study) as writer:
            if insert:
                await writer.do("insert_plan",
                                lambda c: board_trials.insert_plan(c, "pilot1", plan.trials))
            budget = _PricedBudget(test="pilot1", models={}, prices=None, clip_seconds={},
                                   ceiling=Decimal(ceiling))
            return await dispatch(study, [pair], {"m1": FakeRater(invalid_rate=RATE)},
                                  writer=writer, seed=STUDY_SEED, concurrency=1,
                                  budget=budget, max_retries=2)

    assert asyncio.run(run("1", True)) == "ceiling"  # attempt 1 fits; its retry does not
    t = _trials(study)[tid]
    assert (t["state"], t["attempt"]) == ("sent", 1)
    assert _attempts(study)[(tid, 1)]["valid"] == 0
    assert asyncio.run(run("2", False)) is None  # resumed with a higher ceiling
    t = _trials(study)[tid]
    assert (t["state"], t["attempt"]) == ("valid", 2)


# --------------------------------------------------------------------------- review follow-ups


def test_kill_and_resume_zero_retries_after_invalid(study: Path) -> None:
    trials = _trials_planned(study, "zero")
    tid = _with_pattern(trials, [True])
    _kill_run(study, lambda op, key: op == "record_validation" and key == (tid, 1), "zero")
    assert _trials(study, "zero")[tid]["state"] == "sent"
    summary = _resume(study, "zero")
    assert summary.resumed["settled"] >= 1
    t = _trials(study, "zero")[tid]
    assert (t["state"], t["attempt"]) == ("invalid", 1)
    assert (tid, 2) not in _attempts(study)


def test_abandoned_last_attempt_settles_failed(study: Path) -> None:
    trials = _trials_planned(study)
    tid = _with_pattern(trials, [True, True, True])
    _kill_run(study, lambda op, key: op == "append_request" and key == (tid, 3))
    summary = _resume(study)
    assert summary.resumed["settled"] >= 1
    t = _trials(study)[tid]
    assert (t["state"], t["attempt"]) == ("failed", 3)
    attempts = _attempts(study)
    assert attempts[(tid, 3)]["category"] == "attempts_exhausted"
    assert attempts[(tid, 3)]["valid"] is None
    assert (tid, 4) not in attempts
    assert (tid, 3) not in read_responses(study)
    expected_invalid = sum(_expected(x, 2)[0] == "invalid" for x in _trials(study).values()
                           if x["trial_id"] != tid)
    conn = connect(study)
    try:
        m1 = board_trials.invalid_rates(conn, "pilot1")["by_model"]["m1"]
    finally:
        conn.close()
    assert (m1["invalid"], m1["failed"]) == (expected_invalid, 1)
    assert m1["rate"] == expected_invalid / (TRIALS - 1)


def test_kill_after_valid_recorded_settles_without_recollect(study: Path) -> None:
    trials = _trials_planned(study)
    tid = _with_pattern(trials, [True, False])
    _kill_run(study, lambda op, key: op == "record_validation" and key == (tid, 2))
    assert _attempts(study)[(tid, 2)]["valid"] == 1
    assert _trials(study)[tid]["state"] == "sent"
    ops: list[tuple[str, tuple]] = []
    summary = _resume(study, writer_spy=lambda op, key: ops.append((op, key)))
    assert summary.resumed["settled"] >= 1
    assert (_trials(study)[tid]["state"], _trials(study)[tid]["attempt"]) == ("valid", 2)
    assert [op for op, key in ops if key and key[0] == tid] == ["settle"]
    lines = [r for r in _lines(study, RESPONSES_FILE) if r["trial_id"] == tid]
    assert [r["attempt"] for r in lines] == [1, 2]  # no second line for attempt 2


def test_late_collected_invalid_answer_is_retried(study: Path) -> None:
    trials = _trials_planned(study)
    tid = _with_pattern(trials, [True, False])
    _kill_run(study, lambda op, key: op == "set_handle" and key == (tid, 1))
    assert _attempts(study)[(tid, 1)]["valid"] is None
    summary = _resume(study)
    assert summary.resumed["collect"] >= 1
    t = _trials(study)[tid]
    assert (t["state"], t["attempt"]) == ("valid", 2)
    assert _attempts(study)[(tid, 1)]["valid"] == 0


class _ErrorRater(FakeRater):
    async def collect(self, handles):
        return [RaterResult(r.raw, r.usage, r.model_build, "error")
                for r in await super().collect(handles)]


def test_non_ok_category_is_not_validated_or_retried(study: Path) -> None:
    plan, requests, _ = plan_and_render(study, "pilot1")
    pairs = list(zip(plan.trials, requests, strict=True))[:4]
    ops: list[str] = []

    async def run() -> None:
        async with start_writer(study, spy=lambda op, key: ops.append(op)) as writer:
            await writer.do("insert_plan",
                            lambda c: board_trials.insert_plan(c, "pilot1", plan.trials))
            await dispatch(study, pairs, {"m1": _ErrorRater(invalid_rate=RATE)}, writer=writer,
                           seed=STUDY_SEED, concurrency=2, budget=Budget.zero("pilot1"),
                           max_retries=2)

    asyncio.run(run())
    assert "record_validation" not in ops
    trials = _trials(study)
    for trial, _ in pairs:
        assert (trials[trial.trial_id]["state"], trials[trial.trial_id]["attempt"]) == \
            ("failed", 1)
        assert _attempts(study)[(trial.trial_id, 1)]["valid"] is None


def test_negative_max_retries_refused(study: Path) -> None:
    async def run() -> None:
        async with start_writer(study) as writer:
            await dispatch(study, [], {}, writer=writer, seed=1, concurrency=1,
                           budget=Budget.zero("pilot1"), max_retries=-1)

    with pytest.raises(ConsortiumError) as info:
        asyncio.run(run())
    assert info.value.code == "bad_max_retries"


@pytest.mark.parametrize("value", ["1.5", "-0.1", '"0.5"'])
def test_invalid_rate_config_bounds(tmp_path: Path, value: str) -> None:
    study = init_study(tmp_path / "s")
    cfg = study / "study.yaml"
    text = cfg.read_text()
    assert "      invalid_rate: 0 " in text
    cfg.write_text(text.replace("      invalid_rate: 0 ", f"      invalid_rate: {value} ", 1))
    with pytest.raises(ConsortiumError) as info:
        load_study(study)
    assert info.value.code == "config_invalid"
    assert info.value.message.startswith("models.0.fake.invalid_rate")


def test_fake_valid_answers_unbiased_at_half_invalid(template: Path) -> None:
    plan, requests, _ = plan_and_render(template, "pilot1")
    trial, request = next((t, r) for t, r in zip(plan.trials, requests, strict=True)
                          if t.instrument == "godspeed")
    rater = FakeRater(invalid_rate=0.5)
    counts = dict.fromkeys(range(1, 6), 0)
    valid = 0
    for seed in range(10000):
        raw = rater.answer(RaterCall(trial.trial_id, 1, seed, request, ()))
        if is_invalid_attempt(seed, 0.5):
            continue
        valid += 1
        counts[json.loads(raw)["animacy_1"]] += 1
    assert 4500 < valid < 5500
    for value, n in counts.items():
        assert abs(n - valid / 5) < valid / 5 * 0.15, (value, counts)
