from __future__ import annotations

import logging
from collections import deque

from pydantic import BaseModel

LOGGER = logging.getLogger(__name__)


class _PendingLsn(BaseModel):
    ack_id: int
    lsn: int
    published: bool = False


class AckTracker:
    """Tracks highest contiguous LSN confirmed published to Kinesis."""

    def __init__(self, *, initial_lsn: int = 0) -> None:
        self._frontier = initial_lsn
        self._last_registered = initial_lsn
        self._next_ack_id = 0
        self._last_drained_ack_id = 0
        self._pending: deque[_PendingLsn] = deque()
        self._pending_by_ack_id: dict[int, _PendingLsn] = {}
        self._pending_by_lsn: dict[int, deque[int]] = {}

    @property
    def frontier_lsn(self) -> int:
        return self._frontier

    @property
    def last_registered_lsn(self) -> int:
        return self._last_registered

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def register(self, lsn: int) -> int:
        previous_last_registered = self._last_registered
        if lsn < previous_last_registered:
            LOGGER.warning(
                "ack_register_lsn_regression",
                extra={
                    "incoming_lsn": lsn,
                    "last_registered_lsn": previous_last_registered,
                    "frontier_lsn": self._frontier,
                },
            )

        self._next_ack_id += 1
        ack_id = self._next_ack_id
        self._last_registered = max(previous_last_registered, lsn)
        pending = _PendingLsn(ack_id=ack_id, lsn=lsn)
        self._pending.append(pending)
        self._pending_by_ack_id[ack_id] = pending
        self._pending_by_lsn.setdefault(lsn, deque()).append(ack_id)
        return ack_id

    def mark_published(self, lsn: int) -> int | None:
        """Legacy LSN-based ack path; prefer mark_published_by_id."""
        bucket = self._pending_by_lsn.get(lsn)
        if not bucket:
            raise KeyError(f"Unknown or already-published LSN: {lsn}")

        pending: _PendingLsn | None = None
        for ack_id in bucket:
            candidate = self._pending_by_ack_id.get(ack_id)
            if candidate is not None and not candidate.published:
                pending = candidate
                break

        if pending is None:
            raise KeyError(f"Unknown or already-published LSN: {lsn}")

        pending.published = True
        return self._drain_contiguous_published()

    def mark_published_by_id(self, ack_id: int) -> int | None:
        pending = self._pending_by_ack_id.get(ack_id)
        if pending is None:
            if 0 < ack_id <= self._last_drained_ack_id:
                return None
            raise KeyError(f"Unknown ack id: {ack_id}")

        if pending.published:
            return None

        pending.published = True
        return self._drain_contiguous_published()

    def _drain_contiguous_published(self) -> int | None:
        advanced_to: int | None = None
        while self._pending and self._pending[0].published:
            pending = self._pending.popleft()
            self._last_drained_ack_id = pending.ack_id
            del self._pending_by_ack_id[pending.ack_id]

            bucket = self._pending_by_lsn[pending.lsn]
            if not bucket:
                raise RuntimeError(f"Invariant violation: empty LSN bucket for {pending.lsn}")
            bucket_head_ack_id = bucket.popleft()
            if bucket_head_ack_id != pending.ack_id:
                raise RuntimeError(
                    "Invariant violation: pending bucket order mismatch "
                    f"(lsn={pending.lsn}, expected_ack_id={pending.ack_id}, "
                    f"bucket_head_ack_id={bucket_head_ack_id})"
                )
            if not bucket:
                del self._pending_by_lsn[pending.lsn]

            if pending.lsn > self._frontier:
                self._frontier = pending.lsn
                advanced_to = self._frontier

        return advanced_to
