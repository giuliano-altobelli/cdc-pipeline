from __future__ import annotations

import asyncio
import logging
from time import monotonic
from typing import Any

import psycopg
from psycopg import pq
from psycopg.generators import copy_from, copy_to

from cdc_logical_replication.ack import AckTracker
from cdc_logical_replication.models import ChangeEvent
from cdc_logical_replication.partition_key import extract_partition_key
from cdc_logical_replication.protocol import (
    ReplicationProtocolError,
    build_standby_status,
    lsn_int_to_str,
    parse_keepalive,
    parse_xlogdata,
)
from cdc_logical_replication.queue import InflightEventQueue
from cdc_logical_replication.settings import Settings

LOGGER = logging.getLogger(__name__)


def build_start_replication_statement(
    *,
    slot_name: str,
    start_lsn: int,
    wal2json_options_sql: str,
) -> bytes:
    statement = (
        f"START_REPLICATION SLOT {slot_name} "
        f"LOGICAL {lsn_int_to_str(start_lsn)} "
        f"({wal2json_options_sql})"
    )
    return statement.encode("utf-8")


class _ReplicationStream:
    """Minimal COPY_BOTH wrapper for logical replication frames."""

    def __init__(self, *, connection: psycopg.AsyncConnection[Any]) -> None:
        self._connection = connection
        self._pgconn = connection.pgconn

    async def read(self) -> memoryview | None:
        frame_or_result = await self._connection.wait(copy_from(self._pgconn))
        if isinstance(frame_or_result, memoryview):
            return frame_or_result
        return None

    async def write(self, payload: bytes) -> None:
        await self._connection.wait(copy_to(self._pgconn, payload, flush=True))


async def consume_replication_stream(
    *,
    settings: Settings,
    queue: InflightEventQueue,
    ack_tracker: AckTracker,
    frontier_updates: asyncio.Queue[int],
) -> None:
    frontier_lsn = ack_tracker.frontier_lsn
    last_registered_lsn = ack_tracker.last_registered_lsn
    start_lsn = _resolve_start_lsn(
        frontier_lsn=frontier_lsn,
        last_registered_lsn=last_registered_lsn,
    )
    LOGGER.info(
        "replication_start_lsn_selected",
        extra={
            "frontier_lsn": frontier_lsn,
            "last_registered_lsn": last_registered_lsn,
            "start_lsn": start_lsn,
        },
    )

    connection = await psycopg.AsyncConnection.connect(
        conninfo=settings.postgres_conninfo,
        autocommit=True,
        replication="database",
    )

    try:
        async with connection.cursor() as cursor:
            statement = build_start_replication_statement(
                slot_name=settings.replication_slot,
                start_lsn=start_lsn,
                wal2json_options_sql=settings.wal2json_options_sql,
            )
            await cursor.execute(statement)
            pgresult = cursor.pgresult
            if pgresult is None or pgresult.status != pq.ExecStatus.COPY_BOTH:
                raise RuntimeError(
                    "START_REPLICATION did not enter COPY_BOTH mode "
                    f"(status={pgresult.status if pgresult else 'none'})"
                )

            replication_stream = _ReplicationStream(connection=connection)
            await _replication_loop(
                copy=replication_stream,
                settings=settings,
                queue=queue,
                ack_tracker=ack_tracker,
                frontier_updates=frontier_updates,
            )
    finally:
        await connection.close()


def _resolve_start_lsn(*, frontier_lsn: int, last_registered_lsn: int) -> int:
    return max(frontier_lsn, last_registered_lsn)


async def _replication_loop(
    *,
    copy: Any,
    settings: Settings,
    queue: InflightEventQueue,
    ack_tracker: AckTracker,
    frontier_updates: asyncio.Queue[int],
) -> None:
    latest_safe_lsn = ack_tracker.frontier_lsn
    last_feedback_lsn = latest_safe_lsn
    last_feedback_at = monotonic()
    read_task: asyncio.Task[Any] = asyncio.create_task(copy.read(), name="replication_copy_read")

    try:
        while True:
            latest_safe_lsn = _drain_frontier_updates(frontier_updates, default=latest_safe_lsn)
            if latest_safe_lsn > last_feedback_lsn:
                await copy.write(build_standby_status(latest_safe_lsn, reply_requested=0))
                last_feedback_lsn = latest_safe_lsn
                last_feedback_at = monotonic()

            elapsed = monotonic() - last_feedback_at
            timeout = max(0.01, settings.replication_feedback_interval_s - elapsed)
            done, _ = await asyncio.wait({read_task}, timeout=timeout)

            if not done:
                await copy.write(build_standby_status(latest_safe_lsn, reply_requested=0))
                last_feedback_lsn = latest_safe_lsn
                last_feedback_at = monotonic()
                continue

            raw_frame = read_task.result()
            read_task = asyncio.create_task(copy.read(), name="replication_copy_read")

            if raw_frame is None:
                continue

            frame = bytes(raw_frame)
            if not frame:
                continue

            tag = frame[:1]
            if tag == b"w":
                wal_start, _, _, payload = parse_xlogdata(frame)
                # XLogData.wal_end is a sender-side watermark, not the current
                # message position. Registering/acking by wal_start keeps LSN
                # progression tied to message order and prevents false
                # non-monotonic regressions on reconnect/replay.
                partition_key = extract_partition_key(
                    payload,
                    lsn=wal_start,
                    mode=settings.partition_key_mode,
                    fallback=settings.partition_key_fallback,
                    static_fallback_value=settings.partition_key_static_value,
                )
                ack_id = ack_tracker.register(wal_start)
                await queue.put(
                    ChangeEvent(
                        lsn=wal_start,
                        ack_id=ack_id,
                        payload=payload,
                        partition_key=partition_key,
                    )
                )
                continue

            if tag == b"k":
                _, _, reply_requested = parse_keepalive(frame)
                if reply_requested:
                    latest_safe_lsn = _drain_frontier_updates(
                        frontier_updates, default=latest_safe_lsn
                    )
                    await copy.write(build_standby_status(latest_safe_lsn, reply_requested=1))
                    last_feedback_lsn = latest_safe_lsn
                    last_feedback_at = monotonic()
                continue

            raise ReplicationProtocolError(f"Unknown replication frame tag: {tag!r}")
    finally:
        if not read_task.done():
            read_task.cancel()
        await asyncio.gather(read_task, return_exceptions=True)


def _drain_frontier_updates(frontier_updates: asyncio.Queue[int], *, default: int) -> int:
    newest = default
    while True:
        try:
            newest = frontier_updates.get_nowait()
        except asyncio.QueueEmpty:
            return newest
