"""Whether a data source is in use or an archive: one row an administrator may set in the admin."""

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _


class ActivityInfoSetting(models.Model):
    """The one row (pk=1) of "ActivityInfo: in use or archive". No row means Automatic: the row is made
    when an administrator first opens the page (:meth:`load`); readers use ``filter(pk=1).first()`` and
    never write. The rules live in :mod:`neurodb.indicators.services.activityinfo`."""

    class Mode(models.TextChoices):
        AUTO = "auto", _("Automatic (recommended)")
        IN_USE = "in_use", _("In use")
        ARCHIVED = "archived", _("Archived")

    mode = models.CharField(
        _("ActivityInfo is"),
        max_length=10,
        choices=Mode.choices,
        default=Mode.AUTO,
        help_text=_(
            "Automatic: in use while the ActivityInfo import is switched on in Scheduled jobs and this "
            "year has ActivityInfo records from the last three months (in January, last year's December "
            "results count); archived otherwise. Nothing is deleted either way: every ActivityInfo page "
            "of past years stays open. A change shows on every page within a minute."
        ),
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("ActivityInfo: in use or archive")
        verbose_name_plural = _("ActivityInfo: in use or archive")

    def __str__(self):
        return str(_("ActivityInfo: in use or archive"))

    @classmethod
    def load(cls) -> "ActivityInfoSetting":
        """The one row, made (Automatic) when missing: for the admin page only."""
        return cls.objects.get_or_create(pk=1)[0]
