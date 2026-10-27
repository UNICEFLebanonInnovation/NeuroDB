"""Partner implementation monitoring from eTools: PD indicators, what partners reached on them
(Partner Reporting Portal reports, per report and location), and whether each is on track against
the target set in the programme document.

An indicator is identified by its programme document and its eTools id (``PDIndicator.source_id``;
the title only when there is no id): the Datamart lists PD indicators once per location and
disaggregation (``PDIndicator``), and the PRP data reports (``ReportedIndicator``, one row per report and
location) are placed on them by :mod:`neurodb.datamart.identity` (the id, else the title, else the PD
output and title, else the tie-breakers the data proves). Quarterly (QPR) and monthly humanitarian (HR)
reports describe the same achievements, so one report type is read at a time and their values are never
added together; an indicator with no quarterly report at all is read from its humanitarian reports.

Every figure comes from :mod:`neurodb.datamart.reached` (the one rule for "Reached": counted
reports, location and period methods, nothing added twice); this module only selects the indicators
and arranges the figures for the grid, the map, the indicator detail and their summaries.

The partner monitoring page loads in parts (``reports.views.pd_monitoring``): its parts read the
indicators of the filter stored once for every worker (:func:`stored_indicators`, plain copies of
:class:`Indicator`: :class:`Row`), and its frame the filter options stored the same way
(:func:`stored_options`); the map's API stores its places (:func:`stored_map_points`). Their keys are
the filter, the day and the moment of the eTools figures (:mod:`neurodb.datamart.keys`). Every other
reader (the overview, the brief, Ask NeuroDB, the checks) calls :func:`indicators` itself, as before.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import re
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

from django.conf import settings
from django.db.models import Q, QuerySet
from django.urls import reverse

from neurodb.core import compute
from neurodb.geo.models import Location
from neurodb.indicators.services import tracking
from neurodb.indicators.services.tracking import NO_TARGET, tracking_between
from neurodb.partnerships import statuses as pd_statuses
from neurodb.partnerships.models import PCA, PartnerOrganization

from . import disaggregation, identity, keys
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
ROWS_CHUNK = 2000  # PD indicator rows read at a time
NO_SECTION = "—"  # the section of an indicator that names none (a section tile and filter value)
# what the partner monitoring page and its map keep in the shared cache (compute.once): the indicators of a
# filter and the map's places two minutes, the filter options ten (they only change with a sync)
STORED_SECONDS = 120
OPTIONS_SECONDS = 600
# 2 (two lines of work): a stored row carries the rows placed under it and its title's rows left out; or
# Reached in the year with its parts, the quarters and the target's parts. 3 (two lines of work): a Row
# carries both, so a row stored by either version 2 is never read; or a stored row carries the tie-breaker
# that placed its rows (neurodb.datamart.identity). 4: a Row carries all of them, so a row stored by either
# version 3 is never read
CACHE_VERSION = 4


def today() -> datetime.date:
    """The day the partner monitoring figures are read for (their tracking, the overdue reports, the first
    period NeuroDB holds): the pages read it here, where the tests freeze it."""
    return datetime.date.today()


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
    emergency: str = ""  # yes: the indicators of SPDs (the Emergency indicator, as Power BI's), no: others
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

    @property
    def statuses_read(self) -> list[str]:
        """The PD statuses the figures keep, as :func:`_pd_queryset` reads the filter, in one order: the
        chosen ones; with no choice made, the running PDs on the default scope; an empty list for every
        status."""
        if self.pd_statuses:
            return sorted({str(x) for x in self.pd_statuses})
        if self.pd_statuses is None and self.scope == "active":
            return sorted(ACTIVE_PD_STATUSES)
        return []

    def cache_key(self) -> str:
        """Every field of the filter, as JSON, for the keys of what is stored for it: each list in one order
        (the indicators of a filter never depend on the order of its choices), and the PD statuses as the
        figures read them (:attr:`statuses_read`): no ``pd_status`` in the address (the page's own links) and
        the running statuses the filter bar sends for the same choice share a key, while ``pd_status=`` alone
        (every status) keeps its own. The other values come from the address as typed, so two spellings never
        share a key (a space and an underscore, a comma inside a name)."""
        out: dict[str, Any] = {}
        for item in dataclasses.fields(self):
            value = getattr(self, item.name)
            if isinstance(value, dict):
                value = {key: sorted(str(v) for v in values) for key, values in sorted(value.items())}
            elif isinstance(value, list | tuple):
                value = sorted(str(v) for v in value)
            out[item.name] = value
        out["pd_statuses"] = self.statuses_read
        return json.dumps(out, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

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


def _stage(steps: list | None, name: str, value: Any) -> Any:
    """``value`` (what a filter keeps), appended to ``steps`` under ``name`` when the figure trail records
    them (``steps`` a list, :mod:`neurodb.datamart.trail_monitoring`); None on the pages, which record
    nothing."""
    if steps is not None:
        steps.append((name, value))
    return value


def _pd_queryset(filters: Filters, steps: list | None = None) -> QuerySet[PCA]:
    """The programme documents the filter keeps. ``steps``: the figure trail's record of each filter that
    applies, in this order (None on the pages)."""
    qs = _stage(steps, "start", PCA.objects.select_related("partner"))
    qs = _stage(steps, "not_a_partnership", qs.exclude(status__in=pd_statuses.NOT_A_PARTNERSHIP))
    if filters.pd_statuses:  # the statuses chosen in the filter
        qs = _stage(steps, "pd_status", qs.filter(status__in=filters.pd_statuses))
    elif filters.pd_statuses is None and filters.scope == "active":
        qs = _stage(steps, "pd_status", qs.filter(status__in=ACTIVE_PD_STATUSES))
    if filters.scope == "year" and filters.year:  # every PD running in the year, closed ones too
        first, last = datetime.date(filters.year, 1, 1), datetime.date(filters.year, 12, 31)
        qs = _stage(
            steps,
            "year_scope",
            qs.filter(Q(start__isnull=True) | Q(start__lte=last), Q(end__isnull=True) | Q(end__gte=first)),
        )
    if filters.partners:
        ids = [int(p) for p in filters.partners if re.fullmatch(r"[0-9]{1,18}", p)]
        qs = _stage(steps, "partner", qs.filter(partner_id__in=ids))
    if filters.pds:
        qs = _stage(steps, "pds", qs.filter(number__in=filters.pds))
    if filters.pd_ids:
        qs = _stage(steps, "pd_ids", qs.filter(id__in=filters.pd_ids))
    if filters.document_types:
        qs = _stage(steps, "document_type", qs.filter(document_type__in=filters.document_types))
    if filters.offices:
        qs = _stage(steps, "office", qs.filter(offices_set__overlap=filters.offices))
    if filters.partner_types:
        qs = _stage(
            steps,
            "partner_type",
            qs.filter(
                Q(partner__partner_type__in=filters.partner_types)
                | Q(partner__cso_type__in=filters.partner_types)
            ),
        )
    return qs


def _indicator_rows(
    filters: Filters, pd_ids: list[int], steps: list | None = None
) -> QuerySet[dm.PDIndicator]:
    """The PD indicator rows the filters keep (the section is chosen per indicator, in :func:`indicators`).
    ``steps``: as :func:`_pd_queryset`'s."""
    qs = _stage(steps, "rows", dm.PDIndicator.objects.filter(intervention_id__in=pd_ids))
    if filters.q:
        qs = _stage(
            steps, "q", qs.filter(Q(title__icontains=filters.q) | Q(lower_result_name__icontains=filters.q))
        )
    for tag, values in filters.tags.items():
        if values:
            qs = _stage(steps, f"tags.{tag}", qs.filter(**{f"tag_{tag}__in": values}))
    if filters.emergency:  # the programme document is an SPD (reached.is_spd), as Power BI's
        spd = rc.spd_q("intervention__")
        qs = _stage(steps, "emergency", qs.filter(spd) if filters.emergency == "yes" else qs.exclude(spd))
    if filters.report_type == "HR":
        qs = _stage(steps, "hr_only", qs.filter(Q(is_high_frequency=True) | ~Q(cluster_name="")))
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
    # Reached in the year with its numerator and denominator (when the period method keeps them), and from
    # 1 January to the end of each quarter (1 to 4): :attr:`neurodb.datamart.reached.Reached.to_quarter`
    in_year_item: rc.Item = (None, None, None)
    to_quarter: dict[int, rc.Item] = field(default_factory=dict)
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
    ratio: bool = False  # Reached is numerators over denominators: a percentage or a ratio (set with Reached)
    percentage: bool = False  # and × 100
    # the target's numerator and denominator, as eTools gives them (``target`` is read from them)
    target_parts: tuple[float | None, float | None] = (None, None)
    result: rc.Reached | None = None
    rows_placed: int = 0  # the partner report rows placed under it (of the report type read)
    # the rows of its PD with its title, a title several indicators of the PD share, that no indicator got
    # (Data quality's same-title list), and of them those whose PD output is empty or matches none of theirs
    title_rows_left_out: int = 0
    title_rows_unmatched: int = 0
    placed_by: str = ""  # the tie-breakers that placed its rows ("" for its id, title or output)

    @property
    def emergency(self) -> bool:
        """The Emergency indicator: its programme document is an SPD (``reached.is_spd``), as Power BI's (the
        user's decision of 26 Oct 2026); never its high frequency, which only says it has monthly reports."""
        return rc.is_spd(self.pd.document_type)

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
    def achieved_in_year(self) -> float | None:
        """% of the target reached in the year chosen (Reached in the year over the target, as Power BI's
        Reporting year reads % Reached); None without a year, a target or a value in the year."""
        if not self.year or not self.target or self.reached_in_year is None:
            return None
        return self.reached_in_year * 100 / self.target

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
            "emergency": self.emergency,  # the PD is an SPD, as Program Performance's column and Power BI's
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


_target = identity.target_of  # the name reporting and services read


def pd_cp_outputs(pd: PCA) -> str:
    """A PD's country programme outputs, joined: the CP output of an indicator no partner report names
    yet (Power BI shows the PD's outputs the same way)."""
    return ", ".join(dict.fromkeys(str(x).strip() for x in (pd.cp_outputs or []) if str(x or "").strip()))


def _key(source_id: int | None, datamart_id: int) -> str:
    return str(source_id) if source_id else f"r{datamart_id}"


ident_of = identity.ident_of  # an indicator's identity in its PD (reporting and the detail read it here)


def indicators(
    filters: Filters,
    today: datetime.date | None = None,
    *,
    section_counts: Counter | None = None,
    steps: list | None = None,
    record: Any = None,
    totals: bool = False,
) -> list[Indicator]:
    """One entry per PD indicator, with its Reached from the selected report type.

    ``section_counts``: filled with the number of indicators of each section that every other filter
    keeps (the section tiles of Program Performance, :data:`NO_SECTION` for those without one); every
    section's Reached is then computed. Every indicator of the filter is returned: the country figures,
    the counts and the exports read them all, and a page that lists them cuts the list itself
    (:data:`MAX_INDICATORS`). ``steps`` and ``record``: the figure trail's record of each filter and of
    what Reached reads (:mod:`neurodb.datamart.trail_monitoring`); None on the pages. ``totals``: each report
    row's disaggregation read as its total alone (``reached.with_totals``), for a reader of the figures that
    never shows Reached by disaggregation nor the reports' narratives (Compare with Power BI): the figures
    are the same."""
    today = today or datetime.date.today()
    found = collect(filters, steps)
    # the chosen sections: NO_SECTION (the tile of the indicators that name none) keeps those
    sections = {s.strip() for s in filters.sections if s.strip()}
    if section_counts is None and sections:  # only the chosen sections' Reached is computed
        found = {k: e for k, e in found.items() if (e.section or NO_SECTION) in sections}
        _stage(steps, "sections", set(found))
    _attach_reached(found, filters, today, record, totals=totals)
    result = [e for e in found.values() if not filters.status or e.tracking == filters.status]
    if filters.status:
        _stage(steps, "status", {e.ident for e in result})
    if section_counts is not None:
        section_counts.update(e.section or NO_SECTION for e in result)
        if sections:
            result = [e for e in result if (e.section or NO_SECTION) in sections]
    return result


def collect(filters: Filters, steps: list | None = None) -> dict[tuple[int, str], Indicator]:
    """The PD indicators the filters keep, by identity (:func:`ident_of`), without their Reached: the
    programme documents, the indicator filters, the CP output (the PD's own when no report names one yet)
    and the locations. The sections, the status and Reached are left to :func:`indicators`
    (:func:`_attach_reached`), so a reader that needs only which indicators there are computes nothing.
    ``steps``: as :func:`_pd_queryset`'s."""
    pds = {pd.id: pd for pd in _pd_queryset(filters, steps)}
    if not pds:
        return {}
    rows = _indicator_rows(filters, list(pds), steps).values(
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
    # read in chunks: the Datamart lists an indicator once per location and disaggregation
    for row in rows.order_by("lower_result_name", "title", "datamart_id").iterator(chunk_size=ROWS_CHUNK):
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
                target_parts=(number(row["target_numerator"]), number(row["target_denominator"])),
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
    _stage(steps, "one_per_indicator", dict(found))  # the indicators themselves: their attributes
    for entry in found.values():
        if not entry.cp_output:  # not reported yet: the PD's own CP outputs, joined (as Power BI shows them)
            entry.cp_output = pd_cp_outputs(entry.pd)
    if filters.cp_output:
        words = norm(filters.cp_output)
        found = {k: e for k, e in found.items() if words in norm(e.cp_output)}
        _stage(steps, "cp_output", set(found))
    only = location_filter(filters)
    if only is not None:  # planned at a chosen location: by its P-code, or by its name
        found = {
            k: e
            for k, e in found.items()
            if any(key in only or norm(p["name"]) in only for key, p in e.planned.items())
            or any(norm(loc) in only for loc in e.locations)
        }
        _stage(steps, "locations", set(found))
    return found


def location_filter(filters: Filters) -> set[str] | None:
    """The locations a location filter keeps (None: no filter), as :func:`reached.compute` reads them:
    the names chosen (normalised) and the P-code of every PD indicator location of that name
    (:func:`reached.location_key`), so a partner report that spells the place differently ("Akkar
    Governorate", P-code LB11, for the planned "Akkar") is still that place."""
    names = {norm(x) for x in filters.locations if norm(x)}
    if not names:
        return None
    match = Q()
    for name in {x.strip() for x in filters.locations if x.strip()}:
        match |= Q(location_name__iexact=name)
    keys = {
        location_key(code, name)
        for code, name in dm.PDIndicator.objects.filter(match)
        .order_by()
        .values_list("location_pcode", "location_name")
        .distinct()
        if norm(name) in names
    }
    return names | keys


def _attach_reached(
    found: dict[tuple[int, str], Indicator],
    filters: Filters,
    today: datetime.date,
    record: Any = None,
    *,
    totals: bool = False,
) -> None:
    """Reached for every indicator (:func:`neurodb.datamart.reached.compute`), from the selected report
    type; an indicator with no quarterly report at all is read from its humanitarian reports. An
    indicator with no counted value is "not reported", never off track. ``record``: the figure trail's
    recorder (``trail.Recorder``), told where each row went and what each indicator's Reached read; None on
    the pages. ``totals``: as :func:`indicators`'."""
    if not found:
        return
    pd_ids = sorted({ident[0] for ident in found})
    idents = identity.Idents(pd_ids)
    if record is not None:
        record.reading(pd_ids, filters.report_type)
    per: dict[tuple[int, str], list[dict[str, Any]]] = defaultdict(list)
    # the tie-breakers that placed each indicator's rows (identity.BREAKERS), of the report type read
    ways: dict[tuple[int, str], set[str]] = defaultdict(set)
    # each progress report's status, read from every row of the report (all its indicators), so each
    # indicator of a report whose rows disagree (a partial re-sync) has the status the reports' list shows
    words: dict[str, dict[str, rc.Stamp]] = defaultdict(dict)
    # the rows of a title several indicators of a PD share that none of them got, and of them those whose
    # PD output matches none of theirs (Data quality's same-title list), per PD and title
    unplaced: Counter[tuple[int, str]] = Counter()
    unmatched: Counter[tuple[int, str]] = Counter()

    def note(row: dict[str, Any]) -> None:
        text, mine = row["report_status"] or "", words[rc.report_key(row)]
        mine[text] = rc.stamp(mine.get(text), rc.row_stamp(row))

    def place(rows: Any, kind: str) -> list[tuple[dict[str, Any], identity.Placed]]:
        """The rows of one report type and where each goes (identity.Idents.place: per placement group)."""
        rows = list(rows)
        out = []
        for row, spot in zip(rows, idents.place(rows), strict=True):
            note(row)
            pd, title = row["intervention_id"], row["indicator"]
            shared = spot.ident is None and (pd, norm(title)) in idents.shared
            if shared:
                unplaced[(pd, norm(title))] += 1
                unmatched[(pd, norm(title))] += spot.way == "output_unmatched"
            if record is not None:
                record.placed(row, spot, kind, (pd, norm(title)) if shared else None)
            out.append((row, spot))
        return out

    for row, spot in place(rc.report_rows(pd_ids, [filters.report_type], totals=totals), filters.report_type):
        if spot.ident in found:
            per[spot.ident].append(row)
            if spot.way in identity.BREAKERS:
                ways[spot.ident].add(spot.way)
            if record is not None:
                record.used(row)
    read_from = dict.fromkeys(per, filters.report_type)
    missing = [ident for ident in found if ident not in per]
    if filters.report_type == "QPR" and missing:
        humanitarian: dict[tuple[int, str], list[dict[str, Any]]] = defaultdict(list)
        wanted = set(missing)
        hr = rc.report_rows(sorted({ident[0] for ident in missing}), ["HR"], totals=totals)
        for row, spot in place(hr, "HR"):
            if spot.ident in wanted:
                humanitarian[spot.ident].append(row)
                if spot.way in identity.BREAKERS:
                    ways[spot.ident].add(spot.way)
                if record is not None:
                    record.used(row)
        for ident, rows in humanitarian.items():
            per[ident], read_from[ident] = rows, "HR"
    statuses = {key: rc.newest_status(texts) for key, texts in words.items()}
    totals = rc.progress_values(
        (row for rows in per.values() for row in rows),
        record=record.prp_totals if record is not None else None,
    )
    since = rc.history_from(today).date  # a PD that started before it has a partial history
    only = location_filter(filters)
    for ident, entry in found.items():
        entry.rows_placed = len(per.get(ident, []))
        entry.title_rows_left_out = unplaced[(entry.pd.id, norm(entry.title))]
        entry.title_rows_unmatched = unmatched[(entry.pd.id, norm(entry.title))]
        entry.placed_by = ", ".join(name for name in identity.BREAKERS if name in ways.get(ident, ()))
        result = rc.compute(
            per.get(ident, []),
            display_type=entry.display_type,
            unit=entry.unit,
            year=filters.year,
            today=today,
            progress_values=totals,
            only=only,
            partial=bool(entry.pd.start and entry.pd.start < since),
            report_status=statuses,
            output=entry.output,
            record=record.left_out(ident) if record is not None else None,
        )
        if record is not None:
            record.reached(ident, result, read_from.get(ident, filters.report_type))
        if "active" in ways.get(ident, ()):
            result.notes.append(identity.ACTIVE_NOTE)
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
    entry.in_year_item, entry.to_quarter = result.in_year_item, result.to_quarter
    entry.awaiting, entry.awaiting_reports = result.awaiting, result.awaiting_reports
    entry.months, entry.years = result.months, result.years
    entry.reports = len(result.counted)
    entry.by_location = result.by_location
    entry.prp_cumulative, entry.mismatch = result.prp_cumulative, result.mismatch
    entry.value_status, entry.partial = result.value_status, result.partial
    entry.method, entry.period_method = result.method, result.period_method
    entry.ratio, entry.percentage = result.ratio, result.percentage
    latest = result.latest
    entry.as_of = latest.period_end if latest else None
    entry.report_status = latest.status_label if latest else ""
    entry.partner_status = latest.partner_status if latest else ""


# --------------------------------------------------------------------------- stored for the page
@dataclass
class StoredPartner:
    """A programme document's partner, as the pages draw it (its page's link and its name)."""

    id: int
    name: str


@dataclass
class StoredPD:
    """A programme document as the grid, Program Performance and Data quality draw it: plain values, so it can
    be stored for every worker (a PCA instance is not)."""

    id: int
    number: str | None
    partner_id: int | None
    partner: StoredPartner | None
    partner_name: str | None
    status: str | None
    start: datetime.date | None
    end: datetime.date | None
    document_type: str | None


@dataclass
class Row:
    """One indicator of :func:`indicators` as the partner monitoring page draws it and stores it for every
    worker (:func:`stored_indicators`): the same names and values as :class:`Indicator`, its programme
    document a :class:`StoredPD`, its properties (``tracking_label``, ``achieved_in_year``, ``additive``,
    ``method_label``, ``notes``, ``url``) read once into fields, and without what only the map and the
    indicator detail read (the reports and cells of ``result``, ``planned``, ``by_location``).
    :func:`summary`, :func:`grouped`, :func:`sorted_rows`, :func:`data_quality` and :func:`year_columns`
    read a Row as they read an Indicator."""

    key: str
    pd: StoredPD
    ident: tuple[int, str]
    title: str
    output: str
    section: str
    cp_output: str
    unit: str
    display_type: str
    baseline: float | None
    target: float | None
    target_parts: tuple[float | None, float | None]
    high_frequency: bool
    cluster: str
    tags: dict[str, str]
    locations: list[str]
    months: dict[int, float]
    years: dict[int, float]
    reports: int
    reached: float | None
    reached_in_year: float | None
    in_year_item: rc.Item
    to_quarter: dict[int, rc.Item]
    year: int | None
    awaiting: float | None
    awaiting_reports: int
    as_of: datetime.date | None
    prp_cumulative: float | None
    mismatch: bool
    value_status: str
    partial: bool
    report_type: str
    partner_status: str
    report_status: str
    tracking: str
    achieved: float | None
    method: str
    period_method: str
    ratio: bool
    percentage: bool
    tracking_label: str
    achieved_in_year: float | None
    additive: bool
    method_label: str
    notes: list[str]
    url: str
    rows_placed: int = 0
    title_rows_left_out: int = 0
    title_rows_unmatched: int = 0
    placed_by: str = ""

    @property
    def partner(self) -> StoredPartner | None:
        return self.pd.partner

    @property
    def emergency(self) -> bool:
        """As :attr:`Indicator.emergency`: the programme document is an SPD."""
        return rc.is_spd(self.pd.document_type)


def _stored_pd(pd: PCA) -> StoredPD:
    partner = pd.partner
    return StoredPD(
        id=pd.id,
        number=pd.number,
        partner_id=pd.partner_id,
        partner=StoredPartner(id=partner.id, name=partner.name) if partner is not None else None,
        partner_name=pd.partner_name,
        status=pd.status,
        start=pd.start,
        end=pd.end,
        document_type=pd.document_type,
    )


def as_rows(found: list[Indicator]) -> list[Row]:
    """:func:`indicators` as plain :class:`Row` values (one :class:`StoredPD` per programme document)."""
    pds: dict[int, StoredPD] = {}
    out = []
    for i in found:
        pd = pds.get(i.pd.id)
        if pd is None:
            pd = pds[i.pd.id] = _stored_pd(i.pd)
        out.append(
            Row(
                key=i.key,
                pd=pd,
                ident=i.ident,
                title=i.title,
                output=i.output,
                section=i.section,
                cp_output=i.cp_output,
                unit=i.unit,
                display_type=i.display_type,
                baseline=i.baseline,
                target=i.target,
                target_parts=i.target_parts,
                high_frequency=i.high_frequency,
                cluster=i.cluster,
                tags=dict(i.tags),
                locations=list(i.locations),
                months=dict(i.months),
                years=dict(i.years),
                reports=i.reports,
                reached=i.reached,
                reached_in_year=i.reached_in_year,
                in_year_item=i.in_year_item,
                to_quarter=dict(i.to_quarter),
                year=i.year,
                awaiting=i.awaiting,
                awaiting_reports=i.awaiting_reports,
                as_of=i.as_of,
                prp_cumulative=i.prp_cumulative,
                mismatch=i.mismatch,
                value_status=i.value_status,
                partial=i.partial,
                report_type=i.report_type,
                partner_status=i.partner_status,
                report_status=i.report_status,
                tracking=i.tracking,
                achieved=i.achieved,
                method=i.method,
                period_method=i.period_method,
                ratio=i.ratio,
                percentage=i.percentage,
                tracking_label=i.tracking_label,
                achieved_in_year=i.achieved_in_year,
                additive=i.additive,
                method_label=i.method_label,
                notes=list(i.notes),
                url=i.url,
                rows_placed=i.rows_placed,
                title_rows_left_out=i.title_rows_left_out,
                title_rows_unmatched=i.title_rows_unmatched,
                placed_by=i.placed_by,
            )
        )
    return out


def _rows_key(filters: Filters, day: datetime.date, stamp: str) -> str:
    return f"pm-rows:v{CACHE_VERSION}:{filters.cache_key()}:{day}:{stamp}"


def stored_indicators(filters: Filters, day: datetime.date | None = None, *, as_of: str = "") -> list[Row]:
    """:func:`indicators` of ``filters`` on ``day`` as :class:`Row` values, computed once across every
    worker and kept two minutes (the partner monitoring page's parts read them). The key holds the filter,
    the day and the moment of the eTools figures (:func:`neurodb.datamart.keys.fingerprint`: a finished
    eTools sync shows at once), and nothing about the person asking: no figure of an indicator depends on
    who asks (the user's own section reaches it only as the filter's sections). Nothing in it is translated,
    so one value serves every language. ``as_of``, in a part request, is the fingerprint the page's frame
    read: the parts of one page read one computation even when a sync finishes before or between them
    (:func:`neurodb.core.compute.once_as_of`). In DEBUG it is computed at each call."""
    day = day or today()

    def compute_rows() -> list[Row]:
        return as_rows(indicators(filters, day))

    if settings.DEBUG:
        return compute_rows()
    return compute.once_as_of(
        lambda stamp: _rows_key(filters, day, stamp),
        as_of,
        keys.fingerprint(),
        compute_rows,
        STORED_SECONDS,
        lang_neutral=True,
    )


def program_performance_rows(
    filters: Filters,
    day: datetime.date | None = None,
    *,
    as_of: str = "",
    every: list[Row] | None = None,
) -> list[Row]:
    """Program Performance's rows (its table, the comparison sheet): the indicators of the filter without
    its sections (the section tiles' computation, :func:`stored_indicators`) with the chosen sections kept.
    Reached is computed for every section, then the status filter, then the sections: an indicator's figures
    do not depend on the other sections, and the page's two parts read one computation. ``every``: that
    computation, when the caller has read it already (the page's section tiles)."""
    if every is None:
        every = stored_indicators(replace(filters, sections=[]), day, as_of=as_of)
    chosen = {s.strip() for s in filters.sections if s.strip()}  # as indicators() reads them
    if not chosen:
        return every
    return [r for r in every if (r.section or NO_SECTION) in chosen]


def section_counts(rows: list[Row]) -> Counter[str]:
    """Program Performance's section tiles: the indicators of each section (:data:`NO_SECTION` for those that
    name none), counted on the filter without its sections."""
    return Counter(r.section or NO_SECTION for r in rows)


def indicators_stored(filters: Filters, day: datetime.date | None = None) -> bool:
    """Whether :func:`stored_indicators` would answer ``filters`` at once from what is stored: the page's
    frame then draws its parts inline instead of asking for them (it never computes them itself)."""
    if settings.DEBUG:
        return False
    return compute.peek(_rows_key(filters, day or today(), keys.fingerprint()), lang_neutral=True)[0]


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
    only = location_filter(filters)
    whole: dict[tuple[int, str], Indicator] = {}
    if only is not None and rows:
        # under a location filter each row is that location's own Reached: the indicator's figures for
        # every location (its Reached, % and status) are read once more without the filter
        every = replace(filters, locations=[], status="", pd_ids=sorted({r.pd.id for r in rows}))
        whole = {r.ident: r for r in indicators(every, today)}
    places: dict[str, dict[str, Any]] = {}
    pds: dict[int, dict[str, Any]] = {}
    for ind in rows:
        pds.setdefault(ind.pd.id, _funding(ind.pd))
        full = whole.get(ind.ident, ind)
        planned_here = {
            k for k, p in ind.planned.items() if only is None or k in only or norm(p.get("name")) in only
        }
        keys = planned_here | set(ind.by_location)
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
                    # the indicator's Reached over every location, its % of the target and status
                    "reached": full.reached,
                    "additive": full.additive,
                    "method": full.method_label,
                    "achieved_percent": round(full.achieved, 1) if full.achieved is not None else None,
                    "tracking": full.tracking,
                    "tracking_label": full.tracking_label,
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
    # the most indicators first; places with as many by their key, so every computation gives one order (the
    # places come from sets of location keys, whose order changes from one process to the next)
    points.sort(key=lambda p: (-p["indicators"], p["key"]))
    unlocated.sort(key=lambda p: (-p["indicators"], p["key"]))
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


def stored_map_points(filters: Filters, day: datetime.date | None = None) -> dict[str, Any]:
    """:func:`map_points` (the partner monitoring map's API), computed once across every worker and kept two
    minutes. Its key holds the filter, the day, the moment of the eTools figures and the gazetteer as staff
    left it in the admin (:func:`neurodb.datamart.keys.gazetteer`: the map places each location by it), and
    nothing about the person asking; nothing in it is translated. In DEBUG it is computed at each call."""
    day = day or today()
    if settings.DEBUG:
        return map_points(filters, day)
    key = (
        f"pm-map:v{CACHE_VERSION}:{filters.cache_key()}:{day}:{keys.fingerprint()}:"
        f"{compute.fingerprint(keys.gazetteer())}"
    )
    return compute.once(key, lambda: map_points(filters, day), STORED_SECONDS, lang_neutral=True)


# ------------------------------------------------------------------------------------- summaries
def _dq_reached(row: Any, filters: Filters) -> float | None:
    """Reached as Data quality reads it: in the year chosen, else to date."""
    return row.reached_in_year if filters.year else row.reached


# What each indicator figure counts, one named test per figure (an indicator, or a row, and the filter): the
# summary, Data quality and the figure trail count with them, so a figure and its trail never read two rules
FIGURE_TESTS: dict[str, Callable[[Any, Filters], bool]] = {
    "with_target": lambda r, f: bool(r.target),  # a target of 0 counts as none
    # a counted report (Accepted or Submitted) carries a value
    "with_reached": lambda r, f: r.reached is not None,
    "reported_zero": lambda r, f: r.value_status == rc.REPORTED_ZERO,
    "awaiting": lambda r, f: bool(r.awaiting_reports),
    "differs": lambda r, f: r.mismatch,
    "dq_without": lambda r, f: _dq_reached(r, f) is None,
    "dq_zero": lambda r, f: _dq_reached(r, f) == 0,
    "missing_target": lambda r, f: not r.target,
    "no_reached": lambda r, f: _dq_reached(r, f) is None or _dq_reached(r, f) == 0,
    "partial": lambda r, f: r.partial,
    "running": lambda r, f: r.pd.status in pd_statuses.RUNNING,
    "active": lambda r, f: r.pd.status in pd_statuses.ACTIVE,
    "ended": lambda r, f: r.pd.status in pd_statuses.ENDED,
    **{f"tracking.{key}": (lambda r, f, key=key: r.tracking == key) for key in LABELS},
}
# what a figure of programme documents or partners counts once: the indicator's PD, its linked partner (a PD
# with no linked partner adds none)
FIGURE_KEYS: dict[str, Callable[[Any], Any]] = {
    "pds": lambda r: r.pd.id,
    "partners": lambda r: r.pd.partner_id or None,
}


def counted(rows: list[Any], test: str, filters: Filters) -> int:
    """The indicators of ``rows`` a :data:`FIGURE_TESTS` test keeps."""
    check = FIGURE_TESTS[test]
    return sum(1 for r in rows if check(r, filters))


def distinct(rows: list[Any], key: str) -> int:
    """The programme documents or partners (:data:`FIGURE_KEYS`) of ``rows``."""
    return len({FIGURE_KEYS[key](r) for r in rows} - {None})


def overdue_rows(
    pd_ids: Any, report_type: str, today: datetime.date, steps: list | None = None
) -> QuerySet[dm.ReportedIndicator]:
    """The partner report rows behind Partner monitoring's Overdue reports: of these programme documents and
    report type, not submitted (:func:`pending_q`), due before ``today``. ``steps``: as :func:`_pd_queryset`'s
    (the figure trail's record of each filter)."""
    rows = _stage(
        steps,
        "rows",
        dm.ReportedIndicator.objects.filter(intervention_id__in=pd_ids, report_type=report_type),
    )
    rows = _stage(steps, "pending", rows.filter(pending_q()))
    return _stage(steps, "due", rows.filter(due_date__lt=today))


def summary(rows: list[Indicator], filters: Filters, today: datetime.date | None = None) -> dict[str, Any]:
    from . import owed
    from .reporting import report_count

    today = today or datetime.date.today()
    counts = Counter(r.tracking for r in rows)
    pd_ids = {FIGURE_KEYS["pds"](r) for r in rows}
    # progress reports, each its PD, type, number and period (reached.report_key); with the Overdue reports
    # still owed in PRP's own list that the partner reports do not have, once their link is proven (owed)
    overdue = report_count(overdue_rows(pd_ids, filters.report_type, today))
    overdue += owed.overdue_count(pd_ids, filters.report_type, today)
    with_reached = counted(rows, "with_reached", filters)
    return {
        "indicators": len(rows),
        "programme_documents": len(pd_ids),
        "partners": distinct(rows, "partners"),
        "with_target": counted(rows, "with_target", filters),
        "with_reached": with_reached,
        "reported": with_reached,
        "reported_zero": counted(rows, "reported_zero", filters),
        "awaiting_acceptance": counted(rows, "awaiting", filters),
        "differs_from_prp": counted(rows, "differs", filters),
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
    "emergency": lambda r: r.emergency,
    "indicator": lambda r: norm(r.title) or None,
    "target": lambda r: r.target,
    "reached": lambda r: r.reached,
    "achieved": lambda r: r.achieved,
    "reached_in_year": lambda r: r.reached_in_year,
    "achieved_in_year": lambda r: r.achieved_in_year,
    "partner": lambda r: norm(_partner_name(r)) or None,
    "pd": lambda r: (r.pd.number or "").lower() or None,
}
DEFAULT_SORT = "-achieved"  # % Reached, highest first, as Power BI opens Program Performance
YEAR_SORT = "-achieved_in_year"  # with a Reporting year: % Reached in that year, highest first


def sorted_rows(rows: list[Indicator], sort: str, year: int | None = None) -> tuple[list[Indicator], str]:
    """The indicators in the order of ``sort`` (a column of :data:`TABLE_SORTS`, "-" first for highest
    first), ties by PD and title; indicators without a value go last either way. Also the sort applied.
    By default % Reached, highest first; with a ``year``, % Reached in that year."""
    key, desc = (sort or "").lstrip("-"), (sort or "").startswith("-")
    if key not in TABLE_SORTS or (key in ("reached_in_year", "achieved_in_year") and not year):
        key, desc = (YEAR_SORT if year else DEFAULT_SORT).lstrip("-"), True
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
    "achieved_percent_in_year",
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
            "emergency": "Yes" if r.emergency else "No",  # the PD is an SPD
            "pd_output": r.output,
            "indicator": r.title,
            "unit": r.unit,
            "baseline": r.baseline,
            "target": r.target,
            "reached": r.reached,
            "reached_as_of": r.as_of,
            "reached_in_year": r.reached_in_year if filters.year else None,
            "achieved_percent_in_year": (
                round(r.achieved_in_year, 1) if filters.year and r.achieved_in_year is not None else None
            ),
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
    pds = {FIGURE_KEYS["pds"](r): r.pd for r in rows}
    tests = FIGURE_TESTS
    without = [r for r in rows if tests["dq_without"](r, filters)]
    zero = [r for r in rows if tests["dq_zero"](r, filters)]
    missing_target = [r for r in rows if tests["missing_target"](r, filters)]
    titles: dict[tuple[int, str], list[Indicator]] = defaultdict(list)
    for r in rows:
        titles[(r.pd.id, norm(r.title))].append(r)

    # a title two or more indicators of a PD share: the partner reports' rows are placed by their PD output,
    # and where they share the output too by the tie-breakers the data proves (neurodb.datamart.identity). An
    # indicator is left out when it gets none of its rows while rows of its title were left out; sharing an
    # output alone is not enough (a tie-breaker may have placed it, or PRP may report only its twin)
    def left_out(group: list[Indicator]) -> int:
        return sum(1 for r in group if not r.rows_placed and r.title_rows_left_out)

    same_title = [
        {
            "pd": group[0].pd,
            "title": group[0].title,
            "count": len(group),
            "left_out": left_out(group),
            "rows_left_out": group[0].title_rows_left_out,
            "rows_unmatched": group[0].title_rows_unmatched,
            "rows": group,
            "figure": f"{group[0].pd.id}.{norm(group[0].title)}",  # its figures' keys (the figure trail's)
        }
        for group in titles.values()
        if len(group) > 1
    ]
    same_title.sort(key=lambda g: ((g["pd"].number or "").lower(), norm(g["title"])))
    partial = sorted(
        {r.pd.id: r.pd for r in rows if tests["partial"](r, filters)}.values(),
        key=lambda pd: pd.start or datetime.date.min,
    )
    history = rc.history_from(today) if partial else None

    def order(items: list[Indicator]) -> list[Indicator]:
        return sorted(items, key=lambda r: (norm(r.section), (r.pd.number or "").lower(), norm(r.title)))

    no_reached = order(without + zero)
    mismatch = order([r for r in rows if tests["differs"](r, filters)])

    def pds_of(test: str) -> int:  # a PD's status is each of its indicators': counted once per PD
        return len({FIGURE_KEYS["pds"](r) for r in rows if tests[test](r, filters)})

    return {
        "programme_documents": len(pds),
        "active_pds": pds_of("active"),
        "running_pds": pds_of("running"),
        "ended_pds": pds_of("ended"),
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
        "same_title_left_out": sum(g["left_out"] for g in same_title),
        # the rows of those titles no indicator got, and of them those whose PD output matches none
        "same_title_rows_left_out": sum(g["rows_left_out"] for g in same_title),
        "same_title_rows_unmatched": sum(g["rows_unmatched"] for g in same_title),
        "partial": partial[:QUALITY_LIMIT],
        "partial_count": len(partial),
        # where the history is counted from (the feed's first report when the Datamart holds none earlier)
        "read_from": history.date if history else None,
        "read_from_source": history.source if history else "",
        "read_from_asked": history.asked if history else None,
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


def stored_options(filters: Filters) -> dict[str, Any]:
    """:func:`filter_options` for the frames of the partner monitoring page and its map: stored ten minutes
    for every worker under a key the eTools jobs' runs change (:func:`neurodb.datamart.keys.fingerprint`),
    the same for every user and language. A frame that finds another request computing them computes them
    itself rather than wait for it (35 ms on a large database): the frame comes at once. The reporting years
    fall back on the filter's year only when no partner report has a period end, after the stored value is
    read. In DEBUG they are read at each call."""
    if settings.DEBUG:
        return filter_options(filters)
    key = f"pm-options:v{CACHE_VERSION}:{keys.fingerprint()}"
    found = compute.once(
        key, lambda: filter_options(Filters(year=None)), OPTIONS_SECONDS, wait=0, lang_neutral=True
    )
    if found["years"] or not filters.year:
        return found
    return {**found, "years": [filters.year]}


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
        "progress_report": report.progress_report,
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
    newest_first = sorted(
        periods,
        key=lambda p: (p["end"] or datetime.date.min, p["progress_report"], p["report_number"], p["key"]),
        reverse=True,
    )
    return {
        "pd": pd,
        "indicator": indicator,
        "result": result,
        "definition": first,
        "disaggregations": sorted({x for x in same.values_list("disaggregation_name", flat=True) if x}),
        "planned_locations": sorted({x for x in same.values_list("location_name", flat=True) if x}),
        "periods": periods,
        "by_location": by_location,
        "by_disaggregation": disaggregation.reached_by_disaggregation(result),
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
