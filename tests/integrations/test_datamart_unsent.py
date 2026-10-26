"""Partner report rows a whole read no longer returns (``datamart_sync.keep_unsent``): kept, counted on every page
and marked no longer sent (``ReportedIndicator.not_sent_since``); removed only when tonight's read holds their
replacement. The Datamart is the fake of ``fake_datamart`` (pages of 5, the partner reports read from 1 Jan 2025);
every value is made up."""

import datetime as dt
import re

import pytest
import responses

from neurodb.core.models import SyncRun
from neurodb.datamart import models as dm
from neurodb.datamart import raw, reporting
from neurodb.datamart import reached as rc
from neurodb.integrations.etools import datamart_sync as sync
from neurodb.integrations.etools.datamart import DatamartClient
from neurodb.integrations.http import make_session

from .fake_datamart import FakeDatamart, report

pytestmark = pytest.mark.django_db

BASE = "https://datamart.test"
REPORTS = f"{BASE}/api/latest/prp/datareport/"
TODAY = dt.date(2026, 10, 15)  # the suite's day


@pytest.fixture(autouse=True)
def small(settings):
    settings.ETOOLS_DATAMART_PAGE_SIZE = 5
    settings.ETOOLS_DATAMART_REPORTING_YEARS = 2


def client() -> DatamartClient:
    return DatamartClient(
        BASE, "svc-user", "not-a-real-secret", country="Lebanon", session=make_session("", backoff=0)
    )


def run() -> SyncRun:
    (found,) = sync.sync_all(only=["partner_reports"], triggered_by="test", client=client())
    return found


def row(
    pk: int,
    *,
    number: str = "QPR1",
    start: str = "2026-01-01",
    end: str = "2026-03-31",
    place: str = "Beirut",
    p_code: str = "LB11",
    ir: str = "501",
    value: float = 10,
    **extra,
) -> dict:
    """One partner report row of "LEB/PCA-1/PD1" (Partner A), Accepted, its value in the "()" total."""
    found = report(
        pk,
        start,
        report_number=number,
        progress_report=f"pr-{number}-{start}",
        reporting_period_end_date=end,
        report_status="Accepted",
        report_submission_date=end,
        current_location=place,
        p_code=p_code,
        indicator_report=ir,
        etools_cp_output_indicators_id="77",
        pd_output_title="Output 1",
        disaggregation={"()": {"v": value, "d": 1, "c": value}},
    )
    return {**found, **extra}


def held() -> dict[int, dm.ReportedIndicator]:
    return {r.datamart_id: r for r in dm.ReportedIndicator.objects.all()}


def reports(today: dt.date = TODAY) -> list[dict]:
    return reporting.progress_reports(dm.ReportedIndicator.objects.all(), today)


def reached_now():
    rows = dm.ReportedIndicator.objects.order_by(*rc.ROW_ORDER).values(*rc.ROW_FIELDS)
    return rc.compute(list(rows), today=TODAY, year=2026)


@responses.activate
def test_a_whole_read_marks_a_row_it_no_longer_returns_and_keeps_counting_it():
    fake = FakeDatamart([row(1), row(2, number="QPR2", start="2026-04-01", end="2026-06-30")])
    fake.register(REPORTS)
    first = run()
    before = held()[2].synced_at
    assert len(reports()) == 2
    fake.records = [row(1)]
    second = run()
    details = second.details
    assert second.status == SyncRun.Status.SUCCEEDED, details
    kept = held()[2]
    assert kept.not_sent_since == second.started_at and kept.synced_at == before
    assert first.started_at < kept.synced_at < second.started_at
    assert (details["removed"], details["no_longer_sent_new"], details["no_longer_sent"]) == (0, 1, 1)
    assert "removal_held" not in details
    assert len(reports()) == 2  # still counted
    assert held()[1].not_sent_since is None


@responses.activate
def test_a_marked_row_sent_again_is_cleared():
    fake = FakeDatamart([row(1), row(2, number="QPR2", start="2026-04-01", end="2026-06-30")])
    fake.register(REPORTS)
    run()
    fake.records = [row(1)]
    run()
    assert held()[2].not_sent_since is not None
    fake.records = [row(1), row(2, number="QPR2", start="2026-04-01", end="2026-06-30")]
    third = run()
    assert held()[2].not_sent_since is None
    assert (third.details["sent_again"], third.details["no_longer_sent"]) == (1, 0)


