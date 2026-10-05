"""Where Monitoring insights stands: its last refresh and its last reading of the eTools keys."""

from __future__ import annotations

from neurodb.core.models import SyncRun

DONE = (SyncRun.Status.SUCCEEDED, SyncRun.Status.PARTIAL)


def _last(*targets: str) -> SyncRun | None:
    return (
        SyncRun.objects.filter(job=SyncRun.Job.FMM_REFRESH, status__in=DONE, target__in=targets)
        .order_by("-finished_at")
        .first()
    )


def last_refresh() -> SyncRun | None:
    """The last refresh of the visits and their scores that finished (with or without errors)."""
    return _last("full", "scores")


def last_probe() -> SyncRun | None:
    """The last refresh that read the eTools keys (a full one, or the key probe alone)."""
    return _last("full", "probe")


def last_run() -> SyncRun | None:
    """The last refresh of any kind, finished or not (a failed one included)."""
    return SyncRun.objects.filter(job=SyncRun.Job.FMM_REFRESH).order_by("-started_at").first()
