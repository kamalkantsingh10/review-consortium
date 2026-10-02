"""Story 3.3: ``core.eligibility.eligible`` (pure), one test per I/O matrix row and the
precedence rules."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from consortium.board import screening as board_screening
from consortium.board.db import connect
from consortium.core.eligibility import Stamps, eligible
from consortium.core.plan import plan_test

PERSONAS = ["p1", "p2", "p3", "p4", "p5"]
MODELS = ["m1", "m2"]
INSTRUMENTS = ["godspeed", "pairwise_alive"]
AGENTS = [(p, m) for p in PERSONAS for m in MODELS]
STAMPS = Stamps(
    settings={"m1": "S1", "m2": "S2"},
    instruments={"godspeed": "IG", "pairwise_alive": "IP"},
    fidelity="F",
)


def _perception(model: str, name: str, outcome: str = "pass", run: str = "s2", *,
                settings: str | None = None, stamp: str | None = None,
                pair_checks: int = 1) -> dict[str, Any]:
    return {
        "run_id": run, "model_id": model, "agent_id": None, "instrument": name,
        "outcome": outcome, "pair_checks": pair_checks,
        "settings_hash": settings or STAMPS.settings[model],
        "instrument_hash": stamp or STAMPS.instruments[name],
    }


def _fidelity(persona: str, model: str, outcome: str = "pass", run: str = "s1", *,
              settings: str | None = None, stamp: str = "F") -> dict[str, Any]:
    return {
        "run_id": run, "model_id": model, "agent_id": f"{persona}-{model}",
        "instrument": "fidelity", "outcome": outcome, "pair_checks": None,
        "settings_hash": settings or STAMPS.settings[model], "instrument_hash": stamp,
    }


def _all_pass(skip: set[str] = frozenset(), **outcomes: str) -> list[dict[str, Any]]:
    rows = [_perception(m, n) for m in MODELS for n in INSTRUMENTS]
    rows += [_fidelity(p, m, outcomes.get(f"{p}_{m}", "pass")) for p, m in AGENTS
             if f"{p}-{m}" not in skip]
    return rows


def test_all_pass() -> None:
    e = eligible(AGENTS, INSTRUMENTS, _all_pass(), STAMPS)
    assert e.refusal() is None
    assert e.agents_ok == tuple(f"{p}-{m}" for p, m in AGENTS)
    assert e.excluded == () and e.counts() == {}
    assert e.runs == {"fidelity": ("s1",), "perception": ("s2",)}


def test_fidelity_fail_and_missing() -> None:
    e = eligible(AGENTS, INSTRUMENTS, _all_pass(skip={"p5-m1"}, p3_m1="fail"), STAMPS)
    assert e.refusal() is None
    assert [(x.agent_id, x.reason, x.instrument) for x in e.excluded] == [
        ("p3-m1", "fidelity_fail", None), ("p5-m1", "fidelity_missing", None)]
    assert e.counts() == {"fidelity_fail": 1, "fidelity_missing": 1}
    assert len(e.agents_ok) == len(AGENTS) - 2


def test_model_fails_one_instrument() -> None:
    rows = [r for r in _all_pass() if not (r["model_id"] == "m2"
                                           and r["instrument"] == "pairwise_alive")]
    rows.append(_perception("m2", "pairwise_alive", "fail"))
    e = eligible(AGENTS, INSTRUMENTS, rows, STAMPS)
    assert e.refusal() is None
    assert {x.agent_id for x in e.excluded} == {f"{p}-m2" for p in PERSONAS}
    assert {(x.reason, x.instrument) for x in e.excluded} == {("perception_fail",
                                                               "pairwise_alive")}
    assert e.details("perception_fail") == {"m2": ["pairwise_alive"]}
    assert e.agents_ok == tuple(f"{p}-m1" for p in PERSONAS)


def test_perception_missing() -> None:
    rows = [r for r in _all_pass() if not (r["model_id"] == "m2" and r["agent_id"] is None
                                           and r["instrument"] == "godspeed")]
    e = eligible(AGENTS, INSTRUMENTS, rows, STAMPS)
    assert e.counts() == {"perception_missing": 5}
    assert e.details("perception_missing") == {"m2": ["godspeed"]}


def test_no_coverage() -> None:
    rows = [r for r in _all_pass() if r["instrument"] != "pairwise_alive"]
    rows.append(_perception("m1", "pairwise_alive", "fail", pair_checks=0))  # low-level only
    err = eligible(AGENTS, INSTRUMENTS, rows, STAMPS).refusal()
    assert err is not None and err.code == "screening_coverage_missing"
    assert "pairwise_alive" in err.message and "godspeed" not in err.message


def test_stale_rows_never_cover() -> None:
    # Only a stale m1 row has pair checks for pairwise_alive: not covered (Story 3.2), and
    # refused as stale (re-screening fixes it), even with m1 out of the Test.
    rows = [r for r in _all_pass() if r["instrument"] != "pairwise_alive"]
    rows.append(_perception("m1", "pairwise_alive", settings="OLD"))
    e = eligible([a for a in AGENTS if a[1] == "m2"], INSTRUMENTS, rows, STAMPS)
    assert e.coverage_missing == ()
    err = e.refusal()
    assert err is not None and err.code == "screening_stale" and "m1" in err.message
    # Pair checks only in a low-level-only row: coverage is missing.
    rows[-1] = _perception("m1", "pairwise_alive", pair_checks=0)
    err = eligible(AGENTS, INSTRUMENTS, rows, STAMPS).refusal()
    assert err is not None and err.code == "screening_coverage_missing"
    rows[-1] = _perception("m1", "pairwise_alive", settings="OLD")
    # A counting row of any other Model covers it; m1's key is still stale.
    rows.append(_perception("m2", "pairwise_alive"))
    err = eligible(AGENTS, INSTRUMENTS, rows, STAMPS).refusal()
    assert err is not None and err.code == "screening_stale"


def test_settings_changed_is_stale() -> None:
    changed = Stamps({"m1": "S1-new", "m2": "S2"}, STAMPS.instruments, "F")
    e = eligible(AGENTS, INSTRUMENTS, _all_pass(), changed)
    err = e.refusal()
    assert err is not None and err.code == "screening_stale"
    assert "Model m1 (settings changed)" in err.message and "m2" not in err.message
    assert "screen personas" in err.message and "screen models" in err.message
    assert {x.reason for x in e.excluded} == {"screening_stale"}
    assert {x.agent_id for x in e.excluded} == {f"{p}-m1" for p in PERSONAS}


def test_instrument_edited_is_stale() -> None:
    changed = Stamps(STAMPS.settings, {"godspeed": "IG-new", "pairwise_alive": "IP"}, "F")
    err = eligible(AGENTS, INSTRUMENTS, _all_pass(), changed).refusal()
    assert err is not None and err.code == "screening_stale"
    assert "Instrument godspeed (definition changed)" in err.message
    assert "Model" not in err.message and "screen personas" not in err.message


def test_fidelity_stamp_changed_is_stale() -> None:
    changed = Stamps(STAMPS.settings, STAMPS.instruments, "F-new")
    err = eligible(AGENTS, INSTRUMENTS, _all_pass(), changed).refusal()
    assert err is not None and err.code == "screening_stale"
    assert "screen personas" in err.message and "screen models" not in err.message


def _board_rows(tmp_path: Path, runs: list[tuple[str, list[dict[str, Any]]]]) -> list[dict]:
    """Store each ``(kind, rows)`` as a complete screening run through ``board.screening``
    and read back ``complete_results``."""
    conn = connect(tmp_path)
    try:
        for kind, rows in runs:
            run_id = board_screening.next_run_id(conn)
            board_screening.new_run(
                conn, run_id, kind, "scr" if kind == "perception" else None,
                f"seeded/{run_id}", "0" * 64, [], STAMPS.settings, instrument_hash="x",
            )
            board_screening.complete_run(conn, run_id, [
                {k: v for k, v in r.items() if k != "run_id"}
                | {"score": 1.0, "threshold": 0.8, "detail": "{}"} for r in rows])
        return board_screening.complete_results(conn)
    finally:
        conn.close()


def _fidelity_all() -> list[dict[str, Any]]:
    return [_fidelity(p, m) for p, m in AGENTS]


def test_superseded_new_pass_counts(tmp_path: Path) -> None:
    passing = [_perception(m, n) for m in MODELS for n in INSTRUMENTS]
    failing = [r | {"outcome": "fail"} if r["model_id"] == "m1" else r for r in passing]
    rows = _board_rows(tmp_path, [("fidelity", _fidelity_all()), ("perception", failing),
                                  ("perception", passing)])
    e = eligible(AGENTS, INSTRUMENTS, rows, STAMPS)
    assert e.refusal() is None and e.excluded == ()
    assert e.runs == {"fidelity": ("s1",), "perception": ("s3",)}


def test_stale_newest_and_fresh_older_passes(tmp_path: Path) -> None:
    # s2 passes at today's stamps; s3 was run under other m1 settings (since reverted).
    passing = [_perception(m, n) for m in MODELS for n in INSTRUMENTS]
    stale = [r | {"settings_hash": "OLD", "outcome": "fail"} for r in passing]
    rows = _board_rows(tmp_path, [("fidelity", _fidelity_all()), ("perception", passing),
                                  ("perception", stale)])
    assert {r["run_id"] for r in rows} == {"s1", "s2", "s3"}
    e = eligible(AGENTS, INSTRUMENTS, rows, STAMPS)
    assert e.refusal() is None and e.excluded == ()
    assert e.runs["perception"] == ("s2",)


def test_fresh_result_beats_a_stale_one(tmp_path: Path) -> None:
    passing = [_perception(m, n) for m in MODELS for n in INSTRUMENTS]
    rows = _board_rows(tmp_path, [
        ("fidelity", _fidelity_all()), ("perception", passing),
        ("perception", [_perception("m1", "godspeed", "fail", settings="OLD")])])
    e = eligible(AGENTS, INSTRUMENTS, rows, STAMPS)
    assert e.refusal() is None and e.excluded == ()


def test_excluded_models_stale_keys_do_not_refuse() -> None:
    rows = [r for r in _all_pass() if r["model_id"] != "m2"]
    rows += [_perception("m2", "godspeed", "fail"),
             _perception("m2", "pairwise_alive", settings="OLD")]
    rows += [_fidelity(p, "m2", stamp="OLD") for p in PERSONAS]
    e = eligible(AGENTS, INSTRUMENTS, rows, STAMPS)
    assert e.refusal() is None
    assert e.counts() == {"perception_fail": 5}
    assert {(x.instrument, x.reason) for x in e.excluded} == {("godspeed", "perception_fail")}


def test_self_report_instrument_needs_no_perception() -> None:
    rows = _all_pass()  # no perception row for the self-report Instrument
    e = eligible(AGENTS, [*INSTRUMENTS, "fidelity_bfi10"], rows, STAMPS,
                 self_report={"fidelity_bfi10"})
    assert e.refusal() is None and e.excluded == ()
    err = eligible(AGENTS, [*INSTRUMENTS, "fidelity_bfi10"], rows, STAMPS).refusal()
    assert err is not None and err.code == "screening_coverage_missing"


def test_unknown_outcome_is_rejected() -> None:
    rows = _all_pass()
    rows[0] = rows[0] | {"outcome": "maybe"}
    with pytest.raises(ValueError, match="maybe"):
        eligible(AGENTS, INSTRUMENTS, rows, STAMPS)


def test_none_eligible() -> None:
    rows = _all_pass(**{f"{p}_{m}": "fail" for p, m in AGENTS})
    e = eligible(AGENTS, INSTRUMENTS, rows, STAMPS)
    err = e.refusal()
    assert err is not None and err.code == "no_eligible_agents"
    assert "fidelity_fail 10" in err.message


def test_one_reason_per_agent_with_precedence() -> None:
    # perception_fail > screening_stale > perception_missing > fidelity_*
    # m2 fails perception and every m2 Agent fails fidelity: perception wins.
    rows = [r for r in _all_pass(**{f"{p}_m2": "fail" for p in PERSONAS})
            if not (r["model_id"] == "m2" and r["agent_id"] is None
                    and r["instrument"] == "godspeed")]
    rows.append(_perception("m2", "godspeed", "fail"))
    e = eligible(AGENTS, INSTRUMENTS, rows, STAMPS)
    assert e.counts() == {"perception_fail": 5}
    assert len(e.excluded) == len({x.agent_id for x in e.excluded}) == 5
    # A stale fidelity row beats perception_missing (and fidelity_fail).
    rows = [r for r in _all_pass(**{f"{p}_m2": "fail" for p in PERSONAS})
            if not (r["model_id"] == "m2" and r["agent_id"] is None
                    and r["instrument"] == "godspeed") and r.get("agent_id") != "p1-m2"]
    rows.append(_fidelity("p1", "m2", stamp="OLD"))
    e = eligible(AGENTS, INSTRUMENTS, rows, STAMPS)
    reasons = {x.agent_id: x.reason for x in e.excluded}
    assert reasons["p1-m2"] == "screening_stale" and reasons["p2-m2"] == "perception_missing"
    assert sum(e.counts().values()) == len(e.excluded) == 5


def test_refusal_order_coverage_then_stale() -> None:
    changed = Stamps({"m1": "S1-new", "m2": "S2"}, STAMPS.instruments, "F")
    rows = [r for r in _all_pass() if r["instrument"] != "pairwise_alive"]
    err = eligible(AGENTS, INSTRUMENTS, rows, changed).refusal()
    assert err is not None and err.code == "screening_coverage_missing"


class _T:
    test, kind, instruments, clips = "t", "pilot", ["godspeed"], ["c1", "c2"]

    def model_ids(self, _cfg: Any) -> list[str]:
        return MODELS

    def effective_session(self, _cfg: Any) -> Any:
        return type("S", (), {"repeats": 2})()


class _P:
    def __init__(self, pid: str) -> None:
        self.id = pid


def test_plan_agents_filter_keeps_trials_identical() -> None:
    cfg = type("C", (), {"seed": 7})()
    godspeed = type("I", (), {"prompt_variants": {"a": "x"}, "self_report": False,
                              "pairwise": False})()
    personas = [_P(p) for p in PERSONAS]
    full = plan_test(_T(), cfg, personas, {"godspeed": godspeed})
    keep = {"p2-m1", "p4-m2"}
    some = plan_test(_T(), cfg, personas, {"godspeed": godspeed}, agents=keep)
    assert [s for s in full.sessions if s.agent_id in keep] == list(some.sessions)
    assert plan_test(_T(), cfg, personas, {"godspeed": godspeed}, agents=None) == full
