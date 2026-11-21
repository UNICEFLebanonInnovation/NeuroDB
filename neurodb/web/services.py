"""Headline figures for the public landing page: aggregate counts only, cached for an hour."""

from __future__ import annotations

from typing import Any

from django.core.cache import cache

from neurodb.core import years
from neurodb.core.models import SyncRun
from neurodb.datamart import monitoring, reporting
from neurodb.datamart.models import ReportedIndicator
from neurodb.library.models import Map, Resource

# + this year: 1 January starts new figures. v5: eTools alone (no ActivityInfo counts: an archive)
CACHE_KEY = "landing:highlights:v5:{year}"
CACHE_SECONDS = 3600


def cache_key(year: int | None = None) -> str:
    """The key of the figures of ``year`` (this year when None)."""
    return CACHE_KEY.format(year=year or years.this_year())


def public_highlights() -> dict[str, Any]:
    """Counts for this calendar year, from eTools (NeuroDB no longer uses ActivityInfo, an archive whose
    figures no page shows), and the published resources. No names, locations or values leave this
    function."""
    this_year = years.this_year()
    key = cache_key(this_year)
    data = cache.get(key)
    if data is not None:
        return data
    data = {
        "year": this_year,
        **_etools_counts(this_year),
        "resources": Resource.objects.filter(published=True).count()
        + Map.objects.filter(status="Completed").count(),
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
        # each its programme document, type, number and period (reporting.REPORT_KEY), whatever PRP's ids hold
        "progress_reports": ReportedIndicator.objects.filter(period_end__year=calendar_year)
        .exclude(reporting.NO_PD)  # a report naming no PD: never counted (reporting.NO_PD)
        .annotate(_report=reporting.REPORT_KEY)
        .values("_report")
        .distinct()
        .count(),
        "etools_update": last.finished_at if last else None,
    }
