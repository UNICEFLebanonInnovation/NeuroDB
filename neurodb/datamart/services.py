"""Queries over the eTools Datamart tables for the programme, partner, donor and assurance pages."""

from __future__ import annotations

import datetime
import re
from collections import Counter, defaultdict
from collections.abc import Iterator
from decimal import Decimal
from typing import Any

from django.db.models import Count, Q, QuerySet, Sum
from django.db.models.functions import ExtractYear

from neurodb.partnerships.models import PCA, PartnerOrganization

from . import fm
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
    the field monitoring visits that monitored the PD (one per visit, by the year of its end date)."""
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


REPORT_FIELDS = (
    "pd_reference_number",
    "intervention_id",
    "partner_id",
    "partner_name",
    "report_number",
    "report_type",
    "report_status",
    "report_accepted_status",
    "period_start",
    "period_end",
    "due_date",
    "submission_date",
    "acceptance_date",
)


def progress_reports(queryset: QuerySet[dm.ReportedIndicator]) -> list[dict[str, Any]]:
    """One row per progress report (the Datamart has one row per indicator and location).

    A report whose rows disagree on a field (a partial re-sync, a partner link set on some rows
    only) is still listed and counted once: each field takes the first non-empty value of its rows,
    in a fixed order. Rows without a progress report id stay grouped by all their fields."""
    grouped = (
        queryset.values("progress_report", *REPORT_FIELDS)
        .annotate(indicators=Count("indicator", distinct=True))
        .order_by("progress_report", *REPORT_FIELDS)
    )
    counts = dict(
        queryset.exclude(progress_report="")
        .values_list("progress_report")
        .annotate(n=Count("indicator", distinct=True))
        .order_by()
    )
    reports: dict[Any, dict[str, Any]] = {}
    for row in grouped:
        key = row["progress_report"] or tuple(row[f] for f in REPORT_FIELDS)
        report = reports.get(key)
        if report is None:
            reports[key] = {**row, "indicators": counts.get(row["progress_report"], row["indicators"])}
            continue
        for f in REPORT_FIELDS:
            if report[f] in (None, "") and row[f] not in (None, ""):
                report[f] = row[f]
    out = sorted(reports.values(), key=lambda r: (r["pd_reference_number"], r["report_number"]))
    # latest period first, reports without a period end at the top (as the database sorts them)
    out.sort(key=lambda r: r["period_end"] or datetime.date.max, reverse=True)
    return out


def latest_progress(pd: PCA) -> list[dict[str, Any]]:
    """Cumulative progress per indicator from the PD's latest progress report."""
    latest = pd.reported_indicators.exclude(period_end=None).order_by("-period_end").first()
    if latest is None:
        return []
    rows = {}
    for row in pd.reported_indicators.filter(progress_report=latest.progress_report).order_by(
        "pd_output", "indicator"
    ):
        rows.setdefault(
            row.indicator,
            {
                "indicator": row.indicator,
                "output": row.pd_output,
                "target": row.target,
                "progress": row.total_cumulative_progress,
                "status": row.pd_output_progress_status,
                "report": row.report_number,
                "period_end": row.period_end,
            },
        )
    return list(rows.values())


def _flag_overdue(points: list[dm.ActionPoint]) -> list[dm.ActionPoint]:
    """Marks the open action points past their due date, as the Action points page flags them."""
    today = datetime.date.today()
    for point in points:
        point.overdue = bool(
            point.status in dm.ActionPoint.OPEN_STATUSES and point.due_date and point.due_date < today
        )
    return points


def programme_datamart(pd: PCA) -> dict[str, Any]:
    lines = list(dm.FundsReservation.objects.filter(intervention=pd))
    action_points = dm.ActionPoint.objects.filter(intervention=pd)
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
        "latest_progress": latest_progress(pd),
        "monitoring": _monitoring_summary(pd_monitoring.Filters(pds=[pd.number or ""], scope="all")),
    }


def _monitoring_summary(filters: pd_monitoring.Filters) -> dict[str, Any]:
    """On/off-track counts of the PD indicators the partner monitoring page shows for ``filters``."""
    rows = pd_monitoring.indicators(filters)
    counts = Counter(r.tracking for r in rows)
    return {
        "rows": rows,
        "indicators": len(rows),
        "status_counts": {key: counts.get(key, 0) for key in pd_monitoring.LABELS},
        # ahead of schedule is on track too, as the daily review and the brief count it
        "on_track_or_ahead": counts.get("on_track", 0) + counts.get("over_target", 0),
        "reported": sum(1 for r in rows if r.reports),
    }


# ---------------------------------------------------------------------------------------- partners
def reports_by_year(queryset: QuerySet[dm.ReportedIndicator]) -> list[tuple[str, int]]:
    """Progress reports per year of their period end (partner page: reporting activity over time)."""
    years: Counter[str] = Counter()
    for r in progress_reports(queryset):
        if r["period_end"]:
            years[str(r["period_end"].year)] += 1
    return sorted(years.items())


