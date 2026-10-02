"""Cost estimate, ceiling, pause and resume: the story 1.9 I/O matrix and acceptance criteria.

The Fake rater runs at non-zero prices from ``prices.yaml``; nothing touches the network.
"""

from __future__ import annotations

import asyncio
import json
import math
import shutil
import sqlite3
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from consortium.board import trials as board_trials
from consortium.board.clips import insert_clip
from consortium.board.db import DB_FILE, connect, transaction
from consortium.board.ledger import Spend, committed_usd, set_ceiling
from consortium.board.writer import start_writer
from consortium.cli import app
from consortium.config.load import load_prices, load_study
from consortium.core.cost import actual, estimate, estimate_plan, usd
from consortium.core.errors import ConsortiumError
from consortium.core.render import ClipRef, PracticeExample, TrialRequest, canonical_json
from consortium.engine.dispatch import Budget, dispatch
from consortium.raters.base import RaterResult
from consortium.raters.fake import FakeRater
from consortium.stages import personas as personas_stage
from consortium.stages.init import init_study
from consortium.stages.open import open_test, plan_and_render
from consortium.stages.push import push_test

runner = CliRunner()

TARGETS = ["c_aaaaaaaa", "c_bbbbbbbb"]
PRACTICE = ["c_ppppppaa", "c_ppppppab", "c_ppppppac"]
DURATIONS = {"c_aaaaaaaa": 2.5, "c_bbbbbbbb": 3.0, "c_ppppppaa": 1.5, "c_ppppppab": 2.0,
             "c_ppppppac": 1.0}
GODSPEED = {f"animacy_{i}": 3 for i in range(1, 7)} | {f"likeability_{i}": 3 for i in range(1, 6)}
TRIALS = 64 * (2 + 2)  # 64 Personas x 1 Model x 1 Repeat x (2 godspeed + 2 pairwise)
PRICES = """\
schema_version: 1
models:
  m1:
    input_usd_per_mtok: "1.25"
    output_usd_per_mtok: "5"
    media_tokens_per_s: "263"
    chars_per_token: "4"
"""
FAKE_USAGE = {"input_tokens": 1000, "output_tokens": 100}  # well under every reservation


def _add_clips(study: Path) -> None:
    conn = connect(study)
    try:
        with transaction(conn):
            for i, (clip_id, seconds) in enumerate(DURATIONS.items()):
                info = SimpleNamespace(duration_s=seconds, size_bytes=1000, width=640,
                                       height=480, fps=25.0, loudness_lufs=-23.0)
                insert_clip(conn, clip_id, f"{i + 1:064x}", info)
    finally:
        conn.close()


def _write_test(study: Path, name: str) -> Path:
    doc: dict[str, Any] = {
        "schema_version": 1, "test": name, "kind": "pilot",
        "instruments": ["godspeed", "pairwise_alive"], "clips": TARGETS,
        "practice": [
            {"instrument": "godspeed", "clips": [PRACTICE[0]], "answer": GODSPEED},
            {"instrument": "pairwise_alive", "clips": PRACTICE[1:3], "answer": {"alive": "A"}},
        ],
        "session": {"repeats": 1, "practice_clips": 1},
    }
    src = study.parent / f"{name}.yaml"
    src.write_text(yaml.safe_dump(doc, sort_keys=False))
    return src


@pytest.fixture(scope="module")
def template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    study = init_study(tmp_path_factory.mktemp("cost") / "study")
    (study / "prices.yaml").write_text(PRICES)
    cfg = study / "study.yaml"
    cfg.write_text(cfg.read_text().replace(
        "      input_tokens: 0\n      output_tokens: 0\n",
        f"      input_tokens: {FAKE_USAGE['input_tokens']}\n"
        f"      output_tokens: {FAKE_USAGE['output_tokens']}\n",
    ))
    personas_stage.generate(study)
    _add_clips(study)
    push_test(study, _write_test(study, "pilot1"))
    push_test(study, _write_test(study, "pilot2"))
    return study


@pytest.fixture()
def study(tmp_path: Path, template: Path) -> Path:
    copy = tmp_path / "study"
    shutil.copytree(template, copy)
    return copy


def _cli(*args: str, input: str | None = None):
    return runner.invoke(app, ["open", *args], input=input)


def _q(study: Path, sql: str) -> list[tuple]:
    conn = sqlite3.connect(study / DB_FILE)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def _ledger(study: Path) -> list[tuple]:
    return _q(study, "SELECT trial_id, attempt, model_id, reserved_usd, actual_usd FROM ledger")


