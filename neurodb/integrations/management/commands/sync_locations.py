"""``sync_locations [--source datamart|rest]``: the eTools gazetteer (locations, admin levels, parents).

By default from the eTools Datamart, as the nightly Datamart sync does (it carries each location's
admin level, so no separate location types are needed). ``--source rest`` uses the older eTools REST
API (v2 ``sync_locationtype_data`` + ``sync_locations_data``) with ``ETOOLS_TOKEN``."""

from __future__ import annotations

import logging

from django.core.management.base import BaseCommand

from neurodb.core.models import SyncRun
from neurodb.integrations.etools.locations import sync_all_locations
from neurodb.integrations.management.commands._base import add_triggered_by, exit_on_failure, write_summary
from neurodb.integrations.runs import new_run

logger = logging.getLogger(__name__)


def sync_from_datamart(triggered_by: str = "schedule", client=None) -> list[SyncRun]:
    from neurodb.integrations.etools import datamart_sync
    from neurodb.integrations.etools.datamart import DatamartClient

    run = new_run(SyncRun.Job.LOCATIONS, target="locations (Datamart)", triggered_by=triggered_by)
    try:
        datamart_sync.sync_locations(run, client=client or DatamartClient(), links=datamart_sync.Links())
    except Exception:  # recorded FAILED on the run
        logger.error("locations from the Datamart aborted")
    return [run]


class Command(BaseCommand):
    help = "Synchronise the eTools locations (from the Datamart by default)"

    def add_arguments(self, parser):
        add_triggered_by(parser)
        parser.add_argument("--source", choices=["datamart", "rest"], default="datamart")

    def handle(self, *args, **options):
        if options["source"] == "rest":
            runs = sync_all_locations(triggered_by=options["triggered_by"])
        else:
            runs = sync_from_datamart(options["triggered_by"])
        exit_on_failure(write_summary(self, runs))
