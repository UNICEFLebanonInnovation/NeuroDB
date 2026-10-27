"""Reports still owed for a period that has ended, from PRP's own progress reports; shown only while the
link to their programme documents is proven (the gate).

* **Why.** The partner reports feed (``partner_reports``) stops sending a report once its period ends
  unsent, so the reports partners still owe are missing from it; PRP's own list of progress reports
  (``prp_progress_reports``) holds them, as Power BI counts them. Home, Progress reports and its Partners
  tab, Partner monitoring's Overdue reports, the PD and partner pages, the daily review and Ask add them, by
  the user's decision of 26 Oct 2026.
* **The gate** (:func:`link_state`): they are shown only while the sync's recorded verdict says the link of
  PRP's progress reports to their programme documents is proven, agrees with the partner reports on two days
  in a row, and the last read of them received records at most ``datamart_sync.OWED_STALE_DAYS`` days ago
  (``datamart_sync.recorded_pd_link``). It closes at once when one read fails it. The pages never gate on
  the link itself: with no proven pair the sync still links "by today's lookup".
* **Owed** (:func:`owed_reports`, :func:`every_owed`): PRP says Due or Overdue
  (``reached.prp_document_status``, read for the day), its period has ended, a read returned it within the
  last seven days, one per programme document, type and number (``datamart_sync.counted_once``), and the
  partner reports hold no row of that report (``reporting.held_report_numbers``: any period, status or
  filter; the rows then decide).
* **No indicator, no value.** They carry no indicator report (``indicators`` 0): the tiles' indicator report
  hints and Reached leave them out. They are never put on a partner report row's report.
* **People.** Each record is read field by field (``KT``), never whole: PRP's records hold narratives and
  the people who wrote and reviewed them.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import Any

from neurodb.core import compute
from neurodb.partnerships import statuses as pd_statuses
from neurodb.partnerships.models import PCA

from . import models as dm
from . import reached as rc
from . import reporting

SOURCE = "prp_progress_reports"  # the Datamart dataset owed reports come from (each result's ``source``)
SOURCE_WORDS = "PRP progress reports"  # and the exports' "source" column (the partner reports' rows: below)
ROWS_WORDS = "Partner reports"

# the line under the tiles, on the Partners tab, on the follow-up list and in Home's block while the gate is
# closed, by why (``datamart_sync.recorded_pd_link``); the first part is always said
HIDDEN_START = (
    "Some reports still owed for a period that has ended may be missing: eTools stops sending a report once "
    "its period ends unsent. NeuroDB keeps those it received before"
)
HIDDEN = {
    "": f"{HIDDEN_START}, and cannot tie PRP's own list of progress reports to their programme documents "
    "yet.",
    "one_day": f"{HIDDEN_START}. PRP's own list was tied to its programme documents on {{day}}: NeuroDB "
    "shows it once the next read agrees.",
    "old": f"{HIDDEN_START}. PRP's own list of progress reports was last read on {{day}}.",
    "not_read": f"{HIDDEN_START}, and the last read of PRP's own list of progress reports received nothing.",
}
# the line while the gate is open and the filter keeps at least one of them
SHOWN = (
    "Overdue and Due include {n:,} reports from PRP's own list that partners still owe. They have no "
    "indicators, so the indicator counts leave them out."
)
SHOWN_ONE = (
    "Overdue and Due include 1 report from PRP's own list that a partner still owes. It has no indicators, "
    "so the indicator counts leave it out."
)
# the trail's and the help's name of the gate
CHECKS = "eTools reporting checks, check 11"


@dataclass(frozen=True)
class OwedLink:
    """Whether the pages show PRP's progress reports still owed (``shown``), and why not (``why``: the reasons
    of ``datamart_sync.recorded_pd_link``); ``held``: NeuroDB holds PRP's progress reports at all (without
    them the pages say nothing); ``since``: the day they were first shown; ``day``: the day the line names
    (the one day the link passed, or the last read); ``recorded``: the verdict as read."""

    shown: bool
    why: str
    held: bool
    since: datetime.date | None = None
    day: datetime.date | None = None
    recorded: dict[str, Any] = field(default_factory=dict)

    @property
    def line(self) -> str:
        """The line the pages show while the gate is closed ("" when it is open, or nothing is held)."""
        if self.shown or not self.held:
            return ""
        from neurodb.integrations.etools.datamart_sync import day_words

        text = HIDDEN.get(self.why, HIDDEN[""])
        return text.format(day=day_words(self.day.isoformat() if self.day else ""))


def link_state() -> OwedLink:
    """The gate, read once per request (``compute.per_request``): NeuroDB holds PRP's progress reports, and
    the verdict the sync recorded last (``datamart_sync.recorded_pd_link``: the latest finished run of either
    PRP dataset, closed when the last read of the progress reports received nothing or is more than seven days
    old) shows them."""
    return compute.per_request("owed-link", _link_state)


def _link_state() -> OwedLink:
    from neurodb.integrations.etools import datamart_sync as sync

    held = dm.DatamartDocument.objects.filter(dataset=sync.PROGRESS_REPORTS).exists()
    if not held:
        return OwedLink(shown=False, why="not_read", held=False)
    recorded = sync.recorded_pd_link()
    shown = recorded.get("shown") is True
    why = "" if shown else str(recorded.get("why") or "not_proven")
    day = rc.iso_day(recorded.get("read_on") if why == "old" else recorded.get("checked_on"))
    return OwedLink(
        shown=shown,
        why=why,
        held=True,
        since=rc.iso_day(recorded.get("since")),
        day=day,
        recorded=recorded,
    )


def state(n: int) -> dict[str, Any]:
    """What the pages say of owed reports (the hub's ``owed``): ``shown`` (the gate is open), ``n`` (owed
    reports the filter keeps), ``held``, ``why`` and the ``line`` to show: the closed line, or, with the gate
    open and at least one kept, :data:`SHOWN`; "" otherwise."""
    link = link_state()
    line = link.line
    if link.shown and n:
        line = SHOWN_ONE if n == 1 else SHOWN.format(n=n)
    return {"shown": link.shown, "n": n, "held": link.held, "why": link.why, "line": line}


# ------------------------------------------------------------------------------------------------ the builder
def _candidates(today: datetime.date) -> list[dict[str, Any]]:
    """PRP's progress reports still owed, before any filter: linked to a PD NeuroDB holds, returned by a read
    within the last seven days (``datamart_sync.fresh_since``: PRP may have deleted the others), one per
    programme document, type and number (the latest read's version), Due or Overdue for a period that has
    ended (``datamart_sync.linked_progress_reports``' rows)."""
    from neurodb.integrations.etools import datamart_sync as sync

    since = sync.fresh_since()
    fresh = [r for r in sync.linked_progress_reports(today) if r["synced_at"] >= since]
    current, _twins = sync.counted_once(fresh)
    return [r for r in current if r["status"] in sync.OWED and r["end"] is not None and r["end"] <= today]


def _pds(pd_ids: set[int]) -> dict[int, dict[str, Any]]:
    """What the filters and the results read of each programme document: its number, status, type, sections
    and partner (id, names, vendor number, types). No person."""
    out: dict[int, dict[str, Any]] = {}
    fields = (
        "pk",
        "number",
        "status",
        "document_type",
        "section_names",
        "partner_name",
        "partner_id",
        "partner__name",
        "partner__short_name",
        "partner__vendor_number",
        "partner__partner_type",
        "partner__cso_type",
    )
    for row in PCA.objects.filter(pk__in=sorted(pd_ids)).values(*fields):
        out[row["pk"]] = row
    return out


def _not_held(found: list[dict[str, Any]], pds: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    """The reports the partner reports hold no row of (``reporting.held_report_numbers``, read over every row
    of those PDs whatever the filter: a section or a search leaves out rows of a report the rows still hold,
    and must not let its PRP copy in)."""
    numbers = {pds[r["pd"]]["number"] for r in found if r["pd"] in pds and pds[r["pd"]]["number"]}
    held = reporting.held_report_numbers(numbers) if numbers else set()
    return [
        r
        for r in found
        if not (r["key"] and r["base"] and reporting.holds(held, r["base"], r["kind"], r["key"]))
    ]


def _text(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


def _filters(filters: reporting.ReportFilters):
    """The filters of ``ReportFilters.queryset`` that apply, in its order and under its names, each as a test
    of one owed report and its PD (``pds`` of :func:`_pds`)."""
    f = filters
    sections = {_text(s) for s in f.sections}
    partners = {int(p) for p in f.partners}
    if f.report_types:
        types = set(f.report_types)
        yield "report_type", lambda r, pd: r["kind"] in types
    if f.year:
        yield "year", lambda r, pd: r["end"] is not None and r["end"].year == f.year
    if f.q:
        q = _text(f.q)
        yield (
            "q",
            lambda r, pd: any(
                q in _text(pd.get(k))
                for k in (
                    "number",
                    "partner__name",
                    "partner__short_name",
                    "partner__vendor_number",
                    "partner_name",
                )
            ),
        )
    if f.sections:  # its programme document's sections, any case (the only sections an owed report has)
        yield "section", lambda r, pd: bool(sections & {_text(s) for s in pd.get("section_names") or []})
    if f.report_numbers:
        yield (
            "report_number",
            lambda r, pd: (
                bool(r["key"]) and r["key"] in {rc.report_number_key(n, r["kind"]) for n in f.report_numbers}
            ),
        )
    if f.pds:
        wanted = set(f.pds)
        yield "pds", lambda r, pd: pd.get("number") in wanted
    if f.partners:
        yield "partner", lambda r, pd: pd.get("partner_id") in partners
    if f.partner_types:
        kinds = set(f.partner_types)
        yield (
            "partner_type",
            lambda r, pd: bool({pd.get("partner__partner_type"), pd.get("partner__cso_type")} & kinds),
        )
    yield "not_a_partnership", lambda r, pd: pd.get("status") not in pd_statuses.NOT_A_PARTNERSHIP
    if f.emergency:  # the programme document is an SPD (reached.is_spd), as Power BI's Emergency indicator
        yield "emergency", lambda r, pd: rc.is_spd(pd.get("document_type")) == (f.emergency == "yes")
    for name, value, read, later in (
        ("start_from", f.start_from, "start", False),
        ("start_to", f.start_to, "start", True),
        ("end_from", f.end_from, "end", False),
        ("end_to", f.end_to, "end", True),
    ):
        if value is not None:
            yield (
                name,
                (
                    lambda r, pd, value=value, read=read, later=later: (
                        r[read] is not None and (r[read] <= value if later else r[read] >= value)
                    )
                ),
            )
    if f.overdue:
        yield "overdue", lambda r, pd: r["status"] == rc.OVERDUE
    chosen = f.status_choice
    yield "pd_status", lambda r, pd: not chosen or pd.get("status") in chosen


def _result(r: dict[str, Any], pd: dict[str, Any]) -> dict[str, Any]:
    """One owed report in the shape of ``reporting.progress_reports``' (its key as ``reached.report_key``
    makes it), with ``source`` and ``record`` (the PRP record's pk). Nothing else of the record."""
    sections = [s for s in pd.get("section_names") or [] if s]
    report = {
        "progress_report": "",
        "pd_reference_number": pd.get("number") or "",
        "intervention_id": r["pd"],
        "partner_id": pd.get("partner_id"),
        "partner_name": pd.get("partner__name") or pd.get("partner_name") or "",
        "report_number": r["key"],
        "report_type": r["kind"],
        "report_status": r["word"],
        "report_accepted_status": "",
        "period_start": r["start"],
        "period_end": r["end"],
        "due_date": r["due"],
        "submission_date": r["sub"],
        "acceptance_date": None,
        "indicator_sections": {},
        "indicators": 0,
        "sections": {},
        "section": ", ".join(sections),
        "status": r["status"],
        "status_label": rc.STATUS_LABELS[r["status"]],
        "source": SOURCE,
        "record": r["pk"],
    }
    report["key"] = rc.report_key(report)
    return report


def _build(
    filters: reporting.ReportFilters | None, today: datetime.date, steps: list | None = None
) -> list[dict[str, Any]]:
    found = _candidates(today)
    pds = _pds({r["pd"] for r in found})
    found = [r for r in found if r["pd"] in pds]
    found = _not_held(found, pds)
    out = [_result(r, pds[r["pd"]]) for r in found]
    if filters is None:
        return reporting.in_order(out)
    if steps is not None:
        steps.append(("start", list(out)))
    by_record = {r["pk"]: r for r in found}
    for name, test in _filters(filters):
        out = [o for o in out if test(by_record[o["record"]], pds[o["intervention_id"]])]
        if steps is not None:
            steps.append((name, list(out)))
    return reporting.in_order(out)


def owed_reports(
    filters: reporting.ReportFilters, today: datetime.date, *, steps: list | None = None
) -> list[dict[str, Any]]:
    """PRP's progress reports still owed that ``filters`` keeps (every filter of ``ReportFilters.queryset``
    but the status, which the hub applies to every report), in the order ``reporting.progress_reports`` gives;
    ``[]`` while the gate is closed (:func:`link_state`). ``steps``: the figure trail's, which receives
    ``("start", every owed report)`` and then ``(the filter's name, those it keeps)`` after each filter, under
    the names of ``ReportFilters.queryset``'s steps."""
    if not link_state().shown:
        return []
    return _build(filters, today, steps)


def every_owed(today: datetime.date) -> list[dict[str, Any]]:
    """Every PRP progress report still owed, no filter (the daily review, Partner monitoring's Overdue
    reports, the Power BI comparison); ``[]`` while the gate is closed."""
    if not link_state().shown:
        return []
    return compute.per_request(f"owed-every:{today.isoformat()}", lambda: _build(None, today))


def overdue_count(pd_ids: Any, report_type: str, today: datetime.date) -> int:
    """The Overdue reports still owed in PRP's progress reports of these programme documents and report type
    that the partner reports do not have (Partner monitoring's Overdue reports adds them to the rows'): 0
    while the gate is closed."""
    wanted = set(pd_ids)
    kind = (report_type or "").strip().upper()
    return sum(
        1
        for r in every_owed(today)
        if r["status"] == rc.OVERDUE and r["intervention_id"] in wanted and r["report_type"] == kind
    )


def source_words(report: dict[str, Any]) -> str:
    """Where a report of the hub comes from, as the exports' "source" column says it."""
    return SOURCE_WORDS if report.get("source") == SOURCE else ROWS_WORDS
