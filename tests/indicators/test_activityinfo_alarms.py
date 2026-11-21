"""The ActivityInfo import's alarms for administrators follow whether it is followed (``activityinfo.watched``,
Rule 3): the hourly freshness check and the "Needs attention" lines of the admin home (SPEC_HOME §6). The pages
never raise anything about it, followed or not, since ActivityInfo is an archive of past monthly results (the
owner's rule, November 2026): no daily review finding, no For you item about its import, its year-end forecasts
or its yearly rollover (whose population item stays, §6.1).

Scenarios of the survey: S4a (the last ActivityInfo import 45 days ago, the import switched on, records that
stopped in February: archived), S4b (the same with the import switched off), V4 (last year's forecasts in a
new year)."""

import datetime
import itertools
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.urls import reverse
from django.utils import timezone

from neurodb.core import years
from neurodb.core.models import ActivityInfoSetting, PopulationFigure, ScheduledJob, SyncRun
from neurodb.facts.models import ActivityReportNew
from neurodb.indicators.models import Database, MasterIndicator, ReportingYear
from neurodb.indicators.services import activityinfo
from neurodb.insights import services as forecasts
from neurodb.insights.management.commands.forecast_indicators import run as forecast_run
from neurodb.insights.models import IndicatorForecast
from neurodb.integrations.management.commands.check_sync_freshness import SCHEDULED_JOBS, scheduled_jobs
from neurodb.partnerships.models import PartnerLink
from neurodb.review import checks
from neurodb.watch import memory
from neurodb.watch.detectors import DAILY, QUICK, Context, calendar, concerns, system
from neurodb.watch.models import WatchItem
from neurodb.web import health

pytestmark = pytest.mark.django_db

HOUR, DAY = datetime.timedelta(hours=1), datetime.timedelta(days=1)
AI_DATA, AI_STRUCTURE = SyncRun.Job.ACTIVITYINFO_DATA, SyncRun.Job.ACTIVITYINFO_STRUCTURE
SUCCEEDED, FAILED, PARTIAL = SyncRun.Status.SUCCEEDED, SyncRun.Status.FAILED, SyncRun.Status.PARTIAL
SHORT = IndicatorForecast.Status.SHORT
# the lines of "Needs attention" about the ActivityInfo import (Rule 3)
AI_LINES = {
    "databases_stale",
    "masters_without_target",
    "job_failed:ai_data",
    "job_partial:ai_structure",
}
_ids = itertools.count(700001)


# ---------------------------------------------------------------------------- worlds
def _year(name: str, current: bool = False) -> ReportingYear:
    return ReportingYear.objects.create(name=name, year=name[:4], current=current)


def _database(year: ReportingYear, months=(), imported_days_ago: int | None = 45) -> Database:
    """A displayed database of ``year`` with a record of each of ``months``, last imported
    ``imported_days_ago`` (None: never)."""
    database = Database.objects.create(
        ai_id=next(_ids),
        name=f"Database {year.name}",
        username="",
        password="",
        reporting_year=year,
        last_monthly_update_date=(
            timezone.now() - imported_days_ago * DAY if imported_days_ago is not None else None
        ),
    )
    for month in months:
        ActivityReportNew.objects.create(
            dbase=database, month_name=f"{month}-01", month=month, indicator_value=1
        )
    activityinfo.invalidate()  # records reach the state at the end of an import, which drops it
    return database


def _schedule(on: bool) -> None:
    ScheduledJob.objects.filter(command="ai_data").update(enabled=on)  # like the bulk action: no signal
    activityinfo.invalidate()


def _mode(mode: str) -> None:
    ActivityInfoSetting.objects.update_or_create(pk=1, defaults={"mode": mode})
    activityinfo.invalidate()


def world(name: str) -> Database:
    """``in_use``: 2026 records up to August, the import on (as seeded): in use. ``S4a``: records that
    stopped in February, the import on: archived. ``S4b``: records up to August, the import switched off:
    archived. ``archived``: records up to August, set to Archived by an administrator. In each, the 2026
    database was last imported 45 days ago and holds a master indicator without a target."""
    months = ("2026-01", "2026-02") if name == "S4a" else ("2026-07", "2026-08")
    database = _database(_year("2026", current=True), months)
    MasterIndicator.objects.create(database=database, name="Children reached", awp_code="1")  # no target
    if name == "S4b":
        _schedule(False)
    elif name == "archived":
        _mode(ActivityInfoSetting.Mode.ARCHIVED)
    return database


