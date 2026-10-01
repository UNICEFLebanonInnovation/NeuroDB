from django.apps import AppConfig


class InsightsConfig(AppConfig):
    name = "neurodb.insights"
    label = "insights"
    verbose_name = "Insights (machine learning)"
    default_auto_field = "django.db.models.BigAutoField"
