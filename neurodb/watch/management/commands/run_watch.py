"""``run_watch [--daily | --when-requested] [--date YYYY-MM-DD]``: one pass of NeuroDB Watch.

``--daily`` is the morning pass (the scheduled job "watch", 07:45, and "Run NeuroDB Watch now" in the
admin); ``--when-requested`` is the quick pass started shortly after new data arrives. One run at a
time: a second start while one runs does nothing and exits without an error. Every run is recorded
as a ``SyncRun`` (job "watch"); with WATCH_ENABLED off it records only that it was switched off.
"""

from __future__ import annotations

import datetime

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from neurodb.core.models import SyncRun
from neurodb.integrations.management.commands._base import add_triggered_by, exit_on_failure, write_summary
from neurodb.integrations.runs import new_run
from neurodb.watch import lock

DAILY, QUICK = "daily", "quick"
BUSY = "Another run of NeuroDB Watch is running; nothing to do."
SWITCHED_OFF = "NeuroDB Watch is switched off (WATCH_ENABLED); nothing was checked."


class Command(BaseCommand):
    help = "Run NeuroDB Watch: what is due soon and what needs someone, for each person"

    def add_arguments(self, parser):
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument("--daily", action="store_true", help="the morning pass (the default)")
        mode.add_argument(
            "--when-requested",
            action="store_true",
            help="the quick pass, when new data asked for it (started after a hub build or a failed job)",
        )
        parser.add_argument("--date", help="the day the pass reasons from, YYYY-MM-DD (default: today)")
        add_triggered_by(parser)

    def handle(self, *args, **options):
        day = timezone.localdate()
        if options["date"]:
            try:
                day = datetime.date.fromisoformat(options["date"])
            except ValueError as exc:
                raise CommandError(f"--date must be YYYY-MM-DD, got {options['date']!r}") from exc
        mode = QUICK if options["when_requested"] else DAILY
        with lock.hold() as got:
            if not got:
                self.stdout.write(BUSY)
                return
            run = new_run(SyncRun.Job.WATCH, mode, options["triggered_by"])
            if not settings.WATCH_ENABLED:
                run.finish(SyncRun.Status.SUCCEEDED, mode=mode, date=day.isoformat(), note=SWITCHED_OFF)
                self.stdout.write(SWITCHED_OFF)
                return
            # The pass itself (checks, memory, who is told, notes) is added by the runner
            # (neurodb.watch.services); until then a run only records that it ran.
            run.finish(SyncRun.Status.SUCCEEDED, mode=mode, date=day.isoformat())
        exit_on_failure(write_summary(self, [run]))
