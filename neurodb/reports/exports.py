"""CSV (streamed) and XLSX (openpyxl write-only) exports with database-labelled filenames."""

from __future__ import annotations

import csv
import datetime
import io
from collections.abc import Iterable, Iterator, Sequence
from typing import Any

from django.http import HttpResponse, StreamingHttpResponse
from django.utils.text import slugify
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


def export_filename(label: str, ext: str, today: datetime.date | None = None) -> str:
    today = today or datetime.date.today()
    return f"{slugify(label) or 'export'}_{today:%Y-%m-%d}.{ext}"


def _cell(value: Any) -> Any:
    if isinstance(value, list | tuple):
        return ", ".join(str(v) for v in value)
    if isinstance(value, datetime.date | datetime.datetime):
        return value.isoformat()
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
            yield writer.writerow([_cell(row.get(c)) for c in columns])

    response = StreamingHttpResponse(lines(), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


def xlsx_response(
    filename: str, sheets: Sequence[tuple[str, Sequence[str], Iterable[dict[str, Any]]]]
) -> HttpResponse:
    book = Workbook(write_only=True)
    for title, columns, rows in sheets:
        sheet = book.create_sheet(title=title[:31])
        sheet.append(list(columns))
        for row in rows:
            sheet.append([_cell(row.get(c)) for c in columns])
    buffer = io.BytesIO()
    book.save(buffer)
    response = HttpResponse(buffer.getvalue(), content_type=XLSX_CONTENT_TYPE)
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
