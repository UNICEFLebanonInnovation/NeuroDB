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


def navigation(request):
    """Sidebar content generated from the database (v2 hard-coded 190 lines of HTML)."""
    if not getattr(request, "user", None) or not request.user.is_authenticated:
        return {}
    from neurodb.indicators.services.navigation import build_navigation

    return {"nav": build_navigation(), "nav_active": _active_item(request)}
