"""The one timestamp format for Run records: UTC ISO 8601, milliseconds, ``Z``."""

from __future__ import annotations

from datetime import UTC, datetime


def utc_now_ms() -> str:
    """For example ``2026-10-02T09:15:04.123Z``."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
