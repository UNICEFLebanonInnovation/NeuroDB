"""``sync_compiler_education``: the Makani and Bridging counts from Compiler (counts only)."""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from neurodb.education.sync import sync
from neurodb.integrations.management.commands._base import add_triggered_by, exit_on_failure, write_summary
from neurodb.youth import compiler


class Command(BaseCommand):
    help = "Read the education programmes' counts (Makani, Bridging) from Compiler"

    def add_arguments(self, parser):
        add_triggered_by(parser)
        parser.add_argument(
            "--no-calculate", action="store_true", help="only read what BMA has; do not ask it to calculate"
        )

    def handle(self, *args, **options):
        if not compiler.configured():
            raise CommandError("Compiler is not configured: set COMPILER_API_URL and COMPILER_API_TOKEN")
        exit_on_failure(
            write_summary(
                self,
                [sync(triggered_by=options["triggered_by"], calculate_first=not options["no_calculate"])],
            )
        )
