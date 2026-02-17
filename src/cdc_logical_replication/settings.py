from __future__ import annotations

import hashlib
import re
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_SLOT_PATTERN = re.compile(r"^[A-Za-z0-9_]+$")


def slot_hash64(slot_name: str) -> int:
    digest = hashlib.sha256(slot_name.encode("utf-8")).digest()
    value = int.from_bytes(digest[:8], "big", signed=False)
    if value >= (1 << 63):
        value -= 1 << 64
    return value


def _bool_to_wal2json(value: bool) -> str:
    return "1" if value else "0"


def _sql_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(case_sensitive=False)

    pghost: str = Field(alias="PGHOST")
    pgport: int = Field(default=5432, alias="PGPORT")
    pguser: str = Field(alias="PGUSER")
    pgpassword: str = Field(alias="PGPASSWORD")
    pgdatabase: str = Field(alias="PGDATABASE")
    replication_slot: str = Field(default="etl_slot_wal2json", alias="REPLICATION_SLOT")
    output_plugin: Literal["wal2json"] = Field(default="wal2json", alias="OUTPUT_PLUGIN")
    connect_timeout_s: int = Field(default=5, alias="CONNECT_TIMEOUT_S")

    wal2json_format_version: Literal[2] = Field(default=2, alias="WAL2JSON_FORMAT_VERSION")
    wal2json_include_timestamp: bool = Field(default=True, alias="WAL2JSON_INCLUDE_TIMESTAMP")
    wal2json_include_lsn: bool = Field(default=True, alias="WAL2JSON_INCLUDE_LSN")
    wal2json_include_transactions: bool = Field(
        default=False,
        alias="WAL2JSON_INCLUDE_TRANSACTIONS",
    )
    wal2json_include_pk: bool = Field(default=True, alias="WAL2JSON_INCLUDE_PK")
    wal2json_add_tables: str | None = Field(default=None, alias="WAL2JSON_ADD_TABLES")

    aws_region: str = Field(alias="AWS_REGION")
    kinesis_stream: str = Field(alias="KINESIS_STREAM")
    kinesis_batch_max_records: int = Field(default=200, alias="KINESIS_BATCH_MAX_RECORDS")
    kinesis_batch_max_bytes: int = Field(default=900000, alias="KINESIS_BATCH_MAX_BYTES")
    kinesis_batch_max_delay_ms: int = Field(default=10, alias="KINESIS_BATCH_MAX_DELAY_MS")

    partition_key_mode: Literal["primary_key", "fallback"] = Field(
        default="primary_key",
        alias="PARTITION_KEY_MODE",
    )
    partition_key_fallback: Literal["lsn", "table", "static"] = Field(
        default="lsn",
        alias="PARTITION_KEY_FALLBACK",
    )
    partition_key_static_value: str | None = Field(default=None, alias="PARTITION_KEY_STATIC_VALUE")

    inflight_max_messages: int = Field(default=10000, alias="INFLIGHT_MAX_MESSAGES")
    inflight_max_bytes: int = Field(default=134217728, alias="INFLIGHT_MAX_BYTES")

    leader_lock_key_derivation: Literal["slot_hash64"] = Field(
        default="slot_hash64",
        alias="LEADER_LOCK_KEY_DERIVATION",
    )
    leader_lock_key_override: int | None = Field(default=None, alias="LEADER_LOCK_KEY_OVERRIDE")
    standby_retry_interval_s: float = Field(default=5.0, alias="STANDBY_RETRY_INTERVAL_S")

    replication_feedback_interval_s: float = Field(
        default=1.0,
        alias="REPLICATION_FEEDBACK_INTERVAL_S",
    )
    kinesis_retry_base_delay_ms: int = Field(default=100, alias="KINESIS_RETRY_BASE_DELAY_MS")
    kinesis_retry_max_delay_ms: int = Field(default=5000, alias="KINESIS_RETRY_MAX_DELAY_MS")
    kinesis_retry_max_attempts: int = Field(default=3, alias="KINESIS_RETRY_MAX_ATTEMPTS")

    @field_validator("replication_slot")
    @classmethod
    def _validate_slot_name(cls, value: str) -> str:
        if not _SLOT_PATTERN.fullmatch(value):
            raise ValueError(
                "REPLICATION_SLOT must only contain ASCII letters, numbers, and underscore"
            )
        return value

    @field_validator("kinesis_batch_max_records")
    @classmethod
    def _validate_batch_records(cls, value: int) -> int:
        if value < 1 or value > 500:
            raise ValueError("KINESIS_BATCH_MAX_RECORDS must be between 1 and 500")
        return value

    @field_validator("kinesis_batch_max_bytes")
    @classmethod
    def _validate_batch_bytes(cls, value: int) -> int:
        if value < 1 or value > 1_000_000:
            raise ValueError("KINESIS_BATCH_MAX_BYTES must be between 1 and 1_000_000")
        return value

    @field_validator("kinesis_batch_max_delay_ms")
    @classmethod
    def _validate_batch_delay(cls, value: int) -> int:
        if value < 1:
            raise ValueError("KINESIS_BATCH_MAX_DELAY_MS must be > 0")
        return value

    @field_validator("kinesis_retry_max_attempts")
    @classmethod
    def _validate_retry_attempts(cls, value: int) -> int:
        if value < 1:
            raise ValueError("KINESIS_RETRY_MAX_ATTEMPTS must be >= 1")
        return value

    @model_validator(mode="after")
    def _validate_static_partition_fallback(self) -> Settings:
        if self.partition_key_fallback == "static" and not self.partition_key_static_value:
            raise ValueError("PARTITION_KEY_STATIC_VALUE is required when fallback mode is static")
        return self

    @property
    def leader_lock_key(self) -> int:
        if self.leader_lock_key_override is not None:
            return self.leader_lock_key_override
        return slot_hash64(self.replication_slot)

    @property
    def postgres_conninfo(self) -> str:
        # libpq conninfo avoids accidental DSN parsing differences in different call sites.
        return (
            f"host={self.pghost} port={self.pgport} user={self.pguser} "
            f"password={_sql_quote(self.pgpassword)} dbname={self.pgdatabase} "
            f"connect_timeout={self.connect_timeout_s}"
        )

    @property
    def wal2json_options_sql(self) -> str:
        options = {
            "format-version": str(self.wal2json_format_version),
            "include-timestamp": _bool_to_wal2json(self.wal2json_include_timestamp),
            "include-lsn": _bool_to_wal2json(self.wal2json_include_lsn),
            # wal2json option name is singular ("include-transaction").
            "include-transaction": _bool_to_wal2json(self.wal2json_include_transactions),
            "include-pk": _bool_to_wal2json(self.wal2json_include_pk),
        }
        if self.wal2json_add_tables:
            options["add-tables"] = self.wal2json_add_tables
        return ", ".join(f'"{k}" {_sql_quote(v)}' for k, v in options.items())
