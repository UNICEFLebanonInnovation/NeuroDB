"""The eTools reporting checks 8 to 11 say what NeuroDB holds of PRP's indicator reports and what their last
sync did (never "run the sync" after a sync that cannot help), date PRP's records by their progress report
even without a period end of their own, count what the partner reports feed holds of the running programme
documents (a first report missing, a report number missing between two held, nothing owed for an ended
period), and list each section name once. The Section filter of Progress reports offers single names."""

# ruff: noqa: F401, F811 (the fixture "funded" comes from test_r4_views)
from __future__ import annotations

import datetime

import pytest
from django.utils import timezone

from neurodb.core.models import SyncRun
from neurodb.datamart import models as dm
from neurodb.datamart import reporting
from neurodb.partnerships.models import PCA
from tests.reports.test_r4_views import funded
from tests.reports.test_reporting_checks_values import D, figures, one, other_pd, pd, report_row

pytestmark = pytest.mark.django_db
COLUMNS = ["Accepted", "Submitted", "Sent back", "Due", "Overdue", "Not yet due", "Expected"]


def prp_run(status=SyncRun.Status.SUCCEEDED, **fields):
    """The last sync of PRP's indicator reports, as the eTools Datamart sync records it."""
    fields.setdefault("started_at", timezone.make_aware(datetime.datetime(2026, 10, 14, 20, 30)))
    return SyncRun.objects.create(
        job=SyncRun.Job.ETOOLS_DATAMART, target="prp_indicator_reports", status=status, **fields
    )


def document(pd, key, end, source_id=None, **data):
    return dm.DatamartDocument.objects.create(
        dataset="prp_indicator_reports",
        record_key=key,
        source_id=source_id,
        intervention=pd,
        date=end,
        data=data,
    )


def table(found) -> dict:
    return {(r[0], r[1]): dict(zip(COLUMNS, r[2:], strict=True)) for r in found["table"]["rows"]}


# ----------------------------------------------------------------------------------- checks 8 and 9
def test_checks_8_and_9_say_the_dataset_holds_no_record_and_what_its_sync_did(db):
    for key in ("8", "9"):
        found = one(key)
        assert "hold no record" in found["verdict"] and "never been synced" in found["verdict"]
        assert figures(found)["PRP indicator report records held"] == 0 and found["tone"] == "info"
    prp_run(details={"documents": 0, "empty_response": True, "business_area": "2490", "datamart_count": 0})
    found = one("8")
    assert "returned no record (it announced 0)" in found["verdict"] and "2490" in found["verdict"]
    assert "never been synced" not in found["verdict"] and "changes nothing" in found["verdict"]


def test_check_8_tells_records_without_an_id_from_ids_that_match_nothing(pd):
    report_row(pd, indicator_report="900", total={"c": 10})
    document(pd, "a", D(2026, 6, 30), progress_value="10")  # no PRP id
    found = one("8")
    assert (
        figures(found)["PRP indicator report records held"],
        figures(found)["… with a PRP id (source_id)"],
    ) == (1, 0)
    assert "none with a PRP id" in found["verdict"] and found["tone"] == "warning"
    dm.DatamartDocument.objects.update(source_id=123456)
    found = one("8")
    assert (
        "match none of the 1 partner indicator report ids" in found["verdict"] and found["tone"] == "warning"
    )
    dm.DatamartDocument.objects.update(source_id=900)
    found = one("8")
    assert figures(found)["… equal"] == 1 and found["tone"] == "ok"


def test_check_8_counts_partner_ids_that_are_not_whole_numbers(pd):
    report_row(pd, indicator_report="IR-5", total={"c": 10})
    document(pd, "a", D(2026, 6, 30), source_id=5, progress_value="10")
    found = one("8")
    assert figures(found)["… that are not"] == 1 and "1 id is of another kind" in found["verdict"]


def test_check_9_says_when_no_record_carries_a_contribution(pd):
    document(pd, "a", D(2026, 6, 30), report_type="QPR")
    found = one("9")
    assert "1 PRP record is held; none carries a financial contribution" in found["verdict"]


def test_check_9_reads_the_contribution_once_per_progress_report(funded):
    found = one("9")
    numbers = figures(found)
    assert numbers["PRP indicator report records held"] == 5
    assert (
        numbers["Progress reports with a financial contribution"] == 3 and numbers["… where it varies"] == 0
    )
    assert found["tone"] == "ok"


