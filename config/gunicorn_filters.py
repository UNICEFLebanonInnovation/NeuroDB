import logging


class SkipHealthChecks(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return "/healthz/" not in record.getMessage()
