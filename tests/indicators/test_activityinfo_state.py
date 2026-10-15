"""ActivityInfo in use or archived (neurodb.indicators.services.activityinfo): the three rules, the reasons and
their explanation, where the state is kept and what drops it."""

import datetime
import itertools
import re
import zoneinfo
from io import StringIO

import pytest
import time_machine
from django.core.cache import cache
from django.core.management import call_command
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from neurodb.core import years
from neurodb.core.models import ActivityInfoSetting, ScheduledJob, SyncRun
from neurodb.facts.models import ActivityReportNew
from neurodb.indicators.models import Database, ReportingYear
from neurodb.indicators.services import activityinfo
from neurodb.indicators.services.activityinfo import ARCHIVED, AUTO, IN_USE

pytestmark = pytest.mark.django_db

SITE_NOW = datetime.datetime(2026, 10, 15, 9, 0, tzinfo=zoneinfo.ZoneInfo("Asia/Beirut"))  # tests/conftest.py
OCT_15, JAN_5, FEB_1 = datetime.date(2026, 10, 15), datetime.date(2027, 1, 5), datetime.date(2027, 2, 1)
# what the 2026 databases hold: records up to August (recent on 15 October), up to February (stale), up to
# December (the import of 1-22 January), no database at all, or databases waiting for their first record
RECORDS = {"recent": "2026-08", "stale": "2026-02", "december": "2026-12", "none": None, "empty": None}
_ids = itertools.count(900001)


def _year(name, *, current=False, year=None):
    return ReportingYear.objects.create(name=name, year=name[:4] if year is None else year, current=current)


def _database(reporting_year, *, display=True, months=()):
    db = Database.objects.create(
        ai_id=next(_ids),
        name=f"Database {reporting_year.name}",
        username="",
        password="",
        reporting_year=reporting_year,
        display=display,
    )
    for month in months:
        ActivityReportNew.objects.create(dbase=db, month_name=f"{month}-01", month=month, indicator_value=1)
    return db


def _world(records):
    """The 2026 reporting year (ticked current) with its database as ``records`` says."""
    ry = _year("2026", current=True)
    if records != "none":
        _database(ry, months=[RECORDS[records]] if RECORDS[records] else [])
    return ry


def _schedule(on):
    ScheduledJob.objects.filter(command="ai_data").update(enabled=on)  # no signal
    activityinfo.invalidate()


def _mode(mode):
    ActivityInfoSetting.objects.update_or_create(pk=1, defaults={"mode": mode})
    activityinfo.invalidate()


# On Automatic with the import switched on: (in use, watched, reason) by day and records. With the import
# off, Automatic is always archived and not watched ("auto_schedule_off").
AUTO_ON = {
    (OCT_15, "recent"): (True, True, "auto_in_use"),  # August is 2 months back
    (OCT_15, "stale"): (False, False, "auto_not_recent"),  # February is 8 months back
    (OCT_15, "december"): (True, True, "auto_in_use"),
    (OCT_15, "none"): (False, False, "auto_no_records"),
    (OCT_15, "empty"): (False, True, "auto_no_records"),  # watched: the databases wait for records
    (JAN_5, "recent"): (False, False, "auto_not_recent"),  # August 2026 is 5 months before January 2027
    (JAN_5, "stale"): (False, False, "auto_not_recent"),
    (JAN_5, "december"): (True, True, "auto_january"),  # the import still brings in December 2026
    (JAN_5, "none"): (False, False, "auto_no_records"),
    (JAN_5, "empty"): (False, False, "auto_no_records"),  # 2026 ticked current is not this year any more
    (FEB_1, "recent"): (False, False, "auto_no_records"),  # 2027 has no records; January is over
    (FEB_1, "stale"): (False, False, "auto_no_records"),
    (FEB_1, "december"): (False, False, "auto_no_records"),
    (FEB_1, "none"): (False, False, "auto_no_records"),
    (FEB_1, "empty"): (False, False, "auto_no_records"),
}


