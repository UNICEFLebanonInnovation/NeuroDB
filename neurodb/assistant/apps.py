from django.apps import AppConfig


class AssistantConfig(AppConfig):
    name = "neurodb.assistant"
    label = "assistant"
    verbose_name = "AI assistant"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        # A save of AI settings clears the snapshot every process keeps (the module connects its signals);
        # nothing is read here: the AI settings table may not exist yet during a release's migrate.
        from . import llm  # noqa: F401
