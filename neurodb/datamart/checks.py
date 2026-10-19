"""eTools reporting checks: what the partner report data looks like, for the "Reached" rule.

``neurodb.datamart.reached`` is built to be right whichever way these questions come out; this page
(admin → eTools reporting checks, Administrators only, linked from Data health) shows how they do
come out on the data NeuroDB holds, with a plain verdict and what NeuroDB does in each case. Every
query is read-only and aggregate (no person's data).

The checks run as a job (:func:`run_job`: the ``reporting-checks`` schedule every night, or *Run the
checks now* on the page and *Run a job*), with ``JOB_TIMEOUT_MS`` for each statement and
``JOB_BUDGET_S`` for them all, and the result is stored in its run's details: the page shows the latest
one at once. Only when no run has stored a result yet does the page run them itself, with
``TIMEOUT_MS`` per statement and ``PAGE_BUDGET_S`` for them all (well below the web server's 120-second
limit), so it always answers: a check left out says so.
"""

from __future__ import annotations

import datetime
import json
import logging
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from django.conf import settings
from django.contrib.postgres.aggregates import ArrayAgg
from django.core.serializers.json import DjangoJSONEncoder
from django.db import DatabaseError, connection, transaction
from django.db.models import Case, Count, Max, Min, Q, TextField, Value, When
from django.db.models.fields.json import KT
from django.db.models.functions import Coalesce, NullIf, Trim

from neurodb.partnerships import statuses as pd_statuses

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
NUM = r"^\s*-?[0-9]+(\.[0-9]+)?\s*$"
OK, WARN, INFO = "ok", "warning", "info"


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


def _day(day: datetime.date | None) -> str | None:
    """A date in a check's table, as text ("31 Mar 2026", in English whatever the language, as check
    11's months): the page shows the same whether it ran the checks itself or reads what the job
    stored."""
    return f"{day.day} {day:%b %Y}" if day else None


