"""The eTools update clean-up (``neurodb.datamart.backfill``): the work datamart 0010 and 0013 did
before the site started, which on the real tables kept the site from starting at all."""

import importlib

import psycopg
import pytest
from django.core.management import call_command
from django.db import connection, migrations
from django.db.migrations.loader import MigrationLoader

from neurodb.core import jobs, scheduler
from neurodb.core.admin_jobs import BACKGROUND_JOBS
from neurodb.core.models import ScheduledJob, SyncRun
from neurodb.datamart import backfill
from neurodb.datamart import models as dm
from neurodb.integrations import background

pytestmark = pytest.mark.django_db


def _needs_work():
    """A partner report row to fill and a record holding a contact detail."""
    to_fill = dm.ReportedIndicator.objects.create(
        datamart_id=1,
        data={"indicator_report": 501, "cp_output": "Output 2.1", "contact_email": "x@example.org"},
    )
    grant = dm.Grant.objects.create(datamart_id=2, name="SC2", data={"name": "SC2", "Phone": "+961 1"})
    return to_fill, grant


def test_the_two_migrations_do_no_work_before_the_site_starts():
    for name in ("0010_fill_reached_columns", "0013_scrub_contact_details"):
        migration = importlib.import_module(f"neurodb.datamart.migrations.{name}")
        assert migration.Migration.operations == []


def test_the_schedule_is_due_at_once_and_the_scheduler_starts_the_job(monkeypatch):
    job = ScheduledJob.objects.get(key=backfill.SCHEDULE_KEY)
    assert (job.command, job.schedule, job.enabled) == ("datamart_backfill", "*/15 * * * *", True)
    assert job.next_run_at is not None  # due now: not left for the scheduler to plan later
    started = []
    monkeypatch.setattr(jobs, "start", lambda key, triggered_by: started.append(key) or "started")
    assert backfill.SCHEDULE_KEY in scheduler.tick()
    assert started.count("datamart_backfill") == 1


def test_the_job_does_everything_then_switches_its_schedule_off():
    to_fill, grant = _needs_work()
    run = backfill.run("test")
    assert run.status == SyncRun.Status.SUCCEEDED and run.job == SyncRun.Job.DATAMART_BACKFILL
    assert run.details["fill"]["report_rows_filled"] == 1
    assert run.details["scrub"]["records_cleaned"] == 2
    assert run.details["schedule_switched_off"] is True
    to_fill.refresh_from_db()
    grant.refresh_from_db()
    assert (to_fill.indicator_report, to_fill.cp_output) == ("501", "Output 2.1")
    assert "contact_email" not in to_fill.data and grant.data == {"name": "SC2"}
    job = ScheduledJob.objects.get(key=backfill.SCHEDULE_KEY)
    assert (job.enabled, job.next_run_at) == (False, None)
    assert job.last_outcome.startswith("finished")
    again = backfill.run("test")  # nothing left: nothing written
    assert again.status == SyncRun.Status.SUCCEEDED and again.rows_written == 0


def test_the_job_waits_while_the_datamart_sync_runs():
    _needs_work()
    s = connection.settings_dict
    sync = psycopg.connect(
        dbname=s["NAME"],
        user=s["USER"] or None,
        password=s["PASSWORD"] or None,
        host=s["HOST"] or None,
        port=s["PORT"] or None,
        autocommit=True,
    )
    try:
        sync.execute("SELECT pg_advisory_lock(%s)", [background.DATAMART_LOCK_ID])
        assert backfill.run("test") is None
    finally:
        sync.close()
    assert not SyncRun.objects.filter(job=SyncRun.Job.DATAMART_BACKFILL).exists()
    assert ScheduledJob.objects.get(key=backfill.SCHEDULE_KEY).enabled  # the next try does it


def test_a_failed_step_is_a_failed_run_and_keeps_the_schedule(monkeypatch):
    def broken():
        raise RuntimeError("the database went away")

    monkeypatch.setattr(backfill, "STEPS", (("fill", backfill.fill_reached_columns), ("scrub", broken)))
    run = backfill.run("test")
    assert run.status == SyncRun.Status.FAILED and "the database went away" in run.error
    assert "fill" in run.details
    assert ScheduledJob.objects.get(key=backfill.SCHEDULE_KEY).enabled


