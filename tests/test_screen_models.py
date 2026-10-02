"""Story 3.2: perception screening (``consortium screen models TEST``), every matrix row."""

from __future__ import annotations

import csv
import itertools
import json
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from consortium.archive.jsonl import REQUESTS_FILE, read_lines
from consortium.board.clips import insert_clip
from consortium.board.db import DB_FILE, connect, read_only, transaction
from consortium.board.lease import acquire_lease
from consortium.board.screening import current_results, list_runs, run_results
from consortium.cli import app
from consortium.config.load import load_instruments, load_study
from consortium.core.errors import ConsortiumError
from consortium.core.hashes import instrument_hash, settings_hash
from consortium.core.perception import NEUTRAL_CARD
from consortium.raters.fake import FakeRater, fake_latent, likert_from_latent
from consortium.stages import personas as personas_stage
from consortium.stages.init import init_study
from consortium.stages.push import push_test
from consortium.stages.screen import screen_models

runner = CliRunner()
A, B, C, D, E = (f"c_{x * 8}" for x in "abcde")
CLIPS = [A, B, C, D, E]
SHA = {clip: f"{i + 1:064x}" for i, clip in enumerate(CLIPS)}


def _edit_yaml(path: Path, change) -> None:
    doc = yaml.safe_load(path.read_text())
    change(doc)
    path.write_text(yaml.safe_dump(doc, sort_keys=False))


def _setup(study: Path, m1: str = "faithful", m2: str = "unfaithful") -> None:
    def change(doc: dict[str, Any]) -> None:
        doc["personas"]["big_five"] = {"fraction": "1/4", "replicates": 1}
        assert "perception_cues" in doc["instruments"]  # enabled by the template
        first = doc["models"][0]
        first["fake"]["perception"] = m1
        second = json.loads(json.dumps(first))
        second["id"] = "m2"
        second["fake"]["perception"] = m2
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


def _higher(a: str, b: str, item: str) -> str:
    return a if fake_latent(SHA[a], item) > fake_latent(SHA[b], item) else b


def _bucket(clip: str, item: str, points: int) -> int:
    return likert_from_latent(fake_latent(SHA[clip], item), points)


def _godspeed_pair() -> tuple[list[str], str]:
    """A pair of target Clips whose faithful animacy_1 answers differ, and the higher one."""
    for a, b in itertools.combinations([A, B, C, D], 2):
        ba, bb = _bucket(a, "animacy_1", 5), _bucket(b, "animacy_1", 5)
        if ba != bb:
            return [a, b], a if ba > bb else b
    raise AssertionError("no pair with distinct animacy_1 answers")


def checks() -> list[dict[str, Any]]:
    pair, winner = _godspeed_pair()
    return [
        {"instrument": "pairwise_alive", "item": "alive", "clips": [A, B],
         "expected": _higher(A, B, "alive")},
        {"instrument": "pairwise_alive", "item": "alive", "clips": [C, D],
         "expected": _higher(C, D, "alive")},
        {"instrument": "godspeed", "item": "animacy_1", "clips": pair, "expected": winner},
        {"instrument": "perception_cues", "item": "moving", "clips": [A],
         "expected": _bucket(A, "moving", 2)},
        {"instrument": "perception_cues", "item": "speech", "clips": [B],
         "expected": _bucket(B, "speech", 2)},
    ]


def _push(study: Path, name: str, kind: str = "screening", **fields: Any) -> str:
    doc: dict[str, Any] = {
        "schema_version": 1, "test": name, "kind": kind,
        "instruments": ["pairwise_alive", "godspeed", "perception_cues"],
        "clips": [A, B, C, D], "session": {"practice_clips": 0, "repeats": 1},
    }
    doc.update(fields)
    src = study.parent / f"{name}.yaml"
    src.write_text(yaml.safe_dump(doc, sort_keys=False))
    return push_test(study, src)


