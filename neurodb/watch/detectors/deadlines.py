"""Look-ahead checks on eTools programme data: what falls due in the coming days and weeks.

The daily review looks back (reports overdue, high-priority action points overdue, PDs ending under
target). These checks look ahead, so that people hear of a date before it is missed:

- ``report_due_soon``: a partner progress report (QPR) due in the next 14 days and not submitted yet
  (:func:`~neurodb.datamart.monitoring.pending_q`, the rule every page uses), one per report of a PD:
  the Partner Reporting Portal repeats a report on each of its indicator and location rows. Warning
  from 3 days before. A report submitted closes at once; once its date passes it closes as "now
  overdue: see the daily review" and hands over to the review's ``reports_overdue:<PD number>``.
- ``action_point_due``: open action points of any priority due in the next 14 days, one item per
  section and partner (the review's grouping). Warning from 3 days before the soonest.
- ``action_points_overdue_normal``: open normal-priority action points already past their date,
  grouped the same way. The review keeps the high-priority ones.
- ``pd_ending``: every active PD ending in the next 60 days, whatever its achievement. Warning from
  14 days before, or when the daily review flags it (``pd_ending_soon``, under 70 % of target): the
  review's key is then noted on the item, never a second item.
- ``pd_awaiting_closure``: PDs eTools shows as ended and not closed: the final report and the
  liquidation of the funds reservations are still to come. Warning, and the whole-country view too,
  when it ended more than 60 days ago with money still outstanding on its funds reservations.

All of them read only tables the eTools Datamart sync refreshes (progress reports, action points,
funds reservation headers, and the programme documents it writes), never the REST-only legacy
tables, and they run only while that sync is fresh. They never read a field that holds a person
(action point assignees, report submitters, PD focal points) nor free text written upstream (action
point descriptions, report narratives). Titles carry dates, never day counts. Every check starts in
trial: only the whole-country view sees it until an administrator switches it on.

Keys come from lasting identifiers, through :func:`report_key`, :func:`action_points_due_key`,
:func:`action_points_overdue_key`, :func:`pd_end_key` and :func:`closure_key`; :func:`twin_of`
gives the item a daily review finding hands over to.
"""

from __future__ import annotations

import datetime
from collections import defaultdict
from collections.abc import Iterable, Iterator
from decimal import Decimal
from typing import Any

from django.db.models import QuerySet

from neurodb.core.models import SyncRun
from neurodb.datamart import models as dm
from neurodb.datamart.monitoring import ACTIVE_PD_STATUSES, is_submitted, pending_q
from neurodb.partnerships.models import PCA
from neurodb.review.checks import REPORT_TYPE, link, pd_label, pd_number, plural
from neurodb.review.models import DailyReview, ReviewFinding

from ..models import RECORDS_MAX, WatchItem, fit_key
from . import (
    CHANGED,
    CONCERN,
    COUNTRY,
    DEADLINE,
    INFO,
    MISSED,
    SECTION,
    WARNING,
    Candidate,
    Close,
    Context,
    Detector,
    evidence,
    record,
    register,
)

DATAMART = SyncRun.Job.ETOOLS_DATAMART
PROGRAMME = "programme_document"  # the knowledge hub kinds the items are about
PARTNER = "partner"
ENDED = "ended"  # PCA.status of a PD past its end and not closed yet

REPORT_DAYS = 14  # reports due within this many days
REPORT_WARNING_DAYS = 3
REPORT_MILESTONES = (14, 7, 3, 1, 0)
ACTION_POINT_DAYS = 14
ACTION_POINT_WARNING_DAYS = 3
ACTION_POINT_MILESTONES = (14, 7, 3, 1, 0)
PD_ENDING_DAYS = 60
PD_ENDING_WARNING_DAYS = 14
PD_ENDING_MILESTONES = (60, 30, 14, 7)
CLOSURE_LATE_DAYS = 60  # ended longer ago than this with money outstanding: warning, whole country

