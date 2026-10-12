"""Work an update leaves for after the site has started: filling the new columns of rows already
synced, and removing what an older sync stored but should not have.

A deployment applies its migrations before the site starts, and App Service stops a container that
has not started within its start-up limit (about four minutes). On the large eTools tables this work
did not fit: the migration was cut off, rolled back and started again at every restart, and the site
never came up. It runs here instead, as the ``datamart_backfill`` job: the schedule
``etools-update-cleanup`` (core migration 0025) starts it as soon as the site is up, and the job
switches that schedule off once a run has gone through everything. Administrators can also start it
from *Run a job*.

Every step writes only the rows that still need it, in batches committed one by one, so a run cut
short carries on where it stopped and a second run changes nothing. The job holds the eTools Datamart
sync's lock: the sync and the clean-up never write to the same tables at the same time (a run that
finds the sync going leaves the work to the schedule's next try).
"""

from __future__ import annotations

import json
import logging

from django.apps import apps
from django.db import connection

from neurodb.core.models import ScheduledJob, SyncRun
from neurodb.datamart.catalogue import PERSONAL_KEYS, scrub
from neurodb.integrations import background
from neurodb.integrations.runs import fail, new_run

logger = logging.getLogger(__name__)

SCHEDULE_KEY = "etools-update-cleanup"
FILL_BATCH = 20_000  # partner report rows per UPDATE (by id range)
SCRUB_BATCH = 500  # records per UPDATE (each rewritten whole)


# ----------------------------------------------------------------- the columns of datamart 0009
def _key(name: str, size: int) -> str:
    """``data[name]`` as text, as the sync stores it: a nested record by its name, else its title,
    else its id."""
    item = f"data->'{name}'"
    return (
        f"left(coalesce(CASE WHEN jsonb_typeof({item}) = 'object' THEN coalesce("
        f"nullif({item}->>'name', ''), nullif({item}->>'title', ''), {item}->>'id') "
        f"ELSE data->>'{name}' END, ''), {size})"
    )


_REPORT = _key("indicator_report", 64)
_CP_OUTPUT = _key("cp_output", 500)
_CP_OUTPUT_ID = _key("etools_cp_output_id", 64)

# Only rows no sync has written since 0009 (all three columns empty) whose record names one of the
# keys: a row the sync wrote is never overwritten, and a row once filled is not written again. The
# expressions are this module's constants (no outside value).
FILL_REPORTS = f"""
UPDATE datamart_reportedindicator
SET indicator_report = {_REPORT}, cp_output = {_CP_OUTPUT}, etools_cp_output_id = {_CP_OUTPUT_ID}
WHERE id BETWEEN %s AND %s
  AND indicator_report = '' AND cp_output = '' AND etools_cp_output_id = ''
  AND jsonb_typeof(data) = 'object'
  AND ({_REPORT} <> '' OR {_CP_OUTPUT} <> '' OR {_CP_OUTPUT_ID} <> '')
"""  # noqa: S608


def fill_reached_columns() -> dict[str, int]:
    """Copy PRP's indicator report id and the CP output from ``data`` into their columns for the
    partner report rows synced before those columns existed, then give each PD indicator the CP
    output its reports name (as every partner report sync does)."""
    from neurodb.integrations.etools.datamart_sync import derive_cp_outputs

    filled = 0
    with connection.cursor() as cursor:
        cursor.execute("SELECT min(id), max(id) FROM datamart_reportedindicator")
        low, high = cursor.fetchone()
        if low is not None:
            for start in range(low, high + 1, FILL_BATCH):
                cursor.execute(FILL_REPORTS, [start, start + FILL_BATCH - 1])
                filled += max(cursor.rowcount, 0)
    return {"report_rows_filled": filled, "pd_indicators_given_a_cp_output": derive_cp_outputs()}


# ------------------------------------------------------------------- contact details (privacy)
# a key naming an e-mail, a phone or a mobile, in a column's JSON text ("...": value)
KEY_PATTERN = r'"[^"]*(' + "|".join(PERSONAL_KEYS) + r')[^"]*"\s*:'


def _record_columns():
    """(table, column) of every JSON column of the datamart tables that holds records as received."""
    for model in apps.get_app_config("datamart").get_models():
        names = {f.name for f in model._meta.concrete_fields}
        for column in ("data", "details"):
            if column in names:
                yield model._meta.db_table, column


