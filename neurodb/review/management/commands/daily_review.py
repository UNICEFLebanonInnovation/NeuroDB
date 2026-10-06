"""``daily_review [--date YYYY-MM-DD] [--no-narration]``: run the day's review and store it.

Scheduled every morning after the night's eTools sync (the ``daily-review`` job); the admin can also
start it from *Daily reviews*. Re-running a date replaces that day's review. Each run first deletes the
Help assistant's questions older than 90 days (``help.assistant.prune``): the nightly clean-up of its
log, which never fails the review.
"""

from __future__ import annotations

import datetime
import logging

from django.core.management.base import BaseCommand, CommandError

from neurodb.integrations.management.commands._base import add_triggered_by
from neurodb.review import services
from neurodb.review.models import DailyReview

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Run the daily review: fourteen checks over the programme data, diffed against the day before"

    def add_arguments(self, parser):
        parser.add_argument("--date", help="the review date, YYYY-MM-DD (default: today)")
        parser.add_argument(
            "--no-narration",
            action="store_true",
            help="write the summary from the template, not the assistant",
        )
        add_triggered_by(parser)

    def handle(self, *args, **options):
        self._prune_help()
        date = None
        if options["date"]:
            try:
                date = datetime.date.fromisoformat(options["date"])
            except ValueError as exc:
                raise CommandError(f"--date must be YYYY-MM-DD, got {options['date']!r}") from exc
        try:
            review = services.run(
                date=date, triggered_by=options["triggered_by"], narrate=not options["no_narration"]
            )
        except services.ReviewBusy:
            self.stdout.write("Another daily review is running; nothing to do.")
            return
        if review.status == DailyReview.Status.FAILED:
            raise CommandError(f"the daily review of {review.date} failed: {review.error[:300]}")
        findings = list(review.findings.all())
        self.stdout.write(
            self.style.SUCCESS(
                f"Daily review {review.date}: {len(findings)} findings from {review.checks_run} checks "
                f"(summary by {review.narrated_by or 'nobody'})"
            )
        )
        for finding in findings:
            self.stdout.write(f"  [{finding.severity}/{finding.state}] {finding.title}")
        for check, error in ((review.stats or {}).get("check_errors") or {}).items():
            self.stdout.write(self.style.WARNING(f"  check {check} failed: {error[:200]}"))
        if review.summary:
            self.stdout.write("")
            self.stdout.write(review.summary)

    def _prune_help(self) -> None:
        """The Help assistant's questions older than 90 days are deleted (never fails the review)."""
        from neurodb.help.assistant import prune

        try:
            deleted = prune()
        except Exception:
            logger.exception("The Help assistant's old questions could not be deleted")
            return
        if deleted:
            self.stdout.write(f"Deleted {deleted} help questions older than 90 days.")
