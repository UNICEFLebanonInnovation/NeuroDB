"""eTools reporting checks: what the partner report data looks like, for the "Reached" rule.

``neurodb.datamart.reached`` is built to be right whichever way these questions come out; this page
(admin → eTools reporting checks, Administrators only, linked from Data health) shows how they do
come out on the data NeuroDB holds, with a plain verdict and what NeuroDB does in each case. Every
query is read-only and every figure a count: no person's data, and no title, name or text of a record
(key names, section names, PD reference numbers NeuroDB does not hold and business area codes at most).

The checks measure what NeuroDB itself reads. PRP's values are read with Reached's own parser
(:func:`_value`: a number, its text or PRP's ``{v, d, c}``); a progress report is told apart by its
programme document, type, number and period (:func:`_report`), whatever PRP's own ids hold; PRP's
cumulative is compared through :func:`monitoring.indicators`, the figures of Partner monitoring's Data
quality tab; the report rows a check reads in Python come 2,000 at a time and stop at the deadline as
the queries do (:func:`_each`).

The checks run as a job (:func:`run_job`: the ``reporting-checks`` schedule every night, or *Run the
checks now* on the page and *Run a job*), with ``JOB_TIMEOUT_MS`` for each statement and
``JOB_BUDGET_S`` for them all, and the result is stored in its run's details: the page shows the latest
one at once. Only when no run has stored a result yet does the page run them itself, with
``TIMEOUT_MS`` per statement and ``PAGE_BUDGET_S`` for them all (well below the web server's 120-second
limit), so it always answers: a check left out says so.
"""

from __future__ import annotations

import calendar
import datetime
import functools
import itertools
import json
import logging
import re
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

from django.conf import settings
from django.contrib.postgres.aggregates import ArrayAgg
from django.core.serializers.json import DjangoJSONEncoder
from django.db import DatabaseError, connection, transaction
from django.db.models import Case, Count, Max, Min, Q, TextField, Value, When
from django.db.models.fields.json import KT, KeyTransform
from django.db.models.functions import Coalesce, NullIf, Trim
from django.utils import timezone

from neurodb.partnerships import statuses as pd_statuses
from neurodb.partnerships.models import PCA

from . import models as dm
from . import reached as rc
from . import reporting
from .disaggregation import ids_in, parse_key

logger = logging.getLogger(__name__)

TIMEOUT_MS = 20_000  # one statement, on the page
PAGE_BUDGET_S = 75.0  # every check of the page together
JOB_TIMEOUT_MS = 600_000  # one statement, in the job (no web request waits for it)
JOB_BUDGET_S = 3600.0  # every check of the job together
LOCK_ID = 7140438  # one run of the checks at a time; neurodb.integrations.background.REPORTING_CHECKS_LOCK_ID
PRP_COMPARE_MIN_S = 30.0  # the comparison with PRP (check 5) computes Reached: only with this much left
QUERY_CANCELED = "57014"  # PostgreSQL's code for a statement its statement_timeout stopped
OK, WARN, INFO = "ok", "warning", "info"
PRP = "prp_indicator_reports"  # PRP's indicator reports (prp/indicator-report-v2): checks 8, 9 and 11
CHUNK = 2000  # report rows a check reads at a time in Python (:func:`_each`)
STEP_MONTHS = {"QPR": 3, "HR": 1}  # how far apart two reports of a type end (checks 10 and 11)
LATE_FIRST_DAYS = 100  # a running PD whose first report held ends this long after its start (check 10)
SECTIONS_SHOWN = 5  # the section names check 10 shows per row of its table
KEYS_LISTED = 150  # the record keys check 7 lists at most


@dataclass
class Check:
    key: str
    title: str
    question: str
    neurodb: str  # what NeuroDB does, whatever the answer
    run: Callable[[Any], dict[str, Any]]


class OutOfTime(Exception):
    """The time for the checks (the page's or the job's) is used up."""


class _Timed:
    """A cursor whose every statement stops after ``timeout_ms`` (``TIMEOUT_MS`` on the page) and at the
    deadline, whichever comes first (a check runs several statements: the deadline bounds them all)."""

    def __init__(self, cursor, deadline: float, timeout_ms: int = TIMEOUT_MS) -> None:
        self.cursor, self.deadline, self.timeout_ms = cursor, deadline, timeout_ms
        self.last_ms = timeout_ms  # the statement timeout set last: below timeout_ms, the deadline set it

    def remaining(self) -> float:
        """Seconds left before the deadline."""
        return self.deadline - time.monotonic()

    def execute(self, sql: str, params: list[Any] | None = None) -> None:
        left = min(self.timeout_ms, int(self.remaining() * 1000))
        if left <= 0:
            raise OutOfTime
        self.last_ms = left
        self.cursor.execute(f"SET LOCAL statement_timeout = {left}")
        self.cursor.execute(sql, params)

    def bound(self, statements: int = 1) -> None:
        """Set the statement timeout for queries run outside :meth:`execute` (the ORM, on the same
        transaction): the next ``statements`` of them share the time left, ``timeout_ms`` at most each."""
        left = min(self.timeout_ms, int(self.remaining() * 1000 / max(statements, 1)))
        if left <= 0:
            raise OutOfTime
        self.last_ms = left
        self.cursor.execute(f"SET LOCAL statement_timeout = {left}")

    def fetchone(self) -> tuple:
        return self.cursor.fetchone()

    def fetchall(self) -> list[tuple]:
        return self.cursor.fetchall()


def _bound(cursor, statements: int = 1) -> None:
    """:meth:`_Timed.bound` on the checks' timed cursor, on the page or in the job (a plain cursor, in a
    test, has no deadline)."""
    if hasattr(cursor, "bound"):
        cursor.bound(statements)


def _one(cursor, sql: str, params: list[Any] | None = None) -> tuple:
    cursor.execute(sql, params)
    return cursor.fetchone()


def _all(cursor, sql: str, params: list[Any] | None = None) -> list[tuple]:
    cursor.execute(sql, params)
    return cursor.fetchall()


def _each(cursor, queryset, *fields: str) -> Iterator[tuple]:
    """The rows of ``queryset`` as tuples of ``fields``, :data:`CHUNK` at a time from a server-side cursor:
    each fetch is bounded by the time left for the checks (the statement timeout), and so is the Python
    that reads them (:class:`OutOfTime` once it is used up)."""
    _bound(cursor)
    timed = hasattr(cursor, "remaining")
    for n, row in enumerate(queryset.order_by().values_list(*fields).iterator(chunk_size=CHUNK)):
        if timed and n % CHUNK == 0 and cursor.remaining() <= 0:
            raise OutOfTime
        yield row


def _day(day: datetime.date | None) -> str | None:
    """A date in a check's table, as text ("31 Mar 2026", in English whatever the language, as check
    11's months): the page shows the same whether it ran the checks itself or reads what the job
    stored."""
    return f"{day.day} {day:%b %Y}" if day else None


def _local_day(moment: datetime.datetime) -> datetime.date:
    return timezone.localtime(moment).date() if timezone.is_aware(moment) else moment.date()


