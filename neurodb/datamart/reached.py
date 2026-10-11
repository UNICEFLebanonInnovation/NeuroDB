"""eTools "Reached": what a partner reached on one programme document indicator, computed one way for
every page, export, Ask NeuroDB tool and headline. No page adds partner report values itself.

The Datamart's partner reports (``ReportedIndicator``, ``prp/datareport``) hold one row per indicator,
progress report and location, and repeat report-level fields on each of them. The rule:

* **Which reports count.** Accepted and Submitted reports. The part that comes from Submitted reports
  is shown as "awaiting acceptance", so a later send-back is understood. Sent back, Due, Overdue and
  not-yet-due reports never count. "Latest" means the latest counted report.
* **A location's value in one report** is the disaggregation total ``"()"`` (``c``, else v/d, else v),
  else ``total_cumulative_progress_in_location − previous_location_progress``, else
  ``achievement_in_period``. When every location row of a report carries the same
  ``achievement_in_period``, it is the report total repeated on each row and is not added (a data
  note says so). Two rows of one location in one report are never added: the largest is used.
* **A report's value** combines its locations with ``calculation_across_locations`` (sum, max, avg;
  percentages and ratios Σv/Σd). PRP's own report total (``progress_value`` of
  ``prp/indicator-report-v2``, only from a record naming the same PD and indicator) stands in when the
  locations cannot say; one that differs from them (beyond rounding: half a person, half a point) is
  noted, and the locations are used.
* **Reached** combines the counted reports with ``calculation_across_periods`` (sum, max, avg,
  latest; percentages and ratios Σv/Σd): to date, in a year, per month, per location and per
  disaggregation key. For an indicator summed across locations and periods, the months of every year
  add up to Reached, and so do the locations.
* Different indicators, quarterly and humanitarian reports, and percentages are never added.

A programme document that started before the partner reports NeuroDB holds were read from
(:func:`reports_read_from`) has a partial history: its Reached is PRP's own cumulative from the
latest counted report.
"""

from __future__ import annotations

import ast
import datetime
import json
import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

# --------------------------------------------------------------------------------- report statuses
ACCEPTED = "accepted"
SUBMITTED = "submitted"
SENT_BACK = "sent_back"
DUE = "due"
OVERDUE = "overdue"
NOT_YET_DUE = "not_yet_due"
STATUS_LABELS = {
    ACCEPTED: "Accepted",
    SUBMITTED: "Submitted",
    SENT_BACK: "Sent back",
    DUE: "Due",
    OVERDUE: "Overdue",
    NOT_YET_DUE: "Not yet due",
}
COUNTED = frozenset({ACCEPTED, SUBMITTED})
# PRP's status, as a word or its three-letter code, matched whole (as monitoring.is_submitted does)
_STATUS_WORDS = {
    "accepted": ACCEPTED,
    "acc": ACCEPTED,
    "submitted": SUBMITTED,
    "sub": SUBMITTED,
    "sent back": SENT_BACK,
    "sen": SENT_BACK,
}
# PRP's word for a report still to be submitted: it never counts, whatever dates it carries
_DUE_WORDS = frozenset({"due", "ove", "overdue"})

# what an indicator's value says about it
NOT_REPORTED = "not_reported"  # no counted report with a value
REPORTED_ZERO = "reported_zero"  # counted reports, and Reached is 0
REPORTED = "reported"
VALUE_LABELS = {NOT_REPORTED: "Not reported", REPORTED_ZERO: "Reported zero", REPORTED: "Reported"}

MAX_HISTORY_YEARS = 10  # the sync window never reaches further back than this, whatever a PD's start


def status_of(
    text: Any,
    *,
    period_end: datetime.date | None = None,
    due_date: datetime.date | None = None,
    submission_date: datetime.date | None = None,
    today: datetime.date,
) -> str:
    """One of the six report statuses. PRP's word or code decides Accepted, Submitted and Sent back.
    A report PRP calls Due or Overdue never counts: with a submission date (PRP reopened it) it is
    Due, never Overdue, as ``monitoring.pending_q`` leaves it out of every overdue figure. A
    submission date makes any report without a word Submitted (the rule of ``pending_q``, so a
    report is overdue on every page or on none); a report not submitted is Overdue once its due date
    has passed, Not yet due while its period runs, else Due."""
    word = " ".join(str(text or "").lower().replace("_", " ").split())
    if word in _STATUS_WORDS:
        return _STATUS_WORDS[word]
    if submission_date is not None:
        return DUE if word in _DUE_WORDS else SUBMITTED
    if due_date is not None and due_date < today:
        return OVERDUE
    if period_end is not None and period_end > today:
        return NOT_YET_DUE
    return DUE


# ------------------------------------------------------------------------------------------ numbers
NUMBER = re.compile(r"[-+]?\d+(?:\.\d+)?")


def _as_dict(value: Any) -> dict | None:
    """PRP's ``{"v": .., "d": .., "c": ..}`` as a dict, from a dict or its text (JSON or Python)."""
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text.startswith("{"):
        return None
    for read in (json.loads, ast.literal_eval):
        try:
            parsed = read(text)
        except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
            continue
        return parsed if isinstance(parsed, dict) else None
    return None


