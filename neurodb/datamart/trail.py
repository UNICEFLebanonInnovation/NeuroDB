"""The figure trail (SPEC_validation §3): for each figure of Progress reports and of Home's Progress reports
block, where its rows come from, step by step, from what eTools said it had to what the page shows, each step
with its count and the rows it leaves out (as CSV, :mod:`neurodb.datamart.trail_rows`), for the people who
check the figures (``roles.can_validate``). Partner monitoring's, its map's and Home's Indicator progress
block's are :mod:`neurodb.datamart.trail_monitoring`, built from the pieces here.

* **The page's own functions, never a copy.** The steps are the filters of ``ReportFilters.queryset``, in its
  order (its ``steps`` list); a row's progress report and indicator report are the keys ``progress_reports``
  groups with (``reporting.unit_key``); each report's status, sections and partner are ``progress_reports``,
  ``with_pd_details`` and ``partner_key``'s own; the PD status filter's lines are ``left_out``'s; By location
  and By disaggregation are recorded from inside ``location_rows`` and ``disaggregation_rows`` (their
  ``record``). *On the page* is the page's own call (``services.partner_reporting``, Home's
  ``reporting_figures`` and ``home.follow_up_rows``, the stored tables of the two tabs); *rebuilt* counts the
  same figure again from the rows the steps keep (:class:`TrailRows`). A figure whose two values differ is
  listed in red at the top of the page: never hidden.
* **One state of the data.** A trail is read in one ``REPEATABLE READ READ ONLY`` snapshot and kept two
  minutes for every worker (``compute.once``), keyed on the page, the filter, the day the statuses are read
  for and the moment of the eTools figures. What is kept is counts, keys and statuses (:class:`TrailRows`:
  each row's id, its progress report and indicator report, the step that left it out), never a record's
  payload: a CSV reads it to choose its rows, and writes them through the people rule.
* **The hooks** (``steps``, ``record``) are None on the pages: their code path, SQL and figures are the same
  (tests pin it).
"""

from __future__ import annotations

import dataclasses
import datetime
import hashlib
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from django.conf import settings
from django.db.models import Case, CharField, Value, When
from django.utils import timezone

from neurodb.core import compute
from neurodb.partnerships import statuses as pd_statuses

from . import keys, reporting, runfacts
from . import reached as rc

# the pages with a trail, by address
PAGES = {"progress-reports": "Progress reports", "partner-monitoring": "Partner monitoring", "home": "Home"}
HOME_BLOCKS = {"reporting": "Progress reports", "indicators": "Indicator progress"}  # Home's, with a trail
TABLES = ("locations", "disaggregations")  # Progress reports' tabs with a trail of their own
STORED_SECONDS = 120
CACHE_VERSION = 1
SOURCE = "partner_reports"  # the Datamart dataset the Progress reports figures are counted from
# figures whose key holds a partner, a report, a PD or a title
NAMED_KEYS = ("pr.partner.", "pr.report.", "home.pd.", "dq.same_title.", "pm.section.", "pp.section_tile.")
NOT_THE_FILTER = (
    "page",
    "sort",
    "export",
    "tab",
    "_part",
    "_eager",
    "as_of",
    "day",
    "figure",
    "block",
    "what",
    "of",
)

# the units a figure counts, in words (one, many)
IR, PR, PARTNER, PD, LINE, BLOCK, ROW = (
    "indicator_report",
    "progress_report",
    "partner",
    "pd",
    "line",
    "block",
    "row",
)
INDICATOR, PD_ROW, PLACE, SECTION, TITLE = "indicator", "pd_row", "place", "section", "title"
UNIT_WORDS = {
    IR: ("indicator report", "indicator reports"),
    PR: ("progress report", "progress reports"),
    PARTNER: ("partner", "partners"),
    PD: ("programme document", "programme documents"),
    LINE: ("line", "lines"),
    BLOCK: ("location block", "location blocks"),
    ROW: ("report row", "report rows"),
    INDICATOR: ("indicator", "indicators"),
    PD_ROW: ("PD indicator row", "PD indicator rows"),
    PLACE: ("location", "locations"),
    SECTION: ("section", "sections"),
    TITLE: ("title", "titles"),
}
# the filters of ReportFilters (and By disaggregation's), as the trail's header names them
FILTER_LABELS = {
    "q": "Search",
    "statuses": "Report status",
    "report_types": "Report type",
    "year": "Reporting year",
    "sections": "Section",
    "report_numbers": "Report #",
    "pds": "Programme document",
    "partners": "Partner",
    "partner_types": "Partner type",
    "pd_statuses": "PD status",
    "emergency": "Emergency indicator",
    "start_from": "Period start from",
    "start_to": "Period start to",
    "end_from": "Period end from",
    "end_to": "Period end to",
    "overdue": "Overdue only",
    "indicator": "By disaggregation: indicator",
    "location": "By disaggregation: location",
    "p_code": "By disaggregation: P-code",
    "disaggregation": "By disaggregation: disaggregation",
}
NOTES = (
    "Progress reports reads the report row's section; Partner monitoring the PD indicator's.",
    "Overdue on Progress reports is a report status, read from all the report's rows; Partner "
    "monitoring's Overdue reports counts progress reports with any row not submitted after its due date, "
    "quarterly reports only by default.",
    "Reached is read from Accepted and Submitted reports only.",
    "Under a location filter, the table cells read the chosen locations only; the indicator pop-up reads "
    "every location.",
)
DIFFERS = "This trail does not equal the page: tell the NeuroDB team."


def words(n: int | None, unit: str) -> str:
    """``n`` in its unit: "1 indicator report", "2,345 progress reports"."""
    one, many = UNIT_WORDS[unit]
    if n is None:
        return f"— {many}"
    return f"{n:,} {one if n == 1 else many}"


def anchor(key: str) -> str:
    """The id of a figure's row on the trail page (a figure's key can hold any character)."""
    return "figure-" + hashlib.blake2b(key.encode(), digest_size=6).hexdigest()


def filter_query(params: Any):
    """The page's filter from an address: every key but the page shown, its sort, the export, the tab, the day
    and the trail's own keys."""
    from django.http import QueryDict

    out = QueryDict(mutable=True)
    lists = (
        params.lists()
        if hasattr(params, "lists")
        else ((k, v if isinstance(v, list | tuple) else [v]) for k, v in params.items())
    )
    for key, values in lists:
        if key not in NOT_THE_FILTER:
            out.setlist(key, [str(v) for v in values])
    return out


# ------------------------------------------------------------------------------------------------- the values
@dataclass(frozen=True)
class Line:
    """One line of a step: its words, and the CSV of its rows (``what``, :mod:`trail_rows`), or none when
    NeuroDB keeps no record of them (counts only); ``raw``: the source on Raw eTools data, for rows more than
    a download holds."""

    words: str
    what: str = ""
    alert: bool = False
    raw: str = ""


