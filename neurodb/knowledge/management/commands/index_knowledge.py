"""``index_knowledge --document <id>``, ``--pending`` or ``--all``: read documents into the knowledge
base. ``--pending`` reads every document waiting to be read, editions of periodic reports oldest first
(so that later editions name their figures like the earlier ones); one such run at a time, a second
one waits for the first and then reads whatever is still waiting."""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.db.models import F

from neurodb.knowledge.indexing import process
from neurodb.knowledge.models import Document

PENDING_LOCK_ID = 7_260_002


def next_pending() -> Document | None:
    return (
        Document.objects.filter(status=Document.Status.PENDING)
        .order_by(F("issued_on").asc(nulls_last=True), F("edition").asc(nulls_last=True), "pk")
        .first()
    )


class Command(BaseCommand):
    help = "Read, index, link and summarise knowledge base documents"

    def add_arguments(self, parser):
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument("--document", type=int)
        group.add_argument("--pending", action="store_true", help="every document waiting to be read")
        group.add_argument(
            "--all", action="store_true", help="every document again (e.g. after new partners)"
        )

    def handle(self, *args, **options):
        if options["pending"]:
            return self.pending()
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

    def pending(self) -> None:
        locked = connection.vendor == "postgresql"
        if locked:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_lock(%s)", [PENDING_LOCK_ID])
        try:
            while (document := next_pending()) is not None:
                process(document)
                self.stdout.write(f"{document.pk} {document.get_status_display()} {document.error}".strip())
        finally:
            if locked:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_advisory_unlock(%s)", [PENDING_LOCK_ID])