def _committed(study: Path) -> Decimal:
    return sum((Decimal(a if a is not None else r) for _, _, _, r, a in _ledger(study)),
               Decimal(0))


def _ceilings(study: Path) -> list[tuple]:
    return _q(study, "SELECT previous_usd, ceiling_usd FROM ceiling_changes ORDER BY rowid")


def _paused(study: Path, test: str = "pilot1") -> str | None:
    return _q(study, f"SELECT paused_reason FROM tests WHERE name = '{test}'")[0][0]


def _states(study: Path, test: str = "pilot1") -> dict[str, int]:
    return dict(_q(study, f"SELECT state, count(*) FROM trials WHERE test = '{test}'"
                          " GROUP BY state"))


def _expected(study: Path, test: str = "pilot1") -> Decimal:
    plan, requests, _ = plan_and_render(study, test)
    return estimate_plan(plan, requests, load_study(study), load_prices(study),
                         DURATIONS, max_retries=2).expected


def _reservations(study: Path, test: str = "pilot1") -> dict[str, Decimal]:
    plan, requests, _ = plan_and_render(study, test)
    cfg, prices = load_study(study), load_prices(study)
    return {t.trial_id: estimate(r, cfg.model_by_id(t.model_id), prices, DURATIONS)
            for t, r in zip(plan.trials, requests, strict=True)}


def _nothing_sent(study: Path) -> None:
    assert _q(study, "SELECT count(*) FROM trials") == [(0,)]
    assert _ledger(study) == []
    assert not (study / "archive").exists()


def _assert_ledger_invariants(study: Path) -> None:
    """One ledger row per recorded attempt; committed spend within the ceiling."""
    attempts = set(_q(study, "SELECT trial_id, attempt FROM attempts"))
    rows = _ledger(study)
    assert len(rows) == len({(t, a) for t, a, *_ in rows}) == len(attempts)
    assert {(t, a) for t, a, *_ in rows} == attempts
    ceilings = _ceilings(study)
    if ceilings:
        assert _committed(study) <= Decimal(ceilings[-1][1])


# --------------------------------------------------------------------------- pure cost


def _request(practice: tuple, clips: tuple) -> TrialRequest:
    return TrialRequest(persona_card="You are calm.", instructions="Rate it.", items=(),
                        response_schema={}, prompt="How alive?", practice=practice,
                        clips=clips)


PRICE = SimpleNamespace(input_usd_per_mtok=Decimal("2"), output_usd_per_mtok=Decimal("8"),
                        media_tokens_per_s=Decimal("100"), chars_per_token=Decimal("3"))
PRICES_NS = SimpleNamespace(models={"m1": PRICE})
MODEL = SimpleNamespace(id="m1", provider="fake", max_output_tokens=500)


def test_estimate_formula() -> None:
    a, b, p = ClipRef("c_a", "1" * 64), ClipRef("c_b", "2" * 64), ClipRef("c_p", "3" * 64)
    request = _request((PracticeExample((p,), {"x": 1}),), (a, b))
    seconds = {"c_a": 2.25, "c_b": 1.5, "c_p": 0.1}
    chars = len(canonical_json(request).decode("utf-8"))
    media = math.ceil(Decimal("3.85") * 100)  # targets + Practice, 385 tokens
    text = math.ceil(Decimal(chars) / 3)
    want = ((media + text) * Decimal(2) + 500 * Decimal(8)) / Decimal(1_000_000)
    got = estimate(request, MODEL, PRICES_NS, seconds)
    assert isinstance(got, Decimal) and got == want
    # Deterministic and offline: the same inputs always give the same figure.
    assert estimate(request, MODEL, PRICES_NS, seconds) == got
    # No Clips: text and output only.
    bare = _request((), ())
    text = math.ceil(Decimal(len(canonical_json(bare).decode("utf-8"))) / 3)
    assert estimate(bare, MODEL, PRICES_NS, {}) == (text * 2 + 500 * 8) / Decimal(10**6)


def test_estimate_free_model_is_zero() -> None:
    free = SimpleNamespace(models={"m1": SimpleNamespace(
        input_usd_per_mtok=Decimal(0), output_usd_per_mtok=Decimal(0),
        media_tokens_per_s=Decimal(300), chars_per_token=Decimal(4))})
    assert estimate(_request((), ()), MODEL, free, {}) == 0


def test_actual_and_missing_usage() -> None:
    assert actual({"input_tokens": 1000, "output_tokens": 10}, MODEL, PRICES_NS) == (
        Decimal(1000 * 2 + 10 * 8) / Decimal(10**6))
    for usage in (None, {}, {"input_tokens": 3}, {"input_tokens": -1, "output_tokens": 0},
                  {"input_tokens": True, "output_tokens": 0}, {"input_tokens": "3",
                                                               "output_tokens": 1}):
        assert actual(usage, MODEL, PRICES_NS) is None