@dataclass(frozen=True)
class Step:
    """One step of the chain: ``kind`` ``dataset`` (what the sync received), ``filter`` (a filter of the page,
    in its order), ``kept`` or ``status``; ``kept`` its rows and units after it, ``out`` the rows it leaves
    out and the units it leaves out entirely, ``partly`` the indicator reports and progress reports it leaves
    out only partly (some of their rows kept)."""

    key: str
    kind: str
    title: str
    lines: tuple[Line, ...] = ()
    kept: reporting.Units | None = None
    out: reporting.Units | None = None
    partly: tuple[int, int] = (0, 0)
    what: str = ""
    counts_only: bool = False  # NeuroDB keeps no record of these rows: no CSV

    @property
    def kept_words(self) -> str:
        """What is kept after the step: "2,545 report rows, 820 indicator reports in 210 progress reports"."""
        if self.kept is None:
            return ""
        k = self.kept
        return f"{words(k.rows, ROW)}, {words(k.indicator_reports, IR)} in {words(k.progress_reports, PR)}"

    @property
    def out_words(self) -> str:
        """What the step leaves out, entirely and partly (some of their rows kept)."""
        if self.out is None:
            return ""
        o, (ir, pr) = self.out, self.partly
        if not o.rows:
            return "Nothing left out."
        found = f"{words(o.rows, ROW)}; {words(o.indicator_reports, IR)} left out entirely"
        if ir:
            found += f", {ir:,} partly (some of their rows kept)"
        found += f"; {words(o.progress_reports, PR)} left out entirely"
        if pr:
            found += f", {pr:,} partly"
        return found + "."


@dataclass(frozen=True)
class Figure:
    """One figure the page draws: ``key`` (the page's ``data-figure``, the comparison sheet's), ``label`` as
    the page names it, ``unit``, its value on the page and rebuilt from the rows (``select``: the rows it
    counts, :func:`selected`), ``how`` it is counted in one line. ``drawn``: a number of the page's HTML
    (False: the chart's data); ``listed``: a cell of a table drawn a page at a time."""

    key: str
    label: str
    unit: str
    page: int | None
    rebuilt: int | None = None
    how: str = ""
    select: tuple[tuple[str, Any], ...] = ()
    drawn: bool = True
    listed: bool = False
    note: str = ""  # why it is not drawn, in words

    @property
    def same(self) -> bool:
        return self.page == self.rebuilt

    @property
    def anchor(self) -> str:
        return anchor(self.key)

    @property
    def what(self) -> str:
        return f"figure:{self.key}" if self.select else ""


@dataclass(frozen=True)
class Block:
    """The figures of one block of the page (``tab``: the page's tab it is drawn on), laid out as the page."""

    key: str
    title: str
    tab: str
    figures: tuple[Figure, ...]
    notes: tuple[Line, ...] = ()


@dataclass(frozen=True)
class Report:
    """What the trail reads of one progress report the filter keeps (``progress_reports`` and
    ``with_pd_details``): its status and whether the status filter keeps it, the month its period ends in, its
    partner (``partner_key``) and whether that partner has reported, its programme document
    (``pd_follow_up_key``), whether it was submitted late, each indicator report's section and the section the
    report is counted under (``reporting.report_section``), the status word and the newest record carrying
    it, and its rows the filter leaves out."""

    status: str
    chosen: bool
    month: str
    partner: Any
    reported: bool
    pd: str
    late: bool
    sections: dict[str, str]
    word: str
    word_from: Any
    rows_outside: int
    section: str = ""


@dataclass(frozen=True)
class TrailRows:
    """The chain computed once (SPEC_validation §3.6): ``stages`` the filters in their order ("start" first),
    ``rows`` every report row in NeuroDB by pk: (Datamart id, progress report, indicator report, the index of
    the stage that left it out, or ``len(stages)`` when kept, why the PD status filter left it out, period
    start), ``reports`` the progress reports the filter keeps. Never a record's payload."""

    stages: tuple[str, ...]
    rows: dict[int, tuple[Any, str, str, int, str, Any]]
    reports: dict[str, Report]
    history: datetime.date | None = None

    @property
    def kept_at(self) -> int:
        return len(self.stages)


@dataclass(frozen=True)
class Trail:
    """The trail of a page (or of Home's block) for a filter and a day, as kept for two minutes."""

    page: str
    block: str
    query: str
    day: datetime.date
    as_of: datetime.datetime | None
    steps: tuple[Step, ...]
    blocks: tuple[Block, ...]
    rows: TrailRows
    counts: dict[str, Any]
    left_out: dict[str, int]
    filter_words: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def figures(self) -> list[Figure]:
        return [f for b in self.blocks for f in b.figures]

    @property
    def differences(self) -> list[Figure]:
        return [f for f in self.figures if not f.same]

    @property
    def dataset_steps(self) -> list[Step]:
        return [s for s in self.steps if s.kind == "dataset"]

    @property
    def filter_steps(self) -> list[Step]:
        return [s for s in self.steps if s.kind == "filter"]

    @property
    def last_steps(self) -> list[Step]:
        return [s for s in self.steps if s.kind in ("kept", "status")]

    def figure(self, key: str) -> Figure | None:
        return next((f for f in self.figures if f.key == key), None)

    def block_of(self, key: str) -> str:
        """The block a figure is drawn in ("" for none)."""
        return next((b.key for b in self.blocks if any(f.key == key for f in b.figures)), "")


@dataclass(frozen=True)
class TableTrail:
    """The trail of By location or By disaggregation (each a part of its own: the tab's table is read once
    more, recorded)."""

    tab: str
    query: str
    day: datetime.date
    steps: tuple[Line, ...]
    block: Block


# ----------------------------------------------------------------------------------------------- the recorder
class Recorder:
    """What the page's functions tell the trail from inside (their ``record``): the status words of every
    report read and the row behind each, the rows counted against a tab's limit, each indicator report or
    location block a tab's filter leaves out, and each location line with its records. For Partner
    monitoring's Reached (``monitoring._attach_reached``, ``reached.compute`` and ``progress_values``): the
    programme documents and report type read, where each partner report row went (``placed``: its pass, the
    indicator, the way, whether the indicator read it, its progress report), the rows of a shared title left
    out, PRP's report totals found and kept, each indicator's rows left out by ``one_indicator`` and by a
    location filter, and its reports (key, status, counted, with a value, where the value came from)."""

    def __init__(self) -> None:
        self.words: dict[str, dict[str, rc.Stamp]] = {}
        self.newest: dict[str, dict[str, tuple[rc.Stamp, Any]]] = defaultdict(dict)
        self.counts: dict[str, tuple[int, int | None]] = {}
        self.dropped_rows: Counter[str] = Counter()
        self.dropped_units: dict[str, set[tuple[str, str]]] = defaultdict(set)
        self.lines: list[tuple[str, str, str, tuple, Any]] = []
        self.pd_ids: frozenset[int] = frozenset()
        self.report_type = ""
        self.placements: dict[Any, list] = {}  # Datamart id -> [pass, indicator, way, read, progress report]
        self.shared: Counter[tuple[int, str, str]] = Counter()  # (PD, title, way) of a shared title left out
        self.totals: tuple[frozenset[str], frozenset[str]] = (frozenset(), frozenset())
        self.left: dict[tuple[int, str], dict[str, list[Any]]] = defaultdict(lambda: defaultdict(list))
        self.reports: dict[tuple[int, str], tuple[tuple[str, str, bool, bool, str], ...]] = {}
        self.values: dict[tuple[int, str], tuple[Any, bool, str, str]] = {}

    # ------------------------------------------------------------------------ Partner monitoring's Reached
    def reading(self, pd_ids: Iterable[int], report_type: str) -> None:
        self.pd_ids, self.report_type = frozenset(pd_ids), report_type

    def placed(self, row: dict[str, Any], spot: Any, kind: str, shared: tuple[int, str] | None) -> None:
        self.placements[row["datamart_id"]] = [kind, spot.ident, spot.way, False, rc.report_key(row)]
        if shared is not None:
            self.shared[(*shared, spot.way)] += 1

    def used(self, row: dict[str, Any]) -> None:
        self.placements[row["datamart_id"]][3] = True

    def prp_totals(self, found: Iterable[str], kept: Iterable[str]) -> None:
        """PRP's report totals read: the ids of those found with a value, and of those kept."""
        self.totals = (frozenset(found), frozenset(kept))

    def left_out(self, ident: tuple[int, str]) -> Callable[[str, list[Any]], None]:
        """``reached.compute``'s record for one indicator: the rows it leaves out, by why."""
        return lambda why, ids: self.left[ident][why].extend(ids)

    def reached(self, ident: tuple[int, str], result: rc.Reached, read_from: str) -> None:
        counted = {r.key for r in result.counted}
        self.reports[ident] = tuple(
            (r.key, r.status, r.counted, r.key in counted, r.source) for r in result.reports
        )
        self.values[ident] = (result.reached, result.partial, result.source, read_from)

    def status_words(self, found: dict[str, dict[str, rc.Stamp]]) -> None:
        self.words.update(found)

    def status_rows(self, rows: Iterable[dict[str, Any]], wanted: set[str]) -> None:
        """For each report of ``wanted`` and each word its rows carry: the row changed last, then synced last,
        then with the highest Datamart id (the row that gives the report that word), and its Datamart id."""
        for row in rows:
            key = rc.report_key(row)
            if key not in wanted:
                continue
            word, stamp = (row["report_status"] or "").strip(), rc.row_stamp(row)
            held = self.newest[key].get(word)
            if held is None or rc.stamp_order(stamp) > rc.stamp_order(held[0]):
                self.newest[key][word] = (stamp, row["datamart_id"])

    def count(self, name: str, n: int, limit: int | None) -> None:
        self.counts[name] = (n, limit)

    def dropped(self, stage: str, report: str, indicator_report: str, rows: int) -> None:
        self.dropped_rows[stage] += rows
        self.dropped_units[stage].add((report, indicator_report))

    def line(self, report: str, indicator_report: str, place: str, ids: tuple, chosen: Any) -> None:
        self.lines.append((report, indicator_report, place, tuple(ids), chosen))


