"""``fmm_ai_checks [--limit N] [--triggered-by X]``: the AI checks of Monitoring insights' narrative quality
rules (R3, R5, R6, R7, R8, R32...), newest visits first, within the day's budget; then the scores are
recomputed. What the budget leaves waits for the next run (the back-fill of older visits). One run at a
time (its own lock); with the AI or the AI checks switched off it checks nothing and finishes "skipped".
It runs by itself each morning (Scheduled jobs: fmm-ai-checks) and from Import and sync runs → Run a
job. ``--limit``: the most checks to make (a trial run). See :mod:`neurodb.fmm.ai.checks`."""

from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand

from neurodb.fmm.ai import checks
from neurodb.integrations.management.commands._base import add_triggered_by, exit_on_failure, write_summary

SWITCHED_OFF = "Monitoring insights is switched off (FMM_ENABLED=false): nothing done."
BUSY = "The AI checks are already running: nothing done."


class Command(BaseCommand):
    help = (
        "Monitoring insights: run the AI checks of the narrative quality rules on the visits not checked "
        "yet, newest first, within the day's budget, then recompute the scores"
    )

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, help="the most checks to make (a trial run)")
        add_triggered_by(parser)

    def handle(self, *args, **options):
        if not settings.FMM_ENABLED:
            self.stdout.write(SWITCHED_OFF)
            return
        with checks.locked() as got:
            if not got:
                self.stdout.write(BUSY)
                return
            run = checks.run(options["triggered_by"], limit=options["limit"])
        exit_on_failure(write_summary(self, [run]))
