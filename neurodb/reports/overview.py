"""The country overview's data: impact on children, value for money, delivery and assurance,
assurance progress, and the freshness of every source.

One call to :func:`build` reads the programme document indicators once (through the partner
monitoring service, which applies the tracking rule and attaches what partners reported), the
ActivityInfo HPM masters that count children, the funds reservations, the TPM visits, the action
points and the monitoring findings of the year, and shapes them for the overview page. The two
reporting sources are kept apart: an eTools figure and an ActivityInfo figure are only added into
one number where the page says so.

Filters: the year, eTools section names (empty = every section) and one governorate (a gazetteer
governorate name; empty = the country). The result is kept for two minutes per filter in the cache that
every worker shares (:func:`neurodb.core.compute.once`: the overview's parts, requested separately, read
one build), and the key changes as soon as a sync run finishes or an indicator flag changes. Nothing in a
build depends on who asks: the user's own section only reaches it as the ``sections`` of the scope,
which are in the key, so one person's figures are never shown to another.

The filter bar's choices (:func:`options`) and the freshness strip (:func:`freshness`) are kept the same
way, for the page's frame, which draws them before any figure is computed.
"""

from __future__ import annotations

import datetime
import logging
import math
import re
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

from django.conf import settings
from django.db.models import Count, Max, Q, Sum
from django.urls import reverse

from neurodb.core import compute
from neurodb.datamart import fm, headline, monitoring
from neurodb.datamart import models as dm
from neurodb.datamart import reached as rc
from neurodb.datamart.children import counts_children
from neurodb.datamart.children import overrides as children_overrides
from neurodb.datamart.monitoring import ACTIVE_PD_STATUSES, NOT_REPORTED, Filters, Indicator
from neurodb.facts import queries as fact_queries
from neurodb.facts.services.dashboard import fact_filter
from neurodb.geo.models import Location
from neurodb.indicators.models import Database, NeuroReportMasterIndicator
from neurodb.indicators.services.tracking import OFF_TRACK, percentage_elapsed
from neurodb.web.templatetags.ui import half_up

logger = logging.getLogger(__name__)

CACHE_SECONDS = 120  # a build, and the freshness strip
OPTIONS_SECONDS = 600  # the filter bar's choices (they only change with a sync)
CACHE_VERSION = 10  # 10: the freshness strip left the build (freshness())
FRESHNESS_JOBS = ("ai_data", "etools_datamart", "locations", "daily_review")
MONTH_LABELS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
STATUSES = ("on_track", "off_track", "over_target", "no_target", NOT_REPORTED)
LEVEL_GOVERNORATE = monitoring.LEVEL_GOVERNORATE
TPM_NOT_PLANNED = ("draft", "cancelled")
TPM_COMPLETED = ("tpm_reported", "unicef_approved")
TPM_OPEN = ("assigned", "tpm_accepted")
TPM_REPORT_GRACE_DAYS = 14
AP_OPEN = dm.ActionPoint.OPEN_STATUSES
MODULES = {"tpm": "TPM", "fm": "Field monitoring", "audit": "Audit"}
DECISION_ENDING_DAYS = 90
DECISION_ELAPSED = 60
DECISION_DISBURSED = 40
MAX_ATTENTION = 8
# A children indicator above this many times its target (and above the floor) is set aside as an
# error rather than added to the children reached; so is one above the country's child population.
IMPLAUSIBLE_TIMES = headline.IMPLAUSIBLE_TIMES  # the set-aside guard (neurodb.datamart.headline.implausible)
IMPLAUSIBLE_FLOOR = headline.IMPLAUSIBLE_FLOOR
MAX_DONORS = 8
HIGH_RISK = ("high", "significant")
SEX_RULES = (("Girls", r"\bgirls?\b", r"\bboys?\b"), ("Boys", r"\bboys?\b", r"\bgirls?\b"))
NATIONALITY_OF_TAG = {"Lebanese": "Lebanese", "Syrian": "Syrian", "Palestinian": "Palestinian"}
COST_CAVEAT = (
    "Disbursed to date (supplies and operating costs included) over the children reached this year in "
    "eTools partner reports (each programme document's main indicator), the same programmes on both "
    "sides: compare sections, not absolute values."
)


@dataclass
class Scope:
    year: int
    reporting_year: Any = None  # the ActivityInfo ReportingYear, None when none is configured
    sections: list[str] = field(default_factory=list)
    governorate: str = ""
    today: datetime.date = field(default_factory=datetime.date.today)


# --------------------------------------------------------------------------------- names
_GOVERNORATE_ALIASES = {
    "bekaa": "beqaa",
    "bequaa": "beqaa",
    "beqaa": "beqaa",
    "baalbekhermel": "baalbekhermel",
    "baalbekelhermel": "baalbekhermel",
    "baalbek": "baalbekhermel",
    "nabatieh": "nabatieh",
    "nabatiyeh": "nabatieh",
    "nabatiye": "nabatieh",
    "northlebanon": "north",
    "north": "north",
    "southlebanon": "south",
    "south": "south",
    "mountlebanon": "mountlebanon",
    "mountlebanonexceptbeirut": "mountlebanon",
    "beirut": "beirut",
    "akkar": "akkar",
    # French, as ActivityInfo writes them in production ("Mont Liban", "Baalbek_Hermel"…)
    "montliban": "mountlebanon",
    "nord": "north",
    "sud": "south",
    "beyrouth": "beirut",
}


def governorate_key(name: str | None) -> str:
    """One key per governorate whatever the source's spelling: "Bekaa" and "Beqaa", "Baalbek - El
    Hermel" and "Baalbek-Hermel", "Nabatieh Governorate" and "El Nabatieh" all match."""
    text = (name or "").lower()
    text = re.sub(r"\bgovernorate\b|\bmohafaza\b|\bmohafazat\b", " ", text)
    text = re.sub(r"\b(el|al)\b", " ", text)
    text = re.sub(r"[^a-z]", "", text)
    return _GOVERNORATE_ALIASES.get(text, text)


def section_matches(name: str | None, code: str | None, sections: list[str]) -> bool:
    """Whether a NeuroDB section (ActivityInfo) is one of the eTools section names selected, with the
    matching of the partner monitoring page's default section (whole, or contained either way)."""
    if not sections:
        return True
    wanted = {monitoring.norm(name or ""), monitoring.norm(code or "")} - {""}
    for option in sections:
        opt = monitoring.norm(option)
        if opt in wanted or any(len(w) >= 3 and (w in opt or opt in w) for w in wanted):
            return True
    return False


def _link(name: str, *args: Any, **params: Any) -> str:
    url = reverse(name, args=args)
    query = [
        (k, v) for k, values in params.items() for v in (values if isinstance(values, list) else [values])
    ]
    query = [(k, v) for k, v in query if v not in (None, "")]
    return f"{url}?{urlencode(query)}" if query else url


def _pct(part: float | None, whole: float | None) -> float | None:
    if part is None or not whole:
        return None
    return half_up(part * 100 / whole, 1)


def _money(value: Any) -> float:
    return float(value or 0)


def _levels(values: list[float]) -> list[int]:
    """1..6 for the governorate tiles: the share of the largest value, rounded up (0 stays level 1)."""
    top = max(values, default=0)
    return [max(1, math.ceil(v * 6 / top)) if top else 1 for v in values]


def governorate_names() -> dict[str, str]:
    """``{governorate key: the gazetteer's name}``: one spelling of each governorate on every page."""
    names = Location.objects.filter(type__admin_level=LEVEL_GOVERNORATE, is_active=True).values_list(
        "name", flat=True
    )
    return {governorate_key(name): name for name in sorted(set(names))}


