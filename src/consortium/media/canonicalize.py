"""Clip canonicalization (AD-11): ffmpeg/ffprobe subprocesses only.

``canonicalize`` re-encodes a source file to the Study's one media profile with
every piece of metadata stripped. The file name, tags, chapters, container,
codec, frame rate, sample rate, channel layout, sample aspect ratio and colour
tags of the source do not survive ingest. Its aspect ratio does (the width is
scaled to keep it), which the leak report checks. Error messages and logs never
name the source path; they say "input file".
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Protocol

from consortium.core.errors import ConsortiumError

log = logging.getLogger(__name__)

MIN_FFMPEG_MAJOR = 6
# Fixed audio layout: a source's sample rate or channel count must not survive ingest.
_AUDIO_RATE = 48000
_AUDIO_CHANNELS = 2
_LUFS = re.compile(r"^\s*I:\s+(-?[0-9.]+|-?inf)\s+LUFS\s*$", re.MULTILINE)
_VERSION = re.compile(r"version\s+n?(\d+)\.")
TIMEOUT_S = 600


class MediaProfileLike(Protocol):
    """The fields of ``config.models.MediaProfile`` that drive the re-encode."""

    height: int
    video_kbps: int
    audio_kbps: int
    fps: int


@dataclass(frozen=True)
class ClipInfo:
    duration_s: float
    size_bytes: int
    width: int
    height: int
    fps: float
    loudness_lufs: float | None  # None when the audio is silent (-inf LUFS)
    has_audio: bool


def _run(args: list[str], timeout: float = TIMEOUT_S) -> subprocess.CompletedProcess[str]:
    """Run a subprocess; a timeout is reported as ``media_unreadable``."""
    try:
        return subprocess.run(
            args, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=timeout
        )
    except subprocess.TimeoutExpired as err:
        raise _unreadable("timed out in ffmpeg") from err


def _url(path: Path) -> str:
    """Absolute ``file:`` URL, so names starting with '-' or holding ':' are not misparsed."""
    return "file:" + str(path.resolve())


def check_ffmpeg() -> None:
    """Raise ``ffmpeg_missing`` unless ffmpeg and ffprobe >= 6 are on PATH."""
    for tool in ("ffmpeg", "ffprobe"):
        exe = shutil.which(tool)
        if exe is None:
            raise ConsortiumError("ffmpeg_missing", f"{tool} not found on PATH (need >= 6)")
        try:
            out = _run([exe, "-version"], timeout=30)
        except (OSError, ConsortiumError) as err:
            raise ConsortiumError("ffmpeg_missing", f"{tool} cannot be run") from err
        first = out.stdout.splitlines()[0] if out.stdout else ""
        if out.returncode != 0 or not first.startswith(f"{tool} version"):
            raise ConsortiumError("ffmpeg_missing", f"{tool} cannot be run")
        match = _VERSION.search(first)
        # Unnumbered (git snapshot) builds are accepted; numbered ones must be >= 6.
        if match and int(match.group(1)) < MIN_FFMPEG_MAJOR:
            raise ConsortiumError(
                "ffmpeg_missing", f"{tool} {match.group(1)}.x found; need >= {MIN_FFMPEG_MAJOR}"
            )


def _unreadable(detail: str) -> ConsortiumError:
    return ConsortiumError("media_unreadable", f"input file {detail}")


def _rate(value: str | None) -> float:
    if not value or value in ("0/0", "0"):
        return 0.0
    return float(Fraction(value))


def _loudness(path: Path) -> float | None:
    out = _run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-nostats", "-i", _url(path),
         "-map", "0:a:0", "-af", "ebur128=framelog=quiet", "-f", "null", "-"]
    )
    found = _LUFS.findall(out.stderr) if out.returncode == 0 else []
    if not found:
        raise _unreadable("audio could not be decoded")
    value = found[0]  # the first "I:" line is the integrated loudness
    return None if value.endswith("inf") else float(value)


def _streams(path: Path) -> tuple[dict, dict, dict | None]:
    """ffprobe JSON, the first real video stream (cover art skipped), the first audio stream."""
    if not path.is_file():
        raise _unreadable("not found")
    out = _run(
        ["ffprobe", "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", _url(path)]
    )
    if out.returncode != 0:
        raise _unreadable("could not be read")
    try:
        data = json.loads(out.stdout)
        streams = data.get("streams", [])
    except ValueError as err:
        raise _unreadable("could not be read") from err
    video = next(
        (
            s for s in streams
            if s.get("codec_type") == "video"
            and not (s.get("disposition") or {}).get("attached_pic")
        ),
        None,
    )
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if video is None:
        raise _unreadable("has no video stream")
    return data, video, audio


def probe(path: Path | str) -> ClipInfo:
    """Duration, size, resolution, fps (ffprobe JSON) and integrated loudness (ebur128)."""
    path = Path(path)
    data, video, audio = _streams(path)
    has_audio = audio is not None
    try:
        duration = float(data.get("format", {}).get("duration") or video.get("duration") or 0)
        width, height = int(video["width"]), int(video["height"])
        fps = _rate(video.get("avg_frame_rate")) or _rate(video.get("r_frame_rate"))
    except (ValueError, KeyError, TypeError, ZeroDivisionError) as err:
        raise _unreadable("could not be read") from err
    if duration <= 0 or fps <= 0 or width <= 0 or height <= 0:
        raise _unreadable("has no decodable frames")
    loudness = _loudness(path) if has_audio else None
    return ClipInfo(
        duration_s=round(duration, 3),
        size_bytes=path.stat().st_size,
        width=width,
        height=height,
        fps=round(fps, 3),
        loudness_lufs=None if loudness is None else round(loudness, 1),
        has_audio=has_audio,
    )


def canonicalize(src: Path | str, dst: Path | str, profile: MediaProfileLike) -> ClipInfo:
    """Re-encode ``src`` to ``dst`` (MP4) in the canonical ``profile``; return ``dst``'s info.

    Raises ``ffmpeg_missing``, ``media_unreadable`` or ``no_audio``. On failure
    ``dst`` may hold a partial file; the caller removes it.
    """
    check_ffmpeg()
    src, dst = Path(src), Path(dst)
    _, video, audio = _streams(src)
    if audio is None:
        raise ConsortiumError("no_audio", "input file has no audio stream")
    probe(src)  # refuses sources with no decodable frames or undecodable audio
    args = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-i", _url(src),
        "-map", f"0:{int(video['index'])}", "-map", f"0:{int(audio['index'])}",
        "-map_metadata", "-1", "-map_chapters", "-1",
        "-vf",
        f"scale=-2:{profile.height}:flags=bicubic+bitexact"
        f":out_color_matrix=bt709:out_range=tv,setsar=1,fps={profile.fps},format=yuv420p",
        "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
        "-color_range", "tv",
        "-c:v", "libx264", "-b:v", f"{profile.video_kbps}k",
        "-maxrate", f"{profile.video_kbps}k", "-bufsize", f"{profile.video_kbps * 2}k",
        "-c:a", "aac", "-b:a", f"{profile.audio_kbps}k",
        "-ar", str(_AUDIO_RATE), "-ac", str(_AUDIO_CHANNELS),
        "-fflags", "+bitexact", "-flags:v", "+bitexact", "-flags:a", "+bitexact",
        "-movflags", "+faststart",
        "-f", "mp4", _url(dst),
    ]
    result = _run(args)
    if result.returncode != 0:
        # ffmpeg's stderr names the source path, so it is never logged or shown.
        log.debug("ffmpeg re-encode exited with %d", result.returncode)
        raise _unreadable("could not be decoded")
    return probe(dst)