def _make(path: Path, **kw: Any) -> Path:
    study = init_study(path)
    _setup(study, **kw)
    _push(study, "scr", checks=checks())
    return study


@pytest.fixture(scope="module")
def base(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _make(tmp_path_factory.mktemp("models") / "study")


@pytest.fixture()
def study(tmp_path: Path, base: Path) -> Path:
    copy = tmp_path / "copy"
    shutil.copytree(base, copy)
    return copy


def _cli(*args: str):
    return runner.invoke(app, ["screen", "models", *args])


def _q(study: Path, sql: str) -> list[tuple]:
    return read_only(study, lambda conn: conn.execute(sql).fetchall()) or []


def _runs(study: Path) -> list[dict]:
    return read_only(study, list_runs) or []


def _board_rows(study: Path) -> tuple:
    if not (study / DB_FILE).exists():
        return ()
    return tuple(_q(study, f"SELECT count(*) FROM {t}")[0][0]
                 for t in ("tests", "trials", "attempts", "screening_runs", "screening_results"))


def _results(study: Path, run: str = "s1") -> dict[tuple[str, str], dict]:
    rows = read_only(study, lambda conn: run_results(conn, run)) or []
    return {(r["model_id"], r["instrument"]): r for r in rows}


# --------------------------------------------------------------------------- matrix


def test_pass_fail_split_and_stamps(study: Path) -> None:
    result = _cli("scr", "--yes", "--study", str(study))
    assert result.exit_code == 0, result.stderr
    out = result.stdout.splitlines()
    assert out[0] == "test: s1 (screening)"
    assert "screening run: s1 (perception) complete" in out
    assert "coverage_gap" not in result.stdout + result.stderr  # no main or pilot Test
    results = _results(study)
    assert list(results) == [
        ("m1", "pairwise_alive"), ("m1", "godspeed"), ("m1", "perception_cues"),
        ("m2", "pairwise_alive"), ("m2", "godspeed"), ("m2", "perception_cues")]
    cfg = load_study(study)
    defs = load_instruments(study, cfg)
    pair_checks = {"pairwise_alive": 2, "godspeed": 1, "perception_cues": 0}
    units = {"pairwise_alive": 4, "godspeed": 1, "perception_cues": 2}
    for (model, name), row in results.items():
        assert row["agent_id"] is None
        assert row["pair_checks"] == pair_checks[name]
        assert row["threshold"] == 0.8
        assert row["settings_hash"] == settings_hash(cfg.model_by_id(model))
        assert row["instrument_hash"] == instrument_hash(defs[name])
        assert json.loads(row["detail"])["units"] == units[name]
        if model == "m1":
            assert (row["outcome"], row["score"]) == ("pass", 1.0)
        else:
            assert row["outcome"] == "fail"
            if pair_checks[name]:
                assert row["score"] == 0.0
    assert "perception m1 pairwise_alive: pass 1 (4/4), pair checks 2, threshold 0.8" in out
    assert "perception m2 godspeed: fail 0 (0/1), pair checks 1, threshold 0.8" in out
    run = _runs(study)[0]
    assert (run["kind"], run["screening_test"], run["status"]) == ("perception", "scr",
                                                                    "complete")
    assert run["settings_hashes"] == {m.id: settings_hash(m) for m in cfg.models}
    # The run's tests row copies the screening Test's path, sha256 and Clips; not openable.
    assert _q(study, "SELECT openable FROM tests WHERE name = 'scr'") == [(0,)]
    scr = _q(study, "SELECT path, sha256 FROM tests WHERE name = 'scr'")[0]
    assert _q(study, "SELECT kind, openable, path, sha256 FROM tests WHERE name = 's1'") == [
        ("screening", 0, *scr)]
    clips = "SELECT clip_id, role FROM test_clips WHERE test = '{}' ORDER BY clip_id"
    assert _q(study, clips.format("s1")) == _q(study, clips.format("scr"))


def test_only_needed_trials(study: Path) -> None:
    _push(study, "one", instruments=["pairwise_alive"], checks=[
        {"instrument": "pairwise_alive", "item": "alive", "clips": [A, B], "expected": A}])
    result = _cli("one", "--yes", "--study", str(study))
    assert result.exit_code == 0, result.stderr
    assert "trials per session: 2 = 2 (pairwise_alive 2)" in result.stdout.splitlines()
    assert _q(study, "SELECT count(*) FROM trials WHERE test = 's1'") == [(4,)]  # 2 Models


def test_trials_go_through_the_engine_with_the_neutral_card(study: Path) -> None:
    assert _cli("scr", "--yes", "--study", str(study)).exit_code == 0
    trials = _q(study, "SELECT trial_id, persona_id, agent_id, state FROM trials "
                       "WHERE test = 's1'")
    # 2 pairs x 2 positions + 4 godspeed/cues Clips (A, B + the godspeed pair) per Session.
    assert trials and {t[1] for t in trials} == {"p0"}
    assert {t[2] for t in trials} == {"p0-m1", "p0-m2"}
    assert {t[3] for t in trials} == {"valid"}
    lines, _ = read_lines(study, REQUESTS_FILE)
    assert sorted(line["trial_id"] for line in lines) == sorted(t[0] for t in trials)
    assert all(line["request"]["persona_card"] == NEUTRAL_CARD for line in lines)
    assert _q(study, "SELECT count(*) FROM attempts") == [(len(trials),)]
    assert _q(study, "SELECT count(*) FROM ledger")[0][0] >= len(trials)


def _pilot(study: Path) -> None:
    _push(study, "pilot1", kind="pilot", instruments=["godspeed", "presence"], clips=[E])


def test_gap_is_reported_and_the_run_proceeds(study: Path) -> None:
    _pilot(study)
    result = _cli("scr", "--yes", "--study", str(study))
    assert result.exit_code == 0, result.stderr
    assert "coverage_gap: presence (tests: pilot1)" in result.stderr.splitlines()
    assert result.stdout.splitlines()[-1] == "coverage_gap: presence (tests: pilot1)"
    assert _runs(study)[0]["status"] == "complete"

    # A current result of another screening Test with a presence pair check covers it.
    _push(study, "scr2", instruments=["presence"], clips=[A, E], checks=[
        {"instrument": "presence", "item": "presence_1", "clips": [A, E], "expected": A}])
    assert _cli("scr2", "--yes", "--study", str(study)).exit_code == 0
    again = _cli("scr", "--yes", "--study", str(study))
    assert again.exit_code == 0, again.stderr
    assert "coverage_gap" not in again.stderr + again.stdout


def test_gap_is_written_before_confirmation(study: Path) -> None:
    _pilot(study)
    seen: list[str] = []

    def confirm(prompt: str) -> bool:
        seen.append("confirm")
        return False

    with pytest.raises(ConsortiumError) as info:
        screen_models(study, "scr", confirm=confirm, warn=lambda line: seen.append(line))
    assert info.value.code == "not_confirmed"
    assert seen == ["coverage_gap: presence (tests: pilot1)", "confirm"]


def test_second_run_supersedes_and_keeps(study: Path) -> None:
    assert _cli("scr", "--yes", "--study", str(study)).exit_code == 0
    first = _results(study, "s1")
    result = _cli("scr", "--yes", "--study", str(study))
    assert result.exit_code == 0, result.stderr
    assert result.stdout.splitlines()[-1] == "superseded: s1"
    runs = {r["run_id"]: r for r in _runs(study)}
    assert runs["s1"]["superseded_by"] == "s2" and runs["s1"]["status"] == "complete"
    assert _results(study, "s1") == first
    current = read_only(study, lambda conn: current_results(conn, "perception"))
    assert {r["run_id"] for r in current} == {"s2"} and len(current) == 6


def test_second_run_of_another_test_does_not_supersede(study: Path) -> None:
    assert _cli("scr", "--yes", "--study", str(study)).exit_code == 0
    _push(study, "one", instruments=["pairwise_alive"], checks=[
        {"instrument": "pairwise_alive", "item": "alive", "clips": [A, B], "expected": A}])
    assert _cli("one", "--yes", "--study", str(study)).exit_code == 0
    assert {r["run_id"]: r["superseded_by"] for r in _runs(study)} == {"s1": None, "s2": None}


@pytest.mark.parametrize(("check", "fragment"), [
    ({"instrument": "pairwise_alive", "item": "alive", "clips": [A, B], "expected": C},
     "checks.0.expected: must be one of the check's clips"),
    ({"instrument": "godspeed", "item": "alive", "clips": [A], "expected": 3},
     "checks.0.item: 'alive' is not an item of godspeed"),
])
def test_bad_check_is_refused_by_push(study: Path, check: dict, fragment: str) -> None:
    src = study.parent / "bad.yaml"
    src.write_text(yaml.safe_dump({
        "schema_version": 1, "test": "bad", "kind": "screening",
        "instruments": ["pairwise_alive", "godspeed"], "clips": [A, B, C],
        "session": {"practice_clips": 0}, "checks": [check]}))
    result = runner.invoke(app, ["push", "test", str(src), "--study", str(study)])
    assert result.exit_code == 1
    assert result.stderr.startswith("config_invalid:") and fragment in result.stderr


def test_checks_on_a_pilot_are_refused_by_push(study: Path) -> None:
    src = study.parent / "pilot9.yaml"
    src.write_text(yaml.safe_dump({
        "schema_version": 1, "test": "pilot9", "kind": "pilot", "instruments": ["godspeed"],
        "clips": [E], "session": {"practice_clips": 0},
        "checks": [{"instrument": "godspeed", "item": "animacy_1", "clips": [E],
                    "expected": 3}]}))
    result = runner.invoke(app, ["push", "test", str(src), "--study", str(study)])
    assert result.exit_code == 1
    assert result.stderr.startswith("config_invalid:")
    assert "checks: only allowed in a kind: screening Test" in result.stderr


def test_wrong_test_is_refused(study: Path) -> None:
    _pilot(study)
    _push(study, "nochecks")
    before = _board_rows(study)
    for name, code in (("pilot1", "not_a_screening_test"), ("nochecks", "no_screening_checks"),
                       ("nope", "unknown_test")):
        result = _cli(name, "--yes", "--study", str(study))
        assert result.exit_code == 1
        assert result.stderr.startswith(f"{code}:"), result.stderr
        assert _board_rows(study) == before
    assert _cli("scr", "--yes", "--study", str(study)).exit_code == 0
    result = _cli("s1", "--yes", "--study", str(study))
    assert result.stderr.startswith("not_a_screening_test:")


def test_invalid_position_trial_fails_its_unit(
    study: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = FakeRater.answer
    pair = tuple(sorted([A, B]))[::-1]  # position 2 of the (A, B) check

    def answer(self, call):
        clips = tuple(c.clip_id for c in call.request.clips)
        if self.perception == "faithful" and clips == pair:
            return "not json"
        return real(self, call)

    monkeypatch.setattr(FakeRater, "answer", answer)
    summary = screen_models(study, "scr", yes=True)
    assert summary.complete
    results = _results(study)
    row = results[("m1", "pairwise_alive")]
    assert json.loads(row["detail"])["passed"] == 3
    assert (row["outcome"], row["score"]) == ("fail", 0.75)
    assert results[("m1", "godspeed")]["outcome"] == "pass"
    assert _q(study, "SELECT count(*) FROM trials WHERE test = 's1' AND state = 'invalid'") == [
        (1,)]


def test_nothing_to_resume(study: Path) -> None:
    result = _cli("scr", "--yes", "--resume", "--study", str(study))
    assert result.exit_code == 1
    assert result.stderr.startswith("screening_not_open:")
    assert _cli("scr", "--yes", "--study", str(study)).exit_code == 0
    assert _cli("scr", "--yes", "--resume", "--study", str(study)).stderr.startswith(
        "screening_not_open:")


@pytest.mark.parametrize("panel", [False, True])
def test_status_skips_p0(study: Path, panel: bool) -> None:
    if panel:
        personas_stage.generate(study)
    assert _cli("scr", "--yes", "--study", str(study)).exit_code == 0
    result = runner.invoke(app, ["status", "--json", "--study", str(study)])
    assert result.exit_code == 0, result.stderr
    tallies = json.loads(result.stdout)["by_persona_attribute"]
    assert tallies is not None and "s1" in tallies
    assert "p0" not in json.dumps(tallies)
    assert all(n == 0 for value in tallies["s1"].values() for counts in value.values()
               for n in counts.values())


# --------------------------------------------------------------------------- acceptance


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
    result = _cli("scr", "--yes", "--ceiling", "1", "--study", str(study))
    assert result.exit_code == 1
    assert "consortium screen models scr --resume" in result.stderr
    assert "screening run: s1 (perception) open (no results yet)" in result.stdout
    before = _board_rows(study)
    again = _cli("scr", "--yes", "--ceiling", "1000000", "--study", str(study))
    assert again.stderr.startswith("screening_run_open:")
    assert _board_rows(study) == before
    resumed = _cli("scr", "--yes", "--resume", "--ceiling", "1000000", "--study", str(study))
    assert resumed.exit_code == 0, resumed.stderr
    assert "screening run: s1 (perception) complete" in resumed.stdout
    assert len(_results(study)) == 6


def test_resume_after_settings_change_is_test_changed(study: Path) -> None:
    _priced(study)
    assert _cli("scr", "--yes", "--ceiling", "1", "--study", str(study)).exit_code == 1
    _edit_yaml(study / "study.yaml",
               lambda d: d["models"][1]["fake"].__setitem__("perception", "faithful"))
    before = _board_rows(study)
    result = _cli("scr", "--yes", "--resume", "--ceiling", "1000000", "--study", str(study))
    assert result.exit_code == 1 and result.stderr.startswith("test_changed:")
    assert _board_rows(study) == before


def _user_instrument(study: Path, name: str, pairwise: bool = False) -> Path:
    """A user Instrument ``name`` (a 7-point Likert ``feel``, or a pairwise ``alive``),
    enabled in study.yaml."""
    item = ({"id": "alive", "type": "pairwise", "text": "Which one feels more alive?"}
            if pairwise else
            {"id": "feel", "type": "likert", "text": "How alive?", "points": 7,
             "anchors": {"low": "Dead", "high": "Alive"}})
    doc = {"schema_version": 1, "name": name, "version": "1", "instructions": "Rate it.",
           "prompt_variants": {"default": "Answer the question."}, "items": [item]}
    path = study / "instruments" / f"{name}.yaml"
    path.parent.mkdir(exist_ok=True)
    path.write_text(yaml.safe_dump(doc, sort_keys=False))
    _edit_yaml(study / "study.yaml", lambda d: d["instruments"].append(name))
    return path


def _usr(study: Path) -> Path:
    path = _user_instrument(study, "alive_user", pairwise=True)
    _push(study, "usr", instruments=["alive_user"], checks=[
        {"instrument": "alive_user", "item": "alive", "clips": [A, B],
         "expected": _higher(A, B, "alive")}])
    return path


def test_resume_after_instrument_change_is_test_changed(study: Path) -> None:
    path = _usr(study)
    _priced(study)
    assert _cli("usr", "--yes", "--ceiling", "1", "--study", str(study)).exit_code == 1
    _edit_yaml(path, lambda d: d["prompt_variants"].__setitem__("default", "Pick one."))
    before = _board_rows(study)
    result = _cli("usr", "--yes", "--resume", "--ceiling", "1000000", "--study", str(study))
    assert result.exit_code == 1 and result.stderr.startswith("test_changed:")
    assert _board_rows(study) == before


@pytest.mark.parametrize("edit", ["settings", "instrument"])
def test_change_during_confirmation_is_test_changed(study: Path, edit: str) -> None:
    path = _usr(study)

    def confirm(prompt: str) -> bool:
        if edit == "settings":
            _edit_yaml(study / "study.yaml",
                       lambda d: d["models"][0]["fake"].__setitem__("input_tokens", 1))
        else:  # not shown to the Model: only the Instrument stamp changes
            _edit_yaml(path, lambda d: d.__setitem__("version", "2"))
        return True

    before = _board_rows(study)
    with pytest.raises(ConsortiumError) as info:
        screen_models(study, "usr", confirm=confirm)
    assert info.value.code == "test_changed"
    assert _board_rows(study) == before


def test_low_level_only_never_covers(study: Path) -> None:
    _push(study, "pilot2", kind="pilot", instruments=["perception_cues"], clips=[E])
    gap = "coverage_gap: perception_cues (tests: pilot2)"
    for _ in range(2):  # before and after scr's own (low-level only) result exists
        result = _cli("scr", "--yes", "--study", str(study))
        assert result.exit_code == 0, result.stderr
        assert gap in result.stderr.splitlines()
    _push(study, "cues", instruments=["perception_cues"], clips=[A], checks=[
        {"instrument": "perception_cues", "item": "moving", "clips": [A],
         "expected": _bucket(A, "moving", 2)}])
    assert _cli("cues", "--yes", "--study", str(study)).exit_code == 0
    assert gap in _cli("scr", "--yes", "--study", str(study)).stderr.splitlines()


def test_two_screening_tests_on_one_instrument_no_gap(study: Path) -> None:
    pair, winner = _godspeed_pair()
    _push(study, "pilot3", kind="pilot", instruments=["godspeed"], clips=[E])
    _push(study, "gpair", instruments=["godspeed"], checks=[
        {"instrument": "godspeed", "item": "animacy_1", "clips": pair, "expected": winner}])
    _push(study, "glow", instruments=["godspeed"], clips=[A], checks=[
        {"instrument": "godspeed", "item": "animacy_1", "clips": [A],
         "expected": _bucket(A, "animacy_1", 5)}])
    assert _cli("gpair", "--yes", "--study", str(study)).exit_code == 0
    result = _cli("glow", "--yes", "--study", str(study))  # a later, low-level only run
    assert result.exit_code == 0, result.stderr
    assert "coverage_gap" not in result.stderr + result.stdout


def test_stale_covering_result_is_a_gap(study: Path) -> None:
    path = _user_instrument(study, "feel")
    _push(study, "pilot4", kind="pilot", instruments=["feel"], clips=[E])
    _push(study, "sfeel", instruments=["feel"], checks=[
        {"instrument": "feel", "item": "feel", "clips": [A, B],
         "expected": _higher(A, B, "feel")}])
    assert _cli("sfeel", "--yes", "--study", str(study)).exit_code == 0
    assert "coverage_gap" not in _cli("scr", "--yes", "--study", str(study)).stderr
    _edit_yaml(path, lambda d: d.__setitem__("version", "2"))
    result = _cli("scr", "--yes", "--study", str(study))
    assert result.exit_code == 0, result.stderr
    assert "coverage_gap: feel (tests: pilot4)" in result.stderr.splitlines()


def test_unreadable_pilot_is_coverage_unknown(study: Path) -> None:
    _pilot(study)
    (study / "tests" / "pilot1.yaml").write_text("{not yaml: [")
    result = _cli("scr", "--yes", "--study", str(study))
    assert result.exit_code == 0, result.stderr
    assert "coverage_unknown: pilot1" in result.stderr.splitlines()


def test_lower_threshold_passes_three_of_four(
    study: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _edit_yaml(study / "study.yaml",
               lambda d: d["thresholds"].__setitem__("perception_min", 0.7))
    real = FakeRater.answer
    pair = tuple(sorted([A, B]))[::-1]

    def answer(self, call):
        clips = tuple(c.clip_id for c in call.request.clips)
        if self.perception == "faithful" and clips == pair:
            return "not json"
        return real(self, call)

    monkeypatch.setattr(FakeRater, "answer", answer)
    assert screen_models(study, "scr", yes=True).complete
    row = _results(study)[("m1", "pairwise_alive")]
    assert (row["outcome"], row["score"], row["threshold"]) == ("pass", 0.75, 0.7)


def test_abandon_then_fresh_run(study: Path) -> None:
    result = _cli("scr", "--abandon", "--study", str(study))
    assert result.exit_code == 1 and result.stderr.startswith("screening_not_open:")
    _priced(study)
    assert _cli("scr", "--yes", "--ceiling", "1", "--study", str(study)).exit_code == 1
    result = _cli("scr", "--abandon", "--study", str(study))
    assert result.exit_code == 0, result.stderr
    assert result.stdout.strip() == "screening run: s1 (perception) abandoned"
    assert _runs(study)[0]["status"] == "abandoned"
    assert _q(study, "SELECT count(*) FROM screening_results") == [(0,)]
    fresh = _cli("scr", "--yes", "--ceiling", "1000000", "--study", str(study))
    assert fresh.exit_code == 0, fresh.stderr
    assert "screening run: s2 (perception) complete" in fresh.stdout
    both = _cli("scr", "--abandon", "--resume", "--study", str(study))
    assert both.exit_code == 1 and both.stderr.startswith("bad_option:")


def test_personas_generate_allowed_after_perception_runs(study: Path) -> None:
    assert _cli("scr", "--yes", "--study", str(study)).exit_code == 0
    personas_stage.generate(study)
    assert (study / "panel" / "personas" / "index.json").exists()


def test_open_refuses_a_screening_test(study: Path) -> None:
    for args in (["--dry-run"], ["--yes"]):
        result = runner.invoke(app, ["open", "scr", *args, "--study", str(study)])
        assert result.exit_code == 1
        assert result.stderr.startswith("screening_test_not_openable:")


def test_concurrent_dispatcher_is_study_busy(study: Path) -> None:
    before = _board_rows(study)
    with acquire_lease(study), pytest.raises(ConsortiumError) as info:
        screen_models(study, "scr", yes=True)
    assert info.value.code == "study_busy"
    assert _board_rows(study) == before


def test_pilot_export_has_no_screening_row(study: Path) -> None:
    _pilot(study)
    assert _cli("scr", "--yes", "--study", str(study)).exit_code == 0
    personas_stage.generate(study)  # perception (p0) Trials do not freeze the Panel
    opened = runner.invoke(app, ["open", "pilot1", "--yes", "--study", str(study)])
    assert opened.exit_code == 0, opened.stderr
    exported = runner.invoke(app, ["export", "pilot1", "--study", str(study)])
    assert exported.exit_code == 0, exported.stderr
    with (study / "exports" / "pilot1.csv").open(newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert rows
    assert all("s1" not in json.dumps(row) and "p0" not in row.get("agent_id", "")
               for row in rows)
    result = runner.invoke(app, ["export", "s1", "--study", str(study)])
    assert result.stderr.startswith("screening_not_exportable:")
    result = runner.invoke(app, ["export", "scr", "--study", str(study)])
    assert result.stderr.startswith("screening_not_exportable:")


def test_repeated_run_in_a_fresh_study_is_identical(tmp_path: Path) -> None:
    def outcome(path: Path) -> list[tuple]:
        study = _make(path, m1="random", m2="faithful")
        summary = screen_models(study, "scr", yes=True)
        return [(r["model_id"], r["instrument"], r["score"], r["outcome"], r["detail"],
                 r["settings_hash"], r["instrument_hash"], r["pair_checks"])
                for r in summary.results or []]

    first, second = outcome(tmp_path / "a"), outcome(tmp_path / "b")
    assert first == second and len(first) == 6
