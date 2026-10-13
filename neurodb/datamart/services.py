"""Queries over the eTools Datamart tables for the programme, partner, donor and assurance pages."""

from __future__ import annotations

import datetime
import re
from collections import Counter, defaultdict
from collections.abc import Iterator
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.db.models import Count, Max, Q, QuerySet, Sum
from django.db.models.functions import ExtractYear

from neurodb.core import compute
from neurodb.partnerships import statuses as pd_statuses
from neurodb.partnerships.models import PCA, PartnerOrganization

from . import fm, headline, keys, reached, reporting
from . import models as dm
from . import monitoring as pd_monitoring

ENGAGEMENT_TYPES = {
    "audit": "Audit",
    "sa": "Special audit",
    "sc": "Spot check",
    "ma": "Micro-assessment",
}
ENGAGEMENT_STATUSES = {
    "partner_contacted": "Partner contacted",
    "report_submitted": "Report submitted",
    "final": "Final",
    "cancelled": "Cancelled",
}


# TPM visit statuses that are not (or no longer) planned visits, as the overview counts them
TPM_NOT_PLANNED = ("draft", "cancelled")
TPM_COMPLETED = ("tpm_reported", "unicef_approved")


def engagement_type_label(code: str) -> str:
    return ENGAGEMENT_TYPES.get(code or "", (code or "—").replace("_", " ").capitalize())


def _with_type_labels(engagements: list[dm.AuditEngagement]) -> list[dm.AuditEngagement]:
    for engagement in engagements:
        engagement.type_label = engagement_type_label(engagement.engagement_type)
    return engagements


def _getlist(params, key: str) -> list[str]:
    values = params.getlist(key) if hasattr(params, "getlist") else params.get(key, [])
    if isinstance(values, str):
        values = [values]
    return [v for v in values if v]


def _partner_q(q: str) -> Q:
    """Matches the partner linked to a row by its name, short name or vendor number."""
    return (
        Q(partner__name__icontains=q)
        | Q(partner__short_name__icontains=q)
        | Q(partner__vendor_number__icontains=q)
    )


def _year(params) -> int | None:
    return pd_monitoring.year_param(params.get("year"))


# ------------------------------------------------------------------------------ programme documents
def indicators_for(queryset: QuerySet[dm.PDIndicator]) -> list[dict[str, Any]]:
    """One entry per indicator (the Datamart repeats it per location and disaggregation)."""
    grouped: dict[Any, dict[str, Any]] = {}
    for row in queryset.order_by("lower_result_name", "title", "datamart_id"):
        key = row.source_id or ("row", row.datamart_id)
        entry = grouped.get(key)
        if entry is None:
            entry = grouped[key] = {
                "title": row.title,
                "output": row.lower_result_name,
                "section": row.section_name,
                "unit": row.unit,
                "display_type": row.display_type,
                "baseline": _ratio(row.baseline_numerator, row.baseline_denominator, row.display_type),
                "target": _ratio(row.target_numerator, row.target_denominator, row.display_type),
                "cluster": row.cluster_name,
                "high_frequency": row.is_high_frequency,
                "active": row.is_active,
                "locations": [],
                "pd_reference_number": row.pd_reference_number,
            }
        if row.location_name and row.location_name not in entry["locations"]:
            entry["locations"].append(row.location_name)
    return list(grouped.values())


def _ratio(numerator: Decimal | None, denominator: Decimal | None, display_type: str) -> str:
    """eTools shows a number, a percentage or a ratio depending on the indicator's display type."""
    if numerator is None:
        return ""
    if display_type == "percentage" and denominator:
        return f"{numerator / denominator * 100:.0f}%"
    if display_type == "ratio" and denominator:
        return f"{numerator.normalize():f}/{denominator.normalize():f}"
    return f"{numerator.normalize():f}"


def _fr_totals(pd: PCA, lines: list[dm.FundsReservation]) -> dict[str, Any]:
    """FR amounts of a PD: from the FR headers when synced, else from the lines (whose FR totals
    repeat on each line, so each FR counts once)."""
    headers = list(pd.fr_headers.order_by("-start_date"))
    if headers:
        # one entry per header row, as the table under the heading and /funds/ list them
        frs = {h.pk: (h.total_amt, h.actual_amt, h.outstanding_amt) for h in headers}
    else:
        frs = {}
        for line in lines:
            frs.setdefault(line.fr_number, (line.total_amt, line.actual_amt, line.outstanding_amt))
    return {
        "fr_headers": headers,
        "fr_count": len(frs),
        "fr_total": sum((t or 0) for t, _, _ in frs.values()),
        "fr_actual": sum((a or 0) for _, a, _ in frs.values()),
        "fr_outstanding": sum((o or 0) for _, _, o in frs.values()),
    }


def workplan(queryset: QuerySet[dm.PDActivity]) -> list[dict[str, Any]]:
    """Workplan activities grouped by PD output; an activity repeats once per budget line."""
    outputs: dict[str, dict[str, Any]] = {}
    seen: set[tuple[str, str]] = set()
    for row in queryset.order_by("result_code", "code", "datamart_id"):
        if (row.code, row.name) in seen:
            continue
        seen.add((row.code, row.name))
        output = outputs.setdefault(
            row.result or "—", {"output": row.result or "—", "activities": [], "unicef": 0, "partner": 0}
        )
        output["activities"].append(row)
        output["unicef"] += row.unicef_cash or 0
        output["partner"] += row.cso_cash or 0
    return list(outputs.values())


def visits_by_year(pd: PCA) -> list[dict[str, Any]]:
    """Programmatic visits planned (eTools PD plan) and done (staff trips, TPM visits) per year, and
    the field monitoring visits that monitored the PD (one per visit, by the year of its start date,
    else its end date)."""
    years: dict[int, dict[str, int]] = defaultdict(lambda: {"planned": 0, "staff": 0, "tpm": 0, "fm": 0})
    for plan in pd.planned_visits_by_year.exclude(year=None):
        years[plan.year]["planned"] += plan.total
    for day in (
        pd.programmatic_visits.filter(travel_type__icontains="programmatic")
        .exclude(date=None)
        .values_list("date", flat=True)
    ):
        years[day.year]["staff"] += 1
    for _visit, day in (
        pd.tpm_activities.exclude(date=None)
        .order_by()
        .values_list("visit_reference_number", "date")
        .distinct()
    ):
        years[day.year]["tpm"] += 1
    for year, n in fm.visits_by_year(dm.MonitoringFinding.objects.filter(intervention=pd)).items():
        years[year]["fm"] += n
    return [{"year": y, **v} for y, v in sorted(years.items(), reverse=True)]


REPORT_FIELDS = reporting.REPORT_FIELDS


def progress_reports(queryset: QuerySet[dm.ReportedIndicator]) -> list[dict[str, Any]]:
    """One row per progress report, with its status and its indicator reports
    (:func:`neurodb.datamart.reporting.progress_reports`, the one place that groups them)."""
    return reporting.progress_reports(queryset, datetime.date.today())


