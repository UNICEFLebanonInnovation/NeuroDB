"""Page-facing services for database dashboards, pivots, maps and Neuro Reports.

Views call these and render; nothing here touches the request. All return plain dicts/lists so
the same function feeds an HTML page, an HTMX partial and the internal JSON API.
"""

from __future__ import annotations

import calendar
import datetime
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any

from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from neurodb.facts import queries
from neurodb.facts.queries import FactFilter
from neurodb.indicators.models import Database, NeuroReport, NeuroReportComment, ReportingYear
from neurodb.indicators.services.tracking import (
    LABELS,
    TOLERANCE,
    percentage_of_year_elapsed,
    tracking,
    year_of,
)

QUARTERS = {"Q1": 3, "Q2": 6, "Q3": 9, "Q4": 12}

# How a master indicator combines what is reported: a short name for the tables and a sentence for
# their tooltip (the codes are v2's ``aggregation_method``; blank means SUM).
METHODS = {
    "SUM": {"label": _("Sum"), "help": _("Adds up everything reported during the year.")},
    "AVERAGE": {"label": _("Average"), "help": _("The average of the monthly totals.")},
    "MAXIMUM": {"label": _("Maximum"), "help": _("The highest monthly total.")},
    "MINIMUM": {"label": _("Minimum"), "help": _("The lowest monthly total.")},
    "COUNT": {"label": _("Count"), "help": _("The number of records reported.")},
    "SUM_OVER_SUM": {
        "label": _("Ratio"),
        "help": _("A percentage: the numerator sub-indicator divided by the denominator."),
    },
}

# What a sub-indicator link (``effect``) does to its master indicator's value.
EFFECTS = {
    "TOTAL": _("In the total"),
    "NUMERATOR": _("Numerator"),
    "DENOMINATOR": _("Denominator"),
}


def fact_filter(database: Database, **kwargs: Any) -> FactFilter:
    """Funded-by filter defaults to the database flag (v2 commented the filter out everywhere)."""
    return FactFilter(
        database_id=database.id, funded_by_unicef_only=bool(database.is_funded_by_unicef), **kwargs
    )


@dataclass
class IndicatorRow:
    id: int
    label: str
    awp_code: str
    aggregation_method: str
    reporting_level: str | None
    unit: str | None
    target: float | None
    ram_result: float | None
    value: float | None
    reports: int
    tracking: str
    tracking_label: str
    achieved: float | None
    numerator: float | None = None
    denominator: float | None = None


@dataclass
class Dashboard:
    database: Database
    year: int
    indicators: list[IndicatorRow]
    status_counts: dict[str, int]
    totals: dict[str, Any]
    monthly: list[dict[str, Any]]
    last_import: datetime.datetime | None
    sibling_years: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "database": {
                "id": self.database.id,
                "name": self.database.label or self.database.name,
                "ai_id": self.database.ai_id,
            },
            "year": self.year,
            "indicators": [asdict(i) for i in self.indicators],
            "status_counts": self.status_counts,
            "totals": self.totals,
            "monthly": self.monthly,
            "last_import": self.last_import.isoformat() if self.last_import else None,
            "sibling_years": self.sibling_years,
        }


def _rows_to_indicators(
    rows: list[dict[str, Any]], year: int, today: datetime.date | None = None
) -> list[IndicatorRow]:
    """``today`` is the date the status is measured at: the end of a report's period, else today."""
    out = []
    for r in rows:
        value = float(r["value"]) if r["value"] is not None else None
        target = float(r["target"]) if r["target"] else None
        method = r["aggregation_method"] or "SUM"
        # A ratio is a level, not a running total: compare it with the whole target.
        t = tracking(value, target, year, today, prorate=method != "SUM_OVER_SUM")
        out.append(
            IndicatorRow(
                id=r["id"],
                label=r["label"],
                awp_code=r["awp_code"] or "",
                aggregation_method=method,
                reporting_level=r["reporting_level"],
                unit=r.get("unit"),
                target=target,
                ram_result=float(r["ram_result"]) if r.get("ram_result") else None,
                value=value,
                reports=int(r["reports"] or 0),
                tracking=t.status,
                tracking_label=t.label,
                achieved=round(t.achieved, 1) if t.achieved is not None else None,
                numerator=float(r["numerator"]) if r.get("numerator") is not None else None,
                denominator=float(r["denominator"]) if r.get("denominator") is not None else None,
            )
        )
    return out


