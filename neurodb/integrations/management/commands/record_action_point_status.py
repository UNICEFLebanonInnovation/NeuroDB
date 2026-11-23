"""``record_action_point_status``: record the status of each eTools action point, a row only for a point that
changed since its last record, was first seen, left the Datamart or came back (``datamart.ap_history``), under
the day the action points were last read from the Datamart (``ap_history.read_day``: a press the morning after
a late sync files them under that night). No text, no names. It also runs by itself after every eTools
Datamart sync that refreshed the action points; started from *Run a job → Record action point status*."""

from __future__ import annotations

from django.core.management.base import BaseCommand

from neurodb.datamart import ap_history
from neurodb.integrations.management.commands._base import add_triggered_by, exit_on_failure, write_summary


class Command(BaseCommand):
    help = (
        "Record each eTools action point's status, under the day it was read (a row only when it changed): "
        "no text, no names"
    )

    def add_arguments(self, parser):
        add_triggered_by(parser)

    def handle(self, *args, **options):
        result = ap_history.run(options["triggered_by"], ap_history.read_day())
        if isinstance(result, str):  # another record is going
            self.stdout.write(result)
            return
        exit_on_failure(write_summary(self, [result]))
