"""board.lock: one dispatcher per Study; the OS releases the lease when the process dies."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from consortium.board.lease import LOCK_FILE, acquire_lease
from consortium.core.errors import ConsortiumError

HOLDER = """
import sys, time
from consortium.board.lease import acquire_lease
with acquire_lease(sys.argv[1]):
    print("held", flush=True)
    time.sleep(60)
"""


def test_second_lease_in_process_is_busy(tmp_path: Path) -> None:
    with acquire_lease(tmp_path):
        assert (tmp_path / LOCK_FILE).exists()
        with pytest.raises(ConsortiumError) as info, acquire_lease(tmp_path):
            pass
        assert info.value.code == "study_busy"
    with acquire_lease(tmp_path):  # released on exit
        pass


def test_lease_held_by_other_process_then_freed_when_it_dies(tmp_path: Path) -> None:
    proc = subprocess.Popen([sys.executable, "-c", HOLDER, str(tmp_path)],
                            stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout is not None and proc.stdout.readline().strip() == "held"
        with pytest.raises(ConsortiumError) as info, acquire_lease(tmp_path):
            pass
        assert info.value.code == "study_busy"
    finally:
        proc.kill()
        proc.wait()
    deadline = time.monotonic() + 5
    while True:
        try:
            with acquire_lease(tmp_path):
                break
        except ConsortiumError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.05)