def _from_parts(parts: dict) -> float | None:
    """The calculated value when there is one, else the numerator over a real denominator, else the
    numerator."""
    for key in ("c", "v"):
        if parts.get(key) not in (None, ""):
            value = number(parts[key])
            if key == "v" and value is not None:
                d = number(parts.get("d"))
                if d not in (None, 0.0, 1.0):
                    return value / d
            return value
    return None


def number(value: Any) -> float | None:
    """A PRP value as a number: ``1234``, ``"1,234"``, ``"12.5 %"``, ``Decimal``, or PRP's
    ``{"v": .., "d": .., "c": ..}`` (as a dict or as its text). None when the text holds no number
    or several (``"45/100"``, a date): the digits of separate numbers are never glued into one,
    which once turned a structured value into billions of children."""
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float | Decimal):
        return float(value)
    if isinstance(value, dict):
        return _from_parts(value)
    text = str(value).strip()
    if text.startswith("{"):
        parts = _as_dict(text)
        return _from_parts(parts) if parts is not None else None
    found = NUMBER.findall(text.replace(",", ""))
    return float(found[0]) if len(found) == 1 else None


@dataclass(frozen=True)
class Parts:
    """A PRP value: numerator ``v``, denominator ``d``, calculated ``c`` (a plain number is ``v``)."""

    v: float | None = None
    d: float | None = None
    c: float | None = None

    def value(self, ratio: bool = False, percentage: bool = False) -> float | None:
        """The value as NeuroDB shows it: a percentage or ratio from v/d when both are there (a
        percentage × 100), else ``c``, else v (over d when d is a real denominator)."""
        scale = 100.0 if percentage else 1.0
        if ratio and self.v is not None and self.d not in (None, 0.0):
            return self.v / self.d * scale
        if self.c is not None:
            return self.c
        if self.v is not None:
            if self.d not in (None, 0.0, 1.0):
                return self.v / self.d * (scale if ratio else 1.0)
            return self.v
        return None


def parts(value: Any) -> Parts | None:
    if value in (None, ""):
        return None
    found = _as_dict(value)
    if found is not None:
        out = Parts(number(found.get("v")), number(found.get("d")), number(found.get("c")))
        return out if (out.v, out.d, out.c) != (None, None, None) else None
    n = number(value)
    return Parts(v=n) if n is not None else None


def close(a: float | None, b: float | None, *, ratio: bool = False, percentage: bool = False) -> bool:
    """Two figures that are the same once rounding is allowed for: half a person for a count (people
    are whole: 10,040 is never "the same" as 10,000), half a point for a percentage, 0.5 % of the
    larger for a ratio."""
    if a is None or b is None:
        return False
    if percentage or not ratio:
        return abs(a - b) <= 0.5
    return abs(a - b) <= 0.005 * max(abs(a), abs(b))


def norm(text: str | None) -> str:
    return " ".join((text or "").lower().split())


def location_key(p_code: str | None, name: str | None) -> str:
    """The P-code when eTools gives one (the authoritative identity), else the normalised name."""
    return (p_code or "").strip().upper() or f"name:{norm(name)}"


def method_of(text: Any) -> str:
    """A calculation method as PRP names it: sum, max, avg, latest or ratio ("" when not given)."""
    word = norm(str(text or ""))
    return {"average": "avg", "maximum": "max", "mean": "avg", "last": "latest"}.get(word, word)


Item = tuple[float | None, float | None, float | None]  # value, numerator, denominator


def combine_items(
    items: Iterable[Item],
    method: str,
    *,
    ratio: bool = False,
    percentage: bool = False,
    fallback: str = "avg",
) -> Item:
    """Several values as one, the way PRP combines them: sum, max, avg, latest (the last item), and
    for percentages and ratios Σv/Σd (``fallback`` when a numerator or denominator is missing: the
    average of the locations, or the latest report). Empty values are left out."""
    present = [it for it in items if it[0] is not None]
    if not present:
        return (None, None, None)
    values = [it[0] for it in present]
    with_parts = all(it[1] is not None and it[2] is not None for it in present)
    sv = sum(it[1] for it in present) if with_parts else None
    sd = sum(it[2] for it in present) if with_parts else None
    method = method_of(method)
    if method == "max":
        return (max(values), None, None)
    if method == "avg":
        return (sum(values) / len(values), None, None)
    if method == "latest":
        return present[-1]
    if ratio or method == "ratio":
        if with_parts and sd:
            return (sv / sd * (100.0 if percentage else 1.0), sv, sd)
        if fallback == "latest":
            return present[-1]
        return (sum(values) / len(values), None, None)
    return (sum(values), sv, sd)


def combine(values: list[float | None], method: str) -> float | None:
    """Plain values as one: sum (the default), max, avg, latest; a ratio without its numerators and
    denominators is averaged, never added."""
    return combine_items([(v, None, None) for v in values], method)[0]


