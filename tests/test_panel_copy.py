"""Story 3.4: ``consortium panel copy --from SOURCE``, one test per I/O matrix row (Fake
rater), plus the acceptance criteria."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from consortium.board import screening as board_screening
from consortium.board.db import DB_FILE, read_only
from consortium.cli import app
from consortium.config.load import PERSONAS_DIR
from consortium.core.errors import ConsortiumError
from consortium.stages import panel_copy as stage
from consortium.stages import personas as personas_stage
from consortium.stages.init import init_study
from consortium.stages.open import open_test
from consortium.stages.panel_copy import copy_panel
from consortium.stages.screen import screen_models, screen_personas
from test_open_gate import (
    AGENTS,
    MODELS,
    PERSONAS,
    _edit_yaml,
    _push,
    _seed_fidelity,
    _seed_perception,
    _set_temperature,
    _setup,
)
from test_screen_models import checks as perception_checks

runner = CliRunner()


@pytest.fixture(scope="module")
def empty(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Two Models (m1 faithful, m2 unfaithful), Clips and ``pilot1``; no Panel."""
    study = init_study(tmp_path_factory.mktemp("copy") / "study")
    _setup(study)
    _push(study, "pilot1", "pilot")
    return study


@pytest.fixture(scope="module")
def screened(empty: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """``empty`` plus a Panel, a fidelity run s1 and a perception run s2 (Fake rater)."""
    study = tmp_path_factory.mktemp("screened") / "study"
    shutil.copytree(empty, study)
    personas_stage.generate(study)
    _push(study, "scr", "screening", checks=perception_checks(),
          instruments=["pairwise_alive", "godspeed", "perception_cues"])
    screen_personas(study, yes=True)
    screen_models(study, "scr", yes=True, warn=lambda _line: None)
    return study


@pytest.fixture()
def target(tmp_path: Path, empty: Path) -> Path:
    shutil.copytree(empty, tmp_path / "target")
    return tmp_path / "target"


@pytest.fixture()
def source(tmp_path: Path, screened: Path) -> Path:
    shutil.copytree(screened, tmp_path / "source")
    return tmp_path / "source"


@pytest.fixture()
def plain(tmp_path: Path, empty: Path) -> Path:
    """A source with a Panel and no screening run."""
    shutil.copytree(empty, tmp_path / "plain")
    personas_stage.generate(tmp_path / "plain")
    return tmp_path / "plain"


def _cli(target: Path, source: Path):
    return runner.invoke(app, ["panel", "copy", "--from", str(source), "--study", str(target)])


def _tree(root: Path) -> dict[str, tuple[bytes, int]]:
    return {p.relative_to(root).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in root.rglob("*") if p.is_file()}


def _files(root: Path) -> dict[str, bytes]:
    return {k: v for k, (v, _) in _tree(root).items()}


def _results(study: Path) -> list[dict[str, Any]]:
    def read(conn: sqlite3.Connection) -> list[dict[str, Any]]:
        return [row for run in board_screening.list_runs(conn)
                for row in board_screening.run_results(conn, run["run_id"])]
    return read_only(study, read, allow_older=True) or []


def _runs(study: Path) -> list[dict[str, Any]]:
    return read_only(study, board_screening.list_runs) or []


def _refused(target: Path, source: Path, code: str) -> ConsortiumError:
    before = _tree(target)
    with pytest.raises(ConsortiumError) as info:
        copy_panel(target, source)
    assert info.value.code == code, info.value
    assert _tree(target) == before
    return info.value


# --------------------------------------------------------------------------- matrix


def test_happy_path(target: Path, source: Path) -> None:
    before = _tree(source)
    side = {p.name for p in source.iterdir()}
    result = _cli(target, source)
    assert result.exit_code == 0, result.stderr
    assert result.stdout == (
        f"copied {PERSONAS} personas and {len(_results(source))} screening results (2 runs) "
        "from ../source -> panel/personas\n"
    )
    assert result.stderr == ""
    assert _files(target / PERSONAS_DIR) == _files(source / PERSONAS_DIR)
    # The source is only read: same bytes and mtimes, no -wal/-shm or lease file appeared.
    assert _tree(source) == before
    assert {p.name for p in source.iterdir()} == side

    src_runs, runs = _runs(source), _runs(target)
    assert [r["run_id"] for r in runs] == ["s1", "s2"]
    assert [{**r, "run_id": None} for r in runs] == [{**r, "run_id": None} for r in src_runs]
    imported = _results(target)
    assert [{k: v for k, v in r.items() if not k.startswith("source_")} for r in imported] == [
        {k: v for k, v in r.items() if not k.startswith("source_")} for r in _results(source)]
    assert {(r["source_study"], r["source_hash"]) for r in imported} == {
        ("../source", imported[0]["source_hash"])}
    # Acceptance: the hash recomputes from the target's Panel files and imported rows.
    data = {
        "panel": {name: hashlib.sha256(raw).hexdigest()
                  for name, raw in _files(target / PERSONAS_DIR).items()},
        "results": [{k: v for k, v in r.items() if not k.startswith("source_")}
                    for r in imported],
    }
    text = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert imported[0]["source_hash"] == hashlib.sha256(text.encode()).hexdigest()
    # No Trials, Tests, ledger or Blinding key came along.
    assert read_only(target, lambda c: c.execute("SELECT count(*) FROM trials").fetchone()) \
        == (0,)
    assert not (target / "blinding_key.csv").exists()
    assert not list((target / "panel").glob(".personas-*"))


def test_superseded_run_is_not_imported(target: Path, plain: Path) -> None:
    _seed_fidelity(plain, {"p1-m1": "fail"})
    second = _seed_fidelity(plain)
    assert [r["superseded_by"] for r in _runs(plain)] == ["s2", None]
    summary = copy_panel(target, plain)
    assert (summary.runs, summary.results) == (1, PERSONAS * len(MODELS))
    runs = _runs(target)
    assert [(r["run_id"], r["superseded_by"]) for r in runs] == [("s1", None)]
    src = [r for r in _results(plain) if r["run_id"] == second]
    assert [r | {"run_id": "s1", "source_study": "../plain"} for r in src] == [
        r | {"source_hash": None} for r in _results(target)]


def test_unscreened_source_warns(target: Path, plain: Path) -> None:
    result = _cli(target, plain)
    assert result.exit_code == 0, result.stderr
    assert result.stdout == (
        f"copied {PERSONAS} personas and 0 screening results (0 runs) "
        "from ../plain -> panel/personas\n"
    )
    assert result.stderr.startswith("no_screening_results: ../plain has no current ")
    assert _files(target / PERSONAS_DIR) == _files(plain / PERSONAS_DIR)
    assert _results(target) == []


def test_pre_epic3_source_board(target: Path, plain: Path) -> None:
    (plain / DB_FILE).unlink()
    conn = sqlite3.connect(plain / DB_FILE)
    conn.execute("CREATE TABLE clips (clip_id TEXT PRIMARY KEY)")
    conn.execute("PRAGMA user_version = 5")
    conn.commit()
    conn.close()
    before = _tree(plain)
    summary = copy_panel(target, plain)
    assert (summary.runs, summary.results) == (0, 0)
    assert _tree(plain) == before


def test_newer_source_board_is_refused(target: Path, plain: Path) -> None:
    conn = sqlite3.connect(plain / DB_FILE)
    conn.execute("PRAGMA user_version = 99")
    conn.close()
    stale = target / "panel" / ".personas-new-crashed"
    stale.mkdir(parents=True)  # not swept: the source is checked first
    err = _refused(target, plain, "board_version_mismatch")
    assert stale.is_dir()
    assert err.message.startswith("source ../plain: ")
    assert not (target / PERSONAS_DIR).exists()


def test_target_has_a_panel(target: Path, source: Path) -> None:
    personas_stage.generate(target)
    _refused(target, source, "panel_exists")


def test_only_panel_files_are_copied(target: Path, plain: Path) -> None:
    (plain / PERSONAS_DIR / "notes.txt").write_text("stray")
    copy_panel(target, plain)
    assert not (target / PERSONAS_DIR / "notes.txt").exists()


def test_stale_work_folders_are_swept(target: Path, plain: Path) -> None:
    stale = target / "panel" / ".personas-new-crashed"
    stale.mkdir(parents=True)
    (stale / "p1.md").write_text("x")
    copy_panel(target, plain)
    assert sorted(p.name for p in (target / "panel").iterdir()) == ["personas"]


def test_target_with_a_trial_is_in_use(target: Path, source: Path) -> None:
    personas_stage.generate(target)
    open_test(target, "pilot1", dry_run=False, yes=True)
    shutil.rmtree(target / "panel")
    _refused(target, source, "panel_in_use")


def test_target_with_a_screening_result_is_in_use(target: Path, source: Path) -> None:
    personas_stage.generate(target)
    _seed_perception(target)  # results only, no Trials
    shutil.rmtree(target / "panel")
    _refused(target, source, "panel_in_use")


def test_frame_differs(target: Path, source: Path) -> None:
    _edit_yaml(target / "study.yaml",
               lambda doc: doc["personas"]["quotas"].__setitem__("gender", ["woman"]))
    _refused(target, source, "panel_frame_mismatch")


def test_first_error_wins(target: Path, source: Path) -> None:
    personas_stage.generate(target)
    _seed_perception(target)  # in use and a Panel present: in use is checked first
    _refused(target, source, "panel_in_use")
    _edit_yaml(target / "study.yaml",  # and a frame mismatch comes before both
               lambda doc: doc["personas"]["quotas"].__setitem__("gender", ["woman"]))
    _refused(target, source, "panel_frame_mismatch")


def test_invalid_source_panel(target: Path, source: Path) -> None:
    (source / PERSONAS_DIR / "index.json").write_text("[]")
    err = _refused(target, source, "panel_invalid")
    assert err.path == "../source/panel/personas/index.json"


def test_seed_may_differ(target: Path, source: Path) -> None:
    _edit_yaml(target / "study.yaml", lambda doc: doc.__setitem__("seed", 7))
    copy_panel(target, source)
    meta = json.loads((target / PERSONAS_DIR / "meta.json").read_bytes())
    assert meta["seed"] == 1


def test_wording_drift(target: Path, source: Path) -> None:
    card = source / PERSONAS_DIR / "p3.md"
    card.write_text(card.read_text() + "extra\n")
    err = _refused(target, source, "panel_mismatch")
    assert err.message == err.path == "../source/panel/personas/p3.md"
    assert not (target / PERSONAS_DIR).exists()


def test_no_source_panel(target: Path, empty: Path) -> None:
    err = _refused(target, empty, "panel_missing")
    assert err.message.startswith("source ../")
    result = _cli(target, empty)
    assert result.exit_code == 1
    assert result.stderr.startswith("panel_missing: source ")


def test_target_hashes_differ(target: Path, source: Path) -> None:
    _set_temperature(target, 0.9)
    copy_panel(target, source)
    with pytest.raises(ConsortiumError) as info:
        open_test(target, "pilot1", dry_run=True)
    assert info.value.code == "screening_stale"
    assert "Model m1 (settings changed)" in info.value.message


def test_new_model_is_missing(target: Path, source: Path) -> None:
    def add_m3(doc: dict[str, Any]) -> None:
        doc["models"].append(dict(json.loads(json.dumps(doc["models"][0])), id="m3"))

    _edit_yaml(target / "study.yaml", add_m3)
    _edit_yaml(target / "prices.yaml",
               lambda doc: doc["models"].__setitem__("m3", dict(doc["models"]["m1"])))
    copy_panel(target, source)
    summary = open_test(target, "pilot1", dry_run=True)
    assert summary.screening[2].endswith(
        f"perception_missing {PERSONAS} (m3: godspeed, pairwise_alive)")
    assert set(summary.by_model) == {"m1"}


# --------------------------------------------------------------------------- acceptance


def test_copy_then_open_matches_the_source(target: Path, source: Path) -> None:
    expected = open_test(source, "pilot1", dry_run=True).screening
    assert expected[1] == f"eligible agents: {PERSONAS} of {AGENTS}"
    copy_panel(target, source)
    assert open_test(target, "pilot1", dry_run=True).screening == expected


def test_regenerate_after_copy_is_in_use(target: Path, source: Path) -> None:
    copy_panel(target, source)
    result = runner.invoke(app, ["personas", "generate", "--force", "--study", str(target)])
    assert result.exit_code == 1
    assert result.stderr.startswith("panel_in_use: ")


def test_imported_perception_results_alone_do_not_freeze_the_panel(
    target: Path, plain: Path
) -> None:
    _seed_perception(plain)
    copy_panel(target, plain)
    personas_stage.generate(target, force=True)


# --------------------------------------------------------------------------- atomicity


def test_failed_rename_rolls_back(
    target: Path, source: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(new: Path, dest: Path) -> None:
        raise OSError("rename failed")

    monkeypatch.setattr(stage, "move_into_place", fail)
    with pytest.raises(ConsortiumError) as info:
        copy_panel(target, source)
    assert info.value.code == "personas_failed"
    assert not (target / PERSONAS_DIR).exists()
    assert not list((target / "panel").glob(".personas-*"))
    assert _results(target) == [] and _runs(target) == []


def test_failed_commit_removes_the_panel(
    target: Path, source: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    @contextlib.contextmanager
    def no_commit(conn: sqlite3.Connection):
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        finally:
            conn.execute("ROLLBACK")
        raise sqlite3.OperationalError("database or disk is full")

    monkeypatch.setattr(stage, "transaction", no_commit)
    with pytest.raises(ConsortiumError) as info:
        copy_panel(target, source)
    assert info.value.code == "personas_failed"
    assert not (target / PERSONAS_DIR).exists()
    assert not list((target / "panel").glob(".personas-*"))
    assert _results(target) == []


def test_failed_commit_and_rename_back_still_removes_the_panel(
    target: Path, source: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    @contextlib.contextmanager
    def no_commit(conn: sqlite3.Connection):
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        finally:
            conn.execute("ROLLBACK")
        raise sqlite3.OperationalError("disk I/O error")

    real = os.rename

    def rename(src: Path, dst: Path) -> None:
        if Path(dst).name.startswith(".personas-new-"):
            raise OSError("rename back failed")
        real(src, dst)

    monkeypatch.setattr(stage, "transaction", no_commit)
    monkeypatch.setattr(stage.os, "rename", rename)
    with pytest.raises(ConsortiumError) as info:
        copy_panel(target, source)
    assert info.value.code == "personas_failed"
    assert not (target / PERSONAS_DIR).exists()


def test_in_use_rechecked_under_the_lease(
    target: Path, plain: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []

    def appears(conn: sqlite3.Connection) -> bool:  # a screening run starts meanwhile
        calls.append(1)
        return len(calls) > 1

    monkeypatch.setattr(stage, "_in_use", appears)
    with pytest.raises(ConsortiumError) as info:
        copy_panel(target, plain)
    assert info.value.code == "panel_in_use"
    assert len(calls) == 2
    assert not (target / PERSONAS_DIR).exists()
    assert not list((target / "panel").glob(".personas-*"))
