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

import ast
import datetime
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from django.db.models import Q, QuerySet
from django.db.models.functions import Trim
from django.urls import reverse

from neurodb.geo.models import Location
from neurodb.indicators.services import tracking
from neurodb.indicators.services.tracking import NO_TARGET, tracking_between
from neurodb.partnerships.models import PCA, PartnerOrganization

from . import models as dm
from .tags import TAG_FIELDS

REPORT_TYPES = {"QPR": "Quarterly progress reports", "HR": "Humanitarian reports (monthly)"}
DEFAULT_REPORT_TYPE = "QPR"
ACTIVE_PD_STATUSES = ("active", "signed", "suspended")
SUBMITTED = ("submitted", "accepted", "sent back", "sen", "sub", "acc")
MONTHS = tuple(range(1, 13))
NOT_REPORTED = "not_reported"  # no progress report read for the indicator: never "off track"
LABELS = {**tracking.LABELS, NOT_REPORTED: "Not reported"}
MAX_INDICATORS = 2000  # rows a request computes; the page shows PAGE_SIZE of them at a time
PAGE_SIZE = 100


def norm(title: str | None) -> str:
    return " ".join((title or "").lower().split())


def location_key(p_code: str | None, name: str | None) -> str:
    """The P-code when eTools gives one (the authoritative identity), else the normalised name."""
    return (p_code or "").strip().upper() or f"name:{norm(name)}"


NUMBER = re.compile(r"[-+]?\d+(?:\.\d+)?")


def _from_parts(parts: dict) -> float | None:
    """PRP stores a value as ``{"v": numerator, "d": denominator, "c": calculated}``: the calculated
    value when there is one, else the numerator over a real denominator, else the numerator."""
    for key in ("c", "v"):
        if parts.get(key) not in (None, ""):
            value = number(parts[key])
            if key == "v" and value is not None:
                d = number(parts.get("d"))
                if d not in (None, 0.0, 1.0):
                    return value / d
            return value
    return None


def number(value: Any) -> float | None:
    """A PRP value as a number: ``1234``, ``"1,234"``, ``"12.5 %"``, ``Decimal``, or PRP's
    ``{"v": .., "d": .., "c": ..}`` (as a dict or as its text). None when the text holds no number
    or several (``"45/100"``, a date): the digits of separate numbers are never glued into one,
    which once turned a structured value into billions of children."""
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float | Decimal):
        return float(value)
    if isinstance(value, dict):
        return _from_parts(value)
    text = str(value).strip()
    if text.startswith("{"):
        try:
            parts = ast.literal_eval(text)
        except (ValueError, SyntaxError):
            parts = None
        return _from_parts(parts) if isinstance(parts, dict) else None
    found = NUMBER.findall(text.replace(",", ""))
    return float(found[0]) if len(found) == 1 else None


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


def year_param(raw: Any) -> int | None:
    """A ``?year=`` value as a calendar year, or None: one to four ASCII digits, 1 to 9999 (a
    date cannot hold a larger year, so a bigger one would fail every date filter)."""
    text = str(raw or "").strip()
    if not re.fullmatch(r"[0-9]{1,4}", text) or int(text) < 1:
        return None
    return int(text)


# ------------------------------------------------------------------------------------ filters
@dataclass
class Filters:
    sections: list[str] = field(default_factory=list)
    partners: list[str] = field(default_factory=list)  # partner ids as text
    pds: list[str] = field(default_factory=list)  # PD reference numbers
    pd_ids: list[int] = field(default_factory=list)  # PD ids (the detail page: a PD may have no number)
    locations: list[str] = field(default_factory=list)
    report_type: str = DEFAULT_REPORT_TYPE
    year: int | None = None
    scope: str = "active"  # active | all programme documents | year (every PD running in ``year``)
    status: str = ""  # on_track | off_track | over_target | no_target
    q: str = ""
    tags: dict[str, list[str]] = field(default_factory=dict)

    @classmethod
    def from_params(cls, params, today: datetime.date | None = None) -> Filters:
        today = today or datetime.date.today()
        getlist = params.getlist if hasattr(params, "getlist") else (lambda k: params.get(k, []) or [])
        report_type = (params.get("report_type") or DEFAULT_REPORT_TYPE).upper()
        return cls(
            sections=[x for x in getlist("section") if x],
            partners=[x for x in getlist("partner") if x],
            pds=[x for x in getlist("pd") if x],
            locations=[x for x in getlist("location") if x],
            report_type=report_type if report_type in REPORT_TYPES else DEFAULT_REPORT_TYPE,
            year=year_param(params.get("year")) or today.year,
            scope=params.get("scope") if params.get("scope") in ("all", "year") else "active",
            status=params.get("status") or "",
            q=(params.get("q") or "").strip(),
            tags={t: [x for x in getlist(t) if x] for t in TAG_FIELDS},
        )