REPORT_SOURCE = "eTools progress reports"
ACTION_POINT_SOURCE = "eTools action points"
PD_SOURCE = "eTools programme documents"
POINTS = "action_point_ids"  # evidence of an action_point_due item: the Datamart ids of its points

# The only columns read; none holds a person or free text written upstream
PD_FIELDS = (
    "id",
    "number",
    "partner",
    "partner__name",
    "partner_name",
    "status",
    "start",
    "end",
    "section_names",
    "sections",
)
REPORT_FIELDS = (
    "intervention_id",
    "progress_report",
    "report_number",
    "due_date",
    "period_start",
    "period_end",
    "submission_date",
    "report_status",
    "etools_indicator_id",
)
ACTION_POINT_FIELDS = (
    "datamart_id",
    "reference_number",
    "related_module",
    "status",
    "high_priority",
    "due_date",
    "section",
    "partner_id",
    "partner__name",
    "partner_name",
)


# ---------------------------------------------------------------------------- keys
def report_key(number: str, report: str) -> str:
    """A progress report of a PD: ``due:report:<PD number>:<progress report>``."""
    return fit_key(f"due:report:{number}:{report}")


def action_points_due_key(section: str, partner: str) -> str:
    """The action points of one section and partner due soon (the review's grouping)."""
    return fit_key(f"due:action_points:{section}:{partner}")


def action_points_overdue_key(section: str, partner: str) -> str:
    """The normal-priority action points of one section and partner already overdue."""
    return fit_key(f"overdue:action_points:{section}:{partner}")


def pd_end_key(number: str) -> str:
    return fit_key(f"due:pd_end:{number}")


def closure_key(number: str) -> str:
    return fit_key(f"closure:{number}")


def twin_of(review_key: str) -> str:
    """The key of the item these checks follow for a daily review finding, when there is one: a
    ``pd_ending_soon:<PD number>`` finding is the PD ending that ``pd_ending`` follows. Empty
    otherwise. (A report becoming overdue closes its item instead, noting ``reports_overdue:<PD
    number>`` as its ``review_key``.)"""
    check, _, number = str(review_key or "").partition(":")
    return pd_end_key(number) if check == "pd_ending_soon" and number else ""


# ---------------------------------------------------------------------------- shared lookups
def _day(value: datetime.date) -> str:
    return f"{value.day} {value:%b %Y}"


def _known(**numbers: Any) -> dict[str, Any]:
    """The evidence numbers that are known (a missing one is left out, never written as None)."""
    return {name: value for name, value in numbers.items() if value is not None}


def _pds(**filters: Any) -> QuerySet[PCA]:
    """Programme documents with only the fields the checks use (never the focal points)."""
    return PCA.objects.select_related("partner").only(*PD_FIELDS).filter(**filters)


def _active_pds(ctx: Context) -> dict[int, PCA]:
    """The active PDs (the daily review's scope), by id, read once per pass."""
    return ctx.memo("deadlines:active_pds", lambda: {pd.pk: pd for pd in _pds(status__in=ACTIVE_PD_STATUSES)})


def _sections(pd: PCA) -> list[str]:
    """The PD's eTools section names (the names the review falls back on when one is missing)."""
    return [name for name in (pd.section_names or pd.sections or []) if str(name or "").strip()]


def _pd_ids(items: Iterable[WatchItem]) -> set[int]:
    return {int(item.entity_key) for item in items if str(item.entity_key or "").isdigit()}


def _review_keys(ctx: Context) -> set[str]:
    """The keys of the findings still open in the latest succeeded daily review up to today."""

    def load() -> set[str]:
        review = (
            DailyReview.objects.filter(status=DailyReview.Status.SUCCEEDED, date__lte=ctx.today)
            .order_by("-date")
            .first()
        )
        if review is None:
            return set()
        findings = review.findings.exclude(state=ReviewFinding.State.RESOLVED)
        return set(findings.values_list("key", flat=True))

    return ctx.memo("deadlines:review_keys", load)


