"""Whether the pages list a PD indicator: no for one eTools marks inactive in every row that no partner report
row names (``identity.listed``). One column with a constant default (the table's description only on
PostgreSQL, whatever its rows); AddField only, no code runs. Every row starts listed, as before; the next PD
indicators or partner reports sync marks those the pages leave out (``identity.mark_listed``)."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("datamart", "0020_partner_report_not_sent"),
    ]

    operations = [
        migrations.AddField(
            model_name="pdindicator",
            name="listed",
            field=models.BooleanField(
                default=True,
                help_text="listed on the pages; no for an indicator eTools marks inactive in every row that no "
                "partner report row names (set by the PD indicators and partner reports syncs)",
            ),
        ),
    ]
