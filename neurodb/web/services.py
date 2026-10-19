"""Headline figures for the public landing page: aggregate counts only, cached for an hour."""

from __future__ import annotations

from typing import Any

from django.core.cache import cache
from django.db.models import Max

from neurodb.core import years
from neurodb.core.models import SyncRun
from neurodb.datamart import monitoring, reporting
from neurodb.datamart.models import ReportedIndicator
from neurodb.facts.models import ActivityReportNew
from neurodb.indicators.models import Database, MasterIndicator, ReportingYear
from neurodb.indicators.services import activityinfo
from neurodb.library.models import Map, Resource

# + this year, the pk of its ActivityInfo reporting year (or "") and whether ActivityInfo is in use (1/0):
# 1 January, a reporting year getting its first records and a change of the ActivityInfo mode each start
# new figures.
CACHE_KEY = "landing:highlights:v4:{year}:{reporting_year}:{in_use}"
CACHE_SECONDS = 3600


def cache_key(st: activityinfo.State | None = None) -> str:
    """The key of the figures for the ActivityInfo state ``st`` (now when None)."""
    st = st or activityinfo.state()
    return CACHE_KEY.format(
        year=st.this_year, reporting_year=st.pks.get(st.this_year, ""), in_use=int(st.in_use)
    )


def public_highlights() -> dict[str, Any]:
    """Counts for this calendar year. The eTools counts always; the ActivityInfo counts only while
    ActivityInfo is in use, from this year's ActivityInfo reporting year (a year with records), so an
    archive never shows a "…reporting in ActivityInfo this year" figure next to copy saying it is no
    longer updated. No names, locations or values leave this function."""
    st = activityinfo.state()
    key = cache_key(st)
    data = cache.get(key)
    if data is not None:
        return data
    reporting_year = activityinfo.reporting_year_for(st.this_year) if st.in_use else None
    databases = (
        Database.objects.filter(reporting_year=reporting_year, display=True)
        if reporting_year
        else Database.objects.none()
    )
    facts = ActivityReportNew.objects.filter(dbase__in=databases)
    data = {
        "year": st.this_year,
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
        **_etools_counts(st.this_year),
        "resources": Resource.objects.filter(published=True).count()
        + Map.objects.filter(status="Completed").count(),
        "years": ReportingYear.objects.count(),
        # the last ActivityInfo import, said only while ActivityInfo is in use
        "last_update": (
            databases.aggregate(last=Max("last_monthly_update_date"))["last"] if reporting_year else None
        ),
    }
    cache.set(key, data, CACHE_SECONDS)
    return data


def _etools_counts(year: int | None = None) -> dict[str, Any]:
    """Partnerships and partner reporting from eTools in the calendar year ``year`` (this year when
    None), counted as the country overview counts them: programme documents running in the year (closed
    ones included) with their indicators and partners, and progress reports on periods ending in the
    year."""
    calendar_year = year or years.this_year()
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
        .exclude(reporting.NO_PD)  # a report naming no PD: never counted (reporting.NO_PD)
        .values("progress_report")
        .distinct()
        .count(),
        "etools_update": last.finished_at if last else None,
    }
