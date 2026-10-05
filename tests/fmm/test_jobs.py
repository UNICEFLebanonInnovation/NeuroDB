"""Monitoring insights as jobs: their run kinds, locks and settings, and their place on Data health."""

import pytest
from django.core.exceptions import ImproperlyConfigured

from neurodb.core.models import SyncRun
from neurodb.integrations import background
from neurodb.reports.services import data_health
from tests.watch.test_plumbing import _load


def test_both_jobs_have_their_run_kind_and_lock():
    assert SyncRun.Job.FMM_REFRESH.label == "Monitoring insights refresh"
    assert SyncRun.Job.FMM_INSIGHTS.label == "Monitoring insights (AI briefs)"
    assert background.LOCK_IDS[SyncRun.Job.FMM_REFRESH] == background.FMM_REFRESH_LOCK_ID == 7140432
    assert background.LOCK_IDS[SyncRun.Job.FMM_INSIGHTS] == background.FMM_INSIGHTS_LOCK_ID == 7140433
    assert len(set(background.LOCK_IDS.values())) == len(background.LOCK_IDS)


@pytest.mark.django_db
def test_data_health_does_not_list_them_as_syncs():
    SyncRun.objects.create(job=SyncRun.Job.FMM_REFRESH, target="probe", status=SyncRun.Status.SUCCEEDED)
    jobs = [row["job"] for row in data_health()["jobs"]]
    assert SyncRun.Job.FMM_REFRESH not in jobs and SyncRun.Job.FMM_INSIGHTS not in jobs
    assert SyncRun.Job.ETOOLS_DATAMART in jobs


@pytest.mark.django_db
def test_a_running_refresh_is_known_by_its_lock(monkeypatch):
    SyncRun.objects.create(job=SyncRun.Job.FMM_REFRESH, target="probe")
    assert background.is_running(SyncRun.Job.FMM_REFRESH) is False  # nobody holds the lock: cut off
    run = SyncRun.objects.get(job=SyncRun.Job.FMM_REFRESH)
    assert run.status == SyncRun.Status.FAILED and run.error == background.CUT_OFF


def test_the_settings_and_their_defaults(monkeypatch):
    loaded = _load(monkeypatch)
    assert loaded["FMM_ENABLED"] is True and loaded["FMM_KEY_MIN_COVERAGE"] == 0.5
    apps = loaded["INSTALLED_APPS"]
    assert apps.index("neurodb.fmm") == apps.index("neurodb.web") - 1
    changed = _load(monkeypatch, FMM_ENABLED="false", FMM_KEY_MIN_COVERAGE="0.8")
    assert changed["FMM_ENABLED"] is False and changed["FMM_KEY_MIN_COVERAGE"] == 0.8
    for wrong in ("0", "1.5", "-0.2"):
        with pytest.raises(ImproperlyConfigured, match="FMM_KEY_MIN_COVERAGE"):
            _load(monkeypatch, FMM_KEY_MIN_COVERAGE=wrong)


# ------------------------------------------------------------------------------------------ the refresh
def test_the_refresh_is_a_job_with_a_schedule_and_a_button():
    from neurodb.core.admin_jobs import BACKGROUND_JOBS
    from neurodb.core.jobs import COMMANDS

    command = COMMANDS["fmm_refresh"]
    assert (command.args, command.sync_job) == (("fmm_refresh",), SyncRun.Job.FMM_REFRESH)
    button = next(job for job in BACKGROUND_JOBS if job.name == "run_fmm_refresh")
    assert (button.command, button.job, button.icon) == (
        ("fmm_refresh",),
        SyncRun.Job.FMM_REFRESH,
        "monitoring",
    )


@pytest.mark.django_db
def test_the_morning_schedule_is_seeded():
    from neurodb.core.models import ScheduledJob

    job = ScheduledJob.objects.get(key="fmm-refresh")
    assert (job.command, job.schedule, job.enabled) == ("fmm_refresh", "25 5 * * *", True)


def test_a_refresh_asks_for_a_hub_rebuild():
    """The hub reads the visits from stage 8a on: a finished refresh is new data for it."""
    import inspect

    from neurodb.graph import refresh as graph_refresh

    skipped = inspect.getsource(graph_refresh.on_run_finished)
    assert "FMM_REFRESH" not in skipped


@pytest.mark.django_db
def test_data_health_needs_attention_lists_a_failed_or_partial_refresh():
    from neurodb.web import health

    run = SyncRun.objects.create(job=SyncRun.Job.FMM_REFRESH, target="full")
    run.finish(SyncRun.Status.FAILED, error="RuntimeError: broken")
    lines = {line.key: line.text for line in health._jobs()}
    assert (
        "job_failed:fmm_refresh" in lines and "Monitoring insights refresh" in lines["job_failed:fmm_refresh"]
    )
    run = SyncRun.objects.create(job=SyncRun.Job.FMM_REFRESH, target="full", rows_failed=2)
    run.finish(SyncRun.Status.PARTIAL)
    lines = {line.key: line.text for line in health._jobs()}
    assert "2 rows failed" in lines["job_partial:fmm_refresh"]


def test_the_refresh_settings_and_their_defaults(monkeypatch):
    loaded = _load(monkeypatch)
    assert loaded["FMM_REFRESH_AFTER_SYNC"] == "inline" and loaded["FMM_REFRESH_MAX_PASSES"] == 3
    for value, read in (("background", "background"), ("OFF", "off"), ("false", "off")):
        assert _load(monkeypatch, FMM_REFRESH_AFTER_SYNC=value)["FMM_REFRESH_AFTER_SYNC"] == read
    with pytest.raises(ImproperlyConfigured, match="FMM_REFRESH_AFTER_SYNC"):
        _load(monkeypatch, FMM_REFRESH_AFTER_SYNC="sometimes")
