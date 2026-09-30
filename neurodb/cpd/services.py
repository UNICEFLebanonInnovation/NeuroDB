"""Where the Country Programme stands: each indicator's value, its status, and the interventions and
partner reporting behind each output.

An indicator's value for a year is the value typed in the admin for that year, else the sum of its
confirmed linked sources for that year (eTools PD indicators: the partners' cumulative progress;
ActivityInfo: the master indicator's result; Compiler: young people reached, children enrolled).
The cycle's value is the latest year's value (a level) or the sum over the cycle's years (people
reached), as the indicator says.

Status: the share of the way from baseline to target that the value has covered, compared with the
share of the cycle elapsed (or with 100 % of the year's milestone when one is set): within 10 points
is on track, below is off track, above is ahead of schedule — the same rule as the other NeuroDB
pages. No target: "no target"; no value yet: "no data".
"""

from __future__ import annotations

import datetime
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from neurodb.indicators.services.tracking import TOLERANCE

from .models import CountryProgramme, Indicator, Link, Output

ON_TRACK, OFF_TRACK, AHEAD, NO_TARGET, NO_DATA = (
    "on_track",
    "off_track",
    "over_target",
    "no_target",
    "not_reported",
)
LABELS = {
    ON_TRACK: "On track",
    OFF_TRACK: "Off track",
    AHEAD: "Ahead of schedule",
    NO_TARGET: "No target",
    NO_DATA: "No data yet",
}
STATUS_ORDER = (OFF_TRACK, ON_TRACK, AHEAD, NO_DATA, NO_TARGET)
CLOSED_PD_STATUSES = ("draft", "cancelled")


def elapsed_share(programme: CountryProgramme, today: datetime.date) -> float:
    """Share of the cycle elapsed, 0-100 (1 January of the first year to 31 December of the last)."""
    start = datetime.date(programme.start_year, 1, 1)
    end = datetime.date(programme.end_year + 1, 1, 1)
    if today <= start:
        return 0.0
    if today >= end:
        return 100.0
    return round((today - start).days / (end - start).days * 100, 1)


# ------------------------------------------------------------------------------ linked sources
def _etools_values(links: list[Link]) -> dict[int, float | None]:
    from neurodb.datamart import monitoring

    out: dict[int, float | None] = {}
    by_year: dict[int, list[Link]] = defaultdict(list)
    for link in links:
        by_year[link.year].append(link)
    for year, year_links in by_year.items():
        pd_ids = sorted({link.pd_id for link in year_links if link.pd_id})
        rows = (
            monitoring.indicators(monitoring.Filters(pd_ids=pd_ids, scope="all", year=year)) if pd_ids else []
        )
        cumulative = {(row.pd.id, row.key): row.cumulative for row in rows}
        for link in year_links:
            out[link.pk] = cumulative.get((link.pd_id, link.etools_key))
    return out


def _activityinfo_values(links: list[Link]) -> dict[int, float | None]:
    from neurodb.facts import queries
    from neurodb.facts.services.dashboard import fact_filter
    from neurodb.indicators.models import MasterIndicator

    out: dict[int, float | None] = {}
    masters = {m.pk: m for m in MasterIndicator.objects.filter(pk__in=[lk.master_id for lk in links])}
    by_database: dict[int, list[Link]] = defaultdict(list)
    for link in links:
        master = masters.get(link.master_id)
        if master is None:
            out[link.pk] = None
            continue
        by_database[master.database_id].append(link)
    for db_links in by_database.values():
        database = masters[db_links[0].master_id].database
        values = {row["id"]: row["value"] for row in queries.master_indicator_values(fact_filter(database))}
        for link in db_links:
            value = values.get(link.master_id)
            out[link.pk] = float(value) if value is not None else None
    return out


def _youth_values(links: list[Link]) -> dict[int, float | None]:
    from neurodb.youth.figures import Figures
    from neurodb.youth.models import YouthFigures

    out: dict[int, float | None] = {}
    records = {
        r.year: Figures(r.payload)
        for r in YouthFigures.objects.filter(year__in={lk.source_period for lk in links})
    }
    for link in links:
        figures = records.get(link.source_period)
        if figures is None:
            out[link.pk] = None
            continue
        table = figures.table({}, link.youth_level) or {}
        value = table.get((link.youth_id,))
        out[link.pk] = float(value) if value is not None else None
    return out


def _education_values(links: list[Link]) -> dict[int, float | None]:
    from neurodb.education.models import EducationFigures

    out: dict[int, float | None] = {}
    for link in links:
        record = EducationFigures.objects.filter(
            programme=link.education_programme, year=link.source_period
        ).first()
        out[link.pk] = float(education_total(record.payload)) if record else None
    return out


