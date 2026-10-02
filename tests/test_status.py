"""Story 1.11: ``consortium status``, read-only and lease-free."""

from __future__ import annotations

import copy
import json
import shutil
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from consortium.archive.jsonl import REQUESTS_FILE, read_requests, read_responses
from consortium.board import trials as board_trials
from consortium.board.clips import insert_clip
from consortium.board.db import DB_FILE, MIGRATIONS, connect, transaction
from consortium.board.lease import acquire_lease
from consortium.board.ledger import set_ceiling
from consortium.board.tests import set_paused
from consortium.cli import app
from consortium.config.load import load_instruments, load_study
from consortium.core.errors import ConsortiumError
from consortium.core.validate import validate_response
from consortium.stages import personas as personas_stage
from consortium.stages.init import init_study
from consortium.stages.open import open_test
from consortium.stages.push import push_test
from consortium.stages.status import COLUMNS, format_table, status

runner = CliRunner()

TARGETS = ["c_aaaaaaaa", "c_bbbbbbbb"]
PRACTICE = ["c_ppppppaa", "c_ppppppab", "c_ppppppac"]
GODSPEED = {f"animacy_{i}": 3 for i in range(1, 7)} | {f"likeability_{i}": 3 for i in range(1, 6)}
STATES = ("planned", "sent", "valid", "invalid", "refused", "failed")
PER_AGENT = 2 + 2  # 1 Repeat x (2 godspeed + 2 pairwise)


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


