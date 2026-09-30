"""``propose_cpd_framework --proposal <id>``: read a CPD PDF and store the AI-suggested framework."""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from neurodb.cpd import extraction
from neurodb.cpd.models import FrameworkProposal


class Command(BaseCommand):
    help = "Read a CPD document and propose its results framework (reviewed in the admin before use)"

    def add_arguments(self, parser):
        parser.add_argument("--proposal", type=int, required=True)

    def handle(self, *args, **options):
        proposal = FrameworkProposal.objects.select_related("document").filter(pk=options["proposal"]).first()
        if proposal is None:
            raise CommandError("No such proposal")
        extraction.run(proposal)
        if proposal.status == FrameworkProposal.Status.FAILED:
            raise CommandError(proposal.error)
        self.stdout.write(f"proposal {proposal.pk}: {len(extraction.flatten(proposal.items))} items")