def _duration(ms: int) -> str:
    """A time limit in words: "10 minutes", "20 seconds", "1 second"."""
    if ms >= 60_000 and ms % 60_000 == 0:
        n, unit = ms // 60_000, "minute"
    else:
        n, unit = max(ms // 1000, 1), "second"
    return f"{n} {unit}{'' if n == 1 else 's'}"


def _share(part: int, whole: int) -> str:
    return f"{round(100 * part / whole)} %" if whole else "—"


def _count(value: Any) -> int:
    """A count from a run's details (0 when it is not one)."""
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _plural(n: int, one: str, many: str) -> str:
    return f"{n:,} {one if n == 1 else many}"


# ------------------------------------------------------------------------- values and identities
@functools.lru_cache(maxsize=65_536)
def _text_value(text: str) -> float | None:
    found = rc.parts(text)
    return found.value() if found else None


def _value(raw: Any) -> float | None:
    """A PRP value as Reached reads it (:func:`reached.parts`: ``c``, else v over a real d, else v): a
    number, its text, or PRP's ``{"v", "d", "c"}`` as a dict or as its text (Python's or JSON's); None
    when it holds no number. A text is read once, however many rows repeat it."""
    if isinstance(raw, str):
        return _text_value(raw)
    found = rc.parts(raw)
    return found.value() if found else None


def _structured(raw: Any) -> bool:
    """Whether a value is written as PRP's ``{v, d, c}`` (a dict, or its text)."""
    return isinstance(raw, dict) or (isinstance(raw, str) and raw.lstrip().startswith("{"))


def _report(pd_id: int | None, reference: str, kind: str, number: str, start: Any, end: Any) -> tuple:
    """A progress report as the rows tell it apart, whatever PRP's own ids hold (they can be empty, or one
    value for many reports): its programme document (NeuroDB's link, else the reference number the row
    names), type, number and period. Checks 1 to 3, 7 and 11 count reports by it."""
    pd = pd_id if pd_id is not None else f"ref:{reference or ''}"
    return (pd, (kind or "").strip().upper(), number or "", start, end)


# :func:`_report` in SQL (check 7)
REPORT_SQL = (
    "concat_ws('|', coalesce(intervention_id::text, 'ref:' || pd_reference_number), "
    "upper(btrim(report_type)), report_number, coalesce(period_start::text, ''), "
    "coalesce(period_end::text, ''))"
)


def _indicator(indicator_report: str, etools_id: str, title: str, output: str) -> str:
    """One indicator in its report (an *indicator report*): PRP's indicator report id, else its eTools id,
    its title and its PD output (case and spaces aside), as Reached tells two indicators of one report apart
    (:func:`reached.indicator_report_key`, ``reporting.INDICATOR_REPORT`` on the tiles)."""
    return str(indicator_report or "") or f"t|{(etools_id or '').strip()}|{rc.norm(title)}|{rc.norm(output)}"


def _place(p_code: str, location: str) -> str:
    """A row's location as Reached keys it (the P-code, else the name; "" when it names neither)."""
    return rc.location_key(p_code, location) if (p_code or location) else ""


def _number(text: Any) -> int | None:
    """A report's number from its name ("QPR2" → 2, "HR10" → 10); None without one (or above 99)."""
    digits = re.sub(r"\D", "", str(text or ""))
    return int(digits) if digits and len(digits) <= 2 else None


def _month_end(day: datetime.date, months: int) -> datetime.date:
    """The last day of the month ``months`` after (or before) the month of ``day``."""
    year, month = divmod(day.year * 12 + day.month - 1 + months, 12)
    return datetime.date(year, month + 1, calendar.monthrange(year, month + 1)[1])


class _Statuses:
    """Each progress report's status (:func:`reached.status_of`) from its rows, as every page reads it:
    the word of its rows changed last (:func:`reached.newest_status`), with the report's dates."""

    def __init__(self, today: datetime.date) -> None:
        self.today = today
        self.words: dict[tuple, dict[str, rc.Stamp]] = defaultdict(dict)
        self.dates: dict[tuple, list[Any]] = {}

    def add(self, report: tuple, text: str, dates: tuple, stamp: rc.Stamp) -> None:
        words = self.words[report]
        words[text or ""] = rc.stamp(words.get(text or ""), stamp)
        known = self.dates.setdefault(report, list(dates))
        for i, value in enumerate(dates):  # the first date given, as Reached reads a report's fields
            if known[i] is None:
                known[i] = value

    def counted(self) -> set[tuple]:
        """The reports that count in Reached (Accepted or Submitted)."""
        out = set()
        for report, words in self.words.items():
            end, due, submitted = self.dates[report]
            status = rc.status_of(
                rc.newest_status(words),
                period_end=end,
                due_date=due,
                submission_date=submitted,
                today=self.today,
            )
            if status in rc.COUNTED:
                out.add(report)
        return out


_ROW = (
    "intervention_id",
    "pd_reference_number",
    "report_type",
    "report_number",
    "period_start",
    "period_end",
    "report_status",
    "due_date",
    "submission_date",
    "last_modify_date",
    "synced_at",
    "datamart_id",
    "indicator_report",
    "etools_indicator_id",
    "indicator",
    "pd_output",
    "p_code",
    "location",
    "_total",
)


@dataclass(frozen=True)
class _Row:
    report: tuple  # :func:`_report`
    indicator: str  # :func:`_indicator`
    place: str  # :func:`_place`
    total: Any  # the disaggregation total "()" as stored (None: none)
    indicator_report: str
    etools_id: str
    title: str
    output: str  # the PD output the row names
    more: tuple  # the extra fields asked for, in their order


class _Rows:
    """Every partner report row, read once in Python (:func:`_each`) by the checks that parse its values:
    its report, indicator, place and "()" (only that key of its disaggregation), with ``extra`` fields;
    and, as they are read, each report's status (:attr:`statuses`)."""

    def __init__(self, cursor, today: datetime.date, *extra: str) -> None:
        self.cursor, self.extra = cursor, extra
        self.statuses = _Statuses(today)

    def __iter__(self) -> Iterator[_Row]:
        rows = dm.ReportedIndicator.objects.annotate(_total=KeyTransform("()", "disaggregation"))
        width = len(_ROW)
        for values in _each(self.cursor, rows, *_ROW, *self.extra):
            pd, ref, kind, number, start, end, text, due, sub, changed, synced, row_id, ir, eid, title = (
                values[:15]
            )
            output, p_code, location, total = values[15:width]
            report = _report(pd, ref, kind, number, start, end)
            self.statuses.add(report, text, (end, due, sub), (changed, synced, row_id))
            yield _Row(
                report,
                _indicator(ir, eid, title, output),
                _place(p_code, location),
                total,
                ir or "",
                eid or "",
                title or "",
                output or "",
                values[width:],
            )


# ------------------------------------------------------------------------------------- the checks
def _apart(found: list[tuple[float | None, str, str]]) -> dict[str, list[float | None]]:
    """The rows of one title at one location of one report, by the indicator report they come from, as
    Reached tells them apart (:func:`reached.one_indicator`): by PRP's indicator report id when they carry two
    or more, else by their PD output when they carry two or more; else they are one indicator report's.
    ``found``: each row's value, PRP's indicator report id and its PD output (case and spaces aside)."""
    ids = {ir for _, ir, _ in found if ir}
    outputs = {output for _, _, output in found if output}
    by = 1 if len(ids) > 1 else 2 if len(outputs) > 1 else None
    out: dict[str, list[float | None]] = defaultdict(list)
    for row in found:
        out[row[by] if by else ""].append(row[0])
    return out


def _differ(values: list[float | None]) -> bool:
    return len({round(v, 6) for v in values if v is not None}) > 1


def _duplicate_rows(cursor) -> dict[str, Any]:
    read = _Rows(cursor, datetime.date.today(), "progress_report")
    # each location of one title (its eTools id and title) in one report: each row's "()" value, PRP's
    # indicator report id and PD output, which tell two indicator reports of that title apart (:func:`_apart`)
    cells: dict[tuple, list[tuple[float | None, str, str]]] = defaultdict(list)
    # Reached's own key of a location (its report, reached.report_key, the indicator report and the place):
    # the reports it reads as one (none since a report is told apart by its PD, type, number and period: a
    # guard)
    as_one: dict[tuple, set[tuple]] = defaultdict(set)
    rows = 0
    for row in read:
        rows += 1
        title = f"{row.etools_id.strip()}|{rc.norm(row.title)}"
        cells[(row.report, title, row.place)].append(
            (_value(row.total), row.indicator_report, rc.norm(row.output))
        )
        if isinstance(row.report[0], int):  # Reached reads the rows of a PD NeuroDB holds only
            pd, kind, number, start, end = row.report
            key = rc.report_key(
                {
                    "intervention_id": pd,
                    "report_type": kind,
                    "progress_report": row.more[0],
                    "report_number": number,
                    "period_start": start,
                    "period_end": end,
                }
            )
            as_one[(key, row.indicator, row.place)].add(row.report)
    if not rows:
        return {"tone": INFO, "verdict": "No partner report rows are held yet.", "figures": []}
    # the locations two or more indicator reports of one title give in one report (two PRP indicators carry
    # the title), with their values; and each indicator report's location on several rows (duplicates)
    shared: dict[tuple, list[float | None]] = {}
    several: dict[tuple, list[float | None]] = {}
    for key, found in cells.items():
        reports = _apart(found)
        if len(reports) > 1:
            shared[key] = [value for value, _, _ in found]
        for name, values in reports.items():
            if len(values) > 1:
                several[(*key, name)] = values
    differ = {key for key, values in several.items() if _differ(values)}
    shared_differ = sum(1 for values in shared.values() if _differ(values))
    counted = read.statuses.counted()
    differ_counted = sum(1 for key in differ if key[0] in counted)
    spanning = sum(1 for reports in as_one.values() if len(reports) > 1)
    if not several:
        verdict = (
            "Each location appears on one row per indicator in each report (a report told apart by its "
            "programme document, type, number and period; an indicator by PRP's indicator report id, else "
            "its title and PD output)."
        )
    else:
        verdict = (
            f"{_plural(len(several), 'location appears', 'locations appear')} on more than one row of one "
            "indicator in one report"
        )
        if differ:
            verdict += (
                f"; {len(differ):,} of them carry different values, {differ_counted:,} in reports that "
                "count: Reached uses the largest, never their sum."
            )
        else:
            verdict += ": their rows carry the same value, which Reached reads once."
    if shared:
        verdict += (
            f" At {_plural(len(shared), 'location', 'locations')}, two or more indicator reports of one "
            f"report carry the same title ({shared_differ:,} with different values, "
            f"{len(shared) - shared_differ:,} with the same value): Reached keeps the one under the PD "
            "indicator's own output, else leaves that report out with a note, never the largest of them nor "
            "their sum (check 7 counts the rows Partner monitoring places)."
        )
    if spanning:
        verdict += (
            f" Reached reads several reports as one at {_plural(spanning, 'location', 'locations')} (it "
            "keeps the largest value of each). This should not happen: send this check's figures to the "
            "NeuroDB team."
        )
    return {
        "tone": WARN if differ_counted or spanning else OK,
        "verdict": verdict,
        "figures": [
            ("Partner report rows", rows),
            ("Locations on several rows of one indicator in one report", len(several)),
            ("… rows more than one per location", sum(len(values) - 1 for values in several.values())),
            ("… with different values", len(differ)),
            ("… of which in reports that count (Reached takes the largest)", differ_counted),
            ("Locations two or more indicator reports of one title give in one report", len(shared)),
            ("… carrying different values (Reached keeps the one under its own output)", shared_differ),
            ("Locations Reached reads as one report across several reports", spanning),
        ],
    }


def _repeated_value(cursor) -> dict[str, Any]:
    read = _Rows(cursor, datetime.date.today(), "achievement_in_period", "calculation_across_locations")
    # each indicator in each report: its locations' "()" values, the values in the period of its rows and how
    # PRP combines its locations
    places: dict[tuple, dict[str, list[float | None]]] = defaultdict(lambda: defaultdict(list))
    achieved: dict[tuple, set[float | None]] = defaultdict(set)
    methods: dict[tuple, str] = {}
    no_total = 0
    for row in read:
        key = (row.report, row.indicator)
        total = _value(row.total)
        no_total += total is None
        places[key][row.place].append(total)
        value = _value(row.more[0])
        achieved[key].add(None if value is None else round(value, 6))
        methods.setdefault(key, rc.method_of(row.more[1]))
    counted = read.statuses.counted()
    several = same = zero = risky = report_total = 0
    for key, cells in places.items():
        if len(cells) < 2:
            continue
        several += 1
        # one value per location, as Reached chooses it: the largest of its rows
        values = [max((v for v in found if v is not None), default=None) for found in cells.values()]
        if None in values:
            continue
        if len({round(v, 6) for v in values}) == 1:
            same += 1
            if values[0] == 0:
                zero += 1
            elif key[0] in counted and methods[key] in ("", "sum"):  # only a sum adds a total up again
                risky += 1
        given = next(iter(achieved[key])) if len(achieved[key]) == 1 else None
        if given is not None and rc.close(given, rc.combine(values, methods[key] or "sum")):
            report_total += 1
    if not several:
        return {
            "tone": INFO,
            "verdict": "No indicator report has more than one location yet.",
            "figures": [('Rows whose "()" gives no number', no_total)],
        }
    verdict = (
        f"In {same:,} of {several:,} indicator reports with several locations ({_share(same, several)}), "
        f'every location carries the same "()" ({zero:,} of them all zero)'
    )
    if risky:
        verdict += (
            f"; {risky:,} of them, in reports that count and add up their locations, carry the same value "
            "above zero on every location: if it is the report total repeated, adding up the locations "
            "counts it once per location."
        )
    else:
        verdict += (
            "; none in a report that counts and adds up its locations carries the same value above zero on "
            "every location."
        )
    if report_total:
        verdict += (
            ' The value in the period is the report total (the locations\' "()" combined) repeated on every '
            f"row in {report_total:,} of them."
        )
    if no_total:
        verdict += (
            f' {_plural(no_total, "row gives", "rows give")} no number in "()": only for those does Reached '
            "read the running totals or the value in the period (a value repeated on every location is then "
            "the report total)."
        )
    else:
        verdict += ' Every row\'s "()" holds a number: the value in the period decides nothing in Reached.'
    return {
        "tone": WARN if risky else OK,
        "verdict": verdict,
        "figures": [
            ("Indicator reports with several locations", several),
            ('… with the same "()" on every location', same),
            ("… of which all zero", zero),
            ("… of which above zero, in reports that count and add up their locations", risky),
            ('… where the value in the period is the locations\' "()" combined', report_total),
            ('Rows whose "()" gives no number', no_total),
        ],
    }


def _disaggregation(cursor) -> dict[str, Any]:
    (with_total,) = _one(
        cursor,
        "SELECT count(*) FILTER (WHERE disaggregation ? '()') FROM datamart_reportedindicator "
        "WHERE jsonb_typeof(disaggregation) = 'object'",
    )
    keys = _all(
        cursor,
        "SELECT k, count(*) FROM datamart_reportedindicator, jsonb_object_keys(disaggregation) k "
        "WHERE jsonb_typeof(disaggregation) = 'object' GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 20",
    )
    # the labels of the value ids the keys name (read by the partner reports sync, by id)
    seen = ids_in(
        k
        for (k,) in _all(
            cursor,
            "SELECT DISTINCT k FROM datamart_reportedindicator, jsonb_object_keys(disaggregation) k "
            "WHERE jsonb_typeof(disaggregation) = 'object'",
        )
    )
    labels = (
        dict(
            _all(
                cursor,
                "SELECT prp_id, label FROM datamart_disaggregationlabel "
                "WHERE kind = 'value' AND label <> '' AND prp_id = ANY(%s)",
                [sorted(seen)],
            )
        )
        if seen
        else {}
    )
    # both sides read as Reached reads them (a number, its text or PRP's {v, d, c}), row by row and per
    # indicator report: is the value in the period each location's own, or the report total repeated?
    read = _Rows(
        cursor,
        datetime.date.today(),
        "achievement_in_period",
        "calculation_across_locations",
        "calculation_across_periods",
    )
    rows = total_number = given = written = compared = equal = 0
    reports: dict[tuple, list[tuple[str, float | None, float | None]]] = defaultdict(list)
    methods: dict[tuple, tuple[str, bool]] = {}
    for row in read:
        rows += 1
        raw, across, periods = row.more
        total, value = _value(row.total), _value(raw)
        ratio = rc.is_ratio("", "", across or "", periods or "")
        total_number += total is not None
        given += value is not None
        written += value is not None and _structured(raw)
        if total is not None and value is not None:
            compared += 1
            equal += rc.close(total, value, ratio=ratio)
        reports[(row.report, row.indicator)].append((row.place, total, value))
        methods.setdefault((row.report, row.indicator), (rc.method_of(across), ratio))
    several = own = repeated = 0
    for key, cells in reports.items():
        if len({place for place, _, _ in cells}) < 2 or any(t is None or v is None for _, t, v in cells):
            continue
        several += 1
        method, ratio = methods[key]
        if all(rc.close(t, v, ratio=ratio) for _, t, v in cells):
            own += 1
            continue
        by_place: dict[str, float] = {}
        for place, total, _ in cells:  # one value per location: the largest of its rows, as Reached
            by_place[place] = max(by_place.get(place, total), total)
        values = {round(v, 6) for _, _, v in cells}
        if len(values) == 1 and rc.close(
            cells[0][2], rc.combine(list(by_place.values()), method or "sum"), ratio=ratio
        ):
            repeated += 1
    if not rows or (not with_total and not total_number):
        verdict, tone = 'No row carries a disaggregation total ("()") yet: the other fields are used.', INFO
    else:
        if total_number == rows:
            verdict = (
                "Every row's \"()\" holds a number: Reached reads each location's value from it, and the "
                "value in the period decides nothing."
            )
        else:
            verdict = (
                f'{rows - total_number:,} of {rows:,} rows give no number in "()": Reached reads those '
                "locations from their running totals or the value in the period."
            )
        if compared:
            verdict += (
                f' Where both are numbers, "()" equals the value in the period on {equal:,} of {compared:,} '
                f"rows ({_share(equal, compared)})."
            )
        else:
            verdict += (
                ' "()" and the value in the period could not be compared: no row gives both as a number.'
            )
        if several:
            verdict += (
                f" In {several:,} indicator reports with several locations, the value in the period is each "
                f"location's own in {own:,} and the report total repeated on every location in {repeated:,}."
            )
        tone = OK if total_number == rows else INFO
    if seen:
        verdict += (
            f" The labels of {len(labels):,} of the {len(seen):,} disaggregation values named are read."
        )

    def named(key: str) -> str | None:
        parts = parse_key(key)
        if parts == ():
            return "Location total"
        if parts and all(isinstance(p, int) for p in parts):
            found = [labels.get(p) for p in parts]
            return " × ".join(found) if all(found) else None
        return None

    return {
        "tone": tone,
        "verdict": verdict,
        "figures": [
            ('Rows with a disaggregation total ("()")', with_total),
            ('Rows whose "()" holds a number', total_number),
            ("… holding none (Reached reads them another way)", rows - total_number),
            ("Rows whose value in the period is a number", given),
            ("… written as PRP's {v, d, c}", written),
            ('Rows compared ("()" and the value in the period both numbers)', compared),
            ("… equal", equal),
            ("Indicator reports with several locations, compared", several),
            ("… the value in the period is each location's own", own),
            ("… it is the report total, repeated on every location", repeated),
            ("Disaggregation values named by the reports", len(seen)),
            ("… whose label is read", len(labels)),
        ],
        "table": {
            "columns": ("Disaggregation key", "Label", "Rows"),
            "rows": [(key, named(key), n) for key, n in keys],
        },
    }


# A report counts in Reached by the rule of ``reached.status_of``: PRP's word Accepted or Submitted,
# or a submission date with no word that says otherwise (Sent back, Due, Overdue).
_WORD = "lower(trim(regexp_replace(replace(coalesce(report_status, ''), '_', ' '), '\\s+', ' ', 'g')))"
COUNTS_SQL = (
    f"CASE WHEN {_WORD} IN ('accepted', 'acc', 'submitted', 'sub') OR (submission_date IS NOT NULL "
    f"AND {_WORD} NOT IN ('sent back', 'sen', 'due', 'ove', 'overdue')) THEN 'yes' ELSE 'no' END"
)


def _unsubmitted(cursor) -> dict[str, Any]:
    found = _all(
        cursor,
        "SELECT coalesce(nullif(trim(report_status), ''), '(none)'), "  # noqa: S608 (constants only)
        f"{COUNTS_SQL}, CASE WHEN period_end > current_date THEN 'yes' ELSE 'no' END, achievement_in_period, "
        "total_cumulative_progress, count(*) FROM datamart_reportedindicator GROUP BY 1, 2, 3, 4, 5",
    )
    # the values read as Reached reads them (PRP's empty {"v": 0, "d": 0, "c": 0} and "0.0" are 0, "" is
    # none); "Value given" counts only a value other than 0, and a cumulative of {"c": 0, ...} is one of 0
    table: dict[tuple, list[int]] = {}
    for status, counts, running, achieved, cumulative, n in found:
        line = table.setdefault((status, counts, running), [0, 0, 0, 0])
        value, total = _value(achieved), _value(cumulative)
        line[0] += n
        line[1] += n if total == 0 else 0
        line[2] += n if total is None else 0
        line[3] += n if value not in (None, 0.0) else 0
    rows = [(*key, *line) for key, line in sorted(table.items())]
    carrying = sum(r[6] for r in rows if r[1] == "no")
    return {
        "tone": INFO if carrying else OK,
        "verdict": (
            f"{carrying:,} {'row' if carrying == 1 else 'rows'} of reports that do not count (sent back, "
            f"due, overdue or not yet due) {'carries' if carrying == 1 else 'carry'} a value."
            if carrying
            else "Reports that do not count carry no value."
        ),
        "figures": [("Rows of reports that do not count, with a value", carrying)],
        "table": {
            "columns": (
                "PRP status",
                "Counts in Reached",
                "Period not ended",
                "Rows",
                "Cumulative 0",
                "Cumulative empty",
                "Value given",
            ),
            "rows": rows,
        },
    }


@dataclass
class _Comparison:
    """NeuroDB's Reached against PRP's cumulative (check 5), as counts of indicators."""

    several: int = 0  # with several counted reports
    growing: int = 0  # … whose PRP cumulative never goes down from one report to the next
    same: int = 0  # … the same on every one
    compared: int = 0  # Reached and PRP's cumulative both given, no partial history
    partial: int = 0  # both given, with a partial history (Reached is PRP's cumulative)
    differ: int = 0  # Reached differs from PRP's cumulative (Data quality's list)
    why: Counter = field(default_factory=Counter)  # what the indicators where they differ share


# what the indicators where Reached differs from PRP's cumulative share (check 5's table)
WHY = (
    ("above", "PRP's cumulative is above Reached"),
    ("below", "PRP's cumulative is below Reached"),
    ("hr", "Read from the monthly (HR) reports: the indicator has no quarterly one"),
    ("every", "PRP's cumulative equals every report combined, also those that do not count"),
    ("latest", "PRP's cumulative equals the latest counted report's own value"),
    ("steady", "PRP's cumulative is the same on every counted report (a current total, not a running one)"),
    ("source", 'A location is read from its running totals or the value in the period, not from "()"'),
    ("total", "A report is read from a report total (PRP's own, or one repeated on every location)"),
)


def _reached_against_prp() -> _Comparison:
    """Reached against PRP's cumulative for every indicator of the running programme documents, from
    :func:`monitoring.indicators` (Partner monitoring's Data quality tab reads the same: its identity, its
    report type, its report key and its parser): how PRP's cumulative runs over the counted reports, how
    many indicators can be compared, and what those where Reached differs share."""
    from . import monitoring

    out = _Comparison()
    for entry in monitoring.indicators(monitoring.Filters(scope="active", year=None)):
        result = entry.result
        if result is None:
            continue
        same = functools.partial(rc.close, ratio=result.ratio, percentage=result.percentage)
        cumulatives = [r.prp_cumulative for r in result.counted]
        known = len(cumulatives) > 1 and None not in cumulatives
        steady = known and all(same(c, cumulatives[0]) for c in cumulatives)
        if len(cumulatives) > 1:
            out.several += 1
            out.same += steady
            out.growing += known and all(b >= a or same(a, b) for a, b in itertools.pairwise(cumulatives))
        if entry.reached is not None and entry.prp_cumulative is not None:
            if entry.partial:
                out.partial += 1
            else:
                out.compared += 1
        if not entry.mismatch:
            continue
        out.differ += 1
        out.why["above" if entry.prp_cumulative > entry.reached else "below"] += 1
        out.why["hr"] += entry.report_type == "HR"
        every = [r.item for r in result.reports if r.value is not None]
        method = result.period_method or "sum"
        combined = rc.combine_items(
            every, method, ratio=result.ratio, percentage=result.percentage, fallback="latest"
        )[0]
        out.why["every"] += bool(every) and same(entry.prp_cumulative, combined)
        out.why["latest"] += result.latest is not None and same(entry.prp_cumulative, result.latest.value)
        out.why["steady"] += steady
        sources = {c.source for r in result.counted for c in r.cells.values() if c.value is not None}
        out.why["source"] += bool(sources - {"total"})
        out.why["total"] += any(r.source != "locations" for r in result.counted)
    return out


def _cumulative(cursor) -> dict[str, Any]:
    labels = (
        "Indicators of the running programme documents with several counted reports",
        "… whose PRP cumulative never goes down from one report to the next",
        "… with the same PRP cumulative on every report",
        "Indicators compared (running PDs, Reached and PRP's cumulative both given)",
        "Indicators where Reached differs from PRP",
        "Indicators with a partial history (Reached is PRP's cumulative)",
    )
    # NeuroDB's Reached for every indicator of the running PDs (Python; its queries share the time left):
    # only with PRP_COMPARE_MIN_S of the checks' time left
    left = cursor.remaining() if hasattr(cursor, "remaining") else PAGE_BUDGET_S
    if left < PRP_COMPARE_MIN_S:
        return {
            "tone": INFO,
            "verdict": (
                "Not compared: the checks run before it (checks 10 and 11 first) used up most of the time "
                "for the checks, and this one computes Reached for every indicator of the running programme "
                "documents. Partner monitoring → Data quality lists the indicators where Reached differs "
                "from PRP's cumulative."
            ),
            "figures": [(label, "—") for label in labels],
        }
    _bound(cursor)
    c = _reached_against_prp()
    if c.several:
        verdict = (
            f"Of the {c.several:,} indicators of the running programme documents with several counted "
            f"reports, PRP's cumulative never goes down from one report to the next on {c.growing:,} and is "
            f"the same on every report on {c.same:,}"
        )
    else:
        verdict = "No indicator of the running programme documents has several counted reports yet"
    if c.compared:
        verdict += (
            f"; NeuroDB's Reached differs from PRP's cumulative on {c.differ:,} of the {c.compared:,} "
            f"indicators where both are given ({_share(c.differ, c.compared)})"
        )
        if c.differ:
            verdict += (
                ": Partner monitoring → Data quality lists them (choose All sections and Quarterly reports, "
                "no other filter), and the table below says what they share"
            )
    else:
        verdict += "; no indicator of a running programme document has both a Reached and PRP's cumulative"
    if c.partial:
        verdict += f"; {c.partial:,} more have a partial history (their Reached is PRP's cumulative)"
    out = {
        "tone": WARN if c.differ else (INFO if not c.compared or not c.several else OK),
        "verdict": verdict + ".",
        "figures": list(
            zip(labels, (c.several, c.growing, c.same, c.compared, c.differ, c.partial), strict=True)
        ),
    }
    if c.differ:
        out["table"] = {
            "columns": ("What the indicators where Reached differs share", "Indicators"),
            "rows": [(label, c.why[key]) for key, label in WHY],
        }
    return out


def _methods(cursor) -> dict[str, Any]:
    rows = _all(
        cursor,
        "SELECT coalesce(nullif(calculation_across_locations, ''), '(none)'), "
        "coalesce(nullif(calculation_across_periods, ''), '(none)'), count(*) "
        "FROM datamart_reportedindicator GROUP BY 1, 2 ORDER BY 3 DESC",
    )
    other = sum(n for loc, per, n in rows if (loc, per) not in (("sum", "sum"), ("(none)", "(none)")))
    return {
        "tone": INFO,
        "verdict": (
            f"{other:,} rows use another method than a sum across locations and periods."
            if other
            else "Every row is summed across locations and periods (or names no method)."
        ),
        "figures": [("Rows with another method than sum", other)],
        "table": {"columns": ("Across locations", "Across periods", "Rows"), "rows": rows},
    }


# the keys of a partner report record the sync reads beside its field map (``_link_report``, the record's
# ids and country): check 7 lists them even when no record sends them
LINK_KEYS = (
    "id",
    "source_id",
    "last_modify_date",
    "country_name",
    "etools_intervention_id",
    "due_date",
    "reporting_period_due_date",
    "high_frequency",
    "disaggregation",
    "indicator_report",
    "cp_output",
    "etools_cp_output_id",
)
# per key of the records kept whole: the rows with it, with a value, and by the kind of value (no value read)
CENSUS_SQL = (
    "SELECT e.key, count(*), "
    "count(*) FILTER (WHERE jsonb_typeof(e.value) <> 'null' "
    "AND e.value NOT IN ('\"\"'::jsonb, '{}'::jsonb, '[]'::jsonb)), "
    "count(*) FILTER (WHERE jsonb_typeof(e.value) = 'object'), "
    "count(*) FILTER (WHERE jsonb_typeof(e.value) = 'string'), "
    "count(*) FILTER (WHERE jsonb_typeof(e.value) = 'number'), "
    "count(*) FILTER (WHERE jsonb_typeof(e.value) IN ('array', 'boolean')), "
    "count(*) FILTER (WHERE jsonb_typeof(e.value) = 'null') "
    "FROM datamart_reportedindicator r, jsonb_each(r.data) e "
    "WHERE jsonb_typeof(r.data) = 'object' GROUP BY 1"
)


def _identity(cursor) -> dict[str, Any]:
    from neurodb.integrations.etools.datamart_sync import DATASETS

    from . import monitoring

    with_id, matched, other_title = _one(
        cursor,
        "SELECT count(*), count(p.source_id), count(*) FILTER (WHERE p.source_id IS NOT NULL "
        "AND lower(trim(p.title)) <> lower(trim(r.indicator))) "
        "FROM datamart_reportedindicator r LEFT JOIN LATERAL ("
        "SELECT source_id, title FROM datamart_pdindicator p WHERE p.intervention_id = r.intervention_id "
        "AND p.source_id::text = r.etools_indicator_id LIMIT 1) p ON true WHERE r.etools_indicator_id <> ''",
    )
    # PRP's report ids: empty, how many, how many stand for more than one report (a report told apart by its
    # programme document, type, number and period, :func:`_report`), and the reports with more than one
    rows, pr_empty, pr_values, pr_many, pr_split, ir_empty, ir_whole, ir_values, ir_many = _one(
        cursor,
        f"WITH r AS MATERIALIZED (SELECT progress_report, indicator_report, {REPORT_SQL} AS report "  # noqa: S608
        "FROM datamart_reportedindicator) SELECT (SELECT count(*) FROM r), "
        "(SELECT count(*) FROM r WHERE progress_report = ''), "
        "(SELECT count(DISTINCT progress_report) FROM r WHERE progress_report <> ''), "
        "(SELECT count(*) FROM (SELECT 1 FROM r WHERE progress_report <> '' GROUP BY progress_report "
        "HAVING count(DISTINCT report) > 1) x), "
        "(SELECT count(*) FROM (SELECT 1 FROM r WHERE progress_report <> '' GROUP BY report "
        "HAVING count(DISTINCT progress_report) > 1) x), "
        "(SELECT count(*) FROM r WHERE indicator_report = ''), "
        "(SELECT count(*) FROM r WHERE indicator_report ~ '^[0-9]{1,18}$'), "
        "(SELECT count(DISTINCT indicator_report) FROM r WHERE indicator_report <> ''), "
        "(SELECT count(*) FROM (SELECT 1 FROM r WHERE indicator_report <> '' GROUP BY indicator_report "
        "HAVING count(DISTINCT report) > 1) x)",
    )
    # the rows of the running PDs, read by Partner monitoring's own rule (monitoring._Idents): by the eTools
    # id, else by a title one indicator of the PD has, else by the PD output and a title several share; the
    # others are left out (the title and the output shared, an output that matches none, or an unknown title)
    running = dm.ReportedIndicator.objects.filter(intervention__status__in=pd_statuses.RUNNING)
    _bound(cursor)
    pd_ids = sorted(set(running.order_by().values_list("intervention_id", flat=True).distinct()))
    _bound(cursor)
    idents = monitoring._Idents(pd_ids)
    how: Counter[str] = Counter()
    left: set[tuple] = set()  # the indicator reports left out
    types: dict[tuple, set[str]] = defaultdict(set)  # each PD indicator's report types
    for pd, eid, title, output, kind, ir, number, start, end in _each(
        cursor,
        running,
        "intervention_id",
        "etools_indicator_id",
        "indicator",
        "pd_output",
        "report_type",
        "indicator_report",
        "report_number",
        "period_start",
        "period_end",
    ):
        way = idents.how(pd, eid, title, output)
        how[way] += 1
        if way in ("id", "title", "output"):
            types[idents.of(pd, eid, title, output)].add((kind or "").strip().upper())
        else:
            left.add((_report(pd, "", kind, number, start, end), _indicator(ir, eid, title, output)))
    humanitarian = sum(1 for kinds in types.values() if "HR" in kinds)
    both = sum(1 for kinds in types.values() if {"HR", "QPR"} <= kinds)
    # the keys the records send (names and counts only, never a value): those the sync reads first
    read = {*DATASETS["partner_reports"][1].fields.values(), *LINK_KEYS}
    found = {key: counts for key, *counts in _all(cursor, CENSUS_SQL)}
    census = sorted(
        ((key[:60], "yes" if key in read else "no", *found.get(key, (0,) * 7)) for key in {*found, *read}),
        key=lambda line: (line[1] != "yes", line[0]),
    )[:KEYS_LISTED]
    out_rows = how["shared_output"] + how["output_unmatched"] + how["unknown"]
    parts = []
    if not with_id:
        parts.append(
            "No report row carries a PD indicator id: the one key NeuroDB reads for it, "
            "etools_cp_output_indicators_id (named after the CP output's indicators, and never confirmed "
            "as the PD indicator's id), is empty or missing on every row (the table below lists every key "
            "the records send). Rows are matched by title, and by PD output where two indicators of a PD "
            "share a title."
        )
    else:
        parts.append(
            f"{matched:,} of {with_id:,} rows with an eTools indicator id ({_share(matched, with_id)}) match "
            f"a PD indicator of their programme document; {other_title:,} of those carry another title."
        )
    if sum(how.values()):
        parts.append(
            f"Of the {_plural(sum(how.values()), 'row', 'rows')} of the running programme documents, "
            f"{_plural(how['id'], 'is', 'are')} matched by id, {how['title']:,} by title and "
            f"{how['output']:,} by PD output and title (a title several indicators of the PD share)."
        )
        if out_rows:
            parts.append(
                f"Left out: {_plural(how['shared_output'], 'row', 'rows')} whose title and PD output two or "
                "more indicators of their PD share (tell them apart in eTools), "
                f"{_plural(how['output_unmatched'], 'row', 'rows')} whose title several indicators of their "
                "PD share and whose PD output, as PRP sends it, is empty or matches none of theirs in eTools "
                "(correct the output name in PRP or eTools so the two match), and "
                f"{_plural(how['unknown'], 'row', 'rows')} whose title no indicator of their PD has "
                f"({_plural(len(left), 'indicator report', 'indicator reports')} in all). Partner "
                "monitoring → Data quality lists the titles a PD repeats and how many of those indicators "
                "get none of their rows; a title PRP spells otherwise than eTools is corrected in PRP or "
                "eTools."
            )
    if rows:
        if pr_empty == rows:
            parts.append(
                "PRP's progress report id is empty on every row; every page tells reports apart by their "
                "programme document, type, number and period, so nothing depends on it."
            )
        elif pr_many:
            parts.append(
                f"{_plural(pr_many, 'progress report id of PRP stands', 'progress report ids of PRP stand')} "
                "for more than one report (programme document, type, number and period): every page reads "
                "them as the separate reports they are."
            )
        if ir_many:
            parts.append(
                f"{_plural(ir_many, 'indicator report id', 'indicator report ids')} of PRP "
                f"{'stands' if ir_many == 1 else 'stand'} for more than one report."
            )
    problems = out_rows or ir_many or (with_id and (matched != with_id or other_title))
    return {
        "tone": WARN if problems else (OK if rows else INFO),
        "verdict": " ".join(parts),
        "figures": [
            ("Rows with an eTools indicator id", with_id),
            ("… matching a PD indicator", matched),
            ("… of which with another title", other_title),
            ("Rows of the running programme documents", sum(how.values())),
            ("… matched to a PD indicator by its id", how["id"]),
            ("… matched by title (one indicator of the PD has it)", how["title"]),
            ("… matched by PD output and title (a title several indicators of the PD share)", how["output"]),
            (
                "… left out: two or more indicators of the PD have the title and the output",
                how["shared_output"],
            ),
            (
                "… left out: a title several indicators of the PD share, and a PD output none of them has",
                how["output_unmatched"],
            ),
            ("… left out: no indicator of the PD has the title", how["unknown"]),
            ("Indicator reports left out", len(left)),
            ("PD indicators in monthly (HR) reports", humanitarian),
            ("… also in quarterly (QPR) reports", both),
            ("Rows with no PRP progress report id", pr_empty),
            ("PRP progress report ids", pr_values),
            ("PRP progress report ids standing for more than one report", pr_many),
            ("Reports with more than one PRP progress report id", pr_split),
            ("Rows with no PRP indicator report id", ir_empty),
            ("Rows with a whole-number PRP indicator report id", ir_whole),
            ("PRP indicator report ids", ir_values),
            ("PRP indicator report ids standing for more than one report", ir_many),
        ],
        "table": {
            "columns": (
                "Key the Datamart sends",
                "Read by NeuroDB",
                "Rows with the key",
                "… with a value",
                "Object",
                "Text",
                "Number",
                "Other (list, true or false)",
                "Empty (null)",
            ),
            "rows": census,
        },
    }


# ------------------------------------------------------------------------- PRP's indicator reports
AGAIN = (
    "on Import and sync runs, press “Sync eTools Datamart now”, choose “Only the datasets ticked below, in "
    "their usual order” and tick prp_indicator_reports"
)
CODE_SETTING = "Admin → Data and sync → eTools Datamart: PRP business area code"
PROBE = "Run a job → Probe a Datamart dataset"  # on Import and sync runs


def _prp_feed(cursor) -> dict[str, Any]:
    """PRP's indicator reports as NeuroDB holds them, and their last sync (checks 8, 9 and 11), as counts:
    the records held and those with a PRP id (``source_id``); the last run of the dataset is Data health's
    (the latest started, a running one too), with what it asked and read in its details (counts, the
    business area code and the codes the records named)."""
    from neurodb.reports.services import latest_datamart_runs

    _bound(cursor)
    run = latest_datamart_runs([PRP]).get(PRP)
    _bound(cursor)
    held = dm.DatamartDocument.objects.filter(dataset=PRP).aggregate(
        held=Count("pk"), with_id=Count("source_id")
    )
    details = run.details if run is not None and isinstance(run.details, dict) else {}
    return {**held, "run": run, "details": details}


def _area_words(details: dict[str, Any]) -> tuple[str, str]:
    """A PRP sync run's business area in words (checks 8, 9 and 11): the " for business area N (where it came
    from)" clause, and a sentence when the code asked was not a business area code (a country's name: the
    code the next sync takes instead, as NeuroDB resolves it now from the database) or a value was set
    aside."""
    from neurodb.integrations.etools import datamart_sync as sync
    from neurodb.reports.services import BUSINESS_AREA_FROM

    code = str(details.get("business_area") or "")
    where = str(BUSINESS_AREA_FROM.get(details.get("business_area_from"), ""))
    asked = (f" for business area {code}" + (f" ({where})" if where else "")) if code else ""
    if code and not sync.is_area_code(code):
        then = sync.resolve_business_area()
        takes = (
            f"uses {then.code} ({BUSINESS_AREA_FROM.get(then.source, then.source)})"
            if then.code
            else f"finds no code: enter it in {CODE_SETTING}"
        )
        first = f"; a code entered in {CODE_SETTING} comes first" if then.found else ""
        return asked, (
            f"“{code}” is {sync.not_a_code(code)} (a code is digits, as 2490), so the Datamart found "
            f"nothing. The next sync sets it aside and {takes}{first}."
        )
    aside = details.get("business_area_set_aside")
    if isinstance(aside, dict) and aside.get(sync.FROM_ENVIRONMENT):
        value = str(aside[sync.FROM_ENVIRONMENT])
        return asked, (
            f"The hosting team's ETOOLS_DATAMART_BUSINESS_AREA (“{value}”) is not a business area code: it "
            "was set aside."
        )
    return asked, ""


def _feed_state(feed: dict[str, Any], *, listed: bool) -> tuple[str, str]:
    """What the last sync of PRP's indicator reports did, when NeuroDB holds none of its records, in words
    with what to do from the admin, and the tone it gives (a warning for a failed sync, records all of
    another business area, or records none of which could be written). ``listed``: the check lists the
    business area codes the records named (check 11); the others point to it."""
    from neurodb.core.models import SyncRun
    from neurodb.integrations.etools.datamart_sync import is_area_code

    run, details = feed["run"], feed["details"]
    if run is None:
        return f"PRP's indicator reports (prp/indicator-report-v2) have never been synced: {AGAIN}.", INFO
    when = _day(_local_day(run.started_at))
    if run.status == SyncRun.Status.RUNNING:
        return (
            f"A sync of PRP's indicator reports is running (started {when}): run the checks again once it "
            "has ended.",
            INFO,
        )
    if run.status == SyncRun.Status.FAILED:
        # the run's own error, cut short, without an e-mail address (a Datamart answer can quote one)
        error = reporting.EMAIL.sub("(an address)", " ".join(str(run.error or "").split()))
        error = f"{error[:137]}..." if len(error) > 140 else error
        return (
            f"The last sync of PRP's indicator reports ({when}) failed{f' ({error})' if error else ''}: "
            "read it in Import and sync runs (search prp_indicator_reports), and once its cause is fixed, "
            f"{AGAIN}.",
            WARN,
        )
    code = str(details.get("business_area") or "")
    asked, area = _area_words(details)
    skipped = _count(details.get("other_country_skipped"))
    if skipped:
        codes = "the codes they name are listed below" if listed else "check 11 lists the codes they name"
        return (
            f"The last sync of PRP's indicator reports ({when}) asked{asked} and read "
            f"{_plural(skipped, 'record', 'records')}, all of another business area ({codes}): check the "
            f"code in {CODE_SETTING}, then {AGAIN}.",
            WARN,
        )
    if run.rows_failed and not run.rows_written:
        return (
            f"The last sync of PRP's indicator reports ({when}) read "
            f"{_plural(run.rows_in, 'record', 'records')}"
            f"{asked} but could write none: Import and sync runs says why under Why rows failed.",
            WARN,
        )
    raw = details.get("datamart_count")
    announced, read = _count(raw), _count(details.get("read")) or run.rows_in + skipped
    if announced > read:  # the first page announced records the read never returned: cut short
        return (
            f"The last sync of PRP's indicator reports ({when}) asked the Datamart{asked}: it announced "
            f"{_plural(announced, 'record', 'records')}, but its answer stopped after {read:,}. The answer "
            f"was cut short, so a new sync may help: {AGAIN}. If it stops again, see what the Datamart holds "
            f"with {PROBE}.",
            INFO,
        )
    told = f" (it announced {announced:,})" if isinstance(raw, int) and not isinstance(raw, bool) else ""
    start = (
        f"The last sync of PRP's indicator reports ({when}) asked the Datamart{asked} and it returned no "
        f"record{told}"
    )
    if code and not is_area_code(code):  # the next sync asks with another code: syncing again may help
        return f"{start}. {area}", INFO
    if details.get("business_area_found"):  # a code NeuroDB found: one entered in the admin comes first
        then = (
            f"enter the right code in {CODE_SETTING} (it comes first), or see what the Datamart holds with "
            f"{PROBE}."
        )
    else:
        then = (
            f"syncing again changes nothing. Check the code in {CODE_SETTING}, or see what the Datamart "
            f"holds with {PROBE}."
        )
    return f"{start}: {then}" + (f" {area}" if area else ""), INFO


def _report_totals(cursor) -> dict[str, Any]:
    feed = _prp_feed(cursor)
    # the partner indicator reports with a whole-number id: their PDs, and their value as Reached reads it
    # from the locations ("()", the largest of a location's rows, combined by PRP's method)
    reports: dict[int, dict[str, Any]] = {}
    other_ids: set[str] = set()
    rows = dm.ReportedIndicator.objects.exclude(indicator_report="").annotate(
        _total=KeyTransform("()", "disaggregation")
    )
    for ir, pd, p_code, location, total, method in _each(
        cursor,
        rows,
        "indicator_report",
        "intervention_id",
        "p_code",
        "location",
        "_total",
        "calculation_across_locations",
    ):
        if not (ir.isascii() and ir.isdigit()) or len(ir) > 18:
            other_ids.add(ir)
            continue
        entry = reports.setdefault(int(ir), {"pds": set(), "places": {}, "method": rc.method_of(method)})
        if pd is not None:
            entry["pds"].add(pd)
        value = _value(total)
        if value is not None:
            place = _place(p_code, location)
            entry["places"][place] = max(entry["places"].get(place, value), value)
    # PRP's record of each id (the first, as reached.progress_values reads them), and its own total
    found: dict[int, tuple[int | None, Any]] = {}
    ids = sorted(reports)
    for start in range(0, len(ids), 5000):
        _bound(cursor)
        records = (
            dm.DatamartDocument.objects.filter(dataset=PRP, source_id__in=ids[start : start + 5000])
            .order_by("pk")
            .annotate(_value=KT("data__progress_value"))
            .values_list("source_id", "intervention_id", "_value")
        )
        for source_id, pd, raw in records:
            found.setdefault(source_id, (pd, raw))
    compared = equal = other_pd = 0
    for source_id, (pd, raw) in found.items():
        entry = reports[source_id]
        if pd is not None and entry["pds"] and pd not in entry["pds"]:
            other_pd += 1
        theirs = _value(raw)
        mine = (
            rc.combine(list(entry["places"].values()), entry["method"] or "sum") if entry["places"] else None
        )
        if theirs is not None and mine is not None:
            compared += 1
            equal += rc.close(theirs, mine)
    figures = [
        ("PRP indicator report records held", feed["held"]),
        ("… with a PRP id (source_id)", feed["with_id"]),
        ("Partner indicator report ids that are whole numbers", len(reports)),
        ("… that are not", len(other_ids)),
        ("Indicator reports found in PRP's indicator reports", len(found)),
        ("… that can be compared", compared),
        ("… equal", equal),
        ("… about another programme document", other_pd),
    ]
    if not feed["held"]:
        text, tone = _feed_state(feed, listed=False)
        verdict = f"PRP's indicator reports hold no record, so no report total can be compared. {text}"
        return {"tone": tone, "verdict": verdict, "figures": figures}
    if not feed["with_id"]:
        verdict = (
            f"{_plural(feed['held'], 'PRP record is', 'PRP records are')} held, none with a PRP id "
            "(source_id): the two feeds cannot be matched by id, so no PRP report total is used."
        )
        return {"tone": WARN, "verdict": verdict, "figures": figures}
    if not reports:
        verdict = (
            "No partner report row carries a whole-number indicator report id"
            + (f" ({_plural(len(other_ids), 'id is', 'ids are')} of another kind)" if other_ids else "")
            + ": the two feeds cannot be matched by id, so no PRP report total is used."
        )
        return {"tone": WARN if other_ids else INFO, "verdict": verdict, "figures": figures}
    if not found:
        verdict = (
            f"The {feed['with_id']:,} PRP ids match none of the {len(reports):,} partner indicator report "
            "ids: the two feeds may not share their ids, and no PRP report total is used."
        )
        return {"tone": WARN, "verdict": verdict, "figures": figures}
    verdict = (
        f"PRP's report total equals the locations combined on {equal:,} of {compared:,} indicator reports "
        f"that can be compared ({_share(equal, compared)})."
        if compared
        else "No indicator report can be compared with PRP's own total yet."
    )
    if other_pd:
        verdict += (
            f" {other_pd:,} PRP {'record' if other_pd == 1 else 'records'} with the same id "
            f"{'is' if other_pd == 1 else 'are'} about another programme document: the two datasets "
            "do not share their ids, and NeuroDB uses no PRP total that names another programme "
            "document or indicator."
        )
    if other_pd or (compared and equal != compared):
        tone = WARN
    else:
        tone = OK if compared else INFO
    return {"tone": tone, "verdict": verdict, "figures": figures}


def _financial(cursor) -> dict[str, Any]:
    feed = _prp_feed(cursor)
    reports, varying = _one(
        cursor,
        "SELECT count(*), count(*) FILTER (WHERE n > 1) FROM (SELECT data->>'pd_reference_number', "
        "data->>'report_type', data->>'report_number', "
        "count(DISTINCT data->>'financial_contribution_to_date') n "
        "FROM datamart_datamartdocument WHERE dataset = 'prp_indicator_reports' "
        "AND coalesce(data->>'financial_contribution_to_date', '') <> '' GROUP BY 1, 2, 3) g",
    )
    tone = INFO
    if not feed["held"]:
        text, tone = _feed_state(feed, listed=False)
        verdict = (
            "PRP's indicator reports hold no record, so no financial contribution can be read (the Funds "
            f"page shows none). {text}"
        )
    elif not reports:
        verdict = (
            f"{_plural(feed['held'], 'PRP record is', 'PRP records are')} held; none carries a financial "
            "contribution (financial_contribution_to_date)."
        )
    else:
        verdict = (
            f"The financial contribution is the same on every indicator of {reports - varying:,} of "
            f"{reports:,} progress reports."
        )
        tone = OK if not varying else WARN
    return {
        "tone": tone,
        "verdict": verdict,
        "figures": [
            ("PRP indicator report records held", feed["held"]),
            ("Progress reports with a financial contribution", reports),
            ("… where it varies", varying),
        ],
    }


# how a report row belongs to a programme document (check 10)
NO_COUNTRY = "(none)"
LINK_RUNNING = "running"
LINK_OTHER_STATUS = "another status"
LINK_NOT_HELD = "not in NeuroDB"
LINK_NONE = "names none"


def _coverage(cursor) -> dict[str, Any]:
    """How much of the running programme documents' reporting the partner reports hold (check 10), as
    counts: the PDs with no row, those whose first report held ends long after their start, and those
    whose lowest report number of a type is above 1; and the latest partner reports sync's window."""
    from neurodb.reports.services import latest_datamart_runs

    _bound(cursor)
    starts = dict(PCA.objects.filter(status__in=pd_statuses.RUNNING).values_list("pk", "start"))
    rows = dm.ReportedIndicator.objects.filter(intervention__status__in=pd_statuses.RUNNING).order_by()
    _bound(cursor)
    first = dict(
        rows.values("intervention_id")
        .annotate(first=Min("period_end"))
        .values_list("intervention_id", "first")
    )
    lowest: dict[int, dict[str, int]] = defaultdict(dict)
    _bound(cursor)
    for pd, kind, number in rows.values_list("intervention_id", "report_type", "report_number").distinct():
        n, kind = _number(number), (kind or "").strip().upper()
        if n is not None and kind in STEP_MONTHS:
            lowest[pd][kind] = min(lowest[pd].get(kind, n), n)
    _bound(cursor)
    run = latest_datamart_runs(["partner_reports"], finished=True).get("partner_reports")
    return {
        "running": len(starts),
        "none": sum(1 for pk in starts if pk not in first),
        "late": sum(
            1
            for pk, start in starts.items()
            if start and first.get(pk) and (first[pk] - start).days > LATE_FIRST_DAYS
        ),
        "no_first": sum(1 for kinds in lowest.values() if any(n > 1 for n in kinds.values())),
        "run": run,
    }


def _country_and_link(cursor) -> dict[str, Any]:
    from neurodb.integrations.etools.datamart_sync import other_country

    _bound(cursor)
    country = settings.ETOOLS_DATAMART_COUNTRY
    link = Case(
        When(reporting.NO_PD, then=Value(LINK_NONE)),
        When(intervention__isnull=True, then=Value(LINK_NOT_HELD)),
        When(intervention__status__in=pd_statuses.RUNNING, then=Value(LINK_RUNNING)),
        default=Value(LINK_OTHER_STATUS),
    )
    annotated = (
        dm.ReportedIndicator.objects.order_by()
        .annotate(
            _country=Coalesce(
                NullIf(Trim(KT("data__country_name")), Value("", output_field=TextField())),
                Value(NO_COUNTRY, output_field=TextField()),
            )
        )
        .annotate(_link=link)
    )
    groups = list(
        annotated.values("_country", "_link")
        .annotate(
            rows=Count("pk"),
            reports=Count(reporting.INDICATOR_REPORT_KEY, distinct=True),
            first=Min("period_end"),
            last=Max("period_end"),
            numbers=ArrayAgg("pd_reference_number", distinct=True, filter=Q(intervention__isnull=True)),
        )
        .order_by("_country", "_link")
    )
    if not groups:
        return {"tone": INFO, "verdict": "No partner report rows are held yet.", "figures": []}
    # each section name once (a row's field can hold a list, "Child Protection, Adolescent & Youth"), those
    # with the most indicator reports first
    _bound(cursor)
    known = {
        name for names in PCA.objects.values_list("section_names", flat=True) for name in names or [] if name
    }
    _bound(cursor)
    # (country, link) -> each name, case aside -> its spellings and their indicator reports
    sections: dict[tuple, dict[str, Counter[str]]] = defaultdict(lambda: defaultdict(Counter))
    for g in (
        annotated.exclude(section="")
        .values("_country", "_link", "section")
        .annotate(n=Count(reporting.INDICATOR_REPORT_KEY, distinct=True))
    ):
        for name in reporting.section_names(g["section"], known):
            sections[(g["_country"], g["_link"])][name.casefold()][name] += g["n"]
    spelled = {" ".join(name.split()).casefold(): " ".join(name.split()) for name in known}

    def top(key: tuple) -> str | None:
        # a name as eTools spells it for the PDs, else its spelling with the most reports (capitals first)
        found = sorted(
            (
                (spelled.get(fold) or max(ways, key=lambda w: (ways[w], w != w.lower(), w)), ways.total())
                for fold, ways in sections.get(key, {}).items()
            ),
            key=lambda e: (-e[1], e[0].casefold()),
        )
        shown = ", ".join(name for name, _ in found[:SECTIONS_SHOWN])
        more = len(found) - SECTIONS_SHOWN
        return (shown + (f" (+{more} more)" if more > 0 else "")) or None

    # the sync's own rule (case and spaces aside; nothing is another country's while none is set)
    other = sum(
        g["rows"]
        for g in groups
        if g["_country"] != NO_COUNTRY and other_country({"country_name": g["_country"]}, country)
    )
    not_held = sum(g["reports"] for g in groups if g["_link"] == LINK_NOT_HELD)
    no_pd = sum(g["reports"] for g in groups if g["_link"] == LINK_NONE)
    cover = _coverage(cursor)
    parts = []
    if other:
        parts.append(
            f"{other:,} {'row names' if other == 1 else 'rows name'} another country than {country}: "
            f"the next partner reports sync that returns {country}'s reports removes "
            f"{'it' if other == 1 else 'them'}"
        )
    if not_held:
        parts.append(f"{not_held:,} indicator reports belong to programme documents NeuroDB does not hold")
    if no_pd:
        parts.append(f"{no_pd:,} indicator reports name no programme document")
    verdict = (
        "; ".join(parts) + "."
        if parts
        else "Every row is of this country and belongs to a programme document NeuroDB holds."
    )
    if cover["running"]:
        verdict += (
            f" Of the {cover['running']:,} running programme documents, {cover['none']:,} have no partner "
            f"report row, {cover['late']:,} started more than {LATE_FIRST_DAYS} days before the end of their "
            f"first report held and {cover['no_first']:,} lack their first report (QPR1 or HR1)"
        )
        verdict += (
            ": Reached adds up only the reports the feed holds, so theirs may be partial."
            if cover["late"] or cover["no_first"]
            else "."
        )
    run = cover["run"]
    if run is not None:
        details = run.details if isinstance(run.details, dict) else {}
        try:
            since = _day(datetime.date.fromisoformat(str(details.get("read_from") or "")[:10]))
        except ValueError:
            since = None
        window = f" asked for the reports from {since} on and" if since else ""
        verdict += (
            f" The latest partner reports sync ({_day(_local_day(run.started_at))}){window} kept "
            f"{_plural(run.rows_in, 'row', 'rows')}: what the Datamart did not return cannot be counted."
        )
    rows = [
        (
            g["_country"],
            g["_link"],
            g["rows"],
            g["reports"],
            _day(g["first"]),
            _day(g["last"]),
            top((g["_country"], g["_link"])),
            ", ".join(sorted(x for x in g["numbers"] or [] if x)[:3]) or None,
        )
        for g in groups
    ]
    if other or not_held or cover["late"] or cover["no_first"]:
        tone = WARN
    else:
        tone = INFO if no_pd or cover["none"] else OK
    return {
        "tone": tone,
        "verdict": verdict,
        "figures": [
            ("Rows naming another country", other),
            ("Indicator reports of programme documents not held", not_held),
            ("Indicator reports naming no programme document", no_pd),
            ("Running programme documents", cover["running"]),
            ("… with no partner report row", cover["none"]),
            (f"… whose first report held ends over {LATE_FIRST_DAYS} days after their start", cover["late"]),
            ("… without their first report (QPR1 or HR1)", cover["no_first"]),
        ],
        "table": {
            "columns": (
                "Country in the row",
                "Programme document",
                "Rows",
                "Indicator reports",
                "First period end",
                "Last period end",
                f"Sections (top {SECTIONS_SHOWN})",
                "PD numbers not held (first 3)",
            ),
            "rows": rows,
        },
    }


def _number_gaps(cursor, today: datetime.date) -> tuple[Counter[int], int, int]:
    """The progress reports missing between the report numbers the partner reports hold for each running
    programme document and type (a QPR2 missing between QPR1 and QPR3) whose period has ended this year,
    by an estimate from the numbers alone: per month of the estimated period end (the report before it,
    plus the type's months, at most the day before the report after it starts), the indicator reports it
    would hold (as many as the report before it, else after it); and in all, the progress reports and their
    indicator reports. A report owed after the last number held cannot be seen."""
    _bound(cursor)
    # each PD and type: its report numbers -> [period start, period end, indicator reports]
    held: dict[tuple[int, str], dict[int, list[Any]]] = defaultdict(dict)
    grouped = (
        reporting.ReportFilters()  # the running PDs, every year
        .queryset(today)
        .order_by()
        .values("intervention_id", "report_type", "report_number")
        .annotate(
            start=Min("period_start"),
            end=Min("period_end"),
            n=Count(reporting.INDICATOR_REPORT, distinct=True),
        )
    )
    for g in grouped:
        kind, number = (g["report_type"] or "").strip().upper(), _number(g["report_number"])
        if kind not in STEP_MONTHS or number is None or g["intervention_id"] is None:
            continue
        known = held[(g["intervention_id"], kind)].setdefault(number, [None, None, 0])
        known[0] = min((d for d in (known[0], g["start"]) if d), default=None)
        known[1] = min((d for d in (known[1], g["end"]) if d), default=None)
        known[2] = max(known[2], g["n"])
    months: Counter[int] = Counter()
    missing = estimated = 0
    for (_, kind), numbers in held.items():
        step, given = STEP_MONTHS[kind], sorted(numbers)
        for n in range(1, given[-1]):
            if n in numbers:
                continue
            before = max((h for h in given if h < n), default=None)
            after = min(h for h in given if h > n)
            if before is not None and numbers[before][1]:
                end = _month_end(numbers[before][1], (n - before) * step)
            elif numbers[after][1]:
                end = _month_end(numbers[after][1], -(after - n) * step)
            else:
                continue
            if numbers[after][0] and end >= numbers[after][0]:
                end = numbers[after][0] - datetime.timedelta(days=1)
            if end > today or end.year != today.year:
                continue
            missing += 1
            indicators = numbers[before if before is not None else after][2]
            estimated += indicators
            months[end.month] += indicators
    return months, missing, estimated


PROGRESS = "prp_progress_reports"  # PRP's own progress reports (sources/prp/unicefprogressreport): check 11
AGAIN_PROGRESS = AGAIN.replace(PRP, "prp_programme_documents and prp_progress_reports")


def _progress_feed(cursor, today: datetime.date) -> dict[str, Any]:
    """PRP's own progress reports (``prp_progress_reports``) for check 11, as counts: per month of the year
    their period ends in, the progress reports of the running programme documents per status
    (:func:`reached.prp_document_status` over their status, end, due and submission dates), the records held,
    those of the year by their programme document link, those with no period end, and the last sync."""
    from neurodb.reports.services import latest_datamart_runs

    zero = dict.fromkeys(rc.STATUS_LABELS, 0)
    _bound(cursor)
    run = latest_datamart_runs([PROGRESS]).get(PROGRESS)
    _bound(cursor)
    found = (
        dm.DatamartDocument.objects.filter(dataset=PROGRESS)
        .order_by()
        .annotate(
            _link=Case(
                When(intervention__isnull=True, then=Value(LINK_NOT_HELD)),
                When(intervention__status__in=pd_statuses.RUNNING, then=Value(LINK_RUNNING)),
                default=Value(LINK_OTHER_STATUS),
            ),
            _status=KT("data__status"),
            _end=KT("data__end_date"),
            _due=KT("data__due_date"),
            _sub=KT("data__submission_date"),
        )
        .values("_link", "date", "_status", "_end", "_due", "_sub")
        .annotate(n=Count("pk"))
    )
    table: dict[Any, dict[str, int]] = {}
    held: Counter[str] = Counter()  # the records of the year, by their programme document link
    total = undated = 0
    for g in found:
        total += g["n"]
        status, end = rc.prp_document_status(
            status=g["_status"], end=g["date"] or g["_end"], due=g["_due"], sub=g["_sub"], today=today
        )
        if end is None:
            undated += g["n"]
            continue
        if end.year != today.year:
            continue
        held[g["_link"]] += g["n"]
        if g["_link"] == LINK_RUNNING:
            counts = table.setdefault(end.month, dict(zero))
            counts[status] += g["n"]
    details = run.details if run is not None and isinstance(run.details, dict) else {}
    link = details.get("progress_reports_pd_link")
    return {
        "run": run,
        "held": total,
        "table": table,
        "linked": held[LINK_RUNNING] + held[LINK_OTHER_STATUS],
        "running": held[LINK_RUNNING],
        "unlinked": held[LINK_NOT_HELD],
        "undated": undated,
        "link": link if isinstance(link, dict) else {},
        "details": details,
    }


def _progress_state(feed: dict[str, Any], year: int) -> tuple[str, str]:
    """Check 11's words on PRP's progress reports when they list none of a running programme document for
    ``year``: why (records held but none linked to a programme document NeuroDB holds, with the link's
    counts, or none of the year; else never synced, running, failed, none kept: the code asked and where it
    came from, what the Datamart announced and the records left out) and what to do from the admin; and the
    tone (a warning for a failed sync)."""
    from neurodb.core.models import SyncRun
    from neurodb.integrations.etools.datamart_sync import is_area_code, not_a_code

    run, start = (
        feed["run"],
        f"PRP's own progress reports list none of a running programme document for {year}",
    )
    link = feed["link"]
    if feed["unlinked"] and not feed["linked"]:
        why = (
            f" ({_count(link.get('pd_not_named')):,} name no PRP programme document, "
            f"{_count(link.get('pd_not_held')):,} one NeuroDB does not hold, "
            f"{_count(link.get('pd_not_linked')):,} one linked to no eTools programme document)"
            if link
            else ""
        )
        return (
            f"{start}: {_plural(feed['unlinked'], 'is', 'are')} held for {year}, none linked to a programme "
            f"document NeuroDB holds{why}. The link is made through PRP's programme documents: "
            f"{AGAIN_PROGRESS}.",
            INFO,
        )
    if feed["held"]:
        return (
            f"{start}: {_plural(feed['held'], 'record is', 'records are')} held, none of a running programme "
            f"document's period ending in {year} ({feed['undated']:,} with no period end).",
            INFO,
        )
    if run is None:
        return f"{start}: no sync of them has run yet ({AGAIN_PROGRESS}).", INFO
    when = _day(_local_day(run.started_at))
    if run.status == SyncRun.Status.RUNNING:
        return (
            f"{start}: their sync is running (started {when}); run the checks again once it has ended.",
            INFO,
        )
    if run.status == SyncRun.Status.FAILED:
        return (
            f"{start}: their last sync ({when}) failed. Read it in Import and sync runs (search "
            f"{PROGRESS}), and once its cause is fixed, {AGAIN_PROGRESS}.",
            WARN,
        )
    details = feed.get("details") or {}
    code = str(details.get("business_area") or "")
    if not code:  # a run made before the code was recorded
        return (
            f"{start}: their last sync ({when}) kept no record, so syncing again changes nothing; see what "
            f"the Datamart holds with {PROBE}.",
            INFO,
        )
    asked, area = _area_words(details)
    named = not is_area_code(code)  # a country's name was asked: the next sync asks with a code
    raw = details.get("datamart_count")
    got = f"the Datamart announced {raw:,} and " if isinstance(raw, int) and not isinstance(raw, bool) else ""
    other, unnamed = _count(details.get("other_country_skipped")), _count(details.get("unnamed_skipped"))
    why = []
    if other > unnamed:
        why.append(f"{_plural(other - unnamed, 'record', 'records')} of another business area")
    if unnamed:
        why.append(f"{unnamed:,} named no business area and no PRP programme document NeuroDB holds")
    text = (
        f"{start}: their last sync ({when}) asked{asked}"
        + (f", {not_a_code(code)}" if named else "")
        + f": {got}NeuroDB kept none"
        + (f" ({'; '.join(why)})" if why else "")
        + "."
    )
    if named:
        return f"{text} {area}", INFO
    if details.get("business_area_found"):  # a code NeuroDB found: one entered in the admin comes first
        text += (
            f" Enter the right code in {CODE_SETTING} (it comes first), or see what the Datamart holds with "
            f"{PROBE}."
        )
    else:
        text += f" Syncing again changes nothing; see what the Datamart holds with {PROBE}."
    return text + (f" {area}" if area else ""), INFO


def _two_feeds(cursor) -> dict[str, Any]:
    today = datetime.date.today()
    zero = dict.fromkeys(rc.STATUS_LABELS, 0)
    _bound(cursor, statements=2)  # the two queries of progress_reports share the time left
    filters = reporting.ReportFilters(year=today.year)  # the running PDs, as the page by default
    ours: dict[Any, dict[str, int]] = {}  # indicator reports
    ours_reports: dict[Any, dict[str, int]] = {}  # progress reports (to compare with PRP's progress reports)
    for r in reporting.progress_reports(filters.queryset(today), today):
        month = r["period_end"].month if r["period_end"] else None
        counts = ours.setdefault(month, dict(zero))
        counts[r["status"]] += r["indicators"]
        ours_reports.setdefault(month, dict(zero))[r["status"]] += 1
    gaps, missing, estimated = _number_gaps(cursor, today)
    feed = _prp_feed(cursor)
    # PRP's records of the year by the period end they are counted by (their progress report's, else
    # their own: prp_document_status), read from the records whose own end is within a year of it, or that
    # have none of their own (the progress report's may still date them)
    near = dm.DatamartDocument.objects.filter(dataset=PRP).filter(
        Q(date__year__in=(today.year - 1, today.year, today.year + 1)) | Q(date=None)
    )
    _bound(cursor)
    found = (
        near.order_by()
        .annotate(
            _link=Case(
                When(intervention__isnull=True, then=Value(LINK_NOT_HELD)),
                When(intervention__status__in=pd_statuses.RUNNING, then=Value(LINK_RUNNING)),
                default=Value(LINK_OTHER_STATUS),
            ),
            _pr_status=KT("data__pr_status"),
            _status=KT("data__report_status"),
            _pr_end=KT("data__pr_end_date"),
            _pr_due=KT("data__pr_due_date"),
            _due=KT("data__due_date"),
            _pr_sub=KT("data__pr_submission_date"),
            _sub=KT("data__submission_date"),
        )
        .values("_link", "date", "_pr_status", "_status", "_pr_end", "_pr_due", "_due", "_pr_sub", "_sub")
        .annotate(n=Count("pk"))
    )
    theirs: dict[Any, dict[str, int]] = {}
    held: Counter[str] = Counter()  # the records of the year, by their programme document link
    undated = 0  # records with no period end of their own or of their progress report
    for g in found:
        # the rule of the Funds page for these records: the progress report's own fields first
        status, end = rc.prp_document_status(
            pr_status=g["_pr_status"],
            status=g["_status"],
            pr_end=g["_pr_end"],
            end=g["date"],
            pr_due=g["_pr_due"],
            due=g["_due"],
            pr_sub=g["_pr_sub"],
            sub=g["_sub"],
            today=today,
        )
        if end is None:
            undated += g["n"]
            continue
        if end.year != today.year:
            continue
        held[g["_link"]] += g["n"]
        if g["_link"] == LINK_RUNNING:
            counts = theirs.setdefault(end.month, dict(zero))
            counts[status] += g["n"]
    unlinked, other = held[LINK_NOT_HELD], held[LINK_OTHER_STATUS]
    other_years = max(feed["held"] - sum(held.values()) - undated, 0)
    owed = (rc.DUE, rc.OVERDUE)

    def owing(table: dict[Any, dict[str, int]], month: Any) -> int:
        return sum((table.get(month) or zero)[k] for k in owed)

    ours_owed = sum(owing(ours, m) for m in ours)
    theirs_owed = sum(owing(theirs, m) for m in theirs)
    not_yet = sum(counts[rc.NOT_YET_DUE] for counts in ours.values())
    figures = [
        ("Due or overdue in the partner reports", ours_owed),
        ("Not yet due in the partner reports", not_yet),
        ("Expected, not in the partner reports (number gaps, period ended)", estimated),
        ("… progress reports missing between the numbers held", missing),
        ("Due or overdue in PRP's indicator reports", theirs_owed),
        ("PRP indicator report records held", feed["held"]),
        ("PRP indicator reports of the year linked to a programme document", held[LINK_RUNNING] + other),
        ("… not linked to one", unlinked),
        ("PRP indicator report records of other years", other_years),
        ("PRP indicator report records with no period end", undated),
    ]
    run, skipped = feed["run"], _count(feed["details"].get("other_country_skipped"))
    figures += [  # what the last sync of PRP's indicator reports read ("—": never synced)
        (
            "Last sync of PRP's indicator reports: records read",
            "—" if run is None else _count(feed["details"].get("read")) or run.rows_in + skipped,
        ),
        ("… skipped as another business area", "—" if run is None else skipped),
    ]
    seen = feed["details"].get("business_areas_seen")
    tables = (
        [
            {
                "title": "Business area codes the records of the last sync of PRP's indicator reports named",
                "columns": ("Business area code", "Records read"),
                "rows": sorted(
                    ((str(k), _count(n)) for k, n in seen.items()), key=lambda kv: (-kv[1], kv[0])
                ),
            }
        ]
        if isinstance(seen, dict) and seen
        else []
    )
    # PRP's own progress reports: progress reports, compared with the partner reports' progress reports
    # (never with the indicator reports of the table above)
    progress = _progress_feed(cursor, today)
    mine = progress["table"]
    ours_reports_owed = sum(owing(ours_reports, m) for m in ours_reports)
    mine_owed = sum(owing(mine, m) for m in mine)
    figures += [
        ("Progress reports due or overdue in the partner reports", ours_reports_owed),
        ("Progress reports due or overdue in PRP's progress reports", mine_owed),
        ("PRP progress report records held", progress["held"]),
        ("PRP progress reports of the year linked to a programme document", progress["linked"]),
        ("PRP progress reports of the year linked to none", progress["unlinked"]),
        ("PRP progress report records with no period end", progress["undated"]),
    ]
    if mine or ours_reports:
        ends = sorted({*mine, *ours_reports}, key=lambda m: (m is None, m or 0))
        tables.append(
            {
                "title": "Progress reports of the running programme documents (not indicator reports), by "
                "period end: the partner reports and PRP's own progress reports",
                "columns": ("Period end", "Feed", *rc.STATUS_LABELS.values()),
                "rows": [
                    (
                        datetime.date(today.year, m, 1).strftime("%b %Y") if m else "No period end",
                        name,
                        *((counts.get(m) or zero)[k] for k in rc.STATUS_LABELS),
                    )
                    for m in ends
                    for name, counts in (("Partner reports", ours_reports), ("PRP progress reports", mine))
                ],
            }
        )
    if mine:
        name = {m: datetime.date(today.year, m, 1).strftime("%b %Y") for m in mine if m}
        owed_more = [name[m] for m in sorted(name) if owing(mine, m) > owing(ours_reports, m)]
        progress_text = (
            f"PRP's own progress reports list {mine_owed:,} due or overdue progress reports of the running "
            f"programme documents for {today.year}; the partner reports list {ours_reports_owed:,}."
        )
        if owed_more:
            progress_text += (
                f" PRP's progress reports list more reports still owed in {', '.join(owed_more)}: the "
                "partner reports feed does not carry them, and these may be the overdue reports Power BI "
                "counts."
            )
        progress_tone = WARN if owed_more else OK
    else:
        progress_text, progress_tone = _progress_state(progress, today.year)
    # what the partner reports feed carries (what Progress reports counts), whatever PRP's own feed holds
    lead, warn = [], progress_tone == WARN
    if ours and not ours_owed and not_yet:
        lead.append(
            "The partner reports feed (what Progress reports counts) carries no report still owed for a "
            "period that has ended: every report still to submit there is of a period not yet ended, so the "
            "overdue reports PRP or Power BI count are not in this feed."
        )
        warn = True
    if missing:
        lead.append(
            "Between the report numbers the partner reports hold for the running programme documents, "
            f"{_plural(missing, 'progress report', 'progress reports')} of a period ended this year "
            f"{'is' if missing == 1 else 'are'} missing (about "
            f"{_plural(estimated, 'indicator report', 'indicator reports')}, "
            "as many as the report next to each): expected, not in the feed."
        )
        warn = True
    months = sorted({*ours, *theirs, *gaps}, key=lambda m: (m is None, m or 0))
    label = {m: datetime.date(today.year, m, 1).strftime("%b %Y") if m else "No period end" for m in months}
    rows = [
        (
            label[m],
            feed_name,
            *((table.get(m) or zero)[k] for k in rc.STATUS_LABELS),
            gaps.get(m, 0) if table is ours else None,
        )
        for m in months
        for feed_name, table in (("Partner reports", ours), ("PRP indicator reports", theirs))
    ]
    table = {
        "columns": (
            "Period end",
            "Feed",
            *rc.STATUS_LABELS.values(),
            "Expected, not in the feed (number gaps)",
        ),
        "rows": rows,
    }
    if not theirs:
        text, tone = _no_prp(
            today.year, other, unlinked, ours=bool(ours), feed=feed, undated=undated, other_years=other_years
        )
        out = {
            "tone": WARN if warn or tone == WARN else INFO,
            "verdict": " ".join([*lead, text, progress_text]),
            "figures": figures,
            "tables": tables,
        }
        if months:
            out["table"] = table
        return out
    more = [label[m] for m in months if owing(theirs, m) > owing(ours, m)]
    fewer = [label[m] for m in months if owing(theirs, m) < owing(ours, m)]
    other_months = [
        label[m]
        for m in months
        if (ours.get(m) or zero) != (theirs.get(m) or zero) and label[m] not in (*more, *fewer)
    ]
    verdict = (
        f"For {today.year}, the partner reports (what Progress reports counts) list {ours_owed:,} due or "
        f"overdue indicator reports of the running programme documents; PRP's indicator reports list "
        f"{theirs_owed:,}."
    )
    if more:
        verdict += (
            f" PRP lists more reports still owed in {', '.join(more)}: the partner reports feed does not "
            "carry every report still owed."
        )
    if fewer:
        verdict += f" The partner reports list more reports still owed in {', '.join(fewer)}."
    if other_months:
        verdict += f" The two feeds give other statuses in {', '.join(other_months)}."
    if not (more or fewer or other_months):
        verdict += " Every month has the same indicator reports per status in both feeds."
    return {
        "tone": WARN if more or fewer or other_months or warn else OK,
        "verdict": " ".join([*lead, verdict, progress_text]),
        "figures": figures,
        "table": table,
        "tables": tables,
    }


def _no_prp(
    year: int, other: int, unlinked: int, *, ours: bool, feed: dict[str, Any], undated: int, other_years: int
) -> tuple[str, str]:
    """Check 11's verdict when PRP's indicator reports of the year hold none of a running programme
    document, and its tone: why (``other`` of the year link to a PD of another status, ``unlinked`` to none,
    the records held are of other years or have no period end, or the last sync kept none: what it did,
    :func:`_feed_state`), and what to do."""
    start = (
        f"PRP's indicator reports hold nothing of the running programme documents for {year}"
        if ours
        else f"Neither feed holds an indicator report of a running programme document for {year}"
    )
    if unlinked and not other:
        return (
            f"{start}: {unlinked:,} of the year are held, but none links to a programme document NeuroDB "
            "holds (their PD reference numbers match none). A sync helps only if the programme documents "
            "were synced after them.",
            INFO,
        )
    if other:
        return (
            f"{start}: the {other:,} of the year that link to a programme document are of PDs that are not "
            f"running{f', and {unlinked:,} link to none' if unlinked else ''}.",
            INFO,
        )
    if feed["held"]:
        return (
            f"{start}: {_plural(feed['held'], 'of its records is', 'of its records are')} held, none of a "
            f"period ending in {year} ({undated:,} with no period end of their own or of their progress "
            f"report, {other_years:,} of other years).",
            INFO,
        )
    text, tone = _feed_state(feed, listed=True)
    return f"{start}. {text}", tone


RUN_FIRST = ("10", "11")
CHECKS: tuple[Check, ...] = (
    Check(
        "1",
        "Rows per location",
        "Does a report carry more than one row for the same indicator and location?",
        "One value per location in each indicator report: when several rows carry different values, the "
        "largest is used, never their sum. Every page tells one report from another by its programme "
        "document, type, number and period, as this check does, whatever PRP's own ids hold (they can be "
        "empty, or one value for several reports: check 7 counts them); two indicator reports of a report by "
        "PRP's indicator report id, else their title and PD output. Where two or more indicator reports of a "
        "report carry a PD indicator's title, the one under the indicator's own output is used, else that "
        "report is left out with a note, never the largest of them nor their sum.",
        _duplicate_rows,
    ),
    Check(
        "2",
        "Value in the period, per location",
        "Is the value of a report the same on every one of its locations (the report total repeated)?",
        'A location\'s value is the disaggregation total "()" first, then its running total minus its '
        "previous one, then the value in the period. A value in the period repeated on every location is "
        "read as the report total, and never added up over the locations, only when every location's value "
        'comes from the value in the period (no "()" number and no running totals).',
        _repeated_value,
    ),
    Check(
        "3",
        "Disaggregations",
        'Which disaggregation keys appear, does the total ("()") hold a number, and does it equal the value '
        "in the period?",
        'The total "()" is each location\'s value when it holds a number (c, else v over d, else v, written '
        "as a number or as PRP's {v, d, c}). Disaggregation values add up only within one type; they are "
        "never added across types. The partner reports sync reads the label of each value id the reports "
        "name; a value whose label is not read yet is not listed on the By disaggregation tab.",
        _disaggregation,
    ),
    Check(
        "4",
        "Reports that do not count",
        "What do sent-back, due, overdue and not-yet-due reports carry?",
        "Only Accepted and Submitted reports count in Reached; what the others carry changes nothing. Values "
        'are read as Reached reads them, and here a zero (PRP\'s empty {v: 0, d: 0, c: 0}, "0" or "0.0") '
        "counts as no value.",
        _unsubmitted,
    ),
    Check(
        "5",
        "PRP's cumulative",
        "Is PRP's cumulative one running total, and does it equal NeuroDB's Reached?",
        "Reached is computed from the counted reports (Accepted and Submitted) of each indicator; PRP's "
        "cumulative on the latest counted report is shown beside it, and an indicator where they differ "
        "(beyond rounding) is marked Differs from PRP. A programme document with a partial history takes "
        "PRP's "
        "cumulative as its Reached. This check reads the figures of Partner monitoring's Data quality tab.",
        _cumulative,
    ),
    Check(
        "6",
        "Calculation methods",
        "How do the indicators combine locations and reporting periods?",
        "Each indicator's own method is followed: sum, maximum, average, latest; percentages and ratios as "
        "numerators over denominators, never added.",
        _methods,
    ),
    Check(
        "7",
        "Indicator identity",
        "Which PD indicator does each report row belong to, how many rows are left out, and which keys do "
        "the records send?",
        "A row belongs to the PD indicator whose eTools id it carries (the key "
        "etools_cp_output_indicators_id, empty so far), else to the one indicator of its PD that has its "
        "title (case and spaces aside), else, for a title two or more indicators of the PD share, to the one "
        "that also has the row's PD output; a row none of these places is left out. A report is its "
        "programme document, type, number and period, whatever PRP's ids hold. Monthly reports are used only "
        "for an indicator with no quarterly report, and never added to quarterly ones.",
        _identity,
    ),
    Check(
        "8",
        "PRP's report totals",
        "Does PRP's own report total equal the sum of the location totals?",
        "PRP's report total stands in only when the locations cannot say, and only when its record names "
        "the same programme document and indicator; when it differs from the locations, they are used.",
        _report_totals,
    ),
    Check(
        "9",
        "Financial contribution",
        "Is the financial contribution the same on every indicator of one progress report?",
        "It is taken once per progress report, from the latest one that counts, never added across its "
        "indicators (Funds → Partner contributions).",
        _financial,
    ),
    Check(
        "10",
        "Country and programme document of the rows",
        "Which country does each report row name, does it belong to a programme document NeuroDB holds, and "
        "how much of the running programme documents' reporting is held?",
        "A record naming another country is never written (the sync counts it in other_country_skipped). "
        "Progress reports counts the running programme documents by default; a report of a programme "
        "document NeuroDB does not hold only when every PD status is chosen, and a report that names no "
        "programme document never. The page says how many the PD status filter leaves out. Reached adds up "
        "the reports held: a running programme document whose first reports are not in the feed has a "
        "partial sum (the partial history rule covers only a PD that started before the window the sync "
        "read).",
        _country_and_link,
    ),
    Check(
        "11",
        "Reports owed, in PRP's feeds",
        "This year, do the partner reports, PRP's indicator reports and PRP's own progress reports list the "
        "same reports per status for the running programme documents, and which reports does the partner "
        "reports feed miss?",
        "Progress reports and Partner monitoring read the partner reports (prp/datareport); PRP's indicator "
        "reports (prp/indicator-report-v2) are kept whole for Ask NeuroDB and the Funds page, and PRP's own "
        "progress reports (sources/prp/unicefprogressreport) for Ask NeuroDB, each linked to its programme "
        "document through PRP's programme documents after their sync. All are read with the same status rule "
        "(PRP's word, then the dates); indicator reports are compared with indicator reports, progress "
        "reports with progress reports. A report the partner reports feed does not carry is counted nowhere "
        "else: the number gaps (a QPR2 missing between QPR1 and QPR3) are estimated here only.",
        _two_feeds,
    ),
)


def run_all(
    budget: float = PAGE_BUDGET_S,
    *,
    timeout_ms: int = TIMEOUT_MS,
    by: str = "page",
    before: Callable[[], None] | None = None,
) -> list[dict[str, Any]]:
    """Every check, each in its own read-only transaction whose statements stop after ``timeout_ms``;
    a check that fails or runs too long says so (``unfinished``) and the others still run. The checks
    share ``budget`` seconds: once it is used up, the checks left say they were not run (the page always
    answers before the web server's limit). ``by``: "page" or "job", for the words of a check left out.
    ``before``, called before each check, stops them all by raising (the job: its lock is lost)."""
    limit = f"the page's {int(budget)} seconds" if by == "page" else f"the job's {int(budget // 60)} minutes"
    done: dict[str, dict[str, Any]] = {}
    ran: list[str] = []  # the checks run so far, in their order
    deadline = time.monotonic() + budget
    # the checks of RUN_FIRST run first (they answer whether the report rows are this country's and the
    # reports owed, whatever the time the others take); the page lists every check in its order
    for check in sorted(CHECKS, key=lambda c: c.key not in RUN_FIRST):
        entry = {"key": check.key, "title": check.title, "question": check.question, "neurodb": check.neurodb}
        done[check.key] = entry
        if before:
            before()
        if deadline - time.monotonic() < 1:
            entry.update(_not_run(ran, limit=limit, by=by))
            continue
        timed = None
        try:
            with transaction.atomic(), connection.cursor() as cursor:
                timed = _Timed(cursor, deadline, timeout_ms)
                entry.update(check.run(timed))
        except OutOfTime:
            entry.update(_not_run(ran, started=True, limit=limit, by=by))
        except DatabaseError as exc:
            logger.warning("eTools reporting check %s failed: %s", check.key, exc)
            cause = exc.__cause__
            cancelled = (getattr(cause, "sqlstate", None) or getattr(cause, "pgcode", None)) == QUERY_CANCELED
            if cancelled and timed and timed.last_ms < timeout_ms:
                # the time left for the checks, shorter than a query's own limit, stopped it
                entry.update(_not_run(ran, started=True, limit=limit, by=by))
            else:
                why = (
                    f"a query took longer than its {_duration(timeout_ms)}"
                    if cancelled
                    else "the data could not be read"
                )
                entry.update(
                    {
                        "tone": WARN,
                        "verdict": f"This check could not finish ({why}).",
                        "figures": [],
                        "unfinished": True,
                    }
                )
        ran.append(check.key)
    return [done[c.key] for c in CHECKS]


def _not_run(
    ran: list[str],
    *,
    started: bool = False,
    limit: str = f"the page's {int(PAGE_BUDGET_S)} seconds",
    by: str = "page",
) -> dict[str, Any]:
    """The verdict of a check the time for the checks left out: ``ran``, the checks run before it (checks
    10 and 11 run first, so they are not the ones listed above it); ``started``, it ran out partway
    through; ``limit``, that time in words ("the page's 75 seconds")."""
    if started:
        why = f"this check ran out of {limit} partway through"
    elif ran:
        named = ", ".join(ran[:-1]) + f" and {ran[-1]}" if len(ran) > 1 else ran[0]
        kind = "checks" if len(ran) > 1 else "check"
        why = f"{kind} {named}, run before it, used up {limit}"
    else:
        why = f"{limit} for the checks were used up"
    first = " and ".join(RUN_FIRST)
    stops = "; the page stops there so that it always answers" if by == "page" else ""
    return {
        "tone": INFO,
        "verdict": (
            f"Not run: {why} (checks {first} run first, then the others in order{stops}). The data is large "
            "or the database is busy."
        ),
        "figures": [],
        "unfinished": True,
    }


# ------------------------------------------------------------------------------------------- the job
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
        logger.warning("eTools reporting checks: the lock could not be released", exc_info=True)


class LostLock(RuntimeError):
    """The run's database session no longer holds the checks' lock: the connection was lost (the lock
    went with it) and another run may have started."""


def _still_locked() -> None:
    """Raise :class:`LostLock` unless this session still holds the lock. After a lost connection Django
    opens a new session at the next query, which holds no lock: the run would go on unseen, and
    ``background.is_running`` would close its row and let a second run start."""
    if connection.vendor != "postgresql":
        return
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT 1 FROM pg_locks WHERE locktype = 'advisory' AND classid = 0 AND objid = %s "
            "AND objsubid = 1 AND pid = pg_backend_pid() AND granted",
            [LOCK_ID],
        )
        if cursor.fetchone() is None:
            raise LostLock(
                "The database connection was lost during the checks (another run may have started): "
                "nothing was stored."
            )