@pytest.mark.parametrize("mode", [AUTO, IN_USE, ARCHIVED])
@pytest.mark.parametrize("records", list(RECORDS))
@pytest.mark.parametrize("schedule_on", [True, False])
@pytest.mark.parametrize("day", [OCT_15, JAN_5, FEB_1])
def test_the_rules(site_today, mode, records, schedule_on, day):
    site_today.set(day)
    _world(records)
    _schedule(schedule_on)
    _mode(mode)
    st = activityinfo.state()
    if mode == IN_USE:
        expected = (True, True, "set_in_use")
    elif mode == ARCHIVED:
        expected = (False, False, "set_archived")  # never watched, even while databases wait for records
    elif not schedule_on:
        expected = (False, False, "auto_schedule_off")
    else:
        expected = AUTO_ON[(day, records)]
    assert (st.in_use, st.watched, st.reason) == expected
    assert (activityinfo.in_use(), activityinfo.watched()) == expected[:2]
    assert st.mode == mode and st.schedule_on is schedule_on and st.this_year == day.year
    # Rule 1 never depends on the mode or the schedule: a year with records is an ActivityInfo year
    with_records = records in ("recent", "stale", "december")
    assert st.years == ((2026,) if with_records else ())
    assert activityinfo.is_year(2026) is with_records and not activityinfo.is_year(2027)
    assert st.span == ("2026" if with_records else "")
    assert st.setup_pending is (records == "empty" and day == OCT_15)
    assert st.current_name == "2026"


@pytest.mark.parametrize(
    "day, records, serving, latest, recent",
    [
        (OCT_15, "recent", 2026, "2026-08", True),
        (OCT_15, "stale", 2026, "2026-02", False),
        (JAN_5, "december", 2026, "2026-12", True),
        (JAN_5, "recent", 2026, "2026-08", False),
        (FEB_1, "december", None, "", False),
        (OCT_15, "empty", None, "", False),
    ],
)
def test_serving_year_and_latest_month(site_today, day, records, serving, latest, recent):
    site_today.set(day)
    _world(records)
    st = activityinfo.state()
    assert (st.serving, st.latest_month, st.recent) == (serving, latest, recent)


def test_three_months_is_the_limit(site_today):
    _world("recent")  # records up to August 2026
    site_today.set(datetime.date(2026, 11, 30))
    assert activityinfo.state().in_use  # August is 3 months before November
    site_today.set(datetime.date(2026, 12, 1))  # a new month: a new key, nothing to drop
    assert activityinfo.state().reason == "auto_not_recent"


def test_hierarchy_records_of_january_and_february_are_archived_on_the_suites_day(hierarchy):
    st = activityinfo.state()
    assert st.years == (2026,) and st.latest_month == "2026-02"
    assert (st.in_use, st.watched, st.reason) == (False, False, "auto_not_recent")


def test_the_fixtures_set_the_mode(hierarchy, activityinfo_in_use):
    assert activityinfo.in_use() and activityinfo.watched()
    _mode(ARCHIVED)
    assert not activityinfo.in_use() and activityinfo.state().years == (2026,)  # history stays


def test_activityinfo_archived_fixture(activityinfo_archived):
    _world("recent")
    assert activityinfo.state().reason == "set_archived"


def test_a_returning_year_is_watched_before_its_first_records(site_today):
    # 2025 was the last ActivityInfo year; 2026 is created, ticked current, and gets its databases
    _database(_year("2025"), months=["2025-11"])
    ry = _year("2026", current=True)
    _database(ry)
    st = activityinfo.state()
    assert st.years == (2025,) and st.serving is None and st.setup_pending
    assert (st.in_use, st.watched, st.reason) == (False, True, "auto_no_records")
    # the import is switched off: nobody runs it, nobody is told
    _schedule(False)
    assert not activityinfo.state().watched
    _schedule(True)
    # the first records arrive (the import's end drops the state)
    ActivityReportNew.objects.create(dbase=Database.objects.get(reporting_year=ry), month_name="2026-09-01")
    activityinfo.invalidate()
    st = activityinfo.state()
    assert st.years == (2026, 2025) and st.span == "2025–2026" and not st.setup_pending
    assert (st.in_use, st.watched, st.reason) == (True, True, "auto_in_use")


def test_a_database_hidden_from_the_dashboard_does_not_make_a_year():
    ry = _year("2026", current=True)
    _database(ry, display=False, months=["2026-09"])
    st = activityinfo.state()
    assert st.years == () and st.reason == "auto_no_records"
    assert not st.setup_pending  # no database shown: nothing waits