def latest_progress(
    pd: PCA, today: datetime.date | None = None, rows: list[pd_monitoring.Indicator] | None = None
) -> list[dict[str, Any]]:
    """Each indicator of the PD with its Reached (``neurodb.datamart.reached``, the rule of every
    page): target, Reached to date, % of the target, what awaits acceptance and the latest counted
    report. Indicators nobody reported on yet are listed too, as not reported. ``rows``: the PD's
    partner monitoring rows when already read."""
    if rows is None:
        rows = pd_monitoring.indicators(pd_monitoring.Filters(pd_ids=[pd.id], scope="all"), today)
    out = []
    for row in rows:
        latest = row.result.latest if row.result else None
        out.append(
            {
                "indicator": row.title,
                "output": row.output,
                "url": row.url,
                "target": row.target,
                "display_type": row.display_type,
                "reached": row.reached,
                "achieved": row.achieved,
                "awaiting": row.awaiting,
                "awaiting_reports": row.awaiting_reports,
                "tracking": row.tracking,
                "tracking_label": row.tracking_label,
                "value_status": row.value_status,
                "status": row.partner_status,
                "report": latest.number if latest else "",
                "report_status": latest.status_label if latest else "",
                "report_type": row.report_type,
                "period_end": row.as_of,
                "method": row.method_label,
                "additive": row.additive,
            }
        )
    return out


def _flag_overdue(points: list[dm.ActionPoint]) -> list[dm.ActionPoint]:
    """Marks the open action points past their due date, as the Action points page flags them."""
    today = datetime.date.today()
    for point in points:
        point.overdue = bool(
            point.status in dm.ActionPoint.OPEN_STATUSES and point.due_date and point.due_date < today
        )
    return points


def programme_datamart(pd: PCA, today: datetime.date | None = None) -> dict[str, Any]:
    lines = list(dm.FundsReservation.objects.filter(intervention=pd))
    action_points = dm.ActionPoint.objects.filter(intervention=pd)
    monitoring = _monitoring_summary(pd_monitoring.Filters(pd_ids=[pd.id], scope="all"))
    # the PD's main indicator (its children reached on the overview, the brief and the donor page): its
    # unique beneficiaries indicator, else its largest children indicator in the current year, with the
    # overview's set-aside guard, so the PD page names the indicator the other pages count this year
    year = (today or datetime.date.today()).year
    mains = headline.mains_of(monitoring["rows"], year=year)
    main = monitoring["main"] = mains.get(pd.id)
    monitoring["main_year"] = year
    monitoring["number_keys"] = {r.key for r in monitoring["rows"] if headline.is_number(r)}
    # the unique beneficiaries indicator chosen for the PD, and why it does not count when it does not
    # (so the page says so and offers to undo the choice, instead of hiding it)
    flagged = monitoring["flagged_key"] = headline.flagged_keys([pd.id]).get(pd.id)
    monitoring["flag_unused"] = ""
    if flagged and not (main is not None and main.flagged):
        row = next((r for r in monitoring["rows"] if r.key == flagged), None)
        if row is None:
            monitoring["flag_unused"] = "missing"  # no longer among the PD's indicators in eTools
        elif not headline.is_number(row):
            monitoring["flag_unused"] = "not_number"  # now a percentage or a ratio
        else:
            monitoring["flag_unused"] = "implausible"  # set aside: its value cannot be true
    return {
        "fr_lines": lines,
        **_fr_totals(pd, lines),
        "indicators": indicators_for(dm.PDIndicator.objects.filter(intervention=pd)),
        "workplan": workplan(pd.workplan_activities.all()),
        "action_points": _flag_overdue(list(action_points.order_by("status", "due_date")[:50])),
        "open_action_points": action_points.filter(status__in=dm.ActionPoint.OPEN_STATUSES).count(),
        "engagements": _with_type_labels(list(pd.datamart_engagements.order_by("-start_date"))),
        "visits": visits_by_year(pd),
        "tpm_activities": list(pd.tpm_activities.order_by("-date")[:20]),
        "reports": list(progress_reports(pd.reported_indicators.all())[:12]),
        "latest_progress": latest_progress(pd, rows=monitoring["rows"]),
        "monitoring": monitoring,
    }


def _monitoring_summary(filters: pd_monitoring.Filters) -> dict[str, Any]:
    """On/off-track counts of the PD indicators the partner monitoring page shows for ``filters``
    ("with Reached": a counted report carries a value, as the grid counts it)."""
    rows = pd_monitoring.indicators(filters)
    counts = Counter(r.tracking for r in rows)
    with_reached = sum(1 for r in rows if r.reached is not None)
    return {
        "rows": rows,
        "indicators": len(rows),
        "status_counts": {key: counts.get(key, 0) for key in pd_monitoring.LABELS},
        # ahead of schedule is on track too, as the daily review and the brief count it
        "on_track_or_ahead": counts.get("on_track", 0) + counts.get("over_target", 0),
        "with_reached": with_reached,
        "reported": with_reached,
        "awaiting_acceptance": sum(1 for r in rows if r.awaiting_reports),
    }


# ---------------------------------------------------------------------------------------- partners
def reports_by_year(queryset: QuerySet[dm.ReportedIndicator]) -> list[tuple[str, int]]:
    """Progress reports per year of their period end (partner page: reporting activity over time)."""
    return _by_period_year(progress_reports(queryset))


def _by_period_year(reports: list[dict[str, Any]]) -> list[tuple[str, int]]:
    """:func:`reports_by_year` of ``progress_reports`` rows already read."""
    years: Counter[str] = Counter()
    for r in reports:
        if r["period_end"]:
            years[str(r["period_end"].year)] += 1
    return sorted(years.items())


def report_status(report: dict[str, Any], today: datetime.date) -> str:
    """One of the six report statuses (``reached.status_of``) of a ``progress_reports`` row."""
    return reporting.report_status(report, today)


def reporting_summary(queryset: QuerySet[dm.ReportedIndicator]) -> dict[str, Any]:
    """Progress reports by status: Accepted, Submitted (awaiting acceptance), Sent back, Due, Overdue
    and Not yet due (``reached.status_of``, one status each, so they add up to ``reports``), and
    those submitted after their due date. Overdue follows ``monitoring.pending_q`` as every page does."""
    return _status_summary(progress_reports(queryset))


def _status_summary(reports: list[dict[str, Any]]) -> dict[str, Any]:
    statuses = Counter(r["status"] for r in reports)
    return {
        "reports": len(reports),
        "statuses": {key: statuses.get(key, 0) for key in reached.STATUS_LABELS},
        **{key: statuses.get(key, 0) for key in reached.STATUS_LABELS},
        "late": sum(
            1
            for r in reports
            if r["submission_date"] and r["due_date"] and r["submission_date"] > r["due_date"]
        ),
    }


