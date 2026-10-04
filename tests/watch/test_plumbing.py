"""NeuroDB Watch as a job: its schedule, its command, one run at a time, the loop guards and settings."""

import os
import re
import runpy
from pathlib import Path

import environ
import pytest
from django.contrib.auth.models import Group
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connections
from django.urls import reverse

from neurodb.accounts.models import User
from neurodb.accounts.roles import ALL_ROLES, MANAGEMENT, SECTION_EDITOR, VIEWER, ensure_groups, role_of
from neurodb.core import jobs
from neurodb.core.admin_jobs import BACKGROUND_JOBS
from neurodb.core.models import ScheduledJob, SyncRun
from neurodb.graph.models import RefreshRequest
from neurodb.integrations import background
from neurodb.integrations.runs import new_run
from neurodb.reports.services import data_health
from neurodb.watch import lock
from neurodb.watch.management.commands.run_watch import BUSY, SWITCHED_OFF
from neurodb.watch.models import WatchRequest
from neurodb.watch.services import NOTHING_WAITING

ROOT = Path(__file__).resolve().parents[2]
SETTINGS_FILE = ROOT / "config" / "settings.py"
NEW_SETTINGS = re.compile(r"^((?:WATCH|AI_PRICE|AI_DAILY)_[A-Z_]+) = ", re.M)


@pytest.fixture
def started(monkeypatch):
    calls = []
    monkeypatch.setattr(background, "start_command", lambda *args: calls.append(args) or 1)
    return calls


@pytest.fixture
def held_elsewhere(db):
    """Another process holds the watch's lock (a run in progress)."""
    other = connections.create_connection("default")
    with other.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_lock(%s)", [lock.LOCK_ID])
    yield
    other.close()  # ends its session: the lock goes with it


def _run(*args):
    from io import StringIO

    out = StringIO()
    call_command("run_watch", *args, stdout=out)
    return out.getvalue()


# ---------------------------------------------------------------------------- the job and its schedule
@pytest.mark.django_db
def test_the_watch_runs_every_morning_after_the_review_the_hub_and_whats_new():
    job = ScheduledJob.objects.get(key="watch")
    assert (job.command, job.schedule, job.enabled) == ("watch", "45 7 * * *", True)
    command = jobs.COMMANDS["watch"]
    assert command.args == ("run_watch", "--daily") and command.sync_job == SyncRun.Job.WATCH
    assert SyncRun.Job.WATCH.label == "NeuroDB Watch"
    assert background.LOCK_IDS[SyncRun.Job.WATCH] == lock.LOCK_ID == 7140431
    assert len(set(background.LOCK_IDS.values())) == len(background.LOCK_IDS)


@pytest.mark.django_db
def test_the_scheduler_starts_the_morning_pass(started):
    assert jobs.start("watch", "schedule") == "started"
    assert started == [("run_watch", "--daily", "--triggered-by", "schedule")]


@pytest.mark.django_db
def test_run_now_in_the_admin_starts_the_morning_pass(client, admin_user, started):
    assert "run_watch" in [spec.name for spec in BACKGROUND_JOBS]
    admin_user.is_superuser = True
    admin_user.save()
    client.force_login(admin_user)
    url = reverse("admin:core_syncrun_run_watch")
    assert "Run NeuroDB Watch now" in client.get(url).content.decode() and started == []
    assert client.post(url, {"_form_submitted": "on"}).status_code == 302
    assert started == [("run_watch", "--daily", "--triggered-by", admin_user.username)]


# ---------------------------------------------------------------------------- the command
@pytest.mark.django_db
def test_a_morning_pass_is_recorded_as_a_succeeded_run():
    out = _run("--daily", "--triggered-by", "schedule")
    run = SyncRun.objects.get(job=SyncRun.Job.WATCH)
    assert run.status == SyncRun.Status.SUCCEEDED and run.finished_at is not None
    assert run.target == "daily" and run.triggered_by == "schedule" and run.details["mode"] == "daily"
    assert "NeuroDB Watch [daily] succeeded" in out


