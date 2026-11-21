"""Data health and ActivityInfo, an archive of past monthly results (the owner's rule, November 2026): the page
lists no ActivityInfo job card, database import or run, whatever its state; the administration's Scheduled jobs and
runs still list them. The service the internal API, Ask NeuroDB's data freshness lookup and the daily review read
(``data_health``) still holds the archive's jobs and databases, each "Archive" while the import is not followed, as
before (SPEC_HOME §6)."""

from __future__ import annotations

import datetime

import pytest
from django.utils import timezone

from neurodb.core.models import SyncRun
from neurodb.facts.models import ActivityReportNew
from neurodb.indicators.models import Database, ReportingYear
from neurodb.indicators.services import activityinfo
from neurodb.reports import services
from tests.web.parts import placeholders, raw_get, text

pytestmark = pytest.mark.django_db

PAGE = "/data/health/"
AI_LABELS = ("ActivityInfo structure import", "ActivityInfo data import", "ActivityInfo partner links")


def _switch_import(on: bool) -> None:
    from neurodb.core.models import ScheduledJob

    ScheduledJob.objects.filter(key="activityinfo-data").update(enabled=on)
    activityinfo.invalidate()


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


@pytest.mark.parametrize("mode", ["auto", "in_use", "archived"])
def test_the_page_lists_no_activityinfo_job_database_or_run(client_viewer, hierarchy, ai_runs, mode):
    """Archived on Automatic, set in use (its alarms on) or archived: no card, block or run of ActivityInfo."""
    from neurodb.core.models import ActivityInfoSetting

    ActivityInfoSetting.objects.update_or_create(pk=1, defaults={"mode": mode})
    activityinfo.invalidate()
    assert "databases" not in placeholders(text(raw_get(client_viewer, PAGE)))
    html = client_viewer.get(PAGE).content.decode()
    for label in AI_LABELS:
        assert label not in html, label
    assert "ActivityInfo" not in html.split('<details class="sidebar__archive"')[0]
    assert "/databases/" not in html.split('<details class="sidebar__archive"')[0]
    assert {j["job"] for j in services.health_jobs(with_archive=False)}.isdisjoint(services.ARCHIVE_JOBS)
    assert not {r.job for r in services.recent_runs(with_archive=False)} & {
        *services.ARCHIVE_JOBS,
        "forecast",
    }


def test_the_service_still_holds_the_archive_for_the_api_and_ask(hierarchy, ai_runs):
    """hierarchy's records stop in February 2026: on 15 October ActivityInfo is archived (not recent), and its
    import's alarms are off; its import is still switched on (the migrations seed it)."""
    assert not activityinfo.watched() and activityinfo.state().schedule_on
    health = services.data_health()
    assert health["activityinfo_archived"] and health["year"].name == "2026"
    as_dict = services.health_as_dict(health)
    assert as_dict["activityinfo_archived"] and as_dict["databases_year"] == "2026"
    assert as_dict["activityinfo_import_on"] is True
    assert {j["job"]: j["state"] for j in as_dict["jobs"]}["ai_data"] == "archived"
    assert [d["state"] for d in as_dict["databases"]] == ["archive"]


def test_while_followed_the_service_checks_the_jobs_and_databases(hierarchy, ai_runs, activityinfo_in_use):
    assert activityinfo.watched()
    health = services.data_health()
    assert not health["activityinfo_archived"] and health["year"].name == "2026"
    assert {j["job"]: j["state"] for j in health["jobs"]}["ai_data"] == "stale"
    assert [d["state"] for d in health["databases"]] == ["stale"]


def test_archived_the_databases_are_those_of_the_last_activityinfo_year(section, reporting_year):
    """2025 has records; 2026, ticked current, has a database without any: on Automatic with the import
    switched off, the service reads the archive's 2025 databases, not the empty 2026 ones."""
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
    assert [(d["database"].name, d["state"]) for d in services.health_databases(older)] == [
        ("CP 2025", "archive")
    ]


def test_new_databases_waiting_for_records_are_followed(section, reporting_year):
    """2026 ticked current with a database but no record yet, the import on: still archived, but watched
    (setup_pending), so the service lists its databases as never imported, not as an archive."""
    Database.objects.create(
        ai_id=202601, db_id="a26", name="CP 2026", label="CP 2026", username="", password="",
        section=section, reporting_year=reporting_year, display=True,
    )  # fmt: skip
    activityinfo.invalidate()
    state = activityinfo.state()
    assert state.setup_pending and state.watched and not state.in_use
    assert services.health_year() == reporting_year
    assert [d["state"] for d in services.health_databases(reporting_year)] == ["unknown"]


def test_no_reporting_year_at_all(db):
    assert services.health_year() is None and services.health_databases(None) == []
