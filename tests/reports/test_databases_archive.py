"""The ActivityInfo databases page once ActivityInfo is an archive (R1 browser-7): the view tells its template that
the import is no longer followed (``activityinfo.watched()`` false, as Data health's "Archive") and whether its
scheduled job still runs, so the page says so instead of calling a database "Stale" and describing the monthly
import. While ActivityInfo is followed, an old database is still "Stale" as before."""

from __future__ import annotations

import datetime

import pytest
from django.urls import reverse
from django.utils import timezone

from neurodb.core.models import ScheduledJob
from neurodb.indicators.models import Database
from neurodb.indicators.services import activityinfo
from tests.insights.test_forecast_archive import years_of_data  # noqa: F401 - the forecast fixture

pytestmark = pytest.mark.django_db

PAGE = reverse("reports:databases") + "?year=2026"
MONTHLY = "monthly, until the 22nd"
STALE_NOTE = "has not updated this one for more than 40 days"
IMPORT_ON = (
    "ActivityInfo is an archive: its import is no longer followed. It is still switched on in Scheduled jobs, "
    "but NeuroDB no longer warns when it fails or falls behind."
)
IMPORT_OFF = "ActivityInfo is an archive: its import is switched off and its databases are kept as they were."


@pytest.fixture
def old_import(hierarchy, database):
    """The year's database last imported 60 days ago: "Stale" while ActivityInfo is followed."""
    Database.objects.filter(pk=database.pk).update(
        last_monthly_update_date=timezone.now() - datetime.timedelta(days=60)
    )


def _switch_import(on: bool) -> None:
    ScheduledJob.objects.filter(key="activityinfo-data").update(enabled=on)
    activityinfo.invalidate()


def _page(client) -> str:
    response = client.get(PAGE)
    assert response.status_code == 200
    return " ".join(response.content.decode().split())


def test_the_page_knows_activityinfo_is_an_archive(client_viewer, hierarchy, activityinfo_archived):
    response = client_viewer.get(PAGE)
    assert response.status_code == 200 and response.context["activityinfo_archived"] is True


def test_the_page_knows_activityinfo_is_followed(client_viewer, hierarchy, activityinfo_in_use):
    response = client_viewer.get(PAGE)
    assert response.context["activityinfo_archived"] is False


def test_followed_an_old_database_is_stale_and_the_monthly_import_described(
    client_viewer, old_import, activityinfo_in_use
):
    html = _page(client_viewer)
    assert "Stale" in html and STALE_NOTE in html and MONTHLY in html
    assert "ActivityInfo is an archive" not in html


@pytest.mark.parametrize("import_on", [True, False])
def test_archived_no_database_is_stale_and_the_page_says_it_is_an_archive(
    client_viewer, old_import, activityinfo_archived, import_on
):
    _switch_import(import_on)
    html = _page(client_viewer)
    assert "Stale" not in html and STALE_NOTE not in html and MONTHLY not in html
    assert (IMPORT_ON if import_on else IMPORT_OFF) in html
    assert (IMPORT_OFF if import_on else IMPORT_ON) not in html


# ------------------------------------------------------------------------- the forecast card (R2 sweep-1)
CARD = "Likely to fall short by December"


@pytest.fixture
def short_forecast(years_of_data):  # noqa: F811 - the fixture imported above
    """The 2026 database (records to August: in use on the suite's day) with its year-end forecast shown and
    likely short (the forecast fixture of ``tests/insights/test_forecast_archive.py``)."""
    from neurodb.insights import services
    from neurodb.insights.management.commands.forecast_indicators import run
    from neurodb.insights.models import IndicatorForecast

    run("test")
    latest = services.latest_run()
    latest.details["backtest"]["shown"] = True
    latest.save()
    IndicatorForecast.objects.update(status=IndicatorForecast.Status.SHORT, low=500, high=700)
    return years_of_data


def _dashboard(client, database) -> str:
    response = client.get(reverse("reports:database_dashboard", args=[database.pk]))
    assert response.status_code == 200
    return response.content.decode()


def test_the_database_forecast_card_only_while_in_use_and_for_this_year(client, admin_user, short_forecast):
    """R2 sweep-1 (SPEC_HOME §8 rows 2, 3, 5 and 7): the database dashboard's "Likely to fall short by December"
    card shows while ActivityInfo is in use, on this year's database, as the Country overview's card. Archived,
    its forecasts are no longer made: no card."""
    from neurodb.core.models import ActivityInfoSetting
    from neurodb.insights.management.commands.forecast_indicators import run
    from tests.insights.test_forecast_archive import _mode

    client.force_login(admin_user)
    assert activityinfo.in_use() and CARD in _dashboard(client, short_forecast)
    _mode(ActivityInfoSetting.Mode.ARCHIVED)
    run("test")  # skipped: the last forecasts are kept, for the forecasts page
    assert CARD not in _dashboard(client, short_forecast)


def test_a_past_years_database_has_no_forecast_card(client, admin_user, short_forecast, site_today):
    """R2 sweep-1: on 5 January 2027, ActivityInfo set "In use" (the December import), the 2026 database's page no
    longer says its indicators are likely to fall short by a December that has passed."""
    from neurodb.core.models import ActivityInfoSetting
    from tests.insights.test_forecast_archive import _mode

    site_today.set(datetime.date(2027, 1, 5))
    _mode(ActivityInfoSetting.Mode.IN_USE)
    client.force_login(admin_user)
    assert activityinfo.in_use()
    assert CARD not in _dashboard(client, short_forecast)
