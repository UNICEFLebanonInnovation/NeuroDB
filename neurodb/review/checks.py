"""The deterministic checks of the daily review: fourteen questions asked of the programme data
every morning, each answered with findings that say where to look, never with a verdict.

Every check is ``check_<id>(ctx) -> list[Draft]``. The context loads the monitoring rows once
(the PD indicators of the year with what partners reported, from ``neurodb.datamart.monitoring``)
and lends the checks the helpers they share: the active programme documents, their funds, the
children rule, the previous review's snapshot. A check that raises is logged and skipped by
:func:`run_all`; the others still run.
"""

from __future__ import annotations

import datetime
import logging
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import cached_property
from typing import Any
from urllib.parse import urlencode

from django.conf import settings
from django.db.models import Sum
from django.urls import NoReverseMatch, reverse
from django.utils import timezone

from neurodb.core.models import SyncRun
from neurodb.datamart import children, monitoring
from neurodb.datamart import models as dm
from neurodb.datamart.monitoring import ACTIVE_PD_STATUSES, NOT_REPORTED, Filters, Indicator
from neurodb.indicators.services.tracking import OFF_TRACK, ON_TRACK, OVER_TARGET, percentage_elapsed
from neurodb.partnerships.models import PCA, PartnerLink

from .models import DailyReview, ReviewFinding

logger = logging.getLogger(__name__)

CRITICAL = ReviewFinding.Severity.CRITICAL
WARNING = ReviewFinding.Severity.WARNING
INFO = ReviewFinding.Severity.INFO
GOOD = ReviewFinding.Severity.GOOD

REPORT_TYPE = "QPR"
NOT_REPORTED_AFTER_DAYS = 120
TPM_REPORT_GRACE_DAYS = 14
ENDING_SOON_DAYS = 60
ENDING_SOON_ACHIEVED_PERCENT = 70
SPENDING_AHEAD_GAP = 25
SPENDING_AHEAD_ELAPSED = 50
UNDER_DISBURSED_ELAPSED = 60
UNDER_DISBURSED_PERCENT = 40
FINDINGS_WINDOW_DAYS = 30
SYNC_WINDOW_HOURS = 24
AI_DATA_LAST_DAY = 22  # the ai-data job's schedule: days 1-22 of the month (infra/main.bicep)
MAX_PER_CHECK = 200  # a bound, not a display limit: a finding past it would read as resolved the next day
TPM_OPEN_STATUSES = ("assigned", "tpm_accepted")
TPM_NOT_PLANNED = ("draft", "cancelled")
TPM_COMPLETED = ("tpm_reported", "unicef_approved")
OFF_TRACK_RATING = "off"  # matched inside MonitoringFinding.overall_finding_rating, case-insensitive


@dataclass
class Draft:
    """A finding before it is stored: what :func:`neurodb.review.services.run` diffs and ranks."""

    key: str
    check: str
    severity: str
    section: str
    title: str
    detail: str
    evidence: dict[str, Any] = field(default_factory=dict)
    children: int | None = None
    url: str = ""


# ------------------------------------------------------------------------------------ helpers
def pd_number(pd: PCA) -> str:
    return pd.number or f"PD {pd.id}"


def partner_name(pd: PCA) -> str:
    return (pd.partner.name if pd.partner_id and pd.partner else pd.partner_name) or "unknown partner"


def pd_label(pd: PCA) -> str:
    return f"{pd_number(pd)} ({partner_name(pd)})"


def indicator_ref(ind: Indicator) -> str:
    """The indicator's identity across days: its PD number and its eTools indicator key."""
    return f"{pd_number(ind.pd)}:{ind.key}"


def link(name: str, *args: Any, **params: Any) -> str:
    """A page URL with the non-empty query parameters; empty when the page does not exist."""
    try:
        url = reverse(name, args=args)
    except NoReverseMatch:
        return ""
    query = {k: v for k, v in params.items() if v not in (None, "", [])}
    return f"{url}?{urlencode(query, doseq=True)}" if query else url


def pct(value: float | None) -> int | None:
    return round(value) if value is not None else None


def plural(n: int, singular: str, plural_form: str | None = None) -> str:
    return singular if n == 1 else (plural_form or singular + "s")


def _day(value: datetime.date | None) -> str:
    return value.isoformat() if value else ""


