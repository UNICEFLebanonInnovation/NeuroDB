"""The job of Compare with Power BI (``manage.py compare_powerbi --upload-id <pk>``, started in the background
by the upload page's *Upload and compare* and *Compare again*; never from a shell on the server).

1. It takes its own advisory lock (one comparison at a time) and moves the upload from queued to reading with
   a conditional update (an upload failed meanwhile, or done already, is left alone).
2. **Reading.** Each staged export's repacked copy is read into its rows (``powerbi_read.read``) and emptied
   at once; one export that cannot be read fails alone.
3. **The day** the statuses are read for: Power BI's refresh (the newest "Last Updated Date" or "Last Modify
   Date" in the files), else the upload's day; more than 14 days back, today, and the page says so. The
   partner reports sync NeuroDB's data comes from is noted, and Power BI's own fields a filter names are
   looked for in the records it kept (Raw eTools data's fields): when every record carries one, NeuroDB
   applies it from the records.
4. A Datamart sync or a KM Database load started meanwhile stops it (the rows stay: *Compare again* works).
5. **Comparing**, export by export in Power BI's order (the report list and the Data Quality list first: the
   counted visuals are explained from them): an export whose filter names another country is not compared, an
   export with no row is "Empty in Power BI" (with the rows NeuroDB's page has for its filter); every other
   export is compared by its kind's comparer (``powerbi_kinds.COMPARERS``) in its own read-only snapshot of
   the data. Its lines are kept compactly during the snapshot and written after it, needs-a-look first, at
   most :data:`LINE_LIMIT` per export and :data:`UPLOAD_LINE_LIMIT` per upload (the rest are counted).
6. The upload is done: its summary (a verdict per Power BI page, what to do now, the choices for the user,
   ``powerbi_results``), its memory peak and time are kept; uploads older than the newest :data:`KEEP_FULL`
   lose their rows and lines (retention, also applied when an upload is created).

This job writes the three Compare with Power BI tables and nothing else: no import run, no figure of any page.
Its logs carry counts, kinds and class names only, never a value from the files.
"""

from __future__ import annotations

import datetime
import logging
import os
import resource
import time
from typing import Any

from django.conf import settings
from django.db import DatabaseError, connection, transaction
from django.db.models import Q, QuerySet
from django.utils import timezone

from neurodb.integrations import background

from . import powerbi_exports as px
from . import powerbi_facts, powerbi_filters, powerbi_kinds, powerbi_match, powerbi_read, powerbi_xlsx
from . import powerbi_words as words
from .models import PowerBIDifference, PowerBIExport, PowerBIUpload

logger = logging.getLogger(__name__)

LOCK_ID = background.POWERBI_COMPARE_LOCK_ID
HEAVY_LOCKS = (background.DATAMART_LOCK_ID, background.KM_LOAD_LOCK_ID)  # a comparison never runs beside them
COMMAND = "compare_powerbi"
QUEUED_FOR_AT_MOST = datetime.timedelta(minutes=2)  # a queued upload older than this never started
KEEP_FULL = 3  # the newest uploads that keep their rows and lines
LINE_LIMIT = 20_000  # lines written per export (the rest are counted)
UPLOAD_LINE_LIMIT = 60_000  # lines written per upload
MEMORY_LIMIT = 600 * 1024 * 1024  # the job's data limit: past it, a MemoryError ends it (not the container)
NICE = 10
MAX_REFUSED = 100
Status = PowerBIUpload.Status
BUSY_STATUSES = (Status.QUEUED, Status.READING, Status.COMPARING)
READ_STATES = ("read", "compared", "empty", "not_compared")  # an export whose rows NeuroDB read
COMPARERS = powerbi_kinds.COMPARERS  # an export's kind -> the function that compares it with NeuroDB's page
BATCH = 1_000  # lines written at once


