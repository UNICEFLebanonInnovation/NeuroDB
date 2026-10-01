"""``index_documents [--origin library|cpd] [--id N]``: read the library publications and country
programme documents into the knowledge base (new or changed ones; removed ones are dropped)."""

from __future__ import annotations

from django.core.management.base import BaseCommand

from neurodb.knowledge.indexing import process
from neurodb.knowledge.models import Document
from neurodb.knowledge.sources import sync


class Command(BaseCommand):
    help = "Read the library publications and CPD documents into the knowledge base"

    def add_arguments(self, parser):
        parser.add_argument("--origin", choices=["library", "cpd"])
        parser.add_argument("--id", type=int, dest="origin_id")

    def handle(self, *args, **options):
        result = run(options["origin"], options["origin_id"])
        self.stdout.write(f"{result['read']} read, {result['failed']} failed, {result['removed']} removed")


def run(origin: str | None = None, origin_id: int | None = None) -> dict[str, int]:
    changes = sync(origin, origin_id)
    failed = 0
    for document in Document.objects.filter(pk__in=changes["read"]):
        failed += process(document).status == Document.Status.FAILED
    return {"read": len(changes["read"]), "failed": failed, "removed": len(changes["removed"])}
