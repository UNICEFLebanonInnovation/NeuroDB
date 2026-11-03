from django.apps import AppConfig


class DonorsConfig(AppConfig):
    name = "neurodb.donors"
    label = "donors"
    verbose_name = "Donor access"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        from allauth.account.signals import password_changed, password_reset, password_set

        from .models import DonorAccount

        def clear_flag(sender, request, user, **kwargs):
            DonorAccount.objects.filter(user=user, must_change_password=True).update(
                must_change_password=False
            )

        # The temporary password is replaced once the donor chose a password of their own: on the password
        # change page, on the page that sets one (a sign-in without a usable password), or through the link
        # "Forgot password?" emails.
        password_changed.connect(clear_flag, weak=False, dispatch_uid="donors.clear_must_change_password")
        password_set.connect(clear_flag, weak=False, dispatch_uid="donors.clear_must_change_password_set")
        password_reset.connect(clear_flag, weak=False, dispatch_uid="donors.clear_must_change_password_reset")