def test_several_reporting_years_of_one_year_the_current_one_else_the_highest_name():
    first = _year("2025 (first)", year="2025")
    second = _year("2025 (second)", year="2025")
    _year("2025 (empty)", year="2025")
    _database(first, months=["2025-05"])
    _database(second, months=["2025-04"])
    st = activityinfo.state()
    assert st.names == {2025: "2025 (second)"} and st.pks == {2025: second.pk}
    first.current = True
    first.save()
    st = activityinfo.state()
    assert st.names == {2025: "2025 (first)"} and st.current_name == "2025 (first)"
    assert activityinfo.reporting_year_for(2025) == first
    assert activityinfo.reporting_year_for("2025") == first
    assert activityinfo.reporting_year_for(2026) is None and activityinfo.reporting_year_for("x") is None


def test_the_year_of_a_reporting_year_comes_from_its_year_then_its_name():
    _database(_year("Monthly results", year="2024"), months=["2024-03"])
    _database(_year("2023 results", year=""), months=["2023-03"])
    _database(_year("Old results", year=""), months=["2019-03"])  # no year to read: not counted
    st = activityinfo.state()
    assert st.years == (2024, 2023) and st.first == 2023 and st.last == 2024 and st.span == "2023–2024"


def test_months_that_are_not_months_are_ignored(site_today):
    ry = _year("2026", current=True)
    db = _database(ry, months=["2026-09"])
    ActivityReportNew.objects.create(dbase=db, month_name="September")  # sorts after every digit
    assert activityinfo.state().latest_month == "2026-09"
    ActivityReportNew.objects.filter(month_name__startswith="2026").update(month_name="Sept 2026")
    activityinfo.invalidate()
    st = activityinfo.state()
    assert st.years == (2026,) and st.latest_month == "" and st.reason == "auto_not_recent"
    assert activityinfo.explain(st)[0] == (
        "Archived (automatic): the newest ActivityInfo records have no month NeuroDB can read. It turns in "
        "use by itself when newer records are imported."
    )


def test_a_renamed_import_job_still_counts():
    _world("recent")
    job = ScheduledJob.objects.get(key="activityinfo-data")
    job.key = "monthly-results"
    job.save()
    assert activityinfo.state().schedule_on and activityinfo.in_use()
    job.enabled = False
    job.save()
    assert not activityinfo.state().schedule_on
    # another switched-on job that runs the data import, under any key
    ScheduledJob.objects.create(key="ai-again", command="ai_data", schedule="0 3 * * *", enabled=True)
    assert activityinfo.state().schedule_on
    # the structure import is not the data import; an unknown command is ignored
    ScheduledJob.objects.filter(key="ai-again").delete()
    ScheduledJob.objects.filter(key="activityinfo-structure").update(enabled=True)
    ScheduledJob.objects.create(key="gone", command="no_such_command", schedule="0 3 * * *", enabled=True)
    assert not activityinfo.state().schedule_on


@pytest.mark.parametrize(
    "setup, expected",
    [
        (
            dict(day=OCT_15, records="recent"),
            "In use (automatic): 2026 has ActivityInfo records up to Aug 2026 and the ActivityInfo import is "
            "switched on.",
        ),
        (
            dict(day=JAN_5, records="december"),
            "In use (automatic): in January the import still brings in the December 2026 results.",
        ),
        (
            dict(day=OCT_15, records="empty"),
            "Archived (automatic): 2026 has no ActivityInfo records. It turns in use by itself when the first "
            "2026 records are imported.",
        ),
        (
            dict(day=OCT_15, records="stale"),
            "Archived (automatic): the newest ActivityInfo records are for Feb 2026, more than three months "
            "ago. It turns in use by itself when newer records are imported.",
        ),
        (
            dict(day=OCT_15, records="recent", schedule_on=False),
            "Archived (automatic): the ActivityInfo import is switched off in Scheduled jobs.",
        ),
        (dict(day=OCT_15, records="none", mode=IN_USE), "In use (set by an administrator)."),
        (dict(day=OCT_15, records="recent", mode=ARCHIVED), "Archived (set by an administrator)."),
    ],
)
def test_explain_first_line(site_today, setup, expected):
    site_today.set(setup["day"])
    _world(setup["records"])
    _schedule(setup.get("schedule_on", True))
    _mode(setup.get("mode", AUTO))
    assert activityinfo.explain()[0] == expected


