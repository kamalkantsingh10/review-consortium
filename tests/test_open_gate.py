"""Story 3.3: the eligibility gate in ``consortium open``, one test per I/O matrix row
(Fake rater), plus the acceptance criteria."""

from __future__ import annotations

import dataclasses
import json
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from consortium.archive.jsonl import REQUESTS_FILE
from consortium.board import screening as board_screening
from consortium.board.clips import insert_clip
from consortium.board.db import DB_FILE, connect, read_only, transaction
from consortium.cli import app
from consortium.config import load as config_load
from consortium.config.load import (
    card_wording_sha256,
    load_fidelity_instruments,
    load_instruments,
    load_personas,
    load_study,
)
from consortium.core.errors import ConsortiumError
from consortium.core.hashes import fidelity_hash, instrument_hash, settings_hash
from consortium.engine import run as engine
from consortium.stages import open as open_stage
from consortium.stages import personas as personas_stage
from consortium.stages.init import init_study
from consortium.stages.open import open_test
from consortium.stages.push import push_test
from consortium.stages.screen import screen_models, screen_personas
from test_screen_models import CLIPS, SHA
from test_screen_models import checks as perception_checks

runner = CliRunner()
INSTRUMENTS = ["godspeed", "pairwise_alive"]
MODELS = ["m1", "m2"]
PERSONAS = 16  # 1/4 fraction: 8 profiles x 2 NARS bands
AGENTS = PERSONAS * len(MODELS)
TRIALS_PER_SESSION = 16  # godspeed 4 + pairwise_alive 12


class Killed(Exception):
    """Stands in for the process dying mid-Run."""


def _edit_yaml(path: Path, change) -> None:
    doc = yaml.safe_load(path.read_text())
    change(doc)
    path.write_text(yaml.safe_dump(doc, sort_keys=False))


def _setup(study: Path) -> None:
    def change(doc: dict[str, Any]) -> None:
        doc["personas"]["big_five"] = {"fraction": "1/4", "replicates": 1}
        first = doc["models"][0]
        first["fake"]["fidelity"] = "faithful"
        first["fake"]["perception"] = "faithful"
        second = json.loads(json.dumps(first))
        second["id"] = "m2"
        second["fake"]["fidelity"] = "unfaithful"
        second["fake"]["perception"] = "unfaithful"
        doc["models"] = [first, second]

    _edit_yaml(study / "study.yaml", change)
    _edit_yaml(study / "prices.yaml",
               lambda doc: doc["models"].__setitem__("m2", dict(doc["models"]["m1"])))
    conn = connect(study)
    try:
        with transaction(conn):
            for clip in CLIPS:
                info = SimpleNamespace(duration_s=2.0, size_bytes=1000, width=640, height=480,
                                       fps=25.0, loudness_lufs=-23.0)
                insert_clip(conn, clip, SHA[clip], info)
    finally:
        conn.close()


def _push(study: Path, name: str, kind: str, **fields: Any) -> str:
    doc: dict[str, Any] = {
        "schema_version": 1, "test": name, "kind": kind, "instruments": INSTRUMENTS,
        "clips": CLIPS[:4], "session": {"practice_clips": 0, "repeats": 1},
    }
    doc.update(fields)
    src = study.parent / f"{name}.yaml"
    src.write_text(yaml.safe_dump(doc, sort_keys=False))
    return push_test(study, src)


@pytest.fixture(scope="module")
def base(tmp_path_factory: pytest.TempPathFactory) -> Path:
    study = init_study(tmp_path_factory.mktemp("gate") / "study")
    _setup(study)
    personas_stage.generate(study)
    _push(study, "pilot1", "pilot")
    return study


@pytest.fixture()
def study(tmp_path: Path, base: Path) -> Path:
    copy = tmp_path / "copy"
    shutil.copytree(base, copy)
    return copy


# --------------------------------------------------------------------------- seeding


def _stamps(study: Path) -> tuple[dict[str, str], dict[str, str], str]:
    cfg = load_study(study)
    defs = load_instruments(study, cfg)
    return (
        {m.id: settings_hash(m) for m in cfg.models},
        {n: instrument_hash(defs[n]) for n in INSTRUMENTS},
        fidelity_hash(load_fidelity_instruments(study, cfg, warn_draft=False),
                      card_wording_sha256()),
    )