def default_sections(user, options: list[str]) -> list[str]:
    """The eTools section name(s) matching the user's NeuroDB section, for the page's first load.

    A PD manager sees their own section first; the filter bar still offers every section. Names
    match case-insensitively, whole or contained either way ("WASH" ~ "WASH / Water, Sanitation
    and Hygiene"), or by the section code.
    """
    section = getattr(user, "section", None)
    if section is None:
        return []
    wanted = {norm(section.name), norm(section.code or "")} - {""}
    exact = [o for o in options if norm(o) in wanted]
    if exact:
        return exact
    return [o for o in options if any(w in norm(o) or norm(o) in w for w in wanted if len(w) >= 3)]


def _pd_queryset(filters: Filters) -> QuerySet[PCA]:
    qs = PCA.objects.select_related("partner").exclude(status__in=("draft", "cancelled"))
    if filters.scope == "active":
        qs = qs.filter(status__in=ACTIVE_PD_STATUSES)
    elif filters.scope == "year" and filters.year:  # every PD running in the year, closed ones too
        first, last = datetime.date(filters.year, 1, 1), datetime.date(filters.year, 12, 31)
        qs = qs.filter(Q(start__isnull=True) | Q(start__lte=last), Q(end__isnull=True) | Q(end__gte=first))
    if filters.partners:
        qs = qs.filter(partner_id__in=[int(p) for p in filters.partners if re.fullmatch(r"[0-9]{1,18}", p)])
    if filters.pds:
        qs = qs.filter(number__in=filters.pds)
    if filters.pd_ids:
        qs = qs.filter(id__in=filters.pd_ids)
    return qs


def _indicator_rows(filters: Filters, pd_ids: list[int]) -> QuerySet[dm.PDIndicator]:
    qs = dm.PDIndicator.objects.filter(intervention_id__in=pd_ids)
    if filters.sections:  # trimmed: rows synced before names were trimmed may end with a space
        wanted = [s.strip() for s in filters.sections]
        qs = qs.annotate(_section=Trim("section_name")).filter(_section__in=wanted)
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
    planned: dict[str, dict[str, Any]] = field(default_factory=dict)  # location key -> {name, p_code, id}
    by_location: dict[str, dict[str, Any]] = field(default_factory=dict)  # location key -> reported
    months: dict[int, float] = field(default_factory=dict)  # period-end month -> reported value
    reports: int = 0
    cumulative: float | None = None
    cumulative_as_of: datetime.date | None = None
    partner_status: str = ""
    report_status: str = ""
    tracking: str = NO_TARGET
    achieved: float | None = None
    method: str = ""  # how PRP combines the locations of a period: sum (or empty), max, avg

    @property
    def partner(self) -> PartnerOrganization | None:
        return self.pd.partner

    @property
    def tracking_label(self) -> str:
        return (
            LABELS[self.tracking]
            if self.tracking == NOT_REPORTED
            else tracking.label_for(self.tracking, self.achieved)
        )

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
        "location_pcode",
        "location_id",
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
                section=(row["section_name"] or "").strip(),
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
        if row["location_name"] or row["location_pcode"]:
            entry.planned.setdefault(
                location_key(row["location_pcode"], row["location_name"]),
                {
                    "name": row["location_name"],
                    "p_code": row["location_pcode"] or "",
                    "id": row["location_id"],
                },
            )
    if filters.locations:
        wanted = {norm(x) for x in filters.locations}
        found = {k: e for k, e in found.items() if any(norm(loc) in wanted for loc in e.locations)}
    _attach_reports(found, filters, today)
    result = [e for e in found.values() if not filters.status or e.tracking == filters.status]
    return result[:MAX_INDICATORS]


