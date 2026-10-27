"""The figure trail of Partner monitoring, its map and Home's Indicator progress block (SPEC_validation
§3.4.3-3.4.6): for each figure, where its indicators come from, step by step, from what eTools said it had to
what the page shows, each step with its count and the records it leaves out (as CSV,
:mod:`neurodb.datamart.trail_rows`), for the people who check the figures (``roles.can_validate``).

* **The page's own functions, never a copy.** The steps are what ``monitoring.indicators`` records from inside
  (its ``steps``: each filter of ``_pd_queryset``, ``_indicator_rows`` and ``collect`` that applies, in its
  order, then the sections and the tracking status), and the Reached fold what ``_attach_reached``,
  ``reached.compute`` and ``progress_values`` tell its ``record`` (``trail.Recorder``): where each partner
  report row went and why, and what each indicator's Reached read. Each figure is counted again from the
  indicators the steps keep with the page's own tests (``monitoring.FIGURE_TESTS`` and ``FIGURE_KEYS``).
  *On the page* is the page's own value: ``summary``, ``data_quality``, ``program_performance_rows`` and
  ``section_counts`` of the stored indicators (``stored_indicators``), ``stored_map_points``.
* **One build, not two.** When the page's indicators are not stored, the trail's recorded build is the page's:
  it is stored for the page under its own key once the trail's snapshot closes. Only a warm stored value makes
  the trail build them a second time (recorded); a figure whose two values then differ (a sync ended in
  between) is listed in red, never hidden. Program Performance with no section chosen reads that trail's
  build; with sections, it records the filter without them.
* **One state of the data.** A trail is read in one ``REPEATABLE READ READ ONLY`` snapshot and kept two
  minutes for every worker (``compute.once``), keyed on the filter, the day and the moment of the eTools
  figures. What is kept is ids, keys, counts and each indicator's tests, never a record's payload.
"""

from __future__ import annotations

import dataclasses
import datetime
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from django.conf import settings
from django.db.models import Count

from neurodb.core import compute
from neurodb.indicators.services.tracking import NO_TARGET
from neurodb.partnerships import statuses as pd_statuses
from neurodb.partnerships.models import PCA

from . import identity, keys, monitoring, owed, reporting, runfacts, trail
from . import models as dm
from . import reached as rc
from .trail import (
    INDICATOR,
    IR,
    PARTNER,
    PD,
    PD_ROW,
    PLACE,
    PR,
    ROW,
    SECTION,
    TITLE,
    Block,
    Figure,
    Line,
    words,
)

PAGE = "partner-monitoring"
SOURCE = "pd_indicators"  # the Datamart dataset the indicators are read from
PDS = "interventions"  # and their programme documents (Raw eTools data's, for a download too large)
REPORTS = "partner_reports"  # and what partners reached on them
TOTALS = "prp_indicator_reports"  # PRP's own report totals
CACHE_VERSION = 2  # 2: the Reached fold is one chain, in report rows and indicator reports
# the folded parts of the page's trail, each its own computation: Program Performance reads the filter without
# its sections, the map its places
TABLES = ("performance", "map")
NOTES = (
    "Partner monitoring reads the PD indicator's section; Progress reports the report row's.",
    "Overdue reports on Partner monitoring counts progress reports with any row not submitted after its "
    "due date, quarterly reports only by default; Progress reports' Overdue is a report status, read from "
    "all the report's rows.",
    "Reached is read from Accepted and Submitted reports only, never adds a value repeated on every "
    "location, and takes PRP's cumulative for a programme document that started before the reports NeuroDB "
    "holds.",
    "Under a location filter, the table cells read the chosen locations only; the indicator pop-up reads "
    "every location.",
    "An indicator is counted once, although eTools lists it once per location and disaggregation.",
)
DIFFERS = trail.DIFFERS
# the tests of monitoring.FIGURE_TESTS a "Kept indicators" CSV writes, one yes/no column each, in this order
TEST_COLUMNS = (
    "with_target",
    "with_reached",
    "reported_zero",
    "differs",
    "dq_without",
    "dq_zero",
    "missing_target",
    "no_reached",
    "partial",
    "running",
    "active",
    "ended",
    *(f"tracking.{key}" for key in monitoring.LABELS),
)
TEST_WORDS = {  # how each test counts, in one line
    "with_target": "With a target (a target of 0 counts as none)",
    "with_reached": "With Reached to date (a counted report, Accepted or Submitted, carries a value)",
    "reported_zero": "Reached to date is 0",
    "differs": "NeuroDB's Reached differs from PRP's cumulative",
    "dq_without": "Without Reached (in the year chosen, else to date)",
    "dq_zero": "Reached is 0 (in the year chosen, else to date)",
    "missing_target": "Without a target (a target of 0 counts as none)",
    "no_reached": "Without Reached, or Reached 0 (in the year chosen, else to date)",
    "partial": "Its programme document started before the first reporting period NeuroDB holds",
    "running": "Its programme document is running (active, signed or suspended)",
    "active": "Its programme document's status is active",
    "ended": "Its programme document ended (ended, closed, terminated or expired)",
    **{f"tracking.{key}": f"Tracking status {label}" for key, label in monitoring.LABELS.items()},
}


def filters_of(params: Any) -> monitoring.Filters:
    """The page's filter from its address, as the page reads it: the grid and the map open on this year,
    Program Performance and Data quality on every year (the check line always carries the year shown)."""
    view = params.get("view") or "grid"
    return monitoring.Filters.from_params(params, default_year=view in ("grid", "map", "masters"))


# ------------------------------------------------------------------------------------------------- the values
@dataclass(frozen=True)
class Stage:
    """One step of the chain: what it counts (``unit``), what is ``kept`` after it and what it leaves ``out``
    (None: nothing, a step that only says what the one before it kept), the PD indicator rows it keeps and
    leaves out (the programme documents' steps), its lines and the CSV of what it leaves out (``what``). The
    Reached fold's steps count indicator reports too (``irs_kept``; those left out entirely, and partly: some
    of their rows kept), and its monthly pass the rows it adds (``added``, None for every other step)."""

    key: str
    title: str
    unit: str
    kept: int
    out: int | None = None
    rows_kept: int | None = None
    rows_out: int = 0
    lines: tuple[Line, ...] = ()
    what: str = ""
    counts_only: bool = False
    irs_kept: int | None = None
    irs_out: int = 0
    irs_partly: int = 0
    added: int | None = None
    irs_added: int = 0

    @property
    def kept_words(self) -> str:
        found = words(self.kept, self.unit)
        if self.rows_kept is not None:
            found += f" ({words(self.rows_kept, PD_ROW)})"
        if self.irs_kept is not None:
            found += f", {words(self.irs_kept, IR)}"
        return found

    @property
    def out_words(self) -> str:
        if self.out is None:
            return ""
        if self.added is not None:
            return f"Added: {words(self.added, self.unit)}, {words(self.irs_added, IR)}."
        if not self.out and not self.rows_out:
            return "Nothing left out."
        found = "Left out: " + words(self.out, self.unit)
        if self.rows_kept is not None:
            found += f" ({words(self.rows_out, PD_ROW)})"
        if self.irs_kept is not None:
            found += f"; {words(self.irs_out, IR)} left out entirely"
            if self.irs_partly:
                found += f", {self.irs_partly:,} partly (some of their rows kept)"
        return found + "."


@dataclass(frozen=True)
class Kept:
    """What the trail reads of one indicator (of the indicators the PD indicator rows make, M16): its key (its
    page's address), programme document, linked partner, section, title, PD output and CP output, the step
    that left it out (``len(stages)``: kept), the tests of ``monitoring.FIGURE_TESTS`` it passes, its Reached
    (to date and in the year), the partner report rows its Reached read, and the rows of its title left out
    (a title several indicators of the PD share)."""

    key: str
    pd: int
    pd_number: str
    partner: int | None
    section: str
    title: str
    output: str
    cp_output: str
    stage: int
    tests: frozenset[str] = frozenset()
    reached: float | None = None
    reached_in_year: float | None = None
    rows_placed: int = 0
    title_rows_left_out: int = 0


@dataclass(frozen=True)
class Chain:
    """The chain computed once (SPEC_validation §3.6), for one filter and day: ``stages`` every step in order
    (its kind: ``pd``, ``rows`` or ``indicators``, and its name), the programme documents in NeuroDB (the step
    that left each out, its status, its linked partner, its PD indicator rows), the PD indicator rows of the
    programme documents kept (their indicator, the step that left them out), every indicator (:class:`Kept`),
    the indicators kept in the page's order, the PD indicator rows in NeuroDB and those naming no programme
    document NeuroDB holds, and the folds. Never a record's payload."""

    stages: tuple[tuple[str, str], ...]
    pds: dict[int, tuple[int, str, int | None, int]]
    rows: dict[int, tuple[int, int]]
    idents: tuple[tuple[int, str], ...]
    indicators: dict[tuple[int, str], Kept]
    kept: tuple[tuple[int, str], ...]
    held: int
    orphans: int
    fold: Fold
    overdue: dict[int, int]  # partner report row pk -> the Overdue reports step that left it out (3: kept)
    overdue_reports: int  # the progress reports of the rows kept, counted from their keys
    broken: tuple[str, ...] = ()  # the steps that kept records a step before them had left out (never)
    # the Overdue reports still owed in PRP's own list that the rows do not have (owed.overdue_count; 0 while
    # the gate is closed), which the page's Overdue reports adds
    owed_overdue: int = 0

    def stage_of(self, kind: str) -> list[tuple[int, str]]:
        return [(i, name) for i, (k, name) in enumerate(self.stages) if k == kind]

    @property
    def kept_at(self) -> int:
        return len(self.stages)

    @property
    def kept_indicators(self) -> list[Kept]:
        return [self.indicators[i] for i in self.kept]

    @property
    def pds_kept(self) -> set[int]:
        """The programme documents every programme document step keeps (M10)."""
        last = len(self.stage_of("pd"))
        return {pd for pd, facts in self.pds.items() if facts[0] >= last}