def test_explain_other_lines(site_today):
    _world("recent")
    assert activityinfo.explain()[1:] == [
        "Years with ActivityInfo records: 2026.",
        "The ActivityInfo import is on. It imports the reporting year ticked current: 2026.",
    ]
    _database(_year("2019"), months=["2019-05"])
    _schedule(False)
    assert activityinfo.explain()[1:] == [
        "Years with ActivityInfo records: 2019–2026.",
        "The ActivityInfo import is off. It imports the reporting year ticked current: 2026.",
    ]
    ReportingYear.objects.update(current=False)
    activityinfo.invalidate()
    assert activityinfo.explain()[2].endswith("ticked current: none.")


def test_explain_says_when_new_databases_wait_for_their_first_records():
    _world("empty")
    lines = activityinfo.explain()
    assert lines[1] == "Years with ActivityInfo records: none."
    assert (
        lines[3]
        == "The 2026 ActivityInfo databases wait for their first records: the import's alarms stay on."
    )
    _schedule(False)  # the alarms are not on: the line would not be true
    assert len(activityinfo.explain()) == 3


# ------------------------------------------------------------------------------------------- storage
def test_four_queries_cold_none_warm_and_plain_data():
    _world("recent")
    with CaptureQueriesContext(connection) as queries:
        st = activityinfo.state()
    assert len(queries) == 4
    with CaptureQueriesContext(connection) as queries:
        assert activityinfo.state() == st
    assert len(queries) == 0
    stored = cache.get(activityinfo.CACHE_KEY.format(year=2026, month=10))
    assert isinstance(stored, dict) and activityinfo.State(**stored) == st

    def plain(value):
        if isinstance(value, dict):
            return all(isinstance(k, int | str) and plain(v) for k, v in value.items())
        if isinstance(value, tuple | list):
            return all(plain(v) for v in value)
        return value is None or isinstance(value, bool | int | str)

    assert plain(stored)
    assert stored["names"] == {2026: "2026"} and stored["years"] == (2026,)


def test_three_queries_when_no_year_is_served():
    _world("none")
    with CaptureQueriesContext(connection) as queries:
        activityinfo.state()
    assert len(queries) == 3


def test_kept_30_seconds_then_read_again(site_today):
    _world("recent")
    assert activityinfo.CACHE_SECONDS == 30
    assert activityinfo.in_use()
    ScheduledJob.objects.filter(command="ai_data").update(enabled=False)  # no signal: still kept
    site_today.set(site_today.now + datetime.timedelta(seconds=20))
    assert activityinfo.in_use()
    site_today.set(site_today.now + datetime.timedelta(seconds=31))
    assert not activityinfo.in_use()


def test_read_once_per_request():
    from neurodb.core import compute

    _world("recent")
    compute.request_started()
    try:
        first = activityinfo.state()
        _mode(ARCHIVED)  # this request keeps what it read
        assert activityinfo.state() is first
    finally:
        compute.request_finished()
    assert activityinfo.state().reason == "set_archived"


# --------------------------------------------------------------------------------------- invalidation
@pytest.fixture
def admin_client(client, admin_user):
    client.force_login(admin_user)
    return client


def test_the_setting_page_drops_the_state(admin_client, admin_user):
    _world("recent")
    assert activityinfo.in_use()
    response = admin_client.get(reverse("admin:core_activityinfosetting_changelist"))
    setting = ActivityInfoSetting.objects.get()  # opening the page makes the row, Automatic
    assert setting.mode == AUTO
    assert response.status_code == 302
    assert response["Location"] == reverse("admin:core_activityinfosetting_change", args=[setting.pk])
    url = reverse("admin:core_activityinfosetting_change", args=[setting.pk])
    html = admin_client.get(url).content.decode()
    assert "Automatic (recommended)" in html and "In use (automatic): 2026 has ActivityInfo records" in html
    assert _readonly(html, "Years with ActivityInfo records") == "2026"  # the span alone (a11y_words N-1)
    assert "The ActivityInfo import is on." in html
    assert admin_client.post(url, {"mode": ARCHIVED}).status_code == 302
    setting.refresh_from_db()
    assert setting.mode == ARCHIVED and setting.updated_by == admin_user
    assert activityinfo.state().reason == "set_archived"  # at once, on this worker
    assert "Archived (set by an administrator)." in admin_client.get(url).content.decode()


