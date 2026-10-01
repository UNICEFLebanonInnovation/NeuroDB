"""``sync_compiler_wellbeing``: the Makani wellbeing flags and centre summaries from BMA (Compiler)."""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from neurodb.integrations.management.commands._base import add_triggered_by, exit_on_failure, write_summary
from neurodb.wellbeing.sync import sync
from neurodb.youth import compiler


class Command(BaseCommand):
    help = "Read the Makani wellbeing flags and centre summaries from Compiler (children by number only)"

    def add_arguments(self, parser):
        add_triggered_by(parser)
        parser.add_argument("--full", action="store_true", help="read every flag again, not only changes")

    def handle(self, *args, **options):
        if not compiler.configured():
            raise CommandError("Compiler is not configured: set COMPILER_API_URL and COMPILER_API_TOKEN")
        exit_on_failure(
            write_summary(self, [sync(triggered_by=options["triggered_by"], full=options["full"])])
        )