def test_estimate_plan_sums_and_worst_case(study: Path) -> None:
    plan, requests, _ = plan_and_render(study, "pilot1")
    pairs = list(zip(plan.trials, requests, strict=True))
    cfg, prices = load_study(study), load_prices(study)
    est = estimate_plan(plan, (r for _, r in pairs), cfg, prices, DURATIONS, max_retries=2)
    each = [estimate(r, cfg.model_by_id(t.model_id), prices, DURATIONS) for t, r in pairs]
    assert est.trials == TRIALS and est.expected == sum(each, Decimal(0)) > 0
    assert est.worst_case == est.expected * 3
    assert est.providers == ("fake",)
    # Both pairwise orders, Practice clips and every Trial are included.
    assert len({t.trial_id for t, _ in pairs}) == TRIALS


def test_usd_format() -> None:
    assert usd(Decimal("5.00")) == "5"
    assert usd(Decimal("0E-12")) == "0"
    assert usd(Decimal("1.2E-7")) == "0.00000012"
    assert usd(None) == "none"


def test_cost_module_is_pure() -> None:
    src = (Path(__file__).resolve().parents[1] / "src" / "consortium" / "core" / "cost.py")
    text = src.read_text()
    for banned in ("import yaml", "open(", "sqlite3", "consortium.config", "http"):
        assert banned not in text


# --------------------------------------------------------------------------- matrix


