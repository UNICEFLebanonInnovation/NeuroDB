"""``reporting_checks``: run the eTools reporting checks with the job's time and store the result, which
the eTools reporting checks page shows. Started by the ``reporting-checks`` schedule every night, by
*Run the checks now* on the page, or from *Run a job*."""

from __future__ import annotations

from django.core.management.base import BaseCommand

from neurodb.datamart import checks
from neurodb.integrations.management.commands._base import add_triggered_by, exit_on_failure, write_summary


class Command(BaseCommand):
    help = "Run the eTools reporting checks (partner reports, Power BI differences) and store the result"

    def add_arguments(self, parser):
        add_triggered_by(parser)

    def handle(self, *args, **options):
        result = checks.run_job(options["triggered_by"])
        if isinstance(result, str):  # another run of the checks holds the lock
            self.stdout.write(result)
            return
        for check in result.details.get("checks", []):
            self.stdout.write(f"{check['key']:>3} {check['tone']:<8} {check['title']}")
        exit_on_failure(write_summary(self, [result]))