BUSY = "The eTools reporting checks are already running."


def run_job(triggered_by: str = "command"):
    """Run every check with the job's time (``JOB_TIMEOUT_MS`` per statement, ``JOB_BUDGET_S`` in all)
    and store the result in the run's details (``checks``), which the page shows. SUCCEEDED when every
    check finished, PARTIAL when one did not. Returns the run, or :data:`BUSY` when another run holds the
    lock (nothing is recorded)."""
    from neurodb.core.models import SyncRun
    from neurodb.integrations.runs import fail, new_run

    if not _lock():
        return BUSY
    try:
        run = new_run(SyncRun.Job.REPORTING_CHECKS, "", triggered_by)
        try:
            found = run_all(JOB_BUDGET_S, timeout_ms=JOB_TIMEOUT_MS, by="job", before=_still_locked)
            _still_locked()
        except Exception as exc:  # recorded as a failed run; the page keeps the result before it
            return fail(run, exc)
        # stored as JSON: the checks return plain data (check 10 its dates as text already); anything else
        # (a decimal) is stored as text
        found = json.loads(json.dumps(found, cls=DjangoJSONEncoder))
        unfinished = [c["key"] for c in found if c.get("unfinished")]
        run.rows_written = len(found) - len(unfinished)
        run.rows_failed = len(unfinished)
        # Import and sync runs shows these under "Why rows failed"
        run.details["errors"] = [
            {"error": c["verdict"], "count": 1, "examples": [f"check {c['key']}"]}
            for c in found
            if c.get("unfinished")
        ]
        status = SyncRun.Status.PARTIAL if unfinished else SyncRun.Status.SUCCEEDED
        run.finish(status, checks=found, unfinished=unfinished)
        return run
    finally:
        _unlock()


def latest_run():
    """The last finished run of the job that stored the checks, or None (a run that failed before the
    checks ended stored none; one stopped from the admin keeps what it stored)."""
    from neurodb.core.models import SyncRun

    return (
        SyncRun.objects.filter(job=SyncRun.Job.REPORTING_CHECKS, details__has_key="checks")
        .exclude(finished_at=None)
        .order_by("-finished_at", "-id")
        .first()
    )
