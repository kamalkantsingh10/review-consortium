"""consortium push test: every row of the story 1.5 I/O matrix, plus check_media units."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from consortium.board.clips import insert_clip
from consortium.board.db import DB_FILE, MIGRATIONS, connect, m1_clips, transaction
from consortium.board.tests import get_test, target_clip_kinds
from consortium.cli import app
from consortium.core.errors import ConsortiumError
from consortium.core.ids import new_clip_id
from consortium.core.media_limits import TrialShape, check_media, encoded_bytes
from consortium.stages.init import init_study
from consortium.stages.push import push_test

runner = CliRunner()

GODSPEED_ITEMS = [f"animacy_{i}" for i in range(1, 7)] + [f"likeability_{i}" for i in range(1, 6)]
GODSPEED_ANSWER = dict.fromkeys(GODSPEED_ITEMS, 3)
PAIRWISE_ANSWER = {"alive": "A"}

EXTRA_MODELS = """
  - id: m2
    provider: fake
    model: fake-1
    max_output_tokens: 512
    limits: {max_seconds: 300, max_bytes: 20000000, inline_base64: true}
  - id: m3
    provider: fake
    model: fake-1
    max_output_tokens: 512
    limits: {max_seconds: 6000, max_bytes: 10000000, inline_base64: true}
  - id: m4
    provider: fake
    model: fake-1
    max_output_tokens: 512
    limits: {max_seconds: 6000, max_bytes: 10000000, inline_base64: false}
