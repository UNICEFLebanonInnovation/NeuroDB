"""The two parts of the site's URLconf Compare with Power BI links to (``powerbi_links``), mounted where
``config.urls`` mounts them: the admin, and the reports' pages."""

from django.conf import settings
from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path(settings.ADMIN_URL_PATH, admin.site.urls),
    path("", include("neurodb.reports.urls")),
]
