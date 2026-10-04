"""NeuroDB Watch's look-ahead checks on eTools programme data: progress reports and action points due
soon, normal-priority action points overdue, PDs ending soon and PDs awaiting closure."""

import datetime
import itertools
import json
from decimal import Decimal

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from neurodb.accounts.models import Section
from neurodb.core.models import SyncRun
from neurodb.datamart import models as dm
from neurodb.partnerships.models import PCA, PartnerOrganization
from neurodb.review.models import DailyReview, ReviewFinding
from neurodb.watch import detectors, memory
from neurodb.watch.detectors import DAILY, QUICK, Context, deadlines
from neurodb.watch.models import DetectorSetting, SectionMatch, WatchItem

pytestmark = pytest.mark.django_db

UTC = datetime.UTC
NOW = datetime.datetime(2026, 10, 5, 6, 0, tzinfo=UTC)  # 09:00 in Beirut
TODAY = datetime.date(2026, 10, 5)
PD_NUMBER = "LEB/PCA2026001/PD2026001"
DATAMART = SyncRun.Job.ETOOLS_DATAMART
CHECKS = [
    deadlines.REPORT_DUE_SOON,
    deadlines.ACTION_POINT_DUE,
    deadlines.ACTION_POINTS_OVERDUE_NORMAL,
    deadlines.PD_ENDING,
    deadlines.PD_AWAITING_CLOSURE,
]
PEOPLE = ("Zelda", "Walid", "Umar")  # planted in the fields that hold a person, and in free text
_ids = itertools.count(1)


def days(n: int) -> datetime.date:
    return TODAY + datetime.timedelta(days=n)


def synced(day: int = 0) -> SyncRun:
    """A successful eTools Datamart sync one hour before the pass of ``day``."""
    at = NOW + datetime.timedelta(days=day, hours=-1)
    return SyncRun.objects.create(
        job=DATAMART,
        status=SyncRun.Status.SUCCEEDED,
        started_at=at - datetime.timedelta(minutes=20),
        finished_at=at,
    )


def run(day: int = 0, mode: str = DAILY, checks=None) -> memory.Outcome:
    context = Context.make(mode, now=NOW + datetime.timedelta(days=day))
    return memory.run(context, list(checks or CHECKS))


def item(key: str) -> WatchItem:
    return WatchItem.objects.get(key=key)


def keys(detector: str | None = None) -> set[str]:
    items = WatchItem.objects.exclude(detector=detectors.STALE_DETECTOR)
    if detector:
        items = items.filter(detector=detector)
    return set(items.values_list("key", flat=True))


@pytest.fixture
def fresh(db):
    return synced(0)


@pytest.fixture
def partner(db):
    return PartnerOrganization.objects.create(
        etl_id="1", name="Amel Association", partner_type="Civil Society Organization", vendor_number="V1"
    )


def make_pd(partner, number=PD_NUMBER, status="active", end=datetime.date(2026, 12, 31), **fields) -> PCA:
    values = {
        "etl_id": str(next(_ids)),
        "partner": partner,
        "partner_name": partner.name,
        "number": number,
        "title": "Education services",
        "status": status,
        "start": datetime.date(2026, 1, 1),
        "end": end,
        "section_names": ["Education"],
        "unicef_focal_points": ["Umar Plantedsix"],
    }
    values.update(fields)
    return PCA.objects.create(**values)


@pytest.fixture
def pd(partner):
    return make_pd(partner)  # ends 31 Dec 2026: not within 60 days


def report(pd, progress_report="PR-7", due=None, rows=3, **fields):
    """One progress report of ``pd``, repeated on ``rows`` indicator rows as the portal sends it."""
    for n in range(rows):
        values = {
            "datamart_id": next(_ids),
            "partner_id": pd.partner_id,
            "intervention": pd,
            "pd_reference_number": pd.number,
            "progress_report": progress_report,
            "report_number": "QPR3",
            "report_type": "QPR",
            "report_status": "Due",
            "period_start": datetime.date(2026, 7, 1),
            "period_end": datetime.date(2026, 9, 30),
            "due_date": due or days(10),
            "indicator": f"# of children reached {n}",
            "etools_indicator_id": str(100 + n),
            "location": f"Place {n}",
            "submitted_by": "Walid Plantedfour",
            "narrative": "Zelda Planted wrote this narrative",
        }
        values.update(fields)
        dm.ReportedIndicator.objects.create(**values)


