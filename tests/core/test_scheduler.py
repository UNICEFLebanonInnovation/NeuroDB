"""Scheduled jobs: cron parsing, the scheduler's pass, and the admin page."""

import datetime
from zoneinfo import ZoneInfo

import pytest
from django.urls import reverse
from django.utils import timezone

from neurodb.core import cron, scheduler
from neurodb.core.models import ScheduledJob, SchedulerState, SyncRun
from neurodb.integrations import background

BEIRUT = ZoneInfo("Asia/Beirut")


def at(*args):
    return datetime.datetime(*args, tzinfo=BEIRUT)


def test_cron_next_times_in_beirut_time():
    assert cron.next_after("30 20 * * *", at(2026, 9, 28, 20, 29)) == at(2026, 9, 28, 20, 30)
    assert cron.next_after("30 20 * * *", at(2026, 9, 28, 20, 30)) == at(2026, 9, 29, 20, 30)
    # days 1 to 22 only
    assert cron.next_after("0 18 1-22 * *", at(2026, 9, 22, 18, 0)) == at(2026, 10, 1, 18, 0)
    assert cron.next_after("15 * * * *", at(2026, 9, 28, 10, 20)) == at(2026, 9, 28, 11, 15)
    assert cron.next_after("*/15 * * * *", at(2026, 9, 28, 10, 1)) == at(2026, 9, 28, 10, 15)
    # Sundays (0 and 7 both mean Sunday); 2026-10-04 is a Sunday
    assert cron.next_after("0 4 * * 0", at(2026, 9, 28, 0, 0)) == at(2026, 10, 4, 4, 0)
    assert cron.next_after("0 4 * * 7", at(2026, 9, 28, 0, 0)) == at(2026, 10, 4, 4, 0)
    # day of month and day of week both set: either one (cron rule)
    assert cron.next_after("0 9 1 * 1", at(2026, 9, 28, 10, 0)) == at(2026, 10, 1, 9, 0)
    # the same local time across the change to winter time (25 Oct 2026 in Beirut)
    after = cron.next_after("30 20 * * *", at(2026, 10, 25, 21, 0))
    assert after.astimezone(BEIRUT).hour == 20 and after.utcoffset() == datetime.timedelta(hours=2)


@pytest.mark.parametrize(
    "bad", ["", "* * * *", "60 * * * *", "* 24 * * *", "a * * * *", "5-1 * * * *", "0 0 30 2 *"]
)
def test_cron_rejects_bad_schedules(bad):
    with pytest.raises(cron.CronError):
        cron.next_after(bad, at(2026, 1, 1, 0, 0))


def test_cron_descriptions():
    assert cron.describe("30 20 * * *") == "daily at 20:30"
    assert cron.describe("15 * * * *") == "every hour at :15"
    assert cron.describe("0 18 1-22 * *") == "at 18:00 on days 1-22 of the month"
    assert cron.describe("0 4 * * 0") == "Sundays at 04:00"
    assert cron.describe("*/5 1 * * *") == "*/5 1 * * *"


@pytest.fixture
def started(monkeypatch):
    calls = []
    monkeypatch.setattr(background, "start_command", lambda *args: calls.append(args) or 1)
    return calls


@pytest.mark.django_db
def test_default_schedules_exist():
    jobs = dict(ScheduledJob.objects.values_list("key", "schedule"))
    assert jobs["etools-datamart"] == "30 20 * * *" and jobs["daily-review"] == "0 6 * * *"
    assert jobs["activityinfo-data"] == "0 18 1-22 * *" and jobs["freshness"] == "15 * * * *"
    assert jobs["whats-new"] == "30 7 * * *" and jobs["watch"] == "45 7 * * *"  # the watch after the note
    assert not ScheduledJob.objects.get(key="activityinfo-structure").enabled


@pytest.mark.django_db
def test_a_pass_plans_then_starts_due_jobs_once(started):
    ScheduledJob.objects.exclude(key="locations").delete()
    job = ScheduledJob.objects.get(key="locations")  # 0 5 * * *
    now = at(2026, 9, 28, 4, 0)
    assert scheduler.tick(now) == [] and started == []  # first pass only plans
    job.refresh_from_db()
    assert job.next_run_at == at(2026, 9, 28, 5, 0)

    assert scheduler.tick(at(2026, 9, 28, 4, 59)) == []
    # the container was down 05:00-07:00: one catch-up run, then tomorrow
    assert scheduler.tick(at(2026, 9, 28, 7, 0)) == ["locations"]
    assert started == [("sync_locations", "--triggered-by", "schedule")]
    job.refresh_from_db()
    assert job.next_run_at == at(2026, 9, 29, 5, 0) and job.last_outcome == "started"
    assert scheduler.tick(at(2026, 9, 28, 7, 1)) == []
    assert SchedulerState.objects.get(pk=1).last_seen_at == at(2026, 9, 28, 7, 1)