@responses.activate
def test_a_row_replaced_under_a_new_datamart_id_is_removed():
    """The same report, indicator, PRP id and P-code under a new Datamart id, with a smaller value: the old row
    goes, so Reached never takes the old larger value."""
    fake = FakeDatamart([row(1, value=50)])
    fake.register(REPORTS)
    run()
    fake.records = [row(9, value=20)]
    second = run()
    assert set(held()) == {9}
    assert (second.details["replaced"], second.details["removed"], second.details["no_longer_sent"]) == (
        1,
        1,
        0,
    )
    assert reached_now().reached == 20


@responses.activate
def test_a_row_resent_under_a_new_prp_indicator_report_id_is_replaced_at_every_place():
    """The old PRP id at places X and Y; tonight the new id at X only: both old rows go, the report has one
    indicator, and Reached keeps the report (two PRP ids at one place would drop it)."""
    fake = FakeDatamart([row(1, ir="501"), row(2, ir="501", place="Tripoli", p_code="LB21")])
    fake.register(REPORTS)
    run()
    fake.records = [row(9, ir="777", value=30)]
    second = run()
    assert set(held()) == {9} and second.details["replaced"] == 2
    (only,) = reports()
    assert only["indicators"] == 1
    found = reached_now()
    assert len(found.counted) == 1 and found.reached == 30


@responses.activate
def test_a_p_code_filled_in_replaces_the_row_that_had_only_a_name():
    fake = FakeDatamart([row(1, p_code="", value=40)])
    fake.register(REPORTS)
    run()
    fake.records = [row(9, p_code="LB11", value=40)]
    second = run()
    assert set(held()) == {9} and second.details["replaced"] == 1
    assert list(reached_now().by_location) == ["LB11"]  # the place counted once


@responses.activate
def test_a_place_the_read_did_not_return_under_the_same_prp_id_is_kept_and_marked():
    fake = FakeDatamart([row(1), row(2, place="Tripoli", p_code="LB21")])
    fake.register(REPORTS)
    run()
    fake.records = [row(9)]  # the same PRP id, at Beirut only
    second = run()
    assert set(held()) == {2, 9}
    assert held()[2].not_sent_since == second.started_at
    assert (second.details["replaced"], second.details["no_longer_sent_new"]) == (1, 1)


@responses.activate
def test_a_number_sent_again_for_another_period_is_counted():
    """PRP made QPR1 again with new dates (a PD amendment): the old rows are marked, counted as a report sent
    again for another period, and the list's first "updated" choice lists them."""
    fake = FakeDatamart([row(1), row(2, place="Tripoli", p_code="LB21")])
    fake.register(REPORTS)
    run()
    fake.records = [row(9, start="2026-02-01", end="2026-04-30")]
    second = run()
    details = second.details
    assert (details["same_number_other_period"], details["same_number_other_period_rows"]) == (1, 2)
    listed = raw.Filters.from_params({"updated": "unsent_period"}).apply(
        "partner_reports", raw.base("partner_reports"), None
    )
    assert set(listed.values_list("datamart_id", flat=True)) == {1, 2}
    marked = raw.Filters.from_params({"updated": "unsent"}).apply(
        "partner_reports", raw.base("partner_reports"), None
    )
    assert set(marked.values_list("datamart_id", flat=True)) == {1, 2}


@responses.activate
def test_a_not_yet_due_row_dropped_after_its_period_end_is_kept_and_reads_due_then_overdue(site_today):
    """Sent while its period ran (not yet due), then no longer sent once it ended: kept, it reads Due, then
    Overdue once its due date passes, until eTools sends it again."""
    owed = row(2, number="QPR4", start="2026-10-01", end="2026-10-31", report_status="Due",
               report_submission_date=None, due_date="2026-11-20")  # fmt: skip
    fake = FakeDatamart([row(1), owed])
    fake.register(REPORTS)
    run()
    assert {r["report_number"]: r["status"] for r in reports()}["QPR4"] == rc.NOT_YET_DUE
    site_today.set(dt.date(2026, 11, 3))
    fake.records = [row(1)]
    run()
    assert held()[2].not_sent_since is not None
    status = {r["report_number"]: r["status"] for r in reports(dt.date(2026, 11, 3))}
    assert status["QPR4"] == rc.DUE
    status = {r["report_number"]: r["status"] for r in reports(dt.date(2026, 11, 25))}
    assert status["QPR4"] == rc.OVERDUE


