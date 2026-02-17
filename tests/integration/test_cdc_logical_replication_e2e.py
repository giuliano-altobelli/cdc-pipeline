from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from time import monotonic, sleep
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from psycopg import pq, sql
from psycopg.generators import copy_from, copy_to

from src.cdc_logical_replication.ack import AckTracker
from src.cdc_logical_replication.kinesis import KinesisPublisher, create_kinesis_client
from src.cdc_logical_replication.models import ChangeEvent
from src.cdc_logical_replication.partition_key import extract_partition_key
from src.cdc_logical_replication.protocol import (
    build_standby_status,
    parse_keepalive,
    parse_xlogdata,
)
from src.cdc_logical_replication.queue import InflightEventQueue
from src.cdc_logical_replication.replication import build_start_replication_statement
from src.cdc_logical_replication.settings import Settings
from src.cdc_logical_replication.slot import ensure_replication_slot

REQUIRED_ENV_VARS = (
    "PGHOST",
    "PGPORT",
    "PGUSER",
    "PGPASSWORD",
    "PGDATABASE",
    "AWS_REGION",
    "KINESIS_STREAM",
)

EXPECTED_KINDS = ["insert", "update", "delete"]
ACTION_TO_KIND = {
    "I": "insert",
    "U": "update",
    "D": "delete",
}

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("RUN_INTEGRATION_TESTS") != "1",
        reason="Set RUN_INTEGRATION_TESTS=1 to run integration tests.",
    ),
]


class _ReplicationStream:
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


@pytest.fixture(scope="module")
def integration_settings() -> Settings:
    missing = [name for name in REQUIRED_ENV_VARS if not os.getenv(name)]
    if missing:
        pytest.skip(f"Missing required env vars: {', '.join(sorted(missing))}")

    settings = Settings()
    assert settings.wal2json_format_version == 2
    return settings


def test_cdc_logical_replication_e2e(integration_settings: Settings) -> None:
    table_name = f"cdc_it_{uuid4().hex[:12]}"
    slot_name = f"it_slot_{uuid4().hex[:12]}"
    test_settings = integration_settings.model_copy(update={"replication_slot": slot_name})
    kinesis_client = create_kinesis_client(region_name=integration_settings.aws_region)

    _create_test_table(conninfo=test_settings.postgres_conninfo, table_name=table_name)
    try:
        created = asyncio.run(
            ensure_replication_slot(
                conninfo=test_settings.postgres_conninfo,
                slot_name=test_settings.replication_slot,
                output_plugin=test_settings.output_plugin,
            )
        )
        assert isinstance(created, bool)

        captured_events, replicated_kinds = asyncio.run(
            _collect_replication_events(
                settings=test_settings,
                table_name=table_name,
                timeout_s=90.0,
            )
        )
        assert captured_events
        assert _contains_subsequence(replicated_kinds, EXPECTED_KINDS), replicated_kinds

        stream_start_time = datetime.now(timezone.utc) - timedelta(seconds=2)
        published_frontier = asyncio.run(
            _publish_events_to_kinesis(
                settings=test_settings,
                kinesis_client=kinesis_client,
                events=captured_events,
            )
        )
        assert published_frontier >= max(event.lsn for event in captured_events)

        changes, envelopes = _read_changes_from_kinesis(
            client=kinesis_client,
            stream_name=test_settings.kinesis_stream,
            table_name=table_name,
            start_time=stream_start_time,
            timeout_s=120.0,
        )
        kinds = [change["kind"] for change in changes]
        assert _contains_subsequence(kinds, EXPECTED_KINDS), kinds

        for envelope in envelopes:
            _assert_format_version_2_envelope(envelope)
    finally:
        _drop_test_table(conninfo=test_settings.postgres_conninfo, table_name=table_name)
        _drop_replication_slot(conninfo=test_settings.postgres_conninfo, slot_name=test_settings.replication_slot)