def _personas(study: Path) -> list[str]:
    return [p.id for p in load_personas(study)]


def _row(model: str, instrument: str, outcome: str, settings: str, stamp: str,
         agent: str | None = None, pair_checks: int | None = None) -> dict[str, Any]:
    return {"model_id": model, "agent_id": agent, "instrument": instrument,
            "outcome": outcome, "score": 1.0 if outcome == "pass" else 0.0, "threshold": 0.8,
            "settings_hash": settings, "instrument_hash": stamp, "detail": "{}",
            "pair_checks": pair_checks}


def _seed(study: Path, kind: str, rows: list[dict[str, Any]], *,
          complete: bool = True) -> str:
    """A screening run through ``board.screening`` (no Trials), with ``rows`` as results."""
    settings, _, fidelity = _stamps(study)
    conn = connect(study)
    try:
        run_id = board_screening.next_run_id(conn)
        board_screening.new_run(
            conn, run_id, kind, "scr" if kind == "perception" else None, f"seeded/{run_id}",
            "0" * 64, [], settings, instrument_hash=fidelity,
        )
        if complete:
            board_screening.complete_run(conn, run_id, rows)
    finally:
        conn.close()
    return run_id


def _seed_fidelity(study: Path, outcomes: dict[str, str] | None = None,
                   skip: set[str] = frozenset()) -> str:
    settings, _, fidelity = _stamps(study)
    outcomes = outcomes or {}
    rows = [
        _row(m, "fidelity", outcomes.get(f"{p}-{m}", "pass"), settings[m], fidelity,
             agent=f"{p}-{m}")
        for p in _personas(study) for m in MODELS if f"{p}-{m}" not in skip
    ]
    return _seed(study, "fidelity", rows)


def _seed_perception(study: Path, outcomes: dict[tuple[str, str], str] | None = None,
                     instruments: list[str] = INSTRUMENTS) -> str:
    settings, stamps, _ = _stamps(study)
    outcomes = outcomes or {}
    rows = [_row(m, n, outcomes.get((m, n), "pass"), settings[m], stamps[n], pair_checks=2)
            for m in MODELS for n in instruments]
    return _seed(study, "perception", rows)


def _seed_all_pass(study: Path) -> None:
    _seed_fidelity(study)
    _seed_perception(study)


# --------------------------------------------------------------------------- helpers


def _cli(*args: str):
    return runner.invoke(app, ["open", *args])


def _q(study: Path, sql: str) -> list[tuple]:
    return read_only(study, lambda conn: conn.execute(sql).fetchall()) or []


def _line(lines: list[str], prefix: str) -> str:
    return next(line for line in lines if line.startswith(prefix))


def _ungated_digest(study: Path) -> str:
    """The Epic 1 path (``engine.prepare_test``: every Agent, no gate)."""
    prepared = engine.prepare_test(study, "pilot1", open_stage.READER, plan=True)
    return engine.dry_run(study, prepared, None).requests_sha256


def _board_bytes(study: Path) -> bytes:
    return (study / DB_FILE).read_bytes()


def _exclusions(study: Path) -> list[dict[str, Any]]:
    return read_only(study, lambda c: board_screening.test_exclusions(c, "pilot1")) or []


def _set_temperature(study: Path, value: float) -> None:
    _edit_yaml(study / "study.yaml",
               lambda doc: doc["models"][0]["settings"].__setitem__("temperature", value))


def _refused(study: Path, code: str, *args: str) -> str:
    result = _cli("pilot1", *args, "--study", str(study))
    assert result.exit_code == 1
    assert result.stderr.startswith(f"{code}:"), result.stderr
    return result.stderr


# --------------------------------------------------------------------------- matrix