@pytest.mark.django_db
def test_a_job_still_running_is_skipped(started, monkeypatch):
    monkeypatch.setattr(background, "lock_is_held", lambda lock_id: None)
    ScheduledJob.objects.exclude(key="locations").delete()
    ScheduledJob.objects.filter(key="locations").update(next_run_at=at(2026, 9, 28, 5, 0))
    SyncRun.objects.create(job=SyncRun.Job.LOCATIONS, status=SyncRun.Status.RUNNING)
    assert scheduler.tick(timezone.now()) == [] and started == []
    assert "still going" in ScheduledJob.objects.get(key="locations").last_outcome


@pytest.mark.django_db
def test_freshness_runs_without_triggered_by_and_disabled_jobs_never_run(started):
    ScheduledJob.objects.update(next_run_at=at(2026, 9, 28, 0, 0))
    ScheduledJob.objects.exclude(key__in=["freshness", "activityinfo-structure"]).delete()
    assert scheduler.tick(at(2026, 9, 28, 1, 0)) == ["freshness"]
    assert started == [("check_sync_freshness",)]


@pytest.fixture
def superadmin(client, admin_user):
    admin_user.is_superuser = True
    admin_user.save()
    client.force_login(admin_user)
    return client


@pytest.mark.django_db
def test_admin_page_lists_jobs_and_scheduler_status(superadmin):
    html = superadmin.get(reverse("admin:core_scheduledjob_changelist")).content.decode()
    assert "daily at 20:30" in html and "Sync eTools (Datamart)" in html
    assert "has not checked in recently" in html
    SchedulerState.objects.create(pk=1, last_seen_at=timezone.now(), host="web-1 (pid 7)")
    html = superadmin.get(reverse("admin:core_scheduledjob_changelist")).content.decode()
    assert "The scheduler is running" in html and "web-1 (pid 7)" in html


@pytest.mark.django_db
def test_admin_edits_a_schedule_and_it_is_replanned(superadmin, admin_user):
    job = ScheduledJob.objects.get(key="etools-datamart")
    url = reverse("admin:core_scheduledjob_change", args=[job.pk])
    data = {"key": job.key, "command": job.command, "schedule": "0 22 * * *", "enabled": "on"}
    assert superadmin.post(url, data).status_code == 302
    job.refresh_from_db()
    assert job.schedule == "0 22 * * *" and job.updated_by == admin_user.username
    assert job.next_run_at.astimezone(BEIRUT).hour == 22

    bad = superadmin.post(url, {**data, "schedule": "every night"})
    assert bad.status_code == 200 and "five fields" in bad.content.decode()
    shell = superadmin.post(url, {**data, "command": "rm -rf /"})  # only the listed commands
    assert shell.status_code == 200 and ScheduledJob.objects.get(pk=job.pk).command == "etools_datamart"


@pytest.mark.django_db
def test_run_now_and_viewers(superadmin, client, viewer, started):
    job = ScheduledJob.objects.get(key="daily-review")
    url = reverse("admin:core_scheduledjob_run_now", args=[job.pk])
    assert superadmin.get(url).status_code == 200 and started == []  # confirmation first
    assert superadmin.post(url, {"_form_submitted": "on"}).status_code == 302
    assert started == [("daily_review", "--triggered-by", "admin")]

    viewer.is_staff = True
    viewer.save()
    client.force_login(viewer)
    client.post(url, {"_form_submitted": "on"})
    assert len(started) == 1


@pytest.mark.django_db
def test_overdue_jobs_are_marked_and_the_stalled_banner_says_what_to_do(superadmin, settings):
    settings.SUPPORT_EMAIL = "support@example.org"
    ScheduledJob.objects.filter(key="etools-datamart").update(
        next_run_at=timezone.now() - datetime.timedelta(hours=3)
    )
    html = superadmin.get(reverse("admin:core_scheduledjob_changelist")).content.decode()
    assert "Overdue" in html and "Never run" in html
    assert "Run due jobs now" in html and "support@example.org" in html
    assert "check its log" not in html and "Last outcome" not in html
    assert "saves as soon as you click it" in html


@pytest.mark.django_db
def test_run_due_jobs_now_runs_one_pass_and_keeps_the_banner_truthful(superadmin, client, viewer, started):
    ScheduledJob.objects.exclude(key="locations").delete()
    ScheduledJob.objects.filter(key="locations").update(
        next_run_at=timezone.now() - datetime.timedelta(hours=1)
    )
    seen = timezone.now() - datetime.timedelta(hours=15)
    SchedulerState.objects.create(pk=1, last_seen_at=seen, host="web-1 (pid 7)")
    url = reverse("admin:core_scheduledjob_run_due_jobs")

    assert superadmin.get(url).status_code == 200 and started == []  # confirmation first
    response = superadmin.post(url, {"_form_submitted": "on"}, follow=True)
    assert started == [("sync_locations", "--triggered-by", "schedule")]
    assert "Started: locations" in response.content.decode()
    assert SchedulerState.objects.get(pk=1).last_seen_at == seen  # still reported as stalled

    superadmin.post(url, {"_form_submitted": "on"})
    assert len(started) == 1  # planned for tomorrow now: nothing due

    viewer.is_staff = True
    viewer.save()
    client.force_login(viewer)
    ScheduledJob.objects.update(next_run_at=timezone.now() - datetime.timedelta(hours=1))
    client.post(url, {"_form_submitted": "on"})
    assert len(started) == 1
