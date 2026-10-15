"""``forecast_indicators``: back-test the year-end forecast on the past years, then forecast every
additive master indicator of this year's ActivityInfo reporting year.

Only while ActivityInfo is in use and this year has ActivityInfo records
(:mod:`neurodb.indicators.services.activityinfo`): otherwise the run succeeds at once with the note
``details["skipped"]``, and the forecasts of the last run that made them are kept as they were (the pages
say they are no longer updated)."""

from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from neurodb.core import years
from neurodb.core.models import SyncRun
from neurodb.indicators.services import activityinfo
from neurodb.insights import forecast
from neurodb.insights.models import IndicatorForecast
from neurodb.integrations.management.commands._base import add_triggered_by, exit_on_failure, write_summary
from neurodb.integrations.runs import fail, new_run


def skip_reason(year: int) -> str:
    """Why the forecast does not run for ``year`` ('' when it does): ActivityInfo not in use, or no
    ActivityInfo reporting year with records for it."""
    if not activityinfo.in_use() or activityinfo.reporting_year_for(year) is None:
        return f"ActivityInfo is not in use in {year}"
    return ""


def run(triggered_by: str = "schedule", today=None) -> SyncRun:
    sync_run = new_run(SyncRun.Job.FORECAST, "", triggered_by)
    year = (today or years.today()).year
    if reason := skip_reason(year):  # the table is kept: the pages say it is no longer updated
        sync_run.finish(SyncRun.Status.SUCCEEDED, skipped=reason, year=year)
        return sync_run
    try:
        result = forecast.run(today)
        now = timezone.now()
        rows = [
            IndicatorForecast(
                master_id=f.inst.master_id,
                database_id=f.inst.database_id,
                section_id=f.inst.section,
                year=result["year"],
                as_of_month=f.as_of,
                value_to_date=f.to_date,
                target=f.inst.target,
                forecast=f.forecast,
                low=f.low,
                high=f.high,
                linear=f.linear,
                status=f.status,
                basis=f.basis[:120],
                own_years=f.own_years,
                computed_at=now,
            )
            for f in result["forecasts"]
        ]
        with transaction.atomic():
            IndicatorForecast.objects.all().delete()
            IndicatorForecast.objects.bulk_create(rows, batch_size=1000)
    except Exception as exc:
        return fail(sync_run, exc)
    sync_run.rows_in = result["indicators_history"]
    sync_run.rows_written = len(rows)
    counts = {}
    for r in rows:
        counts[r.status] = counts.get(r.status, 0) + 1
    sync_run.finish(
        SyncRun.Status.SUCCEEDED,
        year=result["year"],
        as_of=result["as_of"],
        backtest=result["backtest"],
        statuses=counts,
    )
    return sync_run


class Command(BaseCommand):
    help = "Back-test and forecast the year-end value of every ActivityInfo master indicator"

    def add_arguments(self, parser):
        add_triggered_by(parser)

    def handle(self, *args, **options):
        result = run(options["triggered_by"])
        if result.details.get("skipped"):
            self.stdout.write(f"skipped: {result.details['skipped']}; the last forecasts are kept")
            exit_on_failure(write_summary(self, [result]))
            return
        bt = result.details.get("backtest", {})
        for month, m in sorted(bt.get("by_month", {}).items()):
            self.stdout.write(
                f"month {month}: {m['cases']} cases, median error {m['error']:.0%} "
                f"(straight line {m['linear_error']:.0%}), range held {m['range_held']}"
            )
        self.stdout.write(f"shown: {bt.get('shown')}; statuses: {result.details.get('statuses')}")
        exit_on_failure(write_summary(self, [result]))