def reporting_summary(queryset: QuerySet[dm.ReportedIndicator]) -> dict[str, int]:
    today = datetime.date.today()
    reports = list(progress_reports(queryset))
    submitted = [pd_monitoring.is_submitted(r) for r in reports]  # the rule of the review and the brief
    return {
        "reports": len(reports),
        "submitted": sum(submitted),
        "late": sum(
            1
            for r in reports
            if r["submission_date"] and r["due_date"] and r["submission_date"] > r["due_date"]
        ),
        "overdue": sum(
            1
            for r, sent in zip(reports, submitted, strict=True)
            if not sent and r["due_date"] and r["due_date"] < today
        ),
        "accepted": sum(1 for r in reports if "accept" in (r["report_status"] or "").lower()),
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
        "finding_ratings": dict(Counter(f.overall_finding_rating or "—" for f in findings)),
        "hact_years": list(partner.hact_years.order_by("-year")[:4]),
        "monitoring": _monitoring_summary(pd_monitoring.Filters(partners=[str(partner.pk)])),
        "reporting": reporting_summary(partner.reported_indicators.all()),
        "reports": list(progress_reports(partner.reported_indicators.all())[:10]),
        "reports_by_year": reports_by_year(partner.reported_indicators.all()),
        "staff_visits_by_year": sorted(visits.items()),
        "monitoring_visits_by_year": _monitoring_visits_by_year(partner),
    }


def _monitoring_visits_by_year(partner: PartnerOrganization) -> dict[str, dict[int, int]]:
    """Field monitoring visits (one per monitoring activity, however many findings it has) and planned
    third-party visits (drafts and cancelled ones left out) of a partner, per year."""
    field: Counter[int] = Counter()
    seen: set[str] = set()
    for activity, pk, day in (
        dm.MonitoringFinding.objects.filter(partner=partner)
        .exclude(end_date=None)
        .values_list("monitoring_activity", "pk", "end_date")
    ):
        key = activity or f"#{pk}"
        if key not in seen:
            seen.add(key)
            field[day.year] += 1
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
        findings = findings.filter(end_date__year=year)
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
    for day in findings.exclude(end_date=None).values_list("end_date", flat=True):
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
        "findings": findings.order_by("-end_date", "-datamart_id"),
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
            "years": _years(dm.MonitoringFinding.objects.exclude(end_date=None), "end_date"),
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
        # "open" means open in every figure: the points in progress with it
        wanted = set(f["statuses"])
        if wanted & set(dm.ActionPoint.OPEN_STATUSES):
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
    donors = _getlist(params, "donor")
    grants = _getlist(params, "grant")
    year = _year(params)
    q = (params.get("q") or "").strip()
    lines = dm.FundsReservation.objects.all()
    headers = dm.FundsReservationHeader.objects.select_related("intervention", "intervention__partner")
    if donors:
        lines = lines.filter(donor__in=donors)
    if grants:
        lines = lines.filter(grant_number__in=grants)
    if year:
        lines = lines.filter(start_date__year=year)
        headers = headers.filter(start_date__year=year)
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
        "options": {
            "donors": sorted(
                x
                for x in dm.FundsReservation.objects.order_by().values_list("donor", flat=True).distinct()
                if x
            ),
            "grants": sorted(
                x
                for x in dm.FundsReservation.objects.order_by()
                .values_list("grant_number", flat=True)
                .distinct()
                if x
            ),
            "years": _years(dm.FundsReservationHeader.objects.exclude(start_date=None), "start_date"),
        },
    }


# -------------------------------------------------------------------------------- partner reporting
def partner_reporting(params) -> dict[str, Any]:
    statuses = _getlist(params, "status")
    types = _getlist(params, "report_type")
    year = _year(params)
    q = (params.get("q") or "").strip()
    only_overdue = params.get("overdue") == "1"
    today = datetime.date.today()
    rows = dm.ReportedIndicator.objects.all()
    if statuses:
        rows = rows.filter(report_status__in=statuses)
    if types:
        rows = rows.filter(report_type__in=types)
    if year:
        rows = rows.filter(period_end__year=year)
    if q:
        rows = rows.filter(
            Q(partner_name__icontains=q)
            | Q(vendor_number__icontains=q)
            | Q(pd_reference_number__icontains=q)
            | Q(indicator__icontains=q)
            | _partner_q(q)
        )
    if only_overdue:
        rows = rows.filter(pd_monitoring.pending_q(), due_date__lt=today)
    summary = reporting_summary(rows)
    reports = progress_reports(rows)
    by_status = Counter(r["report_status"] or "—" for r in reports)
    return {
        "reports": reports,
        "summary": summary,
        "by_status": by_status.most_common(),
        "today": today,
        "options": {
            "statuses": sorted(
                x
                for x in dm.ReportedIndicator.objects.order_by()
                .values_list("report_status", flat=True)
                .distinct()
                if x
            ),
            "types": sorted(
                x
                for x in dm.ReportedIndicator.objects.order_by()
                .values_list("report_type", flat=True)
                .distinct()
                if x
            ),
            "years": _years(dm.ReportedIndicator.objects.exclude(period_end=None), "period_end"),
        },
    }


def report_indicators(progress_report: str) -> list[dm.ReportedIndicator]:
    return list(
        dm.ReportedIndicator.objects.filter(progress_report=progress_report).order_by(
            "pd_output", "indicator", "location"
        )
    )
