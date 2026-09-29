from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class CoreConfig(AppConfig):
    name = "neurodb.core"
    label = "core"
    verbose_name = _("Data and sync")  # the admin group its models are listed in (breadcrumbs)
    default_auto_field = "django.db.models.BigAutoField"
