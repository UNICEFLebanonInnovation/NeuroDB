"""NeuroDB Watch's money, assurance and calendar checks: grants and funds reservations ending with money
left, HACT assurance still to do in the last quarter, agreed dates on daily review findings, donor
accounts ending and the yearly rollover of the reporting year."""

import datetime
import itertools
import json
from decimal import Decimal

import pytest
from django.urls import reverse

from neurodb.accounts.models import Section, User
from neurodb.core.models import SyncRun
from neurodb.datamart import models as dm
from neurodb.donors.models import DonorAccount
from neurodb.indicators.models import ReportingYear
from neurodb.partnerships.models import PCA, PartnerOrganization
from neurodb.reports.brief import fr_lines, grant_balances
from neurodb.review.models import DailyReview, FindingAssignment, ReviewFinding
from neurodb.watch import detectors, memory, redact
from neurodb.watch.detectors import DAILY, QUICK, Context, calendar, money
from neurodb.watch.models import DetectorSetting, SectionMatch, WatchItem

pytestmark = pytest.mark.django_db

UTC = datetime.UTC
TODAY = datetime.date(2026, 10, 5)
PD_NUMBER = "LEB/PCA2026001/PD2026001"
DATAMART = SyncRun.Job.ETOOLS_DATAMART
MONEY = [money.GRANT_EXPIRING, money.FR_EXPIRING, money.HACT_ASSURANCE_GAP]
CALENDAR = [calendar.ASSIGNMENT_DUE, calendar.DONOR_ACCOUNT_EXPIRING, calendar.REPORTING_YEAR_ROLLOVER]
OWNER = "Ms Rania Plantedeight"  # a finding assignment's owner and note: never copied
NOTE = "Call Qasim Plantednine before Friday"
FOCAL_POINT = "Umar Plantedsix"
_ids = itertools.count(1)


def days(n: int, today: datetime.date = TODAY) -> datetime.date:
    return today + datetime.timedelta(days=n)


def moment(day: datetime.date) -> datetime.datetime:
    """09:00 in Beirut on ``day``."""
    return datetime.datetime.combine(day, datetime.time(6, 0), tzinfo=UTC)


def synced(day: datetime.date = TODAY) -> SyncRun:
    """A successful eTools Datamart sync one hour before the pass of ``day``."""
    at = moment(day) - datetime.timedelta(hours=1)
    return SyncRun.objects.create(
        job=DATAMART,
        status=SyncRun.Status.SUCCEEDED,
        started_at=at - datetime.timedelta(minutes=20),
        finished_at=at,
    )


def run(day: datetime.date = TODAY, checks=None, mode: str = DAILY) -> memory.Outcome:
    return memory.run(Context.make(mode, now=moment(day)), list(checks or MONEY + CALENDAR))


def item(key: str) -> WatchItem:
    return WatchItem.objects.get(key=key)


def keys(detector: str | None = None) -> set[str]:
    items = WatchItem.objects.exclude(detector=detectors.STALE_DETECTOR)
    if detector:
        items = items.filter(detector=detector)
    return set(items.values_list("key", flat=True))


def text_of(found: WatchItem) -> str:
    """Every text the item keeps, as one string."""
    fields = {
        name: getattr(found, name)
        for name in (
            "key",
            "title",
            "detail",
            "url",
            "entity_key",
            "evidence",
            "story",
            "related",
            "close_reason",
        )
    }
    return json.dumps(fields, default=str)


@pytest.fixture
def fresh(db):
    return synced(TODAY)


@pytest.fixture
def education(db):
    section = Section.objects.create(name="Education", code="EDU")
    SectionMatch.objects.create(etools_name="Education", section=section, how="exact", confirmed=True)
    return section


@pytest.fixture
def partner(db):
    return PartnerOrganization.objects.create(
        etl_id="1", name="Amel Association", partner_type="Civil Society Organization", vendor_number="V1"
    )