def test_it_is_a_job_with_a_button_and_a_command():
    assert jobs.COMMANDS["datamart_backfill"].args == ("datamart_backfill",)
    assert jobs.COMMANDS["datamart_backfill"].sync_job == SyncRun.Job.DATAMART_BACKFILL
    assert [b.command for b in BACKGROUND_JOBS if b.name == "run_datamart_backfill"] == [
        ("datamart_backfill",)
    ]
    assert background.LOCK_IDS[SyncRun.Job.DATAMART_BACKFILL] == background.DATAMART_LOCK_ID
    _needs_work()
    call_command("datamart_backfill", "--triggered-by", "test")
    assert SyncRun.objects.get(job=SyncRun.Job.DATAMART_BACKFILL).triggered_by == "test"


# Every migration that runs code or SQL when a deployment applies it, and why that is quick. A
# deployment applies its migrations before the site starts, and App Service stops a container that
# has not started within its start-up limit (about four minutes): work over the large eTools or
# Monitoring insights tables (partner reports, records kept whole, rule checks) belongs in a
# background job a schedule starts after the deployment (as neurodb.datamart.backfill and core 0025).
QUICK_MIGRATIONS = {
    # schedules and settings rows
    ("core", "0007_default_schedules", "create"),
    ("core", "0009_compiler_youth_job", "create"),
    ("core", "0010_compiler_education_job", "create"),
    ("core", "0012_knowledge_hub_job", "create"),
    ("core", "0013_whats_new_job", "create"),
    ("core", "0014_ml_readiness_job", "create"),
    ("core", "0015_forecast_job", "create"),
    ("core", "0016_compiler_wellbeing_schedule", "create"),
    ("core", "0017_compiler_runs_schedule", "move"),
    ("core", "0018_watch_job", "create"),
    ("core", "0020_fmm_refresh_schedule", "create"),
    ("core", "0021_fmm_insights_schedule", "create"),
    ("core", "0022_fmm_ai_checks", "create"),
    ("core", "0023_fmm_ap_review", "create"),
    ("core", "0024_doc_review", "create"),
    ("core", "0025_etools_update_cleanup", "schedule"),
    # rules, prompts and topics shipped with the code
    ("fmm", "0004_rule_defaults", "seed"),
    ("fmm", "0006_prompt_defaults", "seed"),
    ("fmm", "0009_fms_scoring", "fms_weights"),
    ("fmm", "0011_lebanon_prompt_v2", "seed"),
    ("fmm", "0013_lebanon_rules", "seed_rules"),
    ("fmm", "0013_lebanon_rules", "seed_prompts"),
    ("fmm", "0015_action_point_prompts", "seed_prompts"),
    ("fmm", "0019_records", "new_rules_version"),
    ("fmm", "0019_records", "ask_full_refresh"),
    ("knowledge", "0005_review_topics", "seed"),
    # one statement over the visits (thousands of rows), or schema only
    ("fmm", "0018_visit_date", "fill_visit_date"),
    ("fmm", "0020_records_cleanup", "drop_legacy_checks_when_done"),
    ("fmm", "0021_figures", "RunSQL"),  # a trigger function and its triggers
    ("locations", "0002_location_tables_when_missing", "RunSQL"),
}


def test_no_new_migration_does_long_work_before_the_site_starts():
    loader = MigrationLoader(None, ignore_no_migrations=True)
    found = set()
    for (app, name), migration in loader.disk_migrations.items():
        if not type(migration).__module__.startswith("neurodb."):
            continue
        for op in migration.operations:
            if isinstance(op, migrations.RunPython) and op.code is not migrations.RunPython.noop:
                found.add((app, name, op.code.__name__))
            elif isinstance(op, migrations.RunSQL):
                found.add((app, name, "RunSQL"))
    new = sorted(found - QUICK_MIGRATIONS)
    assert not new, (
        f"{new}: a migration runs before the site starts, and App Service stops a container that has "
        "not started within about four minutes. Work over the eTools or Monitoring insights tables "
        "goes in a background job started by a schedule (neurodb.datamart.backfill, core 0025); a "
        "quick one (settings, schedules, prompts, a small table) is added to QUICK_MIGRATIONS."
    )
