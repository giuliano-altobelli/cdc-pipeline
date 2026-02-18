from __future__ import annotations

import asyncio
from collections import deque
from types import SimpleNamespace

import pytest
from psycopg import pq
from src.cdc_logical_replication import replication
from src.cdc_logical_replication.ack import AckTracker
from src.cdc_logical_replication.protocol import XLOGDATA_HDR, lsn_int_to_str
from src.cdc_logical_replication.queue import InflightEventQueue
from src.cdc_logical_replication.replication import _replication_loop, _resolve_start_lsn


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


class _StubCursor:
    def __init__(self) -> None:
        self.pgresult = SimpleNamespace(status=pq.ExecStatus.COPY_BOTH)
        self.execute_calls: list[bytes] = []

    async def __aenter__(self) -> _StubCursor:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    async def execute(self, statement: bytes) -> None:
        self.execute_calls.append(statement)


class _StubConnection:
    def __init__(self, *, cursor: _StubCursor) -> None:
        self._cursor = cursor
        self.pgconn = object()
        self.closed = False

    def cursor(self) -> _StubCursor:
        return self._cursor

    async def close(self) -> None:
        self.closed = True


def _xlogdata_frame(*, wal_start: int, wal_end: int, payload: bytes = b"{}") -> bytes:
    return XLOGDATA_HDR.pack(b"w", wal_start, wal_end, 0) + payload


def test_replication_loop_accepts_regressive_wal_start_sequence() -> None:
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

        first_wal_start = 402_348_736
        second_wal_start = 402_348_288
        copy = _StubReplicationCopy(
            frames=[
                _xlogdata_frame(wal_start=first_wal_start, wal_end=402_348_984),
                _xlogdata_frame(wal_start=second_wal_start, wal_end=402_348_536),
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
            assert first.lsn == first_wal_start
            assert second.lsn == second_wal_start
            assert first.partition_key == lsn_int_to_str(first_wal_start)
            assert second.partition_key == lsn_int_to_str(second_wal_start)
            assert ack_tracker.pending_count == 2
            assert ack_tracker.last_registered_lsn == first_wal_start
            assert not loop_task.done()

            await queue.task_done(first)
            await queue.task_done(second)
        finally:
            loop_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await loop_task

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("frontier_lsn", "last_registered_lsn", "expected"),
    [
        (200, 900, 900),
        (900, 900, 900),
        (1200, 900, 1200),
    ],
)
def test_resolve_start_lsn_uses_max_frontier_and_last_registered(
    frontier_lsn: int,
    last_registered_lsn: int,
    expected: int,
) -> None:
    assert (
        _resolve_start_lsn(frontier_lsn=frontier_lsn, last_registered_lsn=last_registered_lsn)
        == expected
    )


def test_consume_replication_stream_starts_from_last_registered_when_ahead_of_frontier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        cursor = _StubCursor()
        connection = _StubConnection(cursor=cursor)

        async def fake_connect(
            *,
            conninfo: str,
            autocommit: bool,
            replication: str,
        ) -> _StubConnection:
            assert conninfo == "postgres://example"
            assert autocommit is True
            assert replication == "database"
            return connection

        async def fake_replication_loop(**kwargs: object) -> None:
            _ = kwargs
            return None

        monkeypatch.setattr(replication.psycopg.AsyncConnection, "connect", fake_connect)
        monkeypatch.setattr(replication, "_replication_loop", fake_replication_loop)

        tracker = AckTracker(initial_lsn=200)
        _ = tracker.register(900)
        assert (tracker.frontier_lsn, tracker.last_registered_lsn) == (200, 900)

        await replication.consume_replication_stream(
            settings=SimpleNamespace(
                postgres_conninfo="postgres://example",
                replication_slot="slot_a",
                wal2json_options_sql="'include-lsn' '1'",
            ),
            queue=InflightEventQueue(max_messages=8, max_bytes=8_000_000),
            ack_tracker=tracker,
            frontier_updates=asyncio.Queue(),
        )

        assert len(cursor.execute_calls) == 1
        statement = cursor.execute_calls[0].decode("utf-8")
        assert f"LOGICAL {lsn_int_to_str(900)}" in statement
        assert connection.closed is True

    asyncio.run(scenario())