def make_pd(partner, number=PD_NUMBER, status="active", sections=("Education",), **fields) -> PCA:
    values = {
        "etl_id": str(next(_ids)),
        "partner": partner,
        "partner_name": partner.name,
        "number": number,
        "title": "Education services",
        "status": status,
        "start": datetime.date(2026, 1, 1),
        "end": datetime.date(2026, 12, 31),
        "section_names": list(sections),
        "unicef_focal_points": [FOCAL_POINT],
    }
    values.update(fields)
    return PCA.objects.create(**values)


@pytest.fixture
def pd(partner):
    return make_pd(partner)


def fr(pd, number="0400001", outstanding=50_000, total=80_000, end=datetime.date(2026, 12, 31), **fields):
    """A funds reservation of ``pd`` (its header); its lines are added with :func:`line`."""
    values = {
        "datamart_id": next(_ids),
        "intervention": pd,
        "pd_reference_number": pd.number if pd else "",
        "fr_number": number,
        "total_amt": Decimal(total),
        "actual_amt": Decimal(total - outstanding),
        "outstanding_amt": Decimal(outstanding),
        "start_date": datetime.date(2026, 1, 1),
        "end_date": end,
    }
    values.update(fields)
    return dm.FundsReservationHeader.objects.create(**values)


def line(header, grant="SC1", amount=None, donor="EU"):
    return dm.FundsReservation.objects.create(
        datamart_id=next(_ids),
        intervention_id=header.intervention_id,
        fr_number=header.fr_number,
        line_item=next(_ids),
        donor=donor,
        grant_number=grant,
        overall_amount=Decimal(header.total_amt if amount is None else amount),
    )


def grant(name="SC1", expiry=None, donor="EU"):
    return dm.Grant.objects.create(datamart_id=next(_ids), name=name, donor=donor, expiry=expiry or days(45))


def hact(partner, year=2026, **fields):
    values = {
        "datamart_id": next(_ids),
        "partner": partner,
        "partner_name": partner.name if partner else "",
        "vendor_number": partner.vendor_number if partner else "",
        "year": year,
        "pv_required": 3,
        "pv_completed": 1,
        "sc_required": 1,
        "sc_completed": 1,
        "audits_required": 0,
        "audits_completed": 0,
    }
    values.update(fields)
    return dm.PartnerHACTYear.objects.create(**values)


def assignment(
    key=f"reports_overdue:{PD_NUMBER}", due=TODAY - datetime.timedelta(days=1), status="assigned", **fields
) -> FindingAssignment:
    values = {
        "key": key,
        "title": "Assigned title, typed when assigned",
        "section": "Education",
        "owner": OWNER,
        "note": NOTE,
        "due_date": due,
        "status": status,
    }
    values.update(fields)
    return FindingAssignment.objects.create(**values)


def finding(
    key=f"reports_overdue:{PD_NUMBER}", title=f"2 progress reports overdue for {PD_NUMBER}", day=TODAY
):
    review, _ = DailyReview.objects.get_or_create(date=day, defaults={"status": DailyReview.Status.SUCCEEDED})
    return ReviewFinding.objects.create(
        review=review,
        key=key,
        check_id=key.split(":")[0],
        severity="warning",
        section="Education",
        title=title,
        url="/programmes/?q=x",
        state="new",
    )


def donor_account(name="European Union", expires=None, active=True, username="donor") -> DonorAccount:
    user = User.objects.create_user(
        username=username, password="x-123456789", email="tariq.planted@example.org"
    )
    return DonorAccount.objects.create(
        user=user,
        name=name,
        donors=["EU"],
        contact="Salma Plantedseven",
        active=active,
        expires_on=expires or days(10),
        must_change_password=False,
    )


# ---------------------------------------------------------------------------- the checks
def test_the_checks_are_registered_and_start_in_trial(fresh):
    for check in MONEY + CALENDAR:
        assert detectors.get(check.id) is check
        assert check.default_mode == detectors.TRIAL
        assert "detector" not in check.label.lower()
    assert all(check.source_jobs == (DATAMART,) for check in MONEY)
    assert all(check.source_jobs == () for check in CALENDAR)  # NeuroDB's own tables: always current
    assert money.GRANT_EXPIRING.milestones == (90, 60, 30, 14)
    assert money.FR_EXPIRING.milestones == (30, 14, 7)
    run()
    assert set(DetectorSetting.objects.values_list("detector", "mode")) >= {
        (check.id, DetectorSetting.Mode.TRIAL) for check in MONEY + CALENDAR
    }


