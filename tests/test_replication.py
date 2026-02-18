from __future__ import annotations

import asyncio
from collections import deque
from types import SimpleNamespace

import pytest
from src.cdc_logical_replication.ack import AckTracker
from src.cdc_logical_replication.protocol import XLOGDATA_HDR, lsn_int_to_str
from src.cdc_logical_replication.queue import InflightEventQueue
from src.cdc_logical_replication.replication import _replication_loop


class _StubReplicationCopy:
    def __init__(self, *, frames: list[bytes]) -> None:
        self._frames: deque[bytes] = deque(frames)
        self.writes: list[bytes] = []

    async def read(self) -> memoryview | None:
        if self._frames:
            return memoryview(self._frames.popleft())

        await asyncio.sleep(3600)
        return None

    async def write(self, payload: bytes) -> None:
        self.writes.append(payload)


def _xlogdata_frame(*, wal_start: int, wal_end: int, payload: bytes = b"{}") -> bytes:
    return XLOGDATA_HDR.pack(b"w", wal_start, wal_end, 0) + payload


def test_replication_loop_uses_wal_start_for_register_and_event_lsn() -> None:
    async def scenario() -> None:
        queue = InflightEventQueue(max_messages=8, max_bytes=8_000_000)
        ack_tracker = AckTracker(initial_lsn=402_348_000)
        frontier_updates: asyncio.Queue[int] = asyncio.Queue()
        settings = SimpleNamespace(
            replication_feedback_interval_s=60.0,
            partition_key_mode="fallback",
            partition_key_fallback="lsn",
            partition_key_static_value=None,
        )

        wal_start = 402_348_536
        copy = _StubReplicationCopy(
            frames=[
                _xlogdata_frame(wal_start=wal_start, wal_end=402_348_984),
                _xlogdata_frame(wal_start=wal_start, wal_end=402_348_536),
            ]
        )

        loop_task = asyncio.create_task(
            _replication_loop(
                copy=copy,
                settings=settings,
                queue=queue,
                ack_tracker=ack_tracker,
                frontier_updates=frontier_updates,
            )
        )
        try:
            first = await asyncio.wait_for(queue.get(), timeout=1.0)
            second = await asyncio.wait_for(queue.get(), timeout=1.0)

            assert first.ack_id == 1
            assert second.ack_id == 2
            assert first.lsn == wal_start
            assert second.lsn == wal_start
            assert first.partition_key == lsn_int_to_str(wal_start)
            assert second.partition_key == lsn_int_to_str(wal_start)
            assert ack_tracker.pending_count == 2
            assert not loop_task.done()

            await queue.task_done(first)
            await queue.task_done(second)
        finally:
            loop_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await loop_task

    asyncio.run(scenario())
