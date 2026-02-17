from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Sequence
from typing import Any, Protocol

from cdc_logical_replication.ack import AckTracker
from cdc_logical_replication.batching import MicroBatcher
from cdc_logical_replication.models import ChangeEvent
from cdc_logical_replication.queue import InflightEventQueue

LOGGER = logging.getLogger(__name__)

_NON_RETRIABLE_ERROR_CODES = {
    "AccessDenied",
    "AccessDeniedException",
    "ResourceNotFound",
    "ResourceNotFoundException",
    "ValidationException",
    "InvalidArgumentException",
}
_NON_RETRIABLE_ERROR_PREFIXES = ("AccessDenied", "ResourceNotFound")
_OVERSIZE_ERROR_MARKERS = (
    "too large",
    "exceeds the maximum",
    "must be less than",
    "1 mb",
    "1mib",
)
_NON_RETRIABLE_MESSAGE_MARKERS = (
    "access denied",
    "resource not found",
)

try:
    import boto3
except ImportError:  # pragma: no cover - only used at runtime in deployed container.
    boto3 = None


class KinesisClient(Protocol):
    def put_records(self, *, StreamName: str, Records: list[dict[str, Any]]) -> dict[str, Any]: ...


def create_kinesis_client(*, region_name: str) -> KinesisClient:
    if boto3 is None:
        raise RuntimeError("boto3 is required to create a Kinesis client")

    return boto3.client("kinesis", region_name=region_name)