def education_total(payload: dict[str, Any]) -> int:
    """Children enrolled (registrations) in a Makani or Dirasa payload."""
    cube = (payload.get("cubes") or {}).get("enrolment")
    if cube and cube.get("rows"):
        at = len(cube["dims"]) + cube["measures"].index("registrations")
        return sum(row[at] or 0 for row in cube["rows"])
    for grouping in (payload.get("blocks") or {}).get("registrations", {}).get("figures", []):
        if grouping["by"] == []:
            return grouping["rows"][0][-1] if grouping["rows"] else 0
    return 0


READERS = {
    Link.Kind.ETOOLS: _etools_values,
    Link.Kind.ACTIVITYINFO: _activityinfo_values,
    Link.Kind.YOUTH: _youth_values,
    Link.Kind.EDUCATION: _education_values,
}


def link_values(links: list[Link]) -> dict[int, float | None]:
    """The figure of each link, read in one batch per kind."""
    out: dict[int, float | None] = {}
    by_kind: dict[str, list[Link]] = defaultdict(list)
    for link in links:
        by_kind[link.kind].append(link)
    for kind, kind_links in by_kind.items():
        out.update(READERS[kind](kind_links))
    return out


# ---------------------------------------------------------------------------------- progress
@dataclass
class Progress:
    indicator: Indicator
    years: dict[int, dict[str, Any]] = field(default_factory=dict)  # year -> {value, source, parts}
    value: float | None = None
    value_year: int | None = None
    expected: float | None = None  # % of the way expected by now
    achieved: float | None = None  # % of the way covered
    status: str = NO_DATA
    milestone: float | None = None

    @property
    def label(self) -> str:
        if self.status == AHEAD and self.achieved is not None and self.achieved >= 100:
            return "Target reached"
        return LABELS[self.status]


def _share(value: float, baseline: float | None, target: float) -> float | None:
    start = baseline or 0.0
    if target == start:
        return 100.0 if value == target else None
    return (value - start) / (target - start) * 100


def progress(
    indicator: Indicator, values: dict[int, float | None], today: datetime.date, elapsed: float
) -> Progress:
    """``values``: the figure of every link (from ``link_values``)."""
    result = Progress(indicator)
    manual = {v.year: v for v in indicator.values.all()}
    by_year: dict[int, list[tuple[Link, float | None]]] = defaultdict(list)
    for link in indicator.links.all():
        if link.confirmed:
            by_year[link.year].append((link, values.get(link.pk)))
    for year in indicator.programme.years:
        if year in manual:
            v = manual[year]
            result.years[year] = {"value": v.value, "source": v.source, "parts": [], "manual": True}
        elif by_year.get(year):
            parts = [
                {"label": lk.label, "kind": lk.get_kind_display(), "value": val} for lk, val in by_year[year]
            ]
            known = [p["value"] for p in parts if p["value"] is not None]
            result.years[year] = {
                "value": sum(known) if known else None,
                "source": "linked sources",
                "parts": parts,
                "manual": False,
            }
    years_with_value = [y for y, row in sorted(result.years.items()) if row["value"] is not None]
    if years_with_value:
        result.value_year = years_with_value[-1]
        if indicator.accumulate == Indicator.Accumulate.SUM:
            result.value = sum(result.years[y]["value"] for y in years_with_value)
        else:
            result.value = result.years[result.value_year]["value"]
    if indicator.target is None:
        result.status = NO_TARGET
        return result
    if result.value is None:
        result.status = NO_DATA
        return result
    milestones = {m.year: m.value for m in indicator.milestones.all()}
    this_year = min(max(today.year, indicator.programme.start_year), indicator.programme.end_year)
    result.milestone = milestones.get(this_year)
    if result.milestone is not None and today.year <= indicator.programme.end_year:
        result.expected = _share(result.milestone, indicator.baseline, indicator.target)
    else:
        result.expected = elapsed
    result.achieved = _share(result.value, indicator.baseline, indicator.target)
    if result.achieved is None or result.expected is None:
        result.status = NO_DATA
    elif result.achieved < result.expected - TOLERANCE:
        result.status = OFF_TRACK
    elif result.achieved > result.expected + TOLERANCE:
        result.status = AHEAD
    else:
        result.status = ON_TRACK
    if result.achieved is not None:
        result.achieved = round(result.achieved, 1)
    if result.expected is not None:
        result.expected = round(result.expected, 1)
    return result


# ------------------------------------------------------------------------ interventions
def _norm(text: str | None) -> str:
    """Lower case, punctuation as spaces; the dots of codes (1.1) are kept."""
    text = re.sub(r"[^a-z0-9.]+", " ", (text or "").lower())
    return " ".join(re.sub(r"(?<![0-9])\.|\.(?![0-9])", " ", text).split())


def output_matches(output: Output, etools_name: str) -> bool:
    """The eTools CP output is this output: the same name as set on the output, else the output's
    code at the start of the eTools name ("1.1 Children…", "Output 1.1: …")."""
    name = _norm(etools_name)
    if output.etools_output:
        return name == _norm(output.etools_output)
    code = re.escape(output.code.strip().lower())
    return bool(re.match(rf"^(output\s+)?{code}(?![0-9])", name))


