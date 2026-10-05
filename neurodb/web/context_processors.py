from django.conf import settings

from neurodb.accounts.roles import role_of


def site(request):
    return {
        "SITE_NAME": settings.SITE_NAME,
        "SSO_ENABLED": settings.SSO_ENABLED,
        "user_role": role_of(request.user) if hasattr(request, "user") else None,
        "PUBLIC_PAGES": settings.PUBLIC_PAGES,
        "SUPPORT_EMAIL": settings.SUPPORT_EMAIL,
        "USER_GUIDE_URL": settings.USER_GUIDE_URL,
        "AI_ASSISTANT_ENABLED": settings.AI_ASSISTANT_ENABLED,
        # Monitoring insights (/fmm/): its menu item shows only while the page is on
        "fmm_enabled": getattr(settings, "FMM_ENABLED", False),
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


def _active_item(request) -> dict:
    """Which sidebar block holds the current page, so it opens even when the user collapsed it."""
    match = getattr(request, "resolver_match", None)
    name = match.url_name if match else None
    pk = match.kwargs.get("pk") if match else None
    if name in DATABASE_ROUTES:
        return {"block": "databases", "db": pk}
    if name in REPORT_ROUTES:
        return {"block": "reports", "report": pk}
    if name in HPM_ROUTES:
        return {"block": "hpm", "report": pk}
    return {"block": None}


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


def navigation(request):
    """Sidebar content generated from the database (v2 hard-coded 190 lines of HTML), for the year
    the page shows, so choosing 2025 in the year menu lists the 2025 databases and reports."""
    if not getattr(request, "user", None) or not request.user.is_authenticated:
        return {}
    from neurodb.donors.middleware import donor_account

    if donor_account(request) is not None:  # a donor sees no menu (an error page included)
        return {"is_donor": True}
    from neurodb.indicators.services.navigation import build_navigation

    active = _active_item(request)
    return {"nav": build_navigation(_page_year(request, active)), "nav_active": active}