def _sync_detail(first_error: str, rows: list[SyncRun]) -> str:
    """The first error message and the datasets concerned, without empty parts."""
    text = (first_error or "").strip()[:200] or "No error message was recorded."
    targets = sorted({r.target for r in rows if r.target})
    if targets:
        text = text.rstrip(".") + f". Datasets: {', '.join(targets[:5])}."
    return text


def _evidence(read: str, records: list[str], **numbers: Any) -> dict[str, Any]:
    return {"read": read, "records": records[:10], "numbers": numbers}


class Context:
    """What the checks read: today, the monitoring rows of the year and shared lookups (each
    loaded once, on first use)."""

    def __init__(self, today: datetime.date, previous: DailyReview | None = None, rows=None):
        self.today = today
        self.year = today.year
        self.previous = previous
        self._rows = rows

    @cached_property
    def rows(self) -> list[Indicator]:
        if self._rows is not None:
            return list(self._rows)
        return monitoring.indicators(Filters(year=self.year, report_type=REPORT_TYPE), self.today)

    @cached_property
    def by_pd(self) -> dict[int, list[Indicator]]:
        grouped: dict[int, list[Indicator]] = defaultdict(list)
        for ind in self.rows:
            grouped[ind.pd.id].append(ind)
        return dict(grouped)

    @cached_property
    def active_pds(self) -> dict[int, PCA]:
        """Every active programme document (the same scope as the monitoring rows), by id."""
        pds = {ind.pd.id: ind.pd for ind in self.rows}
        missing = PCA.objects.select_related("partner").filter(status__in=ACTIVE_PD_STATUSES)
        for pd in missing.exclude(pk__in=list(pds)):
            pds[pd.id] = pd
        return pds

    @cached_property
    def funds(self) -> dict[int, dict[str, float]]:
        """Reserved and disbursed USD per active PD, from its funds reservation headers."""
        rows = (
            dm.FundsReservationHeader.objects.filter(intervention_id__in=list(self.active_pds))
            .values("intervention_id")
            .annotate(reserved=Sum("total_amt"), disbursed=Sum("actual_amt"))
        )
        return {
            r["intervention_id"]: {
                "reserved": float(r["reserved"] or 0),
                "disbursed": float(r["disbursed"] or 0),
            }
            for r in rows
        }

    @cached_property
    def overrides(self) -> children.Overrides:
        return children.overrides()

    @cached_property
    def previous_status(self) -> dict[str, str]:
        if self.previous is None:
            return {}
        return dict((self.previous.stats or {}).get("indicator_status") or {})

    @cached_property
    def health(self) -> dict[str, Any]:
        from neurodb.reports.services import data_health

        return data_health()

    def counts_children(self, ind: Indicator) -> bool:
        """The overview's rule (:mod:`neurodb.datamart.children`): a section's flag first, else a
        plain number whose title names children."""
        return children.counts_children(
            "etools",
            ind.key,
            ind.title,
            ind.tags.get("age_group"),
            self.overrides,
            display_type=ind.display_type,
            unit=ind.unit,
        )

    def children_behind(self, inds: list[Indicator]) -> int | None:
        """Target minus cumulative, summed over the children indicators; None when there is none."""
        total: float | None = None
        for ind in inds:
            if not ind.target or not self.counts_children(ind):
                continue
            total = (total or 0.0) + max(ind.target - (ind.cumulative or 0.0), 0.0)
        return round(total) if total is not None else None

    def elapsed(self, pd: PCA) -> float:
        return percentage_elapsed(pd.start, pd.end, self.today)

    def achieved_percent(self, pd: PCA) -> float | None:
        """Mean of the indicators' achieved share of target (those with a target and a report), each
        capped at 100 % as on the overview, so an over-target indicator cannot hide one behind."""
        values = [min(ind.achieved, 100.0) for ind in self.by_pd.get(pd.id, []) if ind.achieved is not None]
        return sum(values) / len(values) if values else None

    def disbursed_percent(self, pd: PCA) -> float | None:
        funds = self.funds.get(pd.id)
        if not funds or funds["reserved"] <= 0:
            return None
        return funds["disbursed"] / funds["reserved"] * 100

    def pd_section(self, pd: PCA) -> str:
        """The section most of the PD's indicators belong to, else the first section eTools lists."""
        sections = Counter(ind.section for ind in self.by_pd.get(pd.id, []) if ind.section)
        if sections:
            return sections.most_common(1)[0][0]
        names = pd.section_names or pd.sections or []
        return names[0] if names else ""