def exports_of(upload: PowerBIUpload | int) -> QuerySet[PowerBIExport]:
    """An upload's exports in Power BI's order, without their stored bytes."""
    return PowerBIExport.objects.filter(upload=upload).defer("content", "rows").order_by("order", "id")


# ------------------------------------------------------------------------------------------------ the lock
def _lock() -> bool:
    if connection.vendor != "postgresql":
        return True
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s)", [LOCK_ID])
        return bool(cursor.fetchone()[0])


def _unlock() -> None:
    if connection.vendor != "postgresql":
        return
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock(%s)", [LOCK_ID])
    except DatabaseError:  # the session is gone, and its lock with it
        logger.warning("Compare with Power BI: the lock could not be released")


def heavy_job_running() -> bool:
    """A Datamart sync or a KM Database load holds its lock now."""
    return any(background.lock_is_held(lock) for lock in HEAVY_LOCKS)


def limit_process() -> None:
    """The job's own process gives way to the web workers: a lower CPU priority, and a data limit past which a
    MemoryError ends the comparison instead of the container being stopped."""
    os.nice(NICE)
    _soft, hard = resource.getrlimit(resource.RLIMIT_DATA)
    limit = MEMORY_LIMIT if hard == resource.RLIM_INFINITY else min(MEMORY_LIMIT, hard)
    resource.setrlimit(resource.RLIMIT_DATA, (limit, hard))


def _empty_content(upload_pk: int) -> None:
    PowerBIExport.objects.filter(upload_id=upload_pk).exclude(content=None).update(content=None)


def _staged_left(upload_pk: int) -> bool:
    return PowerBIExport.objects.filter(upload_id=upload_pk, state="staged").exists()


def close_stale(*, ours: int | None = None) -> int:
    """Fail the uploads whose comparison cannot be running: reading or comparing while nobody holds the lock
    (the job holds it throughout, so its process stopped), and queued for more than two minutes since it was
    queued while nobody holds it (the job never started). Their repacked copies are emptied. ``ours``: the
    upload of the job that called this while holding the lock itself. Returns how many."""
    now = timezone.now()
    if ours is None and background.lock_is_held(LOCK_ID):
        return 0
    stale = PowerBIUpload.objects.filter(
        Q(status__in=(Status.READING, Status.COMPARING))
        | Q(status=Status.QUEUED, queued_at__lt=now - QUEUED_FOR_AT_MOST)
    )
    if ours is not None:
        stale = stale.exclude(pk=ours)
    closed = 0
    for pk in stale.values_list("pk", flat=True):
        error = words.STALE if _staged_left(pk) else words.STALE_AGAIN
        closed += PowerBIUpload.objects.filter(pk=pk, status__in=BUSY_STATUSES).update(
            status=Status.FAILED, error=error, finished_at=now
        )
        _empty_content(pk)
    if closed:
        logger.warning("Compare with Power BI: closed %s upload(s) whose comparison had stopped", closed)
    return closed


def busy() -> PowerBIUpload | None:
    """The upload waiting to be compared, being read or compared, if any (after :func:`close_stale`)."""
    close_stale()
    return PowerBIUpload.objects.filter(status__in=BUSY_STATUSES).order_by("-uploaded_at").first()


# ------------------------------------------------------------------------------------- creating, starting
def known_sections() -> list[str]:
    """The section names NeuroDB's programme documents spell (the words Power BI's ``section_name`` takes)."""
    from neurodb.partnerships.models import PCA

    found = {
        " ".join(name.split())
        for names in PCA.objects.order_by().values_list("section_names", flat=True)
        for name in names or []
        if name and name.strip()
    }
    return sorted(found)


def _aware(moment: datetime.datetime | None) -> datetime.datetime | None:
    if moment is None:
        return None
    return moment if timezone.is_aware(moment) else timezone.make_aware(moment)


