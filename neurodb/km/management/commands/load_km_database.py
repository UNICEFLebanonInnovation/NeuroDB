"""``load_km_database --version-id <pk>``: load an uploaded KM workbook into the KM Database
(``neurodb.km.loading``). Started in the background by Admin → KM Database → Versions → *Upload a new
version*; never from a shell on the server. Writes the KM tables only. (``--version`` is Django's own
option: it prints Django's version.)"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from neurodb.km import loading
from neurodb.km.models import KMVersion


class Command(BaseCommand):
    help = "Load an uploaded KM workbook into the KM Database (writes the KM tables only)"

    def add_arguments(self, parser):
        parser.add_argument(
            "--version-id", dest="version_pk", type=int, required=True, help="the version's id"
        )
        parser.add_argument("--triggered-by", dest="triggered_by", default="command", help="who started it")
        parser.add_argument(
            "--fewer-ok", action="store_true", help="load it even with fewer rows than the version shown now"
        )

    def handle(self, *args, **options):
        version = loading.run(options["version_pk"], fewer_ok=options["fewer_ok"])
        if version is None:
            self.stdout.write("Nothing to do: the version is not waiting to load.")
            return
        if version.status != KMVersion.Status.LIVE:
            raise CommandError(
                f"KM Database version {version.pk} was not loaded: {version.error or 'see its page'}"
            )
        self.stdout.write(
            self.style.SUCCESS(
                f"KM Database version {version.pk} is shown now: {version.documents_count} documents, "
                f"{version.findings_count} findings ({options['triggered_by']})"
            )
        )
