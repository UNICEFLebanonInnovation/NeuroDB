from django.apps import AppConfig


class IndicatorsConfig(AppConfig):
    name = "neurodb.indicators"
    label = "pivoting"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        from . import signals

        signals.connect()  # the ActivityInfo state and the menu's sources follow their inputs
