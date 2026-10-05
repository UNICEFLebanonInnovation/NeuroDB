"""Rebuilding the hub as soon as new data arrives, not only in the morning.

Every finished sync or import (any ``SyncRun`` that succeeded, whatever its source) and every document
read into the knowledge base asks for a rebuild. The request is stored; the first one starts a
background process that waits a minute (a burst of syncs then makes one rebuild, and one process) and
rebuilds while requests remain. One build runs
at a time (a PostgreSQL advisory lock): a process that finds a build running leaves its request to it.
"""

from __future__ import annotations

import datetime
import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager

from django.conf import settings
from django.db import connection, transaction
from django.utils import timezone

from .models import RefreshRequest

logger = logging.getLogger(__name__)

LOCK_ID = 7_260_001  # one build of the hub at a time, across processes and servers
MAX_BUILDS = 5  # per background process: data that keeps arriving is caught by the next request
STALE = datetime.timedelta(minutes=15)  # a request waiting this long lost its process: start another


@contextmanager
def lock(wait: bool = True) -> Iterator[bool]:
    """Hold the hub lock (re-entrant in one process); ``wait=False`` yields False when it is taken."""
    with connection.cursor() as cursor:
        if wait:
            cursor.execute("SELECT pg_advisory_lock(%s)", [LOCK_ID])
            got = True
        else:
            cursor.execute("SELECT pg_try_advisory_lock(%s)", [LOCK_ID])
            got = bool(cursor.fetchone()[0])
    try:
        yield got
    finally:
        if got:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s)", [LOCK_ID])


def request(reason: str) -> None:
    """Ask for a rebuild once the current transaction is committed."""
    if not getattr(settings, "KNOWLEDGE_HUB_ON_NEW_DATA", True):
        return
    oldest = RefreshRequest.objects.order_by("pk").values_list("requested_at", flat=True).first()
    RefreshRequest.objects.create(reason=str(reason)[:120])
    if oldest is None or oldest < timezone.now() - STALE:  # else a process is already waiting for it
        transaction.on_commit(_start)


def _start() -> None:
    from neurodb.integrations import background

    try:
        background.start_command("build_knowledge_hub", "--when-requested")
    except Exception:  # never fail the sync that asked
        logger.exception("could not start the knowledge hub rebuild")


def reasons(up_to: int | None = None) -> list[str]:
    qs = RefreshRequest.objects.all()
    if up_to is not None:
        qs = qs.filter(pk__lte=up_to)
    return sorted(set(qs.values_list("reason", flat=True)))


def latest() -> int | None:
    return RefreshRequest.objects.order_by("-pk").values_list("pk", flat=True).first()


def drain(settle: float | None = None) -> list:
    """Rebuild while requests remain, unless another process is building (it takes them)."""
    from . import build

    settle = getattr(settings, "KNOWLEDGE_HUB_SETTLE_SECONDS", 60) if settle is None else settle
    if settle:
        time.sleep(settle)
    runs = []
    while latest() is not None and len(runs) < MAX_BUILDS:
        with lock(wait=False) as got:
            if not got:
                break
            while latest() is not None and len(runs) < MAX_BUILDS:
                runs.append(build.run(triggered_by="new data", documents=False))
                if runs[-1].status == runs[-1].Status.FAILED:
                    return runs  # the next request or the morning run tries again
    return runs


def on_run_finished(sender, instance, update_fields=None, **kwargs) -> None:
    """``post_save`` of ``SyncRun``: a finished sync brought new data."""
    from neurodb.core.models import SyncRun

    if not update_fields or "finished_at" not in update_fields:
        return
    if instance.job in (
        SyncRun.Job.KNOWLEDGE_HUB,
        SyncRun.Job.WHATS_NEW,
        SyncRun.Job.ML_READINESS,
        SyncRun.Job.FORECAST,
        SyncRun.Job.WATCH,  # it writes nothing to the hub: a rebuild after it would only loop
        SyncRun.Job.FMM_INSIGHTS,  # the AI briefs: nothing the hub reads
    ):
        return  # they bring no new data
    if instance.status not in (SyncRun.Status.SUCCEEDED, SyncRun.Status.PARTIAL):
        return
    request(instance.get_job_display())
