"""Application Insights through OpenTelemetry, switched on by APPLICATIONINSIGHTS_CONNECTION_STRING.

Called once from wsgi.py (web) and manage.py (jobs) before Django loads, so requests, database
calls, outgoing HTTP to ActivityInfo/eTools and exceptions are traced. Without the variable (local
development, tests) nothing is imported and nothing is sent.
"""

from __future__ import annotations

import logging
import os

_configured = False


def setup(role: str) -> None:
    global _configured
    connection_string = os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING", "")
    if _configured or not connection_string:
        return
    os.environ.setdefault("OTEL_SERVICE_NAME", f"neurodb-{role}")
    os.environ.setdefault(
        "OTEL_RESOURCE_ATTRIBUTES", f"service.version={os.environ.get('APP_VERSION', 'dev')}"
    )
    try:
        from azure.monitor.opentelemetry import configure_azure_monitor

        configure_azure_monitor(
            connection_string=connection_string,
            logger_name="neurodb",  # only our loggers are exported; Django request logs come as traces
            enable_live_metrics=role == "web",
        )
        _configured = True
    except Exception:  # telemetry must never stop the application from starting
        logging.getLogger(__name__).exception("Application Insights could not be configured")
