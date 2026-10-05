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


def test_the_ai_settings_and_their_defaults(monkeypatch):
    loaded = _load(monkeypatch)
    assert loaded["FMM_AI"] is False  # off at deploy, switched on at go-live
    assert loaded["FMM_MODEL"] == loaded["AI_ASSISTANT_MODEL"]
    assert loaded["FMM_SAMPLING"] == "auto" and loaded["FMM_SAMPLING_RECHECK_DAYS"] == 30
    assert loaded["FMM_DAILY_TOKEN_CAP"] == 1_200_000 and loaded["FMM_MAX_CALLS_PER_DAY"] == 400
    assert loaded["FMM_CHAT_MAX_RUNNING"] == 4 and loaded["FMM_HISTORY_ANSWER_CHARS"] == 1500
    assert loaded["FMM_NIGHTLY_MAX_INSIGHTS"] == 12 and loaded["FMM_NIGHTLY_MIN_VISITS"] == 3
    assert loaded["FMM_MIN_VISITS_FOR_AI"] == 3 and loaded["FMM_NARRATIVE_CHARS"] == 600
    assert loaded["FMM_INSIGHTS_TIMEOUT_SECONDS"] == 90
    assert loaded["FMM_PAYLOAD_RETENTION_DAYS"] == 30 and loaded["FMM_RETENTION_DAYS"] == 180
    changed = _load(monkeypatch, FMM_AI="true", FMM_MODEL="gpt-x", FMM_SAMPLING="OFF")
    assert changed["FMM_AI"] is True and changed["FMM_MODEL"] == "gpt-x" and changed["FMM_SAMPLING"] == "off"
    with pytest.raises(ImproperlyConfigured, match="FMM_SAMPLING"):
        _load(monkeypatch, FMM_SAMPLING="sometimes")


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


def test_a_refresh_asks_for_a_hub_rebuild_and_the_briefs_do_not():
    """The hub reads the visits from stage 8a on: a finished refresh is new data for it; the AI briefs
    bring nothing the hub reads."""
    import inspect

    from neurodb.graph import refresh as graph_refresh

    skipped = inspect.getsource(graph_refresh.on_run_finished)
    assert "FMM_REFRESH" not in skipped and "SyncRun.Job.FMM_INSIGHTS" in skipped


@pytest.mark.django_db
def test_finished_briefs_do_not_rebuild_the_hub(monkeypatch):
    from neurodb.graph import refresh as graph_refresh

    asked = []
    monkeypatch.setattr(graph_refresh, "request", lambda why: asked.append(why))
    for job in (SyncRun.Job.FMM_INSIGHTS, SyncRun.Job.FMM_REFRESH):
        run = SyncRun.objects.create(job=job, target="nightly")
        run.finish(SyncRun.Status.SUCCEEDED)
    assert asked == ["Monitoring insights refresh"]


# ------------------------------------------------------------------------------------------ the briefs
def test_the_briefs_are_a_job_with_a_schedule_and_a_button():
    from neurodb.core.admin_jobs import BACKGROUND_JOBS
    from neurodb.core.jobs import COMMANDS

    command = COMMANDS["fmm_insights"]
    assert (command.args, command.sync_job) == (("fmm_insights",), SyncRun.Job.FMM_INSIGHTS)
    assert str(command.label) == "Write AI monitoring briefs"
    button = next(job for job in BACKGROUND_JOBS if job.name == "run_fmm_insights")
    assert (button.command, button.job, button.icon) == (
        ("fmm_insights",),
        SyncRun.Job.FMM_INSIGHTS,
        "auto_awesome",
    )


@pytest.mark.django_db
def test_the_morning_briefs_schedule_is_seeded():
    from neurodb.core.models import ScheduledJob

    job = ScheduledJob.objects.get(key="fmm-insights")
    assert (job.command, job.schedule, job.enabled) == ("fmm_insights", "40 5 * * *", True)


@pytest.mark.django_db
def test_data_health_needs_attention_lists_a_partial_night_of_briefs():
    from neurodb.web import health

    run = SyncRun.objects.create(job=SyncRun.Job.FMM_INSIGHTS, target="nightly", rows_in=4, rows_failed=3)
    run.finish(SyncRun.Status.PARTIAL)
    lines = {line.key: line.text for line in health._jobs()}
    assert "3 rows failed" in lines["job_partial:fmm_insights"]


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
