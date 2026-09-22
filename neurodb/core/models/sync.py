from django.db import models
from django.utils import timezone


class SyncRun(models.Model):
    """One row per execution of a scheduled or on-demand data job (new in v3).

    The admin, the data-health page and the freshness alert all read this table. v2 had no
    record of whether a nightly import ran, and stamped 'last updated' before importing.
    """

    class Job(models.TextChoices):
        ACTIVITYINFO_STRUCTURE = "ai_structure", "ActivityInfo structure import"
        ACTIVITYINFO_DATA = "ai_data", "ActivityInfo data import"
        ETOOLS = "etools", "eTools sync"
        LOCATIONS = "locations", "Locations sync"
        POPULATION = "population", "Population figures load"

    class Status(models.TextChoices):
        RUNNING = "running", "Running"
        SUCCEEDED = "succeeded", "Succeeded"
        PARTIAL = "partial", "Succeeded with errors"
        FAILED = "failed", "Failed"

    job = models.CharField(max_length=32, choices=Job.choices, db_index=True)
    target = models.CharField(max_length=64, blank=True, help_text="e.g. the database id or entity name")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.RUNNING, db_index=True)
    started_at = models.DateTimeField(default=timezone.now, db_index=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    rows_in = models.PositiveIntegerField(default=0)
    rows_written = models.PositiveIntegerField(default=0)
    rows_failed = models.PositiveIntegerField(default=0)
    error = models.TextField(blank=True)
    details = models.JSONField(default=dict, blank=True)
    triggered_by = models.CharField(max_length=150, blank=True, help_text="schedule or the username")

    class Meta:
        ordering = ("-started_at",)
        indexes = [models.Index(fields=["job", "status", "-started_at"])]

    def __str__(self):
        return f"{self.get_job_display()} {self.target} {self.status} {self.started_at:%Y-%m-%d %H:%M}"

    @property
    def duration(self):
        if self.finished_at:
            return self.finished_at - self.started_at
        return None

    def finish(self, status, *, error="", **details):
        self.status = status
        self.error = error[:10000]
        self.details.update(details)
        self.finished_at = timezone.now()
        self.save(
            update_fields=[
                "status",
                "error",
                "details",
                "finished_at",
                "rows_in",
                "rows_written",
                "rows_failed",
            ]
        )

    @classmethod
    def last_success(cls, job, target=""):
        qs = cls.objects.filter(job=job, status__in=[cls.Status.SUCCEEDED, cls.Status.PARTIAL])
        if target:
            qs = qs.filter(target=target)
        return qs.order_by("-finished_at").first()
