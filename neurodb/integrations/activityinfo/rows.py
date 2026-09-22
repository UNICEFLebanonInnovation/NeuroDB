"""Pure per-row logic for the ActivityInfo LONG/TEXT extract.

Ports ``pivoting/utils.py`` ``add_rows`` (lines 215-421, the default path) and ``add_rows_temp``
(lines 110-212, selected when ``Database.have_offices`` is true) as functions without I/O:
``parse_row`` turns one extract row into ``ActivityReportNew`` field values.

Kept v2 semantics: value 0 rows and non-UNICEF rows of UNICEF-only databases are skipped
(``None``); partner-name normalisation; emergency keyword detection on the first *present*
tag column; governorate ``NA`` -> code 10 / "National"; the nutrition special case (year >= 2025
and ai_id ending in 18 reads ``indicator_id``/``indicator_name``); ``last_edited_time`` keeps only
the date part; project fields truncated to 245 characters.

Fixed v2 defects: ``month_name`` comes from ``month_of_reporting`` when present (the 2025 extract
leaves ``month`` empty) and rows whose year is not the reporting year +-1 are rejected with
``RowError`` (v2 stored them); values longer than a column raise ``RowError`` up front instead of
failing the whole ``bulk_create`` batch; v2 ``add_rows_temp`` passed columns that do not exist on
``ActivityReportNew`` (``governorate``, ``start_date``, ``site_type``, ...; utils.py:139-208) and
therefore could not insert at all - those columns are dropped here.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from typing import Any

from django.db import models
from django.utils import timezone

from neurodb.facts.models import ActivityReportNew
from neurodb.indicators.models import Database

SEPARATOR = "\x1f"  # ActivityInfo TEXT export column separator
VALUE_COLUMN = "Value"
EMERGENCY_KEYWORDS = ("Cross-Border", "Escalation", "Yes")
EMERGENCY_COLUMNS = ("emergency_tag", "emergency_reporting", "tag", "Emergency Tag", "Emergency Reporting")
COVID_COLUMNS = ("X4.2.3_covid_adaptation", "covid_adaptation", "covid_adapted_sensitization")
PROJECT_TEXT_LIMIT = 245
NUTRITION_SUFFIX = "18"
NUTRITION_FROM_YEAR = 2025
YEAR_TOLERANCE = 1

MAX_LENGTHS: dict[str, int] = {
    field.name: field.max_length
    for field in ActivityReportNew._meta.get_fields()
    if isinstance(field, models.CharField) and field.max_length
}


class RowError(ValueError):
    """The row cannot be imported; the importer counts it in ``rows_failed`` and continues."""


# ------------------------------------------------------------------------------- extract parsing
def iter_extract(data: bytes | str) -> Iterator[dict[str, str]]:
    """Yield one ``{column: value}`` per data line of a LONG/TEXT extract (v2 ``read_file``).

    The first line is the header; both are split on U+001F. Like ``dict(zip(...))`` in v2 a short
    line yields fewer keys. Empty lines are ignored.
    """
    text = data.decode("utf-8-sig") if isinstance(data, bytes) else data
    lines = text.split("\n")
    header = lines[0].rstrip("\n").split(SEPARATOR)
    for line in lines[1:]:
        line = line.rstrip("\n")
        if not line:
            continue
        yield dict(zip(header, line.split(SEPARATOR), strict=False))


def reporting_year_of(database: Database) -> int | None:
    """The database's reporting year as an int, or ``None`` when it is not configured."""
    year = getattr(database.reporting_year, "year", None)
    try:
        return int(year) if year else None
    except (TypeError, ValueError):
        return None


def extraction_month(database: Database, today: dt.date | None = None) -> int:
    """v2 ``get_current_extraction_month``: January of the following year still reports December."""
    today = today or dt.date.today()
    reporting_year = reporting_year_of(database)
    if reporting_year is not None and today.year - 1 == reporting_year and today.month == 1:
        return 12
    return today.month


# -------------------------------------------------------------------------------- field helpers
def awp_code(name: Any) -> str:
    """v2 ``get_awp_code``: the leading token before the first separator character."""
    try:
        return name.split(" ")[0].split("_")[0].split(":")[0].split("-")[0].split("#")[0].split("%")[0]
    except (TypeError, AttributeError):
        return "None"


def parse_value(row: dict[str, str]) -> float:
    """The ``Value`` column as a float; anything unparsable is 0 (v2)."""
    try:
        return float(row.get(VALUE_COLUMN, 0))
    except (TypeError, ValueError):
        return 0.0


