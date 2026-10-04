"""NeuroDB Watch's concern imports: the daily review's open findings and the year-end forecasts likely to
fall short, brought into the same memory under their existing keys."""

import datetime
import itertools
import json

import pytest

from neurodb.accounts.models import Section
from neurodb.core.models import SyncRun
from neurodb.indicators.models import MasterIndicator
from neurodb.insights.models import IndicatorForecast
from neurodb.partnerships.models import PCA, PartnerOrganization
from neurodb.review.models import DailyReview, FindingAssignment, ReviewFinding
from neurodb.watch import detectors, memory
from neurodb.watch.detectors import DAILY, QUICK, Context, concerns, deadlines
from neurodb.watch.models import DetectorSetting, SectionMatch, WatchItem

pytestmark = pytest.mark.django_db

UTC = datetime.UTC
NOW = datetime.datetime(2026, 10, 5, 6, 0, tzinfo=UTC)  # 09:00 in Beirut
TODAY = datetime.date(2026, 10, 5)
PD_NUMBER = "LEB/PCA2026001/PD2026001"
OVERDUE = f"reports_overdue:{PD_NUMBER}"
ENDING = f"pd_ending_soon:{PD_NUMBER}"
REVIEW = concerns.REVIEW_IMPORT
FORECASTS = concerns.FORECAST_SHORT
_ids = itertools.count(1)


def days(n: int) -> datetime.date:
    return TODAY + datetime.timedelta(days=n)


def succeeded(job: str, day: float = 0, **details) -> SyncRun:
    """A successful run of ``job`` one hour before the pass of ``day``."""
    at = NOW + datetime.timedelta(days=day, hours=-1)
    return SyncRun.objects.create(
        job=job,
        status=SyncRun.Status.SUCCEEDED,
        started_at=at - datetime.timedelta(minutes=10),
        finished_at=at,
        details=details,
    )


def finding(key: str, severity: str = "warning", section: str = "Education", **fields) -> dict:
    values = {
        "key": key,
        "check_id": key.split(":")[0],
        "severity": severity,
        "section": section,
        "title": f"Finding {key}",
        "detail": "",
        "evidence": {"read": "Look at it.", "records": [f"record of {key}"], "numbers": {"count": 1}},
        "url": "/programmes/1/detail/",
        "state": "new",
    }
    values.update(fields)
    return values


def reviewed(day: int = 0, *findings: dict, status: str = "succeeded", sync: bool = True) -> DailyReview:
    """The daily review of ``day`` with these findings, and its successful run (unless ``sync`` is off)."""
    review = DailyReview.objects.create(
        date=days(day), status=status, finished_at=NOW + datetime.timedelta(days=day, hours=-1)
    )
    for rank, fields in enumerate(findings):
        ReviewFinding.objects.create(review=review, rank=rank, **fields)
    if sync and status == "succeeded":
        succeeded(SyncRun.Job.DAILY_REVIEW, day)
    return review


def run(day: float = 0, mode: str = DAILY, checks=(REVIEW,)) -> memory.Outcome:
    return memory.run(Context.make(mode, now=NOW + datetime.timedelta(days=day)), list(checks))


def item(key: str) -> WatchItem:
    return WatchItem.objects.get(key=key)


def keys(detector: str) -> set[str]:
    return set(WatchItem.objects.filter(detector=detector).values_list("key", flat=True))


def stories(key: str) -> list[str]:
    return [line["text"] for line in item(key).story]


@pytest.fixture
def education(db):
    section = Section.objects.create(name="Education", code="EDU")
    SectionMatch.objects.create(
        etools_name="Education", section=section, how=SectionMatch.How.EXACT, confirmed=True
    )
    return section