def partner_datamart(partner: PartnerOrganization) -> dict[str, Any]:
    all_engagements = dm.AuditEngagement.objects.filter(partner=partner)
    engagements = _with_type_labels(list(all_engagements.order_by("-start_date")[:50]))
    engagement_counts: Counter[str] = Counter()
    for row in all_engagements.order_by().values("engagement_type").annotate(n=Count("id")):
        engagement_counts[engagement_type_label(row["engagement_type"])] += row["n"]
    action_points = dm.ActionPoint.objects.filter(partner=partner)
    findings = dm.MonitoringFinding.objects.filter(partner=partner)
    tpm_visits = list(dm.TPMVisit.objects.filter(partner=partner).order_by("-start_date")[:20])
    recent_findings = list(findings.order_by("-end_date")[:20])
    staff_visits = partner.programmatic_visits.exclude(date=None)
    visits: dict[int, int] = defaultdict(int)
    for day in staff_visits.filter(travel_type__icontains="programmatic").values_list("date", flat=True):
        visits[day.year] += 1
    # the partner's progress reports as the Progress reports page reads them for the partner (its
    # "All reports" link: ``?partner=<id>&pd_status=``): by the partner link, every PD status but
    # draft and cancelled; read once for the summary, the list and the chart
    reported = partner.reported_indicators.exclude(intervention__status__in=pd_statuses.NOT_A_PARTNERSHIP)
    reports = progress_reports(reported)
    return {
        "assessments": list(
            dm.PartnerAssessment.objects.filter(partner=partner).order_by("-completed_date")[:20]
        ),
        "psea": dm.PSEAAssessment.objects.filter(partner=partner).order_by("-assessment_date").first(),
        "engagements": engagements,
        "engagements_total": sum(engagement_counts.values()),
        # over every engagement of the partner, not only the 50 listed
        "engagement_counts": dict(engagement_counts.most_common()),
        "audit_findings": list(partner.audit_findings.order_by("-created")[:20]),
        "action_points": _flag_overdue(
            list(action_points.filter(status__in=dm.ActionPoint.OPEN_STATUSES).order_by("due_date")[:20])
        ),
        "open_action_points": action_points.filter(status__in=dm.ActionPoint.OPEN_STATUSES).count(),
        "overdue_action_points": action_points.filter(
            status__in=dm.ActionPoint.OPEN_STATUSES, due_date__lt=datetime.date.today()
        ).count(),
        "tpm_visits": tpm_visits,
        "findings": recent_findings,
        "monitoring_count": len(tpm_visits) + len(recent_findings),
        "finding_ratings": _finding_ratings(findings),
        "hact_years": list(partner.hact_years.order_by("-year")[:4]),
        "monitoring": _monitoring_summary(pd_monitoring.Filters(partners=[str(partner.pk)])),
        "reporting": _status_summary(reports),  # reporting_summary(reported)
        "reports": reports[:10],
        "reports_by_year": _by_period_year(reports),  # reports_by_year(reported)
        "staff_visits_by_year": sorted(visits.items()),
        "monitoring_visits_by_year": _monitoring_visits_by_year(partner),
    }


def _finding_ratings(findings: QuerySet[dm.MonitoringFinding]) -> dict[str, int]:
    """Field monitoring findings per overall rating ("—" for none), counted by the database instead of
    reading every finding, in the order the findings come (latest end date first, the undated ones before
    them, as the database sorts them): each rating where its first finding stands. Ratings whose first
    findings share an end date (or are all undated) come in no particular order, as when every finding was
    read: the database's order of rows with the same date was never fixed either."""
    counts: dict[str, int] = {}
    first: dict[str, tuple[int, int]] = {}
    rows = (
        findings.order_by()
        .values("overall_finding_rating")
        .annotate(n=Count("pk"), latest=Max("end_date"), undated=Count("pk", filter=Q(end_date=None)))
    )
    for row in rows:
        rating = row["overall_finding_rating"] or "—"
        counts[rating] = counts.get(rating, 0) + row["n"]
        latest = row["latest"]
        place = (0, 0) if row["undated"] else (1, -latest.toordinal() if latest else 0)
        first[rating] = min(first.get(rating, place), place)
    return {rating: counts[rating] for rating in sorted(counts, key=first.__getitem__)}


def _monitoring_visits_by_year(partner: PartnerOrganization) -> dict[str, dict[int, int]]:
    """Field monitoring visits (one per monitoring activity, however many findings it has) and planned
    third-party visits (drafts and cancelled ones left out) of a partner, per year (a field monitoring
    visit by its start date, else its end date)."""
    # a visit's date as fm.visits_by_year and fmm.Visit.visit_date read it: the earliest start date of
    # its rows, else their latest end date (not the date of whichever row comes first)
    first: dict[str, datetime.date] = {}
    last: dict[str, datetime.date] = {}
    for activity, pk, start, end in (
        dm.MonitoringFinding.objects.filter(partner=partner)
        .exclude(start_date=None, end_date=None)
        .order_by()
        .values_list("monitoring_activity", "pk", "start_date", "end_date")
    ):
        key = activity or f"#{pk}"
        if start and (key not in first or start < first[key]):
            first[key] = start
        if end and (key not in last or end > last[key]):
            last[key] = end
    field: Counter[int] = Counter((first.get(key) or last[key]).year for key in first.keys() | last.keys())
    tpm: Counter[int] = Counter(
        day.year
        for day in dm.TPMVisit.objects.filter(partner=partner)
        .exclude(start_date=None)
        .exclude(status__in=TPM_NOT_PLANNED)
        .values_list("start_date", flat=True)
    )
    return {"field": dict(field), "tpm": dict(tpm)}


def visits_chart(staff: list[tuple[int, int]], monitoring: dict[str, dict[int, int]]) -> dict[str, Any]:
    """The partner page's visits card: UNICEF staff trips, field monitoring and third-party visits per
    year, as grouped bars ({labels, series}); empty when there is none."""
    staff_by_year = dict(staff)
    years = sorted(set(staff_by_year) | set(monitoring["field"]) | set(monitoring["tpm"]))
    if not years:
        return {}
    return {
        "labels": [str(y) for y in years],
        "series": {
            "UNICEF staff trips": [staff_by_year.get(y, 0) for y in years],
            "Field monitoring": [monitoring["field"].get(y, 0) for y in years],
            "Third-party monitoring": [monitoring["tpm"].get(y, 0) for y in years],
        },
    }


# ------------------------------------------------------------------------------------------ donors
def grants_by_donor(donors: list[str], grants: list[str] | None = None) -> list[dict[str, Any]]:
    """The Donors page's main table: each donor's grants (``grants_for_donors``) under a donor row with
    the total reserved, largest donor first."""
    rows = grants_for_donors(donors)
    if grants:
        rows = [r for r in rows if r["grant"].name in grants]
    donor_of = {r["grant"].name: r["grant"].donor or "" for r in rows}
    pds: dict[str, set[int]] = defaultdict(set)
    for grant, pd_id in (
        dm.FundsReservation.objects.filter(grant_number__in=list(donor_of))
        .exclude(intervention=None)
        .values_list("grant_number", "intervention")
    ):
        pds[donor_of[grant]].add(pd_id)
    by_donor: dict[str, dict[str, Any]] = {}
    for row in rows:
        donor = row["grant"].donor or ""
        entry = by_donor.setdefault(donor, {"donor": donor, "reserved": 0, "grants": []})
        entry["reserved"] += row["reserved"]
        entry["grants"].append(row)
    for entry in by_donor.values():
        entry["pds"] = len(pds[entry["donor"]])
        entry["grants"].sort(key=lambda r: -r["reserved"])
    return sorted(by_donor.values(), key=lambda e: (-e["reserved"], e["donor"]))


def grants_for_donors(donors: list[str]) -> list[dict[str, Any]]:
    """Grants with their expiry and the FR amounts reserved against them."""
    reserved = {
        row["grant_number"]: row
        for row in dm.FundsReservation.objects.exclude(grant_number="")
        .values("grant_number")
        .annotate(amount=Sum("overall_amount"), pds=Count("intervention", distinct=True))
    }
    grants = dm.Grant.objects.all()
    if donors:
        grants = grants.filter(donor__in=donors)
    today = datetime.date.today()
    rows = []
    for grant in grants.order_by("expiry", "name"):
        usage = reserved.get(grant.name, {})
        if not usage and not donors:
            continue  # without a donor filter, list only the grants that fund a programme document
        rows.append(
            {
                "grant": grant,
                "reserved": usage.get("amount") or 0,
                "pds": usage.get("pds") or 0,
                "expired": bool(grant.expiry and grant.expiry < today),
                "expiring": bool(
                    grant.expiry and today <= grant.expiry <= today + datetime.timedelta(days=180)
                ),
            }
        )
    return rows


