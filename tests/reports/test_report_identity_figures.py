"""Figure safety for the report identity (findings ID-1, ID-6, ID-7): the figures of Partner monitoring, the
Progress reports page (tiles, reports, Report/Location), the indicator detail, the pop-up and the home page,
read on one small world (``identity_world``) in each of the three shapes PRP's ids can come in.

The figures were frozen in ``data/report_identity_golden.json`` with the code before the identity changed
(commit abb4394): a report told apart by PRP's progress report id first, then its indicator report id, then
its number and period; a title two PD indicators share always left out. Every figure must still equal the
frozen one, except those listed in ``CHANGED``: each with its value before and after, and the reason, which
is always that reports were merged that are not one report, or values were dropped. To freeze the figures
again (only when a figure is meant to change), run this test with ``REPORT_IDENTITY_WRITE_GOLDEN=1``."""

from __future__ import annotations

import datetime
import json
import os
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import pytest
from django.utils import timezone

from neurodb.core.models import SyncRun
from neurodb.datamart import identity, monitoring, reached, reporting, services
from tests.reports.identity_world import HOUSEHOLDS, SHAPES, TODAY, World

pytestmark = pytest.mark.django_db

GOLDEN = Path(__file__).parent / "data" / "report_identity_golden.json"


def _plain(value: Any) -> Any:
    """A figure as JSON keeps it: floats rounded (6 places), dates as ISO text, dict keys as text."""
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, datetime.date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, list | tuple):
        return [_plain(v) for v in value]
    return value


def _indicator_reports_by_month(reports: list[dict[str, Any]]) -> dict[str, list[int]]:
    """The indicator reports by month of the reporting period's end and status, as the chart counted them when
    the figures were frozen (``reporting.by_month``'s series, each report weighing its indicator reports)."""
    months: dict[Any, Counter] = defaultdict(Counter)
    for r in reports:
        months[reporting.report_month(r)][r["status"]] += r["indicators"]
    order = sorted(m for m in months if m is not None) + ([None] if None in months else [])
    present = [k for k in reporting.STATUS_KEYS if any(months[m].get(k) for m in order)]
    return {reached.STATUS_LABELS[k]: [months[m].get(k, 0) for m in order] for k in present}


