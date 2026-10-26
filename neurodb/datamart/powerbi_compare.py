"""The job of Compare with Power BI (``manage.py compare_powerbi --upload-id <pk>``, started in the background
by the upload page's *Upload and compare* and *Compare again*; never from a shell on the server).

1. It takes its own advisory lock (one comparison at a time) and moves the upload from queued to reading with
   a conditional update (an upload failed meanwhile, or done already, is left alone).
2. **Reading.** Each staged export's repacked copy is read into its rows (``powerbi_read.read``) and emptied
   at once; one export that cannot be read fails alone.
3. **The day** the statuses are read for: Power BI's refresh (the newest "Last Updated Date" or "Last Modify
   Date" in the files), else the upload's day; more than 14 days back, today, and the page says so. The
   partner reports sync NeuroDB's data comes from is noted, and Power BI's own fields a filter names are
   looked for in the records it kept (Raw eTools data's fields): NeuroDB's pages never read them, so they are
   applied as NeuroDB reads them, and the export's note says whether the records carry them.
4. A Datamart sync or a KM Database load started meanwhile stops it (the rows stay: *Compare again* works).
5. **Comparing**, export by export in Power BI's order (the report list and the Data Quality list first: the
   counted visuals are explained from them): an export whose filter names another country is not compared, an
   export with no row is "Empty in Power BI" (with the rows NeuroDB's page has for its filter); every other
   export is compared by its kind's comparer (``powerbi_kinds.COMPARERS``) in its own read-only snapshot of
   the data. Its lines are kept compactly during the snapshot, and every export's are written once all are
   compared, group by group across the exports (needs a look first, the expected ones last), at most
   :data:`LINE_LIMIT` per export and :data:`UPLOAD_LINE_LIMIT` per upload (the rest are counted). What an
   export read of NeuroDB's pages is let go before the next (:func:`_release`); an export that fails (out of
   memory too: with the job's memory peak and limit) is recorded once what it held was let go, and fails alone
   (:class:`Failed`). The comparison stops only when the job cannot even record that; the export it stopped
   at is then recorded as it stops (:func:`_stopped_during`).
6. The upload is done: its summary (a verdict per Power BI page, what to do now, the choices for the user,
   ``powerbi_results``), its memory peak and time are kept; uploads older than the newest :data:`KEEP_FULL`
   lose their rows and lines (retention, also applied when an upload is created).

This job writes the three Compare with Power BI tables and nothing else: no import run, no figure of any page.
Its logs carry counts, kinds and class names only, never a value from the files. A comparison that stops (an
export's, or the upload's) says the step it stopped at and what stopped it: the exception's class and, for a
database error, its SQLSTATE code (:func:`failure`), never a value, a query or the database's own message.
"""

from __future__ import annotations

import contextlib
import datetime
import gc
import logging
import os
import re
import resource
import time
import traceback
from dataclasses import dataclass
from typing import Any

from django.conf import settings
from django.db import DatabaseError, connection, transaction
from django.db.models import Q, QuerySet
from django.utils import timezone

from neurodb.integrations import background

from . import powerbi_exports as px
from . import (
    powerbi_facts,
    powerbi_filters,
    powerbi_kinds,
    powerbi_match,
    powerbi_page,
    powerbi_read,
    powerbi_xlsx,
)
from . import powerbi_words as words
from .models import PowerBIDifference, PowerBIExport, PowerBIUpload

logger = logging.getLogger(__name__)

