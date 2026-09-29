"""Programme documents, donors and partners (eTools replica tables), ported from v2 views with ORM only."""

from __future__ import annotations

import datetime
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any

from django.db.models import Count, Exists, IntegerField, OuterRef, Q, QuerySet, Subquery, Sum

from neurodb.datamart.models import ReportedIndicator
from neurodb.facts.models import ActivityReportNew

from .models import PCA, Engagement, PartnerLink, PartnerOrganization, TravelActivity

ACTIVE_STATUSES = ("active",)
EXCLUDED_STATUSES = ("draft",)
# statuses of a programme document still running; eTools can lag in closing one after its end date
RUNNING_STATUSES = ("active", "signed", "suspended")


def past_end_date(status: str | None, end: datetime.date | None, today: datetime.date | None = None) -> bool:
    """True for a running programme document whose end date has passed (its status not yet updated)."""
    return bool(status in RUNNING_STATUSES and end and end < (today or datetime.date.today()))


def _to_float(value: Any) -> float:
    try:
        return float(str(value).replace(",", "")) if value not in (None, "") else 0.0
    except ValueError:
        return 0.0


def _getlist(params, key: str) -> list[str]:
    values = params.getlist(key) if hasattr(params, "getlist") else params.get(key, [])
    if isinstance(values, str):
        values = [values]
    return [v for v in values if v]


@dataclass(frozen=True)
class PDFilters:
    partners: list[str]
    cso_types: list[str]
    sections: list[str]
    offices: list[str]
    statuses: list[str]
    donors: list[str]
    grants: list[str]
    numbers: list[str]
    document_types: list[str]
    year_from: int | None
    year_to: int | None

    @classmethod
    def from_params(cls, params) -> PDFilters:
        years = (params.get("years") or "").split(",") if params.get("years") else []
        return cls(
            partners=_getlist(params, "partner"),
            cso_types=_getlist(params, "cso_type"),
            sections=_getlist(params, "section"),
            offices=_getlist(params, "office"),
            statuses=_getlist(params, "status"),
            donors=_getlist(params, "donor"),
            grants=_getlist(params, "grant"),
            numbers=_getlist(params, "pd"),
            document_types=_getlist(params, "document_type"),
            year_from=int(years[0]) if years and years[0].isdigit() else None,
            year_to=int(years[1]) if len(years) > 1 and years[1].isdigit() else None,
        )

    def apply(self, qs: QuerySet) -> QuerySet:
        if self.partners:
            qs = qs.filter(partner_name__in=self.partners)
        if self.cso_types:
            qs = qs.filter(partner__cso_type__in=self.cso_types)
        if self.statuses:
            qs = qs.filter(status__in=self.statuses)
        if self.numbers:
            qs = qs.filter(number__in=self.numbers)
        if self.document_types:
            qs = qs.filter(document_type__in=self.document_types)
        for values, fieldname in (
            (self.sections, "section_names"),
            (self.offices, "offices_set"),
            (self.donors, "donors"),
            (self.grants, "grants"),
        ):
            if values:
                q = Q()
                for v in values:
                    q |= Q(**{f"{fieldname}__contains": [v]})
                qs = qs.filter(q)
        if self.year_from:
            qs = qs.filter(start__year__gte=self.year_from)
        if self.year_to:
            qs = qs.filter(start__year__lte=self.year_to)
        return qs


def programme_documents(filters: PDFilters | None = None, *, scope: str = "all") -> QuerySet[PCA]:
    qs = PCA.objects.exclude(status__in=EXCLUDED_STATUSES).select_related("partner")
    if scope == "active":
        qs = qs.filter(status__in=ACTIVE_STATUSES)
    if filters:
        qs = filters.apply(qs)
    # id breaks ties so that pages neither repeat nor skip a row
    return qs.order_by("-start", "number", "id")


