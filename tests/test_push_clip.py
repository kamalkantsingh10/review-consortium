"""consortium push clip: every row of the story 1.4 I/O matrix."""

from __future__ import annotations

import csv
import json
import logging
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from consortium.board.blinding import read_key
from consortium.board.db import MIGRATIONS
from consortium.cli import app
from consortium.core.errors import ConsortiumError
from consortium.core.ids import new_clip_id, session_id
from consortium.stages.init import init_study

runner = CliRunner()

HAVE_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
needs_ffmpeg = pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg/ffprobe not on PATH")

TOKEN = "zqxleaktoken42"


def _make_clip(
    path: Path,
    *,
    duration: float = 2.0,
    audio: bool = True,
    tags: dict[str, str] | None = None,
    size: str = "160x120",
    rate: int = 25,
) -> Path:
    args = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", f"testsrc=size={size}:rate={rate}:duration={duration}",
    ]
    if audio:  # mono 44.1 kHz
        args += ["-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=44100:duration={duration}"]
    for key, value in (tags or {}).items():
        args += ["-metadata", f"{key}={value}"]
    args += ["-c:v", "libx264", "-pix_fmt", "yuv420p"]
    if audio:
        args += ["-c:a", "aac", "-shortest"]
    args.append(str(path))
    subprocess.run(args, check=True)
    return path


@pytest.fixture(scope="module")
def media(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    if not HAVE_FFMPEG:
        pytest.skip("ffmpeg/ffprobe not on PATH")
    root = tmp_path_factory.mktemp("media")
    return {
        "av": _make_clip(root / "plain.mp4"),
        "long": _make_clip(root / "long.mp4", duration=4.0),
        "tagged": _make_clip(
            root / f"happy_{TOKEN}_take3.mp4",
            tags={"title": f"title {TOKEN}", "comment": f"comment {TOKEN}"},
        ),
        "video_only": _make_clip(root / "silent.mp4", audio=False),
        "wide": _make_clip(root / "wide.mp4", size="320x120"),
        "fps30": _make_clip(root / "fps30.mp4", rate=30),
        "cover_art": _make_cover_art(root / "cover.mp4"),
    }


def _make_cover_art(path: Path) -> Path:
    """Audio plus a single attached_pic image: no real video stream."""
    subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "sine=duration=2",
         "-f", "lavfi", "-i", "testsrc=size=64x64:duration=1",
         "-frames:v", "1", "-map", "0:a", "-map", "1:v", "-c:a", "aac", "-c:v", "mjpeg",
         "-disposition:v:0", "attached_pic", str(path)],
        check=True,
    )
    return path


def _ffprobe(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams",
         str(path)],
        check=True, capture_output=True, text=True,
    )
    return json.loads(out.stdout)


@pytest.fixture()
def study(tmp_path: Path) -> Path:
    return init_study(tmp_path / "study")


def _push(study: Path, src: Path, *conds: str, verbose: bool = False):
    args = ["-v"] if verbose else []
    args += ["push", "clip", str(src), "--study", str(study)]
    for cond in conds:
        args += ["--condition", cond]
    return runner.invoke(app, args)


def _snapshot(study: Path) -> dict[str, object]:
    clips = study / "clips"
    db = study / "board.db"
    rows = None
    if db.exists():
        with sqlite3.connect(db) as conn:
            rows = conn.execute("SELECT * FROM clips ORDER BY clip_id").fetchall()
    key = study / "blinding_key.csv"
    return {
        "clips": sorted(p.name for p in clips.iterdir()) if clips.exists() else None,
        "rows": rows,
        "key": key.read_bytes() if key.exists() else None,
    }


def _leak_rows(study: Path) -> list[dict[str, str]]:
    with (study / "exports" / "leak-report.csv").open(newline="") as fh:
        return list(csv.DictReader(fh))


# --------------------------------------------------------------------------- ids


def test_ids() -> None:
    ids = {new_clip_id() for _ in range(200)}
    assert len(ids) == 200
    for clip_id in ids:
        assert clip_id.startswith("c_") and len(clip_id) == 10
        assert set(clip_id[2:]) <= set("abcdefghijklmnopqrstuvwxyz234567")
    assert session_id("pilot", "p1-m1", 2) == "pilot/p1-m1/r2"


