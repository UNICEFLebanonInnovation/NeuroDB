"""``review_documents [--pending | --full | --enrich | --locate] [--batch ID] [--document ID]
[--triggered-by X]``: the document review of the knowledge base (findings, key statements and action points
of the documents put in a review batch), within the day's budget; what the budget leaves waits for the next
run. ``--pending`` (the default, nightly: scheduled job doc-review) reads the documents waiting, failed or
partly analysed; ``--full`` every document again; ``--enrich`` the action points again from the stored
findings; ``--locate`` where the findings are again, without AI. ``--batch`` keeps to one batch.
``--document`` analyses one document (its *Analyse* button), waiting for a run in progress; any other run
gives up when one is in progress. Administrators start it from Import and sync runs → Run a job. See
:mod:`neurodb.knowledge.review`."""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from neurodb.integrations.management.commands._base import add_triggered_by, exit_on_failure, write_summary
from neurodb.knowledge import review
from neurodb.knowledge.models import Document

BUSY = "The document review is already running: nothing done."


class Command(BaseCommand):
    help = "Document review: findings, key statements and action points of the documents in a review batch"

    def add_arguments(self, parser):
        modes = parser.add_mutually_exclusive_group()
        modes.add_argument("--pending", action="store_const", dest="mode", const=review.PENDING)
        modes.add_argument("--full", action="store_const", dest="mode", const=review.FULL)
        modes.add_argument("--enrich", action="store_const", dest="mode", const=review.ENRICH)
        modes.add_argument("--locate", action="store_const", dest="mode", const=review.LOCATE)
        parser.add_argument("--batch", type=int, help="only the documents of this batch")
        parser.add_argument("--document", type=int, help="this document only (it must be in a batch)")
        add_triggered_by(parser)

    def handle(self, *args, **options):
        document = options["document"]
        mode = options["mode"] or (review.FULL if document is not None else review.PENDING)
        if document is not None and not review.documents_for(mode, document=document).exists():
            known = Document.objects.filter(pk=document).exists()
            raise CommandError(
                "The document is not in a review batch, is a reference, or has no analysis to start from."
                if known
                else "No such document"
            )
        with review.locked(wait=document is not None) as got:
            if not got:
                self.stdout.write(BUSY)
                return
            run = review.run(
                mode, batch=options["batch"], document=document, triggered_by=options["triggered_by"]
            )
        exit_on_failure(write_summary(self, [run]))
