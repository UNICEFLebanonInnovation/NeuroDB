from django.apps import AppConfig


class DatamartConfig(AppConfig):
    name = "neurodb.datamart"
    label = "datamart"
    verbose_name = "eTools Datamart"
    default_auto_field = "django.db.models.BigAutoField"