def is_ratio(display_type: str = "", unit: str = "", *methods: str) -> bool:
    return (
        (display_type or "").lower() in ("percentage", "ratio")
        or (unit or "").lower() == "percentage"
        or "ratio" in {method_of(m) for m in methods}
    )


# ------------------------------------------------------------------------------ reports and cells
@dataclass
class Cell:
    """One location in one report."""

    value: float | None = None
    v: float | None = None
    d: float | None = None
    source: str = ""  # total ("()"), location_cumulative, achievement, or "" when nothing was reported
    parts: dict[str, float] = field(default_factory=dict)  # disaggregation key -> value
    prp_cumulative: float | None = None  # PRP's total_cumulative_progress_in_location
    rows: int = 1
    repeated: bool = False  # the report total repeated on every location: no value of its own
    disaggregation: dict[str, Any] = field(default_factory=dict)  # PRP's raw values of the row read
    modified: Any = None  # the row's last change in the Datamart (when read)

    @property
    def item(self) -> Item:
        return (self.value, self.v, self.d)


@dataclass
class Report:
    """One indicator in one progress report (``indicator_report`` in PRP)."""

    key: str
    number: str = ""
    report_type: str = ""
    status: str = DUE
    status_text: str = ""
    period_start: datetime.date | None = None
    period_end: datetime.date | None = None
    due_date: datetime.date | None = None
    submission_date: datetime.date | None = None
    indicator_report: str = ""
    method: str = ""  # across locations
    period_method: str = ""  # across reporting periods
    cells: dict[str, Cell] = field(default_factory=dict)
    places: dict[str, dict[str, Any]] = field(default_factory=dict)  # location key -> name, p_code, id
    value: float | None = None
    v: float | None = None
    d: float | None = None
    source: str = "locations"  # locations | report_total (repeated on each row) | prp (progress_value)
    prp_cumulative: float | None = None  # PRP's total_cumulative_progress (the indicator to date)
    progress_value: float | None = None  # PRP's report total from prp/indicator-report-v2
    partner_status: str = ""
    narrative_assessment: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def counted(self) -> bool:
        return self.status in COUNTED

    @property
    def status_label(self) -> str:
        return STATUS_LABELS[self.status]

    @property
    def item(self) -> Item:
        return (self.value, self.v, self.d)

    @property
    def order(self) -> tuple:
        return (self.period_end or datetime.date.min, self.key)


ROW_FIELDS = (
    "datamart_id",
    "intervention_id",
    "indicator",
    "etools_indicator_id",
    "progress_report",
    "indicator_report",
    "report_number",
    "report_type",
    "report_status",
    "period_start",
    "period_end",
    "due_date",
    "submission_date",
    "achievement_in_period",
    "total_cumulative_progress",
    "total_cumulative_progress_in_location",
    "previous_location_progress",
    "calculation_across_locations",
    "calculation_across_periods",
    "disaggregation",
    "location",
    "p_code",
    "location_ref_id",
    "pd_output_progress_status",
    "narrative_assessment",
    "last_modify_date",
    "synced_at",
)
# a fixed reading order: "the first non-empty field" never depends on the query plan
ROW_ORDER = ("period_end", "progress_report", "location", "p_code", "datamart_id")


def report_key(row: dict[str, Any]) -> str:
    """The progress report of a row; rows without one are never pooled with other reports."""
    if row.get("progress_report"):
        return str(row["progress_report"])
    if row.get("indicator_report"):
        return f"ir:{row['indicator_report']}"
    return f"{row.get('report_number') or ''}|{row.get('period_start') or ''}|{row.get('period_end') or ''}"


def indicator_report_key(row: dict[str, Any]) -> str:
    """One indicator in one progress report: PRP's indicator report id, else the eTools id and the title
    (the rule of ``reporting.INDICATOR_REPORT``, which counts them). Two indicators of one report are
    never read as one, even when they share a title or an eTools id."""
    found = str(row.get("indicator_report") or "")
    return found or f"t|{row.get('etools_indicator_id') or ''}|{row.get('indicator') or ''}"


# ------------------------------------------------------------------------- one status per report
Stamp = tuple[Any, Any, Any]  # last change in the Datamart, last sync, Datamart id (None: earliest)


def _later(a: Any, b: Any) -> Any:
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b)


def stamp(current: Stamp | None, new: Stamp) -> Stamp:
    """Two stamps as one: the latest of each part."""
    if current is None:
        return new
    return (_later(current[0], new[0]), _later(current[1], new[1]), _later(current[2], new[2]))


def row_stamp(row: dict[str, Any]) -> Stamp:
    return (row.get("last_modify_date"), row.get("synced_at"), row.get("datamart_id"))


def newest_status(stamps: dict[str, Stamp]) -> str:
    """The status word of a progress report whose rows disagree (a partial re-sync: some rows still
    carry the old status): the word of the rows changed last in the Datamart, then synced last, then
    with the highest Datamart id. ``stamps``: each word PRP gave the report (as stored) and the latest
    stamp of its rows. Every page reads a report's status this way (the reports' list and tiles, the
    indicators' Reached, the pop-up), so one report has one status everywhere."""
    words = {text: s for text, s in stamps.items() if (text or "").strip()}
    if not words:
        return ""

    def order(item: tuple[str, Stamp]) -> tuple:
        return tuple((part is not None, part) for part in item[1])

    return max(words.items(), key=order)[0].strip()


