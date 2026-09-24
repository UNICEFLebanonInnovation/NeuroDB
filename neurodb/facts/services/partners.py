"""What one eTools partner reported in ActivityInfo, by database and by month (partner page).

The ActivityInfo databases keep their own pages and layout; this only reads the records of the
partner's linked ActivityInfo names (:mod:`neurodb.partnerships.linking`) so that the partner page
can show both reporting systems side by side.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from django.urls import reverse

from neurodb.facts import queries
from neurodb.facts.queries import MONTH_LABELS
from neurodb.facts.services.dashboard import fact_filter
from neurodb.indicators.models import Database
from neurodb.partnerships import linking
from neurodb.partnerships.models import PartnerOrganization

MONTHS = list(MONTH_LABELS)  # "01".."12"


def _float(value: Any) -> float | None:
    return float(value) if value is not None else None


def partner_activityinfo(partner: PartnerOrganization) -> dict[str, Any]:
    """Per database: records, indicators, sites and months reported by the partner's ActivityInfo names."""
    labels = linking.partner_labels(partner)
    rows = queries.partner_activity(labels)
    databases = {
        d.id: d
        for d in Database.objects.filter(id__in=[r["database_id"] for r in rows]).select_related(
            "section", "reporting_year"
        )
    }
    by_year: dict[str, int] = defaultdict(int)
    items = []
    for r in rows:
        database = databases.get(r["database_id"])
        if database is None:
            continue
        year = database.reporting_year.name if database.reporting_year else (database.year or "")
        by_year[year] += int(r["records"] or 0)
        items.append(
            {
                "database": database,
                "year": year,
                "section": database.section.name if database.section else "",
                "records": int(r["records"] or 0),
                "indicators": int(r["indicators"] or 0),
                "sites": int(r["sites"] or 0),
                "months": int(r["months"] or 0),
                "first_month": r["first_month"] or "",
                "last_month": r["last_month"] or "",
                "pds": int(r["pds"] or 0),
                "url": reverse("reports:partner_activityinfo", args=[partner.id, database.id]),
                "map_url": reverse("reports:database_map", args=[database.id])
                + "?level=site&partner="
                + "&partner=".join(labels),
            }
        )
    items.sort(key=lambda i: (i["year"], i["section"], i["database"].name), reverse=True)
    return {
        "labels": labels,
        "databases": items,
        "records": sum(i["records"] for i in items),
        "years": sorted({i["year"] for i in items if i["year"]}),
        "by_year": sorted(by_year.items()),
        "link_run": linking.last_run(),
    }


def partner_database_indicators(partner: PartnerOrganization, database: Database) -> dict[str, Any]:
    """The master indicators of ``database`` with the partner's value, the database total and months."""
    labels = linking.partner_labels(partner)
    f = fact_filter(database, partner_labels=tuple(labels))
    totals = {r["id"]: _float(r["value"]) for r in queries.master_indicator_values(fact_filter(database))}
    monthly = queries.master_monthly_values(f)
    months_used: set[str] = set()
    indicators = []
    for r in queries.master_indicator_values(f):
        value = _float(r["value"])
        if value is None and not monthly.get(r["id"]):
            continue
        total = totals.get(r["id"])
        by_month = monthly.get(r["id"], {})
        months_used.update(by_month)
        indicators.append(
            {
                "id": r["id"],
                "label": r["label"],
                "awp_code": r["awp_code"] or "",
                "unit": r.get("unit"),
                "aggregation_method": r["aggregation_method"] or "SUM",
                "value": value,
                "total": total,
                "share": round(value * 100 / total, 1) if value is not None and total else None,
                "target": _float(r["target"]) if r["target"] else None,
                "reports": int(r["reports"] or 0),
                "months": by_month,
                "url": reverse("reports:indicator_detail", args=[database.id, r["id"]]),
            }
        )
    months = [m for m in MONTHS if m in months_used]
    return {
        "labels": labels,
        "database": database,
        "indicators": indicators,
        "months": months,
        "month_labels": {m: MONTH_LABELS[m] for m in months},
        "sites": queries.sites(f),
    }
