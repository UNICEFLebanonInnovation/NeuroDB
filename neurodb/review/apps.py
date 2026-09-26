from django.apps import AppConfig


class ReviewConfig(AppConfig):
    name = "neurodb.review"
    label = "review"
    verbose_name = "Daily review"
    default_auto_field = "django.db.models.BigAutoField"
