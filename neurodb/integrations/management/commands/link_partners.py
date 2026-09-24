"""``link_partners``: match the ActivityInfo partner names to the eTools partners.

See :mod:`neurodb.partnerships.linking`.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from neurodb.integrations.management.commands._base import add_triggered_by, write_summary
from neurodb.partnerships.linking import link_activityinfo_partners


class Command(BaseCommand):
    help = "Link every ActivityInfo partner name to an eTools partner (same name, then programme document)"

    def add_arguments(self, parser):
        add_triggered_by(parser)

    def handle(self, *args, **options):
        run = link_activityinfo_partners(triggered_by=options["triggered_by"])
        write_summary(self, [run])
        d = run.details
        self.stdout.write(
            f"  by name={d['linked_by_name']} by PD={d['linked_by_pd']} by hand={d['set_by_hand']} "
            f"unlinked={d['unlinked']}"
        )
