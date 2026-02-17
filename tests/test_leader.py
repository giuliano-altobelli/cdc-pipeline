from __future__ import annotations

import asyncio
from typing import Any

import pytest
from src.cdc_logical_replication.leader import LeaderSession, leadership_watchdog


class _StubCursor:
    def __init__(
        self,
        *,
        rows: list[tuple[Any, ...] | None] | None = None,
        execute_error: Exception | None = None,
    ) -> None:
        self._rows = rows or []
        self._execute_error = execute_error
        self.execute_calls: list[tuple[str, tuple[Any, ...] | None]] = []

    async def __aenter__(self) -> _StubCursor:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    async def execute(self, query: str, params: tuple[Any, ...] | None = None) -> None:
        self.execute_calls.append((query, params))
        if self._execute_error is not None:
            raise self._execute_error

    async def fetchone(self) -> tuple[Any, ...] | None:
        if not self._rows:
            return None
        return self._rows.pop(0)


class _StubConnection:
    def __init__(self, cursor: _StubCursor) -> None:
        self._cursor = cursor

    def cursor(self) -> _StubCursor:
        return self._cursor


def test_watchdog_exits_cleanly_when_stopped_with_lock_held(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        cursor = _StubCursor(rows=[(True,)])
        session = LeaderSession.model_construct(connection=_StubConnection(cursor), lock_key=123)
        stop_event = asyncio.Event()

        async def fake_sleep(_: float) -> None:
            stop_event.set()

        monkeypatch.setattr("src.cdc_logical_replication.leader.asyncio.sleep", fake_sleep)

        await leadership_watchdog(session, interval_s=0.01, stop_event=stop_event)
        assert len(cursor.execute_calls) == 1

    asyncio.run(scenario())


def test_watchdog_raises_when_lock_is_lost(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        lock_key = -1
        cursor = _StubCursor(rows=[(False,)])
        session = LeaderSession.model_construct(connection=_StubConnection(cursor), lock_key=lock_key)
        stop_event = asyncio.Event()

        async def fake_sleep(_: float) -> None:
            return None

        monkeypatch.setattr("src.cdc_logical_replication.leader.asyncio.sleep", fake_sleep)

        with pytest.raises(RuntimeError, match="leader_lock_lost"):
            await leadership_watchdog(session, interval_s=0.01, stop_event=stop_event)

        assert len(cursor.execute_calls) == 1
        query, params = cursor.execute_calls[0]
        assert "pg_locks" in query
        assert params == (0xFFFFFFFF, 0xFFFFFFFF)

    asyncio.run(scenario())


def test_watchdog_propagates_database_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        cursor = _StubCursor(execute_error=ConnectionError("db unavailable"))
        session = LeaderSession.model_construct(connection=_StubConnection(cursor), lock_key=42)
        stop_event = asyncio.Event()

        async def fake_sleep(_: float) -> None:
            return None

        monkeypatch.setattr("src.cdc_logical_replication.leader.asyncio.sleep", fake_sleep)

        with pytest.raises(ConnectionError, match="db unavailable"):
            await leadership_watchdog(session, interval_s=0.01, stop_event=stop_event)

    asyncio.run(scenario())
