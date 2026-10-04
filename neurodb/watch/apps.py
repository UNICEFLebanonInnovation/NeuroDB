from django.apps import AppConfig


class WatchConfig(AppConfig):
    name = "neurodb.watch"
    label = "watch"
    verbose_name = "NeuroDB Watch"
    default_auto_field = "django.db.models.BigAutoField"