async def _collect_replication_events(
    *,
    settings: Settings,
    table_name: str,
    timeout_s: float,
) -> tuple[list[ChangeEvent], list[str]]:
    start_lsn = await _fetch_current_lsn(settings.postgres_conninfo)
    statement = build_start_replication_statement(
        slot_name=settings.replication_slot,
        start_lsn=start_lsn,
        wal2json_options_sql=settings.wal2json_options_sql,
    )

    connection = await psycopg.AsyncConnection.connect(
        conninfo=settings.postgres_conninfo,
        autocommit=True,
        replication="database",
    )

    events: list[ChangeEvent] = []
    kinds: list[str] = []
    dml_task: asyncio.Task[int] | None = None

    try:
        async with connection.cursor() as cursor:
            await cursor.execute(statement)
            pgresult = cursor.pgresult
            if pgresult is None or pgresult.status != pq.ExecStatus.COPY_BOTH:
                raise AssertionError(
                    "START_REPLICATION did not enter COPY_BOTH mode "
                    f"(status={pgresult.status if pgresult else 'none'})"
                )

            stream = _ReplicationStream(connection=connection)
            dml_task = asyncio.create_task(
                asyncio.to_thread(
                    _emit_insert_update_delete,
                    settings.postgres_conninfo,
                    table_name,
                ),
                name="integration_dml_emitter",
            )
            read_task: asyncio.Task[memoryview | None] = asyncio.create_task(
                stream.read(),
                name="integration_replication_read",
            )

            try:
                deadline = monotonic() + timeout_s
                while monotonic() < deadline:
                    remaining = max(0.1, deadline - monotonic())
                    done, _ = await asyncio.wait({read_task}, timeout=min(1.0, remaining))
                    if not done:
                        continue

                    raw_frame = read_task.result()
                    read_task = asyncio.create_task(
                        stream.read(),
                        name="integration_replication_read",
                    )

                    if raw_frame is None:
                        continue

                    frame = bytes(raw_frame)
                    if not frame:
                        continue

                    tag = frame[:1]
                    if tag == b"w":
                        _, wal_end, _, payload = parse_xlogdata(frame)
                        envelope = _decode_envelope(payload)
                        if envelope is None:
                            continue

                        table_changes = _extract_table_changes(envelope, table_name=table_name)
                        if not table_changes:
                            continue

                        partition_key = extract_partition_key(
                            payload,
                            lsn=wal_end,
                            mode=settings.partition_key_mode,
                            fallback=settings.partition_key_fallback,
                            static_fallback_value=settings.partition_key_static_value,
                        )
                        events.append(
                            ChangeEvent(
                                lsn=wal_end,
                                payload=payload,
                                partition_key=partition_key,
                            )
                        )
                        kinds.extend(
                            change["kind"]
                            for change in table_changes
                            if isinstance(change.get("kind"), str)
                        )

                        await stream.write(build_standby_status(wal_end, reply_requested=0))
                        if _contains_subsequence(kinds, EXPECTED_KINDS):
                            if dml_task is not None:
                                await dml_task
                            return events, kinds
                        continue

                    if tag == b"k":
                        wal_end, _, reply_requested = parse_keepalive(frame)
                        if reply_requested:
                            await stream.write(build_standby_status(wal_end, reply_requested=1))
                        continue

                    raise AssertionError(f"Unexpected replication frame tag: {tag!r}")

                raise AssertionError(
                    "Timed out waiting for replication events. "
                    f"Observed kinds: {kinds or ['none']}"
                )
            finally:
                if not read_task.done():
                    read_task.cancel()
                await asyncio.gather(read_task, return_exceptions=True)
    finally:
        if dml_task is not None and not dml_task.done():
            dml_task.cancel()
            await asyncio.gather(dml_task, return_exceptions=True)
        await connection.close()