def pending_world() -> Database:
    """ActivityInfo coming back: 2025 has records (history); 2026, ticked current, has a displayed database
    still waiting for its first records. Archived, but the import's alarms are on (``setup_pending``)."""
    _database(_year("2025"), ("2025-11",))
    return _database(_year("2026", current=True), imported_days_ago=None)


def ran(job: str, status: str, at: datetime.datetime, **fields) -> SyncRun:
    return SyncRun.objects.create(
        job=job, status=status, started_at=at - 10 * datetime.timedelta(minutes=1), finished_at=at, **fields
    )


def other_sources_fresh() -> None:
    now = timezone.now()
    ran(SyncRun.Job.ETOOLS_DATAMART, SUCCEEDED, now - HOUR)
    ran(SyncRun.Job.LOCATIONS, SUCCEEDED, now - HOUR)


def keys(drafts) -> set[str]:
    return {draft.key for draft in drafts}


def lines() -> set[str]:
    return {line.key for line in health.warnings()}


def watch(detector, day: datetime.date | None = None, mode: str = DAILY) -> memory.Outcome:
    """One pass of ``detector`` now, or at 09:00 in Beirut on ``day``."""
    now = timezone.now()
    if day is not None:
        now = timezone.make_aware(datetime.datetime.combine(day, datetime.time(9, 0)))
    return memory.run(Context.make(mode, now=now), [detector])


def item(key: str) -> WatchItem:
    return WatchItem.objects.get(key=key)


def state_of(detector_id: str) -> dict[str, str]:
    return dict(WatchItem.objects.filter(detector=detector_id).values_list("key", "state"))


# ---------------------------------------------------------------------------- the hourly freshness check
@pytest.mark.parametrize("scenario", ["S4a", "S4b", "archived"])
def test_not_followed_the_hourly_check_passes_with_a_45_day_old_import(scenario, settings):
    settings.SYNC_STALENESS_HOURS = 30
    world(scenario)
    other_sources_fresh()
    ran(AI_DATA, SUCCEEDED, timezone.now() - 45 * DAY)
    st = activityinfo.state()
    assert (st.in_use, st.watched) == (False, False)
    assert scheduled_jobs() == (SyncRun.Job.ETOOLS_DATAMART, SyncRun.Job.LOCATIONS)
    out = StringIO()
    call_command("check_sync_freshness", stdout=out)
    assert "all 2 job(s) fresh" in out.getvalue() and "ai_data" not in out.getvalue()
    with pytest.raises(CommandError, match="ai_data"):  # named, it is still checked
        call_command("check_sync_freshness", jobs="ai_data", stdout=StringIO())


def test_followed_the_same_import_fails_the_hourly_check(settings):
    settings.SYNC_STALENESS_HOURS = 30
    world("in_use")
    other_sources_fresh()
    ran(AI_DATA, SUCCEEDED, timezone.now() - 45 * DAY)
    assert activityinfo.watched() and scheduled_jobs() == SCHEDULED_JOBS
    with pytest.raises(CommandError, match="1 stale job"):
        call_command("check_sync_freshness", stdout=StringIO())


def test_the_check_freshness_button_says_activityinfo_counts_only_while_followed(client, admin_user):
    admin_user.is_superuser = True
    admin_user.save()
    client.force_login(admin_user)
    page = client.get(reverse("admin:core_syncrun_run_check_freshness")).content.decode()
    assert "eTools Datamart, locations, and ActivityInfo data while it is followed" in page


# ---------------------------------------------------------------------------- the daily review
def _review_inputs() -> None:
    """A 45-day-old import, failed ActivityInfo runs this morning and an unlinked ActivityInfo partner."""
    other_sources_fresh()
    now = timezone.now()
    ran(AI_DATA, SUCCEEDED, now - 45 * DAY)
    ran(AI_DATA, FAILED, now - 2 * HOUR, error="credentials")
    ran(AI_STRUCTURE, PARTIAL, now - 3 * HOUR, rows_failed=2)
    PartnerLink.objects.create(label="A partner named in ActivityInfo", records=12)


