"""Story 1.12: ``consortium export``, a tidy CSV with Conditions joined at export only."""

from __future__ import annotations

import csv
import io
import json
import re
import shutil
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from typer.testing import CliRunner

from consortium.board import blinding
from consortium.board import trials as board_trials
from consortium.board.clips import insert_clip
from consortium.board.db import DB_FILE, connect, transaction
from consortium.board.lease import acquire_lease
from consortium.cli import app
from consortium.config.load import load_instruments, load_personas, load_study
from consortium.core.errors import ConsortiumError
from consortium.core.personas import TRAITS
from consortium.stages import export as export_stage
from consortium.stages import personas as personas_stage
from consortium.stages.export import EXPORT_SCHEMA_VERSION, export_test
from consortium.stages.init import init_study
from consortium.stages.open import open_test
from consortium.stages.push import push_test

runner = CliRunner()

TARGETS = ["c_aaaaaaaa", "c_bbbbbbbb"]
PRACTICE = ["c_ppppppaa", "c_ppppppab", "c_ppppppac"]
GODSPEED = {f"animacy_{i}": 3 for i in range(1, 7)} | {f"likeability_{i}": 3 for i in range(1, 6)}
CONDITIONS = {
    "c_aaaaaaaa": {"embodiment": "physical", "speed": "fastpace"},
    "c_bbbbbbbb": {"embodiment": "virtualbody", "speed": "slowpace"},
    # Practice clips carry Conditions too; they must still never be exported.
    "c_ppppppaa": {"embodiment": "practiceonly"},
}
PERSONA_COLUMNS = [
    *(f"persona_{t}" for t in TRAITS),
    "persona_nars", "persona_age_band", "persona_gender", "persona_cultural_region",
    "persona_robot_experience",
]
TAIL = [
    "instrument", "item", "response", "position", "repeat", "seed", "prompt_variant", "status",
    "excluded", "exclusion_reason", "test_kind", "protocol_lock", "timestamp",
]
HEAD = [
    "schema_version", "agent_id", "session_id", "trial_index", "clip_id", "pair_id",
    "clip_id_a", "clip_id_b", *PERSONA_COLUMNS, "model",
]


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


def _write_test(study: Path, name: str, instruments: list[str]) -> Path:
    practice = []
    if "godspeed" in instruments:
        practice.append({"instrument": "godspeed", "clips": [PRACTICE[0]], "answer": GODSPEED})
    if "pairwise_alive" in instruments:
        practice.append(
            {"instrument": "pairwise_alive", "clips": PRACTICE[1:3], "answer": {"alive": "A"}}
        )
    doc = {
        "schema_version": 1, "test": name, "kind": "pilot",
        "instruments": instruments, "clips": TARGETS, "models": ["m1"],
        "practice": practice,
        "session": {"repeats": 1, "practice_clips": 1},
    }
    src = study.parent / f"{name}.yaml"
    src.write_text(yaml.safe_dump(doc, sort_keys=False))
    return src


