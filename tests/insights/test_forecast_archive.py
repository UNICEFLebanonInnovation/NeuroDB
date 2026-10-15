"""Year-end forecasts once ActivityInfo is an archive: the weekly job skips and keeps the last table, the
latest run is the last one that forecast, the pages say the forecasts are no longer updated, and a page
about a year never lists another year's forecasts (V4)."""

import datetime
import itertools
from io import StringIO

import pytest
from django.core.management import call_command
from django.urls import reverse

from neurodb.core.models import ActivityInfoSetting, SyncRun
from neurodb.facts.models import ActivityReportNew
from neurodb.indicators.models import (
    Activity,
    Database,
    IndicatorNew,
    MasterIndicator,
    MasterSubIndicator,
    ReportingYear,
    SubIndicator,
)
from neurodb.indicators.services import activityinfo
from neurodb.insights import forecast, services
from neurodb.insights.management.commands.forecast_indicators import run
from neurodb.insights.models import IndicatorForecast

pytestmark = pytest.mark.django_db

SEASON = [0.3, 0.5, 0.8, 1, 1, 1.1, 0.7, 0.6, 1.3, 1.4, 1.5, 1.8]
BANNER = "These forecasts were made for 2026 while ActivityInfo was in use. They are no longer updated."
_ids = itertools.count(800001)


def _mode(mode: str) -> None:
    ActivityInfoSetting.objects.update_or_create(pk=1, defaults={"mode": mode})
    activityinfo.invalidate()


def _database(year: ReportingYear, section, last_month: int, display: bool = True) -> Database:
    """A database of ``year`` with one additive master indicator reported every month up to
    ``last_month``."""
    number = int(year.year)
    n = next(_ids)
    database = Database.objects.create(
        ai_id=n, db_id=f"db{n}", name=f"CP {year.name}", label=f"Child Protection {year.name}", username="",
        password="", section=section, reporting_year=year, is_funded_by_unicef=False, display=display,
    )  # fmt: skip
    activity = Activity.objects.create(database=database, name="Case", label="Case", ai_form_id=f"f{n}")
    leaf = IndicatorNew.objects.create(
        database=database, activity=activity, ai_indicator=f"i{n}", name="Children", awp_code="1.1"
    )
    sub = SubIndicator.objects.create(
        database=database, name="Children", awp_code="1.1", aggregation_method="SUM"
    )
    sub.indicators.set([leaf])
    master = MasterIndicator.objects.create(
        database=database, name="Children in case management", awp_code="CP.1", aggregation_method="SUM",
        awp_target=1000, sequence=1,
    )  # fmt: skip
    MasterSubIndicator.objects.create(master=master, sub=sub, effect="TOTAL", sequence=1)
    for month in range(1, last_month + 1):
        ActivityReportNew.objects.create(
            dbase=database, database_ai_id=str(n), indicator_id=f"i{n}", indicator_value=SEASON[month - 1] * 40,
            month_name=f"{number}-{month:02d}-01", partner_label="Partner A",
        )  # fmt: skip
    activityinfo.invalidate()  # records reach the state at the end of an import, which drops it
    return database


@pytest.fixture
def years_of_data(db, section):
    """2023–2026, the same master indicator each year; 2026 reported up to August (in use on 15 October)."""
    for number in (2023, 2024, 2025, 2026):
        year = ReportingYear.objects.create(name=str(number), year=str(number), current=number == 2026)
        current = _database(year, section, 8 if number == 2026 else 12)
    return current


def test_archived_the_job_skips_and_keeps_the_last_forecasts(years_of_data):
    made = run("test")
    assert made.status == SyncRun.Status.SUCCEEDED and "skipped" not in made.details
    assert IndicatorForecast.objects.count() == 1
    _mode(ActivityInfoSetting.Mode.ARCHIVED)
    skipped = run("test")
    assert skipped.status == SyncRun.Status.SUCCEEDED
    assert skipped.details == {"skipped": "ActivityInfo is not in use in 2026", "year": 2026}
    assert IndicatorForecast.objects.count() == 1  # the table is kept as it was
    assert services.latest_run() == made  # the last run that forecast, never a skipped one
    assert SyncRun.last_success(SyncRun.Job.FORECAST) == skipped  # the job itself ran (fresh for Watch)


def test_in_use_without_records_this_year_the_job_skips_too(db, section):
    _database(ReportingYear.objects.create(name="2025", year="2025", current=True), section, 12)
    _mode(ActivityInfoSetting.Mode.IN_USE)  # in use, but 2026 has no ActivityInfo reporting year
    assert run("test").details["skipped"] == "ActivityInfo is not in use in 2026"


def test_the_command_says_it_skipped(years_of_data):
    _mode(ActivityInfoSetting.Mode.ARCHIVED)
    out = StringIO()
    call_command("forecast_indicators", stdout=out)
    assert "skipped: ActivityInfo is not in use in 2026; the last forecasts are kept" in out.getvalue()


def test_the_year_forecast_is_this_year_of_the_activityinfo_reporting_year(years_of_data, section):
    """Another reporting year numbered 2026 (a hidden pilot) is history for the back-test, never this year's
    forecast."""
    pilot = ReportingYear.objects.create(name="2026 pilot", year="2026", current=False)
    _database(pilot, section, 8, display=False)
    result = forecast.run(datetime.date(2026, 10, 2))
    assert result["year"] == 2026
    assert [f.inst.database_id for f in result["forecasts"]] == [years_of_data.pk]


def test_at_risk_keeps_to_the_year_asked(years_of_data):
    run("test")
    latest = services.latest_run()
    latest.details["backtest"]["shown"] = True
    latest.save()
    IndicatorForecast.objects.update(status=IndicatorForecast.Status.SHORT, low=500, high=700)
    assert len(services.at_risk()) == len(services.at_risk(year=2026)) == 1
    assert services.at_risk(year=2027) == []


def test_archived_the_forecasts_page_says_they_are_no_longer_updated(years_of_data, admin_user, client):
    run("test")
    client.force_login(admin_user)
    page = client.get(reverse("insights:forecasts"), {"section": ""}).content.decode()
    assert BANNER not in page and "Children in case management" in page
    _mode(ActivityInfoSetting.Mode.ARCHIVED)
    run("test")  # skipped: the page still shows the last forecasts, for 2026
    page = client.get(reverse("insights:forecasts"), {"section": ""}).content.decode()
    assert BANNER in page and 'class="alert alert-secondary"' in page
    assert "Children in case management" in page  # kept, readable
