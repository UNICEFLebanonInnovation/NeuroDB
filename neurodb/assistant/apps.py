from django.apps import AppConfig


class AssistantConfig(AppConfig):
    name = "neurodb.assistant"
    label = "assistant"
    verbose_name = "AI assistant"
    default_auto_field = "django.db.models.BigAutoField"