# --------------------------------------------------------------------------------------- assurance
def assurance(params) -> dict[str, Any]:
    types = _getlist(params, "type")
    statuses = _getlist(params, "status")
    year = _year(params)
    q = (params.get("q") or "").strip()
    engagements = dm.AuditEngagement.objects.select_related("partner")
    if types:
        engagements = engagements.filter(engagement_type__in=types)
    if statuses:
        engagements = engagements.filter(status__in=statuses)
    if year:
        engagements = engagements.filter(
            Q(year_of_audit=year) | Q(year_of_audit__isnull=True, start_date__year=year)
        )
    if q:
        engagements = engagements.filter(
            Q(partner_name__icontains=q)
            | Q(vendor_number__icontains=q)
            | Q(reference_number__icontains=q)
            | _partner_q(q)
        )
    by_type = Counter(engagement_type_label(t) for t in engagements.values_list("engagement_type", flat=True))
    totals = engagements.aggregate(
        value=Sum("total_value"), tested=Sum("amount_tested"), findings=Sum("financial_findings")
    )
    psea = dm.PSEAAssessment.objects.all()
    return {
        "engagements": engagements.order_by("-start_date", "-datamart_id"),
        "by_type": by_type.most_common(),
        "totals": totals,
        "hact": list(dm.HACTAggregate.objects.order_by("-year")[:6]),
        "assessments": list(
            dm.PartnerAssessment.objects.select_related("partner").order_by("-completed_date")[:15]
        ),
        "psea_count": psea.count(),
        "psea_by_status": Counter(psea.values_list("status", flat=True)).most_common(),
        "options": {
            "types": sorted(
                {
                    (t, engagement_type_label(t))
                    for t in dm.AuditEngagement.objects.values_list("engagement_type", flat=True)
                    if t
                }
            ),
            "statuses": sorted(
                x
                for x in dm.AuditEngagement.objects.order_by().values_list("status", flat=True).distinct()
                if x
            ),
            "years": _years(dm.AuditEngagement.objects.exclude(start_date=None), "start_date"),
        },
    }


def _years(queryset: QuerySet, field: str) -> list[int]:
    return sorted(
        {y for y in queryset.annotate(y=ExtractYear(field)).values_list("y", flat=True).distinct() if y},
        reverse=True,
    )


# -------------------------------------------------------------------------------- field monitoring
def monitoring(params) -> dict[str, Any]:
    ratings = _getlist(params, "rating")
    year = _year(params)
    q = (params.get("q") or "").strip()
    findings = dm.MonitoringFinding.objects.select_related("partner")
    visits = dm.TPMVisit.objects.select_related("partner")
    if ratings:
        findings = findings.filter(overall_finding_rating__in=ratings)
    if year:
        findings = findings.filter(fm.finding_year_q(year))  # start date, else end date: as FMM dates a visit
        visits = visits.filter(start_date__year=year)
    if q:
        findings = findings.filter(
            Q(entity__icontains=q)
            | Q(vendor_number__icontains=q)
            | Q(monitoring_activity__icontains=q)
            | _partner_q(q)
        )
        visits = visits.filter(
            Q(partner_name__icontains=q)
            | Q(vendor_number__icontains=q)
            | Q(tpm_name__icontains=q)
            | _partner_q(q)
        )
    by_rating = Counter(findings.values_list("overall_finding_rating", flat=True))
    by_month: dict[str, int] = defaultdict(int)
    for day in findings.annotate(day=fm.finding_date()).exclude(day=None).values_list("day", flat=True):
        by_month[day.strftime("%Y-%m")] += 1
    planned_visits = visits.exclude(status__in=TPM_NOT_PLANNED)
    activities = dm.TPMActivity.objects.select_related("partner", "intervention")
    staff = dm.ProgrammaticVisit.objects.all()
    if year:
        activities = activities.filter(date__year=year)
        staff = staff.filter(date__year=year)
    if q:
        activities = activities.filter(
            Q(partner_name__icontains=q)
            | Q(vendor_number__icontains=q)
            | Q(tpm_name__icontains=q)
            | Q(pd_reference_number__icontains=q)
            | _partner_q(q)
        )
        staff = staff.filter(
            Q(partner_name__icontains=q) | Q(partnership_number__icontains=q) | _partner_q(q)
        )
    return {
        "tpm_activities": list(activities.order_by("-date")[:50]),
        "staff_visits": staff.count(),
        "staff_by_type": Counter(staff.values_list("travel_type", flat=True)).most_common(),
        "findings": findings.order_by(fm.finding_date().desc(nulls_last=True), "-datamart_id"),
        "by_rating": [(r or "—", n) for r, n in by_rating.most_common()],
        "by_month": sorted(by_month.items())[-24:],
        "activities": findings.exclude(monitoring_activity="")
        .values("monitoring_activity")
        .distinct()
        .count(),
        "tpm_visits": list(visits.order_by("-start_date")[:50]),
        "tpm_count": visits.count(),
        # planned = not draft or cancelled, the count the overview's third-party monitoring shows
        "tpm_planned": planned_visits.count(),
        "tpm_completed": planned_visits.filter(status__in=TPM_COMPLETED).count(),
        "options": {
            "ratings": sorted(
                x
                for x in dm.MonitoringFinding.objects.order_by()
                .values_list("overall_finding_rating", flat=True)
                .distinct()
                if x
            ),
            "years": _years(
                dm.MonitoringFinding.objects.annotate(day=fm.finding_date()).exclude(day=None), "day"
            ),
        },
    }


# ----------------------------------------------------------------------------------- action points
# The key the action taken is read from (the record as the Datamart sent it): the first that holds a text
AP_ACTION_TAKEN_KEYS = (
    "action_taken",
    "actions_taken",
    "action_taken_text",
    "actions_taken_text",
    "action_taken_description",
)
AP_CREATED_KEYS = ("created", "date_created", "created_at", "created_date")  # raised in eTools
AP_SOON_DAYS = 30
AP_MONTHS = 24  # months of the monthly trend
AP_DUE = {
    "overdue": "Overdue",
    "soon": f"Due within {AP_SOON_DAYS} days",
    "on_track": "On track",
    "none": "No due date",
}
AP_TIMELINESS = {
    "on_time": "On time",
    "late_30": "Late 1–30 days",
    "late_90": "Late 31–90 days",
    "late_more": "Late > 90 days",
    "no_dates": "No dates",
}
AP_EXPORT_COLUMNS = (
    "reference",
    "description",
    "partner",
    "pd",
    "office",
    "section",
    "assigned_to",
    "priority",
    "due_date",
    "status",
    "completed",
    "raised_from",
    "module_reference",
    "action_taken",
    "visit",
    "link_confidence",
    "ai_verdict",
    "pme_verification",
)
_MONTH = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def ap_completed_q(prefix: str = "") -> Q:
    """The completed action points (completed, closed or resolved, whatever the case)."""
    match = Q()
    for status in dm.ActionPoint.COMPLETED_STATUSES:
        match |= Q(**{f"{prefix}status__iexact": status})
    return match


def ap_action_taken(data: Any) -> str:
    """The action taken written when an action point was closed, on one line; "" when none."""
    if not isinstance(data, dict):
        return ""
    for key in AP_ACTION_TAKEN_KEYS:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())
    return ""


def _raised_month():
    """The month an action point was raised in eTools ("2026-05"), read from its record."""
    from django.db.models import Value
    from django.db.models.fields.json import KT
    from django.db.models.functions import Coalesce, NullIf, Substr

    return Substr(
        Coalesce(*(NullIf(KT(f"data__{key}"), Value("")) for key in AP_CREATED_KEYS), Value("")), 1, 7
    )


