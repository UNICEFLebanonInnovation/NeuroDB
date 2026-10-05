"""``fmm_refresh [--scores-only | --probe-only] [--triggered-by X]``: the Monitoring insights refresh.

Without an option it rebuilds every visit from the synced eTools field monitoring data: it relinks
the findings to their programme documents, reads which keys the records hold and chooses the key of
each field (admin: Monitoring insights > Fields found), builds the visits and their links, and writes
them in one go. ``--scores-only`` recomputes only what changes with the day or the quality rules, from
the visits already built; ``--probe-only`` reads and chooses the keys, and builds nothing.

One run at a time: a start while one runs does nothing and exits without an error (the running one
serves the requests made meanwhile). It runs by itself after every eTools Datamart sync and each
morning. See :mod:`neurodb.fmm.refresh`."""

from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand

from neurodb.fmm import refresh
from neurodb.integrations.management.commands._base import add_triggered_by, exit_on_failure, write_summary

SWITCHED_OFF = "Monitoring insights is switched off (FMM_ENABLED=false): nothing done."
BUSY = "A Monitoring insights refresh is already running: nothing done."


class Command(BaseCommand):
    help = (
        "Monitoring insights: rebuild the visits from the synced eTools field monitoring data "
        "(--scores-only: recompute the scores; --probe-only: read the keys only, shown as Fields found)"
    )

    def add_arguments(self, parser):
        only = parser.add_mutually_exclusive_group()
        only.add_argument(
            "--scores-only",
            action="store_true",
            help="recompute the action point counts and the scores of the visits already built, without "
            "reading the eTools records",
        )
        only.add_argument(
            "--probe-only",
            action="store_true",
            help="relink the findings to their programme documents, read the keys and choose them; "
            "builds no visit",
        )
        add_triggered_by(parser)

    def handle(self, *args, **options):
        if not settings.FMM_ENABLED:
            self.stdout.write(SWITCHED_OFF)
            return
        done = refresh.run(
            triggered_by=options["triggered_by"],
            scores_only=options["scores_only"],
            probe_only=options["probe_only"],
        )
        if done is None:
            self.stdout.write(BUSY)
            return
        exit_on_failure(write_summary(self, [done]))
