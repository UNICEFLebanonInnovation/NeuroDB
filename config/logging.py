"""One JSON object per log line, so Azure Log Analytics (ContainerAppConsoleLogs) can filter by field."""

from __future__ import annotations

import json
import logging

_SKIP = set(vars(logging.makeLogRecord({}))) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        # Extra fields passed with logger.info(..., extra={...}); request objects are not serialisable.
        for key, value in vars(record).items():
            if key not in _SKIP and key != "request":
                payload[key] = (
                    value if isinstance(value, str | int | float | bool | type(None)) else str(value)
                )
        return json.dumps(payload, ensure_ascii=False)
