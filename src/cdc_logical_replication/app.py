from __future__ import annotations

import asyncio
import logging
import os

from cdc_logical_replication.ack import AckTracker
from cdc_logical_replication.kinesis import KinesisPublisher, create_kinesis_client
from cdc_logical_replication.leader import LeaderSession, leadership_watchdog, wait_for_leadership
from cdc_logical_replication.queue import InflightEventQueue
from cdc_logical_replication.replication import consume_replication_stream
from cdc_logical_replication.settings import Settings
from cdc_logical_replication.slot import (
    ensure_replication_slot,
    get_replication_slot_confirmed_lsn,
)

LOGGER = logging.getLogger(__name__)


def configure_logging() -> None:
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


async def run() -> None:
    configure_logging()
    settings = Settings()

    LOGGER.info(
        "service_start",
        extra={
            "replication_slot": settings.replication_slot,
            "kinesis_stream": settings.kinesis_stream,
            "leader_lock_key": settings.leader_lock_key,
        },
    )

    while True:
        leader_session = await wait_for_leadership(
            conninfo=settings.postgres_conninfo,
            lock_key=settings.leader_lock_key,
            retry_interval_s=settings.standby_retry_interval_s,
        )
        LOGGER.info("leadership_acquired")

        try:
            created = await ensure_replication_slot(
                conninfo=settings.postgres_conninfo,
                slot_name=settings.replication_slot,
                output_plugin=settings.output_plugin,
            )
            LOGGER.info("replication_slot_ready", extra={"slot_created": created})

            initial_frontier_lsn = await get_replication_slot_confirmed_lsn(
                conninfo=settings.postgres_conninfo,
                slot_name=settings.replication_slot,
            )
            if initial_frontier_lsn < 0:
                raise RuntimeError(f"Invalid initial frontier LSN: {initial_frontier_lsn}")
            LOGGER.info(
                "replication_start_lsn_resolved",
                extra={"initial_frontier_lsn": initial_frontier_lsn},
            )

            await _run_leader_pipeline(
                settings=settings,
                leader_session=leader_session,
                initial_frontier_lsn=initial_frontier_lsn,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception("leader_cycle_failed")
        finally:
            await leader_session.close()
            LOGGER.info("leadership_released")

        await asyncio.sleep(settings.standby_retry_interval_s)


async def _run_leader_pipeline(
    *,
    settings: Settings,
    leader_session: LeaderSession,
    initial_frontier_lsn: int,
) -> None:
    queue = InflightEventQueue(
        max_messages=settings.inflight_max_messages,
        max_bytes=settings.inflight_max_bytes,
    )
    ack_tracker = AckTracker(initial_lsn=initial_frontier_lsn)
    frontier_updates: asyncio.Queue[int] = asyncio.Queue()

    kinesis_client = create_kinesis_client(region_name=settings.aws_region)
    publisher = KinesisPublisher(
        client=kinesis_client,
        stream_name=settings.kinesis_stream,
        max_records=settings.kinesis_batch_max_records,
        max_bytes=settings.kinesis_batch_max_bytes,
        max_delay_ms=settings.kinesis_batch_max_delay_ms,
        retry_base_delay_ms=settings.kinesis_retry_base_delay_ms,
        retry_max_delay_ms=settings.kinesis_retry_max_delay_ms,
        retry_max_attempts=settings.kinesis_retry_max_attempts,
    )

    stop_event = asyncio.Event()
    tasks = [
        asyncio.create_task(
            consume_replication_stream(
                settings=settings,
                queue=queue,
                ack_tracker=ack_tracker,
                frontier_updates=frontier_updates,
            ),
            name="replication_reader",
        ),
        asyncio.create_task(
            publisher.run(
                queue=queue,
                ack_tracker=ack_tracker,
                frontier_updates=frontier_updates,
            ),
            name="kinesis_publisher",
        ),
        asyncio.create_task(
            leadership_watchdog(
                leader_session,
                interval_s=1.0,
                stop_event=stop_event,
            ),
            name="leader_watchdog",
        ),
    ]

    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
        for task in done:
            exc = task.exception()
            if exc is not None:
                raise exc

        raise RuntimeError("Leader tasks stopped unexpectedly")
    finally:
        stop_event.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
