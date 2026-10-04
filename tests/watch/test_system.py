"""System health shared by the admin home and NeuroDB Watch: stable keys, the levels, the Compiler rule,
the watch's own lines, and the system items told to the administrators only."""

import datetime

import pytest

from neurodb.core.models import ScheduledJob, SchedulerState, SyncRun
from neurodb.review.models import DailyReview
from neurodb.watch import memory, redact
from neurodb.watch.detectors import ADMINS, CRITICAL, DAILY, ON, QUICK, SYSTEM, WARNING, Context, system
from neurodb.watch.models import DetectorSetting, WatchItem, WatchState
from neurodb.web import health

pytestmark = pytest.mark.django_db

UTC = datetime.UTC
NOW = datetime.datetime(2026, 10, 5, 6, 0, tzinfo=UTC)  # 09:00 in Beirut
TODAY = datetime.date(2026, 10, 5)
HOUR = datetime.timedelta(hours=1)
DATAMART = SyncRun.Job.ETOOLS_DATAMART
SUCCEEDED, FAILED = SyncRun.Status.SUCCEEDED, SyncRun.Status.FAILED


def ctx(minutes: int = 0, mode: str = DAILY) -> Context:
    return Context.make(mode, now=NOW + datetime.timedelta(minutes=minutes))


def check(minutes: int = 0, mode: str = DAILY) -> memory.Outcome:
    """One pass of the system check alone."""
    return memory.run(ctx(minutes, mode), [system.SYSTEM_HEALTH])


def lines(now: datetime.datetime = NOW) -> dict[str, health.Warning]:
    return {line.key: line for line in health.warnings(now)}


def items() -> dict[str, WatchItem]:
    return {item.key: item for item in WatchItem.objects.filter(detector=system.ID)}


def ran(job: str, status: str, at: datetime.datetime, target: str = "", **fields) -> SyncRun:
    return SyncRun.objects.create(
        job=job,
        target=target,
        status=status,
        started_at=at,
        finished_at=at + datetime.timedelta(minutes=5),
        **fields,
    )


@pytest.fixture
def healthy(db, reporting_year):
    """Nothing needs attention at NOW: a current year, the scheduler checking in, every job planned
    ahead, today's daily review and this morning's run of NeuroDB Watch."""
    SchedulerState.objects.create(pk=1, last_seen_at=NOW)
    ScheduledJob.objects.update(next_run_at=NOW + HOUR)
    DailyReview.objects.create(date=TODAY, status=DailyReview.Status.SUCCEEDED)
    ran(SyncRun.Job.WATCH, SUCCEEDED, NOW - 2 * HOUR, target="daily")


def test_all_well_means_no_line_and_no_item_and_the_check_is_on_from_the_start(healthy):
    assert health.warnings(NOW) == []
    check()
    assert DetectorSetting.objects.get(detector=system.ID).mode == ON
    assert not WatchItem.objects.exists()


def test_a_failed_datamart_run_is_one_item_with_the_same_key_across_two_runs(healthy):
    run = ran(DATAMART, FAILED, NOW - 3 * HOUR, error="boom")

    first = check()
    second = check(minutes=2)

    assert first.new == ["system:job_failed:etools_datamart"] and second.new == []
    (item,) = items().values()
    assert item.key == "system:job_failed:etools_datamart"
    assert (item.kind, item.severity, item.scope, item.state) == (SYSTEM, WARNING, ADMINS, "open")
    assert item.title == "The last eTools Datamart sync failed."
    assert item.url.endswith(f"/core/syncrun/{run.pk}/change/")
    (found,) = item.evidence["records"]
    assert found["label"] == "Last run of the eTools Datamart sync" and found["value"] == "Failed"
    assert found["date"] == "2026-10-05" and item.evidence["numbers"]["run"] == run.pk
    assert [line["text"] for line in item.story] == ["First noticed"]


