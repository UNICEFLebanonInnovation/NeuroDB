"""Scheduled jobs: which command runs when, edited on the admin's Scheduled jobs page."""

from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


class ScheduledJob(models.Model):
    key = models.SlugField(max_length=40, unique=True, help_text=_("a short name, e.g. etools-nightly"))
    command = models.CharField(max_length=40, help_text=_("what the job runs"))
    schedule = models.CharField(
        max_length=100,
        help_text=_(
            "When it runs, in Beirut time, as five cron fields: minute hour day-of-month month "
            "day-of-week. Examples: '30 20 * * *' every day at 20:30; '0 18 1-22 * *' at 18:00 on days "
            "1 to 22; '15 * * * *' every hour at :15; '0 4 * * 0' Sundays at 04:00."
        ),
    )
    enabled = models.BooleanField(default=True)
    next_run_at = models.DateTimeField(null=True, blank=True, editable=False)
    last_started_at = models.DateTimeField(null=True, blank=True, editable=False)
    last_outcome = models.CharField(max_length=200, blank=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.CharField(max_length=150, blank=True, editable=False)

    class Meta:
        ordering = ("key",)
        verbose_name = _("scheduled job")

    def __str__(self):
        return self.key

    def plan_next(self, after=None):
        """Set next_run_at to the schedule's first time after ``after`` (default now)."""
        from neurodb.core.cron import next_after

        self.next_run_at = next_after(self.schedule, after or timezone.now()) if self.enabled else None


class SchedulerState(models.Model):
    """One row: the scheduler that holds the lock says it is alive (the page shows it)."""

    last_seen_at = models.DateTimeField(null=True, blank=True)
    host = models.CharField(max_length=200, blank=True)

    def __str__(self):
        return f"scheduler on {self.host or '?'}"