def figures(world: World) -> dict[str, Any]:
    out: dict[str, Any] = {}
    # Partner monitoring: every PD, each report type, the year and every year
    for kind in ("QPR", "HR"):
        for year in (2026, None):
            filters = monitoring.Filters(scope="all", report_type=kind, year=year)
            rows = monitoring.indicators(filters, today=TODAY)
            for r in rows:
                result = r.result
                out[f"monitoring/{kind}/{year or 'all'}/{r.pd.number}/{r.key}"] = {
                    "reached": r.reached,
                    "reached_accepted": result.reached_accepted if result else None,
                    "reached_in_year": r.reached_in_year,
                    "awaiting": r.awaiting,
                    "awaiting_reports": r.awaiting_reports,
                    "reports": r.reports,
                    "value_status": r.value_status,
                    "read_from": r.report_type,
                    "latest_status": r.report_status,
                    "as_of": r.as_of,
                    "months": r.months,
                    "years": r.years,
                    "by_location": {p["name"]: p["reached"] for p in r.by_location.values()},
                    "statuses": [f"{x.number}:{x.status}" for x in result.reports] if result else [],
                }
            summary = monitoring.summary(rows, filters, today=TODAY)
            out[f"summary/{kind}/{year or 'all'}"] = {
                k: summary[k]
                for k in (
                    "indicators",
                    "with_reached",
                    "reported_zero",
                    "awaiting_acceptance",
                    "overdue_reports",
                )
            }
            quality = monitoring.data_quality(rows, filters, today=TODAY)
            out[f"quality/{kind}/{year or 'all'}"] = {
                k: quality[k]
                for k in ("with_reached", "without_reached", "same_title_count", "no_reached_count")
            }
    # the indicator detail's matrix: each report's total and status, each location's Reached
    for letter, key, kind in (("A", "1101", "QPR"), ("A", "1103", "HR"), ("B", "2101", "QPR")):
        pd = world.pds[letter]
        detail = monitoring.indicator_detail(pd, key, kind, None, today=TODAY)
        out[f"detail/{pd.number}/{key}"] = {
            "periods": [f"{p['report']}:{p['status']}:{_plain(p['total'])}" for p in detail["periods"]],
            "by_location": {p["location"]: p["reached"] for p in detail["by_location"]},
        }
    # Progress reports: the running PDs (the default) and every PD status
    for choice, params in (("running", {}), ("every", {"pd_status": ""})):
        data = services.partner_reporting(params, today=TODAY, options=False)
        out[f"progress/{choice}/reports"] = sorted(
            f"{r['pd_reference_number']}|{r['report_type']}|{r['report_number']}|{_plain(r['period_end'])}|"
            f"{r['status']}|{r['indicators']}"
            for r in data["reports"]
        )
        # the indicator reports per status and per month, as frozen (the tiles and the chart count progress
        # reports since the user's decision of 26 Oct 2026: "summary" and "reports" freeze those)
        counts = data["counts"]
        out[f"progress/{choice}/counts"] = {
            **{k: v for k, v in counts.items() if k not in ("statuses", *reporting.STATUS_KEYS)},
            **{s["key"]: s["indicator_reports"] for s in counts["statuses"]},
        }
        out[f"progress/{choice}/summary"] = data["summary"]["statuses"]
        out[f"progress/{choice}/by_month"] = _indicator_reports_by_month(data["reports"])
        out[f"progress/{choice}/partners"] = {
            k: data["partners"][k] for k in ("partners", "reported", "not_reported")
        }
        out[f"progress/{choice}/left_out"] = reporting.left_out(
            reporting.ReportFilters.from_params(params), TODAY
        )
        found = reporting.location_rows(data["rows"], data["statuses"], TODAY)
        out[f"progress/{choice}/locations"] = sorted(
            f"{r['programme_document']}|{r['indicator']}|{r['report']}|{r['location']}|"
            f"{_plain(r['value_in_period'])}|{r['status']}|{r['rows']}"
            for r in found["rows"]
        )
    # the pop-up, opened by PRP's progress report id where the rows carry one
    if world.shape == "prp_ids":
        for letter, number in (("A", "QPR1"), ("A", "QPR2"), ("B", "QPR1")):
            detail = services.report_detail(f"{world.pds[letter].number}/{number}", today=TODAY)
            out[f"pop_up/{world.pds[letter].number}/{number}"] = {
                "status": detail["status"],
                "indicators": sorted(
                    f"{i['indicator']}|{i['pd_output']}|{_plain(i['total'])}" for i in detail["indicators"]
                ),
            }
    # the home page's "partner progress reports this year"
    from neurodb.web import services as web_services

    out["home/progress_reports"] = web_services._etools_counts(2026)["progress_reports"]
    return _plain(out)


# --------------------------------------------------------------------- the figures meant to change
# why a figure changes: reports were read as one that are not one report, or values were dropped
HOME = (
    "The home page's 'partner progress reports this year' counted the reports PRP gave an id: none when PRP "
    "sends no progress report id. It counts each programme document's reports by type, number and period."
)
STATUS = (
    "One report, two status words (a partial re-sync: PD A's QPR2 says Accepted on the children's and the "
    "wellbeing rows, changed last, and Submitted on the caregivers'). With no progress report id each "
    "indicator read its own rows' word, and Progress reports listed the report twice. It is one report "
    "with the newest word, Accepted, on every page."
)
CROSS_PD = (
    "PD B's QPR1 (overdue, changed last) has PD A's number and dates. With no PRP id, Partner monitoring "
    "read the status of 'QPR1, 1 Jan - 31 Mar' across every programme document, so PD A's accepted QPR1 "
    "took PD B's word and stopped counting: 300 children, 40 caregivers and a wellbeing period dropped."
)
OUTPUT = (
    "PD B has two indicators titled '# of youth trained', under outputs 2.1 and 2.2: their rows were left "
    "out of Reached. Each row's PD output now places it (12 and 30); the two household indicators share "
    "their output too and stay left out."
)
OUTPUT_TILES = (
    "With no PRP indicator report id, the tiles counted PD B's two youth indicators of QPR2 as one "
    "indicator report (one title); their PD outputs tell them apart."
)
POOLED = (
    "Report/Location grouped rows by number and period only, across programme documents: PD A's QPR1 at "
    "Akkar showed the largest of PD A's 200, PD B's 60 and the unheld PD's 500, and PD B's two youth "
    "indicators were one line. Each programme document's report and each indicator has its own line."
)
both = "{} {}".format  # two reasons together
CROSS_PD_STATUS = both(CROSS_PD, STATUS)
STATUS_TILES = both(STATUS, OUTPUT_TILES)
STATUS_POOLED = both(STATUS, POOLED)

