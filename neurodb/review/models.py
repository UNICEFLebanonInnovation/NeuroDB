"""What one day's review found: the review itself (summary, counts, a snapshot of the indicator
statuses to diff the next day against) and its findings, one row each, ranked; and which checks are in
trial (their findings marked Trial, never sent to NeuroDB Watch)."""

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
    decisions = models.JSONField(
        default=list,
        blank=True,
        help_text="what management should decide, chosen and written by the AI from the findings",
    )
    decided_by = models.CharField(
        max_length=100, blank=True, help_text="the model that chose the decisions, or rules"
    )

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
    trial = models.BooleanField(
        default=False,
        help_text="found by a check in trial: never sent to NeuroDB Watch",
    )

    class Meta:
        ordering = ("rank", "id")
        constraints = [
            models.UniqueConstraint(fields=["review", "key"], name="review_finding_key_per_review")
        ]
        verbose_name = _("review finding")
        verbose_name_plural = _("review findings")

    def __str__(self):
        return f"[{self.severity}] {self.title}"


class CheckSetting(models.Model):
    """Whether one check of the daily review is in trial or on. A check in trial marks its findings Trial
    (on the Daily review page and on Home; the summaries read them as any other): NeuroDB Watch imports none
    of them. A check that is on also sends
    its open critical findings and warnings to NeuroDB Watch. A new check starts in trial until an
    administrator has looked at its findings and switched it on (``review.checks.default_mode``); the checks
    that ran before trials existed are on. A change takes effect from the next review: each finding keeps
    whether its check was in trial on its day (``ReviewFinding.trial``)."""

    class Mode(models.TextChoices):
        TRIAL = "trial", _("Trial: not sent to NeuroDB Watch")
        ON = "on", _("On: also sent to NeuroDB Watch")

    check_id = models.CharField("check", max_length=40, unique=True)
    mode = models.CharField(max_length=8, choices=Mode.choices, default=Mode.TRIAL)
    on_since = models.DateField(null=True, blank=True, help_text="the day it was switched on")
    trial_since = models.DateField(null=True, blank=True, help_text="the day it went into trial")
    updated_by = models.CharField(max_length=150, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("check_id",)
        verbose_name = _("daily review check")
        verbose_name_plural = _("daily review checks")

    def __str__(self):
        """The check in words, as the administration names it (its page's title and breadcrumb)."""
        from neurodb.review.checks import LABELS  # lazy: the checks import this module

        return LABELS.get(self.check_id, self.check_id)

    def save(self, *args, **kwargs):
        """Stamp the day it went on or into trial, once (an administrator's change stamps it again)."""
        from django.utils import timezone

        stamp = "on_since" if self.mode == self.Mode.ON else "trial_since"
        if getattr(self, stamp) is None:
            setattr(self, stamp, timezone.localdate())
            if kwargs.get("update_fields") is not None:
                kwargs["update_fields"] = {*kwargs["update_fields"], stamp}
        super().save(*args, **kwargs)


class FindingAssignment(models.Model):
    """Who owns a finding of the daily review, and by when: the assignment workflow behind the
    management brief. Keyed by the finding's stable key, so it follows the finding from one day's
    review to the next. The owner is a role or a team, never a person's name."""

    class Status(models.TextChoices):
        RAISED = "raised", _("Raised")
        ACKNOWLEDGED = "acknowledged", _("Acknowledged")
        ASSIGNED = "assigned", _("Assigned")
        CLOSED = "closed", _("Closed")

    key = models.CharField(max_length=300, unique=True, help_text="the finding's key, stable across days")
    title = models.CharField(max_length=300, blank=True, help_text="the finding's title when assigned")
    section = models.CharField(max_length=128, blank=True)
    owner = models.CharField(
        max_length=120, blank=True, help_text="a role or a team, e.g. Chief of Education, not a person"
    )
    due_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ACKNOWLEDGED)
    note = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    assigned_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.CharField(max_length=150, blank=True)

    class Meta:
        ordering = ("due_date", "id")
        verbose_name = _("finding assignment")
        verbose_name_plural = _("finding assignments")

    def __str__(self):
        return f"{self.title or self.key} → {self.owner or '—'} ({self.status})"

    def save(self, *args, **kwargs):
        """Stamp the step the status reached, once: the lifecycle chart reads these dates."""
        from django.utils import timezone

        now = timezone.now()
        reached = {
            self.Status.ACKNOWLEDGED: ("acknowledged_at",),
            self.Status.ASSIGNED: ("acknowledged_at", "assigned_at"),
            self.Status.CLOSED: ("acknowledged_at", "assigned_at", "closed_at"),
        }
        for name in reached.get(self.status, ()):
            if getattr(self, name) is None:
                setattr(self, name, now)
        if self.status != self.Status.CLOSED:
            self.closed_at = None
        super().save(*args, **kwargs)