def interventions(programme: CountryProgramme) -> list[Any]:
    """The programme documents running in the cycle (of its eTools country programme when set)."""
    from django.db.models import Q

    from neurodb.partnerships.models import PCA

    first, last = datetime.date(programme.start_year, 1, 1), datetime.date(programme.end_year, 12, 31)
    qs = (
        PCA.objects.exclude(status__in=CLOSED_PD_STATUSES)
        .filter(Q(start__isnull=True) | Q(start__lte=last), Q(end__isnull=True) | Q(end__gte=first))
        .select_related("partner")
    )
    if programme.etools_name:
        qs = qs.filter(country_programme__iexact=programme.etools_name.strip())
    return list(qs)


# --------------------------------------------------------------------------------- dashboard
def dashboard(programme: CountryProgramme, today: datetime.date | None = None) -> dict[str, Any]:
    from neurodb.datamart import monitoring

    today = today or datetime.date.today()
    elapsed = elapsed_share(programme, today)
    indicators = list(
        Indicator.objects.filter(programme=programme)
        .select_related("programme", "outcome", "output")
        .prefetch_related("values", "milestones", "links")
    )
    values = link_values([lk for ind in indicators for lk in ind.links.all() if lk.confirmed])
    progress_of = {ind.pk: progress(ind, values, today, elapsed) for ind in indicators}

    pds = interventions(programme)
    current_year = min(max(today.year, programme.start_year), programme.end_year)
    outcomes = []
    all_pd_ids: set[int] = set()
    output_pds: dict[int, list[Any]] = {}
    for outcome in programme.outcomes.prefetch_related("outputs"):
        for output in outcome.outputs.all():
            matched = [
                pd for pd in pds if any(output_matches(output, name) for name in (pd.cp_outputs or []))
            ]
            output_pds[output.pk] = matched
            all_pd_ids.update(pd.pk for pd in matched)
    reporting = defaultdict(lambda: defaultdict(int))  # pd id -> status -> PD indicators
    if all_pd_ids:
        filters = monitoring.Filters(pd_ids=sorted(all_pd_ids), scope="all", year=current_year)
        for row in monitoring.indicators(filters):
            reporting[row.pd.id][row.tracking] += 1

    counts = defaultdict(int)
    for p in progress_of.values():
        counts[p.status] += 1
    for outcome in programme.outcomes.prefetch_related("outputs"):
        outputs = []
        for output in outcome.outputs.all():
            matched = output_pds.get(output.pk, [])
            statuses = defaultdict(int)
            for pd in matched:
                for status, n in reporting.get(pd.pk, {}).items():
                    statuses[status] += n
            outputs.append(
                {
                    "output": output,
                    "indicators": [progress_of[i.pk] for i in indicators if i.output_id == output.pk],
                    "pds": matched,
                    "partners": sorted(
                        {(pd.partner.name if pd.partner else pd.partner_name or "") for pd in matched}
                    ),
                    "reporting": dict(statuses),
                    "reported": sum(statuses.values()),
                }
            )
        own = [progress_of[i.pk] for i in indicators if i.outcome_id == outcome.pk]
        every = own + [p for row in outputs for p in row["indicators"]]
        outcome_counts = defaultdict(int)
        for p in every:
            outcome_counts[p.status] += 1
        outcomes.append(
            {
                "outcome": outcome,
                "indicators": own,
                "outputs": outputs,
                "counts": [(s, outcome_counts[s]) for s in STATUS_ORDER if outcome_counts.get(s)],
                "total": len(every),
            }
        )
    partners = {pd.partner_id or pd.partner_name for pd in pds if pd.pk in all_pd_ids}
    return {
        "programme": programme,
        "today": today,
        "elapsed": elapsed,
        "year_of_cycle": (min(today.year, programme.end_year) - programme.start_year + 1)
        if today.year >= programme.start_year
        else 0,
        "cycle_years": len(programme.years),
        "outcomes": outcomes,
        "counts": {s: counts.get(s, 0) for s in STATUS_ORDER},
        "indicator_total": len(indicators),
        "pd_total": len(all_pd_ids),
        "pd_in_cycle": len(pds),
        "partner_total": len(partners),
        "reporting_year": current_year,
        "unreviewed": sum(1 for i in indicators if i.origin == "ai_suggested"),
        "status_chart": {s: counts.get(s, 0) for s in STATUS_ORDER if counts.get(s)},
        "status_labels": LABELS,
    }


def indicator_detail(indicator: Indicator, today: datetime.date | None = None) -> Progress:
    today = today or datetime.date.today()
    links = [lk for lk in indicator.links.all() if lk.confirmed]
    return progress(indicator, link_values(links), today, elapsed_share(indicator.programme, today))
