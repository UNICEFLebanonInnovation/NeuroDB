"""``compare_powerbi --upload-id <pk>``: compare an upload of the PRP Power BI exports with NeuroDB's pages
(``neurodb.datamart.powerbi_compare``). Started in the background by Admin → Data and sync → Compare with
Power BI (*Upload and compare*, *Compare again*); never from a shell on the server. Writes the Compare with
Power BI tables only; no import run."""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from neurodb.datamart import powerbi_compare
from neurodb.datamart.models import PowerBIUpload


class Command(BaseCommand):
    help = "Compare an upload of the PRP Power BI exports with NeuroDB (writes its own tables only)"

    def add_arguments(self, parser):
        parser.add_argument("--upload-id", dest="upload_pk", type=int, required=True, help="the upload's id")
        parser.add_argument("--triggered-by", dest="triggered_by", default="command", help="who started it")

    def handle(self, *args, **options):
        limit = powerbi_compare.limit_process()
        try:
            upload = powerbi_compare.run(options["upload_pk"], memory_limit=limit)
        except Exception as exc:  # noqa: BLE001 - the run records its own failure; only the class name here
            raise CommandError(f"Compare with Power BI stopped ({type(exc).__name__})") from None
        if upload is None:
            self.stdout.write("Nothing to do: the upload is not waiting to be compared.")
            return
        if upload.status != PowerBIUpload.Status.DONE:
            raise CommandError(f"Compare with Power BI, upload {upload.pk}: {upload.error or 'see its page'}")
        self.stdout.write(
            self.style.SUCCESS(f"Compare with Power BI, upload {upload.pk}: done ({options['triggered_by']})")
        )