def report_status_texts(rows: Iterable[dict[str, Any]]) -> dict[str, str]:
    """Each progress report's status word (:func:`newest_status`), by :func:`report_key`, from its rows
    (every indicator's: read them all, so each indicator of the report has the report's status)."""
    stamps: dict[str, dict[str, Stamp]] = defaultdict(dict)
    for row in rows:
        text = row.get("report_status") or ""
        mine = stamps[report_key(row)]
        mine[text] = stamp(mine.get(text), row_stamp(row))
    return {key: newest_status(words) for key, words in stamps.items()}


def _cell(row: dict[str, Any], ratio: bool, percentage: bool) -> Cell:
    """A location's value in one report, by the rule of the module docstring."""
    disaggregation = row.get("disaggregation") if isinstance(row.get("disaggregation"), dict) else {}
    found: dict[str, float] = {}
    for key, raw in disaggregation.items():
        p = parts(raw)
        value = p.value(ratio, percentage) if p else None
        if value is not None:
            found[str(key)] = value
    prp_cumulative = parts(row.get("total_cumulative_progress_in_location"))
    cell = Cell(
        parts=found,
        prp_cumulative=prp_cumulative.value(ratio, percentage) if prp_cumulative else None,
        disaggregation=disaggregation,
        modified=row.get("last_modify_date"),
    )
    total = parts(disaggregation.get("()")) if disaggregation else None
    if total is not None and total.value(ratio, percentage) is not None:
        cell.value, cell.v, cell.d, cell.source = total.value(ratio, percentage), total.v, total.d, "total"
        return cell
    current = number(row.get("total_cumulative_progress_in_location"))
    previous = number(row.get("previous_location_progress"))
    summed = method_of(row.get("calculation_across_periods")) in ("", "sum")
    if not ratio and summed and current is not None and previous is not None:
        cell.value, cell.v, cell.d, cell.source = current - previous, None, None, "location_cumulative"
        return cell
    achieved = parts(row.get("achievement_in_period"))
    if achieved is not None and achieved.value(ratio, percentage) is not None:
        cell.value, cell.v, cell.d = achieved.value(ratio, percentage), achieved.v, achieved.d
        cell.source = "achievement"
    return cell


def _first(current: Any, new: Any) -> Any:
    return current if current not in (None, "") else new


def build_reports(
    rows: Iterable[dict[str, Any]],
    *,
    ratio: bool,
    percentage: bool,
    today: datetime.date,
    progress_values: dict[str, float] | None = None,
    report_status: dict[str, str] | None = None,
) -> list[Report]:
    """The reports of one indicator from its rows (read in ``ROW_ORDER``), oldest first.
    ``report_status``: each progress report's status word read from all its rows
    (:func:`report_status_texts`); else the word of these rows (:func:`newest_status`)."""
    reports: dict[str, Report] = {}
    candidates: dict[str, dict[str, list[Cell]]] = defaultdict(lambda: defaultdict(list))
    words: dict[str, dict[str, Stamp]] = defaultdict(dict)
    for row in rows:
        key = report_key(row)
        report = reports.get(key)
        if report is None:
            report = reports[key] = Report(key=key)
        report.number = _first(report.number, row.get("report_number") or "")
        report.report_type = _first(report.report_type, (row.get("report_type") or "").upper())
        text = row.get("report_status") or ""
        words[key][text] = stamp(words[key].get(text), row_stamp(row))
        for name in ("period_start", "period_end", "due_date", "submission_date"):
            setattr(report, name, _first(getattr(report, name), row.get(name)))
        report.indicator_report = _first(report.indicator_report, str(row.get("indicator_report") or ""))
        report.method = _first(report.method, method_of(row.get("calculation_across_locations")))
        report.period_method = _first(report.period_method, method_of(row.get("calculation_across_periods")))
        report.partner_status = _first(report.partner_status, row.get("pd_output_progress_status") or "")
        report.narrative_assessment = _first(
            report.narrative_assessment, row.get("narrative_assessment") or ""
        )
        cumulative = parts(row.get("total_cumulative_progress"))
        cumulative_value = cumulative.value(ratio, percentage) if cumulative else None
        if cumulative_value is not None:  # PRP repeats it on each row; rows that disagree: the largest
            report.prp_cumulative = (
                cumulative_value
                if report.prp_cumulative is None
                else max(report.prp_cumulative, cumulative_value)
            )
        place = (
            location_key(row.get("p_code"), row.get("location"))
            if (row.get("p_code") or row.get("location"))
            else ""
        )
        known = report.places.setdefault(
            place,
            {
                "name": row.get("location") or "",
                "p_code": row.get("p_code") or "",
                "id": row.get("location_ref_id"),
            },
        )
        if known["id"] is None:
            known["id"] = row.get("location_ref_id")
        candidates[key][place].append(_cell(row, ratio, percentage))
    out = []
    for key, report in reports.items():
        given = (report_status or {}).get(key)
        report.status_text = given if given is not None else newest_status(words[key])
        report.status = status_of(
            report.status_text,
            period_end=report.period_end,
            due_date=report.due_date,
            submission_date=report.submission_date,
            today=today,
        )
        for place, cells in candidates[key].items():
            report.cells[place] = _choose(cells, report)
        _report_value(report, ratio, percentage, (progress_values or {}).get(report.indicator_report))
        out.append(report)
    out.sort(key=lambda r: r.order)
    return out


