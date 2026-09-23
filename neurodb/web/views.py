from django.conf import settings
from django.contrib.auth.decorators import login_not_required
from django.db import connection
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.templatetags.static import static
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy as _lazy

from neurodb.core.models import SyncRun

from .services import public_highlights


@login_not_required
def favicon(request):
    """Browsers ask for /favicon.ico directly; send them to the hashed static file."""
    return redirect(static("img/favicon.ico"))


@login_not_required
def healthz(request):
    """Readiness probe: database reachable (503 if not) plus the last successful run of each sync job."""
    status = {
        "database": "ok",
        "version": settings.APP_VERSION,
        "syncs": {},
        "time": timezone.now().isoformat(),
    }
    try:
        with connection.cursor() as cur:
            cur.execute("SELECT 1")
        for job, _label in SyncRun.Job.choices:
            last = SyncRun.last_success(job)
            status["syncs"][job] = last.finished_at.isoformat() if last else None
    except Exception as exc:  # any database failure means "not ready", never a 500
        status["database"] = f"error: {exc.__class__.__name__}"
        return JsonResponse(status, status=503)
    return JsonResponse(status)


WHATS_NEW = [
    (
        "grid",
        "",
        _lazy("Programme overview"),
        _lazy("Every database of the year and its tracking mix on one page."),
    ),
    (
        "save",
        "lp-tint-green",
        _lazy("Saved and shared views"),
        _lazy("Keep pivot layouts and share them with your team."),
    ),
    (
        "search",
        "lp-tint-blue",
        _lazy("Search everything"),
        _lazy("Ctrl K finds any database, report or indicator."),
    ),
    (
        "pulse",
        "lp-tint-amber",
        _lazy("Data health"),
        _lazy("When each source last synchronised, and what failed."),
    ),
    (
        "map",
        "lp-tint-blue",
        _lazy("Interactive maps"),
        _lazy("Governorate to site level, filtered in the browser."),
    ),
    (
        "moon",
        "",
        _lazy("Dark mode and mobile"),
        _lazy("Comfortable at a desk, at night, or on a phone in the field."),
    ),
    (
        "shield",
        "lp-tint-green",
        _lazy("Single sign-on"),
        _lazy("Use your UNICEF account; access follows your role."),
    ),
    ("download", "lp-tint-red", _lazy("Exports everywhere"), _lazy("Copy, CSV or Excel from every table.")),
]


def _quick_links(user) -> list[dict]:
    """Helpful destinations; internal pages show a lock and route through sign-in when not public."""
    login = reverse("account_login")

    def page(name: str, title: str, text: str, icon: str) -> dict:
        url = reverse(f"reports:{name}")
        open_ = user.is_authenticated or f"reports:{name}" in settings.PUBLIC_PAGES
        return {
            "title": title,
            "text": text,
            "icon": icon,
            "url": url if open_ else f"{login}?next={url}",
            "locked": not open_,
        }

    links = [
        page(
            "library", _("Library"), _("Studies, evaluations and assessments on children in Lebanon."), "book"
        ),
        page("maps", _("Map products"), _("Ready-made maps of services, schools and water networks."), "map"),
        page(
            "population",
            _("Population figures"),
            _("Estimates by nationality, governorate, district and age."),
            "chart",
        ),
        page(
            "data_health",
            _("Data health"),
            _("When each source was last synchronised, and what failed."),
            "pulse",
        ),
    ]
    if settings.USER_GUIDE_URL:
        links.append(
            {
                "title": _("User guide"),
                "text": _("Step-by-step help for every page."),
                "icon": "info",
                "url": settings.USER_GUIDE_URL,
                "external": True,
            }
        )
    links += [
        {
            "title": "ActivityInfo",
            "text": _("Where partners report their monthly results."),
            "icon": "external",
            "url": "https://www.activityinfo.org",
            "external": True,
        },
        {
            "title": "UNICEF Lebanon",
            "text": _("Programmes, news and publications."),
            "icon": "external",
            "url": "https://www.unicef.org/lebanon",
            "external": True,
        },
    ]
    if settings.SUPPORT_EMAIL:
        links.append(
            {
                "title": _("Request access"),
                "text": _("Ask the NeuroDB team for an account or a new role."),
                "icon": "users",
                "url": f"mailto:{settings.SUPPORT_EMAIL}?subject=NeuroDB%20access",
                "external": True,
            }
        )
    return links


@login_not_required
def landing(request):
    """Public landing page: what NeuroDB is, why it matters, and where to go next."""
    context = {
        "stats": public_highlights() if settings.PUBLIC_LANDING_STATS else None,
        "quick_links": _quick_links(request.user),
        "public_pages": [p.split(":", 1)[1] for p in settings.PUBLIC_PAGES],
        "whats_new": WHATS_NEW,
    }
    return render(request, "landing.html", context)