# --------------------------------------------------------------------------------- options
def options(year: int) -> dict[str, list[str]]:
    """The filter bar's choices: eTools section names of the PD indicators and gazetteer governorates.
    Kept ten minutes in the shared cache; a finished sync (new indicators, a new gazetteer) changes the
    key. A frame that finds another request computing them computes them itself (a few milliseconds)
    rather than wait for that request's lock, which is looked at every 200 ms: the frame comes at once."""
    if settings.DEBUG:
        return _options()
    key = f"overview-options:v{CACHE_VERSION}:{year}:{compute.data_stamp()}"
    return compute.once(key, _options, OPTIONS_SECONDS, wait=0, lang_neutral=True)  # names, never translated


def _options() -> dict[str, list[str]]:
    names = dm.PDIndicator.objects.order_by().values_list("section_name", flat=True).distinct()
    sections = sorted({(s or "").strip() for s in names} - {""})
    governorates = sorted(
        set(
            Location.objects.filter(type__admin_level=LEVEL_GOVERNORATE, is_active=True).values_list(
                "name", flat=True
            )
        )
    )
    return {"sections": sections, "governorates": governorates}


# --------------------------------------------------------------------------------- ActivityInfo
def children_masters(reporting_year: Any, sections: list[str], flags: Any) -> dict[int, tuple[Any, set[int]]]:
    """``{database id: (database, master ids)}``: the additive (SUM) HPM master indicators of the
    reporting year that count children (title rule or flag), in the databases shown on the dashboard
    and in the selected sections. Shared by the overview and the management brief."""
    links = (
        NeuroReportMasterIndicator.objects.filter(report__is_hpm=True, report__ryear=reporting_year)
        .select_related("master", "master__database", "master__database__section")
        .exclude(master=None)
    )
    by_database: dict[int, tuple[Database, set[int]]] = {}
    for link in links:
        master = link.master
        database = master.database
        if database is None or not database.display:
            continue
        if (master.aggregation_method or "SUM").upper() != "SUM":
            continue
        section = database.section
        if not section_matches(section.name if section else "", section.code if section else "", sections):
            continue
        label = link.label or master.name
        if not counts_children("activityinfo", str(master.id), label, None, flags, unit=""):
            continue
        by_database.setdefault(database.id, (database, set()))[1].add(master.id)
    return by_database


# --------------------------------------------------------------------------------- build
def latest_sync_run() -> int | None:
    """The id of the sync run that finished last (any job, any outcome). In the cache keys of the
    overview and the brief, so every worker drops its copy as soon as a sync ends."""
    from neurodb.core.models import SyncRun

    return (
        SyncRun.objects.exclude(finished_at=None)
        .order_by("-finished_at", "-id")
        .values_list("id", flat=True)
        .first()
    )


def flags_fingerprint() -> str:
    """The indicator flags (children, unique beneficiaries) as a short text that changes whenever one is
    added, changed or removed: in the cache keys of every page that reads the headline rule."""
    from neurodb.datamart.models import IndicatorFlag

    flags = IndicatorFlag.objects.aggregate(n=Count("pk"), last=Max("updated_at"))
    last = flags["last"].isoformat() if flags["last"] else ""
    return f"{flags['n']}:{last}".replace(" ", "_")  # no spaces: memcached-safe


def _cache_key(scope: Scope) -> str:
    """Everything the result depends on, the indicator flags and the last finished sync included (a new
    flag or a sync shows at once). Nothing about the person asking: a build depends on its scope only."""
    sections = "|".join(sorted(scope.sections))
    ryear = getattr(scope.reporting_year, "pk", "")
    return (
        f"overview:v{CACHE_VERSION}:{scope.year}:{ryear}:{sections}:{scope.governorate}:{scope.today}:"
        f"{compute.data_stamp()}"  # the last finished sync run and the flags fingerprint
    ).replace(" ", "_")


def build(scope: Scope, *, cache: bool = True) -> dict[str, Any]:
    """Every block of the overview for ``scope`` (see the module docstring): computed once across every
    worker and kept two minutes. ``cache=False`` (and DEBUG) computes it afresh."""
    if not cache or settings.DEBUG:
        return _Builder(scope).build()
    return compute.once(_cache_key(scope), lambda: _Builder(scope).build(), CACHE_SECONDS)


def stored(scope: Scope) -> bool:
    """Whether :func:`build` would answer ``scope`` at once, from what is already stored: the page's
    frame then draws the figures inline instead of asking for them (it never computes them itself)."""
    if settings.DEBUG:
        return False
    return compute.peek(_cache_key(scope))[0]


def children_population(year: int) -> tuple[dict[str, tuple[str, int]], int | None]:
    """``{governorate key: (name, children)}`` of the latest year of child figures up to ``year``, and
    that year (the overview's coverage, and the child population the set-aside guard compares with)."""
    from neurodb.core.models import PopulationFigure

    base = PopulationFigure.objects.filter(
        category="children",
        level="governorate",
        age_group="",
        sex="",
        vulnerability_level="",
        year__lte=year,
    )
    found = base.order_by("-year").values_list("year", flat=True).first()
    if found is None:
        return {}, None
    rows = base.filter(year=found).values("area_name", "nationality", "value")
    with_all = {governorate_key(r["area_name"]) for r in rows if r["nationality"] == "ALL"}
    out: dict[str, list[Any]] = {}
    for r in rows:
        key = governorate_key(r["area_name"])
        if not key or (key in with_all and r["nationality"] != "ALL"):
            continue
        entry = out.setdefault(key, [r["area_name"], 0])
        entry[1] += r["value"] or 0
    return {k: (v[0], v[1]) for k, v in out.items()}, found


