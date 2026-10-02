"""The append-only Archive (AD-5): ``archive/requests.jsonl`` and ``archive/responses.jsonl``.

One canonical JSON object per line, keyed by ``(trial_id, attempt)``. Media
appear only as Clip ID + SHA-256 (the rendered request carries nothing else).
Every append is flushed and fsynced before it returns (and the ``archive/``
directory is fsynced when it or a file in it is first created). If a crash left
an unterminated last line, the next append first ends it with ``\n``, so it
never merges into the new record; readers ignore an unterminated final line.
Inside a dispatching command, appends run on the writer task only.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from consortium.core.clock import utc_now_ms
from consortium.core.render import TrialRequest, canonical_json

ARCHIVE_DIR = "archive"
REQUESTS_FILE = f"{ARCHIVE_DIR}/requests.jsonl"
RESPONSES_FILE = f"{ARCHIVE_DIR}/responses.jsonl"


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _append(study_dir: Path | str, rel: str, record: dict[str, Any]) -> None:
    path = Path(study_dir) / rel
    new_dir = not path.parent.exists()
    if new_dir:
        path.parent.mkdir()
        _fsync_dir(path.parent.parent)
    new_file = not path.exists()
    line = canonical_json(record) + b"\n"
    fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        size = os.fstat(fd).st_size
        if size and os.pread(fd, 1, size - 1) != b"\n":
            line = b"\n" + line  # end a line a crash left unterminated
        view = memoryview(line)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    finally:
        os.close(fd)
    if new_dir or new_file:
        _fsync_dir(path.parent)


def append_request(
    study_dir: Path | str,
    *,
    trial_id: str,
    attempt: int,
    seed: int,
    model_id: str,
    request: TrialRequest,
    ts: str | None = None,
) -> dict[str, Any]:
    """Append ``{trial_id, attempt, seed, model_id, request, request_sha256, ts}``.

    ``request_sha256`` is the SHA-256 of the request's canonical JSON. Returns the record.
    """
    body = canonical_json(request)
    record = {
        "trial_id": trial_id,
        "attempt": attempt,
        "seed": seed,
        "model_id": model_id,
        "request": json.loads(body),
        "request_sha256": hashlib.sha256(body).hexdigest(),
        "ts": ts or utc_now_ms(),
    }
    _append(study_dir, REQUESTS_FILE, record)
    return record


def append_response(
    study_dir: Path | str,
    *,
    trial_id: str,
    attempt: int,
    request_sha256: str,
    raw: str,
    usage: dict[str, int],
    model_build: str | None,
    category: str,
    ts: str | None = None,
) -> dict[str, Any]:
    """Append ``{trial_id, attempt, request_sha256, raw, usage, model_build, category, ts}``.

    ``request_sha256`` is that of the attempt's request line. Returns the record.
    """
    record = {
        "trial_id": trial_id,
        "attempt": attempt,
        "request_sha256": request_sha256,
        "raw": raw,
        "usage": dict(usage),
        "model_build": model_build,
        "category": category,
        "ts": ts or utc_now_ms(),
    }
    _append(study_dir, RESPONSES_FILE, record)
    return record

