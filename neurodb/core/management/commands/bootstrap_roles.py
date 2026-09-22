from django.core.management.base import BaseCommand

from neurodb.accounts.roles import ensure_groups


class Command(BaseCommand):
    help = "Create the Viewer / Section editor / Administrator groups (idempotent)."

    def handle(self, *args, **options):
        groups = ensure_groups()
        self.stdout.write(self.style.SUCCESS(f"Roles ready: {', '.join(groups)}"))