# ------------------------------------------------------------------------------------- checks
def _grouped_by_section_and_pd(rows: list[Indicator]) -> list[tuple[str, PCA, list[Indicator]]]:
    groups: dict[tuple[str, int], list[Indicator]] = defaultdict(list)
    for ind in rows:
        groups[(ind.section, ind.pd.id)].append(ind)
    return sorted(
        ((section, inds[0].pd, inds) for (section, _pd), inds in groups.items()),
        key=lambda g: (-len(g[2]), pd_number(g[1]), g[0]),
    )


def check_new_off_track(ctx: Context) -> list[Draft]:
    """Indicators off track today that were not off track at the previous review (all of them at
    the first review), per section and programme document."""
    off = [
        ind
        for ind in ctx.rows
        if ind.tracking == OFF_TRACK
        and (ctx.previous is None or ctx.previous_status.get(indicator_ref(ind)) != OFF_TRACK)
    ]
    since = "since the previous review" if ctx.previous else "at this first review"
    drafts = []
    for section, pd, inds in _grouped_by_section_and_pd(off)[:MAX_PER_CHECK]:
        children = ctx.children_behind(inds)
        elapsed = pct(ctx.elapsed(pd))
        total = sum(1 for i in ctx.by_pd.get(pd.id, []) if i.section == section)
        worst = sorted(inds, key=lambda i: i.achieved or 0)
        detail = (
            f"{len(inds)} of the {total} {section or 'unsectioned'} indicators of {pd_number(pd)} "
            f"fell off track {since}, with {elapsed} % of the programme document period elapsed."
        )
        if children:
            detail += f" About {children:,} children are behind target."
        drafts.append(
            Draft(
                key=f"new_off_track:{pd_number(pd)}:{section}",
                check="new_off_track",
                severity=CRITICAL if children else WARNING,
                section=section,
                title=f"{len(inds)} {plural(len(inds), 'indicator')} newly off track in {pd_label(pd)}",
                detail=detail,
                evidence=_evidence(
                    "Compare the cumulative reported value with the target and with the share of the "
                    "PD period elapsed; ask the partner whether the latest report is complete.",
                    [f"{i.title}: {pct(i.achieved)} % of target" for i in worst],
                    indicators=len(inds),
                    children_behind=children,
                    elapsed_percent=elapsed,
                ),
                children=children,
                url=link("reports:pd_monitoring", pd=pd_number(pd), status=OFF_TRACK, year=ctx.year),
            )
        )
    return drafts


def check_not_reported(ctx: Context) -> list[Draft]:
    """Indicators with no progress report although the PD started more than 120 days ago."""
    cutoff = ctx.today - datetime.timedelta(days=NOT_REPORTED_AFTER_DAYS)
    silent = [
        ind
        for ind in ctx.rows
        if ind.tracking == NOT_REPORTED and ind.pd.start is not None and ind.pd.start < cutoff
    ]
    drafts = []
    for section, pd, inds in _grouped_by_section_and_pd(silent)[:MAX_PER_CHECK]:
        days = (ctx.today - pd.start).days
        drafts.append(
            Draft(
                key=f"not_reported:{pd_number(pd)}:{section}",
                check="not_reported",
                severity=WARNING,
                section=section,
                title=f"{len(inds)} {plural(len(inds), 'indicator')} never reported in {pd_label(pd)}",
                detail=(
                    f"The programme document started on {pd.start:%d %b %Y}, {days} days ago, and no "
                    f"quarterly progress report has been read for these {section or ''} indicators."
                ).replace("  ", " "),
                evidence=_evidence(
                    "Check in the Partner Reporting Portal whether a report exists and was accepted; "
                    "an indicator never reported cannot be off track, it is simply unknown.",
                    [i.title for i in inds],
                    indicators=len(inds),
                    pd_start=_day(pd.start),
                    days_since_start=days,
                ),
                url=link("reports:pd_monitoring", pd=pd_number(pd), status=NOT_REPORTED, year=ctx.year),
            )
        )
    return drafts


