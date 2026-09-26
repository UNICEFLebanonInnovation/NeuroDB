"""Start a sync command from the website, in its own process.

On App Service there are no scheduled jobs and no shell into the container, so the admin can start
a sync from *Import and sync runs*. The command runs as a separate process (it can take longer than
any web request and survives the web worker being recycled) and writes its ``SyncRun`` rows as
usual; its log lines go to the container log. The command itself holds a database lock, so a second
start while one is running does nothing.
"""

from __future__ import annotations

import datetime
import logging
import subprocess
import sys

from django.conf import settings
from django.db import connection
from django.utils import timezone

from neurodb.core.models import SyncRun

logger = logging.getLogger(__name__)

RUNNING_FOR_AT_MOST = datetime.timedelta(hours=4)  # a run older than this was cut off (restart)
DATAMART_LOCK_ID = 7140428  # one Datamart sync at a time, whoever started it (schedule, admin, shell)
CUT_OFF = "Cut off: the sync process stopped before this run finished (the container was restarted)."


DAILY_REVIEW_LOCK_ID = 7140429  # neurodb.review.services.LOCK_ID
LOCK_IDS = {SyncRun.Job.ETOOLS_DATAMART: DATAMART_LOCK_ID, SyncRun.Job.DAILY_REVIEW: DAILY_REVIEW_LOCK_ID}


def lock_is_held(lock_id: int) -> bool | None:
    """Whether a process holds the advisory lock ``lock_id`` right now; ``None`` when the database
    cannot tell (not PostgreSQL). A session-level advisory lock dies with the process that took it,
    so this is the truth about a run in progress, unlike the ``SyncRun`` row it may have left behind."""
    if connection.vendor != "postgresql":
        return None
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT 1 FROM pg_locks WHERE locktype = 'advisory' AND classid = 0 AND objid = %s "
            "AND objsubid = 1 LIMIT 1",
            [lock_id],
        )
        return cursor.fetchone() is not None


def datamart_lock_is_held() -> bool | None:
    return lock_is_held(DATAMART_LOCK_ID)


def is_running(job: str) -> bool:
    """A run of ``job`` is in progress. A ``RUNNING`` row whose process no longer holds the lock (the
    container was restarted mid-run, typically by a deployment) is closed as failed so that the next
    sync can start instead of waiting hours for the row to age out."""
    now = timezone.now()
    running = SyncRun.objects.filter(
        job=job, status=SyncRun.Status.RUNNING, started_at__gte=now - RUNNING_FOR_AT_MOST
    )
    if not running.exists():
        return False
    if job == SyncRun.Job.ETOOLS_DATAMART:
        held = datamart_lock_is_held()
    else:
        held = lock_is_held(LOCK_IDS[job]) if job in LOCK_IDS else None
    if held is None or held:
        return True
    closed = running.update(status=SyncRun.Status.FAILED, finished_at=now, error=CUT_OFF)
    logger.warning("%s: closed %s cut-off run(s) left as running by a stopped process", job, closed)
    return False


def start_command(*args: str) -> int:
    """Start ``manage.py <args>`` in the background and return its process id."""
    process = subprocess.Popen(  # noqa: S603 - fixed interpreter and script; arguments come from code
        [sys.executable, str(settings.BASE_DIR / "manage.py"), *args],
        cwd=settings.BASE_DIR,
        stdin=subprocess.DEVNULL,
        start_new_session=True,  # not stopped with the web worker that started it
    )
    logger.info("started manage.py %s in the background (pid %s)", " ".join(args), process.pid)
    return process.pid
