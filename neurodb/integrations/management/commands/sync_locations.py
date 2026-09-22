"""``sync_locations`` (v2 ``sync_locationtype_data`` + ``sync_locations_data``)."""

from __future__ import annotations

from django.core.management.base import BaseCommand

from neurodb.integrations.etools.locations import sync_all_locations
from neurodb.integrations.management.commands._base import add_triggered_by, exit_on_failure, write_summary


class Command(BaseCommand):
    help = "Synchronise location types and locations from eTools"

    def add_arguments(self, parser):
        add_triggered_by(parser)

    def handle(self, *args, **options):
        runs = sync_all_locations(triggered_by=options["triggered_by"])
        exit_on_failure(write_summary(self, runs))