# -------------------------------------------------------------------------------------------- check 11
def test_check_11_without_a_sync_says_so_and_never_blames_a_sync_that_ran(pd):
    report_row(pd)
    found = one("11")
    assert "never been synced" in found["verdict"] and "Sync eTools Datamart now" in found["verdict"]
    assert figures(found)["… skipped as another business area"] == "—"
    prp_run(
        details={
            "documents": 0,
            "empty_response": True,
            "business_area": "2490",
            "business_area_from": "admin",
        }
    )
    found = one("11")
    assert "returned no record" in found["verdict"] and "entered in the admin" in found["verdict"]
    assert "never been synced" not in found["verdict"] and found["tone"] == "info"


def test_check_11_names_a_failed_sync_and_its_error(pd):
    report_row(pd)
    prp_run(SyncRun.Status.FAILED, error="IntegrationError: no business area code for 'the country'")
    found = one("11")
    assert "failed (IntegrationError: no business area code" in found["verdict"]
    assert "never been synced" not in found["verdict"] and found["tone"] == "warning"


def test_check_11_lists_the_business_areas_of_records_all_of_another_one(pd):
    report_row(pd)
    prp_run(details={"documents": 0, "other_country_skipped": 12, "business_areas_seen": {"2340": 12},
                     "business_area": "2490"})  # fmt: skip
    found = one("11")
    assert "all of another business area" in found["verdict"] and found["tone"] == "warning"
    assert figures(found)["… skipped as another business area"] == 12
    assert figures(found)["Last sync of PRP's indicator reports: records read"] == 12
    (codes,) = [t for t in found["tables"] if t["title"].startswith("Business area codes")]
    assert codes["rows"] == [("2340", 12)]


def test_check_11_says_the_records_held_are_of_other_years(pd):
    report_row(pd)
    for n in range(3):
        document(pd, str(n), D(2024, 3 + n, 28))
    found = one("11")
    assert "3 of its records are held, none of a period ending in 2026" in found["verdict"]
    assert figures(found)["PRP indicator report records of other years"] == 3


def test_check_11_dates_a_record_by_its_progress_report_when_it_has_no_end_of_its_own(pd):
    report_row(pd)
    document(pd, "1", None, pr_end_date="2026-06-30", pr_status="Acc")
    document(pd, "2", None, report_status="Due")  # no period end at all
    found = one("11")
    assert table(found)[("Jun 2026", "PRP indicator reports")]["Accepted"] == 1
    assert figures(found)["PRP indicator report records with no period end"] == 1


def test_check_11_says_when_the_feed_carries_nothing_owed_for_an_ended_period(pd):
    report_row(pd, "QPR1", end=D(2026, 6, 30))  # accepted
    report_row(pd, "QPR2", end=D(2026, 12, 31), status="Due")  # its period runs: not yet due
    found = one("11")
    assert figures(found)["Due or overdue in the partner reports"] == 0
    assert figures(found)["Not yet due in the partner reports"] == 1
    assert "carries no report still owed for a period that has ended" in found["verdict"]
    assert found["tone"] == "warning"
    # a report of an ended period, past its due date: owed, and the warning goes
    report_row(other_pd(), "QPR1", end=D(2026, 8, 31), status="Due")
    found = one("11")
    assert figures(found)["Due or overdue in the partner reports"] == 1
    assert "carries no report still owed" not in found["verdict"]


def test_check_11_estimates_the_reports_missing_between_the_numbers_held(pd):
    # QPR1 (June) and QPR3 (December) held: QPR2 is missing, its period ended in September
    report_row(pd, "QPR1", end=D(2026, 6, 30))
    report_row(pd, "QPR1", end=D(2026, 6, 30), title="Teachers trained")
    report_row(pd, "QPR3", end=D(2026, 12, 31), start=D(2026, 10, 1), status="Due")
    found = one("11")
    numbers = figures(found)
    assert numbers["… progress reports missing between the numbers held"] == 1
    assert numbers["Expected, not in the partner reports (number gaps, period ended)"] == 2  # as QPR1
    assert table(found)[("Sep 2026", "Partner reports")]["Expected"] == 2
    assert table(found)[("Sep 2026", "PRP indicator reports")]["Expected"] is None
    assert "expected, not in the feed" in found["verdict"] and found["tone"] == "warning"


