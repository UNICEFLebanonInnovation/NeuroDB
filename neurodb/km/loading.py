"""The load of an uploaded KM workbook (``manage.py load_km_database --version-id <pk>``, started in the
background by the admin's upload page).

1. It takes its own advisory lock (one load at a time) and moves the version from queued to loading with a
   conditional update (a version failed meanwhile, or loaded already, is left alone).
2. Pass 1 reads every row once and checks them, with no database access (:func:`workbook.check_rows`).
3. The fewer-rows guard: a workbook with under 90% of the shown version's findings or documents, or fewer
   rows than its Read Me sheet states, is refused unless the uploader ticked "It has fewer findings on
   purpose".
4. With no problem, one transaction deletes every KM document and finding, writes the documents, then the
   findings 1,000 at a time as pass 2 reads them again, fills the search vectors and makes the version live.
   Readers see the old rows until the commit (no TRUNCATE, which would lock them out).

A failed or stopped load leaves the version shown as it was. The uploaded file is emptied when the load ends,
loaded or not. This job is not an import run (``SyncRun``): it writes the KM tables and nothing else.
"""

from __future__ import annotations

import datetime
import logging
import resource
import time

from django.contrib.postgres.search import SearchVector
from django.db import DatabaseError, connection, transaction
from django.utils import timezone

from neurodb.integrations import background

from . import columns as cols
from . import words, workbook
from .models import KMDocument, KMFinding, KMVersion, forget_live
from .words import counted, say

logger = logging.getLogger(__name__)

LOCK_ID = background.KM_LOAD_LOCK_ID
QUEUED_FOR_AT_MOST = datetime.timedelta(minutes=2)  # a queued version older than this never started
FEWER_SHARE = 0.9  # under this share of the shown version's rows, the load is refused unless ticked
DOCUMENT_BATCH = 500
FINDING_BATCH = 1_000
Status = KMVersion.Status


class Removed(Exception):
    """The version's row was deleted while it loaded: the transaction is rolled back."""


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
        logger.warning("KM Database load: the lock could not be released", exc_info=True)


def close_stale(*, ours: int | None = None) -> int:
    """Fail the versions whose load cannot be running: a loading version while nobody holds the load's lock
    (the job holds it for the whole load, so the process stopped), and a queued version older than two
    minutes while nobody holds it (the job never started). ``ours``: the version of the load that called
    this while holding the lock itself (every other loading version is then stale). Returns how many."""
    now = timezone.now()
    if ours is None and background.lock_is_held(LOCK_ID):
        return 0
    loading = KMVersion.objects.filter(status=Status.LOADING)
    queued = KMVersion.objects.filter(status=Status.QUEUED, uploaded_at__lt=now - QUEUED_FOR_AT_MOST)
    if ours is not None:
        loading, queued = loading.exclude(pk=ours), queued.exclude(pk=ours)
    closed = loading.update(status=Status.FAILED, error=str(words.RESTARTED), content=None, finished_at=now)
    closed += queued.update(status=Status.FAILED, error=str(words.NOT_STARTED), content=None, finished_at=now)
    if closed:
        logger.warning("KM Database: closed %s version(s) whose load had stopped", closed)
    return closed


def busy() -> KMVersion | None:
    """The version waiting to load or loading, if any (after :func:`close_stale`)."""
    return KMVersion.objects.defer("content").filter(status__in=(Status.QUEUED, Status.LOADING)).first()


