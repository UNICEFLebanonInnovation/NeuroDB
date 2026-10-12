from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class CoreConfig(AppConfig):
    name = "neurodb.core"
    label = "core"
    verbose_name = _("Data and sync")  # the admin group its models are listed in (breadcrumbs)
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        # the figures a request has read from the shared cache are kept for the rest of it (compute.once)
        from django.core.signals import request_finished, request_started

        from . import compute

        request_started.connect(compute.request_started, dispatch_uid="core-compute-request-started")
        request_finished.connect(compute.request_finished, dispatch_uid="core-compute-request-finished")
