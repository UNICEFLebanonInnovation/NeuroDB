"""Headline figures for the public landing page: aggregate counts only, cached for an hour."""

from __future__ import annotations

from typing import Any

from django.core.cache import cache
from django.db.models import Max

from neurodb.facts.models import ActivityReportNew
from neurodb.indicators.models import Database, MasterIndicator, ReportingYear
from neurodb.indicators.services.navigation import current_year
from neurodb.library.models import Map, Resource
from neurodb.partnerships.models import PCA

CACHE_KEY = "landing:highlights:v1"
CACHE_SECONDS = 3600


def public_highlights() -> dict[str, Any]:
    """Counts for the current reporting year. No names, locations or values leave this function."""
    data = cache.get(CACHE_KEY)
    if data is not None:
        return data
    year = current_year()
    databases = (
        Database.objects.filter(reporting_year=year, display=True) if year else Database.objects.none()
    )
    facts = ActivityReportNew.objects.filter(dbase__in=databases)
    data = {
        "year": year.name if year else None,
        "sections": databases.exclude(section=None).values("section").distinct().count(),
        "databases": databases.count(),
        "indicators": MasterIndicator.objects.filter(database__in=databases, is_active=True).count(),
        "records": facts.count(),
        "partners": facts.exclude(partner_label__isnull=True)
        .exclude(partner_label="")
        .values("partner_label")
        .distinct()
        .count(),
        "governorates": facts.exclude(location_adminlevel_governorate_code__isnull=True)
        .exclude(location_adminlevel_governorate_code="")
        .values("location_adminlevel_governorate_code")
        .distinct()
        .count(),
        "active_programmes": PCA.objects.filter(status="active").count(),
        "resources": Resource.objects.filter(published=True).count()
        + Map.objects.filter(status="Completed").count(),
        "years": ReportingYear.objects.count(),
        "last_update": databases.aggregate(last=Max("last_monthly_update_date"))["last"],
    }
    cache.set(CACHE_KEY, data, CACHE_SECONDS)
    return data
