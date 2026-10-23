from django.conf import settings
from django.urls import reverse
from django.utils.functional import SimpleLazyObject

from neurodb.accounts.roles import can_validate, role_of
from neurodb.assistant import llm


def site(request):
    user = getattr(request, "user", None)
    return {
        "SITE_NAME": settings.SITE_NAME,
        "SSO_ENABLED": settings.SSO_ENABLED,
        # read when a template uses it (one query for the groups): most parts of a page never do
        "user_role": SimpleLazyObject(lambda: role_of(user)) if user is not None else None,
        # whether the person checks the eTools figures (Raw eTools data, the "Raw data behind this page"
        # line): read in the groups query user_role makes, so no query of its own
        "can_validate": SimpleLazyObject(lambda: can_validate(user)) if user is not None else False,
        "PUBLIC_PAGES": settings.PUBLIC_PAGES,
        "SUPPORT_EMAIL": settings.SUPPORT_EMAIL,
        "USER_GUIDE_URL": settings.USER_GUIDE_URL,
        # whether the AI is on, and how to name its provider (AI_PROVIDER.label...): read when a template
        # uses them, from the AI settings kept in memory (no query)
        "AI_ASSISTANT_ENABLED": SimpleLazyObject(llm.enabled),
        "AI_PROVIDER": SimpleLazyObject(llm.names),
        # Monitoring insights (/fmm/): its menu item shows only while the page is on, its "AI" badge only
        # while its AI is on
        "fmm_enabled": getattr(settings, "FMM_ENABLED", False),
        "fmm_ai": bool(getattr(settings, "FMM_ENABLED", False) and getattr(settings, "FMM_AI", False)),
    }


DATABASE_ROUTES = {
    "database_dashboard",
    "database_analytical",
    "database_map",
    "database_snapshot",
    "indicator_detail",
}
REPORT_ROUTES = {"report_dashboard", "report_analytical"}
HPM_ROUTES = {"report_hpm"}
# The ActivityInfo pages: the archive at the bottom of the menu opens on them (it holds their links)
ARCHIVE_ROUTES = frozenset(
    {f"reports:{name}" for name in DATABASE_ROUTES | REPORT_ROUTES | HPM_ROUTES}
    | {
        "reports:databases",
        "reports:database_raw_data",
        "reports:partner_activityinfo",
        "insights:forecasts",
    }
)


def _active_item(request) -> dict:
    """Which sidebar block holds the current page, so it opens even when the user collapsed it; ``archive``:
    the page is an ActivityInfo page, so the ActivityInfo archive opens too."""
    match = getattr(request, "resolver_match", None)
    name = match.url_name if match else None
    pk = match.kwargs.get("pk") if match else None
    archive = bool(match and match.view_name in ARCHIVE_ROUTES)
    if match is not None and match.namespace != "reports":
        name = None  # the blocks' routes are the reports app's
    if name in DATABASE_ROUTES:
        return {"block": "databases", "db": pk, "archive": archive}
    if name in REPORT_ROUTES:
        return {"block": "reports", "report": pk, "archive": archive}
    if name in HPM_ROUTES:
        return {"block": "hpm", "report": pk, "archive": archive}
    return {"block": None, "archive": archive}


def _page_year(request, active: dict):
    """The reporting year the page shows: ``?year=``, else the year of the database or report open,
    else None (the current year)."""
    from neurodb.indicators.models import Database, NeuroReport, ReportingYear

    raw = request.GET.get("year", "").strip()
    if raw:
        found = ReportingYear.objects.filter(name=raw).first()
        if found:
            return found
    if active.get("db"):
        db = Database.objects.select_related("reporting_year").filter(pk=active["db"]).first()
        return db.reporting_year if db else None
    if active.get("report"):
        report = NeuroReport.objects.select_related("ryear").filter(pk=active["report"]).first()
        return report.ryear if report else None
    return None


def _can_set_up(user) -> bool:
    """:func:`neurodb.accounts.roles.can_set_up`, read once per request."""
    from neurodb.accounts.roles import can_set_up
    from neurodb.core import compute

    return compute.per_request(f"can-set-up:{user.pk}", lambda: can_set_up(user))


def _menu(request, ai) -> dict:
    """The menu as this person sees it on this page (:func:`neurodb.indicators.services.navigation.sidebar`:
    the full menu for a superuser, their role's entries for anyone else): ``{"groups", "help", "archive"}``,
    ``help`` and ``archive`` None when the person's menu leaves them out. Every row is marked ``active`` (and
    ``current``: ``aria-current="page"``) on the pages it stands for, sub-pages included. Only the most
    specific link of a page is the current one: on ``/databases/?year=2025`` the 2025 row of the ActivityInfo
    archive, not "ActivityInfo databases", which stays active only."""
    from neurodb.indicators.services import navigation

    match = getattr(request, "resolver_match", None)
    route = match.view_name if match else ""
    drawn = navigation.sidebar(request.user, can_set_up=lambda: _can_set_up(request.user))
    groups, help_item = drawn["groups"], drawn["help"]
    for item in [i for g in groups for i in g["items"]] + ([help_item] if help_item else []):
        item["active"] = item["current"] = route in item["routes"]
    if not drawn["archive"]:  # not in the person's menu: the ActivityInfo state is not read for it
        return {"groups": groups, "help": help_item, "archive": None}
    links = navigation.archive(ai)
    on_databases = route == "reports:databases"
    shown_year = request.GET.get("year", "").strip()
    for row in links["years"]:
        row["current"] = on_databases and shown_year == row["name"]
    archive = {
        "databases": {
            "url": links["databases_url"],
            "active": on_databases,
            "current": on_databases and not any(row["current"] for row in links["years"]),
        },
        "forecasts": (
            {"url": reverse("insights:forecasts"), "active": route == "insights:forecasts"}
            if navigation.sources()["forecasts"]
            else None
        ),
        "years": links["years"],
    }
    return {"groups": groups, "help": help_item, "archive": archive}


def navigation(request):
    """Sidebar content generated from the database (v2 hard-coded 190 lines of HTML), for the year
    the page shows, so choosing 2025 in the year menu lists the 2025 databases and reports.

    ``nav_menu`` is the menu (its rows, conditions and current page), ``ai`` the ActivityInfo state (in use or
    an archive, its years: the archive's label, place and rows), each read only when a template uses it."""
    if not getattr(request, "user", None) or not request.user.is_authenticated:
        return {}
    from neurodb.donors.middleware import donor_account

    if donor_account(request) is not None:  # a donor sees no menu (an error page included)
        return {"is_donor": True}
    from neurodb.indicators.services import activityinfo
    from neurodb.indicators.services.navigation import build_navigation

    active = _active_item(request)
    # Read when a template uses it: the partials HTMX swaps into a page never draw the sidebar
    nav = SimpleLazyObject(lambda: build_navigation(_page_year(request, active)))
    ai = SimpleLazyObject(activityinfo.state)
    return {
        "nav": nav,
        "nav_active": active,
        "ai": ai,
        "nav_menu": SimpleLazyObject(lambda: _menu(request, ai)),
    }