def create(prepared: powerbi_read.Upload, user: Any) -> PowerBIUpload:
    """The upload and its exports, in one transaction (retention applied in it): read exports with their rows,
    staged ones with their repacked copy."""
    now = timezone.now()
    with transaction.atomic():
        upload = PowerBIUpload.objects.create(
            status=Status.QUEUED,
            queued_at=now,
            uploaded_by=user if getattr(user, "pk", None) else None,
            refused=prepared.refused[:MAX_REFUSED],
            summary={"hidden": prepared.hidden},
        )
        stamps = []
        rows = []
        for item in sorted(prepared.exports, key=lambda e: e.kind.order):
            export = PowerBIExport(
                upload=upload,
                order=item.kind.order,
                kind=item.kind.key,
                file_name=item.file_name,
                state=item.state,
                content=item.content,
                dropped=item.dropped,
                not_compared=item.not_compared,
                unknown_columns=min(item.unknown, 32_000),
            )
            if item.read is not None:
                _keep_read(export, item.read)
                if item.read.stamp:
                    stamps.append(item.read.stamp)
            rows.append(export)
        PowerBIExport.objects.bulk_create(rows)
        if stamps:
            upload.powerbi_as_of = _aware(max(stamps))
            upload.save(update_fields=["powerbi_as_of"])
        retention()
    logger.info(
        "Compare with Power BI: upload %s queued, %s export(s), %s file(s) not read",
        upload.pk,
        len(rows),
        len(prepared.refused),
    )
    return upload


def _keep_read(export: PowerBIExport, found: powerbi_read.Read) -> None:
    export.rows = found.rows
    export.rows_read = found.rows_read
    export.columns = found.columns
    export.filters = found.filter.to_json()
    export.clauses = [{"words": c.words, "state": c.state} for c in found.filter.clauses]
    export.notes = list(found.notes)
    export.state = "read"


def start(upload: PowerBIUpload, user: Any) -> bool:
    """Start the job for ``upload``. When it cannot start, the upload fails and its copies are emptied."""
    try:
        background.start_command(
            COMMAND, "--upload-id", str(upload.pk), "--triggered-by", getattr(user, "username", "") or "page"
        )
        return True
    except Exception as exc:  # noqa: BLE001 - only the class name is kept and logged
        logger.warning("Compare with Power BI: upload %s could not start (%s)", upload.pk, type(exc).__name__)
        PowerBIUpload.objects.filter(pk=upload.pk).update(
            status=Status.FAILED, error=words.START_FAILED, finished_at=timezone.now()
        )
        _empty_content(upload.pk)
        return False


def can_compare_again(upload: PowerBIUpload) -> bool:
    """An upload whose rows are all kept (none waiting in a copy), not trimmed and not running."""
    if upload.trimmed or upload.status in BUSY_STATUSES:
        return False
    exports = PowerBIExport.objects.filter(upload=upload)
    return (
        exports.exists()
        and not exports.filter(state="staged").exists()
        and exports.exclude(rows=None).exists()
    )


def again(upload: PowerBIUpload) -> bool:
    """*Compare again*: the same rows against NeuroDB's current data. Its lines and results are removed and
    it waits for the job again (queued now)."""
    with transaction.atomic():
        locked = PowerBIUpload.objects.select_for_update().filter(pk=upload.pk).first()
        if locked is None or not can_compare_again(locked):
            return False
        exports = PowerBIExport.objects.filter(upload=locked)
        PowerBIDifference.objects.filter(export__upload=locked).delete()
        exports.exclude(rows=None).update(state="read", counts={}, fits=[], verdict="", set_aside=0, error="")
        PowerBIUpload.objects.filter(pk=locked.pk).update(
            status=Status.QUEUED,
            queued_at=timezone.now(),
            error="",
            started_at=None,
            finished_at=None,
            compared_at=None,
            sync_moved=False,
            choices=[],
        )
    return True