def database_dashboard(database: Database) -> Dashboard:
    year = year_of(database.reporting_year) if database.reporting_year else timezone.now().year
    f = fact_filter(database)
    indicators = _rows_to_indicators(queries.master_indicator_values(f), year)
    counts = Counter(i.tracking for i in indicators)
    summary = queries.database_summaries([database.id]).get(database.id, {})
    siblings = (
        [
            {
                "id": d.id,
                "year": d.reporting_year.name if d.reporting_year else "",
                "label": d.label or d.name,
            }
            for d in Database.objects.filter(db_id=database.db_id)
            .exclude(id=database.id)
            .select_related("reporting_year")
            .order_by("-reporting_year__name")
        ]
        if database.db_id
        else []
    )
    return Dashboard(
        database=database,
        year=year,
        indicators=indicators,
        status_counts={k: counts.get(k, 0) for k in LABELS},
        totals={
            "indicators": len(indicators),
            "with_target": sum(1 for i in indicators if i.target),
            "reports": int(summary.get("reports") or 0),
            "partners": int(summary.get("partners") or 0),
            "sites": int(summary.get("sites") or 0),
        },
        monthly=queries.monthly_totals(f),
        last_import=database.last_monthly_update_date,
        sibling_years=siblings,
    )


def master_detail(database: Database, master_id: int) -> list[dict[str, Any]]:
    rows = queries.sub_indicator_values(master_id, fact_filter(database))
    for r in rows:
        r["value"] = float(r["value"]) if r["value"] is not None else None
        r["effect_label"] = EFFECTS.get(r["effect"] or "", "")
    return rows


def _month_point(method: str, parts: dict[str, dict[str, dict[str, float]]], month: str) -> dict[str, Any]:
    """One indicator's value and records in one month, by its method: a ratio divides the month's
    numerator by its denominator, a count is the month's records, the others the month's total."""
    if method == "SUM_OVER_SUM":
        num = parts.get("NUMERATOR", {}).get(month)
        den = parts.get("DENOMINATOR", {}).get(month)
        value = num["value"] * 100 / den["value"] if num and den and den["value"] > 0 else None
        return {"value": value, "reports": den["reports"] if den else 0}
    total = parts.get("TOTAL", {}).get(month)
    if not total:
        return {"value": None, "reports": 0}
    return {
        "value": float(total["reports"]) if method == "COUNT" else total["value"],
        "reports": total["reports"],
    }


def monthly_by_indicator(f: FactFilter, indicators: list[IndicatorRow]) -> dict[str, Any]:
    """The monthly chart of a dashboard, one master indicator at a time (adding indicators with
    different units gave a meaningless total): ``{months, indicators: [{id, label, values,
    reports}], default}``. The default is the first SUM indicator with data."""
    parts = queries.master_monthly_parts(f)
    months = sorted({m for p in parts.values() for effect in p.values() for m in effect})
    series = []
    for i in indicators:
        points = [_month_point(i.aggregation_method, parts.get(i.id, {}), m) for m in months]
        series.append(
            {
                "id": i.id,
                "label": f"{i.awp_code} · {i.label}" if i.awp_code else i.label,
                "unit": i.unit or ("%" if i.aggregation_method == "SUM_OVER_SUM" else ""),
                "values": [p["value"] for p in points],
                "reports": [p["reports"] for p in points],
            }
        )
    with_data = [s for s, i in zip(series, indicators, strict=True) if any(s["reports"]) or i.reports]
    first_sum = next(
        (
            s["id"]
            for s, i in zip(series, indicators, strict=True)
            if i.aggregation_method == "SUM" and i.reports
        ),
        with_data[0]["id"] if with_data else None,
    )
    return {
        "months": [queries.MONTH_LABELS.get(m, m) for m in months],
        "indicators": series,
        "default": first_sum,
    }


