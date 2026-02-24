from __future__ import annotations

import json
import logging
import sys
from datetime import datetime

from src.cdc_logical_replication.json_logging import JsonLogFormatter


def test_json_log_formatter_outputs_parseable_json_with_core_fields() -> None:
    formatter = JsonLogFormatter()
    record = logging.LogRecord(
        name="cdc_logical_replication.app",
        level=logging.INFO,
        pathname=__file__,
        lineno=10,
        msg="service_start %s",
        args=("ok",),
        exc_info=None,
    )

    payload = json.loads(formatter.format(record))
    datetime.fromisoformat(payload["timestamp"].replace("Z", "+00:00"))

    assert payload["level"] == "INFO"
    assert payload["logger"] == "cdc_logical_replication.app"
    assert payload["message"] == "service_start ok"


def test_json_log_formatter_includes_exc_info_for_exception_records() -> None:
    formatter = JsonLogFormatter()
    try:
        raise ValueError("boom")
    except ValueError:
        record = logging.LogRecord(
            name="cdc_logical_replication.app",
            level=logging.ERROR,
            pathname=__file__,
            lineno=24,
            msg="leader_cycle_failed",
            args=(),
            exc_info=sys.exc_info(),
        )

    payload = json.loads(formatter.format(record))
    exc_info = payload["exc_info"]

    assert "Traceback (most recent call last)" in exc_info
    assert "ValueError: boom" in exc_info


def test_json_log_formatter_includes_existing_extra_context() -> None:
    formatter = JsonLogFormatter()
    logger = logging.getLogger("cdc_logical_replication.kinesis")
    record = logger.makeRecord(
        name=logger.name,
        level=logging.WARNING,
        fn=__file__,
        lno=42,
        msg="kinesis_publish_retrying",
        args=(),
        exc_info=None,
        extra={"attempt": 3, "payload": b"bytes", "opaque": complex(1, 2)},
    )

    payload = json.loads(formatter.format(record))

    assert payload["extra"] == {"attempt": 3, "payload": "b'bytes'", "opaque": "(1+2j)"}
