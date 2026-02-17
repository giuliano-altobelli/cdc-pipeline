from __future__ import annotations

import hashlib
import json
from typing import Any

from cdc_logical_replication.protocol import lsn_int_to_str

PARTITION_KEY_MAX_LEN = 256


def extract_partition_key(
    payload: bytes,
    *,
    lsn: int,
    mode: str,
    fallback: str,
    static_fallback_value: str | None,
) -> str:
    if mode == "primary_key":
        parsed_payload = _safe_parse(payload)
        primary = _primary_key_from_payload(parsed_payload)
        if primary:
            return _normalize_partition_key(primary)
        return _normalize_partition_key(
            _fallback_key(
                fallback=fallback,
                static_value=static_fallback_value,
                lsn=lsn,
                parsed_payload=parsed_payload,
            )
        )

    if mode == "fallback":
        return _normalize_partition_key(
            _fallback_key(
                fallback=fallback,
                static_value=static_fallback_value,
                lsn=lsn,
                parsed_payload=_safe_parse(payload),
            )
        )

    raise ValueError(f"Unsupported partition key mode: {mode}")


def _safe_parse(payload: bytes) -> dict[str, Any] | None:
    normalized = payload.rstrip(b"\n")
    if not normalized:
        return None

    try:
        decoded = json.loads(normalized)
    except json.JSONDecodeError:
        return None

    if not isinstance(decoded, dict):
        return None

    return decoded


def _primary_key_from_payload(parsed_payload: dict[str, Any] | None) -> str | None:
    if not parsed_payload:
        return None

    table_name = _table_name(parsed_payload)
    if table_name == "unknown.unknown":
        return None

    pk_entries = parsed_payload.get("pk")
    if not isinstance(pk_entries, list) or not pk_entries:
        return None

    named_values = _column_value_map(parsed_payload)
    key_parts: list[str] = []
    for entry in pk_entries:
        if not isinstance(entry, dict):
            continue

        name = entry.get("name")
        if not isinstance(name, str):
            continue

        value = entry.get("value")
        if value is None and name in named_values:
            value = named_values[name]
        if value is None:
            continue
        key_parts.append(f"{name}={value}")

    if not key_parts:
        return None

    return f"{table_name}:{'|'.join(key_parts)}"


def _fallback_key(
    *,
    fallback: str,
    static_value: str | None,
    lsn: int,
    parsed_payload: dict[str, Any] | None,
) -> str:
    if fallback == "lsn":
        return lsn_int_to_str(lsn)

    if fallback == "table":
        table_name = _table_name(parsed_payload)
        if table_name != "unknown.unknown":
            return table_name
        return lsn_int_to_str(lsn)

    if fallback == "static":
        if not static_value:
            raise ValueError("PARTITION_KEY_STATIC_VALUE must be set for static fallback mode")
        return static_value

    raise ValueError(f"Unsupported fallback partition key mode: {fallback}")


def _column_value_map(parsed_payload: dict[str, Any]) -> dict[str, Any]:
    value_by_name: dict[str, Any] = {}
    for field in ("identity", "columns"):
        entries = parsed_payload.get(field)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name")
            if not isinstance(name, str):
                continue
            if "value" not in entry:
                continue
            value_by_name[name] = entry["value"]
    return value_by_name


def _table_name(parsed_payload: dict[str, Any] | None) -> str:
    if not parsed_payload:
        return "unknown.unknown"

    schema = parsed_payload.get("schema") if isinstance(parsed_payload.get("schema"), str) else "unknown"
    table = parsed_payload.get("table") if isinstance(parsed_payload.get("table"), str) else "unknown"
    return f"{schema}.{table}"


def _normalize_partition_key(candidate: str) -> str:
    if not candidate:
        return "0"

    if len(candidate) <= PARTITION_KEY_MAX_LEN:
        return candidate

    return hashlib.sha256(candidate.encode("utf-8")).hexdigest()
