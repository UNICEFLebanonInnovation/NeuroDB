"""Gunicorn settings for the container. Every value can be overridden with an environment variable."""

import os

bind = f"0.0.0.0:{os.environ.get('PORT', '8000')}"
workers = int(os.environ.get("WEB_CONCURRENCY", "3"))  # 0.5-1 vCPU: 2-3 workers x 4 threads
worker_class = "gthread"
threads = int(os.environ.get("GUNICORN_THREADS", "4"))
timeout = int(os.environ.get("GUNICORN_TIMEOUT", "120"))  # large Excel exports
graceful_timeout = 30  # Container Apps sends SIGTERM and waits 30 s by default
keepalive = 75  # longer than the Azure ingress idle timeout, avoids 502s on reused connections
max_requests = 2000
max_requests_jitter = 200
worker_tmp_dir = "/dev/shm"  # noqa: S108 - heartbeat files off the container filesystem (works with a read-only root)
forwarded_allow_ips = "*"  # only the Azure ingress can reach the container
proxy_allow_ips = "*"
accesslog = "-"
errorlog = "-"
loglevel = os.environ.get("GUNICORN_LOG_LEVEL", "info")
access_log_format = '%(h)s "%(r)s" %(s)s %(b)s %(M)sms "%(a)s"'
# The probes run every few seconds; keep them out of the access log.
logconfig_dict = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {"no_probes": {"()": "config.gunicorn_filters.SkipHealthChecks"}},
    "handlers": {
        "console": {"class": "logging.StreamHandler", "stream": "ext://sys.stdout"},
        "access": {"class": "logging.StreamHandler", "stream": "ext://sys.stdout", "filters": ["no_probes"]},
        "error": {"class": "logging.StreamHandler", "stream": "ext://sys.stderr"},
    },
    "root": {"level": "INFO", "handlers": ["console"]},
    "loggers": {
        "gunicorn.access": {"handlers": ["access"], "level": "INFO", "propagate": False},
        "gunicorn.error": {"handlers": ["error"], "level": "INFO", "propagate": False},
    },
}