@dataclass(frozen=True)
class Fold:
    """Where the partner report rows go, for Reached (SPEC_validation §3.4.4): ``rows`` every report row in
    NeuroDB by pk (its class: R0 a programme document NeuroDB does not hold or none, R1 a programme document
    the filter does not read, R2 another report type, R3 read; its indicator report, a number), ``placed``
    each row read by pk (its pass, the indicator's number in ``Chain.kept``, -1 for none, -2 for one the
    filter leaves out, the way, whether the indicator read it, what became of it), ``reports`` each
    indicator's reports (status, counted, with a value, where the value came from), ``values`` each
    indicator's (Reached, partial history, from PRP's cumulative), ``shared`` the rows of a shared title left
    out, ``totals`` PRP's report totals found with a value and kept for the rows the indicators kept read."""

    report_type: str
    rows: dict[int, tuple[str, int]]
    placed: dict[int, tuple[str, int, str, bool, str]]
    reports: dict[tuple[int, str], tuple[tuple[str, str, bool, bool, str], ...]]
    values: dict[tuple[int, str], tuple[Any, bool, str, str]]
    shared: dict[tuple[int, str, str], int]
    totals: tuple[int, int]
    pd_ids: frozenset[int]


@dataclass(frozen=True)
class MonitoringTrail:
    """The trail of Partner monitoring's grid and Data quality (or of Home's Indicator progress block,
    ``home``) for a filter and a day, as kept for two minutes: the steps, the Reached and Overdue reports
    folds, every figure on the page and rebuilt, and the page's own summary (its tiles, drawn as on the
    page)."""

    query: str
    day: datetime.date
    as_of: datetime.datetime | None
    home: bool
    steps: tuple[Any, ...]
    reached: tuple[Stage, ...]
    overdue: tuple[Stage, ...]
    blocks: tuple[Block, ...]
    rows: Chain
    summary: dict[str, Any]
    filter_words: tuple[str, ...] = ()
    notes: tuple[str, ...] = NOTES

    @property
    def figures(self) -> list[Figure]:
        return [f for b in self.blocks for f in b.figures]

    @property
    def differences(self) -> list[Figure]:
        return [f for f in self.figures if not f.same]

    @property
    def dataset_steps(self) -> list[Any]:
        return [s for s in self.steps if isinstance(s, trail.Step)]

    @property
    def chain_steps(self) -> list[Stage]:
        return [s for s in self.steps if isinstance(s, Stage)]

    def figure(self, key: str) -> Figure | None:
        return next((f for f in self.figures if f.key == key), None)

    def step(self, key: str) -> Any:
        return next((s for s in (*self.steps, *self.reached, *self.overdue) if s.key == key), None)

    def block_of(self, key: str) -> str:
        return next((b.key for b in self.blocks if any(f.key == key for f in b.figures)), "")


@dataclass(frozen=True)
class TableTrail:
    """The trail of Program Performance (``performance``) or of the map (``map``), each a part of its own."""

    tab: str
    query: str
    day: datetime.date
    steps: tuple[Stage, ...]
    block: Block
    rows: Chain
    lines: tuple[Line, ...] = ()
    # Program Performance's: the indicators of the sections chosen (None: those of ``rows``)
    kept: tuple[tuple[int, str], ...] | None = None

    @property
    def figures(self) -> list[Figure]:
        return list(self.block.figures)

    def figure(self, key: str) -> Figure | None:
        return next((f for f in self.block.figures if f.key == key), None)


# ------------------------------------------------------------------------------------------------ the chain
def _split(steps: list[tuple[str, Any]]) -> tuple[list, list, list]:
    """``indicators(steps=...)``'s record, by what each step keeps: programme documents, PD indicator rows,
    indicators."""
    pds: list = []
    rows: list = []
    found: list = []
    target = pds
    for name, value in steps:
        if name == "rows":
            target = rows
        elif name == "one_per_indicator":
            target = found
        target.append((name, value))
    return pds, rows, found


def chain(
    filters: monitoring.Filters, day: datetime.date, steps: list, recorder: trail.Recorder, kept: list[Any]
) -> Chain:
    """The steps of a recorded ``indicators(filters, day, steps=..., record=...)`` (``kept``: what it
    returned) as one value: one query per filter that applies (its ids), the PD indicator rows per programme
    document (one grouped count), the rows of the programme documents kept (their indicator), every indicator
    with its tests, the Overdue reports' rows and the Reached fold. A record is left out by the step at its
    index in ``stages``; one every step of its kind keeps holds the index of the next kind's first step."""
    pd_steps, row_steps, found_steps = _split(steps)
    stages: list[tuple[str, str]] = []
    broken: list[str] = []
    pds: dict[int, list] = {}
    if pd_steps:
        for pk, status, partner in pd_steps[0][1].values_list("pk", "status", "partner_id").order_by():
            pds[pk] = [len(pd_steps), status or "", partner, 0]
        previous = set(pds)
        for index, (name, queryset) in enumerate(pd_steps):
            stages.append(("pd", name))
            if index:
                current = set(queryset.values_list("pk", flat=True).order_by())
                if current - previous:
                    broken.append(name)
                for pk in previous - current:
                    pds[pk][0] = index
                previous &= current
    counts = (
        dm.PDIndicator.objects.exclude(intervention=None)
        .values("intervention_id")
        .annotate(n=Count("pk"))
        .order_by()
        .values_list("intervention_id", "n")
    )
    for pk, n in counts:
        if pk in pds:
            pds[pk][3] = n
    idents: dict[tuple[int, str], int] = {}
    rows: dict[int, list] = {}
    base = len(stages)
    if row_steps:
        start = row_steps[0][1].values_list("pk", "intervention_id", "source_id", "title").order_by()
        for pk, pd, source, title in start:
            ident = monitoring.ident_of(pd, source, title)
            rows[pk] = [idents.setdefault(ident, len(idents)), base + len(row_steps)]
        previous = set(rows)
        for index, (name, queryset) in enumerate(row_steps):
            stages.append(("rows", name))
            if index:
                current = set(queryset.values_list("pk", flat=True).order_by())
                if current - previous:
                    broken.append(name)
                for pk in previous - current:
                    rows[pk][1] = base + index
                previous &= current
    every: dict[tuple[int, str], Any] = found_steps[0][1] if found_steps else {}
    first = len(stages)
    stage_of = dict.fromkeys(every, first + len(found_steps))
    previous = set(every)
    for index, (name, value) in enumerate(found_steps):
        stages.append(("indicators", name))
        if index:
            for ident in previous - set(value):
                stage_of[ident] = first + index
            previous &= set(value)
    kept_ids = tuple(e.ident for e in kept)
    if previous != set(kept_ids):  # never: the steps keep what indicators() returns
        broken.append("kept")
    tests = monitoring.FIGURE_TESTS
    by_ident = {e.ident: e for e in kept}
    used = Counter(p[1] for p in recorder.placements.values() if p[3])
    shared: Counter[tuple[int, str]] = Counter()
    for (pd, title, _way), n in recorder.shared.items():
        shared[(pd, title)] += n
    indicators: dict[tuple[int, str], Kept] = {}
    for ident, entry in every.items():
        mine = by_ident.get(ident)
        indicators[ident] = Kept(
            key=entry.key,
            pd=entry.pd.id,
            pd_number=entry.pd.number or "",
            partner=entry.pd.partner_id or None,
            section=entry.section,
            title=entry.title,
            output=entry.output,
            cp_output=entry.cp_output,
            stage=stage_of[ident],
            tests=frozenset(name for name, test in tests.items() if test(mine, filters))
            if mine
            else frozenset(),
            reached=mine.reached if mine else None,
            reached_in_year=mine.reached_in_year if mine else None,
            rows_placed=used[ident],
            title_rows_left_out=shared[(ident[0], rc.norm(entry.title))],
        )
    overdue, overdue_reports = _overdue(kept, filters, day)
    owed_pds = {monitoring.FIGURE_KEYS["pds"](e) for e in kept}
    return Chain(
        stages=tuple(stages),
        pds={pk: tuple(v) for pk, v in pds.items()},
        rows={pk: tuple(v) for pk, v in rows.items()},
        idents=tuple(idents),
        indicators=indicators,
        kept=kept_ids,
        held=dm.PDIndicator.objects.count(),
        orphans=dm.PDIndicator.objects.filter(intervention=None).count(),
        fold=_fold(recorder, kept_ids),
        overdue=overdue,
        overdue_reports=overdue_reports,
        broken=tuple(broken),
        owed_overdue=owed.overdue_count(owed_pds, filters.report_type, day),
    )


