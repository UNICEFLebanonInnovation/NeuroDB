"""CSV (streamed) and XLSX (openpyxl write-only) exports with database-labelled filenames. A typed workbook
(``xlsx_response(..., typed=True)``, written by :mod:`neurodb.reports.xlsx`) keeps numbers as numbers and
dates as dates, never lets a text become a formula, and stays fast for millions of cells; a CSV keeps a
text that starts like a formula a text (:func:`csv_cell`)."""

from __future__ import annotations

import csv
import datetime
import io
import re
from collections.abc import Iterable, Iterator, Sequence
from typing import Any

from django.http import HttpResponse, StreamingHttpResponse
from django.utils.text import slugify
from django.utils.translation import gettext as _
from openpyxl import Workbook

from neurodb.facts import queries
from neurodb.facts.services.dashboard import analytical_rows
from neurodb.indicators.models import Database

ANALYTICAL_COLUMNS: Sequence[str] = (
    "master_indicator", "target", "sub_indicator", "sequence", "value_role", "ai_indicator", "gender",
    "nationality", "disability", "programme", "age", "indicator_name", "awp_code", "emergency", "governorate",
    "district", "cadaster", "partner", "pd", "plan", "project", "database_ai_id", "month", "indicator_value",
)  # fmt: skip
ETOOLS_LOCATION_COLUMNS: Sequence[str] = (
    "pd_number",
    "title",
    "partner_name",
    "status",
    "start",
    "end",
    "section_names",
    "p_code",
)
XLSX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
FORMATS = ("csv", "xlsx")


def export_filename(
    label: str, ext: str, today: datetime.date | None = None, *, moment: datetime.datetime | None = None
) -> str:
    """``<label>_<YYYY-MM-DD>.<ext>``; with ``moment`` (the figures' moment, as Raw eTools data's downloads
    carry it), ``<label>_<YYYY-MM-DD>_<HHMM>.<ext>`` in the site's time."""
    if moment is not None:
        from django.utils import timezone

        return f"{slugify(label) or 'export'}_{timezone.localtime(moment):%Y-%m-%d_%H%M}.{ext}"
    today = today or datetime.date.today()
    return f"{slugify(label) or 'export'}_{today:%Y-%m-%d}.{ext}"


def too_many_words(n: int, limit: int, what: str, first: str) -> str:
    """The sentence a download over its limit says instead: "The filter holds 31,000 records, more than
    25,000: choose a year, a section, a partner or a programme document first."."""
    return _("The filter holds %(n)s %(what)s, more than %(limit)s: choose %(first)s first.") % {
        "n": f"{n:,}",
        "what": what,
        "limit": f"{limit:,}",
        "first": first,
    }


def too_many(n: int, limit: int, what: str, first: str) -> HttpResponse:
    """A download over its limit: 400, with :func:`too_many_words` as plain text."""
    return HttpResponse(
        too_many_words(n, limit, what, first), status=400, content_type="text/plain; charset=utf-8"
    )


def _cell(value: Any) -> Any:
    if isinstance(value, list | tuple):
        return ", ".join(str(v) for v in value)
    if isinstance(value, datetime.date | datetime.datetime):
        return value.isoformat()
    return value


# a text starting with one of these is read by a spreadsheet as a formula (or a formula's start)
FORMULA_START = ("=", "+", "-", "@", "\t", "\r")
PLAIN_NUMBER = re.compile(r"[+-]?[0-9]+([.,][0-9]+)*")  # a number written as text runs nothing


def csv_cell(value: Any) -> Any:
    """A value as a CSV cell: as :func:`_cell`, and a text that a spreadsheet would run as a formula
    (``=HYPERLINK(...)`` in a partner's or eTools' text) kept as text by a leading apostrophe, as the
    Excel files keep every text a text. Numbers and dates are never changed, nor a number written as
    text ("-12.5")."""
    value = _cell(value)
    if isinstance(value, str) and value.startswith(FORMULA_START) and not PLAIN_NUMBER.fullmatch(value):
        return "'" + value
    return value


class _Echo:
    """A pseudo file object that returns what csv.writer hands it (streaming pattern from the Django docs)."""

    def write(self, value: str) -> str:
        return value


def stream_csv(
    filename: str, columns: Sequence[str], rows: Iterable[dict[str, Any]]
) -> StreamingHttpResponse:
    writer = csv.writer(_Echo())

    def lines() -> Iterator[str]:
        yield "﻿"  # BOM so that Excel opens the file as UTF-8
        yield writer.writerow(list(columns))
        for row in rows:
            yield writer.writerow([csv_cell(row.get(c)) for c in columns])

    response = StreamingHttpResponse(lines(), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


def xlsx_response(
    filename: str,
    sheets: Sequence[tuple[str, Sequence[str], Iterable[dict[str, Any]]]],
    typed: bool = False,
) -> HttpResponse:
    if typed:
        from .xlsx import workbook

        content = workbook(sheets)
    else:
        book = Workbook(write_only=True)
        for title, columns, rows in sheets:
            sheet = book.create_sheet(title=title[:31])
            sheet.append(list(columns))
            for row in rows:
                sheet.append([_cell(row.get(c)) for c in columns])
        buffer = io.BytesIO()
        book.save(buffer)
        content = buffer.getvalue()
    response = HttpResponse(content, content_type=XLSX_CONTENT_TYPE)
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


def analytical_export(
    database: Database, fmt: str, emergency: str | None = None
) -> HttpResponse | StreamingHttpResponse:
    label = f"{database.label or database.name}_raw_data"
    rows = analytical_rows(database, emergency=emergency)
    if fmt == "csv":
        return stream_csv(export_filename(label, "csv"), ANALYTICAL_COLUMNS, rows)
    return xlsx_response(export_filename(label, "xlsx"), [("Raw data", ANALYTICAL_COLUMNS, rows)])


def etools_locations_export(statuses: list[str]) -> HttpResponse:
    rows = queries.etools_planned_locations(statuses)
    return xlsx_response(
        export_filename("etools_planned_locations", "xlsx"), [("Locations", ETOOLS_LOCATION_COLUMNS, rows)]
    )