# ---------------------------------------------------------------------------- grants
def test_the_grant_balance_is_each_frs_outstanding_in_the_grants_share_of_its_lines(pd):
    header = fr(pd, outstanding=30_000, total=100_000)
    line(header, grant="SC1", amount=75_000)
    line(header, grant="SC2", amount=25_000, donor="Germany")
    other = fr(pd, number="0400002", outstanding=10_000, total=10_000)
    line(other, grant="SC1")
    fr(pd, number="0400003", end=datetime.date(2025, 12, 31))  # not running in 2026
    _per_pd, lines = fr_lines([pd.pk], 2026)
    balances = grant_balances(lines)
    assert list(balances) == ["SC1", "SC2"]
    assert balances["SC1"]["unspent"] == pytest.approx(32_500)  # 30,000 x 3/4 + 10,000
    assert balances["SC1"]["reserved"] == pytest.approx(85_000)
    assert balances["SC1"]["frs"] == pytest.approx({"0400001": 22_500, "0400002": 10_000})
    assert (balances["SC1"]["donor"], balances["SC1"]["pd_ids"]) == ("EU", [pd.pk])
    assert balances["SC2"]["unspent"] == pytest.approx(7_500)


def test_a_grant_expiring_in_45_days_with_50000_unspent_reaches_its_sections_and_the_country(
    pd, education, fresh
):
    line(fr(pd, outstanding=50_000))
    grant(expiry=days(45))
    run()
    found = item("due:grant:SC1")
    assert found.detector == "grant_expiring"
    assert found.title == "Grant SC1 (EU) expires 19 Nov 2026 with 50,000 USD unspent"
    assert found.severity == WatchItem.Severity.INFO  # warning from 30 days, critical from 14
    assert found.scope == WatchItem.Scope.COUNTRY  # its sections and the whole-country view
    assert (found.etools_sections, found.section_ids) == (["Education"], [education.pk])
    assert (found.entity_kind, found.entity_key) == ("grant", "SC1")
    assert found.due_date == days(45)
    assert found.url == "/funds/?grant=SC1"
    assert found.evidence["numbers"] == {"unspent": 50_000, "reserved": 80_000, "pds": 1, "days_left": 45}
    assert found.evidence["records"][1]["label"] == "FR 0400001"
    assert found.evidence["milestones"] == [90, 60, 30, 14]
    assert PD_NUMBER in found.detail and FOCAL_POINT not in text_of(found)


@pytest.mark.parametrize(
    ("left", "severity"),
    [(90, "info"), (31, "info"), (30, "warning"), (15, "warning"), (14, "critical"), (0, "critical")],
)
def test_a_grant_gets_more_urgent_as_it_comes_closer(pd, fresh, left, severity):
    line(fr(pd))
    grant(expiry=days(left))
    run()
    assert item("due:grant:SC1").severity == severity


def test_a_grant_with_500_unspent_or_expiring_later_or_already_gives_no_item(pd, fresh):
    line(fr(pd, outstanding=500))
    grant(expiry=days(45))
    line(fr(pd, number="0400002"), grant="SC2")
    grant("SC2", expiry=days(91))
    line(fr(pd, number="0400003"), grant="SC3")
    grant("SC3", expiry=days(-1))
    line(fr(pd, number="0400004"), grant="SC4")  # not in the grants table: no expiry known
    run()
    assert keys("grant_expiring") == set()


def test_a_grant_spent_below_the_amount_followed_closes_at_once(pd, fresh, settings):
    header = fr(pd, outstanding=50_000)
    line(header)
    grant(expiry=days(45))
    run()
    header.outstanding_amt = Decimal(4_000)
    header.save()
    outcome = run(mode=QUICK)
    found = item("due:grant:SC1")
    assert outcome.closed == [found.key] and found.state == WatchItem.State.CLOSED
    assert found.close_reason == (
        "4,000 USD left unspent on this year's funds reservations, below the 10,000 USD followed"
    )
    settings.WATCH_GRANT_MIN_UNSPENT = 1_000
    run()
    assert item("due:grant:SC1").state == WatchItem.State.OPEN  # back again within 14 days