def _overdue(kept: list[Any], filters: monitoring.Filters, day: datetime.date) -> tuple[dict[int, int], int]:
    """The rows of the Overdue reports' steps (``monitoring.overdue_rows`` of the programme documents of the
    indicators kept, each row with the step that left it out, 3 when kept), and the progress reports of the
    rows it keeps, counted from their keys (``reached.report_key``)."""
    steps: list[tuple[str, Any]] = []
    monitoring.overdue_rows({monitoring.FIGURE_KEYS["pds"](e) for e in kept}, filters.report_type, day, steps)
    every = {r["pk"]: r for r in reporting.unit_rows(steps[0][1])}
    rows = dict.fromkeys(every, len(steps))
    previous = set(every)
    for index, (_name, queryset) in enumerate(steps[1:], start=1):
        current = set(queryset.values_list("pk", flat=True).order_by())
        for pk in previous - current:
            rows[pk] = index
        previous &= current
    return rows, len({rc.report_key(every[pk]) for pk in previous})


def _fold(recorder: trail.Recorder, kept: tuple[tuple[int, str], ...]) -> Fold:
    """The Reached fold's rows: every partner report row in NeuroDB by its class, each row read with where it
    went and what became of it (``recorder``)."""
    numbers = {ident: n for n, ident in enumerate(kept)}
    by_id: dict[Any, int] = {}
    units: dict[tuple[str, str], int] = {}
    rows: dict[int, tuple[str, int]] = {}
    totals_of: dict[int, str] = {}  # a row's PRP indicator report id, which its report total is found by
    for row in reporting.unit_rows(dm.ReportedIndicator.objects.all()):
        unit = units.setdefault(reporting.unit_key(row), len(units))
        ir = str(row["_ir"] or "")
        if ir.isdigit() and len(ir) <= 18:  # as reached.progress_values reads it
            totals_of[row["pk"]] = str(int(ir))
        if row["intervention_id"] is None:
            kind = "R0"
        elif row["intervention_id"] not in recorder.pd_ids:
            kind = "R1"
        elif row["report_type"] != recorder.report_type:
            kind = "R2"
        else:
            kind = "R3"
        rows[row["pk"]] = (kind, unit)
        by_id[row["datamart_id"]] = row["pk"]
    reports = {ident: {r[0]: r for r in found} for ident, found in recorder.reports.items()}
    placed: dict[int, tuple[str, int, str, bool, str]] = {}
    for datamart_id, (kind, ident, way, used, report) in recorder.placements.items():
        outcome = ""
        if used:
            left = recorder.left.get(ident, {})
            mine = reports.get(ident, {}).get(report)
            if datamart_id in left.get("one_indicator", ()):
                outcome = "one_indicator"
            elif datamart_id in left.get("other_places", ()):
                outcome = "other_place"
            elif mine is None:
                outcome = "unexplained"
            elif not mine[2]:
                outcome = f"status:{mine[1]}"
            elif not mine[3]:
                outcome = "no_value"
            else:
                outcome = f"counted:{mine[4]}"
        # the indicator's number among those kept; -1: none (left out), -2: one this filter leaves out
        number = -1 if ident is None else numbers.get(ident, -2)
        placed[by_id[datamart_id]] = (kind, number, way, used, outcome)
    found, kept_totals = recorder.totals
    read = {totals_of[pk] for pk, p in placed.items() if p[1] >= 0 and p[3] and pk in totals_of}
    return Fold(
        report_type=recorder.report_type,
        rows=rows,
        placed=placed,
        reports=dict(recorder.reports),
        values=dict(recorder.values),
        shared=dict(recorder.shared),
        totals=(len(found & read), len(kept_totals & read)),
        pd_ids=recorder.pd_ids,
    )


# -------------------------------------------------------------------------------------------------- the steps
PD_TITLES = {
    "start": "Programme documents NeuroDB holds",
    "not_a_partnership": "Draft or cancelled (never read)",
    "year_scope": "Not running in {year}",
    "partner": "Not the partner chosen",
    "pds": "Not the programme document chosen ({pds})",
    "pd_ids": "Not the programme document of the page it was opened from",
    "document_type": "Document type is not {document_types}",
    "office": "Not of the office chosen ({offices})",
    "partner_type": "Partner type is not {partner_types}",
}
ROW_TITLES = {
    "rows": "PD indicator rows of the programme documents kept",
    "q": 'Does not match "{q}" (indicator or PD output)',
    "hr_only": "Humanitarian reports: only high-frequency or cluster indicators",
}
FOUND_TITLES = {
    "one_per_indicator": "One per indicator",
    "cp_output": 'CP output does not contain "{cp_output}"',
    "locations": "Not planned at the location chosen ({locations})",
    "sections": "Not in the section chosen ({sections}: the PD indicator's own section)",
    "status": "Tracking status is not {status}",
}
PLURALS = (
    ("partner chosen", "partners chosen"),
    ("document chosen", "documents chosen"),
    ("office chosen", "offices chosen"),
    ("location chosen", "locations chosen"),
    ("section chosen", "sections chosen"),
)


def stage_title(name: str, filters: monitoring.Filters) -> str:
    """A step in plain words: what it leaves out."""
    f = filters
    values = {
        "year": f.year,
        "pds": ", ".join(f.pds),
        "document_types": ", ".join(f.document_types),
        "offices": ", ".join(f.offices),
        "partner_types": ", ".join(f.partner_types),
        "q": f.q,
        "cp_output": f.cp_output,
        "locations": ", ".join(f.locations),
        "sections": ", ".join(f.sections),
        "status": monitoring.LABELS.get(f.status, f.status),
    }
    if name == "pd_status":
        if f.pd_statuses:
            return f"Status not chosen ({', '.join(pd_statuses.label(s) for s in f.pd_statuses)} chosen)"
        return "Not running (active, signed or suspended)"
    if name == "emergency":
        if f.emergency == "yes":
            return "Its programme document is not an SPD (the Emergency indicator)"
        return "Its programme document is an SPD (the Emergency indicator)"
    if name.startswith("tags."):
        from .comparison import TAG_WORDS

        tag = name[5:]
        return f"Not tagged {TAG_WORDS.get(tag, tag)}: {', '.join(f.tags.get(tag, []))}"
    text = (PD_TITLES.get(name) or ROW_TITLES.get(name) or FOUND_TITLES.get(name) or name).format(**values)
    several = {"partner": f.partners, "pds": f.pds, "office": f.offices, "locations": f.locations}
    if len(several.get(name, f.sections if name == "sections" else [])) > 1:
        for one, many in PLURALS:
            text = text.replace(one, many)
    return text


def _broken(stage: Stage, rows: Chain) -> Stage:
    if stage.key not in rows.broken:
        return stage
    alert = Line(f"This step kept records the step before it had left out. {DIFFERS}", alert=True)
    return dataclasses.replace(stage, lines=(*stage.lines, alert))


def chain_steps(rows: Chain, filters: monitoring.Filters) -> list[Stage]:
    """M1 to K2 (SPEC_validation §3.4.3): the programme documents' steps with their PD indicator rows, the
    rows' steps with the indicators they leave out entirely and partly, the indicators' steps, the indicators
    kept, and the programme documents kept with no indicator of the filter."""
    out: list[Stage] = []
    for index, name in rows.stage_of("pd"):
        if index == 0:
            held = list(rows.pds)
            lines = []
            expected = rows.held - rows.orphans
            if sum(rows.pds[pk][3] for pk in held) != expected:
                lines.append(
                    Line(
                        f"These PD indicator rows are not the {expected:,} in NeuroDB that name a programme "
                        f"document. {DIFFERS}",
                        alert=True,
                    )
                )
            what, more = _csv("step:M1" if held else "", len(held), PDS)
            out.append(
                Stage(
                    "M1",
                    stage_title(name, filters),
                    PD,
                    kept=len(held),
                    rows_kept=sum(rows.pds[pk][3] for pk in held),
                    lines=(*lines, *more),
                    what=what,
                )
            )
            continue
        kept = [pk for pk, f in rows.pds.items() if f[0] > index]
        dropped = [pk for pk, f in rows.pds.items() if f[0] == index]
        what, more = _csv(f"step:{name}" if dropped else "", len(dropped), PDS)
        stage = Stage(
            name,
            stage_title(name, filters),
            PD,
            kept=len(kept),
            out=len(dropped),
            rows_kept=sum(rows.pds[pk][3] for pk in kept),
            rows_out=sum(rows.pds[pk][3] for pk in dropped),
            lines=more,
            what=what,
        )
        out.append(_broken(stage, rows))
    for index, name in rows.stage_of("rows"):
        if name == "rows":
            expected = sum(rows.pds[pk][3] for pk in rows.pds_kept)
            lines = []
            if len(rows.rows) != expected:
                lines.append(
                    Line(
                        f"The programme documents kept hold {expected:,} PD indicator rows: they are not "
                        f"these. {DIFFERS}",
                        alert=True,
                    )
                )
            what, more = _csv("step:M11" if rows.rows else "", len(rows.rows), SOURCE)
            out.append(
                Stage(
                    "M11",
                    stage_title(name, filters),
                    PD_ROW,
                    kept=len(rows.rows),
                    lines=(*lines, *more),
                    what=what,
                )
            )
            continue
        dropped = [r for r in rows.rows.values() if r[1] == index]
        before = {r[0] for r in rows.rows.values() if r[1] >= index}
        after = {r[0] for r in rows.rows.values() if r[1] > index}
        gone = {r[0] for r in dropped}
        what, more = _csv(f"step:{name}" if dropped else "", len(dropped), SOURCE)
        stage = Stage(
            name,
            stage_title(name, filters),
            PD_ROW,
            kept=sum(1 for r in rows.rows.values() if r[1] > index),
            out=len(dropped),
            lines=(
                Line(
                    f"{words(len(before - after), INDICATOR)} left out entirely, {len(gone & after):,} "
                    "partly (some of their rows kept)."
                ),
                *more,
            ),
            what=what,
        )
        out.append(_broken(stage, rows))
    for index, name in rows.stage_of("indicators"):
        if name == "one_per_indicator":
            n = len(rows.indicators)
            made = [rows.idents[r[0]] for r in rows.rows.values() if r[1] >= index]
            lines = [
                Line(
                    f"{words(len(made), PD_ROW)} are {words(n, INDICATOR)}: eTools lists an indicator once "
                    "per location and disaggregation."
                )
            ]
            if set(made) != set(rows.indicators):
                lines.append(Line(f"These are not the indicators of the rows kept. {DIFFERS}", alert=True))
            out.append(Stage("M16", stage_title(name, filters), INDICATOR, kept=n, lines=tuple(lines)))
            continue
        dropped = [i for i in rows.indicators.values() if i.stage == index]
        what, more = _csv(f"step:{name}" if dropped else "", len(dropped), SOURCE)
        out.append(
            Stage(
                name,
                stage_title(name, filters),
                INDICATOR,
                kept=sum(1 for i in rows.indicators.values() if i.stage > index),
                out=len(dropped),
                lines=more,
                what=what,
            )
        )
    kept = rows.kept_indicators
    what, lines = _csv("kept", len(kept), SOURCE)
    if "kept" in rows.broken:
        lines += (Line(f"These are not the indicators the steps keep. {DIFFERS}", alert=True),)
    out.append(Stage("K", "Kept: the page's indicators", INDICATOR, kept=len(kept), lines=lines, what=what))
    none = sorted(rows.pds_kept - {i.pd for i in kept})
    no_row = sum(1 for pk in none if not rows.pds[pk][3])
    what, more = _csv("step:K2" if none else "", len(none), PDS)
    out.append(
        Stage(
            "K2",
            "Programme documents kept with no indicator of this filter",
            PD,
            kept=len(none),
            lines=(
                Line(
                    f"No PD indicator row in NeuroDB: {no_row:,}; every indicator left out by a later step: "
                    f"{len(none) - no_row:,}. They are in no figure of programme documents (see check 10 of "
                    "the eTools reporting checks: running programme documents with no report row)."
                ),
                *more,
            ),
            what=what,
        )
    )
    return adds_up(out)