def check_reports_overdue(ctx: Context) -> list[Draft]:
    """Progress reports past their due date and not submitted, per programme document."""
    rows = (
        dm.ReportedIndicator.objects.filter(
            intervention_id__in=list(ctx.active_pds),
            report_type=REPORT_TYPE,
            submission_date=None,
            due_date__lt=ctx.today,
        )
        .values("intervention_id", "progress_report", "report_number", "due_date")
        .distinct()
    )
    per_pd: dict[int, dict[str, tuple[str, datetime.date]]] = defaultdict(dict)
    for r in rows:
        per_pd[r["intervention_id"]][r["progress_report"] or ""] = (r["report_number"], r["due_date"])
    drafts = []
    for pd_id, reports in per_pd.items():
        pd = ctx.active_pds.get(pd_id)
        if pd is None:
            continue
        oldest = min(due for _, due in reports.values())
        days = (ctx.today - oldest).days
        n = len(reports)
        drafts.append(
            Draft(
                key=f"reports_overdue:{pd_number(pd)}",
                check="reports_overdue",
                severity=WARNING,
                section=ctx.pd_section(pd),
                title=f"{n} progress {plural(n, 'report')} overdue for {pd_label(pd)}",
                detail=(
                    f"The oldest was due on {oldest:%d %b %Y}, {days} days ago, and has not been "
                    "submitted in the Partner Reporting Portal."
                ),
                evidence=_evidence(
                    "Ask the partner for the report; the indicators of this PD keep their last "
                    "reported values until it comes in.",
                    [f"{number or key}: due {_day(due)}" for key, (number, due) in sorted(reports.items())],
                    reports=n,
                    oldest_due=_day(oldest),
                    days_overdue=days,
                ),
                url=link("reports:programme_detail", pd.id),
            )
        )
    drafts.sort(key=lambda d: (-d.evidence["numbers"]["days_overdue"], d.title))
    return drafts[:MAX_PER_CHECK]


def check_spending_ahead(ctx: Context) -> list[Draft]:
    """PDs past half their period whose disbursement runs more than 25 points ahead of delivery."""
    drafts = []
    for pd in ctx.active_pds.values():
        disbursed, achieved, elapsed = ctx.disbursed_percent(pd), ctx.achieved_percent(pd), ctx.elapsed(pd)
        if disbursed is None or achieved is None or elapsed <= SPENDING_AHEAD_ELAPSED:
            continue
        if disbursed - achieved <= SPENDING_AHEAD_GAP:
            continue
        funds = ctx.funds[pd.id]
        drafts.append(
            Draft(
                key=f"spending_ahead:{pd_number(pd)}",
                check="spending_ahead",
                severity=WARNING,
                section=ctx.pd_section(pd),
                title=f"Spending ahead of delivery in {pd_label(pd)}",
                detail=(
                    f"{pct(disbursed)} % of the reserved funds are disbursed while the indicators stand at "
                    f"{pct(achieved)} % of target on average, with {pct(elapsed)} % of the period elapsed."
                ),
                evidence=_evidence(
                    "Disbursements include supplies and operating costs, so compare with the PD's own "
                    "plan; read the latest progress report narrative.",
                    [f"disbursed {funds['disbursed']:,.0f} of {funds['reserved']:,.0f} USD reserved"],
                    disbursed_percent=pct(disbursed),
                    achieved_percent=pct(achieved),
                    elapsed_percent=pct(elapsed),
                    gap=pct(disbursed - achieved),
                ),
                url=link("reports:programme_detail", pd.id),
            )
        )
    drafts.sort(key=lambda d: (-d.evidence["numbers"]["gap"], d.title))
    return drafts[:MAX_PER_CHECK]


def check_under_disbursed(ctx: Context) -> list[Draft]:
    """Active PDs past 60 % of their period with less than 40 % of the reserved funds disbursed."""
    drafts = []
    for pd in ctx.active_pds.values():
        disbursed, elapsed = ctx.disbursed_percent(pd), ctx.elapsed(pd)
        if disbursed is None or elapsed <= UNDER_DISBURSED_ELAPSED or disbursed >= UNDER_DISBURSED_PERCENT:
            continue
        funds = ctx.funds[pd.id]
        drafts.append(
            Draft(
                key=f"under_disbursed:{pd_number(pd)}",
                check="under_disbursed",
                severity=WARNING,
                section=ctx.pd_section(pd),
                title=f"Under-disbursed: {pd_label(pd)} at {pct(disbursed)} % with {pct(elapsed)} % elapsed",
                detail=(
                    f"{pct(elapsed)} % of the period has elapsed but only {funds['disbursed']:,.0f} of the "
                    f"{funds['reserved']:,.0f} USD reserved has been disbursed"
                    + (f"; the PD ends on {pd.end:%d %b %Y}." if pd.end else ".")
                ),
                evidence=_evidence(
                    "Check whether a payment request is pending or the workplan slipped; an extension "
                    "or a budget revision may be needed.",
                    [f"disbursed {funds['disbursed']:,.0f} of {funds['reserved']:,.0f} USD reserved"],
                    disbursed_percent=pct(disbursed),
                    elapsed_percent=pct(elapsed),
                    pd_end=_day(pd.end),
                ),
                url=link("reports:programme_detail", pd.id),
            )
        )
    drafts.sort(key=lambda d: (d.evidence["numbers"]["disbursed_percent"], d.title))
    return drafts[:MAX_PER_CHECK]