def test_check_11_number_gaps_per_type(pd):
    for n, end in ((1, D(2026, 1, 31)), (2, D(2026, 2, 28)), (4, D(2026, 4, 30))):
        report_row(pd, f"HR{n}", end=end)
    for n, end in ((1, D(2026, 3, 31)), (2, D(2026, 6, 30)), (3, D(2026, 9, 30))):
        report_row(pd, f"QPR{n}", end=end)
    for n, end in ((1, D(2026, 3, 31)), (3, D(2026, 9, 30))):
        report_row(pd, f"SR{n}", end=end)  # special reports have no rhythm: never a gap
    found = one("11")
    assert figures(found)["… progress reports missing between the numbers held"] == 1  # HR3
    assert table(found)[("Mar 2026", "Partner reports")]["Expected"] == 1


# -------------------------------------------------------------------------------------------- check 10
def test_check_10_lists_each_section_once_the_most_reported_first(pd):
    for n, section in enumerate(
        (
            "Adolescent & Youth, Child Protection",
            "Child Protection, Adolescent & Youth, Social & Behavioral Change",
            " child protection",
            "Child Protection",
            "PSEA",
        )
    ):
        report_row(pd, indicator_report=str(n), section=section)
    found = one("10")
    (line,) = [r for r in found["table"]["rows"] if r[1] == "running"]
    assert line[6] == "Child Protection, Adolescent & Youth, PSEA, Social & Behavioral Change"
    assert found["table"]["columns"][6] == "Sections (top 5)"
    for n, section in enumerate(("Education", "Health", "WASH")):
        report_row(pd, indicator_report=f"x{n}", section=section)
    (line,) = [r for r in one("10")["table"]["rows"] if r[1] == "running"]
    assert line[6].endswith("(+2 more)") and line[6].count("Child Protection") == 1


def test_check_10_says_how_much_of_the_running_pds_reporting_is_held(pd):
    report_row(pd, "QPR1", end=D(2026, 3, 31))  # from the first report: complete
    late = other_pd("LEB/PD/LATE")  # started on 1 January, first report held ends 31 May
    report_row(late, "QPR1", end=D(2026, 5, 31))
    gap = other_pd("LEB/PD/GAP")  # only QPR2 and QPR3 held
    report_row(gap, "QPR2", end=D(2026, 3, 31))
    report_row(gap, "QPR3", end=D(2026, 6, 30))
    other_pd("LEB/PD/NONE")  # no row at all
    SyncRun.objects.create(
        job=SyncRun.Job.ETOOLS_DATAMART,
        target="partner_reports",
        status=SyncRun.Status.SUCCEEDED,
        finished_at=timezone.now(),
        rows_in=4,
        details={"read_from": "2024-01-01"},
    )
    found = one("10")
    numbers = figures(found)
    assert numbers["Running programme documents"] == 4
    assert numbers["… with no partner report row"] == 1
    assert numbers["… whose first report held ends over 100 days after their start"] == 1  # LATE
    assert numbers["… without their first report (QPR1 or HR1)"] == 1
    assert "from 1 Jan 2024 on and kept 4 rows" in found["verdict"] and found["tone"] == "warning"


# ------------------------------------------------------------------------------- the section names
def test_section_names_split_lists_and_keep_a_known_name_whole():
    assert reporting.section_names("Child Protection, Adolescent & Youth; PSEA") == [
        "Child Protection",
        "Adolescent & Youth",
        "PSEA",
    ]
    assert reporting.section_names("Child Protection , child  protection") == ["Child Protection"]
    known = ["WASH / Water, Sanitation and Hygiene"]
    assert reporting.section_names("PSEA, WASH / Water,Sanitation and Hygiene", known) == [
        "PSEA",
        "WASH / Water, Sanitation and Hygiene",
    ]
    assert reporting.section_names("") == [] and reporting.section_names(None) == []


def test_the_progress_reports_section_filter_offers_single_names(pd):
    PCA.objects.filter(pk=pd.pk).update(section_names=["Child Protection"])
    report_row(pd, indicator_report="1", section="Child Protection, Adolescent & Youth")
    report_row(pd, indicator_report="2", section="Adolescent & Youth; PSEA")
    report_row(pd, indicator_report="3", section="child protection")
    options = reporting.filter_options()["sections"]
    assert options == ["Adolescent & Youth", "Child Protection", "PSEA"]
    rows = dm.ReportedIndicator.objects.filter(reporting.section_q(["PSEA"]))
    assert set(rows.values_list("indicator_report", flat=True)) == {"2"}
    rows = dm.ReportedIndicator.objects.filter(reporting.section_q(["Child Protection"]))
    assert set(rows.values_list("indicator_report", flat=True)) == {"1", "3"}