"""


@pytest.fixture()
def study(tmp_path: Path) -> Path:
    path = init_study(tmp_path / "study")
    cfg = path / "study.yaml"
    text = cfg.read_text()
    marker = (
        "      inline_base64: true   # media sent inline as base64 (counts 4/3 of the file size)\n"
    )
    assert marker in text
    cfg.write_text(text.replace(marker, marker + EXTRA_MODELS, 1))
    prices = path / "prices.yaml"
    ptext = prices.read_text()
    for mid in ("m2", "m3", "m4"):
        ptext += f'  {mid}: {{input_usd_per_mtok: "0", output_usd_per_mtok: "0"}}\n'
    prices.write_text(ptext)
    return path


def _clips(study: Path, n: int, *, duration: float = 2.0, size: int = 1000) -> list[str]:
    conn = connect(study)
    ids = []
    try:
        with transaction(conn):
            for _ in range(n):
                clip_id = new_clip_id()
                info = SimpleNamespace(
                    duration_s=duration, size_bytes=size, width=640, height=480, fps=25.0,
                    loudness_lufs=-23.0,
                )
                insert_clip(conn, clip_id, "0" * 64, info)
                ids.append(clip_id)
    finally:
        conn.close()
    return sorted(ids)


def _write(folder: Path, name: str, **fields: Any) -> Path:
    doc: dict[str, Any] = {"schema_version": 1, "test": name, "kind": "pilot"}
    doc.update(fields)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}.yaml"
    path.write_text(yaml.safe_dump(doc, sort_keys=False))
    return path


def _pilot_fields(study: Path, *, kind: str = "pilot") -> dict[str, Any]:
    targets = _clips(study, 3)
    practice = _clips(study, 6)
    return {
        "kind": kind,
        "instruments": ["godspeed", "pairwise_alive"],
        "clips": targets,
        "practice": [
            {"instrument": "godspeed", "clips": [practice[0]], "answer": GODSPEED_ANSWER},
            {"instrument": "godspeed", "clips": [practice[1]], "answer": GODSPEED_ANSWER},
            {"instrument": "pairwise_alive", "clips": practice[2:4], "answer": PAIRWISE_ANSWER},
            {"instrument": "pairwise_alive", "clips": practice[4:6], "answer": PAIRWISE_ANSWER},
        ],
        "session": {"repeats": 3},
    }


def _snapshot(study: Path) -> dict[str, Any]:
    tests_dir = study / "tests"
    files = None
    if tests_dir.exists():
        files = {p.name: p.read_bytes() if p.is_file() else "<dir>"
                 for p in sorted(tests_dir.iterdir())}
    db = study / DB_FILE
    tables: dict[str, Any] = {}
    if db.exists():
        with sqlite3.connect(db) as conn:
            sql = "SELECT name FROM sqlite_master WHERE type='table'"
            names = [r[0] for r in conn.execute(sql)]
            for table in names:
                tables[table] = sorted(conn.execute(f"SELECT * FROM {table}").fetchall())
    return {"files": files, "db": db.exists(), "tables": tables}


def _refused(study: Path, src: Path, code: str) -> ConsortiumError:
    before = _snapshot(study)
    with pytest.raises(ConsortiumError) as info:
        push_test(study, src)
    assert info.value.code == code, info.value
    assert _snapshot(study) == before
    return info.value


def _cli(study: Path, src: Path):
    return runner.invoke(app, ["push", "test", str(src), "--study", str(study)])


# --------------------------------------------------------------------------- matrix


def test_pilot_ok(study: Path, tmp_path: Path) -> None:
    fields = _pilot_fields(study)
    src = _write(tmp_path / "elsewhere", "pilot1", **fields)
    result = _cli(study, src)
    assert result.exit_code == 0, result.stderr
    assert result.stdout == "pilot1\n"
    stored = study / "tests" / "pilot1.yaml"
    assert stored.read_bytes() == src.read_bytes()
    conn = connect(study)
    try:
        row = get_test(conn, "pilot1")
    finally:
        conn.close()
    assert row is not None
    assert (row["kind"], row["openable"], row["path"]) == ("pilot", True, "tests/pilot1.yaml")
    practice = sorted({c for ex in fields["practice"] for c in ex["clips"]})
    assert sorted(row["clips"]) == sorted(
        [(c, "target") for c in fields["clips"]] + [(c, "practice") for c in practice]
    )
    assert not [p for p in (study / "tests").iterdir() if p.name.startswith(".")]


def test_main_registered_not_openable(study: Path, tmp_path: Path) -> None:
    src = _write(tmp_path / "elsewhere", "main1", **_pilot_fields(study, kind="main"))
    result = _cli(study, src)
    assert result.exit_code == 0, result.stderr
    assert result.stdout == "main1\n"
    assert "not openable until Protocol lock" in result.stderr
    conn = connect(study)
    try:
        row = get_test(conn, "main1")
    finally:
        conn.close()
    assert row is not None and row["kind"] == "main" and row["openable"] is False


def test_repush_identical_is_noop(study: Path, tmp_path: Path) -> None:
    src = _write(tmp_path / "elsewhere", "pilot1", **_pilot_fields(study))
    assert push_test(study, src) == "pilot1"
    stored = study / "tests" / "pilot1.yaml"
    mtime = stored.stat().st_mtime_ns
    before = _snapshot(study)
    assert push_test(study, src) == "pilot1"
    result = _cli(study, stored)  # in place, same bytes
    assert result.exit_code == 0 and result.stdout == "pilot1\n"
    assert _snapshot(study) == before
    assert stored.stat().st_mtime_ns == mtime


def test_repush_changed_refused(study: Path, tmp_path: Path) -> None:
    src = _write(tmp_path / "elsewhere", "pilot1", **_pilot_fields(study))
    push_test(study, src)
    src.write_text(src.read_text() + "# changed\n")
    err = _refused(study, src, "test_exists")
    assert "pilot1" in err.message


def test_in_place_recorded_not_rewritten(study: Path) -> None:
    src = _write(study / "tests", "inplace", **_pilot_fields(study))
    mtime = src.stat().st_mtime_ns
    assert push_test(study, src) == "inplace"
    assert src.stat().st_mtime_ns == mtime
    conn = connect(study)
    try:
        assert get_test(conn, "inplace")["path"] == "tests/inplace.yaml"
    finally:
        conn.close()


def test_outside_source_conflicts_with_unregistered_tests_file(study: Path, tmp_path: Path) -> None:
    # tests/example.yaml exists from init but is not registered; different bytes are refused.
    src = _write(tmp_path / "elsewhere", "example", instruments=["godspeed"],
                 session={"practice_clips": 0})
    _refused(study, src, "test_exists")


def test_unknown_clip(study: Path, tmp_path: Path) -> None:
    _clips(study, 1)
    src = _write(tmp_path / "e", "t1", instruments=["godspeed"], clips=["c_notthere"],
                 session={"practice_clips": 0})
    err = _refused(study, src, "unknown_clip")
    assert err.message.startswith("clips[0]: ")


def test_unknown_clip_without_board_creates_nothing(study: Path, tmp_path: Path) -> None:
    src = _write(tmp_path / "e", "t1", instruments=["godspeed"], clips=["c_notthere"],
                 session={"practice_clips": 0})
    _refused(study, src, "unknown_clip")
    assert not (study / DB_FILE).exists()


def test_unknown_instrument(study: Path, tmp_path: Path) -> None:
    src = _write(tmp_path / "e", "t1", instruments=["foo"], clips=_clips(study, 1))
    _refused(study, src, "unknown_instrument")


@pytest.mark.parametrize("case", ["one_target", "duplicate", "round_robin"])
def test_bad_plan(study: Path, tmp_path: Path, case: str) -> None:
    clips = _clips(study, 2)
    fields: dict[str, Any] = {"instruments": ["pairwise_alive"], "clips": clips,
                              "session": {"practice_clips": 0}}
    if case == "one_target":
        fields["clips"] = clips[:1]
    elif case == "duplicate":
        fields["clips"] = [clips[0], clips[1], clips[0]]
    else:
        fields["session"]["pairing"] = "round_robin"
    src = _write(tmp_path / "e", "t1", **fields)
    _refused(study, src, "bad_pairing")


def test_practice_as_target(study: Path, tmp_path: Path) -> None:
    clips = _clips(study, 2)
    src = _write(tmp_path / "e", "t1", instruments=["godspeed"], clips=clips,
                 practice=[{"instrument": "godspeed", "clips": [clips[0]],
                            "answer": GODSPEED_ANSWER}],
                 session={"practice_clips": 1})
    err = _refused(study, src, "bad_practice")
    assert err.message.startswith("practice: ")


def test_practice_too_few(study: Path, tmp_path: Path) -> None:
    targets, practice = _clips(study, 1), _clips(study, 1)
    src = _write(tmp_path / "e", "t1", instruments=["godspeed"], clips=targets,
                 practice=[{"instrument": "godspeed", "clips": practice,
                            "answer": GODSPEED_ANSWER}])  # practice_clips defaults to 2
    err = _refused(study, src, "bad_practice")
    assert err.message.startswith("practice[0]: ")
    src2 = _write(tmp_path / "e2", "t2", instruments=["godspeed"], clips=targets)
    assert _refused(study, src2, "bad_practice").message.startswith("practice: ")


def test_practice_clip_not_pushed(study: Path, tmp_path: Path) -> None:
    targets = _clips(study, 1)
    src = _write(tmp_path / "e", "t1", instruments=["godspeed"], clips=targets,
                 practice=[{"instrument": "godspeed", "clips": ["c_notthere"],
                            "answer": GODSPEED_ANSWER}],
                 session={"practice_clips": 1})
    assert _refused(study, src, "bad_practice").message.startswith("practice[0]: ")


def test_practice_answer_failing_schema(study: Path, tmp_path: Path) -> None:
    # Checked by load_test (story 1.2), which reports config_invalid.
    targets, practice = _clips(study, 1), _clips(study, 1)
    src = _write(tmp_path / "e", "t1", instruments=["godspeed"], clips=targets,
                 practice=[{"instrument": "godspeed", "clips": practice,
                            "answer": {**GODSPEED_ANSWER, "animacy_1": 9}}],
                 session={"practice_clips": 1})
    err = _refused(study, src, "config_invalid")
    assert "practice.0.answer.animacy_1" in err.message


def test_extra_practice_examples_allowed(study: Path, tmp_path: Path) -> None:
    targets, practice = _clips(study, 1), _clips(study, 3)
    src = _write(tmp_path / "e", "t1", instruments=["godspeed"], clips=targets,
                 practice=[{"instrument": "godspeed", "clips": [c], "answer": GODSPEED_ANSWER}
                           for c in practice],
                 session={"practice_clips": 2})
    assert push_test(study, src) == "t1"


def test_clip_kind_overlap(study: Path, tmp_path: Path) -> None:
    clips = _clips(study, 2)
    base = {"instruments": ["godspeed"], "session": {"practice_clips": 0}}
    push_test(study, _write(tmp_path / "a", "main1", kind="main", clips=clips[:1], **base))
    src = _write(tmp_path / "b", "pilot1", kind="pilot", clips=clips, **base)
    err = _refused(study, src, "clip_kind_overlap")
    assert clips[0] in err.message and "main1" in err.message
    assert err.message.startswith("clips[0]: ")
    # pilot and screening may share; Practice uses are exempt.
    push_test(study, _write(tmp_path / "c", "screen1", kind="screening", clips=clips[1:], **base))
    push_test(study, _write(tmp_path / "d", "pilot2", kind="pilot", clips=clips[1:], **base))
    push_test(study, _write(
        tmp_path / "e", "pilot3", kind="pilot", instruments=["godspeed"], clips=clips[1:],
        practice=[{"instrument": "godspeed", "clips": clips[:1], "answer": GODSPEED_ANSWER}],
        session={"practice_clips": 1},
    ))
    conn = connect(study)
    try:
        kinds = target_clip_kinds(conn, clips)
    finally:
        conn.close()
    assert kinds[clips[0]] == [("main1", "main")]
    assert [t for t, _ in kinds[clips[1]]] == ["pilot2", "pilot3", "screen1"]


def test_too_much_media(study: Path, tmp_path: Path) -> None:
    targets = _clips(study, 2, duration=120.0)
    practice = _clips(study, 2, duration=120.0)
    src = _write(tmp_path / "e", "t1", instruments=["pairwise_alive"], models=["m2"],
                 clips=targets,
                 practice=[{"instrument": "pairwise_alive", "clips": practice,
                            "answer": PAIRWISE_ANSWER}],
                 session={"practice_clips": 1})
    result = _cli(study, src)
    assert result.exit_code == 1
    a, b = targets
    assert result.stderr.strip() == (
        f"media_limit_exceeded: m2 max_seconds 300 < 480 (2 practice + pairwise {a},{b})"
    )


def test_media_only_used_practice_counts(study: Path, tmp_path: Path) -> None:
    targets = _clips(study, 2, duration=120.0)
    practice = _clips(study, 4, duration=120.0)
    src = _write(tmp_path / "e", "t1", instruments=["pairwise_alive"], models=["m2"],
                 clips=targets,
                 practice=[{"instrument": "pairwise_alive", "clips": practice[:2],
                            "answer": PAIRWISE_ANSWER},
                           {"instrument": "pairwise_alive", "clips": practice[2:],
                            "answer": PAIRWISE_ANSWER}],
                 session={"practice_clips": 0})
    assert push_test(study, src) == "t1"  # 240 s targets only


def test_base64_bytes(study: Path, tmp_path: Path) -> None:
    targets = _clips(study, 1, size=7_600_000)
    fields = {"instruments": ["godspeed"], "clips": targets, "session": {"practice_clips": 0}}
    src = _write(tmp_path / "e", "t1", models=["m3"], **fields)
    err = _refused(study, src, "media_limit_exceeded")
    assert err.message.startswith("m3 max_bytes 10000000 < 10133336 ")
    assert push_test(study, _write(tmp_path / "f", "t2", models=["m4"], **fields)) == "t2"


@pytest.mark.parametrize(("stem", "name"), [("pilot1", "Pilot/1"), ("Pilot", "Pilot")])
def test_bad_name(study: Path, tmp_path: Path, stem: str, name: str) -> None:
    folder = tmp_path / "e"
    folder.mkdir()
    src = folder / f"{stem}.yaml"
    src.write_text(yaml.safe_dump({"schema_version": 1, "test": name, "kind": "pilot",
                                   "instruments": ["godspeed"]}))
    _refused(study, src, "bad_test_name")


# --------------------------------------------------------------------------- acceptance


def test_reopen_reads_registration(study: Path, tmp_path: Path) -> None:
    import hashlib

    fields = _pilot_fields(study)
    src = _write(tmp_path / "e", "pilot1", **fields)
    push_test(study, src)
    conn = connect(study)
    try:
        row = get_test(conn, "pilot1")
    finally:
        conn.close()
    assert row["sha256"] == hashlib.sha256(src.read_bytes()).hexdigest()
    assert row["kind"] == "pilot" and row["openable"] is True
    roles = dict(row["clips"])
    assert all(roles[c] == "target" for c in fields["clips"])
    assert all(roles[c] == "practice" for ex in fields["practice"] for c in ex["clips"])


def test_migrates_v1_board(study: Path, tmp_path: Path) -> None:
    with sqlite3.connect(study / DB_FILE) as conn:
        m1_clips(conn)
        conn.execute("PRAGMA user_version = 1")
        conn.execute(
            "INSERT INTO clips VALUES ('c_aaaaaaaa', ?, 2.0, 1000, 640, 480, 25.0, -23.0, 'x')",
            ("0" * 64,),
        )
    src = _write(tmp_path / "e", "t1", instruments=["godspeed"], clips=["c_aaaaaaaa"],
                 session={"practice_clips": 0})
    assert push_test(study, src) == "t1"
    with sqlite3.connect(study / DB_FILE) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS) == 6


# --------------------------------------------------------------------------- check_media


def _model(mid: str, seconds: float, size: int, b64: bool = False) -> SimpleNamespace:
    limits = SimpleNamespace(max_seconds=seconds, max_bytes=size, inline_base64=b64)
    return SimpleNamespace(id=mid, limits=limits)


CLIPS = {
    "c_p": {"duration_s": 10.0, "size_bytes": 100},
    "c_long": {"duration_s": 30.0, "size_bytes": 50},
    "c_big": {"duration_s": 5.0, "size_bytes": 400},
    "c_mid": {"duration_s": 20.0, "size_bytes": 200},
}


def test_check_media_equal_to_limit_passes() -> None:
    single = TrialShape("g", False, ("c_p",), ("c_long", "c_big"))
    assert check_media([single], CLIPS, [_model("m1", 40.0, 500)]) is None  # 10+30 s, 100+400 B
    v = check_media([single], CLIPS, [_model("m1", 39.9, 500)])
    assert v is not None and v.limit == "max_seconds" and v.targets == ("c_long",)
    v = check_media([single], CLIPS, [_model("m1", 40.0, 499)])
    assert v is not None and v.limit == "max_bytes" and v.targets == ("c_big",)


def test_check_media_pairwise_two_largest() -> None:
    pair = TrialShape("p", True, ("c_p", "c_p"), ("c_long", "c_big", "c_mid"))
    assert check_media([pair], CLIPS, [_model("m1", 70.0, 800)]) is None  # 20+50 s; 200+600 B
    v = check_media([pair], CLIPS, [_model("m1", 69.0, 800)])
    assert v is not None
    assert v.message == "m1 max_seconds 69 < 70 (2 practice + pairwise c_long,c_mid)"


def test_check_media_base64_rounding() -> None:
    assert [encoded_bytes(n, True) for n in (0, 1, 2, 3, 4, 6, 7)] == [0, 4, 4, 4, 8, 8, 12]
    assert encoded_bytes(7, False) == 7
    clips = {"c_a": {"duration_s": 1.0, "size_bytes": 4}}  # base64: 8 bytes
    shape = TrialShape("g", False, (), ("c_a",))
    assert check_media([shape], clips, [_model("m1", 10, 8, b64=True)]) is None
    v = check_media([shape], clips, [_model("m1", 10, 7, b64=True)])
    assert v is not None and v.total == 8
    assert check_media([shape], clips, [_model("m1", 10, 4, b64=False)]) is None


def test_check_media_first_violation_in_model_order() -> None:
    shape = TrialShape("g", False, (), ("c_long",))
    v = check_media([shape], CLIPS, [_model("m1", 100, 1000), _model("m2", 1, 1),
                                     _model("m3", 1, 1)])
    assert v is not None and v.model == "m2"


# --------------------------------------------------------------------------- review fixes


def _godspeed_fields(study: Path) -> dict[str, Any]:
    return {"instruments": ["godspeed"], "clips": _clips(study, 1),
            "session": {"practice_clips": 0}}


def _leftovers(study: Path) -> list[str]:
    tests_dir = study / "tests"
    return sorted(p.name for p in tests_dir.glob(".push-*")) if tests_dir.exists() else []


@pytest.mark.parametrize("failure", ["link", "fsync_dir", "insert"])
def test_store_failure_rolls_back(
    study: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    import errno
    import shutil

    import consortium.stages.push as push_mod

    shutil.rmtree(study / "tests")  # so the push creates tests/ itself
    src = _write(tmp_path / "e", "t1", **_godspeed_fields(study))

    def boom(*args: Any, **kwargs: Any) -> None:
        if failure == "insert":
            raise sqlite3.OperationalError("disk I/O error")
        raise OSError(errno.EIO, "I/O error")

    target = {"link": (push_mod.os, "link"), "fsync_dir": (push_mod, "_fsync_dir"),
              "insert": (push_mod, "insert_test")}[failure]
    monkeypatch.setattr(*target, boom)
    err = _refused(study, src, "push_failed")
    assert err.path is not None
    assert not (study / "tests").exists()
    assert _leftovers(study) == []


def test_concurrent_registration_with_other_bytes(
    study: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import consortium.stages.push as push_mod
    from consortium.board.tests import register_test

    src = _write(tmp_path / "e", "t1", **_godspeed_fields(study))
    original = push_mod._validate_test

    def racing(conn, *args: Any, **kwargs: Any) -> None:
        original(conn, *args, **kwargs)
        other = connect(study)
        try:
            register_test(other, {"name": "t1", "kind": "pilot", "path": "tests/t1.yaml",
                                  "sha256": "f" * 64, "openable": True}, [])
        finally:
            other.close()

    monkeypatch.setattr(push_mod, "_validate_test", racing)
    with pytest.raises(ConsortiumError) as info:
        push_test(study, src)
    assert info.value.code == "test_exists"
    assert not (study / "tests" / "t1.yaml").exists()
    assert _leftovers(study) == []
    conn = connect(study)
    try:
        assert get_test(conn, "t1")["sha256"] == "f" * 64
    finally:
        conn.close()


def test_concurrent_identical_registration_is_noop(
    study: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hashlib

    import consortium.stages.push as push_mod
    from consortium.board.tests import register_test

    src = _write(tmp_path / "e", "t1", **_godspeed_fields(study))
    sha = hashlib.sha256(src.read_bytes()).hexdigest()
    original = push_mod._validate_test

    def racing(conn, *args: Any, **kwargs: Any) -> None:
        original(conn, *args, **kwargs)
        (study / "tests" / "t1.yaml").write_bytes(src.read_bytes())
        other = connect(study)
        try:
            register_test(other, {"name": "t1", "kind": "pilot", "path": "tests/t1.yaml",
                                  "sha256": sha, "openable": True}, [])
        finally:
            other.close()

    monkeypatch.setattr(push_mod, "_validate_test", racing)
    assert push_test(study, src) == "t1"
    assert _leftovers(study) == []


def test_file_changed_during_validation(
    study: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import consortium.stages.push as push_mod

    src = _write(tmp_path / "e", "t1", **_godspeed_fields(study))
    original = push_mod._validate_test

    def editing(*args: Any, **kwargs: Any) -> None:
        original(*args, **kwargs)
        src.write_text(src.read_text() + "# edited\n")

    monkeypatch.setattr(push_mod, "_validate_test", editing)
    _refused(study, src, "test_changed")


@pytest.mark.parametrize("damage", ["missing", "edited", "directory"])
def test_noop_checks_stored_file(study: Path, tmp_path: Path, damage: str) -> None:
    src = _write(tmp_path / "e", "t1", **_godspeed_fields(study))
    push_test(study, src)
    stored = study / "tests" / "t1.yaml"
    stored.unlink()
    if damage == "edited":
        stored.write_text("# edited\n")
    elif damage == "directory":
        stored.mkdir()
    code = "push_failed" if damage == "directory" else "test_exists"
    err = _refused(study, src, code)
    if code == "test_exists":
        assert "missing or was edited" in err.message


def test_unreadable_existing_tests_file_is_push_failed(study: Path, tmp_path: Path) -> None:
    (study / "tests" / "t1.yaml").mkdir()
    src = _write(tmp_path / "e", "t1", **_godspeed_fields(study))
    _refused(study, src, "push_failed")


def test_zero_targets_refused(study: Path, tmp_path: Path) -> None:
    _clips(study, 1)
    src = _write(tmp_path / "e", "t1", instruments=["godspeed"], clips=[],
                 session={"practice_clips": 0})
    assert "at least 1 target" in _refused(study, src, "bad_pairing").message


def test_pairwise_practice_same_clip_twice(study: Path, tmp_path: Path) -> None:
    targets, practice = _clips(study, 2), _clips(study, 1)
    src = _write(tmp_path / "e", "t1", instruments=["pairwise_alive"], clips=targets,
                 practice=[{"instrument": "pairwise_alive", "clips": practice * 2,
                            "answer": PAIRWISE_ANSWER}],
                 session={"practice_clips": 1})
    assert _refused(study, src, "bad_practice").message.startswith("practice[0]: ")


@pytest.mark.parametrize("name", ["a" * 65, "pilot-", "pilot_", "pilot1-attrition"])
def test_bad_name_length_and_trailing(study: Path, tmp_path: Path, name: str) -> None:
    src = _write(tmp_path / "e", name, instruments=["godspeed"])
    _refused(study, src, "bad_test_name")


def test_attrition_suffix_message(study: Path, tmp_path: Path) -> None:
    src = _write(tmp_path / "e", "x-attrition", instruments=["godspeed"])
    err = _refused(study, src, "bad_test_name")
    assert "-attrition" in err.message
    assert push_test(study, _write(tmp_path / "f", "attrition-x", **_godspeed_fields(study))) \
        == "attrition-x"


def test_name_at_64_characters_ok(study: Path, tmp_path: Path) -> None:
    name = "a" * 64
    assert push_test(study, _write(tmp_path / "e", name, **_godspeed_fields(study))) == name


def test_test_clips_foreign_keys_enforced(study: Path) -> None:
    from consortium.board.tests import register_test

    conn = connect(study)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            register_test(conn, {"name": "t1", "kind": "pilot", "path": "tests/t1.yaml",
                                 "sha256": "0" * 64, "openable": True},
                          [("c_notthere", "target")])
        assert get_test(conn, "t1") is None
    finally:
        conn.close()


def test_check_media_float_sum_at_limit() -> None:
    clips = {"c_a": {"duration_s": 0.1, "size_bytes": 1},
             "c_b": {"duration_s": 0.2, "size_bytes": 1}}
    shape = TrialShape("g", False, ("c_a",), ("c_b",))  # 0.1 + 0.2 = 0.30000000000000004
    assert check_media([shape], clips, [_model("m1", 0.3, 10)]) is None