@responses.activate
def test_a_read_that_is_not_whole_marks_nothing():
    fake = FakeDatamart([row(1), row(2, number="QPR2", start="2026-04-01", end="2026-06-30")])
    fake.register(REPORTS)
    run()
    fake.records = [row(1)]
    run()  # QPR2 marked
    fake.records = [row(3, number="QPR3", start="2026-07-01", end="2026-09-30"), row(1)]
    fake.cap, fake.cut_count = 1, False  # announces 2, returns 1: not whole
    third = run()
    assert third.details["removal_skipped"] is True and "no_longer_sent_new" not in third.details
    assert set(held()) == {1, 2, 3}
    assert held()[1].not_sent_since is None and held()[2].not_sent_since is not None  # the earlier mark stays


@responses.activate
def test_an_empty_answer_marks_nothing():
    fake = FakeDatamart([row(1), row(2, number="QPR2", start="2026-04-01", end="2026-06-30")])
    fake.register(REPORTS)
    run()
    fake.records = [row(1)]
    second = run()  # QPR2 marked
    fake.records = []
    third = run()
    assert third.details["empty_response"] is True and "no_longer_sent_new" not in third.details
    assert held()[1].not_sent_since is None  # not marked by an empty answer
    assert held()[2].not_sent_since == second.started_at  # the earlier mark stays as it was


@responses.activate
def test_another_country_s_rows_are_still_deleted_marked_or_not():
    fake = FakeDatamart([row(1), row(2, number="QPR2", start="2026-04-01", end="2026-06-30")])
    fake.register(REPORTS)
    run()
    fake.records = [row(1)]
    run()  # QPR2 marked
    dm.ReportedIndicator.objects.filter(datamart_id=2).update(data={"country_name": "Syria"})
    dm.ReportedIndicator.objects.create(
        datamart_id=3, period_start="2026-01-01", data={"country_name": "Syria"}
    )
    third = run()
    assert third.details["other_country_removed"] == 2 and set(held()) == {1}


@responses.activate
def test_a_large_loss_is_marked_at_once_never_held():
    """1,200 rows held, tonight's whole read returns 2 of them: more than half and at least 1,000 are no longer
    sent, and they are marked the same night, never held one night, never removed."""
    dm.ReportedIndicator.objects.bulk_create(
        [
            dm.ReportedIndicator(datamart_id=1000 + n, period_start="2026-01-01", indicator=f"Old {n}")
            for n in range(1200)
        ]
    )
    FakeDatamart([row(1), row(2, place="Tripoli", p_code="LB21")]).register(REPORTS)
    found = run()
    details = found.details
    assert found.status == SyncRun.Status.SUCCEEDED, details
    assert "removal_held" not in details and details["removed"] == 0
    assert (details["no_longer_sent_new"], details["no_longer_sent"]) == (1200, 1200)
    assert dm.ReportedIndicator.objects.count() == 1202


def shared(pk: int, ir: str, **extra) -> dict:
    """A row of one of two PD indicators of the report that share a title and an output and carry no eTools id
    (one indicator to ``reached.indicator_key``), told apart by PRP's indicator report id alone."""
    return row(pk, ir=ir, etools_cp_output_indicators_id="", **extra)


@responses.activate
def test_a_second_indicator_sharing_title_and_output_dropped_is_kept_and_marked():
    """Tonight the Datamart drops the second indicator's row and returns the first one's again under its old
    Datamart id: nothing new came for the second, so it is kept and marked, never removed as replaced."""
    fake = FakeDatamart([shared(1, "501"), shared(2, "502")])
    fake.register(REPORTS)
    run()
    assert reports()[0]["indicators"] == 2
    fake.records = [shared(1, "501")]
    second = run()
    assert set(held()) == {1, 2} and held()[2].not_sent_since == second.started_at
    assert (second.details["replaced"], second.details["no_longer_sent_new"]) == (0, 1)
    assert reports()[0]["indicators"] == 2  # still counted


@responses.activate
def test_the_new_copy_of_one_indicator_never_replaces_another_sharing_its_title_and_output():
    """Tonight the Datamart sends the first indicator's row again under a new Datamart id and drops the
    second's. The new row replaces the first one's old row (its PRP id, its place), never the second's: a PRP
    id another row NeuroDB held carries is another indicator report."""
    fake = FakeDatamart([shared(1, "501"), shared(2, "502")])
    fake.register(REPORTS)
    run()
    fake.records = [shared(9, "501")]
    second = run()
    assert set(held()) == {2, 9} and held()[2].not_sent_since == second.started_at
    assert (second.details["replaced"], second.details["no_longer_sent_new"]) == (1, 1)