@pytest.mark.parametrize("scenario", ["in_use", "S4a", "S4b", "archived"])
def test_the_daily_review_never_finds_a_stale_import_failed_run_or_partner_name(scenario):
    """In use too (15 October: the import runs on days 1 to 22): the archive is nobody's finding."""
    world(scenario)
    _review_inputs()
    ctx = checks.Context(today=years.today())
    assert keys(checks.check_stale_sources(ctx)) == set()
    assert keys(checks.check_sync_failures(ctx)) == set()
    assert "data_quality:partner_links" not in keys(checks.check_data_quality(ctx))


# ---------------------------------------------------------------------------- "Needs attention" and Watch
@pytest.mark.parametrize(
    ("scenario", "followed"), [("in_use", True), ("S4a", False), ("S4b", False), ("archived", False)]
)
def test_the_activityinfo_lines_of_needs_attention_follow_the_import(scenario, followed):
    world(scenario)
    now = timezone.now()
    ran(AI_DATA, FAILED, now - HOUR)
    ran(AI_STRUCTURE, PARTIAL, now - HOUR, rows_failed=3)
    ran(SyncRun.Job.ETOOLS, FAILED, now - HOUR)
    found = lines()
    assert (found & AI_LINES) == (AI_LINES if followed else set())
    assert "job_failed:etools" in found  # the other lines stay


@pytest.mark.parametrize(("mode", "followed"), [("in_use", True), ("auto", False)])
def test_no_reporting_year_at_all_is_a_line_only_while_activityinfo_is_followed(mode, followed):
    _mode(mode)
    assert ("no_current_year" in lines()) is followed


def test_the_import_lines_never_become_watch_items():
    """In use, a failed import is a "Needs attention" line on the admin home, never a For you item."""
    world("in_use")
    ran(AI_DATA, FAILED, timezone.now() - HOUR)
    assert {"job_failed:ai_data", "databases_stale", "masters_without_target"} <= lines()
    watch(system.SYSTEM_HEALTH)
    assert {
        "system:job_failed:ai_data",
        "system:databases_stale",
        "system:masters_without_target",
    }.isdisjoint(state_of(system.ID))


def test_an_import_item_opened_before_closes_as_no_longer_followed_never_fixed():
    """spec_state-4: an item opened while the import was followed closes as changed, never as resolved
    ("fixed"), its last run still failing."""
    world("in_use")
    ran(AI_DATA, FAILED, timezone.now() - HOUR, error="credentials")
    today = years.today()
    WatchItem.objects.create(
        key="system:job_failed:ai_data", detector=system.ID, kind=WatchItem.Kind.SYSTEM,
        severity=WatchItem.Severity.WARNING, title="ActivityInfo data import failed",
        scope=WatchItem.Scope.ADMINS, first_seen_on=today, last_seen_on=today, changed_on=today,
    )  # fmt: skip
    watch(system.SYSTEM_HEALTH)
    found = item("system:job_failed:ai_data")
    assert SyncRun.objects.filter(job=AI_DATA).latest("finished_at").status == FAILED  # still failing
    assert (found.state, found.close_kind) == (WatchItem.State.CLOSED, WatchItem.CloseKind.CHANGED)
    assert found.close_reason == system.NOT_FOLLOWED == "no longer followed: the source is an archive"


def test_a_line_not_about_activityinfo_that_goes_is_fixed():
    world("in_use")
    ran(SyncRun.Job.ETOOLS_DATAMART, FAILED, timezone.now() - 2 * HOUR, target="partners")
    watch(system.SYSTEM_HEALTH)
    _mode(ActivityInfoSetting.Mode.ARCHIVED)
    ran(SyncRun.Job.ETOOLS_DATAMART, SUCCEEDED, timezone.now() - HOUR, target="partners")
    watch(system.SYSTEM_HEALTH)
    etools = item("system:job_failed:etools_datamart")
    assert (etools.close_reason, etools.close_kind) == (system.FIXED, WatchItem.CloseKind.RESOLVED)


@pytest.mark.parametrize(
    ("key", "about"),
    [
        ("no_current_year", True),
        ("databases_never_imported", True),
        ("databases_stale", True),
        ("masters_without_target", True),
        ("job_failed:ai_data", True),
        ("job_partial:ai_structure", True),
        ("job_failed:etools_datamart", False),
        ("job_never_run:ai-data", False),  # the scheduler's line about a switched-on job, not the import's
        ("users_without_role", False),
    ],
)
def test_the_lines_about_the_activityinfo_import(key, about):
    assert health.about_activityinfo(key) is about


