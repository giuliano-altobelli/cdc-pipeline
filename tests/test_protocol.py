from __future__ import annotations

from src.cdc_logical_replication import protocol
from src.cdc_logical_replication.protocol import (
    KEEPALIVE,
    XLOGDATA_HDR,
    build_standby_status,
    lsn_int_to_str,
    lsn_str_to_int,
    now_us,
    parse_keepalive,
    parse_xlogdata,
)


def test_lsn_round_trip() -> None:
    lsn = lsn_str_to_int("16/B374D848")
    assert lsn_int_to_str(lsn) == "16/B374D848"


def test_parse_xlogdata() -> None:
    payload = b'{"change":[]}'
    frame = XLOGDATA_HDR.pack(b"w", 1, 2, 3) + payload

    wal_start, wal_end, server_time_us, parsed_payload = parse_xlogdata(frame)
    assert wal_start == 1
    assert wal_end == 2
    assert server_time_us == 3
    assert parsed_payload == payload


def test_parse_keepalive() -> None:
    frame = KEEPALIVE.pack(b"k", 7, 8, 1)
    wal_end, server_time_us, reply_requested = parse_keepalive(frame)

    assert wal_end == 7
    assert server_time_us == 8
    assert reply_requested == 1


def test_build_standby_status_uses_ack_lsn_for_all_progress_fields() -> None:
    packet = build_standby_status(1234, reply_requested=1)

    assert packet[0:1] == b"r"
    # write_lsn / flush_lsn / apply_lsn (3 int64 fields)
    assert int.from_bytes(packet[1:9], "big", signed=True) == 1234
    assert int.from_bytes(packet[9:17], "big", signed=True) == 1234
    assert int.from_bytes(packet[17:25], "big", signed=True) == 1234
    assert packet[-1] == 1


def test_now_us_uses_postgres_epoch_offset(monkeypatch) -> None:
    monkeypatch.setattr(protocol.time, "time", lambda: 946_684_800.0)
    assert now_us() == 0

    monkeypatch.setattr(protocol.time, "time", lambda: 946_684_801.25)
    assert now_us() == 1_250_000


def test_build_standby_status_uses_postgres_epoch_timestamp(monkeypatch) -> None:
    monkeypatch.setattr(protocol.time, "time", lambda: 946_684_801.25)

    packet = build_standby_status(1234, reply_requested=0)

    assert int.from_bytes(packet[25:33], "big", signed=True) == 1_250_000
