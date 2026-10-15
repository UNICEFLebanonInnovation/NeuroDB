"""Where the Country Programme stands: each indicator's value, its status, and the interventions and
partner reporting behind each output.

An indicator's value for a year is the value typed in the admin for that year, else the sum of its
confirmed linked sources for that year (eTools PD indicators: what the partner reached, from
``neurodb.datamart.reached``; ActivityInfo: the master indicator's result; Compiler: young people
reached, children enrolled). Linked sources of one year are different partners' figures added up:
they may reach the same people. The cycle's value is the latest year's value (a level) or the sum over
the cycle's years (people reached), as the indicator says; so an eTools PD indicator counts, for a
year, its Reached in that year when the CP indicator sums over the cycle (a PD running over several
years is never counted twice), and its Reached to the end of that year when it takes the latest year's
value.

Status: the share of the way from baseline to target that the value has covered, compared with the
share of the cycle elapsed (or with 100 % of the year's milestone when one is set): within 10 points
is on track, below is off track, above is ahead of schedule — the same rule as the other NeuroDB
pages. No target: "no target"; no value yet: "no data".

The page (``cpd.views.dashboard``) loads in parts: its two parts read one :func:`build`, kept ten minutes in
the cache every worker shares (:func:`neurodb.core.compute.once`) under a key that holds the cycle, the day,
everything staff enter by hand that the figures read (:func:`hand_entered`: the cycle, its outcomes, outputs,
indicators, milestones, values and linked sources, and the ActivityInfo set-up of the linked master
indicators) and :func:`neurodb.core.compute.data_stamp` (the last finished sync of any job, which covers
eTools, the eTools Datamart, ActivityInfo and the two Compiler reads, and the indicator flags). A build is
plain data (:class:`IndicatorRef` and its kin stand for the rows the page draws), the same for every user;
the linked sources' kinds are translated as it is computed, so a build is kept per language.
"""

from __future__ import annotations

import datetime
import re
from collections import defaultdict
from dataclasses import dataclass, field, replace
from typing import Any

from django.conf import settings
from django.core.cache import cache as process_cache
from django.db import connection

from neurodb.core import compute
from neurodb.indicators.services.tracking import TOLERANCE

from .models import CountryProgramme, Indicator, Link, Milestone, Outcome, Output, Value

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
CACHE_SECONDS = 600  # a build in the shared cache; its key moves with every edit and every finished sync
CACHE_VERSION = 1
EDUCATION_TOTAL_SECONDS = 600  # children enrolled in a stored payload, per worker (keyed on its fetch)


def current_cycle() -> CountryProgramme | None:
    """The cycle the Country programme page opens on without ``?cycle=``: the one ticked current, else the
    newest (``cpd.views._cycle``). Home and the menu read it."""
    return CountryProgramme.objects.order_by("-current", "-start_year", "name").first()


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
    """Each eTools link's figure for its year (:mod:`neurodb.datamart.reached`): the PD indicator's
    Reached in that year when its CP indicator sums over the cycle (``Accumulate.SUM``), else its
    Reached to the end of that year (``Accumulate.LATEST``)."""
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
        found = {(row.pd.id, row.key): row for row in rows}
        for link in year_links:
            row = found.get((link.pd_id, link.etools_key))
            if row is None:
                out[link.pk] = None
            elif link.indicator.accumulate == Indicator.Accumulate.SUM:
                out[link.pk] = row.reached_in_year
            else:
                out[link.pk] = row.result.through(year) if row.result is not None else row.reached
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
    """Children enrolled in the Makani or Dirasa figures each link names. Each stored payload (1-5 MB) is read
    once per call, however many links name it (it was read once per link: eight links, eight reads), and its
    total is kept per worker for as long as the figures are not fetched again (:func:`_stored_total`)."""
    from neurodb.education.models import EducationFigures

    wanted = {(link.education_programme, link.source_period) for link in links}
    records: dict[tuple[str, str], Any] = {}
    if wanted:
        rows = EducationFigures.objects.defer("payload").filter(
            programme__in={programme for programme, _period in wanted},
            year__in={period for _programme, period in wanted},
        )
        for record in rows:
            records.setdefault((record.programme, record.year), record)
    totals = {ident: _stored_total(record) for ident, record in records.items()}
    return {
        link.pk: float(totals[ident])
        if (ident := (link.education_programme, link.source_period)) in totals
        else None
        for link in links
    }