class _Builder:
    def __init__(self, scope: Scope):
        self.scope = scope
        self.today = scope.today
        self.year = scope.year
        self.year_start = datetime.date(scope.year, 1, 1)
        self.year_end = datetime.date(scope.year, 12, 31)
        self.flags = children_overrides()
        self.gov_key = governorate_key(scope.governorate) if scope.governorate else ""

    # ------------------------------------------------------------------ shared reads
    def _rows(self) -> list[Indicator]:
        """The PD indicators of the scope (partner monitoring rows, read once), narrowed to the
        governorate when one is chosen. ``every_rows``: every indicator of the same PDs, whatever its
        section, among which each PD's main indicator is chosen (:mod:`neurodb.datamart.headline`), so a
        PD counts once and in one section only."""
        rows = monitoring.indicators(
            Filters(
                year=self.year,
                sections=list(self.scope.sections),
                report_type=monitoring.DEFAULT_REPORT_TYPE,
                scope="year",
            ),
            self.today,
        )
        every = rows
        if self.scope.sections and rows:
            computed = {(r.pd.id, r.key): r for r in rows}
            others = monitoring.indicators(
                Filters(
                    year=self.year,
                    pd_ids=sorted({r.pd.id for r in rows}),
                    report_type=monitoring.DEFAULT_REPORT_TYPE,
                    scope="year",
                ),
                self.today,
            )
            every = [computed.get((r.pd.id, r.key), r) for r in others]
        ids: set[int] = set()
        for row in every:
            ids.update(p["id"] for p in row.planned.values() if p.get("id"))
            ids.update(p["id"] for p in row.by_location.values() if p.get("id"))
        self.gazetteer = monitoring._gazetteer(ids)
        self.governorate_of: dict[int, str] = {}
        self.district_of: dict[int, str] = {}
        for location_id in ids:
            place = monitoring._place(location_id, self.gazetteer)
            self.governorate_of[location_id] = place["governorate"]
            self.district_of[location_id] = place["district"]
        self.every_rows = every  # every section of the PDs in scope, the whole country
        self.all_rows = rows  # the sections chosen, the whole country
        if self.gov_key:
            rows = [r for r in rows if self.gov_key in self._governorate_keys(r)]
        return rows

    def _choose_mains(self) -> None:
        """Each PD's main indicator (its unique beneficiaries indicator, else its largest children
        indicator by Reached in the year: :func:`neurodb.datamart.headline.choose`), and the children
        indicators that can be added: a value too large to be true is set aside and listed, never added
        nor chosen (a flagged indicator's too)."""
        flags = headline.flagged_keys({r.pd.id for r in self.every_rows})
        aside = self._implausible(
            headline.candidates(self.every_rows, self._is_children, flags),
            self._national_children(),
            self.year,
        )
        aside_ids = {id(r) for r, _reason in aside}
        in_scope = {id(r) for r in self.rows}
        in_sections = {id(r) for r in self.all_rows}
        self.children_rows = [
            r
            for r in self.every_rows
            if self._is_children(r) and id(r) in in_scope and id(r) not in aside_ids
        ]
        self.implausible = [_aside_entry(r, reason) for r, reason in aside if id(r) in in_scope]
        self.mains = headline.choose(
            self.every_rows,
            is_children=self._is_children,
            year=self.year,
            flags=flags,
            exclude=[r for r, _reason in aside],
        )
        # the PDs whose main indicator is in the sections shown: each counts there, and only there
        self.scope_mains = {pd: m for pd, m in self.mains.items() if id(m.row) in in_sections}

    def _governorate_keys(self, row: Indicator) -> set[str]:
        places = list(row.planned.values()) + list(row.by_location.values())
        return {governorate_key(self.governorate_of.get(p.get("id"))) for p in places if p.get("id")} - {""}

    def _is_children(self, row: Indicator) -> bool:
        return counts_children(
            "etools",
            row.key,
            row.title,
            row.tags.get("age_group"),
            self.flags,
            display_type=row.display_type,
            unit=row.unit,
        )

    @staticmethod
    def _additive(row: Indicator) -> bool:
        """Whether the indicator's locations add up (PRP "sum", a number); a max, an average or a
        ratio never does."""
        return row.method in ("", "sum") and not (row.result is not None and row.result.ratio)

    def _places_in(self, row: Indicator, key: str):
        for place in row.by_location.values():
            if governorate_key(self.governorate_of.get(place.get("id"))) == key:
                yield place

    def _year_value(self, row: Indicator) -> float:
        """The indicator's Reached in the year (:mod:`neurodb.datamart.reached`: its counted reports
        combined by PRP's methods); with a governorate chosen, what its locations there reached
        (indicators summed across locations only: a max across locations cannot be split)."""
        if not self.gov_key:
            return row.reached_in_year or 0.0
        if not self._additive(row):
            return 0.0
        return sum(place.get("achieved") or 0 for place in self._places_in(row, self.gov_key))

    def _months(self, row: Indicator) -> dict[int, float]:
        if not self.gov_key:
            return row.months
        out: dict[int, float] = defaultdict(float)
        if self._additive(row):
            for place in self._places_in(row, self.gov_key):
                for month, value in (place.get("months") or {}).items():
                    out[month] += value or 0
        return out

    def _to_month(self, row: Indicator, last: int) -> float:
        """Reached from January to the end of month ``last``: the months combined by the indicator's
        method across periods (a sum adds them, a max or the latest does not); with a governorate, its
        locations there, each combined the same way."""

        def upto(months: dict[int, float]) -> float:
            values = [v for m, v in sorted(months.items()) if m <= last and v is not None]
            return (rc.combine(values, row.period_method or "sum") or 0.0) if values else 0.0

        if not self.gov_key:
            return upto(row.months)
        if not self._additive(row):
            return 0.0
        return sum(upto(place.get("months") or {}) for place in self._places_in(row, self.gov_key))

    # ------------------------------------------------------------------ build
    def build(self) -> dict[str, Any]:
        rows = self._rows()
        self.rows = rows
        self._choose_mains()
        self.pds = {r.pd.id: r.pd for r in rows}
        self.section_of_pd = self._pd_sections()
        activityinfo = self._activityinfo_children()
        impact = self._impact(activityinfo)
        money = self._money(impact)
        delivery = self._delivery()
        progress = self._progress()
        return {
            "impact": impact,
            "money": money,
            "delivery": delivery,
            "progress": progress,
            "scope": {
                "year": self.year,
                "sections": list(self.scope.sections),
                "governorate": self.scope.governorate,
                "today": self.today.isoformat(),
                # For the management brief, which reads the same programme documents:
                "pd_ids": sorted(self.pds),
                "section_of_pd": {str(pd_id): s for pd_id, s in self.section_of_pd.items()},
            },
        }

    def _pd_sections(self) -> dict[int, str]:
        """One section per PD for the money blocks: the section most of its indicators belong to."""
        counts: dict[int, Counter[str]] = defaultdict(Counter)
        for row in self.rows:
            counts[row.pd.id][row.section or "Other"] += 1
        return {pd_id: c.most_common(1)[0][0] for pd_id, c in counts.items()}

    # ------------------------------------------------------------------ ActivityInfo
    def _activityinfo_children(self) -> dict[str, Any]:
        """ActivityInfo children reached: the additive (SUM) HPM masters of the year whose label names
        children (or that a flag adds), by month and by governorate."""
        out: dict[str, Any] = {
            "total": 0.0,
            "months": [0.0] * 12,
            "by_governorate": defaultdict(float),
            "names": {},
        }
        ryear = self.scope.reporting_year
        if ryear is None:
            return out
        by_database = children_masters(ryear, self.scope.sections, self.flags)
        for database, master_ids in by_database.values():
            try:
                rows = fact_queries.master_values_by_area(fact_filter(database), sorted(master_ids))
            except Exception:  # one database's facts must not break the page
                logger.exception("overview: ActivityInfo values of database %s failed", database.pk)
                continue
            for r in rows:
                value = float(r["value"] or 0)
                gov = r["governorate"] or ""
                key = governorate_key(gov)
                if self.gov_key and key != self.gov_key:
                    continue
                out["total"] += value
                month = r["month_num"] or ""
                if month.isdigit() and 1 <= int(month) <= 12:
                    out["months"][int(month) - 1] += value
                if key:
                    out["by_governorate"][key] += value
                    out["names"].setdefault(key, gov)
        return out

    # ------------------------------------------------------------------ impact
    def _impact(self, activityinfo: dict[str, Any]) -> dict[str, Any]:
        """Children reached: per source, by month, governorate and district. The eTools figure is the
        sum over the programme documents of each one's main indicator (never several indicators of one
        PD); ActivityInfo's is its additive children indicators. The two are shown apart and the
        headline is the larger, never their sum."""
        etools_months = [0.0] * 12
        etools_to_date = [0.0] * 12
        etools_total = 0.0
        by_gov: dict[str, float] = defaultdict(float)
        gov_names: dict[str, str] = {}
        by_district: dict[tuple[str, str], float] = defaultdict(float)
        rules: Counter[str] = Counter()
        awaiting = 0.0  # of the eTools figure, from reports awaiting acceptance (the whole country only)
        for main in self.scope_mains.values():
            row = main.row
            value = self._year_value(row)
            etools_total += value
            if value:
                rules[main.rule] += 1
                if not self.gov_key and row.result is not None:
                    awaiting += row.result.awaiting_in(self.year) or 0.0
            for month, month_value in self._months(row).items():
                if month_value and 1 <= month <= 12:
                    etools_months[month - 1] += month_value
            for month in range(1, 13):
                etools_to_date[month - 1] += self._to_month(row, month)
            if not self._additive(row):
                continue  # a max or an average across locations is not split by governorate
            for place in row.by_location.values():
                gov = self.governorate_of.get(place.get("id")) or ""
                key = governorate_key(gov)
                if place.get("achieved") and key and (not self.gov_key or key == self.gov_key):
                    by_gov[key] += place["achieved"]
                    gov_names.setdefault(key, gov)
                    district = self.district_of.get(place.get("id")) or ""
                    if district:
                        by_district[(district, gov)] += place["achieved"]
        population, population_year = self._children_population()
        keys = (
            set(by_gov)
            | set(activityinfo["by_governorate"])
            | (set(population) if not self.gov_key else set())
        )
        if self.gov_key:
            keys = {k for k in keys | set(population) if k == self.gov_key}
        gazetteer_names = governorate_names()
        governorates = []
        for key in keys:
            etools = half_up(by_gov.get(key, 0))
            ai = half_up(activityinfo["by_governorate"].get(key, 0))
            reached = max(etools, ai)
            pop = population.get(key, (None, None))[1]
            if not reached and not pop:
                continue
            governorates.append(
                {
                    # the gazetteer's spelling first, as in the filter ("Beqaa", never "Bekaa" beside it)
                    "name": gazetteer_names.get(key)
                    or gov_names.get(key)
                    or population.get(key, (None, None))[0]
                    or activityinfo["names"].get(key)
                    or key,
                    "etools": etools,
                    "activityinfo": ai,
                    "reached": reached,
                    "population": pop,
                    "coverage": _pct(reached, pop) if pop else None,
                }
            )
        governorates.sort(key=lambda g: (-g["reached"], g["name"]))
        for g, level in zip(governorates, _levels([g["reached"] for g in governorates]), strict=True):
            g["level"] = level
        etools_value = half_up(etools_total)
        ai_value = half_up(activityinfo["total"])
        return {
            # The same children are often reported in both sources: the headline is the larger one.
            "children_reached": max(etools_value, ai_value),
            "children_etools": etools_value,
            "children_activityinfo": ai_value,
            "children_rule": headline.TOTAL_RULE,
            # of children_etools, from Submitted reports not yet accepted (a send-back takes it off);
            # None with a governorate chosen (not split by place)
            "children_awaiting": None if self.gov_key else half_up(awaiting),
            # how many programme documents add a value, by how their main indicator was chosen
            "main_indicators": {
                "flagged": rules.get(headline.FLAGGED, 0),
                "largest": rules.get(headline.LARGEST, 0),
            },
            "delta_previous_year": None,
            "by_governorate": governorates,
            "by_district": sorted(
                (
                    {"district": d, "governorate": g, "etools": half_up(v)}
                    for (d, g), v in by_district.items()
                    if half_up(v)
                ),
                key=lambda r: (-r["etools"], r["district"]),
            ),
            "by_section": self._achievement_by_section(),
            "achievement": self._achievement(),
            "main_by_section": self._main_by_section(),
            "children_by_tag": self._children_by_tag(),
            "monthly": {
                "labels": list(MONTH_LABELS),
                "etools": [half_up(v) for v in etools_months],
                # Reached from January to each month (what the months add up to for a summed indicator;
                # a max or the latest is not added up)
                "etools_to_date": [half_up(v) for v in etools_to_date],
                "activityinfo": [half_up(v) for v in activityinfo["months"]],
            },
            "population_year": population_year,
            "implausible": self.implausible,
            "source": (
                f"eTools PRP progress reports, {self.year}: each programme document's main indicator (its "
                "unique beneficiaries indicator, else its largest children indicator), summed over the "
                "programme documents, as partners may reach the same children; and the additive "
                f"ActivityInfo HPM indicators that count children, {self.year}. The two sources are shown "
                "apart, and a total is the larger of the two"
            ),
        }

    def _children_by_tag(self) -> list[dict[str, Any]]:
        """Each programme document's main indicator, its value of the year and what its title names: one
        sex (girls without boys, or the reverse), an age band, a nationality, disability. For the
        management brief's equity blocks; a title naming both sexes names neither."""
        out = []
        for main in self.scope_mains.values():
            row = main.row
            value = self._year_value(row)
            if not value:
                continue
            out.append({"value": half_up(value), **_title_tags(row)})
        return out

    def _main_by_section(self) -> list[dict[str, Any]]:
        """Per section, the programme documents whose main indicator is in it: the children reached in
        the year and to date, the main indicators' targets and what was expected by now (target × share
        of each PD's period elapsed). Sums over PDs of one indicator each (the headline rule)."""
        groups: dict[str, dict[str, float]] = {}
        for main in self.scope_mains.values():
            row = main.row
            g = groups.setdefault(
                row.section or "Other",
                {"children": 0.0, "reached": 0.0, "target": 0.0, "expected": 0.0, "programme_documents": 0},
            )
            g["programme_documents"] += 1
            g["children"] += self._year_value(row)
            if row.target:
                g["reached"] += row.reached or 0.0
                g["target"] += row.target
                g["expected"] += row.target * percentage_elapsed(row.pd.start, row.pd.end, self.today) / 100
        return [
            {
                "section": section,
                "programme_documents": g["programme_documents"],
                "children": half_up(g["children"]),
                "reached": half_up(g["reached"]),
                "target": half_up(g["target"]),
                "expected": half_up(g["expected"]),
                "percent": _pct(g["reached"], g["target"]),
            }
            for section, g in sorted(groups.items())
        ]

    def _achievement_by_section(self) -> list[dict[str, Any]]:
        """Per section, the children indicators with a target: how many, how many with Reached, their
        statuses, and the median % of the target reached (each capped at 100), against the median share
        of their PD period elapsed. Never a sum of different indicators."""
        groups: dict[str, list[Indicator]] = defaultdict(list)
        for row in self.children_rows:
            if row.target:
                groups[row.section or "Other"].append(row)
        out = []
        for section, rows in sorted(groups.items()):
            block = self._median_achievement(rows)
            # for the chart: the median share of the target, out of 100
            out.append(
                {"section": section, **block, "achieved": block["percent"], "target": 100.0, "unit": "%"}
            )
        return out

    def _achievement(self) -> dict[str, Any]:
        """The same as one section's achievement, over every children indicator with a target in scope
        (the brief's "Achievement of PD targets")."""
        return self._median_achievement([r for r in self.children_rows if r.target])

    def _median_achievement(self, rows: list[Indicator]) -> dict[str, Any]:
        statuses = Counter(r.tracking for r in rows)
        percents, elapsed, every_elapsed = [], [], []
        for row in rows:
            share = percentage_elapsed(row.pd.start, row.pd.end, self.today)
            every_elapsed.append(share)
            if row.achieved is not None and row.reached is not None:
                percents.append(min(row.achieved, 100.0))
                elapsed.append(share)
        elapsed = elapsed or every_elapsed
        return {
            "indicators": len(rows),
            "with_reached": len(percents),
            **{k: statuses.get(k, 0) for k in STATUSES},
            "percent": half_up(statistics.median(percents), 1) if percents else None,
            "elapsed": half_up(statistics.median(elapsed), 1) if elapsed else None,
        }

    def _national_children(self) -> int | None:
        by_governorate, _year = self._children_population()
        return sum(n for _name, n in by_governorate.values()) or None

    @staticmethod
    def _implausible(
        rows: list[Indicator], national_children: int | None, year: int | None = None
    ) -> list[tuple[Indicator, str]]:
        """The indicators whose figures cannot be true (:func:`neurodb.datamart.headline.implausible`):
        never added nor chosen as a PD's main indicator, and listed on the page so someone checks them in
        eTools."""
        return headline.implausible(rows, national_children, year)

    def _children_population(self) -> tuple[dict[str, tuple[str, int]], int | None]:
        """``{governorate key: (name, children)}`` of the latest year of child figures up to the scope's."""
        return children_population(self.year)

    # ------------------------------------------------------------------ money
    def _fr_headers(self):
        headers = dm.FundsReservationHeader.objects.filter(intervention_id__in=list(self.pds)).filter(
            Q(start_date__isnull=True) | Q(start_date__lte=self.year_end),
            Q(end_date__isnull=True) | Q(end_date__gte=self.year_start),
        )
        return headers

    def _money(self, impact: dict[str, Any]) -> dict[str, Any]:
        """Funds of the PDs in scope, and cost per child on the same PDs on both sides.

        A PD with children reached (its main indicator, :mod:`neurodb.datamart.headline`) counts its
        money and its children in the section of that indicator; a PD without one splits its money by
        its indicators' sections. With a governorate chosen, each PD counts for its share of children
        reached there (PDs without children results cannot be split and are left out)."""
        headers = list(
            self._fr_headers().values(
                "intervention_id", "fr_number", "total_amt", "actual_amt", "outstanding_amt"
            )
        )
        per_pd: dict[int, dict[str, float]] = defaultdict(
            lambda: {"reserved": 0.0, "disbursed": 0.0, "outstanding": 0.0}
        )
        for h in headers:
            per_pd[h["intervention_id"]]["reserved"] += _money(h["total_amt"])
            per_pd[h["intervention_id"]]["disbursed"] += _money(h["actual_amt"])
            per_pd[h["intervention_id"]]["outstanding"] += _money(h["outstanding_amt"])
        children_pd_section: dict[int, Counter[str]] = defaultdict(Counter)  # in scope (governorate aware)
        children_pd_country: dict[int, float] = {}  # the whole PD, for the governorate share
        indicators_pd_section: dict[int, Counter[str]] = defaultdict(Counter)
        for row in self.rows:
            indicators_pd_section[row.pd.id][row.section or "Other"] += 1
        for pd_id, main in self.scope_mains.items():
            value = self._year_value(main.row)
            if value:
                children_pd_section[pd_id][main.row.section or "Other"] += value
            children_pd_country[pd_id] = main.row.reached_in_year or 0.0
        totals = {"reserved": 0.0, "disbursed": 0.0, "outstanding": 0.0, "for_children": 0.0, "children": 0.0}
        sections: dict[str, dict[str, float]] = defaultdict(
            lambda: {"reserved": 0.0, "disbursed": 0.0, "for_children": 0.0, "children": 0.0}
        )
        factors: dict[int, float] = {}  # the share of each PD's money counted in scope
        for pd_id, amounts in per_pd.items():
            by_section = children_pd_section.get(pd_id, Counter())
            children = sum(by_section.values())
            if self.gov_key:
                country = children_pd_country.get(pd_id, 0.0)
                if not country or not children:
                    continue  # cannot say how much of this PD's money went to the governorate
                factor = children / country
            else:
                factor = 1.0
            factors[pd_id] = factor
            weights = by_section if children else indicators_pd_section.get(pd_id) or Counter({"Other": 1})
            weight_total = sum(weights.values()) or 1
            for key in ("reserved", "disbursed", "outstanding"):
                totals[key] += amounts[key] * factor
            if children:
                totals["for_children"] += amounts["disbursed"] * factor
                totals["children"] += children
            for section, weight in weights.items():
                share = factor * weight / weight_total
                sections[section]["reserved"] += amounts["reserved"] * share
                sections[section]["disbursed"] += amounts["disbursed"] * share
                if children:
                    sections[section]["for_children"] += amounts["disbursed"] * share
                    sections[section]["children"] += by_section.get(section, 0.0)
        achieved = {s["section"]: s["percent"] for s in impact["by_section"]}
        by_section = []
        for section in sorted(sections):
            amounts = sections[section]
            children = half_up(amounts["children"])
            disbursed_percent = _pct(amounts["disbursed"], amounts["reserved"])
            achieved_percent = achieved.get(section)
            by_section.append(
                {
                    "section": section,
                    "disbursed": half_up(amounts["disbursed"], 2),
                    "reserved": half_up(amounts["reserved"], 2),
                    "children": children,
                    "cost_per_child": half_up(amounts["for_children"] / children, 1) if children else None,
                    "disbursed_percent": disbursed_percent,
                    "achieved_percent": achieved_percent,
                    "ahead": bool(
                        achieved_percent is not None
                        and disbursed_percent is not None
                        and achieved_percent >= disbursed_percent
                    ),
                }
            )
        where = (
            f" in {self.scope.governorate}, pro-rated by each PD's share of children reached there"
            if self.gov_key
            else ""
        )
        return {
            "reserved": half_up(totals["reserved"], 2),
            "disbursed": half_up(totals["disbursed"], 2),
            "outstanding": half_up(totals["outstanding"], 2),
            "disbursed_percent": _pct(totals["disbursed"], totals["reserved"]),
            "cost_per_child": half_up(totals["for_children"] / totals["children"], 1)
            if totals["children"]
            else None,
            "cost_per_child_previous": None,
            # The children the cost divides by: eTools partner reports only (the money is eTools'),
            # so not the headline, which may take ActivityInfo's larger figure.
            "cost_children": round(totals["children"]),
            "cost_disbursed": half_up(totals["for_children"], 2),
            "cost_caveat": COST_CAVEAT,
            "by_section": by_section,
            "by_donor": self._donors(
                {
                    h["fr_number"]: factors[h["intervention_id"]]
                    for h in headers
                    if h["fr_number"] and h["intervention_id"] in factors
                }
            ),
            "decisions": self._decisions(per_pd),
            "source": (
                f"eTools funds reservations of the programme documents running in {self.year} "
                f"(reserved and disbursed to date, all years, USD){where}; cost per child = what the PDs "
                f"with children results disbursed to date, over the children they reached in {self.year} "
                "(each PD's main indicator, summed over the PDs)"
            ),
        }

    def _donors(self, factors: dict[str, float]) -> list[list[Any]]:
        """Funding by donor of the FRs ``{fr_number: factor}``, each FR's lines counted for its PD's
        share in scope (the same pro-rating as the funds tiles; 1.0 for the whole country)."""
        if not factors:
            return []
        amounts: Counter[str] = Counter()
        for r in (
            dm.FundsReservation.objects.filter(fr_number__in=list(factors))
            .values("fr_number", "donor")
            .annotate(amount=Sum("overall_amount"))
        ):
            amounts[r["donor"] or "Unknown"] += _money(r["amount"]) * factors[r["fr_number"]]
        rows = sorted(amounts.items(), key=lambda kv: (-kv[1], kv[0]))
        top = [[donor, amount] for donor, amount in rows[:MAX_DONORS]]
        rest = sum(amount for _donor, amount in rows[MAX_DONORS:])
        if rest:
            top.append(["Other", rest])
        return top

    def _decisions(self, per_pd: dict[int, dict[str, float]]) -> list[dict[str, Any]]:
        out = []
        horizon = self.today + datetime.timedelta(days=DECISION_ENDING_DAYS)
        achieved_by_pd: dict[int, list[float]] = defaultdict(list)
        for row in self.rows:
            if row.achieved is not None:
                achieved_by_pd[row.pd.id].append(min(row.achieved, 100.0))
        for pd_id, pd in self.pds.items():
            if (pd.status or "").lower() not in ACTIVE_PD_STATUSES:
                continue
            partner = pd.partner.name if pd.partner_id and pd.partner else (pd.partner_name or "")
            url = _link("reports:programme_detail", pd.id)
            achieved = achieved_by_pd.get(pd_id)
            achieved_text = (
                f"{half_up(statistics.mean(achieved))}% achieved on average"
                if achieved
                else "no result reported"
            )
            if pd.end and self.today <= pd.end <= horizon:
                days = (pd.end - self.today).days
                out.append(
                    {
                        "pd_id": pd.id,
                        "pd": pd.number or f"PD {pd.id}",
                        "partner": partner,
                        "section": self.section_of_pd.get(pd_id, ""),
                        "reason": f"ends {pd.end:%d %b %Y}",
                        "detail": f"Ends in {days} days, {achieved_text}",
                        "url": url,
                        "severity": "warning",
                        "sort": days,
                    }
                )
                continue
            amounts = per_pd.get(pd_id)
            elapsed = percentage_elapsed(pd.start, pd.end, self.today)
            share = _pct(amounts["disbursed"], amounts["reserved"]) if amounts else None
            if share is not None and elapsed > DECISION_ELAPSED and share < DECISION_DISBURSED:
                out.append(
                    {
                        "pd_id": pd.id,
                        "pd": pd.number or f"PD {pd.id}",
                        "partner": partner,
                        "section": self.section_of_pd.get(pd_id, ""),
                        "reason": "under-disbursed",
                        "detail": f"{half_up(elapsed)}% of the period elapsed, {half_up(share)}% disbursed",
                        "url": url,
                        "severity": "info",
                        "sort": 1000 + share,
                    }
                )
        out.sort(key=lambda d: (d["severity"] != "warning", d["sort"]))
        for d in out:
            d.pop("sort")
        return out[:10]

    # ------------------------------------------------------------------ delivery
    def _partners_in_scope(self) -> set[int]:
        return {pd.partner_id for pd in self.pds.values() if pd.partner_id}

    def _delivery(self) -> dict[str, Any]:
        counts = Counter(r.tracking for r in self.rows)
        sections: dict[str, Counter[str]] = defaultdict(Counter)
        for row in self.rows:
            sections[row.section or "Other"][row.tracking] += 1
            if row.reports:
                sections[row.section or "Other"]["reported"] += 1
        by_section = [
            {
                "section": s,
                **{k: c.get(k, 0) for k in STATUSES},
                "total": sum(c.get(k, 0) for k in STATUSES),
                "reported": c.get("reported", 0),  # indicators with at least one progress report
            }
            for s, c in sorted(sections.items())
        ]
        partners = {pd.partner for pd in self.pds.values() if pd.partner_id and pd.partner}
        government = sum(1 for p in partners if "government" in (p.partner_type or "").lower())
        cso = sum(1 for p in partners if "civil society" in (p.partner_type or "").lower())
        return {
            "indicators": len(self.rows),
            "status_counts": {k: counts.get(k, 0) for k in STATUSES},
            "by_section": by_section,
            "programme_documents": len(self.pds),
            "partners": len(partners),
            "partners_government": government,
            "partners_cso": cso,
            "partners_other": len(partners) - government - cso,  # UN agencies, bilateral
            "by_partner": self._by_partner(),
            "assurance": self._assurance(partners),
            "findings_by_rating": self._findings_by_rating(),
            **self._attention(),
            "source": (
                "Partner monitoring rule (cumulative achievement against the elapsed PD period, ±10 points), "
                f"field monitoring findings, TPM visits, action points and partner risk ratings, {self.year}"
            ),
        }

    def _by_partner(self) -> list[dict[str, Any]]:
        """One row per partner for the management brief's scorecard: its PDs and indicators in
        scope, the tracking counts, the mean achievement (capped at 100 %) and the children reached (the
        sum over its PDs of each one's main indicator)."""
        groups: dict[int, dict[str, Any]] = {}
        main_ids = {id(m.row) for m in self.scope_mains.values()}
        for row in self.rows:
            pd = row.pd
            key = pd.partner_id or -pd.id
            g = groups.setdefault(
                key,
                {
                    "partner_id": pd.partner_id,
                    "partner": _partner(pd),
                    "pd_ids": set(),
                    "indicators": 0,
                    "reported": 0,
                    "achieved": [],
                    "children": 0.0,
                    **dict.fromkeys(STATUSES, 0),
                },
            )
            g["pd_ids"].add(pd.id)
            g["indicators"] += 1
            g[row.tracking] += 1
            if row.reports:
                g["reported"] += 1
            if row.achieved is not None:
                g["achieved"].append(min(row.achieved, 100.0))
            if id(row) in main_ids:
                g["children"] += self._year_value(row)
        out = []
        for g in groups.values():
            achieved = g.pop("achieved")
            g["pd_ids"] = sorted(g["pd_ids"])
            g["pds"] = len(g["pd_ids"])
            g["achieved_percent"] = half_up(statistics.mean(achieved), 1) if achieved else None
            g["children"] = half_up(g["children"])
            tracked = g["on_track"] + g["off_track"] + g["over_target"]
            g["on_track_percent"] = _pct(g["on_track"] + g["over_target"], tracked) if tracked else None
            out.append(g)
        out.sort(key=lambda g: (-g["indicators"], g["partner"]))
        return out

    def _scoped_findings(self):
        # start date in the year, else end date: as Monitoring insights dates a visit
        qs = dm.MonitoringFinding.objects.filter(fm.finding_year_q(self.year))
        if self.scope.sections:
            qs = qs.filter(partner_id__in=self._partners_in_scope())
        if self.gov_key:
            qs = qs.filter(location_id__in=self._location_ids_in_governorate())
        return qs

    def _location_ids_in_governorate(self) -> list[int]:
        """Every gazetteer location under the chosen governorate (the governorate itself included)."""
        if hasattr(self, "_gov_ids"):
            return self._gov_ids
        roots = [
            pk
            for pk, name in Location.objects.filter(type__admin_level=LEVEL_GOVERNORATE).values_list(
                "id", "name"
            )
            if governorate_key(name) == self.gov_key
        ]
        found, frontier = set(roots), set(roots)
        for _ in range(6):
            if not frontier:
                break
            frontier = (
                set(Location.objects.filter(parent_id__in=frontier).values_list("id", flat=True)) - found
            )
            found |= frontier
        self._gov_ids = list(found)
        return self._gov_ids

    def _section_q(self, field: str, intervention_field: str) -> Q:
        """A record of the selected sections: its own section when it has one, else its PD's."""
        own = Q()
        for name in self.scope.sections:
            own |= Q(**{f"{field}__icontains": name})
        return own | (Q(**{field: ""}) & Q(**{f"{intervention_field}__in": list(self.pds)}))

    def _scoped_action_points(self):
        qs = dm.ActionPoint.objects.all()
        if self.scope.sections:
            qs = qs.filter(self._section_q("section", "intervention_id"))
        if self.gov_key:
            qs = qs.filter(location_id__in=self._location_ids_in_governorate())
        return qs

    def _scoped_tpm_activities(self, *, planned: bool = True):
        """The TPM activities of the year's visits (planned ones only unless ``planned`` is false) that
        match the section and governorate (both conditions on the same activity)."""
        qs = dm.TPMActivity.objects.filter(visit__start_date__year=self.year)
        if planned:
            qs = qs.exclude(visit__status__in=TPM_NOT_PLANNED)
        if self.scope.sections:
            qs = qs.filter(self._section_q("section", "intervention_id"))
        if self.gov_key:
            qs = qs.filter(location_links__in=self._location_ids_in_governorate())
        return qs

    def _scoped_tpm_visits(self, *, planned: bool = True):
        """The TPM visits of the year; ``planned=False`` counts draft and cancelled ones too, as the
        field monitoring page does."""
        visits = dm.TPMVisit.objects.filter(start_date__year=self.year)
        if planned:
            visits = visits.exclude(status__in=TPM_NOT_PLANNED)
        if self.scope.sections or self.gov_key:
            visits = visits.filter(pk__in=self._scoped_tpm_activities(planned=planned).values("visit_id"))
        return visits

    def _assurance(self, partners: set[Any]) -> dict[str, Any]:
        points = self._scoped_action_points()
        open_points = points.filter(status__in=AP_OPEN)
        return {
            "field_monitoring_visits": self._scoped_findings()
            .exclude(monitoring_activity="")
            .values("monitoring_activity")
            .distinct()
            .count(),
            # Every visit of the year, as on the field monitoring page it links to; planned as a sub-count.
            "tpm_visits": self._scoped_tpm_visits(planned=False).count(),
            "tpm_visits_planned": self._scoped_tpm_visits().count(),
            # The card shows completed of planned, the pair the third-party monitoring block uses.
            "tpm_visits_completed": self._scoped_tpm_visits().filter(status__in=TPM_COMPLETED).count(),
            "open_action_points": open_points.count(),
            "overdue_high_priority": open_points.filter(high_priority=True, due_date__lt=self.today).count(),
            "high_risk_partners": sum(1 for p in partners if (p.rating or "").strip().lower() in HIGH_RISK),
            "partners_with_pd": len(partners),
        }

    def _findings_by_rating(self) -> list[list[Any]]:
        rows = (
            self._scoped_findings()
            .values("overall_finding_rating")
            .annotate(n=Count("pk"))
            .order_by("-n", "overall_finding_rating")
        )
        return [[r["overall_finding_rating"] or "Not rated", r["n"]] for r in rows]

    def _attention(self) -> dict[str, Any]:
        """``attention``: at most MAX_ATTENTION items, the overdue action points and one never-reported
        item always among them; ``attention_more``: how many were left out, and where to see them."""
        items: list[dict[str, Any]] = []
        off: dict[tuple[str, int], list[Indicator]] = defaultdict(list)
        unreported: dict[tuple[str, int], list[Indicator]] = defaultdict(list)
        children_ids = {id(r) for r in self.children_rows}
        for row in self.rows:
            key = (row.section or "Other", row.pd.id)
            if row.tracking == OFF_TRACK:
                off[key].append(row)
            elif row.tracking == NOT_REPORTED:
                started = row.pd.start and (self.today - row.pd.start).days > 120
                if started:
                    unreported[key].append(row)
        for (section, _pd_id), group in off.items():
            pd = group[0].pd
            # The raw section in the link: a blank one is left out, as partner monitoring has no "Other".
            # Children behind: the PD's main indicator's gap, else one children indicator's largest gap
            # (different indicators count the same children: their gaps are never added)
            found = headline.behind(group, self.mains.get(pd.id), lambda r: id(r) in children_ids)
            behind = found[0] if found else 0.0
            achieved = [min(r.achieved, 999.0) for r in group if r.achieved is not None]
            detail = (
                f"{half_up(sum(achieved) / len(achieved))}% of target on average, "
                f"{half_up(percentage_elapsed(pd.start, pd.end, self.today))}% of the PD period elapsed"
                if achieved
                else "Off track against the elapsed PD period"
            )
            items.append(
                {
                    "severity": "critical" if behind else "warning",
                    "title": f"{_who(pd)}: {_count(len(group), 'indicator')} off track",
                    "detail": detail,
                    "url": _link(
                        "reports:pd_monitoring",
                        section=group[0].section,
                        pd=pd.number or "",
                        status="off_track",
                        year=self.year,
                        scope="year",
                    ),
                    "section": section,
                    "children": half_up(behind) or None,
                    "kind": "off_track",
                }
            )
        for (section, _pd_id), group in unreported.items():
            pd = group[0].pd
            items.append(
                {
                    "severity": "warning",
                    "title": f"{_who(pd)}: {_count(len(group), 'indicator')} never reported",
                    "detail": f"No progress report read although the PD started on {pd.start:%d %b %Y}",
                    "url": _link(
                        "reports:pd_monitoring",
                        section=group[0].section,
                        pd=pd.number or "",
                        status=NOT_REPORTED,
                        year=self.year,
                        scope="year",
                    ),
                    "section": section,
                    "children": None,
                    "kind": NOT_REPORTED,
                }
            )
        overdue = (
            self._scoped_action_points()
            .filter(status__in=AP_OPEN, high_priority=True, due_date__lt=self.today)
            .values("section")
            .annotate(n=Count("pk"))
            .order_by("-n", "section")
        )
        by_section = [(r["section"] or "No section", r["n"]) for r in overdue]
        if by_section:  # one item (the link is the same for every section), always kept below
            total = sum(n for _section, n in by_section)
            items.append(
                {
                    "severity": "critical",
                    "title": f"{_count(total, 'high-priority action point')} past due",
                    "detail": "Open and past their due date"
                    + (": " + ", ".join(f"{s} {n}" for s, n in by_section) if len(by_section) > 1 else ""),
                    "url": _link("reports:action_points", overdue="1", priority="1"),
                    "section": by_section[0][0] if len(by_section) == 1 else "",
                    "children": None,
                    "kind": "action_points",
                }
            )
        order = {"critical": 0, "warning": 1, "info": 2}

        def rank(item: dict[str, Any]) -> tuple:
            return (order.get(item["severity"], 3), -(item["children"] or 0), item["title"])

        items.sort(key=rank)
        # Off-track PDs alone can fill every slot: keep the action points and one never-reported item.
        kept = [i for i in items if i["kind"] == "action_points"]
        unreported_items = [i for i in items if i["kind"] == NOT_REPORTED]
        if unreported_items and len(kept) < MAX_ATTENTION:
            kept.append(unreported_items[0])
        for item in items:
            if len(kept) >= MAX_ATTENTION:
                break
            if not any(item is k for k in kept):
                kept.append(item)
        kept.sort(key=rank)
        left_out = [i for i in items if not any(i is k for k in kept)]
        kinds = {i["kind"] for i in left_out}
        more_status = next(iter(kinds)) if len(kinds) == 1 and kinds <= {"off_track", NOT_REPORTED} else ""
        return {
            "attention": kept,
            "attention_more": {
                "count": len(left_out),
                "url": _link(
                    "reports:pd_monitoring",
                    section=list(self.scope.sections),
                    status=more_status,
                    year=self.year,
                    scope="year",
                ),
            },
        }

    # ------------------------------------------------------------------ progress
    def _progress(self) -> dict[str, Any]:
        return {
            "tpm": self._tpm(),
            "action_points": self._action_points(),
            "source": (
                "eTools TPM visits (planned = not draft or cancelled, completed = report received or "
                f"approved, overdue = visit ended more than {TPM_REPORT_GRACE_DAYS} days ago without a "
                f"report) and action points (due, closed and past due by month), {self.year}"
            ),
        }

    def _tpm(self) -> dict[str, Any]:
        planned, completed, overdue = [0] * 12, [0] * 12, [0] * 12
        late_before = self.today - datetime.timedelta(days=TPM_REPORT_GRACE_DAYS)
        visits = self._scoped_tpm_visits()
        for start, end, status in visits.values_list("start_date", "end_date", "status"):
            month = start.month - 1
            planned[month] += 1
            if status in TPM_COMPLETED:
                completed[month] += 1
            elif status in TPM_OPEN and end and end < late_before:
                overdue[month] += 1
        done = self._scoped_tpm_activities().filter(visit__status__in=TPM_COMPLETED)
        sites = Location.objects.filter(tpm_activities__in=done).distinct().count()
        planned_total, completed_total = sum(planned), sum(completed)
        return {
            "labels": list(MONTH_LABELS),
            "planned": planned,
            "completed": completed,
            "overdue": overdue,
            "planned_total": planned_total,
            "completed_total": completed_total,
            "overdue_total": sum(overdue),
            "sites_visited": sites,
            "completion_percent": _pct(completed_total, planned_total),
        }

    def _action_points(self) -> dict[str, Any]:
        due, closed, past_due = [0] * 12, [0] * 12, [0] * 12
        points = self._scoped_action_points()
        rows = list(
            points.values("status", "due_date", "date_of_completion", "high_priority", "related_module")
        )
        days_to_close = []
        month_ends = [
            (datetime.date(self.year, m + 2, 1) if m < 11 else datetime.date(self.year + 1, 1, 1))
            - datetime.timedelta(days=1)
            for m in range(12)
        ]
        age: dict[str, Counter[str]] = defaultdict(Counter)
        open_total = high_open = closed_of_due = 0
        for r in rows:
            due_date = r["due_date"]
            done = r["date_of_completion"].date() if r["date_of_completion"] else None
            is_open = r["status"] in AP_OPEN
            if due_date and due_date.year == self.year:
                due[due_date.month - 1] += 1
                if not is_open and done and done <= self.today:
                    closed_of_due += 1
            if done and done.year == self.year and done <= self.today and not is_open:
                closed[done.month - 1] += 1
                if due_date:
                    days_to_close.append((done - due_date).days)
            if due_date:
                for m, month_end in enumerate(month_ends):
                    if month_end.replace(day=1) > self.today:
                        break  # months still to come
                    cut = min(month_end, self.today)  # the current month is counted as of today
                    open_then = is_open or (done is not None and done > cut)
                    if open_then and due_date < cut:
                        past_due[m] += 1
            if is_open:
                open_total += 1
                high_open += 1 if r["high_priority"] else 0
                module = MODULES.get((r["related_module"] or "").lower(), "Other")
                late = (self.today - due_date).days if due_date else 0
                bucket = "under_30" if late < 30 else ("d30_90" if late <= 90 else "over_90")
                age[module][bucket] += 1
                if r["high_priority"]:
                    age[module]["high_priority"] += 1
        order = list(MODULES.values()) + ["Other"]
        return {
            "labels": list(MONTH_LABELS),
            "due": due,
            "closed": closed,
            "past_due": past_due,
            "open_total": open_total,
            "high_priority_open": high_open,
            "closed_total": sum(closed),
            "due_total": sum(due),
            "closure_percent": _pct(closed_of_due, sum(due)),  # of the points due this year
            "median_days_to_close": half_up(statistics.median(days_to_close)) if days_to_close else None,
            "age": [
                {
                    "module": module,
                    "under_30": age[module]["under_30"],
                    "d30_90": age[module]["d30_90"],
                    "over_90": age[module]["over_90"],
                    "high_priority": age[module]["high_priority"],
                }
                for module in order
                if module in age
            ],
        }


