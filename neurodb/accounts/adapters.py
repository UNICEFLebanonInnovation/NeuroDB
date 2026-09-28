"""allauth adapters: no self-registration, SSO only links to pre-registered users."""

from allauth.account.adapter import DefaultAccountAdapter
from allauth.core.exceptions import ImmediateHttpResponse
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from django.contrib import messages
from django.shortcuts import redirect
from django.urls import reverse

from .models import User


class AccountAdapter(DefaultAccountAdapter):
    def is_open_for_signup(self, request):
        return False

    def get_login_redirect_url(self, request):
        """A donor account lands on its page (whatever ``next`` says); everyone else as usual."""
        from neurodb.donors.models import DonorAccount

        if DonorAccount.objects.filter(user=request.user).exists():
            return reverse("donors:page")
        return super().get_login_redirect_url(request)

    def get_password_change_redirect_url(self, request):
        from neurodb.donors.models import DonorAccount

        if DonorAccount.objects.filter(user=request.user).exists():
            return reverse("donors:page")
        return super().get_password_change_redirect_url(request)


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
