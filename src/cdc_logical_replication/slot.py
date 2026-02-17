from __future__ import annotations

import psycopg


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