def action_point(partner, due, section="Education", high=False, status="open", **fields):
    values = {
        "datamart_id": next(_ids),
        "partner": partner,
        "partner_name": partner.name if partner else "",
        "reference_number": f"AP/{next(_ids)}",
        "description": "Ask Zelda Planted to replace the water tank",
        "status": status,
        "high_priority": high,
        "due_date": due,
        "section": section,
        "related_module": "tpm",
        "assigned_to_name": "Zelda Planted",
    }
    values.update(fields)
    return dm.ActionPoint.objects.create(**values)


def flag(day: datetime.date, *finding_keys: str) -> DailyReview:
    """A succeeded daily review on ``day`` with open findings under these keys."""
    review = DailyReview.objects.create(date=day, status=DailyReview.Status.SUCCEEDED)
    for key in finding_keys:
        ReviewFinding.objects.create(
            review=review, key=key, check_id=key.split(":")[0], severity="warning", title=key, state="new"
        )
    return review


# ---------------------------------------------------------------------------- the checks
def test_the_checks_are_registered_on_the_datamart_and_start_in_trial(pd, fresh):
    for check in CHECKS:
        assert detectors.get(check.id) is check
        assert check.source_jobs == (DATAMART,)
        assert check.default_mode == detectors.TRIAL
        assert "detector" not in check.label.lower()
    report(pd)
    run()
    assert DetectorSetting.objects.get(detector="report_due_soon").mode == DetectorSetting.Mode.TRIAL
    assert deadlines.REPORT_DUE_SOON.milestones == (14, 7, 3, 1, 0)
    assert deadlines.PD_ENDING.milestones == (60, 30, 14, 7)


def test_the_twin_of_a_pd_ending_soon_finding_is_the_pd_ending_item():
    assert deadlines.twin_of(f"pd_ending_soon:{PD_NUMBER}") == f"due:pd_end:{PD_NUMBER}"
    assert deadlines.twin_of(f"reports_overdue:{PD_NUMBER}") == ""
    assert deadlines.twin_of("") == ""


# ---------------------------------------------------------------------------- progress reports
def test_three_indicator_rows_of_one_report_give_one_item(pd, fresh):
    report(pd, rows=3)
    run()
    assert keys() == {f"due:report:{PD_NUMBER}:PR-7"}
    found = item(f"due:report:{PD_NUMBER}:PR-7")
    assert found.detector == "report_due_soon"
    assert found.kind == WatchItem.Kind.DEADLINE
    assert found.title == f"Progress report due 15 Oct 2026: {PD_NUMBER} (Amel Association), QPR3"
    assert found.due_date == days(10)
    assert (found.entity_kind, found.entity_key) == ("programme_document", str(pd.pk))
    assert found.etools_sections == ["Education"]
    assert found.scope == WatchItem.Scope.SECTION
    assert found.url == f"/programmes/{pd.pk}/detail/"
    assert found.evidence["source"] == "eTools progress reports"
    assert found.evidence["source_job"] == DATAMART
    assert found.evidence["synced_at"]
    assert found.evidence["numbers"] == {"indicators": 3, "days_left": 10}
    assert found.evidence["milestones"] == [14, 7, 3, 1, 0]
    assert found.evidence["records"][0]["value"] == "not submitted (status: Due)"
    assert "1 Jul 2026 to 30 Sep 2026" in found.detail


@pytest.mark.parametrize(
    "fields",
    [
        {"report_status": "accepted"},
        {"report_status": " Submitted "},
        {"submission_date": datetime.date(2026, 10, 1)},
    ],
)
def test_a_submitted_or_accepted_report_gives_none(pd, fresh, fields):
    report(pd, **fields)
    run()
    assert keys() == set()


def test_only_reports_due_in_the_next_14_days_of_active_pds_count(pd, partner, fresh):
    report(pd, "PR-past", due=days(-1))  # overdue: the daily review's
    report(pd, "PR-today", due=days(0))
    report(pd, "PR-14", due=days(14))
    report(pd, "PR-15", due=days(15))
    report(pd, "PR-hr", due=days(5), report_type="HR")
    report(make_pd(partner, number="LEB/PCA2026001/PD2026009", status="ended"), "PR-ended", due=days(5))
    run()
    assert keys("report_due_soon") == {f"due:report:{PD_NUMBER}:PR-today", f"due:report:{PD_NUMBER}:PR-14"}


