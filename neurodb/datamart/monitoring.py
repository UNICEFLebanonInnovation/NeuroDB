"""Partner implementation monitoring from eTools: PD indicators, what partners reached on them
(Partner Reporting Portal reports, per report and location), and whether each is on track against
the target set in the programme document.

An indicator is identified by its programme document and its eTools id (``PDIndicator.source_id``;
the title only when there is no id): the Datamart lists PD indicators once per location and
disaggregation (``PDIndicator``), and the PRP data reports carry the id (``ReportedIndicator``, one
row per report and location). Quarterly (QPR) and monthly humanitarian (HR) reports describe the
same achievements, so one report type is read at a time and their values are never added together;
an indicator with no quarterly report at all is read from its humanitarian reports.

Every figure comes from :mod:`neurodb.datamart.reached` (the one rule for "Reached": counted
reports, location and period methods, nothing added twice); this module only selects the indicators
and arranges the figures for the grid, the map, the indicator detail and their summaries.
"""

from __future__ import annotations

import datetime
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any

from django.db.models import Q, QuerySet
from django.urls import reverse

from neurodb.geo.models import Location
from neurodb.indicators.services import tracking
from neurodb.indicators.services.tracking import NO_TARGET, tracking_between
from neurodb.partnerships import statuses as pd_statuses
from neurodb.partnerships.models import PCA, PartnerOrganization

from . import models as dm
from . import reached as rc
from .reached import location_key, norm, number  # noqa: F401 (the names other modules import from here)
from .tags import TAG_FIELDS

REPORT_TYPES = {"QPR": "Quarterly progress reports", "HR": "Humanitarian reports (monthly)"}
DEFAULT_REPORT_TYPE = "QPR"
# the running programme documents (active, signed or suspended), as every page reads them
ACTIVE_PD_STATUSES = pd_statuses.RUNNING
SUBMITTED = ("submitted", "accepted", "sent back", "sen", "sub", "acc")  # report_status: word or code
# SUBMITTED as one pattern for the database: the whole status, any case, spaces around allowed
SUBMITTED_REGEX = r"^\s*(" + "|".join(re.escape(s) for s in SUBMITTED) + r")\s*$"
MONTHS = tuple(range(1, 13))
NOT_REPORTED = rc.NOT_REPORTED  # no counted report with a value: never "off track"
LABELS = {**tracking.LABELS, NOT_REPORTED: "Not reported"}
REPORT_STATUSES = rc.STATUS_LABELS  # Accepted, Submitted, Sent back, Due, Overdue, Not yet due
ALL_YEARS = "all"
# rows the grid and Program Performance list (PAGE_SIZE at a time); every count, export, headline and
# Ask NeuroDB tool reads every indicator of the filter (``indicators`` returns them all)
MAX_INDICATORS = 2000
PAGE_SIZE = 100
NO_SECTION = "—"  # the section of an indicator that names none (a section tile and filter value)


# ------------------------------------------------------------------------------ report submitted
def pending_q() -> Q:
    """Progress report rows not submitted yet: no submission date, and a status that is not one of
    ``SUBMITTED``. PRP can mark a report accepted without its date, so the date alone would call it
    missing. This is the one rule behind every "overdue" and "due soon" figure (the daily review,
    the monitoring summary, partner reporting, the brief, the watch), so they never disagree; the
    six report statuses (:func:`neurodb.datamart.reached.status_of`) follow it too."""
    return Q(submission_date=None) & ~Q(report_status__iregex=SUBMITTED_REGEX)


def is_submitted(row: dict[str, Any] | dm.ReportedIndicator) -> bool:
    """The rule of :func:`pending_q` for a row already loaded (a ``values()`` dict or a record):
    submitted when it has a submission date or its status is one of ``SUBMITTED``."""
    if isinstance(row, dict):
        date, status = row.get("submission_date"), row.get("report_status")
    else:
        date, status = row.submission_date, row.report_status
    return date is not None or (status or "").strip().lower() in SUBMITTED


