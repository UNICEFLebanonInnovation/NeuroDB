"""The moment eTools stopped sending a partner report row: a whole read keeps the rows it no longer returns,
counted, and marks them no longer sent (``datamart_sync.keep_unsent``). One nullable column, no default (the
table's description only on PostgreSQL, whatever its rows); AddField only, no code runs."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("datamart", "0019_powerbi_compare"),
    ]

    operations = [
        migrations.AddField(
            model_name="reportedindicator",
            name="not_sent_since",
            field=models.DateTimeField(
                blank=True,
                help_text="when a whole read first no longer returned it: kept and counted, marked no longer sent",
                null=True,
            ),
        ),
    ]