# ---------------------------------------------------------------------------------------------- the rows kept
def _why() -> Case:
    """Why the PD status filter leaves a row out (``reporting.LEFT_OUT_REASONS``, read in the same query)."""
    return Case(
        *(When(q, then=Value(name)) for name, q in reporting.LEFT_OUT_REASONS.items()),
        default=Value(""),
        output_field=CharField(),
    )


def chain(
    filters: reporting.ReportFilters, day: datetime.date
) -> tuple[TrailRows, list[dict[str, Any]], Recorder, list[str]]:
    """The rows of every step of ``filters`` (``ReportFilters.queryset(steps=...)``): one pass over the rows
    in NeuroDB (their keys, no payload), one list of ids per filter that applies, then ``progress_reports`` of
    the rows kept, with their PD's details. Returns the rows, the progress reports kept (every status), the
    recorder and the filters whose rows were not all among the rows before them (never: a chain that does not
    add up)."""
    steps: list[tuple[str, Any]] = []
    kept = filters.queryset(day, steps=steps)
    names: dict[str, str] = {}  # one string per key, shared by its rows (what is kept stays small)
    rows: dict[int, list] = {}
    for row in reporting.unit_rows(steps[0][1]).annotate(_why=_why()):
        report, ir = reporting.unit_key(row)
        rows[row["pk"]] = [
            row["datamart_id"],
            names.setdefault(report, report),
            names.setdefault(ir, ir),
            len(steps),
            row["_why"],
            row["period_start"],
        ]
    broken: list[str] = []
    previous = set(rows)
    for index, (name, queryset) in enumerate(steps[1:], start=1):
        current = set(queryset.values_list("pk", flat=True).order_by())
        if current - previous:
            broken.append(name)
        for pk in previous - current:
            rows[pk][3] = index
        previous = current & previous
    recorder = Recorder()
    reports = reporting.with_pd_details(reporting.progress_reports(kept, day, record=recorder))
    chosen = set(filters.statuses)
    reported = {
        reporting.partner_key(r)
        for r in reports
        if (not chosen or r["status"] in chosen) and r["status"] in rc.COUNTED
    }
    every_row = Counter(r[1] for r in rows.values())
    kept_rows = Counter(r[1] for r in rows.values() if r[3] == len(steps))
    facts: dict[str, Report] = {}
    for r in reports:
        key, partner, word = r["key"], reporting.partner_key(r), r["report_status"] or ""
        newest = recorder.newest.get(key, {}).get(word)
        month = reporting.report_month(r)
        facts[key] = Report(
            status=r["status"],
            chosen=not chosen or r["status"] in chosen,
            month=f"{month[0]:04d}-{month[1]:02d}" if month else "none",
            partner=partner,
            reported=partner in reported,
            pd=reporting.pd_follow_up_key(r),
            late=reporting.submitted_late(r),
            sections=dict(r["indicator_sections"]),
            word=word,
            word_from=newest[1] if newest else None,
            rows_outside=every_row[key] - kept_rows[key],
            section=reporting.report_section(r),
        )
    trail_rows = TrailRows(
        stages=tuple(name for name, _ in steps),
        rows={pk: tuple(r) for pk, r in rows.items()},
        reports=facts,
    )
    return trail_rows, reports, recorder, broken


def _base(rows: TrailRows, name: str) -> Callable[[int, tuple], bool]:
    """The rows a selection starts from: ``all`` (every row in NeuroDB), ``kept`` (by the filter), ``chosen``
    (and by the report status), ``status_out`` (kept, of a report status not chosen), ``stage:<filter>`` (left
    out by that filter), ``reason:<why>`` (left out by the PD status filter for that reason, or for any of
    ``<why>|<why>``), ``history`` (a period starting before the date NeuroDB holds partner reports from),
    ``no_partner`` (chosen, of a report naming no partner)."""
    kept_at = rows.kept_at
    reports = rows.reports
    if name == "all":
        return lambda pk, r: True
    if name == "kept":
        return lambda pk, r: r[3] == kept_at
    if name == "chosen":
        return lambda pk, r: r[3] == kept_at and reports[r[1]].chosen
    if name == "status_out":
        return lambda pk, r: r[3] == kept_at and not reports[r[1]].chosen
    if name == "no_partner":
        return lambda pk, r: r[3] == kept_at and reports[r[1]].chosen and reports[r[1]].partner is None
    if name == "history":
        first = rows.history
        return lambda pk, r: first is not None and r[5] is not None and r[5] < first
    if name.startswith("stage:"):
        index = rows.stages.index(name[6:]) if name[6:] in rows.stages else -1
        return lambda pk, r: r[3] == index
    if name.startswith("reason:"):
        index = rows.stages.index("pd_status") if "pd_status" in rows.stages else -1
        why = set(name[7:].split("|"))
        return lambda pk, r: r[3] == index and r[4] in why
    raise ValueError(f"no rows named {name!r}")


