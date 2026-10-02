"""Shared ID rules (pure)."""

from __future__ import annotations

import secrets

_BASE32 = "abcdefghijklmnopqrstuvwxyz234567"
CLIP_ID_PREFIX = "c_"
CLIP_ID_LENGTH = 8


def new_clip_id() -> str:
    """A fresh Clip ID: ``c_`` + 8 lowercase base32 characters from ``secrets``.

    Random by design (AD-11): the ID carries nothing about the source or its
    Condition. Callers retry on the (unlikely) collision with an existing Clip.
    """
    return CLIP_ID_PREFIX + "".join(secrets.choice(_BASE32) for _ in range(CLIP_ID_LENGTH))


def session_id(test: str, agent: str, repeat: int) -> str:
    """Session ID ``<test>/<agent>/r<repeat>``."""
    return f"{test}/{agent}/r{repeat}"
