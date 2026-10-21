"""Settings of the data sources an administrator may set in the admin, one row each: whether ActivityInfo
is in use or an archive, and the eTools Datamart's PRP business area code."""

from django.conf import settings
from django.core.validators import RegexValidator
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
        # the mode stored: the page's crumbs and title read "ActivityInfo: in use or archive › Archived",
        # not the page's name twice
        return str(self.get_mode_display())

    @classmethod
    def load(cls) -> "ActivityInfoSetting":
        """The one row, made (Automatic) when missing: for the admin page only."""
        return cls.objects.get_or_create(pk=1)[0]


class DatamartSetting(models.Model):
    """The one row (pk=1) of "eTools Datamart: PRP business area code". The PRP datasets of the Datamart
    (``prp_indicator_reports``, ``prp_programme_documents``, ``prp_progress_reports``) carry no country
    name: they are read by the country office's business area code, digits (2490), taken from this row
    first, else from ``ETOOLS_DATAMART_BUSINESS_AREA`` (set by the hosting team) when it is a code, else
    found by NeuroDB: in the Datamart workspace it holds, the Datamart's list of workspaces, or the partner
    report rows (:func:`neurodb.integrations.etools.datamart_sync.resolve_business_area`). A value that is
    not a code (a country's name) is never sent. No row, or an empty code, means NeuroDB finds it. Readers
    use ``filter(pk=1).first()`` and never write."""

    prp_business_area = models.CharField(
        _("PRP business area code"),
        max_length=20,
        blank=True,
        validators=[
            RegexValidator(
                r"^[0-9]+$", _("Digits only, as in 2490: the business area code, not the country's name.")
            )
        ],
        help_text=_(
            "The country office's business area code (digits, as 2490), which the eTools Datamart sync sends "
            "to PRP's indicator reports, programme documents and progress reports (they carry no country "
            "name). A code entered here comes first, before one the hosting team set "
            "(ETOOLS_DATAMART_BUSINESS_AREA). Leave it empty to let NeuroDB find it: in the Datamart "
            "workspace it holds, else in the Datamart's list of workspaces, else on the partner report rows. "
            "It applies from the next sync of those datasets; Run a job → Probe a Datamart dataset shows "
            "first what the Datamart holds for it."
        ),
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("eTools Datamart: PRP business area code")
        verbose_name_plural = _("eTools Datamart: PRP business area code")

    def __str__(self):
        # the code stored: the page's crumbs read "eTools Datamart: PRP business area code › 2490"
        return self.prp_business_area or str(_("Not set"))

    @classmethod
    def load(cls) -> "DatamartSetting":
        """The one row, made (empty) when missing: for the admin page only."""
        return cls.objects.get_or_create(pk=1)[0]