def test_due_in_10_days_is_info_and_due_in_2_days_is_a_warning(pd, fresh):
    report(pd, "PR-10", due=days(10))
    report(pd, "PR-2", due=days(2))
    run()
    assert item(f"due:report:{PD_NUMBER}:PR-10").severity == WatchItem.Severity.INFO
    assert item(f"due:report:{PD_NUMBER}:PR-2").severity == WatchItem.Severity.WARNING


def test_a_report_coming_closer_gets_worse_on_its_own(pd, fresh):
    report(pd, due=days(5))
    run()
    synced(2)
    outcome = run(day=2)
    assert outcome.worse == [f"due:report:{PD_NUMBER}:PR-7"]
    assert item(f"due:report:{PD_NUMBER}:PR-7").severity == WatchItem.Severity.WARNING


def test_a_due_report_that_passes_its_date_hands_over_to_the_daily_review(pd, fresh):
    report(pd, due=days(2))
    run()
    synced(3)
    flag(days(3), f"reports_overdue:{PD_NUMBER}")
    outcome = run(day=3)
    found = item(f"due:report:{PD_NUMBER}:PR-7")
    assert outcome.closed == [found.key]
    assert found.state == WatchItem.State.CLOSED
    assert found.close_reason == "now overdue: see the daily review"
    assert found.review_key == f"reports_overdue:{PD_NUMBER}"
    assert found.story[-1]["text"] == "Closed: now overdue: see the daily review"


def test_a_submitted_report_closes_at_once_even_in_a_quick_pass(pd, fresh):
    report(pd)
    run()
    dm.ReportedIndicator.objects.update(submission_date=datetime.date(2026, 10, 6), report_status="Submitted")
    synced(1)
    run(day=1, mode=QUICK)
    found = item(f"due:report:{PD_NUMBER}:PR-7")
    assert found.state == WatchItem.State.CLOSED
    assert found.close_reason == "submitted on 6 Oct 2026"


def test_a_report_given_a_later_date_closes_as_moved_and_a_vanished_one_is_only_missed(pd, fresh):
    report(pd, "PR-7")
    report(pd, "PR-8")
    run()
    dm.ReportedIndicator.objects.filter(progress_report="PR-7").update(due_date=days(40))
    dm.ReportedIndicator.objects.filter(progress_report="PR-8").delete()
    synced(1)
    run(day=1)
    moved, vanished = item(f"due:report:{PD_NUMBER}:PR-7"), item(f"due:report:{PD_NUMBER}:PR-8")
    assert (moved.state, moved.close_reason) == (WatchItem.State.CLOSED, "due date moved to 14 Nov 2026")
    assert (vanished.state, vanished.missed_runs) == (WatchItem.State.OPEN, 1)


def test_a_report_whose_pd_is_no_longer_active_closes(pd, fresh):
    report(pd)
    run()
    PCA.objects.filter(pk=pd.pk).update(status="terminated")
    synced(1)
    run(day=1)
    assert item(f"due:report:{PD_NUMBER}:PR-7").close_reason == "eTools now shows the PD as terminated"


def test_unknown_numbers_are_left_out_of_the_evidence(pd, partner, fresh):
    report(pd, progress_report="", report_number="", etools_indicator_id="", rows=2)
    make_pd(partner, number="LEB/PD-undated", status="ended", end=None)
    run()
    found = item(f"due:report:{PD_NUMBER}:{days(10).isoformat()}")  # no report id: its due date
    assert found.evidence["numbers"] == {"days_left": 10}
    assert found.title == f"Progress report due 15 Oct 2026: {PD_NUMBER} (Amel Association)"
    undated = item("closure:LEB/PD-undated")
    assert undated.title == "PD ended, awaiting closure: LEB/PD-undated (Amel Association)"
    assert undated.evidence["numbers"] == {"outstanding": 0, "funds_reservations": 0}