def indicator_summary(database: Database, master_id: int) -> dict[str, Any] | None:
    """Achieved value, % of target, status and months of one master indicator (indicator detail)."""
    f = fact_filter(database)
    year = year_of(database.reporting_year) if database.reporting_year else timezone.now().year
    rows = [r for r in queries.master_indicator_values(f) if r["id"] == master_id]
    if not rows:
        return None
    row = _rows_to_indicators(rows, year)[0]
    parts = queries.master_monthly_parts(f).get(master_id, {})
    months = sorted({m for effect in parts.values() for m in effect})
    return {
        "row": row,
        "unit": row.unit or ("%" if row.aggregation_method == "SUM_OVER_SUM" else ""),
        "year": year,
        "months": [{"month": m, **_month_point(row.aggregation_method, parts, m)} for m in months],
    }


def analytical_rows(database: Database, emergency: str | None = None) -> list[dict[str, Any]]:
    rows = queries.analytical_rows(fact_filter(database), emergency=emergency)
    for r in rows:
        r["indicator_value"] = float(r["indicator_value"]) if r["indicator_value"] is not None else 0.0
        r["target"] = float(r["target"]) if r["target"] else None
    return rows


def analytical_filters(database: Database) -> dict[str, list[str]]:
    return {
        dim: queries.distinct_values(database.id, dim)
        for dim in ("partner", "pd", "governorate", "district", "month")
    }


def map_data(database: Database, level: str = "governorate", **filters: str) -> dict[str, Any]:
    from neurodb.geo.services import geojson_for_level

    f = fact_filter(database)
    areas = queries.interventions_by_area(f, level, **filters) if level != "site" else []
    for a in areas:
        a["value"] = float(a["value"]) if a["value"] is not None else 0.0
    site_rows = queries.sites(f, **filters) if level == "site" else []
    for s in site_rows:
        s["value"] = float(s["value"]) if s["value"] is not None else 0.0
    return {
        "level": level,
        "areas": areas,
        "sites": site_rows,
        "geojson": geojson_for_level(level, {a["code"]: a for a in areas}) if level != "site" else None,
        "totals": {
            "interventions": sum(a["interventions"] for a in areas)
            if areas
            else sum(s["interventions"] for s in site_rows),
            "areas": len(areas),
            "sites": len(site_rows),
        },
    }


def snapshot(database: Database) -> dict[str, Any]:
    """Print-friendly bundle: dashboard + top partners + area breakdowns (v2 snapshot page)."""
    from neurodb.partnerships import linking

    dash = database_dashboard(database)
    f = fact_filter(database)
    top_partners = _top_partners(f, 10)
    links = linking.links_for([p["partner"] for p in top_partners])
    for p in top_partners:
        p["etools_partner"] = links.get(p["partner"])
    by_district = queries.interventions_by_area(f, "district")[:15]
    # Two districts may share a name (in two governorates): name the governorate of those.
    names = Counter(a["name"] for a in by_district)
    places = Counter((a["name"], a["governorate"]) for a in by_district)
    for a in by_district:
        # The governorate tells them apart; the district code when the governorate does not.
        extra = [a["governorate"]] if a["governorate"] else []
        if places[(a["name"], a["governorate"])] > 1:
            extra.append(a["code"])
        a["where"] = " · ".join(extra) if names[a["name"]] > 1 else ""
    return {
        "dashboard": dash,
        "by_governorate": queries.interventions_by_area(f, "governorate"),
        "by_district": by_district,
        "top_partners": top_partners,
    }


