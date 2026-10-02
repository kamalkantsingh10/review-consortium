"""Story 3.1: Persona-fidelity screening (``consortium screen personas``), every matrix row."""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from consortium.archive.jsonl import REQUESTS_FILE, read_lines
from consortium.board.db import DB_FILE, read_only
from consortium.board.lease import acquire_lease
from consortium.board.screening import current_results, list_runs, run_results
from consortium.cli import app
from consortium.config.load import card_wording_sha256, load_fidelity_instruments, load_study
from consortium.core.errors import ConsortiumError
from consortium.core.hashes import fidelity_hash, settings_hash
from consortium.raters.fake import FakeRater
from consortium.stages import personas as personas_stage
from consortium.stages.init import init_study
from consortium.stages.screen import screen_personas

runner = CliRunner()
SRC = Path(__file__).resolve().parents[1] / "src" / "consortium"
PERSONAS = 16  # 1/4 fraction: 8 profiles x 2 NARS bands


def _edit_yaml(path: Path, change) -> None:
    doc = yaml.safe_load(path.read_text())
    change(doc)
    path.write_text(yaml.safe_dump(doc, sort_keys=False))


def _setup(study: Path, *, bands: list[str] | None = None, m2: str = "unfaithful",
           m1: str = "faithful") -> None:
    def change(doc: dict[str, Any]) -> None:
        doc["personas"]["big_five"] = {"fraction": "1/4", "replicates": 1}
        if bands is not None:
            doc["personas"]["nars_bands"] = bands
        first = doc["models"][0]
        first["fake"]["fidelity"] = m1
        second = json.loads(json.dumps(first))
        second["id"] = "m2"
        second["fake"]["fidelity"] = m2
        doc["models"] = [first, second]

    _edit_yaml(study / "study.yaml", change)
    _edit_yaml(study / "prices.yaml",
               lambda doc: doc["models"].__setitem__("m2", dict(doc["models"]["m1"])))


def _make(path: Path, **kw: Any) -> Path:
    study = init_study(path)
    _setup(study, **kw)
    personas_stage.generate(study)
    return study