def pd_steps(facts: runfacts.RunFacts, rows: Chain) -> list[Any]:
    """D1 to D5 for the PD indicators, and the programme documents in NeuroDB by the sync that last wrote them
    (the eTools Datamart's programme documents and the eTools REST sync both write them; none is ever
    removed)."""
    out: list[Any] = trail.source_steps(facts, SOURCE, "PD indicator rows", rows.held, what="step:D4")
    what, more = _csv("step:D5" if rows.orphans else "", rows.orphans, SOURCE)
    out.append(
        trail.Step(
            "D5",
            "dataset",
            "PD indicator rows naming no programme document NeuroDB holds",
            (Line(f"{words(rows.orphans, PD_ROW)}: never read by Partner monitoring.", what=what), *more),
        )
    )
    out.append(trail.Step("PD", "dataset", "Programme documents in NeuroDB", (Line(pd_writers()),)))
    return out


def pd_writers() -> str:
    """The programme documents in NeuroDB by the sync that last wrote them, in one line. Each sync writes them
    twice: its ``interventions``, then straight after their budgets (the Datamart's
    ``intervention_budgets``) or details (the REST sync's ``intervention_details``), which save them again: a
    sync's window runs from the start of its ``interventions`` to the end of the first of those after it."""
    from neurodb.core.models import SyncRun

    held = PCA.objects.count()
    found, taken = [], set()
    for job, then, words_of in (
        (
            SyncRun.Job.ETOOLS_DATAMART,
            "intervention_budgets",
            "the last eTools Datamart sync of programme documents",
        ),
        (SyncRun.Job.ETOOLS, "intervention_details", "the last eTools REST sync of programme documents"),
    ):
        run = (
            SyncRun.objects.filter(
                job=job, target="interventions", status__in=(SyncRun.Status.SUCCEEDED, SyncRun.Status.PARTIAL)
            )
            .exclude(finished_at=None)
            .order_by("-started_at")
            .first()
        )
        if run is None:
            found.append(f"{words_of}: never ran")
            continue
        after = (
            SyncRun.objects.filter(job=job, target=then, started_at__gte=run.started_at)
            .exclude(finished_at=None)
            .order_by("started_at")
            .values_list("finished_at", flat=True)
            .first()
        )
        end = max(run.finished_at, after) if after is not None else run.finished_at
        window = PCA.objects.filter(updated_at__gte=run.started_at, updated_at__lte=end)
        mine = set(window.values_list("pk", flat=True)) - taken
        found.append(f"{words_of} ({trail._day(run.finished_at)}): {len(mine):,}")
        taken |= mine
    return (
        f"{words(held, PD)} in NeuroDB. Updated by {'; by '.join(found)}; earlier: {held - len(taken):,} "
        "(NeuroDB never removes a programme document). The status of each decides whether it is running."
    )


def _way_words(way: str) -> str:
    if way in identity.BREAKERS:
        return f"placed by a tie-breaker ({identity.LABELS[way]})"
    if way == "shared_output":
        return f"left out: {identity.WAY_WORDS[way]}, and no tie-breaker tells them apart"
    words_of = identity.WAY_WORDS.get(way, way)
    return words_of if way in identity.PLACED_BY_KEY else f"left out: {words_of}"


OUTCOME_WORDS = {
    "one_indicator": "left out: two indicator reports of this progress report carry this indicator's title",
    "other_place": "at a place the location filter leaves out",
    "no_value": "its report counts (Accepted or Submitted) but carries no value",
    "unexplained": "not explained by the Reached it read",
}


def outcome_words(outcome: str) -> str:
    if outcome.startswith("status:"):
        return f"its report does not count: {rc.STATUS_LABELS.get(outcome[7:], outcome[7:])}"
    if outcome.startswith("counted:"):
        return {
            "counted:prp": "counted: its report's value is PRP's own report total",
            "counted:report_total": "counted: a total repeated on every location, read once for the report",
        }.get(outcome, "counted")
    return OUTCOME_WORDS.get(outcome, outcome)


def reached_tests(read: str) -> dict[str, Callable[[tuple], bool]]:
    """Which partner report rows each step of the Reached fold names after the rows read (its CSV), a row
    as :attr:`Fold.placed` holds it (its pass, the indicator's number among those kept, the way, read, what
    became of it); ``read``: the report type read first. From R8 on, the rows of the indicators kept only."""
    return {
        "R5": lambda p: p[0] == read and p[1] == -2,
        "R6": lambda p: p[0] == read and p[1] == -1,
        "R4": lambda p: p[0] == read and p[1] >= 0,
        "R7": lambda p: p[0] != read,
        "R8": lambda p: p[1] >= 0 and p[4] == "one_indicator",
        "R9": lambda p: p[1] >= 0 and p[4] == "other_place",
        "R10": lambda p: p[1] >= 0 and p[4].startswith("status:"),
        "R11": lambda p: p[1] >= 0 and p[4] == "no_value",
        "R13": lambda p: p[1] >= 0 and p[4].startswith("counted:"),
    }


def reached_rows(fold: Fold, key: str) -> list[int]:
    """The partner report rows of a step of the Reached fold (its CSV): every row in NeuroDB (``all``), a
    class of them (R0 to R3), or those of :func:`reached_tests`."""
    if key == "all":
        return sorted(fold.rows)
    if key in ("R0", "R1", "R2", "R3"):
        return sorted(pk for pk, (kind, _unit) in fold.rows.items() if kind == key)
    test = reached_tests(fold.report_type).get(key)
    return sorted(pk for pk, p in fold.placed.items() if test(p)) if test else []


def _fold_stage(
    key: str,
    title: str,
    before: set[int],
    after: set[int],
    unit_of: dict[int, int],
    *,
    out: int | None = None,
    added: int | None = None,
    listed: int = 0,
    lines: tuple[Line, ...] = (),
) -> Stage:
    """A step of the Reached fold, from the report rows still read before it and after it: what it leaves out
    (``out``, the rows of its CSV; None for a step that only says what the step before it kept) or adds (the
    monthly pass), in report rows and in indicator reports, left out entirely or partly (some of their rows
    kept). ``listed``: the rows of its CSV, which a download holds or not."""
    irs_before = {unit_of[pk] for pk in before}
    irs_after = {unit_of[pk] for pk in after}
    what, more = _csv(f"reached:{key}" if listed else "", listed, REPORTS)
    stage = Stage(key, title, ROW, kept=len(after), irs_kept=len(irs_after), lines=(*lines, *more), what=what)
    if added is not None:
        return dataclasses.replace(stage, out=0, added=added, irs_added=len(irs_after - irs_before))
    if out is None:
        return stage
    gone = {unit_of[pk] for pk in before - after}
    return dataclasses.replace(
        stage, out=out, irs_out=len(irs_before - irs_after), irs_partly=len(gone & irs_after)
    )


