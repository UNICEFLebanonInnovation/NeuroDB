"""Columns the "Reached" rule reads (neurodb.datamart.reached): PRP's indicator report id and the CP
output of each partner report row, and the CP output of each PD indicator (derived from its reports).

The rows already synced keep these keys in ``data`` only: they are copied into the new columns here,
in batches, so no sync has to run first."""

from django.db import migrations, models

BATCH = 20_000

FILL_REPORTS = """
UPDATE datamart_reportedindicator
SET indicator_report = left(coalesce(data->>'indicator_report', ''), 64),
    cp_output = left(coalesce(data->>'cp_output', ''), 500),
    etools_cp_output_id = left(coalesce(data->>'etools_cp_output_id', ''), 64)
WHERE id BETWEEN %s AND %s
"""

# the latest report row of each PD indicator (by its eTools id, else by its title) that names a CP output
DERIVE_BY_ID = """
UPDATE datamart_pdindicator p SET cp_output = r.cp_output
FROM (
    SELECT DISTINCT ON (intervention_id, etools_indicator_id) intervention_id, etools_indicator_id, cp_output
    FROM datamart_reportedindicator
    WHERE cp_output <> '' AND etools_indicator_id <> '' AND intervention_id IS NOT NULL
    ORDER BY intervention_id, etools_indicator_id, period_end DESC NULLS LAST, id DESC
) r
WHERE p.intervention_id = r.intervention_id AND p.source_id::text = r.etools_indicator_id
"""
DERIVE_BY_TITLE = """
UPDATE datamart_pdindicator p SET cp_output = r.cp_output
FROM (
    SELECT DISTINCT ON (intervention_id, lower(trim(indicator))) intervention_id,
           lower(trim(indicator)) AS title, cp_output
    FROM datamart_reportedindicator
    WHERE cp_output <> '' AND intervention_id IS NOT NULL
    ORDER BY intervention_id, lower(trim(indicator)), period_end DESC NULLS LAST, id DESC
) r
WHERE p.cp_output = '' AND p.intervention_id = r.intervention_id AND lower(trim(p.title)) = r.title
"""


def fill(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("SELECT min(id), max(id) FROM datamart_reportedindicator")
        low, high = cursor.fetchone()
        if low is None:
            return
        for start in range(low, high + 1, BATCH):
            cursor.execute(FILL_REPORTS, [start, start + BATCH - 1])
        cursor.execute(DERIVE_BY_ID)
        cursor.execute(DERIVE_BY_TITLE)


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
        migrations.RunPython(fill, migrations.RunPython.noop, elidable=True),
    ]