def test_a_returning_year_waiting_for_its_first_records_is_followed_while_archived():
    pending_world()
    now = timezone.now()
    other_sources_fresh()
    ran(AI_DATA, FAILED, now - HOUR, error="credentials")
    PartnerLink.objects.create(label="A partner named in ActivityInfo", records=12)
    st = activityinfo.state()
    assert (st.in_use, st.watched, st.setup_pending, st.reason) == (False, True, True, "auto_no_records")
    assert scheduled_jobs() == SCHEDULED_JOBS
    found = lines()
    assert {
        "databases_never_imported",
        "job_failed:ai_data",
    } <= found  # a failing comeback: on the admin home
    ctx = checks.Context(today=years.today())
    assert "sync_failures:ai_data" not in keys(checks.check_sync_failures(ctx))  # never a review finding
    assert "data_quality:partner_links" not in keys(checks.check_data_quality(ctx))
    watch(system.SYSTEM_HEALTH)
    assert "system:databases_never_imported" not in state_of(system.ID)  # nor a For you item


# ---------------------------------------------------------------------------- forecast items
def _forecasts(database: Database, year: int = 2026, at: datetime.datetime | None = None) -> MasterIndicator:
    """A shown forecast run of ``year`` (one hour before ``at``) with one indicator likely to fall short."""
    at = at or timezone.now()
    ran(
        SyncRun.Job.FORECAST,
        SUCCEEDED,
        at - HOUR,
        details={"year": year, "as_of": 8, "backtest": {"shown": True}},
    )
    master = MasterIndicator.objects.create(
        database=database, name="Children in case management", awp_code="7"
    )
    IndicatorForecast.objects.create(
        master=master,
        database=database,
        year=year,
        as_of_month=8,
        value_to_date=400,
        target=1000,
        forecast=700,
        low=600,
        high=800,
        linear=650,
        status=SHORT,
        basis="its own past years",
        own_years=3,
        computed_at=at - HOUR,
    )
    return master


@pytest.mark.parametrize("scenario", ["in_use", "S4a", "S4b", "archived"])
def test_the_forecast_check_is_silent_in_use_or_not(scenario):
    """The year-end forecasts are the ActivityInfo archive's (its database dashboards and forecasts page keep
    them): no For you item, even in use with a shown forecast likely to fall short."""
    _forecasts(world(scenario))
    outcome = watch(concerns.FORECAST_SHORT)
    assert state_of(concerns.FORECAST_ID) == {} and outcome.new == []


@pytest.mark.parametrize("day", [datetime.date(2027, 1, 5), datetime.date(2027, 2, 1)])
def test_v4_in_2027_last_years_forecasts_are_no_longer_followed(day, site_today):
    """2026 records up to August and a shown 2026 forecast: in 2027 (January included: August is five months
    before it) ActivityInfo is archived, the job skips, the 2026 table stays, nothing about 2027 is at risk
    and no Watch item opens."""
    _forecasts(world("in_use"))
    site_today.set(day)
    activityinfo.invalidate()
    assert not activityinfo.in_use()
    skipped = forecast_run("test")
    assert skipped.details["skipped"] == "ActivityInfo is not in use in 2027"
    assert IndicatorForecast.objects.filter(year=2026).count() == 1  # the table is kept
    assert forecasts.at_risk(year=2027) == []
    watch(concerns.FORECAST_SHORT, day=day)
    assert state_of(concerns.FORECAST_ID) == {}


STALE_FORECAST = "system:stale:forecast"


@pytest.mark.parametrize("archived", [False, True])
def test_no_stale_forecast_alarm_in_use_or_archived(archived):
    """spec_state-5, now in use too: the forecast check has nothing to follow, so a forecast 9 days old (the job
    switched off once archived) raises no "Year-end indicator forecast: no successful run since ..." item."""
    _forecasts(world("in_use"), at=timezone.now() - 9 * DAY)
    if archived:
        _mode(ActivityInfoSetting.Mode.ARCHIVED)
        ScheduledJob.objects.filter(command="forecast").update(enabled=False)
    outcome = watch(concerns.FORECAST_SHORT)
    assert outcome.stale_sources == [] and not WatchItem.objects.filter(key=STALE_FORECAST).exists()


