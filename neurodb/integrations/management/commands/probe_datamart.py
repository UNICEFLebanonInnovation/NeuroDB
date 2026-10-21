"""``probe_datamart [--dataset prp_indicator_reports]``: ask the eTools Datamart about one dataset with no
filter, with the business area filter and with the country filter (two or three requests of one small page
each, the sync's own query too for the partner reports, then the window requests for the partner reports or
a dataset of 5,000 records or more), and record what it answered as counts and key names only
(``neurodb.integrations.etools.probe``). Started from *Run a job → Probe a Datamart dataset*; it writes
nothing but its own run."""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from neurodb.integrations.etools import probe
from neurodb.integrations.management.commands._base import add_triggered_by, exit_on_failure, write_summary


class Command(BaseCommand):
    help = (
        "Probe one eTools Datamart dataset: what it answers with no filter, the business area filter and the "
        "country filter (counts and key names only; nothing is written but the run)"
    )

    def add_arguments(self, parser):
        parser.add_argument("--dataset", default=probe.DEFAULT, help="one of: " + ", ".join(probe.datasets()))
        add_triggered_by(parser)

    def handle(self, *args, **options):
        name = options["dataset"]
        if name not in probe.datasets():
            raise CommandError(f"unknown eTools Datamart dataset {name!r}")
        result = probe.run_job(name, options["triggered_by"])
        if isinstance(result, str):  # another probe holds the lock
            self.stdout.write(result)
            return
        if result.details.get("verdict"):
            self.stdout.write(result.details["verdict"])
        exit_on_failure(write_summary(self, [result]))
