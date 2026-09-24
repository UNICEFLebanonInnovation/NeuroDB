"""Partner implementation monitoring from eTools: PD indicators, what partners reported on them in
the Partner Reporting Portal (per report, per location), and whether each is on track against the
target set in the programme document.

An indicator is identified by its programme document and its title: the Datamart lists PD
indicators once per location and disaggregation (``PDIndicator``), and the PRP data reports carry
the same title (``ReportedIndicator``, one row per report and location). Quarterly (QPR) and
monthly humanitarian (HR) reports describe the same achievements, so one report type is shown at a
time and their values are never added together. A report's value for the period is the sum of its
location rows (or their maximum or average, when the indicator says so); the cumulative progress
is the one the latest report carries.
"""

from __future__ import annotations

import datetime
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from django.db.models import Q, QuerySet
from django.urls import reverse

from neurodb.indicators.services.tracking import LABELS, NO_TARGET, tracking_between
from neurodb.partnerships.models import PCA, PartnerOrganization

from . import models as dm
from .tags import TAG_FIELDS

REPORT_TYPES = {"QPR": "Quarterly progress reports", "HR": "Humanitarian reports (monthly)"}
DEFAULT_REPORT_TYPE = "QPR"
ACTIVE_PD_STATUSES = ("active", "signed", "suspended")
SUBMITTED = ("submitted", "accepted", "sent back", "sen", "sub", "acc")
MONTHS = tuple(range(1, 13))
MAX_INDICATORS = 400  # rows the page renders; the filters narrow larger sets


def norm(title: str | None) -> str:
    return " ".join((title or "").lower().split())


def number(value: Any) -> float | None:
    """A PRP value as a number: ``"1,234"``, ``"12.5 %"``, ``Decimal`` or None."""
    if value in (None, ""):
        return None
    if isinstance(value, int | float | Decimal):
        return float(value)
    text = re.sub(r"[^0-9.\-]", "", str(value).replace(",", ""))
    try:
        return float(text) if text not in ("", "-", ".") else None
    except ValueError:
        return None


def combine(values: list[float], method: str) -> float | None:
    values = [v for v in values if v is not None]
    if not values:
        return None
    method = (method or "sum").lower()
    if method == "max":
        return max(values)
    if method == "avg":
        return sum(values) / len(values)
    return sum(values)


def status_key(text: str | None) -> str:
    return norm(text).replace(" ", "_")


# ------------------------------------------------------------------------------------ filters
@dataclass
class Filters:
    sections: list[str] = field(default_factory=list)
    partners: list[str] = field(default_factory=list)  # partner ids as text
    pds: list[str] = field(default_factory=list)  # PD reference numbers
    locations: list[str] = field(default_factory=list)
    report_type: str = DEFAULT_REPORT_TYPE
    year: int | None = None
    scope: str = "active"  # active | all programme documents
    status: str = ""  # on_track | off_track | over_target | no_target
    q: str = ""
    tags: dict[str, list[str]] = field(default_factory=dict)

    @classmethod
    def from_params(cls, params, today: datetime.date | None = None) -> Filters:
        today = today or datetime.date.today()
        getlist = params.getlist if hasattr(params, "getlist") else (lambda k: params.get(k, []) or [])
        year_raw = (params.get("year") or "").strip()
        report_type = (params.get("report_type") or DEFAULT_REPORT_TYPE).upper()
        return cls(
            sections=[x for x in getlist("section") if x],
            partners=[x for x in getlist("partner") if x],
            pds=[x for x in getlist("pd") if x],
            locations=[x for x in getlist("location") if x],
            report_type=report_type if report_type in REPORT_TYPES else DEFAULT_REPORT_TYPE,
            year=int(year_raw) if year_raw.isdigit() else today.year,
            scope="all" if params.get("scope") == "all" else "active",
            status=params.get("status") or "",
            q=(params.get("q") or "").strip(),
            tags={t: [x for x in getlist(t) if x] for t in TAG_FIELDS},
        )


def _pd_queryset(filters: Filters) -> QuerySet[PCA]:
    qs = PCA.objects.select_related("partner").exclude(status__in=("draft", "cancelled"))
    if filters.scope == "active":
        qs = qs.filter(status__in=ACTIVE_PD_STATUSES)
    if filters.partners:
        qs = qs.filter(partner_id__in=[int(p) for p in filters.partners if p.isdigit()])
    if filters.pds:
        qs = qs.filter(number__in=filters.pds)
    return qs