def _scrub_column(table: str, column: str) -> int:
    """Remove the contact-detail keys from one JSON column, batch by batch; returns the rows written.
    A row the pattern matches but :func:`scrub` leaves as it is (the words in a text, not a key) is
    not written."""
    quote = connection.ops.quote_name
    written, last = 0, 0
    while True:
        with connection.cursor() as cursor:
            cursor.execute(
                f"SELECT id, {quote(column)} FROM {quote(table)} "  # noqa: S608 - names from the models
                f"WHERE id > %s AND {quote(column)}::text ~* %s ORDER BY id LIMIT %s",
                [last, KEY_PATTERN, SCRUB_BATCH],
            )
            rows = cursor.fetchall()
            params = []
            for pk, value in rows:
                value = json.loads(value) if isinstance(value, str) else value
                cleaned = scrub(value)
                if cleaned != value:
                    params += [pk, json.dumps(cleaned)]
            if params:  # the whole batch in one statement
                count = len(params) // 2
                cursor.execute(
                    f"UPDATE {quote(table)} AS t SET {quote(column)} = v.value "  # noqa: S608 - as above
                    f"FROM (VALUES {', '.join(['(%s::bigint, %s::jsonb)'] * count)}) AS v(id, value) "
                    "WHERE t.id = v.id",
                    params,
                )
                written += count
        if len(rows) < SCRUB_BATCH:
            return written
        last = rows[-1][0]


FOCAL_POINTS = """
UPDATE etools_pca SET unicef_focal_points = ARRAY(
    SELECT x FROM unnest(unicef_focal_points) WITH ORDINALITY AS t(x, n)
    WHERE position('@' in x) = 0 ORDER BY n
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


def scrub_contact_details() -> dict[str, int]:
    """Remove what syncs before the privacy fix stored: the keys naming an e-mail address, a phone or
    a mobile in every record kept (at any depth, as ``catalogue.scrub`` removes them), every UNICEF
    focal point entry holding an e-mail address, and every ``submitted_by`` holding one (in the
    column and in the record kept beside it)."""
    counts = {"records_cleaned": 0, "programme_documents_cleaned": 0, "submitted_by_cleared": 0}
    for table, column in _record_columns():
        counts["records_cleaned"] += _scrub_column(table, column)
    with connection.cursor() as cursor:
        cursor.execute(FOCAL_POINTS)
        counts["programme_documents_cleaned"] = max(cursor.rowcount, 0)
        while True:  # in batches: a large table is never locked whole
            cursor.execute(SUBMITTED_BY, [SCRUB_BATCH * 10])
            counts["submitted_by_cleared"] += max(cursor.rowcount, 0)
            if cursor.rowcount < SCRUB_BATCH * 10:
                break
    return counts


# --------------------------------------------------------------------------------------- the job
STEPS = (("fill", fill_reached_columns), ("scrub", scrub_contact_details))


def _take_sync_lock() -> bool:
    if connection.vendor != "postgresql":
        return True
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s)", [background.DATAMART_LOCK_ID])
        return bool(cursor.fetchone()[0])


def _release_sync_lock() -> None:
    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock(%s)", [background.DATAMART_LOCK_ID])


def finished_schedule() -> int:
    """Switch the schedule that starts this job after a deployment off: nothing is left to do."""
    return ScheduledJob.objects.filter(key=SCHEDULE_KEY, enabled=True).update(
        enabled=False, next_run_at=None, last_outcome="finished: nothing left to do (switched off)"
    )


def run(triggered_by: str = "schedule") -> SyncRun | None:
    """Do every step; ``None`` when the eTools Datamart sync is running (the schedule tries again)."""
    if not _take_sync_lock():
        return None
    try:
        sync_run = new_run(SyncRun.Job.DATAMART_BACKFILL, "", triggered_by)
        details: dict[str, dict[str, int]] = {}
        try:
            for name, step in STEPS:
                details[name] = step()
        except Exception as exc:
            return fail(sync_run, exc, **details)
        sync_run.rows_written = sum(sum(counts.values()) for counts in details.values())
        sync_run.finish(SyncRun.Status.SUCCEEDED, schedule_switched_off=bool(finished_schedule()), **details)
        return sync_run
    finally:
        _release_sync_lock()