def retention() -> int:
    """The uploads older than the newest :data:`KEEP_FULL` lose their rows, copies and lines (their summary,
    counts and notes stay). Returns how many were trimmed."""
    keep = list(
        PowerBIUpload.objects.order_by("-uploaded_at", "-id").values_list("pk", flat=True)[:KEEP_FULL]
    )
    older = list(
        PowerBIUpload.objects.exclude(pk__in=keep).filter(trimmed=False).values_list("pk", flat=True)
    )
    if not older:
        return 0
    PowerBIDifference.objects.filter(export__upload_id__in=older).delete()
    PowerBIExport.objects.filter(upload_id__in=older).update(rows=None, content=None)
    PowerBIUpload.objects.filter(pk__in=older).update(trimmed=True)
    return len(older)


# ------------------------------------------------------------------------------------------------- the run
def run(pk: int) -> PowerBIUpload | None:
    """Compare upload ``pk``. Returns the upload as it ended (done or failed), or None when there was nothing
    to do (it is not waiting)."""
    if not _lock():
        error = words.LOCKED if _staged_left(pk) else words.LOCKED_AGAIN
        if PowerBIUpload.objects.filter(pk=pk, status=Status.QUEUED).update(
            status=Status.FAILED, error=error, finished_at=timezone.now()
        ):
            logger.warning("Compare with Power BI %s: another comparison holds the lock", pk)
            _empty_content(pk)  # only an upload this call failed: another may be reading its own
        return PowerBIUpload.objects.filter(pk=pk).first()
    started = time.monotonic()
    try:
        close_stale(ours=pk)
        if not PowerBIUpload.objects.filter(pk=pk, status=Status.QUEUED).update(
            status=Status.READING, started_at=timezone.now()
        ):
            logger.info("Compare with Power BI %s: not waiting, nothing to do", pk)
            return None
        upload = PowerBIUpload.objects.get(pk=pk)
        _read_staged(upload)
        before = _partner_reports_run()
        _day(upload, before)
        _record_fields(upload, before)
        if heavy_job_running():
            return _fail(upload, words.OTHER_JOB_LATER, started)
        if not PowerBIUpload.objects.filter(pk=pk, status=Status.READING).update(status=Status.COMPARING):
            return PowerBIUpload.objects.filter(pk=pk).first()
        context = _context(upload)
        written = 0
        for n, export in enumerate(_in_order(exports_of(upload)), start=1):
            written += _compare(export, upload, context, UPLOAD_LINE_LIMIT - written)
            _progress(upload, n)
        after = _partner_reports_run()
        upload.sync_moved = _run_id(before) != _run_id(after)
        from . import powerbi_results

        summary = {**(upload.summary or {}), **_summary(upload)}
        summary.pop("compared", None)
        upload.summary = {**summary, **powerbi_results.summary(upload, list(exports_of(upload)))}
        upload.choices = powerbi_results.choices(list(exports_of(upload)))
        upload.status = Status.DONE
        upload.compared_at = upload.finished_at = timezone.now()
        upload.error = ""
        _measure(upload, started)
        upload.save(
            update_fields=[
                "status",
                "compared_at",
                "finished_at",
                "error",
                "sync_moved",
                "summary",
                "choices",
                "day",
                "powerbi_as_of",
                "synced_at",
                "peak_memory_mb",
                "seconds",
            ]
        )
        logger.info("Compare with Power BI %s: done in %.0f s", pk, upload.seconds or 0)
        return upload
    except Exception as exc:  # noqa: BLE001 - the upload keeps our sentence and the class name, nothing else
        logger.warning("Compare with Power BI %s stopped (%s)", pk, type(exc).__name__)
        upload = PowerBIUpload.objects.filter(pk=pk).first()
        if upload is None:
            return None
        error = words.STOPPED_UPLOAD if _staged_left(pk) else words.STOPPED_AGAIN  # its copies are emptied
        return _fail(upload, error.format(cls=type(exc).__name__), started)
    finally:
        try:
            _empty_content(pk)
            retention()
        finally:
            _unlock()


