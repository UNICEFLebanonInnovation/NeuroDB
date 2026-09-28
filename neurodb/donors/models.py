"""Donor accounts: a sign-in that sees one page, the results of its own grants, and nothing else."""

from django.conf import settings
from django.contrib.postgres.fields import ArrayField
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


class DonorAccount(models.Model):
    """Links a user to the eTools donors (and optionally grants) whose results it may see.

    A user with a donor account is confined to the donor page by ``DonorScopeMiddleware``: every
    other page redirects there, every internal API call is refused. The account is never staff."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="donor_account"
    )
    name = models.CharField(_("donor name"), max_length=200, help_text=_("shown as the page title"))
    donors = ArrayField(
        models.CharField(max_length=256),
        help_text=_("the donor names as eTools writes them on funds reservation lines"),
    )
    grants = ArrayField(
        models.CharField(max_length=30),
        default=list,
        blank=True,
        help_text=_("only these grant numbers; empty: every grant of the donors above"),
    )
    show_partner_names = models.BooleanField(
        default=True, help_text=_("off: partners appear as 'Partner 1 (civil society)' and so on")
    )
    contact = models.CharField(
        _("UNICEF contact"), max_length=200, blank=True, help_text=_("the focal point shown on the page")
    )
    active = models.BooleanField(default=True)
    expires_on = models.DateField(
        null=True, blank=True, help_text=_("the account stops working after this day")
    )
    must_change_password = models.BooleanField(
        default=True,
        help_text=_("set when a temporary password is issued; cleared when the donor changes it"),
    )
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.CharField(max_length=150, blank=True, editable=False)
    last_seen_at = models.DateTimeField(null=True, blank=True, editable=False)

    class Meta:
        ordering = ("name",)
        verbose_name = _("donor account")

    def __str__(self):
        return f"{self.name} ({self.user.email or self.user.username})"

    def is_valid_now(self) -> bool:
        return (
            self.active
            and self.user.is_active
            and (self.expires_on is None or self.expires_on >= timezone.localdate())
        )