def test_the_item_closes_at_once_when_the_line_is_gone(healthy):
    ran(DATAMART, FAILED, NOW - 3 * HOUR)
    check()
    ran(DATAMART, SUCCEEDED, NOW - HOUR)

    outcome = check(minutes=2, mode=QUICK)  # positive evidence: even a quick pass closes it

    item = items()["system:job_failed:etools_datamart"]
    assert outcome.closed == [item.key] and item.state == "closed"
    assert item.close_reason == system.FIXED


def test_a_partial_run_has_its_own_key(healthy):
    ran(SyncRun.Job.ETOOLS, SyncRun.Status.PARTIAL, NOW - HOUR, rows_failed=20)
    line = lines()["job_partial:etools"]
    assert line.text == "The last eTools sync succeeded with errors: 20 rows failed."
    assert line.level == WARNING


def test_a_disabled_compiler_job_is_never_listed(healthy):
    ran(SyncRun.Job.COMPILER_YOUTH, FAILED, NOW - HOUR)
    assert not ScheduledJob.objects.get(command="compiler_youth").enabled  # off by default

    assert "job_failed:compiler_youth" not in lines()
    check()
    assert not WatchItem.objects.exists()

    ScheduledJob.objects.filter(command="compiler_youth").update(enabled=True)
    assert "job_failed:compiler_youth" in lines()


def test_a_silent_scheduler_is_a_critical_item_for_the_administrators_only(healthy):
    SchedulerState.objects.filter(pk=1).update(last_seen_at=NOW - 15 * HOUR)

    line = lines()["scheduler_silent"]
    assert line.critical and line.text.startswith("The scheduler has not checked in for 15")
    check()

    item = items()["system:scheduler_silent"]
    assert (item.severity, item.scope, item.kind) == (CRITICAL, ADMINS, SYSTEM)
    # the kept title carries a date, not a duration; never sent to the AI
    assert (
        item.title == "The scheduler has not checked in since 4 Oct, 18:00: scheduled jobs are not starting."
    )
    assert redact.refused(item) == redact.SYSTEM
    # its evidence does not move with the clock
    fingerprint = item.evidence_hash
    check(minutes=30)
    item.refresh_from_db()
    assert item.evidence_hash == fingerprint and item.state == "open"


def test_a_scheduler_that_never_checked_in_and_overdue_jobs(healthy):
    SchedulerState.objects.all().delete()
    ScheduledJob.objects.filter(key="etools-datamart").update(next_run_at=NOW - 2 * HOUR)
    found = lines()
    assert found["scheduler_silent"].text == (
        "The scheduler has never checked in: scheduled jobs are not starting. 1 job is overdue."
    )
    assert "job_never_run:etools-datamart" in found  # the data it brings was never there

    ran(DATAMART, SUCCEEDED, NOW - 30 * HOUR)
    SchedulerState.objects.create(pk=1, last_seen_at=NOW)
    found = lines()
    assert "scheduler_silent" not in found and "job_never_run:etools-datamart" not in found
    assert found["job_overdue:etools-datamart"].text.startswith(
        "Scheduled job “Sync eTools (Datamart)” is overdue"
    )


def test_no_daily_review_is_critical(healthy):
    DailyReview.objects.all().delete()
    DailyReview.objects.create(date=TODAY - datetime.timedelta(days=3), status=DailyReview.Status.SUCCEEDED)

    line = lines()["no_daily_review"]
    assert line.critical and line.text == "No daily review since 2 Oct 2026."
    check()
    assert items()["system:no_daily_review"].severity == CRITICAL


def test_the_watch_not_run_line_and_the_pass_it_asks_for(healthy):
    SyncRun.objects.filter(job=SyncRun.Job.WATCH).update(
        started_at=NOW - 27 * HOUR, finished_at=NOW - 27 * HOUR
    )
    ran(SyncRun.Job.WATCH, SUCCEEDED, NOW - HOUR, target="quick")  # a quick pass does not count

    line = lines()["watch_not_run"]
    assert line.text.startswith(
        "NeuroDB Watch has not run for more than 26 hours (last morning run 4 Oct, 06:00)"
    )
    assert line.level == WARNING and line.url.endswith("?job__exact=watch")

    check()  # the morning pass is the run it asks for: not told
    assert "system:watch_not_run" not in items()
    check(minutes=1, mode=QUICK)  # a quick pass: the morning run is still missing
    assert items()["system:watch_not_run"].state == "open"

    ran(SyncRun.Job.WATCH, SUCCEEDED, NOW, target="daily")
    check(minutes=10)
    assert items()["system:watch_not_run"].state == "closed"