def _readonly(html: str, label: str) -> str:
    """The text of the read-only field labelled ``label`` on an admin change page."""
    found = re.search(
        rf">{re.escape(label)}</label>.*?<div class=\"readonly[^\"]*\">(.*?)</div>", html, flags=re.DOTALL
    )
    assert found, f"no read-only field {label!r}"
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", found[1])).strip()


def test_the_setting_page_explains_what_is_stored_whatever_this_worker_keeps(admin_client, monkeypatch):
    """Another worker saved the setting: its ``invalidate()`` never reaches this worker, which keeps the old
    state for up to 30 seconds. The page where the mode is set still explains the mode just saved
    (time_roles F6), reading the state once for its three lines; the rest of the site follows within 30
    seconds, this worker's at once."""
    _world("recent")
    _mode(ARCHIVED)
    assert activityinfo.state().reason == "set_archived"  # kept on this worker
    ActivityInfoSetting.objects.filter(pk=1).update(mode=AUTO)  # saved on another worker
    assert activityinfo.state().reason == "set_archived"  # still kept here
    computed = []
    compute_state = activityinfo.compute_state
    monkeypatch.setattr(activityinfo, "compute_state", lambda: computed.append(1) or compute_state())
    html = admin_client.get(reverse("admin:core_activityinfosetting_change", args=[1])).content.decode()
    assert _readonly(html, "Now") == (
        "In use (automatic): 2026 has ActivityInfo records up to Aug 2026 and the ActivityInfo import is "
        "switched on."
    )
    assert "Archived (set by an administrator)" not in html
    assert _readonly(html, "Years with ActivityInfo records") == "2026"
    assert _readonly(html, "ActivityInfo import").startswith("The ActivityInfo import is on.")
    assert len(computed) == 1  # once for the page's three lines
    assert activityinfo.state().reason == "auto_in_use"  # this worker keeps what the page read


def test_the_setting_page_names_its_mode_once_in_the_crumbs_and_the_title(admin_client):
    """The crumbs read "… › ActivityInfo: in use or archive › Archived" and the title names the page once,
    not "ActivityInfo: in use or archive" twice (a11y_words N-1); no year with records reads "none"."""
    _mode(ARCHIVED)
    html = admin_client.get(reverse("admin:core_activityinfosetting_change", args=[1])).content.decode()
    crumbs = dict(
        re.findall(r'<a href="(/manage/core/activityinfosetting/[^"]*)"[^>]*>\s*(.*?)\s*</a\s*>', html)
    )
    assert crumbs == {
        "/manage/core/activityinfosetting/": "ActivityInfo: in use or archive",
        "/manage/core/activityinfosetting/1/change/": "Archived",
    }
    title = re.search(r"<title>(.*?)</title>", html, flags=re.DOTALL)[1]
    assert title.count("ActivityInfo: in use or archive") == 1, title
    assert str(ActivityInfoSetting.objects.get(pk=1)) == "Archived"
    assert _readonly(html, "Years with ActivityInfo records") == "none"


def test_the_setting_page_has_one_row_and_is_for_administrators(admin_client, client, viewer):
    assert admin_client.get(reverse("admin:core_activityinfosetting_add")).status_code == 403
    setting = ActivityInfoSetting.load()
    delete = reverse("admin:core_activityinfosetting_delete", args=[setting.pk])
    assert admin_client.post(delete, {"post": "yes"}).status_code == 403
    assert ActivityInfoSetting.objects.count() == 1
    viewer.is_staff = True  # a Viewer in the admin has no permission on it
    viewer.save()
    client.force_login(viewer)
    assert client.get(reverse("admin:core_activityinfosetting_changelist")).status_code == 403


def test_the_admin_home_tile_and_the_reporting_setup_row(admin_client):
    from neurodb.web import admin_site

    _world("stale")
    setup = next(models for name, _, models in admin_site.GROUPS if str(name) == "Reporting setup")
    assert setup[0] == "core.ActivityInfoSetting"
    html = admin_client.get(reverse("admin:index")).content.decode()
    assert "This year" in html and "Current year" not in html
    assert "Archived (automatic): the newest ActivityInfo records are for Feb 2026" in html
    assert reverse("admin:core_activityinfosetting_changelist") in html
    assert "ActivityInfo: in use or archive" in html