def combine(values: list[float | None], method: str) -> float | None:
    """Values as one by PRP's method: sum, max, avg or latest (a ratio is never added)."""
    return rc.combine(values, method)


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
    year: int | None = None  # None: every year (Reached to date, one column per year)
    scope: str = "active"  # active | all programme documents | year (every PD running in ``year``)
    status: str = ""  # on_track | off_track | over_target | no_target | not_reported
    q: str = ""
    tags: dict[str, list[str]] = field(default_factory=dict)
    # eTools PD statuses chosen in the filter: None follows ``scope`` (the running PDs by default), an
    # empty list is every status
    pd_statuses: list[str] | None = None
    document_types: list[str] = field(default_factory=list)  # PD, SPD...
    offices: list[str] = field(default_factory=list)  # the PD's UNICEF offices
    partner_types: list[str] = field(default_factory=list)  # partner type or CSO type
    emergency: str = ""  # yes: the emergency (high-frequency) indicators, no: the others
    cp_output: str = ""  # words in the indicator's country programme output

    @property
    def all_years(self) -> bool:
        return self.year is None

    @property
    def year_param(self) -> str:
        """The year as the page's links carry it ("all" for every year)."""
        return str(self.year) if self.year else ALL_YEARS

    @property
    def status_choice(self) -> list[str]:
        """The PD statuses the filter shows as chosen (none: every status)."""
        if self.pd_statuses is not None:
            return list(self.pd_statuses)
        return list(ACTIVE_PD_STATUSES) if self.scope == "active" else []

    @classmethod
    def from_params(cls, params, today: datetime.date | None = None, default_year: bool = True) -> Filters:
        """The page's filter from its query. ``default_year``: without a year, this calendar year (the
        grid); else every year (Program Performance and Data quality, as Power BI opens them)."""
        today = today or datetime.date.today()

        def getlist(key: str) -> list[str]:
            # a query (several values per key) or a plain dict (Ask NeuroDB, tests): one value or a list
            values = params.getlist(key) if hasattr(params, "getlist") else params.get(key, [])
            return [values] if isinstance(values, str) else list(values or [])

        report_type = (params.get("report_type") or DEFAULT_REPORT_TYPE).upper()
        raw_year = str(params.get("year") or "").strip().lower()
        if raw_year == ALL_YEARS or (not raw_year and not default_year):
            year = None
        else:
            year = year_param(raw_year) or today.year
        emergency = str(params.get("emergency") or "").strip().lower()
        return cls(
            sections=[x for x in getlist("section") if x],
            partners=[x for x in getlist("partner") if x],
            pds=[x for x in getlist("pd") if x],
            locations=[x for x in getlist("location") if x],
            report_type=report_type if report_type in REPORT_TYPES else DEFAULT_REPORT_TYPE,
            year=year,
            scope=params.get("scope") if params.get("scope") in ("all", "year") else "active",
            status=params.get("status") or "",
            q=(params.get("q") or "").strip(),
            tags={t: [x for x in getlist(t) if x] for t in TAG_FIELDS},
            # "pd_status=" alone (nothing chosen) is every status; no key at all, the running PDs
            pd_statuses=[x for x in getlist("pd_status") if x] if "pd_status" in params else None,
            document_types=[x for x in getlist("document_type") if x],
            offices=[x for x in getlist("office") if x],
            partner_types=[x for x in getlist("partner_type") if x],
            emergency=emergency if emergency in ("yes", "no") else "",
            cp_output=(params.get("cp_output") or "").strip(),
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
    qs = PCA.objects.select_related("partner").exclude(status__in=pd_statuses.NOT_A_PARTNERSHIP)
    if filters.pd_statuses:  # the statuses chosen in the filter
        qs = qs.filter(status__in=filters.pd_statuses)
    elif filters.pd_statuses is None and filters.scope == "active":
        qs = qs.filter(status__in=ACTIVE_PD_STATUSES)
    if filters.scope == "year" and filters.year:  # every PD running in the year, closed ones too
        first, last = datetime.date(filters.year, 1, 1), datetime.date(filters.year, 12, 31)
        qs = qs.filter(Q(start__isnull=True) | Q(start__lte=last), Q(end__isnull=True) | Q(end__gte=first))
    if filters.partners:
        qs = qs.filter(partner_id__in=[int(p) for p in filters.partners if re.fullmatch(r"[0-9]{1,18}", p)])
    if filters.pds:
        qs = qs.filter(number__in=filters.pds)
    if filters.pd_ids:
        qs = qs.filter(id__in=filters.pd_ids)
    if filters.document_types:
        qs = qs.filter(document_type__in=filters.document_types)
    if filters.offices:
        qs = qs.filter(offices_set__overlap=filters.offices)
    if filters.partner_types:
        qs = qs.filter(
            Q(partner__partner_type__in=filters.partner_types)
            | Q(partner__cso_type__in=filters.partner_types)
        )
    return qs


def _indicator_rows(filters: Filters, pd_ids: list[int]) -> QuerySet[dm.PDIndicator]:
    """The PD indicator rows the filters keep (the section is chosen per indicator, in :func:`indicators`)."""
    qs = dm.PDIndicator.objects.filter(intervention_id__in=pd_ids)
    if filters.q:
        qs = qs.filter(Q(title__icontains=filters.q) | Q(lower_result_name__icontains=filters.q))
    for tag, values in filters.tags.items():
        if values:
            qs = qs.filter(**{f"tag_{tag}__in": values})
    if filters.emergency:
        qs = qs.filter(is_high_frequency=filters.emergency == "yes")
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
    ident: tuple[int, str] = (0, "")
    source_id: int | None = None
    cp_output: str = ""
    locations: list[str] = field(default_factory=list)
    planned: dict[str, dict[str, Any]] = field(default_factory=dict)  # location key -> {name, p_code, id}
    # location key -> name, p_code, id, reached (to date), achieved (Reached in the year), months,
    # prp_cumulative, period, report, cells (report key -> value)
    by_location: dict[str, dict[str, Any]] = field(default_factory=dict)
    months: dict[int, float] = field(default_factory=dict)  # counted reports of the year, by period-end month
    years: dict[int, float] = field(default_factory=dict)  # Reached in each year
    reports: int = 0  # counted reports (Accepted or Submitted) with a value
    reached: float | None = None  # to date
    reached_in_year: float | None = None
    year: int | None = None
    awaiting: float | None = None  # of Reached, from reports awaiting acceptance
    awaiting_reports: int = 0
    as_of: datetime.date | None = None  # period end of the latest counted report
    prp_cumulative: float | None = None
    mismatch: bool = False
    value_status: str = NOT_REPORTED
    partial: bool = False
    report_type: str = DEFAULT_REPORT_TYPE  # the reports Reached was read from
    partner_status: str = ""
    report_status: str = ""  # the latest counted report's status
    tracking: str = NO_TARGET
    achieved: float | None = None  # % of the target reached
    method: str = ""  # how PRP combines the locations of a period: sum (or empty), max, avg, ratio
    period_method: str = ""  # and the periods: sum (or empty), max, avg, latest, ratio
    result: rc.Reached | None = None

    @property
    def cumulative(self) -> float | None:
        """Reached to date (the name the country overview and its consumers still read)."""
        return self.reached

    @property
    def cumulative_as_of(self) -> datetime.date | None:
        return self.as_of

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
    def additive(self) -> bool:
        return self.result.additive if self.result else self.method in ("", "sum")

    @property
    def method_label(self) -> str:
        return rc.method_label(self.method, self.period_method, bool(self.result and self.result.ratio))

    @property
    def notes(self) -> list[str]:
        return self.result.notes if self.result else []

    @property
    def url(self) -> str:
        return reverse("reports:pd_indicator", args=[self.pd.id, self.key])

    def as_dict(self) -> dict[str, Any]:
        out = {
            "programme_document": self.pd.number,
            "partner": self.partner.name if self.partner else self.pd.partner_name,
            "section": self.section,
            "cp_output": self.cp_output,
            "output": self.output,
            "indicator": self.title,
            "tags": {k: v for k, v in self.tags.items() if v},
            "unit": self.unit,
            "baseline": self.baseline,
            "target": self.target,
            "reached": self.reached,
            "reached_as_of": self.as_of,
            "awaiting_acceptance": self.awaiting,
            "reports_awaiting_acceptance": self.awaiting_reports,
            "achieved_percent": round(self.achieved, 1) if self.achieved is not None else None,
            "status": self.tracking,
            "value_status": self.value_status,
            "partner_assessment": self.partner_status,
            "counted_reports": self.reports,
            "report_type": self.report_type,
            "method": self.method_label,
            "prp_cumulative": self.prp_cumulative,
            "differs_from_prp_cumulative": self.mismatch,
            "partial_history": self.partial,
            "by_year": {str(y): v for y, v in self.years.items()},
            "locations": self.locations[:10],
            "url": self.url,
        }
        if self.year:
            out["year"] = self.year
            out["reached_in_year"] = self.reached_in_year
            out["by_month"] = {f"{m:02d}": v for m, v in sorted(self.months.items())}
        if self.notes:
            out["data_notes"] = self.notes[:5]
        return out


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


def ident_of(pd_id: int, source_id: int | None, title: str | None) -> tuple[int, str]:
    """An indicator's identity in its PD: its eTools id, else its title (when it has no id)."""
    return (pd_id, f"s{source_id}") if source_id else (pd_id, f"t{norm(title)}")


def indicators(
    filters: Filters,
    today: datetime.date | None = None,
    *,
    section_counts: Counter | None = None,
) -> list[Indicator]:
    """One entry per PD indicator, with its Reached from the selected report type.

    ``section_counts``: filled with the number of indicators of each section that every other filter
    keeps (the section tiles of Program Performance, :data:`NO_SECTION` for those without one); every
    section's Reached is then computed. Every indicator of the filter is returned: the country figures,
    the counts and the exports read them all, and a page that lists them cuts the list itself
    (:data:`MAX_INDICATORS`)."""
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
        "cp_output",
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
        ident = ident_of(row["intervention_id"], row["source_id"], row["title"])
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
                ident=ident,
                source_id=row["source_id"],
                cp_output=row["cp_output"] or "",
            )
        if not entry.cp_output and row["cp_output"]:
            entry.cp_output = row["cp_output"]
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
    for entry in found.values():
        if not entry.cp_output:  # not reported yet: the PD's own CP output when it has only one
            outputs = [x for x in (entry.pd.cp_outputs or []) if x]
            entry.cp_output = outputs[0] if len(outputs) == 1 else ""
    if filters.cp_output:
        words = norm(filters.cp_output)
        found = {k: e for k, e in found.items() if words in norm(e.cp_output)}
    if filters.locations:
        wanted = {norm(x) for x in filters.locations}
        found = {k: e for k, e in found.items() if any(norm(loc) in wanted for loc in e.locations)}
    # the chosen sections: NO_SECTION (the tile of the indicators that name none) keeps those
    sections = {s.strip() for s in filters.sections if s.strip()}
    if section_counts is None and sections:  # only the chosen sections' Reached is computed
        found = {k: e for k, e in found.items() if (e.section or NO_SECTION) in sections}
    _attach_reached(found, filters, today)
    result = [e for e in found.values() if not filters.status or e.tracking == filters.status]
    if section_counts is not None:
        section_counts.update(e.section or NO_SECTION for e in result)
        if sections:
            result = [e for e in result if (e.section or NO_SECTION) in sections]
    return result