def _report_rows(pd_ids: list[int], filters: Filters) -> QuerySet[dm.ReportedIndicator]:
    # reports after the selected year change neither its months nor its cumulative
    qs = dm.ReportedIndicator.objects.filter(intervention_id__in=pd_ids, report_type=filters.report_type)
    if filters.year:
        qs = qs.filter(Q(period_end=None) | Q(period_end__year__lte=filters.year))
    if filters.locations:
        q = Q()
        for loc in filters.locations:
            q |= Q(location__iexact=loc)
        qs = qs.filter(q)
    # a fixed reading order: the "first non-empty" report fields never depend on the query plan
    return qs.order_by("period_end", "progress_report", "location", "p_code", "datamart_id")


def _by_source(pd_ids: list[int]) -> dict[tuple[int, str], tuple[int, str]]:
    """(PD, eTools indicator id) -> the PD indicator (PD, title) it identifies, over every indicator
    of the PDs, not only those the filters keep: a report row of a filtered-out indicator is then
    skipped instead of falling back to another indicator's title."""
    out: dict[tuple[int, str], tuple[int, str]] = {}
    rows = dm.PDIndicator.objects.filter(intervention_id__in=pd_ids).exclude(source_id=None)
    for pd_id, source_id, title in rows.order_by("lower_result_name", "title", "datamart_id").values_list(
        "intervention_id", "source_id", "title"
    ):
        if source_id:
            out.setdefault((pd_id, str(source_id)), (pd_id, norm(title)))
    return out


def _ident(
    by_source: dict[tuple[int, str], tuple[int, str]], pd_id: int, etools_id: Any, title: str | None
) -> tuple[int, str]:
    """The PD indicator a report row belongs to: by its eTools indicator id, else by its title."""
    source = str(etools_id or "").strip()
    return (by_source.get((pd_id, source)) if source else None) or (pd_id, norm(title))


def _later(holder: dict[str, Any], order: tuple, value: float | None) -> None:
    """Keep in ``holder["cumulative"]`` the value of the latest report (``order`` = period end,
    report id) that carries one; rows of the same report keep the largest, whatever their order."""
    if value is None:
        return
    if holder.get("_order") is None or order > holder["_order"]:
        holder["_order"], holder["cumulative"] = order, value
    elif order == holder["_order"]:
        holder["cumulative"] = max(holder["cumulative"], value)


