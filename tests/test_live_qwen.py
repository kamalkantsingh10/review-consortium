"""Opt-in live smoke test for the Qwen adapter (story 2.3).

Runs only with ``-m live`` and both ``DASHSCOPE_API_KEY`` and ``DASHSCOPE_BASE_URL``
set (and ffmpeg on PATH): one 2 s ffmpeg ``testsrc`` Clip, one Trial, end to end
through the real OpenAI-compatible endpoint; it asserts the Trial's category is
``ok`` (a valid answer, archived settings and usage). Optional
``QWEN_LIVE_MODEL`` picks the pinned model (default below).
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from consortium.core.render import Anchors, ClipRef, RequestItem, TrialRequest
from consortium.raters.base import CATEGORIES, ModelSpec, RaterCall
from consortium.raters.qwen import QwenRater, is_model_studio

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not os.environ.get("DASHSCOPE_API_KEY"),
                       reason="DASHSCOPE_API_KEY not set"),
    pytest.mark.skipif(not os.environ.get("DASHSCOPE_BASE_URL"),
                       reason="DASHSCOPE_BASE_URL not set"),
    pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not on PATH"),
]

MODEL = os.environ.get("QWEN_LIVE_MODEL", "qwen3.8-omni-flash")
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
        model_id="m1", provider="qwen", model=MODEL, temperature=0.7, fps=None,
        seed_supported=True, media_resolution=None, thinking_level=None,
        api_key=os.environ["DASHSCOPE_API_KEY"], max_output_tokens=1024, clips_dir=tmp_path,
        base_url=os.environ["DASHSCOPE_BASE_URL"],
    )
    rater = QwenRater(spec)
    sha = hashlib.sha256((tmp_path / f"{clip_id}.mp4").read_bytes()).hexdigest()
    request = TrialRequest(
        "You are a 34-year-old who has seen a few robots.", "Rate the video.", (ITEM,), SCHEMA,
        "Watch the video and answer with JSON only.", (), (ClipRef(clip_id, sha),),
    )

    async def go():
        media = (await rater.prepare(request.clips[0]),)
        handles = await rater.submit([RaterCall("live/t1", 1, 12345, request, media)])
        return (await rater.collect(handles))[0]

    async def go_and_close():
        try:
            return await go()
        finally:
            await rater.aclose()

    result = asyncio.run(go_and_close())
    assert result.category in CATEGORIES
    assert result.category == "ok", result.raw
    assert result.raw and spec.api_key not in result.raw
    assert result.usage.get("input_tokens", 0) > 0
    assert result.settings["temperature"]["documented"] is (
        False if is_model_studio(spec.base_url) else None)