class _Idents:
    """Which PD indicator a report row belongs to: by its eTools indicator id, else by its title when
    one indicator of the PD has it (two indicators with the same title are never merged: a row that
    names neither id is then left out).

    Built over every indicator of the PDs, not only those the filters keep: a report row of a
    filtered-out indicator is then skipped instead of falling back to another indicator's title."""

    def __init__(self, pd_ids: list[int]) -> None:
        self.by_source: dict[tuple[int, str], tuple[int, str]] = {}
        titles: dict[tuple[int, str], set[tuple[int, str]]] = defaultdict(set)
        rows = dm.PDIndicator.objects.filter(intervention_id__in=pd_ids)
        for pd_id, source_id, title in rows.values_list("intervention_id", "source_id", "title"):
            ident = ident_of(pd_id, source_id, title)
            if source_id:
                self.by_source.setdefault((pd_id, str(source_id)), ident)
            titles[(pd_id, norm(title))].add(ident)
        self.by_title = {key: next(iter(found)) for key, found in titles.items() if len(found) == 1}

    def of(self, pd_id: int, etools_id: Any, title: str | None) -> tuple[int, str] | None:
        source = str(etools_id or "").strip()
        if source and (pd_id, source) in self.by_source:
            return self.by_source[(pd_id, source)]
        return self.by_title.get((pd_id, norm(title)))


