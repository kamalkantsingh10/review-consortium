"""The single seed rule (AD-10): every seed is derived from ``study.seed``."""

from __future__ import annotations

import hashlib


def derive_seed(study_seed: int, purpose: str, key: str) -> int:
    """``sha256("<study_seed>:<purpose>:<key>")``, first 8 hex digits as int, masked to 31 bits.

    Use the result only through ``random.Random(seed)``.
    """
    digest = hashlib.sha256(f"{study_seed}:{purpose}:{key}".encode()).hexdigest()
    return int(digest[:8], 16) & 0x7FFFFFFF