def _indicator_rows(filters: Filters, pd_ids: list[int]) -> QuerySet[dm.PDIndicator]:
    qs = dm.PDIndicator.objects.filter(intervention_id__in=pd_ids)
    if filters.sections:
        qs = qs.filter(section_name__in=filters.sections)
    if filters.q:
        qs = qs.filter(Q(title__icontains=filters.q) | Q(lower_result_name__icontains=filters.q))
    for tag, values in filters.tags.items():
        if values:
            qs = qs.filter(**{f"tag_{tag}__in": values})
    if filters.report_type == "HR":
        qs = qs.filter(Q(is_high_frequency=True) | ~Q(cluster_name=""))
    return qs


# ------------------------------------------------------------------------------------- the grid
@dataclass
class Indicator:
    key: str
    pd: PCA
    title: str
    output: str
    section: str
    unit: str
    display_type: str
    baseline: float | None
    target: float | None
    high_frequency: bool
    cluster: str
    tags: dict[str, str]
    locations: list[str] = field(default_factory=list)
    months: dict[int, float] = field(default_factory=dict)  # period-end month -> reported value
    reports: int = 0
    cumulative: float | None = None
    cumulative_as_of: datetime.date | None = None
    partner_status: str = ""
    report_status: str = ""
    tracking: str = NO_TARGET
    achieved: float | None = None

    @property
    def partner(self) -> PartnerOrganization | None:
        return self.pd.partner

    @property
    def tracking_label(self) -> str:
        return LABELS[self.tracking]

    @property
    def url(self) -> str:
        return reverse("reports:pd_indicator", args=[self.pd.id, self.key])

    def as_dict(self) -> dict[str, Any]:
        return {
            "programme_document": self.pd.number,
            "partner": self.partner.name if self.partner else self.pd.partner_name,
            "section": self.section,
            "output": self.output,
            "indicator": self.title,
            "tags": {k: v for k, v in self.tags.items() if v},
            "unit": self.unit,
            "baseline": self.baseline,
            "target": self.target,
            "cumulative": self.cumulative,
            "cumulative_as_of": self.cumulative_as_of,
            "achieved_percent": round(self.achieved, 1) if self.achieved is not None else None,
            "status": self.tracking,
            "partner_assessment": self.partner_status,
            "reports": self.reports,
            "by_month": {f"{m:02d}": v for m, v in sorted(self.months.items())},
            "locations": self.locations[:10],
            "url": self.url,
        }


def _target(row: dict[str, Any]) -> float | None:
    numerator = number(row["target_numerator"])
    denominator = number(row["target_denominator"])
    if row["display_type"] in ("percentage", "ratio") and numerator is not None and denominator:
        return (
            numerator / denominator * 100 if row["display_type"] == "percentage" else numerator / denominator
        )
    return numerator


def _key(source_id: int | None, datamart_id: int) -> str:
    return str(source_id) if source_id else f"r{datamart_id}"


def indicators(filters: Filters, today: datetime.date | None = None) -> list[Indicator]:
    """One entry per PD indicator, with the reported values of the selected report type and year."""
    today = today or datetime.date.today()
    pds = {pd.id: pd for pd in _pd_queryset(filters)}
    if not pds:
        return []
    rows = _indicator_rows(filters, list(pds)).values(
        "datamart_id",
        "source_id",
        "intervention_id",
        "title",
        "lower_result_name",
        "section_name",
        "unit",
        "display_type",
        "baseline_numerator",
        "target_numerator",
        "target_denominator",
        "is_high_frequency",
        "cluster_name",
        "location_name",
        *(f"tag_{t}" for t in TAG_FIELDS),
    )
    found: dict[tuple[int, str], Indicator] = {}
    for row in rows.order_by("lower_result_name", "title", "datamart_id"):
        ident = (row["intervention_id"], norm(row["title"]))
        entry = found.get(ident)
        if entry is None:
            entry = found[ident] = Indicator(
                key=_key(row["source_id"], row["datamart_id"]),
                pd=pds[row["intervention_id"]],
                title=row["title"],
                output=row["lower_result_name"],
                section=row["section_name"],
                unit=row["unit"],
                display_type=row["display_type"],
                baseline=number(row["baseline_numerator"]),
                target=_target(row),
                high_frequency=row["is_high_frequency"],
                cluster=row["cluster_name"],
                tags={t: row[f"tag_{t}"] for t in TAG_FIELDS},
            )
        if row["location_name"] and row["location_name"] not in entry.locations:
            entry.locations.append(row["location_name"])
    if filters.locations:
        wanted = {norm(x) for x in filters.locations}
        found = {k: e for k, e in found.items() if any(norm(loc) in wanted for loc in e.locations)}
    _attach_reports(found, filters, today)
    result = [e for e in found.values() if not filters.status or e.tracking == filters.status]
    return result[:MAX_INDICATORS]


