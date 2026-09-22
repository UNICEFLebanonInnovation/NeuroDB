from django.apps import AppConfig


class GeoConfig(AppConfig):
    name = "neurodb.geo"
    label = "locations"
    default_auto_field = "django.db.models.BigAutoField"