class KinesisPublisher:
    def __init__(
        self,
        *,
        client: KinesisClient,
        stream_name: str,
        max_records: int,
        max_bytes: int,
        max_delay_ms: int,
        retry_base_delay_ms: int,
        retry_max_delay_ms: int,
        retry_max_attempts: int,
    ) -> None:
        if retry_max_attempts <= 0:
            raise ValueError("retry_max_attempts must be > 0")

        self._client = client
        self._stream_name = stream_name
        self._retry_base_s = retry_base_delay_ms / 1000.0
        self._retry_max_s = retry_max_delay_ms / 1000.0
        self._retry_max_attempts = retry_max_attempts
        self._batcher = MicroBatcher(
            max_records=max_records,
            max_bytes=max_bytes,
            max_delay_ms=max_delay_ms,
        )

    async def run(
        self,
        *,
        queue: InflightEventQueue,
        ack_tracker: AckTracker,
        frontier_updates: asyncio.Queue[int],
    ) -> None:
        while True:
            batch = await self._batcher.next_batch(queue)
            if not batch:
                continue

            await self._publish_with_retries(
                batch=batch,
                queue=queue,
                ack_tracker=ack_tracker,
                frontier_updates=frontier_updates,
            )

    async def _publish_with_retries(
        self,
        *,
        batch: Sequence[ChangeEvent],
        queue: InflightEventQueue,
        ack_tracker: AckTracker,
        frontier_updates: asyncio.Queue[int],
    ) -> None:
        pending: list[ChangeEvent] = list(batch)
        attempt = 1

        while pending:
            try:
                response = await self._put_records(pending)
                returned = response.get("Records", [])
                if len(returned) != len(pending):
                    raise RuntimeError(
                        "PutRecords returned mismatched result count "
                        f"({len(returned)} != {len(pending)})"
                    )
            except Exception as exc:
                error_code, error_message = _extract_exception_error(exc)
                if _is_non_retriable_error(code=error_code, message=error_message):
                    LOGGER.error(
                        "kinesis_put_records_non_retriable",
                        extra={
                            "pending_count": len(pending),
                            "attempt": attempt,
                            "error_code": error_code,
                        },
                    )
                    await self._drop_failed_events(
                        pending,
                        queue=queue,
                        ack_tracker=ack_tracker,
                        frontier_updates=frontier_updates,
                        reason="non_retriable_exception",
                    )
                    return

                if attempt >= self._retry_max_attempts:
                    LOGGER.error(
                        "kinesis_put_records_retry_exhausted",
                        extra={
                            "pending_count": len(pending),
                            "attempt": attempt,
                            "error_code": error_code,
                        },
                    )
                    await self._drop_failed_events(
                        pending,
                        queue=queue,
                        ack_tracker=ack_tracker,
                        frontier_updates=frontier_updates,
                        reason="retry_exhausted_exception",
                    )
                    return

                LOGGER.exception(
                    "kinesis_put_records_failed",
                    extra={
                        "pending_count": len(pending),
                        "attempt": attempt,
                    },
                )
                await asyncio.sleep(self._retry_delay(attempt - 1))
                attempt += 1
                continue

            failed_retriable: list[ChangeEvent] = []
            failed_non_retriable: list[ChangeEvent] = []
            for event, result in zip(pending, returned, strict=True):
                error_code = result.get("ErrorCode")
                if error_code:
                    if _is_non_retriable_error(
                        code=str(error_code),
                        message=str(result.get("ErrorMessage", "")),
                    ):
                        failed_non_retriable.append(event)
                    else:
                        failed_retriable.append(event)
                    continue

                advanced_to = self._mark_event_published(event=event, ack_tracker=ack_tracker)
                if advanced_to is not None:
                    frontier_updates.put_nowait(advanced_to)
                await queue.task_done(event)

            if failed_non_retriable:
                LOGGER.error(
                    "kinesis_non_retriable_record_failure",
                    extra={
                        "failed": len(failed_non_retriable),
                        "attempt": attempt,
                    },
                )
                await self._drop_failed_events(
                    failed_non_retriable,
                    queue=queue,
                    ack_tracker=ack_tracker,
                    frontier_updates=frontier_updates,
                    reason="non_retriable_record_error",
                )

            if failed_retriable:
                if attempt >= self._retry_max_attempts:
                    LOGGER.error(
                        "kinesis_partial_failure_retry_exhausted",
                        extra={
                            "failed": len(failed_retriable),
                            "succeeded": len(pending) - len(failed_retriable),
                            "attempt": attempt,
                        },
                    )
                    await self._drop_failed_events(
                        failed_retriable,
                        queue=queue,
                        ack_tracker=ack_tracker,
                        frontier_updates=frontier_updates,
                        reason="retry_exhausted_record_error",
                    )
                    return

                LOGGER.warning(
                    "kinesis_partial_failure",
                    extra={
                        "failed": len(failed_retriable),
                        "succeeded": len(pending) - len(failed_retriable),
                        "attempt": attempt,
                    },
                )
                await asyncio.sleep(self._retry_delay(attempt - 1))
                attempt += 1

            pending = failed_retriable

    async def _drop_failed_events(
        self,
        events: Sequence[ChangeEvent],
        *,
        queue: InflightEventQueue,
        ack_tracker: AckTracker,
        frontier_updates: asyncio.Queue[int],
        reason: str,
    ) -> None:
        if not events:
            return

        LOGGER.error(
            "kinesis_records_dropped",
            extra={
                "dropped_count": len(events),
                "reason": reason,
            },
        )

        # TODO: publish dropped records to a durable DLQ/quarantine stream before acking.
        for event in events:
            advanced_to = self._mark_event_published(event=event, ack_tracker=ack_tracker)
            if advanced_to is not None:
                frontier_updates.put_nowait(advanced_to)
            await queue.task_done(event)

    def _mark_event_published(self, *, event: ChangeEvent, ack_tracker: AckTracker) -> int | None:
        if event.ack_id is None:
            raise RuntimeError(
                "ChangeEvent.ack_id is required for publish acknowledgements; "
                f"missing for LSN {event.lsn}"
            )
        return ack_tracker.mark_published_by_id(event.ack_id)

    async def _put_records(self, events: Sequence[ChangeEvent]) -> dict[str, Any]:
        payload = [{"Data": e.payload, "PartitionKey": e.partition_key} for e in events]
        return await asyncio.to_thread(
            self._client.put_records,
            StreamName=self._stream_name,
            Records=payload,
        )

    def _retry_delay(self, attempt: int) -> float:
        exponential = min(self._retry_max_s, self._retry_base_s * (2**attempt))
        return exponential * random.uniform(0.8, 1.2)


def _extract_exception_error(exc: Exception) -> tuple[str | None, str | None]:
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        error = response.get("Error")
        if isinstance(error, dict):
            code = error.get("Code")
            message = error.get("Message")
            return (
                str(code) if code is not None else None,
                str(message) if message is not None else None,
            )

    return None, str(exc)


def _is_non_retriable_error(*, code: str | None, message: str | None) -> bool:
    normalized_code = code.strip() if code else None
    if normalized_code:
        if normalized_code in _NON_RETRIABLE_ERROR_CODES:
            return True
        if any(normalized_code.startswith(prefix) for prefix in _NON_RETRIABLE_ERROR_PREFIXES):
            return True

    message_lc = message.lower() if message else ""
    if "validation" in message_lc:
        return True
    if any(marker in message_lc for marker in _NON_RETRIABLE_MESSAGE_MARKERS):
        return True
    return any(marker in message_lc for marker in _OVERSIZE_ERROR_MARKERS)