def test_the_watch_not_run_line_waits_for_its_first_run_and_follows_the_switches(healthy, settings):
    SyncRun.objects.filter(job=SyncRun.Job.WATCH).delete()
    assert "watch_not_run" not in lines()  # never started: the scheduled job's own line says so

    ran(SyncRun.Job.WATCH, FAILED, NOW - 30 * HOUR, target="daily")
    line = lines()["watch_not_run"]
    assert line.text.startswith(
        "NeuroDB Watch has not completed a morning run since it first ran on 4 Oct, 03:00"
    )

    ScheduledJob.objects.filter(command="watch").update(enabled=False)
    assert "watch_not_run" not in lines()
    ScheduledJob.objects.filter(command="watch").update(enabled=True)
    settings.WATCH_ENABLED = False
    assert "watch_not_run" not in lines()


def test_the_ai_paused_line(healthy, settings):
    state = WatchState.objects.create(
        pk=1, ai_paused_until=NOW + 2 * HOUR, ai_pause_reason="the OpenAI credit ran out"
    )

    line = lines()["watch_ai_paused"]
    assert line.text == "NeuroDB Watch stopped using AI until 11:00: the OpenAI credit ran out."
    assert line.url.endswith("/assistant/aiusage/")

    state.ai_paused_until = NOW + 24 * HOUR
    state.ai_pause_reason = ""
    state.save()
    assert lines()["watch_ai_paused"].text == "NeuroDB Watch stopped using AI until 6 Oct, 09:00."

    assert "watch_ai_paused" not in lines(NOW + 25 * HOUR)  # the pause is over
    settings.WATCH_AI = False
    assert "watch_ai_paused" not in lines()


def test_lines_without_a_watch_state_create_none(healthy):
    health.warnings(NOW)
    assert not WatchState.objects.exists()  # reading the admin home writes nothing


def test_the_keys_are_stable_and_unique(db, settings):
    settings.SCHEDULER_ENABLED = True
    ran(DATAMART, FAILED, NOW - HOUR)
    ran(SyncRun.Job.ETOOLS, SyncRun.Status.PARTIAL, NOW - HOUR, rows_failed=3)
    first = [line.key for line in health.warnings(NOW)]
    second = [line.key for line in health.warnings(NOW + datetime.timedelta(minutes=10))]

    assert first == second and len(set(first)) == len(first)
    assert {"no_current_year", "scheduler_silent", "job_failed:etools_datamart", "job_partial:etools"} <= set(
        first
    )
    assert not any(key.startswith("stale:") for key in first)  # the stale-source items' keys


def test_the_section_lines_become_items_too(healthy):
    from neurodb.watch.models import SectionMatch

    SectionMatch.objects.create(etools_name="Education", how=SectionMatch.How.NONE)

    assert "watch_sections_unconfirmed" in lines()
    check()
    assert items()["system:watch_sections_unconfirmed"].scope == ADMINS


def test_the_jobs_counted_only_on_a_schedule_are_real_jobs_with_a_command():
    from neurodb.core.jobs import COMMANDS

    assert health.SCHEDULED_ONLY <= set(SyncRun.Job.values)
    assert health.SCHEDULED_ONLY <= {command.sync_job for command in COMMANDS.values()}


def test_a_morning_run_recorded_without_its_end_counts_from_its_start(healthy):
    SyncRun.objects.filter(job=SyncRun.Job.WATCH).update(started_at=NOW - 30 * HOUR, finished_at=None)
    assert "watch_not_run" in lines()