def check_pd_ending_soon(ctx: Context) -> list[Draft]:
    """Active PDs ending within 60 days whose indicators are below 70 % of target (or unknown)."""
    horizon = ctx.today + datetime.timedelta(days=ENDING_SOON_DAYS)
    drafts = []
    for pd in ctx.active_pds.values():
        if not pd.end or pd.end < ctx.today or pd.end > horizon:
            continue
        achieved = ctx.achieved_percent(pd)
        if achieved is not None and achieved >= ENDING_SOON_ACHIEVED_PERCENT:
            continue
        days = (pd.end - ctx.today).days
        inds = ctx.by_pd.get(pd.id, [])
        children = ctx.children_behind(inds)
        stand = f"at {pct(achieved)} % of target" if achieved is not None else "with no reported value"
        drafts.append(
            Draft(
                key=f"pd_ending_soon:{pd_number(pd)}",
                check="pd_ending_soon",
                severity=WARNING,
                section=ctx.pd_section(pd),
                title=f"{pd_label(pd)} ends in {days} {plural(days, 'day')} {stand}",
                detail=(
                    f"The programme document ends on {pd.end:%d %b %Y}"
                    + (f" and its {len(inds)} indicators stand {stand} on average." if inds else ".")
                    + (f" About {children:,} children are behind target." if children else "")
                ),
                evidence=_evidence(
                    "Decide early: amendment, no-cost extension or closure; check the final report "
                    "and the last disbursement.",
                    [f"{i.title}: {pct(i.achieved)} % of target" for i in inds if i.achieved is not None],
                    days_left=days,
                    achieved_percent=pct(achieved),
                    children_behind=children,
                    pd_end=_day(pd.end),
                ),
                children=children,
                url=link("reports:programme_detail", pd.id),
            )
        )
    drafts.sort(key=lambda d: (d.evidence["numbers"]["days_left"], d.title))
    return drafts[:MAX_PER_CHECK]


def check_tpm_reports_late(ctx: Context) -> list[Draft]:
    """TPM visits still assigned or accepted more than 14 days after they ended: the report is late."""
    cutoff = ctx.today - datetime.timedelta(days=TPM_REPORT_GRACE_DAYS)
    visits = (
        dm.TPMVisit.objects.filter(status__in=TPM_OPEN_STATUSES, end_date__lt=cutoff)
        .select_related("partner")
        .order_by("end_date", "datamart_id")
    )
    total = visits.count()
    visits = list(visits[:MAX_PER_CHECK])
    sections: dict[int, Counter[str]] = defaultdict(Counter)
    activities = dm.TPMActivity.objects.filter(visit_id__in=[v.id for v in visits]).exclude(section="")
    for visit_id, section in activities.values_list("visit_id", "section"):
        sections[visit_id][section] += 1
    drafts = []
    for visit in visits:
        days = (ctx.today - visit.end_date).days
        partner = (visit.partner.name if visit.partner_id and visit.partner else visit.partner_name) or ""
        reference = visit.reference_number or f"TPM visit {visit.datamart_id}"
        section = sections[visit.id].most_common(1)[0][0] if sections[visit.id] else ""
        drafts.append(
            Draft(
                key=f"tpm_reports_late:{visit.datamart_id}",
                check="tpm_reports_late",
                severity=WARNING,
                section=section,
                title=f"TPM report late: {reference}, visit ended {days} days ago",
                detail=(
                    f"The visit{' of ' + partner if partner else ''} "
                    f"by {visit.tpm_name or 'the TPM partner'} "
                    f"ended on {visit.end_date:%d %b %Y} and its status is still '{visit.status}'; "
                    f"reports are expected within {TPM_REPORT_GRACE_DAYS} days."
                ),
                evidence=_evidence(
                    "Ask the TPM partner for the report in eTools; findings and action points cannot be "
                    "raised before it is submitted.",
                    [reference],
                    days_since_end=days,
                    status=visit.status,
                    visits_late=total,
                ),
                url=link("reports:monitoring", year=visit.end_date.year),
            )
        )
    return drafts


