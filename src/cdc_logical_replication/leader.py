from __future__ import annotations

import asyncio
import logging
from typing import Any

import psycopg
from pydantic import BaseModel, ConfigDict

LOGGER = logging.getLogger(__name__)

_SESSION_HOLDS_LOCK_SQL = """
SELECT EXISTS(
    SELECT 1
    FROM pg_locks
    WHERE locktype = 'advisory'
      AND pid = pg_backend_pid()
      AND granted
      AND classid = %s::oid
      AND objid = %s::oid
      AND objsubid = 1
)
"""


class LeaderSession(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    connection: psycopg.AsyncConnection[Any]
    lock_key: int

    async def close(self) -> None:
        await self.connection.close()


async def try_acquire_leader_lock(*, conninfo: str, lock_key: int) -> LeaderSession | None:
    connection = await psycopg.AsyncConnection.connect(conninfo=conninfo, autocommit=True)
    try:
        async with connection.cursor() as cursor:
            await cursor.execute("SELECT pg_try_advisory_lock(%s)", (lock_key,))
            row = await cursor.fetchone()
    except Exception:
        await connection.close()
        raise

    if not row or row[0] is not True:
        await connection.close()
        return None

    return LeaderSession(connection=connection, lock_key=lock_key)


async def wait_for_leadership(
    *,
    conninfo: str,
    lock_key: int,
    retry_interval_s: float,
) -> LeaderSession:
    while True:
        session = await try_acquire_leader_lock(conninfo=conninfo, lock_key=lock_key)
        if session is not None:
            return session
        await asyncio.sleep(retry_interval_s)


async def leadership_watchdog(
    session: LeaderSession,
    *,
    interval_s: float,
    stop_event: asyncio.Event,
) -> None:
    while not stop_event.is_set():
        await asyncio.sleep(interval_s)
        holds_lock = await _session_holds_lock(session)
        if not holds_lock:
            LOGGER.error("leadership_lost", extra={"leader_lock_key": session.lock_key})
            raise RuntimeError("leader_lock_lost")


def _split_bigint_advisory_lock_key(lock_key: int) -> tuple[int, int]:
    normalized = lock_key & 0xFFFFFFFFFFFFFFFF
    high_32 = (normalized >> 32) & 0xFFFFFFFF
    low_32 = normalized & 0xFFFFFFFF
    return high_32, low_32


async def _session_holds_lock(session: LeaderSession) -> bool:
    classid, objid = _split_bigint_advisory_lock_key(session.lock_key)
    async with session.connection.cursor() as cursor:
        await cursor.execute(_SESSION_HOLDS_LOCK_SQL, (classid, objid))
        row = await cursor.fetchone()
    return bool(row and row[0] is True)
