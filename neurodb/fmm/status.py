"""Where Monitoring insights stands: its last refresh, its last reading of the eTools keys, and whether
its scores are being recomputed."""

from __future__ import annotations

import datetime
from dataclasses import dataclass

from django.db.models import Q

from neurodb.core.models import SyncRun

DONE = (SyncRun.Status.SUCCEEDED, SyncRun.Status.PARTIAL)
RECENT_RUNS = 30  # recent runs the page's reference line looks through


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


@dataclass(frozen=True)
class Snapshot:
    """Where Monitoring insights stands, for one page: read in two queries."""

    visits: int  # visits built (0: the page shows its empty state)
    last_refresh: SyncRun | None  # the last refresh of the visits that finished
    last_run: SyncRun | None  # the last refresh of any kind, failed ones included
    fm_synced: datetime.datetime | None  # when the eTools field monitoring rows were last synced
    rules_version: int  # the current rules version
    pending: bool  # rescore_pending()

    @property
    def failed(self) -> SyncRun | None:
        """The last refresh, when it failed after the last one that finished."""
        last = self.last_run
        if last is None or last.status != SyncRun.Status.FAILED:
            return None
        if self.last_refresh is not None and last.started_at <= self.last_refresh.started_at:
            return None
        return last


def snapshot() -> Snapshot:
    """The page's reference line in two queries: the recent refresh and field monitoring sync runs,
    and the counts behind :func:`rescore_pending` (the oldest rules version of the visits, the current
    one and a waiting request)."""
    from django.db import connection

    from .models import RefreshRequest, RuleSetVersion, Visit

    runs = list(
        SyncRun.objects.filter(
            Q(job=SyncRun.Job.FMM_REFRESH)
            | Q(job=SyncRun.Job.ETOOLS_DATAMART, target="field_monitoring", status__in=DONE)
        ).order_by("-started_at")[:RECENT_RUNS]
    )
    refreshes = [r for r in runs if r.job == SyncRun.Job.FMM_REFRESH]
    done = [r for r in refreshes if r.status in DONE and r.target in ("full", "scores") and r.finished_at]
    last_refresh = max(done, key=lambda r: r.finished_at) if done else None
    if last_refresh is None and len(refreshes) == RECENT_RUNS:
        last_refresh = _last("full", "scores")  # an older one, behind many failed or probe runs
    synced = [r.finished_at for r in runs if r.job == SyncRun.Job.ETOOLS_DATAMART and r.finished_at]
    fm_synced = max(synced) if synced else None
    if fm_synced is None and len(runs) == RECENT_RUNS:
        fm_synced = (
            SyncRun.objects.filter(
                job=SyncRun.Job.ETOOLS_DATAMART, target="field_monitoring", status__in=DONE
            )
            .exclude(finished_at=None)
            .order_by("-finished_at")
            .values_list("finished_at", flat=True)
            .first()
        )
    visit, version, request = (m._meta.db_table for m in (Visit, RuleSetVersion, RefreshRequest))
    with connection.cursor() as cursor:
        cursor.execute(
            f"SELECT (SELECT COUNT(*) FROM {visit}), (SELECT MIN(rules_version) FROM {visit}), "  # noqa: S608
            f"(SELECT MAX(number) FROM {version}), "
            f"(SELECT scores_requested_at IS NOT NULL OR full_requested_at IS NOT NULL FROM {request} "
            "WHERE id = 1)"
        )
        visits, oldest, current, requested = cursor.fetchone()
    current = current or 0
    pending = bool(visits and oldest is not None and oldest < current) or bool(requested)
    return Snapshot(
        visits=visits or 0,
        last_refresh=last_refresh,
        last_run=refreshes[0] if refreshes else None,
        fm_synced=fm_synced,
        rules_version=current,
        pending=pending,
    )
