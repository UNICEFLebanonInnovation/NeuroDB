"""Fill the columns of 0009 for the partner report rows already synced (their indicator report id, CP
output and eTools CP output id are in ``data`` only), and give each PD indicator the CP output its
latest report names, so no sync has to run first.

Not atomic: each batch of rows is committed on its own, so the partner report table (which can be
large) is never held locked whole while the deployment runs, and an interrupted run simply starts
again: filling twice gives the same columns. Rows whose record names none of the keys are not
rewritten."""

from django.db import migrations

BATCH = 20_000


def _key(name: str, size: int) -> str:
    """``data[name]`` as text, as the sync stores it: a nested record by its name, else its title,
    else its id."""
    item = f"data->'{name}'"
    return (
        f"left(coalesce(CASE WHEN jsonb_typeof({item}) = 'object' THEN coalesce("
        f"nullif({item}->>'name', ''), nullif({item}->>'title', ''), {item}->>'id') "
        f"ELSE data->>'{name}' END, ''), {size})"
    )


FILL_REPORTS = f"""
UPDATE datamart_reportedindicator
SET indicator_report = {_key("indicator_report", 64)},
    cp_output = {_key("cp_output", 500)},
    etools_cp_output_id = {_key("etools_cp_output_id", 64)}
WHERE id BETWEEN %s AND %s
  AND jsonb_typeof(data) = 'object'
  AND (data ? 'indicator_report' OR data ? 'cp_output' OR data ? 'etools_cp_output_id')
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
  AND p.cp_output IS DISTINCT FROM r.cp_output
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
    atomic = False

    dependencies = [
        ("datamart", "0009_reached_columns"),
    ]

    operations = [
        migrations.RunPython(fill, migrations.RunPython.noop, elidable=True),
    ]