@pytest.mark.django_db
def test_a_quick_pass_and_a_given_day(settings):
    settings.WATCH_SETTLE_SECONDS = 0
    ScheduledJob.objects.filter(command="watch").update(enabled=False)  # no morning pass to wait for
    assert _run("--when-requested").strip() == NOTHING_WAITING  # no new data asked for it
    WatchRequest.objects.create(reason="Knowledge hub")
    _run("--when-requested", "--date", "2026-10-05")
    run = SyncRun.objects.get(job=SyncRun.Job.WATCH)
    assert run.target == "quick" and run.details["mode"] == "quick" and run.details["date"] == "2026-10-05"
    assert run.triggered_by == "command: Knowledge hub" and not WatchRequest.objects.exists()
    with pytest.raises(CommandError, match="YYYY-MM-DD"):
        _run("--date", "5 Oct")
    with pytest.raises(CommandError):
        _run("--daily", "--when-requested")  # one or the other


@pytest.mark.django_db
def test_switched_off_it_checks_nothing_and_says_so(settings):
    settings.WATCH_ENABLED = False
    assert SWITCHED_OFF in _run("--daily")
    run = SyncRun.objects.get(job=SyncRun.Job.WATCH)
    assert run.status == SyncRun.Status.SUCCEEDED and run.details["note"] == SWITCHED_OFF


@pytest.mark.django_db
def test_a_second_start_while_one_runs_does_nothing(held_elsewhere):
    assert _run("--daily").strip() == BUSY  # exits without an error
    assert not SyncRun.objects.filter(job=SyncRun.Job.WATCH).exists()


@pytest.mark.django_db
def test_the_lock_is_free_again_after_a_run():
    _run("--daily")
    assert background.lock_is_held(lock.LOCK_ID) is False
    _run("--daily")
    assert SyncRun.objects.filter(job=SyncRun.Job.WATCH, status=SyncRun.Status.SUCCEEDED).count() == 2


@pytest.mark.django_db
def test_a_run_cut_off_by_a_restart_is_closed_and_one_in_progress_is_not(held_elsewhere):
    running = SyncRun.objects.create(job=SyncRun.Job.WATCH, status=SyncRun.Status.RUNNING)
    assert background.is_running(SyncRun.Job.WATCH)  # its process holds the lock
    running.refresh_from_db()
    assert running.status == SyncRun.Status.RUNNING


@pytest.mark.django_db
def test_a_run_left_running_without_its_process_is_closed():
    running = SyncRun.objects.create(job=SyncRun.Job.WATCH, status=SyncRun.Status.RUNNING)
    assert not background.is_running(SyncRun.Job.WATCH)
    running.refresh_from_db()
    assert running.status == SyncRun.Status.FAILED and running.error == background.CUT_OFF


