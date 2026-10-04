"""NeuroDB Watch hears about new data: a quick pass follows about ten minutes later.

Three kinds of event ask for a quick pass (:func:`request`):

- a **knowledge hub build** finished (succeeded, or with errors). The hub is rebuilt after every sync,
  every daily review and every document read, so this follows all of them, once their What's new
  changes exist;
- any **job failed**, except the watch's own (the administrators hear of it);
- a finding's **assignment** was saved (a new owner or agreed date counts within minutes).

The request is stored (``WatchRequest``) and, once the transaction is committed, ``run_watch
--when-requested`` starts in the background, unless a request has been waiting for less than 15
minutes (its process is waiting for this one too) or the day's quick passes are used up (the requests
then wait for the morning pass). The watch's own runs ask for nothing, and the watch writes nothing to
the hub, so its runs never cause a rebuild or another pass.
"""

from __future__ import annotations

import datetime
import logging

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import WatchRequest, WatchState

logger = logging.getLogger(__name__)

STALE = datetime.timedelta(minutes=15)  # a request waiting this long lost its process: start another
ASSIGNMENT_EDITED = "A finding's owner or agreed date was edited"
TRIGGERED_BY = "new data"


def on_run_finished(sender, instance, update_fields=None, **kwargs) -> None:
    """``post_save`` of ``SyncRun``: a finished hub build, or a failed job, asks for a quick pass."""
    from neurodb.core.models import SyncRun

    if not update_fields or "finished_at" not in update_fields:
        return  # not the moment the run finished (SyncRun.finish saves finished_at)
    if instance.job == SyncRun.Job.WATCH:
        return  # its own runs: a pass after a pass would only loop
    if instance.job == SyncRun.Job.KNOWLEDGE_HUB and instance.status in (
        SyncRun.Status.SUCCEEDED,
        SyncRun.Status.PARTIAL,
    ):
        request(instance.get_job_display())
    elif instance.status == SyncRun.Status.FAILED:
        request(f"{instance.get_job_display()} failed")


def on_assignment_saved(sender, instance, **kwargs) -> None:
    """``post_save`` of ``review.FindingAssignment``: its owner, agreed date or status may have changed."""
    request(ASSIGNMENT_EDITED)


def request(reason: str) -> None:
    """Ask for a quick pass once the current transaction is committed (see the module's notes). It never
    fails the save that asked."""
    if not settings.WATCH_ENABLED:
        return
    try:
        with transaction.atomic():  # a failure here leaves the caller's transaction as it was
            oldest = WatchRequest.objects.order_by("pk").values_list("requested_at", flat=True).first()
            WatchRequest.objects.create(reason=str(reason)[:120])
            # a request waiting less than 15 minutes has a process waiting for this one too
            start = (oldest is None or oldest < timezone.now() - STALE) and _quick_passes_left()
    except Exception:
        logger.exception("NeuroDB Watch: the request for a quick pass could not be stored")
        return
    if start:
        transaction.on_commit(_start)


def quick_passes_used(moment: datetime.datetime | None = None) -> int:
    """The quick passes (catch-ups included) made on the Beirut day of ``moment`` (default: now)."""
    day = timezone.localdate(moment or timezone.now())
    state = WatchState.objects.filter(pk=1).first()  # read only: the runner creates the row
    return state.quick_passes_count if state is not None and state.quick_passes_on == day else 0


def _quick_passes_left() -> bool:
    return quick_passes_used() < settings.WATCH_QUICK_PASSES_PER_DAY


def _start() -> None:
    from neurodb.integrations import background

    try:
        background.start_command("run_watch", "--when-requested", "--triggered-by", TRIGGERED_BY)
    except Exception:  # never fail the run or the save that asked
        logger.exception("NeuroDB Watch: could not start the quick pass")
