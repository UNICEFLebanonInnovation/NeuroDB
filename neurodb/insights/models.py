"""What the machine learning layer keeps: the latest year-end forecast of each master indicator of
the current reporting year (replaced at every run; the back-test is kept in the run's details)."""

from django.db import models

from .forecast import STATUS_LABELS


class IndicatorForecast(models.Model):
    class Status(models.TextChoices):
        ON_COURSE = "on_course", STATUS_LABELS["on_course"]
        UNCERTAIN = "uncertain", STATUS_LABELS["uncertain"]
        SHORT = "likely_short", STATUS_LABELS["likely_short"]
        NO_REPORTS = "no_reports", STATUS_LABELS["no_reports"]
        TOO_EARLY = "too_early", STATUS_LABELS["too_early"]
        NO_TARGET = "no_target", STATUS_LABELS["no_target"]

    master = models.ForeignKey(
        "pivoting.MasterIndicator", on_delete=models.CASCADE, db_constraint=False, related_name="+"
    )
    database = models.ForeignKey(
        "pivoting.Database", on_delete=models.CASCADE, db_constraint=False, related_name="+"
    )
    section_id = models.IntegerField(null=True, blank=True)
    year = models.PositiveSmallIntegerField()
    as_of_month = models.PositiveSmallIntegerField(help_text="the last settled month used (1-12)")
    value_to_date = models.FloatField()
    target = models.FloatField(null=True, blank=True)
    forecast = models.FloatField(null=True, blank=True, help_text="likely year-end value")
    low = models.FloatField(null=True, blank=True)
    high = models.FloatField(null=True, blank=True)
    linear = models.FloatField(null=True, blank=True, help_text="straight-line projection, for comparison")
    status = models.CharField(max_length=16, choices=Status.choices, db_index=True)
    basis = models.CharField(max_length=120, help_text="what the monthly pattern is based on")
    own_years = models.PositiveSmallIntegerField(default=0)
    computed_at = models.DateTimeField()

    class Meta:
        ordering = ("database_id", "master_id")
        constraints = [models.UniqueConstraint(fields=["master"], name="insights_one_forecast_per_master")]
        verbose_name = "indicator forecast"

    def __str__(self):
        return f"{self.master_id} {self.year}: {self.get_status_display()}"

    def pct(self, value: float | None) -> float | None:
        return round(value * 100 / self.target, 1) if value is not None and self.target else None

    @property
    def forecast_pct(self) -> float | None:
        return self.pct(self.forecast)

    @property
    def low_pct(self) -> float | None:
        return self.pct(self.low)

    @property
    def high_pct(self) -> float | None:
        return self.pct(self.high)

    @property
    def to_date_pct(self) -> float | None:
        return self.pct(self.value_to_date)