def test_saving_a_scheduled_job_drops_the_state():
    _world("recent")
    assert activityinfo.in_use()
    job = ScheduledJob.objects.get(key="activityinfo-data")
    job.enabled = False
    job.save()
    assert not activityinfo.in_use()
    job.enabled = True
    job.save(update_fields=["enabled"])
    assert activityinfo.in_use()
    job.delete()
    assert not activityinfo.in_use()


def test_the_schedulers_own_saves_do_not_drop_the_state():
    _world("recent")
    assert activityinfo.in_use()
    job = ScheduledJob.objects.get(key="activityinfo-data")
    ScheduledJob.objects.filter(pk=job.pk).update(enabled=False)  # behind the state's back
    job.refresh_from_db()
    job.next_run_at = timezone.now()
    job.save(update_fields=["next_run_at"])  # core/scheduler.py: planning the next run
    job.save(update_fields=["next_run_at", "last_started_at", "last_outcome"])  # starting it
    job.save(update_fields=["last_outcome"])  # a start that failed
    assert activityinfo.in_use()  # still the kept state: none of these is an input
    job.save(update_fields=["enabled", "next_run_at"])
    assert not activityinfo.in_use()


def test_switching_jobs_off_in_bulk_drops_the_state(admin_client):
    _world("recent")
    assert activityinfo.in_use()
    job = ScheduledJob.objects.get(key="activityinfo-data")
    response = admin_client.post(
        reverse("admin:core_scheduledjob_changelist"),
        {"action": "disable_jobs", "_selected_action": [job.pk]},
    )
    assert response.status_code == 302
    assert not ScheduledJob.objects.get(pk=job.pk).enabled
    assert not activityinfo.in_use()


def test_reporting_years_and_databases_drop_the_state():
    ry = _world("recent")
    assert activityinfo.state().years == (2026,)
    db = Database.objects.get(reporting_year=ry)
    db.display = False
    db.save()
    assert activityinfo.state().years == ()
    db.display = True
    db.save()
    assert activityinfo.state().years == (2026,)
    ry.name = ry.year = "2025"
    ry.save()
    assert activityinfo.state().years == (2025,)
    db.delete()
    assert activityinfo.state().years == ()
    _database(_year("2024"), months=["2024-01"])
    assert activityinfo.state().years == (2024,)
    ReportingYear.objects.get(name="2024").delete()
    assert activityinfo.state().years == ()


def test_the_end_of_an_import_drops_the_state(monkeypatch):
    import neurodb.integrations.management.commands.import_activityinfo_data as command_module

    ry = _world("empty")
    assert not activityinfo.state().years

    def import_data(database, run, keep_copy):  # the records arrive without any database being saved
        ActivityReportNew.objects.create(dbase=database, month_name="2026-09-01")
        run.finish(SyncRun.Status.SUCCEEDED)

    def link_partners(triggered_by):
        run = SyncRun.objects.create(job=SyncRun.Job.PARTNER_LINKS)
        run.finish(SyncRun.Status.SUCCEEDED)
        return run

    monkeypatch.setattr(command_module, "import_data", import_data)
    monkeypatch.setattr(command_module, "link_activityinfo_partners", link_partners)
    call_command("import_activityinfo_data", current_year=True, stdout=StringIO(), stderr=StringIO())
    assert Database.objects.get(reporting_year=ry).activityreportnew_set.exists()
    st = activityinfo.state()
    assert st.years == (2026,) and st.in_use


# ------------------------------------------------------------------------------------------ the seam
def test_the_suites_clocks_agree(site_today):
    assert timezone.localdate() == datetime.date.today() == years.today() == datetime.date(2026, 10, 15)
    now = timezone.localtime()
    assert (now.date(), now.hour) == (datetime.date(2026, 10, 15), 9)
    site_today.set(datetime.date(2027, 1, 5))
    assert timezone.localdate() == datetime.date.today() == years.today() == datetime.date(2027, 1, 5)
    # 23:30 on 31 December in Beirut is still 31 December for every clock, though UTC says 21:30
    site_today.set(datetime.datetime(2026, 12, 31, 23, 30))
    assert timezone.localdate() == datetime.date.today() == years.today() == datetime.date(2026, 12, 31)


def test_every_test_travels(site_today):
    assert time_machine.escape_hatch.is_travelling() and site_today.now == SITE_NOW


@pytest.mark.real_clock
def test_a_real_clock_test_keeps_the_real_clock(site_today):
    assert site_today is None and not time_machine.escape_hatch.is_travelling()