LOCK_ID = background.POWERBI_COMPARE_LOCK_ID
QUEUE_LOCK_ID = background.POWERBI_QUEUE_LOCK_ID
HEAVY_LOCKS = (background.DATAMART_LOCK_ID, background.KM_LOAD_LOCK_ID)  # a comparison never runs beside them
COMMAND = "compare_powerbi"
QUEUED_FOR_AT_MOST = datetime.timedelta(minutes=2)  # a queued upload older than this never started
KEEP_FULL = 3  # the newest uploads that keep their rows and lines
LINE_LIMIT = 20_000  # lines written per export (the rest are counted)
UPLOAD_LINE_LIMIT = 60_000  # lines written per upload
MB = 1024 * 1024
MEMORY_LIMIT = 600 * MB  # the job's data limit at most: past it, a MemoryError (never the container stopped)
MEMORY_MARGIN = 150 * MB  # left to the web workers and the scheduler's jobs beside the comparison
MEMORY_FLOOR = 250 * MB  # the comparison's data limit at least (else it cannot start)
NICE = 10
MAX_REFUSED = 100
Status = PowerBIUpload.Status
BUSY_STATUSES = (Status.QUEUED, Status.READING, Status.COMPARING)
READ_STATES = ("read", "compared", "empty", "not_compared")  # an export whose rows NeuroDB read
RUN_SUMMARY = ("memory_limit_mb", "compared")  # what one run keeps in the summary as it runs
COMPARERS = powerbi_kinds.COMPARERS  # an export's kind -> the function that compares it with NeuroDB's page
BATCH = 1_000  # lines written at once
SQLSTATE = re.compile(r"[0-9A-Z]{5}")
AT_LIMIT = 0.9  # a memory peak this close to the job's data limit is at it
# where in a comparer an error stopped it: the first of these modules it went through, else its own matching.
# NeuroDB's page is also its pages' own modules (a comparer calls some itself: By location's rows); never
# ``reached``, which the comparers also use to read Power BI's own values
PAGE_MODULES = ("reporting", "monitoring", "funds", "services")
STEP_MODULES = {
    powerbi_page.__name__: "page",
    powerbi_facts.__name__: "facts",
    powerbi_read.__name__: "rows",
    **{f"{__name__.rpartition('.')[0]}.{name}": "page" for name in PAGE_MODULES},
}


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


class Busy(Exception):
    """Another upload waits to be compared, or is being compared: these files are not queued."""


def _queue_lock() -> None:
    """One request at a time queues an upload: the check that none is busy and the queueing are one step
    (held until the request's transaction ends)."""
    if connection.vendor != "postgresql":
        return
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [QUEUE_LOCK_ID])


def heavy_job_running() -> bool:
    """A Datamart sync or a KM Database load holds its lock now."""
    return any(background.lock_is_held(lock) for lock in HEAVY_LOCKS)


def limit_process() -> int:
    """The job's own process gives way to the web workers: a lower CPU priority, and a data limit past which a
    MemoryError fails an export (:class:`Failed`) instead of the container being stopped. The limit is
    what the container has left (:func:`memory_room`, less :data:`MEMORY_MARGIN`), at most
    :data:`MEMORY_LIMIT` and at least :data:`MEMORY_FLOOR`. Returns it, in bytes."""
    os.nice(NICE)
    _soft, hard = resource.getrlimit(resource.RLIMIT_DATA)
    limit = MEMORY_LIMIT
    room = memory_room()
    if room is not None:
        limit = max(MEMORY_FLOOR, min(MEMORY_LIMIT, _own_data() + room - MEMORY_MARGIN))
    if hard != resource.RLIM_INFINITY:
        limit = min(limit, hard)
    resource.setrlimit(resource.RLIMIT_DATA, (limit, hard))
    return limit


CGROUP_V2 = "/sys/fs/cgroup"
CGROUP_V1 = "/sys/fs/cgroup/memory"
NO_LIMIT = 1 << 60  # cgroup v1 writes a huge number for "no limit"
PROC_STATUS = "/proc/self/status"


def memory_room() -> int | None:
    """The memory the container has left (its cgroup's limit less what it uses, reclaimable page cache not
    counted), or None when the container has no limit or it cannot be read."""

    def read(path: str) -> str:
        with open(path, encoding="ascii") as found:
            return found.read().strip()

    def stat(path: str, name: str) -> int:
        for line in read(path).splitlines():
            key, _space, value = line.partition(" ")
            if key == name:
                return int(value)
        return 0

    try:
        if os.path.exists(f"{CGROUP_V2}/memory.max"):
            limit = read(f"{CGROUP_V2}/memory.max")
            if limit == "max":
                return None
            used = int(read(f"{CGROUP_V2}/memory.current"))
            cache = stat(f"{CGROUP_V2}/memory.stat", "inactive_file")
            total = int(limit)
        else:
            total = int(read(f"{CGROUP_V1}/memory.limit_in_bytes"))
            if total >= NO_LIMIT:
                return None
            used = int(read(f"{CGROUP_V1}/memory.usage_in_bytes"))
            cache = stat(f"{CGROUP_V1}/memory.stat", "total_inactive_file")
    except (OSError, ValueError):
        return None
    return max(0, total - (used - cache))