# ---------------------------------------------------------------------------- action points
def test_action_points_due_soon_are_grouped_per_section_and_partner(partner, fresh):
    action_point(partner, days(2), high=True)
    action_point(partner, days(12))
    action_point(partner, days(9), section="WASH")
    action_point(partner, days(15))  # too far
    action_point(partner, days(3), status="completed")
    action_point(None, days(6), section="", partner_name="")
    run()
    assert keys("action_point_due") == {
        "due:action_points:Education:Amel Association",
        "due:action_points:WASH:Amel Association",
        "due:action_points::",
    }
    education = item("due:action_points:Education:Amel Association")
    assert (
        education.title
        == "2 action points due between 7 Oct 2026 and 17 Oct 2026 for Amel Association (Education)"
    )
    assert education.severity == WatchItem.Severity.WARNING  # the soonest is 2 days away
    assert education.due_date == days(2)
    assert education.evidence["numbers"] == {"action_points": 2, "high_priority": 1, "days_left": 2}
    assert education.evidence["records"][0]["label"].endswith("(high priority)")
    assert (education.entity_kind, education.entity_key) == ("partner", str(partner.pk))
    assert education.etools_sections == ["Education"]
    wash = item("due:action_points:WASH:Amel Association")
    assert wash.title == "1 action point due on 14 Oct 2026 for Amel Association (WASH)"
    assert wash.severity == WatchItem.Severity.INFO
    nobody = item("due:action_points::")
    assert nobody.title == "1 action point due on 11 Oct 2026 for no partner"
    assert (nobody.etools_sections, nobody.entity_kind) == ([], "")


def test_an_assignee_and_a_description_never_appear_in_an_item(partner, fresh):
    action_point(partner, days(4))
    action_point(partner, days(-20))
    run()
    for found in WatchItem.objects.filter(detector__startswith="action_point"):
        text = " ".join([found.key, found.title, found.detail, found.url, json.dumps(found.evidence)])
        assert "Zelda" not in text
        assert "tank" not in text


def test_normal_priority_overdue_points_are_followed_and_high_priority_left_to_the_review(partner, fresh):
    action_point(partner, days(-20))
    action_point(partner, days(-3))
    action_point(partner, days(-10), section="WASH", high=True)
    run()
    assert keys("action_points_overdue_normal") == {"overdue:action_points:Education:Amel Association"}
    late = item("overdue:action_points:Education:Amel Association")
    assert late.title == "2 action points overdue since 15 Sep 2026 for Amel Association (Education)"
    assert late.kind == WatchItem.Kind.CONCERN
    assert late.severity == WatchItem.Severity.WARNING
    assert late.due_date is None
    assert late.evidence["numbers"] == {"action_points": 2, "oldest_due": "2026-09-15", "days_overdue": 20}
    dm.ActionPoint.objects.filter(section="Education").update(status="completed")
    synced(1)
    run(day=1)
    late.refresh_from_db()
    assert late.state == WatchItem.State.CLOSED
    assert late.close_reason == "none is overdue any more: closed or given a new date in eTools"


def test_an_action_point_passing_its_date_closes_the_due_item(partner, fresh):
    action_point(partner, days(1), high=True)
    action_point(partner, days(1), section="WASH")
    action_point(partner, days(1), section="Health")
    run()
    dm.ActionPoint.objects.filter(section="Health").update(status="completed")
    synced(2)
    run(day=2)
    high = item("due:action_points:Education:Amel Association")
    assert high.close_reason == "now overdue: see the daily review"
    assert high.review_key == "action_points_overdue:Education:Amel Association"
    assert item("due:action_points:WASH:Amel Association").close_reason == "now overdue"
    assert item("due:action_points:Health:Amel Association").close_reason == "closed in eTools"
    assert keys("action_points_overdue_normal") == {"overdue:action_points:WASH:Amel Association"}


