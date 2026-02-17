from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ChangeEvent(BaseModel):
    """Single wal2json event destined for Kinesis."""

    model_config = ConfigDict(frozen=True)

    lsn: int
    ack_id: int | None = None
    payload: bytes
    partition_key: str

    @property
    def record_size_bytes(self) -> int:
        # Kinesis request sizing is dominated by payload bytes plus partition key length.
        return len(self.payload) + len(self.partition_key.encode("utf-8"))