# ---------------------------------------------------------------------------- the checks
def test_the_imports_are_registered_the_review_on_and_the_forecast_in_trial():
    assert detectors.get("daily_review") is REVIEW
    assert detectors.get("forecast_short") is FORECASTS
    assert REVIEW.source_jobs == (SyncRun.Job.DAILY_REVIEW,)
    assert REVIEW.default_mode == detectors.ON
    assert FORECASTS.source_jobs == (SyncRun.Job.FORECAST,)
    assert FORECASTS.default_mode == detectors.TRIAL
    assert FORECASTS.max_age_hours == 8 * 24
    for check in (REVIEW, FORECASTS):
        assert "detector" not in check.label.lower()
    run(checks=(REVIEW, FORECASTS))
    assert DetectorSetting.objects.get(detector="daily_review").mode == DetectorSetting.Mode.ON
    assert DetectorSetting.objects.get(detector="forecast_short").mode == DetectorSetting.Mode.TRIAL


def test_the_keys_of_the_imports():
    assert concerns.review_item_key(OVERDUE) == f"review:{OVERDUE}"
    assert concerns.finding_key_of(f"review:{OVERDUE}") == OVERDUE
    assert concerns.finding_key_of(f"due:pd_end:{PD_NUMBER}") == ""
    assert concerns.forecast_item_key(2026, 12) == "forecast:2026:12"


# ---------------------------------------------------------------------------- the daily review
def test_open_critical_and_warning_findings_are_imported_under_their_keys(education):
    reviewed(
        0,
        finding(
            OVERDUE,
            title=f"2 progress reports overdue for {PD_NUMBER} (Amel Association)",
            detail="The oldest was due on 12 Sep 2026, 23 days ago, and has not been submitted.",
            evidence={
                "read": "Ask the partner for the report.",
                "records": ["QPR3: due 2026-09-12", "QPR4: due 2026-09-30"],
                "numbers": {"reports": 2, "oldest_due": "2026-09-12", "days_overdue": 23, "children": None},
            },
        ),
        finding("action_points_overdue:Education:Amel Association", "critical"),
        finding("findings_off_track:Amel Association", section=""),
        finding("spending_ahead:LEB/PD9", "good", state="resolved"),
        finding("under_disbursed:LEB/PD8", "warning", state="resolved"),
        finding("tpm_reports_late:77", "info"),
        finding("sync_failures:etools", "critical", section=""),
        finding("data_quality:children", section=""),
        finding("stale_sources:ai", section=""),
        finding("locations_unplaced:3", section=""),
        finding("improvements:LEB/PD7:Education"),
    )
    outcome = run()
    assert keys("daily_review") == {
        f"review:{OVERDUE}",
        "review:action_points_overdue:Education:Amel Association",
        "review:findings_off_track:Amel Association",
    }
    assert len(outcome.new) == 3
    found = item(f"review:{OVERDUE}")
    assert found.kind == WatchItem.Kind.CONCERN
    assert found.severity == WatchItem.Severity.WARNING
    assert found.confidence == WatchItem.Confidence.SURE
    assert found.title == f"2 progress reports overdue for {PD_NUMBER} (Amel Association)"
    assert found.detail == "The oldest was due on 12 Sep 2026, and has not been submitted."
    assert found.due_date is None
    assert found.etools_sections == ["Education"]
    assert found.section_ids == [education.pk]
    assert found.scope == WatchItem.Scope.SECTION
    assert (found.entity_kind, found.entity_key) == ("review_finding", OVERDUE)
    assert found.url == "/programmes/1/detail/"
    assert found.source_mark == TODAY.isoformat()
    assert found.evidence["source"] == "the daily review"
    assert found.evidence["source_job"] == SyncRun.Job.DAILY_REVIEW
    assert found.evidence["synced_at"]
    assert found.evidence["review_date"] == TODAY.isoformat()
    assert found.evidence["read"] == "Ask the partner for the report."
    assert [r["label"] for r in found.evidence["records"]] == ["QPR3: due 2026-09-12", "QPR4: due 2026-09-30"]
    assert found.evidence["records"][0]["url"] == "/programmes/1/detail/"
    assert found.evidence["numbers"] == {"reports": 2, "oldest_due": "2026-09-12", "days_overdue": 23}
    assert found.has_owner is False and found.assignment_status == ""
    assert item("review:action_points_overdue:Education:Amel Association").severity == "critical"
    country = item("review:findings_off_track:Amel Association")
    assert (country.scope, country.etools_sections, country.section_ids) == (WatchItem.Scope.COUNTRY, [], [])


