"""Opt-in live smoke test for the Gemini adapter (story 2.2).

Runs only with ``-m live`` and ``GEMINI_API_KEY`` set (and ffmpeg on PATH): one
2 s ffmpeg ``testsrc`` Clip, one Trial, end to end through the real API.
Optional ``GEMINI_LIVE_MODEL`` picks the pinned model (default below).
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from consortium.core.render import Anchors, ClipRef, RequestItem, TrialRequest
from consortium.core.validate import validate_response
from consortium.raters.base import ModelSpec, RaterCall
from consortium.raters.gemini import GeminiRater

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not os.environ.get("GEMINI_API_KEY"), reason="GEMINI_API_KEY not set"),
    pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not on PATH"),
]

MODEL = os.environ.get("GEMINI_LIVE_MODEL", "gemini-3-flash-preview")
ITEM = RequestItem("animacy_1", "likert", "Dead / Alive", 5, Anchors("Dead", "Alive"), None)
SCHEMA = {"type": "object",
          "properties": {"animacy_1": {"type": "integer", "minimum": 1, "maximum": 5}},
          "required": ["animacy_1"], "additionalProperties": False}


def test_live_one_trial(tmp_path: Path) -> None:
    clip_id = "c_livetest"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", "testsrc=duration=2:size=320x240:rate=25",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-shortest",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
         str(tmp_path / f"{clip_id}.mp4")],
        check=True,
    )
    spec = ModelSpec(
        model_id="m1", provider="gemini", model=MODEL, temperature=0.7, fps=1.0,
        seed_supported=True, media_resolution="low", thinking_level="low",
        api_key=os.environ["GEMINI_API_KEY"], max_output_tokens=1024, clips_dir=tmp_path,
    )
    rater = GeminiRater(spec)
    sha = hashlib.sha256((tmp_path / f"{clip_id}.mp4").read_bytes()).hexdigest()
    request = TrialRequest(
        "You are a 34-year-old who has seen a few robots.", "Rate the video.", (ITEM,), SCHEMA,
        "Watch the video and answer with JSON only.", (), (ClipRef(clip_id, sha),),
    )

    async def go():
        try:
            media = (await rater.prepare(request.clips[0]),)
            handles = await rater.submit([RaterCall("live/t1", 1, 12345, request, media)])
            return (await rater.collect(handles))[0]
        finally:
            name = rater.file_names.get(clip_id)
            if name is not None:
                await rater.client.aio.files.delete(name=name)

    result = asyncio.run(go())
    assert result.category == "ok", result.raw
    assert result.model_build
    assert spec.api_key not in result.raw
    parsed = validate_response(result.raw, SimpleNamespace(name="live", items=(ITEM,)))
    assert 1 <= parsed.answers["animacy_1"] <= 5
