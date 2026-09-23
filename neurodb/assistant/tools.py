"""Read-only data tools the AI assistant calls to answer questions.

Every tool wraps an existing NeuroDB service, so an answer uses the same numbers as the pages. Each
returns JSON-safe data with a ``url`` to the page it came from, so answers can link to the source.
Nothing here writes. Signed-in users may read all of this data on the site (there are no per-section
read restrictions), so the tools apply no extra filtering; the assistant itself requires sign-in.
"""

from __future__ import annotations

import datetime
from collections.abc import Callable
from decimal import Decimal
from typing import Any

from django.db.models import Count, Q, Sum
from django.urls import reverse

from neurodb.core.services import population as population_service
from neurodb.facts.models import ActivityReportNew
from neurodb.facts.services import dashboard as facts
from neurodb.indicators.models import Database, MasterIndicator, NeuroReport, ReportingYear
from neurodb.library.services import completed_maps, search_resources
from neurodb.partnerships import services as partnerships
from neurodb.partnerships.models import PCA, PartnerOrganization
from neurodb.reports import services as report_services

MAX_ROWS = 60  # rows per list; longer lists are cut and flagged so the model can narrow the question


class ToolInputError(ValueError):
    """The model sent arguments that do not match the tool's schema."""


# ------------------------------------------------------------------------- helpers


def _year(raw: str | None) -> ReportingYear | None:
    return report_services.resolve_year(raw or None)


def _db_name(db: Database) -> str:
    return db.label or db.name


def _num(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, float):
        return round(value, 2)
    if isinstance(value, datetime.datetime | datetime.date):
        return value.isoformat()
    return value


