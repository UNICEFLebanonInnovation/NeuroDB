"""The country overview's data: impact on children, value for money, delivery and assurance,
assurance progress, and the freshness of every source.

One call to :func:`build` reads the programme document indicators once (through the partner
monitoring service, which applies the tracking rule and attaches what partners reported), the
ActivityInfo HPM masters that count children, the funds reservations, the TPM visits, the action
points and the monitoring findings of the year, and shapes them for the overview page. The two
reporting sources are kept apart: an eTools figure and an ActivityInfo figure are only added into
one number where the page says so.

Filters: the year, eTools section names (empty = every section) and one governorate (a gazetteer
governorate name; empty = the country). The result is cached for a few minutes per filter.
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
from django.core.cache import cache as django_cache
from django.db.models import Count, Max, Q, Sum
from django.urls import reverse

from neurodb.datamart import models as dm
from neurodb.datamart import monitoring
from neurodb.datamart.children import counts_children
from neurodb.datamart.children import overrides as children_overrides
from neurodb.datamart.monitoring import ACTIVE_PD_STATUSES, NOT_REPORTED, Filters, Indicator
from neurodb.facts import queries as fact_queries
from neurodb.facts.services.dashboard import fact_filter
from neurodb.facts.services.dashboard import overview as activityinfo_overview
from neurodb.geo.models import Location
from neurodb.indicators.models import Database, NeuroReportMasterIndicator
from neurodb.indicators.services.tracking import OFF_TRACK, percentage_elapsed

logger = logging.getLogger(__name__)

CACHE_SECONDS = 600
CACHE_VERSION = 1
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
MAX_DONORS = 8
HIGH_RISK = ("high", "significant")
COST_CAVEAT = (
    "Disbursed to date (supplies and operating costs included) over the children reached this year: "
    "compare sections, not absolute values."
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
    return round(part * 100 / whole, 1)


def _money(value: Any) -> float:
    return float(value or 0)


def _levels(values: list[float]) -> list[int]:
    """1..6 for the governorate tiles: the share of the largest value, rounded up (0 stays level 1)."""
    top = max(values, default=0)
    return [max(1, math.ceil(v * 6 / top)) if top else 1 for v in values]


# --------------------------------------------------------------------------------- options
def options(year: int) -> dict[str, list[str]]:
    """The filter bar's choices: eTools section names of the PD indicators and gazetteer governorates."""
    sections = sorted(
        {s for s in dm.PDIndicator.objects.exclude(section_name="").values_list("section_name", flat=True)}
    )
    governorates = sorted(
        set(
            Location.objects.filter(type__admin_level=LEVEL_GOVERNORATE, is_active=True).values_list(
                "name", flat=True
            )
        )
    )
    return {"sections": sections, "governorates": governorates}


# --------------------------------------------------------------------------------- build
def _cache_key(scope: Scope) -> str:
    """Everything the result depends on, the children flags included (a new flag shows at once)."""
    from neurodb.datamart.models import IndicatorFlag

    flags = IndicatorFlag.objects.aggregate(n=Count("pk"), last=Max("updated_at"))
    sections = "|".join(sorted(scope.sections))
    ryear = getattr(scope.reporting_year, "pk", "")
    return (
        f"overview:v{CACHE_VERSION}:{scope.year}:{ryear}:{sections}:{scope.governorate}:{scope.today}:"
        f"{flags['n']}:{flags['last']}"
    )