def _fail(upload: PowerBIUpload, error: str, started: float) -> PowerBIUpload:
    upload.status = Status.FAILED
    upload.error = error[:300]
    upload.finished_at = timezone.now()
    _measure(upload, started)
    PowerBIUpload.objects.filter(pk=upload.pk).update(
        status=upload.status,
        error=upload.error,
        finished_at=upload.finished_at,
        peak_memory_mb=upload.peak_memory_mb,
        seconds=upload.seconds,
    )
    return upload


def _measure(upload: PowerBIUpload, started: float) -> None:
    upload.peak_memory_mb = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)  # KB on Linux
    upload.seconds = round(time.monotonic() - started, 1)


def _read_staged(upload: PowerBIUpload) -> None:
    """Each staged export read from its repacked copy, which is emptied in the same save; one that cannot be
    read fails alone."""
    from neurodb.watch import people

    names = people.known_names()
    sections = known_sections()
    newest = upload.powerbi_as_of
    for export in PowerBIExport.objects.filter(upload=upload, state="staged").order_by("order", "id"):
        try:
            content = bytes(export.content or b"")
            peek = powerbi_xlsx.peek(content)
            match = px.recognise(peek.header or [], peek.filled)
            if match.kind is None or match.kind.key != export.kind:
                raise ValueError("kind")
            found = powerbi_read.read(content, match, names, sections, peek.sheet)
            _keep_read(export, found)
            export.content = None
            export.save(
                update_fields=[
                    "rows",
                    "rows_read",
                    "columns",
                    "filters",
                    "clauses",
                    "notes",
                    "state",
                    "content",
                ]
            )
            stamp = _aware(found.stamp)
            if stamp and (newest is None or stamp > newest):
                newest = stamp
        except MemoryError:
            raise  # past the job's memory limit: the whole comparison stops
        except Exception as exc:  # noqa: BLE001 - one export never stops the others; its class name only
            logger.warning(
                "Compare with Power BI %s: an export could not be read (%s)", upload.pk, type(exc).__name__
            )
            PowerBIExport.objects.filter(pk=export.pk).update(
                state="failed",
                content=None,
                error=words.COULD_NOT_READ.format(name=export.file_name, cls=type(exc).__name__)[:300],
            )
    if newest != upload.powerbi_as_of:
        upload.powerbi_as_of = newest
        PowerBIUpload.objects.filter(pk=upload.pk).update(powerbi_as_of=newest)


def _partner_reports_run() -> Any:
    from . import runfacts

    return runfacts.runs(["partner_reports"])["partner_reports"][0]


def _run_id(run: Any) -> Any:
    return (run.pk, run.finished_at) if run is not None else None


def _day(upload: PowerBIUpload, run: Any) -> None:
    """The day the statuses are read for (Power BI's refresh, else the upload's day; today when that is more
    than 14 days back), and when the partner reports sync NeuroDB's data comes from finished."""
    from . import comparison, runfacts, services

    today = services.reporting_today()
    moment = upload.powerbi_as_of or upload.uploaded_at
    day = timezone.localtime(moment).date() if moment else today
    notes = []
    if day < today - datetime.timedelta(days=comparison.DAYS_BACK) or day > today:
        if upload.powerbi_as_of and day < today:
            notes.append(words.DAY_OLD.format(day=comparison._day(day)))
        day = today
    upload.day = day
    upload.synced_at = runfacts.moment(run)
    if upload.powerbi_as_of and upload.synced_at:
        refreshed = timezone.localtime(upload.powerbi_as_of).date()
        if timezone.localtime(upload.synced_at).date() < refreshed:
            notes.append(words.SYNC_BEFORE)
    upload.summary = {**(upload.summary or {}), "notes": notes}