def test_the_latest_succeeded_review_up_to_today_is_read(education):
    reviewed(-1, finding("not_reported:LEB/PD1:Education"))
    reviewed(0, finding("not_reported:LEB/PD2:Education"), status="failed")
    reviewed(1, finding("not_reported:LEB/PD3:Education"), sync=False)  # dated tomorrow
    run()
    assert keys("daily_review") == {"review:not_reported:LEB/PD1:Education"}
    assert item("review:not_reported:LEB/PD1:Education").source_mark == days(-1).isoformat()


def test_a_finding_without_records_is_evidenced_by_its_title():
    reviewed(
        0, finding(ENDING, evidence={"records": [], "numbers": {"days_left": 30, "achieved_percent": None}})
    )
    run()
    found = item(f"review:{ENDING}")
    assert [r["label"] for r in found.evidence["records"]] == [f"Finding {ENDING}"]
    assert found.evidence["numbers"] == {"days_left": 30}


def test_day_counts_in_the_review_text_become_dates():
    on = datetime.date(2026, 10, 5)
    assert concerns.dated("LEB/PD1 (Amel) ends in 30 days at 50 % of target", on) == (
        "LEB/PD1 (Amel) ends on 4 Nov 2026 at 50 % of target"
    )
    assert concerns.dated("LEB/PD1 (Amel) ends in 1 day with no reported value", on) == (
        "LEB/PD1 (Amel) ends on 6 Oct 2026 with no reported value"
    )
    assert concerns.dated("TPM report late: TPM/1, visit ended 20 days ago", on) == (
        "TPM report late: TPM/1, visit ended on 15 Sep 2026"
    )
    assert concerns.dated("It started on 01 Jan 2026, 277 days ago, and no report was read.", on) == (
        "It started on 01 Jan 2026, and no report was read."
    )
    unchanged = "3 field monitoring findings off track in the last 30 days; reports within 14 days"
    assert concerns.dated(unchanged, on) == unchanged


def test_an_imported_title_keeps_its_date_from_one_day_to_the_next():
    reviewed(0, finding(ENDING, title=f"{PD_NUMBER} (Amel) ends in 30 days at 50 % of target"))
    run()
    reviewed(1, finding(ENDING, title=f"{PD_NUMBER} (Amel) ends in 29 days at 50 % of target"))
    run(day=1)
    assert item(f"review:{ENDING}").title == f"{PD_NUMBER} (Amel) ends on 4 Nov 2026 at 50 % of target"


def test_an_assignment_says_whether_someone_owns_it_never_who():
    reviewed(
        0,
        finding(OVERDUE),
        finding("not_reported:LEB/PD2:Education"),
        finding("not_reported:LEB/PD3:Education"),
    )
    FindingAssignment.objects.create(
        key=OVERDUE,
        owner="Ms Rania Planted",
        note="Rania Planted (rania.planted@example.org) will call the partner",
        status=FindingAssignment.Status.ASSIGNED,
        due_date=days(3),
    )
    FindingAssignment.objects.create(
        key="not_reported:LEB/PD2:Education", owner="  ", status=FindingAssignment.Status.ACKNOWLEDGED
    )
    FindingAssignment.objects.create(
        key="not_reported:LEB/PD3:Education", owner="Ms Rania Planted", status=FindingAssignment.Status.CLOSED
    )
    run()
    owned = item(f"review:{OVERDUE}")
    assert (owned.has_owner, owned.assignment_status) == (True, "assigned")
    assert owned.due_date is None  # the agreed date is its own item
    blank = item("review:not_reported:LEB/PD2:Education")
    assert (blank.has_owner, blank.assignment_status) == (False, "acknowledged")
    closed = item("review:not_reported:LEB/PD3:Education")
    assert (closed.has_owner, closed.assignment_status) == (False, "closed")
    stored = json.dumps(list(WatchItem.objects.values()), default=str)
    assert "Rania" not in stored and "rania" not in stored
    assert "will call" not in stored


