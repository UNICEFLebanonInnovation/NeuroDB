"""Admin portal: grouped dashboard home, list filters, role actions and the audit trail."""

import datetime

import pytest
from django.contrib.admin.models import CHANGE, LogEntry
from django.contrib.contenttypes.models import ContentType
from django.urls import reverse
from django.utils import timezone

from neurodb.accounts.models import User
from neurodb.accounts.roles import ADMIN, SECTION_EDITOR, VIEWER
from neurodb.core.models import SyncRun
from neurodb.indicators.models import Database


@pytest.fixture
def superuser(db, roles):
    return User.objects.create_superuser(
        username="root", email="root@example.org", password="root-pass-123456"
    )


@pytest.fixture
def client_super(client, superuser):
    client.force_login(superuser)
    return client


def test_dashboard_shows_groups_tiles_and_warnings(client_super, hierarchy):
    database = hierarchy["database"]
    database.display = True
    database.save()
    SyncRun.objects.create(job=SyncRun.Job.ETOOLS, status=SyncRun.Status.FAILED, error="boom")
    User.objects.create_user(username="norole", password="x-pass-123456")

    response = client_super.get(reverse("admin:index"))

    assert response.status_code == 200
    html = response.content.decode()
    for group in ("Reporting setup", "Users and access", "Data and sync", "Partnerships (eTools, read-only)"):
        assert group in html
    assert 'id="group-reporting-setup"' in html
    assert "Active master indicators" in html
    assert "1 displayed database was never imported." in html
    assert "?freshness=never" in html
    assert "1 active user has no role" in html
    assert "The last eTools sync failed." in html
    assert "Quick actions" in html


def test_dashboard_survives_a_database_error(client_super, monkeypatch):
    from django.db import DatabaseError

    from neurodb.web import admin_site

    def broken(request):
        raise DatabaseError("down")

    monkeypatch.setattr(admin_site, "_dashboard", broken)
    response = client_super.get(reverse("admin:index"))
    assert response.status_code == 200
    assert "dashboard figures could not be loaded" in response.content.decode()


def test_sidebar_uses_the_same_groups(client_super):
    response = client_super.get(reverse("admin:users_user_changelist"))
    assert response.status_code == 200
    assert "Users and access" in response.content.decode()


@pytest.mark.parametrize("value", ["never", "stale", "fresh"])
def test_database_freshness_filter(client_super, database, value):
    old = Database.objects.create(
        ai_id=1,
        db_id="old",
        name="Old",
        label="Old db",
        username="",
        password="",
        reporting_year=database.reporting_year,
    )
    Database.objects.filter(pk=old.pk).update(
        last_monthly_update_date=timezone.now() - datetime.timedelta(days=90)
    )
    new = Database.objects.create(
        ai_id=2,
        db_id="new",
        name="New",
        label="New db",
        username="",
        password="",
        reporting_year=database.reporting_year,
    )
    Database.objects.filter(pk=new.pk).update(last_monthly_update_date=timezone.now())

    response = client_super.get(reverse("admin:pivoting_database_changelist") + f"?freshness={value}")

    assert response.status_code == 200  # an unknown filter parameter would redirect to ?e=1
    shown = {obj.pk for obj in response.context["cl"].result_list}
    expected = {"never": {database.pk}, "stale": {old.pk}, "fresh": {new.pk}}[value]
    assert shown == expected


def test_database_list_has_counts_and_dashboard_link(client_super, hierarchy):
    database = hierarchy["database"]
    response = client_super.get(reverse("admin:pivoting_database_changelist"))
    html = response.content.decode()
    assert reverse("reports:database_dashboard", args=[database.pk]) in html
    assert response.context["cl"].result_list[0].active_masters == 2
    assert "nd-badge" in html


def test_master_indicator_target_filter(client_super, hierarchy):
    master = hierarchy["master"]
    master.awp_target = 0
    master.save()
    url = reverse("admin:pivoting_masterindicator_changelist")

    no_target = client_super.get(url + "?has_target=no&is_active__exact=1")
    with_target = client_super.get(url + "?has_target=yes")

    assert {m.pk for m in no_target.context["cl"].result_list} == {master.pk}
    assert {m.pk for m in with_target.context["cl"].result_list} == {hierarchy["ratio"].pk}


def test_role_filter_and_column(client_super, viewer, admin_user):
    plain = User.objects.create_user(username="plain", password="x-pass-123456")
    url = reverse("admin:users_user_changelist")

    none = client_super.get(url + "?role=none")
    admins = client_super.get(url + "?role=admin")

    assert {u.pk for u in none.context["cl"].result_list} == {plain.pk}
    assert {u.username for u in admins.context["cl"].result_list} == {"admin", "root"}
    assert "None (viewer)" in none.content.decode()


def test_set_role_actions_are_exclusive(client_super, viewer):
    url = reverse("admin:users_user_changelist")

    response = client_super.post(url, {"action": "make_section_editor", "_selected_action": [viewer.pk]})
    assert response.status_code == 302
    assert set(viewer.groups.values_list("name", flat=True)) == {SECTION_EDITOR}

    client_super.post(url, {"action": "make_administrator", "_selected_action": [viewer.pk]})
    assert set(viewer.groups.values_list("name", flat=True)) == {ADMIN}

    client_super.post(url, {"action": "make_viewer", "_selected_action": [viewer.pk]})
    assert set(viewer.groups.values_list("name", flat=True)) == {VIEWER}


def test_deactivate_never_locks_yourself_out(client_super, superuser, viewer):
    url = reverse("admin:users_user_changelist")
    client_super.post(url, {"action": "deactivate", "_selected_action": [viewer.pk, superuser.pk]})
    viewer.refresh_from_db()
    superuser.refresh_from_db()
    assert not viewer.is_active
    assert superuser.is_active


def test_sync_runs_and_audit_trail_are_read_only(client_super, superuser, viewer):
    run = SyncRun.objects.create(
        job=SyncRun.Job.ACTIVITYINFO_DATA, target="202618", status=SyncRun.Status.SUCCEEDED
    )
    LogEntry.objects.create(
        user=superuser,
        content_type=ContentType.objects.get_for_model(User),
        object_id=str(viewer.pk),
        object_repr=str(viewer),
        action_flag=CHANGE,
        change_message="Changed email.",
    )

    runs = client_super.get(reverse("admin:core_syncrun_changelist"))
    audit = client_super.get(reverse("admin:admin_logentry_changelist"))

    assert runs.status_code == 200
    assert "Succeeded" in runs.content.decode()
    assert audit.status_code == 200
    assert "Changed email." in audit.content.decode()
    assert client_super.get(reverse("admin:core_syncrun_change", args=[run.pk])).status_code == 200
    assert client_super.get(reverse("admin:core_syncrun_add")).status_code == 403