def _statuses(*names: str) -> dict[str, int]:
    """This process's memory figures in /proc (``VmData``, ``VmPeak``: kB), in bytes, read at once; a figure
    that cannot be read is 0."""
    found = dict.fromkeys(names, 0)
    try:
        with open(PROC_STATUS, encoding="ascii") as status:
            for line in status:
                name, _colon, rest = line.partition(":")
                if name in found:
                    found[name] = int(rest.split()[0]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return found


def _status(name: str) -> int:
    """One of this process's memory figures in /proc (:func:`_statuses`)."""
    return _statuses(name)[name]


def _own_data() -> int:
    """This process's data segment now (its VmData: what RLIMIT_DATA limits), in bytes."""
    return _status("VmData")


def _own_peak() -> int:
    """This process's own data segment at its highest since it started, in bytes: what its data limit limits.
    /proc keeps no peak of VmData itself: it is the address space's peak (VmPeak) less what of the address
    space is not data now (VmSize less VmData: code, libraries, stacks, which a process only adds to); never
    the resident memory's peak (VmHWM counts the libraries and leaves out data not yet touched: a run stopped
    at its limit of 250 MB had a VmHWM of 273 MB). Never ``ru_maxrss`` while /proc can be read: Linux keeps
    the parent's peak in it across exec, and the job is started by a web worker that may have held hundreds
    of MB (Partner monitoring's read), while exec gives the job a memory of its own."""
    found = _statuses("VmPeak", "VmSize", "VmData")
    if all(found.values()):
        return max(found["VmPeak"] - (found["VmSize"] - found["VmData"]), found["VmData"])
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024  # KB on Linux


def _limit() -> int | None:
    """The job's data limit (:func:`limit_process`), in bytes; None when it runs with none."""
    soft, _hard = resource.getrlimit(resource.RLIMIT_DATA)
    return None if soft == resource.RLIM_INFINITY or soft <= 0 else soft


def memory() -> dict[str, Any]:
    """The job's memory as a failure keeps it: its data peak (:func:`_own_peak`) and its data limit
    (:func:`_limit`; None: none), in MB."""
    limit = _limit()
    return {"peak_mb": round(_own_peak() / MB), "limit_mb": round(limit / MB) if limit else None}


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
    staged ones with their repacked copy. Raises :class:`Busy` when another upload is busy: the check and the
    queueing are one step (:func:`_queue_lock`), so two uploads at once never queue two comparisons."""
    now = timezone.now()
    with transaction.atomic():
        _queue_lock()
        if PowerBIUpload.objects.filter(status__in=BUSY_STATUSES).exists():
            raise Busy
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
    export.clauses = _clauses(found.filter)
    export.notes = list(found.notes)
    export.state = "read"


def _clauses(parsed: powerbi_filters.Filter) -> list[dict[str, str]]:
    """The clauses as the pages show them: their words and how NeuroDB applies each."""
    return [{"words": c.words, "state": c.state} for c in parsed.clauses]


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
    it waits for the job again (queued now). Raises :class:`Busy` when another upload is busy (checked and
    queued in one step, as :func:`create`)."""
    with transaction.atomic():
        _queue_lock()
        if PowerBIUpload.objects.filter(status__in=BUSY_STATUSES).exclude(pk=upload.pk).exists():
            raise Busy
        locked = PowerBIUpload.objects.select_for_update().filter(pk=upload.pk).first()
        if locked is None or not can_compare_again(locked):
            return False
        exports = PowerBIExport.objects.filter(upload=locked)
        PowerBIDifference.objects.filter(export__upload=locked).delete()
        exports.exclude(rows=None).update(state="read", counts={}, fits=[], verdict="", set_aside=0, error="")
        # the earlier run's memory and time go with its results: a run that never measures its own (stopped
        # by a restart, refused by the lock) never shows the earlier peak, nor beside its own limit
        summary = {k: v for k, v in (locked.summary or {}).items() if k not in RUN_SUMMARY}
        PowerBIUpload.objects.filter(pk=locked.pk).update(
            status=Status.QUEUED,
            queued_at=timezone.now(),
            error="",
            started_at=None,
            finished_at=None,
            compared_at=None,
            sync_moved=False,
            choices=[],
            summary=summary,
            peak_memory_mb=None,
            seconds=None,
        )
    return True


def retention() -> int:
    """The uploads older than the newest :data:`KEEP_FULL` lose their rows, copies and lines (their summary,
    counts and notes stay). An upload waiting to be compared, or being compared, is never trimmed and never
    counted among the newest: its job trims the others once it ends. Returns how many were trimmed."""
    settled = PowerBIUpload.objects.exclude(status__in=BUSY_STATUSES)
    keep = list(settled.order_by("-uploaded_at", "-id").values_list("pk", flat=True)[:KEEP_FULL])
    older = list(settled.exclude(pk__in=keep).filter(trimmed=False).values_list("pk", flat=True))
    if not older:
        return 0
    PowerBIDifference.objects.filter(export__upload_id__in=older).delete()
    PowerBIExport.objects.filter(upload_id__in=older).update(rows=None, content=None)
    PowerBIUpload.objects.filter(pk__in=older).update(trimmed=True)
    return len(older)


# ------------------------------------------------------------------------------------------------- the run
def run(pk: int, memory_limit: int | None = None) -> PowerBIUpload | None:
    """Compare upload ``pk``. Returns the upload as it ended (done or failed), or None when there was nothing
    to do (it is not waiting). ``memory_limit``: the data limit the job runs under (:func:`limit_process`),
    kept in the upload's summary beside its memory peak."""
    if not _lock():
        error = words.LOCKED if _staged_left(pk) else words.LOCKED_AGAIN
        if PowerBIUpload.objects.filter(pk=pk, status=Status.QUEUED).update(
            status=Status.FAILED, error=error, finished_at=timezone.now()
        ):
            logger.warning("Compare with Power BI %s: another comparison holds the lock", pk)
            _empty_content(pk)  # only an upload this call failed: another may be reading its own
        return PowerBIUpload.objects.filter(pk=pk).first()
    started = time.monotonic()
    step, visual = "starting", ""
    current: PowerBIExport | None = None  # the export being compared
    try:
        close_stale(ours=pk)
        if PowerBIUpload.objects.filter(pk=pk, status=Status.QUEUED, trimmed=True).update(
            status=Status.FAILED, error=words.TRIMMED_QUEUED, finished_at=timezone.now()
        ):  # its rows were removed meanwhile: never compared against nothing
            logger.warning("Compare with Power BI %s: its rows were removed before it started", pk)
            return PowerBIUpload.objects.filter(pk=pk).first()
        if not PowerBIUpload.objects.filter(pk=pk, status=Status.QUEUED).update(
            status=Status.READING, started_at=timezone.now(), peak_memory_mb=None, seconds=None
        ):
            logger.info("Compare with Power BI %s: not waiting, nothing to do", pk)
            return None
        upload = PowerBIUpload.objects.get(pk=pk)
        if memory_limit:  # kept from the start: a comparison that stops says it too
            upload.summary = {**(upload.summary or {}), "memory_limit_mb": round(memory_limit / MB)}
            PowerBIUpload.objects.filter(pk=pk).update(summary=upload.summary)
        step = "reading"
        _read_staged(upload)
        step = "day"
        before = _partner_reports_run()
        _day(upload, before)
        _record_fields(upload, before)
        if heavy_job_running():
            return _fail(upload, words.OTHER_JOB_LATER, started)
        if not PowerBIUpload.objects.filter(pk=pk, status=Status.READING).update(status=Status.COMPARING):
            return PowerBIUpload.objects.filter(pk=pk).first()
        step = "preparing"
        context = _context(upload)
        compared = []
        for n, export in enumerate(_in_order(exports_of(upload)), start=1):
            step, visual, current = "comparing", _visual(export.kind), export
            result = _compare(export, upload, context)
            _release(context)
            if isinstance(result, Failed):  # recorded once what it held was let go
                _record_failure(export, upload.pk, result.found)
            elif result is not None:
                compared.append((export, result))
            _progress(upload, n)
        step = "lines"
        _write_lines(compared)
        step = "summary"
        after = _partner_reports_run()
        upload.sync_moved = _run_id(before) != _run_id(after)
        from . import powerbi_results

        summary = {**(upload.summary or {}), **_summary(upload)}
        summary.pop("compared", None)
        # the choices first: the summary's verdicts point to Choices for you only when one is about them
        upload.choices = powerbi_results.choices(list(exports_of(upload)))
        upload.summary = {**summary, **powerbi_results.summary(upload, list(exports_of(upload)))}
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
    except Exception as exc:  # noqa: BLE001 - the upload keeps our sentence, its step and class (and code)
        found = failure(exc, step)
        logger.warning("Compare with Power BI %s stopped at %s (%s)", pk, step, _logged(found))
        upload = PowerBIUpload.objects.filter(pk=pk).first()
        if upload is None:
            return None
        if step == "comparing" and current is not None:
            _stopped_during(current, pk, exc, found)
        error = words.STOPPED_UPLOAD if _staged_left(pk) else words.STOPPED_AGAIN  # its copies are emptied
        where = words.UPLOAD_STEPS[step].format(visual=visual)
        return _fail(upload, error.format(step=where, what=words.failure(found)), started)
    finally:
        try:
            _empty_content(pk)
            retention()
        finally:
            _unlock()


def failure(exc: BaseException, step: str) -> dict[str, Any]:
    """What stopped a step (``powerbi_words.failure`` says it): the step, the exception's class and, for a
    database error, its SQLSTATE code ("" when it has none: an error of the database client itself, said to be
    at the job's memory limit when its memory peak reached it); out of memory (a MemoryError, or that error at
    the limit), the job's memory peak and limit (:func:`memory`). Never a value, a query or the database's own
    message."""
    found: dict[str, Any] = {"step": step, "cls": type(exc).__name__}
    if isinstance(exc, MemoryError):
        found.update(memory())
    elif isinstance(exc, DatabaseError) or hasattr(exc, "sqlstate"):
        found["code"] = sqlstate(exc)
        if not found["code"] and at_memory_limit():
            found["memory"] = True
            found.update(memory())
    return found


def sqlstate(exc: BaseException) -> str:
    """A database error's SQLSTATE code: its own, else that of the driver's error it wraps ("" when none)."""
    current: BaseException | None = exc
    for _depth in range(5):
        if current is None:
            break
        code = getattr(current, "sqlstate", None)
        if isinstance(code, str) and SQLSTATE.fullmatch(code):
            return code
        current = current.__cause__
    return ""


def at_memory_limit() -> bool:
    """The job's own memory peak (:func:`_own_peak`) or its data now reached its data limit
    (:func:`limit_process`), or nearly (False: no limit)."""
    soft = _limit()
    if soft is None:
        return False
    return max(_own_peak(), _own_data()) >= soft * AT_LIMIT


def _logged(found: dict[str, Any]) -> str:
    """A failure as the logs keep it: the class and the SQLSTATE code only."""
    return " ".join(x for x in (found["cls"], found.get("code") or "") if x)


def _visual(kind: str) -> str:
    """An export as a failed upload names it: its visual, or its Power BI page when the visual is the page's
    only one, a bare "Table" (Program Performance, PD/SPD Indicator, ...)."""
    found = px.KINDS.get(kind)
    if not found:
        return kind
    page_name, visual = found.visual
    return page_name if visual == "Table" else visual


def _step_of(exc: BaseException) -> str:
    """Where a comparer stopped: NeuroDB's page (``powerbi_page``, or a page's own module the comparer called
    itself), the facts behind the reasons (``powerbi_facts``), the export's rows (``powerbi_read``): the first
    of them the error went through (:data:`STEP_MODULES`), else the comparer's own matching."""
    for frame, _line in traceback.walk_tb(exc.__traceback__):
        step = STEP_MODULES.get(frame.f_globals.get("__name__", ""))
        if step:
            return step
    return "matching"


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
    upload.peak_memory_mb = round(_own_peak() / MB, 1)
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
        except Exception as exc:  # noqa: BLE001 - one export never stops the others; its class (and code) only
            found = failure(exc, "reading")
            logger.warning(
                "Compare with Power BI %s: an export could not be read (%s)", upload.pk, _logged(found)
            )
            PowerBIExport.objects.filter(pk=export.pk).update(
                state="failed",
                content=None,
                error=words.COULD_NOT_READ.format(name=export.file_name, cls=words.failure(found))[:300],
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
    # the moments, as each row's reason reads them (powerbi_facts.Context)
    if upload.powerbi_as_of and upload.synced_at and upload.synced_at < upload.powerbi_as_of:
        notes.append(words.SYNC_BEFORE)
    upload.summary = {**(upload.summary or {}), "notes": notes}


def fields_in_records(run: Any) -> set[str]:
    """Power BI's own fields every partner report record of ``run`` carries (Raw eTools data's fields)."""
    from . import raw

    kept = raw.keys_kept(run, "partner_reports")
    most = max((counts[0] for counts in kept.values() if counts), default=0)
    return {f for f in powerbi_filters.RECORD_FIELDS if most and (kept.get(f) or [0])[0] == most}


def _record_fields(upload: PowerBIUpload, run: Any) -> None:
    """Each clause on one of Power BI's own fields is applied as NeuroDB reads it: none of NeuroDB's pages
    reads those fields, even where the records carry them, and the comparison compares with the pages. The
    export's note says whether the records carry the field (Raw eTools data's fields)."""
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
            sentence = words.IN_RECORDS if clause.field in carried else words.NOT_IN_RECORDS
            notes.append(sentence.format(field=clause.field, words=words.AS_READ[clause.field]))
            changed = changed or clause.state != "as_read"
            clause.state = "as_read"
        if changed or notes != export.notes:
            export.filters = parsed.to_json()
            export.clauses = _clauses(parsed)
            export.notes = notes
            export.save(update_fields=["filters", "clauses", "notes"])


def _context(upload: PowerBIUpload) -> powerbi_facts.Context:
    """What every export of the comparison shares: the day, NeuroDB's history, Power BI's refresh and the sync
    (their dates and their moments), the people cleaner's names and the section names NeuroDB knows."""
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
        refreshed_at=upload.powerbi_as_of,
        synced_at=upload.synced_at,
        names=people.known_names(),
        sections=tuple(known_sections()),
    )


def _in_order(exports: QuerySet[PowerBIExport]) -> list[PowerBIExport]:
    """Power BI's order, the report list and the Data Quality list first (the counted visuals and the card are
    explained from them)."""
    found = list(exports)
    first = [e for kind in powerbi_kinds.FIRST for e in found if e.kind == kind]
    return first + [e for e in found if e.kind not in powerbi_kinds.FIRST]


@dataclass(frozen=True)
class Failed:
    """An export whose comparison failed (:func:`_compare`): what stopped it (:func:`failure`). The job
    records it once what the export held was let go (the comparer's frames, then :func:`_release`), and it
    alone could not be compared (out of memory: "not enough memory", with the job's memory peak and limit);
    the next export is compared, and fails alone the same way if memory is short still. Nothing is taken
    first to judge whether the memory was freed: the heap keeps the address space it freed (the job's data
    does not shrink, and its data limit counts it), so a fresh block of a few MB is refused while the next
    export's own objects still fit in what was freed. The comparison stops only when the job cannot even
    record the failure (:func:`_stopped_during`)."""

    found: dict[str, Any]


def _stopped_during(export: PowerBIExport, upload_pk: int, exc: BaseException, found: dict[str, Any]) -> None:
    """The export a stopped comparison was comparing, when nothing recorded it yet (the job could not record
    its failure, or the error came before its comparer ran): it could not be compared, as an export that fails
    alone, never "stopped before it" (the upload names it). One compared or recorded already is left as it is.
    Best effort: the upload's own failure is recorded whatever happens here."""
    with contextlib.suppress(Exception):
        if not PowerBIExport.objects.filter(pk=export.pk, state__in=("staged", "read")).exists():
            return
        if found["cls"] != "MemoryError":  # an export's step ("comparing" is the upload's)
            found = {**found, "step": _step_of(exc)}
        _record_failure(export, upload_pk, found)


def _release(context: powerbi_facts.Context) -> None:
    """What an export read of NeuroDB's pages let go before the next export (``Context.release``), and what
    its objects still tie up in cycles (a full collection): no export's data stays alive into the next."""
    context.release()
    gc.collect()


def _progress(upload: PowerBIUpload, n: int) -> None:
    """How many exports are compared so far (the upload's page says "Comparing… n of 13 exports")."""
    summary = {**(upload.summary or {}), "compared": n}
    upload.summary = summary
    PowerBIUpload.objects.filter(pk=upload.pk).update(summary=summary)


def _compare(
    export: PowerBIExport, upload: PowerBIUpload, context: powerbi_facts.Context
) -> powerbi_match.Result | Failed | None:
    """One export against NeuroDB, in its own snapshot of the data; one that fails leaves the others. Returns
    its result, whose lines are written once every export is compared (:func:`_write_lines`), None (no
    comparer ran), or :class:`Failed` (the job then lets go of what the export held and records it)."""
    from . import powerbi_results, raw

    if export.state == "failed":
        return None
    # the clauses this kind's comparer does not apply are ones NeuroDB cannot apply to this export
    parsed = powerbi_kinds.clause_states(export.kind, powerbi_filters.Filter.from_json(export.filters))
    export.filters = parsed.to_json()
    export.clauses = _clauses(parsed)
    step = "rows"
    try:
        result = None
        if parsed.country_ok is False:
            export.state, export.verdict = "not_compared", "other_country"
            note = words.OTHER_COUNTRY.format(country=settings.ETOOLS_DATAMART_COUNTRY)
            export.notes = [*export.notes, note] if note not in export.notes else export.notes
        elif export.rows_read == 0:
            step = "page"
            with raw.snapshot(raw.STREAM_TIMEOUT_MS):
                count = powerbi_kinds.neurodb_count(export.kind, parsed, context)
            export.state, export.verdict = "empty", "empty"
            export.counts = {"neurodb_rows": count}
        elif export.kind in COMPARERS:
            data = PowerBIExport.objects.filter(pk=export.pk).values_list("rows", flat=True).first()
            if not data:  # its rows were removed (retention): never compared against nothing
                raise LookupError("rows")
            result = powerbi_match.Result(limit=LINE_LIMIT)
            job = powerbi_kinds.Job(
                kind=px.KINDS[export.kind],
                columns=list(export.columns),
                data=bytes(data),
                parsed=parsed,
                context=context,
                result=result,
                rows_read=export.rows_read,
            )
            step = "comparing"
            with raw.snapshot(raw.STREAM_TIMEOUT_MS):
                COMPARERS[export.kind](job)
            step = "saving"
            result.close()
            counts = result.summary()
            counts["written"] = 0
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
        step = "saving"
        export.save(
            update_fields=["state", "verdict", "notes", "counts", "fits", "set_aside", "filters", "clauses"]
        )
        return result
    except Exception as exc:  # noqa: BLE001 - one export never stops the others; its step and class only
        if step == "comparing" and not isinstance(exc, MemoryError):  # out of memory: no step (failure)
            step = _step_of(exc)
        traceback.clear_frames(exc.__traceback__)  # the comparer's frames let go of what they held
        return Failed(failure(exc, step))


def _failed(export: PowerBIExport, upload_pk: int, exc: Exception, step: str) -> None:
    """An export whose lines could not be written (:func:`_write_lines`), recorded as one that could not be
    compared (:func:`_record_failure`)."""
    _record_failure(export, upload_pk, failure(exc, step))


def _record_failure(export: PowerBIExport, upload_pk: int, found: dict[str, Any]) -> None:
    """An export that could not be compared (:class:`Failed`), or its lines not written: its lines and counts
    removed (never a difference to look at that no line shows); the step it stopped at and what stopped it
    (:func:`failure`), kept in its counts for its verdict; out of memory, without its step (the export's whole
    comparison took the memory) but with the job's memory peak and limit."""
    logger.warning(
        "Compare with Power BI %s: %s could not be compared at %s (%s)",
        upload_pk,
        export.kind,
        found["step"],
        _logged(found),
    )
    PowerBIDifference.objects.filter(export=export).delete()
    if found["cls"] == "MemoryError":
        error = words.STOPPED_MEMORY.format(what=words.out_of_memory(found))
    else:
        error = words.STOPPED.format(step=words.EXPORT_STEPS[found["step"]], what=words.failure(found))
    PowerBIExport.objects.filter(pk=export.pk).update(
        state="failed", verdict="failed", error=error[:300], counts={"failure": found}, fits=[]
    )


def _write_lines(compared: list[tuple[PowerBIExport, powerbi_match.Result]]) -> int:
    """Every compared export's lines, group by group across the exports: every export's lines that need a
    look first (in Power BI's order), then those to check in eTools, those waiting for the sync, and the
    expected ones last, at most :data:`LINE_LIMIT` per export and :data:`UPLOAD_LINE_LIMIT` per upload (so
    the limit drops expected lines first). An export whose lines are not all written says how many are
    counted only. Returns the lines written."""
    left = UPLOAD_LINE_LIMIT
    written: dict[int, dict[int, int]] = {export.pk: {} for export, _result in compared}
    failed: set[int] = set()
    for group in powerbi_match.GROUP_ORDER:
        for export, result in compared:
            if export.pk in failed:
                continue
            mine = written[export.pk]
            room = max(0, min(left, LINE_LIMIT - sum(mine.values())))
            try:
                n = _write_group(export, result, group, room, sum(mine.values()))
            except MemoryError:
                raise  # past the job's memory limit: the whole comparison stops
            except Exception as exc:  # noqa: BLE001 - one export never stops the others; its step and class only
                failed.add(export.pk)
                _failed(export, export.upload_id, exc, "lines")
                continue
            if n:
                mine[group] = n
            left -= n
    for export, _result in compared:
        if export.pk in failed:
            continue
        counts = dict(export.counts or {})
        total = sum(written[export.pk].values())
        counts["written"] = total
        counts["written_groups"] = {str(g): n for g, n in sorted(written[export.pk].items())}
        if counts.get("lines", 0) > total:
            note = words.NOTE_LINES_CUT.format(n=counts["lines"] - total)
            counts["notes"] = [*(counts.get("notes") or []), note]
        export.counts = counts
        PowerBIExport.objects.filter(pk=export.pk).update(counts=counts)
    return sum(sum(found.values()) for pk, found in written.items() if pk not in failed)


def _write_group(
    export: PowerBIExport, result: powerbi_match.Result, group: int, room: int, start: int
) -> int:
    """One group's lines of an export, in batches, numbered on from ``start`` (an export's lines are read in
    the order they are numbered: needs a look first): at most ``room``; the rest are let go."""
    batch: list[PowerBIDifference] = []
    n = 0
    for kind, line_group, reason, rule, detail, match, found, link in result.take(group):
        if n >= room:
            break
        n += 1
        batch.append(
            PowerBIDifference(
                export=export,
                n=start + n,
                group=line_group,
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
    export.clauses = _clauses(parsed)


def _summary(upload: PowerBIUpload) -> dict[str, Any]:
    """The counts the history shows: the exports recognised, read and their rows."""
    exports = list(exports_of(upload).values("state", "rows_read"))
    return {
        "exports": len(exports),
        "read": sum(1 for e in exports if e["state"] in READ_STATES),
        "failed": sum(1 for e in exports if e["state"] == "failed"),
        "rows": sum(e["rows_read"] for e in exports),
    }
