"""Nonce-based Content Security Policy. Templates use {{ request.csp_nonce }} on inline scripts."""

import secrets

from django.conf import settings
from django.contrib.auth.middleware import LoginRequiredMiddleware
from django.middleware.gzip import GZipMiddleware


class ContentSecurityPolicyMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.csp_nonce = secrets.token_urlsafe(16)
        response = self.get_response(request)
        if "Content-Security-Policy" not in response:
            nonce = f"'nonce-{request.csp_nonce}'"
            # The admin theme (Unfold) runs Alpine.js, which evaluates its x-* expressions. Only
            # staff reach these pages, and every script is still served from this origin.
            admin_eval = " 'unsafe-eval'" if request.path.startswith(f"/{settings.ADMIN_URL_PATH}") else ""
            policy = (
                "default-src 'self'; "
                f"script-src 'self' {nonce}{admin_eval}; "
                "style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data: blob: https://tile.openstreetmap.org https://*.tile.openstreetmap.org; "
                "font-src 'self' data:; "
                "connect-src 'self' https://tile.openstreetmap.org https://*.tile.openstreetmap.org; "
                "worker-src 'self' blob:; "
                f"frame-src 'self'{getattr(request, 'csp_frame_src', '')}; "
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


class HealthCheckMiddleware:
    """Answer the container probes before host validation, HTTPS redirects and sessions.

    Azure Container Apps and App Service probe the container over plain HTTP with an internal IP as
    the Host header. Behind ALLOWED_HOSTS and SECURE_SSL_REDIRECT those probes would get 400 or 301
    and the platform would restart a healthy container. Only these read-only paths are handled:
    the two health checks, and the path App Service requests once when a container starts (its
    warm-up probe, sent to the container's internal 169.254.x.x address, which is not an allowed
    host; any answer tells App Service the container is up).
    """

    LIVE = "/healthz/live/"
    READY = "/healthz/"
    APP_SERVICE_WARMUP = "/robots933456.txt"

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.method in ("GET", "HEAD"):
            if request.path == self.LIVE:
                from django.http import JsonResponse

                return JsonResponse({"status": "ok", "version": settings.APP_VERSION})
            if request.path == self.READY:
                from neurodb.web.views import healthz

                return healthz(request)
            if request.path == self.APP_SERVICE_WARMUP:
                from django.http import HttpResponse

                return HttpResponse(b"", content_type="text/plain")
        return self.get_response(request)


class CompressMiddleware(GZipMiddleware):
    """Gzip for pages, parts and JSON: App Service does not compress what a Linux container sends, and a
    page's parts are large (the daily review's folded findings alone are about 266 KB).

    Django's GZipMiddleware, which pads each response with random bytes against BREACH, minus three
    kinds of response: event streams (the assistant and help chats send each line as it is written, and
    compression would hold them back), attachments (exports: Excel files are compressed already) and
    streamed responses (large downloads). It sits right after WhiteNoise, so static files, which
    WhiteNoise serves from their own .gz and .br copies, never reach it.
    """

    def process_response(self, request, response):
        if response.streaming:
            return response
        if response.get("Content-Type", "").startswith("text/event-stream"):
            return response
        if response.get("Content-Disposition", "").split(";")[0].strip().lower() == "attachment":
            return response
        return super().process_response(request, response)