def normalise_partner(label: str) -> str:
    """v2 partner-name normalisation: dashes to underscores, accents stripped, control chars removed."""
    return (
        label.replace("-", "_")
        .replace("é", "e")
        .replace("à", "a")
        .replace("ù", "u")
        .replace("ô", "o")
        .replace("\r", " ")
        .replace("\n", " ")
        .replace("\t", "")
        .replace('"', "")
    )


def first_present(row: dict[str, str], keys: tuple[str, ...], default: str = "") -> str:
    """Value of the first key *present* in the row (v2 ``if/elif`` chain; may be empty)."""
    for key in keys:
        if key in row:
            return row[key]
    return default


def coalesce(row: dict[str, str], primary: str, fallback: str) -> str:
    """``row[primary]`` unless it is missing or empty, then ``row[fallback]`` (v2 project fields)."""
    value = row.get(primary, "")
    if value == "":
        value = row.get(fallback, "")
    return value


def emergency_flag(row: dict[str, str]) -> str:
    """``"Yes"`` when the first present tag column contains an emergency keyword, else ``"No"``."""
    tag = first_present(row, EMERGENCY_COLUMNS)
    return "Yes" if any(keyword in tag for keyword in EMERGENCY_KEYWORDS) else "No"


def support_covid(row: dict[str, str]) -> bool:
    return any(row.get(column) == "Yes" for column in COVID_COLUMNS)


def governorate(row: dict[str, str]) -> tuple[str, str]:
    """(code, name) with the v2 ``NA`` -> (10, "National") substitution; ("0", "") when absent."""
    code: str | int = 0
    name = ""
    if "governorate.code" in row:
        code = 10 if row["governorate.code"] == "NA" else row["governorate.code"]
    if "governorate.name" in row:
        name = "National" if row["governorate.name"] == "NA" else row["governorate.name"]
    return str(code), name


def month_name(row: dict[str, str]) -> str:
    """``month_of_reporting`` when populated, else ``month`` (fix for the 2025 extract)."""
    return row.get("month_of_reporting") or row.get("month", "")


def check_year(month: str, reporting_year: int | None) -> None:
    """Reject rows whose ``YYYY-MM`` year is not within +-1 of the reporting year."""
    if reporting_year is None:
        return
    try:
        year = int(month[:4])
    except (TypeError, ValueError):
        raise RowError(f"month {month!r} has no year") from None
    if abs(year - reporting_year) > YEAR_TOLERANCE:
        raise RowError(f"month {month!r} is outside reporting year {reporting_year}")


def last_edited_time(row: dict[str, str]) -> dt.datetime | None:
    """The date part of ``Record last edited time`` as an aware midnight datetime (v2 kept the date)."""
    if "Record last edited time" not in row:
        return None
    try:
        day = dt.datetime.strptime(row["Record last edited time"][0:10], "%Y-%m-%d")
    except ValueError as exc:
        raise RowError(f"bad last edited time: {exc}") from exc
    return timezone.make_aware(day)


def is_nutrition(database: Database) -> bool:
    """v2 special case: 2025+ databases whose ai_id ends in 18 carry the indicator in other columns."""
    ai_id = str(database.ai_id)
    try:
        return int(ai_id[:4]) >= NUTRITION_FROM_YEAR and ai_id[-2:] == NUTRITION_SUFFIX
    except ValueError:
        return False


def validate_lengths(values: dict[str, Any]) -> None:
    for name, value in values.items():
        limit = MAX_LENGTHS.get(name)
        if limit and isinstance(value, str) and len(value) > limit:
            raise RowError(f"{name} longer than {limit} characters")


def _required(row: dict[str, str], key: str) -> str:
    try:
        return row[key]
    except KeyError:
        raise RowError(f"column {key!r} missing") from None


def _location_fields(row: dict[str, str]) -> dict[str, str]:
    return {
        "location_adminlevel_caza_code": row.get("caza.code", ""),
        "location_adminlevel_caza": row.get("caza.name", ""),
        "location_adminlevel_cadastral_area_code": row.get("cadastral_area.cas_code", ""),
        "location_adminlevel_cadastral_area": row.get("cadastral_area.name", ""),
        "location_longitude": row.get("ai_allsites.geographic_location.longitude", ""),
        "location_latitude": row.get("ai_allsites.geographic_location.latitude", ""),
        "location_alternate_name": row.get("ai_allsites.alternate_name", ""),
        "location_name": row.get("ai_allsites.name", ""),
    }