def _status_now(pd: PCA) -> str:
    status = (pd.status or "").strip().replace("_", " ")
    return f"eTools now shows the PD as {status}" if status else "eTools now shows the PD without a status"


# ---------------------------------------------------------------------------- progress reports
def _report_id(row: dict[str, Any]) -> str:
    """A report's identity within its PD: its Partner Reporting Portal report, else its number, else
    its due date."""
    due = row.get("due_date")
    return str(row.get("progress_report") or row.get("report_number") or (due.isoformat() if due else ""))


def _period(start: datetime.date | None, end: datetime.date | None) -> str:
    if start and end:
        return f"{_day(start)} to {_day(end)}"
    return _day(end) if end else ""


def reports_due(ctx: Context) -> Iterator[Candidate]:
    """Progress reports (QPR) of active PDs due in the next 14 days and not submitted, one per report."""
    pds = _active_pds(ctx)
    rows = (
        dm.ReportedIndicator.objects.filter(
            pending_q(),
            intervention_id__in=list(pds),
            report_type=REPORT_TYPE,
            due_date__gte=ctx.today,
            due_date__lte=ctx.today + datetime.timedelta(days=REPORT_DAYS),
        )
        .order_by()
        .values(*REPORT_FIELDS)
    )
    reports: dict[tuple[int, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        reports[(row["intervention_id"], _report_id(row))].append(row)
    for (pd_id, report), group in reports.items():
        yield _report(ctx, pds[pd_id], report, group)


def _report(ctx: Context, pd: PCA, report: str, rows: list[dict[str, Any]]) -> Candidate:
    first = min(rows, key=lambda r: r["due_date"])
    due = first["due_date"]
    left = (due - ctx.today).days
    name = (first["report_number"] or "").strip()
    period = _period(first["period_start"], first["period_end"])
    status = (first["report_status"] or "").strip()
    indicators = len({r["etools_indicator_id"] for r in rows if r["etools_indicator_id"]}) or None
    url = link("reports:programme_detail", pd.pk)
    detail = "The partner has not submitted it in the Partner Reporting Portal yet."
    if period:
        detail += f" It covers {period}."
    return Candidate(
        key=report_key(pd_number(pd), report),
        kind=DEADLINE,
        severity=WARNING if left <= REPORT_WARNING_DAYS else INFO,
        title=f"Progress report due {_day(due)}: {pd_label(pd)}" + (f", {name}" if name else ""),
        detail=detail,
        due_date=due,
        etools_sections=_sections(pd),
        entity_kind=PROGRAMME,
        entity_key=str(pd.pk),
        url=url,
        evidence=evidence(
            ctx,
            REPORT_SOURCE,
            DATAMART,
            [
                record(
                    (name or "Progress report") + (f" ({period})" if period else ""),
                    due,
                    "not submitted" + (f" (status: {status})" if status else ""),
                    url,
                )
            ],
            **_known(indicators=indicators, days_left=left),
        ),
    )


def reports_resolved(ctx: Context, items: list[WatchItem]) -> dict[str, Close]:
    """Reports no longer due soon: submitted (closed at once); past their date and still not submitted
    (handed over to the daily review's overdue reports); given a later date; or their PD no longer
    active. A report gone from eTools gets no reason: it is missed, then gone."""
    pd_ids = _pd_ids(items)
    if not pd_ids:
        return {}
    pds = {pd.pk: pd for pd in _pds(pk__in=pd_ids)}
    rows = (
        dm.ReportedIndicator.objects.filter(intervention_id__in=list(pds), report_type=REPORT_TYPE)
        .order_by()
        .values(
            "intervention_id",
            "progress_report",
            "report_number",
            "due_date",
            "submission_date",
            "report_status",
        )
        .distinct()
    )
    reports: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        reports[report_key(pd_number(pds[row["intervention_id"]]), _report_id(row))].append(row)
    until = ctx.today + datetime.timedelta(days=REPORT_DAYS)
    closes: dict[str, Close] = {}
    for item in items:
        found = reports.get(item.key)
        pd = pds.get(int(item.entity_key)) if str(item.entity_key or "").isdigit() else None
        if not found or pd is None:
            continue
        pending = [r for r in found if not is_submitted(r)]
        if not pending:
            dates = [r["submission_date"] for r in found if r["submission_date"]]
            reason = (
                f"submitted on {_day(max(dates))}"
                if dates
                else "marked submitted in the Partner Reporting Portal"
            )
            closes[item.key] = Close(reason)
            continue
        due = min((r["due_date"] for r in pending if r["due_date"]), default=None)
        active = pd.status in ACTIVE_PD_STATUSES
        if due is not None and due < ctx.today:
            if active:
                closes[item.key] = Close(
                    "now overdue: see the daily review",
                    review_key=f"reports_overdue:{pd_number(pd)}",
                    kind=MISSED,
                )
            else:
                closes[item.key] = Close(
                    f"due on {_day(due)} and not submitted; {_status_now(pd)}", kind=MISSED
                )
        elif not active:
            closes[item.key] = Close(_status_now(pd), kind=CHANGED)
        elif due is not None and due > until:
            closes[item.key] = Close(f"due date moved to {_day(due)}", kind=CHANGED)
    return closes


# ---------------------------------------------------------------------------- action points
def _group_of(row: dict[str, Any]) -> tuple[str, str]:
    """The review's grouping: the section and the partner's name (the linked partner's first)."""
    return row["section"] or "", row["partner__name"] or row["partner_name"] or ""


def _action_points(ctx: Context) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """Every action point, whatever its status, per section and partner, read once per pass."""

    def load() -> dict[tuple[str, str], list[dict[str, Any]]]:
        groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for row in dm.ActionPoint.objects.order_by().values(*ACTION_POINT_FIELDS):
            groups[_group_of(row)].append(row)
        return dict(groups)

    return ctx.memo("deadlines:action_points", load)


def _is_open(row: dict[str, Any]) -> bool:
    return row["status"] in dm.ActionPoint.OPEN_STATUSES and row["due_date"] is not None


def _due_soon(ctx: Context, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    until = ctx.today + datetime.timedelta(days=ACTION_POINT_DAYS)
    return [r for r in rows if _is_open(r) and ctx.today <= r["due_date"] <= until]


def _overdue_normal(ctx: Context, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in rows if _is_open(r) and not r["high_priority"] and r["due_date"] < ctx.today]


def action_points_due(ctx: Context) -> Iterator[Candidate]:
    """Open action points due in the next 14 days, per section and partner."""
    for (section, partner), rows in _action_points(ctx).items():
        due = _due_soon(ctx, rows)
        if due:
            yield _action_point_group(ctx, section, partner, due, overdue=False)


def action_points_overdue(ctx: Context) -> Iterator[Candidate]:
    """Open normal-priority action points past their due date, per section and partner."""
    for (section, partner), rows in _action_points(ctx).items():
        late = _overdue_normal(ctx, rows)
        if late:
            yield _action_point_group(ctx, section, partner, late, overdue=True)


def _action_point_group(
    ctx: Context, section: str, partner: str, rows: list[dict[str, Any]], overdue: bool
) -> Candidate:
    """One section and partner's action points due soon, or overdue at normal priority. The due-soon
    item keeps the Datamart ids of its points (``POINTS``), so that it closes for what became of them."""
    rows = sorted(rows, key=lambda r: (r["due_date"], r["reference_number"] or "", r["datamart_id"]))
    first, last = rows[0]["due_date"], rows[-1]["due_date"]
    n = len(rows)
    high = sum(1 for r in rows if r["high_priority"])
    points = f"action {plural(n, 'point')}"
    who = partner or "no partner"
    where = f" ({section})" if section else ""
    partner_id = next((r["partner_id"] for r in rows if r["partner_id"]), None)
    if overdue:
        url = link("reports:action_points", status="open", overdue="1", q=partner)
        key = action_points_overdue_key(section, partner)
        title = f"{n} {points} overdue since {_day(first)} for {who}{where}"
        detail = (
            f"{n} open normal-priority {points} {'is' if n == 1 else 'are'} past the due date; the oldest "
            f"was due on {_day(first)}. Close what is done in eTools and agree a new date for the rest. "
            "The daily review lists the high-priority ones."
        )
        numbers = {
            "action_points": n,
            "oldest_due": first.isoformat(),
            "days_overdue": (ctx.today - first).days,
        }
    else:
        url = link("reports:action_points", status="open", q=partner)
        key = action_points_due_key(section, partner)
        when = f"on {_day(first)}" if first == last else f"between {_day(first)} and {_day(last)}"
        title = f"{n} {points} due {when} for {who}{where}"
        detail = f"{n} open {points} {'falls' if n == 1 else 'fall'} due {when}"
        if high:
            detail += f", {high} of them high priority" if n > 1 else ", high priority"
        detail += ". Close what is done in eTools and agree a new date for the rest."
        numbers = {"action_points": n, "high_priority": high, "days_left": (first - ctx.today).days}
    records = [
        record(
            (r["reference_number"] or f"{r['related_module'] or 'eTools'} action point".strip())
            + (" (high priority)" if r["high_priority"] else ""),
            r["due_date"],
            "open",
            link("reports:action_points", q=r["reference_number"]) if r["reference_number"] else url,
        )
        for r in rows[:RECORDS_MAX]
    ]
    found = evidence(ctx, ACTION_POINT_SOURCE, DATAMART, records, **numbers)
    if not overdue:
        found[POINTS] = [r["datamart_id"] for r in rows]  # what became of them closes the item
    left = (first - ctx.today).days
    return Candidate(
        key=key,
        kind=CONCERN if overdue else DEADLINE,
        severity=WARNING if overdue or left <= ACTION_POINT_WARNING_DAYS else INFO,
        title=title,
        detail=detail,
        due_date=None if overdue else first,
        etools_sections=[section] if section else [],
        entity_kind=PARTNER if partner_id else "",
        entity_key=str(partner_id) if partner_id else "",
        url=url,
        evidence=found,
    )


def action_points_due_resolved(ctx: Context, items: list[WatchItem]) -> dict[str, Close]:
    """Items whose action points are no longer due in the next 14 days, by what became of the points
    they listed: overdue now (a high-priority one hands over to the daily review), given a later date,
    moved to another section or partner, or closed. Points gone from eTools give no reason: the item
    is missed, then gone."""
    points = {row["datamart_id"]: row for rows in _action_points(ctx).values() for row in rows}
    until = ctx.today + datetime.timedelta(days=ACTION_POINT_DAYS)
    closes: dict[str, Close] = {}
    for item in items:
        ids = (item.evidence or {}).get(POINTS) if isinstance(item.evidence, dict) else None
        rows = [points[pk] for pk in ids or () if pk in points]
        if not rows:
            continue
        late = [r for r in rows if _is_open(r) and r["due_date"] < ctx.today]
        later = [r["due_date"] for r in rows if _is_open(r) and r["due_date"] > until]
        if any(r["high_priority"] for r in late):
            section, partner = _group_of(next(r for r in late if r["high_priority"]))
            closes[item.key] = Close(
                "now overdue: see the daily review",
                review_key=f"action_points_overdue:{section}:{partner}",
                kind=MISSED,
            )
        elif late:
            closes[item.key] = Close("now overdue", kind=MISSED)
        elif _due_soon(ctx, rows):
            closes[item.key] = Close("now listed under another section or partner in eTools", kind=CHANGED)
        elif later:
            closes[item.key] = Close(f"due date moved to {_day(min(later))}", kind=CHANGED)
        else:
            closes[item.key] = Close("closed in eTools")
    return closes


def action_points_overdue_resolved(ctx: Context, items: list[WatchItem]) -> dict[str, Close]:
    """Groups with no normal-priority action point overdue any more: closed, or given a new date. A
    group gone from eTools gives no reason."""
    groups = {action_points_overdue_key(*group): rows for group, rows in _action_points(ctx).items()}
    closes: dict[str, Close] = {}
    for item in items:
        if item.key in groups and not _overdue_normal(ctx, groups[item.key]):
            closes[item.key] = Close("none is overdue any more: closed or given a new date in eTools")
    return closes


# ---------------------------------------------------------------------------- programme documents
def pds_ending(ctx: Context) -> Iterator[Candidate]:
    """Active PDs ending in the next 60 days, whatever their achievement."""
    until = ctx.today + datetime.timedelta(days=PD_ENDING_DAYS)
    flagged = _review_keys(ctx)
    for pd in _active_pds(ctx).values():
        if pd.end is None or not ctx.today <= pd.end <= until:
            continue
        number = pd_number(pd)
        review_key = f"pd_ending_soon:{number}"
        by_review = review_key in flagged
        left = (pd.end - ctx.today).days
        url = link("reports:programme_detail", pd.pk)
        detail = f"It runs from {_day(pd.start)} to {_day(pd.end)}." if pd.start else ""
        if by_review:
            detail += (
                " The daily review flags it too: its indicators are below 70 % of target or not reported."
            )
        detail += (
            " Decide in time on an amendment or a no-cost extension, or plan the final report and payment."
        )
        yield Candidate(
            key=pd_end_key(number),
            kind=DEADLINE,
            severity=WARNING if left <= PD_ENDING_WARNING_DAYS or by_review else INFO,
            title=f"PD ends {_day(pd.end)}: {pd_label(pd)}",
            detail=detail.strip(),
            due_date=pd.end,
            etools_sections=_sections(pd),
            entity_kind=PROGRAMME,
            entity_key=str(pd.pk),
            url=url,
            review_key=review_key if by_review else "",
            evidence=evidence(
                ctx,
                PD_SOURCE,
                DATAMART,
                [record(f"{number} end date", pd.end, pd.status, url)],
                flagged_by_review=by_review,
                days_left=left,
            ),
        )


def pds_ending_resolved(ctx: Context, items: list[WatchItem]) -> dict[str, Close]:
    """PDs no longer ending soon: no longer active, past their end date, or given a later one."""
    pds = {pd.pk: pd for pd in _pds(pk__in=_pd_ids(items))}
    until = ctx.today + datetime.timedelta(days=PD_ENDING_DAYS)
    closes: dict[str, Close] = {}
    for item in items:
        pd = pds.get(int(item.entity_key)) if str(item.entity_key or "").isdigit() else None
        if pd is None or item.key != pd_end_key(pd_number(pd)):
            continue
        if pd.status not in ACTIVE_PD_STATUSES:
            closes[item.key] = Close(_status_now(pd), kind=CHANGED)
        elif pd.end is not None and pd.end < ctx.today:
            closes[item.key] = Close(f"its end date passed on {_day(pd.end)}", kind=CHANGED)
        elif pd.end is not None and pd.end > until:
            closes[item.key] = Close(f"end date moved to {_day(pd.end)}", kind=CHANGED)
    return closes


def _outstanding(pd_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
    """The funds reservations of these PDs not completed and with money outstanding, per PD."""
    rows = (
        dm.FundsReservationHeader.objects.filter(
            intervention_id__in=pd_ids, completed_flag=False, outstanding_amt__gt=0
        )
        .order_by("-outstanding_amt", "fr_number")
        .values("intervention_id", "fr_number", "outstanding_amt", "end_date")
    )
    per_pd: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        per_pd[row["intervention_id"]].append(row)
    return per_pd


def pds_awaiting_closure(ctx: Context) -> Iterator[Candidate]:
    """PDs eTools shows as ended: past their end, not closed yet."""
    pds = list(_pds(status=ENDED))
    money = _outstanding([pd.pk for pd in pds])
    for pd in pds:
        number = pd_number(pd)
        frs = money.get(pd.pk, [])
        total = sum((row["outstanding_amt"] or Decimal(0) for row in frs), Decimal(0))
        since = (ctx.today - pd.end).days if pd.end else None
        late = since is not None and since > CLOSURE_LATE_DAYS and total > 0
        url = link("reports:programme_detail", pd.pk)
        detail = (
            "eTools shows the programme document as ended but not closed: the final report and the "
            "liquidation of its funds reservations are still to be completed."
        )
        if total > 0:
            detail += (
                f" {total:,.0f} USD is still outstanding on {len(frs)} funds "
                f"{plural(len(frs), 'reservation')}."
            )
        records = [record(f"{number} status", pd.end, pd.status, url)]
        records += [
            record(
                f"FR {row['fr_number']}" if row["fr_number"] else "Funds reservation",
                row["end_date"],
                f"{row['outstanding_amt']:,.0f} USD outstanding",
                link("reports:funds", q=row["fr_number"]),
            )
            for row in frs[: RECORDS_MAX - 1]
        ]
        yield Candidate(
            key=closure_key(number),
            kind=CONCERN,
            severity=WARNING if late else INFO,
            title=(
                f"PD ended {_day(pd.end)}, awaiting closure: " if pd.end else "PD ended, awaiting closure: "
            )
            + pd_label(pd),
            detail=detail,
            etools_sections=_sections(pd),
            scope=COUNTRY if late else SECTION,
            entity_kind=PROGRAMME,
            entity_key=str(pd.pk),
            url=url,
            evidence=evidence(
                ctx,
                PD_SOURCE,
                DATAMART,
                records,
                **_known(outstanding=round(total), funds_reservations=len(frs), days_since=since),
            ),
        )


def pds_awaiting_closure_resolved(ctx: Context, items: list[WatchItem]) -> dict[str, Close]:
    """PDs eTools no longer shows as ended (closed, most often)."""
    pds = {pd.pk: pd for pd in _pds(pk__in=_pd_ids(items))}
    closes: dict[str, Close] = {}
    for item in items:
        pd = pds.get(int(item.entity_key)) if str(item.entity_key or "").isdigit() else None
        if pd is not None and item.key == closure_key(pd_number(pd)) and pd.status != ENDED:
            closes[item.key] = Close(_status_now(pd))
    return closes


# ---------------------------------------------------------------------------- the registry
REPORT_DUE_SOON = register(
    Detector(
        id="report_due_soon",
        label="Progress reports due soon",
        run=reports_due,
        resolved=reports_resolved,
        source_jobs=(DATAMART,),
        milestones=REPORT_MILESTONES,
    )
)
ACTION_POINT_DUE = register(
    Detector(
        id="action_point_due",
        label="Action points due soon",
        run=action_points_due,
        resolved=action_points_due_resolved,
        source_jobs=(DATAMART,),
        milestones=ACTION_POINT_MILESTONES,
    )
)
ACTION_POINTS_OVERDUE_NORMAL = register(
    Detector(
        id="action_points_overdue_normal",
        label="Action points overdue (normal priority)",
        run=action_points_overdue,
        resolved=action_points_overdue_resolved,
        source_jobs=(DATAMART,),
    )
)
PD_ENDING = register(
    Detector(
        id="pd_ending",
        label="Programme documents ending soon",
        run=pds_ending,
        resolved=pds_ending_resolved,
        source_jobs=(DATAMART,),
        milestones=PD_ENDING_MILESTONES,
    )
)
PD_AWAITING_CLOSURE = register(
    Detector(
        id="pd_awaiting_closure",
        label="Programme documents awaiting closure",
        run=pds_awaiting_closure,
        resolved=pds_awaiting_closure_resolved,
        source_jobs=(DATAMART,),
    )
)