def test_a_finding_that_got_worse_is_told_as_worse():
    reviewed(0, finding("new_off_track:LEB/PD1:Education"))
    run()
    reviewed(1, finding("new_off_track:LEB/PD1:Education", "critical"))
    outcome = run(day=1)
    assert outcome.worse == ["review:new_off_track:LEB/PD1:Education"]
    assert stories("review:new_off_track:LEB/PD1:Education")[-1] == "Got worse: was warning, now critical"


def test_a_review_rerun_that_drops_a_key_closes_nothing_until_two_newer_reviews_miss_it():
    reviewed(0, finding(OVERDUE), finding("not_reported:LEB/PD2:Education"))
    run()
    DailyReview.objects.filter(date=TODAY).delete()  # the day's review re-run without it
    reviewed(0, finding("not_reported:LEB/PD2:Education"))
    outcome = run(day=0.1)
    assert outcome.missed == []
    assert item(f"review:{OVERDUE}").missed_runs == 0
    reviewed(1, finding(OVERDUE, "good", state="resolved"), finding("not_reported:LEB/PD2:Education"))
    run(day=1, mode=QUICK)
    assert item(f"review:{OVERDUE}").missed_runs == 0  # a quick pass never counts a miss
    run(day=1)
    run(day=1.2)  # the same review again: not a second miss
    found = item(f"review:{OVERDUE}")
    assert (found.state, found.missed_runs) == (WatchItem.State.OPEN, 1)
    reviewed(2, finding("not_reported:LEB/PD2:Education"))
    outcome = run(day=2)
    found = item(f"review:{OVERDUE}")
    assert outcome.gone == [found.key]
    assert (found.state, found.close_reason) == (WatchItem.State.GONE, "No longer in the daily review")
    assert item("review:not_reported:LEB/PD2:Education").state == WatchItem.State.OPEN


def test_a_finding_listed_to_note_only_closes_at_once():
    reviewed(0, finding(OVERDUE))
    run()
    reviewed(1, finding(OVERDUE, "info", state="still_open"))
    outcome = run(day=1, mode=QUICK)
    found = item(f"review:{OVERDUE}")
    assert outcome.closed == [found.key]
    assert (found.state, found.close_reason) == (WatchItem.State.CLOSED, concerns.LOWERED)


def test_a_finding_marked_wrong_stays_hidden_while_only_its_day_counts_move():
    def late(days_since_end: int, elapsed: int, visits: int = 1) -> dict:
        numbers = {"days_since_end": days_since_end, "elapsed_percent": elapsed, "visits_late": visits}
        return finding("tpm_reports_late:77", evidence={"records": ["TPM/77"], "numbers": numbers})

    reviewed(0, late(20, 61))
    run()
    WatchItem.objects.filter(key="review:tpm_reports_late:77").update(state=WatchItem.State.WRONG)
    reviewed(1, late(21, 62))
    run(day=1)
    assert item("review:tpm_reports_late:77").state == WatchItem.State.WRONG
    reviewed(2, late(22, 62, visits=2))
    outcome = run(day=2)
    assert outcome.reopened == ["review:tpm_reports_late:77"]


