"""``check_sync_freshness``: exit 1 when a scheduled job has not succeeded within the staleness window.

Used by the scheduler alert. The structure import and the population load are on demand and
are not checked unless named with ``--jobs``.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from neurodb.core.models import SyncRun

logger = logging.getLogger(__name__)

SCHEDULED_JOBS = (SyncRun.Job.ACTIVITYINFO_DATA, SyncRun.Job.ETOOLS, SyncRun.Job.LOCATIONS)


def stale_jobs(jobs, max_age_hours: int) -> list[tuple[str, str]]:
    """(job, reason) for every job without a SUCCEEDED/PARTIAL run newer than ``max_age_hours``."""
    threshold = timezone.now() - timezone.timedelta(hours=max_age_hours)
    stale = []
    for job in jobs:
        last = SyncRun.last_success(job)
        if last is None:
            stale.append((job, "never succeeded"))
        elif last.finished_at < threshold:
            stale.append((job, f"last success {last.finished_at:%Y-%m-%d %H:%M} ({last.target})"))
    return stale


class Command(BaseCommand):
    help = "Fail (exit 1) when the last successful run of a scheduled job is older than SYNC_STALENESS_HOURS"

    def add_arguments(self, parser):
        parser.add_argument("--jobs", help="comma-separated job codes to check (default: ai_data,etools,locations)")
        parser.add_argument("--max-age-hours", type=int, default=None, help="override SYNC_STALENESS_HOURS")

    def handle(self, *args, **options):
        jobs = [j.strip() for j in options["jobs"].split(",") if j.strip()] if options["jobs"] else SCHEDULED_JOBS
        unknown = [j for j in jobs if j not in SyncRun.Job.values]
        if unknown:
            raise CommandError(f"unknown job(s): {', '.join(unknown)}")
        max_age = options["max_age_hours"] or settings.SYNC_STALENESS_HOURS
        stale = stale_jobs(jobs, max_age)
        for job in jobs:
            if job not in dict(stale):
                self.stdout.write(self.style.SUCCESS(f"{job}: fresh"))
        if stale:
            for job, reason in stale:
                logger.error("sync freshness: %s is stale (%s, limit %dh)", job, reason, max_age)
                self.stdout.write(self.style.ERROR(f"{job}: STALE ({reason})"))
            raise CommandError(f"{len(stale)} stale job(s): " + ", ".join(job for job, _ in stale))
        self.stdout.write(self.style.SUCCESS(f"all {len(jobs)} job(s) fresh within {max_age}h"))