async def _publish_events_to_kinesis(
    *,
    settings: Settings,
    kinesis_client: Any,
    events: list[ChangeEvent],
) -> int:
    queue = InflightEventQueue(max_messages=512, max_bytes=8_000_000)
    ack_tracker = AckTracker(initial_lsn=0)
    frontier_updates: asyncio.Queue[int] = asyncio.Queue()

    for event in events:
        ack_id = ack_tracker.register(event.lsn)
        await queue.put(event.model_copy(update={"ack_id": ack_id}))

    publisher = KinesisPublisher(
        client=kinesis_client,
        stream_name=settings.kinesis_stream,
        max_records=2,
        max_bytes=settings.kinesis_batch_max_bytes,
        max_delay_ms=25,
        retry_base_delay_ms=settings.kinesis_retry_base_delay_ms,
        retry_max_delay_ms=settings.kinesis_retry_max_delay_ms,
        retry_max_attempts=settings.kinesis_retry_max_attempts,
    )

    publisher_task = asyncio.create_task(
        publisher.run(
            queue=queue,
            ack_tracker=ack_tracker,
            frontier_updates=frontier_updates,
        ),
        name="integration_kinesis_publisher",
    )
    try:
        await asyncio.wait_for(queue.join(), timeout=30.0)
        await _wait_for_pending_clear(ack_tracker=ack_tracker, timeout_s=10.0)
        return ack_tracker.frontier_lsn
    finally:
        publisher_task.cancel()
        await asyncio.gather(publisher_task, return_exceptions=True)


async def _wait_for_pending_clear(*, ack_tracker: AckTracker, timeout_s: float) -> None:
    deadline = monotonic() + timeout_s
    while monotonic() < deadline:
        if ack_tracker.pending_count == 0:
            return
        await asyncio.sleep(0.05)

    raise AssertionError(
        f"Publisher did not drain all pending LSNs; pending_count={ack_tracker.pending_count}"
    )


async def _fetch_current_lsn(conninfo: str) -> int:
    connection = await psycopg.AsyncConnection.connect(conninfo=conninfo, autocommit=True)
    try:
        async with connection.cursor() as cursor:
            await cursor.execute("SELECT pg_current_wal_lsn()::text")
            row = await cursor.fetchone()
            assert row is not None
            lsn_str = row[0]
            assert isinstance(lsn_str, str)
            return _lsn_str_to_int(lsn_str)
    finally:
        await connection.close()


def _create_test_table(*, conninfo: str, table_name: str) -> None:
    with psycopg.connect(conninfo=conninfo, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                sql.SQL(
                    "CREATE TABLE {} ("
                    "id BIGSERIAL PRIMARY KEY, "
                    "value TEXT NOT NULL"
                    ")"
                ).format(sql.Identifier(table_name))
            )


def _drop_test_table(*, conninfo: str, table_name: str) -> None:
    with psycopg.connect(conninfo=conninfo, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                sql.SQL("DROP TABLE IF EXISTS {}").format(sql.Identifier(table_name))
            )


def _drop_replication_slot(*, conninfo: str, slot_name: str) -> None:
    with psycopg.connect(conninfo=conninfo, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT active FROM pg_replication_slots WHERE slot_name = %s",
                (slot_name,),
            )
            row = cursor.fetchone()
            if row is None:
                return

            is_active = bool(row[0])
            if is_active:
                return

            cursor.execute("SELECT pg_drop_replication_slot(%s)", (slot_name,))


def _emit_insert_update_delete(conninfo: str, table_name: str) -> int:
    with psycopg.connect(conninfo=conninfo, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                sql.SQL("INSERT INTO {} (value) VALUES (%s) RETURNING id").format(
                    sql.Identifier(table_name)
                ),
                ("created",),
            )
            row = cursor.fetchone()
            assert row is not None
            row_id = row[0]

            cursor.execute(
                sql.SQL("UPDATE {} SET value = %s WHERE id = %s").format(
                    sql.Identifier(table_name)
                ),
                ("updated", row_id),
            )
            cursor.execute(
                sql.SQL("DELETE FROM {} WHERE id = %s").format(sql.Identifier(table_name)),
                (row_id,),
            )
            cursor.execute("SELECT pg_current_wal_lsn()::text")
            lsn_row = cursor.fetchone()
            assert lsn_row is not None
            lsn_str = lsn_row[0]
            assert isinstance(lsn_str, str)
            return _lsn_str_to_int(lsn_str)


