"""eTools reporting checks as a job: ``reporting_checks`` runs them with the job's time (ten minutes a
query, an hour in all) and stores the result; the page shows the stored result at once, or runs them
itself while none is stored and none runs, and its button starts the job in the background; the
schedule, *Run a job* and the job lists know it."""

from __future__ import annotations

import datetime
import time
import types
from io import StringIO

import pytest
from django.core.management import call_command
from django.db import DatabaseError
from django.urls import reverse
from django.utils import dateformat, timezone

from neurodb.core import jobs
from neurodb.core.admin_jobs import BACKGROUND_JOBS
from neurodb.core.models import ScheduledJob, SyncRun
from neurodb.datamart import checks
from neurodb.graph import refresh
from neurodb.integrations import background
from neurodb.reports import services

URL = "admin:datamart_reporting_checks"
JOB = SyncRun.Job.REPORTING_CHECKS


def _ok(cursor):
    return {"tone": checks.OK, "verdict": "Fine.", "figures": [("Rows", 3)]}


def _card(key):
    return {
        "key": key,
        "title": "t",
        "question": "q",
        "neurodb": "n",
        "tone": "ok",
        "verdict": "v",
        "figures": [],
    }


def _never(*args, **kwargs):
    pytest.fail("the page ran the checks itself")


@pytest.fixture
def started(monkeypatch):
    """The commands started in the background: ``background.start_command`` runs (it notes the start),
    with no process."""
    calls = []

    def popen(argv, **kwargs):
        calls.append(tuple(argv[2:]))  # after the interpreter and manage.py
        return types.SimpleNamespace(pid=1)

    monkeypatch.setattr(background.subprocess, "Popen", popen)
    return calls


def test_the_job_runs_every_check_and_stores_the_result(db):
    run = checks.run_job("test")
    assert run.status == SyncRun.Status.SUCCEEDED and run.triggered_by == "test" and run.finished_at
    stored = run.details["checks"]
    assert [c["key"] for c in stored] == [str(n) for n in range(1, 13)]
    assert run.rows_written == 12 and run.rows_failed == 0 and run.details["unfinished"] == []
    assert all(c["verdict"] and c["question"] and c["neurodb"] for c in stored)
    assert checks.latest_run() == run
    assert background.lock_is_held(checks.LOCK_ID) is False  # released


def test_the_job_gives_each_query_ten_minutes_and_the_page_twenty_seconds(db, monkeypatch):
    seen = []

    def probe(cursor):
        cursor.execute("SHOW statement_timeout")
        seen.append(
            (
                cursor.timeout_ms,
                cursor.fetchone()[0],
                cursor.remaining(),
                background.lock_is_held(checks.LOCK_ID),
            )
        )
        return _ok(cursor)

    monkeypatch.setattr(checks, "CHECKS", (checks.Check("1", "t", "q", "n", probe),))
    checks.run_job("test")
    checks.run_all()
    (job_ms, job_set, job_left, job_lock), (page_ms, page_set, page_left, page_lock) = seen
    assert (job_ms, job_set, job_lock) == (600_000, "10min", True) and job_left > 3500
    assert (page_ms, page_set, page_lock) == (20_000, "20s", False) and page_left <= 75


def test_a_check_that_does_not_finish_makes_the_run_partial_and_says_why(db, monkeypatch):
    def slow(cursor):
        cursor.execute("SELECT pg_sleep(3)")
        return _ok(cursor)

    def unreadable(cursor):
        raise DatabaseError("invalid input syntax")

    monkeypatch.setattr(checks, "JOB_TIMEOUT_MS", 1000)  # a query's own limit: 1 second
    monkeypatch.setattr(
        checks,
        "CHECKS",
        (
            checks.Check("1", "t", "q", "n", slow),
            checks.Check("2", "t", "q", "n", unreadable),
            checks.Check("3", "t", "q", "n", _ok),
        ),
    )
    run = checks.run_job("test")
    assert run.status == SyncRun.Status.PARTIAL and run.details["unfinished"] == ["1", "2"]
    assert run.rows_written == 1 and run.rows_failed == 2
    first, second, third = run.details["checks"]
    assert run.details["errors"] == [  # shown under "Why rows failed" on the run's page
        {"error": first["verdict"], "count": 1, "examples": ["check 1"]},
        {"error": second["verdict"], "count": 1, "examples": ["check 2"]},
    ]
    assert first["verdict"] == "This check could not finish (a query took longer than its 1 second)."
    assert first["tone"] == checks.WARN
    assert second["verdict"] == "This check could not finish (the data could not be read)."
    assert third["figures"] == [["Rows", 3]] and "unfinished" not in third
    assert checks._duration(600_000) == "10 minutes" and checks._duration(20_000) == "20 seconds"