def test_a_due_item_closes_for_what_became_of_its_own_points(partner, fresh):
    action_point(partner, days(-30), high=True)  # long overdue: the daily review's, not in the item
    done = action_point(partner, days(5))
    moved = action_point(partner, days(6), section="WASH")
    later = action_point(partner, days(8), section="Health")
    vanished = action_point(partner, days(9), section="Social Policy")
    run()
    assert item("due:action_points:Education:Amel Association").evidence["action_point_ids"] == [
        done.datamart_id
    ]
    dm.ActionPoint.objects.filter(pk=done.pk).update(status="completed")
    dm.ActionPoint.objects.filter(pk=moved.pk).update(section="Child Protection")
    dm.ActionPoint.objects.filter(pk=later.pk).update(due_date=days(30))
    dm.ActionPoint.objects.filter(pk=vanished.pk).delete()
    synced(1)
    run(day=1)
    assert item("due:action_points:Education:Amel Association").close_reason == "closed in eTools"
    assert (
        item("due:action_points:WASH:Amel Association").close_reason
        == "now listed under another section or partner in eTools"
    )
    assert item("due:action_points:Health:Amel Association").close_reason == "due date moved to 4 Nov 2026"
    gone = item("due:action_points:Social Policy:Amel Association")
    assert (gone.state, gone.missed_runs) == (WatchItem.State.OPEN, 1)
    assert item("due:action_points:Child Protection:Amel Association").state == WatchItem.State.OPEN


# ---------------------------------------------------------------------------- programme documents
def test_every_active_pd_ending_within_60_days_is_followed(partner, fresh):
    make_pd(partner, number="LEB/PD-45", end=days(45))
    make_pd(partner, number="LEB/PD-10", end=days(10), status="suspended")
    make_pd(partner, number="LEB/PD-61", end=days(61))
    make_pd(partner, number="LEB/PD-past", end=days(-1))
    make_pd(partner, number="LEB/PD-draft", end=days(20), status="draft")
    run()
    assert keys("pd_ending") == {"due:pd_end:LEB/PD-45", "due:pd_end:LEB/PD-10"}
    later = item("due:pd_end:LEB/PD-45")
    assert later.title == "PD ends 19 Nov 2026: LEB/PD-45 (Amel Association)"
    assert later.severity == WatchItem.Severity.INFO
    assert later.due_date == days(45)
    assert later.evidence["milestones"] == [60, 30, 14, 7]
    assert later.review_key == ""
    assert later.detail.startswith("It runs from 1 Jan 2026 to 19 Nov 2026.")
    assert item("due:pd_end:LEB/PD-10").severity == WatchItem.Severity.WARNING


def test_a_pd_the_daily_review_flags_carries_its_key_and_is_a_warning(partner, fresh):
    make_pd(partner, number="LEB/PD-45", end=days(45))
    flag(TODAY, "pd_ending_soon:LEB/PD-45")
    run()
    found = item("due:pd_end:LEB/PD-45")
    assert found.review_key == "pd_ending_soon:LEB/PD-45"
    assert found.severity == WatchItem.Severity.WARNING
    assert "The daily review flags it too" in found.detail
    assert WatchItem.objects.filter(entity_key=found.entity_key).count() == 1  # never a second item


def test_a_pd_ending_given_a_later_date_or_ended_closes(partner, fresh):
    extended = make_pd(partner, number="LEB/PD-ext", end=days(20))
    ended = make_pd(partner, number="LEB/PD-end", end=days(20))
    run()
    PCA.objects.filter(pk=extended.pk).update(end=days(200))
    PCA.objects.filter(pk=ended.pk).update(status="ended")
    synced(1)
    run(day=1)
    assert item("due:pd_end:LEB/PD-ext").close_reason == "end date moved to 23 Apr 2027"
    assert item("due:pd_end:LEB/PD-end").close_reason == "eTools now shows the PD as ended"
    assert keys("pd_awaiting_closure") == {"closure:LEB/PD-end"}
    # eTools marked it ended before its planned end date: the title does not say it ended on a date to come
    assert item("closure:LEB/PD-end").title.startswith("PD marked ended before its end date (25 Oct 2026)")


