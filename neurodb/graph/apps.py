from django.apps import AppConfig


class GraphConfig(AppConfig):
    name = "neurodb.graph"
    label = "graph"
    verbose_name = "Knowledge hub"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        from django.db.models.signals import post_save

        from neurodb.core.models import SyncRun

        from .refresh import on_run_finished

        post_save.connect(on_run_finished, sender=SyncRun, dispatch_uid="graph_refresh_on_new_data")
