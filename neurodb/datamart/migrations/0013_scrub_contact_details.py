"""Privacy: no e-mail address, phone or mobile number is kept from eTools.

Until now the typed eTools tables kept each record whole in ``data`` (and the audit details in
``AuditEngagement.details``) without removing contact details, as the records kept whole in
``DatamartDocument`` always were; a UNICEF focal point eTools knew only by an e-mail address was
stored under that address in ``PCA.unicef_focal_points``; and a progress report's ``submitted_by`` was
kept even when it was an e-mail address (in its column and in the record kept beside it). The sync no
longer does any of these; this removes what earlier syncs stored: the keys naming an e-mail address, a
phone or a mobile (at any depth, as ``catalogue.scrub`` removes them), every focal point entry holding
an e-mail address, and every ``submitted_by`` holding one, in the column and in the record.

Batched and not atomic, so a large table is never locked whole; running it twice changes nothing.
"""

import json

from django.db import migrations

BATCH = 500
PERSONAL_KEYS = ("email", "phone", "mobile")
# a key naming one of them, in a column's JSON text ("...": value)
KEY_PATTERN = r'"[^"]*(email|phone|mobile)[^"]*"\s*:'


def scrub(value):
    """``catalogue.scrub`` as it is today (a migration keeps its own copy)."""
    if isinstance(value, dict):
        return {k: scrub(v) for k, v in value.items() if not any(p in str(k).lower() for p in PERSONAL_KEYS)}
    if isinstance(value, list):
        return [scrub(v) for v in value]
    if isinstance(value, str):
        return value.replace("\x00", "")
    return value


def _columns(apps):
    """(table, column) of every JSON column of the datamart tables that holds records as received."""
    for model in apps.get_app_config("datamart").get_models():
        names = {f.name for f in model._meta.concrete_fields}
        for column in ("data", "details"):
            if column in names:
                yield model._meta.db_table, column


def scrub_records(apps, schema_editor):
    connection = schema_editor.connection
    quote = connection.ops.quote_name
    for table, column in _columns(apps):
        last = 0
        while True:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"SELECT id, {quote(column)} FROM {quote(table)} "
                    f"WHERE id > %s AND {quote(column)}::text ~* %s ORDER BY id LIMIT %s",
                    [last, KEY_PATTERN, BATCH],
                )
                rows = cursor.fetchall()
                for pk, value in rows:
                    value = json.loads(value) if isinstance(value, str) else value
                    cursor.execute(
                        f"UPDATE {quote(table)} SET {quote(column)} = %s::jsonb WHERE id = %s",
                        [json.dumps(scrub(value)), pk],
                    )
            if len(rows) < BATCH:
                break
            last = rows[-1][0]


FOCAL_POINTS = """
UPDATE etools_pca SET unicef_focal_points = ARRAY(
    SELECT x FROM unnest(unicef_focal_points) WITH ORDINALITY AS t(x, n) WHERE position('@' in x) = 0 ORDER BY n
)
WHERE EXISTS (SELECT 1 FROM unnest(unicef_focal_points) AS x WHERE position('@' in x) > 0)
"""


SUBMITTED_BY = """
UPDATE datamart_reportedindicator SET
    submitted_by = CASE WHEN position('@' in submitted_by) > 0 THEN '' ELSE submitted_by END,
    data = CASE WHEN position('@' in coalesce(data->>'submitted_by', '')) > 0
        THEN jsonb_set(data, '{submitted_by}', '""'::jsonb) ELSE data END
WHERE id IN (
    SELECT id FROM datamart_reportedindicator
    WHERE position('@' in submitted_by) > 0
        OR (jsonb_typeof(data) = 'object' AND position('@' in coalesce(data->>'submitted_by', '')) > 0)
    LIMIT %s
)
"""


def drop_focal_point_addresses(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(FOCAL_POINTS)
        while True:  # in batches: a large table is never locked whole
            cursor.execute(SUBMITTED_BY, [BATCH * 10])
            if cursor.rowcount < BATCH * 10:
                break


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("datamart", "0012_disaggregation_labels"),
        ("etools", "0003_partnerlink"),
    ]

    operations = [
        migrations.RunPython(scrub_records, migrations.RunPython.noop, elidable=True),
        migrations.RunPython(drop_focal_point_addresses, migrations.RunPython.noop, elidable=True),
    ]