def _stored_total(record: Any) -> int:
    """:func:`education_total` of a stored row (read without its payload), kept per worker under its id, its
    fetch time and the card versions chosen: a new fetch of the figures, or a new choice, is a new key. The
    payload is read only when the total is not kept."""
    from neurodb.education import power_bi
    from neurodb.education.models import EducationFigures

    stamp = record.fetched_at.isoformat() if record.fetched_at else ""
    key = f"cpd:education-total:{record.pk}:{stamp}:{power_bi.cards_stamp()}"
    total = process_cache.get(key)
    if total is None:
        payload = EducationFigures.objects.filter(pk=record.pk).values_list("payload", flat=True).first()
        total = education_total(payload or {}, record.programme)
        process_cache.set(key, total, EDUCATION_TOTAL_SECONDS)
    return total


def education_total(payload: dict[str, Any], programme: str | None = None) -> int:
    """Children enrolled in a Makani or Dirasa payload (``programme``, else the payload's own), as the
    programme's page counts its children card (one definition per headline figure): the version an
    administrator chose, by default the registrations (Makani) or the unique children (Dirasa)
    (:func:`neurodb.education.power_bi.children_total`)."""
    from neurodb.education import power_bi

    return power_bi.children_total(payload or {}, programme)


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


# ------------------------------------------------------------------------------ plain rows
# What a build holds of the rows the page draws: plain data, so a build can be kept for every worker (a model
# instance would query again for each relation a template follows once read back). Each has the attributes the
# templates and Ask NeuroDB read, under the model's names.
@dataclass(frozen=True)
class IndicatorRef:
    pk: int
    code: str
    title: str
    unit: str
    accumulate: str
    baseline: float | None
    baseline_year: int | None
    target: float | None
    origin: str

    @classmethod
    def of(cls, indicator: Indicator) -> IndicatorRef:
        return cls(
            pk=indicator.pk,
            code=indicator.code,
            title=indicator.title,
            unit=indicator.unit,
            accumulate=indicator.accumulate,
            baseline=indicator.baseline,
            baseline_year=indicator.baseline_year,
            target=indicator.target,
            origin=indicator.origin,
        )


@dataclass(frozen=True)
class OutcomeRef:
    pk: int
    code: str
    title: str
    sections: str


@dataclass(frozen=True)
class OutputRef:
    pk: int
    code: str
    title: str


@dataclass(frozen=True)
class PDRef:
    """A programme document contributing to an output: the page links to it by its number."""

    pk: int
    number: str | None


# ---------------------------------------------------------------------------------- progress
@dataclass
class Progress:
    indicator: Indicator | IndicatorRef
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
def cycle_facts(programme: CountryProgramme, today: datetime.date) -> dict[str, Any]:
    """Where the cycle stands in time (arithmetic only): the page's frame draws it, and every build carries
    it."""
    return {
        "today": today,
        "elapsed": elapsed_share(programme, today),
        "year_of_cycle": (min(today.year, programme.end_year) - programme.start_year + 1)
        if today.year >= programme.start_year
        else 0,
        "cycle_years": len(programme.years),
    }


