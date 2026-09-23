from django.apps import AppConfig
from django.contrib.admin import apps as admin_apps


class WebConfig(AppConfig):
    name = "neurodb.web"
    label = "web"
    default = True
    default_auto_field = "django.db.models.BigAutoField"


class NeuroDBAdminConfig(admin_apps.AdminConfig):
    """Django admin with the NeuroDB site (grouped models, dashboard home)."""

    default = False
    default_site = "neurodb.web.admin_site.NeuroDBAdminSite"

    def ready(self):
        super().ready()  # autodiscovers every admin.py
        from django.contrib import admin

        from neurodb.web.admin_site import adopt_unfold

        adopt_unfold(admin.site)
