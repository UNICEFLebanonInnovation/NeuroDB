"""``suggest_master_indicators``: suggest the partners' PD indicators for the eTools master indicators and
propose new ones (:mod:`neurodb.datamart.master_links`; no AI), and record the run with its counts for each
section. Started by the ``etools-masters`` schedule every night or from *Run a job*. Nothing counts until a
person confirms it."""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from neurodb.core import years
from neurodb.datamart import master_links
from neurodb.integrations.management.commands._base import add_triggered_by, exit_on_failure, write_summary


class Command(BaseCommand):
    help = (
        "Suggest PD indicators for the eTools master indicators and propose new ones (nothing counts until "
        "someone confirms it)"
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--year",
            help="the calendar year (default: this year); last year's programme documents are read too",
        )
        add_triggered_by(parser)

    def handle(self, *args, **options):
        year = None
        if options["year"] is not None:
            year = years.parse(options["year"])
            if year is None or str(options["year"]).strip() != str(year):
                raise CommandError("--year: a calendar year from 2000 to next year, such as 2026")
        result = master_links.run(year, options["triggered_by"])
        if isinstance(result, str):  # another run holds the lock
            self.stdout.write(result)
            return
        for section, counts in result.details.get("sections", {}).items():
            found = ", ".join(f"{what.replace('_', ' ')} {n}" for what, n in counts.items() if n)
            self.stdout.write(f"{section}: {found or 'nothing new'}")
        exit_on_failure(write_summary(self, [result]))
