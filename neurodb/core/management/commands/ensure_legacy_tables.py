"""``ensure_legacy_tables``: create the tables of the NeuroDB models that a database never had.

The v2 tables were applied as unmanaged for a long time, so a database restored without some of
them (production had no ``locations`` tables) has their initial migration recorded but not the
tables. ``migrate_locked`` runs this after ``migrate``; it creates a missing table from the model
and never touches an existing one.
"""

from __future__ import annotations

from django.apps import apps
from django.core.management.base import BaseCommand
from django.db import connection

LOCAL_PREFIX = "neurodb."


def missing_models() -> list[type]:
    existing = set(connection.introspection.table_names())
    found = []
    for config in apps.get_app_configs():
        if not config.name.startswith(LOCAL_PREFIX):
            continue
        for model in config.get_models():
            if model._meta.managed and not model._meta.proxy and model._meta.db_table not in existing:
                found.append(model)
    return found


class Command(BaseCommand):
    help = (
        "Create the tables of NeuroDB models that do not exist in the database (never alters existing ones)"
    )

    def handle(self, *args, **options):
        created = []
        for model in missing_models():
            with connection.schema_editor() as editor:
                editor.create_model(model)
            created.append(model._meta.db_table)
            self.stdout.write(self.style.WARNING(f"created {model._meta.db_table}"))
        if not created:
            self.stdout.write(self.style.SUCCESS("every table is present"))
        return None
