"""``datamart_backfill``: finish an eTools update after the site has started (fill the new columns of
the partner reports already synced, remove the contact details older syncs stored). Started by the
``etools-update-cleanup`` schedule right after a deployment, or from *Run a job*."""

from __future__ import annotations

from django.core.management.base import BaseCommand

from neurodb.datamart import backfill
from neurodb.integrations.management.commands._base import add_triggered_by, exit_on_failure, write_summary

SYNC_RUNNING = "The eTools Datamart sync is running; the clean-up waits for the schedule's next try."


class Command(BaseCommand):
    help = "Finish an eTools update: fill the new partner report columns and remove stored contact details"

    def add_arguments(self, parser):
        add_triggered_by(parser)

    def handle(self, *args, **options):
        result = backfill.run(options["triggered_by"])
        if result is None:
            self.stdout.write(SYNC_RUNNING)
            return
        exit_on_failure(write_summary(self, [result]))
