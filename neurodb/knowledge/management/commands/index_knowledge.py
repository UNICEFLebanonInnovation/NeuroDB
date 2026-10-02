"""``index_knowledge --document <id>``, ``--pending`` or ``--all``: read documents into the knowledge
base. ``--pending`` reads every document waiting to be read, editions of periodic reports oldest first
(so that later editions name their figures like the earlier ones); one such run at a time, a second
one waits for the first and then reads whatever is still waiting. ``--pending --stale`` reads only
those left behind (waiting for over STALE_MINUTES, or cut off while being read by a restart): the
morning knowledge hub job starts it, so nothing stays unread."""

from __future__ import annotations

import datetime

from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.db.models import F
from django.utils import timezone

from neurodb.knowledge.indexing import process
from neurodb.knowledge.models import Document

PENDING_LOCK_ID = 7_260_002
STALE_MINUTES = 60  # waiting longer than this (or being read for longer than CUT_OFF_HOURS): left behind
CUT_OFF_HOURS = 3


def left_behind() -> int:
    """Mark the documents cut off while being read (a restart) as waiting again; the number of
    documents now waiting for longer than STALE_MINUTES."""
    now = timezone.now()
    Document.objects.filter(
        status=Document.Status.INDEXING, updated_at__lt=now - datetime.timedelta(hours=CUT_OFF_HOURS)
    ).update(status=Document.Status.PENDING, updated_at=now - datetime.timedelta(minutes=STALE_MINUTES + 1))
    return Document.objects.filter(
        status=Document.Status.PENDING, updated_at__lt=now - datetime.timedelta(minutes=STALE_MINUTES)
    ).count()


def next_pending(stale: bool = False) -> Document | None:
    waiting = Document.objects.filter(status=Document.Status.PENDING)
    if stale:
        waiting = waiting.filter(updated_at__lt=timezone.now() - datetime.timedelta(minutes=STALE_MINUTES))
    return waiting.order_by(
        F("issued_on").asc(nulls_last=True), F("edition").asc(nulls_last=True), "pk"
    ).first()


class Command(BaseCommand):
    help = "Read, index, link and summarise knowledge base documents"

    def add_arguments(self, parser):
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument("--document", type=int)
        group.add_argument("--pending", action="store_true", help="every document waiting to be read")
        parser.add_argument(
            "--stale", action="store_true", help="with --pending: only those left behind (see left_behind)"
        )
        group.add_argument(
            "--all", action="store_true", help="every document again (e.g. after new partners)"
        )

    def handle(self, *args, **options):
        if options["pending"]:
            return self.pending(stale=options["stale"])
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

    def pending(self, stale: bool = False) -> None:
        locked = connection.vendor == "postgresql"
        if locked:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_lock(%s)", [PENDING_LOCK_ID])
        try:
            if stale:
                left_behind()
            while (document := next_pending(stale)) is not None:
                process(document)
                self.stdout.write(f"{document.pk} {document.get_status_display()} {document.error}".strip())
        finally:
            if locked:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_advisory_unlock(%s)", [PENDING_LOCK_ID])