# --------------------------------------------------------------------------------- freshness
def freshness() -> list[dict[str, Any]]:
    """When each source last changed: the same states as the data health page, without its per-dataset
    counts. Plain values (``finished_at``, never the run itself), kept two minutes in the shared cache;
    a finished sync changes the key, so a new run shows at once. Like :func:`options`, never waited for:
    a frame computes it itself when another request is at it."""
    if settings.DEBUG:
        return _freshness()
    key = f"overview-freshness:v{CACHE_VERSION}:{compute.data_stamp()}"  # the last finished sync run
    return compute.once(key, _freshness, CACHE_SECONDS, wait=0)


def _freshness() -> list[dict[str, Any]]:
    from django.utils import timezone

    from neurodb.core.models import SyncRun

    staleness = datetime.timedelta(hours=settings.SYNC_STALENESS_HOURS)
    now = timezone.now()
    labels = dict(SyncRun.Job.choices)
    out = []
    for job in FRESHNESS_JOBS:
        last = SyncRun.last_success(job)
        finished = last.finished_at if last is not None else None
        if finished is None:
            state, state_label = "unknown", "Not yet synced"
        elif now - finished > staleness:
            state, state_label = "stale", "Stale"
        else:
            state, state_label = "fresh", "Fresh"
        out.append(
            {
                "job": job,
                "label": str(labels.get(job, job)),
                "finished_at": finished,
                "state": state,
                "state_label": state_label,
            }
        )
    return out


