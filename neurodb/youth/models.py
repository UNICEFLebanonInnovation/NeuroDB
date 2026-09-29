"""The youth figures read from Compiler, and the links between its youth indicators and eTools.

Compiler (the registration platform of the youth partners) counts the young people each youth
indicator reached; NeuroDB keeps the counts it sends, one document per reporting year, and never a
row about a person. A link says which eTools programme document indicator reports on the same youth
indicator, so the dashboard can put what Compiler counted next to what the partner reported.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _


class YouthFigures(models.Model):
    year = models.CharField(max_length=16, unique=True, help_text=_("Compiler's reporting year"))
    fetched_at = models.DateTimeField()
    payload = models.JSONField(help_text=_("counts of young people per grouping, as Compiler sent them"))

    class Meta:
        ordering = ("-year",)
        verbose_name = _("youth figures")
        verbose_name_plural = _("youth figures")

    def __str__(self):
        return f"Youth figures {self.year}"


class YouthIndicatorLink(models.Model):
    class Level(models.TextChoices):
        MASTER = "master", _("Master indicator")
        SUB = "sub", _("Sub indicator")

    class Source(models.TextChoices):
        SUGGESTED = "suggested", _("Suggested")
        CONFIRMED = "confirmed", _("Confirmed")

    year = models.CharField(max_length=16, db_index=True)
    level = models.CharField(max_length=8, choices=Level.choices)
    youth_indicator_id = models.PositiveIntegerField(help_text=_("the indicator's id in Compiler"))
    youth_indicator = models.CharField(max_length=300, help_text=_("number and name, as Compiler sent them"))
    compiler_pd_id = models.PositiveIntegerField(null=True, blank=True)
    compiler_pd_code = models.CharField(_("Compiler project code"), max_length=64, blank=True)
    pd = models.ForeignKey(
        "etools.PCA",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        db_constraint=False,
        related_name="youth_links",
        verbose_name=_("eTools programme document"),
    )
    etools_key = models.CharField(_("eTools indicator key"), max_length=32)
    etools_title = models.CharField(_("eTools indicator"), max_length=512)
    source = models.CharField(max_length=16, choices=Source.choices, default=Source.SUGGESTED)
    score = models.FloatField(null=True, blank=True, help_text=_("how closely the titles match (0-1)"))
    note = models.CharField(max_length=300, blank=True)
    confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("year", "compiler_pd_code", "level", "youth_indicator")
        verbose_name = _("youth indicator link")
        constraints = [
            models.UniqueConstraint(
                fields=["year", "level", "youth_indicator_id", "compiler_pd_id", "pd", "etools_key"],
                name="youth_link_unique",
            )
        ]

    def __str__(self):
        return f"{self.youth_indicator} → {self.etools_title}"