def check_action_points_overdue(ctx: Context) -> list[Draft]:
    """Open high-priority action points past their due date, per section and partner."""
    points = dm.ActionPoint.objects.filter(
        status__in=dm.ActionPoint.OPEN_STATUSES, high_priority=True, due_date__lt=ctx.today
    ).values(
        "section",
        "partner_name",
        "partner__name",
        "due_date",
        "reference_number",
        "related_module",
    )
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for p in points:
        partner = p["partner__name"] or p["partner_name"] or ""
        groups[(p["section"] or "", partner)].append(p)
    drafts = []
    for (section, partner), rows in groups.items():
        oldest = min(p["due_date"] for p in rows)
        days = (ctx.today - oldest).days
        n = len(rows)
        who = partner or "no partner"
        drafts.append(
            Draft(
                key=f"action_points_overdue:{section}:{partner}",
                check="action_points_overdue",
                severity=CRITICAL,
                section=section,
                title=f"{n} high-priority action {plural(n, 'point')} overdue for {who}"
                + (f" ({section})" if section else ""),
                detail=f"The oldest was due on {oldest:%d %b %Y}, {days} days ago.",
                evidence=_evidence(
                    "Open the action points in eTools: close what is done, re-plan what is not, "
                    "and escalate what the partner has not answered.",
                    [
                        f"{p['reference_number'] or p['related_module']}: due {_day(p['due_date'])}"
                        for p in rows
                    ],
                    action_points=n,
                    oldest_due=_day(oldest),
                    days_overdue=days,
                ),
                url=link("reports:action_points", overdue="1", priority="1", status="open"),
            )
        )
    drafts.sort(key=lambda d: (-d.evidence["numbers"]["action_points"], d.title))
    return drafts[:MAX_PER_CHECK]


def check_findings_off_track(ctx: Context) -> list[Draft]:
    """Field monitoring findings rated off track in the last 30 days, per partner."""
    since = ctx.today - datetime.timedelta(days=FINDINGS_WINDOW_DAYS)
    findings = dm.MonitoringFinding.objects.filter(
        overall_finding_rating__icontains=OFF_TRACK_RATING, end_date__gte=since, end_date__lte=ctx.today
    ).select_related("partner")
    groups: dict[str, list[dm.MonitoringFinding]] = defaultdict(list)
    for f in findings:
        name = (f.partner.name if f.partner_id and f.partner else "") or f.vendor_number or "unknown partner"
        groups[name].append(f)
    drafts = []
    for partner, rows in groups.items():
        n = len(rows)
        rating = rows[0].overall_finding_rating
        drafts.append(
            Draft(
                key=f"findings_off_track:{partner}",
                check="findings_off_track",
                severity=WARNING,
                section="",
                title=(
                    f"{n} field monitoring {plural(n, 'finding')} off track for {partner} in the last 30 days"
                ),
                detail=(
                    f"The latest visit ended on {max(f.end_date for f in rows):%d %b %Y}; "
                    "the entities monitored are listed in the evidence."
                ),
                evidence=_evidence(
                    "Read the narrative findings and check whether an action point was raised for each.",
                    [f"{f.monitoring_activity or f.reference_number}: {f.entity}" for f in rows],
                    findings=n,
                    last_visit=_day(max(f.end_date for f in rows)),
                ),
                url=link("reports:monitoring", rating=rating, year=ctx.year),
            )
        )
    drafts.sort(key=lambda d: (-d.evidence["numbers"]["findings"], d.title))
    return drafts[:MAX_PER_CHECK]


def check_sync_failures(ctx: Context) -> list[Draft]:
    """Sync runs that failed or wrote with errors in the last 24 hours, per job."""
    since = timezone.now() - datetime.timedelta(hours=SYNC_WINDOW_HOURS)
    runs = (
        SyncRun.objects.filter(
            status__in=(SyncRun.Status.FAILED, SyncRun.Status.PARTIAL), started_at__gte=since
        )
        .exclude(job=SyncRun.Job.DAILY_REVIEW)
        .order_by("-started_at")
    )
    groups: dict[str, list[SyncRun]] = defaultdict(list)
    for run in runs:
        groups[run.job].append(run)
    labels = dict(SyncRun.Job.choices)
    drafts = []
    for job, rows in groups.items():
        failed = sum(1 for r in rows if r.status == SyncRun.Status.FAILED)
        partial = len(rows) - failed
        first_error = next((r.error for r in rows if r.error), "")
        drafts.append(
            Draft(
                key=f"sync_failures:{job}",
                check="sync_failures",
                severity=WARNING,
                section="",
                title=f"{labels.get(job, job)}: {failed} failed and {partial} partial "
                f"{plural(len(rows), 'run')} in the last 24 hours",
                detail=_sync_detail(first_error, rows),
                evidence=_evidence(
                    "Open the run on the Data health page: the details list the failed items; re-run "
                    "the job for that dataset.",
                    [
                        f"{r.target or job} {r.status} at {timezone.localtime(r.started_at):%H:%M}"
                        for r in rows
                    ],
                    failed=failed,
                    partial=partial,
                ),
                url=link("reports:data_health"),
            )
        )
    return drafts


