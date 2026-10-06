"""Keeps a donor account on the donor page.

A user with a ``DonorAccount`` reaches the donor page, the password change page and sign out, and
nothing else: any other page (or an address that matches none) redirects to the donor page, and the
internal API, the assistants (Ask NeuroDB, Chat with Data, the Help assistant) and HTMX requests are
refused with 403 (a redirect would hand them a page they did not ask for). An account that is switched
off or past its end date is signed out. Until the donor replaces the temporary password, every page
leads to the password change page. The Power BI feed, read with a key and never with a session, is left
out.
"""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth import logout
from django.http import JsonResponse
from django.shortcuts import redirect
from django.utils import timezone
from django.utils.translation import gettext as _

from .models import DonorAccount

ALLOWED = {"donors:page", "account_logout", "account_change_password", "account_reauthenticate", "healthz"}
PASSWORD_PAGES = {"account_change_password", "account_logout", "account_reauthenticate"}
SEEN_EVERY_SECONDS = 300


def donor_account(request) -> DonorAccount | None:
    """The request's donor account, read once per request (None for staff and anonymous users)."""
    if not hasattr(request, "_donor_account"):
        user = getattr(request, "user", None)
        account = None
        if user is not None and user.is_authenticated:
            account = DonorAccount.objects.filter(user=user).first()
        request._donor_account = account
    return request._donor_account


def _key_feed(view_func) -> bool:
    """A feed read with a key of its own (the Power BI feed, ``fmm.powerbi.key_only``): no session, so no
    donor account, applies to it."""
    return bool(getattr(view_func, "powerbi_feed", False))


class DonorScopeMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if _key_feed(getattr(getattr(request, "resolver_match", None), "func", None)):
            return response
        # An address that matches no page (a stale link, a ``next`` from before sign-in) leads a
        # donor back to the donor page rather than to a "not found" page.
        if response.status_code == 404 and request.method == "GET" and not self._is_api(request):
            account = donor_account(request)
            if account is not None and account.is_valid_now() and not account.must_change_password:
                return redirect("donors:page")
        return response

    def process_view(self, request, view_func, view_args, view_kwargs):
        if _key_feed(view_func):  # read with its own key, never with a session
            return None
        account = donor_account(request)
        if account is None:
            return None
        match = getattr(request, "resolver_match", None)
        name = match.view_name if match else ""
        if not account.is_valid_now():
            logout(request)
            messages.error(request, _("This donor access has ended. Contact your UNICEF focal point."))
            return redirect("account_login")
        self._seen(account)
        if account.must_change_password and name not in PASSWORD_PAGES:
            if self._is_api(request):
                return self._refused()
            messages.info(request, _("Choose your own password to continue."))
            return redirect("account_change_password")
        if name in ALLOWED:
            return None
        if self._is_api(request):
            return self._refused()
        return redirect("donors:page")

    @staticmethod
    def _is_api(request) -> bool:
        return (
            request.path.startswith(("/api/", "/ask/", "/fmm/chat/", "/help/stream/"))
            or request.headers.get("HX-Request") == "true"
            or "application/json" in request.headers.get("Accept", "")
        )

    @staticmethod
    def _refused():
        return JsonResponse({"detail": "Not available for donor accounts."}, status=403)

    @staticmethod
    def _seen(account: DonorAccount) -> None:
        now = timezone.now()
        if account.last_seen_at is None or (now - account.last_seen_at).total_seconds() > SEEN_EVERY_SECONDS:
            DonorAccount.objects.filter(pk=account.pk).update(last_seen_at=now)
            account.last_seen_at = now