def build(scope: Scope, *, cache: bool = True) -> dict[str, Any]:
    """Every block of the overview for ``scope`` (see the module docstring)."""
    use_cache = cache and not settings.DEBUG
    key = _cache_key(scope)
    if use_cache:
        found = django_cache.get(key)
        if found is not None:
            return found
    data = _Builder(scope).build()
    if use_cache:
        django_cache.set(key, data, CACHE_SECONDS)
    return data


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
        governorate when one is chosen."""
        rows = monitoring.indicators(
            Filters(
                year=self.year,
                sections=list(self.scope.sections),
                report_type=monitoring.DEFAULT_REPORT_TYPE,
                scope="year",
            ),
            self.today,
        )
        ids: set[int] = set()
        for row in rows:
            ids.update(p["id"] for p in row.planned.values() if p.get("id"))
            ids.update(p["id"] for p in row.by_location.values() if p.get("id"))
        self.gazetteer = monitoring._gazetteer(ids)
        self.governorate_of: dict[int, str] = {}
        for location_id in ids:
            place = monitoring._place(location_id, self.gazetteer)
            self.governorate_of[location_id] = place["governorate"]
        if self.gov_key:
            rows = [r for r in rows if self.gov_key in self._governorate_keys(r)]
        return rows

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
        """Whether the indicator's locations add up (PRP "sum"); a max or an average never does."""
        return row.method in ("", "sum")

    def _places_in(self, row: Indicator, key: str):
        for place in row.by_location.values():
            if governorate_key(self.governorate_of.get(place.get("id"))) == key:
                yield place

    def _year_value(self, row: Indicator) -> float:
        """What the indicator's reports of the year add up to; with a governorate chosen, what its
        locations there add up to (additive indicators only: a max across locations cannot be split)."""
        if not self.gov_key:
            return sum(v for v in row.months.values() if v)
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

    # ------------------------------------------------------------------ build
    def build(self) -> dict[str, Any]:
        rows = self._rows()
        self.rows = rows
        self.children_rows = [r for r in rows if self._is_children(r)]
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
            "freshness": self._freshness(),
            "scope": {
                "year": self.year,
                "sections": list(self.scope.sections),
                "governorate": self.scope.governorate,
                "today": self.today.isoformat(),
            },
            "activityinfo": activityinfo_overview(self.scope.reporting_year),
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
        links = (
            NeuroReportMasterIndicator.objects.filter(report__is_hpm=True, report__ryear=ryear)
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
            if not section_matches(
                section.name if section else "", section.code if section else "", self.scope.sections
            ):
                continue
            label = link.label or master.name
            if not counts_children("activityinfo", str(master.id), label, None, self.flags, unit=""):
                continue
            by_database.setdefault(database.id, (database, set()))[1].add(master.id)
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
        etools_months = [0.0] * 12
        etools_total = 0.0
        by_gov: dict[str, float] = defaultdict(float)
        gov_names: dict[str, str] = {}
        for row in self.children_rows:
            etools_total += self._year_value(row)
            for month, value in self._months(row).items():
                if value and 1 <= month <= 12:
                    etools_months[month - 1] += value
            if not self._additive(row):
                continue  # a max or an average across locations is not split by governorate
            for place in row.by_location.values():
                gov = self.governorate_of.get(place.get("id")) or ""
                key = governorate_key(gov)
                if place.get("achieved") and key and (not self.gov_key or key == self.gov_key):
                    by_gov[key] += place["achieved"]
                    gov_names.setdefault(key, gov)
        population, population_year = self._children_population()
        keys = (
            set(by_gov)
            | set(activityinfo["by_governorate"])
            | (set(population) if not self.gov_key else set())
        )
        if self.gov_key:
            keys = {k for k in keys | set(population) if k == self.gov_key}
        governorates = []
        for key in keys:
            etools = round(by_gov.get(key, 0))
            ai = round(activityinfo["by_governorate"].get(key, 0))
            reached = max(etools, ai)
            pop = population.get(key, (None, None))[1]
            if not reached and not pop:
                continue
            governorates.append(
                {
                    "name": gov_names.get(key)
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
        etools_value = round(etools_total)
        ai_value = round(activityinfo["total"])
        return {
            # The same children are often reported in both sources: the headline is the larger one.
            "children_reached": max(etools_value, ai_value),
            "children_etools": etools_value,
            "children_activityinfo": ai_value,
            "delta_previous_year": None,
            "by_governorate": governorates,
            "by_section": self._achievement_by_section(),
            "monthly": {
                "labels": list(MONTH_LABELS),
                "etools": [round(v) for v in etools_months],
                "activityinfo": [round(v) for v in activityinfo["months"]],
            },
            "population_year": population_year,
            "source": (
                "eTools PRP progress reports of the PD indicators that count children, and the additive "
                f"ActivityInfo HPM indicators that count children, {self.year}; the two sources are shown "
                "apart, and a total is the larger of the two (at least that many children)"
            ),
        }

    def _achievement_by_section(self) -> list[dict[str, Any]]:
        groups: dict[str, dict[str, Any]] = {}
        for row in self.children_rows:
            if not row.target:
                continue
            g = groups.setdefault(
                row.section or "Other",
                {"achieved": 0.0, "target": 0.0, "elapsed_weight": 0.0, "indicators": 0},
            )
            g["achieved"] += row.cumulative or 0
            g["target"] += row.target
            g["elapsed_weight"] += percentage_elapsed(row.pd.start, row.pd.end, self.today) * row.target
            g["indicators"] += 1
        out = []
        for section, g in sorted(groups.items()):
            out.append(
                {
                    "section": section,
                    "achieved": round(g["achieved"]),
                    "target": round(g["target"]),
                    "percent": _pct(g["achieved"], g["target"]),
                    "elapsed": round(g["elapsed_weight"] / g["target"], 1) if g["target"] else None,
                    "indicators": g["indicators"],
                }
            )
        return out

    def _children_population(self) -> tuple[dict[str, tuple[str, int]], int | None]:
        """``{governorate key: (name, children)}`` of the latest year of child figures up to the scope's."""
        from neurodb.core.models import PopulationFigure

        base = PopulationFigure.objects.filter(
            category="children",
            level="governorate",
            age_group="",
            sex="",
            vulnerability_level="",
            year__lte=self.year,
        )
        year = base.order_by("-year").values_list("year", flat=True).first()
        if year is None:
            return {}, None
        rows = base.filter(year=year).values("area_name", "nationality", "value")
        with_all = {governorate_key(r["area_name"]) for r in rows if r["nationality"] == "ALL"}
        out: dict[str, list[Any]] = {}
        for r in rows:
            key = governorate_key(r["area_name"])
            if not key or (key in with_all and r["nationality"] != "ALL"):
                continue
            entry = out.setdefault(key, [r["area_name"], 0])
            entry[1] += r["value"] or 0
        return {k: (v[0], v[1]) for k, v in out.items()}, year

    # ------------------------------------------------------------------ money
    def _fr_headers(self):
        headers = dm.FundsReservationHeader.objects.filter(intervention_id__in=list(self.pds)).filter(
            Q(start_date__isnull=True) | Q(start_date__lte=self.year_end),
            Q(end_date__isnull=True) | Q(end_date__gte=self.year_start),
        )
        return headers

    def _money(self, impact: dict[str, Any]) -> dict[str, Any]:
        """Funds of the PDs in scope, and cost per child on the same PDs on both sides.

        A PD's money is split across sections in proportion to the children its indicators of each
        section reached (by indicator count when it has no children indicator). With a governorate
        chosen, each PD counts for its share of children reached there (PDs without children results
        cannot be split and are left out)."""
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
        children_pd_country: dict[int, float] = defaultdict(float)  # the whole PD, for the governorate share
        indicators_pd_section: dict[int, Counter[str]] = defaultdict(Counter)
        for row in self.rows:
            indicators_pd_section[row.pd.id][row.section or "Other"] += 1
        for row in self.children_rows:
            children_pd_section[row.pd.id][row.section or "Other"] += self._year_value(row)
            children_pd_country[row.pd.id] += sum(v for v in row.months.values() if v)
        totals = {"reserved": 0.0, "disbursed": 0.0, "outstanding": 0.0, "for_children": 0.0, "children": 0.0}
        sections: dict[str, dict[str, float]] = defaultdict(
            lambda: {"reserved": 0.0, "disbursed": 0.0, "for_children": 0.0, "children": 0.0}
        )
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
            children = round(amounts["children"])
            disbursed_percent = _pct(amounts["disbursed"], amounts["reserved"])
            achieved_percent = achieved.get(section)
            by_section.append(
                {
                    "section": section,
                    "disbursed": round(amounts["disbursed"], 2),
                    "reserved": round(amounts["reserved"], 2),
                    "children": children,
                    "cost_per_child": round(amounts["for_children"] / children, 1) if children else None,
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
            "reserved": round(totals["reserved"], 2),
            "disbursed": round(totals["disbursed"], 2),
            "outstanding": round(totals["outstanding"], 2),
            "disbursed_percent": _pct(totals["disbursed"], totals["reserved"]),
            "cost_per_child": round(totals["for_children"] / totals["children"], 1)
            if totals["children"]
            else None,
            "cost_per_child_previous": None,
            "cost_caveat": COST_CAVEAT,
            "by_section": by_section,
            "by_donor": self._donors([h["fr_number"] for h in headers if h["fr_number"]]),
            "decisions": self._decisions(per_pd),
            "source": (
                f"eTools funds reservations of the programme documents running in {self.year} "
                f"(reserved and disbursed to date, all years, USD){where}; cost per child = what the PDs "
                f"with children results disbursed to date, over the children they reached in {self.year}"
            ),
        }

    def _donors(self, fr_numbers: list[str]) -> list[list[Any]]:
        if not fr_numbers:
            return []
        rows = list(
            dm.FundsReservation.objects.filter(fr_number__in=set(fr_numbers))
            .values("donor")
            .annotate(amount=Sum("overall_amount"))
            .order_by("-amount")
        )
        top = [[r["donor"] or "Unknown", _money(r["amount"])] for r in rows[:MAX_DONORS]]
        rest = sum(_money(r["amount"]) for r in rows[MAX_DONORS:])
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
                f"{round(statistics.mean(achieved))}% achieved on average"
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
                        "detail": f"{round(elapsed)}% of the period elapsed, {round(share)}% disbursed",
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
        by_section = [
            {"section": s, **{k: c.get(k, 0) for k in STATUSES}, "total": sum(c.values())}
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
            "assurance": self._assurance(partners),
            "findings_by_rating": self._findings_by_rating(),
            "attention": self._attention(),
            "source": (
                "Partner monitoring rule (cumulative achievement against the elapsed PD period, ±10 points), "
                f"field monitoring findings, TPM visits, action points and partner risk ratings, {self.year}"
            ),
        }

    def _scoped_findings(self):
        qs = dm.MonitoringFinding.objects.filter(end_date__year=self.year)
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

    def _scoped_tpm_activities(self):
        """The TPM activities of the year's planned visits that match the section and governorate (both
        conditions on the same activity)."""
        qs = dm.TPMActivity.objects.filter(visit__start_date__year=self.year).exclude(
            visit__status__in=TPM_NOT_PLANNED
        )
        if self.scope.sections:
            qs = qs.filter(self._section_q("section", "intervention_id"))
        if self.gov_key:
            qs = qs.filter(location_links__in=self._location_ids_in_governorate())
        return qs

    def _scoped_tpm_visits(self):
        visits = dm.TPMVisit.objects.filter(start_date__year=self.year).exclude(status__in=TPM_NOT_PLANNED)
        if self.scope.sections or self.gov_key:
            visits = visits.filter(pk__in=self._scoped_tpm_activities().values("visit_id"))
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
            "tpm_visits": self._scoped_tpm_visits().count(),
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

    def _attention(self) -> list[dict[str, Any]]:
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
            behind = sum(
                max((r.target or 0) - (r.cumulative or 0), 0) for r in group if id(r) in children_ids
            )
            achieved = [min(r.achieved, 999.0) for r in group if r.achieved is not None]
            detail = (
                f"{round(sum(achieved) / len(achieved))}% of target on average, "
                f"{round(percentage_elapsed(pd.start, pd.end, self.today))}% of the PD period elapsed"
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
                        section=section,
                        pd=pd.number or "",
                        status="off_track",
                        year=self.year,
                        scope="year",
                    ),
                    "section": section,
                    "children": round(behind) or None,
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
                        section=section,
                        pd=pd.number or "",
                        status=NOT_REPORTED,
                        year=self.year,
                        scope="year",
                    ),
                    "section": section,
                    "children": None,
                }
            )
        overdue = (
            self._scoped_action_points()
            .filter(status__in=AP_OPEN, high_priority=True, due_date__lt=self.today)
            .values("section")
            .annotate(n=Count("pk"))
        )
        for r in overdue:
            section = r["section"] or "No section"
            items.append(
                {
                    "severity": "critical",
                    "title": f"{_count(r['n'], 'high-priority action point')} past due",
                    "detail": "Open and past their due date",
                    "url": _link("reports:action_points", overdue="1", priority="1"),
                    "section": section,
                    "children": None,
                }
            )
        order = {"critical": 0, "warning": 1, "info": 2}
        items.sort(key=lambda i: (order.get(i["severity"], 3), -(i["children"] or 0), i["title"]))
        return items[:MAX_ATTENTION]

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
            if done and done.year == self.year and not is_open:
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
            "median_days_to_close": round(statistics.median(days_to_close)) if days_to_close else None,
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

    # ------------------------------------------------------------------ freshness
    def _freshness(self) -> list[dict[str, Any]]:
        """When each source last changed: the same states as the data health page, without its
        per-dataset counts."""
        from django.utils import timezone

        from neurodb.core.models import SyncRun

        staleness = datetime.timedelta(hours=settings.SYNC_STALENESS_HOURS)
        now = timezone.now()
        labels = dict(SyncRun.Job.choices)
        out = []
        for job in ("ai_data", "etools_datamart", "locations", "daily_review"):
            last = SyncRun.last_success(job)
            if last is None or last.finished_at is None:
                state, state_label = "unknown", "Never succeeded"
            elif now - last.finished_at > staleness:
                state, state_label = "stale", "Stale"
            else:
                state, state_label = "fresh", "Fresh"
            out.append(
                {
                    "job": job,
                    "label": labels.get(job, job),
                    "last_success": last,
                    "state": state,
                    "state_label": state_label,
                }
            )
        return out


def _who(pd: Any) -> str:
    return f"{pd.number or pd.id} · {_partner(pd)}"


def _count(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _partner(pd: Any) -> str:
    return (pd.partner.name if pd.partner_id and pd.partner else pd.partner_name) or "unknown partner"


__all__ = ["Scope", "build", "governorate_key", "options", "section_matches"]
