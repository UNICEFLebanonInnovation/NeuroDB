from django.conf import settings
from django.contrib import admin
from django.urls import include, path

from neurodb.web.views import healthz

urlpatterns = [
    path(settings.ADMIN_URL_PATH, admin.site.urls),
    path("accounts/", include("allauth.urls")),
    path("healthz/", healthz, name="healthz"),
    path("api/internal/", include("neurodb.reports.api_urls")),
    path("", include("neurodb.reports.urls")),
]

if settings.DEBUG and "debug_toolbar" in settings.INSTALLED_APPS:
    urlpatterns = [path("__debug__/", include("debug_toolbar.urls"))] + urlpatterns