def test_a_grant_whose_expiry_moves_or_passes_closes_with_the_reason(pd, fresh):
    line(fr(pd))
    line(fr(pd, number="0400002"), grant="SC2")
    sc1, sc2 = grant(expiry=days(45)), grant("SC2", expiry=days(2))
    run()
    sc1.expiry = days(200)
    sc1.save()
    synced(days(3))
    run(days(3))
    assert item("due:grant:SC1").close_reason == "expiry moved to 23 Apr 2027"
    assert (
        item("due:grant:SC2").close_reason == f"expired on {sc2.expiry.day} Oct 2026 with 50,000 USD unspent"
    )


# ---------------------------------------------------------------------------- funds reservations
def test_an_fr_ending_soon_with_money_outstanding_gives_an_item(pd, education, fresh):
    fr(pd, end=days(20), outstanding=6_000, total=10_000)
    run()
    found = item("due:fr:0400001")
    assert found.detector == "fr_expiring"
    assert found.title == (
        f"Funds reservation 0400001 ends 25 Oct 2026 with 6,000 USD outstanding: {PD_NUMBER} (Amel Association)"
    )
    assert found.severity == WatchItem.Severity.INFO
    assert found.scope == WatchItem.Scope.SECTION and found.section_ids == [education.pk]
    assert (found.entity_kind, found.entity_key) == ("programme_document", str(pd.pk))
    numbers = {"outstanding": 6_000, "reserved": 10_000, "disbursed": 4_000, "days_left": 20}
    assert found.evidence["numbers"] == numbers
    assert FOCAL_POINT not in text_of(found)


def test_an_fr_with_nothing_outstanding_completed_or_ending_later_gives_no_item(pd, fresh):
    fr(pd, number="0400001", end=days(10), outstanding=0)
    fr(pd, number="0400002", end=days(10), completed_flag=True)
    fr(pd, number="0400003", end=days(31))
    fr(pd, number="0400004", end=days(-1))
    run()
    assert keys("fr_expiring") == set()


def test_an_fr_within_7_days_is_a_warning_and_completing_it_closes_it_at_once(pd, fresh):
    header = fr(pd, end=days(7))
    run()
    assert item("due:fr:0400001").severity == WatchItem.Severity.WARNING
    header.completed_flag = True
    header.save()
    run(mode=QUICK)
    found = item("due:fr:0400001")
    assert (found.state, found.close_reason) == (WatchItem.State.CLOSED, "completed in eTools")


def test_an_fr_that_ends_with_money_outstanding_closes_saying_so(pd, fresh):
    fr(pd, end=days(1), outstanding=6_000)
    run()
    synced(days(2))
    run(days(2))
    assert item("due:fr:0400001").close_reason == "ended on 6 Oct 2026 with 6,000 USD still outstanding"


# ---------------------------------------------------------------------------- HACT assurance
def test_the_hact_gap_is_raised_from_1_october_and_critical_from_15_december(pd, partner, education):
    hact(partner)
    for day, severity in (
        (datetime.date(2026, 9, 30), None),
        (datetime.date(2026, 10, 1), "info"),
        (datetime.date(2026, 11, 14), "info"),
        (datetime.date(2026, 11, 15), "warning"),
        (datetime.date(2026, 12, 15), "critical"),
    ):
        synced(day)
        run(day, MONEY)
        found = WatchItem.objects.filter(key=f"hact:2026:{partner.pk}").first()
        assert (found.severity if found else None) == severity, day
    assert (
        found.title == "HACT assurance due by 31 Dec 2026 for Amel Association: 2 programmatic visits to go"
    )
    assert found.due_date == datetime.date(2026, 12, 31)
    assert found.scope == WatchItem.Scope.COUNTRY
    assert (found.etools_sections, found.section_ids) == (["Education"], [education.pk])  # its active PDs
    assert (found.entity_kind, found.entity_key) == ("partner", str(partner.pk))
    assert found.evidence["records"][0]["value"] == "1 of 3 completed"
    assert found.evidence["numbers"]["pv_completed"] == 1
    assert found.first_seen_on == datetime.date(2026, 10, 1)