def _duration(ms: int) -> str:
    """A time limit in words: "10 minutes", "20 seconds", "1 second"."""
    if ms >= 60_000 and ms % 60_000 == 0:
        n, unit = ms // 60_000, "minute"
    else:
        n, unit = max(ms // 1000, 1), "second"
    return f"{n} {unit}{'' if n == 1 else 's'}"


def _share(part: int, whole: int) -> str:
    return f"{round(100 * part / whole)} %" if whole else "—"


def _duplicate_rows(cursor) -> dict[str, Any]:
    (rows,) = _one(cursor, "SELECT count(*) FROM datamart_reportedindicator")
    (groups,) = _one(
        cursor,
        "SELECT count(*) FROM (SELECT 1 FROM datamart_reportedindicator "
        "GROUP BY progress_report, etools_indicator_id, indicator, coalesce(nullif(p_code, ''), location) "
        "HAVING count(*) > 1) x",
    )
    if not rows:
        return {"tone": INFO, "verdict": "No partner report rows are held yet.", "figures": []}
    return {
        "tone": OK if not groups else WARN,
        "verdict": (
            "Each location appears on one row per indicator in each report."
            if not groups
            else f"{groups:,} locations appear on more than one row of the same report and indicator."
        ),
        "figures": [("Partner report rows", rows), ("Locations on several rows of one report", groups)],
    }


def _repeated_value(cursor) -> dict[str, Any]:
    several, same = _one(
        cursor,
        "SELECT count(*) FILTER (WHERE l > 1), count(*) FILTER (WHERE l > 1 AND v = 1) FROM ("
        "SELECT progress_report, etools_indicator_id, indicator, "
        "count(DISTINCT coalesce(nullif(p_code, ''), location)) l, count(DISTINCT achievement_in_period) v "
        "FROM datamart_reportedindicator WHERE achievement_in_period <> '' GROUP BY 1, 2, 3) g",
    )
    if not several:
        verdict, tone = "No indicator report has more than one location with a value yet.", INFO
    elif same / several >= 0.8:
        verdict = (
            f"In {same:,} of {several:,} indicator reports with several locations ({_share(same, several)}), "
            "every location carries the same value: the field is the report total repeated on each row."
        )
        tone = WARN
    else:
        verdict = (
            f"The value differs between locations in {several - same:,} of {several:,} indicator reports "
            "with several locations: it looks like each location's own value."
        )
        tone = OK
    return {
        "tone": tone,
        "verdict": verdict,
        "figures": [
            ("Indicator reports with several locations", several),
            ("… with the same value on every location", same),
        ],
    }


def _disaggregation(cursor) -> dict[str, Any]:
    with_total, equal = _one(
        cursor,
        "SELECT count(*) FILTER (WHERE disaggregation ? '()'), count(*) FILTER (WHERE disaggregation ? '()' "
        "AND achievement_in_period ~ %s AND (disaggregation->'()'->>'c') ~ %s "
        "AND achievement_in_period::numeric = (disaggregation->'()'->>'c')::numeric) "
        "FROM datamart_reportedindicator WHERE jsonb_typeof(disaggregation) = 'object'",
        [NUM, NUM],
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
    if not with_total:
        verdict, tone = 'No row carries a disaggregation total ("()") yet: the other fields are used.', INFO
    else:
        verdict = (
            f"{with_total:,} rows carry a disaggregation total; on {equal:,} of them "
            f"({_share(equal, with_total)}) it equals the value in the period."
        )
        tone = OK if equal == with_total else INFO
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
            ("Rows with a disaggregation total", with_total),
            ("… equal to the value in the period", equal),
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
    rows = _all(
        cursor,
        "SELECT coalesce(nullif(trim(report_status), ''), '(none)'), "  # noqa: S608 (constants only)
        f"{COUNTS_SQL}, "
        "CASE WHEN period_end > current_date THEN 'yes' ELSE 'no' END, count(*), "
        "count(*) FILTER (WHERE total_cumulative_progress ~ '^\\s*0+(\\.0+)?\\s*$'), "
        "count(*) FILTER (WHERE total_cumulative_progress = ''), "
        "count(*) FILTER (WHERE achievement_in_period NOT IN ('', '0')) "
        "FROM datamart_reportedindicator GROUP BY 1, 2, 3 ORDER BY 1, 2, 3",
    )
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


def _cumulative(cursor) -> dict[str, Any]:
    several, same = _one(
        cursor,
        "SELECT count(*), count(*) FILTER (WHERE cums = 1) FROM ("
        "SELECT intervention_id, etools_indicator_id, indicator, "
        "count(DISTINCT total_cumulative_progress) cums "
        "FROM datamart_reportedindicator WHERE total_cumulative_progress <> '' GROUP BY 1, 2, 3 "
        "HAVING count(DISTINCT progress_report) > 1) g",
    )
    # NeuroDB's Reached for every indicator of the running PDs (Python; its queries keep the statement
    # timeout the query above set, within the same transaction): only with PRP_COMPARE_MIN_S of the
    # checks' time left
    left = cursor.remaining() if hasattr(cursor, "remaining") else PAGE_BUDGET_S
    differ, compared = _reached_against_prp() if left >= PRP_COMPARE_MIN_S else (0, 0)
    skipped = left < PRP_COMPARE_MIN_S
    if not several:
        verdict, tone = "No indicator has several reports with PRP's cumulative yet.", INFO
    else:
        verdict = (
            f"{same:,} of {several:,} indicators with several reports carry the same cumulative on every "
            f"report ({_share(same, several)}); "
            + (
                "NeuroDB's Reached was not compared with PRP's cumulative (the checks run before it, "
                "checks 10 and 11 first, used up most of the time for the checks); the Data quality tab of "
                "Partner monitoring lists the indicators where they differ."
                if skipped
                else f"NeuroDB's Reached differs from PRP's cumulative on {differ:,} of {compared:,} "
                "indicators of the running programme documents."
                if compared
                else "no running programme document has an indicator to compare."
            )
        )
        tone = WARN if differ else (INFO if skipped else OK)
    return {
        "tone": tone,
        "verdict": verdict,
        "figures": [
            ("Indicators with several reports", several),
            ("… with the same cumulative on every report", same),
            ("Indicators where Reached differs from PRP", "—" if skipped else differ),
        ],
    }


def _reached_against_prp() -> tuple[int, int]:
    """How many indicators of the running PDs have a Reached that differs from PRP's cumulative."""
    from . import monitoring

    rows = monitoring.indicators(monitoring.Filters(scope="active", year=None))
    compared = [r for r in rows if r.reached is not None and r.prp_cumulative is not None]
    return sum(1 for r in compared if r.mismatch), len(compared)


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


def _identity(cursor) -> dict[str, Any]:
    with_id, matched, other_title = _one(
        cursor,
        "SELECT count(*), count(p.source_id), count(*) FILTER (WHERE p.source_id IS NOT NULL "
        "AND lower(trim(p.title)) <> lower(trim(r.indicator))) "
        "FROM datamart_reportedindicator r LEFT JOIN LATERAL ("
        "SELECT source_id, title FROM datamart_pdindicator p WHERE p.intervention_id = r.intervention_id "
        "AND p.source_id::text = r.etools_indicator_id LIMIT 1) p ON true WHERE r.etools_indicator_id <> ''",
    )
    humanitarian, both = _one(
        cursor,
        "SELECT count(*) FILTER (WHERE hr), count(*) FILTER (WHERE hr AND qpr) FROM ("
        "SELECT intervention_id, etools_indicator_id, indicator, bool_or(report_type = 'HR') hr, "
        "bool_or(report_type = 'QPR') qpr FROM datamart_reportedindicator GROUP BY 1, 2, 3) g",
    )
    if not with_id:
        verdict, tone = "No report row carries an eTools indicator id: rows are matched by title.", INFO
    else:
        verdict = (
            f"{matched:,} of {with_id:,} rows with an eTools indicator id ({_share(matched, with_id)}) match "
            f"a PD indicator of their programme document; {other_title:,} of those carry another title."
        )
        tone = OK if matched == with_id and not other_title else WARN
    return {
        "tone": tone,
        "verdict": verdict,
        "figures": [
            ("Rows with an eTools indicator id", with_id),
            ("… matching a PD indicator", matched),
            ("… of which with another title", other_title),
            ("Indicators in monthly (HR) reports", humanitarian),
            ("… also in quarterly (QPR) reports", both),
        ],
    }


def _report_totals(cursor) -> dict[str, Any]:
    reports, compared, equal, other_pd = _one(
        cursor,
        "WITH r AS MATERIALIZED (SELECT indicator_report, min(intervention_id) pd, "
        "sum(CASE WHEN (disaggregation->'()'->>'c') ~ %s THEN (disaggregation->'()'->>'c')::numeric END) s "
        "FROM datamart_reportedindicator WHERE indicator_report ~ '^[0-9]{1,18}$' GROUP BY 1) "
        "SELECT count(*), count(*) FILTER (WHERE d.v IS NOT NULL AND r.s IS NOT NULL), "
        "count(*) FILTER (WHERE d.v IS NOT NULL AND r.s IS NOT NULL AND abs(d.v - r.s) <= 0.5), "
        "count(*) FILTER (WHERE d.pd IS NOT NULL AND r.pd IS NOT NULL AND d.pd <> r.pd) "
        "FROM r JOIN LATERAL (SELECT CASE WHEN (x.data->>'progress_value') ~ %s "
        "THEN (x.data->>'progress_value')::numeric WHEN (x.data->'progress_value'->>'c') ~ %s "
        "THEN (x.data->'progress_value'->>'c')::numeric END v, x.intervention_id pd "
        "FROM datamart_datamartdocument x "
        "WHERE x.dataset = 'prp_indicator_reports' AND x.source_id = r.indicator_report::bigint LIMIT 1) d "
        "ON true",
        [NUM, NUM, NUM],
    )
    if not reports:
        verdict, tone = "No indicator report can be found in PRP's indicator reports yet.", INFO
    else:
        verdict = (
            f"PRP's report total equals the sum of the location totals on {equal:,} of {compared:,} "
            f"indicator reports that can be compared ({_share(equal, compared)})."
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
    return {
        "tone": tone,
        "verdict": verdict,
        "figures": [
            ("Indicator reports found in PRP's indicator reports", reports),
            ("… that can be compared", compared),
            ("… equal", equal),
            ("… about another programme document", other_pd),
        ],
    }


def _financial(cursor) -> dict[str, Any]:
    reports, varying = _one(
        cursor,
        "SELECT count(*), count(*) FILTER (WHERE n > 1) FROM (SELECT data->>'pd_reference_number', "
        "data->>'report_type', data->>'report_number', "
        "count(DISTINCT data->>'financial_contribution_to_date') n "
        "FROM datamart_datamartdocument WHERE dataset = 'prp_indicator_reports' "
        "AND coalesce(data->>'financial_contribution_to_date', '') <> '' GROUP BY 1, 2, 3) g",
    )
    if not reports:
        verdict, tone = "No PRP indicator report carries a financial contribution yet.", INFO
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
    groups = list(
        dm.ReportedIndicator.objects.order_by()
        .annotate(
            _country=Coalesce(
                NullIf(Trim(KT("data__country_name")), Value("", output_field=TextField())),
                Value(NO_COUNTRY, output_field=TextField()),
            )
        )
        .annotate(_link=link)
        .values("_country", "_link")
        .annotate(
            rows=Count("pk"),
            reports=Count(reporting.INDICATOR_REPORT_KEY, distinct=True),
            first=Min("period_end"),
            last=Max("period_end"),
            sections=ArrayAgg(NullIf(Trim("section"), Value("")), distinct=True, filter=~Q(section="")),
            numbers=ArrayAgg("pd_reference_number", distinct=True, filter=Q(intervention__isnull=True)),
        )
        .order_by("_country", "_link")
    )
    if not groups:
        return {"tone": INFO, "verdict": "No partner report rows are held yet.", "figures": []}
    # the sync's own rule (case and spaces aside; nothing is another country's while none is set)
    other = sum(
        g["rows"]
        for g in groups
        if g["_country"] != NO_COUNTRY and other_country({"country_name": g["_country"]}, country)
    )
    not_held = sum(g["reports"] for g in groups if g["_link"] == LINK_NOT_HELD)
    no_pd = sum(g["reports"] for g in groups if g["_link"] == LINK_NONE)
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
    rows = [
        (
            g["_country"],
            g["_link"],
            g["rows"],
            g["reports"],
            _day(g["first"]),
            _day(g["last"]),
            ", ".join(sorted(x for x in g["sections"] or [] if x)[:5]) or None,
            ", ".join(sorted(x for x in g["numbers"] or [] if x)[:3]) or None,
        )
        for g in groups
    ]
    return {
        "tone": WARN if other or not_held else (INFO if no_pd else OK),
        "verdict": (
            "; ".join(parts) + "."
            if parts
            else "Every row is of this country and belongs to a programme document NeuroDB holds."
        ),
        "figures": [
            ("Rows naming another country", other),
            ("Indicator reports of programme documents not held", not_held),
            ("Indicator reports naming no programme document", no_pd),
        ],
        "table": {
            "columns": (
                "Country in the row",
                "Programme document",
                "Rows",
                "Indicator reports",
                "First period end",
                "Last period end",
                "Sections (first 5)",
                "PD numbers not held (first 3)",
            ),
            "rows": rows,
        },
    }


def _two_feeds(cursor) -> dict[str, Any]:
    today = datetime.date.today()
    zero = dict.fromkeys(rc.STATUS_LABELS, 0)
    _bound(cursor, statements=2)  # the two queries of progress_reports share the time left
    filters = reporting.ReportFilters(year=today.year)  # the running PDs, as the page by default
    ours: dict[Any, dict[str, int]] = {}
    for r in reporting.progress_reports(filters.queryset(today), today):
        counts = ours.setdefault(r["period_end"].month if r["period_end"] else None, dict(zero))
        counts[r["status"]] += r["indicators"]
    # PRP's records of the year by the period end they are counted by (their progress report's, else
    # their own: prp_document_status), read from the records whose own end is within a year of it
    near = dm.DatamartDocument.objects.filter(
        dataset="prp_indicator_reports", date__year__in=(today.year - 1, today.year, today.year + 1)
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
        if end is None or end.year != today.year:
            continue
        held[g["_link"]] += g["n"]
        if g["_link"] == LINK_RUNNING:
            counts = theirs.setdefault(end.month, dict(zero))
            counts[status] += g["n"]
    unlinked, other = held[LINK_NOT_HELD], held[LINK_OTHER_STATUS]
    figures = [
        ("Due or overdue in the partner reports", 0),
        ("Due or overdue in PRP's indicator reports", 0),
        ("PRP indicator reports of the year linked to a programme document", held[LINK_RUNNING] + other),
        ("… not linked to one", unlinked),
    ]
    if not ours and not theirs:
        return {
            "tone": INFO,
            "verdict": _no_prp(today.year, other, unlinked, ours=False),
            "figures": figures,
        }
    owed = (rc.DUE, rc.OVERDUE)

    def owing(table: dict[Any, dict[str, int]], month: Any) -> int:
        return sum((table.get(month) or zero)[k] for k in owed)

    months = sorted({*ours, *theirs}, key=lambda m: (m is None, m or 0))
    label = {m: datetime.date(today.year, m, 1).strftime("%b %Y") if m else "No period end" for m in months}
    rows = [
        (label[m], feed, *((table.get(m) or zero)[k] for k in rc.STATUS_LABELS))
        for m in months
        for feed, table in (("Partner reports", ours), ("PRP indicator reports", theirs))
    ]
    ours_owed, theirs_owed = sum(owing(ours, m) for m in months), sum(owing(theirs, m) for m in months)
    figures[0], figures[1] = (figures[0][0], ours_owed), (figures[1][0], theirs_owed)
    table = {"columns": ("Period end", "Feed", *rc.STATUS_LABELS.values()), "rows": rows}
    if not theirs:
        return {
            "tone": INFO,
            "verdict": _no_prp(today.year, other, unlinked, ours=True),
            "figures": figures,
            "table": table,
        }
    more = [label[m] for m in months if owing(theirs, m) > owing(ours, m)]
    fewer = [label[m] for m in months if owing(theirs, m) < owing(ours, m)]
    other = [
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
    if other:
        verdict += f" The two feeds give other statuses in {', '.join(other)}."
    if not (more or fewer or other):
        verdict += " Every month has the same indicator reports per status in both feeds."
    return {
        "tone": WARN if more or fewer or other else OK,
        "verdict": verdict,
        "figures": figures,
        "table": table,
    }


def _no_prp(year: int, other: int, unlinked: int, *, ours: bool) -> str:
    """Check 11's verdict when PRP's indicator reports of the year hold none of a running programme
    document: why (``other`` of the year link to a PD of another status, ``unlinked`` to none), and what
    to do."""
    start = (
        f"PRP's indicator reports hold nothing of the running programme documents for {year}"
        if ours
        else f"Neither feed holds an indicator report of a running programme document for {year}"
    )
    if unlinked and not other:
        return (
            f"{start}: {unlinked:,} of the year are held, but none links to a programme document NeuroDB "
            "holds (their PD reference numbers match none). A sync helps only if the programme documents "
            "were synced after them."
        )
    if other:
        return (
            f"{start}: the {other:,} of the year that link to a programme document are of PDs that are not "
            f"running{f', and {unlinked:,} link to none' if unlinked else ''}."
        )
    return f"{start}: run the eTools Datamart sync (prp_indicator_reports) to compare."


RUN_FIRST = ("10", "11")
CHECKS: tuple[Check, ...] = (
    Check(
        "1",
        "Rows per location",
        "Does a report carry more than one row for the same indicator and location?",
        "One value per location in each report: when several rows carry different values, the largest is "
        "used, never their sum.",
        _duplicate_rows,
    ),
    Check(
        "2",
        "Value in the period, per location",
        "Is the value in the period the same on every location of a report (the report total repeated)?",
        "A location's value is the disaggregation total first, then its running total minus its previous "
        "one, then the value in the period. A value repeated on every location is read as the report total "
        "and never added up over the locations.",
        _repeated_value,
    ),
    Check(
        "3",
        "Disaggregations",
        'Which disaggregation keys appear, and does the total ("()") equal the value in the period?',
        'The total "()" is each location\'s value. Disaggregation values add up only within one type; '
        "they are never added across types. The partner reports sync reads the label of each value id the "
        "reports name; a value whose label is not read yet is not listed on the By disaggregation tab.",
        _disaggregation,
    ),
    Check(
        "4",
        "Reports that do not count",
        "What do sent-back, due, overdue and not-yet-due reports carry?",
        "Only Accepted and Submitted reports count in Reached; what the others carry changes nothing.",
        _unsubmitted,
    ),
    Check(
        "5",
        "PRP's cumulative",
        "Is PRP's cumulative one running total, and does it equal NeuroDB's Reached?",
        "Reached is computed from the reports; PRP's cumulative on the latest counted report is shown beside "
        "it, and an indicator where they differ is marked.",
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
        "Does the eTools indicator id of a report row match the PD indicator's id, and do monthly indicators "
        "also appear in quarterly reports?",
        "Rows are matched to the PD indicator by its id, by title only when there is no id. Monthly reports "
        "are used only for an indicator with no quarterly report, and never added to quarterly ones.",
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
        "Which country does each report row name, and does it belong to a programme document NeuroDB holds?",
        "A record naming another country is never written (the sync counts it in other_country_skipped). "
        "Progress reports counts the running programme documents by default; a report of a programme "
        "document NeuroDB does not hold only when every PD status is chosen, and a report that names no "
        "programme document never. The page says how many the PD status filter leaves out.",
        _country_and_link,
    ),
    Check(
        "11",
        "Reports owed, in PRP's two feeds",
        "This year, do the partner reports and PRP's indicator reports list the same reports per status, for "
        "the running programme documents?",
        "Progress reports and Partner monitoring read the partner reports (prp/datareport); PRP's indicator "
        "reports (prp/indicator-report-v2) are kept whole for Ask NeuroDB. Both are read with the same "
        "status rule (PRP's word, then the dates).",
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
