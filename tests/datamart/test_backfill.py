"""The eTools update clean-up (``neurodb.datamart.backfill``): the work datamart 0010 and 0013 did
before the site started, which on the real tables kept the site from starting at all."""

import importlib
import io
import math

import psycopg
import pytest
from django.core.management import call_command
from django.db import connection, migrations
from django.db.migrations.loader import MigrationLoader
from django.test.utils import CaptureQueriesContext

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


# The migration stamped the job due at the real moment the test database was created, before any test moved
# the clock: "due now" is a statement about the real clock.
@pytest.mark.real_clock
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


def _other_session(*lock_ids):
    """Another database session (another process) holding ``lock_ids``; close it to release them."""
    s = connection.settings_dict
    other = psycopg.connect(
        dbname=s["NAME"],
        user=s["USER"] or None,
        password=s["PASSWORD"] or None,
        host=s["HOST"] or None,
        port=s["PORT"] or None,
        autocommit=True,
    )
    for lock_id in lock_ids:
        other.execute("SELECT pg_advisory_lock(%s)", [lock_id])
    return other


def test_the_job_waits_while_the_datamart_sync_runs():
    _needs_work()
    sync = _other_session(background.DATAMART_LOCK_ID)
    try:
        assert backfill.run("test") == backfill.SYNC_RUNNING
        assert not background.datamart_cleanup_is_running()  # the sync's, not the clean-up's
        assert not background.lock_is_held(background.DATAMART_BACKFILL_LOCK_ID)  # released at once
    finally:
        sync.close()
    assert not SyncRun.objects.filter(job=SyncRun.Job.DATAMART_BACKFILL).exists()
    job = ScheduledJob.objects.get(key=backfill.SCHEDULE_KEY)
    assert job.enabled and job.last_outcome == backfill.WAITING  # the next try does it


def test_started_by_hand_during_a_sync_its_schedule_is_switched_back_on():
    """A finished run switched the schedule off; *Run a job* pressed during a sync must still happen."""
    _needs_work()
    backfill.finished_schedule()
    sync = _other_session(background.DATAMART_LOCK_ID)
    try:
        call_command("datamart_backfill", "--triggered-by", "admin", stdout=(out := io.StringIO()))
    finally:
        sync.close()
    assert backfill.SYNC_RUNNING in out.getvalue()
    job = ScheduledJob.objects.get(key=backfill.SCHEDULE_KEY)
    assert job.enabled and job.next_run_at is not None and job.next_run_at.minute % 15 == 0
    assert job.last_outcome == backfill.WAITING


def test_a_second_clean_up_does_nothing_and_says_so():
    _needs_work()
    first = _other_session(background.DATAMART_BACKFILL_LOCK_ID, background.DATAMART_LOCK_ID)
    try:
        call_command("datamart_backfill", "--triggered-by", "test", stdout=(out := io.StringIO()))
    finally:
        first.close()
    assert backfill.ALREADY_RUNNING in out.getvalue() and "sync is running" not in out.getvalue()
    assert not SyncRun.objects.filter(job=SyncRun.Job.DATAMART_BACKFILL).exists()
    assert ScheduledJob.objects.get(key=backfill.SCHEDULE_KEY).last_outcome == ""


def test_the_job_holds_its_own_lock_and_the_sync_lock_while_it_runs(monkeypatch):
    held = []

    def step():
        held.append((background.datamart_cleanup_is_running(), background.datamart_lock_is_held()))
        return {}

    monkeypatch.setattr(backfill, "STEPS", (("probe", step),))
    assert backfill.run("test").status == SyncRun.Status.SUCCEEDED
    assert held == [(True, True)]
    assert not background.datamart_cleanup_is_running() and not background.datamart_lock_is_held()


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
    # its own lock (the sync knows the clean-up holds the Datamart lock), unique like every job's
    assert background.LOCK_IDS[SyncRun.Job.DATAMART_BACKFILL] == background.DATAMART_BACKFILL_LOCK_ID
    assert len(set(background.LOCK_IDS.values())) == len(background.LOCK_IDS)
    _needs_work()
    call_command("datamart_backfill", "--triggered-by", "test")
    assert SyncRun.objects.get(job=SyncRun.Job.DATAMART_BACKFILL).triggered_by == "test"


def test_the_fill_strips_spaces_as_the_sync_does():
    """The sync strips every CharField: " 77 " is stored as "77", and a record naming only spaces
    leaves the three columns empty, so the fill writes nothing there."""
    padded = dm.ReportedIndicator.objects.create(
        datamart_id=11, data={"indicator_report": " 77 ", "cp_output": {"name": "Output 2.1 \t"}}
    )
    blank = dm.ReportedIndicator.objects.create(datamart_id=12, data={"cp_output": "  "})
    assert backfill.fill_reached_columns()["report_rows_filled"] == 1
    padded.refresh_from_db()
    blank.refresh_from_db()
    assert (padded.indicator_report, padded.cp_output) == ("77", "Output 2.1")
    assert (blank.indicator_report, blank.cp_output, blank.etools_cp_output_id) == ("", "", "")


