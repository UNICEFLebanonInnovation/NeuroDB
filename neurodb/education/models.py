"""The education programmes' counts read from Compiler: Makani (MSCC) and Bridging (Dirasa).

Compiler counts the children itself, at night, and NeuroDB keeps what it sends: one document per
programme and year (Makani: the rounds of a year; Bridging: one round), never a row about a child.
"""

from __future__ import annotations

from django.db import models
from django.utils.translation import gettext_lazy as _


class EducationFigures(models.Model):
    programme = models.CharField(max_length=32, help_text=_("Compiler's programme key, e.g. mscc"))
    year = models.CharField(max_length=64, help_text=_("the year (Makani) or round (Bridging)"))
    counted_at = models.DateTimeField(null=True, blank=True, help_text=_("when Compiler counted"))
    fetched_at = models.DateTimeField()
    payload = models.JSONField()

    class Meta:
        ordering = ("programme", "-year")
        verbose_name = _("education figures")
        verbose_name_plural = _("education figures")
        constraints = [models.UniqueConstraint(fields=["programme", "year"], name="education_figures_unique")]

    def __str__(self):
        return f"{self.payload.get('label') or self.programme} {self.year}"
