"""The append-only Archive (AD-5): ``archive/requests.jsonl`` and ``archive/responses.jsonl``.

One canonical JSON object per line, keyed by ``(trial_id, attempt)``. Media
appear only as Clip ID + SHA-256 (the rendered request carries nothing else).
Every append is flushed and fsynced before it returns (and the ``archive/``
directory is fsynced when it or a file in it is first created). If a crash left
an unterminated last line, the next append first ends it with ``\n``, so it
never merges into the new record; readers ignore an unterminated final line
and skip (and count) any line that is not valid JSON, the fragment such a
crash leaves; a line that parses but is not a keyed record is
``archive_corrupt``.

Readers (``read_requests``, ``read_responses``) key records by
``(trial_id, attempt)`` and take the **last line per key**: a Run killed
after a response append but before the Trial's state write appends a second
response line for the same key when it is resumed, and that line wins.
Inside a dispatching command, appends run on the writer task only.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from consortium.core.clock import utc_now_ms
from consortium.core.errors import ConsortiumError
from consortium.core.render import TrialRequest, canonical_json

ARCHIVE_DIR = "archive"
REQUESTS_FILE = f"{ARCHIVE_DIR}/requests.jsonl"
RESPONSES_FILE = f"{ARCHIVE_DIR}/responses.jsonl"

Key = tuple[str, int]  # (trial_id, attempt)

log = logging.getLogger(__name__)


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
    settings: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Append ``{trial_id, attempt, request_sha256, raw, usage, model_build, category, ts}``.

    ``request_sha256`` is that of the attempt's request line. ``settings`` (the
    settings the adapter sent, ``{name: {value, documented}}``) is added as a
    ``settings`` field only when not None. Returns the record.
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
    if settings is not None:
        record["settings"] = dict(settings)
    _append(study_dir, RESPONSES_FILE, record)
    return record



def read_lines(study_dir: Path | str, rel: str) -> tuple[list[dict[str, Any]], int]:
    """Every record of the Archive file ``rel`` in file order, and the number of fragments.

    A fragment is a line that is not valid JSON (what a crash mid-append leaves);
    it is skipped and counted. An unterminated final line is ignored (not
    counted). A line that parses but is not an object with a string
    ``trial_id`` and an integer ``attempt`` is corruption: ``archive_corrupt``.
    """
    path = Path(study_dir) / rel
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return [], 0
    lines = data.split(b"\n")
    lines.pop()  # empty after a final "\n", else an unterminated last line: ignored
    records: list[dict[str, Any]] = []
    fragments = 0
    for number, line in enumerate(lines, start=1):
        try:
            record = json.loads(line)
        except ValueError:
            log.warning("%s line %d: a fragment, not a JSON record; skipped", rel, number)
            fragments += 1
            continue
        if not (
            isinstance(record, dict)
            and isinstance(record.get("trial_id"), str)
            and isinstance(record.get("attempt"), int)
            and not isinstance(record.get("attempt"), bool)
        ):
            raise ConsortiumError(
                "archive_corrupt",
                f"{rel}:{number}: not a record with a string trial_id and an integer attempt",
                path=rel,
            )
        records.append(record)
    return records, fragments


def _read(study_dir: Path | str, rel: str) -> dict[Key, dict[str, Any]]:
    out: dict[Key, dict[str, Any]] = {}
    for record in read_lines(study_dir, rel)[0]:
        key = (record["trial_id"], record["attempt"])
        out.pop(key, None)  # re-insert, so the dict follows the order of the winning lines
        out[key] = record
    return out


def read_requests(study_dir: Path | str) -> dict[Key, dict[str, Any]]:
    """Every ``archive/requests.jsonl`` record by ``(trial_id, attempt)``; last line wins."""
    return _read(study_dir, REQUESTS_FILE)


def read_responses(study_dir: Path | str) -> dict[Key, dict[str, Any]]:
    """Every ``archive/responses.jsonl`` record by ``(trial_id, attempt)``; last line wins."""
    return _read(study_dir, RESPONSES_FILE)