def test_confirm_prints_estimate_before_any_rater_call(
    study: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import consortium.stages.open as open_stage

    calls: list[str] = []

    class _SpyRater(FakeRater):
        async def prepare(self, clip):
            calls.append("prepare")
            return await super().prepare(clip)

        async def submit(self, c):
            calls.append("submit")
            return await super().submit(c)

        async def collect(self, h):
            calls.append("collect")
            return await super().collect(h)

    monkeypatch.setattr(open_stage, "FakeRater", _SpyRater)
    announced: list[str] = []

    def confirm(prompt: str) -> bool:
        assert calls == []  # nothing reached a Rater, not even prepare()
        assert any(line.startswith("cost estimate: expected ") for line in announced)
        assert _ledger(study) == [] and _ceilings(study) == []
        return True

    summary = open_test(study, "pilot1", dry_run=False, ceiling="100", confirm=confirm,
                        announce=announced.extend)
    assert summary.states == {"valid": TRIALS}
    assert "prepare" in calls and "submit" in calls
    exp = _expected(study)
    assert summary.estimate.expected == exp
    assert f"cost estimate: expected {usd(exp)} USD, worst case {usd(exp * 3)} USD " \
           "(max_retries 2)" in summary.lines()
    assert "cost covers: 256 Trials; Clips go to: fake" in summary.lines()
    assert _ceilings(study) == [(None, "100")]
    _assert_ledger_invariants(study)


def test_decline_writes_nothing(study: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import consortium.cli as cli_module

    monkeypatch.setattr(cli_module, "_stdin_is_tty", lambda: True)
    result = _cli("pilot1", "--ceiling", "100", "--study", str(study), input="n\n")
    assert result.exit_code == 1
    assert "not_confirmed:" in result.stderr
    assert "cost estimate: expected " in result.stdout
    _nothing_sent(study)
    assert _ceilings(study) == []


def test_over_ceiling(study: Path) -> None:
    exp = _expected(study)
    ceiling = (exp / 2).quantize(Decimal("0.0001"))
    result = _cli("pilot1", "--yes", "--ceiling", str(ceiling), "--study", str(study))
    assert result.exit_code == 1
    assert result.stderr == (
        f"over_ceiling: expected {usd(exp)} > ceiling {usd(ceiling)}; nothing was sent\n")
    assert f"cost estimate: expected {usd(exp)} USD, worst case {usd(exp * 3)} USD " \
           "(max_retries 2)" in result.stdout
    _nothing_sent(study)
    assert _ceilings(study) == []


def test_over_ceiling_counts_committed_spend(
    study: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import consortium.stages.open as open_stage

    monkeypatch.setattr(open_stage, "FakeRater", _NoUsageRater)  # reservations stand
    exp = _expected(study)
    ceiling = exp * Decimal("1.5")
    assert _cli("pilot1", "--yes", "--ceiling", str(ceiling), "--study", str(study)).exit_code == 0
    spent = _committed(study)
    assert spent > 0
    result = _cli("pilot2", "--yes", "--study", str(study))  # the logged ceiling applies
    assert result.exit_code == 1
    assert result.stderr.startswith(
        f"over_ceiling: committed {usd(spent)} + expected {usd(_expected(study, 'pilot2'))} > "
        f"ceiling {usd(ceiling)}")
    assert _q(study, "SELECT count(*) FROM trials WHERE test = 'pilot2'") == [(0,)]


def test_only_worst_case_over_runs(study: Path) -> None:
    exp = _expected(study)
    ceiling = exp * 2  # expected <= ceiling < worst case (x3)
    result = _cli("pilot1", "--yes", "--ceiling", str(ceiling), "--study", str(study))
    assert result.exit_code == 0, result.stderr
    out = result.stdout.splitlines()
    assert f"cost estimate: expected {usd(exp)} USD, worst case {usd(exp * 3)} USD " \
           "(max_retries 2)" in out
    assert out[-2] == f"states: valid {TRIALS}"
    assert out[-1] == f"cost: committed {usd(_committed(study))} USD, ceiling {usd(ceiling)} USD"
    assert _paused(study) is None
    _assert_ledger_invariants(study)


def _to_gemini(study: Path) -> None:
    cfg = study / "study.yaml"
    text = cfg.read_text()
    start, end = text.index("    fake:"), text.index("      invalid_rate:")
    text = text[:start] + text[text.index("\n", end) + 1:]
    cfg.write_text(text.replace("provider: fake ", "provider: gemini ", 1))


def _free_prices(study: Path) -> None:
    (study / "prices.yaml").write_text(PRICES.replace('"1.25"', '"0"').replace('"5"', '"0"'))


def test_no_ceiling_real_model(study: Path) -> None:
    _to_gemini(study)
    result = _cli("pilot1", "--yes", "--study", str(study))
    assert result.exit_code == 1
    assert result.stderr.startswith("ceiling_required:")
    _nothing_sent(study)


def test_no_ceiling_priced_fake_is_required(study: Path) -> None:
    """A Fake Model priced above 0 needs a ceiling too."""
    result = _cli("pilot1", "--yes", "--study", str(study))
    assert result.exit_code == 1
    assert result.stderr.startswith("ceiling_required:")
    _nothing_sent(study)


def test_no_ceiling_fake_only_runs_uncapped(study: Path) -> None:
    _free_prices(study)
    result = _cli("pilot1", "--yes", "--study", str(study))
    assert result.exit_code == 0, result.stderr
    out = result.stdout.splitlines()
    assert "ceiling: none, committed before: 0 USD" in out
    assert out[-1] == f"cost: committed {usd(_committed(study))} USD, ceiling none"
    assert out[-1].endswith("ceiling none")
    # Reserves and records costs anyway: one row per attempt, actual from the Fake usage.
    rows = _ledger(study)
    reserved = _reservations(study)
    assert len(rows) == TRIALS
    want_actual = Decimal(0)
    for trial_id, attempt, model_id, r, a in rows:
        assert (attempt, model_id) == (1, "m1")
        assert Decimal(r) == reserved[trial_id]
        assert Decimal(a) == want_actual
    assert _ceilings(study) == []


@pytest.mark.parametrize("ceiling", ["-1", "0", "abc"])
def test_bad_ceiling_changes_nothing(study: Path, ceiling: str) -> None:
    before = (_ceilings(study), _ledger(study))
    result = _cli("pilot1", "--yes", "--ceiling", ceiling, "--study", str(study))
    assert result.exit_code == 1
    assert result.stderr.startswith("invalid_ceiling:")
    assert (_ceilings(study), _ledger(study)) == before
    _nothing_sent(study)


def test_exact_fit_is_dispatched(study: Path) -> None:
    conn = connect(study)
    try:
        plan, _, _ = plan_and_render(study, "pilot1")
        trials = list(plan.trials)
        board_trials.insert_plan(conn, "pilot1", trials)
        a, b, c = (t.trial_id for t in trials[:3])
        spend = Spend()
        assert board_trials.begin_attempt(conn, a, 1, Decimal("0.3"), Decimal("1"),
                                          spend) == (
            1, board_trials.derive_seed(1, "model", f"{trials[0].session_id}:"
                                                   f"{trials[0].trial_index}:1"))
        # committed 0.3 + 0.7 == ceiling 1: dispatched (<=).
        assert board_trials.begin_attempt(conn, b, 1, Decimal("0.7"), Decimal("1"),
                                          spend) is not None
        assert committed_usd(conn) == spend.committed(conn) == Decimal("1.0")
        # Refused: nothing written, no attempt.
        assert board_trials.begin_attempt(conn, c, 1, Decimal("0.0001"), Decimal("1"),
                                          spend) is None
        assert conn.execute("SELECT attempt FROM trials WHERE trial_id = ?", (c,)).fetchone() \
            == (0,)
        assert conn.execute("SELECT count(*) FROM attempts WHERE trial_id = ?", (c,)).fetchone() \
            == (0,)
        assert conn.execute("SELECT count(*) FROM ledger").fetchone() == (2,)
        # No ceiling: never refused.
        assert board_trials.begin_attempt(conn, c, 1, Decimal("99"), None, spend) is not None
        assert spend.committed(conn) == committed_usd(conn) == Decimal("100.0")
    finally:
        conn.close()


def test_exact_fit_run(study: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Ceiling == expected and no usage (reservations stand): every Trial is dispatched."""
    import consortium.stages.open as open_stage

    monkeypatch.setattr(open_stage, "FakeRater", _NoUsageRater)
    exp = _expected(study)
    result = _cli("pilot1", "--yes", "--ceiling", format(exp, "f"), "--study", str(study))
    assert result.exit_code == 0, result.stderr
    assert _states(study) == {"valid": TRIALS}
    assert _committed(study) == exp
    _assert_ledger_invariants(study)


class _NoUsageRater(FakeRater):
    async def collect(self, handles):
        return [RaterResult(r.raw, {}, r.model_build, r.category)
                for r in await super().collect(handles)]


def test_no_usage_reserved_counts(study: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import consortium.stages.open as open_stage

    monkeypatch.setattr(open_stage, "FakeRater", _NoUsageRater)
    assert _cli("pilot1", "--yes", "--ceiling", "100", "--study", str(study)).exit_code == 0
    rows = _ledger(study)
    assert len(rows) == TRIALS and all(a is None for *_, a in rows)
    assert _committed(study) == _expected(study)


def _engine_pause(study: Path, ceiling: Decimal, concurrency: int) -> str | None:
    plan, requests, _ = plan_and_render(study, "pilot1")
    pairs = list(zip(plan.trials, requests, strict=True))
    cfg, prices = load_study(study), load_prices(study)
    budget = Budget(test="pilot1", models={m.id: m for m in cfg.models}, prices=prices,
                    clip_seconds=DURATIONS, ceiling=ceiling)

    async def run() -> str | None:
        async with start_writer(study) as writer:
            await writer.do("insert_plan",
                            lambda c: board_trials.insert_plan(c, "pilot1", plan.trials))
            await writer.do("set_ceiling", lambda c: set_ceiling(c, ceiling, "pilot1", "run"))
            return await dispatch(study, pairs, {"m1": _NoUsageRater()}, writer=writer,
                                  seed=1, concurrency=concurrency, budget=budget,
                                  max_retries=2)

    return asyncio.run(run())


@pytest.mark.parametrize("concurrency", [1, 4])
def test_mid_run_cap_engine(study: Path, concurrency: int) -> None:
    """The next reservation crosses the ceiling: in-flight attempts are collected, then pause."""
    ceiling = _expected(study) / 3
    assert _engine_pause(study, ceiling, concurrency) == "ceiling"
    states = _states(study)
    assert set(states) == {"valid", "planned"}  # every begun attempt was collected
    assert 0 < states["valid"] < TRIALS
    assert _paused(study) == "ceiling"
    committed = _committed(study)
    assert committed <= ceiling
    # The refused reservation would have crossed it; no untried Trial was started.
    reserved = _reservations(study)
    started = {t for t, *_ in _ledger(study)}
    assert any(committed + reserved[t] > ceiling for t in reserved if t not in started)
    assert _q(study, "SELECT count(*) FROM trials WHERE state = 'planned' AND attempt > 0") \
        == [(0,)]
    _assert_ledger_invariants(study)


def _kill_then(study: Path, ceiling: Decimal) -> None:
    """A Run at ``ceiling`` killed part way: some attempts reserved but never answered."""
    state = {"n": 0}

    class Killed(Exception):
        pass

    def spy(op: str, key: tuple) -> None:
        if state["n"] >= 40:
            raise Killed(op)
        if op == "mark_sent":  # the 40th Trial is left sent without a handle: re-sent later
            state["n"] += 1

    with pytest.raises(ConsortiumError) as info:
        open_test(study, "pilot1", dry_run=False, yes=True, ceiling=format(ceiling, "f"),
                  writer_spy=spy)
    assert info.value.code == "run_failed"


def test_mid_run_cap_resume_same_then_higher(study: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import consortium.stages.open as open_stage

    monkeypatch.setattr(open_stage, "FakeRater", _NoUsageRater)
    cfg = study / "study.yaml"  # one call at a time: a deterministic dispatch order
    cfg.write_text(cfg.read_text().replace("concurrency: 4", "concurrency: 1"))
    exp = _expected(study)
    _kill_then(study, exp)  # exact budget: killed attempts' reservations are now spent
    killed = _states(study)
    assert killed.get("planned", 0) + killed.get("sent", 0) > 0
    requests_before = (study / "archive" / "requests.jsonl").read_bytes()

    # Resume, same cap: the re-sends cannot all be afforded; pauses before crossing it.
    result = _cli("pilot1", "--yes", "--resume", "--study", str(study))
    assert result.exit_code == 1
    assert result.stderr.splitlines()[-1].startswith("ceiling_reached:")
    assert "paused: ceiling" in result.stdout.splitlines()
    assert _paused(study) == "ceiling"
    assert _committed(study) <= exp
    assert "sent" not in _states(study) or all(
        h is None for (h,) in _q(study, "SELECT a.handle FROM attempts a JOIN trials t"
                                        " ON t.trial_id = a.trial_id AND t.attempt = a.attempt"
                                        " WHERE t.state = 'sent'"))
    paused_states = _states(study)
    assert paused_states.get("valid", 0) < TRIALS
    _assert_ledger_invariants(study)
    # A paused Run resumed again at the same cap pauses again before any send.
    lines = (study / "archive" / "requests.jsonl").read_bytes()
    result = _cli("pilot1", "--yes", "--resume", "--study", str(study))
    assert result.exit_code == 1 and result.stderr.splitlines()[-1].startswith("ceiling_reached:")
    assert (study / "archive" / "requests.jsonl").read_bytes() == lines
    assert lines.startswith(requests_before)

    # Resume, higher: logged, remaining estimate shown, continues with no re-send.
    higher = exp * 2
    result = _cli("pilot1", "--yes", "--resume", "--ceiling", format(higher, "f"),
                  "--study", str(study))
    assert result.exit_code == 0, result.stderr
    assert _states(study) == {"valid": TRIALS}
    assert _paused(study) is None
    assert _ceilings(study) == [(None, format(exp, "f")), (format(exp, "f"), format(higher, "f"))]
    remaining = [x for x in result.stdout.splitlines() if x.startswith("cost covers: ")]
    assert remaining and remaining[0] != f"cost covers: {TRIALS} Trials; Clips go to: fake"
    # No Trial valid before the resume was re-sent: one request line per attempt key.
    keys = [(r["trial_id"], r["attempt"]) for r in map(
        json.loads, (study / "archive" / "requests.jsonl").read_bytes().splitlines())]
    assert len(keys) == len(set(keys))
    _assert_ledger_invariants(study)
    assert _committed(study) <= higher


def test_dry_run_and_run_print_identical_estimates(study: Path) -> None:
    dry = _cli("pilot1", "--dry-run", "--ceiling", "100", "--study", str(study))
    assert dry.exit_code == 0, dry.stderr
    run = _cli("pilot1", "--yes", "--ceiling", "100", "--study", str(study))
    assert run.exit_code == 0, run.stderr

    def cost(out: str) -> list[str]:
        return [x for x in out.splitlines() if x.startswith(("cost estimate:", "cost covers:"))]

    assert cost(dry.stdout) == cost(run.stdout) and len(cost(dry.stdout)) == 2


def test_dry_run_writes_no_ceiling(study: Path) -> None:
    result = _cli("pilot1", "--dry-run", "--ceiling", "3", "--study", str(study))
    assert result.exit_code == 0, result.stderr
    assert "ceiling: 3 USD, committed before: 0 USD" in result.stdout.splitlines()
    assert _ceilings(study) == []


def test_missing_price_is_config_invalid(study: Path) -> None:
    (study / "prices.yaml").write_text("schema_version: 1\nmodels: {}\n")
    result = _cli("pilot1", "--yes", "--study", str(study))
    assert result.exit_code == 1
    assert result.stderr.startswith("config_invalid: models.m1:")
    _nothing_sent(study)


def test_bad_chars_per_token(study: Path) -> None:
    (study / "prices.yaml").write_text(PRICES.replace('chars_per_token: "4"',
                                                      'chars_per_token: "0"'))
    with pytest.raises(ConsortiumError) as info:
        load_prices(study)
    assert info.value.code == "config_invalid"
    assert "models.m1.chars_per_token" in info.value.message


def test_fake_usage_from_settings() -> None:
    rater = FakeRater(input_tokens=7, output_tokens=3)
    request = _request((), ())

    async def run() -> RaterResult:
        from consortium.raters.base import RaterCall

        (handle,) = await rater.submit([RaterCall("t", 1, 5, request, ())])
        (result,) = await rater.collect([handle])
        return result

    assert asyncio.run(run()).usage == {"input_tokens": 7, "output_tokens": 3}


# --------------------------------------------------------------------------- review follow-ups


def test_overshoot_from_actual_cost_pauses_at_once(study: Path) -> None:
    """Fake usage above the reservation: committed crosses the ceiling, the Run pauses."""
    cfg = study / "study.yaml"
    cfg.write_text(cfg.read_text().replace("input_tokens: 1000", "input_tokens: 10000000"))
    exp = _expected(study)
    ceiling = exp * 2
    result = _cli("pilot1", "--yes", "--ceiling", format(ceiling, "f"), "--study", str(study))
    assert result.exit_code == 1
    committed = _committed(study)
    assert committed > ceiling  # an actual cost, never a reservation, crossed it
    err = result.stderr.splitlines()
    assert (f"ceiling_overshoot: committed {usd(committed)} > ceiling {usd(ceiling)} "
            "(actual cost exceeded the estimate)") in err
    assert err[-1].startswith("ceiling_reached:")
    assert "paused: ceiling" in result.stdout.splitlines()
    assert _paused(study) == "ceiling"
    states = _states(study)
    assert set(states) == {"valid", "planned"}  # in-flight attempts were collected
    assert states["valid"] <= 4  # at most the attempts in flight (concurrency 4)
    _assert_ledger_invariants_rows(study)


def _assert_ledger_invariants_rows(study: Path) -> None:
    attempts = set(_q(study, "SELECT trial_id, attempt FROM attempts"))
    assert {(t, a) for t, a, *_ in _ledger(study)} == attempts


def _edit_prices(study: Path):
    def confirm(prompt: str) -> bool:
        (study / "prices.yaml").write_text(PRICES.replace('"1.25"', '"2"'))
        return True
    return confirm


def test_prices_edited_inside_confirm_run(study: Path) -> None:
    with pytest.raises(ConsortiumError) as info:
        open_test(study, "pilot1", dry_run=False, ceiling="100", confirm=_edit_prices(study))
    assert info.value.code == "test_changed"
    _nothing_sent(study)
    assert _ceilings(study) == []


def test_prices_edited_inside_confirm_resume(study: Path) -> None:
    _kill_then(study, Decimal(100))
    before = (_ledger(study), _ceilings(study), _states(study))
    with pytest.raises(ConsortiumError) as info:
        open_test(study, "pilot1", dry_run=False, resume=True, ceiling="200",
                  confirm=_edit_prices(study))
    assert info.value.code == "test_changed"
    assert (_ledger(study), _ceilings(study), _states(study)) == before


def test_committed_added_inside_confirm_is_over_ceiling(study: Path) -> None:
    exp = _expected(study)

    def spend_then_yes(prompt: str) -> bool:
        conn = sqlite3.connect(study / DB_FILE)  # foreign keys off: a stray ledger row
        conn.execute("INSERT INTO ledger VALUES ('x', 1, 'm1', ?, NULL)", (format(exp, "f"),))
        conn.commit()
        conn.close()
        return True

    with pytest.raises(ConsortiumError) as info:
        open_test(study, "pilot1", dry_run=False, ceiling=format(exp * Decimal("1.5"), "f"),
                  confirm=spend_then_yes)
    assert info.value.code == "over_ceiling"
    assert _q(study, "SELECT count(*) FROM trials") == [(0,)]
    assert not (study / "archive").exists()
    assert _ceilings(study) == []


def test_resume_gemini_without_ceiling_is_required(study: Path) -> None:
    _free_prices(study)
    with pytest.raises(ConsortiumError):
        state = {"n": 0}

        def spy(op: str, key: tuple) -> None:
            if state["n"] >= 40:
                raise RuntimeError("killed")
            state["n"] += op == "set_state"

        open_test(study, "pilot1", dry_run=False, yes=True, writer_spy=spy)
    _to_gemini(study)
    result = _cli("pilot1", "--yes", "--resume", "--study", str(study))
    assert result.exit_code == 1
    assert result.stderr.startswith("ceiling_required:")  # before provider_unavailable


def test_resume_on_downgraded_board_ledgers_collected_attempts(study: Path) -> None:
    state = {"n": 0}

    def spy(op: str, key: tuple) -> None:
        if state["n"] >= 40:
            raise RuntimeError("killed")
        state["n"] += op == "set_handle"

    with pytest.raises(ConsortiumError):
        open_test(study, "pilot1", dry_run=False, yes=True, ceiling="100", writer_spy=spy)
    handled = set(_q(study, "SELECT a.trial_id, a.attempt FROM attempts a JOIN trials t"
                            " ON t.trial_id = a.trial_id AND t.attempt = a.attempt"
                            " WHERE t.state = 'sent' AND a.handle IS NOT NULL"))
    assert handled
    raw = sqlite3.connect(study / DB_FILE, isolation_level=None)
    raw.execute("DROP TABLE ledger")
    raw.execute("DROP TABLE ceiling_changes")
    raw.execute("ALTER TABLE tests DROP COLUMN paused_reason")
    for column in ("valid", "invalid_reason", "answer_json"):  # m5 (story 1.10)
        raw.execute(f"ALTER TABLE attempts DROP COLUMN {column}")
    raw.execute("PRAGMA user_version = 3")
    raw.close()
    summary = open_test(study, "pilot1", dry_run=False, yes=True, resume=True, ceiling="100")
    assert summary.states == {"valid": TRIALS}
    rows = {(t, a): (r, act) for t, a, _, r, act in _ledger(study)}
    reserved = _reservations(study)
    want_actual = (1000 * Decimal("1.25") + 100 * 5) / Decimal(10**6)
    for key in handled:  # collected pre-ledger attempts got a row and count
        r, act = rows[key]
        assert Decimal(r) == reserved[key[0]] and Decimal(act) == want_actual
    assert summary.committed == _committed(study)


def test_dry_run_says_would_refuse(study: Path) -> None:
    result = _cli("pilot1", "--dry-run", "--study", str(study))
    assert result.exit_code == 0, result.stderr
    out = result.stdout.splitlines()
    assert out[-1] == "would refuse: ceiling_required"
    result = _cli("pilot1", "--dry-run", "--ceiling", "0.0001", "--study", str(study))
    assert result.exit_code == 0
    assert result.stdout.splitlines()[-1] == "would refuse: over_ceiling"
    result = _cli("pilot1", "--dry-run", "--ceiling", "100", "--study", str(study))
    assert not any(x.startswith("would refuse") for x in result.stdout.splitlines())


def test_ceiling_below_committed_is_over_ceiling(
    study: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import consortium.stages.open as open_stage

    monkeypatch.setattr(open_stage, "FakeRater", _NoUsageRater)
    assert _cli("pilot1", "--yes", "--ceiling", "100", "--study", str(study)).exit_code == 0
    committed = _committed(study)
    result = _cli("pilot2", "--yes", "--ceiling", "0.0001", "--study", str(study))
    assert result.exit_code == 1
    assert result.stderr == (
        f"over_ceiling: committed {usd(committed)} > ceiling 0.0001; nothing was sent\n")
    assert _ceilings(study) == [(None, "100")]


def test_paused_run_dry_run_and_reopen_hint(study: Path) -> None:
    ceiling = _expected(study) / 3
    _engine_pause(study, ceiling, 1)
    dry = _cli("pilot1", "--dry-run", "--study", str(study))
    assert dry.exit_code == 0
    assert dry.stdout.splitlines()[-1] == "paused: ceiling"
    result = _cli("pilot1", "--yes", "--study", str(study))
    assert result.exit_code == 1
    assert result.stderr.startswith("test_already_open:")
    assert "it is paused at the ceiling, use --resume --ceiling <higher>" in result.stderr


def test_prompt_names_the_ceiling(study: Path) -> None:
    prompts: list[str] = []
    with pytest.raises(ConsortiumError):
        open_test(study, "pilot1", dry_run=False, ceiling="100",
                  confirm=lambda p: prompts.append(p) or False)
    assert prompts[0].endswith(f"Run {TRIALS} Trials on fake, ceiling 100 USD (study-wide)?")


def test_corrupt_ledger_amount_is_board_unreadable(study: Path) -> None:
    conn = connect(study)
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("INSERT INTO ledger VALUES ('x', 1, 'm1', 'oops', NULL)")
    with pytest.raises(ConsortiumError) as info:
        committed_usd(conn)
    conn.close()
    assert info.value.code == "board_unreadable"


def test_fake_settings_on_other_provider_is_config_invalid(study: Path) -> None:
    cfg = study / "study.yaml"
    cfg.write_text(cfg.read_text().replace("provider: fake ", "provider: gemini ", 1))
    with pytest.raises(ConsortiumError) as info:
        load_study(study)
    assert info.value.code == "config_invalid"
    assert "fake" in info.value.message