def test_all_pass_plan_identical_to_ungated(study: Path) -> None:
    ungated = _ungated_digest(study)
    _seed_all_pass(study)
    dry = _cli("pilot1", "--dry-run", "--study", str(study))
    assert dry.exit_code == 0, dry.stderr
    lines = dry.stdout.splitlines()
    assert lines[6:10] == [
        f"by type: single {AGENTS * 4}, pairwise {AGENTS * 12}",
        "screening: fidelity s1, perception s2",
        f"eligible agents: {AGENTS} of {AGENTS}",
        "excluded: none",
    ]
    assert _line(lines, "requests sha256:") == f"requests sha256: {ungated}"
    assert "unscreened_pilot" not in dry.stderr
    run = _cli("pilot1", "--yes", "--study", str(study))
    assert run.exit_code == 0, run.stderr
    assert f"requests sha256: {ungated}" in run.stdout.splitlines()
    assert _exclusions(study) == []
    stamps = read_only(study, lambda c: board_screening.test_stamps(c, "pilot1"))
    settings, defs, fidelity = _stamps(study)
    assert stamps == {(m, n): (settings[m], defs[n]) for m in MODELS for n in INSTRUMENTS} | {
        (m, "fidelity"): (settings[m], fidelity) for m in MODELS}
    # The decision is kept for audit: the run IDs that decided each key.
    assert sorted(_q(study, "SELECT model_id, instrument, runs FROM screening_stamps")) == [
        ("m1", "fidelity", '["s1"]'), ("m1", "godspeed", '["s2"]'),
        ("m1", "pairwise_alive", '["s2"]'), ("m2", "fidelity", '["s1"]'),
        ("m2", "godspeed", '["s2"]'), ("m2", "pairwise_alive", '["s2"]')]


def test_fidelity_fail_and_missing_dry_run_matches_run(study: Path) -> None:
    p = _personas(study)
    _seed_fidelity(study, {f"{p[2]}-m1": "fail"}, skip={f"{p[4]}-m1"})
    _seed_perception(study)
    before = _board_bytes(study)
    dry = _cli("pilot1", "--dry-run", "--study", str(study))
    assert dry.exit_code == 0, dry.stderr
    assert _board_bytes(study) == before  # a dry run changes nothing
    assert not (study / f"{DB_FILE}-wal").exists()
    expected = [
        f"eligible agents: {AGENTS - 2} of {AGENTS}",
        "excluded: fidelity_fail 1, fidelity_missing 1",
    ]
    out = dry.stdout.splitlines()
    assert out[8:10] == expected
    run = _cli("pilot1", "--yes", "--study", str(study))
    assert run.exit_code == 0, run.stderr
    ran = run.stdout.splitlines()
    assert ran[8:10] == expected
    assert _line(ran, "requests sha256:") == _line(out, "requests sha256:")
    assert _exclusions(study) == [
        {"agent_id": f"{p[2]}-m1", "persona_id": p[2], "model_id": "m1", "instrument": None,
         "reason": "fidelity_fail"},
        {"agent_id": f"{p[4]}-m1", "persona_id": p[4], "model_id": "m1", "instrument": None,
         "reason": "fidelity_missing"},
    ]
    agents = {r[0] for r in _q(study, "SELECT DISTINCT agent_id FROM trials "
                                      "WHERE test = 'pilot1'")}
    assert len(agents) == AGENTS - 2 and f"{p[2]}-m1" not in agents


def test_model_fails_one_instrument(study: Path) -> None:
    _seed_fidelity(study)
    _seed_perception(study, {("m2", "pairwise_alive"): "fail"})
    summary = open_test(study, "pilot1", dry_run=True)
    assert summary.screening[1:] == (
        f"eligible agents: {PERSONAS} of {AGENTS}",
        f"excluded: perception_fail {PERSONAS} (m2: pairwise_alive)",
    )
    assert summary.by_model == {"m1": PERSONAS * TRIALS_PER_SESSION}
    run = open_test(study, "pilot1", dry_run=False, yes=True)
    assert run.requests_sha256 == summary.requests_sha256
    rows = _exclusions(study)
    assert len(rows) == PERSONAS
    assert {(r["model_id"], r["instrument"], r["reason"]) for r in rows} == {
        ("m2", "pairwise_alive", "perception_fail")}
    assert set(read_only(study, lambda c: board_screening.test_stamps(c, "pilot1"))) == {
        ("m1", n) for n in [*INSTRUMENTS, "fidelity"]}


