from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

_LOG_RECORD_DEFAULT_ATTRS = frozenset(logging.makeLogRecord({}).__dict__) | {"asctime", "message"}


def _json_default(value: Any) -> str:
    if isinstance(value, (bytes, bytearray, memoryview)):
        return repr(bytes(value))
    return str(value)


class JsonLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": self._format_timestamp(record),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        extra = {
            key: value
            for key, value in record.__dict__.items()
            if key not in _LOG_RECORD_DEFAULT_ATTRS
        }
        if extra:
            payload["extra"] = extra

        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=_json_default, separators=(",", ":"))

    @staticmethod
    def _format_timestamp(record: logging.LogRecord) -> str:
        return (
            datetime.fromtimestamp(record.created, tz=timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