@pytest.fixture(scope="module")
def base(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _make(tmp_path_factory.mktemp("screen") / "study")


@pytest.fixture()
def study(tmp_path: Path, base: Path) -> Path:
    copy = tmp_path / "copy"
    shutil.copytree(base, copy)
    return copy


def _cli(*args: str):
    return runner.invoke(app, ["screen", "personas", *args])


def _q(study: Path, sql: str) -> list[tuple]:
    return read_only(study, lambda conn: conn.execute(sql).fetchall()) or []


def _runs(study: Path) -> list[dict]:
    return read_only(study, list_runs) or []


def _stamp(study: Path) -> str:
    return fidelity_hash(load_fidelity_instruments(study, load_study(study)),
                         card_wording_sha256())


def _board_rows(study: Path) -> tuple:
    if not (study / DB_FILE).exists():
        return ()
    return tuple(_q(study, f"SELECT count(*) FROM {t}")[0][0]
                 for t in ("tests", "trials", "attempts", "screening_runs", "screening_results"))


# --------------------------------------------------------------------------- matrix


def test_pass_fail_split_and_stamps(study: Path) -> None:
    result = _cli("--yes", "--study", str(study))
    assert result.exit_code == 0, result.stderr
    out = result.stdout.splitlines()
    assert out[0] == "test: s1 (screening)"
    assert "trials per session: 1 + 1 = 2 (fidelity_bfi10 1, fidelity_nars 1)" in out
    assert out[-3:] == [
        "screening run: s1 (fidelity) complete",
        f"fidelity m1: pass {PERSONAS}, fail 0 (insufficient data 0), threshold 0.8",
        f"fidelity m2: pass 0, fail {PERSONAS} (insufficient data 0), threshold 0.8",
    ]
    assert "draft_instrument: fidelity_nars" in result.stderr
    cfg = load_study(study)
    stamp = _stamp(study)
    run = _runs(study)[0]
    assert run["started_at"].endswith("Z") and len(run["started_at"]) == 24  # milliseconds
    assert run | {"started_at": None} == {
        "run_id": "s1", "kind": "fidelity", "screening_test": None, "started_at": None,
        "status": "complete", "superseded_by": None, "instrument_hash": stamp,
        "settings_hashes": {m.id: settings_hash(m) for m in cfg.models}}
    rows = read_only(study, lambda conn: run_results(conn, "s1"))
    assert len(rows) == 2 * PERSONAS
    for row in rows:
        faithful = row["model_id"] == "m1"
        assert row["instrument"] == "fidelity"
        assert row["score"] == (1.0 if faithful else 0.0)
        assert row["outcome"] == ("pass" if faithful else "fail")
        assert row["threshold"] == 0.8
        assert row["settings_hash"] == settings_hash(cfg.model_by_id(row["model_id"]))
        assert row["instrument_hash"] == stamp
        detail = json.loads(row["detail"])
        assert detail["total"] == 6 and set(detail["checks"]) == {
            "openness", "conscientiousness", "extraversion", "agreeableness", "neuroticism",
            "nars"}
        assert detail["checks"]["nars"]["subscales"].keys() == {"s1", "s2", "s3"}
    assert [r["agent_id"] for r in rows[:3]] == ["p1-m1", "p1-m2", "p2-m1"]
    # The run is a Test row; its Trials carry no Clip.
    assert _q(study, "SELECT kind, openable, path FROM tests WHERE name = 's1'") == [
        ("screening", 0, "<built-in>/screening/personas")]
    assert _q(study, "SELECT DISTINCT clip_ids FROM trials WHERE test = 's1'") == [("[]",)]


def test_archive_requests_have_no_clip_and_the_card(study: Path) -> None:
    assert _cli("--yes", "--study", str(study)).exit_code == 0
    lines, _ = read_lines(study, REQUESTS_FILE)
    assert len(lines) == 4 * PERSONAS
    for line in lines:
        request = line["request"]
        assert request["clips"] == [] and request["practice"] == []
        persona = line["trial_id"].split("/")[1].split("-")[0]
        card = (study / "panel" / "personas" / f"{persona}.md").read_text()
        assert request["persona_card"] == card
        assert "keys" not in json.dumps(request)  # scoring keys never reach a Model
        assert "reversed" not in json.dumps(request)


def test_second_run_supersedes_and_keeps(study: Path) -> None:
    assert _cli("--yes", "--study", str(study)).exit_code == 0
    first = read_only(study, lambda conn: run_results(conn, "s1"))
    result = _cli("--yes", "--study", str(study))
    assert result.exit_code == 0, result.stderr
    assert result.stdout.splitlines()[-1] == "superseded: s1"
    runs = {r["run_id"]: r for r in _runs(study)}
    assert runs["s1"]["superseded_by"] == "s2" and runs["s1"]["status"] == "complete"
    assert runs["s2"]["superseded_by"] is None and runs["s2"]["status"] == "complete"
    assert read_only(study, lambda conn: run_results(conn, "s1")) == first
    current = read_only(study, current_results)
    assert {r["run_id"] for r in current} == {"s2"} and len(current) == 2 * PERSONAS
    # A third run supersedes s2 and leaves s1's link alone.
    assert _cli("--yes", "--study", str(study)).exit_code == 0
    runs = {r["run_id"]: r for r in _runs(study)}
    assert (runs["s1"]["superseded_by"], runs["s2"]["superseded_by"]) == ("s2", "s3")


def _priced(study: Path) -> None:
    def prices(doc: dict) -> None:
        for model in doc["models"].values():
            model["input_usd_per_mtok"] = "1"

    def usage(doc: dict) -> None:
        doc["concurrency"] = 1
        for model in doc["models"]:
            model["fake"]["input_tokens"] = 10_000_000  # far above the estimate: pauses

    _edit_yaml(study / "prices.yaml", prices)
    _edit_yaml(study / "study.yaml", usage)


def test_ceiling_pause_then_resume_scores(study: Path) -> None:
    _priced(study)
    result = _cli("--yes", "--ceiling", "1", "--study", str(study))
    assert result.exit_code == 1
    assert result.stderr.splitlines()[-1].startswith("ceiling_reached: Screening run paused")
    assert "screening run: s1 (fidelity) open (no results yet)" in result.stdout
    assert _runs(study)[0]["status"] == "open"
    assert _q(study, "SELECT count(*) FROM screening_results") == [(0,)]

    # Run already open: refused, nothing written.
    before = _board_rows(study)
    again = _cli("--yes", "--ceiling", "1000000", "--study", str(study))
    assert again.exit_code == 1
    assert again.stderr.startswith("screening_run_open: screening run s1 is not complete")
    assert "--resume" in again.stderr
    assert _board_rows(study) == before

    resumed = _cli("--yes", "--resume", "--ceiling", "1000000", "--study", str(study))
    assert resumed.exit_code == 0, resumed.stderr
    assert resumed.stdout.splitlines()[-3] == "screening run: s1 (fidelity) complete"
    assert _runs(study)[0]["status"] == "complete"
    assert _q(study, "SELECT count(*) FROM screening_results") == [(2 * PERSONAS,)]
    assert _q(study, "SELECT paused_reason FROM tests WHERE name = 's1'") == [(None,)]


def test_nothing_to_resume(study: Path) -> None:
    result = _cli("--yes", "--resume", "--study", str(study))
    assert result.exit_code == 1
    assert result.stderr.startswith("screening_not_open:")
    assert _cli("--yes", "--study", str(study)).exit_code == 0
    assert _cli("--yes", "--resume", "--study", str(study)).stderr.startswith(
        "screening_not_open:")


def test_refused_bfi_trial_fails_the_agent(study: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real = FakeRater.category

    def category(self, call):
        if call.request.items[0].id.startswith("bfi_"):
            return "refused"
        return real(self, call)

    monkeypatch.setattr(FakeRater, "category", category)
    summary = screen_personas(study, yes=True)
    assert summary.complete
    for row in summary.results or []:
        assert row["outcome"] == "fail"
        checks = json.loads(row["detail"])["checks"]
        assert all(not checks[t]["match"] and checks[t]["score"] is None for t in
                   ("openness", "conscientiousness", "extraversion", "agreeableness",
                    "neuroticism"))
        assert checks["nars"]["match"] is (row["model_id"] == "m1")
        assert all(checks[t]["insufficient_data"] for t in ("openness", "neuroticism"))
        assert json.loads(row["detail"])["insufficient_data"] is True
        assert row["score"] == (1 / 6 if row["model_id"] == "m1" else 0.0)
    assert summary.result_lines()[1] == (
        f"fidelity m1: pass 0, fail {PERSONAS} (insufficient data {PERSONAS}), threshold 0.8")


def test_no_panel_is_refused_before_planning(tmp_path: Path) -> None:
    study = init_study(tmp_path / "s")
    before = _board_rows(study)
    result = _cli("--yes", "--study", str(study))
    assert result.exit_code == 1
    assert result.stderr.startswith("panel_missing:")
    assert _board_rows(study) == before


def test_misuse_is_refused(study: Path) -> None:
    assert _cli("--yes", "--study", str(study)).exit_code == 0
    doc = {"schema_version": 1, "test": "pilot9", "kind": "pilot",
           "instruments": ["fidelity_bfi10"], "clips": []}
    src = study.parent / "pilot9.yaml"
    src.write_text(yaml.safe_dump(doc))
    result = runner.invoke(app, ["push", "test", str(src), "--study", str(study)])
    assert result.exit_code == 1 and result.stderr.startswith("instrument_not_allowed:")

    # Enabled in study.yaml makes no difference.
    _edit_yaml(study / "study.yaml", lambda d: d["instruments"].append("fidelity_bfi10"))
    result = runner.invoke(app, ["push", "test", str(src), "--study", str(study)])
    assert result.exit_code == 1 and result.stderr.startswith("instrument_not_allowed:")

    doc.update(test="s3", instruments=["godspeed"])
    src = study.parent / "s3.yaml"
    src.write_text(yaml.safe_dump(doc))
    result = runner.invoke(app, ["push", "test", str(src), "--study", str(study)])
    assert result.exit_code == 1 and result.stderr.startswith("bad_test_name:")
    assert "reserved" in result.stderr

    result = runner.invoke(app, ["export", "s1", "--study", str(study)])
    assert result.exit_code == 1 and result.stderr.startswith("screening_not_exportable:")
    assert not (study / "exports" / "s1.csv").exists()

    result = runner.invoke(app, ["open", "s1", "--yes", "--study", str(study)])
    assert result.exit_code == 1  # a run is never openable
    assert result.stderr.startswith("screening_test_not_openable:")
    result = runner.invoke(app, ["open", "s1", "--dry-run", "--study", str(study)])
    assert result.stderr.startswith("screening_test_not_openable:")


def test_no_nars_bands(tmp_path: Path) -> None:
    study = _make(tmp_path / "s", bands=[])
    index = json.loads((study / "panel" / "personas" / "index.json").read_text())
    assert len(index) == 8 and all(p["nars"] is None for p in index)
    card = (study / "panel" / "personas" / "p1.md").read_text()
    assert len(card.splitlines()) == 6
    result = _cli("--yes", "--study", str(study))
    assert result.exit_code == 0, result.stderr
    assert "trials per session: 1 = 1 (fidelity_bfi10 1)" in result.stdout.splitlines()
    assert "draft_instrument" not in result.stderr
    rows = read_only(study, lambda conn: run_results(conn, "s1"))
    assert len(rows) == 16
    for row in rows:
        detail = json.loads(row["detail"])
        assert detail["total"] == 5 and "nars" not in detail["checks"]
        assert row["outcome"] == ("pass" if row["model_id"] == "m1" else "fail")
    cfg = load_study(study)
    [bfi] = load_fidelity_instruments(study, cfg)
    assert rows[0]["instrument_hash"] == fidelity_hash([bfi], card_wording_sha256())


def _user_nars(study: Path, name: str = "nars_s1", change=None) -> None:
    doc = yaml.safe_load((SRC / "templates" / "nars_instrument.yaml").read_text())
    doc["name"] = name
    doc["citation"] = "Nomura et al. (2006), Interaction Studies 7(3)."
    for i, item in enumerate(doc["items"], start=1):
        item["text"] = f"User statement {i}"
    if change is not None:
        change(doc)
    (study / "instruments").mkdir(exist_ok=True)
    (study / "instruments" / f"{name}.yaml").write_text(yaml.safe_dump(doc, sort_keys=False))
    _edit_yaml(study / "study.yaml",
               lambda d: d.__setitem__("screening", {"nars_instrument": name}))


def test_user_nars_instrument(study: Path) -> None:
    _user_nars(study)
    result = _cli("--yes", "--study", str(study))
    assert result.exit_code == 0, result.stderr
    assert "trials per session: 1 + 1 = 2 (fidelity_bfi10 1, nars_s1 1)" in result.stdout
    assert "draft_instrument" not in result.stderr
    cfg = load_study(study)
    instruments = load_fidelity_instruments(study, cfg)
    assert [i.name for i in instruments] == ["fidelity_bfi10", "nars_s1"]
    rows = read_only(study, lambda conn: run_results(conn, "s1"))
    assert {r["instrument_hash"] for r in rows} == {
        fidelity_hash(instruments, card_wording_sha256())}
    assert _q(study, "SELECT DISTINCT instrument FROM trials ORDER BY 1") == [
        ("fidelity_bfi10",), ("nars_s1",)]
    assert {r["outcome"] for r in rows if r["model_id"] == "m1"} == {"pass"}


def test_s1_only_short_form_is_valid(study: Path) -> None:
    def short(doc: dict) -> None:
        keep = {k for k, v in doc["keys"].items() if v["subscale"] == "s1"}
        doc["items"] = [i for i in doc["items"] if i["id"] in keep]
        doc["keys"] = {k: v for k, v in doc["keys"].items() if k in keep}

    _user_nars(study, change=short)
    summary = screen_personas(study, yes=True)
    assert summary.complete
    detail = json.loads(summary.results[0]["detail"])
    assert detail["checks"]["nars"]["subscales"].keys() == {"s1"}


@pytest.mark.parametrize("case", ["not_self_report", "unkeyed", "not_nars", "missing"])
def test_bad_nars_instrument(study: Path, case: str) -> None:
    def change(doc: dict) -> None:
        if case == "not_self_report":
            doc["self_report"] = False
        elif case == "unkeyed":
            del doc["keys"]["nars_3"]
        elif case == "not_nars":
            doc["keys"]["nars_3"] = {"construct": "openness", "reversed": False}

    if case == "missing":
        _edit_yaml(study / "study.yaml",
                   lambda d: d.__setitem__("screening", {"nars_instrument": "nope"}))
    else:
        _user_nars(study, change=change)
    before = _board_rows(study)
    with pytest.raises(ConsortiumError) as info:
        screen_personas(study, yes=True)
    if case == "missing":
        assert info.value.code == "unknown_instrument"
        assert info.value.message.startswith("screening.nars_instrument: 'nope'")
    else:
        assert info.value.code == "config_invalid"
        assert (info.value.path, info.value.message.split(":")[0]) == (
            "study.yaml", "screening.nars_instrument")
    assert _board_rows(study) == before


def test_template_matches_placeholder_structure() -> None:
    template = yaml.safe_load((SRC / "templates" / "nars_instrument.yaml").read_text())
    builtin = yaml.safe_load((SRC / "instruments" / "fidelity_nars.yaml").read_text())
    assert template["keys"] == builtin["keys"]
    assert [i["id"] for i in template["items"]] == [i["id"] for i in builtin["items"]]
    assert all(i["text"] == "" for i in template["items"])
    assert {k: template[k] for k in ("self_report", "draft")} == {
        "self_report": True, "draft": False}
    assert "citation" in template and builtin["draft"] is True
    s3 = {k for k, v in builtin["keys"].items() if v["subscale"] == "s3"}
    assert s3 == {"nars_3", "nars_5", "nars_6"}
    assert all(v["reversed"] is (k in s3) for k, v in builtin["keys"].items())


def test_placeholder_nars_logs_draft(study: Path) -> None:
    result = _cli("--yes", "--study", str(study))
    assert result.exit_code == 0
    assert "draft_instrument: fidelity_nars" in result.stderr


# --------------------------------------------------------------------------- acceptance


def test_concurrent_dispatcher_is_study_busy(study: Path) -> None:
    before = _board_rows(study)
    with acquire_lease(study), pytest.raises(ConsortiumError) as info:
        screen_personas(study, yes=True)
    assert info.value.code == "study_busy"
    assert _board_rows(study) == before


def _pilot(study: Path) -> None:
    from types import SimpleNamespace

    from consortium.board.clips import insert_clip
    from consortium.board.db import connect, transaction
    from consortium.stages.push import push_test

    conn = connect(study)
    try:
        with transaction(conn):
            info = SimpleNamespace(duration_s=2.0, size_bytes=1000, width=640, height=480,
                                   fps=25.0, loudness_lufs=-23.0)
            insert_clip(conn, "c_aaaaaaaa", "1" * 64, info)
    finally:
        conn.close()
    src = study.parent / "pilot1.yaml"
    src.write_text(yaml.safe_dump({
        "schema_version": 1, "test": "pilot1", "kind": "pilot", "instruments": ["godspeed"],
        "clips": ["c_aaaaaaaa"], "session": {"practice_clips": 0, "repeats": 1}}))
    push_test(study, src)


def test_open_or_screen_during_screening_is_study_busy(study: Path) -> None:
    from consortium.stages.open import open_test

    _pilot(study)
    seen: list[str] = []

    def spy(op: str, key: tuple) -> None:
        if op == "insert_plan" and not seen:
            for attempt in (lambda: screen_personas(study, yes=True),
                            lambda: open_test(study, "pilot1", dry_run=False, yes=True)):
                with pytest.raises(ConsortiumError) as info:
                    attempt()
                seen.append(info.value.code)

    assert screen_personas(study, yes=True, writer_spy=spy).complete
    assert seen == ["study_busy", "study_busy"]
    assert _q(study, "SELECT count(*) FROM trials WHERE test = 'pilot1'") == [(0,)]


def test_panel_in_use_after_screening(study: Path) -> None:
    assert _cli("--yes", "--study", str(study)).exit_code == 0
    with pytest.raises(ConsortiumError) as info:
        personas_stage.generate(study, force=True)
    assert info.value.code == "panel_in_use"


def test_repeated_screening_is_deterministic(tmp_path: Path) -> None:
    def outcome(path: Path) -> list[tuple]:
        study = _make(path, m1="random", m2="faithful")
        summary = screen_personas(study, yes=True)
        return [(r["agent_id"], r["score"], r["outcome"], r["detail"], r["settings_hash"],
                 r["instrument_hash"]) for r in summary.results or []]

    first, second = outcome(tmp_path / "a"), outcome(tmp_path / "b")
    assert first == second
    assert {o for _, _, o, *_ in first} == {"pass", "fail"}


def test_fidelity_repeats(study: Path) -> None:
    _edit_yaml(study / "study.yaml",
               lambda d: d.__setitem__("screening", {"fidelity_repeats": 2}))
    summary = screen_personas(study, yes=True)
    assert summary.run.sessions == 2 * 2 * PERSONAS
    detail = json.loads(summary.results[0]["detail"])
    assert detail["checks"]["openness"]["n"] == 4  # 2 items x 2 repeats


def test_older_board_is_migrated(study: Path) -> None:
    from consortium.board.db import connect

    connect(study).close()
    raw = sqlite3.connect(study / DB_FILE, isolation_level=None)
    raw.execute("DROP TABLE screening_results")
    raw.execute("DROP TABLE screening_runs")
    raw.execute("PRAGMA user_version = 5")
    raw.close()
    assert screen_personas(study, yes=True).complete


# --------------------------------------------------------------------------- review fixes


def test_four_of_five_on_band_less_panel_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    study = _make(tmp_path / "s", bands=[])
    real = FakeRater.answer

    def answer(self, call):  # openness answered at the midpoint: 4 of 5 checks match
        out = json.loads(real(self, call))
        for item in ("bfi_5", "bfi_10"):
            if item in out:
                out[item] = 3
        return json.dumps(out)

    monkeypatch.setattr(FakeRater, "answer", answer)
    summary = screen_personas(study, yes=True)
    m1 = [r for r in summary.results or [] if r["model_id"] == "m1"]
    assert m1 and all(r["score"] == 0.8 and r["outcome"] == "pass" for r in m1)
    assert all(json.loads(r["detail"])["total"] == 5 for r in m1)


def _paused_run(study: Path) -> None:
    _priced(study)
    assert _cli("--yes", "--ceiling", "1", "--study", str(study)).exit_code == 1
    assert _runs(study)[0]["status"] == "open"


def test_resume_after_instrument_change_is_test_changed(study: Path) -> None:
    _paused_run(study)
    _user_nars(study)
    before = _board_rows(study)
    result = _cli("--yes", "--resume", "--ceiling", "1000000", "--study", str(study))
    assert result.exit_code == 1 and result.stderr.startswith("test_changed:")
    assert _board_rows(study) == before


def test_resume_after_settings_change_is_test_changed(study: Path) -> None:
    _paused_run(study)
    _edit_yaml(study / "study.yaml",
               lambda d: d["models"][0]["settings"].__setitem__("temperature", 0.2))
    before = _board_rows(study)
    result = _cli("--yes", "--resume", "--ceiling", "1000000", "--study", str(study))
    assert result.exit_code == 1
    assert result.stderr.splitlines()[-1].startswith("test_changed: Model settings changed")
    assert "m1" in result.stderr
    assert _board_rows(study) == before


def test_settings_stamp_recorded_at_creation(study: Path) -> None:
    cfg = load_study(study)
    _paused_run(study)
    recorded = _runs(study)[0]["settings_hashes"]
    resumed = screen_personas(study, yes=True, resume=True, ceiling="1000000")
    priced = load_study(study)  # _priced changed fake usage, so the stamp moved with it
    assert recorded == {m.id: settings_hash(m) for m in priced.models}
    assert recorded != {m.id: settings_hash(m) for m in cfg.models}
    assert {r["settings_hash"] for r in resumed.results or []} == set(recorded.values())


def test_resume_scores_a_terminal_unscored_run(study: Path) -> None:
    def spy(op: str, key: tuple) -> None:
        if op == "finish":
            raise ConsortiumError("run_failed", "killed before scoring")

    with pytest.raises(ConsortiumError):
        screen_personas(study, yes=True, writer_spy=spy)
    assert _runs(study)[0]["status"] == "open"
    assert _q(study, "SELECT count(*) FROM trials WHERE state = 'valid'") == [(4 * PERSONAS,)]
    summary = screen_personas(study, yes=True, resume=True)
    assert summary.complete and len(summary.results or []) == 2 * PERSONAS
    assert len({r["agent_id"] for r in summary.results or []}) == 2 * PERSONAS


def test_abandon_then_fresh_run(study: Path) -> None:
    result = _cli("--abandon", "--study", str(study))
    assert result.exit_code == 1 and result.stderr.startswith("screening_not_open:")
    _paused_run(study)
    trials = _q(study, "SELECT count(*) FROM trials")
    result = _cli("--abandon", "--study", str(study))
    assert result.exit_code == 0, result.stderr
    assert result.stdout == "screening run: s1 (fidelity) abandoned\n"
    assert _runs(study)[0]["status"] == "abandoned"
    assert _q(study, "SELECT count(*) FROM trials") == trials
    assert _q(study, "SELECT count(*) FROM screening_results") == [(0,)]
    assert _cli("--yes", "--resume", "--study", str(study)).stderr.startswith(
        "screening_not_open:")
    fresh = _cli("--yes", "--ceiling", "1000000", "--study", str(study))
    assert fresh.exit_code == 0, fresh.stderr
    assert "screening run: s2 (fidelity) complete" in fresh.stdout
    assert {r["run_id"]: r["status"] for r in _runs(study)} == {
        "s1": "abandoned", "s2": "complete"}
    assert {r["run_id"] for r in read_only(study, current_results)} == {"s2"}


def test_abandon_is_study_busy_under_lease(study: Path) -> None:
    _paused_run(study)
    with acquire_lease(study):
        result = _cli("--abandon", "--study", str(study))
    assert result.stderr.startswith("study_busy:")
    assert _runs(study)[0]["status"] == "open"


def test_draft_nars_with_real_model_is_refused(study: Path) -> None:
    def gemini(doc: dict) -> None:
        doc["models"][1] = {
            "id": "m2", "provider": "gemini", "model": "gemini-3-flash-preview",
            "max_output_tokens": 512,
            "limits": {"max_seconds": 600, "max_bytes": 2_000_000, "inline_base64": False},
        }

    _edit_yaml(study / "study.yaml", gemini)
    before = _board_rows(study)
    with pytest.raises(ConsortiumError) as info:
        screen_personas(study, yes=True)
    assert info.value.code == "draft_instrument_not_allowed"
    assert "m2" in info.value.message
    assert _board_rows(study) == before


def test_draft_flag_in_detail(study: Path) -> None:
    summary = screen_personas(study, yes=True)
    assert all(json.loads(r["detail"])["draft"] is True for r in summary.results or [])
    _user_nars(study)
    summary = screen_personas(study, yes=True)
    assert all(json.loads(r["detail"])["draft"] is False for r in summary.results or [])


def test_frame_and_panel_disagree_on_nars(study: Path, tmp_path: Path) -> None:
    _edit_yaml(study / "study.yaml", lambda d: d["personas"].__setitem__("nars_bands", []))
    with pytest.raises(ConsortiumError) as info:
        screen_personas(study, yes=True)
    assert info.value.code == "panel_frame_mismatch"

    bandless = _make(tmp_path / "b", bands=[])
    _edit_yaml(bandless / "study.yaml",
               lambda d: d["personas"].__setitem__("nars_bands", ["low", "high"]))
    with pytest.raises(ConsortiumError) as info:
        screen_personas(bandless, yes=True)
    assert info.value.code == "panel_frame_mismatch"


def test_mixed_nars_index_is_panel_invalid(study: Path) -> None:
    path = study / "panel" / "personas" / "index.json"
    index = json.loads(path.read_text())
    index[0]["nars"] = None
    path.write_text(json.dumps(index))
    with pytest.raises(ConsortiumError) as info:
        screen_personas(study, yes=True)
    assert info.value.code == "panel_invalid"
    assert "nars" in info.value.message


def test_nars_item_id_shared_with_bfi_is_config_invalid(study: Path) -> None:
    def clash(doc: dict) -> None:
        doc["items"][0]["id"] = "bfi_1"
        doc["keys"]["bfi_1"] = doc["keys"].pop("nars_1")

    _user_nars(study, change=clash)
    with pytest.raises(ConsortiumError) as info:
        screen_personas(study, yes=True)
    assert (info.value.code, info.value.path) == ("config_invalid", "study.yaml")
    assert "bfi_1" in info.value.message


def test_later_run_with_fewer_models_keeps_earlier_results(study: Path) -> None:
    assert screen_personas(study, yes=True).complete
    _edit_yaml(study / "study.yaml", lambda d: d.__setitem__("models", d["models"][:1]))
    _edit_yaml(study / "prices.yaml", lambda d: d["models"].pop("m2"))
    second = screen_personas(study, yes=True)
    assert second.superseded == []
    runs = {r["run_id"]: r for r in _runs(study)}
    assert runs["s1"]["superseded_by"] is None
    current = read_only(study, current_results)
    by_model = {(r["model_id"], r["run_id"]) for r in current}
    assert by_model == {("m1", "s2"), ("m2", "s1")}
    assert len(current) == 2 * PERSONAS
    # Once a later run covers m2 again, s1 is superseded.
    _setup(study)
    third = screen_personas(study, yes=True)
    assert third.superseded == ["s1", "s2"]
    assert {r["run_id"] for r in read_only(study, current_results)} == {"s3"}


def test_faithful_with_invalid_answers(study: Path) -> None:
    _edit_yaml(study / "study.yaml",
               lambda d: d["models"][0]["fake"].__setitem__("invalid_rate", 0.3))
    summary = screen_personas(study, yes=True)
    assert _q(study, "SELECT count(*) FROM attempts WHERE valid = 0")[0][0] > 0
    for row in summary.results or []:
        if row["model_id"] == "m1":
            detail = json.loads(row["detail"])
            assert row["outcome"] == "pass" or detail["insufficient_data"]


def test_unknown_nars_band_is_config_invalid(study: Path) -> None:
    _edit_yaml(study / "study.yaml",
               lambda d: d["personas"].__setitem__("nars_bands", ["medium"]))
    with pytest.raises(ConsortiumError) as info:
        load_study(study)
    assert info.value.code == "config_invalid"
    assert info.value.message.startswith("personas.nars_bands")


def test_dry_run_writes_nothing(study: Path) -> None:
    def snapshot() -> dict[str, bytes]:
        return {str(p.relative_to(study)): p.read_bytes() for p in study.rglob("*")
                if p.is_file()}

    before = snapshot()
    result = _cli("--dry-run", "--study", str(study))
    assert result.exit_code == 0, result.stderr
    out = result.stdout.splitlines()
    assert out[0] == "test: s1 (screening) dry run"
    assert any(line.startswith("requests sha256: ") for line in out)
    assert any(line.startswith("cost estimate: ") for line in out)
    assert out[-1] == "screening run: s1 (fidelity) dry run"
    assert snapshot() == before
    assert _cli("--dry-run", "--resume", "--study", str(study)).stderr.startswith("bad_option:")


def test_completed_run_with_overshoot_exits_zero(study: Path) -> None:
    def prices(doc: dict) -> None:
        for model in doc["models"].values():
            model["input_usd_per_mtok"] = "1"

    def usage(doc: dict) -> None:
        doc["concurrency"] = 1
        for model in doc["models"]:
            model["fake"]["input_tokens"] = 10_000

    _edit_yaml(study / "prices.yaml", prices)
    _edit_yaml(study / "study.yaml", usage)
    # Exactly enough for all but the last attempt's actual cost: the last one overshoots.
    from decimal import Decimal

    expected = screen_personas(study, dry_run=True).run.estimate.expected
    ceiling = Decimal("0.01") * (4 * PERSONAS) - Decimal("0.005")
    assert expected < ceiling
    result = _cli("--yes", "--ceiling", str(ceiling), "--study", str(study))
    assert "screening run: s1 (fidelity) complete" in result.stdout, result.stderr
    assert result.exit_code == 0
    assert "ceiling_overshoot" in result.stderr


@pytest.mark.parametrize(
    ("change", "match"),
    [
        (lambda d: d["keys"]["nars_1"].pop("subscale"), "subscale: required"),
        (lambda d: d["keys"].__setitem__("x1", {"construct": "openness", "reversed": False,
                                                 "subscale": "s1"}), "only allowed"),
        (lambda d: d["keys"].__setitem__("nope", {"construct": "openness", "reversed": False}),
         "keys.nope: not an item"),
        (lambda d: (d["items"].append({"id": "free", "type": "free_text", "text": "Why?"}),
                    d["keys"].__setitem__("free", {"construct": "openness",
                                                   "reversed": False})),
         "keys.free: only likert"),
        (lambda d: d["keys"].pop("nars_2"), "must key every item"),
    ],
)
def test_item_key_validation(change, match: str) -> None:
    from pydantic import ValidationError

    from consortium.config.models import InstrumentDef

    doc = yaml.safe_load((SRC / "instruments" / "fidelity_nars.yaml").read_text())
    InstrumentDef.model_validate(doc)
    change(doc)
    with pytest.raises(ValidationError, match=match):
        InstrumentDef.model_validate(doc)