@responses.activate
def test_a_copy_sent_beside_the_old_row_for_a_night_replaces_it_once_the_old_row_goes():
    """One whole read returns row 1 and its copy, row 9 (the same report, indicator, PRP id and P-code, a smaller
    value); the next returns row 9 alone. Row 9 was first written the night before, yet it is row 1's copy: row 1
    goes, and Reached never takes its old larger value."""
    fake = FakeDatamart([row(1, value=50)])
    fake.register(REPORTS)
    run()
    fake.records = [row(1, value=50), row(9, value=20)]
    run()
    fake.records = [row(9, value=20)]
    third = run()
    assert set(held()) == {9} and held()[9].not_sent_since is None
    assert (third.details["replaced"], third.details["no_longer_sent_new"]) == (1, 0)
    assert reached_now().reached == 20


@responses.activate
def test_a_copy_first_written_by_a_read_that_is_not_whole_replaces_the_old_row_at_the_next_whole_read():
    """A read that is not whole writes row 9, row 1's copy, and removes nothing; the next whole read no longer
    returns row 1: row 9 replaces it, and Reached counts the copy's value, not the old one beside it."""
    fake = FakeDatamart([row(1, value=50)])
    fake.register(REPORTS)
    run()
    fake.records = [row(9, value=20), row(3, number="QPR3", start="2026-07-01", end="2026-09-30")]
    fake.cap, fake.cut_count = 1, False  # announces 2, returns 1: not whole
    second = run()
    assert second.details["removal_skipped"] is True and set(held()) == {1, 9}
    fake.cap = None
    third = run()
    assert set(held()) == {3, 9}
    assert (third.details["replaced"], third.details["no_longer_sent_new"]) == (1, 0)
    assert reached_now().reached == 30


@responses.activate
def test_a_reload_that_renumbers_rows_during_a_read_that_is_not_whole_leaves_no_old_row_marked():
    """The Datamart reloads (new ids for every row) while NeuroDB reads: that read is not whole and writes 4 of
    the 5 copies; the next whole read returns all 5. Every old row is replaced, none is kept as no longer sent."""

    def reports_under(first_id: int) -> list[dict]:
        return [
            row(first_id + n, number=f"QPR{n}", start=f"2026-0{n}-01", end=f"2026-0{n}-28")
            for n in range(1, 6)
        ]

    fake = FakeDatamart(reports_under(0))
    fake.register(REPORTS)
    run()
    fake.records = reports_under(100)
    fake.cap, fake.cut_count = 4, False  # announces 5, returns 4: not whole
    second = run()
    assert second.details["removal_skipped"] is True and len(held()) == 9
    fake.cap = None
    third = run()
    assert set(held()) == {101, 102, 103, 104, 105}
    details = third.details
    assert (details["replaced"], details["no_longer_sent_new"], details["no_longer_sent"]) == (5, 0, 0)


@responses.activate
def test_a_copy_of_one_indicator_written_earlier_replaces_its_own_row_never_another_sharing_its_title():
    """Two PD indicators share a title and an output (PRP ids 501 and 502). A read that is not whole writes row 9,
    the copy of the first's row 1. The next whole read returns row 9 alone: it replaces row 1, and the second
    indicator's row 2 is kept and marked."""
    fake = FakeDatamart([shared(1, "501"), shared(2, "502")])
    fake.register(REPORTS)
    run()
    fake.records = [shared(9, "501"), shared(2, "502")]
    fake.cap, fake.cut_count = 1, False  # announces 2, returns 1: not whole
    run()
    fake.cap, fake.records = None, [shared(9, "501")]
    third = run()
    assert set(held()) == {2, 9} and held()[2].not_sent_since == third.started_at
    assert (third.details["replaced"], third.details["no_longer_sent_new"]) == (1, 1)
    assert reports()[0]["indicators"] == 2


@responses.activate
def test_a_read_that_is_not_whole_never_deletes_a_row_of_this_country():
    """Rows 1 and 2 are this country's. Tonight's read is cut (3 records announced, 2 returned) and returns
    Datamart id 2 naming another country, as a Datamart that ignores the country filter sends every country's:
    a read not whole removes nothing, and row 2's stored record is still this country's."""
    fake = FakeDatamart([row(1), row(2, number="QPR2", start="2026-04-01", end="2026-06-30")])
    fake.register(REPORTS)
    run()
    fake.records = [
        row(1),
        row(2, number="QPR2", start="2026-04-01", end="2026-06-30", country_name="Syria"),
        row(3, number="QPR3", start="2026-07-01", end="2026-09-30"),
    ]
    fake.cap, fake.cut_count, fake.ignore = 2, False, {"country_name"}
    second = run()
    assert second.details["removal_skipped"] is True and "other_country_removed" not in second.details
    assert set(held()) == {1, 2} and held()[2].not_sent_since is None


