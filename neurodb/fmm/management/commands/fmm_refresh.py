"""``fmm_refresh --probe-only [--triggered-by X]``: read which keys the eTools field monitoring records
hold and choose the key of each field Monitoring insights reads (admin: Monitoring insights > Fields
found). It relinks the findings to their programme documents first. One run at a time: a start while
one runs does nothing and exits without an error. The visits and their scores are not built yet, so
``--probe-only`` is required. See :mod:`neurodb.fmm.refresh`."""

from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from neurodb.fmm import refresh
from neurodb.integrations.management.commands._base import add_triggered_by, exit_on_failure, write_summary

SWITCHED_OFF = "Monitoring insights is switched off (FMM_ENABLED=false): nothing done."
BUSY = "A Monitoring insights refresh is already running: nothing done."
PROBE_ONLY = "Only the key check exists so far: run fmm_refresh --probe-only."


class Command(BaseCommand):
    help = (
        "Monitoring insights: read which keys the eTools field monitoring records hold and choose the key "
        "of each field (--probe-only; shown in the admin as Fields found)"
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--probe-only",
            action="store_true",
            help="relink the findings to their programme documents, read the keys and choose them; "
            "builds no visit",
        )
        add_triggered_by(parser)

    def handle(self, *args, **options):
        if not options["probe_only"]:
            raise CommandError(PROBE_ONLY)
        if not settings.FMM_ENABLED:
            self.stdout.write(SWITCHED_OFF)
            return
        done = refresh.run(triggered_by=options["triggered_by"], probe_only=True)
        if done is None:
            self.stdout.write(BUSY)
            return
        exit_on_failure(write_summary(self, [done]))
