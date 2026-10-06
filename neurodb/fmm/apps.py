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

        # Ask NeuroDB can count, list and search the visits too (structured fields only: without a chat's
        # context the look-ups read no note, answer or snippet). Registered here, so the assistant's
        # tools never import this app.
        from django.conf import settings

        if settings.FMM_ENABLED:
            from neurodb.assistant import tools as assistant_tools

            from .ai.ap_tools import ASK_TOOLS
            from .ai.tools import FMM_TOOLS

            assistant_tools.TOOLS.update(FMM_TOOLS)
            # ... and count the action points by the AI's verdict and the PME verification (Ask only)
            assistant_tools.TOOLS.update(ASK_TOOLS)