def _choose(cells: list[Cell], report: Report) -> Cell:
    """One location of one report, read from several rows: never their sum. The same value on every
    row is that value; different values: the largest, with a note."""
    with_value = [c for c in cells if c.value is not None]
    if not with_value:
        cells[0].rows = len(cells)
        return cells[0]
    chosen = max(with_value, key=lambda c: c.value)
    chosen.rows = len(cells)
    if len({c.value for c in with_value}) > 1:
        report.notes.append(
            "Several rows for one location in this report carry different values: the largest is used, "
            "never their sum."
        )
    return chosen


def _report_value(report: Report, ratio: bool, percentage: bool, progress_value: Any) -> None:
    cells = [c for c in report.cells.values() if c.value is not None]
    if (
        len(cells) > 1
        and len(cells) == len(report.cells)
        and all(c.source == "achievement" for c in cells)
        and len({c.value for c in cells}) == 1
    ):
        # the report total repeated on each location row: one value for the report, none per location
        report.value, report.v, report.d, report.source = (
            cells[0].value,
            cells[0].v,
            cells[0].d,
            "report_total",
        )
        for cell in report.cells.values():
            cell.repeated = True
        report.notes.append(
            f"Every location row of this report carries the same value ({cells[0].value:g}): it is read "
            "as the report total, not added up over the locations."
        )
    else:
        report.value, report.v, report.d = combine_items(
            [c.item for c in cells],
            report.method or "sum",
            ratio=ratio,
            percentage=percentage,
            fallback="avg",
        )
    total = parts(progress_value)
    total_value = total.value(ratio, percentage) if total else None
    if total_value is None:
        return
    report.progress_value = total_value
    if report.value is None or report.source == "report_total":
        # the locations cannot say: PRP's own total for the report
        report.value, report.v, report.d, report.source = total_value, total.v, total.d, "prp"
    elif close(total_value, report.value, ratio=ratio, percentage=percentage):
        # they agree (to the rounding): the locations' figure stays, so they add up to the report
        # exactly and a percentage keeps the numerator and denominator its periods are combined by
        pass
    else:
        report.notes.append(
            f"PRP's own total for this report ({total_value:g}) differs from its locations "
            f"({report.value:g}); the locations are used."
        )


# --------------------------------------------------------------------------------- per indicator
@dataclass
class Reached:
    """Everything NeuroDB says about one PD indicator's progress (one report type)."""

    reports: list[Report] = field(default_factory=list)  # every report of the type, oldest first
    counted: list[Report] = field(default_factory=list)  # Accepted and Submitted reports with a value
    reached: float | None = None  # to date
    reached_accepted: float | None = None  # to date, Accepted reports only
    reached_in_year: float | None = None
    year: int | None = None
    awaiting: float | None = None  # of Reached, what Submitted reports add (awaiting acceptance)
    awaiting_reports: int = 0
    months: dict[int, float] = field(default_factory=dict)  # month of the period end in ``year``
    years: dict[int, float] = field(default_factory=dict)
    by_location: dict[str, dict[str, Any]] = field(default_factory=dict)
    disaggregation: dict[str, dict[str, float]] = field(default_factory=dict)  # location -> key -> value
    prp_cumulative: float | None = None  # PRP's cumulative on the latest counted report
    prp_as_of: datetime.date | None = None
    mismatch: bool = False  # Reached differs from PRP's cumulative
    latest: Report | None = None
    method: str = ""
    period_method: str = ""
    ratio: bool = False
    percentage: bool = False
    value_status: str = NOT_REPORTED
    partial: bool = False  # the PD started before the reports are synced from
    source: str = "computed"  # computed | prp_cumulative (partial history)
    location_filtered: bool = False
    notes: list[str] = field(default_factory=list)
    # each counted report's period end, value and PRP cumulative, oldest first (for :meth:`through`)
    periods: list[tuple[datetime.date | None, Item, float | None]] = field(default_factory=list)
    # each counted report's value, by report key (for :meth:`awaiting_in`)
    items: dict[str, Item] = field(default_factory=dict)

    def awaiting_in(self, year: int) -> float | None:
        """Of Reached in ``year``, the part from reports still awaiting acceptance (Submitted): what a
        send-back would take off. None when no counted report of that year awaits acceptance."""
        in_year = [r for r in self.counted if r.period_end and r.period_end.year == year]
        submitted = [r for r in in_year if r.status == SUBMITTED]
        if not submitted:
            return None
        if self.additive:
            return sum(self.items[r.key][0] or 0.0 for r in submitted)

        def over(reports: list[Report]) -> float | None:
            if not reports:
                return None
            return combine_items(
                [self.items[r.key] for r in reports],
                self.period_method or "sum",
                ratio=self.ratio,
                percentage=self.percentage,
                fallback="latest",
            )[0]

        every = over(in_year)
        if every is None:
            return None
        accepted = over([r for r in in_year if r.status == ACCEPTED])
        return max(every - (accepted or 0.0), 0.0)

    def through(self, year: int) -> float | None:
        """Reached to the end of ``year``: the counted reports whose period ends by 31 December of that
        year, combined as Reached is (with a partial history, PRP's cumulative on the latest of them
        when it gives one). None when no counted report ends by then."""
        cut = datetime.date(year, 12, 31)
        upto = [p for p in self.periods if p[0] is not None and p[0] <= cut]
        if not upto:
            return None
        if self.partial and upto[-1][2] is not None:
            return upto[-1][2]
        return combine_items(
            [item for _end, item, _prp in upto],
            self.period_method or "sum",
            ratio=self.ratio,
            percentage=self.percentage,
            fallback="latest",
        )[0]

    @property
    def additive(self) -> bool:
        """Whether the locations and the periods add up to Reached (both methods "sum")."""
        return not self.ratio and self.method in ("", "sum") and self.period_method in ("", "sum")

    @property
    def method_label(self) -> str:
        return method_label(self.method, self.period_method, self.ratio)


