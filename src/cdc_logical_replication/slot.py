from __future__ import annotations

import psycopg

from cdc_logical_replication.protocol import lsn_str_to_int


async def ensure_replication_slot(*, conninfo: str, slot_name: str, output_plugin: str) -> bool:
    """Create the logical replication slot if missing.

    Returns True when slot was created in this call, False when it already existed.
    """

    connection = await psycopg.AsyncConnection.connect(conninfo=conninfo, autocommit=True)
    try:
        async with connection.cursor() as cursor:
            await cursor.execute(
                "SELECT 1 FROM pg_replication_slots WHERE slot_name = %s",
                (slot_name,),
            )
            existing = await cursor.fetchone()
            if existing:
                return False

            await cursor.execute(
                "SELECT * FROM pg_create_logical_replication_slot(%s, %s)",
                (slot_name, output_plugin),
            )
            _ = await cursor.fetchone()
            return True
    finally:
        await connection.close()


async def get_replication_slot_confirmed_lsn(*, conninfo: str, slot_name: str) -> int:
    """Return the slot checkpoint LSN used for replication startup.

    Preference order:
    1. confirmed_flush_lsn
    2. restart_lsn
    3. 0 when neither LSN has been initialized yet
    """

    connection = await psycopg.AsyncConnection.connect(conninfo=conninfo, autocommit=True)
    try:
        async with connection.cursor() as cursor:
            await cursor.execute(
                """
                SELECT confirmed_flush_lsn::text, restart_lsn::text
                FROM pg_replication_slots
                WHERE slot_name = %s
                """,
                (slot_name,),
            )
            row = await cursor.fetchone()
            if row is None:
                raise RuntimeError(f"Replication slot not found: {slot_name}")

            confirmed_flush_lsn, restart_lsn = row
            return _resolve_slot_start_lsn(
                slot_name=slot_name,
                confirmed_flush_lsn=confirmed_flush_lsn,
                restart_lsn=restart_lsn,
            )
    finally:
        await connection.close()


def _resolve_slot_start_lsn(
    *,
    slot_name: str,
    confirmed_flush_lsn: object,
    restart_lsn: object,
) -> int:
    confirmed_lsn = _parse_lsn_field(
        slot_name=slot_name,
        field_name="confirmed_flush_lsn",
        value=confirmed_flush_lsn,
    )
    if confirmed_lsn is not None:
        return confirmed_lsn

    restart_lsn_int = _parse_lsn_field(
        slot_name=slot_name,
        field_name="restart_lsn",
        value=restart_lsn,
    )
    if restart_lsn_int is not None:
        return restart_lsn_int

    return 0


def _parse_lsn_field(*, slot_name: str, field_name: str, value: object) -> int | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise RuntimeError(
            f"Unexpected {field_name} type for slot {slot_name}: {type(value).__name__}"
        )
    return lsn_str_to_int(value)
