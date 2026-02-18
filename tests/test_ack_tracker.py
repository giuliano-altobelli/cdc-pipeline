from __future__ import annotations

import pytest
from src.cdc_logical_replication.ack import AckTracker


def test_register_returns_monotonic_ack_ids_for_duplicate_lsns() -> None:
    tracker = AckTracker(initial_lsn=0)
    first = tracker.register(100)
    second = tracker.register(100)
    third = tracker.register(200)

    assert (first, second, third) == (1, 2, 3)


def test_last_registered_lsn_tracks_registered_progress_not_frontier() -> None:
    tracker = AckTracker(initial_lsn=10)
    ack_100 = tracker.register(100)
    tracker.register(100)
    tracker.register(120)

    assert tracker.last_registered_lsn == 120
    assert tracker.frontier_lsn == 10

    assert tracker.mark_published_by_id(ack_100) == 100
    assert tracker.frontier_lsn == 100
    assert tracker.last_registered_lsn == 120


def test_frontier_advances_only_for_contiguous_successes_with_id_acks() -> None:
    tracker = AckTracker(initial_lsn=0)
    ack_100 = tracker.register(100)
    ack_200 = tracker.register(200)
    ack_300 = tracker.register(300)

    assert tracker.mark_published_by_id(ack_200) is None
    assert tracker.frontier_lsn == 0

    assert tracker.mark_published_by_id(ack_100) == 200
    assert tracker.frontier_lsn == 200

    assert tracker.mark_published_by_id(ack_300) == 300
    assert tracker.frontier_lsn == 300


def test_duplicate_lsn_entries_are_safe_when_acknowledged_by_unique_id() -> None:
    tracker = AckTracker(initial_lsn=0)
    first_100 = tracker.register(100)
    second_100 = tracker.register(100)
    ack_200 = tracker.register(200)

    assert tracker.mark_published_by_id(second_100) is None
    assert tracker.frontier_lsn == 0

    assert tracker.mark_published_by_id(first_100) == 100
    assert tracker.frontier_lsn == 100

    assert tracker.mark_published_by_id(ack_200) == 200
    assert tracker.frontier_lsn == 200


def test_legacy_mark_published_lsn_keeps_existing_strict_behavior() -> None:
    tracker = AckTracker(initial_lsn=0)
    tracker.register(100)

    assert tracker.mark_published(100) == 100
    assert tracker.frontier_lsn == 100

    with pytest.raises(KeyError):
        tracker.mark_published(100)


def test_mark_published_by_id_duplicate_callback_is_idempotent() -> None:
    tracker = AckTracker(initial_lsn=0)
    ack_100 = tracker.register(100)
    ack_200 = tracker.register(200)

    assert tracker.mark_published_by_id(ack_200) is None
    assert tracker.frontier_lsn == 0

    assert tracker.mark_published_by_id(ack_100) == 200
    assert tracker.frontier_lsn == 200
    assert tracker.mark_published_by_id(ack_100) is None
    assert tracker.frontier_lsn == 200


def test_mark_published_by_id_unknown_future_id_raises() -> None:
    tracker = AckTracker(initial_lsn=0)
    tracker.register(100)

    with pytest.raises(KeyError):
        tracker.mark_published_by_id(200)


def test_large_inflight_reverse_publish_advances_frontier_only_when_contiguous() -> None:
    tracker = AckTracker(initial_lsn=0)
    max_lsn = 10_000
    ack_ids: list[int] = []
    for lsn in range(1, max_lsn + 1):
        ack_ids.append(tracker.register(lsn))

    for ack_id in range(max_lsn, 1, -1):
        assert tracker.mark_published_by_id(ack_ids[ack_id - 1]) is None
        assert tracker.frontier_lsn == 0

    assert tracker.mark_published_by_id(ack_ids[0]) == max_lsn
    assert tracker.frontier_lsn == max_lsn


def test_register_allows_regressive_lsn_and_keeps_high_watermark() -> None:
    tracker = AckTracker(initial_lsn=0)
    first = tracker.register(300)
    second = tracker.register(100)

    assert (first, second) == (1, 2)
    assert tracker.frontier_lsn == 0
    assert tracker.last_registered_lsn == 300

    assert tracker.mark_published_by_id(first) == 300
    assert tracker.mark_published_by_id(second) is None
    assert tracker.frontier_lsn == 300
    assert tracker.last_registered_lsn == 300
