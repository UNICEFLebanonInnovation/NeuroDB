"""Nonce-based Content Security Policy. Templates use {{ request.csp_nonce }} on inline scripts."""

import secrets

from django.conf import settings
from django.contrib.auth.middleware import LoginRequiredMiddleware


class ContentSecurityPolicyMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.csp_nonce = secrets.token_urlsafe(16)
        response = self.get_response(request)
        if "Content-Security-Policy" not in response:
            nonce = f"'nonce-{request.csp_nonce}'"
            policy = (
                "default-src 'self'; "
                f"script-src 'self' {nonce}; "
                "style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data: blob: https://tile.openstreetmap.org https://*.tile.openstreetmap.org; "
                "font-src 'self' data:; "
                "connect-src 'self' https://tile.openstreetmap.org https://*.tile.openstreetmap.org; "
                "worker-src 'self' blob:; "
                "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
            )
            response["Content-Security-Policy"] = policy
        return response


class PublicPagesLoginRequiredMiddleware(LoginRequiredMiddleware):
    """Django's LoginRequiredMiddleware, plus read-only access to the pages in settings.PUBLIC_PAGES."""

    def process_view(self, request, view_func, view_args, view_kwargs):
        match = getattr(request, "resolver_match", None)
        if (
            match is not None
            and request.method in ("GET", "HEAD")
            and match.view_name in settings.PUBLIC_PAGES
        ):
            return None
        return super().process_view(request, view_func, view_args, view_kwargs)