def _bounded(queries, table, marker):
    """The statements on ``table`` containing ``marker``, each bounded by an id window."""
    found = [q["sql"] for q in queries if f'"{table}"' in q["sql"] or f" {table} " in q["sql"]]
    found = [sql for sql in found if marker in sql]
    assert found and all("id <= " in sql for sql in found), found
    return found


def test_the_clean_up_reads_each_table_once_window_by_window(monkeypatch):
    """A search ``ORDER BY id LIMIT n`` read the rest of the table again for every batch: hours on a
    large table where many records hold contact details. Each statement now reads one window of ids."""
    monkeypatch.setattr(backfill, "SCRUB_WINDOW", 2)
    monkeypatch.setattr(backfill, "SCRUB_BATCH", 2)
    monkeypatch.setattr(backfill, "FILL_BATCH", 2)
    grants = [
        dm.Grant.objects.create(datamart_id=100 + n, name=f"SC{n}", data={"name": f"SC{n}", "Phone": "+961"})
        for n in range(5)
    ]
    reports = [
        dm.ReportedIndicator.objects.create(
            datamart_id=200 + n,
            submitted_by="someone@example.org",
            data={"submitted_by": "someone@example.org"},
        )
        for n in range(5)
    ]
    with CaptureQueriesContext(connection) as queries:
        counts = backfill.scrub_contact_details()
    assert counts["records_cleaned"] == 5 and counts["submitted_by_cleared"] == 5
    for grant in grants:
        grant.refresh_from_db()
        assert grant.data == {"name": grant.name}
    for report in reports:
        report.refresh_from_db()
        assert (report.submitted_by, report.data["submitted_by"]) == ("", "")
    searches = _bounded(queries.captured_queries, "datamart_grant", "~*")
    assert len(searches) == math.ceil(len(grants) / 2)  # one statement per window, not per batch found
    _bounded(queries.captured_queries, "datamart_reportedindicator", "submitted_by = CASE")
    assert backfill.scrub_contact_details() == dict.fromkeys(counts, 0)  # nothing left: nothing written