def method_label(method: str, period_method: str, ratio: bool = False) -> str:
    """How the figure is combined, in words ("sum of locations and periods", "max of periods")."""
    if (
        ratio
        and method_of(method) not in ("max", "avg")
        and method_of(period_method) not in ("max", "avg", "latest")
    ):
        return "numerators over denominators"
    words = {"": "sum", "sum": "sum", "max": "max", "avg": "average", "latest": "latest", "ratio": "ratio"}
    across = words.get(method_of(method), method_of(method))
    over = words.get(method_of(period_method), method_of(period_method))
    if across == over:
        return f"{across} of locations and periods"
    return f"{across} of locations, {over} of periods"


def location_method_label(method: str, ratio: bool = False) -> str:
    """How a report's locations are combined, in words ("sum of locations", "max of locations")."""
    method = method_of(method)
    if method == "max":
        return "max of locations"
    if method == "avg":
        return "average of locations"
    if ratio or method == "ratio":
        return "numerators over denominators"
    return "sum of locations"


def compute(
    rows: Iterable[dict[str, Any]],
    *,
    display_type: str = "",
    unit: str = "",
    year: int | None = None,
    today: datetime.date,
    progress_values: dict[str, float] | None = None,
    only: set[str] | None = None,
    partial: bool = False,
    report_status: dict[str, str] | None = None,
) -> Reached:
    """Reached for one indicator from its report rows (one report type, read in ``ROW_ORDER``).

    ``only``: the locations to keep, for a location filter, by :func:`location_key` (the P-code) or by
    normalised name (:func:`location_filter`): the figures are then those locations' own Reached, and
    PRP's report totals and cumulative (for the whole PD) are not used. ``report_status``: each
    progress report's status word (:func:`report_status_texts`)."""
    rows = list(rows)
    ratio = is_ratio(
        display_type,
        unit,
        *{r.get("calculation_across_locations") or "" for r in rows},
        *{r.get("calculation_across_periods") or "" for r in rows},
    )
    percentage = (display_type or "").lower() == "percentage" or (unit or "").lower() == "percentage"
    reports = build_reports(
        rows,
        ratio=ratio,
        percentage=percentage,
        today=today,
        progress_values=None if only else progress_values,
        report_status=report_status,
    )
    out = Reached(reports=reports, year=year, ratio=ratio, percentage=percentage, partial=partial)
    out.location_filtered = only is not None
    latest_any = next((r for r in reversed(reports) if r.method or r.period_method), None)
    out.method = latest_any.method if latest_any else ""
    out.period_method = latest_any.period_method if latest_any else ""

    def wanted(place: str, report: Report) -> bool:
        return only is None or place in only or norm(report.places.get(place, {}).get("name")) in only

    def item(report: Report) -> Item:
        if only is None:
            return report.item
        return combine_items(
            # a report total repeated on each row says nothing about one location
            [c.item for p, c in report.cells.items() if wanted(p, report) and not c.repeated],
            report.method or "sum",
            ratio=ratio,
            percentage=percentage,
        )

    def over(items: list[Item]) -> Item:
        return combine_items(
            items, out.period_method or "sum", ratio=ratio, percentage=percentage, fallback="latest"
        )

    values = {r.key: item(r) for r in reports}
    out.counted = counted = [r for r in reports if r.counted and values[r.key][0] is not None]
    out.periods = [(r.period_end, values[r.key], r.prp_cumulative if only is None else None) for r in counted]
    out.items = {r.key: values[r.key] for r in counted}
    out.reached = over([values[r.key] for r in counted])[0]
    accepted = [r for r in counted if r.status == ACCEPTED]
    out.reached_accepted = over([values[r.key] for r in accepted])[0]
    submitted = [r for r in counted if r.status == SUBMITTED]
    out.awaiting_reports = len(submitted)
    if submitted:
        if out.additive:
            out.awaiting = sum(values[r.key][0] for r in submitted)
        elif out.reached is not None and (out.reached_accepted is None or out.reached > out.reached_accepted):
            out.awaiting = out.reached - (out.reached_accepted or 0.0)
    by_year: dict[int, list[Report]] = defaultdict(list)
    for r in counted:
        if r.period_end:
            by_year[r.period_end.year].append(r)
    out.years = {y: over([values[r.key] for r in reps])[0] for y, reps in sorted(by_year.items())}
    if year is not None:
        in_year = by_year.get(year, [])
        out.reached_in_year = over([values[r.key] for r in in_year])[0] if in_year else None
        by_month: dict[int, list[Report]] = defaultdict(list)
        for r in in_year:
            by_month[r.period_end.month].append(r)
        out.months = {m: over([values[r.key] for r in reps])[0] for m, reps in sorted(by_month.items())}
    out.latest = counted[-1] if counted else None
    if out.latest is not None and only is None:
        out.prp_cumulative, out.prp_as_of = out.latest.prp_cumulative, out.latest.period_end
        out.mismatch = (
            out.prp_cumulative is not None
            and out.reached is not None
            and not close(out.prp_cumulative, out.reached, ratio=ratio, percentage=percentage)
            and not close(out.prp_cumulative, out.reached_accepted, ratio=ratio, percentage=percentage)
        )
        if partial and out.prp_cumulative is not None:
            out.reached, out.source, out.mismatch = out.prp_cumulative, "prp_cumulative", False
            out.notes.append(
                "The programme document started before the partner reports NeuroDB reads: Reached is "
                "PRP's own cumulative on the latest counted report."
            )
    out.by_location = _locations(reports, counted, values, year, over, wanted)
    out.disaggregation = {place: keys for place in out.by_location if (keys := _keys(place, counted, over))}
    if out.reached is None:
        out.value_status = NOT_REPORTED
    elif out.reached == 0:
        out.value_status = REPORTED_ZERO
    else:
        out.value_status = REPORTED
    seen = set()
    for r in reports:
        for note in r.notes:
            if note not in seen and r.counted:
                seen.add(note)
                out.notes.append(f"{r.number or r.key}: {note}")
    return out


