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


ASK_ITEM = (
    "sparkle",
    "lp-tint-amber",
    _lazy("Ask NeuroDB"),
    _lazy("Questions in plain language, answered with links to the figures."),
)
WHATS_NEW = [
    (
        "grid",
        "",
        _lazy("Country overview"),
        _lazy("Children reached, funds, delivery, assurance and the daily review on one page."),
    ),
    (
        "target",
        "lp-tint-green",
        _lazy("Management brief"),
        _lazy("Comparisons, confidence, partners, money and action, read for decisions."),
    ),
    (
        "users",
        "lp-tint-blue",
        _lazy("eTools partner reporting"),
        _lazy("Progress reports on every programme document indicator, tracked against its period."),
    ),
    (
        "clock",
        "lp-tint-amber",
        _lazy("Daily review"),
        _lazy("Fourteen checks every morning; findings get an owner and a due date."),
    ),
    ASK_ITEM,
    (
        "eye",
        "lp-tint-red",
        _lazy("Assurance and funds"),
        _lazy("Monitoring visits, action points, audits, HACT and funds by donor."),
    ),
    (
        "database",
        "",
        _lazy("ActivityInfo databases"),
        _lazy("Every database of the year, its status and its latest import."),
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
        _lazy("Ctrl K finds any page, partner, programme document or indicator."),
    ),
    (
        "pulse",
        "lp-tint-amber",
        _lazy("Data health"),
        _lazy("When each source last synchronised, what failed, and data-quality checks."),
    ),
    (
        "map",
        "lp-tint-blue",
        _lazy("Interactive maps"),
        _lazy("Governorate to site level, filtered in the browser."),
    ),
    (
        "shield",
        "lp-tint-green",
        _lazy("Single sign-on, dark mode, mobile"),
        _lazy("Your UNICEF account, at a desk, at night or on a phone in the field."),
    ),
]

# Public figures, in order of preference; the first six that are not zero are shown. The eTools
# ones are counted as the country overview counts them (programme documents running in the year).
LANDING_STATS = [
    ("running_programmes", _lazy("programme documents running this year")),
    ("etools_partners", _lazy("partners with a programme document this year")),
    ("pd_indicators", _lazy("programme document indicators this year")),
    ("progress_reports", _lazy("partner progress reports this year")),
    ("indicators", _lazy("ActivityInfo results tracked against targets")),
    ("governorates", _lazy("governorates reached")),
    ("records", _lazy("activity records this year")),
    ("partners", _lazy("partners reporting in ActivityInfo")),
    ("sections", _lazy("programme sections")),
]


def _stat_items(stats: dict | None) -> list[tuple[int, str]]:
    if not stats:
        return []
    return [(stats[key], label) for key, label in LANDING_STATS if stats.get(key)][:6]


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
            "title": "eTools",
            "text": _("Programme documents, partner reporting and assurance."),
            "icon": "external",
            "url": "https://etools.unicef.org",
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
    stats = public_highlights() if settings.PUBLIC_LANDING_STATS else None
    last_sync = SyncRun.last_success(SyncRun.Job.ETOOLS_DATAMART)
    context = {
        "stats": stats,
        "stat_items": _stat_items(stats),
        "quick_links": _quick_links(request.user),
        "public_pages": [p.split(":", 1)[1] for p in settings.PUBLIC_PAGES],
        # Ask NeuroDB is only promoted where it is switched on
        "whats_new": [item for item in WHATS_NEW if settings.AI_ASSISTANT_ENABLED or item is not ASK_ITEM],
        "etools_synced": last_sync.finished_at if last_sync else None,
    }
    return render(request, "landing.html", context)
