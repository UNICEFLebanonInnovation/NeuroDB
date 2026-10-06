import logging
import re

# A key sent in an address (``?key=``: how Power BI sends a Power BI key to the live feed) or in an
# Authorization header: never written to the access log
_QUERY_KEY = re.compile(r"((?:^|[?&])key=)[^&\s\"]*", re.IGNORECASE)
_BEARER = re.compile(r"(Bearer\s+)[^\s\"]+", re.IGNORECASE)
HIDDEN = "[hidden]"


def hide_keys(text: str) -> str:
    return _BEARER.sub(rf"\1{HIDDEN}", _QUERY_KEY.sub(rf"\1{HIDDEN}", text))


class SkipHealthChecks(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return "/healthz/" not in record.getMessage()


class HideKeys(logging.Filter):
    """Gunicorn's access log writes the request line with its query string: the key of the Power BI
    feed (``/powerbi/fmm/<table>.csv?key=…``) is replaced by "[hidden]" before the line is written."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, dict):  # gunicorn hands its atoms over as one mapping
            for name, value in list(record.args.items()):
                if isinstance(value, str):
                    record.args[name] = hide_keys(value)
        elif isinstance(record.args, tuple):
            record.args = tuple(hide_keys(a) if isinstance(a, str) else a for a in record.args)
        if isinstance(record.msg, str):
            record.msg = hide_keys(record.msg)
        return True