# (shapes, figure, change, why). A change is {field: (frozen, now)} for a figure of several fields,
# (removed, added) for a list, (frozen, now) for a number. In the ``prp_ids`` shape nothing changes.
CHANGED: list[tuple[tuple[str, ...], str, Any, str]] = [
    (("ir_only", "production"), "home/progress_reports", (0, 12), HOME),
    (
        ("ir_only",),
        "monitoring/QPR/2026/LEB/PCA-A/PD1/1102",
        {
            "awaiting": (40.0, 10.0),
            "awaiting_reports": (2, 1),
            "reached_accepted": (40.0, 70.0),
            "statuses": (
                ["QPR1:accepted", "QPR2:submitted", "QPR3:submitted"],
                ["QPR1:accepted", "QPR2:accepted", "QPR3:submitted"],
            ),
        },
        STATUS,
    ),
    (
        ("ir_only",),
        "monitoring/QPR/all/LEB/PCA-A/PD1/1102",
        {
            "awaiting": (40.0, 10.0),
            "awaiting_reports": (2, 1),
            "reached_accepted": (40.0, 70.0),
            "statuses": (
                ["QPR1:accepted", "QPR2:submitted", "QPR3:submitted"],
                ["QPR1:accepted", "QPR2:accepted", "QPR3:submitted"],
            ),
        },
        STATUS,
    ),
    (
        ("ir_only",),
        "progress/every/by_month",
        {
            "Accepted": ([1, 1, 1, 4, 0, 1, 7, 0, 0], [1, 1, 1, 4, 0, 1, 8, 0, 0]),
            "Submitted": ([0, 0, 0, 0, 0, 0, 1, 2, 0], [0, 0, 0, 0, 0, 0, 0, 2, 0]),
        },
        STATUS,
    ),
    (
        ("ir_only",),
        "progress/every/counts",
        {"accepted": (15, 16), "progress_reports": (14, 13), "submitted": (3, 2)},
        STATUS,
    ),
    (
        ("ir_only",),
        "progress/every/locations",
        (
            ["LEB/PCA-A/PD1|# of caregivers reached with parenting sessions|QPR2|Akkar|30.0|submitted|1"],
            ["LEB/PCA-A/PD1|# of caregivers reached with parenting sessions|QPR2|Akkar|30.0|accepted|1"],
        ),
        STATUS,
    ),
    (
        ("ir_only",),
        "progress/every/reports",
        (
            ["LEB/PCA-A/PD1|QPR|QPR2|2026-06-30|accepted|2", "LEB/PCA-A/PD1|QPR|QPR2|2026-06-30|submitted|1"],
            ["LEB/PCA-A/PD1|QPR|QPR2|2026-06-30|accepted|3"],
        ),
        STATUS,
    ),
    (("ir_only", "production"), "progress/every/summary", {"submitted": (2, 1)}, STATUS),
    (
        ("ir_only",),
        "progress/running/by_month",
        {
            "Accepted": ([1, 1, 3, 0, 1, 7, 0, 0], [1, 1, 3, 0, 1, 8, 0, 0]),
            "Submitted": ([0, 0, 0, 0, 0, 1, 2, 0], [0, 0, 0, 0, 0, 0, 2, 0]),
        },
        STATUS,
    ),
    (
        ("ir_only",),
        "progress/running/counts",
        {"accepted": (13, 14), "progress_reports": (12, 11), "submitted": (3, 2)},
        STATUS,
    ),
    (
        ("ir_only",),
        "progress/running/locations",
        (
            ["LEB/PCA-A/PD1|# of caregivers reached with parenting sessions|QPR2|Akkar|30.0|submitted|1"],
            ["LEB/PCA-A/PD1|# of caregivers reached with parenting sessions|QPR2|Akkar|30.0|accepted|1"],
        ),
        STATUS,
    ),
    (
        ("ir_only",),
        "progress/running/reports",
        (
            ["LEB/PCA-A/PD1|QPR|QPR2|2026-06-30|accepted|2", "LEB/PCA-A/PD1|QPR|QPR2|2026-06-30|submitted|1"],
            ["LEB/PCA-A/PD1|QPR|QPR2|2026-06-30|accepted|3"],
        ),
        STATUS,
    ),
    (("ir_only", "production"), "progress/running/summary", {"submitted": (2, 1)}, STATUS),
    (
        ("production",),
        "monitoring/QPR/2026/LEB/PCA-A/PD1/1101",
        {
            "by_location": ({"Akkar": 230.0, "Bekaa": 70.0}, {"Akkar": 430.0, "Bekaa": 170.0}),
            "months": ({"6": 200.0, "9": 100.0}, {"3": 300.0, "6": 200.0, "9": 100.0}),
            "reached": (300.0, 600.0),
            "reached_accepted": (200.0, 500.0),
            "reached_in_year": (300.0, 600.0),
            "reports": (2, 3),
            "statuses": (
                ["QPR1:due", "QPR2:accepted", "QPR3:submitted", "QPR4:not_yet_due"],
                ["QPR1:accepted", "QPR2:accepted", "QPR3:submitted", "QPR4:not_yet_due"],
            ),
            "years": ({"2026": 300.0}, {"2026": 600.0}),
        },
        CROSS_PD,
    ),
    (
        ("production",),
        "monitoring/QPR/2026/LEB/PCA-A/PD1/1104",
        {
            "by_location": ({"Akkar": 80.0, "Bekaa": None}, {"Akkar": 80.0, "Bekaa": 40.0}),
            "months": ({"6": 80.0}, {"3": 50.0, "6": 80.0}),
            "reports": (1, 2),
            "statuses": (["QPR1:due", "QPR2:accepted"], ["QPR1:accepted", "QPR2:accepted"]),
        },
        CROSS_PD,
    ),
    (
        ("production",),
        "monitoring/QPR/all/LEB/PCA-A/PD1/1101",
        {
            "by_location": ({"Akkar": 230.0, "Bekaa": 70.0}, {"Akkar": 430.0, "Bekaa": 170.0}),
            "reached": (300.0, 600.0),
            "reached_accepted": (200.0, 500.0),
            "reports": (2, 3),
            "statuses": (
                ["QPR1:due", "QPR2:accepted", "QPR3:submitted", "QPR4:not_yet_due"],
                ["QPR1:accepted", "QPR2:accepted", "QPR3:submitted", "QPR4:not_yet_due"],
            ),
            "years": ({"2026": 300.0}, {"2026": 600.0}),
        },
        CROSS_PD,
    ),
    (
        ("production",),
        "monitoring/QPR/all/LEB/PCA-A/PD1/1104",
        {
            "by_location": ({"Akkar": 80.0, "Bekaa": None}, {"Akkar": 80.0, "Bekaa": 40.0}),
            "reports": (1, 2),
            "statuses": (["QPR1:due", "QPR2:accepted"], ["QPR1:accepted", "QPR2:accepted"]),
        },
        CROSS_PD,
    ),
    (
        ("production",),
        "monitoring/QPR/2026/LEB/PCA-A/PD1/1102",
        {
            "by_location": ({"Akkar": 40.0}, {"Akkar": 80.0}),
            "months": ({"6": 30.0, "9": 10.0}, {"3": 40.0, "6": 30.0, "9": 10.0}),
            "reached": (40.0, 80.0),
            "reached_accepted": (30.0, 70.0),
            "reached_in_year": (40.0, 80.0),
            "reports": (2, 3),
            "statuses": (
                ["QPR1:due", "QPR2:accepted", "QPR3:submitted"],
                ["QPR1:accepted", "QPR2:accepted", "QPR3:submitted"],
            ),
            "years": ({"2026": 40.0}, {"2026": 80.0}),
        },
        CROSS_PD_STATUS,
    ),
    (
        ("production",),
        "monitoring/QPR/all/LEB/PCA-A/PD1/1102",
        {
            "by_location": ({"Akkar": 40.0}, {"Akkar": 80.0}),
            "reached": (40.0, 80.0),
            "reached_accepted": (30.0, 70.0),
            "reports": (2, 3),
            "statuses": (
                ["QPR1:due", "QPR2:accepted", "QPR3:submitted"],
                ["QPR1:accepted", "QPR2:accepted", "QPR3:submitted"],
            ),
            "years": ({"2026": 40.0}, {"2026": 80.0}),
        },
        CROSS_PD_STATUS,
    ),
    (
        ("ir_only", "production"),
        "monitoring/QPR/2026/LEB/PCA-B/PD2/2201",
        {
            "as_of": (None, "2026-06-30"),
            "by_location": ({}, {"Akkar": 12.0}),
            "latest_status": ("", "Accepted"),
            "months": ({}, {"6": 12.0}),
            "reached": (None, 12.0),
            "reached_accepted": (None, 12.0),
            "reached_in_year": (None, 12.0),
            "reports": (0, 1),
            "statuses": ([], ["QPR2:accepted"]),
            "value_status": ("not_reported", "reported"),
            "years": ({}, {"2026": 12.0}),
        },
        OUTPUT,
    ),
    (
        ("ir_only", "production"),
        "monitoring/QPR/2026/LEB/PCA-B/PD2/2202",
        {
            "as_of": (None, "2026-06-30"),
            "by_location": ({}, {"Akkar": 30.0}),
            "latest_status": ("", "Accepted"),
            "months": ({}, {"6": 30.0}),
            "reached": (None, 30.0),
            "reached_accepted": (None, 30.0),
            "reached_in_year": (None, 30.0),
            "reports": (0, 1),
            "statuses": ([], ["QPR2:accepted"]),
            "value_status": ("not_reported", "reported"),
            "years": ({}, {"2026": 30.0}),
        },
        OUTPUT,
    ),
    (
        ("ir_only", "production"),
        "monitoring/QPR/all/LEB/PCA-B/PD2/2201",
        {
            "as_of": (None, "2026-06-30"),
            "by_location": ({}, {"Akkar": 12.0}),
            "latest_status": ("", "Accepted"),
            "reached": (None, 12.0),
            "reached_accepted": (None, 12.0),
            "reports": (0, 1),
            "statuses": ([], ["QPR2:accepted"]),
            "value_status": ("not_reported", "reported"),
            "years": ({}, {"2026": 12.0}),
        },
        OUTPUT,
    ),
    (
        ("ir_only", "production"),
        "monitoring/QPR/all/LEB/PCA-B/PD2/2202",
        {
            "as_of": (None, "2026-06-30"),
            "by_location": ({}, {"Akkar": 30.0}),
            "latest_status": ("", "Accepted"),
            "reached": (None, 30.0),
            "reached_accepted": (None, 30.0),
            "reports": (0, 1),
            "statuses": ([], ["QPR2:accepted"]),
            "value_status": ("not_reported", "reported"),
            "years": ({}, {"2026": 30.0}),
        },
        OUTPUT,
    ),
    (
        ("ir_only", "production"),
        "quality/QPR/2026",
        {"no_reached_count": (5, 3), "with_reached": (5, 7), "without_reached": (5, 3)},
        OUTPUT,
    ),
    (
        ("ir_only", "production"),
        "quality/QPR/all",
        {"no_reached_count": (4, 2), "with_reached": (6, 8), "without_reached": (4, 2)},
        OUTPUT,
    ),
    (("ir_only", "production"), "summary/QPR/2026", {"with_reached": (6, 8)}, OUTPUT),
    (("ir_only", "production"), "summary/QPR/all", {"with_reached": (6, 8)}, OUTPUT),
    (
        ("production",),
        "progress/every/by_month",
        {
            "Accepted": ([1, 1, 1, 4, 0, 1, 5, 0, 0], [1, 1, 1, 4, 0, 1, 7, 0, 0]),
            "Submitted": ([0, 0, 0, 0, 0, 0, 1, 2, 0], [0, 0, 0, 0, 0, 0, 0, 2, 0]),
        },
        STATUS_TILES,
    ),
    (
        ("production",),
        "progress/every/counts",
        {
            "accepted": (13, 15),
            "indicator_reports": (20, 21),
            "progress_reports": (14, 13),
            "submitted": (3, 2),
        },
        STATUS_TILES,
    ),
    (
        ("production",),
        "progress/every/reports",
        (
            [
                "LEB/PCA-A/PD1|QPR|QPR2|2026-06-30|accepted|2",
                "LEB/PCA-A/PD1|QPR|QPR2|2026-06-30|submitted|1",
                "LEB/PCA-B/PD2|QPR|QPR2|2026-06-30|accepted|3",
            ],
            ["LEB/PCA-A/PD1|QPR|QPR2|2026-06-30|accepted|3", "LEB/PCA-B/PD2|QPR|QPR2|2026-06-30|accepted|4"],
        ),
        STATUS_TILES,
    ),
    (
        ("production",),
        "progress/running/by_month",
        {
            "Accepted": ([1, 1, 3, 0, 1, 5, 0, 0], [1, 1, 3, 0, 1, 7, 0, 0]),
            "Submitted": ([0, 0, 0, 0, 0, 1, 2, 0], [0, 0, 0, 0, 0, 0, 2, 0]),
        },
        STATUS_TILES,
    ),
    (
        ("production",),
        "progress/running/counts",
        {
            "accepted": (11, 13),
            "indicator_reports": (18, 19),
            "progress_reports": (12, 11),
            "submitted": (3, 2),
        },
        STATUS_TILES,
    ),
    (
        ("production",),
        "progress/running/reports",
        (
            [
                "LEB/PCA-A/PD1|QPR|QPR2|2026-06-30|accepted|2",
                "LEB/PCA-A/PD1|QPR|QPR2|2026-06-30|submitted|1",
                "LEB/PCA-B/PD2|QPR|QPR2|2026-06-30|accepted|3",
            ],
            ["LEB/PCA-A/PD1|QPR|QPR2|2026-06-30|accepted|3", "LEB/PCA-B/PD2|QPR|QPR2|2026-06-30|accepted|4"],
        ),
        STATUS_TILES,
    ),
    (
        ("production",),
        "progress/every/locations",
        (
            [
                "LEB/PCA-A/PD1|# of caregivers reached with parenting sessions|QPR2|Akkar|30.0|submitted|1",
                "LEB/PCA-A/PD1|# of children reached with PSS|QPR1|Akkar|500.0|accepted|3",
                "LEB/PCA-A/PD1|# of children reached with PSS|QPR2|Akkar|150.0|accepted|2",
                "LEB/PCA-B/PD2|# of youth trained|QPR2|Akkar|30.0|accepted|2",
            ],
            [
                "LEB/PCA-A/PD1|# of caregivers reached with parenting sessions|QPR2|Akkar|30.0|accepted|1",
                "LEB/PCA-A/PD1|# of children reached with PSS|QPR1|Akkar|200.0|accepted|1",
                "LEB/PCA-A/PD1|# of children reached with PSS|QPR2|Akkar|150.0|accepted|1",
                "LEB/PCA-B/PD2|# of children reached with PSS|QPR1|Akkar|60.0|overdue|1",
                "LEB/PCA-B/PD2|# of children reached with PSS|QPR2|Akkar|70.0|accepted|1",
                "LEB/PCA-B/PD2|# of youth trained|QPR2|Akkar|12.0|accepted|1",
                "LEB/PCA-B/PD2|# of youth trained|QPR2|Akkar|30.0|accepted|1",
                "LEB/PCA-X/PD9|# of children reached with PSS|QPR1|Akkar|500.0|accepted|1",
            ],
        ),
        STATUS_POOLED,
    ),
    (
        ("production",),
        "progress/running/locations",
        (
            [
                "LEB/PCA-A/PD1|# of caregivers reached with parenting sessions|QPR2|Akkar|30.0|submitted|1",
                "LEB/PCA-A/PD1|# of children reached with PSS|QPR1|Akkar|200.0|accepted|2",
                "LEB/PCA-A/PD1|# of children reached with PSS|QPR2|Akkar|150.0|accepted|2",
                "LEB/PCA-B/PD2|# of youth trained|QPR2|Akkar|30.0|accepted|2",
            ],
            [
                "LEB/PCA-A/PD1|# of caregivers reached with parenting sessions|QPR2|Akkar|30.0|accepted|1",
                "LEB/PCA-A/PD1|# of children reached with PSS|QPR1|Akkar|200.0|accepted|1",
                "LEB/PCA-A/PD1|# of children reached with PSS|QPR2|Akkar|150.0|accepted|1",
                "LEB/PCA-B/PD2|# of children reached with PSS|QPR1|Akkar|60.0|overdue|1",
                "LEB/PCA-B/PD2|# of children reached with PSS|QPR2|Akkar|70.0|accepted|1",
                "LEB/PCA-B/PD2|# of youth trained|QPR2|Akkar|12.0|accepted|1",
                "LEB/PCA-B/PD2|# of youth trained|QPR2|Akkar|30.0|accepted|1",
            ],
        ),
        STATUS_POOLED,
    ),
]


