"""Columns the "Reached" rule reads (neurodb.datamart.reached): PRP's indicator report id and the CP
output of each partner report row, and the CP output of each PD indicator (derived from its reports).

The rows already synced keep these keys in ``data`` only: 0010 copies them into the new columns, in
batches committed one by one."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("datamart", "0008_monitoringfinding_pd_link"),
    ]

    operations = [
        migrations.AddField(
            model_name="pdindicator",
            name="cp_output",
            field=models.CharField(
                blank=True,
                help_text="the country programme output, from the indicator's partner reports (not in pd-indicators)",
                max_length=500,
                verbose_name="CP output",
            ),
        ),
        migrations.AddField(
            model_name="reportedindicator",
            name="indicator_report",
            field=models.CharField(
                blank=True,
                db_index=True,
                help_text="PRP's id of this indicator in this report",
                max_length=64,
            ),
        ),
        migrations.AddField(
            model_name="reportedindicator",
            name="cp_output",
            field=models.CharField(blank=True, max_length=500, verbose_name="CP output"),
        ),
        migrations.AddField(
            model_name="reportedindicator",
            name="etools_cp_output_id",
            field=models.CharField(blank=True, max_length=64, verbose_name="eTools CP output id"),
        ),
    ]