def _check(rows: TrailRows, name: str, value: Any) -> Callable[[int, tuple], bool]:
    """One condition of a selection, on a row the filter keeps."""
    reports = rows.reports
    if name == "status":
        return lambda pk, r: reports[r[1]].status == value
    if name == "section":  # the section a progress report is counted under (reporting.report_section)
        return lambda pk, r: reports[r[1]].section == value
    if name == "month":
        return lambda pk, r: reports[r[1]].month == value
    if name == "late":
        return lambda pk, r: reports[r[1]].late == value
    if name == "partner":
        return lambda pk, r: reports[r[1]].partner == value
    if name == "has_partner":
        return lambda pk, r: (reports[r[1]].partner is not None) == value
    if name == "reported":
        return lambda pk, r: reports[r[1]].reported == value
    if name == "pd":
        return lambda pk, r: reports[r[1]].pd == value
    if name == "report":
        return lambda pk, r: r[1] == value
    if name == "follow_up":  # a report to follow up (one not yet due among the Due ones)
        return lambda pk, r: (reports[r[1]].status in reporting.FOLLOW_UP_STATUSES) == value
    if name == "follow_up_status":  # its status in the lists to follow up: one not yet due is Due there
        return lambda pk, r: reporting.follow_up_status(reports[r[1]].status) == value
    raise ValueError(f"no condition named {name!r}")


def selected(
    rows: TrailRows, select: Iterable[tuple[str, Any]], pks: Iterable[int] | None = None
) -> list[int]:
    """The rows (pks, in order) a selection keeps: its ``set`` (:func:`_base`, ``kept`` by default), then
    every other condition (:func:`_check`)."""
    select = tuple(select)
    base = _base(rows, next((v for k, v in select if k == "set"), "kept"))
    checks = [_check(rows, k, v) for k, v in select if k != "set"]
    out = []
    for pk in sorted(rows.rows) if pks is None else pks:
        r = rows.rows[pk]
        if base(pk, r) and all(c(pk, r) for c in checks):
            out.append(pk)
    return out


def unit_of(rows: TrailRows, unit: str) -> Callable[[tuple], Any]:
    """A row's unit of a figure: its indicator report, progress report, partner or programme document (None:
    the row counts in no unit, such as a report naming no partner)."""
    if unit == IR:
        return lambda r: (r[1], r[2])
    if unit == PR:
        return lambda r: r[1]
    if unit == PARTNER:
        return lambda r: rows.reports[r[1]].partner if r[1] in rows.reports else None
    if unit == PD:
        return lambda r: rows.reports[r[1]].pd if r[1] in rows.reports else None
    return lambda r: r[0]


class _Index:
    """The rows the filter keeps, by report status, report, partner and programme document: each figure reads
    the rows of its narrowest condition only, so a trail of many figures stays one pass per figure kind."""

    FIELDS = ("status", "report", "partner", "pd")

    def __init__(self, rows: TrailRows) -> None:
        self.rows = rows
        self.by: dict[str, dict[Any, list[int]]] = {name: defaultdict(list) for name in self.FIELDS}
        self.kept: list[int] = []
        for pk in sorted(rows.rows):
            r = rows.rows[pk]
            if r[3] != rows.kept_at:
                continue
            report = rows.reports[r[1]]
            self.kept.append(pk)
            self.by["status"][report.status].append(pk)
            self.by["report"][r[1]].append(pk)
            self.by["partner"][report.partner].append(pk)
            self.by["pd"][report.pd].append(pk)

    def count(self, select: tuple[tuple[str, Any], ...], unit: str) -> int:
        candidates: list[int] | None = None
        for name, value in select:
            if name in self.by:
                found = self.by[name].get(value, [])
                if candidates is None or len(found) < len(candidates):
                    candidates = found
        base = dict(select).get("set", "kept")
        if candidates is None and base in ("kept", "chosen", "no_partner", "status_out"):
            candidates = self.kept
        of = unit_of(self.rows, unit)
        units = {of(self.rows.rows[pk]) for pk in selected(self.rows, select, candidates)}
        units.discard(None)
        return len(units)


# ------------------------------------------------------------------------------------------------ the figures
CHOSEN = (("set", "chosen"),)


def _tiles(counts: dict[str, Any]) -> list[Figure]:
    """The tiles: progress reports per status (as Power BI's Reporting Progress counts them, the user's
    decision of 26 Oct 2026), and under each the indicator reports in them."""
    out = [
        Figure(
            "pr.progress_reports",
            "Progress reports",
            PR,
            counts["progress_reports"],
            how="Every progress report kept, of the report statuses chosen",
            select=CHOSEN,
        )
    ]
    for s in counts["statuses"]:
        out.append(
            Figure(
                f"pr.status.{s['key']}",
                s["label"],
                PR,
                s["progress_reports"],
                how=f"With the status {s['label']} (one status per progress report, read from all its rows)",
                select=(*CHOSEN, ("status", s["key"])),
            )
        )
    out.append(
        Figure(
            "pr.indicator_reports",
            "Progress reports: the indicator reports under it",
            IR,
            counts["indicator_reports"],
            how="Every indicator report kept, of the report statuses chosen",
            select=CHOSEN,
        )
    )
    for s in counts["statuses"]:
        out.append(
            Figure(
                f"pr.status_indicators.{s['key']}",
                f"{s['label']}: the indicator reports under it",
                IR,
                s["indicator_reports"],
                how=f"Indicator reports of the progress reports with the status {s['label']}",
                select=(*CHOSEN, ("status", s["key"])),
            )
        )
    return out


def _left_out(left: dict[str, int]) -> list[Figure]:
    """The note on what the PD status filter leaves out (drawn when it leaves out something): P16 to P18
    themselves, read from ``left_out``."""
    shown = bool(left.get("by_pd_status") or left.get("no_pd"))
    by_status = bool(left.get("by_pd_status"))
    reasons = {
        "other_status": "Of programme documents with another status",
        "not_held": "Of programme documents NeuroDB does not hold",
        "no_pd": "Naming no programme document",
    }
    out = [
        Figure(
            "pr.left_out.by_pd_status",
            "Left out by the PD status filter",
            IR,
            left.get("by_pd_status", 0),
            how="Of programme documents with another status or not in NeuroDB, whatever their report status",
            select=(("set", "reason:other_status|not_held"),),
            drawn=shown and by_status,
            note="" if shown and by_status else "not drawn: none left out",
        )
    ]
    for name, label in reasons.items():
        out.append(
            Figure(
                f"pr.left_out.{name}",
                label,
                IR,
                left.get(name, 0),
                how="Left out by the PD status filter, whatever their report status",
                select=(("set", f"reason:{name}"),),
                drawn=(drawn := shown and (by_status if name != "no_pd" else bool(left.get("no_pd")))),
                note="" if drawn else "not drawn: none left out",
            )
        )
    return out


