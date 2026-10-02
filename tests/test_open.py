"""consortium open --dry-run and board.db read-only connect: the story 1.6 I/O matrix."""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from consortium.board.blinding import append_conditions, read_key
from consortium.board.clips import insert_clip
from consortium.board.db import DB_FILE, MIGRATIONS, connect, m1_clips, read_only, transaction
from consortium.cli import app
from consortium.core.errors import ConsortiumError
from consortium.core.render import canonical_json
from consortium.stages import personas as personas_stage
from consortium.stages.init import init_study
from consortium.stages.open import open_test, plan_and_render
from consortium.stages.push import push_test

runner = CliRunner()

TARGETS = ["c_aaaaaaaa", "c_bbbbbbbb", "c_cccccccc", "c_dddddddd"]
PRACTICE = [f"c_pppppp{x}" for x in ("aa", "ab", "ac", "ad", "ae", "af")]
GODSPEED = {f"animacy_{i}": 3 for i in range(1, 7)} | {f"likeability_{i}": 3 for i in range(1, 6)}
CONDITIONS = {
    "c_aaaaaaaa": {"gait": "smoothwalk"},
    "c_bbbbbbbb": {"gait": "jerkystep"},
    "c_cccccccc": {"gait": "smoothwalk"},
    "c_dddddddd": {"gait": "jerkystep"},
}


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


def _write_test(study: Path, name: str, kind: str = "pilot", **fields: Any) -> Path:
    doc: dict[str, Any] = {
        "schema_version": 1, "test": name, "kind": kind,
        "instruments": ["godspeed", "pairwise_alive"], "clips": TARGETS,
        "practice": [
            {"instrument": "godspeed", "clips": [PRACTICE[0]], "answer": GODSPEED},
            {"instrument": "godspeed", "clips": [PRACTICE[1]], "answer": GODSPEED},
            {"instrument": "pairwise_alive", "clips": PRACTICE[2:4], "answer": {"alive": "A"}},
            {"instrument": "pairwise_alive", "clips": PRACTICE[4:6], "answer": {"alive": "B"}},
        ],
        "session": {"repeats": 3},
    }
    doc.update(fields)
    src = study.parent / f"{name}.yaml"
    src.write_text(yaml.safe_dump(doc, sort_keys=False))
    return src


def _make_study(path: Path) -> Path:
    study = init_study(path)
    personas_stage.generate(study)
    _add_clips(study, TARGETS + PRACTICE)
    for clip_id, cond in CONDITIONS.items():
        append_conditions(study, clip_id, cond)
    push_test(study, _write_test(study, "pilot1"))
    return study


