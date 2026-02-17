from __future__ import annotations

from src.cdc_logical_replication.partition_key import extract_partition_key
from src.cdc_logical_replication.protocol import lsn_str_to_int


def test_primary_key_mode_uses_wal2json_pk() -> None:
    payload = (
        b'{"action":"I","schema":"public","table":"orders",'
        b'"columns":[{"name":"id","value":42},{"name":"status","value":"created"}],'
        b'"pk":[{"name":"id","type":"bigint"}]}\n'
    )

    partition_key = extract_partition_key(
        payload,
        lsn=lsn_str_to_int("0/16B6D80"),
        mode="primary_key",
        fallback="lsn",
        static_fallback_value=None,
    )

    assert partition_key == "public.orders:id=42"


def test_primary_key_mode_uses_identity_values_for_delete() -> None:
    payload = (
        b'{"action":"D","schema":"public","table":"orders",'
        b'"identity":[{"name":"id","value":7}],"pk":[{"name":"id","type":"bigint"}]}'
    )

    partition_key = extract_partition_key(
        payload,
        lsn=lsn_str_to_int("0/16B6D80"),
        mode="primary_key",
        fallback="lsn",
        static_fallback_value=None,
    )

    assert partition_key == "public.orders:id=7"


def test_primary_key_mode_falls_back_when_payload_invalid() -> None:
    partition_key = extract_partition_key(
        b"not-json",
        lsn=lsn_str_to_int("0/16B6D80"),
        mode="primary_key",
        fallback="lsn",
        static_fallback_value=None,
    )

    assert partition_key == "0/16B6D80"


def test_table_fallback_prefers_schema_table() -> None:
    payload = b'{"action":"I","schema":"finance","table":"transactions"}'

    partition_key = extract_partition_key(
        payload,
        lsn=lsn_str_to_int("0/16B6D80"),
        mode="fallback",
        fallback="table",
        static_fallback_value=None,
    )

    assert partition_key == "finance.transactions"


def test_static_fallback_requires_value() -> None:
    payload = b"{}"

    try:
        extract_partition_key(
            payload,
            lsn=0,
            mode="fallback",
            fallback="static",
            static_fallback_value=None,
        )
    except ValueError as exc:
        assert "PARTITION_KEY_STATIC_VALUE" in str(exc)
    else:
        raise AssertionError("Expected ValueError for missing static fallback")


def test_long_keys_are_hashed() -> None:
    long_value = "x" * 300
    payload = (
        b'{"action":"I","schema":"public","table":"orders","columns":[{"name":"id","value":"'
        + long_value.encode("utf-8")
        + b'"}],"pk":[{"name":"id","type":"text"}]}'
    )

    partition_key = extract_partition_key(
        payload,
        lsn=0,
        mode="primary_key",
        fallback="lsn",
        static_fallback_value=None,
    )

    assert len(partition_key) == 64