def _by_section(table: dict[str, Any]) -> list[Figure]:
    labels = [rc.STATUS_LABELS[k] for k in reporting.STATUS_KEYS]
    out = []
    for row in table["rows"]:
        section = row["section"]
        name = "(no section)" if section == reporting.NO_SECTION else section
        how = (
            f"Progress reports counted under {name} (their Section as the table shows it: their rows' own "
            "sections, else their programme document's, joined)"
        )
        for key, label, n in zip(reporting.STATUS_KEYS, labels, row["counts"], strict=True):
            out.append(
                Figure(
                    f"pr.section.{section}.{key}",
                    f"{name} · {label}",
                    PR,
                    n,
                    how=f"{how}, status {label}",
                    select=(*CHOSEN, ("status", key), ("section", section)),
                )
            )
        out.append(
            Figure(
                f"pr.section.{section}.total",
                f"{name} · Total",
                PR,
                row["total"],
                how=how,
                select=(*CHOSEN, ("section", section)),
            )
        )
    drawn = bool(table["rows"])  # with no row the page says "No progress reports match." instead of the table
    note = "" if drawn else "not drawn: no progress report"
    for key, label, n in zip(reporting.STATUS_KEYS, labels, table["totals"], strict=True):
        out.append(
            Figure(
                f"pr.section_total.{key}",
                f"Total · {label}",
                PR,
                n,
                how=f"Every section, status {label}",
                select=(*CHOSEN, ("status", key)),
                drawn=drawn,
                note=note,
            )
        )
    out.append(
        Figure(
            "pr.section_total.total",
            "Total",
            PR,
            table["total"],
            how="Every section",
            select=CHOSEN,
            drawn=drawn,
            note=note,
        )
    )
    return out


def month_key(label: str) -> str:
    """A month of the chart (``reporting.by_month``'s label, "Mar 2026") as the figure's key: 2026-03, or
    "none" for the reports with no period end."""
    try:
        return datetime.datetime.strptime(label, "%b %Y").strftime("%Y-%m")
    except ValueError:
        return "none"


def _by_month(chart: dict[str, Any]) -> list[Figure]:
    keys = {label: key for key, label in rc.STATUS_LABELS.items()}
    out = []
    for i, month in enumerate(chart["labels"]):
        code = month_key(month)
        for label, values in chart["series"].items():
            out.append(
                Figure(
                    f"pr.month.{code}.{keys[label]}",
                    f"{month} · {label}",
                    PR,
                    values[i],
                    how=f"Period ends in {month}, status {label}",
                    select=(*CHOSEN, ("status", keys[label]), ("month", code)),
                    drawn=False,  # the chart's data, not a number of the page's HTML
                    note="the chart's data",
                )
            )
    return out


def report_words(report: dict[str, Any]) -> str:
    """A progress report as the table names it: its programme document, report # and type."""
    return " ".join(
        x
        for x in (
            report.get("pd_reference_number") or "—",
            report.get("report_number") or "—",
            report.get("report_type") or "",
        )
        if x
    )


def _reports(reports: list[dict[str, Any]]) -> list[Figure]:
    out = [
        Figure(
            "pr.reports_listed",
            "Progress reports: the table's rows",
            PR,
            len(reports),
            how="The progress reports of the table, of the report statuses chosen",
            select=CHOSEN,
        )
    ]
    for r in reports:
        out.append(
            Figure(
                f"pr.report.{r['key']}.indicators",
                f"{report_words(r)}: Indicators",
                IR,
                r["indicators"],
                how="The indicator reports of this progress report",
                select=(*CHOSEN, ("report", r["key"])),
                listed=True,
            )
        )
    return out


def _partners(partners: dict[str, Any], *, every: bool = True, drawn: bool = True) -> list[Figure]:
    """The Partners tab's figures, or Home's partners line (``every=False``; ``drawn``: Home draws it only
    when there is a progress report)."""
    base = (*CHOSEN, ("has_partner", True))
    note = "" if drawn else "not drawn: no progress report"
    out = [
        Figure(
            "pr.partners",
            "Partners",
            PARTNER,
            partners["partners"],
            how="Partners with a progress report in the filter (by eTools partner, else by name)",
            select=base,
            drawn=drawn,
            note=note,
        ),
        Figure(
            "pr.partners_reported",
            "With at least one report",
            PARTNER,
            partners["reported"],
            how="Partners with at least one Submitted or Accepted report",
            select=(*base, ("reported", True)),
            drawn=drawn,
            note=note,
        ),
        Figure(
            "pr.partners_not_reported",
            "Not reported",
            PARTNER,
            partners["not_reported"],
            how="Partners without a Submitted or Accepted report",
            select=(*base, ("reported", False)),
            drawn=drawn,
            note=note,
        ),
    ]
    if not every:  # Home's partners line
        return out
    for s in partners["by_status"]:
        out.append(
            Figure(
                f"pr.partners_status.{s['key']}",
                f"Partners with a report {s['label']}",
                PARTNER,
                s["partners"],
                how=f"Partners with a report in the status {s['label']}",
                select=(*base, ("status", s["key"])),
            )
        )
    keys = {label: key for key, label in rc.STATUS_LABELS.items()}
    for row in partners["not_reported_list"]:
        for label, n in row["statuses"].items():
            out.append(
                Figure(
                    f"pr.partner.{row['key']}.{keys[label]}",
                    f"{row['name']}: {label.lower()}",
                    PR,
                    n,
                    how=f"This partner's progress reports in the status {label}",
                    select=(*CHOSEN, ("partner", row["key"]), ("status", keys[label])),
                )
            )
    out.append(
        Figure(
            "pr.follow_up",
            "Reports to follow up",
            PR,
            len(partners["follow_up"]),
            how="Progress reports Overdue, Sent back, Due (one not yet due among them, as in Power BI's "
            "list) or Submitted",
            select=(*CHOSEN, ("follow_up", True)),
        )
    )
    return out


def _home_pds(pds: dict[str, Any], rows: list[dict[str, Any]]) -> list[Figure]:
    out = [
        Figure(
            "home.pds",
            "Programme documents to follow up",
            PD,
            pds["count"],
            how="Programme documents with a report to follow up (by NeuroDB's link, else by reference)",
            select=(*CHOSEN, ("follow_up", True)),
        )
    ]
    for row in rows:
        name = row["pd_reference_number"] or "—"
        for key in reporting.FOLLOW_UP:
            label = rc.STATUS_LABELS[key]
            also = " (one not yet due among them, as in Power BI's follow-up list)" if key == rc.DUE else ""
            out.append(
                Figure(
                    f"home.pd.{row['pd_reference_number'] or row['key']}.{key}",
                    f"{name}: {label}",
                    PR,
                    row["counts"][key],
                    how=f"This programme document's progress reports with the status {label}{also}",
                    select=(*CHOSEN, ("pd", row["key"]), ("follow_up_status", key)),
                )
            )
    return out


def _rebuild(blocks: list[Block], rows: TrailRows) -> tuple[Block, ...]:
    """Each figure with its value rebuilt from the rows (:class:`_Index`)."""
    index = _Index(rows)
    return tuple(
        dataclasses.replace(
            block,
            figures=tuple(
                dataclasses.replace(f, rebuilt=index.count(f.select, f.unit) if f.select else f.page)
                for f in block.figures
            ),
        )
        for block in blocks
    )


# -------------------------------------------------------------------------------------------------- the steps
def _day(day: datetime.date | datetime.datetime | None) -> str:
    from .comparison import _day as words_of

    return words_of(day)


def dataset_steps(
    facts: runfacts.RunFacts, rows: TrailRows, held: reporting.Units, day: datetime.date
) -> list[Step]:
    """D1 to D4b (SPEC_validation §3.4.1): what eTools said it had, what NeuroDB received, what it did not
    keep, what is in NeuroDB, and from which date it holds partner reports. Counts only, but D4 and D4b."""
    from .comparison import _history

    out = source_steps(facts, SOURCE, "partner report rows", held.rows, kept=held)
    _first, history = _history(day)
    before = len(selected(rows, (("set", "history"),)))
    out.append(
        Step(
            "D4b",
            "dataset",
            "From which date",
            (
                Line(
                    f"NeuroDB holds partner reports from {history}; rows starting before it: {before:,}.",
                    what="step:history" if before else "",
                ),
            ),
            what="step:history" if before else "",
        )
    )
    return out