# --------------------------------------------------------------------------- matrix


@needs_ffmpeg
def test_first_push(study: Path, media: dict[str, Path]) -> None:
    result = _push(study, media["av"], "emotion=happy", "gait=fast")
    assert result.exit_code == 0, result.output
    clip_id = result.stdout.strip()
    assert clip_id.startswith("c_") and len(clip_id) == 10
    assert (study / "board.db").is_file()
    assert sorted(p.name for p in (study / "clips").iterdir()) == [f"{clip_id}.mp4"]
    assert read_key(study) == {clip_id: {"emotion": "happy", "gait": "fast"}}
    lines = (study / "blinding_key.csv").read_text().splitlines()
    assert lines == ["clip_id,factor,level", f"{clip_id},emotion,happy", f"{clip_id},gait,fast"]
    rows = _leak_rows(study)
    assert {(r["factor"], r["level"]) for r in rows} == {("emotion", "happy"), ("gait", "fast")}
    assert all(r["flagged"] == "false" for r in rows)

    with sqlite3.connect(study / "board.db") as conn:
        row = conn.execute("SELECT * FROM clips").fetchone()
    stored = study / "clips" / f"{clip_id}.mp4"
    assert row[0] == clip_id
    assert row[3] == stored.stat().st_size
    assert (row[4], row[5], row[6]) == (640, 480, 25.0)  # canonical profile height/fps
    assert 1.8 <= row[2] <= 2.2
    assert row[7] is not None


@needs_ffmpeg
def test_two_pushes_board_shape(study: Path, media: dict[str, Path]) -> None:
    assert _push(study, media["av"], "emotion=happy").exit_code == 0
    assert _push(study, media["av"], "emotion=happy").exit_code == 0  # no duplicate detection
    conn = sqlite3.connect(study / "board.db")
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        assert sorted(tables) == ["attempts", "clips", "test_clips", "tests", "trials"]
        assert conn.execute("SELECT COUNT(*) FROM clips").fetchone()[0] == 2
    finally:
        conn.close()


@needs_ffmpeg
def test_tagged_source_leaves_no_trace(
    study: Path, media: dict[str, Path], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    result = _push(study, media["tagged"], "emotion=happy", verbose=True)
    assert result.exit_code == 0, result.output
    clip_id = result.stdout.strip()
    stored = study / "clips" / f"{clip_id}.mp4"

    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams",
         str(stored)],
        check=True, capture_output=True, text=True,
    )
    data = json.loads(probe.stdout)
    user_tags = {"title", "comment", "artist", "album", "date", "description", "encoder"}
    assert not user_tags & set(data["format"].get("tags", {}))
    for stream in data["streams"]:
        assert not {"title", "comment"} & set(stream.get("tags", {}))
    assert [s["codec_type"] for s in data["streams"]] == ["video", "audio"]

    needle = TOKEN.encode()
    assert needle not in stored.read_bytes()
    for name in ("board.db", "board.db-wal"):
        path = study / name
        if path.exists():
            assert needle not in path.read_bytes()
            assert b"happy" not in path.read_bytes()
    output = result.stdout + result.stderr + caplog.text
    assert TOKEN not in output
    assert str(media["tagged"]) not in output
    assert "happy" not in output and "emotion" not in output


@needs_ffmpeg
def test_no_condition(study: Path, media: dict[str, Path]) -> None:
    first = _push(study, media["av"], "emotion=happy")
    practice = _push(study, media["av"])
    assert first.exit_code == 0 and practice.exit_code == 0, practice.output
    practice_id = practice.stdout.strip()
    assert (study / "clips" / f"{practice_id}.mp4").is_file()
    assert practice_id not in read_key(study)
    assert practice_id not in (study / "blinding_key.csv").read_text()
    assert all(r["n"] == "1" for r in _leak_rows(study))


@needs_ffmpeg
def test_no_condition_first_push_writes_no_key(study: Path, media: dict[str, Path]) -> None:
    result = _push(study, media["av"])
    assert result.exit_code == 0, result.output
    assert not (study / "blinding_key.csv").exists()
    assert read_key(study) == {}
    assert _leak_rows(study) == []