# ------------------------------------------------------------------------------------------------- the run
def run(pk: int, fewer_ok: bool = False) -> KMVersion | None:
    """Load version ``pk``. Returns the version as it ended (live or failed), or None when there was nothing
    to do (it is not waiting to load)."""
    if not _lock():
        if KMVersion.objects.filter(pk=pk, status=Status.QUEUED).update(
            status=Status.FAILED, error=str(words.LOCKED), content=None, finished_at=timezone.now()
        ):
            logger.warning("KM Database load %s: another load holds the lock", pk)
        return KMVersion.objects.defer("content").filter(pk=pk).first()
    try:
        close_stale(ours=pk)
        started = time.monotonic()
        if not KMVersion.objects.filter(pk=pk, status=Status.QUEUED).update(
            status=Status.LOADING, started_at=timezone.now()
        ):
            logger.info("KM Database load %s: not waiting to load, nothing to do", pk)
            return None
        version = KMVersion.objects.get(pk=pk)
        fewer_ok = fewer_ok or version.fewer_ok
        content = bytes(version.content or b"")
        try:
            report = workbook.check_rows(content)
        except Exception as exc:  # noqa: BLE001 - a file that cannot be read again fails its version
            logger.exception("KM Database load %s: the workbook could not be read", pk)
            return _fail(version, error=say(words.STOPPED, error=type(exc).__name__))
        problems = report.problems or _fewer(report, fewer_ok)
        _summary(version, report, fewer_ok)
        if problems:
            return _fail(version, problems=problems)
        try:
            _replace(version, report, content, started)
        except Exception as exc:
            logger.exception("KM Database load %s stopped", pk)
            return _fail(version, error=say(words.STOPPED, error=type(exc).__name__))
        forget_live()
        return KMVersion.objects.defer("content").get(pk=pk)
    finally:
        _unlock()


def _summary(version: KMVersion, report: workbook.Report, fewer_ok: bool) -> None:
    """What the first pass learnt, kept on the version whether it loads or not."""
    version.prepared_on = report.read_me.prepared_on
    version.source_name = report.read_me.source_name
    version.stated_documents = report.read_me.stated.get(cols.DOCUMENTS)
    version.stated_findings = report.read_me.stated.get(cols.FINDINGS)
    version.documents_count = len(report.documents)
    version.findings_count = report.findings
    version.review_count = report.review_findings + report.review_documents
    version.check_count = report.check_findings + report.check_documents
    version.warnings = (_stated_warnings(report, fewer_ok) + report.warnings)[: workbook.MAX_MESSAGES]


def _rows_of(report: workbook.Report) -> dict[str, int]:
    return {cols.DOCUMENTS: len(report.documents), cols.FINDINGS: report.findings}


WHAT = {cols.DOCUMENTS: words.DOCUMENTS_N, cols.FINDINGS: words.FINDINGS_N}


def _fewer(report: workbook.Report, fewer_ok: bool) -> list[str]:
    """The fewer-rows guard, checked before anything is written."""
    if fewer_ok:
        return []
    problems = []
    rows = _rows_of(report)
    shown = KMVersion.objects.defer("content").filter(status=Status.LIVE).first()
    if shown is not None:
        for kind, words_, before in (
            (cols.FINDINGS, words.FEWER_FINDINGS, shown.findings_count),
            (cols.DOCUMENTS, words.FEWER_DOCUMENTS, shown.documents_count),
        ):
            if rows[kind] < FEWER_SHARE * before:
                problems.append(say(words_, n=rows[kind], m=f"{before:,}", fewer=words.FEWER_LABEL))
    for kind in (cols.FINDINGS, cols.DOCUMENTS):
        stated = report.read_me.stated.get(kind)
        if stated is not None and rows[kind] < stated:
            problems.append(
                say(
                    words.FEWER_THAN_STATED,
                    stated=counted(stated, WHAT[kind]),
                    sheet=report.sheets[kind].title,
                    rows=counted(rows[kind], words.ROWS_N),
                    fewer=words.FEWER_LABEL,
                )
            )
    return problems


def _stated_warnings(report: workbook.Report, fewer_ok: bool) -> list[str]:
    """The Read Me's stated counts that differ from the rows: fewer than the rows, or more when the uploader
    ticked "It has fewer findings on purpose" (otherwise more is a problem, :func:`_fewer`)."""
    found = []
    rows = _rows_of(report)
    for kind in (cols.FINDINGS, cols.DOCUMENTS):
        stated = report.read_me.stated.get(kind)
        if stated is None or not report.sheets.get(kind) or stated == rows[kind]:
            continue
        if stated < rows[kind] or fewer_ok:
            found.append(
                say(
                    words.STATED_DIFFERS,
                    stated=counted(stated, WHAT[kind]),
                    sheet=report.sheets[kind].title,
                    rows=counted(rows[kind], words.ROWS_N),
                )
            )
    return found


