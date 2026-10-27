"""Compare with Power BI, export by export: how each of the 13 kinds is matched with its NeuroDB page, which
of its columns are compared, and why a row differs (Power BI's own value against the page's, for Power BI's
filter).

Every comparer reads Power BI's rows as they were kept (``PowerBIExport.rows``) and NeuroDB's from the page's
own function (``powerbi_page``), pairs them (``powerbi_match.pair``), and adds each difference to the export's
:class:`~neurodb.datamart.powerbi_match.Result` with the first reason of ``powerbi_words.REASON_ORDER`` whose
test passes; when several columns of a row differ, the line takes the reason that most needs doing (the
highest group). The tests come from simple facts about NeuroDB's records (``powerbi_facts``): is the PD there,
what is its status, is the report there, under which words, when did the sync finish.

* A rule that applies to every row (Emergency is Yes for an SPD, the PD's sections and CP outputs, progress
  reports against indicator reports, Power BI's 0 for no value) is a note of the export, never a line.
* Where Power BI's rule is not known (Program Performance's and PD/SPD Indicator's Reached and Target, Data
  Quality's reading, the financial contribution, Outstanding DCT), NeuroDB counts Power BI's possible rules
  from its records and the export keeps a fit table: how many rows each fits.
* The counted visuals (Partner Performance's cards, Reporting Progress's table and chart) are explained from
  the PD/SPD Report list compared in the same upload (its gaps by status, sections and month); Data Quality's
  card from its list.

No comparer writes anything: it runs inside the comparison's read-only snapshot, and the job writes the lines
after it (``powerbi_compare``).
"""

from __future__ import annotations

import dataclasses
import datetime
import gzip
import io
import json
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any

from neurodb.partnerships import statuses as pd_statuses

from . import powerbi_exports as px
from . import powerbi_facts as pf
from . import powerbi_match as pm
from . import powerbi_page as page
from . import powerbi_read
from . import powerbi_words as words
from . import reached as rc
from .powerbi_filters import Filter
from .powerbi_links import reverse

# compared first: the counted visuals and the follow-up list are explained from the report list, the
# disaggregations (which have no PD #) tied to their PDs through the location export, Data Quality's card from
# its list
FIRST = ("report_list", "locations", "dq_list")
# the exports that read what the location export tells them (``Context.location_*``, ``held_locations``):
# once none is left to compare, the job lets those facts go (``powerbi_compare``)
LOCATION_READERS = frozenset({"locations", "dq_list", "disaggregations"})
OWED_LABELS = frozenset({"overdue", "due", "not yet due"})
# a report's steps: one Power BI is further along has moved on since NeuroDB's sync
STEPS = {
    rc.NOT_YET_DUE: 0,
    rc.DUE: 0,
    rc.OVERDUE: 0,
    rc.SUBMITTED: 1,
    rc.SENT_BACK: 1,
    rc.ACCEPTED: 2,
}
LABEL_STEPS = {"not yet due": 0, "due": 0, "overdue": 0, "submitted": 1, "sent back": 1, "accepted": 2}
CHART_COLUMNS = {  # a counted visual's column -> NeuroDB's status
    "accepted": rc.ACCEPTED,
    "submitted": rc.SUBMITTED,
    "sent_back": rc.SENT_BACK,
    "due": rc.DUE,
    "overdue": rc.OVERDUE,
    "not_yet_due": rc.NOT_YET_DUE,
}
Reason = tuple[str, str, str]  # (reason, rule, detail)
# Power BI's filter fields each kind's comparer applies, beyond the country and the PD statuses (every kind's)
# and Power BI's own fields NeuroDB reads as it reads them: a clause on another field is one NeuroDB cannot
# apply to that export (a difference may come from it, and What to do now asks to clear it)
REPORTS_APPLY = frozenset({"reporting_year", "section_name", "report_status_label"})
INDICATORS_APPLY = frozenset({"reporting_year", "section_name", "actual_value"})
APPLIES: dict[str, frozenset[str]] = {
    "program_performance": frozenset({"reporting_year", "section_name", "report_status_label"}),
    **dict.fromkeys(("partners_count", "partners_reported", "partners_not_reported"), REPORTS_APPLY),
    "partner_cards": REPORTS_APPLY,
    "follow_up": REPORTS_APPLY,
    **dict.fromkeys(("accepted_report", "submitted_report", "due_report", "overdue_report"), REPORTS_APPLY),
    "by_sector": REPORTS_APPLY,
    "by_month": REPORTS_APPLY,
    "dq_card": INDICATORS_APPLY,
    **dict.fromkeys(
        (
            "pd_count",
            "active_pds",
            "ended_pds",
            "dq_total",
            "dq_with_reached",
            "dq_with_target",
            "dq_without_target",
        ),
        frozenset({"reporting_year", "section_name"}),
    ),
    "missing_targets": frozenset({"reporting_year", "section_name"}),
    "dq_list": INDICATORS_APPLY,
    "pd_indicators": frozenset({"reporting_year", "section_name"}),
    "report_list": REPORTS_APPLY,
    "locations": REPORTS_APPLY | {"location_value"},
    "disaggregations": frozenset({"reporting_year", "section_name"}),
    "fund_monitoring": frozenset(),  # NeuroDB's funds pages have no year and no section
    "funded_programmes": frozenset(),
}
EVERY_KIND_APPLIES = frozenset({"country_name", "status"})
# the kinds that read one year's figure (Reached in the year): several years are a filter they cannot apply
ONE_YEAR = frozenset(
    {
        "program_performance",
        "dq_card",
        "dq_list",
        "pd_indicators",
        "missing_targets",
        "pd_count",
        "active_pds",
        "ended_pds",
        "dq_total",
        "dq_with_reached",
        "dq_with_target",
        "dq_without_target",
    }
)
# the kinds whose NeuroDB page has no year: the comparison applies it to Reached itself
YEAR_WHILE_COMPARING = frozenset({"pd_indicators"})
# PDs whose indicators of every status are read at once, why a Power BI row has none on the page (``_titles``)
TITLE_PDS = 50


def clause_states(kind: str, parsed: Filter) -> Filter:
    """Each clause's state for this kind of export (``parsed`` changed in place and returned): an applied
    clause on a field the kind's comparer does not apply is one NeuroDB cannot apply, and so are several years
    where the kind reads one year's figure; a year Programmes → Indicators has no filter for is applied while
    comparing (to Reached); Program Performance's report status only when it is Accepted alone (the
    comparison then keeps the indicators with an Accepted report)."""
    applies = APPLIES.get(kind, frozenset()) | EVERY_KIND_APPLIES
    for clause in parsed.clauses:
        if clause.state != "applied":
            continue
        if clause.field not in applies:
            clause.state = "cannot"
        elif clause.field == "reporting_year" and kind in ONE_YEAR and len(parsed.years) > 1:
            clause.state = "cannot"
        elif clause.field == "reporting_year" and kind in YEAR_WHILE_COMPARING:
            clause.state = "by_comparison"
        elif clause.field == "report_status_label" and kind == "program_performance":
            if [pm.text(s) for s in parsed.report_statuses] != ["accepted"]:
                clause.state = "cannot"
    return parsed


def one_year(parsed: Filter) -> int | None:
    """The year Power BI's filter names, when it names one."""
    return parsed.years[0] if len(parsed.years) == 1 else None


