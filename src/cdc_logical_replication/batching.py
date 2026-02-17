from __future__ import annotations

import asyncio
from time import monotonic

from cdc_logical_replication.models import ChangeEvent
from cdc_logical_replication.queue import InflightEventQueue


class MicroBatcher:
    def __init__(self, *, max_records: int, max_bytes: int, max_delay_ms: int) -> None:
        if max_records <= 0:
            raise ValueError("max_records must be > 0")
        if max_bytes <= 0:
            raise ValueError("max_bytes must be > 0")
        if max_delay_ms <= 0:
            raise ValueError("max_delay_ms must be > 0")

        self._max_records = max_records
        self._max_bytes = max_bytes
        self._max_delay_s = max_delay_ms / 1000.0
        self._carry: ChangeEvent | None = None

    async def next_batch(self, queue: InflightEventQueue) -> list[ChangeEvent]:
        first = await self._next_event(queue=queue, timeout_s=None)
        if first is None:
            return []

        batch = [first]
        size_bytes = first.record_size_bytes
        deadline = monotonic() + self._max_delay_s

        while len(batch) < self._max_records:
            if size_bytes >= self._max_bytes:
                break

            remaining = deadline - monotonic()
            if remaining <= 0:
                break

            nxt = await self._next_event(queue=queue, timeout_s=remaining)
            if nxt is None:
                break

            if size_bytes + nxt.record_size_bytes > self._max_bytes:
                self._carry = nxt
                break

            batch.append(nxt)
            size_bytes += nxt.record_size_bytes

        return batch

    async def _next_event(
        self,
        *,
        queue: InflightEventQueue,
        timeout_s: float | None,
    ) -> ChangeEvent | None:
        if self._carry is not None:
            carry = self._carry
            self._carry = None
            return carry

        if timeout_s is None:
            return await queue.get()

        try:
            return await asyncio.wait_for(queue.get(), timeout=timeout_s)
        except asyncio.TimeoutError:
            return None