def pd_filter_options() -> dict[str, list[str]]:
    # no ordering: SELECT DISTINCT would otherwise also select the Meta.ordering column and repeat values
    base = PCA.objects.exclude(status__in=EXCLUDED_STATUSES).order_by()
    sections, offices, donors, grants = set(), set(), set(), set()
    for row in base.values_list("section_names", "offices_set", "donors", "grants"):
        sections.update(row[0] or [])
        offices.update(row[1] or [])
        donors.update(row[2] or [])
        grants.update(row[3] or [])
    return {
        "partners": sorted(
            base.exclude(partner_name__isnull=True).values_list("partner_name", flat=True).distinct()
        ),
        "cso_types": sorted(
            x
            for x in PartnerOrganization.objects.order_by().values_list("cso_type", flat=True).distinct()
            if x
        ),
        "sections": sorted(sections),
        "offices": sorted(offices),
        "statuses": sorted(x for x in base.values_list("status", flat=True).distinct() if x),
        "donors": sorted(donors),
        "grants": sorted(grants),
        "document_types": sorted(x for x in base.values_list("document_type", flat=True).distinct() if x),
        "numbers": sorted(x for x in base.values_list("number", flat=True).distinct() if x),
        "years": sorted({d.year for d in base.exclude(start__isnull=True).values_list("start", flat=True)}),
    }


def pd_intervention_counts(numbers: list[str]) -> dict[str, int]:
    """ActivityInfo records per PD number prefix (v2 matched project_label to the number before '-')."""
    prefixes = {n.split("-")[0] for n in numbers}
    rows = (
        ActivityReportNew.objects.filter(project_label__in=prefixes)
        .values("project_label")
        .annotate(n=Count("id"))
    )
    return {r["project_label"]: r["n"] for r in rows}


def _donor_rows(donors_set: Any) -> list[dict[str, Any]]:
    """eTools donor entries used several key spellings over the years; normalise them once here."""
    rows = []
    for d in donors_set or []:
        if not isinstance(d, dict):
            continue
        rows.append(
            {
                "donor": str(d.get("donor") or d.get("donor_name") or d.get("name") or "Unknown"),
                "grant": str(d.get("grant") or d.get("grant_number") or ""),
                # a raw eTools FR line item (older REST syncs) carries overall_amount, not value
                "value": _to_float(
                    d.get("value") if d.get("value") not in (None, "") else d.get("overall_amount")
                ),
            }
        )
    return rows


def pd_detail(pd: PCA) -> dict[str, Any]:
    donors = _donor_rows(pd.donors_set)
    prefix = (pd.number or "").split("-")[0]
    return {
        "pd": pd,
        "donations": sum(d["value"] for d in donors),
        "donors": donors,
        # a PD without a number has no ActivityInfo records (a blank label matches unrelated ones)
        "interventions": ActivityReportNew.objects.filter(project_label=prefix).count() if prefix else 0,
        "sections": pd.section_names or [],
        "offices": pd.offices_set or [],
    }


def pd_summary(scope: str = "active") -> dict[str, Any]:
    """Counts by partner and by section and PDs ending within 90 days (v2 PCA summary pages).

    The same PDs as :func:`programme_documents` for the scope: every non-draft PD, or the active ones.
    """
    qs = PCA.objects.exclude(status__in=EXCLUDED_STATUSES)
    if scope == "active":
        qs = qs.filter(status__in=ACTIVE_STATUSES)
    by_partner = Counter()
    by_section = Counter()
    by_type = Counter()
    budget_total = 0.0
    for pd in qs.only("partner_name", "section_names", "document_type", "total_budget"):
        by_partner[pd.partner_name or "Unknown"] += 1
        for s in pd.section_names or []:
            by_section[s] += 1
        by_type[pd.document_type or "Unknown"] += 1
        budget_total += _to_float(pd.total_budget)
    today = datetime.date.today()
    ending_soon = PCA.objects.filter(
        status__in=ACTIVE_STATUSES, end__gte=today, end__lte=today + datetime.timedelta(days=90)
    ).order_by("end")
    return {
        "scope": scope,
        "count": qs.count(),
        "budget_total": budget_total,
        "by_partner": by_partner.most_common(),
        "by_section": by_section.most_common(),
        "by_type": by_type.most_common(),
        "ending_soon": list(ending_soon.select_related("partner")),
        "last_sync": qs.order_by("-updated_at").values_list("updated_at", flat=True).first(),
    }


