from __future__ import annotations

import asyncio
import os
from collections.abc import Sequence
from typing import Any
from uuid import uuid4

import pytest

from src.cdc_logical_replication.ack import AckTracker
from src.cdc_logical_replication.kinesis import KinesisPublisher, create_kinesis_client
from src.cdc_logical_replication.models import ChangeEvent
from src.cdc_logical_replication.queue import InflightEventQueue

REQUIRED_ENV_VARS = ("AWS_REGION", "KINESIS_STREAM")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("RUN_INTEGRATION_TESTS") != "1",
        reason="Set RUN_INTEGRATION_TESTS=1 to run integration tests.",
    ),
]


class _FlakyThenRealKinesisClient:
    def __init__(
        self,
        *,
        real_client: Any,
        failures_before_success: int,
        error_code: str,
        error_message: str,
    ) -> None:
        self._real_client = real_client
        self._failures_before_success = failures_before_success
        self._error_code = error_code
        self._error_message = error_message
        self.calls = 0

    def put_records(self, *, StreamName: str, Records: list[dict[str, Any]]) -> dict[str, Any]:
        self.calls += 1
        if self.calls <= self._failures_before_success:
            return {
                "FailedRecordCount": len(Records),
                "Records": [
                    {
                        "ErrorCode": self._error_code,
                        "ErrorMessage": self._error_message,
                    }
                    for _ in Records
                ],
            }
        return self._real_client.put_records(StreamName=StreamName, Records=Records)


class _CountingKinesisClient:
    def __init__(self, *, real_client: Any) -> None:
        self._real_client = real_client
        self.calls = 0

    def put_records(self, *, StreamName: str, Records: list[dict[str, Any]]) -> dict[str, Any]:
        self.calls += 1
        return self._real_client.put_records(StreamName=StreamName, Records=Records)


@pytest.fixture(scope="module")
def kinesis_env() -> dict[str, str]:
    missing = [name for name in REQUIRED_ENV_VARS if not os.getenv(name)]
    if missing:
        pytest.skip(f"Missing required env vars: {', '.join(sorted(missing))}")

    return {
        "aws_region": os.environ["AWS_REGION"],
        "kinesis_stream": os.environ["KINESIS_STREAM"],
    }


async def _prepare_batch(
    events: Sequence[ChangeEvent],
) -> tuple[list[ChangeEvent], InflightEventQueue, AckTracker, asyncio.Queue[int]]:
    queue = InflightEventQueue(max_messages=32, max_bytes=1_000_000)
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


def test_kinesis_publisher_retries_transient_record_errors_then_succeeds(
    kinesis_env: dict[str, str],
) -> None:
    """
    Simulates 2 retriable per-record failures, then sends to real Kinesis.
    Verifies 3 attempts total, queue drain, and frontier advancement.
    """
    async def scenario() -> None:
        event = ChangeEvent(
            lsn=100,
            payload=f'{{"retry_test":"{uuid4().hex}"}}'.encode("utf-8"),
            partition_key="retry-behavior",
        )
        batch, queue, ack_tracker, frontier_updates = await _prepare_batch([event])

        real_client = create_kinesis_client(region_name=kinesis_env["aws_region"])
        flaky_client = _FlakyThenRealKinesisClient(
            real_client=real_client,
            failures_before_success=2,
            error_code="ProvisionedThroughputExceededException",
            error_message="simulated transient throttling",
        )
        publisher = KinesisPublisher(
            client=flaky_client,
            stream_name=kinesis_env["kinesis_stream"],
            max_records=10,
            max_bytes=1_000_000,
            max_delay_ms=10,
            retry_base_delay_ms=1,
            retry_max_delay_ms=2,
            retry_max_attempts=3,
        )

        await publisher._publish_with_retries(
            batch=batch,
            queue=queue,
            ack_tracker=ack_tracker,
            frontier_updates=frontier_updates,
        )

        await asyncio.wait_for(queue.join(), timeout=2.0)
        assert flaky_client.calls == 3
        assert ack_tracker.pending_count == 0
        assert ack_tracker.frontier_lsn == event.lsn
        assert queue.bytes_inflight == 0
        assert frontier_updates.qsize() >= 1

    asyncio.run(scenario())


def test_kinesis_publisher_drops_non_retriable_stream_error_without_retry(
    kinesis_env: dict[str, str],
) -> None:
    """
    Uses a guaranteed-missing stream name to trigger non-retriable ResourceNotFound.
    Verifies fail-fast (single call), queue drain, and frontier advancement.
    """
    async def scenario() -> None:
        event = ChangeEvent(
            lsn=200,
            payload=f'{{"drop_test":"{uuid4().hex}"}}'.encode("utf-8"),
            partition_key="retry-behavior",
        )
        batch, queue, ack_tracker, frontier_updates = await _prepare_batch([event])

        real_client = create_kinesis_client(region_name=kinesis_env["aws_region"])
        counting_client = _CountingKinesisClient(real_client=real_client)
        missing_stream = f"{kinesis_env['kinesis_stream']}-missing-{uuid4().hex[:8]}"
        publisher = KinesisPublisher(
            client=counting_client,
            stream_name=missing_stream,
            max_records=10,
            max_bytes=1_000_000,
            max_delay_ms=10,
            retry_base_delay_ms=1,
            retry_max_delay_ms=2,
            retry_max_attempts=3,
        )

        await publisher._publish_with_retries(
            batch=batch,
            queue=queue,
            ack_tracker=ack_tracker,
            frontier_updates=frontier_updates,
        )

        await asyncio.wait_for(queue.join(), timeout=2.0)
        assert counting_client.calls == 1
        assert ack_tracker.pending_count == 0
        assert ack_tracker.frontier_lsn == event.lsn
        assert queue.bytes_inflight == 0
        assert frontier_updates.qsize() >= 1

    asyncio.run(scenario())