@pytest.fixture(scope="module")
def ran(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Fake m1 answering invalidly 50% of the time; mixed, pairwise and single Tests run."""
    study = init_study(tmp_path_factory.mktemp("export") / "study")
    cfg = yaml.safe_load((study / "study.yaml").read_text())
    cfg["models"][0]["fake"]["invalid_rate"] = 0.5
    (study / "study.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False))
    personas_stage.generate(study)
    _add_clips(study, TARGETS + PRACTICE)
    for clip, conditions in CONDITIONS.items():
        blinding.append_conditions(study, clip, conditions)
    push_test(study, _write_test(study, "pilot1", ["godspeed", "pairwise_alive"]))
    push_test(study, _write_test(study, "pair", ["pairwise_alive"]))
    push_test(study, _write_test(study, "single", ["godspeed"]))
    push_test(study, _write_test(study, "pilot2", ["godspeed"]))  # never opened
    for name in ("pilot1", "pair", "single"):
        open_test(study, name, dry_run=False, yes=True)
    return study


@pytest.fixture()
def study(tmp_path: Path, ran: Path) -> Path:
    copy_ = tmp_path / "study"
    shutil.copytree(ran, copy_)
    shutil.rmtree(copy_ / "exports", ignore_errors=True)
    return copy_


def _cli(study: Path, *args: str):
    return runner.invoke(app, ["export", *args, "--study", str(study)])


def _read(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    text = path.read_bytes().decode("utf-8")
    reader = csv.reader(io.StringIO(text, newline=""))
    header = next(reader)
    return header, [dict(zip(header, r, strict=True)) for r in reader]


def _trials(study: Path, test: str) -> list[dict]:
    conn = connect(study)
    try:
        return board_trials.load_trials(conn, test)
    finally:
        conn.close()


def _exec(study: Path, sql: str, args: tuple = ()) -> None:
    conn = connect(study)
    try:
        with transaction(conn):
            conn.execute(sql, args)
    finally:
        conn.close()


def _snapshot(study: Path) -> dict[str, bytes]:
    files = [study / DB_FILE, study / blinding.KEY_FILE, *sorted((study / "archive").iterdir())]
    return {str(p.relative_to(study)): p.read_bytes() for p in files}


# --------------------------------------------------------------------------- finished Tests


def test_finished_mixed_test(study: Path) -> None:
    result = _cli(study, "pilot1")
    assert result.exit_code == 0, result.stderr
    path = study / "exports" / "pilot1.csv"
    assert result.stdout.strip() == str(path)
    raw = path.read_bytes()
    assert b"\r" not in raw and raw.endswith(b"\n")
    header, rows = _read(path)
    assert header == [
        *HEAD, "embodiment", "embodiment_a", "embodiment_b", "speed", "speed_a", "speed_b",
        *TAIL,
    ]
    trials = _trials(study, "pilot1")
    instruments = load_instruments(study, load_study(study))
    items = {name: [i.id for i in inst.items] for name, inst in instruments.items()}
    # Every Trial present; one row per Item.
    assert {(r["session_id"], int(r["trial_index"])) for r in rows} == {
        (t["session_id"], t["trial_index"]) for t in trials}
    assert len(rows) == sum(len(items[t["instrument"]]) for t in trials)
    assert {r["schema_version"] for r in rows} == {str(EXPORT_SCHEMA_VERSION)}
    assert {(r["excluded"], r["exclusion_reason"], r["protocol_lock"]) for r in rows} == {
        ("false", "", "")}
    assert {r["test_kind"] for r in rows} == {"pilot"}
    # Order: session_id (natural), trial_index, Item order.
    sessions = list(dict.fromkeys(r["session_id"] for r in rows))
    assert sessions[:3] == ["pilot1/p1-m1/r1", "pilot1/p2-m1/r1", "pilot1/p3-m1/r1"]
    by_trial = {(t["session_id"], t["trial_index"]): t for t in trials}
    for sid in sessions[:5]:
        mine = [r for r in rows if r["session_id"] == sid]
        indexes = [int(r["trial_index"]) for r in mine]
        assert indexes == sorted(indexes)
        for index in set(indexes):
            t = by_trial[(sid, index)]
            assert [r["item"] for r in mine if int(r["trial_index"]) == index] == items[
                t["instrument"]]
    # Persona columns from index.json.
    personas = {p.id: p for p in load_personas(study)}
    for r in rows[:50]:
        p = personas[r["agent_id"].split("-")[0]]
        assert [r[c] for c in PERSONA_COLUMNS] == [
            *(p.big_five[t] for t in TRAITS), p.nars, p.age_band, p.gender, p.cultural_region,
            p.robot_experience]
        assert r["model"] == "m1"
    states = Counter(r["status"] for r in rows)
    assert states["valid"] and states["invalid"]  # Fake rater with invalid rate > 0
    conn = connect(study)
    try:
        for r in rows:
            t = by_trial[(r["session_id"], int(r["trial_index"]))]
            assert r["status"] == t["state"]
            assert r["timestamp"].endswith("Z") and r["seed"].isdigit()
            if t["state"] != "valid":
                assert r["response"] == ""
                continue
            chosen = board_trials.chosen_answer(conn, t["trial_id"])
            seed = conn.execute(
                "SELECT seed FROM attempts WHERE trial_id = ? AND attempt = ?",
                (t["trial_id"], chosen["attempt"])).fetchone()[0]
            assert r["seed"] == str(seed)
            value = chosen["answers"][r["item"]]
            if t["pair_id"] is None:
                assert r["clip_id"] == t["clip_ids"][0]
                assert (r["pair_id"], r["clip_id_a"], r["clip_id_b"]) == ("", "", "")
                cond = CONDITIONS[r["clip_id"]]
                assert (r["embodiment"], r["speed"]) == (cond["embodiment"], cond["speed"])
                assert r["embodiment_a"] == r["embodiment_b"] == r["speed_a"] == ""
                assert r["response"] == str(value)
                assert r["position"] == ""
            else:
                assert r["clip_id"] == "" and r["pair_id"] == t["pair_id"]
                assert (r["clip_id_a"], r["clip_id_b"]) == tuple(t["clip_ids"])
                assert r["embodiment_a"] == CONDITIONS[r["clip_id_a"]]["embodiment"]
                assert r["embodiment_b"] == CONDITIONS[r["clip_id_b"]]["embodiment"]
                assert r["speed_b"] == CONDITIONS[r["clip_id_b"]]["speed"]
                assert r["embodiment"] == ""
                assert r["response"] in (r["clip_id_a"], r["clip_id_b"])
                assert r["response"] == t["clip_ids"][0 if value == "A" else 1]
                assert r["position"] == str(t["position"])
    finally:
        conn.close()


def test_pairwise_only_and_single_only_headers(study: Path) -> None:
    header, rows = _read(export_test(study, "pair"))
    assert header == [*HEAD, "embodiment_a", "embodiment_b", "speed_a", "speed_b", *TAIL]
    assert all(r["clip_id"] == "" and r["pair_id"] for r in rows)
    assert all(r["response"] in (r["clip_id_a"], r["clip_id_b"]) for r in rows
               if r["status"] == "valid")
    header, rows = _read(export_test(study, "single"))
    assert header == [*HEAD, "embodiment", "speed", *TAIL]
    assert all(r["clip_id"] and not r["pair_id"] for r in rows)


def test_no_practice_in_export(study: Path) -> None:
    text = export_test(study, "pilot1").read_text(encoding="utf-8")
    for clip in PRACTICE:
        assert clip not in text
    assert "practiceonly" not in text
    assert "practice" not in text.splitlines()[0]


def test_stderr_reports_invalid_rates_without_conditions(study: Path) -> None:
    result = _cli(study, "pilot1")
    assert result.exit_code == 0, result.stderr
    assert "invalid_rate: model m1 " in result.stderr
    assert "invalid_rate_above_max: agent p" in result.stderr  # 0.5 Fake rate > 0.05
    for conditions in CONDITIONS.values():
        for level in conditions.values():
            assert level not in result.stderr


def test_refused_and_failed_rows(study: Path) -> None:
    trials = [t for t in _trials(study, "pilot1") if t["state"] == "valid"]
    refused, failed = trials[0], trials[1]
    _exec(study, "UPDATE trials SET state = 'refused' WHERE trial_id = ?", (refused["trial_id"],))
    _exec(study, "UPDATE trials SET state = 'failed' WHERE trial_id = ?", (failed["trial_id"],))
    _header, rows = _read(export_test(study, "pilot1"))
    for t, state in ((refused, "refused"), (failed, "failed")):
        mine = [r for r in rows
                if (r["session_id"], int(r["trial_index"])) == (t["session_id"],
                                                                 t["trial_index"])]
        assert mine and {r["status"] for r in mine} == {state}
        assert {r["response"] for r in mine} == {""}


def test_reexport_identical_bytes(study: Path) -> None:
    path = study / "exports" / "pilot1.csv"
    path.parent.mkdir(exist_ok=True)
    path.write_text("old")
    first = export_test(study, "pilot1").read_bytes()
    assert first != b"old"
    assert export_test(study, "pilot1").read_bytes() == first
    # no temp left
    assert sorted(p.name for p in path.parent.iterdir()) == ["pilot1-attrition.csv", "pilot1.csv"]


def test_while_other_test_dispatching(study: Path) -> None:
    with acquire_lease(study):
        result = _cli(study, "pilot1")
    assert result.exit_code == 0, result.stderr


def test_read_only(study: Path) -> None:
    before = _snapshot(study)
    files = sorted(p.name for p in study.iterdir())
    assert _cli(study, "pilot1").exit_code == 0
    assert _cli(study, "pilot2").exit_code == 1
    assert _snapshot(study) == before
    assert sorted(p.name for p in study.iterdir()) == sorted({*files, "exports"})
    assert sorted(p.name for p in (study / "exports").iterdir()) == [
        "pilot1-attrition.csv", "pilot1.csv"]


# --------------------------------------------------------------------------- refusals


@pytest.fixture()
def spy(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    calls: list[Path] = []
    real = blinding.read_key

    def read_key(study_dir):
        calls.append(Path(study_dir))
        return real(study_dir)

    monkeypatch.setattr(export_stage.blinding, "read_key", read_key)
    return calls


def _refused(study: Path, test: str, expected: str) -> None:
    result = _cli(study, test)
    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr.strip() == expected
    exports = study / "exports"
    assert not exports.exists() or list(exports.iterdir()) == []


def test_read_key_once_per_success(study: Path, spy: list[Path]) -> None:
    export_test(study, "pilot1")
    assert len(spy) == 1


@pytest.mark.parametrize("state", ["planned", "sent"])
def test_sessions_running(study: Path, spy: list[Path], state: str) -> None:
    ids = [t["trial_id"] for t in _trials(study, "pilot1")][:12]
    for trial_id in ids:
        _exec(study, "UPDATE trials SET state = ? WHERE trial_id = ?", (state, trial_id))
    _refused(study, "pilot1", "sessions_running: 12 Trials not terminal")
    assert spy == []


def test_nothing_to_export(study: Path, spy: list[Path]) -> None:
    _refused(study, "pilot2", "nothing_to_export: pilot2")
    assert spy == []


def test_unknown_test(study: Path, spy: list[Path]) -> None:
    _refused(study, "nope", "unknown_test: nope")
    assert spy == []


def test_unknown_test_without_board(tmp_path: Path) -> None:
    study = init_study(tmp_path / "fresh")
    _refused(study, "pilot1", "unknown_test: pilot1")
    assert not (study / DB_FILE).exists()


def test_clip_missing_from_key(study: Path) -> None:
    """A target Clip pushed with no Condition exports empty Condition cells."""
    key = study / blinding.KEY_FILE
    lines = key.read_text().splitlines(keepends=True)
    key.write_text("".join(line for line in lines if not line.startswith("c_bbbbbbbb,")))
    header, rows = _read(export_test(study, "pilot1"))
    assert "embodiment" in header and "speed_b" in header
    for r in rows:
        if r["clip_id"] == "c_bbbbbbbb":
            assert (r["embodiment"], r["speed"]) == ("", "")
        if r["clip_id"] == "c_aaaaaaaa":
            assert r["embodiment"] == "physical"
        if r["pair_id"]:
            side = "a" if r["clip_id_a"] == "c_bbbbbbbb" else "b"
            assert r[f"embodiment_{side}"] == ""


def test_study_without_conditions(study: Path) -> None:
    """No blinding_key.csv at all (no Clip ever had a Condition): no Condition columns."""
    (study / blinding.KEY_FILE).unlink()
    result = _cli(study, "pilot1")
    assert result.exit_code == 0, result.stderr
    header, rows = _read(study / "exports" / "pilot1.csv")
    assert header == [*HEAD, *TAIL]
    assert rows


def test_unreadable_key(study: Path) -> None:
    (study / blinding.KEY_FILE).write_bytes(b"clip_id,level\nc_aaaaaaaa,\xff\n")
    result = _cli(study, "pilot1")
    assert result.exit_code == 1
    assert result.stderr.startswith("blinding_key_missing: blinding_key.csv cannot be read")
    assert not (study / "exports").exists()


def test_clip_lacking_one_factor(study: Path) -> None:
    blinding.append_conditions(study, "c_aaaaaaaa", {"lighting": "dimlight"})
    header, rows = _read(export_test(study, "pilot1"))
    assert "lighting" in header and "lighting_a" in header
    single = [r for r in rows if r["clip_id"]]
    assert {r["lighting"] for r in single if r["clip_id"] == "c_aaaaaaaa"} == {"dimlight"}
    assert {r["lighting"] for r in single if r["clip_id"] == "c_bbbbbbbb"} == {""}
    for r in rows:
        if r["pair_id"]:
            assert r["lighting_a"] == ("dimlight" if r["clip_id_a"] == "c_aaaaaaaa" else "")


def test_factor_name_clash(study: Path) -> None:
    blinding.append_conditions(study, "c_aaaaaaaa", {"model": "x"})
    _refused(study, "pilot1", "condition_name_clash: model")


def test_factor_clash_with_side_column(study: Path) -> None:
    blinding.append_conditions(study, "c_aaaaaaaa", {"clip_id": "x"})
    _refused(study, "pair", "condition_name_clash: clip_id")


def test_panel_edited(study: Path, spy: list[Path]) -> None:
    card = study / "panel" / "personas" / "p3.md"
    card.write_text(card.read_text() + "Edited.\n")
    _refused(study, "pilot1", "panel_mismatch: panel/personas/p3.md")
    assert spy == []


def test_panel_missing(study: Path) -> None:
    (study / "panel" / "personas" / "index.json").unlink()
    result = _cli(study, "pilot1")
    assert result.exit_code == 1 and result.stderr.startswith("panel_missing: ")


def test_api_raises(study: Path) -> None:
    with pytest.raises(ConsortiumError) as info:
        export_test(study, "pilot2")
    assert info.value.code == "nothing_to_export"
    assert json.dumps(info.value.message) == '"pilot2"'


# --------------------------------------------------------------------------- seeds, timestamps


TIMESTAMP = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")


def test_seed_timestamp_repeat_variant(study: Path) -> None:
    _header, rows = _read(export_test(study, "pilot1"))
    by_trial = {(t["session_id"], t["trial_index"]): t for t in _trials(study, "pilot1")}
    conn = connect(study)
    try:
        attempts = {
            (trial_id, attempt): (seed, answered_at)
            for trial_id, attempt, seed, answered_at in conn.execute(
                "SELECT trial_id, attempt, seed, answered_at FROM attempts")
        }
        seen = Counter()
        for r in rows:
            t = by_trial[(r["session_id"], int(r["trial_index"]))]
            assert TIMESTAMP.match(r["timestamp"]), r["timestamp"]
            assert r["repeat"] == str(t["repeat"])
            assert r["prompt_variant"] == t["prompt_variant"]
            if t["state"] == "valid":
                won = board_trials.chosen_answer(conn, t["trial_id"])["attempt"]
                assert r["timestamp"] == attempts[(t["trial_id"], won)][1]
            else:
                assert r["seed"] == str(attempts[(t["trial_id"], t["attempt"])][0])
            seen[t["state"]] += 1
        assert seen["valid"] and seen["invalid"]
    finally:
        conn.close()


def test_failed_trial_without_attempts(study: Path) -> None:
    t = next(t for t in _trials(study, "pilot1") if t["state"] == "valid")
    _exec(study, "DELETE FROM ledger WHERE trial_id = ?", (t["trial_id"],))
    _exec(study, "DELETE FROM attempts WHERE trial_id = ?", (t["trial_id"],))
    _exec(study, "UPDATE trials SET state = 'failed', attempt = 0 WHERE trial_id = ?",
          (t["trial_id"],))
    _header, rows = _read(export_test(study, "pilot1"))
    mine = [r for r in rows
            if (r["session_id"], int(r["trial_index"])) == (t["session_id"], t["trial_index"])]
    assert mine
    for r in mine:
        assert (r["status"], r["response"], r["seed"], r["timestamp"]) == ("failed", "", "", "")


def test_missing_attempt_row(study: Path) -> None:
    t = next(t for t in _trials(study, "pilot1") if t["state"] == "invalid")
    _exec(study, "DELETE FROM ledger WHERE trial_id = ?", (t["trial_id"],))
    _exec(study, "DELETE FROM attempts WHERE trial_id = ?", (t["trial_id"],))
    result = _cli(study, "pilot1")
    assert result.exit_code == 1 and result.stderr.startswith("board_unreadable: ")
    assert not (study / "exports").exists()


def test_agents_within_threshold_not_warned(study: Path) -> None:
    result = _cli(study, "pilot1")
    assert result.exit_code == 0, result.stderr
    conn = connect(study)
    try:
        rates = board_trials.invalid_rates(conn, "pilot1")["by_agent"]
    finally:
        conn.close()
    threshold = load_study(study).thresholds.invalid_rate_max
    below = [a for a, e in rates.items() if e["rate"] is not None and e["rate"] <= threshold]
    above = [a for a, e in rates.items() if e["rate"] is not None and e["rate"] > threshold]
    assert below and above
    for agent in below:
        assert f"agent {agent} " not in result.stderr
    for agent in above:
        assert f"invalid_rate_above_max: agent {agent} " in result.stderr


# --------------------------------------------------------------------------- stored answers


def test_instrument_changed(study: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real = export_stage.load_instruments

    def edited(study_dir, cfg):
        loaded = dict(real(study_dir, cfg))
        g = loaded["godspeed"]
        loaded["godspeed"] = g.model_copy(update={"items": g.items[:-1]})  # Item removed
        return loaded

    monkeypatch.setattr(export_stage, "load_instruments", edited)
    _refused(study, "pilot1", "instrument_changed: godspeed: stored answers do not match its Items")


def _set_answer(study: Path, instrument: str, change) -> None:
    t = next(t for t in _trials(study, "pilot1")
             if t["state"] == "valid" and t["instrument"] == instrument)
    conn = connect(study)
    try:
        chosen = board_trials.chosen_answer(conn, t["trial_id"])
    finally:
        conn.close()
    answers = change(chosen["answers"])
    _exec(study, "UPDATE attempts SET answer_json = ? WHERE trial_id = ? AND attempt = ?",
          (json.dumps(answers), t["trial_id"], chosen["attempt"]))


@pytest.mark.parametrize("value", [None, True, 3.0, "3"])
def test_bad_likert_value(study: Path, value: object) -> None:
    _set_answer(study, "godspeed", lambda a: {**a, "animacy_1": value})
    result = _cli(study, "pilot1")
    assert result.exit_code == 1 and result.stderr.startswith("board_unreadable: ")
    assert not (study / "exports").exists()


@pytest.mark.parametrize("value", [None, 1, "C"])
def test_bad_pairwise_value(study: Path, value: object) -> None:
    _set_answer(study, "pairwise_alive", lambda a: {**a, "alive": value})
    result = _cli(study, "pilot1")
    assert result.exit_code == 1 and result.stderr.startswith("board_unreadable: ")


def test_bad_clip_ids_length(study: Path) -> None:
    t = next(t for t in _trials(study, "pilot1") if t["pair_id"] is not None)
    _exec(study, "UPDATE trials SET clip_ids = ? WHERE trial_id = ?",
          (json.dumps([t["clip_ids"][0]]), t["trial_id"]))
    result = _cli(study, "pilot1")
    assert result.exit_code == 1 and result.stderr.startswith("board_unreadable: ")


# --------------------------------------------------------------------------- attrition (2.1)

ATTRITION_HEADER = [
    "schema_version", "dimension", "attribute", "value", "trials", "invalid", "refused",
    "failed", "failed_fatal", "failed_transient", "failed_exhausted",
]


def _expected_attrition(
    header: list[str], rows: list[dict[str, str]], kinds: dict[tuple[str, str], str]
) -> list[list[str]]:
    """The sidecar recomputed from the tidy CSV: one Trial per (session_id, trial_index).

    ``kinds`` maps a failed Trial's key to its ``failed_*`` column (default fatal).
    """
    trials: dict[tuple[str, str], dict[str, str]] = {}
    for r in rows:
        trials.setdefault((r["session_id"], r["trial_index"]), r)
    conditions = header[header.index("model") + 1:header.index("instrument")]
    blocks = [("model", "model_id", "model"),
              *(("persona", c, c) for c in PERSONA_COLUMNS),
              *(("condition", c, c) for c in conditions),
              ("instrument", "instrument", "instrument")]
    out = []
    for dimension, attribute, column in blocks:
        tally: dict[str, dict[str, int]] = {}
        for key, r in trials.items():
            acc = tally.setdefault(r[column], dict.fromkeys(ATTRITION_HEADER[4:], 0))
            acc["trials"] += 1
            if r["status"] in ("invalid", "refused", "failed"):
                acc[r["status"]] += 1
            if r["status"] == "failed":
                acc[kinds.get(key, "failed_fatal")] += 1
        out.extend(["1", dimension, attribute, value, *map(str, acc.values())]
                   for value, acc in tally.items())
    return out


def test_attrition_sidecar(study: Path) -> None:
    trials = _trials(study, "pilot1")
    for t in trials[:6]:
        _exec(study, "UPDATE trials SET state = 'refused' WHERE trial_id = ?", (t["trial_id"],))
    failed = trials[100:103]
    for t in failed:
        _exec(study, "UPDATE trials SET state = 'failed' WHERE trial_id = ?", (t["trial_id"],))
    for t, category in zip(failed[1:], ("transient", "attempts_exhausted"), strict=True):
        _exec(study, "UPDATE attempts SET category = ? WHERE trial_id = ? AND attempt = ?",
              (category, t["trial_id"], t["attempt"]))
    kinds = {(t["session_id"], str(t["trial_index"])): k for t, k in
             zip(failed, ("failed_fatal", "failed_transient", "failed_exhausted"), strict=True)}
    path = export_test(study, "pilot1")
    sidecar = study / "exports" / "pilot1-attrition.csv"
    assert export_stage.attrition_path(study, "pilot1") == sidecar
    header, rows = _read(path)
    text = sidecar.read_bytes().decode("utf-8")
    got = list(csv.reader(io.StringIO(text, newline="")))
    assert got[0] == ATTRITION_HEADER
    assert got[1:] == _expected_attrition(header, rows, kinds)
    dims = [r[1] for r in got[1:]]
    assert dims == sorted(dims, key=["model", "persona", "condition", "instrument"].index)
    assert {r[2] for r in got[1:] if r[1] == "condition"} == {
        "embodiment", "embodiment_a", "embodiment_b", "speed", "speed_a", "speed_b"}
    assert [r[3] for r in got[1:] if r[1] == "instrument"] == ["godspeed", "pairwise_alive"]
    invalid = sum(t["state"] == "invalid" for t in _trials(study, "pilot1"))
    assert invalid > 0
    model = next(r for r in got[1:] if r[1] == "model")
    assert model == ["1", "model", "model_id", "m1", str(len(trials)), str(invalid), "6", "3",
                     "1", "1", "1"]
    persona_attrs = list(dict.fromkeys(r[2] for r in got[1:] if r[1] == "persona"))
    assert persona_attrs == PERSONA_COLUMNS
    for attribute in {r[2] for r in got[1:]}:
        mine = [r for r in got[1:] if r[2] == attribute]
        assert sum(int(r[4]) for r in mine) == len(trials)
        assert [sum(int(r[i]) for r in mine) for i in range(5, 11)] == [invalid, 6, 3, 1, 1, 1]
    assert export_test(study, "pilot1") == path  # re-export: identical bytes
    assert sidecar.read_bytes().decode("utf-8") == text


def test_export_writes_both_or_neither(study: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A failure while writing the second temp file leaves no target and no temp file."""
    real = export_stage.os.fsync
    calls = {"n": 0}

    def flaky(fd: int) -> None:
        calls["n"] += 1
        if calls["n"] == 2:  # the sidecar's temp file
            raise OSError("disk full")
        real(fd)

    monkeypatch.setattr(export_stage.os, "fsync", flaky)
    with pytest.raises(OSError):
        export_test(study, "pilot1")
    assert list((study / "exports").iterdir()) == []


def test_attrition_sidecar_single_and_no_conditions(study: Path) -> None:
    (study / blinding.KEY_FILE).unlink()
    export_test(study, "single")
    got = list(csv.reader(io.StringIO(
        (study / "exports" / "single-attrition.csv").read_text(), newline="")))
    assert got[0] == ATTRITION_HEADER
    assert {r[1] for r in got[1:]} == {"model", "persona", "instrument"}


def test_attrition_not_written_on_refusal(study: Path) -> None:
    trial_id = _trials(study, "pilot1")[0]["trial_id"]
    _exec(study, "UPDATE trials SET state = 'sent' WHERE trial_id = ?", (trial_id,))
    with pytest.raises(ConsortiumError) as info:
        export_test(study, "pilot1")
    assert info.value.code == "sessions_running"
    assert not (study / "exports" / "pilot1-attrition.csv").exists()
    assert not (study / "exports" / "pilot1.csv").exists()