def _read_changes_from_kinesis(
    *,
    client: Any,
    stream_name: str,
    table_name: str,
    start_time: datetime,
    timeout_s: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    shards = client.list_shards(StreamName=stream_name).get("Shards", [])
    assert shards, f"No shards were returned for stream {stream_name}"

    shard_iterators: dict[str, str] = {}
    for shard in shards:
        shard_id = shard.get("ShardId")
        if not isinstance(shard_id, str):
            continue
        response = client.get_shard_iterator(
            StreamName=stream_name,
            ShardId=shard_id,
            ShardIteratorType="AT_TIMESTAMP",
            Timestamp=start_time,
        )
        iterator = response.get("ShardIterator")
        if isinstance(iterator, str):
            shard_iterators[shard_id] = iterator

    deadline = monotonic() + timeout_s
    collected_changes: list[dict[str, Any]] = []
    collected_envelopes: list[dict[str, Any]] = []
    seen_sequence_numbers: set[str] = set()

    while monotonic() < deadline:
        made_progress = False

        for shard_id, iterator in list(shard_iterators.items()):
            if not iterator:
                continue

            response = client.get_records(ShardIterator=iterator, Limit=200)
            shard_iterators[shard_id] = response.get("NextShardIterator")

            records = response.get("Records", [])
            if records:
                made_progress = True

            for record in records:
                sequence_number = record.get("SequenceNumber")
                if isinstance(sequence_number, str):
                    if sequence_number in seen_sequence_numbers:
                        continue
                    seen_sequence_numbers.add(sequence_number)

                envelope = _decode_envelope(record.get("Data"))
                if envelope is None:
                    continue

                changes = _extract_table_changes(envelope, table_name=table_name)
                if not changes:
                    continue

                collected_envelopes.append(envelope)
                collected_changes.extend(changes)

        kinds = [
            kind
            for kind in (change.get("kind") for change in collected_changes)
            if isinstance(kind, str)
        ]
        if _contains_subsequence(kinds, EXPECTED_KINDS):
            return collected_changes, collected_envelopes

        sleep(0.2 if made_progress else 0.5)

    raise AssertionError(
        "Timed out waiting for insert/update/delete wal2json changes in Kinesis. "
        f"Observed kinds: {[change.get('kind') for change in collected_changes]}"
    )


def _decode_envelope(raw_data: object) -> dict[str, Any] | None:
    if isinstance(raw_data, memoryview):
        payload = raw_data.tobytes()
    elif isinstance(raw_data, (bytes, bytearray)):
        payload = bytes(raw_data)
    else:
        return None

    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None

    if not isinstance(decoded, dict):
        return None

    return decoded


def _extract_table_changes(envelope: dict[str, Any], *, table_name: str) -> list[dict[str, Any]]:
    action = envelope.get("action")
    if not isinstance(action, str):
        return []
    kind = ACTION_TO_KIND.get(action)
    if kind is None:
        return []

    if envelope.get("schema") != "public":
        return []
    if envelope.get("table") != table_name:
        return []

    change = dict(envelope)
    change["kind"] = kind
    return [change]


def _assert_format_version_2_envelope(envelope: dict[str, Any]) -> None:
    action = envelope.get("action")
    assert isinstance(action, str), envelope
    assert action in ACTION_TO_KIND, envelope

    lsn = envelope.get("lsn")
    assert isinstance(lsn, str), envelope

    timestamp = envelope.get("timestamp")
    assert isinstance(timestamp, str), envelope

    schema = envelope.get("schema")
    table = envelope.get("table")
    assert isinstance(schema, str), envelope
    assert isinstance(table, str), envelope

    pk = envelope.get("pk")
    assert isinstance(pk, list) and pk, envelope

    if action in {"I", "U"}:
        columns = envelope.get("columns")
        assert isinstance(columns, list) and columns, envelope

    if action in {"U", "D"}:
        identity = envelope.get("identity")
        assert isinstance(identity, list) and identity, envelope


def _contains_subsequence(values: list[str], expected: list[str]) -> bool:
    if not expected:
        return True

    idx = 0
    for value in values:
        if value == expected[idx]:
            idx += 1
            if idx == len(expected):
                return True
    return False


def _lsn_str_to_int(lsn: str) -> int:
    high, low = lsn.split("/")
    return (int(high, 16) << 32) | int(low, 16)
