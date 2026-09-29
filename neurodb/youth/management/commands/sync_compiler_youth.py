"""``sync_compiler_youth``: the youth indicator figures from Compiler (counts only)."""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from neurodb.integrations.management.commands._base import add_triggered_by, exit_on_failure, write_summary
from neurodb.youth import compiler
from neurodb.youth.sync import sync


class Command(BaseCommand):
    help = "Read the youth indicator figures from Compiler and suggest links to eTools indicators"

    def add_arguments(self, parser):
        add_triggered_by(parser)

    def handle(self, *args, **options):
        if not compiler.configured():
            raise CommandError("Compiler is not configured: set COMPILER_API_URL and COMPILER_API_TOKEN")
        exit_on_failure(write_summary(self, [sync(triggered_by=options["triggered_by"])]))
