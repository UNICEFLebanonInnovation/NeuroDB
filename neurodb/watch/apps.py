from django.apps import AppConfig


class WatchConfig(AppConfig):
    name = "neurodb.watch"
    label = "watch"
    verbose_name = "NeuroDB Watch"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        from django.db.models.signals import post_save

        from neurodb.core.models import SyncRun
        from neurodb.review.models import FindingAssignment

        from .signals import on_assignment_saved, on_run_finished

        post_save.connect(on_run_finished, sender=SyncRun, dispatch_uid="watch_on_new_data")
        post_save.connect(on_assignment_saved, sender=FindingAssignment, dispatch_uid="watch_on_assignment")
