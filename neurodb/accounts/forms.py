"""allauth forms: the password reset form refuses to send when NeuroDB cannot send email."""

from allauth.account.forms import ResetPasswordForm as AllauthResetPasswordForm
from django import forms
from django.conf import settings
from django.utils.translation import gettext_lazy as _

NOT_SET_UP = _("Resetting a password by email is not set up in NeuroDB.")


class ResetPasswordForm(AllauthResetPasswordForm):
    """Without ``EMAIL_URL`` Django would send through an SMTP server on this machine, which does not exist:
    the form failed with a server error. The page shows no form then (account/password_reset.html); one sent
    anyway is refused with the page's own words."""

    def clean(self):
        if not settings.PASSWORD_RESET_BY_EMAIL:
            raise forms.ValidationError(NOT_SET_UP, code="email_not_set_up")
        return super().clean()