def _write_test(study: Path, name: str, models: list[str]) -> Path:
    doc = {
        "schema_version": 1, "test": name, "kind": "pilot",
        "instruments": ["godspeed", "pairwise_alive"], "clips": TARGETS, "models": models,
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
    """A Study with two Fake Models (m1 answers invalidly half the time) and two Tests."""
    study = init_study(tmp_path_factory.mktemp("status") / "study")
    cfg = yaml.safe_load((study / "study.yaml").read_text())
    m1 = cfg["models"][0]
    m1["fake"]["invalid_rate"] = 0.5
    m2 = copy.deepcopy(m1)
    m2.update(id="m2", model="fake-2")
    m2["fake"]["invalid_rate"] = 0
    cfg["models"].append(m2)
    (study / "study.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False))
    prices = yaml.safe_load((study / "prices.yaml").read_text())
    prices["models"]["m2"] = dict(prices["models"]["m1"])
    (study / "prices.yaml").write_text(yaml.safe_dump(prices, sort_keys=False))
    personas_stage.generate(study)
    _add_clips(study, TARGETS + PRACTICE)
    push_test(study, _write_test(study, "pilot1", ["m1", "m2"]))
    push_test(study, _write_test(study, "pilot2", ["m2"]))
    return study


@pytest.fixture(scope="module")
def ran(tmp_path_factory: pytest.TempPathFactory, template: Path) -> Path:
    """pilot1 run to completion (pilot2 registered, never opened); read-only for tests."""
    study = tmp_path_factory.mktemp("ran") / "study"
    shutil.copytree(template, study)
    open_test(study, "pilot1", dry_run=False, yes=True)
    return study


@pytest.fixture()
def study(tmp_path: Path, ran: Path) -> Path:
    copy_ = tmp_path / "study"
    shutil.copytree(ran, copy_)
    return copy_


def _cli(study: Path, *args: str):
    return runner.invoke(app, ["status", *args, "--study", str(study)])


def _trials(study: Path, test: str = "pilot1") -> list[dict]:
    conn = connect(study)
    try:
        return board_trials.load_trials(conn, test)
    finally:
        conn.close()


def _by_key(rows: list[dict]) -> dict[tuple[str, str, str], dict]:
    return {(r["test"], r["model"], r["agent"]): r for r in rows}


# --------------------------------------------------------------------------- completed Run


def test_completed_run_table(ran: Path) -> None:
    result = _cli(ran)
    assert result.exit_code == 0, result.stderr
    lines = result.stdout.splitlines()
    assert lines[0].split() == list(COLUMNS)
    assert lines[-1] == "committed 0 / ceiling none   state: ok"
    body = [line.split() for line in lines[1:-1]]
    assert len(body) == 1 + 2 + 2 * 64 + 1  # pilot1 total, 2 Model totals, 128 Agents; pilot2
    assert body[-1] == ["pilot2", "*", "*", *["0"] * 8, "0"]  # registered, never opened
    assert body[0][:3] == ["pilot1", "*", "*"]
    assert body[1][:3] == ["pilot1", "m1", "*"]
    assert body[2][:3] == ["pilot1", "m1", "p1-m1"]
    assert body[3][:3] == ["pilot1", "m1", "p2-m1"]  # natural order: p2 before p10
    assert body[2 + 64][:3] == ["pilot1", "m2", "*"]


def test_rows_match_board_and_roll_up(ran: Path) -> None:
    report = status(ran)
    rows = _by_key(report.rows)
    assert rows.pop(("pilot2", "*", "*"))["trials"] == 0
    trials = _trials(ran)
    assert set(rows) == (
        {("pilot1", "*", "*"), ("pilot1", "m1", "*"), ("pilot1", "m2", "*")}
        | {("pilot1", t["model_id"], t["agent_id"]) for t in trials}
    )
    for (test, model, agent), row in rows.items():
        assert list(row) == list(COLUMNS)
        mine = [t for t in trials
                if model in ("*", t["model_id"]) and agent in ("*", t["agent_id"])]
        states = Counter(t["state"] for t in mine)
        assert {s: row[s] for s in STATES} == {s: states[s] for s in STATES}
        assert row["trials"] == len(mine)
        assert sum(row[s] for s in STATES) == len(mine)  # every Trial in exactly one state
        assert row["paused"] is None
        assert row["retried"] == sum(max(t["attempt"] - 1, 0) for t in mine)
        rate = None if row["valid"] + row["invalid"] == 0 else (
            row["invalid"] / (row["valid"] + row["invalid"]))
        assert row["invalid_rate"] == rate
        assert row["cost_usd"] == "0"
        children = [r for k, r in rows.items() if k[0] == test and (
            (model == "*" and k[1] != "*" and k[2] == "*")
            or (model != "*" and agent == "*" and k[1] == model and k[2] != "*"))]
        if agent == "*":
            assert children
            for col in ("trials", *STATES, "retried"):
                assert row[col] == sum(c[col] for c in children), (test, model, col)
    total = rows[("pilot1", "*", "*")]
    assert total["invalid"] > 0 and total["retried"] > 0
    assert rows[("pilot1", "m2", "*")]["invalid"] == 0
    assert rows[("pilot1", "m2", "*")]["retried"] == 0
    assert rows[("pilot1", "m2", "*")]["invalid_rate"] == 0.0


def test_counts_reconcile_with_archive(ran: Path) -> None:
    """Per Agent, the Archive alone gives the same ``retried``, valid and invalid counts."""
    cfg = load_study(ran)
    instruments = load_instruments(ran, cfg)
    instrument_of = {t["trial_id"]: t["instrument"] for t in _trials(ran)}
    requests = read_requests(ran)
    responses = read_responses(ran)
    expected: dict[str, Counter] = defaultdict(Counter)
    tried: dict[str, set[str]] = defaultdict(set)
    lines = (ran / REQUESTS_FILE).read_bytes().splitlines()
    assert len(lines) == len(requests)  # no repeated request key after a completed Run
    for line in lines:
        trial_id = json.loads(line)["trial_id"]
        agent = trial_id.split("/")[1]
        expected[agent]["requests"] += 1  # raw request lines
        tried[agent].add(trial_id)
    last: dict[str, tuple[int, dict[str, Any]]] = {}
    for (trial_id, attempt), record in responses.items():
        if trial_id not in last or attempt > last[trial_id][0]:
            last[trial_id] = (attempt, record)
    for trial_id, (_attempt, record) in last.items():
        agent = trial_id.split("/")[1]
        try:
            validate_response(record["raw"], instruments[instrument_of[trial_id]])
            expected[agent]["valid"] += 1
        except ConsortiumError as err:
            assert err.code == "invalid_response"
            expected[agent]["invalid"] += 1
    rows = [r for r in status(ran).rows if r["agent"] != "*"]
    assert len(rows) == len(expected) == 128
    for row in rows:
        exp = expected[row["agent"]]
        assert row["retried"] == exp["requests"] - len(tried[row["agent"]])
        assert (row["valid"], row["invalid"]) == (exp["valid"], exp["invalid"])
    assert sum(r["invalid"] for r in rows) > 0


def test_json(ran: Path) -> None:
    result = _cli(ran, "--json")
    assert result.exit_code == 0, result.stderr
    doc = json.loads(result.stdout)
    assert set(doc) == {"schema_version", "test", "rows", "committed", "ceiling", "state",
                        "paused"}
    assert (doc["schema_version"], doc["test"]) == (1, None)
    assert (doc["committed"], doc["ceiling"], doc["state"], doc["paused"]) == (
        "0", None, "ok", [])
    assert doc["rows"] == status(ran).rows
    one = json.loads(_cli(ran, "pilot1", "--json").stdout)
    assert one["test"] == "pilot1" and {r["test"] for r in one["rows"]} == {"pilot1"}
    assert all(list(r) == list(COLUMNS) for r in doc["rows"])


def test_board_unchanged(ran: Path) -> None:
    db = ran / DB_FILE
    before = (db.read_bytes(), db.stat().st_mtime_ns)
    files = sorted(p.name for p in ran.iterdir())
    assert _cli(ran).exit_code == 0
    assert _cli(ran, "pilot1", "--json").exit_code == 0
    assert (db.read_bytes(), db.stat().st_mtime_ns) == before
    assert sorted(p.name for p in ran.iterdir()) == files


# --------------------------------------------------------------------------- one Test, unknown


def test_one_test(study: Path) -> None:
    set_ceiling_and_spend(study)
    result = _cli(study, "pilot2")
    assert result.exit_code == 0, result.stderr
    lines = result.stdout.splitlines()
    assert len(lines) == 3  # pilot2 has no Trials: header, its zero Test total, footer
    assert lines[1].split() == ["pilot2", "*", "*", *["0"] * 8, "0"]
    assert lines[-1] == "committed 1.5 / ceiling 5   state: ok"  # footer is Study-wide
    rows = status(study, "pilot2").rows
    assert len(rows) == 1 and rows[0]["trials"] == 0 and rows[0]["invalid_rate"] is None
    rows = status(study, "pilot1").rows
    assert rows and len(rows) == 1 + 2 + 128 and {r["test"] for r in rows} == {"pilot1"}


def test_unknown_test(ran: Path) -> None:
    result = _cli(ran, "nope")
    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr.startswith("unknown_test: nope")


# --------------------------------------------------------------------------- cost and pause


def set_ceiling_and_spend(study: Path) -> tuple[str, str]:
    """Ceiling 5; two attempts of one m1 Agent cost 1 (actual) + 0.5 (reserved only)."""
    trials = [t for t in _trials(study) if t["model_id"] == "m1"]
    agent = trials[0]["agent_id"]
    a, b = [t["trial_id"] for t in trials if t["agent_id"] == agent][:2]
    conn = connect(study)
    try:
        set_ceiling(conn, Decimal("5.00"), "pilot1", "run")
        with transaction(conn):
            conn.execute("UPDATE ledger SET actual_usd = '1.00' WHERE trial_id = ? AND attempt = 1",
                         (a,))
            conn.execute("UPDATE ledger SET reserved_usd = '0.50', actual_usd = NULL"
                         " WHERE trial_id = ? AND attempt = 1", (b,))
    finally:
        conn.close()
    return agent, "m1"


def test_cost_and_pause(study: Path) -> None:
    agent, model = set_ceiling_and_spend(study)
    conn = connect(study)
    try:
        set_paused(conn, "pilot2", "ceiling")
    finally:
        conn.close()
    report = status(study)
    rows = _by_key(report.rows)
    assert rows[("pilot2", "*", "*")]["paused"] == "ceiling"
    assert rows[("pilot1", "*", "*")]["paused"] is None
    assert rows[("pilot1", model, agent)]["cost_usd"] == "1.5"
    assert rows[("pilot1", model, "*")]["cost_usd"] == "1.5"
    assert rows[("pilot1", "*", "*")]["cost_usd"] == "1.5"
    assert rows[("pilot1", "m2", "*")]["cost_usd"] == "0"
    assert report.footer() == "committed 1.5 / ceiling 5   state: paused pilot2 (ceiling)"
    doc = json.loads(_cli(study, "--json").stdout)
    assert (doc["committed"], doc["ceiling"], doc["state"]) == ("1.5", "5", "paused")
    assert doc["paused"] == [{"test": "pilot2", "reason": "ceiling"}]
    # The footer is Study-wide, also for a Test that is not paused.
    assert _cli(study, "pilot1").stdout.splitlines()[-1].endswith("state: paused pilot2 (ceiling)")


def test_multiple_paused_tests(study: Path) -> None:
    conn = connect(study)
    try:
        set_paused(conn, "pilot2", "ceiling")
        set_paused(conn, "pilot1", "ceiling")
    finally:
        conn.close()
    result = _cli(study)
    assert result.exit_code == 0, result.stderr
    lines = result.stdout.splitlines()
    assert lines[-1] == (
        "committed 0 / ceiling none   state: paused pilot1 (ceiling), pilot2 (ceiling)")
    totals = [line.split() for line in lines[1:-1] if line.split()[1:3] == ["*", "*"]]
    assert [(t[0], t[-1]) for t in totals] == [("pilot1", "ceiling"), ("pilot2", "ceiling")]
    doc = json.loads(_cli(study, "--json").stdout)
    assert doc["state"] == "paused"
    assert doc["paused"] == [{"test": "pilot1", "reason": "ceiling"},
                             {"test": "pilot2", "reason": "ceiling"}]
    paused_rows = {(r["test"], r["model"], r["agent"]): r["paused"] for r in doc["rows"]
                   if r["paused"] is not None}
    assert paused_rows == {("pilot1", "*", "*"): "ceiling", ("pilot2", "*", "*"): "ceiling"}


# --------------------------------------------------------------------------- mid-run


def test_mid_run_while_writer_holds_lease(tmp_path: Path, template: Path) -> None:
    study = tmp_path / "study"
    shutil.copytree(template, study)
    seen = {"n": 0}

    def spy(op: str, key: tuple) -> None:
        if op == "append_request":
            seen["n"] += 1
            if seen["n"] > 40:
                raise Killed(op)

    with pytest.raises(ConsortiumError) as info:
        open_test(study, "pilot1", dry_run=False, yes=True, writer_spy=spy)
    assert info.value.code == "run_failed"

    with acquire_lease(study):
        writer = connect(study)
        try:
            writer.execute("BEGIN IMMEDIATE")
            writer.execute("UPDATE tests SET paused_reason = 'ceiling' WHERE name = 'pilot2'")
            result = _cli(study, "--json")
            # A dispatcher would refuse with study_busy; status does not.
            with pytest.raises(ConsortiumError) as busy, acquire_lease(study):
                pass
            assert busy.value.code == "study_busy"
            writer.execute("ROLLBACK")
        finally:
            writer.close()
    assert result.exit_code == 0, result.stderr
    doc = json.loads(result.stdout)
    assert (doc["state"], doc["paused"]) == ("ok", [])  # the uncommitted write is not seen
    total = _by_key(doc["rows"])[("pilot1", "*", "*")]
    trials = _trials(study)
    states = Counter(t["state"] for t in trials)
    assert total["sent"] > 0 and total["planned"] > 0
    assert {s: total[s] for s in STATES} == {s: states[s] for s in STATES}


# --------------------------------------------------------------------------- no board, version


def test_no_board_yet(tmp_path: Path) -> None:
    study = init_study(tmp_path / "fresh")
    assert not (study / DB_FILE).exists()
    result = _cli(study)
    assert result.exit_code == 0, result.stderr
    assert result.stdout.splitlines() == [
        "  ".join(COLUMNS), "committed 0 / ceiling none   state: ok",
    ]  # no board.db: no registered Test, so no rows
    assert not (study / DB_FILE).exists()
    doc = json.loads(_cli(study, "--json").stdout)
    assert doc == {"schema_version": 1, "test": None, "rows": [], "committed": "0",
                   "ceiling": None, "state": "ok", "paused": []}
    unknown = _cli(study, "pilot1")
    assert unknown.exit_code == 1 and unknown.stdout == ""
    assert unknown.stderr.startswith("unknown_test: pilot1")


@pytest.mark.parametrize("delta", [1, -1])
def test_board_version_mismatch(study: Path, delta: int) -> None:
    conn = connect(study)
    try:
        conn.execute(f"PRAGMA user_version = {len(MIGRATIONS) + delta}")
    finally:
        conn.close()
    result = _cli(study)
    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr.startswith("board_version_mismatch:")


def test_format_table_exact_text() -> None:
    def row(agent: str, valid: int, invalid: int, rate: float | None, paused: str | None):
        return {"test": "t", "model": "m1", "agent": agent, "trials": valid + invalid,
                "planned": 0, "sent": 0, "valid": valid, "invalid": invalid, "refused": 0,
                "failed": 0, "retried": 12, "invalid_rate": rate, "cost_usd": "1.5",
                "paused": paused}

    text = format_table(
        [row("*", 3, 1, 0.25, "ceiling"), row("p10-m1", 0, 0, None, None)], "footer line"
    )
    assert text.splitlines() == [
        "test  model  agent   trials  planned  sent  valid  invalid  refused  failed"
        "  retried  invalid_rate  cost_usd  paused",
        "t     m1     *            4        0     0      3        1        0       0"
        "       12        0.2500       1.5  ceiling",
        "t     m1     p10-m1       0        0     0      0        0        0       0"
        "       12                     1.5",
        "footer line",
    ]
