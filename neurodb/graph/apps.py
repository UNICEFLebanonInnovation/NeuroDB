from django.apps import AppConfig


class GraphConfig(AppConfig):
    name = "neurodb.graph"
    label = "graph"
    verbose_name = "Knowledge hub"
    default_auto_field = "django.db.models.BigAutoField"
