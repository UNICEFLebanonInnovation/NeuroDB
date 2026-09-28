"""The in-app scheduler: starts the scheduled jobs (admin → Scheduled jobs) when they are due.

App Service has no scheduler, so the web container runs one. Every gunicorn worker starts this loop
in a thread (``gunicorn.conf.py``, ``post_worker_init``); only the one holding a PostgreSQL advisory
lock acts, so with several workers or replicas a job starts once. When that worker stops, its
database session ends, the lock is freed and another worker takes over within a minute.

A due job starts its command in the background (``background.start_command``), exactly as the
admin's buttons do, recorded as a run with "schedule" as its trigger. A job whose previous run is
still going is skipped until its next time. Missed times while no scheduler ran (a restart) are
caught up once, not once per missed time. ``SCHEDULER_ENABLED=false`` switches it off.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import time

from django.conf import settings
from django.db import close_old_connections, connection
from django.utils import timezone

logger = logging.getLogger(__name__)

LOCK_ID = 7140430  # one scheduler at a time (see background.py for the other lock ids)
INTERVAL_SECONDS = 30
_started = False


def _holds_lock() -> bool:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT 1 FROM pg_locks WHERE locktype = 'advisory' AND classid = 0 AND objid = %s "
            "AND objsubid = 1 AND pid = pg_backend_pid() AND granted",
            [LOCK_ID],
        )
        if cursor.fetchone():
            return True
        cursor.execute("SELECT pg_try_advisory_lock(%s)", [LOCK_ID])
        return bool(cursor.fetchone()[0])


def tick(now=None) -> list[str]:
    """One pass: start the jobs that are due. Returns the keys started (for tests and logs)."""
    from neurodb.core import jobs
    from neurodb.core.models import ScheduledJob, SchedulerState

    now = now or timezone.now()
    SchedulerState.objects.update_or_create(
        pk=1, defaults={"last_seen_at": now, "host": f"{socket.gethostname()} (pid {os.getpid()})"}
    )
    started = []
    for job in ScheduledJob.objects.filter(enabled=True):
        try:
            if job.next_run_at is None:  # new, re-enabled or edited: plan it, do not run it now
                job.plan_next(now)
                job.save(update_fields=["next_run_at"])
                continue
            if job.next_run_at > now:
                continue
            if job.command not in jobs.COMMANDS:
                job.last_outcome = f"not started: unknown command {job.command}"
            else:
                outcome = jobs.start(job.command, triggered_by="schedule")
                skipped = "skipped: the previous run is still going"
                job.last_outcome = "started" if outcome == "started" else skipped
                if outcome == "started":
                    job.last_started_at = now
                    started.append(job.key)
            job.plan_next(now)
            job.save(update_fields=["next_run_at", "last_started_at", "last_outcome"])
        except Exception as exc:  # one broken job never stops the others
            logger.exception("scheduler: job %s", job.key)
            job.last_outcome = f"error: {exc}"[:200]
            job.save(update_fields=["last_outcome"])
    if started:
        logger.info("scheduler: started %s", ", ".join(started))
    return started


def run_forever(interval: int = INTERVAL_SECONDS) -> None:
    while True:
        try:
            close_old_connections()
            if connection.vendor == "postgresql" and _holds_lock():
                tick()
        except Exception:  # a database hiccup: log it, drop the connection (and the lock), go on
            logger.exception("scheduler pass failed")
            try:
                connection.close()
            except Exception:  # noqa: BLE001, S110 - already broken
                pass
        time.sleep(interval)


def start() -> None:
    """Start the loop in a daemon thread, once per process, unless SCHEDULER_ENABLED is false."""
    global _started
    if _started or not settings.SCHEDULER_ENABLED:
        return
    _started = True
    threading.Thread(target=run_forever, name="neurodb-scheduler", daemon=True).start()
    logger.info("scheduler thread started (pid %s)", os.getpid())