def test_a_stale_review_imports_nothing_and_closes_nothing():
    reviewed(0, finding(OVERDUE))
    run()
    reviewed(2, sync=False)  # no successful run of the review for two days
    outcome = run(day=2)
    found = item(f"review:{OVERDUE}")
    assert (found.state, found.missed_runs) == (WatchItem.State.OPEN, 0)
    assert outcome.detectors["daily_review"]["skipped"] == f"stale: {SyncRun.Job.DAILY_REVIEW}"
    assert WatchItem.objects.filter(key=f"system:stale:{SyncRun.Job.DAILY_REVIEW}").exists()


# ---------------------------------------------------------------------------- twins with the look-ahead
@pytest.fixture
def ending_pd(db):
    partner = PartnerOrganization.objects.create(
        etl_id="1", name="Amel Association", partner_type="Civil Society Organization", vendor_number="V1"
    )
    return PCA.objects.create(
        etl_id=str(next(_ids)),
        partner=partner,
        partner_name=partner.name,
        number=PD_NUMBER,
        title="Education services",
        status="active",
        start=datetime.date(2026, 1, 1),
        end=days(30),
        section_names=["Education"],
    )


def pd_ending(mode: str) -> None:
    DetectorSetting.objects.update_or_create(detector=deadlines.PD_ENDING.id, defaults={"mode": mode})


BOTH = (REVIEW, deadlines.PD_ENDING)


def test_a_pd_ending_soon_finding_attaches_to_the_watch_pd_item_with_no_duplicate(ending_pd):
    pd_ending(DetectorSetting.Mode.ON)
    succeeded(SyncRun.Job.ETOOLS_DATAMART)
    reviewed(0, finding(ENDING, title=f"{PD_NUMBER} (Amel Association) ends in 30 days at 40 % of target"))
    run(checks=BOTH)
    run(day=0.1, checks=BOTH)
    assert keys("daily_review") == set()
    found = item(f"due:pd_end:{PD_NUMBER}")
    assert found.review_key == ENDING
    assert found.state == WatchItem.State.OPEN
    assert WatchItem.objects.count() == 1


def test_a_finding_twin_of_a_trial_check_is_imported_then_handed_over_once_the_check_is_on(ending_pd):
    succeeded(SyncRun.Job.ETOOLS_DATAMART)
    reviewed(0, finding(ENDING))
    run(checks=BOTH)  # pd_ending in trial: only the whole-country view sees its items
    assert keys("daily_review") == {f"review:{ENDING}"}
    pd_ending(DetectorSetting.Mode.ON)
    outcome = run(day=0.1, checks=BOTH)
    imported = item(f"review:{ENDING}")
    assert outcome.closed == [imported.key]
    assert imported.state == WatchItem.State.CLOSED
    assert (
        imported.close_reason
        == f"followed from now on as: PD ends 4 Nov 2026: {PD_NUMBER} (Amel Association)"
    )
    assert item(f"due:pd_end:{PD_NUMBER}").review_key == ENDING


def test_with_a_stale_datamart_the_finding_attaches_to_the_pd_item_carried_forward(ending_pd):
    pd_ending(DetectorSetting.Mode.ON)
    succeeded(SyncRun.Job.ETOOLS_DATAMART)
    reviewed(0)
    run(checks=BOTH)
    assert item(f"due:pd_end:{PD_NUMBER}").review_key == ""
    reviewed(1, finding(ENDING))
    outcome = run(day=1.3, checks=BOTH)  # the Datamart has not synced for 32 hours
    assert outcome.detectors["pd_ending"]["skipped"].startswith("stale")
    assert keys("daily_review") == set()
    found = item(f"due:pd_end:{PD_NUMBER}")
    assert found.review_key == ENDING
    assert stories(found.key)[-1] == "The daily review now flags it too"


def test_a_reports_overdue_finding_has_no_twin_and_is_imported(ending_pd):
    pd_ending(DetectorSetting.Mode.ON)
    succeeded(SyncRun.Job.ETOOLS_DATAMART)
    reviewed(0, finding(OVERDUE))
    run(checks=BOTH)
    assert keys("daily_review") == {f"review:{OVERDUE}"}  # the due report closed, noting this key