def _expected(frozen: Any, change: Any) -> Any:
    """The figure ``change`` makes of ``frozen``; AssertionError when the frozen figure is not the one the
    change starts from."""
    if isinstance(change, dict):
        for name, (old, _new) in change.items():
            assert frozen.get(name) == old, name
        return {**frozen, **{name: new for name, (_old, new) in change.items()}}
    old, new = change
    if isinstance(frozen, list):
        assert set(old) <= set(frozen), old
        return sorted([x for x in frozen if x not in old] + list(new))
    assert frozen == old
    return new


@pytest.mark.parametrize("shape", SHAPES)
def test_figures_stay_identical_except_where_reports_were_merged(shape):
    now = figures(World(shape).build())
    if os.environ.get("REPORT_IDENTITY_WRITE_GOLDEN"):
        frozen = json.loads(GOLDEN.read_text()) if GOLDEN.exists() else {}
        frozen[shape] = now
        GOLDEN.parent.mkdir(exist_ok=True)
        GOLDEN.write_text(json.dumps(frozen, indent=1, sort_keys=True, ensure_ascii=False) + "\n")
        pytest.skip("figures frozen")
    before = json.loads(GOLDEN.read_text())[shape]
    changes = {key: change for shapes, key, change, _why in CHANGED if shape in shapes}
    differ = {}  # every figure that is not as expected: (frozen, expected, now)
    for key in sorted({*before, *now}):
        expected = _expected(before.get(key), changes[key]) if key in changes else before.get(key)
        if now.get(key) != expected:
            differ[key] = (before.get(key), expected, now.get(key))
    assert not differ, json.dumps(differ, indent=1, ensure_ascii=False)


