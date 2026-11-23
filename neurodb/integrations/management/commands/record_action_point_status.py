"""``record_action_point_status``: record today's status of each eTools action point, a row only for a point
that changed since its last record, was first seen, left the Datamart or came back (``datamart.ap_history``).
No text, no names. It also runs by itself after every eTools Datamart sync that refreshed the action points;
started from *Run a job → Record action point status*."""

from __future__ import annotations

from django.core.management.base import BaseCommand

from neurodb.datamart import ap_history
from neurodb.integrations.management.commands._base import add_triggered_by, exit_on_failure, write_summary


class Command(BaseCommand):
    help = "Record each eTools action point's status today (a row only when it changed): no text, no names"

    def add_arguments(self, parser):
        add_triggered_by(parser)

    def handle(self, *args, **options):
        result = ap_history.run(options["triggered_by"])
        if isinstance(result, str):  # another record is going
            self.stdout.write(result)
            return
        exit_on_failure(write_summary(self, [result]))