# ---------------------------------------------------------------------------- year-end forecasts
@pytest.fixture
def masters(database):
    return [
        MasterIndicator.objects.create(
            database=database, name=f"Children reached {n}", awp_code=f"1.{n}", aggregation_method="SUM"
        )
        for n in (1, 2)
    ]


def forecast_week(week: int, shown: bool = True, rows=(), day: float | None = None) -> SyncRun:
    """A forecast run ``week`` weeks after today (before the pass of that day) and its rows, replacing
    the earlier ones: (master, status, low, high, section id)."""
    run_ = succeeded(
        SyncRun.Job.FORECAST, week * 7 if day is None else day, year=2026, as_of=8, backtest={"shown": shown}
    )
    IndicatorForecast.objects.all().delete()
    for master, status, low, high, section_id in rows:
        IndicatorForecast.objects.create(
            master=master,
            database=master.database,
            section_id=section_id,
            year=2026,
            as_of_month=8,
            value_to_date=400,
            target=1000,
            forecast=(low + high) / 2,
            low=low,
            high=high,
            linear=800,
            status=status,
            basis="its own past years",
            own_years=3,
            computed_at=run_.finished_at,
        )
    return run_


SHORT, ON_COURSE = IndicatorForecast.Status.SHORT, IndicatorForecast.Status.ON_COURSE


def forecast_key(master) -> str:
    return f"forecast:2026:{master.pk}"


def test_without_shown_forecasts_there_is_no_forecast_item(masters, section):
    forecast_week(0, shown=False, rows=[(masters[0], SHORT, 760, 900, section.pk)])
    run(checks=(FORECASTS,))
    assert keys("forecast_short") == set()


def test_a_forecast_likely_to_fall_short_is_followed_as_an_estimate_with_its_range(masters, section):
    forecast_week(
        0,
        rows=[
            (masters[0], SHORT, 760, 900, section.pk),
            (masters[1], ON_COURSE, 950, 1100, section.pk),
        ],
    )
    run(checks=(FORECASTS,))
    run(day=0.1, checks=(FORECASTS,))
    assert keys("forecast_short") == {forecast_key(masters[0])}
    found = item(forecast_key(masters[0]))
    assert found.kind == WatchItem.Kind.CONCERN
    assert found.severity == WatchItem.Severity.INFO
    assert found.confidence == WatchItem.Confidence.LIKELY
    assert found.title == (
        "Likely to fall short of its 2026 target (estimate): Children reached 1 (Child Protection)"
    )
    assert "Estimate" in found.detail and "likely between 760 and 900" in found.detail
    assert "(76–90% of target)" in found.detail and "months up to August" in found.detail
    assert found.section_ids == [section.pk]
    assert found.etools_sections == []
    assert found.scope == WatchItem.Scope.SECTION
    assert (found.entity_kind, found.entity_key) == ("master_indicator", str(masters[0].pk))
    assert found.url == f"/insights/forecasts/?section={section.pk}&status=likely_short"
    assert found.evidence["source"] == "the year-end forecasts"
    assert found.evidence["records"][0]["value"] == "likely 760 to 900 of 1,000"
    numbers = found.evidence["numbers"]
    assert (numbers["low"], numbers["high"], numbers["target"], numbers["forecast"]) == (760, 900, 1000, 830)
    assert (numbers["low_pct"], numbers["high_pct"]) == (76.0, 90.0)
    assert stories(found.key) == ["First noticed"]
    assert DetectorSetting.objects.get(detector="forecast_short").mode == DetectorSetting.Mode.TRIAL


