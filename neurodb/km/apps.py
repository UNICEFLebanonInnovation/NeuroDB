from django.apps import AppConfig


class KMConfig(AppConfig):
    name = "neurodb.km"
    label = "km"
    verbose_name = "KM Database"
    default_auto_field = "django.db.models.BigAutoField"
