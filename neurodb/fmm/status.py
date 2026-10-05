"""Where Monitoring insights stands: its last refresh, its last reading of the eTools keys, and whether
its scores are being recomputed."""

from __future__ import annotations

from django.db.models import Q

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


def last_build() -> SyncRun | None:
    """The last full refresh that finished: the one that built the visits now shown."""
    return _last("full")


def last_probe() -> SyncRun | None:
    """The last refresh that read the eTools keys (a full one, or the key probe alone)."""
    return _last("full", "probe")


def last_run() -> SyncRun | None:
    """The last refresh of any kind, finished or not (a failed one included)."""
    return SyncRun.objects.filter(job=SyncRun.Job.FMM_REFRESH).order_by("-started_at").first()


def rescore_pending() -> bool:
    """Scores are being recomputed: a visit carries an older rules version than the current one, or a
    refresh someone asked for has not run yet."""
    from .models import RefreshRequest, Visit
    from .refresh import current_rules_version

    if Visit.objects.filter(rules_version__lt=current_rules_version()).exists():
        return True
    return (
        RefreshRequest.objects.filter(pk=1)
        .filter(Q(scores_requested_at__isnull=False) | Q(full_requested_at__isnull=False))
        .exists()
    )