SUMMARY_FIELDS = (
    "prepared_on",
    "source_name",
    "stated_documents",
    "stated_findings",
    "documents_count",
    "findings_count",
    "review_count",
    "check_count",
    "warnings",
)


def _fail(version: KMVersion, *, problems: list[str] | None = None, error: str = "") -> KMVersion:
    """The version failed: its problems or error kept, its file emptied. The version shown is untouched."""
    version.status = Status.FAILED
    version.problems = (problems or [])[: workbook.MAX_MESSAGES]
    version.error = error
    version.content = None
    version.finished_at = timezone.now()
    version.peak_memory_mb = _peak_mb()
    fields = (*SUMMARY_FIELDS, "status", "problems", "error", "content", "finished_at", "peak_memory_mb")
    try:  # update_fields: a row removed meanwhile is not written back
        version.save(update_fields=fields)
    except DatabaseError:
        logger.warning("KM Database load %s: the version was removed while it loaded", version.pk)
    return version


def _peak_mb() -> int:
    return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024)  # kilobytes on Linux


def _replace(version: KMVersion, report: workbook.Report, content: bytes, started: float) -> None:
    with transaction.atomic():
        KMFinding.objects.all().delete()
        KMDocument.objects.all().delete()
        KMDocument.objects.bulk_create(
            [KMDocument(position=n, checks=c.checks, **c.values) for n, c in report.documents],
            batch_size=DOCUMENT_BATCH,
        )
        documents = dict(KMDocument.objects.values_list("record_id", "pk"))
        types = dict(KMDocument.objects.values_list("record_id", "study_type"))
        batch: list[KMFinding] = []
        for number, cells in workbook.findings(content, report.sheets):
            record = cells.values["record_id"]
            batch.append(
                KMFinding(
                    position=number,
                    document_id=documents[record],
                    document_type=types[record],
                    checks=cells.checks,
                    **cells.values,
                )
            )
            if len(batch) >= FINDING_BATCH:
                KMFinding.objects.bulk_create(batch)
                batch = []
        if batch:
            KMFinding.objects.bulk_create(batch)
        _fill_vectors()
        KMVersion.objects.filter(status=Status.LIVE).update(status=Status.REPLACED)
        written = KMVersion.objects.filter(pk=version.pk, status=Status.LOADING).update(
            **{name: getattr(version, name) for name in SUMMARY_FIELDS},
            status=Status.LIVE,
            problems=[],
            error="",
            content=None,
            finished_at=timezone.now(),
            peak_memory_mb=_peak_mb(),
            load_seconds=round(time.monotonic() - started),
        )
        if not written:
            raise Removed(str(words.REMOVED))


def _fill_vectors() -> None:
    """The search vectors, filled in the database: for the page's search box every searchable text, for the
    assistant only the finding's own words (not its document's methods, population or titles, which every
    finding of the document shares). Never the authors, the marks or the IDs."""
    eight = (
        "key_finding",
        "measure",
        "document_title",
        "item_title",
        "analysis",
        "population",
        "methods",
        "limitations",
    )
    KMFinding.objects.update(
        search_vector=SearchVector("key_finding", weight="A", config="english")
        + SearchVector("measure", "document_title", "item_title", weight="B", config="english")
        + SearchVector("analysis", "population", "methods", "limitations", weight="C", config="english")
        + SearchVector(*eight, weight="D", config="simple"),
        own_vector=SearchVector("key_finding", weight="A", config="english")
        + SearchVector("measure", "analysis", weight="B", config="english")
        + SearchVector("limitations", weight="C", config="english")
        + SearchVector("sub_question", "sub_sub_question", "indicator", weight="D", config="english"),
    )
    KMDocument.objects.update(
        search_vector=SearchVector("title", "item_title", weight="A", config="english")
        + SearchVector("title", "item_title", weight="D", config="simple")
    )
