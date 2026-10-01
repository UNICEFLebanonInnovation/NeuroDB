"""``index_knowledge --document <id>`` (or ``--all``): read documents into the knowledge base."""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from neurodb.knowledge.indexing import process
from neurodb.knowledge.models import Document


class Command(BaseCommand):
    help = "Read, index, link and summarise knowledge base documents"

    def add_arguments(self, parser):
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument("--document", type=int)
        group.add_argument(
            "--all", action="store_true", help="every document again (e.g. after new partners)"
        )

    def handle(self, *args, **options):
        documents = (
            Document.objects.all() if options["all"] else Document.objects.filter(pk=options["document"])
        )
        if not documents.exists():
            raise CommandError("No such document")
        failed = 0
        for document in documents.iterator():
            process(document)
            failed += document.status == Document.Status.FAILED
            self.stdout.write(f"{document.pk} {document.get_status_display()} {document.error}".strip())
        if failed and not options["all"]:
            raise CommandError(document.error)