_TOP_PARTNERS_SQL = """
SELECT r.partner_label AS partner, COUNT(*) AS interventions, SUM(r.indicator_value) AS value,
       COUNT(DISTINCT r.location_name) AS sites
FROM pivoting_activityreportnew r
WHERE r.dbase_id = %(database_id)s AND COALESCE(r.partner_label, '') <> ''
  AND (NOT %(unicef_only)s OR r.funded_by = 'UNICEF')
GROUP BY r.partner_label ORDER BY interventions DESC LIMIT %(limit)s
"""


def _top_partners(f: FactFilter, limit: int) -> list[dict[str, Any]]:
    rows = queries._rows(  # noqa: SLF001 - module-private helper shared with this package
        _TOP_PARTNERS_SQL,
        {"database_id": f.database_id, "limit": limit, "unicef_only": bool(f.funded_by_unicef_only)},
    )
    for r in rows:
        r["value"] = float(r["value"]) if r["value"] is not None else 0.0
    return rows


# ------------------------------------------------------------------------- Neuro Reports / HPM


def last_ended_month(year: int, today: datetime.date | None = None) -> int:
    """The last month of ``year`` that has ended: December for a past year, the month before this one
    for the current year (January while January runs, so a report always has a period)."""
    today = today or datetime.date.today()
    if year < today.year:
        return 12
    if year > today.year:
        return 1
    return max(today.month - 1, 1)


def resolve_period(
    month: int | None, quarter: str | None, today: datetime.date | None = None, last_month: int = 12
) -> tuple[int, str | None]:
    """Default month is the last ended one: the previous month (v2's rule) for the current year,
    December for a past year. A quarter maps to its last month.

    Periods that have not ended (after ``last_month``) are not offered: a later month or quarter
    falls back to ``last_month``, so year-to-date values are never labelled as a future total.
    """
    today = today or datetime.date.today()
    if quarter in QUARTERS:
        if QUARTERS[quarter] <= last_month:
            return QUARTERS[quarter], quarter
        return last_month, None
    if month and 1 <= month <= 12:
        return min(month, last_month), None
    return last_month, None


def neuroreport(
    report: NeuroReport,
    month: int | None = None,
    quarter: str | None = None,
    today: datetime.date | None = None,
) -> dict[str, Any]:
    """Values of a Neuro Report to the end of a period with the HPM cut-off and previous-period deltas.

    The cut-off ports v2's rule: records last edited after the 17th of the month following the
    period are excluded (a June report viewed in September ignores edits made after 17 July; edits
    made on the 17th, Beirut time, still count). Statuses are measured at the end of the period.
    """
    today = today or datetime.date.today()
    year = year_of(report.ryear) if report.ryear else timezone.now().year
    last_month = last_ended_month(year, today)
    month, quarter = resolve_period(month, quarter, today, last_month)
    period_end = datetime.date(year, month, calendar.monthrange(year, month)[1])
    cutoff_month = month + 1
    cutoff = datetime.date(year + (1 if cutoff_month > 12 else 0), (cutoff_month - 1) % 12 + 1, 17)
    # Records edited before the end of the cut-off day (in the site's time zone) count.
    edited_before = timezone.make_aware(
        datetime.datetime.combine(cutoff + datetime.timedelta(days=1), datetime.time.min)
    )
    databases = Database.objects.filter(masterindicator__neuroreportmasterindicator__report=report).distinct()
    prev_month = month - (3 if quarter else 1)
    sections = []
    for db in databases.select_related("section").order_by("section__name", "name"):
        f_now = fact_filter(db, month_to=month, edited_before=edited_before)
        rows_now = _rows_to_indicators(
            queries.master_indicator_values(f_now, report_id=report.id), year, min(today, period_end)
        )
        prev = {}
        if prev_month >= 1:
            f_prev = fact_filter(db, month_to=prev_month, edited_before=edited_before)
            prev = {r["id"]: r for r in queries.master_indicator_values(f_prev, report_id=report.id)}
        items = []
        for i in rows_now:
            p = prev.get(i.id, {}).get("value")
            p = float(p) if p is not None else 0.0
            # Counts of people or things show whole numbers; a ratio keeps one decimal.
            digits = 1 if i.aggregation_method == "SUM_OVER_SUM" else 0
            items.append({**asdict(i), "previous": p, "delta": (i.value or 0.0) - p, "digits": digits})
        if items:
            sections.append({"database": db, "items": items})
    comments = list(
        NeuroReportComment.objects.filter(report=report, is_active=True, related_month__lte=f"{month:02d}")
        .select_related("master__master")
        .order_by("related_month", "entry_date")
    )
    # The HPM table shows the comments of a section in one cell beside all its rows, each with the
    # code of its indicator, so a comment is not read as a note on the section's first indicator.
    for s in sections:
        ids = {i["id"] for i in s["items"]}
        s["comments"] = [c for c in comments if c.master and c.master.master_id in ids]
    return {
        "report": report,
        "year": year,
        "month": month,
        "month_label": datetime.date(year, month, 1).strftime("%B"),
        # What "previous" means: the end of the month (or quarter) before; nothing before January.
        "previous_label": datetime.date(year, prev_month, 1).strftime("%B") if prev_month >= 1 else "",
        "quarter": quarter,
        "cutoff": cutoff,
        # The statuses are measured at the end of the period (or today, if earlier), not today.
        "status_ref": {
            "date": min(today, period_end),
            "elapsed": percentage_of_year_elapsed(year, min(today, period_end)),
            "tolerance": TOLERANCE,
        },
        "last_month": last_month,
        "quarters": [q for q, m in QUARTERS.items() if m <= last_month],
        "sections": sections,
        "comments": comments,
        "totals": {"indicators": sum(len(s["items"]) for s in sections), "databases": len(sections)},
    }


