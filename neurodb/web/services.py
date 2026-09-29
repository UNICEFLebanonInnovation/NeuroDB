"""Headline figures for the public landing page: aggregate counts only, cached for an hour."""

from __future__ import annotations

from typing import Any

from django.core.cache import cache
from django.db.models import Max
from django.utils import timezone

from neurodb.core.models import SyncRun
from neurodb.datamart import monitoring
from neurodb.datamart.models import ReportedIndicator
from neurodb.facts.models import ActivityReportNew
from neurodb.indicators.models import Database, MasterIndicator, ReportingYear
from neurodb.indicators.services.navigation import current_year
from neurodb.library.models import Map, Resource

CACHE_KEY = "landing:highlights:v3"
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
        **_etools_counts(year),
        "resources": Resource.objects.filter(published=True).count()
        + Map.objects.filter(status="Completed").count(),
        "years": ReportingYear.objects.count(),
        "last_update": databases.aggregate(last=Max("last_monthly_update_date"))["last"],
    }
    cache.set(CACHE_KEY, data, CACHE_SECONDS)
    return data


def _etools_counts(year: Any) -> dict[str, Any]:
    """Partnerships and partner reporting from eTools, counted as the country overview counts them:
    programme documents running in the year (closed ones included) with their indicators and
    partners, and progress reports on periods ending in the year."""
    label = str((year.year or year.name) if year else "").strip()
    calendar_year = int(label) if label.isdigit() else timezone.localdate().year
    rows = monitoring.indicators(
        monitoring.Filters(year=calendar_year, report_type=monitoring.DEFAULT_REPORT_TYPE, scope="year")
    )
    pds = {row.pd.id: row.pd for row in rows}
    last = SyncRun.last_success(SyncRun.Job.ETOOLS_DATAMART)
    return {
        "calendar_year": calendar_year,
        "running_programmes": len(pds),
        "etools_partners": len({pd.partner_id for pd in pds.values() if pd.partner_id}),
        "pd_indicators": len(rows),
        "progress_reports": ReportedIndicator.objects.filter(period_end__year=calendar_year)
        .exclude(progress_report="")
        .values("progress_report")
        .distinct()
        .count(),
        "etools_update": last.finished_at if last else None,
    }
