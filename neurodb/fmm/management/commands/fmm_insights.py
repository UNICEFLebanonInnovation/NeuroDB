"""``fmm_insights [--scopes country,sections,rolling] [--dry-run] [--insight PK] [--triggered-by X]``: the
AI monitoring briefs of Monitoring insights.

Without ``--insight`` it writes the morning briefs of the filters people land on: the whole country this
year, each section that has users (as they land on it), and the whole country over the last 90 days
(at most ``FMM_NIGHTLY_MAX_INSIGHTS``). A brief whose data has not changed is reused at no cost. One run
at a time (its own lock); with the AI switched off it writes nothing and finishes "skipped". It also
closes stopped briefs and applies the retention of briefs and their payloads.

``--insight PK`` writes one brief someone asked for (Regenerate, or an administrator's test run), the
row the page created and polls; it takes no job lock and keeps no run of its own: the brief is its
record. ``--dry-run`` builds the facts of each filter and prints their sizes and estimates, without a
call. See :mod:`neurodb.fmm.ai.insights`."""

from __future__ import annotations

from contextlib import contextmanager

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from neurodb.fmm.ai import budget, insights, profiles
from neurodb.fmm.models import Insight
from neurodb.integrations.management.commands._base import add_triggered_by, exit_on_failure, write_summary

SWITCHED_OFF = "Monitoring insights is switched off (FMM_ENABLED=false): nothing done."
BUSY = "The AI monitoring briefs are already being written: nothing done."


@contextmanager
def _locked():
    """The nightly run's lock, without waiting: False when another run holds it."""
    if connection.vendor != "postgresql":
        yield True
        return
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s)", [insights.LOCK_ID])
        got = bool(cursor.fetchone()[0])
    try:
        yield got
    finally:
        if got:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s)", [insights.LOCK_ID])


class Command(BaseCommand):
    help = (
        "Monitoring insights: write the AI monitoring briefs of the country, each section and the last 90 "
        "days (--insight PK: write one brief someone asked for; --dry-run: build the facts only)"
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--scopes",
            default=",".join(insights.NIGHTLY_KINDS),
            help="which filters: country, sections and/or rolling (comma-separated)",
        )
        parser.add_argument("--dry-run", action="store_true", help="build the facts and print their sizes")
        parser.add_argument("--insight", type=int, help="write the brief of this running row")
        add_triggered_by(parser)

    def handle(self, *args, **options):
        if not settings.FMM_ENABLED:
            self.stdout.write(SWITCHED_OFF)
            return
        if options["insight"]:
            self._one(options["insight"])
            return
        kinds = tuple(k.strip() for k in options["scopes"].split(",") if k.strip())
        unknown = sorted(set(kinds) - set(insights.NIGHTLY_KINDS))
        if unknown:
            raise CommandError(f"unknown scopes: {', '.join(unknown)}")
        if options["dry_run"]:
            self._dry_run(kinds)
            return
        with _locked() as got:
            if not got:
                self.stdout.write(BUSY)
                return
            run = insights.write_nightly(insights.nightly_scopes(kinds=kinds), options["triggered_by"])
        exit_on_failure(write_summary(self, [run]))

    def _one(self, pk: int) -> None:
        row = Insight.objects.select_related("version", "created_by").filter(pk=pk).first()
        if row is None or row.status != Insight.Status.RUNNING:
            self.stdout.write(f"Brief {pk} is not waiting to be written: nothing done.")
            return
        scope = insights.scope_of(row.scope)
        done = insights.generate(
            scope, version=row.version, user=row.created_by, trigger=row.trigger, insight=row
        )
        self.stdout.write(f"Brief {pk}: {done.status}" + (f" ({done.reason})" if done.reason else ""))

    def _dry_run(self, kinds: tuple[str, ...]) -> None:
        from neurodb.fmm.ai.facts import build, dump

        version = profiles.published()
        if version is None:
            self.stdout.write("No prompt version is published: nothing to build.")
            return
        for scope in insights.nightly_scopes(kinds=kinds):
            facts = build(scope, version)
            self.stdout.write(
                f"{scope.label()}: {len(dump(facts.payload)):,} characters, about "
                f"{budget.estimate(facts, version):,} tokens with the output; notes "
                f"{facts.sent['narratives']}/{facts.sent['narratives_allowed']} "
                f"({facts.sent['narratives_withheld']} withheld), visits "
                f"{facts.sent['visits']}/{facts.sent['visits_allowed']}"
            )