def test_a_query_the_time_left_stopped_says_the_time_ran_out_not_that_it_was_slow(db, monkeypatch):
    def slow(cursor):
        cursor.execute("SELECT pg_sleep(4)")
        return _ok(cursor)

    monkeypatch.setattr(checks, "CHECKS", (checks.Check("1", "t", "q", "n", slow),))
    (found,) = checks.run_all(budget=2.0)  # 2 seconds left: the query gets them, not its 20 seconds
    assert "ran out of the page's 2 seconds partway through" in found["verdict"]
    assert found["tone"] == checks.INFO and found["unfinished"]


def test_a_check_left_out_by_the_time_says_whose_time(db):
    page = checks._not_run(["10", "11"])["verdict"]
    assert "checks 10 and 11, run before it, used up the page's 75 seconds" in page
    assert "the page stops there" in page
    job = checks._not_run(["10"], started=True, limit="the job's 60 minutes", by="job")["verdict"]
    assert "this check ran out of the job's 60 minutes partway through" in job
    assert "page" not in job


def test_a_run_while_another_holds_the_lock_records_nothing(db, monkeypatch):
    monkeypatch.setattr(checks, "_lock", lambda: False)
    assert checks.run_job("test") == checks.BUSY
    assert not SyncRun.objects.filter(job=JOB).exists()
    out = StringIO()
    call_command("reporting_checks", stdout=out)
    assert checks.BUSY in out.getvalue()


def test_a_failed_run_keeps_the_last_result_and_the_page_names_it(client, admin_user, monkeypatch):
    good = checks.run_job("test")

    def boom(*args, **kwargs):
        raise RuntimeError("no database")

    monkeypatch.setattr(checks, "run_all", boom)
    bad = checks.run_job("test")
    assert bad.status == SyncRun.Status.FAILED and "no database" in bad.error
    assert checks.latest_run() == good
    client.force_login(admin_user)
    page = client.get(reverse(URL)).text
    assert reverse("admin:core_syncrun_change", args=[bad.pk]) in page
    when = dateformat.format(timezone.localtime(good.finished_at), "j M Y, H:i")
    assert f"The cards below are those of the last run that stored a result ({when})." in page
    assert "Check 11 ·" in page


def test_the_command_prints_each_check_and_the_run(db):
    out = StringIO()
    call_command("reporting_checks", "--triggered-by", "test", stdout=out)
    text = out.getvalue()
    assert "eTools reporting checks [] succeeded: in=0 written=12 failed=0" in text
    assert " 12 " in text and SyncRun.objects.get(job=JOB).triggered_by == "test"


def test_the_page_shows_the_stored_result_without_running_the_checks(client, admin_user, monkeypatch):
    run = checks.run_job("test")
    run.details["checks"][0]["verdict"] = "The stored verdict of check one."
    run.save(update_fields=["details"])
    monkeypatch.setattr(checks, "run_all", _never)
    client.force_login(admin_user)
    page = client.get(reverse(URL)).text
    assert "The stored verdict of check one." in page and "Check 11 ·" in page
    assert "by the reporting checks job, with up to 60 minutes" in page
    assert "Run the checks now" in page and 'name="csrfmiddlewaretoken"' in page
    assert "The button runs it again now, in the background." in page


def test_with_nothing_stored_the_page_runs_the_checks_itself_and_says_so(client, admin_user):
    client.force_login(admin_user)
    page = client.get(reverse(URL)).text
    assert "Check 1 ·" in page and "Check 11 ·" in page
    assert "Checked on this page just now" in page and "with 75 seconds for all the checks" in page


def test_while_the_job_runs_the_page_says_so_and_never_runs_the_checks(client, admin_user, monkeypatch):
    SyncRun.objects.create(job=JOB, triggered_by="schedule")  # running
    monkeypatch.setattr(background, "lock_is_held", lambda lock_id: True)
    monkeypatch.setattr(checks, "run_all", _never)
    client.force_login(admin_user)
    page = client.get(reverse(URL)).text
    assert "The checks are running in the background" in page and "Run the checks now" not in page
    assert "No result is stored yet: a run of the checks is under way." in page and "Check 1 ·" not in page
    card = {
        "key": "11",
        "title": "t",
        "question": "q",
        "neurodb": "n",
        "tone": "ok",
        "verdict": "v",
        "figures": [],
    }
    SyncRun.objects.create(  # an earlier result: shown, without offering the button while one runs
        job=JOB, status=SyncRun.Status.SUCCEEDED, finished_at=timezone.now(), details={"checks": [card]}
    )
    page = client.get(reverse(URL)).text
    assert "by the reporting checks job" in page and "Check 11 ·" in page
    assert "The button runs it again now" not in page and "The checks are running" in page


