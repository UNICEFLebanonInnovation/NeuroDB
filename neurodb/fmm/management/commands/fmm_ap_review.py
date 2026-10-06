"""``fmm_ap_review [--limit N] [--triggered-by X]``: the AI review of completed eTools action points (does
the action taken resolve the issue raised?), most recently completed first, within the day's budget. What
the budget leaves waits for the next run. One run at a time (its own lock); with the AI or the review
switched off it reviews nothing and finishes "skipped". It runs by itself each morning (Scheduled jobs:
fmm-ap-review), from Import and sync runs → Run a job, and from the action points page's "Run AI review"
(``--limit``: its batch size). See :mod:`neurodb.fmm.ai.ap_review`."""

from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand

from neurodb.fmm.ai import ap_review
from neurodb.integrations.management.commands._base import add_triggered_by, exit_on_failure, write_summary

SWITCHED_OFF = "Monitoring insights is switched off (FMM_ENABLED=false): nothing done."
BUSY = "The AI review of action points is already running: nothing done."


class Command(BaseCommand):
    help = (
        "Action points: the AI review of the completed action points not reviewed yet, most recently "
        "completed first, within the day's budget"
    )

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, help="the most reviews to make (the page's batch size)")
        add_triggered_by(parser)

    def handle(self, *args, **options):
        if not settings.FMM_ENABLED:
            self.stdout.write(SWITCHED_OFF)
            return
        with ap_review.locked() as got:
            if not got:
                self.stdout.write(BUSY)
                return
            run = ap_review.run(options["triggered_by"], limit=options["limit"])
        exit_on_failure(write_summary(self, [run]))