def _date_param(value: Any) -> datetime.date | None:
    try:
        return datetime.date.fromisoformat(str(value or "").strip()[:10])
    except ValueError:
        return None


def _fmm_points():
    """``neurodb.fmm.action_points`` when Monitoring insights is installed and on, else None (a lazy
    import: this module never depends on that app)."""
    from django.apps import apps
    from django.conf import settings

    if not apps.is_installed("neurodb.fmm") or not getattr(settings, "FMM_ENABLED", False):
        return None
    from neurodb.fmm import action_points as fmm_points

    return fmm_points


def action_point_filters(params) -> dict[str, Any]:
    """The filters of the action points page, read from ``params``."""
    return {
        "statuses": _getlist(params, "status"),
        "modules": _getlist(params, "module"),
        "offices": _getlist(params, "office"),
        "sections": _getlist(params, "section"),
        "partners": [int(p) for p in _getlist(params, "partner") if p.isdigit() and len(p) <= 18],
        "q": (params.get("q") or "").strip()[:200],
        "assignee": (params.get("assignee") or "").strip()[:100],
        "changed_from": _date_param(params.get("changed_from")),
        "changed_to": _date_param(params.get("changed_to")),
        "overdue": params.get("overdue") == "1",
        "priority": params.get("priority") == "1",
        "fm": params.get("fm") == "1",
        "due": params.get("due") if params.get("due") in AP_DUE else "",
        "timeliness": params.get("timeliness") if params.get("timeliness") in AP_TIMELINESS else "",
        "raised": params.get("raised") if _MONTH.match(str(params.get("raised") or "")) else "",
        "completed": params.get("completed") if _MONTH.match(str(params.get("completed") or "")) else "",
        "verdict": (params.get("verdict") or "")[:20],
        "pme": (params.get("pme") or "")[:20],
        "link": (params.get("link") or "")[:20],
    }


def filtered_action_points(params, today: datetime.date | None = None) -> tuple[QuerySet, dict | None, dict]:
    """The action points of the page's filter (``params``), the visit of ``?visit=`` and the filters
    read."""
    from datetime import timedelta

    from django.db.models import F
    from django.db.models.functions import TruncDate

    today = today or datetime.date.today()
    f = action_point_filters(params)
    points = dm.ActionPoint.objects.select_related("partner", "intervention")
    if f["statuses"]:
        # "open" means open in every figure: the points in progress with it (but "in progress" alone
        # keeps only the points in progress)
        wanted = set(f["statuses"])
        if "open" in wanted:
            wanted |= set(dm.ActionPoint.OPEN_STATUSES)
        points = points.filter(status__in=sorted(wanted))
    if f["modules"]:
        points = points.filter(related_module__in=f["modules"])
    if f["fm"]:
        points = points.filter(related_module__iexact="fm")
    if f["offices"]:
        points = points.filter(office__in=f["offices"])
    if f["sections"]:
        points = points.filter(section__in=f["sections"])
    if f["partners"]:
        points = points.filter(partner_id__in=f["partners"])
    if f["assignee"]:
        points = points.filter(assigned_to_name__icontains=f["assignee"])
    if f["changed_from"]:
        points = points.filter(last_modify_date__date__gte=f["changed_from"])
    if f["changed_to"]:
        points = points.filter(last_modify_date__date__lte=f["changed_to"])
    if f["priority"]:
        points = points.filter(high_priority=True)
    open_ = Q(status__in=dm.ActionPoint.OPEN_STATUSES)
    if f["overdue"]:
        points = points.filter(open_, due_date__lt=today)
    soon = today + timedelta(days=AP_SOON_DAYS)
    due = {
        "overdue": open_ & Q(due_date__lt=today),
        "soon": open_ & Q(due_date__gte=today, due_date__lte=soon),
        "on_track": open_ & Q(due_date__gt=soon),
        "none": open_ & Q(due_date__isnull=True),
    }
    if f["due"]:
        points = points.filter(due[f["due"]])
    if f["timeliness"]:
        points = points.filter(ap_completed_q()).annotate(done_on=TruncDate("date_of_completion"))
        dated = Q(due_date__isnull=False, done_on__isnull=False)
        late = {
            "on_time": dated & Q(done_on__lte=F("due_date")),
            "late_30": dated & Q(done_on__gt=F("due_date"), done_on__lte=F("due_date") + timedelta(days=30)),
            "late_90": dated
            & Q(
                done_on__gt=F("due_date") + timedelta(days=30),
                done_on__lte=F("due_date") + timedelta(days=90),
            ),
            "late_more": dated & Q(done_on__gt=F("due_date") + timedelta(days=90)),
            "no_dates": Q(due_date__isnull=True) | Q(done_on__isnull=True),
        }
        points = points.filter(late[f["timeliness"]])
    if f["raised"]:
        points = points.annotate(raised_month=_raised_month()).filter(raised_month=f["raised"])
    if f["completed"]:
        year, month = (int(x) for x in f["completed"].split("-"))
        points = points.filter(
            ap_completed_q(), date_of_completion__year=year, date_of_completion__month=month
        )
    visit = _fm_visit(params)
    if visit is not None:
        points = points.filter(pk__in=visit["ids"])
    q = f["q"]
    if q:
        match = (
            Q(reference_number__icontains=q)
            | Q(description__icontains=q)
            | Q(partner_name__icontains=q)
            | Q(assigned_to_name__icontains=q)
            | Q(intervention_number__icontains=q)
            | Q(module_reference_number__icontains=q)  # a visit's reference finds its action points
            | Q(status__iexact=q)
            | _partner_q(q)
        )
        for key in AP_ACTION_TAKEN_KEYS[:2]:  # the action taken, as the Datamart names it
            match |= Q(**{f"data__{key}__icontains": q})
        if q.isdigit() and len(q) <= 18:
            match |= Q(related_module_id=int(q))  # ... and so does an eTools activity id
        points = points.filter(match)
    fmm_points = _fmm_points()
    if fmm_points is not None:
        if f["verdict"] and (match := fmm_points.verdict_q(f["verdict"])) is not None:
            points = points.filter(match)
        if f["link"] and (match := fmm_points.confidence_q(f["link"])) is not None:
            points = points.filter(match)
        if f["pme"]:
            points = fmm_points.verification_filter(points, f["pme"])
    return points, visit, f


def action_points(params) -> dict[str, Any]:
    today = datetime.date.today()
    points, visit, f = filtered_action_points(params, today)
    overdue = Q(status__in=dm.ActionPoint.OPEN_STATUSES, due_date__lt=today)
    return {
        "points": points.order_by("-high_priority", "due_date", "-datamart_id"),
        "visit": visit,
        "filters": f,
        "visit_links": _fm_visit_links(points),
        "open": points.filter(status__in=dm.ActionPoint.OPEN_STATUSES).count(),
        "overdue": points.filter(overdue).count(),
        "high_priority": points.filter(high_priority=True, status__in=dm.ActionPoint.OPEN_STATUSES).count(),
        "completed": points.filter(ap_completed_q()).count(),
        "by_module": Counter(points.values_list("related_module", flat=True)).most_common(),
        "today": today,
        "options": {
            "statuses": _distinct_ap("status"),
            "modules": _distinct_ap("related_module"),
            "offices": _distinct_ap("office"),
            "sections": _distinct_ap("section"),
            "partners": list(
                PartnerOrganization.objects.filter(
                    pk__in=dm.ActionPoint.objects.exclude(partner=None).values("partner_id")
                )
                .order_by("name")
                .values_list("pk", "name")
            ),
            "due": list(AP_DUE.items()),
            "timeliness": list(AP_TIMELINESS.items()),
        },
    }