def reached_steps(
    rows: Chain, filters: monitoring.Filters, totals: runfacts.RunFacts, totals_held: int
) -> list[Any]:
    """The Reached fold, R0 to R14 (SPEC_validation §3.4.4), after D1 to D4 of the partner reports: where
    every partner report row in NeuroDB went, from the rows Partner monitoring never reads to the reports
    that count, in report rows and indicator reports, then the indicators with Reached. One chain: each step
    keeps what the step before it kept, less what it leaves out (the monthly pass adds the rows it reads for
    the indicators kept), and from the rows read on, only the rows of the indicators kept go on. PRP's report
    totals come with D1 to D4 of their dataset (``totals``; ``totals_held``: its records, counted in the
    trail's snapshot)."""
    fold = rows.fold
    read = fold.report_type or filters.report_type
    kind = "quarterly" if read == "QPR" else "monthly"
    other = "monthly" if read == "QPR" else "quarterly"
    tests = reached_tests(read)
    unit_of = {pk: unit for pk, (_kind, unit) in fold.rows.items()}

    def of(key: str) -> set[int]:
        return {pk for pk, p in fold.placed.items() if tests[key](p)}

    out: list[Any] = []
    kept = set(fold.rows)
    titles = {
        "R0": "Partner report rows of a programme document NeuroDB does not hold, or naming none (never read "
        "by Partner monitoring)",
        "R1": "Rows of programme documents NeuroDB holds that this filter does not keep (left out by a "
        "programme document step, or with no indicator kept)",
        "R2": f"Rows of the other report type ({other} reports: {kind} reports are read)",
    }
    for key, title in titles.items():
        gone = {pk for pk, (k, _unit) in fold.rows.items() if k == key}
        out.append(_fold_stage(key, title, kept, kept - gone, unit_of, out=len(gone), listed=len(gone)))
        kept -= gone
    read_rows = {pk for pk, (k, _unit) in fold.rows.items() if k == "R3"}
    first = {pk for pk, p in fold.placed.items() if p[0] == read}
    lines: tuple[Line, ...] = ()
    if first != read_rows:
        alert = f"Partner monitoring placed {len(first):,} rows: they are not these. {DIFFERS}"
        lines = (Line(alert, alert=True),)
    title = f"Rows read for these programme documents, {kind} reports"
    out.append(_fold_stage("R3", title, kept, read_rows, unit_of, listed=len(read_rows), lines=lines))
    kept = read_rows
    pass_words = "Quarterly pass: " if read == "QPR" else ""
    # placed to an indicator this filter leaves out, then left out by why: what is left is placed to the
    # indicators kept (R4, last)
    elsewhere = of("R5")
    title = (
        f"{pass_words}placed to an indicator this filter leaves out (another section, a search, a status…)"
    )
    out.append(
        _fold_stage("R5", title, kept, kept - elsewhere, unit_of, out=len(elsewhere), listed=len(elsewhere))
    )
    kept -= elsewhere
    left = of("R6")
    reasons = Counter(fold.placed[pk][2] for pk in left)
    lines = tuple(Line(f"{_way_words(way)}: {words(n, ROW)}.") for way, n in sorted(reasons.items()))
    title = (
        f"{pass_words}left out, and why (check 7 of the eTools reporting checks counts them for the running "
        "programme documents)"
    )
    out.append(
        _fold_stage("R6", title, kept, kept - left, unit_of, out=len(left), listed=len(left), lines=lines)
    )
    kept -= left
    placed = of("R4")
    ways = Counter(fold.placed[pk][2] for pk in placed)
    breakers = ", ".join(f"{identity.LABELS[w].lower()} {ways[w]:,}" for w in identity.BREAKERS if ways[w])
    line = Line(
        f"By its eTools id {ways['id']:,}, by title {ways['title']:,}, by PD output {ways['output']:,}, by a "
        f"tie-breaker {sum(ways[w] for w in identity.BREAKERS):,}"
        + (f" ({breakers})" if breakers else "")
        + "."
    )
    title = f"{pass_words}placed to an indicator of this filter"
    out.append(_fold_stage("R4", title, kept, placed, unit_of, listed=len(placed), lines=(line,)))
    kept = placed
    second = of("R7")
    if second:
        mine = {pk for pk in second if fold.placed[pk][1] >= 0 and fold.placed[pk][3]}
        later = sum(1 for pk in second if fold.placed[pk][1] == -2 and fold.placed[pk][3])
        not_used = sum(1 for pk in second if not fold.placed[pk][3] and fold.placed[pk][1] != -1)
        reasons = Counter(fold.placed[pk][2] for pk in second if fold.placed[pk][1] == -1)
        found = (
            f"{words(len(second), ROW)} of the {other} reports left out above are read for the indicators "
            f"with no {kind} report: placed to one this filter keeps {len(mine):,}"
        )
        if later:
            found += f"; to one the tracking status then leaves out {later:,}"
        found += (
            f"; to an indicator that has {kind} reports, or that this filter leaves out (read, not used) "
            f"{not_used:,}."
        )
        lines = (
            Line(found),
            *(Line(f"{_way_words(way)}: {words(n, ROW)}.") for way, n in sorted(reasons.items())),
        )
        title = "Monthly pass (indicators with no quarterly report)"
        stage = _fold_stage(
            "R7", title, kept, kept | mine, unit_of, added=len(mine), listed=len(second), lines=lines
        )
        out.append(stage)
        kept |= mine
    statuses = (rc.SENT_BACK, rc.DUE, rc.OVERDUE, rc.NOT_YET_DUE)
    for key, title in (
        (
            "R8",
            "Left out because two indicator reports of one progress report carry the indicator's title, and "
            "its PD output does not say which is this one",
        ),
        (
            "R9",
            "Under the location filter: rows at other places (the chosen locations' cells only; PRP's report "
            "totals and cumulative are not read)",
        ),
        ("R10", "Reports that do not count, by status (only Accepted and Submitted reports count)"),
        ("R11", "Accepted or Submitted with no value"),
    ):
        if key == "R9" and not filters.locations:
            continue
        gone = of(key)
        lines = ()
        if key == "R10":
            irs = {s: {unit_of[pk] for pk in gone if fold.placed[pk][4] == f"status:{s}"} for s in statuses}
            lines = (
                Line(
                    "By status, in indicator reports: "
                    + "; ".join(f"{rc.STATUS_LABELS[s]} {len(irs[s]):,}" for s in statuses)
                    + "."
                ),
            )
        out.append(
            _fold_stage(key, title, kept, kept - gone, unit_of, out=len(gone), listed=len(gone), lines=lines)
        )
        kept -= gone
    counted = of("R13")
    prp = {unit_of[pk] for pk in counted if fold.placed[pk][4] == "counted:prp"}
    total = {unit_of[pk] for pk in counted if fold.placed[pk][4] == "counted:report_total"}
    # D1 to D4 of PRP's report totals: "PRP's report totals: what eTools said it had", …
    for step in trail.source_steps(
        totals, TOTALS, "PRP indicator reports", totals_held, what="reached:totals"
    ):
        title = f"PRP's report totals: {step.title[0].lower()}{step.title[1:]}"
        out.append(dataclasses.replace(step, key=f"R12-{step.key}", title=title))
    found, kept_totals = fold.totals
    lines = [
        Line(
            f"Found with a value for the rows these indicators read: {found:,}; kept {kept_totals:,}; not "
            f"kept (about another programme document or indicator) {found - kept_totals:,}. A report's value "
            f"is PRP's own total where its locations give none: {words(len(prp), IR)}."
        )
    ]
    if filters.locations:
        lines.append(Line("Under the location filter, they are not read."))
    out.append(
        trail.Step("R12", "dataset", "PRP's report totals (the PRP indicator reports dataset)", tuple(lines))
    )
    lines = [
        Line(
            f"Of them, read from PRP's report total: {words(len(prp), IR)}; from a total repeated on every "
            f"location, read once for the report: {len(total):,}."
        )
    ]
    unexplained = sum(1 for pk in kept if fold.placed[pk][4] == "unexplained")
    if unexplained:
        lines.append(Line(f"{words(unexplained, ROW)} are not explained. {DIFFERS}", alert=True))
    out.append(
        _fold_stage(
            "R13", "Reports that count", kept, counted, unit_of, listed=len(counted), lines=tuple(lines)
        )
    )
    values = [fold.values[i] for i in rows.kept if i in fold.values]
    partial = sum(1 for v in values if v[1])
    cumulative = sum(1 for v in values if v[2] == "prp_cumulative")
    out.append(
        Stage(
            "R14",
            "Indicators with Reached",
            INDICATOR,
            kept=sum(1 for v in values if v[0] is not None),
            lines=(
                Line(
                    f"Partial history (the programme document started before the first reporting period "
                    f"NeuroDB holds): {words(partial, INDICATOR)}; of them, Reached is PRP's own cumulative: "
                    f"{cumulative:,}."
                ),
            ),
        )
    )
    return adds_up(out)


def adds_up(steps: list[Any]) -> list[Any]:
    """SPEC_validation §3.8: each step of a chain keeps what the step before it kept, less what it leaves
    out (plus what the monthly pass adds), in its unit and in indicator reports; a step that does not says so
    in red. A step of another unit starts again; the dataset steps are not in the chain."""
    out: list[Any] = []
    before: Stage | None = None
    for step in steps:
        if isinstance(step, Stage):
            if before is not None and step.unit == before.unit and not _follows(before, step):
                if not any(line.alert for line in step.lines):
                    alert = Line(
                        f"This step's count is not the step before less what it leaves out. {DIFFERS}",
                        alert=True,
                    )
                    step = dataclasses.replace(step, lines=(*step.lines, alert))
            before = step
        out.append(step)
    return out


