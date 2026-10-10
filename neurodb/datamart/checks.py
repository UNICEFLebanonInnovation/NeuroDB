"""eTools reporting checks: what the partner report data looks like, for the "Reached" rule.

``neurodb.datamart.reached`` is built to be right whichever way these questions come out; this page
(admin → eTools reporting checks, Administrators only, linked from Data health) shows how they do
come out on the data NeuroDB holds, with a plain verdict and what NeuroDB does in each case. Every
query is read-only, aggregate (no person's data), and stopped after ``TIMEOUT_MS``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from django.db import DatabaseError, connection, transaction

logger = logging.getLogger(__name__)

TIMEOUT_MS = 20_000
NUM = r"^\s*-?[0-9]+(\.[0-9]+)?\s*$"
OK, WARN, INFO = "ok", "warning", "info"


@dataclass
class Check:
    key: str
    title: str
    question: str
    neurodb: str  # what NeuroDB does, whatever the answer
    run: Callable[[Any], dict[str, Any]]


def _one(cursor, sql: str, params: list[Any] | None = None) -> tuple:
    cursor.execute(sql, params)
    return cursor.fetchone()


def _all(cursor, sql: str, params: list[Any] | None = None) -> list[tuple]:
    cursor.execute(sql, params)
    return cursor.fetchall()


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
    if not with_total:
        verdict, tone = 'No row carries a disaggregation total ("()") yet: the other fields are used.', INFO
    else:
        verdict = (
            f"{with_total:,} rows carry a disaggregation total; on {equal:,} of them "
            f"({_share(equal, with_total)}) it equals the value in the period."
        )
        tone = OK if equal == with_total else INFO
    return {
        "tone": tone,
        "verdict": verdict,
        "figures": [
            ("Rows with a disaggregation total", with_total),
            ("… equal to the value in the period", equal),
        ],
        "table": {"columns": ("Disaggregation key", "Rows"), "rows": keys},
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
    differ, compared = _reached_against_prp()
    if not several:
        verdict, tone = "No indicator has several reports with PRP's cumulative yet.", INFO
    else:
        verdict = (
            f"{same:,} of {several:,} indicators with several reports carry the same cumulative on every "
            f"report ({_share(same, several)}); "
            + (
                f"NeuroDB's Reached differs from PRP's cumulative on {differ:,} of {compared:,} indicators "
                "of the running programme documents."
                if compared
                else "no running programme document has an indicator to compare."
            )
        )
        tone = WARN if differ else OK
    return {
        "tone": tone,
        "verdict": verdict,
        "figures": [
            ("Indicators with several reports", several),
            ("… with the same cumulative on every report", same),
            ("Indicators where Reached differs from PRP", differ),
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
        "they are never added across types.",
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
        "It is taken once per progress report, never added across its indicators.",
        _financial,
    ),
)


def run_all() -> list[dict[str, Any]]:
    """Every check, each in its own read-only transaction stopped after ``TIMEOUT_MS``; a check that
    fails or runs too long says so and the others still run."""
    out = []
    for check in CHECKS:
        entry = {"key": check.key, "title": check.title, "question": check.question, "neurodb": check.neurodb}
        try:
            with transaction.atomic(), connection.cursor() as cursor:
                cursor.execute(f"SET LOCAL statement_timeout = {int(TIMEOUT_MS)}")
                entry.update(check.run(cursor))
        except DatabaseError as exc:
            logger.warning("eTools reporting check %s failed: %s", check.key, exc)
            entry.update(
                {
                    "tone": WARN,
                    "verdict": (
                        "This check could not finish (it took too long or the data could not be read)."
                    ),
                    "figures": [],
                }
            )
        out.append(entry)
    return out