def test_every_change_says_why():
    assert all(why.strip() for *_rest, why in CHANGED)
    assert not any("prp_ids" in shapes for shapes, *_rest in CHANGED)  # consistent PRP ids: nothing changes


def test_a_stored_calibration_leaves_the_identical_pair_out():
    """PD B's two household indicators share their title, their output and every key a tie-breaker reads (the
    same high frequency, target 1000, both plan Akkar and Bekaa): with every tie-breaker in use and the PD
    indicators read whole they still stay out, and no figure moves."""
    world = World("production").build()
    before = figures(world)
    SyncRun.objects.create(
        job=SyncRun.Job.ETOOLS_DATAMART,
        target="pd_indicators",
        status=SyncRun.Status.SUCCEEDED,
        finished_at=timezone.now(),
        details={
            "datamart_count": 20,
            "identity": {"version": identity.CALIBRATION_VERSION, "enabled": list(identity.BREAKERS)},
        },
    )
    assert identity.calibration().enabled == frozenset(identity.BREAKERS) and identity.indicators_read_whole()
    assert figures(world) == before
    rows = list(reached.report_rows([world.pds["B"].id], ["QPR"]))
    placed = identity.Idents([world.pds["B"].id]).place(rows)
    assert {p.way for r, p in zip(rows, placed, strict=True) if r["indicator"] == HOUSEHOLDS} == {
        "shared_output"
    }