def _follows(before: Stage, step: Stage) -> bool:
    if step.kept != before.kept - (step.out or 0) + (step.added or 0):
        return False
    if step.irs_kept is None or before.irs_kept is None:
        return True
    return step.irs_kept == before.irs_kept - step.irs_out + step.irs_added


def _csv(what: str, n: int, source: str) -> tuple[str, tuple[Line, ...]]:
    """A step's CSV (``what``) when a download holds its ``n`` records; past that none, and the sentence that
    says so, with Raw eTools data of the source, whose filters make a download smaller (SPEC_validation
    §1.3)."""
    from . import raw

    if not what or n <= raw.csv_limit(source):
        return what, ()
    return "", (Line(raw.too_many(source, n), raw=source),)


def overdue_steps(rows: Chain, filters: monitoring.Filters, day: datetime.date) -> list[Stage]:
    """The Overdue reports' fold: O1 the rows of these programme documents and report type, O2 not
    submitted, O3 due before the day, then the progress reports they make."""
    kind = monitoring.REPORT_TYPES.get(filters.report_type, filters.report_type).lower()
    titles = (
        f"Rows of these programme documents, {kind}",
        "Not submitted (no submission date, and a status not Submitted, Accepted or Sent back)",
        f"Due before {trail._day(day)}",
    )
    out = []
    total = len(rows.overdue)
    for index, title in enumerate(titles):
        kept = sum(1 for s in rows.overdue.values() if s > index)
        dropped = sum(1 for s in rows.overdue.values() if s == index)
        if index == 0:
            what, more = _csv("overdue:O1" if total else "", total, REPORTS)
            out.append(Stage("O1", title, ROW, kept=total, lines=more, what=what))
            continue
        what, more = _csv(f"overdue:O{index + 1}" if dropped else "", dropped, REPORTS)
        out.append(Stage(f"O{index + 1}", title, ROW, kept=kept, out=dropped, lines=more, what=what))
    kept = sum(1 for s in rows.overdue.values() if s == 3)
    what, more = _csv("overdue:O4" if kept else "", kept, REPORTS)
    out.append(
        Stage(
            "O4",
            "Progress reports of these rows (one per programme document, type, number and period)",
            PR,
            kept=rows.overdue_reports,
            lines=(
                Line(
                    f"{words(kept, ROW)} make them. This is not Progress reports' Overdue tile (see Notes)."
                ),
                *more,
            ),
            what=what,
        )
    )
    return adds_up(out) + [_owed_stage(rows)]


def _owed_stage(rows: Chain) -> Stage:
    """O5: the Overdue reports still owed in PRP's own list of progress reports that the partner reports do
    not have, which the page adds to O4 once the link to their programme documents is proven, or why it does
    not (``owed.link_state``)."""
    link = owed.link_state()
    if link.shown:
        said = (
            f"Shown since {trail._day(link.since)}: {words(rows.owed_overdue, PR)} of these programme "
            "documents and report type that the partner reports do not have (counted once). Overdue reports: "
            f"{rows.overdue_reports + rows.owed_overdue:,}."
        )
    else:
        why = link.line or "NeuroDB has none of PRP's own progress reports."
        said = f"Not shown. {why} The figure counts the partner reports only."
    return Stage(
        "O5",
        "Overdue reports still owed in PRP's own list of progress reports",
        PR,
        kept=rows.owed_overdue,
        lines=(Line(said),),
    )


# ------------------------------------------------------------------------------------------------ the figures
def _counter(rows: Chain) -> Callable[[str], int]:
    kept = rows.kept_indicators
    return lambda test: sum(1 for i in kept if test in i.tests)


def _pds_with(rows: Chain, test: str = "") -> int:
    return len({i.pd for i in rows.kept_indicators if not test or test in i.tests})


REACHED = " (see Where Reached comes from)"


def tile_figures(summary: dict[str, Any], rows: Chain, *, home: bool = False) -> list[Figure]:
    """The tiles of Partner monitoring's grid (``_pm_tiles.html``), or Home's Indicator progress block
    (``home``: no Overdue reports, no No target)."""
    n = _counter(rows)
    kept = rows.kept_indicators
    out = [
        Figure(
            "pm.indicators",
            "Indicators",
            INDICATOR,
            summary["indicators"],
            len(kept),
            how="Every indicator kept: one per programme document and eTools id (else title)",
            select=(("set", "kept"),),
        ),
        Figure(
            "pm.with_target",
            "Indicators: with a target",
            INDICATOR,
            summary["with_target"],
            n("with_target"),
            how=TEST_WORDS["with_target"],
            select=(("test", "with_target"),),
        ),
    ]
    for key, label in monitoring.LABELS.items():
        if home and key == NO_TARGET:  # Home draws no No target
            continue
        out.append(
            Figure(
                f"pm.status.{key}",
                label,
                INDICATOR,
                summary["status_counts"][key],
                n(f"tracking.{key}"),
                how=TEST_WORDS[f"tracking.{key}"] + REACHED,
                select=(("test", f"tracking.{key}"),),
            )
        )
    out += [
        Figure(
            "pm.with_reached",
            "Programme documents: indicators with Reached",
            INDICATOR,
            summary["with_reached"],
            n("with_reached"),
            how=TEST_WORDS["with_reached"] + REACHED,
            select=(("test", "with_reached"),),
        ),
        Figure(
            "pm.pds",
            "Programme documents",
            PD,
            summary["programme_documents"],
            _pds_with(rows),
            how="Programme documents of these indicators (see Programme documents kept with no indicator)",
            select=(("set", "pds"),),
        ),
        Figure(
            "pm.partners",
            "Partners",
            PARTNER,
            summary["partners"],
            len({i.partner for i in kept} - {None}),
            how="The linked partners of their programme documents (a programme document with no linked "
            "partner adds none)",
            select=(("set", "partners"),),
        ),
    ]
    if not home:
        out.append(
            Figure(
                "pm.overdue_reports",
                "Overdue reports",
                PR,
                summary["overdue_reports"],
                rows.overdue_reports + rows.owed_overdue,
                how="Progress reports of these programme documents and report type with a row not submitted "
                "after its due date (see Overdue reports, step by step)",
                select=(("set", "overdue"),),
            )
        )
        for section, count in summary["by_section"]:
            out.append(
                Figure(
                    f"pm.section.{section}",
                    f"Indicators by section: {section}",
                    INDICATOR,
                    count,
                    sum(1 for i in kept if (i.section or monitoring.NO_SECTION) == section),
                    how="Indicators of this section (the PD indicator's own section)",
                    select=(("section", section),),
                    drawn=False,
                    note="the chart's data",
                )
            )
    return out


