"""NeuroDB's own grouping of partners' PD indicators is removed (the owner's decision: the eTools Datamart has no
such grouping): its models, its nightly job with its schedule, runs and lock, its admin and its health line.

The two migrations are quick and listed: core 0036 alters the job's choices (nothing in the database changes) and
deletes the job's schedule rows and runs, and makes the reporting checks due a quarter of an hour later; datamart
0024 deletes the three models from the state only (no SQL): the tables stay one release, so an image of the
previous release that is rolled back to works with its data.

None of these would pass before the removal: the job, its choice, its lock and the three models existed, and the
two migrations did not."""

from __future__ import annotations

import datetime
import importlib.util
import re
from importlib import import_module
from io import StringIO

import pytest
from django.apps import apps as django_apps
from django.core.management import call_command
from django.db import connection, migrations
from django.db.migrations.loader import MigrationLoader
from django.utils import timezone

from neurodb.core import jobs
from neurodb.core.admin_jobs import BACKGROUND_JOBS
from neurodb.core.management.commands.ensure_legacy_tables import missing_models
from neurodb.core.models import ScheduledJob, SyncRun
from neurodb.integrations import background
from neurodb.web import admin_site, health
from tests.datamart.test_backfill import QUICK_MIGRATIONS

CORE = ("core", "0036_drop_etools_masters_job")
DATAMART = ("datamart", "0024_drop_etools_masters")
TABLES = ("datamart_etoolsmaster", "datamart_etoolsmasteryear", "datamart_etoolsmasterlink")
MODELS = ("EtoolsMaster", "EtoolsMasterYear", "EtoolsMasterLink")
COMMAND = "etools_masters"
LOCK = 7140440  # retired: never reused


@pytest.fixture(scope="module")
def loader():
    return MigrationLoader(None, ignore_no_migrations=True)


# ------------------------------------------------------------------------------------------- the migrations
def test_datamart_0024_deletes_the_three_models_from_the_state_only(loader):
    migration = loader.disk_migrations[DATAMART]
    (op,) = migration.operations
    assert isinstance(op, migrations.SeparateDatabaseAndState) and op.database_operations == []
    assert [(type(o).__name__, o.name) for o in op.state_operations] == [
        ("DeleteModel", "EtoolsMasterLink"),
        ("DeleteModel", "EtoolsMasterYear"),
        ("DeleteModel", "EtoolsMaster"),
    ]
    assert ("datamart", "0023_page_choices") in migration.dependencies and CORE in migration.dependencies
    assert loader.graph.leaf_nodes("datamart") == [DATAMART]


@pytest.mark.django_db
def test_datamart_0024_runs_no_sql():
    out = StringIO()
    call_command("sqlmigrate", *DATAMART, stdout=out)
    statements = [
        line
        for line in out.getvalue().splitlines()
        if line.strip() and not line.startswith("--") and line.strip() not in ("BEGIN;", "COMMIT;")
    ]
    assert statements == []


def test_core_0036_alters_the_choices_and_drops_the_rows(loader):
    migration = loader.disk_migrations[CORE]
    assert [type(op).__name__ for op in migration.operations] == ["AlterField", "RunPython"]
    alter, run = migration.operations
    assert (alter.model_name, alter.name) == ("syncrun", "job")
    assert list(alter.field.choices) == list(SyncRun.Job.choices)
    assert COMMAND not in dict(alter.field.choices)
    assert run.code.__name__ == "drop_rows" and run.reverse_code is migrations.RunPython.noop
    assert ("core", "0035_ap_history_job") in migration.dependencies
    assert loader.graph.leaf_nodes("core") == [CORE]


def test_both_are_listed_as_quick():
    assert {
        (*CORE, "AlterField"),
        (*CORE, "drop_rows"),
        (*DATAMART, "SeparateDatabaseAndState"),
    } <= QUICK_MIGRATIONS


@pytest.fixture
def state_apps(loader):
    """The models as core 0036 sees them (the knowledge hub's changes, which point to a run, among them)."""
    return loader.project_state(CORE, at_end=False).apps


def _drop_rows(state_apps):
    module = import_module("neurodb.core.migrations.0036_drop_etools_masters_job")
    module.drop_rows(state_apps, None)
    return module


