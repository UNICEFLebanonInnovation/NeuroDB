"""Makani wellbeing flags and centre summaries, read from Compiler (BMA), where they are worked out.

A flag says "check on this child" and why. The child is known here only by the BMA registration
number, gender, age band and nationality: no name, contact detail or date of birth; the BMA
profile opens with the user's own BMA access. Follow-ups recorded here are sent to BMA, which
closes the flag.
"""

from django.conf import settings
from django.db import models


class Flag(models.Model):
    OPEN, FOLLOWED_UP, RESOLVED = "open", "followed_up", "resolved"
    STATUSES = ((OPEN, "Open"), (FOLLOWED_UP, "Followed up"), (RESOLVED, "Resolved by itself"))

    bma_id = models.PositiveIntegerField(unique=True)
    registration = models.PositiveIntegerField(db_index=True, help_text="BMA registration number")
    kind = models.CharField(max_length=4)
    kind_label = models.CharField(max_length=120)
    status = models.CharField(max_length=12, choices=STATUSES, db_index=True)
    urgent = models.BooleanField(default=False)
    priority = models.BooleanField(default=False)
    reason = models.TextField()
    evidence = models.JSONField(default=dict, blank=True)
    as_of = models.DateField(null=True, blank=True)
    opened_on = models.DateField()
    last_seen_on = models.DateField(null=True, blank=True)
    resolved_on = models.DateField(null=True, blank=True)
    followed_up_on = models.DateField(null=True, blank=True)
    follow_up_type = models.CharField(max_length=40, blank=True)
    result = models.CharField(max_length=20, blank=True)
    result_label = models.CharField(max_length=120, blank=True)
    note = models.TextField(blank=True)
    followed_up_by_name = models.CharField(max_length=150, blank=True)
    followed_up_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    center_id = models.PositiveIntegerField(null=True, blank=True, db_index=True)
    center_name = models.CharField(max_length=200, blank=True)
    partner_id = models.PositiveIntegerField(null=True, blank=True, db_index=True)
    partner_name = models.CharField(max_length=200, blank=True)
    round_name = models.CharField(max_length=100, blank=True)
    child_gender = models.CharField(max_length=20, blank=True)
    child_age_band = models.CharField(max_length=20, blank=True)
    child_nationality = models.CharField(max_length=100, blank=True)
    bma_path = models.CharField(max_length=200, blank=True)
    bma_modified = models.DateTimeField(null=True, blank=True)
    synced_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-urgent", "-priority", "opened_on", "bma_id")
        indexes = [models.Index(fields=["status", "kind"])]

    def __str__(self):
        return f"{self.registration} {self.kind_label} ({self.get_status_display()})"

    @property
    def days_open(self) -> int:
        from django.utils import timezone

        end = self.followed_up_on or self.resolved_on or timezone.localdate()
        return (end - self.opened_on).days

    @property
    def bma_url(self) -> str:
        base = (settings.COMPILER_API_URL or "").rstrip("/")
        return f"{base}{self.bma_path}" if base and self.bma_path else ""


class CenterSummary(models.Model):
    center_id = models.PositiveIntegerField()
    center_name = models.CharField(max_length=200)
    governorate = models.CharField(max_length=100, blank=True)
    partner_id = models.PositiveIntegerField(null=True, blank=True)
    partner_name = models.CharField(max_length=200, blank=True)
    round_id = models.PositiveIntegerField(null=True, blank=True)
    round_name = models.CharField(max_length=100, blank=True)
    month = models.DateField()
    figures = models.JSONField(default=dict)
    computed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-month", "center_name")
        constraints = [
            models.UniqueConstraint(
                fields=["center_id", "round_id", "month"], name="wellbeing_summary_unique"
            )
        ]

    def __str__(self):
        return f"{self.center_name} {self.month:%Y-%m}"


class SyncState(models.Model):
    """What the last reading of BMA left: the change cursor, the thresholds and the labels."""

    flags_modified_since = models.CharField(max_length=40, blank=True)
    settings = models.JSONField(default=dict, blank=True)
    kinds = models.JSONField(default=dict, blank=True)
    results = models.JSONField(default=dict, blank=True)
    synced_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "wellbeing sync state"

    def __str__(self):
        return f"Read from BMA {self.synced_at:%Y-%m-%d %H:%M}" if self.synced_at else "Never read"

    @classmethod
    def current(cls) -> "SyncState":
        return cls.objects.order_by("pk").first() or cls.objects.create()
