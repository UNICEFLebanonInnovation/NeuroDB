from django.apps import AppConfig


class DonorsConfig(AppConfig):
    name = "neurodb.donors"
    label = "donors"
    verbose_name = "Donor access"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        from allauth.account.signals import password_changed

        from .models import DonorAccount

        def clear_flag(sender, request, user, **kwargs):
            DonorAccount.objects.filter(user=user, must_change_password=True).update(
                must_change_password=False
            )

        password_changed.connect(clear_flag, weak=False, dispatch_uid="donors.clear_must_change_password")
