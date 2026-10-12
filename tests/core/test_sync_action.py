"""Starting the eTools Datamart sync from the admin (App Service has no scheduled jobs or shell)."""

import datetime

import pytest
from django.core.management import call_command
from django.urls import reverse
from django.utils import timezone

from neurodb.core.models import SyncRun
from neurodb.integrations import background
from neurodb.integrations.management.commands import sync_etools_datamart

pytestmark = pytest.mark.django_db
URL = "admin:core_syncrun_sync_etools_datamart"


@pytest.fixture
def started(monkeypatch):
    calls = []
    monkeypatch.setattr(background, "start_command", lambda *args: calls.append(args) or 123)
    return calls


@pytest.fixture
def admin_client(client, admin_user):
    client.force_login(admin_user)
    return client


@pytest.fixture
def credentials(settings):
    settings.ETOOLS_USERNAME, settings.ETOOLS_PASSWORD = "svc-user", "not-a-real-secret"


def test_the_changelist_offers_the_action(admin_client, admin_user):
    admin_user.is_superuser = True  # the role's model permissions come from bootstrap_roles
    admin_user.save()
    page = admin_client.get(reverse("admin:core_syncrun_changelist"))
    assert page.status_code == 200 and reverse(URL) in page.text
    dialog = admin_client.get(reverse(URL))
    assert dialog.status_code == 200 and "What to sync" in dialog.text


@pytest.mark.parametrize(
    ("scope", "expected"),
    [("core", ("--only", "locations,partners,interventions,intervention_budgets,agreements")), ("all", ())],
)
def test_starting_a_sync(admin_client, admin_user, started, credentials, scope, expected):
    response = admin_client.post(reverse(URL), {"_form_submitted": "on", "scope": scope})
    assert response.status_code == 302
    assert started == [("sync_etools_datamart", "--triggered-by", admin_user.username, *expected)]


def test_the_dialog_redirects_the_whole_page_when_posted_with_htmx(admin_client, started, credentials):
    response = admin_client.post(
        reverse(URL), {"_form_submitted": "on", "scope": "core"}, headers={"HX-Request": "true"}
    )
    assert response.status_code == 204 and response["HX-Redirect"] == reverse("admin:core_syncrun_changelist")


def test_missing_credentials_are_explained_and_nothing_starts(admin_client, started, settings):
    settings.ETOOLS_USERNAME, settings.ETOOLS_PASSWORD = "", ""
    response = admin_client.post(reverse(URL), {"_form_submitted": "on", "scope": "core"}, follow=True)
    assert started == [] and "ETOOLS_USERNAME" in response.text


def test_a_running_sync_is_not_started_twice(admin_client, started, credentials, monkeypatch):
    monkeypatch.setattr(background, "datamart_lock_is_held", lambda: True)  # its process is alive
    SyncRun.objects.create(job=SyncRun.Job.ETOOLS_DATAMART, target="partners", status=SyncRun.Status.RUNNING)
    response = admin_client.post(reverse(URL), {"_form_submitted": "on", "scope": "core"}, follow=True)
    assert started == [] and "already running" in response.text
    SyncRun.objects.update(started_at=timezone.now() - datetime.timedelta(hours=5))  # cut off long ago
    admin_client.post(reverse(URL), {"_form_submitted": "on", "scope": "core"})
    assert len(started) == 1


def test_a_run_whose_process_died_is_closed_and_the_next_sync_starts(
    admin_client, started, credentials, monkeypatch
):
    """A deployment restarts the container mid-sync: the row stays RUNNING but nobody holds the lock."""
    monkeypatch.setattr(background, "datamart_lock_is_held", lambda: False)
    run = SyncRun.objects.create(
        job=SyncRun.Job.ETOOLS_DATAMART, target="partner_reports", status=SyncRun.Status.RUNNING
    )
    admin_client.post(reverse(URL), {"_form_submitted": "on", "scope": "core"})
    assert len(started) == 1
    run.refresh_from_db()
    assert (run.status, run.error) == (SyncRun.Status.FAILED, background.CUT_OFF)
    assert run.finished_at is not None


def test_the_lock_check_sees_the_lock_the_command_takes():
    from django.db import connection

    assert background.datamart_lock_is_held() is False
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s)", [background.DATAMART_LOCK_ID])
        try:
            assert background.datamart_lock_is_held() is True
        finally:
            cursor.execute("SELECT pg_advisory_unlock(%s)", [background.DATAMART_LOCK_ID])
    assert background.datamart_lock_is_held() is False
    assert sync_etools_datamart.LOCK_ID == background.DATAMART_LOCK_ID


def test_the_lock_check_ignores_the_same_lock_in_another_database():
    import psycopg
    from django.db import connection

    s = connection.settings_dict
    other = psycopg.connect(
        dbname="postgres",
        user=s["USER"] or None,
        password=s["PASSWORD"] or None,
        host=s["HOST"] or None,
        port=s["PORT"] or None,
        autocommit=True,
    )
    try:
        other.execute("SELECT pg_advisory_lock(%s)", [background.DATAMART_LOCK_ID])
        assert background.datamart_lock_is_held() is False
    finally:
        other.close()