def check_data_quality(ctx: Context) -> list[Draft]:
    """Datasets whose last sync failed to write rows, and ActivityInfo partner names still unlinked."""
    drafts = []
    failing = [
        row
        for row in ctx.health.get("datasets", [])
        if row.get("run") is not None and (row["run"].rows_failed or 0) > 0
    ]
    if failing:
        n = len(failing)
        drafts.append(
            Draft(
                key="data_quality:datamart",
                check="data_quality",
                severity=INFO,
                section="",
                title=f"{n} eTools {plural(n, 'dataset')} had rows that failed to write at the last sync",
                detail="Numbers from these datasets may be incomplete: "
                + ", ".join(f"{row['name']} ({row['run'].rows_failed} failed)" for row in failing[:8])
                + ".",
                evidence=_evidence(
                    "The Data health page lists why the rows failed (usually a partner or PD missing "
                    "upstream); a full sync of partners and programme documents often fixes it.",
                    [row["name"] for row in failing],
                    datasets=n,
                    rows_failed=sum(row["run"].rows_failed for row in failing),
                ),
                url=link("reports:data_health"),
            )
        )
    unlinked = PartnerLink.objects.filter(partner=None, records__gt=0).count()
    if unlinked:
        drafts.append(
            Draft(
                key="data_quality:partner_links",
                check="data_quality",
                severity=INFO,
                section="",
                title=(
                    f"{unlinked} ActivityInfo partner {plural(unlinked, 'name')} "
                    "not linked to an eTools partner"
                ),
                detail=(
                    "Their activity records cannot be shown next to the partner's eTools data until the "
                    "name is linked in the admin."
                ),
                evidence=_evidence(
                    "Admin, Partnerships, ActivityInfo partner links: pick the eTools partner for each name.",
                    [],
                    unlinked=unlinked,
                ),
                url=link("admin:etools_partnerlink_changelist"),
            )
        )
    return drafts


def check_locations_unplaced(ctx: Context) -> list[Draft]:
    """PD indicator rows of active PDs naming a location that no eTools location resolves."""
    count = (
        dm.PDIndicator.objects.filter(location=None, intervention__status__in=ACTIVE_PD_STATUSES)
        .exclude(location_name="")
        .count()
    )
    if not count:
        return []
    return [
        Draft(
            key="locations_unplaced",
            check="locations_unplaced",
            severity=INFO,
            section="",
            title=f"{count} indicator location {plural(count, 'row')} not placed on the map",
            detail=(
                "These rows name a location but no eTools location (P-code) was resolved for it; they "
                "count in the totals but cannot be mapped or rolled up by governorate."
            ),
            evidence=_evidence(
                "Run the locations sync, then the PD indicators sync; a name that still does not "
                "resolve is spelled differently in eTools.",
                [],
                rows=count,
            ),
            url=link("reports:pd_monitoring_map", year=ctx.year),
        )
    ]


def check_stale_sources(ctx: Context) -> list[Draft]:
    """Scheduled jobs whose last success is older than SYNC_STALENESS_HOURS."""
    from neurodb.integrations.management.commands.check_sync_freshness import SCHEDULED_JOBS

    drafts = []
    for job in ctx.health.get("jobs", []):
        if job["job"] not in SCHEDULED_JOBS or job["state"] != "stale":
            continue
        if job["job"] == SyncRun.Job.ACTIVITYINFO_DATA and ctx.today.day > AI_DATA_LAST_DAY:
            continue  # the ActivityInfo import only runs on days 1 to 22: expected, not stale
        age = job.get("age_hours")
        drafts.append(
            Draft(
                key=f"stale_sources:{job['job']}",
                check="stale_sources",
                severity=INFO,
                section="",
                title=f"{job['label']} is stale: last success {round(age) if age else '?'} hours ago",
                detail=(
                    f"The limit is {settings.SYNC_STALENESS_HOURS} hours; numbers from this source may "
                    "be out of date until the job runs again."
                ),
                evidence=_evidence(
                    "Check the job's last run on the Data health page and start it again from the admin.",
                    [job["job"]],
                    age_hours=age,
                    limit_hours=settings.SYNC_STALENESS_HOURS,
                ),
                url=link("reports:data_health"),
            )
        )
    return drafts


