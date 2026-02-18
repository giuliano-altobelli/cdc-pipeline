from __future__ import annotations

import asyncio
from typing import Any

import pytest
from src.cdc_logical_replication.protocol import lsn_str_to_int
from src.cdc_logical_replication.slot import get_replication_slot_confirmed_lsn


class _StubCursor:
    def __init__(self, *, rows: list[tuple[object, object] | None]) -> None:
        self._rows = rows
        self.execute_calls: list[tuple[str, tuple[Any, ...] | None]] = []

    async def __aenter__(self) -> _StubCursor:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    async def execute(self, query: str, params: tuple[Any, ...] | None = None) -> None:
        self.execute_calls.append((query, params))

    async def fetchone(self) -> tuple[object, object] | None:
        if not self._rows:
            return None
        return self._rows.pop(0)


class _StubConnection:
    def __init__(self, cursor: _StubCursor) -> None:
        self._cursor = cursor
        self.closed = False

    def cursor(self) -> _StubCursor:
        return self._cursor

    async def close(self) -> None:
        self.closed = True


def test_get_replication_slot_confirmed_lsn_prefers_confirmed_flush_lsn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        cursor = _StubCursor(rows=[("16/B374D848", "16/B0000000")])
        connection = _StubConnection(cursor)

        async def fake_connect(*, conninfo: str, autocommit: bool) -> _StubConnection:
            assert conninfo == "postgres://example"
            assert autocommit is True
            return connection

        monkeypatch.setattr(
            "src.cdc_logical_replication.slot.psycopg.AsyncConnection.connect",
            fake_connect,
        )

        resolved = await get_replication_slot_confirmed_lsn(
            conninfo="postgres://example",
            slot_name="slot_a",
        )

        assert resolved == lsn_str_to_int("16/B374D848")
        assert connection.closed is True
        assert len(cursor.execute_calls) == 1
        query, params = cursor.execute_calls[0]
        assert "confirmed_flush_lsn" in query
        assert params == ("slot_a",)

    asyncio.run(scenario())


def test_get_replication_slot_confirmed_lsn_falls_back_to_restart_lsn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        cursor = _StubCursor(rows=[(None, "0/16B6D80")])
        connection = _StubConnection(cursor)

        async def fake_connect(*, conninfo: str, autocommit: bool) -> _StubConnection:
            assert conninfo == "postgres://example"
            assert autocommit is True
            return connection

        monkeypatch.setattr(
            "src.cdc_logical_replication.slot.psycopg.AsyncConnection.connect",
            fake_connect,
        )

        resolved = await get_replication_slot_confirmed_lsn(
            conninfo="postgres://example",
            slot_name="slot_a",
        )

        assert resolved == lsn_str_to_int("0/16B6D80")
        assert connection.closed is True

    asyncio.run(scenario())


def test_get_replication_slot_confirmed_lsn_returns_zero_when_slot_lsn_is_uninitialized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        cursor = _StubCursor(rows=[(None, None)])
        connection = _StubConnection(cursor)

        async def fake_connect(*, conninfo: str, autocommit: bool) -> _StubConnection:
            assert conninfo == "postgres://example"
            assert autocommit is True
            return connection

        monkeypatch.setattr(
            "src.cdc_logical_replication.slot.psycopg.AsyncConnection.connect",
            fake_connect,
        )

        resolved = await get_replication_slot_confirmed_lsn(
            conninfo="postgres://example",
            slot_name="slot_a",
        )

        assert resolved == 0
        assert connection.closed is True

    asyncio.run(scenario())


def test_get_replication_slot_confirmed_lsn_raises_when_slot_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        cursor = _StubCursor(rows=[None])
        connection = _StubConnection(cursor)

        async def fake_connect(*, conninfo: str, autocommit: bool) -> _StubConnection:
            assert conninfo == "postgres://example"
            assert autocommit is True
            return connection

        monkeypatch.setattr(
            "src.cdc_logical_replication.slot.psycopg.AsyncConnection.connect",
            fake_connect,
        )

        with pytest.raises(RuntimeError, match="Replication slot not found"):
            await get_replication_slot_confirmed_lsn(
                conninfo="postgres://example",
                slot_name="slot_a",
            )

        assert connection.closed is True

    asyncio.run(scenario())
