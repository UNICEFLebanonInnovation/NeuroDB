"""Data health once ActivityInfo is an archive (SPEC_HOME §6): while its import's alarms are off (not *watched*),
its two jobs read "Not in use (archive)" on a neutral pill, never stale or failing, and the databases part lists
the archive's last year, each "Archive", under a note; while watched, the year the import serves (or the year
ticked current while its new databases wait for their first records), fresh, stale or never imported as before.
The page's own parts are checked, not only ``data_health()``."""

from __future__ import annotations

import datetime
import re

import pytest
from django.utils import timezone

from neurodb.core.models import SyncRun
from neurodb.facts.models import ActivityReportNew
from neurodb.indicators.models import Database, ReportingYear
from neurodb.indicators.services import activityinfo
from neurodb.reports import services
from tests.web.parts import get_part, text

pytestmark = pytest.mark.django_db

PAGE = "/data/health/"
NOTE = "ActivityInfo is an archive: these {} databases are kept as they were and are no longer imported."


def _job_card(html: str, label: str) -> str:
    """One job's card (the cards are panels side by side)."""
    return next(
        card for card in html.split('<div class="panel">') if f'<h3 class="panel__title">{label}</h3>' in card
    )


def _rows(html: str) -> dict[str, str]:
    """Database label -> its state pill's status."""
    return {
        label: status
        for label, status in re.findall(
            r'<tr data-href="[^"]*"><td><a href="[^"]*">([^<]+)</a>.*?data-status="([^"]+)"', html, re.S
        )
    }


@pytest.fixture
def ai_runs(database):
    """An old success of each ActivityInfo job (stale by the staleness rule) and a failed import since."""
    long_ago = timezone.now() - datetime.timedelta(days=60)
    for job in (SyncRun.Job.ACTIVITYINFO_STRUCTURE, SyncRun.Job.ACTIVITYINFO_DATA):
        run = SyncRun.objects.create(job=job, started_at=long_ago)
        run.finish(SyncRun.Status.SUCCEEDED)
        SyncRun.objects.filter(pk=run.pk).update(finished_at=long_ago)
    failed = SyncRun.objects.create(job=SyncRun.Job.ACTIVITYINFO_DATA, started_at=timezone.now())
    failed.finish(SyncRun.Status.FAILED, error="timeout")
    database.last_monthly_update_date = timezone.now() - datetime.timedelta(days=90)  # stale while followed
    database.save()


def test_archived_the_jobs_and_databases_say_archive(client_viewer, hierarchy, ai_runs):
    """hierarchy's records stop in February 2026: on 15 October ActivityInfo is archived (not recent), and its
    import's alarms are off."""
    assert not activityinfo.watched()
    jobs = text(get_part(client_viewer, PAGE, "jobs"))
    for label in ("ActivityInfo structure import", "ActivityInfo data import"):
        card = _job_card(jobs, label)
        assert 'class="pill pill--neutral pill--sm" data-status="archived"' in card, label
        assert "Not in use (archive)" in card and "Stale" not in card and "Never succeeded" not in card
    databases = text(get_part(client_viewer, PAGE, "databases"))
    assert NOTE.format("2026") in databases
    assert _rows(databases) == {"Child Protection": "archive"}
    assert ">Archive<" in databases and "Stale" not in databases and "Never imported" not in databases
    # the internal API and the daily review read the same (health_as_dict, ctx.health)
    health = services.data_health()
    assert health["activityinfo_archived"] and health["year"].name == "2026"
    as_dict = services.health_as_dict(health)
    assert as_dict["activityinfo_archived"] and as_dict["databases_year"] == "2026"
    assert {j["job"]: j["state"] for j in as_dict["jobs"]}["ai_data"] == "archived"
    assert [d["state"] for d in as_dict["databases"]] == ["archive"]


def test_while_followed_the_jobs_and_databases_are_checked(
    client_viewer, hierarchy, ai_runs, activityinfo_in_use
):
    assert activityinfo.watched()
    jobs = text(get_part(client_viewer, PAGE, "jobs"))
    card = _job_card(jobs, "ActivityInfo data import")
    assert 'data-status="stale"' in card and "Not in use" not in card
    databases = text(get_part(client_viewer, PAGE, "databases"))
    assert "is an archive" not in databases and _rows(databases) == {"Child Protection": "stale"}
    health = services.data_health()
    assert not health["activityinfo_archived"] and health["year"].name == "2026"


def test_archived_the_databases_are_those_of_the_last_activityinfo_year(
    client_viewer, section, reporting_year
):
    """2025 has records; 2026, ticked current, has a database without any: on Automatic with the import
    switched off, Data health lists the archive's 2025 databases, not the empty 2026 ones."""
    from neurodb.core.models import ScheduledJob

    ScheduledJob.objects.update(enabled=False)
    older = ReportingYear.objects.create(name="2025", year="2025")
    db25 = Database.objects.create(
        ai_id=202501, db_id="a25", name="CP 2025", label="CP 2025", username="", password="",
        section=section, reporting_year=older, display=True,
    )  # fmt: skip
    Database.objects.create(
        ai_id=202601, db_id="a26", name="CP 2026", label="CP 2026", username="", password="",
        section=section, reporting_year=reporting_year, display=True,
    )  # fmt: skip
    ActivityReportNew.objects.create(
        dbase=db25, database_ai_id="202501", indicator_id="x", month_name="2025-12-01"
    )
    activityinfo.invalidate()
    assert services.health_year() == older
    databases = text(get_part(client_viewer, PAGE, "databases"))
    assert NOTE.format("2025") in databases and _rows(databases) == {"CP 2025": "archive"}


def test_new_databases_waiting_for_records_are_followed(client_viewer, section, reporting_year):
    """2026 ticked current with a database but no record yet, the import on: still archived, but watched
    (setup_pending), so its databases are listed as never imported, not as an archive."""
    Database.objects.create(
        ai_id=202601, db_id="a26", name="CP 2026", label="CP 2026", username="", password="",
        section=section, reporting_year=reporting_year, display=True,
    )  # fmt: skip
    activityinfo.invalidate()
    state = activityinfo.state()
    assert state.setup_pending and state.watched and not state.in_use
    assert services.health_year() == reporting_year
    databases = text(get_part(client_viewer, PAGE, "databases"))
    assert "is an archive" not in databases and _rows(databases) == {"CP 2026": "unknown"}
    assert "Never imported" in databases


def test_no_reporting_year_at_all(client_viewer, db):
    assert services.health_year() is None
    databases = text(get_part(client_viewer, PAGE, "databases"))
    assert "No ActivityInfo databases." in databases and "is an archive" not in databases
