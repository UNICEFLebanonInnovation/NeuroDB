"""Work an update leaves for after the site has started: filling the new columns of rows already
synced, and removing what an older sync stored but should not have.

A deployment applies its migrations before the site starts, and App Service stops a container that
has not started within its start-up limit (about four minutes). On the large eTools tables this work
did not fit: the migration was cut off and started again from the beginning at every restart, and the
site never came up. It runs here instead, as the ``datamart_backfill`` job: the schedule
``etools-update-cleanup`` (core migration 0025) starts it once the site is up (usually within a
minute, at most 15 minutes), and the job switches that schedule off once a run has gone through
everything. Administrators can also start it from *Run a job*.

Every step writes only the rows that still need it, in batches committed one by one, so a run cut
short carries on where it stopped and a second run changes nothing. Each step reads a table once,
window by window along the primary key, however many rows need work.

The job holds the eTools Datamart sync's lock, so the sync and the clean-up never write to the same
tables at the same time, and its own lock first, so that a sync finding the Datamart lock taken
knows it is the clean-up and waits for it (``sync_etools_datamart``). A clean-up that finds the sync
going leaves the work to its schedule, switched back on if a finished run had switched it off.

The partner report rows eTools no longer sends (kept and counted by the sync, ``datamart_sync.keep_unsent``)
are removed here too, by a superuser after review (Raw eTools data → Partner reports, :func:`remove_unsent`):
a run of this job with the target :data:`UNSENT_TARGET`, never a sync, holding both locks in the same order.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator

from django.apps import apps
from django.db import connection, transaction
from django.db.models import Count, QuerySet

from neurodb.core.models import ScheduledJob, SyncRun
from neurodb.datamart.catalogue import PERSONAL_KEYS, scrub
from neurodb.integrations import background
from neurodb.integrations.runs import fail, new_run

logger = logging.getLogger(__name__)

SCHEDULE_KEY = "etools-update-cleanup"
FILL_BATCH = 20_000  # partner report rows per UPDATE (by id range)
SCRUB_WINDOW = 2_000  # rows read per statement when looking for contact details (by id, in order)
SCRUB_BATCH = 500  # records per UPDATE (each rewritten whole)

ALREADY_RUNNING = "Another eTools update clean-up is running; nothing to do."
SYNC_RUNNING = (
    "The eTools Datamart sync is running: the clean-up starts again at its schedule's next try "
    "(every 15 minutes) once the sync has finished."
)
WAITING = "waiting: the eTools Datamart sync is running"
# a superuser's removal of the partner report rows eTools no longer sends, after review (remove_unsent)
UNSENT_TARGET = "partner_reports:no_longer_sent"
UNSENT_BUSY = "An eTools sync is running: remove them once it ends."
UNSENT_CHANGED = "The rows changed since you looked: review them again."
UNSENT_FAILED = "Nothing was removed: the removal failed. Its run in Import and sync runs says why."


# ----------------------------------------------------------------- the columns of datamart 0009
# spaces, tabs and line breaks around a value: the sync strips them from every CharField (str.strip())
_SPACES = "' ' || chr(9) || chr(10) || chr(11) || chr(12) || chr(13)"


def _key(name: str, size: int) -> str:
    """``data[name]`` as text, as the sync stores it: a nested record by its name, else its title,
    else its id; without leading or trailing spaces, cut to the column's size."""
    item = f"data->'{name}'"
    value = (
        f"CASE WHEN jsonb_typeof({item}) = 'object' THEN coalesce("
        f"nullif({item}->>'name', ''), nullif({item}->>'title', ''), {item}->>'id') "
        f"ELSE data->>'{name}' END"
    )
    return f"left(btrim(coalesce({value}, ''), {_SPACES}), {size})"


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


def _windows(table: str, size: int) -> Iterator[tuple[int, int]]:
    """``(after, upto)``: consecutive windows of ``size`` rows of ``table`` in id order (the rows with
    ``after < id <= upto``). A statement bounded by a window reads only those rows through the primary
    key, so a pass reads the table once however many rows need work; a search ``ORDER BY id LIMIT n``
    would read the rest of the table again for every batch."""
    quote = connection.ops.quote_name
    after = 0
    while True:
        with connection.cursor() as cursor:
            cursor.execute(
                f"SELECT max(id) FROM (SELECT id FROM {quote(table)} "  # noqa: S608 - a model's table
                "WHERE id > %s ORDER BY id LIMIT %s) AS window_ids",
                [after, size],
            )
            upto = cursor.fetchone()[0]
        if upto is None:
            return
        yield after, upto
        after = upto


def _record_columns():
    """(table, column) of every JSON column of the datamart tables that holds records as received."""
    for model in apps.get_app_config("datamart").get_models():
        names = {f.name for f in model._meta.concrete_fields}
        for column in ("data", "details"):
            if column in names:
                yield model._meta.db_table, column


