"""NeuroDB's side of Compare with Power BI: for each kind of export, the rows of the NeuroDB page that shows
the same thing, from the page's own function, with Power BI's filter given explicitly (never a page default:
the running PDs, the user's own section, this year).

The common filter: Power BI's PD statuses (``status is active`` is ``active`` only; no status clause is every
status), its reporting years (the year the reporting period ends in; several years are read each and added),
its sections, and on the progress reports its report statuses (Power BI's Due covers reports not yet due).
Each function returns plain dicts holding only the fields this module names (:func:`shown`): never a focal
point, a person who submitted a report, a narrative or a record's whole data, even where the page's own
function returns them.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable
from typing import Any

from . import powerbi_facts as pf
from . import powerbi_match as pm
from . import reached as rc
from .powerbi_filters import Filter

# Power BI's labels of NeuroDB's six report statuses on its lists (its Due covers reports not yet due)
LIST_LABELS = {
    rc.ACCEPTED: "Accepted",
    rc.SUBMITTED: "Submitted",
    rc.SENT_BACK: "Sent back",
    rc.DUE: "Due",
    rc.OVERDUE: "Overdue",
    rc.NOT_YET_DUE: "Due",
}
# and on its charts and cards (where Not Yet Due has a column of its own)
CHART_LABELS = {**LIST_LABELS, rc.NOT_YET_DUE: "Not Yet Due"}
REPORT_FIELDS = (
    "key",
    "pd",
    "pd_id",
    "partner",
    "report",
    "report_type",
    "status",
    "period_start",
    "period_end",
    "due_date",
    "submission_date",
    "sections",
    "pd_sections",
    "document_type",
    "pd_status",
    "section_text",
    "source",  # "prp_progress_reports": a report still owed from PRP's own list (datamart.owed); else ""
)


def shown(fields: Iterable[str], row: dict[str, Any]) -> dict[str, Any]:
    """Only these fields of a row (never the row itself: a page's row can carry people)."""
    return {name: row.get(name) for name in fields}


def report_params(parsed: Filter, *, year: bool = True, sections: bool = True) -> dict[str, Any]:
    """Progress reports' filter for Power BI's: its PD statuses (none: every status), its year (one), its
    sections."""
    params: dict[str, Any] = {"pd_status": list(parsed.pd_statuses)}
    if year and len(parsed.years) == 1:
        params["year"] = str(parsed.years[0])
    if sections and parsed.sections:
        params["section"] = list(parsed.sections)
    return params


def wanted_statuses(parsed: Filter) -> set[str] | None:
    """NeuroDB's report statuses for Power BI's report status clause (None: no such clause, every status)."""
    from .powerbi_filters import REPORT_STATUSES

    if not parsed.report_statuses:
        return None
    return {s for label in parsed.report_statuses for s in REPORT_STATUSES.get(label, ())}


def in_years(parsed: Filter, end: Any) -> bool:
    if not parsed.years:
        return True
    found = pm.as_date(end)
    return bool(found and found.year in parsed.years)


# ---------------------------------------------------------------------------------------- progress reports
def reporting(parsed: Filter, context: pf.Context, *, sections: bool = True) -> dict[str, Any]:
    """The Progress reports page for Power BI's filter (``services.partner_reporting``), read once per filter
    in a comparison."""
    from . import services

    params = report_params(parsed, sections=sections)
    key = ("reporting", repr(sorted(params.items())))
    return context.once(key, lambda: services.partner_reporting(params, context.day, options=False))


def reports(parsed: Filter, context: pf.Context, *, sections: bool = True) -> list[dict[str, Any]]:
    """The page's progress reports (one per report, its six statuses read for the day), in Power BI's years
    and report statuses, each as :data:`REPORT_FIELDS`."""
    held = pf.pds(context)
    wanted = wanted_statuses(parsed)
    out = []
    for report in reporting(parsed, context, sections=sections)["reports"]:
        if not in_years(parsed, report["period_end"]):
            continue
        if wanted is not None and report["status"] not in wanted:
            continue
        pd = held.by_id.get(report["intervention_id"]) if report["intervention_id"] else None
        own = [s for s in report["sections"] if s != "—"]
        out.append(
            shown(
                REPORT_FIELDS,
                {
                    "key": report["key"],
                    "pd": (pd.number if pd else "") or report["pd_reference_number"] or "",
                    "pd_id": pd.id if pd else None,
                    "partner": context.clean(report["partner_name"] or (pd.partner if pd else "")),
                    "report": pm.report_number(report["report_number"], report["report_type"]),
                    "report_type": (report["report_type"] or "").upper(),
                    "status": report["status"],
                    "period_start": report["period_start"],
                    "period_end": report["period_end"],
                    "due_date": report["due_date"],
                    "submission_date": report["submission_date"],
                    "sections": pm.sections(own, context.sections),
                    "pd_sections": pm.sections(pd.sections if pd else (), context.sections),
                    "document_type": report["document_type"] or (pd.document_type if pd else ""),
                    "pd_status": pd.status if pd else "",
                    "section_text": ", ".join(pd.sections) if pd and pd.sections else ", ".join(own),
                    "source": report.get("source") or "",
                },
            )
        )
    return out


def partner_performance(parsed: Filter, context: pf.Context) -> dict[str, Any]:
    """Progress reports → Partners (``reporting.partner_performance`` on the page's reports, in Power BI's
    years and report statuses): its counts of partners, read once per filter in an export."""
    from . import reporting as rp

    def build() -> dict[str, Any]:
        found = reporting(parsed, context)
        wanted = wanted_statuses(parsed)
        every = [
            r
            for r in found["reports"]
            if in_years(parsed, r["period_end"]) and (wanted is None or r["status"] in wanted)
        ]
        performance = rp.partner_performance(every)
        return {
            "partners": performance["partners"],
            "reported": performance["reported"],
            "not_reported": performance["not_reported"],
            "by_status": {entry["key"]: entry["partners"] for entry in performance["by_status"]},
        }

    return context.once(("partner_performance", repr(parsed.to_json())), build)


def partners_by_status(parsed: Filter, context: pf.Context) -> dict[str, int]:
    """Partner Performance's table of statuses: the partners with a report in each status."""
    return partner_performance(parsed, context)["by_status"]


# --------------------------------------------------------------------------------------- partner monitoring
INDICATOR_FIELDS = (
    "pd_id",
    "pd",
    "pd_status",
    "pd_start",
    "partner",
    "title",
    "output",
    "section",
    "pd_sections",
    "pd_section_text",
    "cp_output",
    "pd_cp_outputs",
    "emergency",
    "document_type",
    "target",
    "reached",
    "reached_in_year",
    "achieved",
    "partial",
    "report_type",
    "key",
    "ident",
    "accepted",
    "percentage",
)


def monitoring_filters(parsed: Filter, context: pf.Context, view: str, *, sections: bool = True):
    from . import monitoring

    params: dict[str, Any] = {
        "pd_status": list(parsed.pd_statuses),
        "year": str(parsed.years[0]) if len(parsed.years) == 1 else monitoring.ALL_YEARS,
        "view": view,
    }
    if sections and parsed.sections:
        params["section"] = list(parsed.sections)
    return monitoring.Filters.from_params(params, context.day, default_year=False)


def indicators(
    parsed: Filter,
    context: pf.Context,
    view: str = "table",
    *,
    pd_ids: Iterable[int] | None = None,
    reached: bool = True,
) -> list[dict[str, Any]]:
    """Partner monitoring's indicators for Power BI's filter (every section: the comparison reads which ones
    the page's section filter keeps, ``in_section``), each as :data:`INDICATOR_FIELDS`, kept for the export
    as these fields alone (never the page's own entries, which hold every report read). Each report row's
    disaggregation is read as its total alone (``monitoring.indicators``' ``totals``: the figures are the
    page's). ``pd_ids``: those of these PDs only (none when it is empty), read for this call alone.
    ``reached``: False for the indicators alone (``monitoring.collect``: no report is read), their Reached,
    Reached in the year, % and Accepted-only figure None."""
    filters = monitoring_filters(parsed, context, view, sections=False)
    if pd_ids is None:
        key = ("indicators", filters.cache_key(), reached)
        return context.once(key, lambda: _indicators(filters, context, reached))
    ids = sorted(set(pd_ids))
    if not ids:
        return []
    return _indicators(dataclasses.replace(filters, pd_ids=ids), context, reached)


def _indicators(filters, context: pf.Context, reached: bool) -> list[dict[str, Any]]:
    from . import monitoring

    if reached:
        found = monitoring.indicators(filters, context.day, totals=True)
    else:  # what Reached would say of its history: a PD that started before it has a partial one
        found = list(monitoring.collect(filters).values())
        for entry in found:
            entry.partial = bool(entry.pd.start and entry.pd.start < context.history)
    held = pf.pds(context)
    out = []
    for entry in found:
        pd = held.by_id.get(entry.pd.id)
        result = entry.result
        out.append(
            shown(
                INDICATOR_FIELDS,
                {
                    "pd_id": entry.pd.id,
                    "pd": entry.pd.number or "",
                    "pd_status": entry.pd.status or "",
                    "pd_start": entry.pd.start,
                    "partner": context.clean(entry.partner.name if entry.partner else entry.pd.partner_name),
                    "title": context.clean(entry.title),
                    "output": context.clean(entry.output),
                    "section": entry.section or "",
                    "pd_sections": pm.sections(pd.sections if pd else (), context.sections),
                    "pd_section_text": ", ".join(pd.sections) if pd else "",
                    "cp_output": context.clean(entry.cp_output),
                    "pd_cp_outputs": [context.clean(c) for c in (pd.cp_outputs if pd else ())],
                    "emergency": entry.emergency,  # the PD is an SPD, as Power BI's Emergency indicator
                    "document_type": entry.pd.document_type or "",
                    "target": entry.target,
                    "reached": entry.reached,
                    "reached_in_year": entry.reached_in_year,
                    "achieved": entry.achieved,
                    "partial": entry.partial,
                    "report_type": entry.report_type,
                    "key": entry.key,
                    "ident": entry.ident,
                    "accepted": result.reached_accepted if result is not None else None,
                    "percentage": bool(entry.percentage or (entry.display_type or "") == "percentage"),
                },
            )
        )
    return out


def in_section(row: dict[str, Any], parsed: Filter) -> bool:
    """Partner monitoring's section filter: the indicator's own section."""
    from . import monitoring

    chosen = {pm.text(s) for s in parsed.sections}
    return not chosen or pm.text(row["section"] or monitoring.NO_SECTION) in chosen


def in_pd_sections(row: dict[str, Any], parsed: Filter) -> bool:
    """Power BI's Section slicer: the indicator's programme document has one of the sections chosen."""
    chosen = {pm.text(s) for s in parsed.sections}
    return not chosen or bool(chosen & row["pd_sections"])


def quality(parsed: Filter, context: pf.Context, *, by_pd: bool = False) -> dict[str, int]:
    """Data quality's figures for Power BI's filter, as its cards count them, of the indicators the page's
    section filter keeps (``monitoring.data_quality``'s rules, ``FIGURE_TESTS``): the programme documents
    (active, ended), the indicators, those with a Reached above 0 (Power BI's card without Reached counts the
    zeros), those with a target (a target of 0 counts as none) and those without one. ``by_pd``: the same
    figures with the section read Power BI's way, by the PD's sections (:func:`in_pd_sections`), never the
    page's."""
    from neurodb.partnerships import statuses as pd_statuses

    year = parsed.years[0] if len(parsed.years) == 1 else None
    kept = in_pd_sections if by_pd else in_section
    rows = [r for r in indicators(parsed, context, view="quality") if kept(r, parsed)]
    pds = {r["pd_id"]: r["pd_status"] for r in rows}
    reached = [r["reached_in_year"] if year else r["reached"] for r in rows]
    with_target = sum(1 for r in rows if r["target"])
    return {
        "pd_count": len(pds),
        "active_pds": sum(1 for status in pds.values() if status in pd_statuses.ACTIVE),
        "ended_pds": sum(1 for status in pds.values() if status in pd_statuses.ENDED),
        "dq_total": len(rows),
        "dq_with_reached": sum(1 for value in reached if value),
        "dq_with_target": with_target,
        "dq_without_target": len(rows) - with_target,
    }


def missing_targets(parsed: Filter, context: pf.Context) -> list[dict[str, Any]]:
    """Data quality's "missing target" list (``FIGURE_TESTS["missing_target"]``: a target of 0 counts as
    none), of the indicators the page's section filter keeps."""
    return [
        r for r in indicators(parsed, context, view="quality") if in_section(r, parsed) and not r["target"]
    ]


def no_reached(parsed: Filter, context: pf.Context) -> list[dict[str, Any]]:
    """Data quality's "without or with zero Reached" list (``FIGURE_TESTS["no_reached"]``, with no cut), of
    the indicators the page's section filter keeps."""
    year = parsed.years[0] if len(parsed.years) == 1 else None
    out = []
    for row in indicators(parsed, context, view="quality"):
        value = row["reached_in_year"] if year else row["reached"]
        if (value is None or value == 0) and in_section(row, parsed):
            out.append(row)
    return out


# ------------------------------------------------------------------------------------------ PD/SPD Indicator
PD_INDICATOR_FIELDS = (
    "pd_id",
    "pd",
    "pd_status",
    "partner",
    "document_type",
    "pd_title",
    "start",
    "end",
    "offices",
    "section",
    "pd_sections",
    "cp_output",
    "pd_cp_outputs",
    "output",
    "title",
    "target",
    "ident",
)


def pd_indicators(parsed: Filter, context: pf.Context) -> list[dict[str, Any]]:
    """Programmes → Indicators (``reporting.pd_indicators`` on ``partnerships.programme_documents``) for Power
    BI's PD statuses, every section (the comparison reads the page's section filter itself)."""
    from neurodb.partnerships.services import PDFilters, programme_documents

    from . import reporting

    pds = programme_documents(PDFilters.from_params({"status": list(parsed.pd_statuses)}))
    held = pf.pds(context)
    out = []
    for entry in reporting.pd_indicators(pds, {}):
        pd = held.by_id.get(entry["pd"].id)
        out.append(
            shown(
                PD_INDICATOR_FIELDS,
                {
                    "pd_id": entry["pd"].id,
                    "pd": entry["programme_document"],
                    "pd_status": entry["pd"].status or "",
                    "partner": context.clean(entry["partner"]),
                    "document_type": entry["document_type"],
                    "pd_title": context.clean(entry["pd_title"]),
                    "start": entry["start"],
                    "end": entry["end"],
                    "offices": pm.sections(entry["office"]),
                    "section": entry["section"],
                    "pd_sections": pm.sections(pd.sections if pd else (), context.sections),
                    "cp_output": context.clean(entry["cp_output"]),
                    "pd_cp_outputs": [context.clean(c) for c in (pd.cp_outputs if pd else ())],
                    "output": context.clean(entry["pd_output"]),
                    "title": context.clean(entry["indicator"]),
                    "target": entry["target"],
                    "ident": entry["ident"],
                },
            )
        )
    return out


# ---------------------------------------------------------------------------------------------------- funds
def fund_filters(parsed: Filter):
    from .funds import FundFilters

    return dataclasses.replace(FundFilters(), pd_statuses=list(parsed.pd_statuses))


FUND_FIELDS = (
    "pd_id",
    "pd",
    "pd_status",
    "partner",
    "partner_type",
    "cso_type",
    "currency",
    "unicef_planned",
    "total",
    "partner_contribution",
    "partner_share",
    "financial_contribution",
)


def fund_monitoring(parsed: Filter, context: pf.Context) -> list[dict[str, Any]]:
    """Funds → Partner Fund Monitoring (``funds.partner_contributions``) for Power BI's PD statuses."""
    from . import funds

    out = []
    for row in funds.partner_contributions(fund_filters(parsed), context.day)["rows"]:
        reported = row["reported"] or {}
        out.append(
            shown(
                FUND_FIELDS,
                {
                    **{name: row[name] for name in FUND_FIELDS if name in row and name != "partner"},
                    "pd": row["programme_document"],
                    "pd_status": row["pd"].status or "",
                    "partner": context.clean(row["partner"]),
                    "financial_contribution": reported.get("value"),
                },
            )
        )
    return out


FUNDED_FIELDS = (
    "pd_id",
    "pd",
    "pd_status",
    "grant",
    "donor",
    "reserved",
    "start",
    "end",
    "days_to_end",
    "partner",
    "currency",
    "unicef_planned",
    "total",
    "disbursed",
    "partner_contribution",
    "first",
)


def funded_programmes(parsed: Filter, context: pf.Context) -> list[dict[str, Any]]:
    """Funds → Funded Programs (``funds.funded_programmes``): one row per PD and grant; ``first`` marks the
    row that carries the PD's own amounts."""
    from . import funds

    out = []
    for row in funds.funded_programmes(fund_filters(parsed), context.day)["rows"]:
        out.append(
            shown(
                FUNDED_FIELDS,
                {
                    **{
                        name: row[name]
                        for name in FUNDED_FIELDS
                        if name in row and name not in ("partner", "donor")
                    },
                    "pd": row["programme_document"],
                    "pd_status": row["pd"].status or "",
                    "partner": context.clean(row["partner"]),
                    "donor": context.clean(row["donor"]),
                    "first": row["pd_amounts"],
                },
            )
        )
    return out