def check_improvements(ctx: Context) -> list[Draft]:
    """Indicators that were off track at the previous review and are on track or over target now."""
    if ctx.previous is None:
        return []
    better = [
        ind
        for ind in ctx.rows
        if ind.tracking in (ON_TRACK, OVER_TARGET)
        and ctx.previous_status.get(indicator_ref(ind)) == OFF_TRACK
    ]
    drafts = []
    for section, pd, inds in _grouped_by_section_and_pd(better)[:MAX_PER_CHECK]:
        drafts.append(
            Draft(
                key=f"improvements:{pd_number(pd)}:{section}",
                check="improvements",
                severity=GOOD,
                section=section,
                title=f"{len(inds)} {plural(len(inds), 'indicator')} back on track in {pd_label(pd)}",
                detail=(
                    f"Off track at the review of {ctx.previous.date:%d %b %Y}, on track or over target today."
                ),
                evidence=_evidence(
                    "A new progress report usually explains it.",
                    [f"{i.title}: {pct(i.achieved)} % of target" for i in inds],
                    indicators=len(inds),
                ),
                url=link("reports:pd_monitoring", pd=pd_number(pd), year=ctx.year),
            )
        )
    return drafts


CHECKS: list[tuple[str, Callable[[Context], list[Draft]]]] = [
    ("new_off_track", check_new_off_track),
    ("not_reported", check_not_reported),
    ("reports_overdue", check_reports_overdue),
    ("spending_ahead", check_spending_ahead),
    ("under_disbursed", check_under_disbursed),
    ("pd_ending_soon", check_pd_ending_soon),
    ("tpm_reports_late", check_tpm_reports_late),
    ("action_points_overdue", check_action_points_overdue),
    ("findings_off_track", check_findings_off_track),
    ("sync_failures", check_sync_failures),
    ("data_quality", check_data_quality),
    ("locations_unplaced", check_locations_unplaced),
    ("stale_sources", check_stale_sources),
    ("improvements", check_improvements),
]
CHECK_IDS = [check_id for check_id, _ in CHECKS]


def run_all(ctx: Context) -> tuple[list[Draft], dict[str, str]]:
    """Every check's drafts, and the error of each check that raised (logged, never fatal)."""
    drafts: list[Draft] = []
    errors: dict[str, str] = {}
    for check_id, check in CHECKS:
        try:
            drafts.extend(check(ctx))
        except Exception as exc:
            logger.exception("daily review: check %s failed", check_id)
            errors[check_id] = f"{type(exc).__name__}: {exc}"[:500]
    return drafts, errors


def snapshot(ctx: Context) -> dict[str, Any]:
    """The day's counts, kept on the review: what the next review diffs against and what the page
    reads for its 'changed' strip."""
    counts = Counter(ind.tracking for ind in ctx.rows)
    tracked = counts.get(ON_TRACK, 0) + counts.get(OFF_TRACK, 0) + counts.get(OVER_TARGET, 0)
    planned = dm.TPMVisit.objects.filter(start_date__year=ctx.year).exclude(status__in=TPM_NOT_PLANNED)
    planned_n = planned.count()
    completed_n = planned.filter(status__in=TPM_COMPLETED).count()
    past_due = dm.ActionPoint.objects.filter(
        status__in=dm.ActionPoint.OPEN_STATUSES, due_date__lt=ctx.today
    ).count()
    return {
        "indicators": len(ctx.rows),
        "programme_documents": len(ctx.by_pd),
        "partners": len({ind.pd.partner_id for ind in ctx.rows if ind.pd.partner_id}),
        "status_counts": {key: counts.get(key, 0) for key in monitoring.LABELS},
        "on_track_percent": round(counts.get(ON_TRACK, 0) * 100 / tracked, 1) if tracked else None,
        "indicator_status": {indicator_ref(ind): ind.tracking for ind in ctx.rows},
        "action_points_past_due": past_due,
        "tpm_completion": {
            "planned": planned_n,
            "completed": completed_n,
            "percent": round(completed_n * 100 / planned_n, 1) if planned_n else None,
        },
    }