def test_an_ended_pd_awaits_closure_and_long_after_with_money_left_it_is_a_warning(partner, fresh):
    recent = make_pd(partner, number="LEB/PD-recent", status="ended", end=days(-10))
    old = make_pd(partner, number="LEB/PD-old", status="ended", end=days(-90))
    for n, (amount, completed) in enumerate(
        [(Decimal("5000.40"), False), (Decimal(900), True), (Decimal(0), False)]
    ):
        dm.FundsReservationHeader.objects.create(
            datamart_id=next(_ids),
            intervention=old,
            fr_number=f"040000{n}",
            outstanding_amt=amount,
            completed_flag=completed,
            end_date=days(-90),
        )
    run()
    assert keys("pd_awaiting_closure") == {"closure:LEB/PD-recent", "closure:LEB/PD-old"}
    first = item("closure:LEB/PD-recent")
    assert first.title == "PD ended 25 Sep 2026, awaiting closure: LEB/PD-recent (Amel Association)"
    assert (first.severity, first.scope, first.kind) == (
        WatchItem.Severity.INFO,
        WatchItem.Scope.SECTION,
        WatchItem.Kind.CONCERN,
    )
    late = item("closure:LEB/PD-old")
    assert (late.severity, late.scope) == (WatchItem.Severity.WARNING, WatchItem.Scope.COUNTRY)
    assert "5,000 USD is still outstanding on 1 funds reservation." in late.detail
    assert late.evidence["numbers"] == {"outstanding": 5000, "funds_reservations": 1, "days_since": 90}
    assert [r["label"] for r in late.evidence["records"]] == ["LEB/PD-old status", "FR 0400000"]
    PCA.objects.filter(pk=recent.pk).update(status="closed")
    synced(1)
    run(day=1)
    assert item("closure:LEB/PD-recent").close_reason == "eTools now shows the PD as closed"
    assert item("closure:LEB/PD-old").state == WatchItem.State.OPEN


# ---------------------------------------------------------------------------- all of them
@pytest.fixture
def everything(partner, pd, fresh):
    report(pd)
    action_point(partner, days(4))
    action_point(partner, days(-20))
    make_pd(partner, number="LEB/PD-45", end=days(45))
    make_pd(partner, number="LEB/PD-old", status="ended", end=days(-90))


def test_keys_are_the_same_across_two_runs(everything):
    first = run()
    found = keys()
    assert len(found) == 5
    assert sorted(first.new) == sorted(found)
    second = run()
    assert keys() == found
    assert (second.new, second.closed, second.missed) == ([], [], [])


def test_no_person_field_free_text_or_legacy_table_is_read(everything):
    with CaptureQueriesContext(connection) as queries:
        run()
    sql = "\n".join(query["sql"] for query in queries.captured_queries)
    for column in (
        "assigned_to_name",
        "submitted_by",
        "author_name",
        "unicef_focal_points",
        "narrative",
        "description",
    ):
        assert column not in sql
    for table in ('"etools_actionpoint"', '"etools_travel"', '"etools_engagement"'):
        assert table not in sql
    text = json.dumps(
        list(WatchItem.objects.values("key", "title", "detail", "url", "evidence")), default=str
    )
    for name in PEOPLE:
        assert name not in text


def test_titles_carry_dates_never_day_counts(everything):
    run()
    for found in WatchItem.objects.all():
        assert " day" not in found.title


def test_sections_come_from_the_confirmed_section_names(everything):
    education = Section.objects.create(name="Education", code="EDU")
    SectionMatch.objects.create(etools_name="Education", section=education, how="exact", confirmed=True)
    run()
    assert item(f"due:report:{PD_NUMBER}:PR-7").section_ids == [education.pk]
    assert item("due:action_points:Education:Amel Association").section_ids == [education.pk]


def test_a_stale_datamart_gives_no_item_and_closes_nothing(everything):
    run()
    opened = keys()
    dm.ReportedIndicator.objects.update(submission_date=TODAY)  # would close the report
    dm.ActionPoint.objects.update(status="completed")  # would close the action points
    PCA.objects.update(status="closed")  # would close the PDs
    report(make_pd(PartnerOrganization.objects.get(), number="LEB/PD-new"), "PR-new", due=days(6))
    outcome = run(day=3)  # the last sync is now 73 hours old
    assert keys() == opened
    assert set(WatchItem.objects.filter(key__in=opened).values_list("state", flat=True)) == {
        WatchItem.State.OPEN
    }
    assert outcome.closed == []
    assert outcome.details()["detectors"]["report_due_soon"]["skipped"] == f"stale: {DATAMART}"
    assert item(f"system:stale:{DATAMART}").state == WatchItem.State.OPEN


def test_without_any_datamart_sync_nothing_is_raised(partner, pd):
    report(pd)
    run()
    assert keys() == set()
    assert WatchItem.objects.filter(key=f"system:stale:{DATAMART}").exists()
