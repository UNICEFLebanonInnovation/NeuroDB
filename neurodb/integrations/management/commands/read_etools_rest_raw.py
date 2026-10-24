"""``read_etools_rest_raw [--only rest_partners,...]``: read the eTools REST API into Raw eTools data, a raw
copy of its records that no page reads (``neurodb.integrations.etools.rest_raw``). Started by "Read the eTools
REST API now" on Raw eTools data, or from *Run a job*; never from a shell on the server."""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from neurodb.integrations.etools import rest_raw
from neurodb.integrations.management.commands._base import add_triggered_by, exit_on_failure, write_summary


class Command(BaseCommand):
    help = "Read the eTools REST API into Raw eTools data (a raw copy of its records; no other table written)"

    def add_arguments(self, parser):
        parser.add_argument(
            "--only",
            help="comma-separated subset, in any order, of: " + ", ".join(rest_raw.READS),
        )
        add_triggered_by(parser)

    def handle(self, *args, **options):
        only = (
            [name.strip() for name in options["only"].split(",") if name.strip()] if options["only"] else None
        )
        try:
            result = rest_raw.read_all(only=only, triggered_by=options["triggered_by"])
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        if isinstance(result, str):  # another read holds the lock
            self.stdout.write(result)
            return
        exit_on_failure(write_summary(self, result))
