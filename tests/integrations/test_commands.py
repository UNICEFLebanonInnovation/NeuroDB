import datetime as dt
from io import StringIO

import pytest
import responses
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from neurodb.core.models import SyncRun

pytestmark = pytest.mark.django_db


def finished_run(job, hours_ago: float, status=SyncRun.Status.SUCCEEDED) -> SyncRun:
    run = SyncRun.objects.create(job=job, target="x")
    run.finish(status)
    SyncRun.objects.filter(pk=run.pk).update(finished_at=timezone.now() - dt.timedelta(hours=hours_ago))
    return run


def test_check_sync_freshness_fails_when_nothing_ran():
    with pytest.raises(CommandError, match="3 stale job"):
        call_command("check_sync_freshness", stdout=StringIO())


def test_check_sync_freshness_passes_with_recent_runs(settings, caplog):
    settings.SYNC_STALENESS_HOURS = 30
    finished_run(SyncRun.Job.ACTIVITYINFO_DATA, 1)
    finished_run(SyncRun.Job.ETOOLS_DATAMART, 2, SyncRun.Status.PARTIAL)
    finished_run(SyncRun.Job.LOCATIONS, 3)
    out = StringIO()
    call_command("check_sync_freshness", stdout=out)
    assert "all 3 job(s) fresh" in out.getvalue()


def test_check_sync_freshness_flags_old_and_failed_runs(settings, caplog):
    settings.SYNC_STALENESS_HOURS = 30
    finished_run(SyncRun.Job.ACTIVITYINFO_DATA, 40)
    finished_run(SyncRun.Job.ETOOLS_DATAMART, 1, SyncRun.Status.FAILED)
    finished_run(SyncRun.Job.LOCATIONS, 1)
    with (
        caplog.at_level("ERROR", logger="neurodb.integrations"),
        pytest.raises(CommandError, match="ai_data, etools_datamart"),
    ):
        call_command("check_sync_freshness", stdout=StringIO())
    assert any("ai_data is stale" in record.message for record in caplog.records)


def test_check_sync_freshness_rejects_unknown_job():
    with pytest.raises(CommandError, match="unknown job"):
        call_command("check_sync_freshness", jobs="bogus", stdout=StringIO())


@responses.activate
def test_sync_etools_only_subset(settings):
    settings.ETOOLS_BASE_URL = "https://etools.test"
    settings.ETOOLS_TOKEN = "abc"
    responses.get("https://etools.test/api/v2/partners/", json=[])
    out = StringIO()
    call_command("sync_etools", only="partners", stdout=out)
    assert "eTools sync [partners] succeeded" in out.getvalue()
    assert SyncRun.objects.filter(job=SyncRun.Job.ETOOLS).count() == 1
    assert responses.calls[0].request.headers["Authorization"] == "Token abc"


def test_sync_etools_unknown_entity_is_a_command_error():
    with pytest.raises(CommandError, match="unknown eTools entities"):
        call_command("sync_etools", only="nope", stdout=StringIO())


def test_import_activityinfo_data_unknown_database():
    with pytest.raises(CommandError, match="no database"):
        call_command("import_activityinfo_data", database=1, stdout=StringIO())


def test_import_activityinfo_data_failure_exits_non_zero(database, monkeypatch):
    import neurodb.integrations.management.commands.import_activityinfo_data as command_module

    def boom(db, *, run, keep_copy):
        run.finish(SyncRun.Status.FAILED, error="export failed")
        raise RuntimeError("export failed")

    monkeypatch.setattr(command_module, "import_data", boom)
    out, err = StringIO(), StringIO()
    with pytest.raises(CommandError, match="1 run\\(s\\) failed"):
        call_command("import_activityinfo_data", database=database.ai_id, stdout=out, stderr=err)
    assert "failed" in out.getvalue()
