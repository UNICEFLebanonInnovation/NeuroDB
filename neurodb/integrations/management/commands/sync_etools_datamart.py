"""``sync_etools_datamart [--only partners,interventions,...]``: the eTools Datamart (basic auth).

After the sync (and its lock) is done, the partners are linked to ActivityInfo, and Monitoring insights
rebuilds its visits when a dataset it reads was synced (``FMM_REFRESH_AFTER_SYNC``). That refresh is
its own run: its failure never fails this command."""

from __future__ import annotations

import logging

from django.apps import apps
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from neurodb.integrations import background
from neurodb.integrations.background import DATAMART_LOCK_ID as LOCK_ID
from neurodb.integrations.etools.datamart import DatamartNotConfigured
from neurodb.integrations.etools.datamart_sync import ENTITY_SYNCS, sync_all
from neurodb.integrations.management.commands._base import add_triggered_by, exit_on_failure, write_summary
from neurodb.partnerships.linking import link_activityinfo_partners

logger = logging.getLogger(__name__)

FAILED = "failed"
CORE = ("locations", "partners", "interventions", "intervention_budgets", "agreements")
FMM_NOT_STARTED = (
    "The Monitoring insights refresh could not be started after this sync (see the log); "
    "it runs again at 05:25."
)


class Command(BaseCommand):
    help = (
        "Synchronise partners, programme documents, budgets, funds reservations, grants, PD indicators, "
        "assessments, audits, action points, TPM visits, field monitoring and HACT from the eTools Datamart"
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
        if not self._lock():
            self.stdout.write("Another eTools Datamart sync is running; nothing to do.")
            return
        try:
            runs = sync_all(only=only, triggered_by=options["triggered_by"])
        except (ValueError, DatamartNotConfigured) as exc:
            raise CommandError(str(exc)) from exc
        finally:
            self._unlock()
        if any(run.target in ("partners", "interventions") and run.status != FAILED for run in runs):
            runs.append(link_activityinfo_partners(triggered_by=options["triggered_by"]))
        fmm_run = self._monitoring_insights(runs, options["triggered_by"])
        write_summary(self, runs + ([fmm_run] if fmm_run else []))
        exit_on_failure(runs)  # not the Monitoring insights run: its failure never fails the sync

    def _monitoring_insights(self, runs, triggered_by: str):
        """Rebuild the Monitoring insights visits when a dataset they read was synced (the Datamart lock
        is released by now). Never raises: a failure is its own failed run, and a refresh that cannot
        even start is logged (the 05:25 run catches up)."""
        mode = settings.FMM_REFRESH_AFTER_SYNC  # "inline" (default) | "background" | "off"
        if not (settings.FMM_ENABLED and mode != "off" and apps.is_installed("neurodb.fmm")):
            return None
        try:
            from neurodb.fmm import refresh as fmm_refresh  # lazy: no module-level dependency

            if not fmm_refresh.wanted_after(runs):
                return None
            if mode == "background":
                background.start_command("fmm_refresh", "--triggered-by", triggered_by)
                return None
            return fmm_refresh.run_safely(triggered_by=triggered_by)
        except Exception:  # e.g. the background process could not be started
            logger.exception("The Monitoring insights refresh after the Datamart sync could not start")
            self.stdout.write(self.style.WARNING(FMM_NOT_STARTED))
            return None

    def _lock(self) -> bool:
        if connection.vendor != "postgresql":
            return True
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_try_advisory_lock(%s)", [LOCK_ID])
            return bool(cursor.fetchone()[0])

    def _unlock(self) -> None:
        if connection.vendor == "postgresql":
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s)", [LOCK_ID])