def _locations(reports, counted, values, year, over, wanted) -> dict[str, dict[str, Any]]:
    """Per location: Reached to date and in the year, months, PRP's cumulative there, and its value in
    each report (``cells``, every status; the report total repeated on each row has none)."""
    out: dict[str, dict[str, Any]] = {}
    for report in reports:
        if report.status == NOT_YET_DUE:
            continue
        for place, cell in report.cells.items():
            if not place or not wanted(place, report):
                continue
            meta = report.places.get(place, {})
            entry = out.setdefault(
                place,
                {
                    "key": place,
                    "name": meta.get("name") or "",
                    "p_code": meta.get("p_code") or "",
                    "id": meta.get("id"),
                    "cells": {},
                    "reached": None,
                    "achieved": None,
                    "months": {},
                    "prp_cumulative": None,
                    "period": None,
                    "report": "",
                },
            )
            if entry["id"] is None:
                entry["id"] = meta.get("id")
            entry["cells"][report.key] = None if cell.repeated else cell.value
    for place, entry in out.items():
        mine = [
            r
            for r in counted
            if place in r.cells and not r.cells[place].repeated and r.cells[place].value is not None
        ]
        entry["reached"] = over([r.cells[place].item for r in mine])[0]
        if year is not None:
            in_year = [r for r in mine if r.period_end and r.period_end.year == year]
            entry["achieved"] = over([r.cells[place].item for r in in_year])[0] if in_year else None
            months: dict[int, list[Report]] = defaultdict(list)
            for r in in_year:
                months[r.period_end.month].append(r)
            entry["months"] = {
                m: over([r.cells[place].item for r in reps])[0] for m, reps in sorted(months.items())
            }
        with_place = [r for r in counted if place in r.cells]
        if with_place:
            last = with_place[-1]
            entry["prp_cumulative"] = last.cells[place].prp_cumulative
            entry["period"], entry["report"] = last.period_end, last.key
    return dict(sorted(out.items(), key=lambda kv: (norm(kv[1]["name"]), kv[1]["p_code"])))


def _keys(place: str, counted: list[Report], over) -> dict[str, float]:
    """Reached per disaggregation key at one location (labelled and grouped by
    ``disaggregation.reached_by_disaggregation``)."""
    keys: dict[str, list[Item]] = defaultdict(list)
    for report in counted:
        cell = report.cells.get(place)
        if cell is None:
            continue
        for key, value in cell.parts.items():
            keys[key].append((value, None, None))
    return {key: over(items)[0] for key, items in sorted(keys.items())}


