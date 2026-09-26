"""What one day's review found: the review itself (summary, counts, a snapshot of the indicator
statuses to diff the next day against) and its findings, one row each, ranked."""

from django.db import models
from django.utils.translation import gettext_lazy as _


class DailyReview(models.Model):
    """One run of the daily review for one date. Re-running a date replaces its review."""

    class Status(models.TextChoices):
        RUNNING = "running", _("Running")
        SUCCEEDED = "succeeded", _("Succeeded")
        FAILED = "failed", _("Failed")

    TEMPLATE = "template"  # narrated_by when the summary was written without the assistant

    date = models.DateField(unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.RUNNING, db_index=True)
    triggered_by = models.CharField(
        max_length=150, blank=True, help_text="the job, a command or the username"
    )
    summary = models.TextField(blank=True, help_text="the narrative, 4 to 6 sentences")
    narrated_by = models.CharField(
        max_length=100, blank=True, help_text="the model that wrote it, or template"
    )
    checks_run = models.PositiveIntegerField(default=0)
    stats = models.JSONField(default=dict, blank=True)
    duration_ms = models.PositiveIntegerField(null=True, blank=True)
    error = models.TextField(blank=True)
    model_input_tokens = models.PositiveIntegerField(default=0)
    model_output_tokens = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ("-date",)
        verbose_name = _("daily review")
        verbose_name_plural = _("daily reviews")

    def __str__(self):
        return f"Daily review {self.date:%Y-%m-%d} ({self.status})"

    @property
    def duration(self):
        if self.finished_at and self.created_at:
            return self.finished_at - self.created_at
        return None


class ReviewFinding(models.Model):
    """One thing to look at, found by one check. The key is stable from day to day so that the
    next review can tell a new finding from one still open, and notice the ones that went away."""

    class Severity(models.TextChoices):
        CRITICAL = "critical", _("Critical")
        WARNING = "warning", _("Warning")
        INFO = "info", _("To note")
        GOOD = "good", _("Good news")

    class State(models.TextChoices):
        NEW = "new", _("New")
        STILL_OPEN = "still_open", _("Still open")
        RESOLVED = "resolved", _("Resolved")

    review = models.ForeignKey(DailyReview, on_delete=models.CASCADE, related_name="findings")
    key = models.CharField(max_length=300, help_text="stable across days, e.g. reports_overdue:LEB/PD1")
    check_id = models.CharField("check", max_length=40, db_index=True)  # not "check": Model.check()
    severity = models.CharField(max_length=16, choices=Severity.choices, db_index=True)
    section = models.CharField(max_length=128, blank=True, help_text="eTools section name; empty = country")
    title = models.CharField(max_length=300)
    detail = models.TextField(blank=True)
    evidence = models.JSONField(
        default=dict, blank=True, help_text='{"read": ..., "records": [...], "numbers": {...}}'
    )
    children = models.IntegerField(null=True, blank=True, help_text="children behind target, when known")
    url = models.CharField(max_length=500, blank=True)
    state = models.CharField(max_length=16, choices=State.choices, default=State.NEW)
    rank = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ("rank", "id")
        constraints = [
            models.UniqueConstraint(fields=["review", "key"], name="review_finding_key_per_review")
        ]
        verbose_name = _("review finding")
        verbose_name_plural = _("review findings")

    def __str__(self):
        return f"[{self.severity}] {self.title}"