def fields_in_records(run: Any) -> set[str]:
    """Power BI's own fields every partner report record of ``run`` carries (Raw eTools data's fields)."""
    from . import raw

    kept = raw.keys_kept(run, "partner_reports")
    most = max((counts[0] for counts in kept.values() if counts), default=0)
    return {f for f in powerbi_filters.RECORD_FIELDS if most and (kept.get(f) or [0])[0] == most}


def _record_fields(upload: PowerBIUpload, run: Any) -> None:
    """How each clause on one of Power BI's own fields is applied: from the records when they carry it, else
    as NeuroDB reads it (with a note)."""
    carried = fields_in_records(run)
    for export in exports_of(upload).exclude(state="failed"):
        parsed = powerbi_filters.Filter.from_json(export.filters)
        notes = [n for n in export.notes if not n.startswith("Power BI's field ")]
        changed = False
        for clause in parsed.clauses:
            if clause.field not in powerbi_filters.RECORD_FIELDS or clause.state not in (
                "as_read",
                "from_records",
            ):
                continue
            state = "from_records" if clause.field in carried else "as_read"
            if state == "as_read":
                notes.append(
                    words.NOT_IN_RECORDS.format(field=clause.field, words=words.AS_READ[clause.field])
                )
            changed = changed or state != clause.state
            clause.state = state
        if changed or notes != export.notes:
            export.filters = parsed.to_json()
            export.clauses = [{"words": c.words, "state": c.state} for c in parsed.clauses]
            export.notes = notes
            export.save(update_fields=["filters", "clauses", "notes"])


def _context(upload: PowerBIUpload) -> powerbi_facts.Context:
    """What every export of the comparison shares: the day, NeuroDB's history, Power BI's refresh and the sync
    (dates), the people cleaner's names and the section names NeuroDB knows."""
    from neurodb.watch import people

    from . import reached

    refreshed = timezone.localtime(upload.powerbi_as_of).date() if upload.powerbi_as_of else None
    synced = timezone.localtime(upload.synced_at).date() if upload.synced_at else None
    day = upload.day or timezone.localdate()
    return powerbi_facts.Context(
        day=day,
        history=reached.history_from(day).date,
        refreshed=refreshed,
        synced=synced,
        names=people.known_names(),
        sections=tuple(known_sections()),
    )


def _in_order(exports: QuerySet[PowerBIExport]) -> list[PowerBIExport]:
    """Power BI's order, the report list and the Data Quality list first (the counted visuals and the card are
    explained from them)."""
    found = list(exports)
    first = [e for kind in powerbi_kinds.FIRST for e in found if e.kind == kind]
    return first + [e for e in found if e.kind not in powerbi_kinds.FIRST]


def _progress(upload: PowerBIUpload, n: int) -> None:
    """How many exports are compared so far (the upload's page says "Comparing… n of 13 exports")."""
    summary = {**(upload.summary or {}), "compared": n}
    upload.summary = summary
    PowerBIUpload.objects.filter(pk=upload.pk).update(summary=summary)