def test_a_forecast_back_on_course_closes_as_recovered_and_its_story_keeps_the_history(masters, section):
    key = forecast_key(masters[0])
    forecast_week(0, rows=[(masters[0], SHORT, 760, 900, section.pk)])
    run(checks=(FORECASTS,))
    forecast_week(1, rows=[(masters[0], ON_COURSE, 960, 1100, section.pk)])
    outcome = run(day=7, checks=(FORECASTS,))
    found = item(key)
    assert outcome.closed == [key]
    assert found.state == WatchItem.State.CLOSED
    assert found.close_reason.startswith("recovered")
    forecast_week(2, rows=[(masters[0], SHORT, 700, 880, section.pk)])
    outcome = run(day=14, checks=(FORECASTS,))
    found = item(key)
    assert outcome.reopened == [key] and outcome.new == []
    assert found.first_seen_on == TODAY
    assert stories(key) == [
        "First noticed",
        "Closed: recovered: the year-end forecast is on course again",
        "Back again (it closed on 12 Oct 2026)",
    ]


def test_a_forecast_no_longer_short_closes_naming_its_status_and_a_vanished_one_is_missed(masters, section):
    forecast_week(
        0, rows=[(masters[0], SHORT, 760, 900, section.pk), (masters[1], SHORT, 500, 700, section.pk)]
    )
    run(checks=(FORECASTS,))
    forecast_week(1, rows=[(masters[0], IndicatorForecast.Status.UNCERTAIN, 800, 1100, section.pk)])
    run(day=7, checks=(FORECASTS,))
    uncertain, vanished = item(forecast_key(masters[0])), item(forecast_key(masters[1]))
    assert uncertain.close_reason == "no longer likely to fall short (forecast now: Uncertain)"
    assert (vanished.state, vanished.missed_runs) == (WatchItem.State.OPEN, 1)
    run(day=8, checks=(FORECASTS,))  # the same forecast again: not a second miss
    assert item(forecast_key(masters[1])).missed_runs == 1


def test_forecasts_no_longer_shown_close_their_items(masters, section):
    forecast_week(0, rows=[(masters[0], SHORT, 760, 900, section.pk)])
    run(checks=(FORECASTS,))
    forecast_week(1, shown=False, rows=[(masters[0], SHORT, 760, 900, section.pk)])
    run(day=7, checks=(FORECASTS,))
    found = item(forecast_key(masters[0]))
    assert (found.state, found.close_reason) == (WatchItem.State.CLOSED, concerns.NOT_SHOWN)


def test_a_forecast_is_fresh_for_8_days(masters, section):
    forecast_week(0, rows=[(masters[0], SHORT, 760, 900, section.pk)], day=-7)
    run(checks=(FORECASTS,))
    assert keys("forecast_short") == {forecast_key(masters[0])}
    WatchItem.objects.all().delete()
    SyncRun.objects.filter(job=SyncRun.Job.FORECAST).update(finished_at=NOW - datetime.timedelta(days=9))
    outcome = run(checks=(FORECASTS,))
    assert keys("forecast_short") == set()
    assert outcome.detectors["forecast_short"]["skipped"] == f"stale: {SyncRun.Job.FORECAST}"


def test_a_forecast_without_a_section_goes_to_the_whole_country(masters):
    forecast_week(0, rows=[(masters[0], SHORT, 760, 900, None)])
    run(checks=(FORECASTS,))
    found = item(forecast_key(masters[0]))
    assert (found.scope, found.section_ids) == (WatchItem.Scope.COUNTRY, [])
    assert found.url == "/insights/forecasts/?section=&status=likely_short"


def test_a_forecast_of_last_year_closes_when_the_forecasts_move_to_the_new_year(masters, section):
    forecast_week(0, rows=[(masters[0], SHORT, 760, 900, section.pk)])
    run(checks=(FORECASTS,))
    forecast_week(1, rows=[(masters[0], SHORT, 100, 200, section.pk)])
    IndicatorForecast.objects.update(year=2027)
    run(day=7, checks=(FORECASTS,))
    assert item(forecast_key(masters[0])).close_reason == "the forecasts are now for 2027"
    assert item(f"forecast:2027:{masters[0].pk}").state == WatchItem.State.OPEN
