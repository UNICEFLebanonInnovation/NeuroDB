"""``record_datamart_samples [--limit N] [--only a,b]``: real Datamart records as test fixtures."""

from __future__ import annotations

from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from neurodb.integrations.etools import samples
from neurodb.integrations.etools.datamart import DatamartClient, DatamartNotConfigured
from neurodb.integrations.etools.datamart_sync import ENTITY_SYNCS


class Command(BaseCommand):
    help = (
        "Read a few records of every eTools Datamart dataset, scrub contact details and write them to "
        "tests/fixtures/datamart/ so the sync tests run on real shapes"
    )

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=25, help="records per dataset (default 25)")
        parser.add_argument("--only", help="comma-separated subset of: " + ", ".join(ENTITY_SYNCS))
        parser.add_argument("--out", help=f"folder to write to (default {samples.FIXTURES})")

    def handle(self, *args, **options):
        names = (
            [n.strip() for n in options["only"].split(",") if n.strip()]
            if options["only"]
            else list(ENTITY_SYNCS)
        )
        unknown = [n for n in names if n not in ENTITY_SYNCS]
        if unknown:
            raise CommandError(f"unknown dataset(s): {', '.join(unknown)}")
        try:
            client = DatamartClient()
        except DatamartNotConfigured as exc:
            raise CommandError(str(exc)) from exc
        folder = Path(options["out"]) if options["out"] else samples.FIXTURES
        for name in names:
            try:
                sample = samples.record(client, name, options["limit"])
            except Exception as exc:  # one dataset must not stop the others
                self.stderr.write(self.style.ERROR(f"{name}: {exc.__class__.__name__}: {str(exc)[:200]}"))
                continue
            target = samples.write(sample, folder)
            self.stdout.write(f"{name}: {len(sample['results'])} records -> {target.relative_to(Path.cwd())}")
