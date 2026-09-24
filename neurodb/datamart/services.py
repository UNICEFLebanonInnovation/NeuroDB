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


def programme_datamart(pd: PCA) -> dict[str, Any]:
    lines = list(dm.FundsReservation.objects.filter(intervention=pd))
    frs: dict[str, dict[str, Any]] = {}
    for line in lines:  # FR totals repeat on each of their lines: count each FR once
        frs.setdefault(
            line.fr_number,
            {
                "total": line.total_amt or 0,
                "actual": line.actual_amt or 0,
                "outstanding": line.outstanding_amt or 0,
                "currency": line.currency,
                "start": line.start_date,
                "end": line.end_date,
            },
        )
    action_points = dm.ActionPoint.objects.filter(intervention=pd)
    return {
        "fr_lines": lines,
        "fr_count": len(frs),
        "fr_total": sum(f["total"] for f in frs.values()),
        "fr_actual": sum(f["actual"] for f in frs.values()),
        "fr_outstanding": sum(f["outstanding"] for f in frs.values()),
        "indicators": indicators_for(dm.PDIndicator.objects.filter(intervention=pd)),
        "action_points": list(action_points.order_by("status", "due_date")[:50]),
        "open_action_points": action_points.filter(status__in=dm.ActionPoint.OPEN_STATUSES).count(),
        "engagements": _with_type_labels(list(pd.datamart_engagements.order_by("-start_date"))),
    }


# ---------------------------------------------------------------------------------------- partners
def partner_datamart(partner: PartnerOrganization) -> dict[str, Any]:
    engagements = _with_type_labels(
        list(dm.AuditEngagement.objects.filter(partner=partner).order_by("-start_date")[:50])
    )
    action_points = dm.ActionPoint.objects.filter(partner=partner)
    findings = dm.MonitoringFinding.objects.filter(partner=partner)
    tpm_visits = list(dm.TPMVisit.objects.filter(partner=partner).order_by("-start_date")[:20])
    recent_findings = list(findings.order_by("-end_date")[:20])
    return {
        "assessments": list(
            dm.PartnerAssessment.objects.filter(partner=partner).order_by("-completed_date")[:20]
        ),
        "psea": dm.PSEAAssessment.objects.filter(partner=partner).order_by("-assessment_date").first(),
        "engagements": engagements,
        "engagement_counts": dict(Counter(engagement_type_label(e.engagement_type) for e in engagements)),
        "action_points": list(
            action_points.filter(status__in=dm.ActionPoint.OPEN_STATUSES).order_by("due_date")[:20]
        ),
        "open_action_points": action_points.filter(status__in=dm.ActionPoint.OPEN_STATUSES).count(),
        "tpm_visits": tpm_visits,
        "findings": recent_findings,
        "monitoring_count": len(tpm_visits) + len(recent_findings),
        "finding_ratings": dict(Counter(f.overall_finding_rating or "—" for f in findings)),
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
    return {
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