@pytest.mark.django_db
def test_drop_rows_removes_the_schedule_and_runs_and_makes_the_checks_due_soon(state_apps):
    from neurodb.graph.models import Change

    ScheduledJob.objects.filter(command=COMMAND).delete()  # the test database's, if any
    ScheduledJob.objects.create(key="etools-masters", command=COMMAND, schedule="40 2 * * *", enabled=True)
    ScheduledJob.objects.create(
        key="etools-masters-copy", command=COMMAND, schedule="0 3 * * *", enabled=False
    )
    later = timezone.now() + datetime.timedelta(days=1)
    ScheduledJob.objects.update_or_create(
        key="reporting-checks",
        defaults={
            "command": "reporting_checks",
            "schedule": "15 2 * * *",
            "enabled": True,
            "next_run_at": later,
        },
    )
    other = ScheduledJob.objects.exclude(command=COMMAND).exclude(key="reporting-checks").count()
    gone = SyncRun.objects.create(job=COMMAND, status=SyncRun.Status.SUCCEEDED, finished_at=timezone.now())
    SyncRun.objects.create(job=COMMAND, status=SyncRun.Status.FAILED, finished_at=timezone.now())
    kept = SyncRun.objects.create(
        job=SyncRun.Job.REPORTING_CHECKS, status=SyncRun.Status.SUCCEEDED, finished_at=timezone.now()
    )
    change = Change.objects.create(
        detected_at=timezone.now(), run=gone, kind="partner", key="p1", name="Example", op="added"
    )

    before = timezone.now()
    module = _drop_rows(state_apps)

    assert not ScheduledJob.objects.filter(command=COMMAND).exists()
    assert ScheduledJob.objects.exclude(command=COMMAND).exclude(key="reporting-checks").count() == other
    assert not SyncRun.objects.filter(job=COMMAND).exists()
    assert SyncRun.objects.filter(pk=kept.pk).exists()
    change.refresh_from_db()
    assert change.run_id is None  # the knowledge hub's change is kept, with no run
    due = ScheduledJob.objects.get(key="reporting-checks").next_run_at
    assert before + module.AFTER_RELEASE <= due <= timezone.now() + module.AFTER_RELEASE
    assert module.AFTER_RELEASE == datetime.timedelta(minutes=15)


@pytest.mark.django_db
@pytest.mark.parametrize("enabled", [True, False])
def test_drop_rows_leaves_the_checks_alone_when_off_or_due_sooner(state_apps, enabled):
    soon = timezone.now() + datetime.timedelta(minutes=5)
    later = timezone.now() + datetime.timedelta(days=1)
    ScheduledJob.objects.update_or_create(
        key="reporting-checks",
        defaults={
            "command": "reporting_checks",
            "schedule": "15 2 * * *",
            "enabled": enabled,
            "next_run_at": soon if enabled else later,
        },
    )
    _drop_rows(state_apps)
    assert ScheduledJob.objects.get(key="reporting-checks").next_run_at == (soon if enabled else later)


@pytest.mark.django_db
def test_the_migrated_database_has_no_schedule_of_the_job():
    assert not ScheduledJob.objects.filter(command=COMMAND).exists()
    assert not ScheduledJob.objects.filter(key="etools-masters").exists()


@pytest.mark.django_db
def test_the_three_tables_stay_one_release_with_no_model():
    assert set(TABLES) <= set(connection.introspection.table_names())
    for name in MODELS:
        with pytest.raises(LookupError):
            django_apps.get_model("datamart", name)
    # the start-up's table check never recreates them nor stumbles on them
    assert not {m._meta.db_table for m in missing_models()} & set(TABLES)
    call_command("ensure_legacy_tables", stdout=StringIO())


# ------------------------------------------------------------------------------------------- the code
def test_the_job_its_choice_its_button_and_its_lock_are_gone():
    assert COMMAND not in jobs.COMMANDS and COMMAND not in dict(jobs.COMMAND_CHOICES)
    assert COMMAND not in SyncRun.Job.values and not hasattr(SyncRun.Job, "ETOOLS_MASTERS")
    assert not any("suggest_master_indicators" in job.command for job in BACKGROUND_JOBS)
    assert LOCK not in background.LOCK_IDS.values() and not hasattr(background, "ETOOLS_MASTERS_LOCK_ID")
    # the number stays reserved: no other lock takes it
    assert LOCK not in {
        value
        for name, value in vars(background).items()
        if name.endswith("LOCK_ID") and isinstance(value, int)
    }


@pytest.mark.parametrize(
    "module",
    [
        "neurodb.datamart.masters",
        "neurodb.datamart.master_links",
        "neurodb.datamart.masters_tab",
        "neurodb.datamart.curation",
        "neurodb.reports.master_views",
        "neurodb.integrations.management.commands.suggest_master_indicators",
    ],
)
def test_the_grouping_code_is_gone(module):
    assert importlib.util.find_spec(module) is None


def test_the_administration_lists_no_grouping():
    listed = {model for _group, _about, models in admin_site.GROUPS for model in models}
    keep = admin_site.DEVELOPER_ONLY_KEEP | set(admin_site.ICONS)
    for name in MODELS:
        assert f"datamart.{name}" not in listed | keep
    assert "pivoting.MasterIndicator" in listed  # the ActivityInfo archive's own, kept


@pytest.mark.django_db
def test_needs_attention_has_no_line_for_the_grouping(reporting_year):
    assert not hasattr(health, "_etools_masters_without_target")
    assert not [line for line in health.warnings() if re.search("etools_master", line.key)]
