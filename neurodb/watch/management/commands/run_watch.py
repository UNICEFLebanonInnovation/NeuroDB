"""``run_watch [--daily | --when-requested] [--date YYYY-MM-DD]``: one pass of NeuroDB Watch.

``--daily`` is the morning pass (the scheduled job "watch", 07:45, and "Run NeuroDB Watch now" in the
admin); ``--when-requested`` is the quick pass started shortly after new data arrives (it waits
``WATCH_SETTLE_SECONDS`` first, and runs the morning pass instead when that was missed today). One run
at a time: a second start while one runs does nothing and exits without an error. Every pass is
recorded as a ``SyncRun`` (job "watch"); with WATCH_ENABLED off a run records only that it was
switched off. The steps are in :mod:`neurodb.watch.services`.
"""

from __future__ import annotations

import datetime

from django.core.management.base import BaseCommand, CommandError

from neurodb.integrations.management.commands._base import add_triggered_by, exit_on_failure, write_summary
from neurodb.watch import services
from neurodb.watch.services import BUSY, DAILY, QUICK, SWITCHED_OFF

__all__ = ["BUSY", "SWITCHED_OFF", "Command"]


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
        day = None
        if options["date"]:
            try:
                day = datetime.date.fromisoformat(options["date"])
            except ValueError as exc:
                raise CommandError(f"--date must be YYYY-MM-DD, got {options['date']!r}") from exc
        mode = QUICK if options["when_requested"] else DAILY
        done = services.run(mode, options["triggered_by"], today=day)
        if done.note:
            self.stdout.write(done.note)
        if done.runs:
            exit_on_failure(write_summary(self, done.runs))
