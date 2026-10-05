from django.apps import AppConfig


class FmmConfig(AppConfig):
    name = "neurodb.fmm"
    label = "fmm"
    verbose_name = "Monitoring insights"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        # The field monitoring team names join the names NeuroDB removes from every text it sends
        from neurodb.watch import people

        from . import privacy

        if privacy.team_names not in people.EXTRA_SOURCES:
            people.EXTRA_SOURCES.append(privacy.team_names)
