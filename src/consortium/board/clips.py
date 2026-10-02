"""SQL for the ``clips`` table (m1)."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any, Protocol

_COLUMNS = (
    "clip_id", "sha256", "duration_s", "size_bytes", "width", "height", "fps",
    "loudness_lufs", "pushed_at",
)


class ClipInfoLike(Protocol):
    duration_s: float
    size_bytes: int
    width: int
    height: int
    fps: float
    loudness_lufs: float | None


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def clip_exists(conn: sqlite3.Connection, clip_id: str) -> bool:
    return conn.execute("SELECT 1 FROM clips WHERE clip_id = ?", (clip_id,)).fetchone() is not None


def insert_clip(conn: sqlite3.Connection, clip_id: str, sha256: str, info: ClipInfoLike) -> None:
    """Insert one Clip row. Runs inside the caller's transaction."""
    conn.execute(
        f"INSERT INTO clips ({', '.join(_COLUMNS)}) VALUES ({', '.join('?' * len(_COLUMNS))})",
        (
            clip_id, sha256, info.duration_s, info.size_bytes, info.width, info.height,
            info.fps, info.loudness_lufs, _now(),
        ),
    )


def list_clips(conn: sqlite3.Connection, ids: Iterable[str] | None = None) -> list[dict[str, Any]]:
    """Clip rows as dicts, ordered by ``clip_id``; only ``ids`` when given."""
    sql = f"SELECT {', '.join(_COLUMNS)} FROM clips"
    params: list[str] = []
    if ids is not None:
        params = sorted(set(ids))
        if not params:
            return []
        sql += f" WHERE clip_id IN ({', '.join('?' * len(params))})"
    rows = conn.execute(sql + " ORDER BY clip_id", params).fetchall()
    return [dict(zip(_COLUMNS, row, strict=True)) for row in rows]
