"""The education programmes' counts read from Compiler: Makani (MSCC) and Bridging (Dirasa).

Compiler counts the children itself, at night, and NeuroDB keeps what it sends: one document per
programme and year (Makani: the rounds of a year; Bridging: one round), never a row about a child.
"""

from __future__ import annotations

from django.conf import settings
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


# ---------------------------------------------------------------------------------------- Power BI
# Power BI's Makani, Dirasa and Youth dashboards read three views of Compiler's database (vw_mscc_data,
# vw_bridging_data, vw_youth_data_all). Compiler counts the rows of those views and says, in each payload's
# ``power_bi`` block, how it counted and how its count compares with the view's rows (counts only). The sync
# keeps that block in small rows (power_bi.store), so Data health never reads the payloads.


class PowerBiView(models.Model):
    """One view of Compiler's database Power BI reads, or a related view, as Compiler last described it:
    its columns, the relations it reads and its definition (string literals and long number lists masked by
    Compiler) with the hash of the raw text. A view that disappears keeps its last definition."""

    name = models.CharField(max_length=63, unique=True)
    kind = models.CharField(max_length=24, blank=True, help_text=_("view or materialized view"))
    exists = models.BooleanField(default=False)
    can_select = models.BooleanField(
        default=False, help_text=_("Compiler's counting role may read it (GRANT SELECT)")
    )
    columns = models.JSONField(default=list, blank=True, help_text=_("[[name, type], ...]"))
    reads = models.JSONField(default=list, blank=True, help_text=_("[[schema, relation, kind], ...]"))
    definition = models.TextField(blank=True, help_text=_("masked by Compiler: no names, no lists of ids"))
    sha256 = models.CharField(max_length=64, blank=True, help_text=_("of the raw definition"))
    previous_sha256 = models.CharField(max_length=64, blank=True)
    first_seen = models.DateTimeField()
    changed_at = models.DateTimeField(null=True, blank=True, help_text=_("when its definition last changed"))
    checked_at = models.DateTimeField(help_text=_("when Compiler last described it"))

    class Meta:
        ordering = ("name",)
        verbose_name = _("Power BI view")
        verbose_name_plural = _("Power BI views")

    def __str__(self):
        return self.name


class PowerBiCheck(models.Model):
    """How Compiler counted one programme and period, compared with the Power BI view (counts only): taken
    from the payload at sync time."""

    programme = models.CharField(max_length=32, help_text=_("mscc, bridging or youth"))
    period = models.CharField(max_length=64, help_text=_("the year or round, or 'all' (youth)"))
    checked_at = models.DateTimeField(null=True, blank=True, help_text=_("when Compiler compared"))
    counted_at = models.DateTimeField(null=True, blank=True, help_text=_("when Compiler counted"))
    figures_counted_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=_(
            "when the figures kept for this period were counted: later than counted_at when a payload "
            "without a comparison (an earlier Compiler) came after it"
        ),
    )
    view = models.CharField(max_length=63, blank=True)
    mode = models.CharField(max_length=8, blank=True, help_text=_("view: counted from the view; rule: not"))
    reason = models.CharField(max_length=300, blank=True, help_text=_("why the view could not be used"))
    base = models.JSONField(default=dict, blank=True)
    key_check = models.JSONField(default=dict, blank=True)
    reconciliation = models.JSONField(default=dict, blank=True)
    periods = models.JSONField(default=list, blank=True)
    related = models.JSONField(default=dict, blank=True)
    errors = models.JSONField(default=list, blank=True)
    variants = models.JSONField(
        default=dict, blank=True, help_text=_("card -> {definition: value} at the period total")
    )

    class Meta:
        ordering = ("programme", "-period")
        verbose_name = _("Power BI check")
        verbose_name_plural = _("Power BI checks")
        constraints = [models.UniqueConstraint(fields=["programme", "period"], name="power_bi_check_unique")]

    def __str__(self):
        return f"{self.programme} {self.period}: {self.mode or '?'}"


class PowerBiCard(models.Model):
    """An administrator's choice of definition for one card, and the number Power BI shows for it."""

    programme = models.CharField(max_length=32)
    card = models.CharField(max_length=32)
    variant = models.CharField(max_length=32, help_text=_("the definition the pages use for this card"))
    powerbi_value = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text=_(
            "Type the number Power BI shows for this card and period; Data health marks the "
            "definition that gives it."
        ),
    )
    powerbi_period = models.CharField(max_length=64, blank=True)
    noted_at = models.DateTimeField(null=True, blank=True)
    noted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        ordering = ("programme", "card")
        verbose_name = _("Power BI card")
        verbose_name_plural = _("Power BI cards")
        constraints = [models.UniqueConstraint(fields=["programme", "card"], name="power_bi_card_unique")]

    def __str__(self):
        return f"{self.programme} {self.card}: {self.variant}"
