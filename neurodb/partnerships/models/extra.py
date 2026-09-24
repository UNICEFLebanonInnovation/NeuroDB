"""Hand-written additions to the partnerships app (the v2 tables are in ``legacy.py``)."""

from django.db import models
from django.utils.translation import gettext_lazy as _

__all__ = ["PartnerLink"]


class PartnerLink(models.Model):
    """An ActivityInfo partner name, as reported in the activity records, linked to an eTools partner.

    ActivityInfo and eTools know nothing of each other: a partner reports in ActivityInfo under the
    name entered there (``ActivityReportNew.partner_label``) and in eTools under its vendor record.
    This table is the bridge, filled by :func:`neurodb.partnerships.linking.link_activityinfo_partners`
    after each import (same name, then the programme document the records name) and corrected by
    hand in the admin, which is never overwritten.
    """

    class Method(models.TextChoices):
        MANUAL = "manual", _("Set by hand")
        NAME = "name", _("Same name")
        PD = "pd", _("Programme document number")
        NONE = "", _("Not linked")

    label = models.CharField(_("ActivityInfo partner"), max_length=250, unique=True)
    partner = models.ForeignKey(
        "etools.PartnerOrganization",
        verbose_name=_("eTools partner"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="activityinfo_links",
    )
    method = models.CharField(_("Linked by"), max_length=10, choices=Method.choices, blank=True, default="")
    records = models.PositiveIntegerField(_("Activity records"), default=0)
    first_month = models.CharField(max_length=7, blank=True, help_text="YYYY-MM of the first record")
    last_month = models.CharField(max_length=7, blank=True, help_text="YYYY-MM of the last record")
    database_ids = models.JSONField(default=list, blank=True, help_text="ActivityInfo databases reported in")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("label",)
        verbose_name = _("ActivityInfo partner link")
        verbose_name_plural = _("ActivityInfo partner links")

    def __str__(self):
        return f"{self.label} → {self.partner or '—'}"