def quality_figures(quality: dict[str, Any], rows: Chain) -> tuple[list[Figure], list[Figure]]:
    """Data quality's tiles and its lists' counts (``data_quality``), each rebuilt from the indicators
    kept."""
    n = _counter(rows)
    kept = rows.kept_indicators
    tiles = [
        Figure(
            "dq.pds",
            "Programme documents",
            PD,
            quality["programme_documents"],
            _pds_with(rows),
            how="Programme documents of these indicators",
            select=(("set", "pds"),),
        ),
        Figure(
            "dq.running",
            "Programme documents: running",
            PD,
            quality["running_pds"],
            _pds_with(rows, "running"),
            how=TEST_WORDS["running"],
            select=(("pd_test", "running"),),
        ),
        Figure(
            "dq.active",
            "Active PDs",
            PD,
            quality["active_pds"],
            _pds_with(rows, "active"),
            how=TEST_WORDS["active"],
            select=(("pd_test", "active"),),
        ),
        Figure(
            "dq.ended",
            "Ended PDs",
            PD,
            quality["ended_pds"],
            _pds_with(rows, "ended"),
            how=TEST_WORDS["ended"],
            select=(("pd_test", "ended"),),
        ),
        Figure(
            "dq.indicators",
            "Indicators",
            INDICATOR,
            quality["indicators"],
            len(kept),
            how="Every indicator kept",
            select=(("set", "kept"),),
        ),
        Figure(
            "dq.with_reached",
            "With Reached",
            INDICATOR,
            quality["with_reached"],
            len(kept) - n("dq_without"),
            how="With Reached (in the year chosen, else to date)" + REACHED,
            select=(("not_test", "dq_without"),),
        ),
        Figure(
            "dq.zero_reached",
            "With Reached: reported zero",
            INDICATOR,
            quality["zero_reached"],
            n("dq_zero"),
            how=TEST_WORDS["dq_zero"] + REACHED,
            select=(("test", "dq_zero"),),
        ),
        Figure(
            "dq.without_reached",
            "Without Reached",
            INDICATOR,
            quality["without_reached"],
            n("dq_without"),
            how=TEST_WORDS["dq_without"] + REACHED,
            select=(("test", "dq_without"),),
        ),
        Figure(
            "dq.with_target",
            "With a target",
            INDICATOR,
            quality["with_target"],
            n("with_target"),
            how=TEST_WORDS["with_target"],
            select=(("test", "with_target"),),
        ),
        Figure(
            "dq.without_target",
            "Without a target",
            INDICATOR,
            quality["without_target"],
            n("missing_target"),
            how=TEST_WORDS["missing_target"],
            select=(("test", "missing_target"),),
        ),
    ]
    groups: dict[tuple[int, str], list[Kept]] = defaultdict(list)
    for i in kept:
        groups[(i.pd, rc.norm(i.title))].append(i)
    shared = {key: group for key, group in groups.items() if len(group) > 1}
    fold = rows.fold

    def left_out(group: list[Kept]) -> int:
        return sum(1 for i in group if not i.rows_placed and i.title_rows_left_out)

    def unmatched(key: tuple[int, str]) -> int:
        return fold.shared.get((*key, "output_unmatched"), 0)

    drawn = bool(quality["same_title"])
    lists = [
        Figure(
            "dq.list.missing_target",
            "Indicators with missing targets",
            INDICATOR,
            quality["without_target"],
            n("missing_target"),
            how=TEST_WORDS["missing_target"],
            select=(("test", "missing_target"),),
        ),
        Figure(
            "dq.list.no_reached",
            "Indicators without Reached or with zero",
            INDICATOR,
            quality["no_reached_count"],
            n("no_reached"),
            how=TEST_WORDS["no_reached"] + REACHED,
            select=(("test", "no_reached"),),
        ),
        Figure(
            "dq.list.differs",
            "NeuroDB's Reached differs from PRP's cumulative",
            INDICATOR,
            quality["mismatch_count"],
            n("differs"),
            how=TEST_WORDS["differs"] + REACHED,
            select=(("test", "differs"),),
        ),
        Figure(
            "dq.list.same_title",
            "Same title twice in one PD",
            TITLE,
            quality["same_title_count"],
            len(shared),
            how="Titles two or more indicators of a programme document share",
            select=(("set", "same_title"),),
        ),
        Figure(
            "dq.list.partial",
            "Partial history",
            PD,
            quality["partial_count"],
            _pds_with(rows, "partial"),
            how=TEST_WORDS["partial"],
            select=(("pd_test", "partial"),),
        ),
        Figure(
            "dq.same_title_left_out",
            "Same title: indicators that get none of their rows",
            INDICATOR,
            quality["same_title_left_out"],
            sum(left_out(g) for g in shared.values()),
            how="Of these titles, the indicators no row was placed to while rows of their title were left "
            "out (R6, both passes)",
            select=(("set", "same_title_left_out"),),
            drawn=drawn,
            note="" if drawn else "not drawn: no title twice",
        ),
        Figure(
            "dq.same_title_rows_unmatched",
            "Same title: rows whose PD output matches none",
            ROW,
            quality["same_title_rows_unmatched"],
            sum(unmatched(key) for key in shared),
            how="Rows of these titles left out: a PD output none of the indicators has (R6, both passes)",
            drawn=drawn and bool(quality["same_title_rows_unmatched"]),
            note="" if drawn and quality["same_title_rows_unmatched"] else "not drawn: none",
        ),
    ]
    for g in quality["same_title"]:  # the titles the list shows (at most monitoring.QUALITY_LIMIT)
        key = (g["pd"].id, rc.norm(g["title"]))
        mine = shared.get(key, [])
        name = f"{g['pd'].number or '—'}: {title_words(g['title'])}"
        lists += [
            Figure(
                f"dq.same_title.{key[0]}.{key[1]}.count",
                f"{name} · Indicators",
                INDICATOR,
                g["count"],
                len(mine),
                how="The indicators of this programme document with this title",
                select=(("group", key),),
                listed=True,
            ),
            Figure(
                f"dq.same_title.{key[0]}.{key[1]}.left_out",
                f"{name} · Left out",
                INDICATOR,
                g["left_out"],
                left_out(mine),
                how="Of them, those that get none of their rows",
                listed=True,
            ),
        ]
    return tiles, lists


def title_words(title: str) -> str:
    """An indicator's title as the trail names a figure with it: an e-mail address or a phone number copied
    into it withheld. A name stays, as on the page (the CSVs withhold it: they clean the step's words with the
    names NeuroDB knows, read for each download, never in the trail's build)."""
    from . import raw_people

    return raw_people.text(title, SOURCE, "title", frozenset(), frozenset())


def filter_words(filters: monitoring.Filters) -> tuple[str, ...]:
    """The filter in words, one entry per field set: the programme documents read and the year always."""
    from .comparison import _default, _partner_names, _words

    labels = {
        "sections": "Section",
        "partners": "Partner",
        "pds": "Programme document",
        "pd_ids": "Programme document (its page)",
        "locations": "Location",
        "report_type": "Report type",
        "status": "Tracking status",
        "q": "Indicator or output",
        "tags": "Tags",
        "document_types": "Document type",
        "offices": "Office",
        "partner_types": "Partner type",
        "emergency": "Emergency",
        "cp_output": "CP output",
    }
    partners = _partner_names(filters.partners)
    out = [
        f"Programme documents: {_words(PAGE, 'scope', filters.scope, filters, partners)}"
        if filters.scope != "active" or filters.pd_statuses is not None
        else "Programme documents: running (active, signed or suspended)",
    ]
    if filters.pd_statuses:
        out.append(f"PD status: {_words(PAGE, 'pd_statuses', filters.pd_statuses, filters, partners)}")
    out.append(f"Reporting year: {filters.year}" if filters.year else "Every reporting year")
    for item in dataclasses.fields(filters):
        if item.name in ("scope", "pd_statuses", "year"):
            continue
        value = getattr(filters, item.name)
        if value == _default(item) or (isinstance(value, dict) and not any(value.values())):
            continue
        out.append(f"{labels[item.name]}: {_words(PAGE, item.name, value, filters, partners)}")
    return tuple(out)


def moment() -> datetime.datetime | None:
    """The moment of the eTools figures Partner monitoring reads: the end of its sources' latest sync."""
    from .comparison import moment as of

    return of(PAGE)


# --------------------------------------------------------------------------------------------- the builds
def _recorded(
    filters: monitoring.Filters, day: datetime.date
) -> tuple[list, trail.Recorder, list, Any, bool]:
    """The page's indicators built once more, recorded (``steps``, ``record``), and the page's own: the stored
    ones (warm), else this build's (cold), which the caller stores for the page once its snapshot closes."""
    key = monitoring._rows_key(filters, day, keys.fingerprint())
    warm, page = (False, None) if settings.DEBUG else compute.peek(key, lang_neutral=True)
    steps: list = []
    recorder = trail.Recorder()
    found = monitoring.indicators(filters, day, steps=steps, record=recorder)
    if not warm:
        page = monitoring.as_rows(found)
    return steps, recorder, found, page, warm


def _store_page(filters: monitoring.Filters, day: datetime.date, page: list) -> None:
    """The page's indicators, computed by the trail's build, stored under the page's own key
    (``stored_indicators``): the page then reads them, computed once (after the trail's snapshot: nothing is
    stored inside a transaction)."""
    if settings.DEBUG:
        return
    key = monitoring._rows_key(filters, day, keys.fingerprint())
    compute.once(key, lambda: page, monitoring.STORED_SECONDS, lang_neutral=True)


def monitoring_trail(params: Any, day: datetime.date, *, home: bool = False) -> MonitoringTrail:
    """The trail of Partner monitoring's grid and Data quality (or of Home's Indicator progress block,
    ``home``) for the filter of ``params`` on ``day``: computed once in one snapshot of the data, kept two
    minutes for every worker."""
    params = trail.filter_query(params)
    filters = filters_of(params)
    key = f"trail-pm:v{CACHE_VERSION}:{int(home)}:{filters.cache_key()}:{day}:{keys.fingerprint()}"
    return trail._stored(key, lambda: _monitoring_trail(params, filters, day, home))


def _monitoring_trail(
    params: Any, filters: monitoring.Filters, day: datetime.date, home: bool
) -> MonitoringTrail:
    from .raw import STREAM_TIMEOUT_MS, snapshot

    with snapshot(STREAM_TIMEOUT_MS):
        steps, recorder, found, page, warm = _recorded(filters, day)
        rows = chain(filters, day, steps, recorder, found)
        summary = monitoring.summary(page, filters, day)
        quality = monitoring.data_quality(page, filters, day)
        dataset = pd_steps(runfacts.facts(SOURCE), rows)
        reached = [
            *trail.source_steps(
                runfacts.facts(REPORTS),
                REPORTS,
                "partner report rows",
                len(rows.fold.rows),
                what="reached:all",
            ),
            *reached_steps(
                rows,
                filters,
                runfacts.facts(TOTALS),
                dm.DatamartDocument.objects.filter(dataset=TOTALS).count(),
            ),
        ]
        overdue = overdue_steps(rows, filters, day)
        tiles = tile_figures(summary, rows, home=home)
        if home:
            blocks = [Block("home", "Indicator progress: tiles", "home", tuple(tiles))]
        else:
            listed = Figure(
                "pm.listed",
                "Indicators: the grid's rows",
                INDICATOR,
                min(len(page), monitoring.MAX_INDICATORS),
                min(len(rows.kept), monitoring.MAX_INDICATORS),
                how=f"Listed: the first {monitoring.MAX_INDICATORS:,} in the page's order; the count is cut",
                select=(("set", "kept"),),
            )
            no_partner = sum(1 for pk in {i.pd for i in rows.kept_indicators} if not rows.pds[pk][2])
            quality_tiles, quality_lists = quality_figures(quality, rows)
            blocks = [
                Block(
                    "grid",
                    "Grid: tiles",
                    "grid",
                    tuple(tiles),
                    notes=(
                        Line(
                            "Programme documents with no linked partner add none to Partners: "
                            f"{words(no_partner, PD)}.",
                            what="no_partner" if no_partner else "",
                        ),
                    ),
                ),
                Block("list", "Grid: the list", "grid", (listed,)),
                Block("quality", "Data quality: tiles", "quality", tuple(quality_tiles)),
                Block("quality_lists", "Data quality: the lists", "quality", tuple(quality_lists)),
            ]
        as_of = moment()
    if not warm:
        _store_page(filters, day, page)
    return MonitoringTrail(
        query=params.urlencode(),
        day=day,
        as_of=as_of,
        home=home,
        steps=(*dataset, *chain_steps(rows, filters)),
        reached=tuple(reached),
        overdue=tuple(overdue),
        blocks=tuple(blocks),
        rows=rows,
        summary=summary,
        filter_words=filter_words(filters),
    )