# ------------------------------------------------------------------------------------ parse_row
def parse_row(
    row: dict[str, str],
    database: Database,
    *,
    month: int | None = None,
    today: dt.date | None = None,
) -> dict[str, Any] | None:
    """Map one extract row to ``ActivityReportNew`` field values.

    Returns ``None`` for rows v2 skipped silently, raises ``RowError`` for rows that must be
    counted as failed. ``month`` is the extraction month (v2 ``get_current_extraction_month``),
    computed from ``today`` when not given. Dispatches to the ``have_offices`` variant.
    """
    month = extraction_month(database, today) if month is None else month
    if database.have_offices:
        values = _parse_office_row(row, database)
    else:
        values = _parse_partner_row(row, database)
    if values is None:
        return None
    values["month"] = str(month)
    validate_lengths(values)
    return values


def _parse_partner_row(row: dict[str, str], database: Database) -> dict[str, Any] | None:
    """v2 ``add_rows``."""
    value = parse_value(row)
    if value == 0:
        return None
    funded_by = row.get("funded_by.funded_by", "UNICEF")
    if funded_by.lower() != "unicef" and database.is_funded_by_unicef is True:
        return None

    partner_label = normalise_partner(row.get("partner.name", ""))
    if partner_label == "UNICEF":
        funded_by = "UNICEF"
    gov_code, gov_name = governorate(row)

    indicator_name = _required(row, "Quantity Field")
    code = awp_code(indicator_name)
    if is_nutrition(database):
        indicator_name = row.get("indicator_name", "")
        code = row.get("indicator_id", code)

    reported_month = month_name(row)
    check_year(reported_month, reporting_year_of(database))

    values: dict[str, Any] = {
        "ai_folder": _required(row, "Folder"),
        "database_ai_id": str(database.ai_id),
        "dbase": database,
        "report_id": _required(row, "FormId"),
        "indicator_id": _required(row, "Quantity Field ID"),
        "indicator_name": indicator_name,
        "indicator_awp_code": code,
        "month_name": reported_month,
        "partner_label": partner_label,
        "location_adminlevel_governorate_code": gov_code,
        "location_adminlevel_governorate": gov_name,
        "form": row.get("Form", ""),
        "partner_description": row.get("partner.partner_full_name", ""),
        "project_label": coalesce(row, "projects.project_code", "project_code")[:PROJECT_TEXT_LIMIT],
        "project_description": row.get("projects.project_name", "")[:PROJECT_TEXT_LIMIT],
        "project": coalesce(row, "projects.please_select_related_project", "please_select_related_project")[
            :PROJECT_TEXT_LIMIT
        ],
        "project_plan": coalesce(row, "projects.select_plan", "select_plan"),
        "funded_by": funded_by,
        "indicator_value": value,
        "reporting_section": row.get("reporting_section", ""),
        "partner_id": row.get("partner_id", partner_label),
        "support_covid": support_covid(row),
        "last_edited_time": last_edited_time(row),
        "parent_form": row.get("ParentForm", ""),
        "emergency": emergency_flag(row),
    }
    values.update(_location_fields(row))
    return values


def _parse_office_row(row: dict[str, str], database: Database) -> dict[str, Any] | None:
    """v2 ``add_rows_temp`` (databases with ``have_offices``): UNICEF field-office reporting.

    Value 0 rows are kept, partner and funder are UNICEF, the governorate is the reporting office
    and, as in v2, rows outside the reporting year are skipped (not counted as failed).
    """
    reported_month = month_name(row)
    if not reported_month or reported_month == "NA":
        raise RowError("month missing")
    reporting_year = reporting_year_of(database)
    if reporting_year is not None and str(reporting_year) not in reported_month:
        return None
    office = _required(row, "reporting_office")
    values: dict[str, Any] = {
        "ai_folder": _required(row, "Folder"),
        "database_ai_id": str(database.ai_id),
        "dbase": database,
        "report_id": _required(row, "FormId"),
        "indicator_id": _required(row, "Quantity Field ID"),
        "indicator_name": _required(row, "Quantity Field"),
        "indicator_awp_code": "",
        "month_name": reported_month,
        "partner_label": "UNICEF",
        "partner_description": "UNICEF",
        "partner_id": row.get("partner_id", "UNICEF"),
        "location_adminlevel_governorate_code": office,
        "location_adminlevel_governorate": office,
        "form": row.get("Form", ""),
        "project_label": row.get("projects.project_code", ""),
        "project_description": row.get("projects.project_name", ""),
        "funded_by": "UNICEF",
        "indicator_value": parse_value(row),
        "reporting_section": row.get("reporting_section", ""),
    }
    values.update(_location_fields(row))
    return values
