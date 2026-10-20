"""The ActivityInfo import's alarms follow whether it is followed (``activityinfo.watched``, Rule 3), and the
alarms about presenting ActivityInfo follow whether it is in use (``activityinfo.in_use``, Rule 2): the hourly
freshness check, the daily review's checks, the administrators' "Needs attention" lines and their Watch
items, the forecast items, and the yearly rollover with its population item (SPEC_HOME §6, §6.1).

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
from neurodb.watch import memory, redact
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


@pytest.mark.parametrize("scenario", ["S4a", "S4b", "archived"])
def test_not_followed_the_daily_review_finds_no_stale_import_failed_run_or_partner_name(scenario):
    world(scenario)
    _review_inputs()
    ctx = checks.Context(today=years.today())
    assert keys(checks.check_stale_sources(ctx)) == set()
    assert keys(checks.check_sync_failures(ctx)) == set()
    assert "data_quality:partner_links" not in keys(checks.check_data_quality(ctx))


def test_in_use_the_daily_review_finds_all_three():
    world("in_use")
    _review_inputs()
    ctx = checks.Context(today=years.today())  # 15 October: the import runs on days 1 to 22
    assert keys(checks.check_stale_sources(ctx)) == {"stale_sources:ai_data"}
    assert keys(checks.check_sync_failures(ctx)) == {"sync_failures:ai_data", "sync_failures:ai_structure"}
    assert "data_quality:partner_links" in keys(checks.check_data_quality(ctx))


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


def test_the_watch_items_of_the_import_close_once_it_is_no_longer_followed():
    world("in_use")
    ran(AI_DATA, FAILED, timezone.now() - HOUR)
    watch(system.SYSTEM_HEALTH)
    opened = {"system:job_failed:ai_data", "system:databases_stale", "system:masters_without_target"}
    assert {key for key, state in state_of(system.ID).items() if state == "open"} >= opened
    job = ScheduledJob.objects.get(command="ai_data")
    job.enabled = False
    job.save()  # switched off in Scheduled jobs: the state follows at once
    outcome = watch(system.SYSTEM_HEALTH)
    assert set(outcome.closed) >= opened
    # no longer followed, not fixed (spec_state-4): the last import still failed, the database is still stale
    assert {(item(key).close_reason, item(key).close_kind) for key in opened} == {
        (system.NOT_FOLLOWED, WatchItem.CloseKind.CHANGED)
    }


def test_set_to_archived_a_failing_import_is_not_said_fixed():
    """spec_state-4: the import's item opened while in use; an administrator sets Archived while its last run
    still failed. The item closes as changed ("ActivityInfo is an archive: its import is no longer
    followed"), as the forecast items do, never as resolved ("fixed")."""
    world("in_use")
    ran(AI_DATA, FAILED, timezone.now() - HOUR, error="credentials")
    watch(system.SYSTEM_HEALTH)
    assert state_of(system.ID)["system:job_failed:ai_data"] == "open"
    _mode(ActivityInfoSetting.Mode.ARCHIVED)
    watch(system.SYSTEM_HEALTH)
    found = item("system:job_failed:ai_data")
    assert SyncRun.objects.filter(job=AI_DATA).latest("finished_at").status == FAILED  # still failing
    assert (found.state, found.close_kind) == (WatchItem.State.CLOSED, WatchItem.CloseKind.CHANGED)
    assert found.close_reason == "ActivityInfo is an archive: its import is no longer followed"


def test_while_followed_a_line_that_goes_is_still_fixed():
    """The other side of spec_state-4: while the import is followed, a line that goes was fixed (a new
    import succeeded), and a line not about ActivityInfo is fixed whatever ActivityInfo is."""
    world("in_use")
    ran(AI_DATA, FAILED, timezone.now() - 2 * HOUR)
    ran(SyncRun.Job.ETOOLS_DATAMART, FAILED, timezone.now() - 2 * HOUR, target="partners")
    watch(system.SYSTEM_HEALTH)
    ran(AI_DATA, SUCCEEDED, timezone.now() - HOUR)
    watch(system.SYSTEM_HEALTH)
    fixed = item("system:job_failed:ai_data")
    assert (fixed.close_reason, fixed.close_kind) == (system.FIXED, WatchItem.CloseKind.RESOLVED)
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
    assert {"databases_never_imported", "job_failed:ai_data"} <= found  # a failing comeback is reported
    ctx = checks.Context(today=years.today())
    assert "sync_failures:ai_data" in keys(checks.check_sync_failures(ctx))
    assert "data_quality:partner_links" not in keys(checks.check_data_quality(ctx))  # follows "in use"
    watch(system.SYSTEM_HEALTH)
    assert state_of(system.ID)["system:databases_never_imported"] == "open"


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


def test_in_use_a_forecast_likely_short_is_followed_and_archived_closes_it():
    master = _forecasts(world("in_use"))
    key = concerns.forecast_item_key(2026, master.pk)
    watch(concerns.FORECAST_SHORT)
    assert state_of(concerns.FORECAST_ID) == {key: "open"}
    _mode(ActivityInfoSetting.Mode.ARCHIVED)
    outcome = watch(concerns.FORECAST_SHORT)
    found = item(key)
    assert outcome.closed == [key]
    assert found.close_reason == "ActivityInfo is archived: its forecasts are no longer followed"
    assert found.close_kind == WatchItem.CloseKind.CHANGED
    outcome = watch(concerns.FORECAST_SHORT)
    assert outcome.new == [] and outcome.reopened == []  # silent from then on


@pytest.mark.parametrize("scenario", ["S4a", "S4b", "archived"])
def test_not_in_use_the_forecast_check_is_silent(scenario):
    _forecasts(world(scenario))
    watch(concerns.FORECAST_SHORT)
    assert state_of(concerns.FORECAST_ID) == {}


@pytest.mark.parametrize("day", [datetime.date(2027, 1, 5), datetime.date(2027, 2, 1)])
def test_v4_in_2027_last_years_forecasts_are_no_longer_followed(day, site_today):
    """2026 records up to August and a shown 2026 forecast: in 2027 (January included: August is five months
    before it) ActivityInfo is archived, the job skips, the 2026 table stays, nothing about 2027 is at risk
    and the Watch items close."""
    master = _forecasts(world("in_use"))
    key = concerns.forecast_item_key(2026, master.pk)
    watch(concerns.FORECAST_SHORT)
    assert state_of(concerns.FORECAST_ID) == {key: "open"}
    site_today.set(day)
    activityinfo.invalidate()
    assert not activityinfo.in_use()
    skipped = forecast_run("test")
    assert skipped.details["skipped"] == "ActivityInfo is not in use in 2027"
    assert IndicatorForecast.objects.filter(year=2026).count() == 1  # the table is kept
    assert forecasts.at_risk(year=2027) == []
    outcome = watch(concerns.FORECAST_SHORT, day=day)
    assert outcome.closed == [key]
    assert item(key).close_reason == concerns.ARCHIVED


def test_in_use_only_this_years_forecasts_open_items():
    master = _forecasts(world("in_use"), year=2025)  # a table left from last year
    watch(concerns.FORECAST_SHORT)
    assert state_of(concerns.FORECAST_ID) == {}
    IndicatorForecast.objects.update(year=2026)
    watch(concerns.FORECAST_SHORT)
    assert state_of(concerns.FORECAST_ID) == {concerns.forecast_item_key(2026, master.pk): "open"}


STALE_FORECAST = "system:stale:forecast"


def test_archived_with_the_forecast_job_switched_off_no_stale_forecast_alarm():
    """spec_state-5: Archived, then the (now pointless) forecast job switched off; the last forecast ran 9 days
    ago. The forecast check has nothing to follow, so no "Year-end indicator forecast: no successful run
    since ..." item goes to the administrators, and its own items close as "no longer followed"."""
    master = _forecasts(world("in_use"), at=timezone.now() - 7 * DAY)  # fresh: the forecast is weekly
    key = concerns.forecast_item_key(2026, master.pk)
    watch(concerns.FORECAST_SHORT)
    assert state_of(concerns.FORECAST_ID) == {key: "open"}
    _mode(ActivityInfoSetting.Mode.ARCHIVED)
    ScheduledJob.objects.filter(command="forecast").update(enabled=False)
    outcome = watch(concerns.FORECAST_SHORT, day=years.today() + 2 * DAY)  # the last forecast: 9 days old
    assert outcome.stale_sources == []
    assert not WatchItem.objects.filter(key=STALE_FORECAST).exists()
    assert (item(key).close_reason, item(key).close_kind) == (concerns.ARCHIVED, WatchItem.CloseKind.CHANGED)


def test_a_stale_forecast_alarm_raised_while_in_use_closes_once_activityinfo_is_archived():
    """spec_state-5: the stale-source item raised while the forecasts were followed closes as changed, with
    the check's reason, once nothing reads the forecasts; while in use it is raised as before."""
    _forecasts(world("in_use"), at=timezone.now() - 9 * DAY)
    outcome = watch(concerns.FORECAST_SHORT)
    assert outcome.stale_sources == ["forecast"] and item(STALE_FORECAST).state == "open"  # in use: an alarm
    _mode(ActivityInfoSetting.Mode.ARCHIVED)
    outcome = watch(concerns.FORECAST_SHORT)
    stale = item(STALE_FORECAST)
    assert outcome.stale_sources == [] and stale.state == WatchItem.State.CLOSED
    assert (stale.close_reason, stale.close_kind) == (concerns.ARCHIVED, WatchItem.CloseKind.CHANGED)


def test_in_january_while_in_use_last_years_forecast_items_close_as_the_year_ended(site_today):
    """spec_state-6: 2026 records up to December keep ActivityInfo in use in January (the December import);
    the weekly job skips (no 2027 ActivityInfo year), the table still holds 2026. Last year's item closes at
    once as changed, "2026 has ended", instead of lingering until it is counted gone."""
    database = _database(_year("2026", current=True), ("2026-11", "2026-12"))
    master = _forecasts(database)
    key = concerns.forecast_item_key(2026, master.pk)
    watch(concerns.FORECAST_SHORT)
    assert state_of(concerns.FORECAST_ID) == {key: "open"}
    site_today.set(datetime.date(2027, 1, 5))
    activityinfo.invalidate()
    assert activityinfo.state().reason == "auto_january"
    assert forecast_run("test").details["skipped"] == "ActivityInfo is not in use in 2027"
    outcome = watch(concerns.FORECAST_SHORT, day=datetime.date(2027, 1, 5))
    assert outcome.closed == [key] and outcome.new == []
    found = item(key)
    assert (found.close_reason, found.close_kind) == (
        "2026 has ended: its forecasts are no longer followed",
        WatchItem.CloseKind.CHANGED,
    )


# ---------------------------------------------------------------------------- the yearly rollover (§6.1)
ROLLOVER = calendar.REPORTING_YEAR_ROLLOVER
DEC_15, JAN_2, JAN_15, JAN_16 = (
    datetime.date(2026, 12, 15),
    datetime.date(2027, 1, 2),
    datetime.date(2027, 1, 15),
    datetime.date(2027, 1, 16),
)
ROLLOVER_DETAIL = (
    "NeuroDB's pages move to 2027 by themselves on 1 January. The ActivityInfo import brings in the December "
    "2026 results until 22 January, so keep the 2026 reporting year ticked current until then. If partners "
    "still report in ActivityInfo in 2027, then create the 2027 reporting year, tick it current and add its "
    "databases (yearly rollover steps in the operations guide). If not, switch off its import in Scheduled "
    "jobs after 22 January; ActivityInfo then becomes an archive by itself (or set it to Archived in the "
    "admin: ActivityInfo: in use or archive)."
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


def test_the_activityinfo_rollover_item_says_what_to_do_and_where():
    world("in_use")
    watch(ROLLOVER, DEC_15)
    assert rollover_keys() == {"rollover:2027"}
    found = item("rollover:2027")
    assert found.title == "ActivityInfo in 2027: set it up, or set it to Archived"
    assert found.detail == ROLLOVER_DETAIL
    assert found.url == reverse("admin:core_activityinfosetting_changelist")  # the setting's page
    assert (found.scope, found.severity) == ("admins", "info") and redact.refused(found) == redact.ADMINS_ONLY
    assert [r["label"] for r in found.evidence["records"]] == [
        "ActivityInfo setting",
        "ActivityInfo import",
        "Databases for 2027",
    ]
    assert "Reporting years" not in found.detail


def test_set_in_use_the_rollover_item_is_raised_whatever_the_import_and_the_records():
    _mode(ActivityInfoSetting.Mode.IN_USE)
    _schedule(False)
    watch(ROLLOVER, DEC_15)
    assert rollover_keys() == {"rollover:2027"}
    assert [r["value"] for r in item("rollover:2027").evidence["records"]] == [
        "In use",
        "switched off",
        "none yet",
    ]


@pytest.mark.parametrize("scenario", ["archived", "S4b", "no_2026_records", "2027_databases"])
def test_no_activityinfo_rollover_item_then_the_population_item_instead(scenario):
    if scenario == "no_2026_records":
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


def test_the_rollover_closes_when_activityinfo_is_set_to_archived_and_the_population_item_follows():
    world("in_use")
    watch(ROLLOVER, DEC_15)
    _mode(ActivityInfoSetting.Mode.ARCHIVED)
    watch(ROLLOVER, DEC_15 + DAY)
    assert (item("rollover:2027").state, item("rollover:2027").close_reason) == (
        "closed",
        "ActivityInfo was set to Archived",
    )
    assert item("rollover:2027:population").state == "open"  # archiving never drops the population reminder


def test_the_rollover_closes_when_the_import_is_switched_off():
    world("in_use")
    watch(ROLLOVER, JAN_2)
    job = ScheduledJob.objects.get(command="ai_data")
    job.enabled = False
    job.save()
    watch(ROLLOVER, JAN_2 + DAY, mode=QUICK)
    assert item("rollover:2027").close_reason == "the ActivityInfo import was switched off"
