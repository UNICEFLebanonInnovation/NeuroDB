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