def _distinct_ap(field: str) -> list[str]:
    return sorted(x for x in dm.ActionPoint.objects.order_by().values_list(field, flat=True).distinct() if x)


def action_point_charts(points: QuerySet, today: datetime.date | None = None) -> dict[str, Any]:
    """The charts of the action points page over ``points`` (the filter): by status; the due-date status
    of the open ones; raised and completed per month (the last 24 months with any); the timeliness of
    the completed ones with their average days late; the open ones by office and by section. Each bar
    carries the value its click filters on."""
    from datetime import timedelta

    today = today or datetime.date.today()
    soon = today + timedelta(days=AP_SOON_DAYS)
    rows = points.order_by().annotate(raised_month=_raised_month())
    status_counts: Counter = Counter()
    due: Counter = Counter()
    timeliness: Counter = Counter()
    raised: Counter = Counter()
    completed: Counter = Counter()
    offices: Counter = Counter()
    sections: Counter = Counter()
    late_days: list[int] = []
    completed_set = set(dm.ActionPoint.COMPLETED_STATUSES)
    for status, due_date, done_at, office, section, month in rows.values_list(
        "status", "due_date", "date_of_completion", "office", "section", "raised_month"
    ).iterator(chunk_size=2000):
        status_counts[status or ""] += 1
        if month and _MONTH.match(month):
            raised[month] += 1
        if status in dm.ActionPoint.OPEN_STATUSES:
            due[
                "none"
                if due_date is None
                else "overdue"
                if due_date < today
                else "soon"
                if due_date <= soon
                else "on_track"
            ] += 1
            offices[office or ""] += 1
            sections[section or ""] += 1
        if (status or "").lower() in completed_set:
            done = done_at.date() if done_at else None
            if done:
                completed[f"{done.year:04d}-{done.month:02d}"] += 1
            if done is None or due_date is None:
                timeliness["no_dates"] += 1
            else:
                late = (done - due_date).days
                key = (
                    "on_time"
                    if late <= 0
                    else "late_30"
                    if late <= 30
                    else "late_90"
                    if late <= 90
                    else "late_more"
                )
                timeliness[key] += 1
                if late > 0:
                    late_days.append(late)
    months = sorted(set(raised) | set(completed))[-AP_MONTHS:]
    return {
        "by_status": [
            [code_label(status) if status else "No status", n, status or ""]
            for status, n in status_counts.most_common()
        ],
        "due": [[label, due[key], key] for key, label in AP_DUE.items() if due[key]],
        "monthly": {
            "labels": months,
            "series": {"Raised": [raised[m] for m in months], "Completed": [completed[m] for m in months]},
            "drill": {"labels": months, "series": {"Raised": "raised", "Completed": "completed"}},
        }
        if months
        else {},
        "timeliness": [
            [label, timeliness[key], key] for key, label in AP_TIMELINESS.items() if timeliness[key]
        ],
        "late_average": round(sum(late_days) / len(late_days), 1) if late_days else None,
        "late_count": len(late_days),
        "by_office": [[name or "No office", n, name] for name, n in offices.most_common(15)],
        "by_section": [[name or "No section", n, name] for name, n in sections.most_common(15)],
    }


def code_label(code: str) -> str:
    """An eTools code written for people ("in_progress" -> "In progress")."""
    return (code or "").replace("_", " ").strip().capitalize()


def action_point_rows(points: QuerySet) -> Iterator[dict[str, Any]]:
    """The action points of ``points`` as the CSV export writes them, with the visit, link confidence,
    AI verdict and PME verification when Monitoring insights is on."""
    fmm_points = _fmm_points()
    rows = points.order_by("-high_priority", "due_date", "-datamart_id")
    for start in range(0, rows.count(), 1000):
        chunk = list(rows[start : start + 1000])
        links = fmm_points.visit_links([p.pk for p in chunk]) if fmm_points else {}
        reviews = fmm_points.current_reviews(chunk) if fmm_points else {}
        checks = fmm_points.latest_verifications([p.datamart_id for p in chunk]) if fmm_points else {}
        for p in chunk:
            link = links.get(p.pk)
            review = reviews.get(p.datamart_id)
            check = checks.get(p.datamart_id)
            yield {
                "reference": p.reference_number,
                "description": p.description,
                "partner": p.partner_name or (p.partner.name if p.partner else ""),
                "pd": p.intervention_number or (p.intervention.number if p.intervention else ""),
                "office": p.office,
                "section": p.section,
                "assigned_to": p.assigned_to_name,
                "priority": "High" if p.high_priority else "",
                "due_date": p.due_date,
                "status": p.status,
                "completed": p.date_of_completion.date() if p.date_of_completion else None,
                "raised_from": p.related_module,
                "module_reference": p.module_reference_number,
                "action_taken": ap_action_taken(p.data),
                "visit": link["label"] if link else "",
                "link_confidence": (
                    fmm_points.CONFIDENCE_LABELS[link["confidence"]]
                    if link
                    else ("Unmatched" if fmm_points and (p.related_module or "").lower() == "fm" else "")
                ),
                "ai_verdict": review.get_verdict_display() if review else "",
                "pme_verification": check.get_state_display() if check else "",
            }


def _fm_visit_links(points: QuerySet[dm.ActionPoint]) -> dict[int, tuple[str, str]]:
    """``{action point id: (visit key, label)}`` for the field monitoring action points of ``points``
    linked to a visit of Monitoring insights (``fmm.services.visits_for_action_points``, read through
    a lazy import in one query); empty when that app is not installed or switched off."""
    from django.apps import apps
    from django.conf import settings

    if not apps.is_installed("neurodb.fmm") or not getattr(settings, "FMM_ENABLED", False):
        return {}
    from neurodb.fmm import services as fmm_services

    # the refresh matches FM action points whatever the case of their module ("fm", "FM")
    fm_points = points.filter(related_module__iexact="fm").order_by().values("pk")
    return fmm_services.visits_for_action_points(fm_points)


def _fm_visit(params) -> dict[str, Any] | None:
    """``?visit=<key>``: the action points linked to one field monitoring visit of Monitoring insights
    (the same set its visit page lists), read through a lazy import when that app is installed and on.
    ``{"key", "label", "ids"}``, or None without the parameter."""
    from django.apps import apps
    from django.conf import settings

    key = (params.get("visit") or "").strip()[:40]
    if not key or not apps.is_installed("neurodb.fmm") or not getattr(settings, "FMM_ENABLED", False):
        return None
    from neurodb.fmm import services as fmm_services

    return {
        "key": key,
        "label": fmm_services.visit_label(key) or key,
        "ids": fmm_services.action_point_ids(key),
    }


def engagement_detail(engagement: dm.AuditEngagement) -> dict[str, Any]:
    engagement.type_label = engagement_type_label(engagement.engagement_type)
    # linked by the sync (engagement FK), listed in the engagement's data, or raised in the audit module
    # under the engagement's reference number
    match = Q(engagement=engagement) | Q(source_id__in=_action_point_ids(engagement))
    if engagement.reference_number:
        match |= Q(module_reference_number=engagement.reference_number)
    points = dm.ActionPoint.objects.filter(match)
    findings = list(engagement.findings.order_by("finding_number"))
    return {
        "engagement": engagement,
        "findings": findings,
        # the Datamart count when synced (0 included), else the findings listed
        "findings_count": (
            engagement.financial_findings_count
            if engagement.financial_findings_count is not None
            else len(findings)
        ),
        "action_points": list(points.order_by("status", "due_date")),
        "interventions": list(engagement.interventions.all()),
        "details": engagement.details or {},
    }


