"""``map_watch_sections [--rematch]``: match the eTools section names to NeuroDB sections by hand.

The morning pass of NeuroDB Watch does the same first thing every day; see
:mod:`neurodb.watch.sections`. New names only, unless ``--rematch``: then the names matched
automatically and not confirmed yet are matched again too (after a section was added, renamed or
deleted). A name an administrator set by hand is never changed. Prints what is left to confirm in
the admin ("eTools section names").
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from neurodb.watch import sections


class Command(BaseCommand):
    help = "Match the eTools section names to NeuroDB sections for NeuroDB Watch (new names only)"

    def add_arguments(self, parser):
        parser.add_argument(
            "--rematch",
            action="store_true",
            help="also match again the names matched automatically and not confirmed (never one set by hand)",
        )

    def handle(self, *args, **options):
        counts = sections.seed(rematch=options["rematch"])
        self.stdout.write(
            self.style.SUCCESS(
                f"eTools section names: {counts['names']} seen, {counts['added']} added "
                f"({counts['confirmed']} confirmed, {counts['to_confirm']} to confirm, "
                f"{counts['unmatched']} without a section), {counts['rematched']} matched again"
            )
        )
        for row in sections.waiting():
            target = f"{row.section} ({row.get_how_display()})" if row.section_id else "no section"
            self.stdout.write(f"  to confirm: {row.etools_name} -> {target}")
        for section in sections.unmatched_sections():
            self.stdout.write(f"  no eTools section name points to: {section}")
