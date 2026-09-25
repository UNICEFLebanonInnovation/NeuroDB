"""Page-facing services for database dashboards, pivots, maps and Neuro Reports.

Views call these and render; nothing here touches the request. All return plain dicts/lists so
the same function feeds an HTML page, an HTMX partial and the internal JSON API.
"""

from __future__ import annotations

import datetime
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any

from django.utils import timezone

from neurodb.facts import queries
from neurodb.facts.queries import FactFilter
from neurodb.indicators.models import Database, NeuroReport, NeuroReportComment, ReportingYear
from neurodb.indicators.services.tracking import LABELS, tracking, year_of

QUARTERS = {"Q1": 3, "Q2": 6, "Q3": 9, "Q4": 12}


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


def _rows_to_indicators(rows: list[dict[str, Any]], year: int) -> list[IndicatorRow]:
    out = []
    for r in rows:
        value = float(r["value"]) if r["value"] is not None else None
        target = float(r["target"]) if r["target"] else None
        t = tracking(value, target, year)
        out.append(
            IndicatorRow(
                id=r["id"],
                label=r["label"],
                awp_code=r["awp_code"] or "",
                aggregation_method=r["aggregation_method"] or "SUM",
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
    return rows


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
    return {
        "dashboard": dash,
        "by_governorate": queries.interventions_by_area(f, "governorate"),
        "by_district": queries.interventions_by_area(f, "district")[:15],
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


def resolve_period(
    month: int | None, quarter: str | None, today: datetime.date | None = None
) -> tuple[int, str | None]:
    """v2 rule: default month is the previous month; a quarter maps to its last month."""
    today = today or datetime.date.today()
    if quarter in QUARTERS:
        return QUARTERS[quarter], quarter
    if month and 1 <= month <= 12:
        return month, None
    return (today.month - 1) or 12, None


def neuroreport(report: NeuroReport, month: int | None = None, quarter: str | None = None) -> dict[str, Any]:
    """Values of a Neuro Report to the end of a period with the HPM cut-off and previous-period deltas.

    The cut-off ports v2's rule: records last edited after the 17th of the month following the
    period are excluded (a June report viewed in September ignores edits after 17 July).
    """
    month, quarter = resolve_period(month, quarter)
    year = year_of(report.ryear) if report.ryear else timezone.now().year
    cutoff_month = month + 1
    cutoff = datetime.date(year + (1 if cutoff_month > 12 else 0), (cutoff_month - 1) % 12 + 1, 17)
    databases = Database.objects.filter(masterindicator__neuroreportmasterindicator__report=report).distinct()
    prev_month = month - (3 if quarter else 1)
    sections = []
    for db in databases.select_related("section").order_by("section__name", "name"):
        f_now = fact_filter(db, month_to=month, edited_before=cutoff)
        rows_now = _rows_to_indicators(queries.master_indicator_values(f_now, report_id=report.id), year)
        prev = {}
        if prev_month >= 1:
            f_prev = fact_filter(db, month_to=prev_month, edited_before=cutoff)
            prev = {r["id"]: r for r in queries.master_indicator_values(f_prev, report_id=report.id)}
        items = []
        for i in rows_now:
            p = prev.get(i.id, {}).get("value")
            p = float(p) if p is not None else 0.0
            items.append({**asdict(i), "previous": p, "delta": (i.value or 0.0) - p})
        if items:
            sections.append({"database": db, "items": items})
    comments = (
        NeuroReportComment.objects.filter(report=report, is_active=True, related_month__lte=f"{month:02d}")
        .select_related("master")
        .order_by("related_month", "entry_date")
    )
    return {
        "report": report,
        "year": year,
        "month": month,
        "month_label": datetime.date(year, month, 1).strftime("%B"),
        "quarter": quarter,
        "cutoff": cutoff,
        "sections": sections,
        "comments": list(comments),
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
        cards.append(
            {
                "database": db,
                "indicators": len(inds),
                "status_counts": {k: counts.get(k, 0) for k in LABELS},
                "reports": int(s.get("reports") or 0),
                "partners": int(s.get("partners") or 0),
                "last_import": db.last_monthly_update_date,
                "stale": bool(
                    db.last_monthly_update_date and (timezone.now() - db.last_monthly_update_date).days > 40
                ),
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
            "partners": len({p for c in cards for p in [c["partners"]]}),
        },
        "last_runs": [
            {"job": job, "label": label, "run": SyncRun.last_success(job)}
            for job, label in SyncRun.Job.choices
        ],
    }
