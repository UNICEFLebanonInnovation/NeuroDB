"""Queries over the eTools Datamart tables for the programme, partner, donor and assurance pages."""

from __future__ import annotations

import datetime
from collections import Counter, defaultdict
from decimal import Decimal
from typing import Any

from django.db.models import Count, Q, QuerySet, Sum
from django.db.models.functions import ExtractYear

from neurodb.partnerships.models import PCA, PartnerOrganization

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
    raw = (params.get("year") or "").strip()
    return int(raw) if raw.isdigit() else None


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
        frs = {h.fr_number: (h.total_amt, h.actual_amt, h.outstanding_amt) for h in headers}
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
    """Programmatic visits planned (eTools PD plan) and done (staff trips, TPM visits) per year."""
    years: dict[int, dict[str, int]] = defaultdict(lambda: {"planned": 0, "staff": 0, "tpm": 0})
    for plan in pd.planned_visits_by_year.exclude(year=None):
        years[plan.year]["planned"] += plan.total
    for day in (
        pd.programmatic_visits.filter(travel_type__icontains="programmatic")
        .exclude(date=None)
        .values_list("date", flat=True)
    ):
        years[day.year]["staff"] += 1
    for _visit, day in (
        pd.tpm_activities.exclude(date=None).values_list("visit_reference_number", "date").distinct()
    ):
        years[day.year]["tpm"] += 1
    return [{"year": y, **v} for y, v in sorted(years.items(), reverse=True)]


def progress_reports(queryset: QuerySet[dm.ReportedIndicator]) -> QuerySet:
    """One row per progress report (the Datamart has one row per indicator and location)."""
    return (
        queryset.values(
            "progress_report",
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
        .annotate(indicators=Count("indicator", distinct=True))
        .order_by("-period_end", "pd_reference_number", "report_number")
    )


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


def programme_datamart(pd: PCA) -> dict[str, Any]:
    lines = list(dm.FundsReservation.objects.filter(intervention=pd))
    action_points = dm.ActionPoint.objects.filter(intervention=pd)
    return {
        "fr_lines": lines,
        **_fr_totals(pd, lines),
        "indicators": indicators_for(dm.PDIndicator.objects.filter(intervention=pd)),
        "workplan": workplan(pd.workplan_activities.all()),
        "action_points": list(action_points.order_by("status", "due_date")[:50]),
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
    return {
        "reports": len(reports),
        "submitted": sum(1 for r in reports if r["submission_date"]),
        "late": sum(
            1
            for r in reports
            if r["submission_date"] and r["due_date"] and r["submission_date"] > r["due_date"]
        ),
        "overdue": sum(
            1 for r in reports if not r["submission_date"] and r["due_date"] and r["due_date"] < today
        ),
        "accepted": sum(1 for r in reports if "accept" in (r["report_status"] or "").lower()),
    }


def partner_datamart(partner: PartnerOrganization) -> dict[str, Any]:
    engagements = _with_type_labels(
        list(dm.AuditEngagement.objects.filter(partner=partner).order_by("-start_date")[:50])
    )
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
        "engagement_counts": dict(Counter(engagement_type_label(e.engagement_type) for e in engagements)),
        "audit_findings": list(partner.audit_findings.order_by("-created")[:20]),
        "action_points": list(
            action_points.filter(status__in=dm.ActionPoint.OPEN_STATUSES).order_by("due_date")[:20]
        ),
        "open_action_points": action_points.filter(status__in=dm.ActionPoint.OPEN_STATUSES).count(),
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
    }


# ------------------------------------------------------------------------------------------ donors
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
                x for x in dm.AuditEngagement.objects.values_list("status", flat=True).distinct() if x
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
        "options": {
            "ratings": sorted(
                x
                for x in dm.MonitoringFinding.objects.values_list(
                    "overall_finding_rating", flat=True
                ).distinct()
                if x
            ),
            "years": _years(dm.MonitoringFinding.objects.exclude(end_date=None), "end_date"),
        },
    }