# ------------------------------------------------------------------------- Overview (new in v3)


def overview(year: ReportingYear | None) -> dict[str, Any]:
    """Cross-database KPIs for the landing page: status distribution, freshness, top movers."""
    from neurodb.core.models import SyncRun

    databases = (
        list(
            Database.objects.filter(reporting_year=year, display=True)
            .select_related("section")
            .order_by("section__name", "name")
        )
        if year
        else []
    )
    yr = year_of(year) if year else timezone.now().year
    summaries = queries.database_summaries([d.id for d in databases])
    cards = []
    status_total: Counter[str] = Counter()
    for db in databases:
        rows = queries.master_indicator_values(fact_filter(db))
        inds = _rows_to_indicators(rows, yr)
        counts = Counter(i.tracking for i in inds)
        status_total.update(counts)
        s = summaries.get(db.id, {})
        stale = bool(db.last_monthly_update_date and (timezone.now() - db.last_monthly_update_date).days > 40)
        cards.append(
            {
                "database": db,
                "indicators": len(inds),
                "status_counts": {k: counts.get(k, 0) for k in LABELS},
                "reports": int(s.get("reports") or 0),
                "partners": int(s.get("partners") or 0),
                "last_import": db.last_monthly_update_date,
                "stale": stale,
                # The data import runs database by database: the last attempt on this one says why
                # it is old while the import itself succeeded recently (for the others).
                "last_attempt": SyncRun.objects.filter(
                    job=SyncRun.Job.ACTIVITYINFO_DATA, target=str(db.ai_id)
                )
                .order_by("-started_at")
                .first()
                if stale
                else None,
            }
        )
    return {
        "year": year,
        "cards": cards,
        "status_counts": {k: status_total.get(k, 0) for k in LABELS},
        "totals": {
            "databases": len(cards),
            "indicators": sum(c["indicators"] for c in cards),
            "reports": sum(c["reports"] for c in cards),
            "partners": queries.partner_count([d.id for d in databases]),
        },
        "last_runs": [
            {"job": job, "label": label, "run": SyncRun.last_success(job)}
            for job, label in SyncRun.Job.choices
        ],
    }