@pytest.fixture(scope="module")
def study(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Shared and never mutated; tests that change the Study use ``fresh``."""
    return _make_study(tmp_path_factory.mktemp("open") / "study")


@pytest.fixture()
def fresh(tmp_path: Path, study: Path) -> Path:
    """A private copy of ``study`` that a test may change."""
    copy = tmp_path / "copy"
    shutil.copytree(study, copy)
    return copy


def _snapshot(root: Path) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for dirpath, _dirnames, filenames in os.walk(root):
        d = Path(dirpath)
        out[str(d.relative_to(root))] = ("<dir>", d.stat().st_mtime_ns)
        for name in filenames:
            p = d / name
            out[str(p.relative_to(root))] = (p.read_bytes(), p.stat().st_mtime_ns)
    return out


def _digest(study: Path) -> str:
    _, requests, _ = plan_and_render(study, "pilot1")
    digest = hashlib.sha256()
    for request in requests:
        digest.update(canonical_json(request))
    return digest.hexdigest()


def _cli(*args: str):
    return runner.invoke(app, ["open", *args])


# --------------------------------------------------------------------------- matrix


def test_dry_run_counts_and_writes_nothing(study: Path) -> None:
    before = _snapshot(study)
    result = _cli("pilot1", "--dry-run", "--study", str(study))
    assert result.exit_code == 0, result.stderr
    assert result.stdout.splitlines() == [
        "test: pilot1 (pilot) dry run",
        "sessions: 192",
        "trials per session: 4 + 12 = 16 (godspeed 4, pairwise_alive 12)",
        "trials: 3072",
        "by model: m1 3072",
        "by instrument: godspeed 768, pairwise_alive 2304",
        "by type: single 768, pairwise 2304",
        f"requests sha256: {_digest(study)}",
    ]
    assert _snapshot(study) == before
    assert not (study / f"{DB_FILE}-wal").exists() and not (study / f"{DB_FILE}-shm").exists()


def test_same_inputs_byte_identical_across_folders(tmp_path: Path, study: Path) -> None:
    other = _make_study(tmp_path / "other")
    plan_a, req_a, _ = plan_and_render(study, "pilot1")
    plan_b, req_b, _ = plan_and_render(other, "pilot1")
    assert [t.trial_id for t in plan_a.trials] == [t.trial_id for t in plan_b.trials]
    assert [(t.clip_ids, t.prompt_variant) for t in plan_a.trials] == [
        (t.clip_ids, t.prompt_variant) for t in plan_b.trials
    ]
    assert [canonical_json(r) for r in req_a] == [canonical_json(r) for r in req_b]
    assert open_test(study, "pilot1", dry_run=True).requests_sha256 == open_test(
        other, "pilot1", dry_run=True
    ).requests_sha256


def test_requests_leak_no_condition_id_or_other_clip(study: Path) -> None:
    plan, requests, _ = plan_and_render(study, "pilot1")
    levels = {lvl for cond in read_key(study).values() for lvl in cond.values()}
    assert levels == {"smoothwalk", "jerkystep"}
    for trial, req in zip(plan.trials, requests, strict=True):
        text = canonical_json(req).decode("utf-8")
        for value in levels | {trial.session_id, trial.trial_id, "pilot1", trial.agent_id}:
            assert value not in text
        for clip in TARGETS:
            assert (clip in text) == (clip in trial.clip_ids)
        assert req.persona_card == (
            study / "panel" / "personas" / f"{trial.persona_id}.md"
        ).read_text(encoding="utf-8")


def test_main_refused(fresh: Path) -> None:
    # Practice clips may be shared with a main Test; targets may not, so use fresh ones.
    targets = ["c_mmmmmmma", "c_mmmmmmmb"]
    _add_clips(fresh, targets)
    push_test(fresh, _write_test(fresh, "main1", kind="main", clips=targets))
    before = _snapshot(fresh)
    result = _cli("main1", "--dry-run", "--study", str(fresh))
    assert result.exit_code == 1
    assert result.stderr.startswith("protocol_lock_unavailable:")
    assert "kind main" in result.stderr
    assert result.stdout == ""
    assert _snapshot(fresh) == before


def test_not_openable_non_main_refused(fresh: Path) -> None:
    raw = sqlite3.connect(fresh / DB_FILE)
    with raw:
        raw.execute("UPDATE tests SET openable = 0 WHERE name = 'pilot1'")
    raw.close()
    with pytest.raises(ConsortiumError) as info:
        open_test(fresh, "pilot1", dry_run=True)
    assert info.value.code == "protocol_lock_unavailable"
    assert info.value.message == "Test 'pilot1' is registered as not openable"


def test_unknown_test(study: Path) -> None:
    result = _cli("nope", "--dry-run", "--study", str(study))
    assert result.exit_code == 1
    assert result.stderr.startswith("unknown_test:")


def test_no_board_db_is_unknown_test(tmp_path: Path) -> None:
    study = init_study(tmp_path / "s")
    before = _snapshot(study)
    with pytest.raises(ConsortiumError) as info:
        open_test(study, "example", dry_run=True)
    assert info.value.code == "unknown_test"
    assert _snapshot(study) == before


def test_no_personas_is_panel_missing(fresh: Path) -> None:
    shutil.rmtree(fresh / "panel" / "personas")
    with pytest.raises(ConsortiumError) as info:
        open_test(fresh, "pilot1", dry_run=True)
    assert info.value.code == "panel_missing"


def test_edited_registered_file_refused(fresh: Path) -> None:
    path = fresh / "tests" / "pilot1.yaml"
    path.write_text(path.read_text() + "# edited\n")
    with pytest.raises(ConsortiumError) as info:
        open_test(fresh, "pilot1", dry_run=True)
    assert info.value.code == "test_exists"


def test_missing_registered_file_refused(fresh: Path) -> None:
    (fresh / "tests" / "pilot1.yaml").unlink()
    with pytest.raises(ConsortiumError) as info:
        open_test(fresh, "pilot1", dry_run=True)
    assert info.value.code == "test_exists"


def test_corrupt_board_is_unreadable(fresh: Path) -> None:
    (fresh / DB_FILE).write_bytes(b"this is not an SQLite database" * 200)
    result = _cli("pilot1", "--dry-run", "--study", str(fresh))
    assert result.exit_code == 1
    assert result.stderr.startswith("board_unreadable:"), result.stderr


def test_practice_clips_raised_after_push(fresh: Path) -> None:
    cfg = fresh / "study.yaml"
    text = cfg.read_text()
    assert "  practice_clips: 2 " in text
    cfg.write_text(text.replace("  practice_clips: 2 ", "  practice_clips: 3 ", 1))
    with pytest.raises(ConsortiumError) as info:
        open_test(fresh, "pilot1", dry_run=True)
    assert info.value.code == "bad_practice"


def test_media_limits_tightened_after_push(fresh: Path) -> None:
    cfg = fresh / "study.yaml"
    text = cfg.read_text()
    assert "max_seconds: 600" in text
    cfg.write_text(text.replace("max_seconds: 600", "max_seconds: 3", 1))
    with pytest.raises(ConsortiumError) as info:
        open_test(fresh, "pilot1", dry_run=True)
    assert info.value.code == "media_limit_exceeded"


@pytest.mark.parametrize("ceiling", ["abc", "0", "-1", "NaN", "Infinity"])
def test_bad_ceiling(study: Path, ceiling: str) -> None:
    result = _cli("pilot1", "--dry-run", "--ceiling", ceiling, "--study", str(study))
    assert result.exit_code == 1
    assert result.stderr.startswith("bad_ceiling:")


def test_good_ceiling_accepted(study: Path) -> None:
    result = _cli("pilot1", "--dry-run", "--ceiling", "5.00", "--study", str(study))
    assert result.exit_code == 0, result.stderr


# --------------------------------------------------------------------------- read-only connect


def test_readonly_absent_returns_none(tmp_path: Path) -> None:
    assert connect(tmp_path, readonly=True) is None
    assert list(tmp_path.iterdir()) == []


def test_readonly_never_writes(study: Path) -> None:
    before = _snapshot(study)
    conn = connect(study, readonly=True)
    assert conn is not None
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM clips")
    finally:
        conn.close()
    assert _snapshot(study) == before


def test_readonly_alongside_open_writer(fresh: Path) -> None:
    study = fresh
    writer = connect(study)  # creates -wal and -shm while open
    try:
        assert (study / f"{DB_FILE}-wal").exists()
        conn = connect(study, readonly=True)
        assert conn is not None
        assert conn.execute("SELECT count(*) FROM tests").fetchone()[0] >= 1
        conn.close()
    finally:
        writer.close()


@pytest.mark.parametrize("version", [1, len(MIGRATIONS) + 1])
def test_readonly_version_mismatch(tmp_path: Path, version: int) -> None:
    raw = sqlite3.connect(tmp_path / DB_FILE, isolation_level=None)
    raw.execute("PRAGMA journal_mode=WAL")
    m1_clips(raw)
    raw.execute(f"PRAGMA user_version = {version}")
    raw.close()
    before = _snapshot(tmp_path)
    with pytest.raises(ConsortiumError) as info:
        connect(tmp_path, readonly=True)
    assert info.value.code == "board_version_mismatch"
    assert _snapshot(tmp_path) == before


def test_read_only_redoes_reads_when_a_writer_starts(fresh: Path) -> None:
    calls: list[str] = []
    writers: list[sqlite3.Connection] = []

    def read(conn: sqlite3.Connection) -> int:
        calls.append("read")
        if not writers:  # a writer starts during the first (immutable) read
            writers.append(connect(fresh))
        return conn.execute("SELECT count(*) FROM tests").fetchone()[0]

    try:
        assert read_only(fresh, read) == 1
    finally:
        for w in writers:
            w.close()
    assert calls == ["read", "read"]


def test_read_only_absent_returns_none(tmp_path: Path) -> None:
    assert read_only(tmp_path, lambda conn: 1) is None