def test_hact_assurance_done_gives_no_item_and_closes_one_open(partner, fresh):
    other = PartnerOrganization.objects.create(
        etl_id="2", name="Done Org", partner_type="Civil Society Organization", vendor_number="V2"
    )
    hact(other, pv_completed=3)
    hact(partner, year=2025)  # last year's
    row = hact(partner)
    run()
    assert keys("hact_assurance_gap") == {f"hact:2026:{partner.pk}"}
    row.pv_completed = 3
    row.save()
    run(mode=QUICK)
    found = item(f"hact:2026:{partner.pk}")
    assert found.state == WatchItem.State.CLOSED
    assert found.close_reason == "assurance complete: every required visit, spot check and audit is done"


def test_the_hact_gap_closes_when_the_year_ends_saying_what_was_not_done(partner):
    hact(partner, sc_completed=0)
    synced(datetime.date(2026, 12, 31))
    run(datetime.date(2026, 12, 31), MONEY)
    synced(datetime.date(2027, 1, 1))
    run(datetime.date(2027, 1, 1), MONEY)
    found = item(f"hact:2026:{partner.pk}")
    assert found.close_reason == (
        "the 2026 HACT year ended on 31 Dec 2026 with 2 programmatic visits, 1 spot check not done"
    )


def test_a_stale_datamart_gives_no_money_item_and_closes_nothing(pd, partner):
    header = fr(pd, end=days(20))
    line(header)
    grant(expiry=days(20))
    hact(partner)
    synced(days(-5))  # older than SYNC_STALENESS_HOURS
    run(checks=MONEY)
    assert set(WatchItem.objects.values_list("key", flat=True)) == {f"system:stale:{DATAMART}"}
    synced(TODAY)
    run(checks=MONEY)
    followed = {"due:grant:SC1", "due:fr:0400001", f"hact:2026:{partner.pk}"}
    assert keys() == followed
    header.completed_flag = True  # would close the FR, and its grant has nothing unspent any more
    header.save()
    dm.Grant.objects.all().delete()
    dm.PartnerHACTYear.objects.all().delete()
    for later in (3, 4, 5):  # no sync since: stale
        run(days(later), MONEY)
    assert set(WatchItem.objects.filter(key__in=followed).values_list("state", "missed_runs")) == {
        ("open", 0)
    }


# ---------------------------------------------------------------------------- agreed dates
def test_an_agreed_date_passed_gives_an_item_without_the_owner_and_closing_closes_it(education):
    finding()
    found_assignment = assignment(due=days(-1), status="assigned")
    run(checks=CALENDAR)
    key = f"assignment:reports_overdue:{PD_NUMBER}"
    found = item(key)
    assert found.title == f"Agreed date 4 Oct 2026 passed: 2 progress reports overdue for {PD_NUMBER}"
    assert found.severity == WatchItem.Severity.WARNING
    assert found.scope == WatchItem.Scope.COUNTRY and found.section_ids == [education.pk]
    assert (found.has_owner, found.assignment_status) == (True, "assigned")
    assert (found.entity_kind, found.entity_key) == ("review_finding", f"reports_overdue:{PD_NUMBER}")
    assert found.url == "/programmes/?q=x"
    for planted in ("Rania", "Plantedeight", "Qasim", "Friday"):
        assert planted not in text_of(found)
    found_assignment.status = FindingAssignment.Status.CLOSED
    found_assignment.save()
    outcome = run(checks=CALENDAR, mode=QUICK)
    found = item(key)
    assert outcome.closed == [key]
    assert found.state == WatchItem.State.CLOSED and found.close_reason.startswith("closed on ")


def test_agreed_dates_are_raised_from_3_days_before_and_only_while_open(db):
    assignment("a:1", due=days(3), owner="")
    assignment("a:2", due=days(4))
    assignment("a:3", due=days(-30), status="closed")
    assignment("a:4", due=None)
    run(checks=CALENDAR)
    assert keys("assignment_due") == {"assignment:a:1"}
    found = item("assignment:a:1")
    assert found.title == "Agreed date 8 Oct 2026: Assigned title, typed when assigned"  # no review finding
    assert found.has_owner is False and "No one is named" in found.detail
    assert found.evidence["milestones"] == [3, 0]