# ---------------------------------------------------------------------------- loop guards
@pytest.mark.django_db
def test_a_finished_watch_run_asks_for_no_hub_rebuild(started, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        new_run(SyncRun.Job.WATCH, "daily", "test").finish(SyncRun.Status.SUCCEEDED)
        new_run(SyncRun.Job.WATCH, "quick", "test").finish(SyncRun.Status.PARTIAL)
    assert not RefreshRequest.objects.exists() and started == []


@pytest.mark.django_db
def test_data_health_lists_no_watch_row():
    new_run(SyncRun.Job.WATCH, "daily", "test").finish(SyncRun.Status.SUCCEEDED)
    assert SyncRun.Job.WATCH not in [row["job"] for row in data_health()["jobs"]]
    assert SyncRun.Job.ETOOLS_DATAMART in [row["job"] for row in data_health()["jobs"]]


# ---------------------------------------------------------------------------- the Management group
@pytest.mark.django_db
def test_management_is_a_group_without_permissions_and_not_a_role():
    groups = ensure_groups()
    assert list(groups) == list(ALL_ROLES) and MANAGEMENT not in ALL_ROLES
    management = Group.objects.get(name=MANAGEMENT)
    assert not management.permissions.exists()
    ensure_groups()  # every start: still one group
    assert Group.objects.filter(name=MANAGEMENT).count() == 1


@pytest.mark.django_db
def test_a_management_member_keeps_their_role_and_setting_a_role_keeps_management(client, roles):
    root = User.objects.create_superuser(username="root", email="root@example.org", password="root-pass-123")
    member = User.objects.create_user(username="deputy", email="deputy@example.org", password="x-123456789")
    member.groups.add(Group.objects.get(name=VIEWER), Group.objects.get(name=MANAGEMENT))
    assert role_of(member) == VIEWER
    client.force_login(root)
    response = client.post(
        reverse("admin:users_user_changelist"),
        {"action": "make_section_editor", "_selected_action": [member.pk]},
    )
    assert response.status_code == 302
    names = set(member.groups.values_list("name", flat=True))
    assert names == {SECTION_EDITOR, MANAGEMENT} and role_of(member) == SECTION_EDITOR
    only_management = User.objects.create_user(username="m", email="m@example.org", password="x-123456789")
    only_management.groups.add(Group.objects.get(name=MANAGEMENT))
    html = client.get(reverse("admin:users_user_changelist") + "?role=none").content.decode()
    assert "m@example.org" in html and "deputy@example.org" not in html  # still has no role


# ---------------------------------------------------------------------------- settings
def _load(monkeypatch, environ_dict=None, **env):
    monkeypatch.setattr(environ.Env, "read_env", classmethod(lambda cls, *args, **kwargs: None))
    if environ_dict is not None:
        monkeypatch.setattr(environ.Env, "ENVIRON", environ_dict)
    for var in list(os.environ):
        if NEW_SETTINGS.match(f"{var} = "):
            monkeypatch.delenv(var)
    for var, value in env.items():
        monkeypatch.setenv(var, value)
    return runpy.run_path(str(SETTINGS_FILE))


def test_the_watch_settings_and_their_defaults(monkeypatch):
    loaded = _load(monkeypatch)
    assert loaded["WATCH_ENABLED"] is True and loaded["WATCH_AI"] is True and loaded["WATCH_EMAIL"] is True
    assert loaded["WATCH_MODEL"] == loaded["AI_ASSISTANT_MODEL"]
    assert loaded["WATCH_DAILY_TOKEN_CAP"] == 300_000 and loaded["WATCH_MAX_MODEL_CALLS_PER_DAY"] == 24
    assert loaded["WATCH_INVESTIGATE_ENABLED"] is True and loaded["WATCH_INVESTIGATE_PER_DAY"] == 3
    assert loaded["WATCH_SETTLE_SECONDS"] == 600 and loaded["WATCH_QUICK_PASSES_PER_DAY"] == 6
    assert loaded["WATCH_TIME_LIMIT_SECONDS"] == 900
    assert loaded["WATCH_NEEDS_YOU_PER_DAY"] == 5 and loaded["WATCH_GOOD_TO_KNOW_PER_DAY"] == 10
    assert loaded["WATCH_GRANT_MIN_UNSPENT"] == 10_000
    assert loaded["AI_DAILY_TOKEN_SOFT_CAP"] == 3_000_000
    assert loaded["AI_PRICE_INPUT_PER_MTOK"] is None and loaded["AI_PRICE_OUTPUT_PER_MTOK"] is None
    assert "neurodb.watch" in loaded["INSTALLED_APPS"]
    assert (
        loaded["INSTALLED_APPS"].index("neurodb.watch")
        == loaded["INSTALLED_APPS"].index("neurodb.insights") + 1
    )

    changed = _load(
        monkeypatch,
        WATCH_ENABLED="false",
        WATCH_MODEL=" other-model ",
        WATCH_INVESTIGATE_ENABLED="off",
        AI_PRICE_INPUT_PER_MTOK="1.25",
        AI_PRICE_CACHED_PER_MTOK=" ",
    )
    assert changed["WATCH_ENABLED"] is False and changed["WATCH_MODEL"] == "other-model"
    assert changed["WATCH_INVESTIGATE_ENABLED"] is False
    assert changed["AI_PRICE_INPUT_PER_MTOK"] == 1.25 and changed["AI_PRICE_CACHED_PER_MTOK"] is None


def test_the_example_env_file_lists_every_new_setting_and_loads(monkeypatch):
    names = NEW_SETTINGS.findall(SETTINGS_FILE.read_text(encoding="utf-8"))
    example_text = (ROOT / ".env.example").read_text(encoding="utf-8")
    listed = {line.split("=", 1)[0] for line in example_text.splitlines() if "=" in line and line[0] != "#"}
    assert names and not set(names) - listed
    example = {}
    monkeypatch.setattr(environ.Env, "ENVIRON", example)
    environ.Env.read_env(str(ROOT / ".env.example"), overwrite=True)
    loaded = _load(monkeypatch, example)
    assert (
        loaded["WATCH_DAILY_TOKEN_CAP"] == 300_000 and loaded["WATCH_MODEL"] == loaded["AI_ASSISTANT_MODEL"]
    )
    assert loaded["AI_PRICE_INPUT_PER_MTOK"] is None and loaded["WATCH_INVESTIGATE_PER_DAY"] == 3
