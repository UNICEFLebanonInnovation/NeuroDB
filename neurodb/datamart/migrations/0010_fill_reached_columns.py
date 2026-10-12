"""Formerly: fill the columns of 0009 for the partner report rows already synced.

That work no longer runs here. A deployment applies its migrations before the site starts, and App
Service stops a container that has not started within its start-up limit (about four minutes): on a
large partner report table the fill was cut off, rolled back and started again at every restart, and
the site never came up. The ``datamart_backfill`` job (``neurodb.datamart.backfill``) does it after
the site has started: core migration 0025 schedules it to start at once. Kept, without operations, so
that the databases that applied it and those that did not share one migration history."""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("datamart", "0009_reached_columns"),
    ]

    operations = []