# --------------------------------------------------------------------------------------------- one export
@dataclass
class Job:
    """One export being compared: its kind, its kept rows, Power BI's filter, the comparison's context and the
    result being written."""

    kind: px.Kind
    columns: list[str]
    data: bytes | None
    parsed: Filter
    context: pf.Context
    result: pm.Result
    rows_read: int = 0
    set_aside: int = 0
    neurodb_set_aside: int = 0  # NeuroDB's own rows of periods before its history, set aside the same way
    by_comparison: list[str] = field(default_factory=list)  # filter fields applied while comparing

    def has(self, name: str) -> bool:
        return name in self.columns

    def rows(self) -> Iterator[dict[str, Any]]:
        names = self.columns
        for values in powerbi_read.rows_of(self.data):
            yield dict(zip(names, values, strict=False))

    def line(self, kind: str, why: Reason, **parts: Any) -> pm.Line:
        reason, rule, detail = why
        return pm.Line(kind, reason, self.context.group(reason), rule=rule, detail=detail, **parts)

    def add(self, kind: str, why: Reason, *, row: bool = True, listed: bool = True, **parts: Any) -> None:
        """One line (``listed``: False for one a note of the export states once, counted all the same)."""
        self.result.add(self.line(kind, why, **parts), row=row, listed=listed)

    def row(self, kind: str, match: list[Any]) -> RowGuard:
        """One row compared (``with job.row(kind, match):``), see :class:`RowGuard`."""
        return RowGuard(self, kind, match)

    def present(self, names: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(n for n in names if self.has(n))

    def before_history(
        self, row: dict[str, Any], start: str = "period_start", end: str = "period_end"
    ) -> bool:
        """A row about a period before NeuroDB's history (set aside, never matched)."""
        day = pm.as_date(row.get(start)) or pm.as_date(row.get(end))
        return bool(day and day < self.context.history)

    def note_set_aside(self) -> None:
        if self.set_aside:
            sentence = words.either(self.set_aside, words.NOTE_SET_ASIDE_ONE, words.NOTE_SET_ASIDE)
            self.result.note(sentence.format(n=self.set_aside, history=self.context.history_words))

    def after_history(self, rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        """NeuroDB's own rows, those of a period before its history set aside as Power BI's are (NeuroDB still
        holds them from an earlier sync: compared, they would be only in NeuroDB) and counted."""
        kept = []
        for row in rows:
            if self.before_history(row):
                self.neurodb_set_aside += 1
            else:
                kept.append(row)
        return kept

    def note_neurodb_set_aside(self) -> None:
        if self.neurodb_set_aside:
            n = self.neurodb_set_aside
            sentence = words.either(n, words.NOTE_SET_ASIDE_NEURODB_ONE, words.NOTE_SET_ASIDE_NEURODB)
            self.result.note(sentence.format(n=n, history=self.context.history_words))


# what one value NeuroDB cannot compute with raises while its row is compared (eTools and PRP keep amounts and
# values as text: a huge number, a decimal beyond its precision)
ROW_ERRORS = (ArithmeticError, ValueError, TypeError)


class RowGuard:
    """One row being compared: an error a value of the row causes (:data:`ROW_ERRORS`) makes the row a
    difference of its own, "A value NeuroDB cannot read" (its values left out), and the comparison goes on
    with the next row. Any other error still stops the export's comparison: a database error (its snapshot
    cannot go on after one), a memory error (the export alone fails, once it let go of what it held), a fault
    of the comparer itself."""

    __slots__ = ("job", "kind", "match", "rows")

    def __init__(self, job: Job, kind: str, match: list[Any]) -> None:
        self.job, self.kind, self.match = job, kind, match

    def __enter__(self) -> RowGuard:
        self.rows = self.job.result.counts["rows"]
        return self

    def __exit__(self, kind: Any, error: Any, trace: Any) -> bool:
        if kind is None or not issubclass(kind, ROW_ERRORS):
            return False
        if self.job.result.counts["rows"] == self.rows:  # the row's own line was not added yet
            self.job.add(self.kind, ("unreadable", "", words.DETAIL_UNREADABLE), match=self.match)
        return True


def pick(options: list[Reason], context: pf.Context) -> Reason:
    """The reason of a line whose columns give several (or of a counted visual's gap whose reports give
    several): the one that most needs doing, in the order of ``powerbi_match.GROUP_ORDER`` (needs a look
    first, what NeuroDB cannot show yet before the expected), then the order of ``REASON_ORDER``."""
    order = words.REASON_ORDER
    return min(options, key=lambda o: (pm.GROUP_ORDER.index(context.group(o[0])), order.index(o[0])))


def period_ended(end: Any, day: datetime.date) -> bool:
    """A report's period has ended on ``day`` (no period end: ended), NeuroDB's rule (``reached.status_of``:
    a report is not yet due while its period runs)."""
    found = pm.as_date(end)
    return found is None or found <= day


def owed_reason(
    job: Job,
    detail: str = words.DETAIL_OWED,
    *,
    prp: list[pf.HeldReport] | None = None,
    powerbi: str = "",
    pd: Any = None,
) -> Reason:
    """A report still owed for a period that has ended, that NeuroDB does not list: NeuroDB cannot show it
    yet (group 5) while it does not show PRP's own progress reports. Once it does (``context.owed_shown``),
    PRP's own list may still hold it (``prp``: its records of the report, ``pf.prp_reports``, the latest
    first): still owed there (Due or Overdue), which NeuroDB does not show as it counts each report once, the
    pages showing another record of it (:func:`counted_once_detail`: needs a look, whatever the sync's
    timing); under another status (the status reason, :func:`status_reason`: moved on since Power BI's
    refresh, for one); or only in records no read returned for 7 days (NeuroDB no longer counts them); else
    neither feed has it: not in the Datamart read. For report lines only (the report list, the follow-up list,
    ``pd`` the line's PD)."""
    if not job.context.owed_shown:
        return ("owed", "", detail)
    fresh = [r for r in prp or [] if not r.stale]
    if fresh:
        status = rc.STATUS_LABELS.get(fresh[0].status, fresh[0].status)
        in_prp = words.DETAIL_IN_PRP.format(status=status)
        if fresh[0].status in (rc.DUE, rc.OVERDUE):
            return ("counted_once", "", f"{in_prp} {counted_once_detail(job, pd, fresh[0])}")
        reason, label, said = status_reason(job, fresh, powerbi)
        return (reason, label, " ".join(x for x in (in_prp, said) if x))
    if prp:
        return ("not_in_read", "", words.DETAIL_PRP_STALE)
    return ("not_in_read", "", f"{words.DETAIL_NOT_IN_PRP} {job.context.not_in_read_detail()}")


def counted_once_detail(job: Job, pd: Any, report: pf.HeldReport) -> str:
    """Why the pages do not show a report PRP's own list still owes (``owed.every_owed`` counts each report
    once, and only one the partner report rows do not hold): the rows hold its number for another period, or
    the pages show another version of PRP's list (another period end); else they do not show it (no period
    end, for one)."""
    number = pm.text(report.number)
    others = [
        h
        for h in pf.reports(job.context).of_pd(pd)
        if pm.text(h.number) == number and h.period_end != report.period_end
    ]
    if any(h.source != "prp" for h in others):
        return words.DETAIL_ROWS_OTHER_PERIOD
    if others:
        return words.DETAIL_PRP_OTHER_PERIOD
    return words.DETAIL_PRP_NOT_SHOWN


def _prp_found(
    job: Job, pd: Any, report: Any = None, end: Any = None, due: Any = None
) -> list[pf.HeldReport]:
    """PRP's own records of a report (by number and period end, else by due date), read only once NeuroDB
    shows PRP's reports still owed."""
    if not job.context.owed_shown:
        return []
    held = pf.prp_reports(job.context)
    return held.find(pd, report, end) if report is not None else held.due(pd, due)


def draft_reason(job: Job) -> Reason:
    """A value Power BI shows in a report still owed: the partner's draft, which the eTools Datamart never
    sends (and PRP's progress reports carry no value): NeuroDB cannot show it, whatever it shows of the
    report."""
    return ("owed", "", words.DETAIL_OWED_DRAFT)


def fallback(job: Job, *, only_neurodb: bool = False) -> Reason:
    """No test passed: a filter NeuroDB cannot apply may explain it; a row only NeuroDB has, an export Power
    BI may have cut (it holds exactly 1,000, 30,000 or 150,000 rows: the row may be beyond the cut); else it
    is not explained yet."""
    others = job.parsed.others
    if others:
        return ("filter", "", words.DETAIL_CLAUSE.format(clause=others[0].words or words.TODO_UNREAD))
    if only_neurodb and cut_at(job):
        return ("cut", "", words.ROUND_COUNT.format(n=job.rows_read))
    return ("unknown", "", "")


def cut_at(job: Job) -> bool:
    """Power BI may have cut this export: it holds exactly as many rows as Power BI cuts an export at."""
    return job.rows_read in powerbi_read.ROUND_COUNTS


def neurodb_only(job: Job) -> Reason:
    """Why a row only NeuroDB has, when no test passed (:func:`fallback`)."""
    return fallback(job, only_neurodb=True)


def running(job: Job, pd: pf.Pd) -> bool:
    """A PD NeuroDB's page reads under Power BI's filter (never a draft or cancelled one)."""
    if pd.status in pd_statuses.NOT_A_PARTNERSHIP:
        return False
    return not job.parsed.pd_statuses or pd.status in job.parsed.pd_statuses


def pd_reason(job: Job, number: Any) -> Reason | None:
    """A Power BI row NeuroDB does not list because of its PD: NeuroDB has the PD under a status the filter
    leaves out (check it in eTools), or holds no PD of that number."""
    pd, _amended = pf.pds(job.context).find(number)
    if pd is not None and not running(job, pd):
        return ("pd_not_running", "", words.DETAIL_NOT_RUNNING.format(status=pd.status_label))
    if pd is None:
        return ("not_in_read", "", f"{words.DETAIL_NO_PD} {job.context.not_in_read_detail()}")
    return None


def not_in_read(job: Job) -> Reason:
    return ("not_in_read", "", job.context.not_in_read_detail())


def earlier_step(neurodb: str, powerbi: str) -> bool:
    """NeuroDB's status is an earlier step of the same report than Power BI's (Submitted against Accepted, Due
    against Submitted)."""
    return STEPS.get(neurodb, 0) < LABEL_STEPS.get(pm.text(powerbi), 0)


def later_step(neurodb: str, powerbi: str) -> bool:
    """NeuroDB's status is a later step of the same report than Power BI's (Accepted against Submitted)."""
    label = pm.text(powerbi)
    return label in LABEL_STEPS and STEPS.get(neurodb, 0) > LABEL_STEPS[label]


def owed_detail(context: pf.Context, powerbi: Any, status: str, due: Any) -> str:
    """Why a report still owed has another of the owed words (Due, Overdue, Not Yet Due) in NeuroDB: read for
    another day than Power BI's refresh, Power BI's Overdue on the due date itself, else said plainly."""
    if context.refreshed and context.refreshed != context.day:
        return words.DETAIL_OTHER_DAY
    if pm.text(powerbi) == "overdue" and status == rc.DUE and pm.as_date(due) == context.day:
        return words.DETAIL_DUE_DAY
    return words.DETAIL_OWED_WORDS


def status_reason(job: Job, held: list[pf.HeldReport], powerbi: str) -> Reason:
    """Why NeuroDB has a report under another status than Power BI: NeuroDB behind (its sync ran before Power
    BI's refresh) or ahead (the report moved on since), another owed word, the newest word of a report's rows,
    else not explained yet. A report not yet due is Due in NeuroDB's lists as in Power BI's (the user's
    decision), so Power BI's Due against it is no other status."""
    report = held[0]
    context = job.context
    label = pm.text(powerbi)
    same_label = label == "due" and report.status == rc.NOT_YET_DUE
    # the read the report comes from: the partner reports' sync, or PRP's own list for a report still owed
    # found only there (Context.before_refresh)
    before, after = context.before_refresh(report), context.after_refresh(report)
    if earlier_step(report.status, powerbi) and before:
        detail = words.DETAIL_EARLIER_STEP.format(status=rc.STATUS_LABELS[report.status], powerbi=powerbi)
        return ("older_version", "", detail)
    if later_step(report.status, powerbi) and not before:
        detail = words.DETAIL_MOVED_ON if after else words.DETAIL_MAY_HAVE_MOVED_ON
        return ("moved_on", "", detail)
    if label in OWED_LABELS and report.status in (rc.DUE, rc.OVERDUE, rc.NOT_YET_DUE) and not same_label:
        return ("status", "", owed_detail(context, powerbi, report.status, report.due_date))
    if len({w for w in report.words if w}) > 1:
        return ("status", "", words.DETAIL_NEWEST_WORD)
    return fallback(job)


def section_same(powerbi: Any, *readings: frozenset[str], known: tuple[str, ...]) -> bool:
    found = pm.sections(powerbi, known)
    return any(found == r for r in readings)


def raw_link(pd: Any, report_type: str = "", year: Any = None, context: pf.Context | None = None) -> str:
    """Raw eTools data → Partner reports, filtered to the PD (and the report type and year)."""
    from urllib.parse import urlencode

    if context is not None and context.clean(pd) != str(pd or ""):
        return ""
    query = {"pd": str(pd or "")}
    if report_type:
        query["report_type"] = report_type
    if year:
        query["year"] = str(year)
    return f"{reverse('admin:datamart_raw_source', args=['partner_reports'])}?{urlencode(query)}"


def pd_link(pd_id: Any) -> str:
    return reverse("reports:programme_detail", args=[pd_id]) if pd_id else ""


def month_key(day: Any) -> str:
    found = pm.as_date(day)
    return f"{found:%Y-%m}" if found else ""


def chart_label(status: str) -> str:
    return page.CHART_LABELS.get(status, "")


def powerbi_chart_label(label: Any, period_end: Any, day: datetime.date) -> str:
    """A report list's status as the charts count it: its "Due" is Not Yet Due while the period runs."""
    text = pm.text(label)
    if text == "due":
        end = pm.as_date(period_end)
        return "Not Yet Due" if end and end > day else "Due"
    return {
        "accepted": "Accepted",
        "submitted": "Submitted",
        "sent back": "Sent back",
        "overdue": "Overdue",
    }.get(text, "Not Yet Due" if text == "not yet due" else "")


def explain(
    job: Job,
    direction: str,
    label: str,
    sections: str | None = None,
    month: str | None = None,
    chart_total: dict[int | None, float] | None = None,
) -> Reason:
    """A counted visual's gap, from the report list's lines of the same upload (its rows set aside before
    NeuroDB's history too): the reason that most needs doing among its reports' (:func:`pick`: a gap of 15
    reports before the history and 1 not in the read needs a look), and how many have each. No report behind
    it: the list may be cut (``list_cut``) only when Power BI may have cut it and it holds fewer rows of the
    year than the visual counts (``chart_total``: the visual's reports per year; None, the partner cards:
    the cut alone), else the chart and its own list differ."""
    gaps = job.context.gaps
    if gaps is None:
        return ("see_reports", "", "")
    found: Counter[str] = Counter()
    for (side, status, keyed, when, reason), n in gaps.items():
        if side != direction or status != label:
            continue
        if sections is not None and keyed != sections:
            continue
        if month is not None and when != month:
            continue
        if job.parsed.years and (not when or int(when[:4]) not in job.parsed.years):
            continue
        found[reason] += n
    if not found:
        # the reports behind it may be beyond the list's cut: the list in parts
        if job.context.gaps_cut and _list_short(job, month, chart_total):
            return ("list_cut", "", words.DETAIL_LIST_CUT.format(n=job.context.gaps_cut))
        return ("unknown", "", words.DETAIL_NO_GAP_LINES)
    total = sum(found.values())
    detail = words.either(total, words.DETAIL_GAP_ONE, words.DETAIL_GAP)
    reason = pick([(r, "", "") for r in found], job.context)[0]
    return (reason, "", detail.format(n=total, parts=gap_parts(found)))


def _list_short(job: Job, month: str | None, chart_total: dict[int | None, float] | None) -> bool:
    """The report list holds fewer rows of the gap's year (its month's, else the visual's one year; None:
    every year) than the visual counts reports in it: what it lacks may be beyond its cut. ``chart_total``
    None: not known (the partner cards), the cut alone decides."""
    if chart_total is None:
        return True
    listed = job.context.listed_years or Counter()
    year = int(month[:4]) if month else next(iter(chart_total), None)
    counted = chart_total.get(year, 0)
    rows = listed.get(year, 0) if year is not None else sum(listed.values())
    return rows < counted


def gap_parts(found: Counter) -> str:
    """How many of a gap's reports have each reason, in words ("3 are reports still owed; 1 was not in the
    Datamart read")."""
    out = []
    for reason, n in found.most_common():
        if n == 1:
            out.append(words.DETAIL_GAP_PART_ONE.get(reason, words.DETAIL_GAP_PART_ONE["unknown"]))
        else:
            out.append(words.DETAIL_GAP_PART.get(reason, words.DETAIL_GAP_PART["unknown"]).format(n=n))
    return "; ".join(out)


def counted_gaps(
    job: Job,
    match: list[Any],
    powerbi: dict[str, Any] | None,
    neurodb: dict[str, Any] | None,
    names: tuple[str, ...],
    *,
    sections: str | None = None,
    month: str | None = None,
    rules: dict[str, Reason] | None = None,
    chart_total: dict[int | None, float] | None = None,
    more: dict[str, str] | None = None,
) -> None:
    """One row of a counted visual: each column whose count differs is a line with the reason the report list
    gives for it (``rules``: a column's reason given, ``chart_total``: :func:`explain`'s, ``more``: a column's
    sentence added to its detail); a value missing on one side is 0 when the other side is 0 (the same)."""
    kind = (
        "different"
        if powerbi is not None and neurodb is not None
        else ("only_powerbi" if powerbi is not None else "only_neurodb")
    )
    first = True
    for name in names:
        a = (powerbi or {}).get(name)
        b = (neurodb or {}).get(name)
        if pm.same_count(a, b):
            continue
        status = chart_label(CHART_COLUMNS[name])
        direction = "powerbi" if (pm.number(a) or 0) > (pm.number(b) or 0) else "neurodb"
        why = (rules or {}).get(name) or explain(job, direction, status, sections, month, chart_total)
        extra = (more or {}).get(name) if direction == "powerbi" else None
        if extra:
            why = (why[0], why[1], " ".join(x for x in (why[2], extra) if x))
        job.add(kind, why, row=first, match=match, found={name: [a, b, None]})
        first = False
    if first:
        zero = powerbi is None or neurodb is None
        job.result.same(zero=zero)


# ======================================================================================== PD/SPD Report
REPORT_COLUMNS = (
    "report_status",
    "due_date",
    "period_start",
    "partner",
    "section",
    "document_type",
    "pd_status",
)


def report_list(job: Job) -> None:
    """PD/SPD Report's list against Progress reports' reports. What it tells the exports compared after it is
    built here and set at the end (``Context.reset`` on a failure): its gaps by status, sections and month
    (its rows set aside before NeuroDB's history too, as before the history: the charts count them), its rows
    (:class:`powerbi_facts.Listed`, set aside ones too) and their count per period-end year, and the reason
    each row was given ("held" when paired)."""
    context, parsed = job.context, job.parsed
    held = pf.reports(context)
    powerbi = []
    listed = pf.Listed()
    years: Counter = Counter()
    aside: list[dict[str, Any]] = []
    for row in job.rows():
        end = pm.as_date(row.get("period_end"))
        listed.add(
            {
                "pd": row["pd"],
                "report": row["report"],
                "label": pm.text(row.get("report_status")),
                "period_start": pm.as_date(row.get("period_start")),
                "period_end": end,
                "due": pm.as_date(row.get("due_date")),
                "partner": row.get("partner"),
                "sections": row.get("section"),
            }
        )
        if end is not None:
            years[end.year] += 1
        if job.before_history(row):
            job.set_aside += 1
            aside.append(row)
        else:
            powerbi.append(row)
    job.note_set_aside()
    reasons: dict[tuple[str, str, str], str] = {}
    neurodb = job.after_history(page.reports(parsed, context))
    job.note_neurodb_set_aside()
    from_prp = sum(1 for n in neurodb if n.get("source"))
    if from_prp:  # PRP's own reports still owed, which the pages show once their link is proven
        job.result.note(
            words.either(from_prp, words.NOTE_FROM_PRP_ONE, words.NOTE_FROM_PRP).format(n=from_prp)
        )

    def key_pb(row: dict[str, Any], base: bool = False) -> tuple:
        number = pm.pd_base(row["pd"]) if base else pm.pd_number(row["pd"])
        return (number, pm.text(row["report"]), pm.iso(row["period_end"]))

    def key_nd(row: dict[str, Any], base: bool = False) -> tuple:
        number = pm.pd_base(row["pd"]) if base else pm.pd_number(row["pd"])
        return (number, pm.text(row["report"]), pm.iso(row["period_end"]))

    def score(p: dict[str, Any], n: dict[str, Any]) -> tuple:
        return (pm.same_text(p.get("report_status"), page.LIST_LABELS.get(n["status"])),)

    pairs, left_pb, left_nd = pm.pair(powerbi, neurodb, key_pb, key_nd, score)
    more, left_pb, left_nd = pm.pair(
        left_pb, left_nd, lambda r: key_pb(r, True), lambda r: key_nd(r, True), score
    )
    amended = {id(p) for p, _n in more}
    pairs += more
    # one report Power BI lists twice, with two statuses: the other row pairs with nothing
    paired_keys = Counter(key_pb(p) for p, _n in pairs)
    gaps: Counter = Counter()
    names = job.present(REPORT_COLUMNS)
    for p, n in pairs:
        reasons[key_pb(p)] = "held"
        diffs = report_diffs(job, p, n, names)
        sections = pm.sections_key(n["pd_sections"] or n["sections"])
        month = month_key(n["period_end"])
        if not diffs:
            job.result.same()
            continue
        options = []
        for name in diffs:
            if name == "report_status":
                options.append(
                    status_reason(
                        job,
                        held.find(p["pd"], p["report"], p["period_end"])
                        or [
                            pf.HeldReport(
                                n["pd_id"], n["report_type"], n["report"], None, None, None, n["status"]
                            )
                        ],
                        p["report_status"],
                    )
                )
            elif name in ("due_date", "period_start", "pd_status") and context.sync_before_refresh:
                options.append(("older_version", "", ""))
            elif name == "partner":
                options.append(("rule", "partner_name", words.DETAIL_PARTNER))
            else:
                options.append(fallback(job))
        why = pick(options, context)
        if id(p) in amended:
            why = (why[0], why[1], " ".join(x for x in (why[2], words.DETAIL_AMENDMENT) if x))
        job.add(
            "different",
            why,
            match=[p["pd"], p["report"], p.get("period_end")],
            found={name: report_found(p, n, name) for name in diffs},
            link=raw_link(
                p["pd"],
                n["report_type"],
                pm.as_date(n["period_end"]).year if n["period_end"] else None,
                context,
            ),
        )
        if "report_status" in diffs:
            gaps[
                (
                    "powerbi",
                    powerbi_chart_label(p["report_status"], p["period_end"], context.day),
                    sections,
                    month,
                    why[0],
                )
            ] += 1
            gaps[("neurodb", chart_label(n["status"]), sections, month, why[0])] += 1
    for p in left_pb:
        twice = paired_keys.get(key_pb(p))
        if twice:
            why: Reason = ("status", "", words.DETAIL_TWICE)
        else:
            why = report_missing(job, p, held)
        reasons.setdefault(key_pb(p), why[0])
        job.add(
            "only_powerbi",
            why,
            match=[p["pd"], p["report"], p.get("period_end")],
            found={name: [p.get(name), None, None] for name in names},
            link=raw_link(p["pd"], context=context),
            pd=p["pd"] if why[0] == "pd_not_running" else "",
        )
        sections = pm.sections_key(_powerbi_sections(job, p))
        label = powerbi_chart_label(p["report_status"], p["period_end"], context.day)
        gaps[("powerbi", label, sections, month_key(p["period_end"]), why[0])] += 1
    for n in left_nd:
        why = neurodb_only(job)
        job.add(
            "only_neurodb",
            why,
            match=[n["pd"], n["report"], n["period_end"]],
            found={name: [None, report_value(n, name), None] for name in names},
            link=raw_link(n["pd"], n["report_type"], context=context),
        )
        if why[0] == "cut":
            continue  # beyond a list Power BI may have cut: it explains no counted visual
        sections = pm.sections_key(n["pd_sections"] or n["sections"])
        gaps[("neurodb", chart_label(n["status"]), sections, month_key(n["period_end"]), why[0])] += 1
    # the rows set aside: Power BI's charts count them (NeuroDB holds no report before its history)
    for p in aside:
        label = powerbi_chart_label(p.get("report_status"), p.get("period_end"), context.day)
        sections = pm.sections_key(_powerbi_sections(job, p))
        gaps[("powerbi", label, sections, month_key(p.get("period_end")), "before_history")] += 1
    context.gaps = gaps
    context.gaps_cut = job.rows_read if cut_at(job) else 0
    context.listed, context.listed_years, context.listed_reasons = listed, years, reasons


def _powerbi_sections(job: Job, p: dict[str, Any]) -> frozenset[str]:
    """A report list row's sections, as the counted visuals group them: Power BI's Section, else (an export
    without that column) the sections NeuroDB holds for the PD."""
    if job.has("section"):
        return pm.sections(p.get("section"), job.context.sections)
    pd, _amended = pf.pds(job.context).find(p["pd"])
    return pm.sections(pd.sections if pd else (), job.context.sections)


def report_value(n: dict[str, Any], name: str) -> Any:
    if name == "report_status":
        return page.LIST_LABELS.get(n["status"], "")
    if name == "section":
        return n["section_text"]
    if name == "pd_status":
        return pd_statuses.label(n["pd_status"]) if n["pd_status"] else ""
    return n.get(name)


def report_found(p: dict[str, Any], n: dict[str, Any], name: str) -> list[Any]:
    return [p.get(name), report_value(n, name), None]


def report_diffs(job: Job, p: dict[str, Any], n: dict[str, Any], names: tuple[str, ...]) -> list[str]:
    known = job.context.sections
    out = []
    for name in names:
        a = p.get(name)
        if name == "report_status":
            same = pm.same_text(a, page.LIST_LABELS.get(n["status"]))
        elif name == "section":
            same = section_same(a, n["pd_sections"], n["sections"], known=known)
        elif name == "partner":
            same = pm.partner(a) == pm.partner(n["partner"])
        elif name == "document_type":
            same = pm.document_type(a) == pm.document_type(n["document_type"])
        elif name == "pd_status":
            same = pm.text(a) in (pm.text(n["pd_status"]), pm.text(pd_statuses.label(n["pd_status"])))
        else:
            same = pm.same_date(a, n.get(name))
        if not same:
            out.append(name)
    return out


def report_missing(job: Job, p: dict[str, Any], held: pf.Reports) -> Reason:
    """Why NeuroDB's page does not list a report of Power BI's list: its PD not running, no such PD, a report
    still owed for a period that has ended (one not yet due that NeuroDB lacks was not in the read: the feed
    carries those), not in the Datamart read, the page's section, its status."""
    pd, _amended = pf.pds(job.context).find(p["pd"])
    if pd is not None and not running(job, pd):
        return ("pd_not_running", "", words.DETAIL_NOT_RUNNING.format(status=pd.status_label))
    found = held.find(p["pd"], p["report"], p["period_end"])
    if pd is None:
        return ("not_in_read", "", f"{words.DETAIL_NO_PD} {job.context.not_in_read_detail()}")
    owed = pm.text(p.get("report_status")) in OWED_LABELS
    if owed and not found and period_ended(p.get("period_end"), job.context.day):
        prp = _prp_found(job, p["pd"], p["report"], p["period_end"])
        return owed_reason(job, prp=prp, powerbi=p.get("report_status") or "", pd=p["pd"])
    if not found:
        return not_in_read(job)
    if job.parsed.sections:
        return ("left_out", "section", words.DETAIL_SECTION)
    return status_reason(job, found, p.get("report_status") or "")


# ================================================================================== Partner Performance
def follow_up(job: Job) -> None:
    """Power BI's follow-up list against the Partners tab's (``reporting.FOLLOW_UP_STATUSES``: a report not
    yet due is one of its Due ones, as in Power BI's, by the user's decision), by PD, status and due date."""
    from . import reporting

    context, parsed = job.context, job.parsed
    held = pf.reports(context)
    statuses = set(reporting.FOLLOW_UP_STATUSES)
    wanted = page.wanted_statuses(parsed)
    if wanted is not None:
        statuses &= wanted
    grouped: dict[tuple, dict[str, Any]] = {}
    for report in page.reports(parsed, context):
        if report["status"] not in statuses:
            continue
        label = page.LIST_LABELS[report["status"]]
        key = (pm.pd_number(report["pd"]), pm.text(label), pm.iso(report["due_date"]))
        row = grouped.setdefault(
            key,
            {**report, "label": label, "all_sections": set(), "pd_all_sections": set()},
        )
        row["all_sections"] |= report["sections"]
        row["pd_all_sections"] |= report["pd_sections"]
    neurodb = list(grouped.values())
    powerbi = list(job.rows())

    def key_pb(row: dict[str, Any], base: bool = False) -> tuple:
        pd = pm.pd_base(row["pd"]) if base else pm.pd_number(row["pd"])
        return (pd, pm.text(row["status"]), pm.iso(row["due_date"]))

    def key_nd(row: dict[str, Any], base: bool = False) -> tuple:
        pd = pm.pd_base(row["pd"]) if base else pm.pd_number(row["pd"])
        return (pd, pm.text(row["label"]), pm.iso(row["due_date"]))

    pairs, left_pb, left_nd = pm.pair(powerbi, neurodb, key_pb, key_nd)
    more, left_pb, left_nd = pm.pair(left_pb, left_nd, lambda r: key_pb(r, True), lambda r: key_nd(r, True))
    amended = {id(p) for p, _n in more}
    names = job.present(("partner", "section"))
    for p, n in pairs + more:
        diffs = []
        if "partner" in names and pm.partner(p["partner"]) != pm.partner(n["partner"]):
            diffs.append("partner")
        if "section" in names and not section_same(
            p["section"],
            frozenset(n["pd_all_sections"]),
            frozenset(n["all_sections"]),
            known=context.sections,
        ):
            diffs.append("section")
        if not diffs:
            job.result.same()
            continue
        options = [
            ("rule", "partner_name", words.DETAIL_PARTNER) if d == "partner" else fallback(job) for d in diffs
        ]
        why = pick(options, context)
        if id(p) in amended:
            why = (why[0], why[1], " ".join(x for x in (why[2], words.DETAIL_AMENDMENT) if x))
        found = {
            "partner": [p.get("partner"), n["partner"], None],
            "section": [p.get("section"), n["section_text"], None],
        }
        job.add(
            "different",
            why,
            match=[p["pd"], p["status"], p["due_date"]],
            found={d: found[d] for d in diffs},
            link=raw_link(p["pd"], context=context),
        )
    by_pd_due = defaultdict(list)
    for n in neurodb:
        by_pd_due[(pm.pd_base(n["pd"]), pm.iso(n["due_date"]))].append(n)
    for p in left_pb:
        found = held.due(p["pd"], p["due_date"])
        pd, _amended = pf.pds(context).find(p["pd"])
        label = pm.text(p["status"])
        # the report list's rows of the same PD, status and due date: the periods this list lacks
        group = context.listed.group(p["pd"], label, p["due_date"]) if context.listed is not None else []
        if not found and _follow_up_before_history(context, p, group):
            why: Reason = ("before_history", "", words.DETAIL_BEFORE_HISTORY)
        elif pd is not None and not running(job, pd):
            why = ("pd_not_running", "", words.DETAIL_NOT_RUNNING.format(status=pd.status_label))
        elif pd is None:
            why = ("not_in_read", "", f"{words.DETAIL_NO_PD} {context.not_in_read_detail()}")
        elif label in OWED_LABELS and not found:
            # a Due row whose list periods all still run is a report not yet due: NeuroDB lists those as Due
            # too, and the feed carries them, so it was not in the read; with no list, a row of it without a
            # period end, or one ended, a report still owed
            running_still = bool(group) and all(
                r["period_end"] is not None and r["period_end"] > context.day for r in group
            )
            if label == "due" and running_still:
                why = not_in_read(job)
            else:
                prp = _prp_found(job, p["pd"], due=p["due_date"])
                why = owed_reason(job, prp=prp, powerbi=p["status"], pd=p["pd"])
        elif not found:
            why = not_in_read(job)
        else:
            why = status_reason(job, found, p["status"])
        job.add(
            "only_powerbi",
            why,
            match=[p["pd"], p["status"], p["due_date"]],
            found={name: [p.get(name), None, None] for name in names},
            link=raw_link(p["pd"], context=context),
            pd=p["pd"] if why[0] == "pd_not_running" else "",
        )
    powerbi_by_due: dict[tuple[str, str], Any] = {}
    for p in powerbi:
        powerbi_by_due.setdefault((pm.pd_base(p["pd"]), pm.iso(p["due_date"])), p["status"])
    for n in left_nd:
        mine = (pm.pd_base(n["pd"]), pm.iso(n["due_date"]))
        if mine in powerbi_by_due:
            if n["status"] in (rc.DUE, rc.OVERDUE):
                detail = owed_detail(context, powerbi_by_due[mine], n["status"], n["due_date"])
            else:
                detail = words.DETAIL_NEWEST_WORD
            why = ("status", "", detail)
        elif context.sync_before_refresh:
            why = ("older_version", "", "")
        else:
            why = neurodb_only(job)
        job.add(
            "only_neurodb",
            why,
            match=[n["pd"], n["label"], n["due_date"]],
            found={
                "partner": [None, n["partner"], None],
                "section": [None, n["section_text"], None],
            },
            link=raw_link(n["pd"], context=context),
        )


def _follow_up_before_history(context: pf.Context, p: dict[str, Any], group: list[dict[str, Any]]) -> bool:
    """A follow-up row about reports before NeuroDB's history: its due date before it, or (the report list of
    the same upload having rows of its PD, status and due date) every one of them starting before it. With no
    list, the due date alone; a row of the group starting after the history, or with no start, keeps it."""
    due = pm.as_date(p.get("due_date"))
    if due and due < context.history:
        return True
    return bool(group) and all(
        r["period_start"] is not None and r["period_start"] < context.history for r in group
    )


# a partner card's column -> its status as Power BI's report list writes it (its Due covers not yet due)
CARD_LABELS = {
    "accepted": "accepted",
    "submitted": "submitted",
    "sent_back": "sent back",
    "due": "due",
    "overdue": "overdue",
}


def partner_cards(job: Job) -> None:
    names = job.present(("accepted", "submitted", "sent_back", "due", "overdue"))
    neurodb = page.partners_by_status(job.parsed, job.context)
    more = {}
    for name, n in _partners_before(job).items():
        more[name] = words.plural(n, words.DETAIL_PARTNERS_BEFORE_ONE, words.DETAIL_PARTNERS_BEFORE)
    for row in job.rows():
        counted_gaps(job, [], row, {n: neurodb.get(CHART_COLUMNS[n], 0) for n in names}, names, more=more)


def _partners_before(job: Job) -> dict[str, int]:
    """Per partner card: how many partners' report list rows of its status are all before NeuroDB's history
    (Partner Performance has no year: Power BI counts them, NeuroDB holds none). From the report list's
    Partner column (a count, never a name); none without the report list or that column."""
    listed = job.context.listed
    if listed is None:
        return {}
    every: dict[str, dict[str, list[bool]]] = defaultdict(lambda: defaultdict(list))
    history = job.context.history
    for rows in listed.by_number.values():
        for row in rows:
            name = pm.partner(row.get("partner"))
            if not name:
                continue
            day = row["period_start"] or row["period_end"]
            every[row["label"]][name].append(bool(day and day < history))
    out = {}
    for column, label in CARD_LABELS.items():
        n = sum(1 for marks in every.get(label, {}).values() if marks and all(marks))
        if n:
            out[column] = n
    return out


# ===================================================================================== Reporting Progress
def progress_counts(reports: list[dict[str, Any]], by: Callable[[dict[str, Any]], str]) -> dict[str, Counter]:
    """Progress reports (one per report) per ``by`` and status, as Power BI's Reporting Progress and NeuroDB's
    count them."""
    out: dict[str, Counter] = defaultdict(Counter)
    for report in reports:
        out[by(report)][report["status"]] += 1
    return out


def by_sector(job: Job) -> None:
    context = job.context
    names = ("accepted", "submitted", "due", "overdue")
    powerbi: dict[str, dict[str, float]] = {}
    joined: Counter[str] = Counter()
    total: dict[str, Any] | None = None
    shown: dict[str, str] = {}
    for row in job.rows():
        if pm.text(row["section"]) == "total":
            total = row
            continue
        key = pm.sections_key(pm.sections(row["section"], context.sections))
        mine = powerbi.setdefault(key, dict.fromkeys(names, 0.0))
        for name in names:
            mine[name] += pm.number(row.get(name)) or 0.0
        joined[key] += 1
        shown.setdefault(key, row["section"])
    if sum(n - 1 for n in joined.values() if n > 1):
        job.result.note(words.NOTE_JOINED_SECTIONS.format(n=sum(n for n in joined.values() if n > 1)))
    job.result.note(words.NOTE_SECTOR_SECTIONS)
    reports = page.reports(job.parsed, context)
    counts = progress_counts(reports, lambda r: pm.sections_key(r["pd_sections"] or r["sections"]))
    neurodb = {key: {n: c.get(CHART_COLUMNS[n], 0) for n in names} for key, c in counts.items()}
    # the reports the table counts, in its one year (None: several or every year)
    chart_total = {one_year(job.parsed): sum(sum(r.values()) for r in powerbi.values())}
    for key in sorted(set(powerbi) | set(neurodb)):
        a, b = powerbi.get(key), neurodb.get(key)
        counted_gaps(job, [shown.get(key) or key], a, b, names, sections=key, chart_total=chart_total)
    if total is not None:  # checked against Power BI's own rows and NeuroDB's total: notes, never a line
        caption = px.KINDS["by_sector"].column
        sums = {n: sum(r[n] for r in powerbi.values()) for n in names}
        off = [caption(n).caption for n in names if not pm.same_count(total.get(n), sums[n])]
        if off:
            job.result.note(words.NOTE_TOTAL_GAP.format(columns=", ".join(off)))
        every = {n: sum(r[n] for r in neurodb.values()) for n in names}
        off = [caption(n).caption for n in names if not pm.same_count(total.get(n), every[n])]
        if off:
            job.result.note(words.NOTE_TOTAL_NEURODB.format(columns=", ".join(off)))


MONTH_COLUMNS = ("accepted", "submitted", "overdue", "due", "not_yet_due", "sent_back")


def by_month(job: Job) -> None:
    """The month chart: Power BI writes a status's column only when its data has that status, so a column the
    export lacks is Power BI's 0, and NeuroDB's reports in that status are compared against it."""
    context = job.context
    present = job.present(MONTH_COLUMNS)
    names = MONTH_COLUMNS
    powerbi = {row["month"]: row for row in job.rows() if row.get("month")}
    reports = page.reports(job.parsed, context)
    counts = progress_counts(reports, lambda r: f"{r['period_end']:%b %Y}" if r["period_end"] else "")
    neurodb = {m: {n: c.get(CHART_COLUMNS[n], 0) for n in names} for m, c in counts.items() if m}
    absent = [n for n in names if n not in present and any(r[n] for r in neurodb.values())]
    if absent:
        captions = ", ".join(job.kind.column(n).caption for n in absent)
        job.result.note(words.NOTE_NO_STATUS_COLUMN.format(columns=captions))
    rules: dict[str, Reason] = {}
    if "accepted" in present:
        a = sum(pm.number(r.get("accepted")) or 0 for r in powerbi.values())
        b = sum(r["accepted"] for r in neurodb.values())
        if pm.same_count(a, b):
            rules["accepted"] = ("rule", "month", words.NOTE_MONTH_TOTALS)

    def order(month: str) -> tuple:
        try:
            return (0, datetime.datetime.strptime(month, "%b %Y"))
        except ValueError:
            return (1, month)

    # the reports the chart counts in each year, over its own status columns
    chart_total: dict[int | None, float] = Counter()
    for month, row in powerbi.items():
        found = order(month)
        if found[0] == 0:
            chart_total[found[1].year] += sum(pm.number(row.get(n)) or 0 for n in present)
    for month in sorted(set(powerbi) | set(neurodb), key=order):
        a, b = powerbi.get(month), neurodb.get(month)
        found = order(month)
        key = f"{found[1]:%Y-%m}" if found[0] == 0 else None
        counted_gaps(job, [month], a, b, names, month=key, rules=rules, chart_total=chart_total)


# =========================================================================================== Data Quality
def dq_card(job: Job) -> None:
    context = job.context
    neurodb = len(page.no_reached(job.parsed, context))
    job.result.note(words.NOTE_DQ_CARD)
    for row in job.rows():
        a = row.get("indicators")
        if pm.same_count(a, neurodb):
            job.result.same()
            continue
        job.add("different", dq_reason(job), match=[], found={"indicators": [a, neurodb, None]})


def dq_reason(job: Job, *, with_reached: bool = False) -> Reason:
    """Why a Data Quality card of indicators without (or with) Reached differs, from the list of the same
    upload: both sides of its gap, the indicators only NeuroDB lists and those only Power BI lists, the reason
    that most needs doing among them (:func:`pick`). The list counts indicators without Reached: the card
    without Reached says "more in NeuroDB" of them; the card with Reached (``with_reached``) names the list,
    as one more without Reached in NeuroDB is one fewer with Reached there."""
    dq = job.context.dq
    if dq is None:
        return ("see_dq_list", "", "")
    ours: Counter = dq.get("neurodb") or Counter()
    theirs: Counter = dq.get("powerbi") or Counter()
    found = ours + theirs
    if not found:
        return ("unknown", "", "")
    nd, pb = sum(ours.values()), sum(theirs.values())
    sentence = words.DETAIL_DQ_WITH if with_reached else words.DETAIL_DQ_BOTH
    detail = sentence.format(nd=nd, pb=pb, net=nd - pb, parts=gap_parts(found))
    return (pick([(r, "", "") for r in found], job.context)[0], "", detail)


def missing_targets(job: Job) -> None:
    """Data Quality's Indicators with Missing Targets against Data quality's indicators without a target (a
    target of 0 counts as none), by PD and title. A row only Power BI has whose indicator NeuroDB holds with a
    target differs in its target; one NeuroDB holds without a target, outside the filter's sections by its own
    section but inside them by its PD's, is the section rule (counted in a note, as on Data Quality's list);
    one NeuroDB holds otherwise is not explained yet, never "not in the read". A row only NeuroDB has, inside
    the filter's sections by its own section but not by its PD's, is the section rule too."""
    context, parsed = job.context, job.parsed
    neurodb = page.missing_targets(parsed, context)
    # every indicator NeuroDB holds, any target and section (one with a target first), by PD and title
    held: dict[tuple, dict[str, Any]] = {}
    for n in page.indicators(parsed, context, view="quality"):
        for key in ((pm.pd_number(n["pd"]), pm.text(n["title"])), (pm.pd_base(n["pd"]), pm.text(n["title"]))):
            if key not in held or (n["target"] and not held[key]["target"]):
                held[key] = n

    def holds(row: dict[str, Any]) -> dict[str, Any] | None:
        title = pm.text(row.get("indicator_title"))
        found = held.get((pm.pd_number(row.get("pd")), title))
        return found if found is not None else held.get((pm.pd_base(row.get("pd")), title))

    def pd_section_only(n: dict[str, Any]) -> bool:
        return bool(parsed.sections) and not page.in_section(n, parsed) and page.in_pd_sections(n, parsed)

    powerbi = list(job.rows())

    def key_pb(row: dict[str, Any], title: str) -> tuple:
        return (pm.pd_number(row.get("pd")), title)

    def key_nd(row: dict[str, Any], title: str) -> tuple:
        return (pm.pd_number(row["pd"]), title)

    pairs, left_pb, left_nd, _amended = title_pair(job, powerbi, neurodb, key_pb, key_nd)
    names = job.present(("target", "section"))
    for p, n in pairs:
        with job.row("different", [p.get("pd"), p.get("indicator_title")]):
            diffs: dict[str, list[Any]] = {}
            if "target" in names and not pm.same_number(p.get("target"), n["target"], zero_is_none=True):
                diffs["target"] = [p.get("target"), n["target"], None]
            if "section" in names and not section_same(
                p.get("section"),
                pm.sections(n["section"], context.sections),
                n["pd_sections"],
                known=context.sections,
            ):
                diffs["section"] = [p.get("section"), n["section"], None]
            if not diffs:
                job.result.same()
                continue
            job.add(
                "different",
                fallback(job),
                match=[p.get("pd"), p.get("indicator_title")],
                found=diffs,
                link=reverse("reports:pd_indicator", args=[n["pd_id"], n["key"]]),
            )
    for p in left_pb:
        with job.row("only_powerbi", [p.get("pd"), p.get("indicator_title")]):
            n = holds(p)
            if n is not None and n["target"]:  # NeuroDB has a target for it
                job.add(
                    "different",
                    older_or_fallback(job),
                    match=[p.get("pd"), p.get("indicator_title")],
                    found={"target": [p.get("target"), n["target"], None]},
                    link=reverse("reports:pd_indicator", args=[n["pd_id"], n["key"]]),
                )
                continue
            if n is not None:  # held without a target, outside the page's section filter
                section = pd_section_only(n)
                job.add(
                    "only_powerbi",
                    ("left_out", "section", words.DETAIL_SECTION) if section else fallback(job),
                    listed=not section,  # only the section differs: a note says it once
                    match=[p.get("pd"), p.get("indicator_title")],
                    found={"target": [p.get("target"), None, None]},
                    link=reverse("reports:pd_indicator", args=[n["pd_id"], n["key"]]),
                )
                continue
            why = pd_reason(job, p.get("pd")) or not_in_read(job)
            job.add(
                "only_powerbi",
                why,
                match=[p.get("pd"), p.get("indicator_title")],
                found={"target": [p.get("target"), None, None]},
                pd=p.get("pd") if why[0] == "pd_not_running" else "",
            )
    for n in left_nd:
        outside = bool(parsed.sections) and not page.in_pd_sections(n, parsed)
        job.add(
            "only_neurodb",
            ("rule", "section", words.DETAIL_SECTION) if outside else neurodb_only(job),
            match=[n["pd"], n["title"]],
            found={"target": [None, n["target"], None]},
            link=reverse("reports:pd_indicator", args=[n["pd_id"], n["key"]]),
        )
    note_sections(job)


# ======================================================================================== the cards
# a card's NeuroDB figure: Partner Performance's (Progress reports → Partners), Reporting Progress's
# (progress reports of the status), Data Quality's (``powerbi_page.quality``)
PARTNER_COUNTS = {
    "partners_count": "partners",
    "partners_reported": "reported",
    "partners_not_reported": "not_reported",
}
CARD_STATUSES = {
    "accepted_report": rc.ACCEPTED,
    "submitted_report": rc.SUBMITTED,
    "due_report": rc.DUE,
    "overdue_report": rc.OVERDUE,
}
DQ_CARDS = (
    "pd_count",
    "active_pds",
    "ended_pds",
    "dq_total",
    "dq_with_reached",
    "dq_with_target",
    "dq_without_target",
)


def _card(job: Job, neurodb: Any, why: Callable[[Any], Reason], alternative: Any = None) -> None:
    """A card: its one figure against NeuroDB's; a difference takes ``why(Power BI's value)``, and shows
    ``alternative`` (NeuroDB's figure counted Power BI's way) when there is one."""
    for row in job.rows():
        a = row.get("count")
        if pm.same_count(a, neurodb):
            job.result.same()
            continue
        job.add("different", why(a), match=[], found={"count": [a, neurodb, alternative]})


def partner_count(job: Job) -> None:
    """Partner Performance's cards against Progress reports → Partners (the partners with a report)."""
    figures = page.partner_performance(job.parsed, job.context)
    _card(job, figures[PARTNER_COUNTS[job.kind.key]], lambda _a: fallback(job))


def status_card(job: Job) -> None:
    """Reporting Progress's cards against the Progress reports page's tiles: the progress reports of a status,
    explained from the report list of the same upload, as the table by sector is."""
    context = job.context
    status = CARD_STATUSES[job.kind.key]
    neurodb = sum(1 for r in page.reports(job.parsed, context) if r["status"] == status)

    def why(powerbi: Any) -> Reason:
        direction = "powerbi" if (pm.number(powerbi) or 0) > neurodb else "neurodb"
        total = {one_year(job.parsed): pm.number(powerbi) or 0}
        return explain(job, direction, chart_label(status), chart_total=total)

    _card(job, neurodb, why)


def dq_figure(job: Job) -> None:
    """Data Quality's cards of programme documents and indicators (``powerbi_page.quality``). Under a Section
    filter, a card Power BI counts by the PD's sections (``quality(by_pd=True)``: NeuroDB's page reads the
    indicator's own) is the section rule; else its card with Reached is explained from the list of the same
    upload, as its card without Reached is."""
    key, parsed = job.kind.key, job.parsed
    neurodb = page.quality(parsed, job.context)[key]
    by_pd = page.quality(parsed, job.context, by_pd=True)[key] if parsed.sections else None
    by_pd = None if by_pd == neurodb else by_pd

    def why(powerbi: Any) -> Reason:
        if by_pd is not None and pm.same_count(powerbi, by_pd):
            return ("left_out", "section", words.DETAIL_SECTION)
        return dq_reason(job, with_reached=True) if key == "dq_with_reached" else fallback(job)

    _card(job, neurodb, why, by_pd)


def title_pair(
    job: Job,
    powerbi: list[dict[str, Any]],
    neurodb: list[dict[str, Any]],
    key_pb: Callable[[dict[str, Any], str], Any],
    key_nd: Callable[[dict[str, Any], str], Any],
    score: Callable[[Any, Any], tuple] | None = None,
) -> tuple[list[tuple[Any, Any]], list[Any], list[Any], set[int]]:
    """Pairs by the kind's match values with the title in them (``key_*(row, title)``): first the whole title,
    then a title Power BI cut at 255 characters against NeuroDB's that starts with it, then the PD # without
    its amendment suffix. Returns the pairs, the rows left over and the Power BI rows whose PD # only matched
    so."""
    cut_name = "indicator_title"
    pairs, left_pb, left_nd = pm.pair(
        powerbi,
        neurodb,
        lambda r: key_pb(r, pm.text(r.get(cut_name))),
        lambda r: key_nd(r, pm.text(r["title"])),
        score,
    )
    cut = [p for p in left_pb if p.get(f"{cut_name}__cut")]
    if cut and left_nd:
        rest = [p for p in left_pb if not p.get(f"{cut_name}__cut")]
        found, cut, left_nd = pm.pair(
            cut,
            left_nd,
            lambda r: key_pb(r, ""),
            lambda r: key_nd(r, ""),
            lambda p, n: (
                (pm.same_text(p.get(cut_name), n["title"], cut=True),) + (score(p, n) if score else ())
            ),
        )
        good = [(p, n) for p, n in found if pm.same_text(p.get(cut_name), n["title"], cut=True)]
        bad = [(p, n) for p, n in found if not pm.same_text(p.get(cut_name), n["title"], cut=True)]
        pairs += good
        left_pb = rest + cut + [p for p, _n in bad]
        left_nd = left_nd + [n for _p, n in bad]
        if good:
            job.result.note(words.NOTE_CUT)
    amended: set[int] = set()
    if left_pb and left_nd and job.has("pd"):

        def base_pb(r: dict[str, Any]) -> Any:
            found = key_pb(r, pm.text(r.get(cut_name)))
            return (pm.pd_base(r["pd"]), *found[1:]) if found else None

        def base_nd(r: dict[str, Any]) -> Any:
            found = key_nd(r, pm.text(r["title"]))
            return (pm.pd_base(r["pd"]), *found[1:]) if found else None

        more, left_pb, left_nd = pm.pair(left_pb, left_nd, base_pb, base_nd, score)
        pairs += more
        amended = {id(p) for p, _n in more}
    return pairs, left_pb, left_nd, amended


def dq_list(job: Job) -> None:
    """Data Quality's list against Partner monitoring → Data quality's "without or with zero Reached", with
    Power BI's possible readings of its filter ranked by the indicators they agree on (in on both sides, or
    out on both). An indicator only NeuroDB lists is, in this order: one eTools marks inactive with no report
    row, a copy of another of its PD's indicators (same title and PD output) Power BI lists or that has the
    year's rows with a value (:func:`_has_value`; never a copy NeuroDB lists too: Power BI then lists none of
    them), one whose report of the year was not in the Datamart read (the location export names it
    under that report), one of a PD that ends after the year (Power BI lists an indicator only in its PD's
    last year: only while Power BI lists no indicator of such a PD), one with no report in the year, one with
    draft values, one outside the PD's sections, else not explained yet."""
    context, parsed = job.context, job.parsed
    year = parsed.years[0] if len(parsed.years) == 1 else None
    every = page.indicators(parsed, context, view="quality")  # every section: the readings need them all
    facts = pf.indicator_facts({r["pd_id"] for r in every}, context.day, context)
    inactive = pf.inactive(context, {r["pd_id"] for r in every})
    held_pds = pf.pds(context)
    powerbi = list(job.rows())

    def key_pb(row: dict[str, Any], title: str) -> tuple:
        return (pm.pd_number(row["pd"]), title, pm.text(row.get("pd_output")))

    def key_nd(row: dict[str, Any], title: str) -> tuple:
        return (pm.pd_number(row["pd"]), title, pm.text(row["output"]))

    pairs, left_pb, left_nd, _amended = title_pair(job, powerbi, every, key_pb, key_nd)
    of_powerbi = {id(n): p for p, n in pairs}
    # Power BI lists an indicator only in its PD's last year: never said while it lists an indicator of a PD
    # that ends after the year (or of one NeuroDB cannot tell: none, or two of that number)
    pd_year = (
        year is not None
        and not any(_ends_after(held_pds.by_id.get(n["pd_id"]), year) for _p, n in pairs)
        and not any(_ends_after(held_pds.find(p["pd"])[0], year) for p in left_pb)
    )

    def flags(n: dict[str, Any]) -> dict[str, bool]:
        fact = facts.get(n["ident"]) or pf.IndicatorFact(n["ident"])
        value = n["reached_in_year"] if year else n["reached"]
        every_value = fact.value(year)
        own = page.in_section(n, parsed)
        pd_in = not parsed.sections or bool({pm.text(s) for s in parsed.sections} & n["pd_sections"])
        none = value is None or value == 0
        empty = fact.empty_row(year)
        pd = held_pds.by_id.get(n["pd_id"])
        return {
            "own": own,
            "pd": pd_in,
            "row": bool(fact.in_year(year)),
            "none": none,
            "every_none": every_value is None or every_value == 0,
            "empty": empty,
            "draft": none and not (every_value is None or every_value == 0),
            "pd_year": bool(pd_year and pd is not None and pd.end is not None and pd.end.year == year),
            "later": bool(pd_year and pd is not None and pd.end is not None and pd.end.year > year),
            "no_rows": not fact.every,
        }

    readings = {
        "dq_neurodb": lambda f: f["own"] and f["none"],
        "dq_pd_section": lambda f: f["pd"] and f["none"],
        "dq_needs_row": lambda f: f["own"] and f["row"] and f["none"],
        "dq_every_status": lambda f: f["own"] and f["every_none"],
        "dq_per_row": lambda f: f["own"] and (f["none"] or f["empty"]),
        "dq_all": lambda f: f["pd"] and f["row"] and (f["every_none"] or f["empty"]),
        "dq_pd_year": lambda f: f["own"] and f["none"] and f["pd_year"],
    }
    marks = {id(n): flags(n) for n in every}
    listed = {id(n) for _p, n in pairs}
    unmatched = len(left_pb)
    table = []
    for name, test in readings.items():
        chosen = {id(n) for n in every if test(marks[id(n)])}
        # agreement over every indicator: in on both sides, and out on both (a looser reading that lists
        # what Power BI does not loses as much as it gains)
        agreed = len(chosen & listed) + len(every) - len(chosen | listed)
        table.append(
            {"candidate": name, "fits": agreed, "of": len(every) + unmatched, "above": 0, "chosen": False}
        )
    if table:
        best = max(table, key=lambda t: (t["fits"], -list(readings).index(t["candidate"])))
        best["chosen"] = True
        job.result.fits.append({"title": "dq", "rows": table, "set": True})
    # each PD indicator's copies: the same PD, raw title and PD output
    copies: dict[tuple, list[dict[str, Any]]] = defaultdict(list)
    for n in every:
        copies[(n["pd_id"], n["title"], n["output"])].append(n)
    gaps = {"powerbi": Counter(), "neurodb": Counter()}
    neurodb_list = [n for n in every if readings["dq_neurodb"](marks[id(n)])]
    names = job.present(("reached", "cp_output", "section"))
    for n in every:
        p = of_powerbi.get(id(n))
        f = marks[id(n)]
        in_neurodb = readings["dq_neurodb"](f)
        if p is None and not in_neurodb:
            continue
        link = reverse("reports:pd_indicator", args=[n["pd_id"], n["key"]])
        if p is None:
            others = [c for c in copies[(n["pd_id"], n["title"], n["output"])] if c is not n]
            if n["ident"] in inactive and f["no_rows"]:
                why: Reason = ("inactive", "", words.DETAIL_INACTIVE)
            elif any(id(c) in listed or (marks[id(c)]["row"] and _has_value(marks[id(c)])) for c in others):
                why = ("rule", "canonical", words.DETAIL_CANONICAL)
            elif _report_not_in_read(context, n, year):
                why = not_in_read(job)
            elif f["later"]:
                why = ("rule", "pd_year", words.DETAIL_PD_YEAR)
            elif not f["row"]:
                why = ("rule", "no_report_in_year", "")
            elif f["draft"]:
                why = ("rule", "draft_values", "")
            elif f["own"] and not f["pd"]:
                why = ("rule", "section", words.DETAIL_SECTION)
            else:
                why = neurodb_only(job)
            job.add(
                "only_neurodb",
                why,
                match=[n["pd"], n["title"], n["output"]],
                found={"reached": [None, n["reached_in_year"] if year else n["reached"], None]},
                link=link,
            )
            gaps["neurodb"][why[0]] += 1
            continue
        if not in_neurodb:
            listed_line = True
            if not running(job, pf.pds(context).by_id.get(n["pd_id"]) or _no_pd()):
                why = ("pd_not_running", "", "")
            elif f["pd"] and not f["own"]:
                why = ("left_out", "section", words.DETAIL_SECTION)
                # only the section differs (NeuroDB has no Reached either): a note says it once
                listed_line = not f["none"]
            elif f["empty"]:
                why = ("rule", "per_row", "")
            elif not f["every_none"] and context.sync_before_refresh:
                why = ("older_version", "", "")
            else:
                why = fallback(job)
            value = n["reached_in_year"] if year else n["reached"]
            job.add(
                "only_powerbi",
                why,
                listed=listed_line,
                match=[p["pd"], p["indicator_title"], p.get("pd_output")],
                found={"reached": [p.get("reached"), value, None]},
                link=link,
                pd=p["pd"] if why[0] == "pd_not_running" else "",
            )
            gaps["powerbi"][why[0]] += 1
            continue
        diffs = {}
        value = n["reached_in_year"] if year else n["reached"]
        if "reached" in names and not pm.same_number(p.get("reached"), value, zero_is_none=True):
            diffs["reached"] = [p.get("reached"), value, None]
        if "cp_output" in names and not (
            pm.same_text(p.get("cp_output"), n["cp_output"])
            or pm.cp_outputs_fit(p.get("cp_output"), n["pd_cp_outputs"])
        ):
            diffs["cp_output"] = [p.get("cp_output"), n["cp_output"], None]
        if "section" in names and not section_same(
            p.get("section"),
            pm.sections(n["section"], context.sections),
            n["pd_sections"],
            known=context.sections,
        ):
            diffs["section"] = [p.get("section"), n["section"], None]
        if not diffs:
            job.result.same()
            continue
        job.add(
            "different",
            fallback(job),
            match=[p["pd"], p["indicator_title"], p.get("pd_output")],
            found=diffs,
            link=link,
        )
    for p in left_pb:
        why = pd_reason(job, p["pd"]) or not_in_read(job)
        job.add(
            "only_powerbi",
            why,
            match=[p["pd"], p["indicator_title"], p.get("pd_output")],
            found={"reached": [p.get("reached"), None, None]},
            pd=p["pd"] if why[0] == "pd_not_running" else "",
        )
        gaps["powerbi"][why[0]] += 1
    if any(p.get("reached") == 0 for p in powerbi):
        job.result.note(words.NOTE_ZERO)
    note_sections(job)
    context.dq = {"neurodb": len(neurodb_list), **gaps}
    del left_nd


def _has_value(f: dict[str, bool]) -> bool:
    """A copy of an indicator has a value in the year (Accepted or Submitted, or a draft's, which Power BI may
    count): the one copy Power BI keeps has a Reached, so its list holds none of them."""
    return not f["none"] or not f["every_none"]


def _ends_after(pd: pf.Pd | None, year: int) -> bool:
    """A PD that ends after ``year``, or may (NeuroDB holds none of that number, two, or it has no end)."""
    return pd is None or pd.end is None or pd.end.year > year


def _report_not_in_read(context: pf.Context, n: dict[str, Any], year: int | None) -> bool:
    """An indicator only NeuroDB lists whose report of the year is missing from the Datamart read: the
    location export names it under a report of the year (its PD and title) that the report list's line says
    was not in the read. Without the location export or the report list: never (today's reason)."""
    titles, reasons = context.location_titles, context.listed_reasons
    if titles is None or reasons is None:
        return False
    for pd, report, end in titles.get((pm.pd_base(n["pd"]), pm.text(n["title"])), ()):
        if year is not None and not end.startswith(f"{year}-"):
            continue
        if reasons.get((pd, report, end)) == "not_in_read":
            return True
    return False


def note_sections(job: Job) -> None:
    """The lines counted, not listed, because Power BI reads the PD's sections and NeuroDB the indicator's
    own (equal in every other column): one note."""
    n = job.result.noted.get("section", 0)
    if n:
        job.result.note(words.plural(n, words.NOTE_PD_SECTIONS_ONE, words.NOTE_PD_SECTIONS))


def _no_pd() -> pf.Pd:
    return pf.Pd(0, "", "", "", (), (), "", None, None, (), "")


# ============================================================================ Program Performance, PD/SPD
def held_whole(n: dict[str, Any], year: int | None, history: datetime.date) -> bool:
    """NeuroDB holds every report behind an indicator's Reached: its PD started on or after NeuroDB's history,
    or (Power BI's filter naming one year) that year starts on or after it."""
    start = n.get("pd_start")
    if start and start >= history:
        return True
    if year is not None:
        return datetime.date(year, 1, 1) >= history
    return not n.get("partial") and not (start and start < history)


def reached_candidates(fact: pf.IndicatorFact, n: dict[str, Any], year: int | None) -> dict[str, Any]:
    """Power BI's possible Reached of an indicator: to date, or in the one year Power BI's filter names (the
    page's Accepted-only figure then counted for that year, from the reports of the type the page reads)."""
    if year is None:
        return fact.reached(n.get("accepted"))
    return fact.reached(fact.accepted_in(year, n.get("report_type") or ""), year)


def fit_reached(
    job: Job,
    pairs: list[tuple[dict[str, Any], dict[str, Any]]],
    facts: dict[tuple[int, str], pf.IndicatorFact],
    *,
    reached: str = "reached",
    year: int | None = None,
) -> tuple[str, str]:
    """The Reached and Target fit tables on the rows NeuroDB can check whole (:func:`held_whole`), Reached in
    ``year`` when Power BI's filter names one: the candidates chosen."""
    context = job.context
    reached_rows, target_rows, aside = [], [], 0
    for p, n in pairs:
        if not held_whole(n, year, context.history):
            aside += 1
            continue
        fact = facts.get(n["ident"]) or pf.IndicatorFact(n["ident"])
        if job.has(reached):
            reached_rows.append((pm.number(p.get(reached)), reached_candidates(fact, n, year)))
        if job.has("target"):
            target_rows.append((pm.number(p.get("target")), fact.target(n.get("target"))))
    chosen = ["", ""]
    for i, (name, rows, candidates) in enumerate(
        (("reached", reached_rows, pf.REACHED_CANDIDATES), ("target", target_rows, pf.TARGET_CANDIDATES))
    ):
        if not rows and not (aside and job.has(name)):
            continue
        table = pf.fit(rows, candidates)
        chosen[i] = pf.chosen(table)
        job.result.fits.append({"title": name, "rows": table, "set_aside": aside})
    return chosen[0], chosen[1]


def candidate_reason(
    job: Job,
    column: str,
    powerbi: Any,
    neurodb: dict[str, Any],
    candidates: dict[str, Any],
    chosen: str,
    room: int = pm.DETAIL_LIMIT,
    year: int | None = None,
) -> Reason:
    """A Reached or Target difference: Power BI's way when it equals the candidate chosen (the others that fit
    in the detail), a partial history (to date: PRP's cumulative; in a year that starts before NeuroDB's
    history: reports before it), Power BI's Reached from Accepted reports only when no rule was chosen
    (Power BI 0 or empty, NeuroDB's Accepted-only figure fits it, NeuroDB's own above 0: never the rule that
    fits most rows overall, which zeros fit by chance), NeuroDB's older version, else not explained yet. A
    candidate that is NeuroDB's own figure never explains a difference. ``room``: the characters the detail
    may take (the line adds its own sentences after it)."""
    value = neurodb.get(column)
    fitting = [
        name
        for name, found in candidates.items()
        if pf.fits_value(powerbi, found) and not _own_figure(found, value)
    ]
    if column == "reached" and not held_whole(neurodb, year, job.context.history):
        if year is None:
            return ("rule", "partial", "")
        return ("before_history", "", words.DETAIL_BEFORE_HISTORY)
    if chosen and chosen in fitting:
        picked = candidates[chosen]
        detail = words.DETAIL_ALTERNATIVE.format(
            words=words.CANDIDATE_WORDS[chosen],
            alt=_number_words(powerbi if isinstance(picked, list) else picked),
            value=_number_words(value),
        )
        detail = also_fits(detail, [c for c in fitting if c != chosen], room)
        if column == "reached" and neurodb.get("report_type") == "HR" and chosen == "qpr_sum":
            return ("rule", "monthly", detail)
        return ("rule", column, detail)
    if (
        column == "reached"
        and not chosen
        and not pm.number(powerbi)
        and "accepted" in fitting
        and (pm.number(value) or 0) > 0
    ):
        detail = words.DETAIL_ALTERNATIVE.format(
            words=words.CANDIDATE_WORDS["accepted"],
            alt=_number_words(candidates["accepted"] or 0),
            value=_number_words(value),
        )
        return ("rule", "accepted_only", also_fits(detail, [c for c in fitting if c != "accepted"], room))
    if (
        column == "reached"
        and job.context.sync_before_refresh
        and (pm.number(powerbi) or 0) > (pm.number(value) or 0)
    ):
        return ("older_version", "", "")
    if fitting:
        return ("unknown", "", also_fits("", fitting, room))
    return fallback(job)


def also_fits(detail: str, others: list[str], room: int) -> str:
    """``detail`` with the other rules that fit Power BI's value: by their short names, else (past ``room``
    characters) how many, so a detail is never cut."""
    if not others:
        return detail
    names = words.DETAIL_ALSO_FITS.format(words=", ".join(words.CANDIDATE_SHORT[c] for c in others))
    count = words.plural(len(others), words.DETAIL_ALSO_FITS_ONE, words.DETAIL_ALSO_FITS_COUNT)
    for sentence in (names, count):
        found = f"{detail} {sentence}".strip()
        if len(found) <= room:
            return found
    return detail


def _own_figure(candidate: Any, neurodb: Any) -> bool:
    """A candidate that is NeuroDB's own figure (eTools' target against NeuroDB's target): it fits Power BI
    only where Power BI's 0 stands for no value, and never explains a difference."""
    return not isinstance(candidate, list) and pm.same_number(candidate, neurodb, zero_is_none=True)


def _number_words(value: Any) -> str:
    found = pm.number(value)
    if found is None:
        return "no value"
    return f"{found:,.10g}"


def program_performance(job: Job) -> None:
    """Partner monitoring's Program Performance, its Reached to date or, when Power BI's filter names one
    reporting year, in that year (as Power BI's Reporting Year reads it)."""
    context, parsed = job.context, job.parsed
    year = one_year(parsed)
    shown_reached = "reached_in_year" if year else "reached"
    every = [n for n in page.indicators(parsed, context) if page.in_section(n, parsed)]
    facts = pf.indicator_facts({n["pd_id"] for n in every}, context.day, context)
    if [pm.text(s) for s in parsed.report_statuses] == ["accepted"]:
        every = [n for n in every if (facts.get(n["ident"]) or pf.IndicatorFact(n["ident"])).any_accepted]
        job.by_comparison.append("report_status_label")
        job.result.note(words.NOTE_ACCEPTED_ONLY)
    powerbi = list(job.rows())

    def key_pb(row: dict[str, Any], title: str) -> tuple:
        return (pm.partner(row.get("partner")), title)

    def key_nd(row: dict[str, Any], title: str) -> tuple:
        return (pm.partner(row["partner"]), title)

    def score(p: dict[str, Any], n: dict[str, Any]) -> tuple:
        return (
            section_same(p.get("section"), n["pd_sections"], known=context.sections),
            pm.cp_outputs_fit(p.get("cp_output"), n["pd_cp_outputs"]),
            pm.yes_no(p.get("emergency")) == n["emergency"],
            pm.closeness(p.get("target"), n["target"]),
        )

    pairs, left_pb, left_nd, _amended = title_pair(job, powerbi, every, key_pb, key_nd, score)
    pairs, left_pb, left_nd = _joined(job, pairs, left_pb, left_nd, shown_reached)
    # Partner written differently: the title, sections and target tell them apart, when only one fits
    partner_by = set()
    if left_pb and left_nd:

        def alt_pb(r: dict[str, Any]) -> tuple:
            return (
                pm.text(r.get("indicator_title")),
                pm.sections_key(pm.sections(r.get("section"), context.sections)),
                round(pm.number(r.get("target")) or 0, 6),
            )

        def alt_nd(r: dict[str, Any]) -> tuple:
            return (
                pm.text(r["title"]),
                pm.sections_key(r["pd_sections"]),
                round(pm.number(r["target"]) or 0, 6),
            )

        counts = Counter(alt_nd(n) for n in left_nd)
        unique = [n for n in left_nd if counts[alt_nd(n)] == 1]
        others = [n for n in left_nd if counts[alt_nd(n)] != 1]
        more, left_pb, rest = pm.pair(left_pb, unique, alt_pb, alt_nd)
        left_nd = rest + others
        partner_by = {id(p) for p, _n in more}
        pairs += more
    chosen_reached, chosen_target = fit_reached(job, [(p, n) for p, n, *_ in pairs], facts, year=year)
    names = job.present(("target", "reached", "achieved", "emergency", "section", "cp_output"))
    for p, n, *joined in pairs:
        with job.row("different", [p.get("partner"), p.get("indicator_title")]):
            fact = facts.get(n["ident"]) or pf.IndicatorFact(n["ident"])
            target, reached = n["target"], n[shown_reached]
            if joined:
                target, reached = joined[0]
            diffs: dict[str, list[Any]] = {}
            options: list[Reason] = []
            # the characters a detail may take: the partner's sentence comes after it
            room = pm.DETAIL_LIMIT - (len(words.DETAIL_PARTNER) + 1 if id(p) in partner_by else 0)
            if "target" in names and not pm.same_number(p.get("target"), target, zero_is_none=True):
                candidates = fact.target(n["target"])
                diffs["target"] = [p.get("target"), target, _first_fit(p.get("target"), candidates, target)]
                options.append(
                    candidate_reason(
                        job,
                        "target",
                        p.get("target"),
                        {**n, "target": target},
                        candidates,
                        chosen_target,
                        room,
                    )
                )
            if "reached" in names and not pm.same_number(p.get("reached"), reached, zero_is_none=True):
                candidates = reached_candidates(fact, n, year)
                diffs["reached"] = [
                    p.get("reached"),
                    reached,
                    _first_fit(p.get("reached"), candidates, reached),
                ]
                options.append(
                    candidate_reason(
                        job,
                        "reached",
                        p.get("reached"),
                        {**n, "reached": reached},
                        candidates,
                        chosen_reached,
                        room,
                        year,
                    )
                )
            if "achieved" in names and "target" not in diffs and "reached" not in diffs:
                ours = (reached or 0) * 100 / target if target else None
                theirs = pm.number(p.get("achieved"))
                if not pm.same_number(theirs * 100 if theirs is not None else None, ours, zero_is_none=True):
                    diffs["achieved"] = [p.get("achieved"), ours / 100 if ours is not None else None, None]
                    options.append(fallback(job))
            # NeuroDB's Emergency indicator is Power BI's: the PD is an SPD (the user's decision)
            if "emergency" in names and pm.yes_no(p.get("emergency")) != n["emergency"]:
                diffs["emergency"] = [p.get("emergency"), "Yes" if n["emergency"] else "No", None]
                options.append(fallback(job))
            if "section" in names and not section_same(
                p.get("section"),
                n["pd_sections"],
                pm.sections(n["section"], context.sections),
                known=context.sections,
            ):
                diffs["section"] = [p.get("section"), n["pd_section_text"] or n["section"], None]
                options.append(fallback(job))
            if "cp_output" in names and not (
                pm.cp_outputs_fit(p.get("cp_output"), n["pd_cp_outputs"])
                or pm.same_text(p.get("cp_output"), n["cp_output"])
            ):
                diffs["cp_output"] = [
                    p.get("cp_output"),
                    ", ".join(n["pd_cp_outputs"]) or n["cp_output"],
                    None,
                ]
                options.append(fallback(job))
            if not diffs:
                job.result.same()
                continue
            why = pick(options, context)
            detail = [why[2]]
            rule = why[1]
            if joined and why[0] != "rule":
                rule = "joined"
            if id(p) in partner_by:
                detail.append(words.DETAIL_PARTNER)
            job.add(
                "different",
                (why[0], rule, " ".join(d for d in detail if d)),
                match=[p.get("partner"), p.get("indicator_title")],
                found=diffs,
                link=trail_link(job, n),
            )
    if "section" in names:
        job.result.note(words.NOTE_RULE.format(words=words.RULE_WORDS["section"]))
    if "cp_output" in names:
        job.result.note(words.NOTE_RULE.format(words=words.RULE_WORDS["cp_output"]))
    if any(pm.number(p.get("reached")) == 0 or pm.number(p.get("target")) == 0 for p in powerbi):
        job.result.note(words.NOTE_ZERO)
    job.result.note(words.NOTE_FOLLOWS)
    titled = _titles(context, parsed, [p.get("indicator_title") for p in left_pb])
    for p in left_pb:
        with job.row("only_powerbi", [p.get("partner"), p.get("indicator_title")]):
            why = _pp_missing(job, p, titled, facts)
            job.add(
                "only_powerbi",
                why,
                match=[p.get("partner"), p.get("indicator_title")],
                found={name: [p.get(name), None, None] for name in names},
            )
    for n in left_nd:
        job.add(
            "only_neurodb",
            neurodb_only(job),
            match=[n["partner"], n["title"]],
            found={"target": [None, n["target"], None], "reached": [None, n[shown_reached], None]},
            link=trail_link(job, n),
        )


def _first_fit(powerbi: Any, candidates: dict[str, Any], neurodb: Any = None) -> Any:
    """The alternative a line shows: a candidate's value that equals Power BI's (Counted Power BI's way),
    never NeuroDB's own figure."""
    for value in candidates.values():
        if pf.fits_value(powerbi, value) and not _own_figure(value, neurodb):
            return powerbi if isinstance(value, list) else value
    return None


def _joined(job: Job, pairs, left_pb, left_nd, shown_reached: str = "reached"):
    """Power BI joins rows that are the same in every column it shows: one Power BI row whose Reached and
    Target are each the sums of NeuroDB's rows left over under the same partner and title pairs with them
    (``shown_reached``: the Reached compared, to date or in the year)."""
    if not left_nd:
        return pairs, left_pb, left_nd
    groups: dict[tuple, list[dict[str, Any]]] = defaultdict(list)
    for n in left_nd:
        groups[(pm.partner(n["partner"]), pm.text(n["title"]))].append(n)
    for i, (p, n, *_rest) in enumerate(pairs):
        mates = groups.get((pm.partner(n["partner"]), pm.text(n["title"])))
        if not mates:
            continue
        rows = [n, *mates]
        target = sum(pm.number(r["target"]) or 0 for r in rows)
        reached = sum(pm.number(r[shown_reached]) or 0 for r in rows)
        if pm.same_number(p.get("target"), target, zero_is_none=True) and pm.same_number(
            p.get("reached"), reached, zero_is_none=True
        ):
            pairs[i] = (p, n, (target, reached))
            for m in mates:
                left_nd.remove(m)
            groups.pop((pm.partner(n["partner"]), pm.text(n["title"])))
    return pairs, left_pb, left_nd


def _titles(context: pf.Context, parsed: Filter, wanted: Iterable[Any]) -> dict[str, list[dict[str, Any]]]:
    """The indicators NeuroDB holds of these titles, of any PD status, by title (why a Power BI row has none
    on the page): the indicators alone, never their reports (:func:`_pp_missing` reads their PD, section and
    partner, and the facts of the page's own PDs). Read for the PDs that hold one of the titles only (each
    PD's titles first, one small query), :data:`TITLE_PDS` PDs at a time, and only these titles' indicators
    kept: never every PD indicator of every status, which production holds by the hundred thousand, even
    when a title is shared by most PDs (a renewed PD keeps its logframe's titles)."""
    from . import models as dm

    titles = {pm.text(t) for t in wanted}
    if not titles:
        return {}
    pairs = (
        dm.PDIndicator.objects.exclude(intervention=None).order_by().values_list("intervention_id", "title")
    )
    ids = sorted({pd for pd, title in pairs.distinct() if pm.text(context.clean(title)) in titles})
    every = dataclasses.replace(parsed, pd_statuses=[], sections=[])
    found: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for start in range(0, len(ids), TITLE_PDS):
        for n in page.indicators(every, context, pd_ids=ids[start : start + TITLE_PDS], reached=False):
            title = pm.text(n["title"])
            if title in titles:  # another title of the same PD: never kept
                found[title].append(n)
    return found


def _pp_missing(job: Job, p: dict[str, Any], titled, facts) -> Reason:
    """Why NeuroDB's Program Performance has no row for a Power BI row: no indicator of that title, its PD not
    running, the page's section filter, or (Power BI keeps the indicators with an Accepted report) none of its
    reports Accepted in NeuroDB: expected when its PD started before NeuroDB's history (its Accepted reports
    can be older than that)."""
    context = job.context
    found = titled.get(pm.text(p.get("indicator_title"))) or []
    if not found:
        return not_in_read(job)
    held = [pf.pds(context).by_id.get(n["pd_id"]) for n in found]
    if all(pd is not None and not running(job, pd) for pd in held):
        pd = held[0]
        return ("pd_not_running", "", words.DETAIL_NOT_RUNNING.format(status=pd.status_label))
    if job.parsed.sections and not any(page.in_section(n, job.parsed) for n in found):
        return ("left_out", "section", words.DETAIL_SECTION)
    accepted = [n for n in found if (facts.get(n["ident"]) or pf.IndicatorFact(n["ident"])).any_accepted]
    if not accepted and job.by_comparison:
        # the indicators the page would list (the Power BI row's partner's, when NeuroDB has one)
        shown = [n for n, pd in zip(found, held, strict=True) if pd is not None and running(job, pd)]
        shown = [n for n in shown if page.in_section(n, job.parsed)] or shown
        shown = [n for n in shown if pm.partner(n["partner"]) == pm.partner(p.get("partner"))] or shown
        if shown and all(n["partial"] or (n["pd_start"] and n["pd_start"] < context.history) for n in shown):
            return ("before_history", "", words.DETAIL_BEFORE_HISTORY)
        if context.sync_before_refresh:
            return ("older_version", "", words.DETAIL_NONE_ACCEPTED)
        return ("not_in_read", "", words.DETAIL_NONE_ACCEPTED)
    return fallback(job)


def trail_link(job: Job, n: dict[str, Any]) -> str:
    """Program Performance's Reached of the indicator on its figure trail, for Power BI's filter and day;
    without Power BI's sections when they would take the address past a line's (the indicator's Reached is the
    same in every section)."""
    from urllib.parse import urlencode

    from . import monitoring, trail

    key = f"pp.reached.{n['pd_id']}.{n['key']}"
    path = reverse("reports:check_figures", args=["partner-monitoring"])
    statuses = [("pd_status", s) for s in job.parsed.pd_statuses] or [("pd_status", "")]
    year = str(job.parsed.years[0]) if len(job.parsed.years) == 1 else monitoring.ALL_YEARS
    for sections in (job.parsed.sections, []):
        query: list[tuple[str, str]] = [*statuses, ("year", year), ("view", "table")]
        query += [("section", s) for s in sections]
        if job.context.day != monitoring.today():
            query.append(("day", job.context.day.isoformat()))
        query.append(("figure", key))
        link = f"{path}?{urlencode(query)}#{trail.anchor(key)}"
        if len(link) <= pm.LINK_LIMIT:
            break
    return link


PD_INDICATOR_COLUMNS = (
    "pd_status",
    "partner",
    "document_type",
    "pd_title",
    "start",
    "end",
    "offices",
    "section",
    "cp_output",
    "target",
    "reached",
)


def pd_indicators(job: Job) -> None:
    """Programmes → Indicators, with Partner monitoring's Reached (by the indicator's identity): to date, or
    in the one reporting year Power BI's filter names (the page has no year: the comparison applies it to
    Reached). A Power BI row pairs first with an indicator the page's section filter keeps (two copies of a
    title, one in the section). One paired with an indicator outside the section is compared column by
    column all the same: equal in every other column, a note counts it (Power BI reads the PD's sections);
    one that also differs is listed with its own reason. An indicator only NeuroDB lists is one eTools marks
    inactive with no report row, one of a title two PD outputs share (Power BI lists the other), one outside
    the PD's sections, else not explained yet."""
    context, parsed = job.context, job.parsed
    year = one_year(parsed)
    every = page.pd_indicators(parsed, context)
    monitored = {n["ident"]: n for n in page.indicators(parsed, context)}
    facts = pf.indicator_facts({n["pd_id"] for n in every}, context.day, context)
    inactive = pf.inactive(context, {n["pd_id"] for n in every})
    for n in every:
        m = monitored.get(n["ident"]) or {}
        n.update(
            reached=m.get("reached_in_year") if year else m.get("reached"),
            partial=m.get("partial", False),
            accepted=m.get("accepted"),
            key=m.get("key"),
            pd_start=m.get("pd_start"),
            report_type=m.get("report_type", ""),
        )
    powerbi = list(job.rows())
    in_page = {id(n) for n in every if page.in_section(n, parsed)}

    def key_pb(row: dict[str, Any], title: str) -> tuple:
        return (pm.pd_number(row["pd"]), title)

    def key_nd(row: dict[str, Any], title: str) -> tuple:
        return (pm.pd_number(row["pd"]), title)

    def score(p: dict[str, Any], n: dict[str, Any]) -> tuple:
        return (
            id(n) in in_page,  # the copy of a title in the section Power BI's filter names first
            pm.same_number(p.get("target"), n["target"], zero_is_none=True),
            pm.closeness(p.get("target"), n["target"]),
            pm.same_text(p.get("cp_output"), n["cp_output"]),
        )

    pairs, left_pb, left_nd, amended = title_pair(job, powerbi, every, key_pb, key_nd, score)
    chosen_reached, chosen_target = fit_reached(job, pairs, facts, year=year)
    names = job.present(PD_INDICATOR_COLUMNS)
    section_rule = False
    for p, n in pairs:
        with job.row("different", [p["pd"], p["indicator_title"]]):
            fact = facts.get(n["ident"]) or pf.IndicatorFact(n["ident"])
            diffs: dict[str, list[Any]] = {}
            options: list[Reason] = []
            # the room a detail has: the amendment's, Partner monitoring's, the section's sentences after it
            room = pm.DETAIL_LIMIT - len(words.DETAIL_FROM_MONITORING) - 1
            if id(p) in amended:
                room -= len(words.DETAIL_AMENDMENT) + 1
            outside = id(n) not in in_page
            if outside:
                room -= len(words.DETAIL_SECTION) + 1
            for name in names:
                a = p.get(name)
                if name == "pd_status":
                    same = pm.text(a) in (pm.text(n["pd_status"]), pm.text(pd_statuses.label(n["pd_status"])))
                    b = pd_statuses.label(n["pd_status"])
                elif name == "partner":
                    same, b = pm.partner(a) == pm.partner(n["partner"]), n["partner"]
                elif name == "document_type":
                    same, b = pm.document_type(a) == pm.document_type(n["document_type"]), n["document_type"]
                elif name == "pd_title":
                    same, b = pm.same_text(a, n["pd_title"], cut=bool(p.get("pd_title__cut"))), n["pd_title"]
                elif name in ("start", "end"):
                    same, b = pm.same_date(a, n[name]), n[name]
                elif name == "offices":
                    same, b = pm.sections(a) == n["offices"], sorted(n["offices"])
                elif name == "section":
                    own = pm.sections(n["section"], context.sections)
                    same = section_same(a, own, n["pd_sections"], known=context.sections)
                    section_rule = section_rule or (same and pm.sections(a, context.sections) != own)
                    b = n["section"]
                elif name == "cp_output":
                    same = pm.same_text(a, n["cp_output"]) or pm.cp_outputs_fit(a, n["pd_cp_outputs"])
                    b = n["cp_output"]
                elif name == "target":
                    same, b = pm.same_number(a, n["target"], zero_is_none=True), n["target"]
                else:
                    same, b = pm.same_number(a, n["reached"], zero_is_none=True), n["reached"]
                if same:
                    continue
                alt = None
                if name in ("target", "reached"):
                    if name == "target":
                        candidates = fact.target(n["target"])
                    else:
                        candidates = reached_candidates(fact, n, year)
                    alt = _first_fit(a, candidates, b)
                    chosen = chosen_target if name == "target" else chosen_reached
                    options.append(candidate_reason(job, name, a, n, candidates, chosen, room, year))
                elif name == "partner":
                    options.append(("rule", "partner_name", words.DETAIL_PARTNER))
                else:
                    options.append(fallback(job))
                diffs[name] = [a, b, alt]
            if outside and not diffs:
                # the page's section filter leaves it out, and only the section differs: a note says it once
                job.add(
                    "only_powerbi",
                    ("left_out", "section", words.DETAIL_SECTION),
                    listed=False,
                    match=[p["pd"], p["indicator_title"]],
                    found={name: [p.get(name), None, None] for name in names},
                    link=pd_link(n["pd_id"]),
                )
                continue
            if not diffs:
                job.result.same()
                continue
            why = pick(options, context)
            detail = " ".join(
                x
                for x in (
                    why[2],
                    words.DETAIL_AMENDMENT if id(p) in amended else "",
                    words.DETAIL_FROM_MONITORING if "reached" in diffs else "",
                    words.DETAIL_SECTION if outside else "",
                )
                if x
            )
            job.add(
                "different",
                (why[0], why[1], detail),
                match=[p["pd"], p["indicator_title"]],
                found=diffs,
                link=pd_link(n["pd_id"]),
            )
    if section_rule:
        job.result.note(words.NOTE_RULE.format(words=words.RULE_WORDS["section"]))
    note_sections(job)
    if any(pm.number(p.get("reached")) == 0 or pm.number(p.get("target")) == 0 for p in powerbi):
        job.result.note(words.NOTE_ZERO)
    for p in left_pb:
        why = pd_reason(job, p["pd"]) or not_in_read(job)
        job.add(
            "only_powerbi",
            why,
            match=[p["pd"], p["indicator_title"]],
            found={name: [p.get(name), None, None] for name in names},
            pd=p["pd"] if why[0] == "pd_not_running" else "",
        )
    # the outputs Power BI lists each PD's titles under (a title two PD outputs share: it lists one)
    paired_outputs: dict[tuple[int, str], set[str]] = defaultdict(set)
    for _p, n in pairs:
        paired_outputs[(n["pd_id"], n["title"])].add(pm.text(n["output"]))
    lapsed = 0
    for n in left_nd:
        if id(n) not in in_page:
            continue  # the page's section filter leaves it out, as Power BI's does
        fact = facts.get(n["ident"])
        own = pm.sections(n["section"], context.sections)
        twins = paired_outputs.get((n["pd_id"], n["title"]), set())
        if n["ident"] in inactive and (fact is None or not fact.every):
            why = ("inactive", "", words.DETAIL_INACTIVE)
            lapsed += 1
        elif twins and pm.text(n["output"]) not in twins:
            why = ("rule", "shared_output", words.DETAIL_SHARED_OUTPUT)
        elif parsed.sections and not ({pm.text(s) for s in parsed.sections} & n["pd_sections"]) and own:
            why = ("rule", "section", words.DETAIL_SECTION)
        else:
            why = neurodb_only(job)
        job.add(
            "only_neurodb",
            why,
            match=[n["pd"], n["title"]],
            found={"target": [None, n["target"], None], "reached": [None, n["reached"], None]},
            link=pd_link(n["pd_id"]),
        )
    if lapsed:
        job.result.note(words.plural(lapsed, words.NOTE_INACTIVE_ONE, words.NOTE_INACTIVE))


# ============================================================================== PD/SPD Report/Location
class Recorder:
    """The figure trail's interface for ``reporting.location_rows``: with it each location line carries its
    chosen record (the page's values are the same with or without it)."""

    def count(self, name: str, n: int, limit: int | None) -> None:
        return None

    def dropped(self, *args: Any) -> None:
        return None

    def line(self, *args: Any) -> None:
        return None


LOCATION_COLUMNS = (
    "reached",
    "report_status",
    "period_start",
    "period_end",
    "location",
    "submitted_on",
    "target",
)


def location_rows(job: Job) -> tuple[list[dict[str, Any]], int]:
    """NeuroDB's By location rows for Power BI's filter (``reporting.location_rows``), in Power BI's unit (a
    percentage indicator as a fraction), with the target of the record each line reads; and the rows above the
    tab's limit (0: read whole)."""
    from . import models as dm
    from . import reporting

    context = job.context
    data = page.reporting(job.parsed, context)
    wanted = page.wanted_statuses(job.parsed)
    found = reporting.location_rows(data["rows"], data["statuses"], context.day, wanted, record=Recorder())
    chosen = [f["chosen_record_id"] for f in found["rows"] if f.get("chosen_record_id")]
    records: dict[Any, tuple] = {}
    for start in range(0, len(chosen), 5000):
        for pk, datamart_id, target, submitted in dm.ReportedIndicator.objects.filter(
            datamart_id__in=chosen[start : start + 5000]
        ).values_list("pk", "datamart_id", "target", "submission_date"):
            records[datamart_id] = (pk, target, submitted)
    out = []
    for f in found["rows"]:
        pk, target, submitted = records.get(f.get("chosen_record_id"), (None, None, None))
        parsed_target = rc.parts(target)
        target_value = parsed_target.value(f["ratio"], f["percentage"]) if parsed_target else None
        scale = 100.0 if f["percentage"] else 1.0
        value = f["value_in_period"]
        out.append(
            {
                "pd": f["programme_document"],
                "pd_id": f["pd_id"],
                "title": context.clean(f["indicator"]),
                "report": pm.report_number(f["report"], f["report_type"]),
                "report_type": f["report_type"],
                "p_code": f["p_code"],
                "reached": value / scale if value is not None else None,
                "raw_value": value,
                "scale": scale,
                "ratio": f["ratio"],
                "percentage": f["percentage"],
                "status": f["status"],
                "period_start": f["period_start"],
                "period_end": f["period_end"],
                "location": context.clean(f["location"]),
                "submitted_on": submitted,
                "target": target_value / scale if target_value is not None else None,
                "repeated": f["repeated"],
                "records": len(f.get("record_ids") or ()),
                "record_pk": pk,
                "record_ids": tuple(f.get("record_ids") or ()),
                "chosen": f.get("chosen_record_id"),
            }
        )
    return out, found["too_many"]


def locations(job: Job) -> None:
    """PD/SPD Report/Location against By location. What it tells the later exports (the disaggregations,
    which have no PD #, and Data Quality's list) is built from its rows (copies joined) and NeuroDB's, and set
    at the end (``Context.reset`` on a failure)."""
    from . import reporting

    context, parsed = job.context, job.parsed
    names = job.present(LOCATION_COLUMNS)
    # Power BI repeats a location's row once per CP output of the PD: its copies are one row
    seen: dict[tuple, dict[str, Any]] = {}
    copies, differ = 0, set()
    for row in job.rows():
        if job.before_history(row):
            job.set_aside += 1
            continue
        key = (
            pm.pd_number(row["pd"]),
            pm.text(row["report"]),
            pm.text(row["indicator_title"]),
            pm.text(row["p_code"]),
            pm.iso(row.get("period_end")),
        )
        first = seen.get(key)
        if first is None:
            seen[key] = row
            continue
        copies += 1
        differ |= {n for n in names if pm.shown(first.get(n)) != pm.shown(row.get(n))}
    powerbi = list(seen.values())
    texts: dict[str, str] = {}  # one string per distinct text of the facts kept for later exports
    told = _location_index(job, powerbi, texts)
    job.note_set_aside()
    if copies:
        job.result.note(words.NOTE_COPIES.format(n=copies))
    if differ:
        captions = [job.kind.column(n).caption for n in sorted(differ)]
        job.result.note(words.DETAIL_COPIES.format(columns=", ".join(captions)))
    neurodb, too_many = location_rows(job)
    neurodb = _added_up(job.after_history(neurodb))
    job.note_neurodb_set_aside()
    if too_many:
        job.result.note(words.DETAIL_LIMIT.format(limit=reporting.LOCATION_LIMIT))
    held = pf.reports(context)

    def key_pb(row: dict[str, Any], title: str) -> tuple:
        return (pm.pd_number(row["pd"]), pm.text(row["report"]), title, pm.text(row["p_code"]))

    def key_nd(row: dict[str, Any], title: str) -> tuple:
        return (pm.pd_number(row["pd"]), pm.text(row["report"]), title, pm.text(row["p_code"]))

    def score(p: dict[str, Any], n: dict[str, Any]) -> tuple:
        return (
            pm.same_date(p.get("period_end"), n["period_end"]),
            pm.closeness(p.get("reached"), n["reached"]),
        )

    pairs, left_pb, left_nd, amended = title_pair(job, powerbi, neurodb, key_pb, key_nd, score)
    shared = Counter(key_nd(n, pm.text(n["title"])) for n in neurodb)
    if any(v > 1 for v in shared.values()):
        job.result.note(words.NOTE_SHARED_TITLE)
    newest = _newest_values([n for _p, n in pairs if n["records"] > 1])
    added_up = 0
    for p, n in pairs:
        with job.row("different", [p["pd"], p["report"], p["indicator_title"], p["p_code"]]):
            diffs: dict[str, list[Any]] = {}
            options: list[Reason] = []
            for name in names:
                a = p.get(name)
                if name == "reached":
                    same, b = pm.same_number(a, n["reached"], zero_is_none=True), n["reached"]
                elif name == "report_status":
                    label, ours = pm.text(a), n["status"]
                    same = label == pm.text(page.CHART_LABELS[ours]) or (
                        label == "due" and ours == rc.NOT_YET_DUE
                    )
                    b = page.CHART_LABELS[ours]
                elif name == "location":
                    same, b = pm.same_text(a, n["location"]), n["location"]
                elif name == "target":
                    same, b = pm.same_number(a, n["target"]), n["target"]
                else:
                    same, b = pm.same_date(a, n.get(name)), n.get(name)
                if same:
                    continue
                diffs[name] = [a, b, None]
                if name == "reached":
                    options.append(_location_reached(job, a, n, newest))
                elif name == "report_status":
                    report = pf.HeldReport(
                        n["pd_id"], n["report_type"], n["report"], None, None, None, n["status"]
                    )
                    options.append(status_reason(job, [report], a or ""))
                elif name == "target" and _units(a, n["target"]):
                    options.append(("rule", "units", ""))
                else:
                    options.append(older_or_fallback(job))
            if not diffs:
                added_up += bool(n.get("added_up"))
                job.result.same()
                continue
            why = pick(options, context)
            if n.get("added_up") and ("reached" in diffs or "target" in diffs):
                more = words.DETAIL_ADDED_UP.format(n=n["added_up"])
                why = (why[0], why[1], " ".join(x for x in (why[2], more) if x))
            if id(p) in amended:
                why = (why[0], why[1], " ".join(x for x in (why[2], words.DETAIL_AMENDMENT) if x))
            job.add(
                "different",
                why,
                match=[p["pd"], p["report"], p["indicator_title"], p["p_code"]],
                found=diffs,
                link=_record_link(n),
            )
    if added_up:
        job.result.note(words.plural(added_up, words.NOTE_ADDED_UP_ONE, words.NOTE_ADDED_UP))
    if any(pm.number(p.get("reached")) == 0 for p in powerbi):
        job.result.note(words.NOTE_ZERO)
    location_facts = _held_locations(context, {p["pd"] for p in left_pb})
    for p in left_pb:
        found = held.find(p["pd"], p["report"], p.get("period_end"))
        # a report NeuroDB shows only from PRP's own list (the gate open) carries no value: a value Power BI
        # shows in it is the draft too
        with_rows = [r for r in found if r.source != "prp"]
        pd, _amended = pf.pds(context).find(p["pd"])
        where = (
            pm.pd_base(p["pd"]),
            pm.text(p["report"]),
            pm.text(p["indicator_title"]),
            pm.text(p["p_code"]),
        )
        if pd is not None and not running(job, pd):
            why: Reason = ("pd_not_running", "", words.DETAIL_NOT_RUNNING.format(status=pd.status_label))
        elif pd is None:
            why = ("not_in_read", "", f"{words.DETAIL_NO_PD} {context.not_in_read_detail()}")
        elif pm.text(p.get("report_status")) in OWED_LABELS and not with_rows and _location_ended(job, p):
            why = draft_reason(job)  # a value in a report the partner still owes: its draft
        elif not found or where not in location_facts:
            why = not_in_read(job)
        elif too_many:
            why = ("left_out", "", words.DETAIL_LIMIT.format(limit=reporting.LOCATION_LIMIT))
        elif parsed.sections:
            why = ("left_out", "section", words.DETAIL_SECTION)
        else:
            why = fallback(job)
        job.add(
            "only_powerbi",
            why,
            match=[p["pd"], p["report"], p["indicator_title"], p["p_code"]],
            found={name: [p.get(name), None, None] for name in names},
            link=raw_link(p["pd"], context=context),
            pd=p["pd"] if why[0] == "pd_not_running" else "",
        )
    above = parsed.location_value
    for n in left_nd:
        if above is not None and not _passes(n["reached"], above):
            continue  # Power BI keeps only the location values above 0: so does the comparison here
        if job.has("period_end") and period_latest(
            context, told["location_reports"], n["pd"], n["title"], n["report"], n["period_end"]
        ):
            why = ("rule", "period_latest", words.DETAIL_PERIOD_LATEST)
        else:
            why = neurodb_only(job)
        job.add(
            "only_neurodb",
            why,
            match=[n["pd"], n["report"], n["title"], n["p_code"]],
            found={
                "reached": [None, n["reached"], None],
                "report_status": [None, page.CHART_LABELS[n["status"]], None],
            },
            link=_record_link(n),
        )
    share = texts.setdefault
    told["held_locations"] = {
        tuple(
            share(text, text)
            for text in (
                pm.pd_base(n["pd"]),
                pm.text(n["title"]),
                pm.text(n["report"]),
                pm.iso(n["period_end"]),
                pm.text(n["p_code"]),
            )
        )
        for n in neurodb
    }
    for name, value in told.items():
        setattr(context, name, value)


def _location_index(job: Job, powerbi: list[dict[str, Any]], texts: dict[str, str]) -> dict[str, Any]:
    """What the location export tells the later exports (``powerbi_facts.Context``): its rows' PDs by title,
    report, period (start when it has one) and P-code; the reports of each PD, title and period end; each
    PD and title's reports; each report's Report Status. Each distinct text is one string (``texts``, which
    NeuroDB's own location rows share too): they stay until the last export that reads them."""
    share = texts.setdefault
    with_start = job.has("period_start")
    pds: dict[tuple, set[str]] = defaultdict(set)
    every: dict[tuple, set[str]] = defaultdict(set)
    reports: dict[tuple, set[str]] = defaultdict(set)
    titles: dict[tuple, set[tuple[str, str, str]]] = defaultdict(set)
    status: dict[tuple, str] = {}
    for row in powerbi:
        pd, base = pm.pd_number(row["pd"]), pm.pd_base(row["pd"])
        pd, base = share(pd, pd), share(base, base)
        title, report = pm.text(row.get("indicator_title")), pm.text(pm.report_number(row.get("report")))
        title, report = share(title, title), share(report, report)
        end, p_code = pm.iso(row.get("period_end")), pm.text(row.get("p_code"))
        end, p_code = share(end, end), share(p_code, p_code)
        start = pm.iso(row.get("period_start")) if with_start else ""
        start = share(start, start)
        pds[(title, report, start, end, p_code)].add(pd)
        every[(title, report, end, p_code)].add(pd)
        reports[(base, title, end)].add(report)
        titles[(base, title)].add((pd, report, end))
        if row.get("report_status"):
            status.setdefault((pd, report, end), pm.text(row["report_status"]))
    return {
        "location_pds": dict(pds),
        "location_any": dict(every),
        "location_start": with_start,
        "location_reports": dict(reports),
        "location_titles": dict(titles),
        "location_status": status,
    }


def _added_up(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """NeuroDB's location rows, two indicators of one PD that share a title (the same raw title, report,
    P-code and period end) as one row, their values and targets added up: Power BI's location visual has no PD
    Output, and adds them up. Never two titles equal only once their case or spaces are set aside, nor two
    reports, places or PDs. The row added up reads no record's newest value (``records`` 1, ``chosen``
    None)."""
    groups: dict[tuple, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        key = (pm.pd_number(row["pd"]), row["report"], row["title"], pm.text(row["p_code"]))
        groups[(*key, pm.iso(row["period_end"]))].append(row)
    out = []
    for found in groups.values():
        if len(found) == 1:
            out.append(found[0])
            continue

        def total(name: str, rows: list[dict[str, Any]] = found) -> float | None:
            values = [r[name] for r in rows if r[name] is not None]
            return sum(values) if values else None

        out.append(
            {
                **found[0],
                "reached": total("reached"),
                "raw_value": total("raw_value"),
                "target": total("target"),
                "records": 1,
                "record_ids": (),
                "chosen": None,
                "repeated": any(r["repeated"] for r in found),
                "added_up": len(found),
            }
        )
    return out


def _location_ended(job: Job, p: dict[str, Any]) -> bool:
    """A location row's period has ended: its own Period End, else (an export without that column) the
    periods the report list of the same upload gives the PD's report, when they all agree; taken as ended
    (a report owed) when the list has none of its rows, or they disagree."""
    if job.has("period_end"):
        return period_ended(p.get("period_end"), job.context.day)
    listed = job.context.listed
    rows = listed.report(p["pd"], p["report"]) if listed is not None else []
    ended = {period_ended(r["period_end"], job.context.day) for r in rows}
    return ended != {False}


def period_latest(
    context: pf.Context, shown: dict[tuple, set[str]] | None, pd: Any, title: Any, report: Any, end: Any
) -> bool:
    """A row only NeuroDB has that Power BI's ``is_period_latest`` left out: Power BI shows another report of
    the same PD that ends on the same day for that indicator (and none of this one's), and NeuroDB has that
    other report. Power BI keeps one report per indicator, location and period end."""
    found = (shown or {}).get((pm.pd_base(pd), pm.text(title), pm.iso(end)))
    if not found or pm.text(pm.report_number(report)) in found:
        return False
    held = pf.reports(context)
    return any(held.find(pd, other, end) for other in found)


def _units(powerbi: Any, neurodb: Any) -> bool:
    """A factor of 100 left between the two values (a percentage shown as a fraction on one side only)."""
    a, b = pm.number(powerbi), pm.number(neurodb)
    if a is None or b is None or a == 0:
        return False
    return pm.same_number(a * 100, b) or pm.same_number(a, b * 100)


def _passes(value: Any, clause: dict[str, Any]) -> bool:
    """Whether a value passes Power BI's number filter ("location_value is greater than 0")."""
    found = pm.number(value)
    if found is None:
        return bool(clause.get("or_blank"))
    limit = clause["value"]
    return {">": found > limit, ">=": found >= limit, "<": found < limit, "<=": found <= limit}[clause["op"]]


def _record_link(n: dict[str, Any]) -> str:
    if not n.get("record_pk"):
        return ""
    return reverse("admin:datamart_raw_record", args=["partner_reports", n["record_pk"]])


def _location_reached(job: Job, powerbi: Any, n: dict[str, Any], newest: dict[Any, float | None]) -> Reason:
    """Why a location's value differs: PRP's report total repeated on every location (NeuroDB gives it none),
    the draft of a report still owed for a period that has ended (NeuroDB holds it without a value), a factor
    of 100 (units), the newest of several records (the page reads the largest: a rule, as NeuroDB holds the
    newest too, unless its sync ran before Power BI's refresh and the next one may bring the change), an
    earlier step, else not explained yet."""
    a = pm.number(powerbi)
    if n["repeated"]:
        return ("repeated", "", words.DETAIL_REPEATED)
    if (
        n["reached"] is None
        and n["status"] in (rc.DUE, rc.OVERDUE)
        and period_ended(n.get("period_end"), job.context.day)
    ):
        # a value in a report still owed for a period that has ended: its draft (as By disaggregation reads
        # one; a report not yet due is no report still owed)
        return draft_reason(job)
    if _units(a, n["reached"]):
        return ("rule", "units", "")
    if n["records"] > 1 and n["chosen"] in newest:
        value = newest[n["chosen"]]
        if value is not None and pm.same_number(a, value):
            if job.context.sync_before_refresh:
                return ("older_version", "", words.DETAIL_OLDER_RECORD.format(n=n["records"]))
            return ("rule", "several_records", words.DETAIL_NEWEST_RECORD.format(n=n["records"]))
    if job.context.sync_before_refresh and n["status"] != rc.ACCEPTED:
        return ("older_version", "", "")
    return fallback(job)


def _newest_values(rows: list[dict[str, Any]]) -> dict[Any, float | None]:
    """For each chosen record of a location NeuroDB holds several records of, when the newest of them (by its
    change in the Datamart) is another: that one's value, in Power BI's unit."""
    from django.db.models.fields.json import KeyTransform

    from . import models as dm

    out: dict[Any, float | None] = {}
    ids = sorted({i for r in rows for i in r["record_ids"]})
    stamps: dict[Any, tuple] = {}
    for start in range(0, len(ids), 5000):
        for datamart_id, changed, total in (
            dm.ReportedIndicator.objects.filter(datamart_id__in=ids[start : start + 5000])
            .annotate(_total=KeyTransform("()", "disaggregation"))
            .values_list("datamart_id", "last_modify_date", "_total")
        ):
            stamps[datamart_id] = (changed, total)
    for r in rows:
        mine = [(stamps[i][0], i) for i in r["record_ids"] if i in stamps]
        if not mine:
            continue
        newest = max(
            mine,
            key=lambda m: (
                m[0] is not None,
                m[0] or datetime.datetime.min.replace(tzinfo=datetime.UTC),
                m[1],
            ),
        )[1]
        if newest == r["chosen"]:
            continue
        parts = rc.parts(stamps[newest][1])
        value = parts.value(r["ratio"], r["percentage"]) if parts else None
        out[r["chosen"]] = value / r["scale"] if value is not None else None
    return out


def _held_locations(context: pf.Context, numbers: set[Any]) -> set[tuple]:
    """Every location row NeuroDB holds of these PDs, whatever its status or year: (PD # without its
    amendment, report, indicator, P-code)."""
    from django.db.models import Q

    from . import models as dm

    held = pf.pds(context)
    ids, refs = set(), set()
    for number in numbers:
        pd, _amended = held.find(number)
        if pd is not None:
            ids.add(pd.id)
        refs.add(str(number or ""))
    if not ids and not refs:
        return set()
    out = set()
    rows = (
        dm.ReportedIndicator.objects.filter(
            Q(intervention_id__in=sorted(ids)) | Q(pd_reference_number__in=sorted(refs))
        )
        .values_list(
            "intervention_id", "pd_reference_number", "report_type", "report_number", "indicator", "p_code"
        )
        .distinct()
        .order_by()
    )
    for pd_id, ref, kind, number, title, p_code in rows:
        pd = held.by_id.get(pd_id) if pd_id else None
        for name in {pd.number if pd else "", ref or ""} - {""}:
            out.add(
                (
                    pm.pd_base(name),
                    pm.text(pm.report_number(number, kind)),
                    pm.text(context.clean(title)),
                    pm.text(p_code),
                )
            )
    return out


# ======================================================================== PD/SPD Report/Disaggregation
def disaggregations(job: Job) -> None:
    """A summing kind, compared month by month (by Period End): Power BI's rows with the same match values are
    added up, and so are NeuroDB's values of one label; a value missing on one side is 0 when the other is 0.
    """
    from . import reporting

    context = job.context
    months: dict[str, tuple[io.BytesIO, gzip.GzipFile]] = {}
    cut_titles: set[str] = set()
    # the match values Power BI's export has (Period Start and Report Type are optional columns)
    with_start, with_type = job.has("period_start"), job.has("report_type")
    for row in job.rows():
        if job.before_history(row):
            job.set_aside += 1
            continue
        month = month_key(row.get("period_end"))
        spool = months.get(month)
        if spool is None:
            buffer = io.BytesIO()
            spool = months[month] = (
                buffer,
                gzip.GzipFile(fileobj=buffer, mode="wb", compresslevel=1, mtime=0),
            )
        spool[1].write(
            json.dumps([row.get(n) for n in job.columns], ensure_ascii=False, separators=(",", ":")).encode()
        )
        spool[1].write(b"\n")
        if row.get("indicator_title__cut"):
            cut_titles.add(pm.text(row["indicator_title"]))
    job.note_set_aside()
    data = page.reporting(job.parsed, context)
    labels_shared = combined = False
    zero = 0
    # NeuroDB's own months too (in Power BI's years, within its history): its values in a month Power BI's
    # export has no row for are only in NeuroDB
    held = {
        month_key(day)
        for day in data["rows"].order_by().dates("period_end", "month")
        if day >= context.history and page.in_years(job.parsed, day)
    }
    for month in sorted(set(months) | held):
        spool = months.pop(month, None)
        if spool is None:
            powerbi = {}
        else:
            buffer, stream = spool
            stream.close()
            powerbi = _disaggregation_sums(job, buffer.getvalue(), with_start, with_type)
            del buffer
        neurodb, shared, unlabelled = _neurodb_disaggregations(job, data, month, with_start, with_type)
        cut = bool(job.result.extra.pop("cut", False))  # a PD of this month above the tab's limit
        labels_shared = labels_shared or shared
        _resolve_cut_titles(powerbi, neurodb, cut_titles)
        for key in sorted(set(powerbi) | set(neurodb), key=lambda k: tuple(str(x) for x in k)):
            a, b = powerbi.get(key), neurodb.get(key)
            av = a["reached"] if a else None
            bv = b["reached"] if b else None
            if (a is None and not bv) or (b is None and not av):
                job.result.same(zero=True)
                zero += 1
                continue
            match = (a or b)["match"]
            with job.row("different", match):
                if a is None:
                    # Power BI kept another report of the same PD and period end (one PD's rows only)
                    latest = not b["several"] and period_latest(
                        context, context.location_reports, b["pd"], b["match"][0], b["match"][5], key[3]
                    )
                    rule: Reason = ("rule", "period_latest", words.DETAIL_PERIOD_LATEST)
                    job.add(
                        "only_neurodb",
                        rule if latest else neurodb_only(job),
                        match=match,
                        found={"reached": [None, bv, None]},
                        link=raw_link(b["pd"], b["report_type"], context=context),
                    )
                    continue
                if b is None:
                    if key[:-1] in unlabelled:
                        why: Reason = ("not_in_read", "", words.DETAIL_UNLABELLED)
                    elif cut:
                        why = (
                            "left_out",
                            "",
                            words.DETAIL_LIMIT.format(limit=reporting.DISAGGREGATION_LIMIT),
                        )
                    else:
                        why = _disaggregation_missing(job, a, key, with_start)
                    job.add(
                        "only_powerbi",
                        why,
                        match=match,
                        found={"reached": [av, None, None]},
                    )
                    continue
                diffs: dict[str, list[Any]] = {}
                options: list[Reason] = []
                if not pm.same_number(av, bv, zero_is_none=True):
                    diffs["reached"] = [av, bv, b.get("combined")]
                    if _units(av, bv):
                        options.append(("rule", "units", ""))
                    elif b.get("combined") is not None and pm.same_number(av, b["combined"]):
                        options.append(("rule", "combined", ""))
                        combined = True
                    elif b["repeated"]:
                        options.append(("repeated", "", words.DETAIL_REPEATED))
                    elif b["unlabelled"]:
                        options.append(("not_in_read", "", words.DETAIL_UNLABELLED))
                    else:
                        options.append(_disaggregation_above(job, a, key, with_start, av, bv))
                # Power BI's status: that of its rows that carry a value (its 0 rows can be another PD's)
                theirs = a["valued"] or a["statuses"]
                if job.has("report_status") and theirs and not (theirs & b["statuses"]):
                    diffs["report_status"] = [sorted(a["statuses"]), sorted(b["statuses"]), None]
                    options.append(("status", "", ""))
                if not diffs:
                    job.result.same()
                    continue
                job.add(
                    "different",
                    pick(options, context),
                    match=match,
                    found=diffs,
                    link=raw_link(b["pd"], b["report_type"], context=context),
                )
    job.note_neurodb_set_aside()
    if labels_shared:
        job.result.note(words.NOTE_SHARED_LABEL)
    if combined:
        job.result.note(words.NOTE_CROSS_TAB)
    if zero:
        job.result.note(words.NOTE_SAME_ZERO.format(n=zero))


def _disaggregation_key(
    title: str,
    p_code: Any,
    start: Any,
    end: Any,
    kind: Any,
    report: Any,
    label: Any,
    with_start: bool,
    with_type: bool,
) -> tuple:
    return (
        title,
        pm.text(p_code),
        pm.iso(start) if with_start else "",
        pm.iso(end),
        pm.text(kind) if with_type else "",
        pm.text(pm.report_number(report, kind)),
        pm.text(label),
    )


def _disaggregation_sums(
    job: Job, data: bytes, with_start: bool, with_type: bool
) -> dict[tuple, dict[str, Any]]:
    """Power BI's rows of one month, added up by their match values: the sum and the statuses. Each title is
    kept once, as written and as compared (Power BI repeats it on every row of a report, place and label)."""
    out: dict[tuple, dict[str, Any]] = {}
    names = job.columns
    titles: dict[Any, tuple[Any, str]] = {}
    with gzip.GzipFile(fileobj=io.BytesIO(data), mode="rb") as stream:
        for raw in stream:
            row = dict(zip(names, json.loads(raw), strict=False))
            found = titles.get(row["indicator_title"])
            if found is None:
                title = row["indicator_title"]
                found = titles[title] = (title, pm.text(title))
            title, text = found
            key = _disaggregation_key(
                text,
                row["p_code"],
                row.get("period_start"),
                row["period_end"],
                row.get("report_type") or "",
                row["report"],
                row["disaggregation"],
                with_start,
                with_type,
            )
            mine = out.get(key)
            if mine is None:
                mine = out[key] = {
                    "match": [
                        title,
                        row["p_code"],
                        row.get("period_start"),
                        row["period_end"],
                        row.get("report_type"),
                        row["report"],
                        row["disaggregation"],
                    ],
                    "reached": None,
                    "statuses": set(),
                    # the statuses of its rows whose Reached is a number other than 0 (None: no such row yet;
                    # most keys have none, and an empty set each would weigh on the job's memory)
                    "valued": None,
                }
            value = pm.number(row.get("reached"))
            if value is not None:
                mine["reached"] = (mine["reached"] or 0.0) + value
            if row.get("report_status"):
                mine["statuses"].add(pm.text(row["report_status"]))
                if value:
                    if mine["valued"] is None:
                        mine["valued"] = set()
                    mine["valued"].add(pm.text(row["report_status"]))
    return out


def _neurodb_disaggregations(
    job: Job, data: dict[str, Any], month: str, with_start: bool, with_type: bool
) -> tuple[dict[tuple, dict[str, Any]], bool, set[tuple]]:
    """NeuroDB's By disaggregation values of one month (``reporting.disaggregation_rows`` and ``fill_block``),
    by Power BI's match values: each single disaggregation's value by its label (two disaggregations sharing a
    label added together); where a block holds a label only in a cross-tab, the cross-tab's cells of that
    label added up (``combined``). Also whether two disaggregations shared a label, and the blocks holding a
    value whose name NeuroDB has not read yet."""
    from . import disaggregation as dg
    from . import reporting

    context = job.context
    out: dict[tuple, dict[str, Any]] = {}
    if not month:
        return out, False, set()
    first = datetime.date(int(month[:4]), int(month[5:7]), 1)
    last = (first + datetime.timedelta(days=32)).replace(day=1) - datetime.timedelta(days=1)
    rows = data["rows"].filter(period_end__gte=first, period_end__lte=last)
    found = reporting.disaggregation_rows(rows, data["statuses"], context.day)
    parts = [found]
    if found["too_many"]:  # one month above the tab's limit: read it PD by PD
        pd_ids = sorted(set(rows.order_by().values_list("intervention_id", flat=True).distinct()) - {None})
        parts = [
            reporting.disaggregation_rows(rows.filter(intervention_id=pd_id), data["statuses"], context.day)
            for pd_id in pd_ids
        ]
    shared = False
    unlabelled: set[tuple] = set()  # the blocks holding a value whose name NeuroDB has not read yet
    if any(part["too_many"] for part in parts):  # one PD alone above the tab's limit: its values are not read
        job.result.extra["cut"] = True
        job.result.note(words.DETAIL_LIMIT.format(limit=reporting.DISAGGREGATION_LIMIT))
    for part in parts:
        for block in part.get("blocks", []):
            reporting.fill_block(block, part["labels"])
            if job.before_history(block):  # NeuroDB still holds it from an earlier sync: set aside
                singles = [x for group in block["groups"] if not group.cross for x in group.lines]
                job.neurodb_set_aside += sum(1 for x in [*singles, *block["untyped"]] if x.value is not None)
                block.clear()
                continue
            if block.get("unlabelled"):
                unlabelled.add(
                    _disaggregation_key(
                        pm.text(context.clean(block["indicator"])),
                        block["p_code"],
                        block["period_start"],
                        block["period_end"],
                        block["report_type"],
                        block["report"],
                        "",
                        with_start,
                        with_type,
                    )[:-1]
                )
            scale = 100.0 if block["percentage"] else 1.0
            singles: dict[str, float] = {}
            types: dict[str, set[str]] = defaultdict(set)
            crosses: dict[str, float] = defaultdict(float)
            for group in block["groups"]:
                for line in group.lines:
                    if line.value is None:
                        continue
                    if group.cross:
                        for label in line.label.split(dg.CROSS):
                            crosses[pm.text(label)] += line.value
                    else:
                        singles[pm.text(line.label)] = singles.get(pm.text(line.label), 0.0) + line.value
                        types[pm.text(line.label)].add(group.name)
            for line in block["untyped"]:
                if line.value is not None:
                    singles[pm.text(line.label)] = singles.get(pm.text(line.label), 0.0) + line.value
            shared = shared or any(len(t) > 1 for t in types.values())
            title = context.clean(block["indicator"])
            text = pm.text(title)  # one for the block's labels
            for label in dict.fromkeys([*singles, *crosses]):
                value = singles.get(label)
                from_cross = crosses.get(label) if value is None else None
                key = _disaggregation_key(
                    text,
                    block["p_code"],
                    block["period_start"],
                    block["period_end"],
                    block["report_type"],
                    block["report"],
                    label,
                    with_start,
                    with_type,
                )
                mine = out.get(key)
                if mine is None:
                    mine = out[key] = {
                        "match": [
                            title,
                            block["p_code"],
                            block["period_start"],
                            block["period_end"],
                            block["report_type"],
                            pm.report_number(block["report"], block["report_type"]),
                            label,
                        ],
                        "reached": None,
                        "statuses": set(),
                        "pd": block["programme_document"],
                        "report_type": block["report_type"],
                        "repeated": block["repeated"],
                        "unlabelled": block.get("unlabelled", 0),
                        "combined": None,
                        # its blocks add up several PDs' (``pd`` is the first): a flag, never a set per key
                        "several": False,
                    }
                if block["programme_document"] != mine["pd"]:
                    mine["several"] = True
                if value is not None:
                    mine["reached"] = (mine["reached"] or 0.0) + value / scale
                if from_cross is not None:
                    mine["combined"] = (mine["combined"] or 0.0) + from_cross / scale
                mine["statuses"].add(pm.text(page.CHART_LABELS.get(block["status"], "")))
                if block["status"] == rc.NOT_YET_DUE:
                    mine["statuses"].add("due")
            block.clear()
    return out, shared, unlabelled


def _resolve_cut_titles(
    powerbi: dict[tuple, dict[str, Any]], neurodb: dict[tuple, dict[str, Any]], cut: set[str]
) -> None:
    """A title Power BI cut at 255 characters stands for NeuroDB's title that starts with it (when only one
    does): Power BI's sums move under NeuroDB's match values."""
    if not cut:
        return
    titles = {k[0] for k in neurodb}
    for key in [k for k in powerbi if k[0] in cut and k not in neurodb]:
        found = [t for t in titles if t.startswith(key[0])]
        if len(found) == 1:
            new = (found[0], *key[1:])
            if new not in powerbi:
                powerbi[new] = powerbi.pop(key)


def _disaggregation_missing(job: Job, a: dict[str, Any], key: tuple, with_start: bool) -> Reason:
    """A value only Power BI has (above 0): the draft of a report still owed (the rows that carry a value, or
    every row when none does, are of reports still owed for a period that has ended), else the report of the
    PD the location export ties the row to (:func:`_tie`): not in the Datamart read, or still owed; else (no
    tie) not in the read while the sync may lag Power BI's refresh, else not explained yet."""
    statuses = a["valued"] or a["statuses"]
    if statuses and statuses <= OWED_LABELS and period_ended(a["match"][3], job.context.day):
        return draft_reason(job)
    tie = _tie(job, key, with_start, several=True)
    if tie == "not_in_read":
        return not_in_read(job)
    if tie == "owed":
        return draft_reason(job)
    if not job.context.sync_after_refresh:
        return not_in_read(job)
    return fallback(job)


def _disaggregation_above(
    job: Job, a: dict[str, Any], key: tuple, with_start: bool, powerbi: Any, neurodb: Any
) -> Reason:
    """A value both have and Power BI's is above NeuroDB's: Power BI adds another PD's report (the export has
    no PD #) that the Datamart read did not bring, or a draft of a report still owed; else (NeuroDB above:
    neither explains it) an older version or not explained yet."""
    if (pm.number(powerbi) or 0) > (pm.number(neurodb) or 0):
        tie = _tie(job, key, with_start, several=False)
        if tie == "not_in_read":
            return not_in_read(job)
        valued = a["valued"]
        draft = bool(valued) and valued <= OWED_LABELS and period_ended(a["match"][3], job.context.day)
        if tie == "owed" or draft:
            return draft_reason(job)
    return older_or_fallback(job)


def _tie(job: Job, key: tuple, with_start: bool, *, several: bool) -> str:
    """Why the report of a disaggregation row's PD is missing, the row tied to its PDs through the location
    export of the same upload (by title, report, period start when both exports have one, period end and
    P-code; never by the title alone: several PDs share standard titles): "not_in_read" (the report list says
    so), "owed" (the report list's or the location export's status is a word of a report still owed, for a
    period that has ended), else "". ``several``: a row tied to several PDs is explained only when none of
    those whose report NeuroDB has has a location row of its own at that place for that title (the value
    may be that report's, a disaggregation NeuroDB lacks)."""
    context = job.context
    if context.location_pds is None:
        return ""
    title, p_code, start, end, _kind, report, _label = key
    if with_start and context.location_start:
        tied = context.location_pds.get((title, report, start, end, p_code))
    else:
        tied = (context.location_any or {}).get((title, report, end, p_code))
    if not tied:
        return ""
    reasons = {pd: _tied_reason(context, pd, report, end) for pd in sorted(tied)}
    found = {r for r in reasons.values() if r}
    if not found:
        return ""
    if several and len(reasons) > 1:
        places = context.held_locations or set()
        for pd, reason in reasons.items():
            if not reason and (pm.pd_base(pd), title, report, end, p_code) in places:
                return ""
    return "not_in_read" if "not_in_read" in found else "owed"


def _tied_reason(context: pf.Context, pd: str, report: str, end: str) -> str:
    """Why a tied PD's report is missing from NeuroDB (:func:`_tie`): "owed" when Power BI's report list, else
    its location export, gives it a word of a report still owed for a period that has ended (whatever
    NeuroDB shows of it: PRP's progress reports carry no value); "not_in_read" when the report list's line
    says so; else ""."""
    labels: set[str] = set()
    if context.listed is not None:
        labels = {r["label"] for r in context.listed.report(pd, report) if pm.iso(r["period_end"]) == end}
    if not labels and context.location_status:
        found = context.location_status.get((pd, report, end))
        labels = {found} if found else set()
    if labels and labels <= OWED_LABELS and period_ended(end, context.day):
        return "owed"
    if (context.listed_reasons or {}).get((pd, report, end)) == "not_in_read":
        return "not_in_read"
    return ""


# ======================================================================================== Funds
FUND_COLUMNS = (
    "partner",
    "partner_type",
    "currency",
    "unicef_planned",
    "cash",
    "partner_contribution",
    "partner_share",
    "financial_contribution",
)


def fund_monitoring(job: Job) -> None:
    context = job.context
    neurodb = page.fund_monitoring(job.parsed, context)
    candidates = pf.contributions({n["pd_id"] for n in neurodb}, context.day)
    powerbi = list(job.rows())

    def key(row: dict[str, Any]) -> str:
        return pm.pd_number(row["pd"])

    pairs, left_pb, left_nd = pm.pair(powerbi, neurodb, key, key)
    more, left_pb, left_nd = pm.pair(
        left_pb, left_nd, lambda r: pm.pd_base(r["pd"]), lambda r: pm.pd_base(r["pd"])
    )
    amended = {id(p) for p, _n in more}
    pairs += more
    names = job.present(FUND_COLUMNS)
    fit_rows = []
    for p, n in pairs:
        if "financial_contribution" in names:
            fit_rows.append(
                (
                    pm.number(p.get("financial_contribution")),
                    {k: v for k, v in candidates.get(n["pd_id"], {}).items()},
                )
            )
    if fit_rows:
        table = pf.fit(fit_rows, ("fc_latest_accepted", "fc_latest_counted", "fc_sum"))
        job.result.fits.append({"title": "financial_contribution", "rows": table, "set_aside": 0})
    best = pf.chosen(job.result.fits[-1]["rows"]) if fit_rows else ""
    budgets = pf.budgets(context, {n["pd_id"] for _p, n in pairs})
    for p, n in pairs:
        with job.row("different", [p["pd"]]):
            diffs: dict[str, list[Any]] = {}
            options: list[Reason] = []
            for name in names:
                a = p.get(name)
                if name == "partner":
                    same, b = pm.partner(a) == pm.partner(n["partner"]), n["partner"]
                elif name == "partner_type":
                    same = pm.text(a) in (pm.text(n["partner_type"]), pm.text(n["cso_type"]))
                    b = n["partner_type"] or n["cso_type"]
                elif name == "currency":
                    same, b = pm.same_text(a, n["currency"]), n["currency"]
                elif name == "unicef_planned":
                    same, b = pm.same_money(a, n["unicef_planned"]), n["unicef_planned"]
                elif name == "cash":
                    same, b = pm.same_money(a, n["total"]), n["total"]
                elif name == "partner_contribution":
                    same, b = pm.same_money(a, n["partner_contribution"]), n["partner_contribution"]
                elif name == "partner_share":
                    if "cash" in diffs or "partner_contribution" in diffs:
                        continue  # it follows the two amounts
                    theirs = pm.number(a)
                    b = pm.number(n["partner_share"]) / 100 if n["partner_share"] is not None else None
                    same = pm.same_number(theirs, b, zero_is_none=True)
                else:
                    b = n["financial_contribution"]
                    same = pm.same_money(a, b)
                if same:
                    continue
                alt = None
                if name == "financial_contribution":
                    mine = candidates.get(n["pd_id"], {})
                    fitting = [c for c, v in mine.items() if pm.same_money(a, v)]
                    if best and best in fitting:
                        alt = mine[best]
                        detail = words.DETAIL_ALTERNATIVE.format(
                            words=words.CANDIDATE_WORDS[best], alt=_number_words(alt), value=_number_words(b)
                        )
                        options.append(("rule", "contribution", detail))
                    else:
                        options.append(fallback(job))
                elif name == "partner":
                    options.append(("rule", "partner_name", words.DETAIL_PARTNER))
                else:
                    options.append(older_or_fallback(job))
                diffs[name] = [a, b, alt]
            if not diffs:
                job.result.same()
                continue
            why = pick(options, context)
            if id(p) in amended:
                why = (why[0], why[1], " ".join(x for x in (why[2], words.DETAIL_AMENDMENT) if x))
            why = with_budget(why, diffs, budgets.get(n["pd_id"]))
            job.add("different", why, match=[p["pd"]], found=diffs, link=pd_link(n["pd_id"]))
    for p in left_pb:
        with job.row("only_powerbi", [p["pd"]]):
            why = pd_reason(job, p["pd"]) or fallback(job)
            job.add(
                "only_powerbi",
                why,
                match=[p["pd"]],
                found={name: [p.get(name), None, None] for name in names},
                pd=p["pd"] if why[0] == "pd_not_running" else "",
            )
    for n in left_nd:
        job.add(
            "only_neurodb",
            neurodb_only(job),
            match=[n["pd"]],
            found={"partner": [None, n["partner"], None], "cash": [None, n["total"], None]},
            link=pd_link(n["pd_id"]),
        )


# the funds pages' columns of a PD's budget (Partner Fund Monitoring's and Funded Programs')
BUDGET_COLUMNS = frozenset({"unicef_planned", "cash", "partner_contribution", "partner_share"})


def with_budget(why: Reason, diffs: dict[str, Any], budget: tuple[Any, ...] | None) -> Reason:
    """A line whose PD budget columns differ says the budget fields NeuroDB has for the PD (a diagnostic of
    amended PDs' budgets: an earlier amendment's, or another record's)."""
    if budget is None or not BUDGET_COLUMNS & set(diffs):
        return why
    from .funds import amount

    unicef, total, partner, _cash = (_number_words(amount(v)) for v in budget)
    found = words.DETAIL_BUDGET.format(unicef=unicef, total=total, partner=partner)
    return (why[0], why[1], pm.cut(" ".join(x for x in (why[2], found) if x)))


def older_or_fallback(job: Job) -> Reason:
    """An amount or a word eTools changed after NeuroDB's sync, when it ran before Power BI's refresh."""
    if job.context.sync_before_refresh:
        return ("older_version", "", "")
    return fallback(job)


FUNDED_PD_COLUMNS = (
    "pd_status",
    "start",
    "end",
    "days_to_end",
    "partner",
    "currency",
    "unicef_planned",
    "cash",
    "disbursed",
    "partner_contribution",
    "partner_share",
)


def funded_programmes(job: Job) -> None:
    context = job.context
    neurodb = page.funded_programmes(job.parsed, context)
    powerbi = list(job.rows())

    def key(row: dict[str, Any]) -> tuple:
        return (pm.pd_number(row["pd"]), pm.grant(row["grant"]))

    pairs, left_pb, left_nd = pm.pair(powerbi, neurodb, key, key)
    more, left_pb, left_nd = pm.pair(
        left_pb,
        left_nd,
        lambda r: (pm.pd_base(r["pd"]), pm.grant(r["grant"])),
        lambda r: (pm.pd_base(r["pd"]), pm.grant(r["grant"])),
    )
    amended = {id(p) for p, _n in more}
    pairs += more
    grant_names = job.present(("donor", "fund_reservation"))
    pd_names = job.present(FUNDED_PD_COLUMNS)
    seen_pds: set[str] = set()
    budgets = pf.budgets(context, {n["pd_id"] for _p, n in pairs})
    for p, n in pairs:
        with job.row("different", [p["pd"], p["grant"]]):
            diffs: dict[str, list[Any]] = {}
            options: list[Reason] = []
            for name in grant_names:
                a = p.get(name)
                if name == "donor":
                    same = pm.same_text(a, n["donor"]) or pm.sections(a) == pm.sections(n["donor"])
                    b = n["donor"]
                else:
                    same, b = pm.same_money(a, n["reserved"]), n["reserved"]
                if not same:
                    diffs[name] = [a, b, None]
                    options.append(older_or_fallback(job))
            pd_key = pm.pd_number(n["pd"])
            if pd_key not in seen_pds:  # the PD's own values: once per PD, never added over its grants
                seen_pds.add(pd_key)
                for name in pd_names:
                    a = p.get(name)
                    if name == "pd_status":
                        same = pm.text(a) in (
                            pm.text(n["pd_status"]),
                            pm.text(pd_statuses.label(n["pd_status"])),
                        )
                        b = pd_statuses.label(n["pd_status"])
                    elif name in ("start", "end"):
                        same, b = pm.same_date(a, n[name]), n[name]
                    elif name == "days_to_end":
                        same, b = pm.same_count(a, n["days_to_end"]), n["days_to_end"]
                    elif name == "partner":
                        same, b = pm.partner(a) == pm.partner(n["partner"]), n["partner"]
                    elif name == "currency":
                        same, b = pm.same_text(a, n["currency"]), n["currency"]
                    elif name == "cash":
                        same, b = pm.same_money(a, n["total"]), n["total"]
                    elif name == "partner_share":
                        if "cash" in diffs or "partner_contribution" in diffs:
                            continue
                        total, part = pm.number(n["total"]), pm.number(n["partner_contribution"])
                        b = part / total if total and part is not None else None
                        same = pm.same_number(a, b, zero_is_none=True)
                    else:
                        same, b = pm.same_money(a, n.get(name)), n.get(name)
                    if same:
                        continue
                    diffs[name] = [a, b, None]
                    if name == "partner":
                        options.append(("rule", "partner_name", words.DETAIL_PARTNER))
                    elif (
                        name == "days_to_end"
                        and job.context.refreshed
                        and job.context.refreshed != job.context.day
                    ):
                        options.append(("status", "", words.DETAIL_OTHER_DAY))
                    else:
                        options.append(older_or_fallback(job))
            if not diffs:
                job.result.same()
                continue
            why = pick(options, context)
            if id(p) in amended:
                why = (why[0], why[1], " ".join(x for x in (why[2], words.DETAIL_AMENDMENT) if x))
            why = with_budget(why, diffs, budgets.get(n["pd_id"]))
            job.add("different", why, match=[p["pd"], p["grant"]], found=diffs, link=pd_link(n["pd_id"]))
    funded = {pm.pd_base(n["pd"]) for n in neurodb}
    for p in left_pb:
        why = pd_reason(job, p["pd"])
        if why is None:
            why = funds_elsewhere(context, p["pd"])
        if why is None:
            why = ("rule", "fr_only", "") if pm.pd_base(p["pd"]) not in funded else older_or_fallback(job)
        job.add(
            "only_powerbi",
            why,
            match=[p["pd"], p["grant"]],
            found={name: [p.get(name), None, None] for name in (*grant_names, "cash")},
            pd=p["pd"] if why[0] == "pd_not_running" else "",
        )
    for n in left_nd:
        job.add(
            "only_neurodb",
            neurodb_only(job),
            match=[n["pd"], n["grant"]],
            found={"donor": [None, n["donor"], None], "fund_reservation": [None, n["reserved"], None]},
            link=pd_link(n["pd_id"]),
        )
    if job.has("outstanding_dct"):
        job.result.note(words.NOTE_DCT)
        sums = pf.outstanding({n["pd_id"] for n in neurodb})
        ids = {pm.pd_number(n["pd"]): n["pd_id"] for n in neurodb}
        rows, seen = [], set()
        for p in powerbi:
            pd_id = ids.get(pm.pd_number(p["pd"]))
            if pd_id is None or pd_id in seen:
                continue
            seen.add(pd_id)
            rows.append((pm.number(p.get("outstanding_dct")), {k: v for k, v in sums.get(pd_id, {}).items()}))
        if rows:
            job.result.fits.append(
                {"title": "outstanding_dct", "rows": pf.fit(rows, ("dct_fr", "dct_pd")), "set_aside": 0}
            )


def funds_elsewhere(context: pf.Context, pd: Any) -> Reason | None:
    """A Funded Programs row only Power BI has, whose funds reservation lines name its PD while NeuroDB links
    them to another PD, or to none (a number of an unclear amendment, or of a PD NeuroDB does not hold): never
    "funds reservation only", which would hide it. Never paired across PDs by grant and amount."""
    linked = pf.fr_elsewhere(context).get(pm.pd_base(pd))
    if not linked:
        return None
    others = sorted(n for n in linked if n and pm.pd_base(n) != pm.pd_base(pd))
    if others:
        return ("funds_elsewhere", "", words.DETAIL_FUNDS_ELSEWHERE.format(pd=", ".join(others[:3])))
    if "" in linked:
        return ("funds_elsewhere", "", words.DETAIL_FUNDS_UNLINKED)
    return None


# ===================================================================================== the comparers
COMPARERS: dict[str, Callable[[Job], None]] = {
    "program_performance": program_performance,
    **dict.fromkeys(PARTNER_COUNTS, partner_count),
    "partner_cards": partner_cards,
    "follow_up": follow_up,
    **dict.fromkeys(CARD_STATUSES, status_card),
    "by_sector": by_sector,
    "by_month": by_month,
    **dict.fromkeys(DQ_CARDS, dq_figure),
    "dq_card": dq_card,
    "missing_targets": missing_targets,
    "dq_list": dq_list,
    "pd_indicators": pd_indicators,
    "report_list": report_list,
    "locations": locations,
    "disaggregations": disaggregations,
    "fund_monitoring": fund_monitoring,
    "funded_programmes": funded_programmes,
}


def neurodb_count(kind: str, parsed: Filter, context: pf.Context) -> int:
    """The rows NeuroDB's page has for an export Power BI exported empty (its verdict says how many)."""
    from . import reporting

    if kind in PARTNER_COUNTS:
        return int(page.partner_performance(parsed, context)[PARTNER_COUNTS[kind]])
    if kind in CARD_STATUSES:
        return sum(1 for r in page.reports(parsed, context) if r["status"] == CARD_STATUSES[kind])
    if kind in DQ_CARDS:
        return int(page.quality(parsed, context)[kind])
    if kind == "missing_targets":
        return len(page.missing_targets(parsed, context))
    if kind in ("report_list", "follow_up", "by_sector", "by_month", "partner_cards"):
        found = page.reports(parsed, context)
        if kind == "follow_up":
            return sum(1 for r in found if r["status"] in reporting.FOLLOW_UP_STATUSES)
        return len(found)
    if kind in ("dq_card", "dq_list"):
        return len(page.no_reached(parsed, context))
    if kind == "program_performance":
        return len([n for n in page.indicators(parsed, context) if page.in_section(n, parsed)])
    if kind == "pd_indicators":
        return len([n for n in page.pd_indicators(parsed, context) if page.in_section(n, parsed)])
    if kind in ("locations", "disaggregations"):
        data = page.reporting(parsed, context)
        return (
            data["rows"].count()
            if kind == "disaggregations"
            else len(
                reporting.location_rows(
                    data["rows"], data["statuses"], context.day, page.wanted_statuses(parsed)
                )["rows"]
            )
        )
    if kind == "fund_monitoring":
        return len(page.fund_monitoring(parsed, context))
    if kind == "funded_programmes":
        return len(page.funded_programmes(parsed, context))
    return 0