@pytest.mark.parametrize(
    "conds",
    [
        ("emotion",), ("=x",), ("emotion=",), ("emotion=happy", "emotion=sad"),
        ("emotion=ha\x01ppy",), ("emo\ttion=happy",),
    ],
)
def test_bad_condition(study: Path, tmp_path: Path, conds: tuple[str, ...]) -> None:
    src = tmp_path / "anything.mp4"
    src.write_bytes(b"not used")
    before = _snapshot(study)
    result = _push(study, src, *conds)
    assert result.exit_code == 1
    assert result.stderr.startswith("bad_condition: ")
    assert result.stdout == ""
    assert _snapshot(study) == before
    for cond in conds:
        for part in cond.split("="):
            if part:
                assert part not in result.stderr


@needs_ffmpeg
def test_ffmpeg_missing(
    study: Path, media: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty = tmp_path / "emptybin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    before = _snapshot(study)
    result = _push(study, media["av"], "emotion=happy")
    assert result.exit_code == 1
    assert result.stderr.startswith("ffmpeg_missing: ")
    assert _snapshot(study) == before


@needs_ffmpeg
def test_ffmpeg_too_old(
    study: Path, media: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = tmp_path / "oldbin"
    fake.mkdir()
    for tool in ("ffmpeg", "ffprobe"):
        script = fake / tool
        script.write_text(f"#!/bin/sh\necho '{tool} version 5.1.2 Copyright'\n")
        script.chmod(0o755)
    monkeypatch.setenv("PATH", str(fake))
    before = _snapshot(study)
    result = _push(study, media["av"], "emotion=happy")
    assert result.exit_code == 1
    assert result.stderr.startswith("ffmpeg_missing: ")
    assert _snapshot(study) == before


@needs_ffmpeg
def test_no_audio(study: Path, media: dict[str, Path]) -> None:
    before = _snapshot(study)
    result = _push(study, media["video_only"], "emotion=happy")
    assert result.exit_code == 1
    assert result.stderr.strip() == "no_audio: input file has no audio stream"
    assert _snapshot(study) == before


@needs_ffmpeg
def test_unreadable_missing_file(study: Path, tmp_path: Path) -> None:
    src = tmp_path / f"{TOKEN}.mp4"
    before = _snapshot(study)
    result = _push(study, src, "emotion=happy")
    assert result.exit_code == 1
    assert result.stderr.startswith("media_unreadable: input file")
    assert TOKEN not in result.stderr
    assert _snapshot(study) == before


@needs_ffmpeg
def test_unreadable_garbage(study: Path, media: dict[str, Path], tmp_path: Path) -> None:
    assert _push(study, media["av"], "emotion=happy").exit_code == 0
    src = tmp_path / f"{TOKEN}.mp4"
    src.write_bytes(b"\x00garbage" * 200)
    before = _snapshot(study)
    result = _push(study, src, "emotion=sad")
    assert result.exit_code == 1
    assert result.stderr.startswith("media_unreadable: input file")
    assert TOKEN not in result.stderr
    assert _snapshot(study) == before


@needs_ffmpeg
@pytest.mark.parametrize("prior_push", [True, False])
@pytest.mark.parametrize("target", ["replace", "fsync_dir"])
def test_failure_after_append_rolls_back(
    study: Path,
    media: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    prior_push: bool,
    target: str,
) -> None:
    from consortium.stages import push

    if prior_push:
        assert _push(study, media["av"], "emotion=happy").exit_code == 0
    before = _snapshot(study)

    def boom(*_a: object, **_k: object) -> None:
        raise OSError(28, "No space left on device")

    if target == "replace":
        monkeypatch.setattr(push.os, "replace", boom)
    else:  # fails after the file has been moved into place
        monkeypatch.setattr(push, "_fsync_dir", boom)
    with pytest.raises(ConsortiumError) as info:
        push.push_clip(study, media["av"], ["emotion=sad"])
    assert info.value.code == "push_failed"
    assert str(media["av"]) not in info.value.message
    after = _snapshot(study)
    if not prior_push:  # board.db may now exist, migrated but with no rows
        assert after.pop("rows") in (None, [])
        before.pop("rows")
    assert after == before
    if not prior_push:
        assert not (study / "blinding_key.csv").exists()
        assert not (study / "clips").exists()


@needs_ffmpeg
def test_leak_report_failure_still_prints_clip_id(
    study: Path, media: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    from consortium.stages import push

    def boom(*_a: object, **_k: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(push, "write_leak_report", boom)
    result = _push(study, media["av"], "emotion=happy")
    assert result.exit_code == 0, result.output
    clip_id = result.stdout.strip()
    assert (study / "clips" / f"{clip_id}.mp4").is_file()
    assert "leak_report_failed" in result.stderr


@needs_ffmpeg
def test_stored_audio_and_fps_are_canonical(study: Path, media: dict[str, Path]) -> None:
    result = _push(study, media["fps30"], "emotion=happy")
    assert result.exit_code == 0, result.output
    data = _ffprobe(study / "clips" / f"{result.stdout.strip()}.mp4")
    video, audio = data["streams"]
    assert video["avg_frame_rate"] == "25/1"
    assert video.get("sample_aspect_ratio", "1:1") == "1:1"
    assert video.get("color_primaries") == "bt709"
    assert video.get("color_range") == "tv"
    assert (audio["sample_rate"], audio["channels"]) == ("48000", 2)


@needs_ffmpeg
@pytest.mark.parametrize("name", ["-x.mp4", "a:b.mp4"])
def test_awkward_source_names(
    study: Path, media: dict[str, Path], tmp_path: Path, name: str
) -> None:
    from consortium.stages.push import push_clip

    src = tmp_path / name
    shutil.copy(media["av"], src)
    clip_id = push_clip(study, src, ["emotion=happy"])
    assert (study / "clips" / f"{clip_id}.mp4").is_file()


@needs_ffmpeg
def test_cover_art_only_is_rejected(study: Path, media: dict[str, Path]) -> None:
    before = _snapshot(study)
    result = _push(study, media["cover_art"], "emotion=happy")
    assert result.exit_code == 1
    assert result.stderr.strip() == "media_unreadable: input file has no video stream"
    assert _snapshot(study) == before


def test_odd_media_height_is_refused(study: Path, tmp_path: Path) -> None:
    yaml_path = study / "study.yaml"
    yaml_path.write_text(yaml_path.read_text().replace("height: 480", "height: 481"))
    src = tmp_path / "anything.mp4"
    src.write_bytes(b"not used")
    result = _push(study, src, "emotion=happy")
    assert result.exit_code == 1
    assert result.stderr.startswith("config_invalid: media.height")
    assert "even" in result.stderr


@needs_ffmpeg
def test_leak_flags_width_for_different_aspect_ratio(
    study: Path, media: dict[str, Path]
) -> None:
    assert _push(study, media["av"], "shape=narrow").exit_code == 0
    assert _push(study, media["wide"], "shape=wide").exit_code == 0
    rows = {(r["metric"], r["level"]): r for r in _leak_rows(study)}
    assert rows[("width", "narrow")]["flagged"] == "true"
    assert rows[("width", "wide")]["flagged"] == "true"
    assert rows[("height", "wide")]["flagged"] == "false"
    assert rows[("fps", "wide")]["flagged"] == "false"


@needs_ffmpeg
def test_leak_flag(study: Path, media: dict[str, Path]) -> None:
    for _ in range(2):
        assert _push(study, media["av"], "emotion=happy").exit_code == 0
    assert _push(study, media["long"], "emotion=sad").exit_code == 0
    rows = _leak_rows(study)
    assert list(rows[0]) == [
        "factor", "metric", "level", "n", "mean", "max_diff", "tolerance", "flagged",
    ]
    duration = {r["level"]: r for r in rows if r["metric"] == "duration_s"}
    assert duration["happy"]["n"] == "2" and duration["sad"]["n"] == "1"
    assert duration["happy"]["tolerance"] == "1.0"
    assert float(duration["happy"]["max_diff"]) > 1.0
    assert duration["happy"]["flagged"] == "true"
    assert duration["sad"]["flagged"] == "true"
    for metric in ("width", "height", "fps", "loudness_lufs"):
        assert {r["flagged"] for r in rows if r["metric"] == metric} == {"false"}


def test_newer_board_is_refused(study: Path) -> None:
    from consortium.board.db import connect

    with sqlite3.connect(study / "board.db") as conn:
        conn.execute("PRAGMA user_version = 99")
    with pytest.raises(ConsortiumError) as info:
        connect(study)
    assert info.value.code == "board_version_mismatch"