def freshness_note(jobs: list[dict[str, Any]]) -> str:
    """One plain line under the freshness strip: whether the figures can be used as they are."""
    sources = [j for j in jobs if j["job"] != "daily_review"]
    never = [j["label"] for j in sources if j["state"] == "unknown"]
    stale = [j["label"] for j in sources if j["state"] == "stale"]
    if not never and not stale:
        return "Every source synced on time: the figures on this page are current."
    parts = []
    if never:
        its = "its" if len(never) == 1 else "their"
        parts.append(
            f"{' and '.join(never)}: not yet synced on this server, so {its} figures may be incomplete."
        )
    if stale:
        its = "its" if len(stale) == 1 else "their"
        parts.append(f"{' and '.join(stale)}: out of date, so {its} figures may miss recent changes.")
    if len(never) + len(stale) < len(sources):
        parts.append("The other figures are current.")
    return " ".join(parts)


def _who(pd: Any) -> str:
    return f"{pd.number or pd.id} · {_partner(pd)}"


def _count(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _partner(pd: Any) -> str:
    return (pd.partner.name if pd.partner_id and pd.partner else pd.partner_name) or "unknown partner"


__all__ = [
    "Scope",
    "build",
    "children_by_pd",
    "children_masters",
    "children_population",
    "governorate_key",
    "options",
    "section_matches",
]


# ------------------------------------------------------------------ per programme document
def children_by_pd(scope: Scope) -> dict[int, dict[str, Any]]:
    """Each programme document of the year with its children and results, for pages that attribute
    them (the donor page). One indicator per PD, its main indicator (:mod:`neurodb.datamart.headline`:
    its unique beneficiaries indicator, else its largest children indicator, with the overview's
    set-aside guard): children reached in the year, Reached to date against its target, where (by
    governorate, indicators summed across locations only), girls and boys and the age as its title names
    them; with the PD's section (its main indicator's when it reached children in the year, else the one
    most of its indicators are in, as the overview's money) and the status of all its indicators."""
    b = _Builder(scope)
    rows = b._rows()
    b.rows = rows
    b._choose_mains()
    out: dict[int, dict[str, Any]] = {}
    for row in rows:
        e = out.setdefault(
            row.pd.id,
            {
                "sections": Counter(),
                "children": 0.0,
                "achieved": 0.0,
                "target": 0.0,
                "by_governorate": {},
                "names": {},
                "sex": Counter(),
                "age": Counter(),
                "statuses": Counter(),
                "main": None,
                "elapsed": percentage_elapsed(row.pd.start, row.pd.end, scope.today),
            },
        )
        e["sections"][row.section or "Other"] += 1
        e["statuses"][row.tracking] += 1
    for pd_id, main in b.scope_mains.items():
        e = out.get(pd_id)
        if e is None:
            continue
        row = main.row
        value = b._year_value(row)
        e["children"] = value
        e["achieved"] = (row.reached or 0.0) if row.target else 0.0
        e["target"] = row.target or 0.0
        e["main"] = {
            "title": row.title,
            "cumulative": half_up(row.reached or 0),
            "reached": half_up(row.reached or 0),
            "target": half_up(row.target) if row.target else None,
            "tracking": row.tracking,
            "rule": main.rule,
            "rule_label": main.label,
        }
        if value:  # as the overview's money: a PD without children reached splits by its indicators
            e["main_section"] = row.section or "Other"
        tags = _title_tags(row)
        e["sex"][tags["sex"] or "Not named"] += value
        e["age"][tags["age_group"] or "Not named"] += value
        if b._additive(row):
            by_governorate: dict[str, float] = defaultdict(float)
            for place in row.by_location.values():
                gov = b.governorate_of.get(place.get("id")) or ""
                key = governorate_key(gov)
                if place.get("achieved") and key:
                    by_governorate[key] += place["achieved"]
                    e["names"].setdefault(key, gov)
            e["by_governorate"] = dict(by_governorate)
    for e in out.values():
        sections = e.pop("sections")
        # a PD with children reached counts in the section of its main indicator, as on the overview
        # (its money and its children, so a donor's cost per child is set against the same section's)
        e["section"] = e.pop("main_section", None) or sections.most_common(1)[0][0]
    return out


def _aside_entry(row: Indicator, reason: str) -> dict[str, Any]:
    return {
        "pd": row.pd.number or "",
        "partner": _partner(row.pd),
        "section": row.section or "",
        "title": row.title,
        "value": half_up(headline.largest_value(row)),
        "target": half_up(row.target) if row.target else None,
        "reason": reason,
        "url": row.url,
    }


def _title_tags(row: Indicator) -> dict[str, Any]:
    """What an indicator's title names: one sex (girls without boys, or the reverse), an age band, a
    nationality, disability."""
    title = (row.title or "").lower()
    sex = next(
        (label for label, yes, no in SEX_RULES if re.search(yes, title) and not re.search(no, title)), ""
    )
    return {
        "sex": sex,
        "age_group": row.tags.get("age_group") or "",
        "nationality": NATIONALITY_OF_TAG.get(row.tags.get("nationality") or "", ""),
        "disability": bool(row.tags.get("disability")),
    }