def _attach_reports(found: dict[tuple[int, str], Indicator], filters: Filters, today: datetime.date) -> None:
    """Reports match their PD indicator by the eTools indicator id when the Datamart gives one,
    else by title within the PD; an indicator with no report read is "not reported", never off track.

    An indicator's figures depend only on its own report rows, never on the order they are read in
    or on the other indicators and PDs of the query: a report's cumulative is the largest its
    location rows carry (PRP repeats the same report total on each of them)."""
    if not found:
        return
    pd_ids = list({ident[0] for ident in found})
    by_source = _by_source(pd_ids)
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
        "location",
        "p_code",
        "location_ref_id",
        "total_cumulative_progress_in_location",
        "etools_indicator_id",
    )
    # per indicator, per report: the location values and the report-level fields
    per_report: dict[tuple[int, str], dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in rows:
        ident = _ident(by_source, row["intervention_id"], row["etools_indicator_id"], row["indicator"])
        if ident not in found:
            continue
        report_key = row["progress_report"] or ""
        report = per_report[ident].setdefault(
            report_key,
            {
                "period_end": row["period_end"],
                "values": [],
                "cumulative": None,
                "status": "",
                "partner_status": "",
                "method": "",
            },
        )
        value = number(row["achievement_in_period"])
        report["values"].append(value)
        report["method"] = report["method"] or row["calculation_across_locations"]
        end = row["period_end"]
        if row["location"] or row["p_code"]:
            place = found[ident].by_location.setdefault(
                location_key(row["p_code"], row["location"]),
                {
                    "name": row["location"] or "",
                    "p_code": row["p_code"] or "",
                    "id": row["location_ref_id"],
                    "achieved": None,
                    "cumulative": None,
                    "period": None,
                    "report": "",
                    "months": {},
                },
            )
            if place["id"] is None:
                place["id"] = row["location_ref_id"]
            if value is not None and end and end.year == filters.year:
                place["achieved"] = (place["achieved"] or 0) + value
                place["months"][end.month] = place["months"].get(end.month, 0) + value
            if end and (place["period"] is None or (end, report_key) > (place["period"], place["report"])):
                place["period"], place["report"] = end, report_key
            if end:
                _later(place, (end, report_key), number(row["total_cumulative_progress_in_location"]))
        cumulative = number(row["total_cumulative_progress"])
        if cumulative is not None:
            report["cumulative"] = (
                cumulative if report["cumulative"] is None else max(report["cumulative"], cumulative)
            )
        report["status"] = report["status"] or row["report_status"]
        report["partner_status"] = report["partner_status"] or row["pd_output_progress_status"]
    for ident, reports in per_report.items():
        entry = found[ident]
        for place in entry.by_location.values():
            place.pop("_order", None)
        # by period end, then report id: two reports ending the same day always read the same way
        ordered = [
            r
            for _, r in sorted(
                reports.items(), key=lambda kv: (kv[1]["period_end"] or datetime.date.min, kv[0])
            )
        ]
        entry.reports = len(ordered)
        for report in ordered:
            value = combine(report["values"], report["method"])
            if report["period_end"] and report["period_end"].year == filters.year and value is not None:
                entry.months[report["period_end"].month] = (
                    entry.months.get(report["period_end"].month, 0) + value
                )
        latest = ordered[-1]
        entry.method = (latest["method"] or "").strip().lower()
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
        if not entry.reports:
            entry.tracking, entry.achieved = NOT_REPORTED, None
            continue
        result = tracking_between(entry.cumulative, entry.target, entry.pd.start, entry.pd.end, today)
        entry.tracking, entry.achieved = result.status, result.achieved


# ------------------------------------------------------------------------------------------ map
LEVEL_GOVERNORATE, LEVEL_DISTRICT = 1, 2  # eTools admin levels in Lebanon (0 = country)


def _gazetteer(ids: set[int]) -> dict[int, dict[str, Any]]:
    """The locations ``ids`` and every ancestor, as plain dicts (one query per level of the tree)."""
    found: dict[int, dict[str, Any]] = {}
    pending = {i for i in ids if i}
    while pending:
        rows = Location.objects.filter(pk__in=pending).values(
            "id", "name", "p_code", "latitude", "longitude", "parent_id", "type__admin_level", "type__name"
        )
        pending = set()
        for r in rows:
            found[r["id"]] = r
            if r["parent_id"] and r["parent_id"] not in found:
                pending.add(r["parent_id"])
    return found


def _place(location_id: int | None, gazetteer: dict[int, dict[str, Any]]) -> dict[str, Any]:
    """Coordinates and hierarchy of a location: its own point, else the nearest ancestor's."""
    out: dict[str, Any] = {
        "admin_level": None,
        "level_name": "",
        "governorate": "",
        "district": "",
        "latitude": None,
        "longitude": None,
        "approximate": False,
        "located_by": "",
    }
    row = gazetteer.get(location_id) if location_id else None
    if row is None:
        return out
    out["name"] = row["name"]  # the gazetteer's spelling, not the indicator's
    out["admin_level"], out["level_name"] = row["type__admin_level"], row["type__name"] or ""
    node, hops = row, 0
    while node is not None and hops < 8:
        if node["type__admin_level"] == LEVEL_GOVERNORATE:
            out["governorate"] = node["name"]
        if node["type__admin_level"] == LEVEL_DISTRICT:
            out["district"] = node["name"]
        if out["latitude"] is None and node["latitude"] is not None and node["longitude"] is not None:
            out["latitude"], out["longitude"] = node["latitude"], node["longitude"]
            out["approximate"], out["located_by"] = node is not row, node["name"]
        node, hops = gazetteer.get(node["parent_id"]) if node["parent_id"] else None, hops + 1
    return out


def _funding(pd: PCA) -> dict[str, Any]:
    return {
        "id": pd.id,
        "number": pd.number,
        "title": pd.title,
        "status": pd.status,
        "start": pd.start,
        "end": pd.end,
        "partner": pd.partner.name if pd.partner else pd.partner_name,
        "partner_id": pd.partner_id,
        "url": reverse("reports:programme_detail", args=[pd.id]),
        "total_budget": number(pd.total_budget),
        "unicef_cash": number(pd.unicef_cash),
        "partner_contribution": number(pd.cso_contribution),
        "currency": pd.budget_currency or "",
        "donors": list(pd.donors or [])[:8],
        "agreement": pd.agreement.agreement_number if pd.agreement_id and pd.agreement else "",
    }


def _monitoring_at(pd_ids: set[int], location_ids: set[int]) -> dict[int, dict[str, int]]:
    """TPM activities, field monitoring findings and open action points per location, for these PDs."""
    counts: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    if not location_ids:
        return counts
    tpm = dm.TPMActivity.location_links.through.objects.filter(
        location_id__in=location_ids, tpmactivity__intervention_id__in=pd_ids
    ).values_list("location_id", flat=True)
    for loc in tpm:
        counts[loc]["tpm_activities"] += 1
    findings = dm.MonitoringFinding.objects.filter(location_id__in=location_ids).filter(
        Q(partner__interventions__in=pd_ids) | Q(partner=None)
    )
    # one per finding: the partner join repeats a finding once per PD, and the model's default
    # ordering (end date) must not enter the DISTINCT
    for _id, loc, rating in (
        findings.order_by().values_list("id", "location_id", "overall_finding_rating").distinct()
    ):
        counts[loc]["findings"] += 1
        if "off" in (rating or "").lower():
            counts[loc]["findings_off_track"] += 1
    points = dm.ActionPoint.objects.filter(location_id__in=location_ids, intervention_id__in=pd_ids)
    for loc, status in points.values_list("location_id", "status"):
        counts[loc]["action_points"] += 1
        if status in dm.ActionPoint.OPEN_STATUSES:
            counts[loc]["action_points_open"] += 1
    return counts


def map_points(filters: Filters, today: datetime.date | None = None) -> dict[str, Any]:
    """Every implementation location of the filtered indicators, with what is planned and reported
    there: partner, PD (and its funding), indicator, target, achieved, status, period, and the
    monitoring done at the place. A location is placed by its own eTools coordinates, else by the
    nearest ancestor with coordinates (flagged approximate); names alone never place anything."""
    rows = indicators(filters, today)
    places: dict[str, dict[str, Any]] = {}
    pds: dict[int, dict[str, Any]] = {}
    for ind in rows:
        pds.setdefault(ind.pd.id, _funding(ind.pd))
        keys = set(ind.planned) | set(ind.by_location)
        for key in keys:
            planned, reported = ind.planned.get(key), ind.by_location.get(key)
            meta = planned or reported or {}
            place = places.setdefault(
                key,
                {
                    "key": key,
                    "name": meta.get("name") or "",
                    "p_code": meta.get("p_code") or "",
                    "id": meta.get("id"),
                    "rows": [],
                },
            )
            if not place["id"]:
                place["id"] = (planned or {}).get("id") or (reported or {}).get("id")
            if not place["name"]:
                place["name"] = meta.get("name") or ""
            place["rows"].append(
                {
                    "pd_id": ind.pd.id,
                    "pd": ind.pd.number,
                    "partner": ind.partner.name if ind.partner else ind.pd.partner_name,
                    "partner_id": ind.pd.partner_id,
                    "indicator": ind.title,
                    "key": ind.key,
                    "url": ind.url,
                    "section": ind.section,
                    "output": ind.output,
                    "target": ind.target,
                    "achieved_here": reported["achieved"] if reported else None,
                    "cumulative_here": reported["cumulative"] if reported else None,
                    "cumulative": ind.cumulative,
                    "achieved_percent": round(ind.achieved, 1) if ind.achieved is not None else None,
                    "tracking": ind.tracking,
                    "tracking_label": ind.tracking_label,
                    "period": reported["period"] if reported else None,
                    "report": reported["report"] if reported else "",
                    "planned": planned is not None,
                    "reported": reported is not None,
                    "high_frequency": ind.high_frequency,
                }
            )
    # rows synced before their location reached the gazetteer: resolve the P-code now
    unresolved = {p["p_code"].upper() for p in places.values() if not p["id"] and p["p_code"]}
    if unresolved:
        by_pcode = {
            (code or "").upper(): pk
            for pk, code in Location.objects.filter(p_code__in=unresolved).values_list("pk", "p_code")
        }
        for place in places.values():
            if not place["id"] and place["p_code"]:
                place["id"] = by_pcode.get(place["p_code"].upper())
    gazetteer = _gazetteer({p["id"] for p in places.values() if p["id"]})
    monitoring = _monitoring_at({pd for pd in pds}, {p["id"] for p in places.values() if p["id"]})
    points, unlocated = [], []
    order = {"off_track": 0, "on_track": 1, "over_target": 2, "no_target": 3, NOT_REPORTED: 4}
    for place in places.values():
        counts = Counter(r["tracking"] for r in place["rows"])
        place.update(_place(place["id"], gazetteer))
        place.update(
            {
                "partners": len({r["partner_id"] or r["partner"] for r in place["rows"]}),
                "pds": len({r["pd_id"] for r in place["rows"]}),
                "indicators": len(place["rows"]),
                "reported": sum(1 for r in place["rows"] if r["reported"]),
                "status_counts": {k: counts.get(k, 0) for k in LABELS},
                "worst": min((r["tracking"] for r in place["rows"]), key=lambda t: order.get(t, 9)),
                "monitoring": dict(monitoring.get(place["id"], {})) if place["id"] else {},
            }
        )
        place["rows"].sort(key=lambda r: (r["partner"] or "", r["pd"] or "", r["indicator"]))
        (points if place["latitude"] is not None else unlocated).append(place)
    points.sort(key=lambda p: -p["indicators"])
    unlocated.sort(key=lambda p: -p["indicators"])
    return {
        "points": points,
        "unlocated": unlocated,
        "pds": pds,
        "year": filters.year,  # the indicator links open the detail for the same year and report type
        "report_type": filters.report_type,
        "totals": {
            "locations": len(places),
            "located": len(points),
            "approximate": sum(1 for p in points if p["approximate"]),
            "indicators": len(rows),
            # counted as on the grid: a PD with no linked partner adds none
            "partners": len({r.pd.partner_id for r in rows if r.pd.partner_id}),
            "programme_documents": len(pds),
            "status_counts": dict(Counter(r.tracking for r in rows)),
        },
    }


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
        "sections": sorted({(x or "").strip() for x in base.values_list("section_name", flat=True)} - {""}),
        "partners": [{"value": str(pk), "label": name} for pk, name in partners],
        "pds": sorted({x for x in pds.values_list("number", flat=True) if x}),
        "locations": sorted({x for x in base.values_list("location_name", flat=True) if x}),
        "years": years or [filters.year],
        "report_types": REPORT_TYPES,
    }
    for tag in TAG_FIELDS:
        options[tag] = sorted(x for x in base.order_by().values_list(f"tag_{tag}", flat=True).distinct() if x)
    return options


