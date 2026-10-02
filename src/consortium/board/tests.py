"""SQL for the ``tests`` and ``test_clips`` tables (m2)."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from consortium.board.db import transaction

_COLUMNS = ("name", "kind", "path", "sha256", "openable", "registered_at")
ROLES = ("target", "practice")


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_test(conn: sqlite3.Connection, name: str) -> dict[str, Any] | None:
    """The registered Test row, with ``openable`` as a bool and its ``clips``; None if absent.

    ``clips`` is a list of ``(clip_id, role)`` ordered by role then Clip ID.
    """
    row = conn.execute(
        f"SELECT {', '.join(_COLUMNS)} FROM tests WHERE name = ?", (name,)
    ).fetchone()
    if row is None:
        return None
    out = dict(zip(_COLUMNS, row, strict=True))
    out["openable"] = bool(out["openable"])
    out["clips"] = [
        (clip_id, role)
        for clip_id, role in conn.execute(
            "SELECT clip_id, role FROM test_clips WHERE test = ? ORDER BY role DESC, clip_id",
            (name,),
        )
    ]
    return out


def register_test(
    conn: sqlite3.Connection,
    row: Mapping[str, Any],
    clips: Iterable[tuple[str, str]],
) -> None:
    """Insert one Test row and its ``(clip_id, role)`` rows in one transaction.

    ``row`` holds ``name``, ``kind``, ``path``, ``sha256`` and ``openable``;
    ``registered_at`` is set here. Raises ``sqlite3.IntegrityError`` if the
    name is already registered.
    """
    with transaction(conn):
        insert_test(conn, row, clips)


def insert_test(
    conn: sqlite3.Connection,
    row: Mapping[str, Any],
    clips: Iterable[tuple[str, str]],
) -> None:
    """``register_test`` inside the caller's transaction."""
    conn.execute(
        f"INSERT INTO tests ({', '.join(_COLUMNS)}) VALUES ({', '.join('?' * len(_COLUMNS))})",
        (
            row["name"], row["kind"], row["path"], row["sha256"], int(bool(row["openable"])),
            _now(),
        ),
    )
    conn.executemany(
        "INSERT INTO test_clips (test, clip_id, role) VALUES (?, ?, ?)",
        [(row["name"], clip_id, role) for clip_id, role in clips],
    )


def target_clip_kinds(
    conn: sqlite3.Connection, clip_ids: Iterable[str]
) -> dict[str, list[tuple[str, str]]]:
    """For each Clip that is a target of a registered Test: ``[(test, kind), ...]`` by Test name.

    Clips that are no Test's target are absent. Practice uses are not counted.
    """
    ids = sorted(set(clip_ids))
    if not ids:
        return {}
    rows = conn.execute(
        "SELECT tc.clip_id, t.name, t.kind FROM test_clips tc JOIN tests t ON t.name = tc.test"
        f" WHERE tc.role = 'target' AND tc.clip_id IN ({', '.join('?' * len(ids))})"
        " ORDER BY tc.clip_id, t.name",
        ids,
    ).fetchall()
    out: dict[str, list[tuple[str, str]]] = {}
    for clip_id, name, kind in rows:
        out.setdefault(clip_id, []).append((name, kind))
    return out