def _clean(obj: Any) -> Any:
    """Make service output JSON-safe (dates, decimals, model instances by str)."""
    if isinstance(obj, dict):
        return {str(k): _clean(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [_clean(v) for v in obj]
    if obj is None or isinstance(obj, str | int | bool):
        return obj
    if isinstance(obj, float | Decimal | datetime.date | datetime.datetime):
        return _num(obj)
    return str(obj)


def _cut(rows: list, key: str = "rows") -> dict[str, Any]:
    return {key: rows[:MAX_ROWS], "total_rows": len(rows), "truncated": len(rows) > MAX_ROWS}


def _database(database_id: int) -> Database:
    db = Database.objects.select_related("section", "reporting_year").filter(pk=database_id).first()
    if not db:
        raise ToolInputError(f"No database with id {database_id}. Use list_databases to find ids.")
    return db


def _match_options(query: str | None, options: list[str]) -> list[str]:
    """Case-insensitive partial match of a user phrase against the exact values a filter accepts."""
    if not query:
        return []
    q = query.strip().lower()
    return [o for o in options if q in o.lower()]


# ------------------------------------------------------------------------- tools


def list_databases(year: str | None = None) -> dict[str, Any]:
    ry = _year(year)
    data = facts.overview(ry)
    reports = NeuroReport.objects.filter(is_active=True, ryear=ry).order_by("name") if ry else []
    return {
        "reporting_year": ry.name if ry else None,
        "available_years": list(ReportingYear.objects.order_by("-name").values_list("name", flat=True)),
        "databases": [
            {
                "id": c["database"].id,
                "name": _db_name(c["database"]),
                "section": c["database"].section.name if c["database"].section else None,
                "funded_by_unicef_only": c["database"].is_funded_by_unicef,
                "master_indicators": c["indicators"],
                "status_counts": c["status_counts"],
                "activity_reports": c["reports"],
                "partners": c["partners"],
                "last_import": _num(c["last_import"]),
                "import_is_stale": c["stale"],
                "url": reverse("reports:database_dashboard", args=[c["database"].id]),
            }
            for c in data["cards"]
        ],
        "neuro_reports": [
            {
                "id": r.id,
                "name": r.name,
                "is_hpm": r.is_hpm,
                "url": reverse("reports:report_hpm" if r.is_hpm else "reports:report_dashboard", args=[r.id]),
            }
            for r in reports
        ],
        "status_counts": data["status_counts"],
    }


def find_indicators(query: str, year: str | None = None) -> dict[str, Any]:
    ry = _year(year)
    qs = (
        MasterIndicator.objects.filter(is_active=True)
        .filter(Q(name__icontains=query) | Q(awp_code__icontains=query) | Q(database__name__icontains=query))
        .select_related("database")
        .order_by("database__name", "sequence")
    )
    if ry:
        qs = qs.filter(database__reporting_year=ry)
    rows = [
        {
            "master_id": m.id,
            "name": m.name,
            "awp_code": m.awp_code,
            "database_id": m.database_id,
            "database": _db_name(m.database),
            "target": _num(m.awp_target),
            "url": reverse("reports:indicator_detail", args=[m.database_id, m.id]),
        }
        for m in qs[: MAX_ROWS + 1]
    ]
    return {"reporting_year": ry.name if ry else None, **_cut(rows, "master_indicators")}


def database_results(database_id: int) -> dict[str, Any]:
    db = _database(database_id)
    dash = facts.database_dashboard(db)
    indicators = [
        {
            "master_id": i.id,
            "name": i.label,
            "awp_code": i.awp_code,
            "aggregation": i.aggregation_method,
            "unit": i.unit,
            "target": _num(i.target),
            "achieved_value": _num(i.value),
            "percent_of_target": _num(i.achieved),
            "status": i.tracking_label,
            "activity_reports": i.reports,
        }
        for i in dash.indicators
    ]
    return {
        "database": _db_name(db),
        "section": db.section.name if db.section else None,
        "year": dash.year,
        "funded_by_unicef_only": db.is_funded_by_unicef,
        "status_counts": dash.status_counts,
        "totals": dash.totals,
        "monthly_totals": _clean(dash.monthly),
        "last_import": _num(dash.last_import),
        "status_meaning": (
            "on track / off track compare % of target achieved with % of the year elapsed (±10 points)"
        ),
        "url": reverse("reports:database_dashboard", args=[db.id]),
        **_cut(indicators, "indicators"),
    }


def indicator_breakdown(database_id: int, master_id: int) -> dict[str, Any]:
    db = _database(database_id)
    master = MasterIndicator.objects.filter(pk=master_id, database=db).first()
    if not master:
        raise ToolInputError(f"Master indicator {master_id} is not in database {database_id}.")
    rows = facts.master_detail(db, master.id)
    subs = [
        {
            "name": r.get("label") or r.get("name"),
            "awp_code": r.get("awp_code"),
            "effect": r.get("effect"),
            "target": _num(r.get("target")),
            "value": _num(r.get("value")),
            "activity_reports": r.get("reports"),
        }
        for r in rows
    ]
    return {
        "master_indicator": master.name,
        "awp_code": master.awp_code,
        "aggregation": master.aggregation_method,
        "target": _num(master.awp_target),
        "database": _db_name(db),
        "url": reverse("reports:indicator_detail", args=[db.id, master.id]),
        **_cut(subs, "sub_indicators"),
    }


GROUP_BY = {
    "governorate": "location_adminlevel_governorate",
    "district": "location_adminlevel_caza",
    "cadaster": "location_adminlevel_cadastral_area",
    "site": "location_name",
    "partner": "partner_label",
    "programme_document": "project_label",
    "month": "month_name",
    "indicator": "indicator_name",
}
FILTERS = {
    "partner": "partner_label__icontains",
    "governorate": "location_adminlevel_governorate__icontains",
    "district": "location_adminlevel_caza__icontains",
    "programme_document": "project_label__icontains",
    "indicator": "indicator_name__icontains",
    "awp_code": "indicator_awp_code__istartswith",
}


def activity_breakdown(
    database_id: int,
    group_by: str,
    partner: str | None = None,
    governorate: str | None = None,
    district: str | None = None,
    programme_document: str | None = None,
    indicator: str | None = None,
    awp_code: str | None = None,
    month: int | None = None,
) -> dict[str, Any]:
    db = _database(database_id)
    qs = ActivityReportNew.objects.filter(dbase=db)
    if db.is_funded_by_unicef:  # same rule as the dashboards
        qs = qs.filter(funded_by="UNICEF")
    applied = {}
    for name, value in (
        ("partner", partner),
        ("governorate", governorate),
        ("district", district),
        ("programme_document", programme_document),
        ("indicator", indicator),
        ("awp_code", awp_code),
    ):
        if value:
            qs = qs.filter(**{FILTERS[name]: value})
            applied[name] = value
    if month:
        qs = qs.filter(month_name__regex=rf"^\d{{4}}-{month:02d}")
        applied["month"] = month
    field = GROUP_BY[group_by]
    grouped = (
        qs.exclude(**{f"{field}__isnull": True})
        .values(field)
        .annotate(
            activity_reports=Count("id"),
            sites=Count("location_name", distinct=True),
            partners=Count("partner_label", distinct=True),
            summed_value=Sum("indicator_value"),
        )
        .order_by(field if group_by == "month" else "-activity_reports")
    )
    rows = [
        {
            group_by: (r[field][:7] if group_by == "month" else r[field]),
            "activity_reports": r["activity_reports"],
            "sites": r["sites"],
            "partners": r["partners"],
            "summed_value": _num(r["summed_value"]),
        }
        for r in grouped[: MAX_ROWS + 1]
    ]
    return {
        "database": _db_name(db),
        "filters_applied": applied,
        "note": (
            "summed_value adds indicator_value across every matching record. It is only a meaningful "
            "total when the records are one indicator (filter by awp_code or indicator); otherwise use "
            "activity_reports, sites and partners. Official results per indicator come from database_results."
        ),
        "url": reverse("reports:database_analytical", args=[db.id]),
        **_cut(rows),
    }


def neuro_report(report_id: int, month: int | None = None, quarter: str | None = None) -> dict[str, Any]:
    report = NeuroReport.objects.select_related("ryear").filter(pk=report_id).first()
    if not report:
        raise ToolInputError(f"No Neuro report with id {report_id}. Use list_databases to find report ids.")
    data = facts.neuroreport(report, month=month, quarter=quarter)
    sections = [
        {
            "database": _db_name(s["database"]),
            "indicators": [
                {
                    "name": i["label"],
                    "awp_code": i["awp_code"],
                    "target": _num(i["target"]),
                    "value": _num(i["value"]),
                    "previous_period_value": _num(i["previous"]),
                    "change": _num(i["delta"]),
                    "percent_of_target": _num(i["achieved"]),
                    "status": i["tracking_label"],
                }
                for i in s["items"]
            ][:MAX_ROWS],
        }
        for s in data["sections"]
    ]
    return {
        "report": report.name,
        "is_hpm": report.is_hpm,
        "year": data["year"],
        "period": data["quarter"] or data["month_label"],
        "cutoff_rule": f"records edited after {data['cutoff'].isoformat()} are excluded",
        "sections": sections,
        "comments": [
            {"month": c.related_month, "indicator": str(c.master) if c.master_id else None, "text": str(c)}
            for c in data["comments"][:20]
        ],
        "url": reverse(
            "reports:report_hpm" if report.is_hpm else "reports:report_dashboard", args=[report.id]
        ),
    }


def _pd_row(pd: PCA) -> dict[str, Any]:
    return {
        "number": pd.number,
        "title": pd.title,
        "partner": pd.partner_name,
        "type": pd.document_type,
        "status": pd.status,
        "start": _num(pd.start),
        "end": _num(pd.end),
        "total_budget": pd.total_budget,
        "sections": pd.section_names or [],
        "donors": pd.donors or [],
        "url": reverse("reports:programme_detail", args=[pd.id]),
    }


def _pd_filters(partner, section, donor, office, status, year_from, year_to):
    options = partnerships.pd_filter_options()
    matched = {
        "partners": _match_options(partner, options["partners"]),
        "sections": _match_options(section, options["sections"]),
        "donors": _match_options(donor, options["donors"]),
        "offices": _match_options(office, options["offices"]),
    }
    for name, query in (("partners", partner), ("sections", section), ("donors", donor), ("offices", office)):
        if query and not matched[name]:
            raise ToolInputError(f"No {name[:-1]} matches '{query}'. Known values: {options[name][:40]}")
    filters = partnerships.PDFilters(
        partners=matched["partners"],
        cso_types=[],
        sections=matched["sections"],
        offices=matched["offices"],
        statuses=[status] if status else [],
        donors=matched["donors"],
        grants=[],
        numbers=[],
        document_types=[],
        year_from=year_from,
        year_to=year_to,
    )
    return filters, {k: v for k, v in matched.items() if v}


def search_programmes(
    text: str | None = None,
    partner: str | None = None,
    section: str | None = None,
    donor: str | None = None,
    office: str | None = None,
    status: str | None = None,
    active_only: bool = False,
    year_from: int | None = None,
    year_to: int | None = None,
) -> dict[str, Any]:
    filters, matched = _pd_filters(partner, section, donor, office, status, year_from, year_to)
    qs = partnerships.programme_documents(filters, scope="active" if active_only else "all")
    if text:
        qs = qs.filter(Q(title__icontains=text) | Q(number__icontains=text))
    pds = list(qs[: MAX_ROWS + 1])
    budget = 0.0
    for pd in qs.select_related(None).only("total_budget"):
        budget += partnerships._to_float(pd.total_budget)
    return {
        "matched_filter_values": matched,
        "count": qs.count(),
        "total_budget_of_all_matches": round(budget, 2),
        "url": reverse("reports:programmes"),
        **_cut([_pd_row(pd) for pd in pds], "programme_documents"),
    }


def programme_details(number: str) -> dict[str, Any]:
    pd = (
        PCA.objects.filter(number__iexact=number).first()
        or PCA.objects.filter(number__istartswith=number).first()
    )
    if not pd:
        raise ToolInputError(f"No programme document numbered '{number}'.")
    detail = partnerships.pd_detail(pd)
    return {
        **_pd_row(pd),
        "offices": detail["offices"],
        "donations_total": _num(detail["donations"]),
        "donors_detail": _clean(detail["donors"]),
        "activity_reports_linked": detail["interventions"],
        "planned_locations": len(pd.location_p_codes or []),
    }


def donor_funding(
    donor: str | None = None,
    partner: str | None = None,
    section: str | None = None,
    year_from: int | None = None,
    year_to: int | None = None,
) -> dict[str, Any]:
    filters, matched = _pd_filters(partner, section, donor, None, None, year_from, year_to)
    data = partnerships.donor_mapping(filters)
    return {
        "matched_filter_values": matched,
        "programme_documents": data["count"],
        "donations_total": _num(data["donations_total"]),
        "funds_by_donor": [{"donor": d, "amount": _num(v)} for d, v in data["funds_by_donor"]],
        "funds_by_start_year": [{"year": y, "amount": _num(v)} for y, v in data["funds_by_year"]],
        "activity_reports": data["interventions_total"],
        "activity_reports_by_governorate": [
            {"governorate": r["location_adminlevel_governorate"], "activity_reports": r["n"]}
            for r in data["interventions_by_governorate"]
        ],
        "unique_actual_locations": data["unique_actual_locations"],
        "planned_locations_unique": data["planned_locations_unique"],
        "url": reverse("reports:donors"),
    }


def search_partners(text: str | None = None, partner_type: str | None = None) -> dict[str, Any]:
    params = {"q": text or "", "partner_type": [partner_type] if partner_type else []}
    qs = partnerships.partners(_Params(params))
    rows = [
        {
            "partner_id": p.id,
            "name": p.name,
            "short_name": p.short_name,
            "type": p.partner_type,
            "cso_type": p.cso_type,
            "risk_rating": p.rating,
            "programme_documents": p.pd_count,
            "active_programme_documents": p.active_pd_count,
            "url": reverse("reports:partner_profile", args=[p.id]),
        }
        for p in qs[: MAX_ROWS + 1]
    ]
    return {"count": qs.count(), **_cut(rows, "partners")}


def partner_details(partner_id: int) -> dict[str, Any]:
    partner = PartnerOrganization.objects.filter(pk=partner_id).first()
    if not partner:
        raise ToolInputError(f"No partner with id {partner_id}. Use search_partners first.")
    profile = partnerships.partner_profile(partner)
    return {
        "name": partner.name,
        "short_name": partner.short_name,
        "type": partner.partner_type,
        "cso_type": partner.cso_type,
        "vendor_number": partner.vendor_number,
        "risk_rating": profile["risk_rating"],
        "hact": _clean(profile["hact"]),
        "active_programme_documents": profile["active_count"],
        "programme_documents": [_pd_row(pd) for pd in profile["programme_documents"][:30]],
        "assurance_engagements": profile["engagement_counts"],
        "field_visits_by_year": _clean(profile["visits_by_year"]),
        "activity_reports": profile["interventions"],
        "url": reverse("reports:partner_profile", args=[partner.id]),
    }


def population(year: int | None = None, category: str = "total") -> dict[str, Any]:
    years = population_service.available_years()
    if not years:
        return {"error": "No population figures are loaded."}
    year = year if year in years else years[0]
    data = population_service.population_view(year, category)
    return {
        "year": year,
        "available_years": years,
        "category": category,
        "all_nationalities_total": data["grand_total"],
        "totals_by_nationality": data["totals_by_nationality"],
        "nationality_codes": {"LEB": "Lebanese", "SYR": "Syrian", "PRL": "Palestinian refugees in Lebanon",
                              "PRS": "Palestinian refugees from Syria", "OTH": "Other (migrants)"},
        "by_governorate": _clean(data["by_governorate"]),
        "by_district": _clean(data["by_district"]),
        "by_age_group": _clean(data["by_age_group"]),
        "url": reverse("reports:population") + f"?year={year}&view={category}",
    }  # fmt: skip


def search_library(
    text: str | None = None, year: str | None = None, programme: str | None = None
) -> dict[str, Any]:
    params = {"q": text or "", "year": [year] if year else [], "section": [programme] if programme else []}
    page = search_resources(_Params(params), 1, per_page=25)
    return {
        "count": page.paginator.count,
        "resources": [
            {
                "title": r.title,
                "year": r.publication_year,
                "programme": r.section,
                "type": r.type.name if r.type else None,
                "topic": r.topic.name if r.topic else None,
                "summary": (r.description or "")[:600],
                "url": reverse("reports:library_item", args=[r.id]),
            }
            for r in page
        ],
        "maps": [
            {
                "name": m.name,
                "summary": (m.description or "")[:300],
                "url": reverse("reports:map_item", args=[m.id]),
            }
            for m in completed_maps()
            if not text or text.lower() in f"{m.name} {m.description or ''}".lower()
        ][:15],
    }


def data_freshness() -> dict[str, Any]:
    health = report_services.data_health()
    return {
        "jobs": [
            {
                "job": j["label"],
                "state": j["state_label"],
                "last_success": _num(j["last_success"].finished_at) if j["last_success"] else None,
                "age_hours": _num(j["age_hours"]),
            }
            for j in health["jobs"]
        ],
        "databases": [
            {
                "database": _db_name(d["database"]),
                "last_import": _num(d["last_import"]),
                "state": d["state_label"],
            }
            for d in health["databases"]
        ],
        "url": reverse("reports:data_health"),
    }


class _Params(dict):
    """The QueryDict-like object the page services expect (``get`` and ``getlist``)."""

    def getlist(self, key):
        value = self.get(key)
        return value if isinstance(value, list) else ([value] if value else [])


# ------------------------------------------------------------------------- definitions for Claude

_YEAR = {"type": "string", "description": 'Reporting year name, e.g. "2026". Omit for the current year.'}
_DB = {"type": "integer", "description": "Database id from list_databases."}


def _schema(properties: dict, required: list[str] | None = None) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


TOOLS: dict[str, tuple[Callable[..., dict], str, dict, str]] = {
    # name: (function, description, input schema, progress label shown to the user)
    "list_databases": (
        list_databases,
        "List the reporting databases (one per programme section, e.g. Child Protection 2026) of a "
        "reporting year with their ids, indicator status counts, number of activity reports and partners, "
        "and import freshness; also lists the Neuro reports / HPM reports with their ids. Start here "
        "when the question names a programme, sector or report.",
        _schema({"year": _YEAR}),
        "Listing databases and reports",
    ),
    "find_indicators": (
        find_indicators,
        "Find master indicators by words in their name, their AWP code or their database name. Returns "
        "master_id and database_id for database_results and indicator_breakdown.",
        _schema(
            {"query": {"type": "string", "description": "Words or an AWP code."}, "year": _YEAR}, ["query"]
        ),
        "Finding indicators",
    ),
    "database_results": (
        database_results,
        "Official results of every master indicator in one database: target, achieved value, % of "
        "target, on/off-track status and number of activity reports, plus monthly totals. Use this for "
        "progress, achievement and target questions.",
        _schema({"database_id": _DB}, ["database_id"]),
        "Reading indicator results",
    ),
    "indicator_breakdown": (
        indicator_breakdown,
        "The sub-indicators that make up one master indicator, with their values and targets.",
        _schema({"database_id": _DB, "master_id": {"type": "integer"}}, ["database_id", "master_id"]),
        "Reading sub-indicators",
    ),
    "activity_breakdown": (
        activity_breakdown,
        "Count the raw ActivityInfo activity reports of one database grouped by governorate, district, "
        "cadaster, site, partner, programme_document (PD number), month or indicator, optionally "
        "filtered. Returns reports, distinct sites and partners per group, and a summed value that is "
        "only meaningful when filtered to one indicator (awp_code or indicator). Use for 'where', "
        "'which partners', 'how many sites', 'by month' questions.",
        _schema(
            {
                "database_id": _DB,
                "group_by": {"type": "string", "enum": list(GROUP_BY)},
                "partner": {"type": "string", "description": "Part of a partner name."},
                "governorate": {"type": "string"},
                "district": {"type": "string"},
                "programme_document": {"type": "string", "description": "PD number or its prefix."},
                "indicator": {"type": "string", "description": "Part of an ActivityInfo indicator name."},
                "awp_code": {"type": "string", "description": "AWP code prefix, e.g. 1.2"},
                "month": {"type": "integer", "minimum": 1, "maximum": 12},
            },
            ["database_id", "group_by"],
        ),
        "Counting activity reports",
    ),
    "neuro_report": (
        neuro_report,
        "Values of a Neuro report or HPM report for a month or quarter, with the previous period and the "
        "change, applying the HPM cut-off rule. Defaults to the previous month.",
        _schema(
            {
                "report_id": {"type": "integer"},
                "month": {"type": "integer", "minimum": 1, "maximum": 12},
                "quarter": {"type": "string", "enum": ["Q1", "Q2", "Q3", "Q4"]},
            },
            ["report_id"],
        ),
        "Reading the Neuro report",
    ),
    "search_programmes": (
        search_programmes,
        "Search eTools programme documents (PDs/SSFAs) by title or number, partner, section, donor, "
        "office, status or start-year range. Filter values are matched by partial name. Returns the "
        "count, total budget and the documents.",
        _schema(
            {
                "text": {"type": "string"},
                "partner": {"type": "string"},
                "section": {"type": "string"},
                "donor": {"type": "string"},
                "office": {"type": "string"},
                "status": {"type": "string", "description": "e.g. active, ended, closed, signed, suspended"},
                "active_only": {"type": "boolean"},
                "year_from": {"type": "integer"},
                "year_to": {"type": "integer"},
            }
        ),
        "Searching programme documents",
    ),
    "programme_details": (
        programme_details,
        "Details of one programme document by its number: dates, budget, donors and grants, sections, "
        "offices, linked activity reports and planned locations.",
        _schema({"number": {"type": "string"}}, ["number"]),
        "Opening the programme document",
    ),
    "donor_funding": (
        donor_funding,
        "Funding by donor and by start year across programme documents, optionally for one donor, "
        "partner, section or year range, with activity reports per governorate.",
        _schema(
            {
                "donor": {"type": "string"},
                "partner": {"type": "string"},
                "section": {"type": "string"},
                "year_from": {"type": "integer"},
                "year_to": {"type": "integer"},
            }
        ),
        "Summarising donor funding",
    ),
    "search_partners": (
        search_partners,
        "Find implementing partners by name, short name or vendor number, optionally by type; returns "
        "partner ids, risk rating and programme document counts.",
        _schema({"text": {"type": "string"}, "partner_type": {"type": "string"}}),
        "Searching partners",
    ),
    "partner_details": (
        partner_details,
        "Profile of one partner: programme documents, HACT and risk rating, assurance engagements, field "
        "visits per year and linked activity reports.",
        _schema({"partner_id": {"type": "integer"}}, ["partner_id"]),
        "Opening the partner profile",
    ),
    "population": (
        population,
        "Population figures of Lebanon by nationality, governorate, district and age group (category "
        "total or children).",
        _schema(
            {
                "year": {"type": "integer"},
                "category": {"type": "string", "enum": list(report_services.POPULATION_VIEWS)},
            }
        ),
        "Reading population figures",
    ),
    "search_library": (
        search_library,
        "Search published studies, evaluations, assessments and briefs in the library (title and "
        "summary), and completed map products.",
        _schema(
            {
                "text": {"type": "string"},
                "year": {"type": "string"},
                "programme": {"type": "string", "description": "Programme/section name, e.g. Education"},
            }
        ),
        "Searching the library",
    ),
    "data_freshness": (
        data_freshness,
        "When each data source (ActivityInfo, eTools, locations, population) last synced and how fresh "
        "each database's import is. Use when asked how up to date the data is.",
        _schema({}),
        "Checking data freshness",
    ),
}


def definitions() -> list[dict[str, Any]]:
    """Tool definitions for the Messages API, in a fixed order (keeps the prompt cache stable)."""
    return [
        {
            "name": name,
            "description": description,
            "input_schema": schema,
            # Answers stream to the browser; stream tool inputs too, and validate them (validate()).
            "eager_input_streaming": True,
        }
        for name, (_, description, schema, _) in TOOLS.items()
    ]


_TYPES = {"string": str, "integer": int, "boolean": bool}


def validate(name: str, args: Any) -> dict[str, Any]:
    """Check arguments against the tool schema (eager input streaming skips the server-side check)."""
    if name not in TOOLS:
        raise ToolInputError(f"Unknown tool {name}.")
    if not isinstance(args, dict):
        raise ToolInputError("Arguments must be a JSON object.")
    schema = TOOLS[name][2]
    props = schema["properties"]
    for key in schema["required"]:
        if key not in args:
            raise ToolInputError(f"Missing required argument '{key}'.")
    clean = {}
    for key, value in args.items():
        if key not in props:
            raise ToolInputError(f"Unknown argument '{key}'.")
        if value is None:
            continue
        spec = props[key]
        expected = _TYPES[spec["type"]]
        if not isinstance(value, expected) or (expected is int and isinstance(value, bool)):
            raise ToolInputError(f"Argument '{key}' must be a {spec['type']}.")
        if "enum" in spec and value not in spec["enum"]:
            raise ToolInputError(f"Argument '{key}' must be one of {spec['enum']}.")
        if expected is int and not (spec.get("minimum", value) <= value <= spec.get("maximum", value)):
            raise ToolInputError(f"Argument '{key}' is out of range.")
        if expected is str:
            value = value.strip()[:200]
        clean[key] = value
    return clean


def run(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Validate and run one tool. Input problems raise ToolInputError (sent back to Claude)."""
    clean = validate(name, args)
    return _clean(TOOLS[name][0](**clean))


def label(name: str) -> str:
    return TOOLS[name][3] if name in TOOLS else "Looking up data"