def test_a_row_left_running_by_a_stopped_process_is_closed_and_the_button_offered(client, admin_user):
    left = SyncRun.objects.create(job=JOB, triggered_by="schedule")  # no process holds the lock
    client.force_login(admin_user)
    page = client.get(reverse(URL)).text
    assert "Run the checks now" in page and "The checks are running" not in page
    left.refresh_from_db()
    assert left.status == SyncRun.Status.FAILED and left.error == background.CUT_OFF


def test_the_button_starts_the_job_in_the_background_once(client, admin_user, started, monkeypatch):
    monkeypatch.setattr(checks, "run_all", _never)  # nothing stored: the page that follows waits for the job
    client.force_login(admin_user)
    answer = client.post(reverse(URL), follow=True)
    assert answer.redirect_chain == [(reverse(URL), 302)]
    assert started == [("reporting_checks", "--triggered-by", admin_user.username)]
    assert "started in the background" in answer.text
    assert (
        "The checks are starting in the background" in answer.text and "Run the checks now" not in answer.text
    )
    assert "No result is stored yet: a run of the checks is under way." in answer.text
    SyncRun.objects.create(job=JOB, triggered_by=admin_user.username)  # its run, now going
    monkeypatch.setattr(background, "lock_is_held", lambda lock_id: True)
    answer = client.post(reverse(URL), follow=True)
    assert len(started) == 1 and "already running" in answer.text


def test_only_administrators_see_or_start_the_checks(client, viewer, started):
    viewer.is_staff = True
    viewer.save()
    client.force_login(viewer)
    assert client.post(reverse(URL)).status_code in (302, 404)
    assert client.get(reverse(URL)).status_code in (302, 404)
    assert started == []


def test_the_schedule_run_a_job_and_the_job_lists_know_the_checks(db, monkeypatch):
    job = ScheduledJob.objects.get(key="reporting-checks")
    assert (job.command, job.schedule, job.enabled) == ("reporting_checks", "15 2 * * *", True)
    command = jobs.COMMANDS["reporting_checks"]
    assert command.args == ("reporting_checks",) and command.sync_job == JOB and command.triggered_by
    (spec,) = [s for s in BACKGROUND_JOBS if s.name == "run_reporting_checks"]
    assert spec.command == ("reporting_checks",) and spec.job == JOB
    assert background.LOCK_IDS[JOB] == checks.LOCK_ID
    assert JOB in services.HEALTH_SKIPPED_JOBS  # a check, not a sync that goes stale
    asked = []
    monkeypatch.setattr(refresh, "request", lambda *args: asked.append(args))
    checks.run_job("test")
    assert asked == []  # counts only: no knowledge hub rebuild after it


def test_the_daily_review_counts_a_failed_run_of_the_checks_not_a_check_that_did_not_finish(db):
    from neurodb.core import years
    from neurodb.review import checks as review

    SyncRun.objects.create(job=JOB, status=SyncRun.Status.PARTIAL, finished_at=timezone.now())
    assert review.check_sync_failures(review.Context(today=years.today())) == []
    SyncRun.objects.create(job=JOB, status=SyncRun.Status.FAILED, finished_at=timezone.now(), error="boom")
    (draft,) = review.check_sync_failures(review.Context(today=years.today()))
    assert draft.key == f"sync_failures:{JOB}"


