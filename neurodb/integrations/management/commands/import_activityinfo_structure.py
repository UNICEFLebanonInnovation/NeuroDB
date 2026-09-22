"""``import_activityinfo_structure [--database AI_ID | --all]`` (v2 ``import_database_structure``)."""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from neurodb.core.models import SyncRun
from neurodb.indicators.models import Database
from neurodb.integrations.activityinfo.structure import import_structure
from neurodb.integrations.management.commands._base import add_triggered_by, exit_on_failure, write_summary
from neurodb.integrations.runs import new_run


class Command(BaseCommand):
    help = (
        "Import ActivityInfo forms and indicators into Activity/IndicatorNew for one or all current databases"
    )

    def add_arguments(self, parser):
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument("--database", type=int, metavar="AI_ID", help="the Database.ai_id to import")
        group.add_argument("--all", action="store_true", help="every database of the current reporting year")
        add_triggered_by(parser)

    def handle(self, *args, **options):
        if options["all"]:
            databases = list(Database.objects.filter(reporting_year__current=True).order_by("ai_id"))
        else:
            databases = list(Database.objects.filter(ai_id=options["database"]))
            if not databases:
                raise CommandError(f"no database with ai_id {options['database']}")
        runs = []
        for database in databases:
            run = new_run(SyncRun.Job.ACTIVITYINFO_STRUCTURE, str(database.ai_id), options["triggered_by"])
            try:
                import_structure(database, run=run)
            except Exception:
                self.stderr.write(f"structure import of {database.ai_id} aborted (see log)")
            runs.append(run)
        exit_on_failure(write_summary(self, runs))
