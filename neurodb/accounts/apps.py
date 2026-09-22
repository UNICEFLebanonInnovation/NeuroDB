from django.apps import AppConfig


class AccountsConfig(AppConfig):
    name = "neurodb.accounts"
    label = "users"
    default_auto_field = "django.db.models.BigAutoField"