def _compare(
    export: PowerBIExport,
    upload: PowerBIUpload,
    context: powerbi_facts.Context,
    room: int = UPLOAD_LINE_LIMIT,
) -> int:
    """One export against NeuroDB, in its own snapshot of the data; one that fails leaves the others. Returns
    the lines written (at most ``room``)."""
    from . import powerbi_results, raw

    if export.state == "failed":
        return 0
    parsed = powerbi_filters.Filter.from_json(export.filters)
    try:
        written = 0
        if parsed.country_ok is False:
            export.state, export.verdict = "not_compared", "other_country"
            note = words.OTHER_COUNTRY.format(country=settings.ETOOLS_DATAMART_COUNTRY)
            export.notes = [*export.notes, note] if note not in export.notes else export.notes
        elif export.rows_read == 0:
            with raw.snapshot(raw.STREAM_TIMEOUT_MS):
                count = powerbi_kinds.neurodb_count(export.kind, parsed, context)
            export.state, export.verdict = "empty", "empty"
            export.counts = {"neurodb_rows": count}
        elif export.kind in COMPARERS:
            data = PowerBIExport.objects.filter(pk=export.pk).values_list("rows", flat=True).first()
            result = powerbi_match.Result(limit=LINE_LIMIT)
            job = powerbi_kinds.Job(
                kind=px.KINDS[export.kind],
                columns=list(export.columns),
                data=bytes(data) if data else None,
                parsed=parsed,
                context=context,
                result=result,
                rows_read=export.rows_read,
            )
            with raw.snapshot(raw.STREAM_TIMEOUT_MS):
                COMPARERS[export.kind](job)
            written = _write_lines(export, result, min(room, LINE_LIMIT))
            counts = result.summary()
            counts["written"] = written
            if counts["lines"] > written:
                result.note(words.NOTE_LINES_CUT.format(n=counts["lines"] - written))
            counts["extra"] = dict(result.extra)
            counts["notes"] = list(result.notes)  # the comparison's (the read's stay in ``notes``)
            export.counts = counts
            export.fits = list(result.fits)
            export.set_aside = job.set_aside
            if job.by_comparison:
                _by_comparison(export, job.by_comparison)
            export.state = "compared"
            export.verdict = powerbi_results.verdict_code(export)
        else:
            export.state, export.verdict = "read", ""
        export.save(
            update_fields=["state", "verdict", "notes", "counts", "fits", "set_aside", "filters", "clauses"]
        )
        return written
    except MemoryError:
        raise  # past the job's memory limit: the whole comparison stops
    except Exception as exc:  # noqa: BLE001 - one export never stops the others; its class name only
        logger.warning(
            "Compare with Power BI %s: %s could not be compared (%s)",
            upload.pk,
            export.kind,
            type(exc).__name__,
        )
        PowerBIDifference.objects.filter(export=export).delete()
        PowerBIExport.objects.filter(pk=export.pk).update(
            state="failed", verdict="failed", error=words.STOPPED.format(cls=type(exc).__name__)[:300]
        )
        return 0


def _write_lines(export: PowerBIExport, result: powerbi_match.Result, room: int) -> int:
    """The export's lines, needs-a-look first, in batches: at most ``room`` (the export's limit, or the
    upload's lines left)."""
    batch: list[PowerBIDifference] = []
    n = 0
    for kind, group, reason, rule, detail, match, found, link in result.lines():
        if n >= room:
            break
        n += 1
        batch.append(
            PowerBIDifference(
                export=export,
                n=n,
                group=group,
                kind=kind,
                reason=reason,
                rule=rule,
                detail=detail,
                match=match,
                found=found,
                link=link,
            )
        )
        if len(batch) >= BATCH:
            PowerBIDifference.objects.bulk_create(batch)
            batch = []
    if batch:
        PowerBIDifference.objects.bulk_create(batch)
    return n


def _by_comparison(export: PowerBIExport, fields: list[str]) -> None:
    """A clause NeuroDB's page has no filter for, which the comparison applies itself (Program Performance's
    report status)."""
    parsed = powerbi_filters.Filter.from_json(export.filters)
    for clause in parsed.clauses:
        if clause.field in fields and clause.state == "applied":
            clause.state = "by_comparison"
    export.filters = parsed.to_json()
    export.clauses = [{"words": c.words, "state": c.state} for c in parsed.clauses]


def _summary(upload: PowerBIUpload) -> dict[str, Any]:
    """The counts the history shows: the exports recognised, read and their rows."""
    exports = list(exports_of(upload).values("state", "rows_read"))
    return {
        "exports": len(exports),
        "read": sum(1 for e in exports if e["state"] in READ_STATES),
        "failed": sum(1 for e in exports if e["state"] == "failed"),
        "rows": sum(e["rows_read"] for e in exports),
    }
