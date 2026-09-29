"""Every operator command has a button in the admin ("Run a job" on Import and sync runs)."""

import pytest
from django.urls import reverse

from neurodb.core.admin_jobs import BACKGROUND_JOBS
from neurodb.core.models import PopulationFigure, SyncRun
from neurodb.integrations import background

pytestmark = pytest.mark.django_db


@pytest.fixture
def started(monkeypatch):
    calls = []
    monkeypatch.setattr(background, "start_command", lambda *args: calls.append(args) or 1)
    return calls


@pytest.fixture
def superadmin(client, admin_user):
    admin_user.is_superuser = True
    admin_user.save()
    client.force_login(admin_user)
    return client


def _url(name):
    return reverse(f"admin:core_syncrun_{name}")


def test_the_menu_lists_every_job(superadmin):
    html = superadmin.get(reverse("admin:core_syncrun_changelist")).content.decode()
    assert "Run a job" in html and "Sync eTools Datamart now" in html
    for title in [s.menu for s in BACKGROUND_JOBS] + [
        "Population figures",
        "Check freshness",
        "Repair roles",
    ]:
        assert str(title) in html


@pytest.mark.parametrize("spec", BACKGROUND_JOBS, ids=lambda s: s.name)
def test_background_jobs_start_their_command(superadmin, admin_user, started, spec):
    url = _url(spec.name)
    assert superadmin.get(url).status_code == 200 and started == []  # a GET only confirms
    assert superadmin.post(url, {"_form_submitted": "on"}).status_code == 302
    assert started == [(*spec.command, "--triggered-by", admin_user.username)]


def test_a_running_job_is_not_started_twice(superadmin, started, monkeypatch):
    monkeypatch.setattr(background, "lock_is_held", lambda lock_id: None)
    SyncRun.objects.create(job=SyncRun.Job.LOCATIONS, status=SyncRun.Status.RUNNING)
    response = superadmin.post(_url("run_sync_locations"), {"_form_submitted": "on"}, follow=True)
    assert started == [] and "already running" in response.content.decode()


def test_quick_jobs_run_in_the_request(superadmin, roles):
    response = superadmin.post(_url("run_bootstrap_roles"), {"_form_submitted": "on"}, follow=True)
    assert "Roles ready" in response.content.decode()

    response = superadmin.post(_url("run_check_freshness"), {"_form_submitted": "on"}, follow=True)
    assert "Out of date" in response.content.decode()  # nothing has ever synced in the test database

    response = superadmin.post(_url("run_reload_population"), {"_form_submitted": "on"}, follow=True)
    assert "Loaded" in response.content.decode() and PopulationFigure.objects.exists()


def test_viewers_cannot_run_jobs(client, viewer, started):
    viewer.is_staff = True
    viewer.save()
    client.force_login(viewer)
    for spec in BACKGROUND_JOBS:
        client.post(_url(spec.name), {"_form_submitted": "on"})
    client.post(_url("run_reload_population"), {"_form_submitted": "on"})
    assert started == [] and not PopulationFigure.objects.exists()


def test_database_imports_run_in_the_background(superadmin, admin_user, started, database):
    response = superadmin.post(
        reverse("admin:pivoting_database_changelist"),
        {"action": "queue_data_import", "_selected_action": [database.pk]},
    )
    assert response.status_code == 302
    assert started == [
        ("import_activityinfo_data", "--database", str(database.ai_id), "--triggered-by", admin_user.username)
    ]


@pytest.mark.parametrize(
    ("job", "error", "hint"),
    [
        ("etools", "GET /api/v2/partners/: HTTP 503 Service Unavailable", "eTools was unavailable"),
        ("ai_data", "POST /resources/query: ReadTimeout", "ActivityInfo did not answer in time"),
        (
            "etools_datamart",
            "GET /api/latest/datamart/: HTTP 401 Unauthorized",
            "refused NeuroDB's credentials",
        ),
        ("etools", "a KeyError nobody foresaw", ""),
    ],
)
def test_known_errors_get_a_plain_hint(job, error, hint):
    from neurodb.core.admin import error_hint

    result = str(error_hint(SyncRun(job=job, error=error)))
    assert (hint in result) if hint else result == ""


def test_triggered_by_reads_as_scheduler_manual_or_command():
    from neurodb.core.admin import triggered_by_label

    assert str(triggered_by_label("schedule")) == "Scheduler"
    assert str(triggered_by_label("command")) == "Command line"
    assert str(triggered_by_label("demo-admin")) == "Manual (demo-admin)"
    assert triggered_by_label("") == "—"


def test_the_run_list_explains_the_two_etools_syncs_and_its_errors(superadmin):
    SyncRun.objects.create(job="etools", status="failed", error="GET /x: HTTP 503 Service Unavailable")
    SyncRun.objects.create(job="population", status="succeeded", triggered_by="demo-admin")
    html = superadmin.get(reverse("admin:core_syncrun_changelist")).content.decode()
    assert "Two eTools syncs" in html and "Import and sync runs" in html and "Data and sync" in html
    assert "eTools was unavailable: try again later." in html and "HTTP 503" in html
    assert "Manual (demo-admin)" in html
