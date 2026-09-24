"""``sync_etools_datamart [--only partners,interventions,...]``: the eTools Datamart (basic auth)."""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from neurodb.integrations.etools.datamart import DatamartNotConfigured
from neurodb.integrations.etools.datamart_sync import ENTITY_SYNCS, sync_all
from neurodb.integrations.management.commands._base import add_triggered_by, exit_on_failure, write_summary

LOCK_ID = 7140428  # one Datamart sync at a time, whoever started it (schedule, admin, shell)
CORE = ("partners", "interventions", "intervention_budgets", "agreements")


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
        exit_on_failure(write_summary(self, runs))

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