def _report_rows(pd_ids: list[int], filters: Filters) -> QuerySet[dm.ReportedIndicator]:
    qs = dm.ReportedIndicator.objects.filter(intervention_id__in=pd_ids, report_type=filters.report_type)
    if filters.locations:
        q = Q()
        for loc in filters.locations:
            q |= Q(location__iexact=loc)
        qs = qs.filter(q)
    return qs


def _attach_reports(found: dict[tuple[int, str], Indicator], filters: Filters, today: datetime.date) -> None:
    if not found:
        return
    pd_ids = list({ident[0] for ident in found})
    rows = _report_rows(pd_ids, filters).values(
        "intervention_id",
        "indicator",
        "progress_report",
        "period_end",
        "achievement_in_period",
        "total_cumulative_progress",
        "calculation_across_locations",
        "report_status",
        "pd_output_progress_status",
    )
    # per indicator, per report: the location values and the report-level fields
    per_report: dict[tuple[int, str], dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in rows:
        ident = (row["intervention_id"], norm(row["indicator"]))
        if ident not in found:
            continue
        report = per_report[ident].setdefault(
            row["progress_report"] or "",
            {
                "period_end": row["period_end"],
                "values": [],
                "cumulative": None,
                "status": "",
                "partner_status": "",
                "method": "",
            },
        )
        report["values"].append(number(row["achievement_in_period"]))
        report["method"] = report["method"] or row["calculation_across_locations"]
        cumulative = number(row["total_cumulative_progress"])
        if cumulative is not None:
            report["cumulative"] = cumulative
        report["status"] = report["status"] or row["report_status"]
        report["partner_status"] = report["partner_status"] or row["pd_output_progress_status"]
    for ident, reports in per_report.items():
        entry = found[ident]
        ordered = sorted(reports.values(), key=lambda r: r["period_end"] or datetime.date.min)
        entry.reports = len(ordered)
        for report in ordered:
            value = combine(report["values"], report["method"])
            if report["period_end"] and report["period_end"].year == filters.year and value is not None:
                entry.months[report["period_end"].month] = (
                    entry.months.get(report["period_end"].month, 0) + value
                )
        latest = ordered[-1]
        entry.cumulative_as_of = latest["period_end"]
        entry.report_status = latest["status"]
        entry.partner_status = latest["partner_status"]
        with_cumulative = [r for r in ordered if r["cumulative"] is not None]
        if with_cumulative:
            entry.cumulative = with_cumulative[-1]["cumulative"]
        else:  # no cumulative from PRP: add the report values up (the usual "sum" across periods)
            totals = [combine(r["values"], r["method"]) for r in ordered]
            entry.cumulative = combine([t for t in totals if t is not None], "sum")
    for entry in found.values():
        result = tracking_between(entry.cumulative, entry.target, entry.pd.start, entry.pd.end, today)
        entry.tracking, entry.achieved = result.status, result.achieved


# ------------------------------------------------------------------------------------- summaries
def summary(rows: list[Indicator], filters: Filters, today: datetime.date | None = None) -> dict[str, Any]:
    today = today or datetime.date.today()
    counts = Counter(r.tracking for r in rows)
    pd_ids = {r.pd.id for r in rows}
    overdue = (
        dm.ReportedIndicator.objects.filter(
            intervention_id__in=pd_ids,
            report_type=filters.report_type,
            submission_date=None,
            due_date__lt=today,
        )
        .values("progress_report")
        .distinct()
        .count()
    )
    return {
        "indicators": len(rows),
        "programme_documents": len(pd_ids),
        "partners": len({r.pd.partner_id for r in rows if r.pd.partner_id}),
        "with_target": sum(1 for r in rows if r.target),
        "reported": sum(1 for r in rows if r.reports),
        "status_counts": {key: counts.get(key, 0) for key in LABELS},
        "overdue_reports": overdue,
        "by_section": Counter(r.section or "—" for r in rows).most_common(),
    }


def grouped(rows: list[Indicator]) -> list[dict[str, Any]]:
    """Section -> programme document -> output -> indicators, the way the sector databases read."""
    sections: dict[str, dict[str, Any]] = {}
    for row in rows:
        section = sections.setdefault(
            row.section or "—", {"section": row.section or "—", "pds": {}, "count": 0}
        )
        section["count"] += 1
        pd = section["pds"].setdefault(row.pd.id, {"pd": row.pd, "outputs": {}, "count": 0})
        pd["count"] += 1
        output = pd["outputs"].setdefault(row.output or "—", {"output": row.output or "—", "indicators": []})
        output["indicators"].append(row)
    return [
        {**s, "pds": [{**p, "outputs": list(p["outputs"].values())} for p in s["pds"].values()]}
        for s in sorted(sections.values(), key=lambda s: s["section"])
    ]


def filter_options(filters: Filters) -> dict[str, Any]:
    base = dm.PDIndicator.objects.exclude(intervention=None)
    pds = (
        PCA.objects.exclude(status__in=("draft", "cancelled")).filter(pd_indicators__isnull=False).distinct()
    )
    partners = (
        PartnerOrganization.objects.filter(interventions__in=pds)
        .distinct()
        .order_by("name")
        .values_list("id", "name")
    )
    years = sorted(
        {
            d.year
            for d in dm.ReportedIndicator.objects.exclude(period_end=None).values_list(
                "period_end", flat=True
            )
        },
        reverse=True,
    )
    options = {
        "sections": sorted(x for x in base.values_list("section_name", flat=True).distinct() if x),
        "partners": [{"value": str(pk), "label": name} for pk, name in partners],
        "pds": sorted(x for x in pds.values_list("number", flat=True) if x),
        "locations": sorted(x for x in base.values_list("location_name", flat=True).distinct() if x),
        "years": years or [filters.year],
        "report_types": REPORT_TYPES,
    }
    for tag in TAG_FIELDS:
        options[tag] = sorted(x for x in base.values_list(f"tag_{tag}", flat=True).distinct() if x)
    return options


# -------------------------------------------------------------------------------------- detail
def indicator_detail(
    pd: PCA, key: str, report_type: str, year: int, today: datetime.date | None = None
) -> dict[str, Any] | None:
    """One indicator of a PD: its definition, locations x periods, per-location cumulative and reports."""
    today = today or datetime.date.today()
    rows = dm.PDIndicator.objects.filter(intervention=pd)
    rows = rows.filter(datamart_id=int(key[1:])) if key.startswith("r") else rows.filter(source_id=int(key))
    first = rows.order_by("datamart_id").first()
    if first is None:
        return None
    filters = Filters(
        report_type=report_type if report_type in REPORT_TYPES else DEFAULT_REPORT_TYPE,
        year=year,
        scope="all",
    )
    same = dm.PDIndicator.objects.filter(intervention=pd, title=first.title)
    indicator = next(
        (
            i
            for i in indicators(
                Filters(pds=[pd.number or ""], scope="all", report_type=filters.report_type, year=year), today
            )
            if i.key == key
        ),
        None,
    )
    reports_qs = dm.ReportedIndicator.objects.filter(intervention=pd, indicator__iexact=first.title)
    periods: dict[str, dict[str, Any]] = {}
    by_location: dict[str, dict[str, Any]] = {}
    for row in reports_qs.filter(report_type=filters.report_type).order_by("period_end"):
        period = periods.setdefault(
            row.progress_report,
            {
                "key": row.progress_report,
                "report": row.report_number or row.progress_report,
                "start": row.period_start,
                "end": row.period_end,
                "status": row.report_status,
                "due": row.due_date,
                "submitted": row.submission_date,
                "total": 0.0,
            },
        )
        value = number(row.achievement_in_period)
        loc = by_location.setdefault(
            row.location or "—", {"location": row.location or "—", "cells": {}, "cumulative": None}
        )
        if value is not None:
            loc["cells"][row.progress_report] = value
            period["total"] += value
        cumulative = number(row.total_cumulative_progress_in_location)
        if cumulative is not None:
            loc["cumulative"] = cumulative
    all_reports = reports_qs.values(
        "progress_report",
        "report_type",
        "report_number",
        "period_start",
        "period_end",
        "due_date",
        "submission_date",
        "report_status",
        "pd_output_progress_status",
        "narrative_assessment",
        "total_cumulative_progress",
    ).order_by("-period_end", "report_type")
    seen, reports = set(), []
    for r in all_reports:
        if r["progress_report"] in seen:
            continue
        seen.add(r["progress_report"])
        reports.append(r)
    return {
        "pd": pd,
        "indicator": indicator,
        "definition": first,
        "disaggregations": sorted({x for x in same.values_list("disaggregation_name", flat=True) if x}),
        "planned_locations": sorted({x for x in same.values_list("location_name", flat=True) if x}),
        "periods": list(periods.values()),
        "by_location": sorted(by_location.values(), key=lambda loc: loc["location"]),
        "reports": reports,
        "report_type": filters.report_type,
        "year": year,
    }