def dashboard(programme: CountryProgramme, today: datetime.date | None = None) -> dict[str, Any]:
    """Every figure of the cycle's page (and of Ask NeuroDB's country programme look-up), as plain data: the
    rows it draws are :class:`IndicatorRef`, :class:`OutcomeRef`, :class:`OutputRef` and :class:`PDRef`."""
    from neurodb.datamart import monitoring

    today = today or datetime.date.today()
    facts = cycle_facts(programme, today)
    elapsed = facts["elapsed"]
    indicators = list(
        Indicator.objects.filter(programme=programme)
        .select_related("programme", "outcome", "output")
        .prefetch_related("values", "milestones", "links")
    )
    values = link_values([lk for ind in indicators for lk in ind.links.all() if lk.confirmed])
    progress_of = {
        ind.pk: replace(progress(ind, values, today, elapsed), indicator=IndicatorRef.of(ind))
        for ind in indicators
    }

    pds = interventions(programme)
    current_year = min(max(today.year, programme.start_year), programme.end_year)
    outcomes = []
    all_pd_ids: set[int] = set()
    output_pds: dict[int, list[Any]] = {}
    framework = list(programme.outcomes.prefetch_related("outputs"))
    for outcome in framework:
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
    for outcome in framework:
        outputs = []
        for output in outcome.outputs.all():
            matched = output_pds.get(output.pk, [])
            statuses = defaultdict(int)
            for pd in matched:
                for status, n in reporting.get(pd.pk, {}).items():
                    statuses[status] += n
            outputs.append(
                {
                    "output": OutputRef(pk=output.pk, code=output.code, title=output.title),
                    "indicators": [progress_of[i.pk] for i in indicators if i.output_id == output.pk],
                    "pds": [PDRef(pk=pd.pk, number=pd.number) for pd in matched],
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
                "outcome": OutcomeRef(
                    pk=outcome.pk, code=outcome.code, title=outcome.title, sections=outcome.sections
                ),
                "indicators": own,
                "outputs": outputs,
                "counts": [(s, outcome_counts[s]) for s in STATUS_ORDER if outcome_counts.get(s)],
                "total": len(every),
            }
        )
    partners = {pd.partner_id or pd.partner_name for pd in pds if pd.pk in all_pd_ids}
    return {
        **facts,
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


# ------------------------------------------------------------------------- the stored build
def hand_entered(programme: CountryProgramme) -> str:
    """Everything staff enter by hand that the cycle's figures read, as one digest that changes with every
    edit, addition or removal: the cycle itself (its eTools country programme and its years choose the
    interventions and the years shown), its outcomes, outputs (their eTools names match the programme
    documents), indicators (baseline, target, how the years add up), milestones, values and linked sources
    (a suggestion confirmed), and, for the ActivityInfo master indicators linked, how the admin set them up
    (the database, the master indicator, its sub-indicators, which indicators each lists, and those
    indicators themselves: their ActivityInfo field and database choose the reports a figure adds up). One
    query over the rows themselves, so an edit in place shows even where a table keeps no time of change. Read
    once per request."""

    def read() -> str:
        from neurodb.indicators.models import (
            Database,
            IndicatorNew,
            MasterIndicator,
            MasterSubIndicator,
            SubIndicator,
        )

        q = connection.ops.quote_name
        table = {
            "cycle": q(CountryProgramme._meta.db_table),
            "outcome": q(Outcome._meta.db_table),
            "output": q(Output._meta.db_table),
            "indicator": q(Indicator._meta.db_table),
            "milestone": q(Milestone._meta.db_table),
            "value": q(Value._meta.db_table),
            "link": q(Link._meta.db_table),
            "master": q(MasterIndicator._meta.db_table),
            "database": q(Database._meta.db_table),
            "master_sub": q(MasterSubIndicator._meta.db_table),
            "sub": q(SubIndicator._meta.db_table),
            "sub_leaf": q(SubIndicator.indicators.through._meta.db_table),
            "leaf": q(IndicatorNew._meta.db_table),
        }
        outcomes = "SELECT id FROM outcomes"
        indicators = "SELECT id FROM indicators"
        masters = "SELECT id FROM masters"
        subs = f"SELECT sub_id FROM {table['master_sub']} WHERE master_id IN ({masters})"  # noqa: S608
        leaves = f"SELECT indicatornew_id FROM {table['sub_leaf']} WHERE subindicator_id IN ({subs})"  # noqa: S608
        # (table, the rows of the cycle in it): every row's whole text, in a stable order
        parts = [
            ("cycle", "id = %(cycle)s"),
            ("outcome", "programme_id = %(cycle)s"),
            ("output", f"outcome_id IN ({outcomes})"),
            ("indicator", "programme_id = %(cycle)s"),
            ("milestone", f"indicator_id IN ({indicators})"),
            ("value", f"indicator_id IN ({indicators})"),
            ("link", f"indicator_id IN ({indicators})"),
            ("master", f"id IN ({masters})"),
            ("database", f"id IN (SELECT database_id FROM {table['master']} WHERE id IN ({masters}))"),  # noqa: S608
            ("master_sub", f"master_id IN ({masters})"),
            ("sub", f"id IN ({subs})"),
            ("sub_leaf", f"subindicator_id IN ({subs})"),
            # the indicators themselves (facts.queries._LEAF_CTE reads their ai_indicator and database)
            ("leaf", f"id IN ({leaves})"),
        ]
        rows = "coalesce((SELECT string_agg(t::text, ',' ORDER BY t::text) FROM {} t WHERE {}), '')"
        digests = ", ".join(rows.format(table[name], where) for name, where in parts)
        # the table names come from the models, every value is a parameter
        sql = (
            f"WITH outcomes AS (SELECT id FROM {table['outcome']} WHERE programme_id = %(cycle)s), "  # noqa: S608
            f"indicators AS (SELECT id FROM {table['indicator']} WHERE programme_id = %(cycle)s), "
            f"masters AS (SELECT master_id AS id FROM {table['link']} WHERE kind = %(activityinfo)s "
            f"AND master_id IS NOT NULL AND indicator_id IN ({indicators})) "
            f"SELECT md5(concat_ws('|', {digests}))"
        )
        with connection.cursor() as cursor:
            cursor.execute(sql, {"cycle": programme.pk, "activityinfo": Link.Kind.ACTIVITYINFO})
            return cursor.fetchone()[0]

    return compute.per_request(f"cpd:hand-entered:{programme.pk}", read)


def fingerprint(programme: CountryProgramme) -> str:
    """What a build of the cycle depends on besides the cycle and the day, as 16 hex digits: what staff enter
    by hand (:func:`hand_entered`), the Makani and Dirasa card versions an administrator chose (the children
    enrolled a Compiler source gives, :func:`neurodb.education.power_bi.cards_stamp`) and
    :func:`neurodb.core.compute.data_stamp` (the last finished sync, the indicator flags). In the build's key,
    and in the address of each part of the page (``as_of``)."""
    from neurodb.education import power_bi

    return compute.fingerprint(f"{hand_entered(programme)}:{power_bi.cards_stamp()}:{compute.data_stamp()}")


def _cache_key(programme: CountryProgramme, today: datetime.date, stamp: str = "") -> str:
    """The cycle, the day (the share of the cycle elapsed and the year of partner reporting follow it) and
    :func:`fingerprint` (read now unless given). Nothing about the person asking: a build is the same for
    everyone (the staff-only admin link is the frame's)."""
    return f"cpd:v{CACHE_VERSION}:{programme.pk}:{today.isoformat()}:{stamp or fingerprint(programme)}"


def build(programme: CountryProgramme, today: datetime.date, *, as_of: str = "") -> dict[str, Any]:
    """:func:`dashboard` of the cycle, computed once across every worker and kept ten minutes (per language:
    the linked sources' kinds are translated). ``as_of``, in a part request, is the :func:`fingerprint` the
    page's frame read: both parts show one build even when an edit or a sync came before or between them
    (:func:`neurodb.core.compute.once_as_of`). DEBUG computes it afresh."""
    if settings.DEBUG:
        return dashboard(programme, today)
    return compute.once_as_of(
        lambda stamp: _cache_key(programme, today, stamp),
        as_of,
        fingerprint(programme),
        lambda: dashboard(programme, today),
        CACHE_SECONDS,
    )


def stored(programme: CountryProgramme, today: datetime.date) -> bool:
    """Whether :func:`build` would answer at once, from what is already stored: the page's frame then draws
    both parts inline (it never computes the build itself)."""
    if settings.DEBUG:
        return False
    return compute.peek(_cache_key(programme, today))[0]


def indicator_detail(indicator: Indicator, today: datetime.date | None = None) -> Progress:
    today = today or datetime.date.today()
    links = [lk for lk in indicator.links.all() if lk.confirmed]
    return progress(indicator, link_values(links), today, elapsed_share(indicator.programme, today))