def _attach_reached(found: dict[tuple[int, str], Indicator], filters: Filters, today: datetime.date) -> None:
    """Reached for every indicator (:func:`neurodb.datamart.reached.compute`), from the selected report
    type; an indicator with no quarterly report at all is read from its humanitarian reports. An
    indicator with no counted value is "not reported", never off track."""
    if not found:
        return
    pd_ids = sorted({ident[0] for ident in found})
    idents = _Idents(pd_ids)
    per: dict[tuple[int, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rc.report_rows(pd_ids, [filters.report_type]):
        ident = idents.of(row["intervention_id"], row["etools_indicator_id"], row["indicator"])
        if ident in found:
            per[ident].append(row)
    read_from = dict.fromkeys(per, filters.report_type)
    missing = [ident for ident in found if ident not in per]
    if filters.report_type == "QPR" and missing:
        humanitarian: dict[tuple[int, str], list[dict[str, Any]]] = defaultdict(list)
        wanted = set(missing)
        for row in rc.report_rows(sorted({ident[0] for ident in missing}), ["HR"]):
            ident = idents.of(row["intervention_id"], row["etools_indicator_id"], row["indicator"])
            if ident in wanted:
                humanitarian[ident].append(row)
        for ident, rows in humanitarian.items():
            per[ident], read_from[ident] = rows, "HR"
    totals = rc.progress_values(row for rows in per.values() for row in rows)
    since = rc.reports_read_from(today)  # a PD that started before it has a partial history
    only = {norm(x) for x in filters.locations} or None
    for ident, entry in found.items():
        result = rc.compute(
            per.get(ident, []),
            display_type=entry.display_type,
            unit=entry.unit,
            year=filters.year,
            today=today,
            progress_values=totals,
            only=only,
            partial=bool(entry.pd.start and entry.pd.start < since),
        )
        _apply(entry, result, read_from.get(ident, filters.report_type))
        if result.reached is None:
            entry.tracking, entry.achieved = NOT_REPORTED, None
            continue
        status = tracking_between(entry.reached, entry.target, entry.pd.start, entry.pd.end, today)
        entry.tracking, entry.achieved = status.status, status.achieved


def _apply(entry: Indicator, result: rc.Reached, report_type: str) -> None:
    entry.result = result
    entry.report_type = report_type
    entry.reached, entry.reached_in_year, entry.year = result.reached, result.reached_in_year, result.year
    entry.awaiting, entry.awaiting_reports = result.awaiting, result.awaiting_reports
    entry.months, entry.years = result.months, result.years
    entry.reports = len(result.counted)
    entry.by_location = result.by_location
    entry.prp_cumulative, entry.mismatch = result.prp_cumulative, result.mismatch
    entry.value_status, entry.partial = result.value_status, result.partial
    entry.method, entry.period_method = result.method, result.period_method
    latest = result.latest
    entry.as_of = latest.period_end if latest else None
    entry.report_status = latest.status_label if latest else ""
    entry.partner_status = latest.partner_status if latest else ""


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
    """Coordinates and hierarchy of a location: its own point, else the nearest ancestor's. The
    governorate and district are given by name and by gazetteer id (``governorate_id``,
    ``district_id``; None when the location is not under one)."""
    out: dict[str, Any] = {
        "admin_level": None,
        "level_name": "",
        "governorate": "",
        "district": "",
        "governorate_id": None,
        "district_id": None,
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
            out["governorate"], out["governorate_id"] = node["name"], node["id"]
        if node["type__admin_level"] == LEVEL_DISTRICT:
            out["district"], out["district_id"] = node["name"], node["id"]
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
    there: partner, PD (and its funding), indicator, target, the location's own Reached (to date and
    in the year), the indicator's Reached, status, period, and the monitoring done at the place. A
    location is placed by its own eTools coordinates, else by the nearest ancestor with coordinates
    (flagged approximate); names alone never place anything."""
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
                    # the location's own Reached (to date and in the year) and PRP's cumulative there
                    "reached_here": reported["reached"] if reported else None,
                    "reached_here_in_year": reported["achieved"] if reported else None,
                    "prp_cumulative_here": reported["prp_cumulative"] if reported else None,
                    "reached": ind.reached,
                    "additive": ind.additive,
                    "method": ind.method_label,
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
        "year": filters.year or ALL_YEARS,  # the indicator links open the detail for the same year and type
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
            pending_q(),
            intervention_id__in=pd_ids,
            report_type=filters.report_type,
            due_date__lt=today,
        )
        .values("progress_report")
        .distinct()
        .count()
    )
    with_reached = sum(1 for r in rows if r.reached is not None)
    return {
        "indicators": len(rows),
        "programme_documents": len(pd_ids),
        "partners": len({r.pd.partner_id for r in rows if r.pd.partner_id}),
        "with_target": sum(1 for r in rows if r.target),
        # "with Reached": a counted report (Accepted or Submitted) carries a value
        "with_reached": with_reached,
        "reported": with_reached,
        "reported_zero": sum(1 for r in rows if r.value_status == rc.REPORTED_ZERO),
        "awaiting_acceptance": sum(1 for r in rows if r.awaiting_reports),
        "differs_from_prp": sum(1 for r in rows if r.mismatch),
        "status_counts": {key: counts.get(key, 0) for key in LABELS},
        "overdue_reports": overdue,
        "by_section": Counter(r.section or NO_SECTION for r in rows).most_common(),
    }


# ------------------------------------------------------------------------- Program Performance
def _partner_name(row: Indicator) -> str:
    return row.partner.name if row.partner else (row.pd.partner_name or "")


# the columns Program Performance sorts by (None values always last)
TABLE_SORTS: dict[str, Any] = {
    "section": lambda r: norm(r.section) or None,
    "cp_output": lambda r: norm(r.cp_output) or None,
    "emergency": lambda r: r.high_frequency,
    "indicator": lambda r: norm(r.title) or None,
    "target": lambda r: r.target,
    "reached": lambda r: r.reached,
    "achieved": lambda r: r.achieved,
    "reached_in_year": lambda r: r.reached_in_year,
    "partner": lambda r: norm(_partner_name(r)) or None,
    "pd": lambda r: (r.pd.number or "").lower() or None,
}
DEFAULT_SORT = "-achieved"  # % Reached, highest first, as Power BI opens Program Performance


def sorted_rows(rows: list[Indicator], sort: str) -> tuple[list[Indicator], str]:
    """The indicators in the order of ``sort`` (a column of :data:`TABLE_SORTS`, "-" first for highest
    first), ties by PD and title; indicators without a value go last either way. Also the sort applied."""
    key, desc = (sort or "").lstrip("-"), (sort or "").startswith("-")
    if key not in TABLE_SORTS:
        key, desc = DEFAULT_SORT.lstrip("-"), True
    value = TABLE_SORTS[key]
    present = [r for r in rows if value(r) is not None]
    missing = [r for r in rows if value(r) is None]
    tie = lambda r: ((r.pd.number or "").lower(), norm(r.title))  # noqa: E731
    present.sort(key=tie)
    present.sort(key=value, reverse=desc)  # stable: equal values keep the PD and title order
    missing.sort(key=tie)
    return present + missing, ("-" if desc else "") + key


TABLE_COLUMNS = (
    "section",
    "cp_output",
    "emergency",
    "pd_output",
    "indicator",
    "unit",
    "baseline",
    "target",
    "reached",
    "reached_as_of",
    "reached_in_year",
    "awaiting_acceptance",
    "achieved_percent",
    "status",
    "reported",
    "partner",
    "programme_document",
    "pd_status",
    "document_type",
    "read_from",
    "method",
    "prp_cumulative",
    "differs_from_prp",
    "partial_history",
    "link",
)


def table_rows(rows: list[Indicator], filters: Filters, base_url: str = "") -> Any:
    """Program Performance as rows for the CSV and Excel exports, one per indicator (every row of the
    filter, not only the page shown): the figures of the page, nothing added across indicators."""
    for r in rows:
        yield {
            "section": r.section,
            "cp_output": r.cp_output,
            "emergency": "Yes" if r.high_frequency else "No",
            "pd_output": r.output,
            "indicator": r.title,
            "unit": r.unit,
            "baseline": r.baseline,
            "target": r.target,
            "reached": r.reached,
            "reached_as_of": r.as_of,
            "reached_in_year": r.reached_in_year if filters.year else None,
            "awaiting_acceptance": r.awaiting if r.awaiting_reports else None,
            "achieved_percent": round(r.achieved, 1) if r.achieved is not None else None,
            "status": r.tracking_label,
            "reported": rc.VALUE_LABELS.get(r.value_status, ""),
            "partner": _partner_name(r),
            "programme_document": r.pd.number or "",
            "pd_status": pd_statuses.label(r.pd.status),
            "document_type": r.pd.document_type or "",
            "read_from": REPORT_TYPES.get(r.report_type, r.report_type),
            "method": r.method_label,
            "prp_cumulative": r.prp_cumulative,
            "differs_from_prp": "Yes" if r.mismatch else "",
            "partial_history": "Yes" if r.partial else "",
            "link": base_url + r.url,
        }


# ---------------------------------------------------------------------------------- data quality
QUALITY_LIMIT = 500  # rows each Data quality table lists (the counts are complete)


def data_quality(
    rows: list[Indicator], filters: Filters, today: datetime.date | None = None
) -> dict[str, Any]:
    """Data quality of the filtered PD indicators (Power BI's Data Quality page, plus checks only NeuroDB
    makes): programme documents (running, active, ended), indicators with and without Reached and a
    target, and the lists to follow up. "Reached" is Reached to date, or Reached in the year when one is
    chosen; "with Reached" means a counted report (Accepted or Submitted) carries a value."""
    today = today or datetime.date.today()
    pds = {r.pd.id: r.pd for r in rows}

    def reached(row: Indicator) -> float | None:
        return row.reached_in_year if filters.year else row.reached

    without = [r for r in rows if reached(r) is None]
    zero = [r for r in rows if reached(r) == 0]
    missing_target = [r for r in rows if not r.target]
    titles: dict[tuple[int, str], list[Indicator]] = defaultdict(list)
    for r in rows:
        titles[(r.pd.id, norm(r.title))].append(r)
    same_title = [
        {"pd": group[0].pd, "title": group[0].title, "count": len(group), "rows": group}
        for group in titles.values()
        if len(group) > 1
    ]
    same_title.sort(key=lambda g: ((g["pd"].number or "").lower(), norm(g["title"])))
    partial = sorted(
        {r.pd.id: r.pd for r in rows if r.partial}.values(), key=lambda pd: pd.start or datetime.date.min
    )

    def order(items: list[Indicator]) -> list[Indicator]:
        return sorted(items, key=lambda r: (norm(r.section), (r.pd.number or "").lower(), norm(r.title)))

    no_reached = order(without + zero)
    mismatch = order([r for r in rows if r.mismatch])
    return {
        "programme_documents": len(pds),
        "active_pds": sum(1 for pd in pds.values() if pd.status in pd_statuses.ACTIVE),
        "running_pds": sum(1 for pd in pds.values() if pd.status in pd_statuses.RUNNING),
        "ended_pds": sum(1 for pd in pds.values() if pd.status in pd_statuses.ENDED),
        "indicators": len(rows),
        "with_reached": len(rows) - len(without),
        "without_reached": len(without),
        "zero_reached": len(zero),
        "with_target": len(rows) - len(missing_target),
        "without_target": len(missing_target),
        "missing_target": order(missing_target)[:QUALITY_LIMIT],
        "no_reached": no_reached[:QUALITY_LIMIT],
        "no_reached_count": len(no_reached),
        "mismatch": mismatch[:QUALITY_LIMIT],
        "mismatch_count": len(mismatch),
        "same_title": same_title[:QUALITY_LIMIT],
        "same_title_count": len(same_title),
        "partial": partial[:QUALITY_LIMIT],
        "partial_count": len(partial),
        "read_from": rc.reports_read_from(today) if partial else None,
        "limit": QUALITY_LIMIT,
    }


def year_columns(rows: list[Indicator]) -> list[int]:
    """The years the "All years" grid shows, one column each: every year with a counted value."""
    return sorted({y for r in rows for y in r.years})


def grouped(rows: list[Indicator]) -> list[dict[str, Any]]:
    """Section -> programme document -> output -> indicators, the way the sector databases read."""
    sections: dict[str, dict[str, Any]] = {}
    for row in rows:
        section = sections.setdefault(
            row.section or NO_SECTION, {"section": row.section or NO_SECTION, "pds": {}, "count": 0}
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
    offices: set[str] = set()
    for row in pds.order_by().values_list("offices_set", flat=True):
        offices.update(x for x in row or [] if x)
    partner_rows = PartnerOrganization.objects.filter(interventions__in=pds).order_by()
    options = {
        "sections": sorted({(x or "").strip() for x in base.values_list("section_name", flat=True)} - {""}),
        "partners": [{"value": str(pk), "label": name} for pk, name in partners],
        "pds": sorted({x for x in pds.values_list("number", flat=True) if x}),
        "locations": sorted({x for x in base.values_list("location_name", flat=True) if x}),
        "years": years or ([filters.year] if filters.year else []),
        "report_types": REPORT_TYPES,
        # eTools PD statuses, the running ones first (the filter's default)
        "pd_statuses": sorted(
            {x for x in pds.order_by().values_list("status", flat=True) if x} | set(ACTIVE_PD_STATUSES),
            key=lambda x: (x not in ACTIVE_PD_STATUSES, x),
        ),
        "document_types": sorted({x for x in pds.order_by().values_list("document_type", flat=True) if x}),
        "offices": sorted(offices),
        "partner_types": sorted(
            {x for pair in partner_rows.values_list("partner_type", "cso_type") for x in pair if x}
        ),
    }
    for tag in TAG_FIELDS:
        options[tag] = sorted(x for x in base.order_by().values_list(f"tag_{tag}", flat=True).distinct() if x)
    return options


# -------------------------------------------------------------------------------------- detail
def _report_entry(report: rc.Report) -> dict[str, Any]:
    return {
        "key": report.key,
        "progress_report": report.key,
        "report": report.number or report.key,
        "report_number": report.number,
        "report_type": report.report_type,
        "start": report.period_start,
        "end": report.period_end,
        "period_start": report.period_start,
        "period_end": report.period_end,
        "due_date": report.due_date,
        "submission_date": report.submission_date,
        "status": report.status,
        "status_label": report.status_label,
        "status_text": report.status_text,
        "counted": report.counted,
        "total": report.value,
        "source": report.source,
        "prp_cumulative": report.prp_cumulative,
        "progress_value": report.progress_value,
        "pd_output_progress_status": report.partner_status,
        "narrative_assessment": report.narrative_assessment,
        "notes": report.notes,
    }


def indicator_detail(
    pd: PCA, key: str, report_type: str, year: int | None, today: datetime.date | None = None
) -> dict[str, Any] | None:
    """One indicator of a PD: its definition, its Reached, and a location × report matrix of every
    report of the type (every year; not-yet-due reports left out) whose margins are the figures:
    each row's total is the location's Reached, each column's the report's value, the corner the
    indicator's Reached. Reports that do not count (Sent back, Due, Overdue) are shown greyed and
    stay out of every total. ``year`` (None: every year) chooses the "Reached in" figure."""
    today = today or datetime.date.today()
    if not re.fullmatch(r"r?[0-9]{1,18}", key or ""):
        return None
    rows = dm.PDIndicator.objects.filter(intervention=pd)
    rows = rows.filter(datamart_id=int(key[1:])) if key.startswith("r") else rows.filter(source_id=int(key))
    first = rows.order_by("datamart_id").first()
    if first is None:
        return None
    report_type = report_type if report_type in REPORT_TYPES else DEFAULT_REPORT_TYPE
    ident = ident_of(pd.id, first.source_id, first.title)
    indicator = next(
        (
            i
            for i in indicators(
                Filters(pd_ids=[pd.id], scope="all", report_type=report_type, year=year), today
            )
            if i.ident == ident
        ),
        None,
    )
    same = dm.PDIndicator.objects.filter(intervention=pd)
    same = same.filter(source_id=first.source_id) if first.source_id else same.filter(title=first.title)
    result = indicator.result if indicator else None
    reports = [r for r in result.reports if r.status != rc.NOT_YET_DUE] if result else []
    periods = [_report_entry(r) for r in reports]
    by_location = [
        {
            "location": place["name"] or "—",
            "p_code": place["p_code"],
            "location_id": place["id"],
            "cells": place["cells"],
            "reached": place["reached"],
            "achieved": place["achieved"],
            "prp_cumulative": place["prp_cumulative"],
        }
        for place in (result.by_location.values() if result else [])
    ]
    newest_first = sorted(periods, key=lambda p: (p["end"] or datetime.date.min, p["key"]), reverse=True)
    return {
        "pd": pd,
        "indicator": indicator,
        "result": result,
        "definition": first,
        "disaggregations": sorted({x for x in same.values_list("disaggregation_name", flat=True) if x}),
        "planned_locations": sorted({x for x in same.values_list("location_name", flat=True) if x}),
        "periods": periods,
        "by_location": by_location,
        "reports": newest_first,
        "repeated_totals": any(r.source == "report_total" for r in reports),
        # the margins add up only for a sum of locations and periods whose counted reports each come
        # from their locations (not a report total without values per location, not PRP's cumulative)
        "adds_up": bool(
            result
            and result.additive
            and result.source == "computed"
            and all(r.source == "locations" for r in result.counted)
        ),
        "report_type": report_type,
        # the reports the matrix shows: the type chosen, or the monthly ones of an indicator that has no
        # quarterly report at all
        "read_type": indicator.report_type if indicator else report_type,
        "year": year,
        "year_param": str(year) if year else ALL_YEARS,
        "report_statuses": REPORT_STATUSES,
    }
