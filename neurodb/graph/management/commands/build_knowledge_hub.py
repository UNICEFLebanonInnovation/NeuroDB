"""``build_knowledge_hub``: read new library and CPD documents, then link every named thing of
NeuroDB across sources (partners, programme documents, donors, places, indicators, CPD results,
Compiler programmes and centres, documents, findings)."""

from __future__ import annotations

from django.core.management.base import BaseCommand

from neurodb.graph.build import run
from neurodb.integrations.management.commands._base import add_triggered_by, exit_on_failure, write_summary


class Command(BaseCommand):
    help = "Rebuild the knowledge hub (and read new library and CPD documents first)"

    def add_arguments(self, parser):
        add_triggered_by(parser)
        parser.add_argument(
            "--no-documents", action="store_true", help="skip reading library and CPD documents"
        )
        parser.add_argument(
            "--when-requested",
            action="store_true",
            help="rebuild only if new data asked for it (started after each sync)",
        )

    def handle(self, *args, **options):
        if options["when_requested"]:
            from neurodb.graph.refresh import drain

            runs = drain()
            if not runs:
                self.stdout.write("Nothing to rebuild (or a build is running and takes the requests)")
                return
            exit_on_failure(write_summary(self, runs))
            return
        exit_on_failure(
            write_summary(self, [run(options["triggered_by"], documents=not options["no_documents"])])
        )