# ------------------------------------------------------------------------------------ data access
def history_start(today: datetime.date | None = None) -> datetime.date:
    """The first period start the partner reports are synced from: the last
    ``ETOOLS_DATAMART_REPORTING_YEARS`` years and, unless ``ETOOLS_DATAMART_REPORTING_FROM_PD_START``
    is off, the start of every running programme document (never more than ``MAX_HISTORY_YEARS``
    back). The Datamart sync reads from this date (:func:`reports_read_from` is the date the latest
    sync did read from)."""
    from django.conf import settings
    from django.db.models import Min, Q
    from django.utils import timezone

    from neurodb.partnerships.models import PCA

    from .monitoring import ACTIVE_PD_STATUSES

    today = today or timezone.localdate()
    since = datetime.date(today.year - settings.ETOOLS_DATAMART_REPORTING_YEARS + 1, 1, 1)
    if getattr(settings, "ETOOLS_DATAMART_REPORTING_FROM_PD_START", True):
        floor = datetime.date(today.year - MAX_HISTORY_YEARS, 1, 1)
        earliest = (
            PCA.objects.exclude(status__in=("draft", "cancelled"))
            .filter(Q(status__in=ACTIVE_PD_STATUSES) | Q(end__gte=today), start__gte=floor)
            .aggregate(first=Min("start"))["first"]
        )
        if earliest is not None and earliest < since:
            since = earliest
    return since


def reports_read_from(today: datetime.date | None = None) -> datetime.date:
    """The first period start NeuroDB holds partner reports from: the window of the latest partner
    reports sync that finished (``read_from`` in its details; a run made before that was recorded
    read the last ``ETOOLS_DATAMART_REPORTING_YEARS`` years of its day), else :func:`history_start`.
    A PD that started before it has a partial history. It is the window actually read, not the one
    the next sync will read: when the window is widened (a new running PD, the setting turned on,
    a deployment), the early reports count only once a sync has read them."""
    from django.conf import settings
    from django.utils import timezone

    from neurodb.core.models import SyncRun

    runs = SyncRun.objects.filter(
        job=SyncRun.Job.ETOOLS_DATAMART,
        target="partner_reports",
        status__in=(SyncRun.Status.SUCCEEDED, SyncRun.Status.PARTIAL),
    ).order_by("-started_at", "-pk")
    for started, details in runs.values_list("started_at", "details")[:20]:
        details = details if isinstance(details, dict) else {}
        if details.get("empty_response"):
            continue  # nothing was read: the rows held are those of an earlier run
        try:
            return datetime.date.fromisoformat(str(details.get("read_from") or "")[:10])
        except ValueError:
            day = timezone.localtime(started).date() if timezone.is_aware(started) else started.date()
            return datetime.date(day.year - settings.ETOOLS_DATAMART_REPORTING_YEARS + 1, 1, 1)
    return history_start(today)


def report_rows(pd_ids: Iterable[int], report_types: Iterable[str]):
    """The partner report rows of these PDs and report types, every year, in ``ROW_ORDER``."""
    from . import models as dm

    return (
        dm.ReportedIndicator.objects.filter(
            intervention_id__in=list(pd_ids), report_type__in=list(report_types)
        )
        .order_by(*ROW_ORDER)
        .values(*ROW_FIELDS)
    )


def progress_values(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """PRP's own report totals (``progress_value`` of ``prp/indicator-report-v2``) by indicator report,
    for these report rows. A v2 record is found by its id and kept only when it is about the same
    programme document and indicator as the rows (its PD link, its ``performance_indicator``): that the
    two datasets share their ids is not confirmed, and another indicator's total must never stand in
    for this one's."""
    from django.db.models.fields.json import KT

    from .models import DatamartDocument

    expected: dict[int, tuple[set[int], set[str]]] = {}
    for row in rows:
        key = str(row.get("indicator_report") or "")
        if not key.isdigit() or len(key) > 18:
            continue
        pds, titles = expected.setdefault(int(key), (set(), set()))
        if row.get("intervention_id"):
            pds.add(row["intervention_id"])
        if norm(row.get("indicator")):
            titles.add(norm(row.get("indicator")))
    ids = sorted(expected)
    out: dict[str, Any] = {}
    for start in range(0, len(ids), 5000):
        found = (
            DatamartDocument.objects.filter(
                dataset="prp_indicator_reports", source_id__in=ids[start : start + 5000]
            )
            .annotate(_value=KT("data__progress_value"), _title=KT("data__performance_indicator"))
            .values_list("source_id", "intervention_id", "_title", "_value")
        )
        for source_id, pd_id, title, value in found:
            if value in (None, ""):
                continue
            pds, titles = expected[source_id]
            title = norm(title)
            same = (pd_id is not None and pd_id in pds) or (bool(title) and title in titles)
            other_pd = pd_id is not None and bool(pds) and pd_id not in pds
            other_title = bool(title) and bool(titles) and title not in titles
            if same and not other_pd and not other_title:
                out[str(source_id)] = value
    return out
