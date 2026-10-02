"""Reading the forecasts for the pages and the assistant: the latest run, its back-test, and whether
forecasts may be shown."""

from __future__ import annotations

from typing import Any

from neurodb.core.models import SyncRun

from .models import IndicatorForecast

STATUS_PILLS = {  # forecast status -> the colours of the existing status pills
    "on_course": "on_track",
    "likely_short": "off_track",
    "uncertain": "partial",
}
MONTHS = ["", "January", "February", "March", "April", "May", "June", "July", "August", "September",
          "October", "November", "December"]  # fmt: skip


def latest_run() -> SyncRun | None:
    return SyncRun.last_success(SyncRun.Job.FORECAST)


def shown(run: SyncRun | None = None) -> bool:
    """Forecasts appear on the dashboards only after the back-test showed they are good enough."""
    run = run if run is not None else latest_run()
    return bool(run and (run.details.get("backtest") or {}).get("shown"))


def forecasts(**filters: Any):
    return (
        IndicatorForecast.objects.filter(**filters)
        .select_related("master", "database")
        .order_by("database__label", "master__sequence", "master__awp_code")
    )


def at_risk(section_ids: list[int] | None = None, database_id: int | None = None, limit: int = 8):
    """The indicators likely to fall short, the furthest from their target first."""
    if not shown():
        return []
    qs = IndicatorForecast.objects.filter(status=IndicatorForecast.Status.SHORT).select_related(
        "master", "database"
    )
    if section_ids:
        qs = qs.filter(section_id__in=section_ids)
    if database_id:
        qs = qs.filter(database_id=database_id)
    rows = sorted(qs, key=lambda f: f.high_pct if f.high_pct is not None else 0)
    return rows[:limit]
