"""``import_activityinfo_data [--database AI_ID | --current-year]`` (v2 ``import_data_v2``)."""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from neurodb.core.models import SyncRun
from neurodb.indicators.models import Database
from neurodb.integrations.activityinfo.data import import_data
from neurodb.integrations.management.commands._base import add_triggered_by, exit_on_failure, write_summary
from neurodb.integrations.runs import new_run


class Command(BaseCommand):
    help = "Run the ActivityInfo export of each database and replace its ActivityReportNew rows"

    def add_arguments(self, parser):
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument("--database", type=int, metavar="AI_ID", help="the Database.ai_id to import")
        group.add_argument(
            "--current-year", action="store_true", help="every database whose reporting year is current"
        )
        parser.add_argument("--no-copy", action="store_true", help="do not keep a copy of the extract in storage")
        add_triggered_by(parser)

    def handle(self, *args, **options):
        if options["current_year"]:
            databases = list(Database.objects.filter(reporting_year__current=True).order_by("ai_id"))
        else:
            databases = list(Database.objects.filter(ai_id=options["database"]))
            if not databases:
                raise CommandError(f"no database with ai_id {options['database']}")
        runs = []
        for database in databases:
            run = new_run(SyncRun.Job.ACTIVITYINFO_DATA, str(database.ai_id), options["triggered_by"])
            try:
                import_data(database, run=run, keep_copy=not options["no_copy"])
            except Exception:
                self.stderr.write(f"data import of {database.ai_id} aborted (see log)")
            runs.append(run)
        exit_on_failure(write_summary(self, runs))