def test_no_coverage(study: Path) -> None:
    _seed_fidelity(study)
    _seed_perception(study, instruments=["godspeed"])
    before = _board_bytes(study)
    err = _refused(study, "screening_coverage_missing", "--yes")
    assert "pairwise_alive" in err and "godspeed" not in err
    _refused(study, "screening_coverage_missing", "--dry-run")
    assert _board_bytes(study) == before
    assert _q(study, "SELECT count(*) FROM trials") == [(0,)]


def test_settings_changed_is_stale(study: Path) -> None:
    _seed_all_pass(study)
    _set_temperature(study, 0.9)
    err = _refused(study, "screening_stale", "--dry-run")
    assert "Model m1 (settings changed)" in err and "m2" not in err
    assert "screen personas" in err and "screen models" in err
    _refused(study, "screening_stale", "--yes")
    assert _q(study, "SELECT count(*) FROM trials") == [(0,)]


def test_instrument_edited_is_stale(study: Path, tmp_path: Path,
                                    monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_all_pass(study)
    builtin = tmp_path / "builtin"
    shutil.copytree(Path(config_load.__file__).parent.parent / "instruments", builtin)
    path = builtin / "godspeed.yaml"
    path.write_text(path.read_text().replace('"Dead / Alive"', '"Dead / Very alive"'))
    monkeypatch.setattr(config_load, "_builtin_dir", lambda: builtin)
    with pytest.raises(ConsortiumError) as info:
        open_test(study, "pilot1", dry_run=True)
    assert info.value.code == "screening_stale"
    assert "Instrument godspeed (definition changed)" in info.value.message
    assert "pairwise_alive" not in info.value.message


def test_superseded_new_pass_counts(study: Path) -> None:
    _seed_fidelity(study)
    _seed_perception(study, {("m1", "pairwise_alive"): "fail"})
    _seed_perception(study)  # s3 passes everything and supersedes s2
    summary = open_test(study, "pilot1", dry_run=True)
    assert summary.screening == (
        "screening: fidelity s1, perception s3",
        f"eligible agents: {AGENTS} of {AGENTS}",
        "excluded: none",
    )


def test_none_eligible(study: Path) -> None:
    _seed_fidelity(study, {f"{p}-{m}": "fail" for p in _personas(study) for m in MODELS})
    _seed_perception(study)
    before = _board_bytes(study)
    err = _refused(study, "no_eligible_agents", "--yes")
    assert f"fidelity_fail {AGENTS}" in err
    assert _board_bytes(study) == before
    assert not (study / "archive").exists() or not (study / REQUESTS_FILE).exists()


def test_pre_screening_pilot_runs_ungated(study: Path) -> None:
    ungated = _ungated_digest(study)
    dry = _cli("pilot1", "--dry-run", "--study", str(study))
    assert dry.exit_code == 0, dry.stderr
    assert dry.stderr == "unscreened_pilot: Test pilot1 runs without screening\n"
    assert "screening: none" in dry.stdout.splitlines()
    assert "eligible agents" not in dry.stdout
    assert f"requests sha256: {ungated}" in dry.stdout.splitlines()
    warned: list[str] = []
    run = open_test(study, "pilot1", dry_run=False, yes=True, warn=warned.append)
    assert warned == ["unscreened_pilot: Test pilot1 runs without screening"]
    assert run.requests_sha256 == ungated and run.trials == AGENTS * TRIALS_PER_SESSION
    assert _exclusions(study) == []
    assert read_only(study, lambda c: board_screening.test_stamps(c, "pilot1")) == {}


def test_only_a_complete_run_gates_pilots(study: Path) -> None:
    _seed(study, "fidelity", [], complete=False)  # an open run with no results yet
    conn = connect(study)
    try:
        board_screening.abandon_run(conn, "s1")
    finally:
        conn.close()
    dry = _cli("pilot1", "--dry-run", "--study", str(study))
    assert dry.exit_code == 0, dry.stderr
    assert "unscreened_pilot" in dry.stderr and "screening: none" in dry.stdout
    _seed(study, "perception", [], complete=False)  # s2 stays open
    assert _cli("pilot1", "--dry-run", "--study", str(study)).exit_code == 0
    _seed_fidelity(study)  # s3, complete
    err = _refused(study, "screening_coverage_missing", "--dry-run")
    assert "(pilots are gated once a screening run exists: s3)" in err


def test_main_test_is_always_gated(study: Path) -> None:
    ctx, test_cfg, personas = engine.load_test_context(study, "pilot1", open_stage.READER)
    main = dataclasses.replace(ctx, kind="main")
    with pytest.raises(ConsortiumError) as info:  # no screening run at all
        open_stage.gate_test(study, main, test_cfg, personas)
    assert info.value.code == "screening_coverage_missing"
    assert "pilots are gated" not in info.value.message
    _seed_all_pass(study)
    gate = open_stage.gate_test(study, main, test_cfg, personas)
    assert gate.gated and not gate.warn and gate.agents is not None
    assert len(gate.agents) == AGENTS


def test_perception_missing_detail_line(study: Path) -> None:
    _seed_fidelity(study)
    settings, stamps, _ = _stamps(study)
    rows = [_row(m, n, "pass", settings[m], stamps[n], pair_checks=2)
            for m, n in [("m1", "godspeed"), ("m1", "pairwise_alive"), ("m2", "godspeed")]]
    _seed(study, "perception", rows)
    dry = _cli("pilot1", "--dry-run", "--study", str(study))
    assert dry.exit_code == 0, dry.stderr
    assert f"excluded: perception_missing {PERSONAS} (m2: pairwise_alive)" in \
        dry.stdout.splitlines()


def test_stale_fidelity_through_the_cli(study: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_all_pass(study)
    monkeypatch.setattr(config_load, "card_wording_sha256", lambda: "f" * 64)
    err = _refused(study, "screening_stale", "--dry-run")
    assert "the fidelity Instruments or Persona card wording (definition changed)" in err
    assert "screen personas" in err and "screen models" not in err


def test_regate_refusal_under_the_lease_is_test_changed(study: Path) -> None:
    _seed_all_pass(study)

    def confirm(_prompt: str) -> bool:  # m1's settings change before the lease
        _set_temperature(study, 0.9)
        return True

    with pytest.raises(ConsortiumError) as info:
        open_test(study, "pilot1", dry_run=False, confirm=confirm)
    assert info.value.code == "test_changed"
    assert isinstance(info.value.__cause__, ConsortiumError)
    assert info.value.__cause__.code == "screening_stale"
    assert _q(study, "SELECT count(*) FROM trials WHERE test = 'pilot1'") == [(0,)]


def _kill_run(study: Path) -> None:
    state = {"count": 0, "dead": False}

    def spy(op: str, key: tuple) -> None:
        if state["dead"]:
            raise Killed(f"killed before {op} {key}")
        if op == "record_validation":
            state["count"] += 1
            state["dead"] = state["count"] == 20

    with pytest.raises(ConsortiumError) as info:
        open_test(study, "pilot1", dry_run=False, yes=True, writer_spy=spy)
    assert info.value.code == "run_failed"


def test_resume_after_edit_is_stale(study: Path) -> None:
    p = _personas(study)
    _seed_fidelity(study, {f"{p[0]}-m2": "fail"})
    _seed_perception(study)
    _kill_run(study)
    assert len(_exclusions(study)) == 1
    _set_temperature(study, 0.9)
    requests = (study / REQUESTS_FILE).read_bytes()
    states = _q(study, "SELECT trial_id, state, attempt FROM trials ORDER BY seq")
    err = _refused(study, "screening_stale", "--yes", "--resume")
    assert "Model m1 (settings changed)" in err
    assert (study / REQUESTS_FILE).read_bytes() == requests  # nothing sent
    assert _q(study, "SELECT trial_id, state, attempt FROM trials ORDER BY seq") == states
    # Restoring the settings resumes; eligibility stays frozen (no re-gating).
    _set_temperature(study, 0.7)
    _seed_fidelity(study, {f"{p[1]}-m1": "fail"})  # would exclude another Agent if re-gated
    result = _cli("pilot1", "--yes", "--resume", "--study", str(study))
    assert result.exit_code == 0, result.stderr
    assert "screening: frozen at open (1 Agent excluded)" in result.stdout.splitlines()
    assert len(_exclusions(study)) == 1
    assert _q(study, "SELECT count(*) FROM trials WHERE state != 'valid'") == [(0,)]


def _edit_builtin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, old: str, new: str) -> None:
    builtin = tmp_path / "builtin"
    shutil.copytree(Path(config_load.__file__).parent.parent / "instruments", builtin)
    path = builtin / "godspeed.yaml"
    text = path.read_text()
    assert old in text
    path.write_text(text.replace(old, new, 1))
    monkeypatch.setattr(config_load, "_builtin_dir", lambda: builtin)


def _killed_gated_run(study: Path) -> tuple[bytes, list[tuple]]:
    _seed_all_pass(study)
    _kill_run(study)
    return ((study / REQUESTS_FILE).read_bytes(),
            _q(study, "SELECT trial_id, state, attempt FROM trials ORDER BY seq"))


def _nothing_sent(study: Path, before: tuple[bytes, list[tuple]]) -> None:
    assert (study / REQUESTS_FILE).read_bytes() == before[0]
    assert _q(study, "SELECT trial_id, state, attempt FROM trials ORDER BY seq") == before[1]


def test_resume_after_unrendered_instrument_edit_is_stale(
    study: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = _killed_gated_run(study)
    _edit_builtin(tmp_path, monkeypatch, "draft: false\n", "draft: false\ncitation: Edited\n")
    with pytest.raises(ConsortiumError) as info:
        open_test(study, "pilot1", dry_run=False, yes=True, resume=True)
    assert info.value.code == "screening_stale"
    assert "Instrument godspeed (definition changed)" in info.value.message
    _nothing_sent(study, before)


def test_resume_after_fidelity_stamp_change_is_stale(
    study: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = _killed_gated_run(study)
    monkeypatch.setattr(config_load, "card_wording_sha256", lambda: "f" * 64)
    err = _refused(study, "screening_stale", "--yes", "--resume")
    assert "fidelity Instruments or Persona card wording" in err
    _nothing_sent(study, before)


def test_resume_checks_only_models_it_will_send(study: Path) -> None:
    _killed_gated_run(study)
    conn = connect(study)
    try:
        with transaction(conn):  # every m2 Trial terminal: m2 sends nothing more
            conn.execute("UPDATE trials SET state = 'failed' WHERE model_id = 'm2'"
                         " AND state NOT IN ('valid', 'invalid', 'refused', 'failed')")
    finally:
        conn.close()
    _edit_yaml(study / "study.yaml",
               lambda doc: doc["models"][1]["settings"].__setitem__("temperature", 0.9))
    result = _cli("pilot1", "--yes", "--resume", "--study", str(study))
    assert result.exit_code == 0, result.stderr
    assert _q(study, "SELECT count(*) FROM trials WHERE model_id = 'm1'"
                     " AND state != 'valid'") == [(0,)]


def test_regate_under_lease_detects_a_new_decision(study: Path) -> None:
    _seed_all_pass(study)
    p = _personas(study)

    def confirm(_prompt: str) -> bool:  # a new fidelity run lands before the lease
        _seed_fidelity(study, {f"{p[0]}-m1": "fail"})
        return True

    with pytest.raises(ConsortiumError) as info:
        open_test(study, "pilot1", dry_run=False, confirm=confirm)
    assert info.value.code == "test_changed"
    assert _q(study, "SELECT count(*) FROM trials WHERE test = 'pilot1'") == [(0,)]
    assert _exclusions(study) == []


def test_real_screening_runs_gate_the_pilot(study: Path) -> None:
    """3.1/3.2's faithful (m1) and unfaithful (m2) Fake configs, end to end."""
    _push(study, "scr", "screening", checks=perception_checks(),
          instruments=["pairwise_alive", "godspeed", "perception_cues"])
    screen_personas(study, yes=True)
    screen_models(study, "scr", yes=True, warn=lambda _line: None)
    summary = open_test(study, "pilot1", dry_run=True)
    assert summary.screening == (
        "screening: fidelity s1, perception s2",
        f"eligible agents: {PERSONAS} of {AGENTS}",
        f"excluded: perception_fail {PERSONAS} (m2: godspeed, pairwise_alive)",
    )
    assert summary.by_model == {"m1": PERSONAS * TRIALS_PER_SESSION}