# ----------------------------------------------------------------------------------- action points
def action_points(params) -> dict[str, Any]:
    statuses = _getlist(params, "status")
    modules = _getlist(params, "module")
    q = (params.get("q") or "").strip()
    only_overdue = params.get("overdue") == "1"
    only_priority = params.get("priority") == "1"
    today = datetime.date.today()
    points = dm.ActionPoint.objects.select_related("partner", "intervention")
    if statuses:
        points = points.filter(status__in=statuses)
    if modules:
        points = points.filter(related_module__in=modules)
    if only_priority:
        points = points.filter(high_priority=True)
    overdue = Q(status__in=dm.ActionPoint.OPEN_STATUSES, due_date__lt=today)
    if only_overdue:
        points = points.filter(overdue)
    if q:
        points = points.filter(
            Q(reference_number__icontains=q)
            | Q(description__icontains=q)
            | Q(partner_name__icontains=q)
            | Q(assigned_to_name__icontains=q)
            | Q(intervention_number__icontains=q)
            | _partner_q(q)
        )
    return {
        "points": points.order_by("-high_priority", "due_date", "-datamart_id"),
        "open": points.filter(status__in=dm.ActionPoint.OPEN_STATUSES).count(),
        "overdue": points.filter(overdue).count(),
        "high_priority": points.filter(high_priority=True, status__in=dm.ActionPoint.OPEN_STATUSES).count(),
        "by_module": Counter(points.values_list("related_module", flat=True)).most_common(),
        "today": today,
        "options": {
            "statuses": sorted(
                x for x in dm.ActionPoint.objects.values_list("status", flat=True).distinct() if x
            ),
            "modules": sorted(
                x for x in dm.ActionPoint.objects.values_list("related_module", flat=True).distinct() if x
            ),
        },
    }


def engagement_detail(engagement: dm.AuditEngagement) -> dict[str, Any]:
    engagement.type_label = engagement_type_label(engagement.engagement_type)
    points = (
        dm.ActionPoint.objects.filter(
            Q(module_reference_number=engagement.reference_number)
            | Q(source_id__in=_action_point_ids(engagement))
        )
        if engagement.reference_number
        else dm.ActionPoint.objects.none()
    )
    return {
        "engagement": engagement,
        "findings": list(engagement.findings.order_by("finding_number")),
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


def hact_compliance(year: int | None = None) -> dict[str, Any]:
    """Assurance done against assurance required, per partner, for one HACT year (latest by default)."""
    years = sorted({y for y in dm.PartnerHACTYear.objects.values_list("year", flat=True) if y}, reverse=True)
    year = year if year in years else (years[0] if years else None)
    rows = list(
        dm.PartnerHACTYear.objects.filter(year=year).select_related("partner").order_by("-cash_transfers")
    )
    for row in rows:
        row.pv_gap = max((row.pv_required or 0) - (row.pv_completed or 0), 0)
        row.sc_gap = max((row.sc_required or 0) - (row.sc_completed or 0), 0)
    return {
        "year": year,
        "years": years,
        "rows": rows,
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
                x for x in dm.FundsReservation.objects.values_list("donor", flat=True).distinct() if x
            ),
            "grants": sorted(
                x for x in dm.FundsReservation.objects.values_list("grant_number", flat=True).distinct() if x
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
        rows = rows.filter(submission_date=None, due_date__lt=today)
    summary = reporting_summary(rows)
    by_status = Counter(r["report_status"] or "—" for r in progress_reports(rows))
    return {
        "reports": progress_reports(rows),
        "summary": summary,
        "by_status": by_status.most_common(),
        "today": today,
        "options": {
            "statuses": sorted(
                x
                for x in dm.ReportedIndicator.objects.values_list("report_status", flat=True).distinct()
                if x
            ),
            "types": sorted(
                x for x in dm.ReportedIndicator.objects.values_list("report_type", flat=True).distinct() if x
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
