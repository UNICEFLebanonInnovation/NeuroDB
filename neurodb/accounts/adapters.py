"""allauth adapters: no self-registration, SSO only links to pre-registered users."""

from allauth.account.adapter import DefaultAccountAdapter
from allauth.core.exceptions import ImmediateHttpResponse
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from django.contrib import messages
from django.shortcuts import redirect

from .models import User


class AccountAdapter(DefaultAccountAdapter):
    def is_open_for_signup(self, request):
        return False


class SocialAccountAdapter(DefaultSocialAccountAdapter):
    def is_open_for_signup(self, request, sociallogin):
        return False

    def pre_social_login(self, request, sociallogin):
        """Link a Microsoft identity to an existing active user by verified email; never create one."""
        if sociallogin.is_existing:
            return
        email = (
            sociallogin.account.extra_data.get("mail")
            or sociallogin.account.extra_data.get("userPrincipalName")
            or ""
        )
        email = email.strip().lower()
        user = User.objects.filter(email__iexact=email, is_active=True).first() if email else None
        if user is None:
            messages.error(
                request,
                "Your Microsoft account is not registered for NeuroDB. Ask an administrator for access.",
            )
            raise ImmediateHttpResponse(redirect("account_login"))
        sociallogin.connect(request, user)
