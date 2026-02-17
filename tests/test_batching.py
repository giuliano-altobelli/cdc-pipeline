from __future__ import annotations

import asyncio
import time

from src.cdc_logical_replication.batching import MicroBatcher
from src.cdc_logical_replication.models import ChangeEvent
from src.cdc_logical_replication.queue import InflightEventQueue


def _event(lsn: int, payload_size: int) -> ChangeEvent:
    return ChangeEvent(lsn=lsn, payload=(b"x" * payload_size), partition_key="pk")


def test_batching_honors_record_limit() -> None:
    async def scenario() -> None:
        queue = InflightEventQueue(max_messages=10, max_bytes=1000)
        await queue.put(_event(1, 10))
        await queue.put(_event(2, 10))
        await queue.put(_event(3, 10))

        batcher = MicroBatcher(max_records=2, max_bytes=1000, max_delay_ms=100)
        first = await batcher.next_batch(queue)
        second = await batcher.next_batch(queue)

        assert [e.lsn for e in first] == [1, 2]
        assert [e.lsn for e in second] == [3]

    asyncio.run(scenario())


def test_batching_honors_byte_limit() -> None:
    async def scenario() -> None:
        queue = InflightEventQueue(max_messages=10, max_bytes=1000)
        await queue.put(_event(1, 10))
        await queue.put(_event(2, 10))

        # Each event is 12 bytes once partition key bytes are included.
        batcher = MicroBatcher(max_records=10, max_bytes=20, max_delay_ms=100)
        first = await batcher.next_batch(queue)
        second = await batcher.next_batch(queue)

        assert [e.lsn for e in first] == [1]
        assert [e.lsn for e in second] == [2]

    asyncio.run(scenario())


def test_batching_honors_delay_limit() -> None:
    async def scenario() -> None:
        queue = InflightEventQueue(max_messages=10, max_bytes=1000)
        await queue.put(_event(1, 10))

        batcher = MicroBatcher(max_records=10, max_bytes=1000, max_delay_ms=40)
        started = time.monotonic()
        batch = await batcher.next_batch(queue)
        elapsed = time.monotonic() - started

        assert [e.lsn for e in batch] == [1]
        assert elapsed >= 0.03

    asyncio.run(scenario())