@responses.activate
def test_a_whole_read_that_returns_a_row_s_id_naming_another_country_keeps_it_marked():
    """A whole read that returns Datamart id 2 naming another country: the row NeuroDB holds under it (this
    country's) is no longer sent, so it is kept and marked like any other, never deleted as another country's."""
    fake = FakeDatamart([row(1), row(2, number="QPR2", start="2026-04-01", end="2026-06-30")])
    fake.register(REPORTS)
    run()
    fake.records = [row(1), row(2, number="QPR2", start="2026-04-01", end="2026-06-30", country_name="Syria")]
    fake.ignore = {"country_name"}
    second = run()
    assert second.details["other_country_skipped"] == 1 and "other_country_removed" not in second.details
    assert held()[2].not_sent_since == second.started_at and second.details["no_longer_sent_new"] == 1


@responses.activate
def test_check_10_and_data_health_count_the_marked_rows_after_a_read_not_whole_or_an_empty_answer():
    """A whole read marks QPR2; a read that is not whole, then an empty answer, mark nothing, but QPR2's row is
    still kept, marked and counted on every page: check 10 and Data health say so, as Raw eTools data does."""
    from neurodb.datamart import checks
    from neurodb.reports.services import quality_row

    fake = FakeDatamart([row(1), row(2, number="QPR2", start="2026-04-01", end="2026-06-30")])
    fake.register(REPORTS)
    run()
    fake.records = [row(1)]
    run()  # QPR2 marked
    fake.records = [row(3, number="QPR3", start="2026-07-01", end="2026-09-30"), row(1)]
    fake.cap, fake.cut_count = 1, False  # announces 2, returns 1: not whole
    third = run()
    fake.records, fake.cap = [], None
    fourth = run()  # an empty answer
    assert raw.no_longer_sent()["rows"] == 1
    for found in (third, fourth):
        said, figures, _, _ = checks._partner_read(found)
        assert dict(figures)["… rows no longer sent (kept and counted)"] == 1
        assert "; 1 row marked no longer sent stays kept and counted." in said, said
        assert quality_row("partner_reports", found, 3)["no_longer_sent"] == 1
    assert "(removal skipped); 1 row marked" in checks._partner_read(third)[0]


@responses.activate
def test_check_10_says_what_each_whole_read_marked_and_how_many_are_kept_in_all():
    """Three whole reads: 2 rows, then 1 (QPR2 marked), then the same 1 again (nothing marked). Check 10 says the
    second marked 1, and the third none, of the 1 row kept in all."""
    from neurodb.datamart import checks

    fake = FakeDatamart([row(1), row(2, number="QPR2", start="2026-04-01", end="2026-06-30")])
    fake.register(REPORTS)
    run()
    fake.records = [row(1)]
    second = run()
    third = run()
    assert (third.details["no_longer_sent_new"], third.details["no_longer_sent"]) == (0, 1)
    assert "marked 1 no longer sent (kept and counted)" in checks._partner_read(second)[0]
    said = checks._partner_read(third)[0]
    assert "marked 0 no longer sent (1 in all, kept and counted)" in said and "marked 1 " not in said


@responses.activate
def test_after_a_failed_read_data_health_and_check_10_still_count_the_rows_kept():
    """A whole read marks QPR2; the next read fails (HTTP 500) and records nothing of them. QPR2's row is still
    kept and counted on every page: Data health and check 10 say so, as Raw eTools data does."""
    from neurodb.datamart import checks
    from neurodb.reports.services import datamart_quality

    fake = FakeDatamart([row(1), row(2, number="QPR2", start="2026-04-01", end="2026-06-30")])
    fake.register(REPORTS)
    run()
    fake.records = [row(1)]
    run()  # QPR2 marked
    responses.reset()
    responses.add(
        responses.GET, re.compile(rf"{re.escape(REPORTS)}(\?.*)?$"), status=500, json={"detail": "x"}
    )
    failed = run()
    assert failed.status == SyncRun.Status.FAILED and "no_longer_sent" not in (failed.details or {})
    assert raw.no_longer_sent()["rows"] == 1
    health = next(d for d in datamart_quality() if d["name"] == "partner_reports")
    assert health["run"] == failed and health["no_longer_sent"] == 1
    said, figures, _, _ = checks._partner_read(failed)
    assert dict(figures)["… rows no longer sent (kept and counted)"] == 1
    assert "; 1 row marked no longer sent stays kept and counted." in said, said
