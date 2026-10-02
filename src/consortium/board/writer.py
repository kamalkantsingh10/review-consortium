"""The single writer task (AD-3).

Inside a dispatching command, one asyncio task owns the ``board.db`` connection
and performs every write, and every Archive append, in queue order. Other tasks
submit operations with ``Writer.do`` and await their results; ordering comes
from the queue, not from locks.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from consortium.board.db import connect

Spy = Callable[[str, tuple[Any, ...]], None]


class Writer:
    """Queue front-end of the writer task; create it with ``start_writer``."""

    def __init__(self, conn: sqlite3.Connection, spy: Spy | None = None) -> None:
        self._conn = conn
        self._queue: asyncio.Queue[tuple | None] = asyncio.Queue()
        self._spy = spy

    async def do[T](
        self, op: str, fn: Callable[[sqlite3.Connection], T], *key: Any
    ) -> T:
        """Queue ``fn(conn)`` as operation ``op`` and return its result (or raise its error).

        ``key`` (for example ``trial_id, attempt``) is passed to the spy only.
        """
        future: asyncio.Future[T] = asyncio.get_running_loop().create_future()
        await self._queue.put((op, fn, key, future))
        return await future

    async def _run(self) -> None:
        while True:
            entry = await self._queue.get()
            if entry is None:
                return
            op, fn, key, future = entry
            if future.cancelled():
                continue
            try:
                if self._spy is not None:
                    self._spy(op, key)
                result = fn(self._conn)
            except BaseException as err:  # handed to the submitter
                future.set_exception(err)
            else:
                future.set_result(result)


@asynccontextmanager
async def start_writer(study_dir: Path | str, *, spy: Spy | None = None) -> AsyncIterator[Writer]:
    """Open ``board.db`` (migrating it) and run the writer task until the block exits.

    The connection is closed when the task stops; queued operations finish first.
    """
    conn = connect(study_dir)
    writer = Writer(conn, spy)
    task = asyncio.create_task(writer._run(), name="board-writer")
    try:
        yield writer
    finally:
        await writer._queue.put(None)
        try:
            await task
        finally:
            conn.close()


def migrate(study_dir: Path | str) -> None:
    """Bring an existing ``board.db`` to the current layout (call it holding the lease).

    Raises ``board_version_mismatch`` for a newer layout.
    """
    connect(study_dir).close()
