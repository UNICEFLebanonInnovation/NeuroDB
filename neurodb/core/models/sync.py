from django.db import models
from django.utils import timezone

STOPPED = "Stopped by"  # the error of a run stopped from the admin


class SyncRun(models.Model):
    """One row per execution of a scheduled or on-demand data job (new in v3).

    The admin, the data-health page and the freshness alert all read this table. v2 had no
    record of whether a nightly import ran, and stamped 'last updated' before importing.
    """

    class Job(models.TextChoices):
        ACTIVITYINFO_STRUCTURE = "ai_structure", "ActivityInfo structure import"
        ACTIVITYINFO_DATA = "ai_data", "ActivityInfo data import"
        ETOOLS = "etools", "eTools sync"
        ETOOLS_DATAMART = "etools_datamart", "eTools Datamart sync"
        LOCATIONS = "locations", "Locations sync"
        POPULATION = "population", "Population figures load"
        PARTNER_LINKS = "partner_links", "ActivityInfo partner links"
        DAILY_REVIEW = "daily_review", "Daily AI review"
        COMPILER_YOUTH = "compiler_youth", "Compiler youth figures"
        COMPILER_EDUCATION = "compiler_education", "Compiler education figures"
        COMPILER_WELLBEING = "compiler_wellbeing", "Compiler Makani wellbeing flags"
        KNOWLEDGE_HUB = "knowledge_hub", "Knowledge hub"
        WHATS_NEW = "whats_new", "What's new note"
        ML_READINESS = "ml_readiness", "Machine learning readiness check"
        FORECAST = "forecast", "Year-end indicator forecast"
        WATCH = "watch", "NeuroDB Watch"
        FMM_REFRESH = "fmm_refresh", "Monitoring insights refresh"
        FMM_INSIGHTS = "fmm_insights", "Monitoring insights (AI briefs)"

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
        verbose_name = "import and sync run"  # the admin menu's name for this list
        indexes = [models.Index(fields=["job", "status", "-started_at"])]

    def __str__(self):
        return f"{self.get_job_display()} {self.target} {self.status} {self.started_at:%Y-%m-%d %H:%M}"

    @property
    def duration(self):
        if self.finished_at:
            return self.finished_at - self.started_at
        return None

    def finish(self, status, *, error="", **details):
        """Record how the run ended. A run stopped from the admin while its process kept working
        stays stopped: only its counts and details are added."""
        self.details.update(details)
        stopped = SyncRun.objects.filter(pk=self.pk, error__startswith=STOPPED).values("error", "finished_at")
        if self.pk and (row := stopped.first()):
            self.status, self.error, self.finished_at = self.Status.FAILED, row["error"], row["finished_at"]
            self.save(update_fields=["details", "rows_in", "rows_written", "rows_failed"])
            return
        self.status = status
        self.error = error[:10000]
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

    def stop(self, by: str) -> bool:
        """Mark a running run as stopped (from the admin). A job waiting on another system notices
        and quits (see ``stopped``); any other one finishes its work in the background without
        changing this. False when the run was not running."""
        now = timezone.now()
        done = SyncRun.objects.filter(pk=self.pk, status=self.Status.RUNNING).update(
            status=self.Status.FAILED,
            error=f"{STOPPED} {by} at {timezone.localtime(now):%H:%M}"[:10000],
            finished_at=now,
        )
        self.refresh_from_db()
        return bool(done)

    def stopped(self) -> bool:
        """It was stopped from the admin while running."""
        return SyncRun.objects.filter(pk=self.pk, error__startswith=STOPPED).exists()

    @classmethod
    def last_success(cls, job, target=""):
        qs = cls.objects.filter(job=job, status__in=[cls.Status.SUCCEEDED, cls.Status.PARTIAL])
        if target:
            qs = qs.filter(target=target)
        return qs.order_by("-finished_at").first()