def _action_point_ids(engagement: dm.AuditEngagement) -> list[int]:
    ids = []
    for point in (engagement.data or {}).get("action_points") or []:
        if isinstance(point, dict) and str(point.get("id", "")).isdigit():
            ids.append(int(point["id"]))
    return ids


def _fm_links(year: int | None, partner_ids: list[int]) -> dict[int, str]:
    """``{partner id: the partner's programmatic visits of the HACT year in Monitoring insights}``,
    built by ``fmm.scope.link`` through a lazy import; empty when that app is not installed or off."""
    from django.apps import apps
    from django.conf import settings

    if not year or not apps.is_installed("neurodb.fmm") or not getattr(settings, "FMM_ENABLED", False):
        return {}
    from neurodb.fmm.scope import link

    return {pk: link(partner=pk, programmatic=1, year=year) for pk in partner_ids}


def hact_compliance(year: int | None = None) -> dict[str, Any]:
    """Assurance done against assurance required, per partner, for one HACT year (latest by default)."""
    years = sorted({y for y in dm.PartnerHACTYear.objects.values_list("year", flat=True) if y}, reverse=True)
    year = year if year in years else (years[0] if years else None)
    rows = list(
        dm.PartnerHACTYear.objects.filter(year=year).select_related("partner").order_by("-cash_transfers")
    )
    # NeuroDB's own count of completed programmatic field monitoring visits, per partner of each row
    fm_pv = fm.programmatic_visits_by_partner(year) if year else {}
    fm_links = _fm_links(year, [row.partner_id for row in rows if row.partner_id])
    for row in rows:
        row.pv_gap = max((row.pv_required or 0) - (row.pv_completed or 0), 0)
        row.sc_gap = max((row.sc_required or 0) - (row.sc_completed or 0), 0)
        row.fm_pv = fm_pv.get(row.partner_id, 0) if row.partner_id else None
        row.fm_url = fm_links.get(row.partner_id, "")
    return {
        "year": year,
        "years": years,
        "rows": rows,
        "fm_pv": fm_pv,
        "partners_behind": sum(1 for r in rows if r.pv_gap or r.sc_gap),
        "cash_transfers": sum((r.cash_transfers or 0) for r in rows),
    }


# ------------------------------------------------------------------------------------------- funds
def funds(params) -> dict[str, Any]:
    """The Funds page's funds reservations tab: FRs, grants and their amounts, on the page's filter
    (``funds.FundFilters``, shared with Partner Fund Monitoring and Funded Programs)."""
    from . import funds as fund_views

    f = fund_views.FundFilters.from_params(params)
    donors, grants, year, q = f.donors, f.grants, f.year, f.q
    lines = f.lines().filter(f.pd_q("intervention__"))
    headers = dm.FundsReservationHeader.objects.select_related(
        "intervention", "intervention__partner"
    ).filter(f.pd_q("intervention__"))
    if year:
        headers = headers.filter(start_date__year=year)
    if f.fr:
        headers = headers.filter(fr_number__icontains=f.fr)
    if q:
        match = (
            Q(intervention__number__icontains=q)
            | Q(intervention__partner_name__icontains=q)
            | Q(intervention__partner__vendor_number__icontains=q)
            | Q(fr_number__icontains=q)
        )
        lines = lines.filter(match)
        headers = headers.filter(match | Q(pd_reference_number__icontains=q))
    if donors or grants:
        headers = headers.filter(fr_number__in=lines.values("fr_number"))
    totals = headers.aggregate(
        reserved=Sum("total_amt"), disbursed=Sum("actual_amt"), outstanding=Sum("outstanding_amt")
    )
    by_donor = list(lines.values("donor").annotate(amount=Sum("overall_amount")).order_by("-amount")[:25])
    expiry = dict(dm.Grant.objects.values_list("name", "expiry"))
    today = datetime.date.today()
    by_grant = []
    for row in (
        lines.exclude(grant_number="")
        .values("grant_number", "donor")
        .annotate(amount=Sum("overall_amount"), pds=Count("intervention", distinct=True))
        .order_by("-amount")[:50]
    ):
        ends = expiry.get(row["grant_number"])
        row["expiry"] = ends
        row["expired"] = bool(ends and ends < today)
        row["expiring"] = bool(ends and today <= ends <= today + datetime.timedelta(days=180))
        by_grant.append(row)
    by_year: dict[int, dict[str, Decimal]] = defaultdict(
        lambda: {"reserved": Decimal(0), "disbursed": Decimal(0)}
    )
    for start, total, actual in headers.exclude(start_date=None).values_list(
        "start_date", "total_amt", "actual_amt"
    ):
        by_year[start.year]["reserved"] += total or 0
        by_year[start.year]["disbursed"] += actual or 0
    return {
        "headers": headers.order_by("-start_date", "fr_number"),
        "totals": totals,
        "fr_count": headers.count(),
        "pds": headers.exclude(intervention=None).values("intervention").distinct().count(),
        "by_donor": [(r["donor"] or "Unknown", float(r["amount"] or 0)) for r in by_donor],
        "by_grant": by_grant,
        "by_year": [(y, float(v["reserved"])) for y, v in sorted(by_year.items())],
        "disbursed_by_year": [(y, float(v["disbursed"])) for y, v in sorted(by_year.items())],
        "options": funds_options(),
        "filters": f,
    }


def funds_options() -> dict[str, Any]:
    """The Funds page's filter choices (donors, grants, FR start years, and the PD statuses, partners and
    partner types of :func:`neurodb.datamart.funds.filter_options`), on their own: the Partner Fund Monitoring
    and Funded Programs tabs read only these of :func:`funds` (6 queries instead of 13)."""
    from . import funds as fund_views

    return {
        "donors": sorted(
            x for x in dm.FundsReservation.objects.order_by().values_list("donor", flat=True).distinct() if x
        ),
        "grants": sorted(
            x
            for x in dm.FundsReservation.objects.order_by().values_list("grant_number", flat=True).distinct()
            if x
        ),
        "years": _years(dm.FundsReservationHeader.objects.exclude(start_date=None), "start_date"),
        **fund_views.filter_options(),
    }


# -------------------------------------------------------------------------------- partner reporting
REPORTING_TABLES = ("locations", "disaggregations")  # the tabs whose table loads as a part of its own


def reporting_today() -> datetime.date:
    """The day the Progress reports page reads each report's status for (Due, Overdue, Not yet due): its
    stored tables are keyed on it. The tests freeze it here."""
    return datetime.date.today()


def _reporting_table_key(tab: str, params, day: datetime.date, stamp: str) -> str:
    filters = reporting.ReportFilters.from_params(params)
    tab_filters = (
        reporting.DisaggregationFilters.from_params(params).cache_key() if tab == "disaggregations" else ""
    )
    # the rows a tab reads at once: past them the table is only "too many" (a constant, here for its tests)
    limit = reporting.LOCATION_LIMIT if tab == "locations" else reporting.DISAGGREGATION_LIMIT
    return (
        f"pr-table:v{reporting.CACHE_VERSION}:{tab}:{limit}:{filters.cache_key()}:{tab_filters}:{day}:{stamp}"
    )


