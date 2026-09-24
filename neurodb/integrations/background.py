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
from django.utils import timezone

from neurodb.core.models import SyncRun

logger = logging.getLogger(__name__)

RUNNING_FOR_AT_MOST = datetime.timedelta(hours=4)  # a run older than this was cut off (restart)


def is_running(job: str) -> bool:
    since = timezone.now() - RUNNING_FOR_AT_MOST
    return SyncRun.objects.filter(job=job, status=SyncRun.Status.RUNNING, started_at__gte=since).exists()


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