def test_an_agreed_date_moved_later_or_deleted_closes_saying_so(db):
    row = assignment("a:1", due=days(2))
    run(checks=CALENDAR)
    row.due_date = days(20)
    row.save()
    run(checks=CALENDAR, mode=QUICK)
    assert item("assignment:a:1").close_reason == "agreed date moved to 25 Oct 2026"
    assignment("a:2", due=days(1))
    run(checks=CALENDAR)
    FindingAssignment.objects.filter(key="a:2").delete()
    run(checks=CALENDAR, mode=QUICK)
    assert item("assignment:a:2").close_reason == "the assignment was deleted"


# ---------------------------------------------------------------------------- donor accounts
def test_a_donor_account_ending_within_14_days_reaches_administrators_only(db):
    account = donor_account(expires=days(10))
    donor_account("Later donor", expires=days(15), username="later")
    donor_account("Switched off", active=False, username="off")
    run(checks=CALENDAR)
    assert keys("donor_account_expiring") == {f"donor_account:{account.pk}"}
    found = item(f"donor_account:{account.pk}")
    assert found.title == "Donor account European Union expires on 15 Oct 2026"
    assert found.severity == WatchItem.Severity.INFO
    assert found.scope == WatchItem.Scope.ADMINS and found.section_ids == [] and found.etools_sections == []
    assert found.url == reverse("admin:donors_donoraccount_change", args=[account.pk])
    assert redact.refused(found) == redact.ADMINS_ONLY  # never sent to the AI
    for planted in ("Salma", "Plantedseven", "tariq", "example.org"):
        assert planted not in text_of(found)


def test_a_donor_account_extended_closes_and_three_days_before_is_a_warning(db):
    account = donor_account(expires=days(3))
    run(checks=CALENDAR)
    assert item(f"donor_account:{account.pk}").severity == WatchItem.Severity.WARNING
    account.expires_on = days(90)
    account.save()
    run(checks=CALENDAR, mode=QUICK)
    assert item(f"donor_account:{account.pk}").close_reason == "extended to 3 Jan 2027"


# ---------------------------------------------------------------------------- the yearly rollover
def test_the_rollover_is_raised_from_15_december_to_15_january_for_administrators(db):
    ReportingYear.objects.create(name="2026", year="2026", current=True)
    run(datetime.date(2026, 12, 14), CALENDAR)
    assert keys("reporting_year_rollover") == set()
    run(datetime.date(2026, 12, 15), CALENDAR)
    found = item("rollover:2027")
    assert found.title == "Yearly rollover: make 2027 the current reporting year from 1 Jan 2027"
    assert (found.severity, found.scope, found.due_date) == ("info", "admins", datetime.date(2027, 1, 1))
    assert found.evidence["records"][1]["value"] == "not created yet"
    assert redact.refused(found) == redact.ADMINS_ONLY
    run(datetime.date(2027, 1, 2), CALENDAR)
    assert item("rollover:2027").severity == WatchItem.Severity.WARNING
    ReportingYear.objects.update(current=False)
    ReportingYear.objects.create(name="2027", year="2027", current=True)
    run(datetime.date(2027, 1, 3), CALENDAR, mode=QUICK)
    found = item("rollover:2027")
    assert (found.state, found.close_reason) == ("closed", "2027 is now the current reporting year")


def test_the_rollover_closes_after_15_january_saying_the_year_is_still_not_current(db):
    ReportingYear.objects.create(name="2026", year="2026", current=True)
    ReportingYear.objects.create(name="2027", year="2027", current=False)
    run(datetime.date(2027, 1, 15), CALENDAR)
    assert item("rollover:2027").evidence["records"][1]["value"] == "created, not marked current"
    run(datetime.date(2027, 1, 16), CALENDAR)
    assert item("rollover:2027").close_reason == (
        "the rollover period ended on 15 Jan 2027 with 2027 still not marked current"
    )
    assert calendar.rollover_year(datetime.date(2026, 6, 1)) is None