def test_in_january_the_forecast_job_skips_and_no_item_opens(site_today):
    """spec_state-6: 2026 records up to December keep ActivityInfo in use in January (the December import);
    the weekly job skips (no 2027 ActivityInfo year), the table still holds 2026, and nothing is followed."""
    _forecasts(_database(_year("2026", current=True), ("2026-11", "2026-12")))
    site_today.set(datetime.date(2027, 1, 5))
    activityinfo.invalidate()
    assert activityinfo.state().reason == "auto_january"
    assert forecast_run("test").details["skipped"] == "ActivityInfo is not in use in 2027"
    outcome = watch(concerns.FORECAST_SHORT, day=datetime.date(2027, 1, 5))
    assert outcome.new == [] and state_of(concerns.FORECAST_ID) == {}


# ---------------------------------------------------------------------------- the yearly rollover (§6.1)
ROLLOVER = calendar.REPORTING_YEAR_ROLLOVER
DEC_15, JAN_2, JAN_15, JAN_16 = (
    datetime.date(2026, 12, 15),
    datetime.date(2027, 1, 2),
    datetime.date(2027, 1, 15),
    datetime.date(2027, 1, 16),
)
# Administrators have no shell and cannot add a year's population file in the app, so the text says who
# takes that step (R2 sweep-4: text changed on purpose).
POPULATION_DETAIL = (
    "NeuroDB's pages move to 2027 by themselves on 1 January. When the newest population estimates are "
    "published, ask the NeuroDB team to add the year's file; it loads by itself at the next start (step 5 "
    "of the yearly rollover in the operations guide)."
)


def rollover_keys() -> set[str]:
    return set(WatchItem.objects.filter(detector=ROLLOVER.id).values_list("key", flat=True))


def population(year: int) -> PopulationFigure:
    return PopulationFigure.objects.create(
        year=year, nationality="ALL", level="national", category="total", value=5_000_000
    )


@pytest.mark.parametrize(
    "scenario", ["in_use", "set_in_use", "archived", "S4b", "no_2026_records", "2027_databases"]
)
def test_never_an_activityinfo_rollover_item_the_population_item_instead(scenario):
    """In use, set In use with its import switched off, archived, or set up ahead: the rollover asks for the
    population figures alone (ActivityInfo, an archive, is no longer set up from For you)."""
    if scenario == "set_in_use":
        _mode(ActivityInfoSetting.Mode.IN_USE)
        _schedule(False)
    elif scenario == "no_2026_records":
        _database(_year("2025", current=True), ("2025-11",))  # the import is on over 2025 only
    elif scenario == "2027_databases":
        world("in_use")
        _database(_year("2027"), imported_days_ago=None)  # set up ahead of time
    else:
        world(scenario)
    watch(ROLLOVER, DEC_15)
    assert rollover_keys() == {"rollover:2027:population"}


def test_the_population_item_asks_for_the_new_years_figures_and_closes_when_they_are_added():
    population(2026)
    watch(ROLLOVER, DEC_15)
    found = item("rollover:2027:population")
    assert found.title == "Population figures for 2027"
    assert found.detail == POPULATION_DETAIL
    assert found.url == reverse("reports:population")
    assert (found.scope, found.severity, found.due_date) == ("admins", "info", datetime.date(2027, 1, 1))
    assert found.evidence["records"][0]["value"] == "2026"
    watch(ROLLOVER, JAN_2)
    assert item("rollover:2027:population").severity == WatchItem.Severity.WARNING
    population(2027)
    watch(ROLLOVER, JAN_2, mode=QUICK)
    found = item("rollover:2027:population")
    assert (found.state, found.close_reason) == ("closed", "population figures for 2027 were added")


def test_no_population_item_when_the_new_years_figures_are_there():
    population(2027)
    watch(ROLLOVER, DEC_15)
    assert rollover_keys() == set()


def test_the_population_item_ends_missed_on_16_january():
    watch(ROLLOVER, JAN_15)
    watch(ROLLOVER, JAN_16)
    found = item("rollover:2027:population")
    assert (
        found.close_reason == "the rollover period ended on 15 Jan 2027 without population figures for 2027"
    )
    assert found.close_kind == WatchItem.CloseKind.MISSED
