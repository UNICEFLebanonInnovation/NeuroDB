"""The few things the reports pages let people enter by hand (the rest is synced)."""

from django.db import models
from django.utils.translation import gettext_lazy as _


class SectionPlan(models.Model):
    """What a section planned for a year, entered in the admin for the management brief: the
    Country Programme target for children reached (the bullet chart's outer mark) and the funds
    required (the appeal figure the funded-against-required chart compares reservations with).

    Neither figure exists in eTools: the CP results framework and the appeal are kept elsewhere.
    """

    year = models.PositiveSmallIntegerField(db_index=True)
    section = models.CharField(max_length=128, help_text="the eTools section name, as on the PD indicators")
    children_target = models.PositiveIntegerField(
        null=True, blank=True, help_text="children to reach in the year (Country Programme target)"
    )
    required_usd = models.DecimalField(
        max_digits=14, decimal_places=2, null=True, blank=True, help_text="funds required in the year (USD)"
    )
    note = models.CharField(max_length=500, blank=True, help_text="the source of the figures")
    updated_by = models.CharField(max_length=150, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-year", "section")
        constraints = [models.UniqueConstraint(fields=["year", "section"], name="section_plan_per_year")]
        verbose_name = _("section plan")
        verbose_name_plural = _("section plans (CP targets and requirements)")

    def __str__(self):
        return f"{self.section} {self.year}"