def reporting_table(tab: str, params, day: datetime.date | None = None) -> dict[str, Any]:
    """The table of the Progress reports page's Report/Location (``tab="locations"``:
    :func:`neurodb.datamart.reporting.location_rows`) or Report/Disaggregation tab (``"disaggregations"``:
    :func:`neurodb.datamart.reporting.disaggregation_rows`, each block without its lines: the page fills
    those of the blocks it shows, on a copy), with the chart data the tab carries (``by_month``), for the
    page's filter (``params``). Computed once across every worker and kept two minutes: the tab's table
    part, its pages and the tab drawn inline read it. The key holds the filter, the tab's own filters, the
    day (the statuses Due, Overdue and Not yet due follow it) and the moment of the eTools figures (a
    finished eTools sync shows at once); nothing about the person asking, and nothing translated. In DEBUG
    it is computed at each call."""
    day = day or reporting_today()

    def build() -> dict[str, Any]:
        data = partner_reporting(params, day)
        wanted = set(data["filters"].statuses) or None
        if tab == "locations":
            found = reporting.location_rows(data["rows"], data["statuses"], data["today"], wanted)
        else:
            found = reporting.disaggregation_rows(
                data["rows"], data["statuses"], data["today"], wanted, params
            )
        return {tab: found, "by_month": data["by_month"]}

    if tab not in REPORTING_TABLES:
        raise ValueError(f"no stored table for the tab {tab!r}")
    if settings.DEBUG:
        return build()
    key = _reporting_table_key(tab, params, day, keys.fingerprint())
    return compute.once(key, build, reporting.STORED_SECONDS, lang_neutral=True)


def reporting_table_stored(tab: str, params, day: datetime.date | None = None) -> bool:
    """Whether :func:`reporting_table` would answer at once from what is stored: the page's frame then draws
    the table inline instead of asking for it."""
    if settings.DEBUG:
        return False
    key = _reporting_table_key(tab, params, day or reporting_today(), keys.fingerprint())
    return compute.peek(key, lang_neutral=True)[0]


def partner_reporting(params, today: datetime.date | None = None) -> dict[str, Any]:
    """The Progress reports page: the progress reports of the filter (one of six statuses each, with
    their PD's status, document type and UNICEF focal points), the progress reports per status
    (``summary``), the indicator reports per status (``counts``: what the tiles show), by section and
    by month of the reporting period's end, and Partner Performance. ``statuses``: the status of every
    progress report of the filter, read before the status filter (Report/Location reads each report's
    status there, so a report left out by the status filter is never let back in by one of its rows);
    ``by_status``: the progress reports per status, by name (as ``summary`` counts them)."""
    today = today or datetime.date.today()
    filters = reporting.ReportFilters.from_params(params)
    rows = filters.queryset(today)
    reports = reporting.progress_reports(rows, today)
    statuses = {r["progress_report"]: r["status"] for r in reports if r["progress_report"]}
    if filters.statuses:
        reports = [r for r in reports if r["status"] in filters.statuses]
    reporting.with_pd_details(reports)
    summary = _status_summary(reports)  # progress reports per status
    counts = reporting.counts(reports)  # indicator reports per status (the tiles)
    return {
        "filters": filters,
        "rows": rows,
        "reports": reports,
        "statuses": statuses,
        "summary": summary,
        "counts": counts,
        "by_status": [(reached.STATUS_LABELS[k], n) for k, n in summary["statuses"].items() if n],
        "by_section": reporting.by_section(reports),
        "by_month": reporting.by_month(reports),
        "partners": reporting.partner_performance(reports),
        "today": today,
        "options": reporting.stored_options(),
    }


def report_indicators(progress_report: str) -> list[dm.ReportedIndicator]:
    return list(
        dm.ReportedIndicator.objects.filter(progress_report=progress_report).order_by(
            "pd_output", "indicator", "location"
        )
    )


def report_detail(progress_report: str, today: datetime.date | None = None) -> dict[str, Any] | None:
    """One progress report, indicator by indicator: each location's value in the report (the
    disaggregation total "()", as ``reached`` reads it, parsed), PRP's value to date there, and a
    total row that combines the locations by the indicator's method (never a sum of a percentage,
    never the report total repeated on each row added up)."""
    today = today or datetime.date.today()
    if not (progress_report or "").strip():
        return None  # rows without a report id belong to no one report
    rows = list(
        dm.ReportedIndicator.objects.filter(progress_report=progress_report)
        .order_by(*reached.ROW_ORDER)
        .values(*reached.ROW_FIELDS, "pd_output", "target")
    )
    if not rows:
        return None
    report = (
        dm.ReportedIndicator.objects.filter(progress_report=progress_report).order_by("datamart_id").first()
    )
    definitions: dict[str, tuple[str, str, float | None]] = {}
    if report.intervention_id:
        for row in dm.PDIndicator.objects.filter(intervention_id=report.intervention_id).values(
            "source_id", "title", "display_type", "unit", "target_numerator", "target_denominator"
        ):
            # by eTools id ("s…"), else by title ("t…"): a row without an id never takes the type of
            # an indicator that has none. The target as Partner monitoring reads it (a percentage × 100)
            found = (row["display_type"], row["unit"], pd_monitoring._target(row))
            if row["source_id"]:
                definitions.setdefault(f"s{row['source_id']}", found)
            definitions.setdefault(f"t{pd_monitoring.norm(row['title'])}", found)
    # one entry per indicator report (one indicator in this report), as the tiles count them: two
    # indicators that share a title or an eTools id are never one line
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[reached.indicator_report_key(row)].append(row)
    totals = reached.progress_values(rows)
    words = reached.report_status_texts(rows)  # the report's one status, from all its rows
    indicators = []
    for _key, group in sorted(
        groups.items(), key=lambda kv: (kv[1][0]["pd_output"] or "", kv[1][0]["indicator"] or "", kv[0])
    ):
        etools_id, title = (group[0]["etools_indicator_id"] or "").strip(), group[0]["indicator"] or ""
        display_type, unit, target = (
            (definitions.get(f"s{etools_id}") if etools_id else None)
            or definitions.get(f"t{pd_monitoring.norm(title)}")
            or ("", "", None)
        )
        ratio = reached.is_ratio(
            display_type,
            unit,
            group[0]["calculation_across_locations"],
            group[0]["calculation_across_periods"],
        )
        percentage = display_type == "percentage" or unit == "percentage"
        (item,) = reached.build_reports(
            group,
            ratio=ratio,
            percentage=percentage,
            today=today,
            progress_values=totals,
            report_status=words,
        )
        if target is None:  # the report's own copy of the target, read as the values are
            found = reached.parts(group[0]["target"])
            target = found.value(ratio, percentage) if found else None
        indicators.append(
            {
                "indicator": title,
                "pd_output": group[0]["pd_output"],
                "partner_status": item.partner_status,
                "target": target,
                "locations": [
                    {
                        "name": item.places.get(place, {}).get("name") or "—",
                        "p_code": item.places.get(place, {}).get("p_code") or "",
                        "value": None if cell.repeated else cell.value,
                        "prp_to_date": cell.prp_cumulative,
                    }
                    for place, cell in item.cells.items()
                ],
                "total": item.value,
                "prp_cumulative": item.prp_cumulative,
                "method": reached.location_method_label(item.method, ratio),
                "notes": item.notes,
                "percentage": percentage,
            }
        )
    # the report's status as every page reads it: its rows' newest word, and the first date its rows
    # give (in the order Reached reads them)
    (whole,) = reached.build_reports(rows, ratio=False, percentage=False, today=today, report_status=words)
    status = whole.status
    return {
        "report": report,
        "status": status,
        "status_label": reached.STATUS_LABELS[status],
        "counted": status in reached.COUNTED,
        "indicators": indicators,
    }