# -------------------------------------------------------------------------------------- detail
def indicator_detail(
    pd: PCA, key: str, report_type: str, year: int, today: datetime.date | None = None
) -> dict[str, Any] | None:
    """One indicator of a PD: its definition, locations x periods, per-location cumulative and reports.

    The report rows are matched and bounded by year as on the grid (reports ending after ``year``
    are left out), so the matrix, the Reports table and the KPIs describe the same reports."""
    today = today or datetime.date.today()
    if not re.fullmatch(r"r?[0-9]{1,18}", key or ""):
        return None
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
    ident = (pd.id, norm(first.title))
    same = dm.PDIndicator.objects.filter(intervention=pd, title=first.title)
    indicator = next(
        (
            i
            for i in indicators(
                Filters(pd_ids=[pd.id], scope="all", report_type=filters.report_type, year=year), today
            )
            if norm(i.title) == ident[1]
        ),
        None,
    )
    by_source = _by_source([pd.id])
    reports_qs = dm.ReportedIndicator.objects.filter(intervention=pd)
    if year:
        reports_qs = reports_qs.filter(Q(period_end=None) | Q(period_end__year__lte=year))
    mine = [
        row
        for row in reports_qs.values(
            "datamart_id",
            "indicator",
            "etools_indicator_id",
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
            "achievement_in_period",
            "calculation_across_locations",
            "location",
            "p_code",
            "location_ref_id",
            "total_cumulative_progress_in_location",
        ).order_by("period_end", "progress_report", "location", "p_code", "datamart_id")
        if _ident(by_source, pd.id, row["etools_indicator_id"], row["indicator"]) == ident
    ]
    periods: dict[str, dict[str, Any]] = {}
    by_location: dict[str, dict[str, Any]] = {}
    for row in mine:
        if row["report_type"] != filters.report_type:
            continue
        report_key = row["progress_report"] or ""
        period = periods.setdefault(
            report_key,
            {
                "key": report_key,
                "report": row["report_number"] or report_key,
                "start": row["period_start"],
                "end": row["period_end"],
                "status": row["report_status"],
                "due": row["due_date"],
                "submitted": row["submission_date"],
                "method": "",
                "values": [],
            },
        )
        period["method"] = period["method"] or row["calculation_across_locations"]
        value = number(row["achievement_in_period"])
        period["values"].append(value)
        loc = by_location.setdefault(
            location_key(row["p_code"], row["location"]),
            {
                "location": row["location"] or "—",
                "p_code": row["p_code"] or "",
                "location_id": row["location_ref_id"],
                "values": defaultdict(list),
                "cumulative": None,
            },
        )
        if loc["location_id"] is None:
            loc["location_id"] = row["location_ref_id"]
        if value is not None:
            loc["values"][report_key].append(value)
        order = (row["period_end"] or datetime.date.min, report_key)
        _later(loc, order, number(row["total_cumulative_progress_in_location"]))
    # a report's cells and its "All locations" total combine the rows the way PRP says (sum, max
    # or average across locations), as the grid's month does; several rows of one place add up
    # the same way instead of the last one hiding the others
    for period in periods.values():
        period["total"] = combine(period["values"], period["method"])
    for loc in by_location.values():
        loc.pop("_order", None)
        loc["cells"] = {
            report_key: combine(values, periods[report_key]["method"])
            for report_key, values in loc.pop("values").items()
        }
    reports: dict[str, dict[str, Any]] = {}
    for row in mine:
        report = reports.setdefault(row["progress_report"] or "", {**row, "total_cumulative_progress": None})
        cumulative = number(row["total_cumulative_progress"])
        if cumulative is not None:  # the report total: the largest its rows carry, as for the KPI
            current = report["total_cumulative_progress"]
            report["total_cumulative_progress"] = cumulative if current is None else max(current, cumulative)
    ordered_reports = sorted(reports.values(), key=lambda r: (r["report_type"], r["progress_report"]))
    ordered_reports.sort(key=lambda r: r["period_end"] or datetime.date.min, reverse=True)
    return {
        "pd": pd,
        "indicator": indicator,
        "definition": first,
        "disaggregations": sorted({x for x in same.values_list("disaggregation_name", flat=True) if x}),
        "planned_locations": sorted({x for x in same.values_list("location_name", flat=True) if x}),
        "periods": sorted(periods.values(), key=lambda p: (p["end"] or datetime.date.min, p["key"])),
        "by_location": sorted(by_location.values(), key=lambda loc: (loc["location"], loc["p_code"])),
        "reports": ordered_reports,
        "report_type": filters.report_type,
        "year": year,
    }