def test_the_one_off_clean_up_is_not_a_sync_that_goes_stale_and_a_run_with_nothing_to_do_rebuilds_nothing(
    monkeypatch,
):
    from neurodb.graph import refresh as graph_refresh
    from neurodb.reports.services import data_health

    asked = []
    monkeypatch.setattr(graph_refresh, "request", lambda why: asked.append(why))
    _needs_work()
    first = backfill.run("test")
    assert first.rows_written > 0 and asked == ["eTools update clean-up"]
    assert backfill.run("test").rows_written == 0 and asked == ["eTools update clean-up"]
    assert SyncRun.Job.DATAMART_BACKFILL not in [row["job"] for row in data_health()["jobs"]]


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
    ("core", "0029_reporting_checks_job", "create"),
    ("core", "0031_etools_masters_job", "create"),
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
    # the shared page figures' table: a new, empty table and its index
    ("core", "0026_shared_cache_table", "RunSQL"),
    # SET LOCAL lock_timeout before two plain indexes over the sync runs (a few thousand rows, built in
    # milliseconds; the wait for the table's lock is bounded to 10 s, never a concurrent build)
    ("core", "0027_syncrun_indexes", "RunSQL"),
    # the sections' comments on the HACT visit plan: a new, empty table (CreateModel only, no code runs;
    # listed so that every migration of the HACT visit plan is accounted for here)
    ("datamart", "0014_visit_plan_comment", "CreateModel"),
    # the eTools master indicators, their years and their PD indicators: three new, empty tables (CreateModel
    # only, no code runs; listed so that the migration is accounted for here)
    ("datamart", "0016_etools_masters", "CreateModel"),
    # whether ActivityInfo is in use or an archive: a new, empty table of one row (CreateModel only, no code
    # runs; listed so that the migration is accounted for here)
    ("core", "0028_activityinfo_setting", "CreateModel"),
    # the eTools Datamart's PRP business area code (a new, empty table of one row) and the probe's job choice
    # (CreateModel and AlterField only, no code runs; listed so that the migration is accounted for here)
    ("core", "0030_datamart_prp_setting", "CreateModel"),
    # the PRP business area code accepts digits only: validator and help text (AlterField only, no code runs;
    # listed so the migration is accounted for here)
    ("core", "0032_datamart_area_digits", "AlterField"),
    # SET LOCAL lock_timeout before the knowledge search's rank column, added empty (no default: the table's
    # description only, whatever the number of passages); the knowledge hub job fills it after start-up
    ("knowledge", "0007_chunk_rank_vector", "RunSQL"),
    # the comparison with Power BI's views (PowerBiView, PowerBiCheck, PowerBiCard): three new, empty tables
    # of a few dozen rows each (CreateModel only, no code runs; listed so the migration is accounted for here)
    ("education", "0002_power_bi", "CreateModel"),
    # the youth figures of all years together in a table of their own: a new table, and at most one row
    # moved into it from the youth figures (the "all" row a build of this release may have kept there)
    ("youth", "0002_youth_all_years", "move_all_years"),
    # the person names the eTools records hold, kept out of what Ask sends: a new, empty table of one row
    # (CreateModel only, no code runs; the next eTools Datamart sync fills it)
    ("datamart", "0015_record_people", "CreateModel"),
    # AI settings (provider, sealed keys, models): a new, empty table of one row; and the AI use's tokens
    # written to the cache, a column with a database default (metadata only on PostgreSQL over a table of one
    # row per day, feature and model), and two help texts (CreateModel, AddField and AlterField only, no code
    # runs; listed so that the migration is accounted for here)
    ("assistant", "0004_ai_settings", "CreateModel"),
    # the label of NeuroDB Watch's "credit ran out" note reason, which no longer names OpenAI (AlterField of a
    # choice label only, no code runs and nothing in the database changes; listed so that the migration is
    # accounted for here)
    ("watch", "0002_quota_label", "AlterField"),
    # the menu each role sees (the superusers' Menu page): a new, empty table of at most three rows (CreateModel
    # only, no code runs; listed so that the migration is accounted for here)
    ("web", "0001_menu_choice", "CreateModel"),
    # the fields a superuser reviewed on Raw eTools data: a new, empty table of a few dozen rows (CreateModel
    # only, no code runs; listed so that the migration is accounted for here)
    ("datamart", "0017_raw_field_review", "CreateModel"),
    # the eTools REST records kept whole for Raw eTools data: a new, empty table with its key and index (CreateModel
    # only, no code runs; listed so that the migration is accounted for here)
    ("datamart", "0018_rest_record", "CreateModel"),
    # the choice of the job that reads the eTools REST API into Raw eTools data (AlterField of the job's choices
    # only, no code runs and nothing in the database changes; listed so that the migration is accounted for here)
    ("core", "0033_etools_rest_raw_job", "AlterField"),
    # the KM Database's versions, documents and findings: new, empty tables with their search indexes; the
    # workbook is read by the background load, never by a migration (CreateModel only, no code runs; listed
    # so that the migration is accounted for here)
    ("km", "0001_initial", "CreateModel"),
    # the KM Database's corrections of how its downloads show an author entry (organisation, person or
    # words): a new, empty table an administrator fills in the admin (CreateModel only, no code runs)
    ("km", "0002_organisation", "CreateModel"),
}


def _runs_code(operations):
    """The names of the operations that run code or SQL, also inside SeparateDatabaseAndState."""
    for op in operations:
        if isinstance(op, migrations.RunPython) and op.code is not migrations.RunPython.noop:
            yield op.code.__name__
        elif isinstance(op, migrations.RunSQL):
            yield "RunSQL"
        elif isinstance(op, migrations.SeparateDatabaseAndState):
            yield from _runs_code(op.database_operations)


def test_the_guard_sees_code_inside_separate_database_and_state():
    def fill(apps, schema_editor):
        pass

    hidden = migrations.SeparateDatabaseAndState(
        database_operations=[migrations.RunPython(fill), migrations.RunSQL("SELECT 1")]
    )
    assert list(_runs_code([hidden, migrations.RunPython(migrations.RunPython.noop)])) == ["fill", "RunSQL"]


def test_no_new_migration_does_long_work_before_the_site_starts():
    loader = MigrationLoader(None, ignore_no_migrations=True)
    found = set()
    for (app, name), migration in loader.disk_migrations.items():
        if not type(migration).__module__.startswith("neurodb."):
            continue
        found |= {(app, name, code) for code in _runs_code(migration.operations)}
    new = sorted(found - QUICK_MIGRATIONS)
    assert not new, (
        f"{new}: a migration runs before the site starts, and App Service stops a container that has "
        "not started within about four minutes. Work over the eTools or Monitoring insights tables "
        "goes in a background job started by a schedule (neurodb.datamart.backfill, core 0025); a "
        "quick one (settings, schedules, prompts, a small table) is added to QUICK_MIGRATIONS."
    )
