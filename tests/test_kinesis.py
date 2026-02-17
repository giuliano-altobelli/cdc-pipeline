from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

import pytest

from src.cdc_logical_replication.ack import AckTracker
from src.cdc_logical_replication.kinesis import KinesisPublisher
from src.cdc_logical_replication.models import ChangeEvent
from src.cdc_logical_replication.queue import InflightEventQueue


class _AwsLikeException(Exception):
    def __init__(self, *, code: str, message: str) -> None:
        super().__init__(message)
        self.response = {"Error": {"Code": code, "Message": message}}


class _StubKinesisClient:
    def __init__(self, responses: Sequence[dict[str, Any] | Exception]) -> None:
        self._responses = list(responses)
        self.calls = 0

    def put_records(self, *, StreamName: str, Records: list[dict[str, Any]]) -> dict[str, Any]:
        if not self._responses:
            raise AssertionError("No stubbed Kinesis responses left")

        response = self._responses[min(self.calls, len(self._responses) - 1)]
        self.calls += 1
        if isinstance(response, Exception):
            raise response
        return response


def _event(lsn: int, payload: bytes = b"payload") -> ChangeEvent:
    return ChangeEvent(lsn=lsn, payload=payload, partition_key="pk")


async def _prepare_batch(
    events: Sequence[ChangeEvent],
) -> tuple[list[ChangeEvent], InflightEventQueue, AckTracker, asyncio.Queue[int]]:
    queue = InflightEventQueue(max_messages=100, max_bytes=1_000_000)
    ack_tracker = AckTracker(initial_lsn=0)
    frontier_updates: asyncio.Queue[int] = asyncio.Queue()
    prepared_events: list[ChangeEvent] = []

    for event in events:
        ack_id = ack_tracker.register(event.lsn)
        prepared_event = event.model_copy(update={"ack_id": ack_id})
        prepared_events.append(prepared_event)
        await queue.put(prepared_event)

    batch = [await queue.get() for _ in prepared_events]
    return batch, queue, ack_tracker, frontier_updates


def test_non_retriable_exception_drops_batch_and_advances_frontier() -> None:
    async def scenario() -> None:
        events = [_event(100), _event(200)]
        batch, queue, ack_tracker, frontier_updates = await _prepare_batch(events)
        client = _StubKinesisClient(
            [_AwsLikeException(code="AccessDeniedException", message="no access")]
        )
        publisher = KinesisPublisher(
            client=client,
            stream_name="stream",
            max_records=100,
            max_bytes=1_000_000,
            max_delay_ms=10,
            retry_base_delay_ms=1,
            retry_max_delay_ms=5,
            retry_max_attempts=3,
        )

        await publisher._publish_with_retries(
            batch=batch,
            queue=queue,
            ack_tracker=ack_tracker,
            frontier_updates=frontier_updates,
        )

        assert client.calls == 1
        assert ack_tracker.pending_count == 0
        assert ack_tracker.frontier_lsn == 200
        assert queue.bytes_inflight == 0
        assert frontier_updates.qsize() >= 1

    asyncio.run(scenario())


def test_retriable_record_errors_are_capped_then_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        sleep_calls: list[float] = []

        async def fake_sleep(delay: float) -> None:
            sleep_calls.append(delay)

        monkeypatch.setattr("src.cdc_logical_replication.kinesis.asyncio.sleep", fake_sleep)

        events = [_event(123)]
        batch, queue, ack_tracker, frontier_updates = await _prepare_batch(events)
        client = _StubKinesisClient(
            [
                {
                    "Records": [
                        {
                            "ErrorCode": "ProvisionedThroughputExceededException",
                            "ErrorMessage": "throttled",
                        }
                    ]
                }
            ]
        )
        publisher = KinesisPublisher(
            client=client,
            stream_name="stream",
            max_records=100,
            max_bytes=1_000_000,
            max_delay_ms=10,
            retry_base_delay_ms=1,
            retry_max_delay_ms=5,
            retry_max_attempts=3,
        )

        await publisher._publish_with_retries(
            batch=batch,
            queue=queue,
            ack_tracker=ack_tracker,
            frontier_updates=frontier_updates,
        )

        assert client.calls == 3
        assert len(sleep_calls) == 2
        assert ack_tracker.pending_count == 0
        assert ack_tracker.frontier_lsn == 123
        assert queue.bytes_inflight == 0
        assert frontier_updates.qsize() >= 1

    asyncio.run(scenario())


def test_non_retriable_record_error_is_dropped_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        sleep_calls: list[float] = []

        async def fake_sleep(delay: float) -> None:
            sleep_calls.append(delay)

        monkeypatch.setattr("src.cdc_logical_replication.kinesis.asyncio.sleep", fake_sleep)

        events = [_event(10), _event(20)]
        batch, queue, ack_tracker, frontier_updates = await _prepare_batch(events)
        client = _StubKinesisClient(
            [
                {
                    "Records": [
                        {
                            "ErrorCode": "ValidationException",
                            "ErrorMessage": "record too large",
                        },
                        {},
                    ]
                }
            ]
        )
        publisher = KinesisPublisher(
            client=client,
            stream_name="stream",
            max_records=100,
            max_bytes=1_000_000,
            max_delay_ms=10,
            retry_base_delay_ms=1,
            retry_max_delay_ms=5,
            retry_max_attempts=3,
        )

        await publisher._publish_with_retries(
            batch=batch,
            queue=queue,
            ack_tracker=ack_tracker,
            frontier_updates=frontier_updates,
        )

        assert client.calls == 1
        assert not sleep_calls
        assert ack_tracker.pending_count == 0
        assert ack_tracker.frontier_lsn == 20
        assert queue.bytes_inflight == 0
        assert frontier_updates.qsize() >= 1

    asyncio.run(scenario())


def test_duplicate_lsn_mixed_result_retries_failed_record_without_losing_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        sleep_calls: list[float] = []

        async def fake_sleep(delay: float) -> None:
            sleep_calls.append(delay)

        monkeypatch.setattr("src.cdc_logical_replication.kinesis.asyncio.sleep", fake_sleep)

        events = [_event(100, b"a"), _event(100, b"b")]
        batch, queue, ack_tracker, frontier_updates = await _prepare_batch(events)
        client = _StubKinesisClient(
            [
                {
                    "Records": [
                        {
                            "ErrorCode": "ProvisionedThroughputExceededException",
                            "ErrorMessage": "throttled",
                        },
                        {},
                    ]
                },
                {"Records": [{}]},
            ]
        )
        publisher = KinesisPublisher(
            client=client,
            stream_name="stream",
            max_records=100,
            max_bytes=1_000_000,
            max_delay_ms=10,
            retry_base_delay_ms=1,
            retry_max_delay_ms=5,
            retry_max_attempts=3,
        )

        await publisher._publish_with_retries(
            batch=batch,
            queue=queue,
            ack_tracker=ack_tracker,
            frontier_updates=frontier_updates,
        )

        assert client.calls == 2
        assert len(sleep_calls) == 1
        assert ack_tracker.pending_count == 0
        assert ack_tracker.frontier_lsn == 100
        assert queue.bytes_inflight == 0
        assert frontier_updates.qsize() >= 1

    asyncio.run(scenario())