def source_steps(
    facts: runfacts.RunFacts,
    source: str,
    noun: str,
    held: int,
    *,
    kept: reporting.Units | None = None,
    what: str = "step:start",
) -> list[Step]:
    """D1 to D4 of one source (``noun``: its records in words, "partner report rows"): what eTools said it
    had, what NeuroDB received, what it did not keep and what is in NeuroDB (``held``, counted in the trail's
    snapshot), with the CSV of D4 (``what``), or past a download's limit Raw eTools data of the source."""
    from . import raw

    run = facts.run
    when = f" (the sync of {_day(run.finished_at or run.started_at)})" if run is not None else ""
    out = []
    if facts.said_it_had is None:
        said = "The last sync did not say how many records eTools had." if run else "No sync has run yet."
    else:
        said = f"eTools said it had {facts.said_it_had:,} {noun}{when}."
        if facts.other_country:  # it sent other countries' rows: the count is every country's
            said = said[:-1] + ", of every country: the Datamart does not filter this list by country."
    out.append(Step("D1", "dataset", "What eTools said it had", (Line(said),), counts_only=True))
    lines = [Line(f"NeuroDB received {facts.received or 0:,}.")]
    if facts.sent_twice:
        lines.append(
            Line(f"{facts.sent_twice:,} were sent twice (each counted once in what NeuroDB received).")
        )
    if facts.never_came:
        besides = f" and the {facts.copies_proven:,} it sent twice" if facts.copies_proven else ""
        then = (
            "NeuroDB counts a record sent twice as eTools' own copy only when every copy comes identical in "
            "one answer of the Datamart (one page): these did not, so the read is not complete."
            if facts.gap_by_repeats
            else "The next sync reads them."
        )
        said = f" {facts.shift_words}" if facts.shift_words else ""
        lines.append(
            Line(
                f"eTools said it had {facts.never_came:,} more than NeuroDB received{besides}.{said} {then}",
                alert=True,
            )
        )
    elif facts.shift_words:
        lines.append(Line(facts.shift_words))
    if facts.more_than_said:
        lines.append(
            Line(
                f"NeuroDB received {facts.more_than_said:,} more than eTools said it had: records added "
                "during the sync."
            )
        )
    out.append(Step("D2", "dataset", "What NeuroDB received", tuple(lines), counts_only=True))
    other = f"Not kept: {facts.other_country:,} of another country"
    if facts.through_area_filter:
        other += f" (of which {facts.through_area_filter:,} through the business area filter)"
    lines = [
        Line(other + "."),
        Line(
            f"{facts.unnamed:,} named no country and no programme document NeuroDB holds. They could be "
            "Lebanon's: see check 11 of the eTools reporting checks."
        ),
        Line(f"{facts.not_saved:,} could not be saved.", alert=bool(facts.not_saved)),
    ]
    lines += [Line(f"{e.count:,} could not be saved: {e.words}", alert=True) for e in facts.error_lines]
    out.append(Step("D3", "dataset", "What NeuroDB did not keep", tuple(lines), counts_only=True))
    limit = raw.csv_limit(source)
    them = "rows" if "rows" in noun else "records"
    unsent = ""
    if facts.no_longer_sent:  # the partner report rows a whole read no longer returned: kept and counted
        unsent = (
            f", of which {facts.no_longer_sent:,} eTools no longer sends (last sent "
            f"{raw.between(facts.last_sent_first, facts.last_sent_last)}): kept and counted"
        )
    lines = [
        Line(
            f"{facts.held:,} {them} are in NeuroDB: {facts.updated_by_sync:,} updated by that sync, "
            f"{facts.updated_since:,} updated since (a sync is running), {facts.from_earlier:,} kept from an "
            f"earlier sync{unsent}.",
            what=what if held <= limit else "",
        )
    ]
    if held > limit:  # Raw eTools data has the filters that make a download small enough
        lines.append(
            Line(
                f"These {held:,} {them} are more than the {limit:,} a download holds: download them from Raw "
                f"eTools data, choosing {raw.CHOOSE}.",
                raw=source,
            )
        )
    if facts.newer_failed is not None:
        lines.append(
            Line(
                f"The last sync, on {_day(facts.newer_failed.started_at)}, failed: the records in NeuroDB "
                f"come from the sync of {_day(run.started_at) if run else '—'}.",
                alert=True,
            )
        )
    if facts.removal_skipped:
        lines.append(
            Line(
                "The last sync did not get every record, so records eTools may have deleted are still here.",
                alert=True,
            )
        )
    if facts.unexplained:
        lines.append(
            Line(
                f"{facts.unexplained:,} records are not explained by the sync's counts: tell the NeuroDB "
                "team.",
                alert=True,
            )
        )
    if facts.held != held:
        lines.append(Line(f"The rows changed while they were read: {held:,} read now. {DIFFERS}", alert=True))
    out.append(
        Step(
            "D4",
            "dataset",
            "What is in NeuroDB",
            tuple(lines),
            kept=kept,
            what=what if held <= limit else "",
        )
    )
    return out


def _filter_title(name: str, filters: reporting.ReportFilters) -> str:
    """A filter of ``ReportFilters.queryset`` in plain words: what it leaves out."""
    f = filters
    several = {
        "report_type": f.report_types,
        "section": f.sections,
        "report_number": f.report_numbers,
        "pds": f.pds,
    }
    plural = "s" if len(several.get(name) or []) > 1 else ""
    found = {
        "report_type": f"Report type is not {', '.join(f.report_types)}",
        "year": f"Reporting period does not end in {f.year}",
        "q": f'Does not match the search "{f.q}" (partner, vendor number, PD reference or indicator)',
        "section": f"Not in the section{plural} {', '.join(f.sections)} (the row's own section, else its "
        "programme document's)",
        "report_number": f"Report # is not {', '.join(f.report_numbers)}",
        "pds": f"Not the programme document{plural} {', '.join(f.pds)}",
        "partner": "Not the partner chosen" if len(f.partners) == 1 else "Not the partners chosen",
        "partner_type": f"Partner type is not {', '.join(f.partner_types)}",
        "not_a_partnership": "Programme document is a draft or cancelled (never read)",
        "emergency": "Its programme document is not an SPD (the Emergency indicator)"
        if f.emergency == "yes"
        else "Its programme document is an SPD (the Emergency indicator)",
        "start_from": f"Period starts before {_day(f.start_from)}",
        "start_to": f"Period starts after {_day(f.start_to)}",
        "end_from": f"Period ends before {_day(f.end_from)}",
        "end_to": f"Period ends after {_day(f.end_to)}",
        "overdue": "Not overdue (submitted, or its due date has not passed)",
        "pd_status": "The PD status filter",
    }
    return found[name]


def _units_of(rows: TrailRows, at_least: int) -> tuple[reporting.Units, set, set]:
    """The rows still there after the stage ``at_least - 1`` (``rows[3] >= at_least``), their units and the
    sets of their indicator reports and progress reports."""
    found = [r for r in rows.rows.values() if r[3] >= at_least]
    irs = {(r[1], r[2]) for r in found}
    prs = {r[1] for r in found}
    return reporting.Units(len(found), len(irs), len(prs)), irs, prs