def test_a_run_started_counts_as_starting_for_everyone_until_its_run_appears_or_two_minutes_pass(
    client, admin_user, started, monkeypatch, django_user_model
):
    from django.core.cache import caches

    from neurodb.core import compute
    from neurodb.datamart import admin as dm_admin

    monkeypatch.setattr(checks, "run_all", _never)
    client.force_login(admin_user)
    client.post(reverse(URL))
    assert "The checks are starting" in client.get(reverse(URL)).text  # its process is not up yet
    other = django_user_model.objects.create_user(
        username="admin-two", password="x-pass-123456", is_staff=True
    )
    other.groups.set(admin_user.groups.all())
    second = type(client)()
    second.force_login(other)
    assert "The checks are starting" in second.get(reverse(URL)).text  # another administrator too
    # a start from Run a job or the schedule is noted the same way (both go through start_command)
    jobs.start("reporting_checks", triggered_by="schedule")
    assert started[-1] == ("reporting_checks", "--triggered-by", "schedule")
    # its row appears: the page follows the run, and the note goes
    SyncRun.objects.create(job=JOB, triggered_by="schedule")
    with monkeypatch.context() as held:
        held.setattr(background, "lock_is_held", lambda lock_id: True)
        assert "The checks are running in the background" in client.get(reverse(URL)).text
    assert background.started_at("reporting_checks") is None
    # it ended (here, cut off: no process holds the lock) and a stored result shows
    run = SyncRun.objects.create(job=JOB, status=SyncRun.Status.SUCCEEDED, finished_at=timezone.now())
    run.details = {"checks": [_card("1")]}
    run.save(update_fields=["details"])
    page = client.get(reverse(URL)).text
    assert "The checks are starting" not in page and "Run the checks now" in page
    # a start noted long ago whose process never came: the page runs the checks itself again
    SyncRun.objects.filter(job=JOB).delete()
    caches[compute.CACHE].set(
        background.STARTED_KEY.format("reporting_checks"), time.time() - dm_admin.STARTING_S - 1, 300
    )
    monkeypatch.setattr(checks, "run_all", lambda *a, **k: [_card("1")])
    page = client.get(reverse(URL)).text
    assert "The checks are starting" not in page and "Checked on this page just now" in page


def test_a_run_whose_database_connection_was_lost_stops_and_stores_nothing(db, monkeypatch):
    def loses_the_lock(cursor):
        cursor.execute("SELECT pg_advisory_unlock(%s)", [checks.LOCK_ID])  # as a dropped session would
        return _ok(cursor)

    monkeypatch.setattr(
        checks,
        "CHECKS",
        (checks.Check("10", "t", "q", "n", loses_the_lock), checks.Check("1", "t", "q", "n", _never)),
    )
    run = checks.run_job("test")
    assert run.status == SyncRun.Status.FAILED and "connection was lost" in run.error
    assert "checks" not in run.details and checks.latest_run() is None


def test_check_10_dates_read_the_same_on_the_page_and_in_the_stored_result(db):
    from django.utils import translation

    from neurodb.datamart import models as dm

    end = datetime.date(2026, 3, 31)
    dm.ReportedIndicator.objects.create(
        datamart_id=1,
        progress_report="PR-1",
        report_type="QPR",
        report_status="Accepted",
        period_start=end - datetime.timedelta(days=90),
        period_end=end,
        indicator="x",
        location="Akkar",
    )
    with translation.override("fr"):
        (page,) = [c for c in checks.run_all() if c["key"] == "10"]
    (stored,) = [c for c in checks.run_job("test").details["checks"] if c["key"] == "10"]
    assert page["table"]["rows"][0][4:6] == ("31 Mar 2026", "31 Mar 2026")
    assert stored["table"]["rows"][0][4:6] == ["31 Mar 2026", "31 Mar 2026"]
    assert checks._day(None) is None


def test_a_check_that_did_not_finish_raises_no_alarm_on_the_admin_home_a_failed_run_does(db):
    from neurodb.web import health

    def keys():
        return {line.key for line in health.warnings()}

    SyncRun.objects.create(job=JOB, status=SyncRun.Status.PARTIAL, finished_at=timezone.now(), rows_failed=2)
    assert not {k for k in keys() if k.endswith(f":{JOB}")}
    SyncRun.objects.create(job=JOB, status=SyncRun.Status.FAILED, finished_at=timezone.now(), error="boom")
    assert f"job_failed:{JOB}" in keys()


def test_with_a_failed_run_and_nothing_stored_the_page_never_says_first_run(client, admin_user, monkeypatch):
    SyncRun.objects.create(job=JOB, status=SyncRun.Status.FAILED, finished_at=timezone.now(), error="cut off")
    SyncRun.objects.create(job=JOB, triggered_by="schedule")  # the next run, going
    monkeypatch.setattr(background, "lock_is_held", lambda lock_id: True)
    monkeypatch.setattr(checks, "run_all", _never)
    client.force_login(admin_user)
    page = client.get(reverse(URL)).text
    assert "No result is stored yet: a run of the checks is under way." in page and "first run" not in page
    assert "The checks are running in the background" in page and "failed" in page
