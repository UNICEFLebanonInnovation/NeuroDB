"""``sync_etools [--only partners,agreements,...]`` (v2 ``sync_etools_data``). At its end it stores Partner
monitoring's filter choices again (``monitoring.store_options_after``, recorded in the last run's details): a
failure there never fails the sync."""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from neurodb.integrations.etools.sync import ENTITY_SYNCS, sync_all
from neurodb.integrations.management.commands._base import add_triggered_by, exit_on_failure, write_summary


class Command(BaseCommand):
    help = (
        "Synchronise partners, agreements, interventions, travels, engagements and action points from eTools"
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--only",
            help="comma-separated subset, in any order, of: " + ", ".join(ENTITY_SYNCS),
        )
        add_triggered_by(parser)

    def handle(self, *args, **options):
        only = (
            [name.strip() for name in options["only"].split(",") if name.strip()] if options["only"] else None
        )
        try:
            runs = sync_all(only=only, triggered_by=options["triggered_by"])
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        if (
            runs
        ):  # the programme documents and partners Partner monitoring's filter offers; never fails the sync
            from neurodb.datamart import monitoring  # lazy: it reads what this sync writes

            monitoring.store_options_after(runs[-1])
        exit_on_failure(write_summary(self, runs))