def filter_steps(
    rows: TrailRows, filters: reporting.ReportFilters, left: dict[str, int], broken: list[str]
) -> list[Step]:
    """One step per filter that applies, in ``ReportFilters.queryset``'s order: the rows it leaves out, the
    indicator reports and progress reports it leaves out entirely and partly (some of their rows kept)."""
    out = []
    before, irs_before, prs_before = _units_of(rows, 0)
    for index, name in enumerate(rows.stages[1:], start=1):
        after, irs, prs = _units_of(rows, index + 1)
        dropped = [r for r in rows.rows.values() if r[3] == index]
        out_irs = {(r[1], r[2]) for r in dropped}
        out_prs = {r[1] for r in dropped}
        gone = reporting.Units(
            before.rows - after.rows,
            before.indicator_reports - after.indicator_reports,
            before.progress_reports - after.progress_reports,
        )
        partly = (len(out_irs & irs), len(out_prs & prs))
        lines = []
        if name == "pd_status":
            if filters.status_choice:
                labels = ", ".join(pd_statuses.label(s) for s in filters.status_choice)
                lines += [
                    Line(
                        f"Programme document status not chosen ({labels} chosen): "
                        f"{words(left['other_status'], IR)}",
                        what="reason:other_status" if left["other_status"] else "",
                    ),
                    Line(
                        f"Programme document not in NeuroDB: {words(left['not_held'], IR)}",
                        what="reason:not_held" if left["not_held"] else "",
                    ),
                ]
            lines.append(
                Line(
                    f"Names no programme document: {words(left['no_pd'], IR)}",
                    what="reason:no_pd" if left["no_pd"] else "",
                )
            )
            if gone.indicator_reports != left["by_pd_status"] + left["no_pd"]:
                lines.append(
                    Line(f"These do not add up to the indicator reports left out. {DIFFERS}", alert=True)
                )
        if name in broken:
            lines.append(
                Line(f"This filter kept rows the step before it had left out. {DIFFERS}", alert=True)
            )
        out.append(
            Step(
                name,
                "filter",
                _filter_title(name, filters),
                tuple(lines),
                kept=after,
                out=gone,
                partly=partly,
                what=f"step:{name}" if dropped else "",
            )
        )
        before = after
    return out


def kept_steps(rows: TrailRows, filters: reporting.ReportFilters) -> list[Step]:
    """K, the rows kept by the filter, and S, the report status (one per progress report, read from all its
    rows) when a status is chosen."""
    kept, irs, prs = _units_of(rows, rows.kept_at)
    out = [Step("K", "kept", "Kept by the filter", kept=kept, what="kept")]
    if filters.statuses:
        labels = ", ".join(rc.STATUS_LABELS[s] for s in filters.statuses)
        left = [r for r in rows.rows.values() if r[3] == rows.kept_at and not rows.reports[r[1]].chosen]
        gone = reporting.Units(len(left), len({(r[1], r[2]) for r in left}), len({r[1] for r in left}))
        after = reporting.Units(
            kept.rows - gone.rows,
            kept.indicator_reports - gone.indicator_reports,
            kept.progress_reports - gone.progress_reports,
        )
        out.append(
            Step(
                "S",
                "status",
                f"Report status not chosen ({labels} chosen; one status per progress report, read from all "
                "its rows)",
                kept=after,
                out=gone,
                what="step:S" if left else "",
            )
        )
    return out


def filter_words(filters: Any, tab_filters: Any = None) -> tuple[str, ...]:
    """The filter in words, one entry per field: the PD statuses and the reporting period always."""
    from .comparison import _default, _partner_names, _words

    partners = _partner_names(getattr(filters, "partners", []))
    out = []
    for instance in (filters, tab_filters):
        for item in dataclasses.fields(instance) if instance is not None else ():
            value = getattr(instance, item.name)
            if item.name == "pd_statuses":
                chosen = instance.status_choice
                text = ", ".join(pd_statuses.label(s) for s in chosen) if chosen else "every status"
                if instance.pd_statuses is None:
                    text = f"running programme documents ({text.lower()})"
                out.append(f"PD status: {text}")
                continue
            if item.name == "year" and not value:
                out.append("every reporting period")
                continue
            if value == _default(item):
                continue
            text = _words("progress-reports", item.name, value, instance, partners)
            out.append(f"{FILTER_LABELS[item.name]}: {text}")
    return tuple(out)


# -------------------------------------------------------------------------------------------- building trails
def _stored(key: str, build: Callable[[], Any]) -> Any:
    if settings.DEBUG:
        return build()
    return compute.once(key, build, STORED_SECONDS, lang_neutral=True)


def progress_trail(page: str, params: Any, day: datetime.date) -> Trail:
    """The trail of Progress reports (``page="progress-reports"``) or of Home's Progress reports block
    (``"home"``) for the filter of ``params`` on ``day``: computed once in one snapshot of the data, kept two
    minutes for every worker."""
    if page not in ("progress-reports", "home"):
        raise ValueError(f"no Progress reports trail for the page {page!r}")
    params = filter_query(params)
    filters = reporting.ReportFilters.from_params(params)
    key = f"trail:v{CACHE_VERSION}:{page}:{filters.cache_key()}:{day}:{keys.fingerprint()}"
    return _stored(key, lambda: _progress_trail(page, params, day))


def _progress_trail(page: str, params: Any, day: datetime.date) -> Trail:
    from . import services
    from .raw import STREAM_TIMEOUT_MS, snapshot

    with snapshot(STREAM_TIMEOUT_MS):
        filters = reporting.ReportFilters.from_params(params)
        left = reporting.left_out(filters, day)
        rows, _kept, _recorder, broken = chain(filters, day)
        rows = TrailRows(rows.stages, rows.rows, rows.reports, rc.history_from(day).date)
        facts = runfacts.facts(SOURCE)
        held, _irs, _prs = _units_of(rows, 0)
        if page == "home":
            from neurodb.reports.home import follow_up_rows

            figures = services.reporting_figures(params, day)
            counts = figures["counts"]
            blocks = [
                Block("tiles", "Progress reports: tiles", "home", tuple(_tiles(counts))),
                Block(
                    "home_pds",
                    "Programme documents to follow up",
                    "home",
                    tuple(_home_pds(figures["pds"], follow_up_rows(figures))),
                ),
                Block(
                    "partners",
                    "The partners line",
                    "home",
                    tuple(
                        _partners(figures["partners"], every=False, drawn=bool(counts["progress_reports"]))
                    ),
                ),
            ]
            notes = (
                "Home shows no note on what the PD status filter leaves out: here it is, under The PD "
                "status filter.",
                *NOTES,
            )
        else:
            data = services.partner_reporting(params, day, options=False)
            counts = data["counts"]
            late = Figure(
                "pr.late",
                "Submitted after their due date",
                PR,
                data["summary"]["late"],
                how="Progress reports submitted after their due date (any status)",
                select=(*CHOSEN, ("late", True)),
            )
            no_partner = len({rows.rows[pk][1] for pk in selected(rows, (("set", "no_partner"),))})
            blocks = [
                Block("tiles", "Reporting progress: tiles", "reports", (*_tiles(counts), late)),
                Block(
                    "left_out",
                    "The note on what the PD status filter leaves out",
                    "reports",
                    tuple(_left_out(left)),
                ),
                Block(
                    "sections",
                    "Progress reports by section",
                    "reports",
                    tuple(_by_section(data["by_section"])),
                ),
                Block(
                    "months",
                    "Progress reports by month of the reporting period's end (the chart)",
                    "reports",
                    tuple(_by_month(data["by_month"])),
                ),
                Block(
                    "reports",
                    "Progress reports (the table)",
                    "reports",
                    tuple(_reports(data["reports"])),
                    notes=(
                        Line(
                            "One row per progress report of the table, as the page lists them.",
                            what="reports",
                        ),
                    ),
                ),
                Block(
                    "partners",
                    "Partners",
                    "partners",
                    tuple(_partners(data["partners"])),
                    notes=(
                        Line(
                            "Progress reports with neither a partner id nor a partner name: "
                            f"{words(no_partner, PR)} (left out of every Partners figure).",
                            what="no_partner" if no_partner else "",
                        ),
                        Line("One row per report to follow up, as the page lists them.", what="follow_up"),
                    ),
                ),
            ]
            notes = NOTES
        steps = [
            *dataset_steps(facts, rows, held, day),
            *filter_steps(rows, filters, left, broken),
            *kept_steps(rows, filters),
        ]
        return Trail(
            page=page,
            block="reporting" if page == "home" else "",
            query=params.urlencode(),
            day=day,
            as_of=runfacts.moment(facts.run),
            steps=tuple(steps),
            blocks=_rebuild(blocks, rows),
            rows=rows,
            counts=counts,
            left_out=left,
            filter_words=filter_words(filters),
            notes=notes,
        )