def performance_trail(params: Any, day: datetime.date) -> TableTrail:
    """The trail of Program Performance: the trail of the filter without its sections (Reached computed for
    every section, then the tracking status; with no section chosen, the grid's trail of the same filter),
    then the sections chosen kept, by the page's own function (``program_performance_rows``); kept two
    minutes."""
    params = trail.filter_query(params)
    filters = filters_of(params)
    key = f"trail-pp:v{CACHE_VERSION}:{filters.cache_key()}:{day}:{keys.fingerprint()}"
    return trail._stored(key, lambda: _performance_trail(params, filters, day))


def _performance_trail(params: Any, filters: monitoring.Filters, day: datetime.date) -> TableTrail:
    from .raw import STREAM_TIMEOUT_MS, snapshot

    every_filter = dataclasses.replace(filters, sections=[])
    if not filters.sections:
        # the filter is the grid's and Data quality's trail's (no section to take out): its one recorded
        # build, and the page's indicators it stored for the page when they were not
        rows = monitoring_trail(params, day).rows
        every = monitoring.stored_indicators(every_filter, day)
        page = monitoring.program_performance_rows(filters, day, every=every)
        tiles = monitoring.section_counts(every)
        chosen = {(i.pd, i.key) for i in rows.kept_indicators}
    else:
        with snapshot(STREAM_TIMEOUT_MS):
            steps, recorder, found, every, warm = _recorded(every_filter, day)
            rows = chain(every_filter, day, steps, recorder, found)
            page = monitoring.program_performance_rows(filters, day, every=every)
            tiles = monitoring.section_counts(every)
            # the sections step, after the tracking status: the page's own rows of the sections chosen
            recorded = monitoring.program_performance_rows(filters, day, every=monitoring.as_rows(found))
        if not warm:
            _store_page(every_filter, day, every)
        chosen = {(r.pd.id, r.key) for r in recorded}
    kept = [i for i in rows.kept_indicators if (i.pd, i.key) in chosen]
    sections = Counter(i.section or monitoring.NO_SECTION for i in rows.kept_indicators)
    # the steps of the filter without its sections, then the sections chosen, after the tracking status
    stages = [s for s in chain_steps(rows, every_filter) if s.key not in ("K", "K2")]
    if filters.sections:
        what, more = _csv(
            "step:sections" if len(rows.kept) > len(kept) else "", len(rows.kept) - len(kept), SOURCE
        )
        stages.append(
            Stage(
                "sections",
                stage_title("sections", filters) + ", after the tracking status (as the page reads them)",
                INDICATOR,
                kept=len(kept),
                out=len(rows.kept) - len(kept),
                lines=more,
                what=what,
            )
        )
    what, more = _csv("kept", len(kept), SOURCE)
    stages.append(
        Stage("K", "Kept: Program Performance's indicators", INDICATOR, kept=len(kept), lines=more, what=what)
    )
    stages = adds_up(stages)
    out = [
        Figure(
            "pp.indicators",
            "Program Performance: indicators (the sum of the section tiles chosen)",
            INDICATOR,
            len(page),
            len(kept),
            how="Every indicator of the filter, not cut at 2,000: the comparison sheet's",
            select=(("set", "kept"),),
            drawn=False,
            note="the comparison sheet's figure",
        ),
        Figure(
            "pp.every_section",
            "Every section: sections",
            SECTION,
            len(tiles),
            len(sections),
            how="The sections the other filters keep, chosen or not",
        ),
    ]
    for name, count in sorted(tiles.items()):
        out.append(
            Figure(
                f"pp.section_tile.{name}",
                f"Section tile: {name}",
                INDICATOR,
                count,
                sections.get(name, 0),
                how="Indicators of this section, whatever the sections chosen",
                select=(("section", name),),
            )
        )
    out.append(
        Figure(
            "pp.rows",
            "Program Performance: the table's rows",
            INDICATOR,
            min(len(page), monitoring.MAX_INDICATORS),
            min(len(kept), monitoring.MAX_INDICATORS),
            how=f"Listed: the first {monitoring.MAX_INDICATORS:,} in the page's order; the count is cut",
            select=(("set", "kept"),),
        )
    )
    by_key = {(i.pd, i.key): i for i in kept}
    numbers = {ident: n for n, ident in enumerate(rows.kept)}
    for r in page:
        mine = by_key.get((r.pd.id, r.key))
        value = r.reached_in_year if filters.year else r.reached
        rebuilt = None
        if mine is not None:
            rebuilt = mine.reached_in_year if filters.year else mine.reached
        out.append(
            Figure(
                f"pp.reached.{r.pd.id}.{r.key}",
                f"{r.pd.number or '—'}: {title_words(r.title)}",
                INDICATOR,
                value,
                rebuilt,
                how=("Reached in the year" if filters.year else "Reached to date")
                + ": its Reached fold, the rows placed to it, used or not, and why",
                select=(("indicator", numbers.get(r.ident, -1)),) if r.ident in numbers else (),
                listed=True,
            )
        )
    return TableTrail(
        tab="performance",
        query=params.urlencode(),
        day=day,
        steps=tuple(stages),
        block=Block("performance", "Program Performance", "table", tuple(out)),
        rows=rows,
        kept=tuple(i for i in rows.kept if (i[0], rows.indicators[i].key) in chosen),
    )


def map_trail(params: Any, day: datetime.date) -> TableTrail:
    """The trail of the map: its tiles and its totals line read from the map's own stored places
    (``stored_map_points``), each rebuilt from the trail's indicators or from the places themselves; kept two
    minutes."""
    params = trail.filter_query(params)
    params["view"] = "map"
    filters = filters_of(params)
    key = f"trail-map:v{CACHE_VERSION}:{filters.cache_key()}:{day}:{keys.fingerprint()}"
    return trail._stored(key, lambda: _map_trail(params, filters, day))


def _map_trail(params: Any, filters: monitoring.Filters, day: datetime.date) -> TableTrail:
    base = monitoring_trail(params, day)  # the indicators of the same filter, stored (and the page's)
    rows = base.rows
    # the map's places as the page reads them: stored for the map too when they were not
    points = monitoring.stored_map_points(filters, day)
    totals = points["totals"]
    n = _counter(rows)
    kept = rows.kept_indicators
    out = [
        Figure(
            "map.locations",
            "Locations",
            PLACE,
            totals["locations"],
            len(points["points"]) + len(points["unlocated"]),
            how="Every location planned or reported for these indicators (the map's places)",
            select=(("set", "map"),),
        ),
        Figure(
            "map.indicators",
            "Indicators",
            INDICATOR,
            totals["indicators"],
            len(kept),
            how="Every indicator kept",
            select=(("set", "kept"),),
        ),
        Figure(
            "map.pds",
            "Programme documents",
            PD,
            totals["programme_documents"],
            _pds_with(rows),
            how="Programme documents of these indicators",
            select=(("set", "pds"),),
        ),
        Figure(
            "map.partners",
            "Partners",
            PARTNER,
            totals["partners"],
            len({i.partner for i in kept} - {None}),
            how="The linked partners of their programme documents",
            select=(("set", "partners"),),
        ),
    ]
    for key in ("on_track", "off_track", "not_reported"):
        out.append(
            Figure(
                f"map.status.{key}",
                monitoring.LABELS[key],
                INDICATOR,
                totals["status_counts"].get(key, 0),
                n(f"tracking.{key}"),
                how=TEST_WORDS[f"tracking.{key}"] + REACHED,
                select=(("test", f"tracking.{key}"),),
            )
        )
    out += [
        Figure(
            "map.located",
            "On the map",
            PLACE,
            totals["located"],
            len(points["points"]),
            how="Places with coordinates, their own or a parent area's",
            select=(("set", "map"),),
        ),
        Figure(
            "map.approximate",
            "On the map: approximate",
            PLACE,
            totals["approximate"],
            sum(1 for p in points["points"] if p["approximate"]),
            how="Placed at the nearest parent area with coordinates",
            select=(("set", "map"),),
        ),
        Figure(
            "map.unlocated",
            "Without coordinates",
            PLACE,
            len(points["unlocated"]),
            totals["locations"] - totals["located"],
            how="Neither the location nor a parent area has coordinates: listed, not drawn",
            select=(("set", "map"),),
        ),
    ]
    lines = []
    if filters.locations:
        lines.append(
            Line("Under the location filter, the tiles read each indicator at the chosen locations only.")
        )
    return TableTrail(
        tab="map",
        query=params.urlencode(),
        day=day,
        steps=(),
        block=Block("map", "Map", "map", tuple(out)),
        rows=rows,
        lines=tuple(lines),
    )


__all__ = [
    "Chain",
    "Kept",
    "MonitoringTrail",
    "Stage",
    "TableTrail",
    "chain",
    "map_trail",
    "monitoring_trail",
    "performance_trail",
]
