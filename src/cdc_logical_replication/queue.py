from __future__ import annotations

import asyncio

from cdc_logical_replication.models import ChangeEvent


class InflightEventQueue:
    """Bounded queue constrained by both record count and total bytes."""

    def __init__(self, *, max_messages: int, max_bytes: int) -> None:
        if max_messages <= 0:
            raise ValueError("max_messages must be > 0")
        if max_bytes <= 0:
            raise ValueError("max_bytes must be > 0")

        self._queue: asyncio.Queue[ChangeEvent] = asyncio.Queue(maxsize=max_messages)
        self._max_bytes = max_bytes
        self._bytes_inflight = 0
        self._bytes_lock = asyncio.Condition()

    @property
    def bytes_inflight(self) -> int:
        return self._bytes_inflight

    @property
    def max_bytes(self) -> int:
        return self._max_bytes

    def qsize(self) -> int:
        return self._queue.qsize()

    async def put(self, event: ChangeEvent) -> None:
        size = event.record_size_bytes
        if size > self._max_bytes:
            raise ValueError(
                f"Event size ({size}) exceeds queue byte capacity ({self._max_bytes})"
            )

        async with self._bytes_lock:
            await self._bytes_lock.wait_for(lambda: self._bytes_inflight + size <= self._max_bytes)
            self._bytes_inflight += size

        try:
            await self._queue.put(event)
        except BaseException:
            async with self._bytes_lock:
                self._release_bytes(size)
            raise

    async def get(self) -> ChangeEvent:
        return await self._queue.get()

    async def task_done(self, event: ChangeEvent) -> None:
        self._queue.task_done()
        async with self._bytes_lock:
            self._release_bytes(event.record_size_bytes)

    async def join(self) -> None:
        await self._queue.join()

    def _release_bytes(self, size: int) -> None:
        self._bytes_inflight -= size
        if self._bytes_inflight < 0:
            self._bytes_inflight = 0
        self._bytes_lock.notify_all()