def donor_mapping(filters: PDFilters) -> dict[str, Any]:
    """Donor page: programmes, funds per donor, interventions, planned vs actual locations."""
    qs = filters.apply(PCA.objects.exclude(status__in=EXCLUDED_STATUSES))
    pds = list(
        qs.only(
            "id",
            "number",
            "title",
            "partner_name",
            "status",
            "start",
            "end",
            "donors",
            "donors_set",
            "section_names",
            "offices_set",
            "location_p_codes",
            "document_type",
            "total_budget",
        )
    )
    funds_by_donor: Counter[str] = Counter()
    funds_by_year: Counter[int] = Counter()
    programmes = []
    planned_codes: Counter[str] = Counter()
    for pd in pds:
        donations = 0.0
        for d in _donor_rows(pd.donors_set):
            donations += d["value"]
            funds_by_donor[d["donor"]] += d["value"]
            if pd.start:
                funds_by_year[pd.start.year] += d["value"]
        for code in pd.location_p_codes or []:
            planned_codes[code] += 1
        programmes.append({"pd": pd, "donations": donations})
    numbers = [pd.number for pd in pds if pd.number]
    prefixes = list({n.split("-")[0] for n in numbers})
    interventions = ActivityReportNew.objects.filter(project_label__in=prefixes)
    interventions_by_gov = list(
        interventions.exclude(location_adminlevel_governorate__isnull=True)
        .values("location_adminlevel_governorate")
        .annotate(n=Count("id"))
        .order_by("-n")
    )
    return {
        "programmes": programmes,
        "count": len(pds),
        "donations_total": sum(p["donations"] for p in programmes),
        "funds_by_donor": funds_by_donor.most_common(25),
        "funds_by_year": sorted(funds_by_year.items()),
        "interventions_total": interventions.count(),
        "interventions_by_governorate": interventions_by_gov,
        "unique_actual_locations": interventions.exclude(location_name__isnull=True)
        .values("location_name")
        .distinct()
        .count(),
        "planned_locations": sum(planned_codes.values()),
        "planned_locations_unique": len(planned_codes),
    }


# ------------------------------------------------------------------------- partners


def partners(params) -> QuerySet[PartnerOrganization]:
    qs = PartnerOrganization.objects.filter(deleted_flag=False, hidden=False)
    types = _getlist(params, "partner_type")
    csos = _getlist(params, "cso_type")
    q = (params.get("q") or "").strip()
    if types:
        qs = qs.filter(partner_type__in=types)
    if csos:
        qs = qs.filter(cso_type__in=csos)
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(short_name__icontains=q) | Q(vendor_number__icontains=q))
    activityinfo = (
        PartnerLink.objects.filter(partner=OuterRef("pk"))
        .values("partner")
        .annotate(total=Sum("records"))
        .values("total")
    )
    return qs.annotate(
        # every non-draft PD, the same set as the partner profile's 'All documents'
        pd_count=Count(
            "interventions", distinct=True, filter=~Q(interventions__status__in=EXCLUDED_STATUSES)
        ),
        active_pd_count=Count(
            "interventions", distinct=True, filter=Q(interventions__status__in=ACTIVE_STATUSES)
        ),
        activityinfo_records=Subquery(activityinfo, output_field=IntegerField()),
        etools_reporting=Exists(ReportedIndicator.objects.filter(partner=OuterRef("pk"))),
    ).order_by("name", "id")


def partner_filter_options() -> dict[str, list[str]]:
    base = PartnerOrganization.objects.filter(deleted_flag=False, hidden=False).order_by()
    return {
        "partner_types": sorted(x for x in base.values_list("partner_type", flat=True).distinct() if x),
        "cso_types": sorted(x for x in base.values_list("cso_type", flat=True).distinct() if x),
    }


def partner_profile(partner: PartnerOrganization) -> dict[str, Any]:
    pds = PCA.objects.filter(partner=partner).exclude(status__in=EXCLUDED_STATUSES).order_by("-start")
    engagements = Engagement.objects.filter(partner=partner).order_by("-start_date")
    eng_counts = Counter(e.engagement_type for e in engagements)
    visits = TravelActivity.objects.filter(partner=partner).exclude(date__isnull=True)
    visits_by_year: dict[int, int] = defaultdict(int)
    for d in visits.values_list("date", flat=True):
        visits_by_year[d.year] += 1
    return {
        "partner": partner,
        "programme_documents": list(pds),
        "active_count": sum(1 for p in pds if p.status in ACTIVE_STATUSES),
        "engagements": list(engagements[:50]),
        "engagements_total": engagements.count(),
        "engagement_counts": dict(eng_counts),
        "visits_by_year": sorted(visits_by_year.items()),
        "hact": partner.hact_values or {},
        "risk_rating": partner.rating,
    }
