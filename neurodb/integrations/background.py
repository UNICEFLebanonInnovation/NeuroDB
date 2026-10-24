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
import time

from django.conf import settings
from django.db import connection
from django.utils import timezone

from neurodb.core.models import SyncRun

logger = logging.getLogger(__name__)

RUNNING_FOR_AT_MOST = datetime.timedelta(hours=4)  # a run older than this was cut off (restart)
DATAMART_LOCK_ID = 7140428  # one Datamart sync at a time, whoever started it (schedule, admin, shell)
CUT_OFF = "Cut off: the sync process stopped before this run finished (the container was restarted)."


DAILY_REVIEW_LOCK_ID = 7140429  # neurodb.review.services.LOCK_ID
WATCH_LOCK_ID = 7140431  # neurodb.watch.lock.LOCK_ID (7140430 is the scheduler's)
FMM_REFRESH_LOCK_ID = 7140432  # neurodb.fmm.refresh.LOCK_ID
FMM_INSIGHTS_LOCK_ID = 7140433  # the AI briefs of Monitoring insights
FMM_AI_CHECKS_LOCK_ID = 7140434  # the AI checks of Monitoring insights' quality rules
FMM_AP_REVIEW_LOCK_ID = 7140435  # the AI review of completed action points
DOC_REVIEW_LOCK_ID = 7140436  # the document review of the knowledge base
# the eTools update clean-up (neurodb.datamart.backfill): taken first, then held with the Datamart
# sync's lock for the whole run, so a sync that finds the Datamart lock taken knows who has it
DATAMART_BACKFILL_LOCK_ID = 7140437
REPORTING_CHECKS_LOCK_ID = 7140438  # neurodb.datamart.checks.LOCK_ID: one run of the checks at a time
DATAMART_PROBE_LOCK_ID = 7140439  # neurodb.integrations.etools.probe.LOCK_ID: one probe at a time
# neurodb.datamart.master_links.LOCK_ID: one run of the master indicators' suggestions at a time
ETOOLS_MASTERS_LOCK_ID = 7140440
# neurodb.integrations.etools.rest_raw.LOCK_ID: one read of the eTools REST API into Raw eTools data at a time
ETOOLS_REST_RAW_LOCK_ID = 7140441
LOCK_IDS = {
    SyncRun.Job.ETOOLS_DATAMART: DATAMART_LOCK_ID,
    SyncRun.Job.DAILY_REVIEW: DAILY_REVIEW_LOCK_ID,
    SyncRun.Job.WATCH: WATCH_LOCK_ID,
    SyncRun.Job.FMM_REFRESH: FMM_REFRESH_LOCK_ID,
    SyncRun.Job.FMM_INSIGHTS: FMM_INSIGHTS_LOCK_ID,
    SyncRun.Job.FMM_AI_CHECKS: FMM_AI_CHECKS_LOCK_ID,
    SyncRun.Job.FMM_AP_REVIEW: FMM_AP_REVIEW_LOCK_ID,
    SyncRun.Job.DOC_REVIEW: DOC_REVIEW_LOCK_ID,
    SyncRun.Job.DATAMART_BACKFILL: DATAMART_BACKFILL_LOCK_ID,
    SyncRun.Job.REPORTING_CHECKS: REPORTING_CHECKS_LOCK_ID,
    SyncRun.Job.DATAMART_PROBE: DATAMART_PROBE_LOCK_ID,
    SyncRun.Job.ETOOLS_MASTERS: ETOOLS_MASTERS_LOCK_ID,
    SyncRun.Job.ETOOLS_REST_RAW: ETOOLS_REST_RAW_LOCK_ID,
}


def lock_is_held(lock_id: int) -> bool | None:
    """Whether a process holds the advisory lock ``lock_id`` right now; ``None`` when the database
    cannot tell (not PostgreSQL). A session-level advisory lock dies with the process that took it,
    so this is the truth about a run in progress, unlike the ``SyncRun`` row it may have left behind.
    Advisory locks belong to one database, so a lock with the same id in another database on the
    same server (another site, another test run) does not count."""
    if connection.vendor != "postgresql":
        return None
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT 1 FROM pg_locks WHERE locktype = 'advisory' AND classid = 0 AND objid = %s "
            "AND objsubid = 1 "
            "AND database = (SELECT oid FROM pg_database WHERE datname = current_database()) LIMIT 1",
            [lock_id],
        )
        return cursor.fetchone() is not None


