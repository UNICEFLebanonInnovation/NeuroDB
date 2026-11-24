"""``store_monitoring_choices``: compute Partner monitoring's filter choices (its sections, partners,
programme documents, places, years, statuses and tags) and store them for the page's frames and its map
(``monitoring.store_options``), which read them and never the PD indicator rows. The eTools jobs that write
what the choices read store them at the end of each run; this stores them at any time: right after a
deployment, or after a change no job made. Started from *Run a job → Store the partner monitoring filter
choices*, or by *Empty the stored page figures*."""

from __future__ import annotations

from django.core.management.base import BaseCommand

from neurodb.datamart import monitoring
from neurodb.integrations.management.commands._base import add_triggered_by


class Command(BaseCommand):
    help = "Store Partner monitoring's filter choices for its frames (they never read the PD indicator rows)"

    def add_arguments(self, parser):
        add_triggered_by(parser)  # accepted as by every job; nothing records a run

    def handle(self, *args, **options):
        found = monitoring.store_options()
        self.stdout.write(
            f"Partner monitoring filter choices stored: {len(found['sections'])} sections, "
            f"{len(found['partners'])} partners, {len(found['pds'])} programme documents, "
            f"{len(found['locations'])} places, {len(found['years'])} years."
        )