def _scrub_column(table: str, column: str) -> int:
    """Remove the contact-detail keys from one JSON column, window by window; returns the rows written.
    A row the pattern matches but :func:`scrub` leaves as it is (the words in a text, not a key) is
    not written."""
    quote = connection.ops.quote_name
    written = 0
    for after, upto in _windows(table, SCRUB_WINDOW):
        with connection.cursor() as cursor:
            cursor.execute(
                f"SELECT id, {quote(column)} FROM {quote(table)} "  # noqa: S608 - names from the models
                f"WHERE id > %s AND id <= %s AND {quote(column)}::text ~* %s",
                [after, upto, KEY_PATTERN],
            )
            changed = []
            for pk, value in cursor.fetchall():
                value = json.loads(value) if isinstance(value, str) else value
                cleaned = scrub(value)
                if cleaned != value:
                    changed.append((pk, json.dumps(cleaned)))
            for start in range(0, len(changed), SCRUB_BATCH):  # a batch in one statement
                batch = changed[start : start + SCRUB_BATCH]
                cursor.execute(
                    f"UPDATE {quote(table)} AS t SET {quote(column)} = v.value "  # noqa: S608 - as above
                    f"FROM (VALUES {', '.join(['(%s::bigint, %s::jsonb)'] * len(batch))}) AS v(id, value) "
                    "WHERE t.id = v.id",
                    [part for row in batch for part in row],
                )
                written += len(batch)
    return written


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
WHERE id > %s AND id <= %s
  AND (position('@' in submitted_by) > 0
    OR (jsonb_typeof(data) = 'object' AND position('@' in coalesce(data->>'submitted_by', '')) > 0))
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
    for after, upto in _windows("datamart_reportedindicator", FILL_BATCH):  # never the table locked whole
        with connection.cursor() as cursor:
            cursor.execute(SUBMITTED_BY, [after, upto])
            counts["submitted_by_cleared"] += max(cursor.rowcount, 0)
    return counts


# --------------------------------------------------------------------------------------- the job
STEPS = (("fill", fill_reached_columns), ("scrub", scrub_contact_details))


def _take(lock_id: int) -> bool:
    if connection.vendor != "postgresql":
        return True
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s)", [lock_id])
        return bool(cursor.fetchone()[0])


def _release(lock_id: int) -> None:
    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock(%s)", [lock_id])


def finished_schedule() -> int:
    """Switch the schedule that starts this job after a deployment off: nothing is left to do."""
    return ScheduledJob.objects.filter(key=SCHEDULE_KEY, enabled=True).update(
        enabled=False, next_run_at=None, last_outcome="finished: nothing left to do (switched off)"
    )


def _try_after_the_sync() -> None:
    """The sync holds the Datamart lock: the schedule tries again at its next quarter-hour, switched
    back on when a finished run had switched it off (a run started by hand during a sync then still
    happens, after the sync)."""
    job = ScheduledJob.objects.filter(key=SCHEDULE_KEY).first()
    if job is None:
        return
    job.enabled = True
    if job.next_run_at is None:
        job.plan_next()
    job.last_outcome = WAITING
    job.save(update_fields=["enabled", "next_run_at", "last_outcome"])


def run(triggered_by: str = "schedule") -> SyncRun | str:
    """Do every step. When it cannot run now it returns why, a sentence for the log, instead of a run:
    another clean-up is running, or the eTools Datamart sync is (the schedule then tries again)."""
    if not _take(background.DATAMART_BACKFILL_LOCK_ID):
        return ALREADY_RUNNING
    try:
        if not _take(background.DATAMART_LOCK_ID):
            _try_after_the_sync()
            return SYNC_RUNNING
        try:
            return _run_steps(triggered_by)
        finally:
            _release(background.DATAMART_LOCK_ID)
    finally:
        _release(background.DATAMART_BACKFILL_LOCK_ID)


def _run_steps(triggered_by: str) -> SyncRun:
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


def remove_unsent(rows: QuerySet, shown: int, words: str, triggered_by: str) -> SyncRun | str:
    """Remove ``rows`` (the partner report rows of a filter of Raw eTools data eTools no longer sends, up to
    the newest mark the page showed), once a superuser has reviewed them and the eTools Datamart team
    confirmed PRP deleted their reports. It takes the clean-up's lock, then the sync's, as :func:`run` does,
    so neither a sync nor a clean-up writes meanwhile and a sync reads it as a clean-up; it refuses when
    either is held, or when the rows are no longer the ``shown`` the reviewer saw. The run is the clean-up's
    (target :data:`UNSENT_TARGET`), never a sync: a removal never counts as a fresh eTools sync. ``words``:
    the filter, as the page says it. Returns the run (failed when the removal did, nothing removed), or why
    nothing was removed."""
    from . import models as dm
    from . import reporting

    if not _take(background.DATAMART_BACKFILL_LOCK_ID):
        return UNSENT_BUSY
    try:
        if not _take(background.DATAMART_LOCK_ID):
            return UNSENT_BUSY
        try:
            pks = list(rows.order_by().values_list("pk", flat=True))
            if len(pks) != shown:
                return UNSENT_CHANGED
            sync_run = new_run(SyncRun.Job.DATAMART_BACKFILL, UNSENT_TARGET, triggered_by)
            try:
                with transaction.atomic():
                    gone = dm.ReportedIndicator.objects.filter(pk__in=pks)
                    reports = gone.aggregate(n=Count(reporting.REPORT_KEY, distinct=True))["n"]
                    _, deleted = gone.delete()
            except Exception as exc:
                return fail(sync_run, exc)
            removed = deleted.get(dm.ReportedIndicator._meta.label, 0)
            sync_run.rows_written = removed
            sync_run.finish(SyncRun.Status.SUCCEEDED, removed=removed, reports=reports, filter=words)
            return sync_run
        finally:
            _release(background.DATAMART_LOCK_ID)
    finally:
        _release(background.DATAMART_BACKFILL_LOCK_ID)