def datamart_lock_is_held() -> bool | None:
    return lock_is_held(DATAMART_LOCK_ID)


def datamart_cleanup_is_running() -> bool:
    """The eTools update clean-up holds the Datamart sync's lock: one session holds both its own lock
    and that one (it takes its own first), so a sync can tell it from another sync."""
    if connection.vendor != "postgresql":
        return False
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT 1 FROM pg_locks d JOIN pg_locks c ON c.pid = d.pid AND c.database = d.database "
            "WHERE d.locktype = 'advisory' AND d.classid = 0 AND d.objid = %s AND d.objsubid = 1 "
            "AND d.granted AND c.locktype = 'advisory' AND c.classid = 0 AND c.objid = %s "
            "AND c.objsubid = 1 AND c.granted "
            "AND d.database = (SELECT oid FROM pg_database WHERE datname = current_database()) LIMIT 1",
            [DATAMART_LOCK_ID, DATAMART_BACKFILL_LOCK_ID],
        )
        return cursor.fetchone() is not None


def is_running(job: str) -> bool:
    """A run of ``job`` is in progress. A ``RUNNING`` row whose process no longer holds the lock (the
    container was restarted mid-run, typically by a deployment) is closed as failed so that the next
    sync can start instead of waiting hours for the row to age out: every such row of the job, whatever
    its age (a row older than ``RUNNING_FOR_AT_MOST`` was never closed before, and stayed "running" on
    the pages that list runs). Otherwise a row younger than that counts as running."""
    now = timezone.now()
    running = SyncRun.objects.filter(job=job, status=SyncRun.Status.RUNNING)
    if not running.exists():
        return False
    if job == SyncRun.Job.ETOOLS_DATAMART:
        # the eTools update clean-up holds the same lock: then no sync is running
        held = datamart_lock_is_held() and not datamart_cleanup_is_running()
    else:
        held = lock_is_held(LOCK_IDS[job]) if job in LOCK_IDS else None
    if held is None or held:
        return running.filter(started_at__gte=now - RUNNING_FOR_AT_MOST).exists()
    closed = running.update(status=SyncRun.Status.FAILED, finished_at=now, error=CUT_OFF)
    logger.warning("%s: closed %s cut-off run(s) left as running by a stopped process", job, closed)
    return False


def start_command(*args: str) -> int:
    """Start ``manage.py <args>`` in the background and return its process id. The start is noted in the
    shared cache (:func:`started_at`): the process needs a few seconds before it writes its run."""
    process = subprocess.Popen(  # noqa: S603 - fixed interpreter and script; arguments come from code
        [sys.executable, str(settings.BASE_DIR / "manage.py"), *args],
        cwd=settings.BASE_DIR,
        stdin=subprocess.DEVNULL,
        start_new_session=True,  # not stopped with the web worker that started it
    )
    logger.info("started manage.py %s in the background (pid %s)", " ".join(args), process.pid)
    _note_started(args[0])
    return process.pid


STARTED_KEY = "background-started:{}"  # the shared cache: when a command was last started from here
STARTED_TTL = 300  # seconds: long enough for any command to write its first row


def _note_started(command: str) -> None:
    from django.core.cache import caches

    from neurodb.core import compute

    try:
        caches[compute.CACHE].set(STARTED_KEY.format(command), time.time(), STARTED_TTL)
    except Exception:  # noqa: BLE001 - the note is a courtesy: the start itself went through
        logger.warning("could not note the start of %s", command, exc_info=True)


def started_at(command: str) -> float | None:
    """When ``command`` was last started in the background (any worker, the scheduler included), as a
    Unix time, if within ``STARTED_TTL`` seconds; None otherwise."""
    from django.core.cache import caches

    from neurodb.core import compute

    try:
        return caches[compute.CACHE].get(STARTED_KEY.format(command))
    except Exception:  # noqa: BLE001 - unknown: treated as not started
        return None


def forget_started(command: str) -> None:
    """Drop the note of :func:`started_at` (its run is there, or it ended)."""
    from django.core.cache import caches

    from neurodb.core import compute

    try:
        caches[compute.CACHE].delete(STARTED_KEY.format(command))
    except Exception:  # noqa: BLE001
        logger.warning("could not forget the start of %s", command, exc_info=True)
