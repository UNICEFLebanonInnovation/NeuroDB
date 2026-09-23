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
    }


def navigation(request):
    """Sidebar content generated from the database (v2 hard-coded 190 lines of HTML)."""
    if not getattr(request, "user", None) or not request.user.is_authenticated:
        return {}
    from neurodb.indicators.services.navigation import build_navigation

    return {"nav": build_navigation()}
