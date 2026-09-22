from django.apps import AppConfig


class LibraryConfig(AppConfig):
    name = "neurodb.library"
    label = "library"
    default_auto_field = "django.db.models.BigAutoField"
