from django.apps import AppConfig


class KnowledgeConfig(AppConfig):
    name = "neurodb.knowledge"
    label = "knowledge"
    verbose_name = "Knowledge base"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        from . import signals

        signals.connect()