def test_selected_datasets_run_alone(admin_client, admin_user, started, credentials):
    data = {"_form_submitted": "on", "scope": "selected", "datasets": ["locations", "partners"]}
    response = admin_client.post(reverse(URL), data)
    assert response.status_code == 302
    assert started == [
        ("sync_etools_datamart", "--triggered-by", admin_user.username, "--only", "locations,partners")
    ]


def test_selected_scope_needs_a_dataset(admin_client, started, credentials):
    response = admin_client.post(reverse(URL), {"_form_submitted": "on", "scope": "selected"})
    assert started == [] and "Tick at least one dataset" in response.text
    dialog = admin_client.get(reverse(URL))
    assert 'value="locations"' in dialog.text and 'value="pd_indicators"' in dialog.text


def test_viewers_cannot_start_a_sync(client, viewer, started, credentials):
    viewer.is_staff = True
    viewer.save()
    client.force_login(viewer)
    assert client.post(reverse(URL), {"_form_submitted": "on", "scope": "core"}).status_code in (302, 403)
    assert started == []


def test_the_command_does_nothing_while_another_run_holds_the_lock(monkeypatch, capsys):
    monkeypatch.setattr(sync_etools_datamart.Command, "_lock", lambda self: False)
    call_command("sync_etools_datamart", "--only", "grants")
    assert "Another eTools Datamart sync is running" in capsys.readouterr().out
    assert not SyncRun.objects.exists()


def _other_session(*lock_ids):
    import psycopg
    from django.db import connection

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


def test_a_sync_started_during_the_etools_update_clean_up_waits_for_it(monkeypatch, capsys):
    """The clean-up holds the Datamart lock for minutes after a deployment; the nightly sync (or one
    started by hand) that finds it there runs after it instead of doing nothing."""
    cleanup = _other_session(background.DATAMART_BACKFILL_LOCK_ID, background.DATAMART_LOCK_ID)
    waits, synced = [], []

    def the_cleanup_finishes(seconds):
        waits.append(seconds)
        cleanup.close()

    monkeypatch.setattr(sync_etools_datamart.time, "sleep", the_cleanup_finishes)
    monkeypatch.setattr(
        sync_etools_datamart, "sync_all", lambda only, triggered_by: synced.append(only) or []
    )
    try:
        call_command("sync_etools_datamart", "--only", "grants")
    finally:
        cleanup.close()
    assert sync_etools_datamart.CLEANUP_RUNNING in capsys.readouterr().out
    assert waits == [sync_etools_datamart.POLL_SECONDS] and synced == [["grants"]]
    assert background.datamart_lock_is_held() is False  # released after the sync


def test_a_sync_started_during_another_sync_does_not_wait(monkeypatch, capsys):
    other = _other_session(background.DATAMART_LOCK_ID)

    def never(seconds):
        raise AssertionError("a sync does not wait for another sync")

    monkeypatch.setattr(sync_etools_datamart.time, "sleep", never)
    monkeypatch.setattr(sync_etools_datamart, "sync_all", lambda only, triggered_by: [][0])
    try:
        call_command("sync_etools_datamart", "--only", "grants")
    finally:
        other.close()
    assert sync_etools_datamart.ANOTHER_SYNC in capsys.readouterr().out


def test_a_sync_left_running_is_closed_while_the_clean_up_holds_the_lock():
    """The clean-up holding the Datamart lock is no sync: a run a restart cut off is closed as usual."""
    run = SyncRun.objects.create(job=SyncRun.Job.ETOOLS_DATAMART, target="partners")
    cleanup = _other_session(background.DATAMART_BACKFILL_LOCK_ID, background.DATAMART_LOCK_ID)
    try:
        assert background.datamart_lock_is_held() and background.datamart_cleanup_is_running()
        assert background.is_running(SyncRun.Job.ETOOLS_DATAMART) is False
    finally:
        cleanup.close()
    run.refresh_from_db()
    assert run.error == background.CUT_OFF
    sync = _other_session(background.DATAMART_LOCK_ID)  # a sync's session holds only the Datamart lock
    try:
        assert background.datamart_lock_is_held() and not background.datamart_cleanup_is_running()
    finally:
        sync.close()


def test_the_button_says_the_sync_waits_for_the_clean_up(admin_client, started, credentials, monkeypatch):
    monkeypatch.setattr(background, "datamart_cleanup_is_running", lambda: True)
    response = admin_client.post(reverse(URL), {"_form_submitted": "on", "scope": "core"}, follow=True)
    assert len(started) == 1 and "clean-up is running" in response.text


def test_start_command_runs_manage_py_in_its_own_session(monkeypatch, settings):
    seen = {}

    class FakePopen:
        pid = 42

        def __init__(self, args, **kwargs):
            seen.update(args=args, **kwargs)

    monkeypatch.setattr(background.subprocess, "Popen", FakePopen)
    assert background.start_command("sync_etools_datamart", "--only", "grants") == 42
    assert seen["args"][1:] == [
        str(settings.BASE_DIR / "manage.py"),
        "sync_etools_datamart",
        "--only",
        "grants",
    ]
    assert seen["start_new_session"] is True
