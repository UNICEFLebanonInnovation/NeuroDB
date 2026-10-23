from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from neurodb.accounts.roles import ALL_ROLES


class MenuChoice(models.Model):
    """The menu entries one role sees, as a superuser ticked them in the administration's Menu page. No row
    for a role: the focused menu (:data:`neurodb.indicators.services.navigation.FOCUSED`); a save that ticks
    exactly that deletes the role's row, so a later change of the focused menu reaches the role. Readers use
    :mod:`neurodb.web.menu_choices`, never this table directly."""

    role = models.CharField(_("role"), max_length=32, unique=True, choices=[(r, _(r)) for r in ALL_ROLES])
    # navigation.keys(), in that order; a name the menu no longer has is ignored when read
    shown = models.JSONField(_("entries shown"), default=list, blank=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("menu")
        verbose_name_plural = _("menu")
        ordering = ["role"]

    def __str__(self):
        return str(self.get_role_display())