def table_trail(tab: str, params: Any, day: datetime.date) -> TableTrail:
    """The trail of Progress reports' By location (``tab="locations"``) or By disaggregation tab: the tab's
    table read once more, recorded from inside, beside the table the page reads (stored); kept two minutes."""
    if tab not in TABLES:
        raise ValueError(f"no figure trail for the tab {tab!r}")
    params = filter_query(params)
    filters = reporting.ReportFilters.from_params(params)
    tab_key = (
        reporting.DisaggregationFilters.from_params(params).cache_key() if tab == "disaggregations" else ""
    )
    key = f"trail-table:v{CACHE_VERSION}:{tab}:{filters.cache_key()}:{tab_key}:{day}:{keys.fingerprint()}"
    return _stored(key, lambda: _table_trail(tab, params, day))


def table_lines(tab: str, params: Any, day: datetime.date) -> tuple[dict[str, Any], Recorder]:
    """The tab's lines (By location) or blocks (By disaggregation), as the page computes them, with their
    records (``record``)."""
    from . import services

    data = services.partner_reporting(params, day, options=False)
    wanted = set(data["filters"].statuses) or None
    recorder = Recorder()
    if tab == "locations":
        found = reporting.location_rows(
            data["rows"], data["statuses"], data["today"], wanted, record=recorder
        )
    else:
        found = reporting.disaggregation_rows(
            data["rows"], data["statuses"], data["today"], wanted, params, record=recorder
        )
    return found, recorder


def _table_trail(tab: str, params: Any, day: datetime.date) -> TableTrail:
    from . import services
    from .raw import STREAM_TIMEOUT_MS, snapshot

    with snapshot(STREAM_TIMEOUT_MS):
        found, recorder = table_lines(tab, params, day)
        # the table the page reads: stored, else the one just computed (the same function, the same rows)
        stored = services.reporting_table_stored(tab, params, day)
        page = services.reporting_table(tab, params, day)[tab] if stored else found
    total, limit = recorder.counts.get("limit", (0, None))
    over = total > (limit or total)
    lines = []
    if tab == "disaggregations" and "before_indicator" in recorder.counts:
        before = recorder.counts["before_indicator"][0]
        indicator = reporting.DisaggregationFilters.from_params(params).indicator
        lines.append(Line(f"Rows kept by the filter: {words(before, ROW)}."))
        lines.append(
            Line(f'Indicator does not contain every word of "{indicator}": {words(before - total, ROW)}.')
        )
    else:
        lines.append(Line(f"Rows kept by the filter: {words(total, ROW)}."))
    lines.append(
        Line(
            f"The filter holds {words(total, ROW)}; the table reads at most {limit or 0:,}"
            + (": over it, the table is not drawn." if over else "."),
            alert=over,
        )
    )
    if recorder.dropped_rows["status"]:
        lines.append(
            Line(
                f"Report status not chosen: {words(len(recorder.dropped_units['status']), IR)} "
                f"({words(recorder.dropped_rows['status'], ROW)}) left out."
            )
        )
    tab_filters = reporting.DisaggregationFilters.from_params(params)
    for stage, label in (
        ("location", f'Location does not contain "{tab_filters.location}"'),
        ("p_code", f'P-code does not contain "{tab_filters.p_code}"'),
        ("disaggregation", f'No value matches "{tab_filters.disaggregation}"'),
    ):
        if recorder.dropped_rows[stage]:
            lines.append(Line(f"{label}: {words(recorder.dropped_rows[stage], BLOCK)} left out."))
    if tab == "locations":
        rebuilt = None if over else len(recorder.lines)
        shown = None if page["too_many"] else len(page["rows"])
        unit, key, label, what = LINE, "pr.location_lines", "By location: lines", "lines"
        lines.append(
            Line(
                f"Lines (one per indicator report and location): {words(rebuilt, LINE)}.",
                what=what if rebuilt else "",
            )
        )
    else:
        dropped = sum(recorder.dropped_rows[s] for s in ("location", "p_code", "disaggregation"))
        rebuilt = None if over else len(recorder.lines) - dropped
        shown = None if page["too_many"] else len(page["blocks"])
        unit, key, label, what = (
            BLOCK,
            "pr.disaggregation_locations",
            "By disaggregation: location blocks",
            "blocks",
        )
        lines.append(
            Line(
                f"Location blocks (one per indicator report and location): {words(rebuilt, BLOCK)}.",
                what=what if rebuilt else "",
            )
        )
    figures = []
    if page["too_many"]:
        figures.append(
            Figure(
                f"pr.{'location' if tab == 'locations' else 'disaggregation'}_rows_held",
                "The filter holds N report rows (more than the table reads)",
                ROW,
                page["too_many"],
                total if over else None,
                how="The rows kept by the filter, counted before the report status",
            )
        )
    figures.append(
        Figure(
            key,
            label,
            unit,
            shown,
            rebuilt,
            how="The table's rows, as the tab draws them",
            drawn=shown is not None,
        )
    )
    block = Block(tab, "By location" if tab == "locations" else "By disaggregation", tab, tuple(figures))
    return TableTrail(tab=tab, query=params.urlencode(), day=day, steps=tuple(lines), block=block)


# ------------------------------------------------------------------------------------------- the sync running
def sync_running() -> datetime.datetime | None:
    """When the eTools sync running now started (any job of ``keys.ETOOLS_JOBS``), or None."""
    from neurodb.core.models import SyncRun
    from neurodb.integrations import background

    for job in keys.ETOOLS_JOBS:
        if background.is_running(job):
            run = (
                SyncRun.objects.filter(job=job, status=SyncRun.Status.RUNNING).order_by("-started_at").first()
            )
            return run.started_at if run is not None else timezone.now()
    return None


__all__ = [
    "PAGES",
    "Block",
    "Figure",
    "Line",
    "Recorder",
    "Step",
    "TableTrail",
    "Trail",
    "TrailRows",
    "anchor",
    "chain",
    "progress_trail",
    "selected",
    "sync_running",
    "table_trail",
]
