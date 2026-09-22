"""ActivityInfo data import: extract file -> ``ActivityReportNew`` rows for one database.

Ports ``pivoting/utils.py`` ``import_data_via_r_script`` / ``read_data_from_file`` /
``add_rows`` (and ``add_rows_temp`` for ``have_offices`` databases, see ``rows.py``): the export
job is run, the extract parsed, all existing rows of the database (by ``database_ai_id``, as v2)
are deleted and the new ones inserted.

Fixed v2 defects: delete followed by one ``create`` per row without a transaction (~52k rows) ->
delete + ``bulk_create(batch_size=2000)`` inside ``transaction.atomic()``; the extract written
into the package directory -> a temporary directory plus an optional copy in ``default_storage``
under ``extracts/<ai_id>/<date>.csv``; ``Database.last_monthly_update_date`` stamped before the
import (``pivoting/tasks.py:33``) -> stamped only after the transaction commits; per-row
failures counted and reported on the ``SyncRun`` instead of a ``print``.
"""

from __future__ import annotations

import datetime as dt
import logging
import tempfile
from pathlib import Path
from typing import Any

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import transaction
from django.utils import timezone

from neurodb.core.models import SyncRun
from neurodb.facts.models import ActivityReportNew
from neurodb.indicators.models import Database
from neurodb.integrations.activityinfo.client import ActivityInfoClient
from neurodb.integrations.activityinfo.rows import (
    RowError,
    extraction_month,
    iter_extract,
    parse_row,
    reporting_year_of,
)
from neurodb.integrations.runs import fail, finish_by_counts

logger = logging.getLogger(__name__)

BATCH_SIZE = 2000
LOG_FAILURE_SAMPLE = 20


def store_extract(database: Database, extract: bytes, *, keep_copy: bool = True) -> dict[str, str]:
    """Write the extract to a temp dir and, when ``keep_copy``, to ``default_storage``."""
    paths: dict[str, str] = {}
    tmp_dir = Path(tempfile.mkdtemp(prefix=f"ai_{database.ai_id}_"))
    tmp_file = tmp_dir / f"{database.ai_id}_ai_data.txt"
    tmp_file.write_bytes(extract)
    paths["temp_path"] = str(tmp_file)
    if keep_copy:
        name = f"extracts/{database.ai_id}/{dt.date.today():%Y-%m-%d}.csv"
        try:
            paths["storage_path"] = default_storage.save(name, ContentFile(extract))
        except Exception as exc:
            logger.warning("extract copy for %s not stored: %s", database.ai_id, type(exc).__name__)
    return paths


def build_reports(
    database: Database, extract: bytes, *, today: dt.date | None = None
) -> tuple[list[ActivityReportNew], dict[str, int]]:
    """Parse the extract into unsaved ``ActivityReportNew`` objects plus row counters."""
    counts = {"rows_in": 0, "rows_skipped": 0, "rows_failed": 0}
    month = extraction_month(database, today)
    reports: list[ActivityReportNew] = []
    for row in iter_extract(extract):
        counts["rows_in"] += 1
        try:
            values = parse_row(row, database, month=month, today=today)
        except RowError as exc:
            counts["rows_failed"] += 1
            if counts["rows_failed"] <= LOG_FAILURE_SAMPLE:
                logger.warning("data %s: row %s rejected: %s", database.ai_id, row.get("RecordId"), exc)
            continue
        if values is None:
            counts["rows_skipped"] += 1
            continue
        reports.append(ActivityReportNew(**values))
    return reports, counts


def replace_reports(database: Database, reports: list[ActivityReportNew]) -> tuple[int, int]:
    """Delete the database's rows and insert ``reports`` in one transaction; returns (deleted, created)."""
    with transaction.atomic():
        deleted, _ = ActivityReportNew.objects.filter(database_ai_id=str(database.ai_id)).delete()
        created = ActivityReportNew.objects.bulk_create(reports, batch_size=BATCH_SIZE)
    return deleted, len(created)


def import_data(
    database: Database,
    *,
    client: ActivityInfoClient | None = None,
    run: SyncRun,
    extract_bytes: bytes | None = None,
    keep_copy: bool = True,
    today: dt.date | None = None,
) -> dict[str, Any]:
    """Import the reporting-year extract of ``database`` and finish ``run``.

    ``extract_bytes`` bypasses the export job (tests, re-imports of a saved extract). Returns the
    stats also written to ``run.details``; raises after finishing the run FAILED when the export
    or the transaction fails. ``last_monthly_update_date`` is stamped only on success.
    """
    stats: dict[str, Any] = {}
    try:
        if reporting_year_of(database) is None:
            raise ValueError(f"database {database.ai_id} has no reporting year")
        if extract_bytes is None:
            client = client or ActivityInfoClient()
            extract_bytes = client.export_database(database)
        stats.update(store_extract(database, extract_bytes, keep_copy=keep_copy))
        reports, counts = build_reports(database, extract_bytes, today=today)
        run.rows_in = counts["rows_in"]
        run.rows_failed = counts["rows_failed"]
        stats["rows_skipped"] = counts["rows_skipped"]
        stats["rows_deleted"], run.rows_written = replace_reports(database, reports)
    except Exception as exc:
        fail(run, exc, **stats)
        raise
    database.last_monthly_update_date = timezone.now()
    database.save(update_fields=["last_monthly_update_date"])
    finish_by_counts(run, **stats)
    stats["status"] = run.status
    logger.info(
        "data %s: %d rows in, %d written, %d skipped, %d failed",
        database.ai_id,
        run.rows_in,
        run.rows_written,
        stats["rows_skipped"],
        run.rows_failed,
    )
    return stats
