"""The eTools reporting checks 8 to 11 say what NeuroDB holds of PRP's indicator reports and what their last
sync did (never "run the sync" after a sync that cannot help), date PRP's records by their progress report
even without a period end of their own, count what the partner reports feed holds of the running programme
documents (a first report missing, a report number missing between two held, nothing owed for an ended
period), and list each section name once. The Section filter of Progress reports offers single names."""

# ruff: noqa: F401, F811 (the fixture "funded" comes from test_r4_views)
from __future__ import annotations

import datetime
import json

import pytest
from django.utils import timezone

from neurodb.core.models import SyncRun
from neurodb.datamart import checks, reporting
from neurodb.datamart import models as dm
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
    assert "Admin → Data and sync → eTools Datamart: PRP business area code" in found["verdict"]


def test_checks_8_9_and_11_say_a_read_cut_short_may_be_helped_by_a_new_sync(pd):
    """The first page announced 500 records and the answer returned none: never "syncing again changes
    nothing", but that the answer stopped after 0 of the 500 and how to sync again."""
    report_row(pd)
    prp_run(details={"documents": 0, "empty_response": True, "business_area": "2490", "datamart_count": 500,
                     "read": 0})  # fmt: skip
    for key in ("8", "9", "11"):
        verdict = one(key)["verdict"]
        assert "changes nothing" not in verdict and "it announced 500 records" in verdict
        assert "its answer stopped after 0" in verdict and "a new sync may help" in verdict
        assert "Sync eTools Datamart now" in verdict


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


def test_checks_8_9_and_11_say_a_name_is_not_a_business_area_code(pd, settings):
    """Production's run: the hosting team set the country's name, the Datamart was asked for it and announced
    0. The checks say so, and which code the next sync takes instead: never "syncing again changes
    nothing"."""
    settings.ETOOLS_DATAMART_BUSINESS_AREA = "Lebanon"
    report_row(pd)
    prp_run(details={"documents": 0, "empty_response": True, "business_area": "Lebanon",
                     "business_area_from": "environment", "datamart_count": 0, "read": 0})  # fmt: skip
    for key in ("8", "9", "11"):
        verdict = one(key)["verdict"]
        assert "changes nothing" not in verdict, key
        assert (
            "asked the Datamart for business area Lebanon (set by the hosting team) and it returned no record (it "
            "announced 0). “Lebanon” is a name, not a business area code (a code is digits, as 2490), so the "
            "Datamart found nothing. The next sync sets it aside and finds no code: enter it in Admin → Data and "
            "sync → eTools Datamart: PRP business area code."
        ) in verdict, key
    dm.DatamartDocument.objects.create(
        dataset="workspaces", record_key="1", data={"name": "Lebanon", "business_area_code": "2490"}
    )
    verdict = one("8")["verdict"]
    assert (
        "The next sync sets it aside and uses 2490 (found in the workspace NeuroDB holds); a code entered in "
        "Admin → Data and sync → eTools Datamart: PRP business area code comes first."
    ) in verdict
    # the next run read with the code NeuroDB found, and still nothing: the admin's code is pointed to
    prp_run(details={"documents": 0, "empty_response": True, "business_area": "2490", "datamart_count": 0,
                     "business_area_from": "workspace_held", "business_area_found": True,
                     "business_area_set_aside": {"environment": "Lebanon"}},
            started_at=timezone.make_aware(datetime.datetime(2026, 10, 15, 20, 30)))  # fmt: skip
    verdict = one("8")["verdict"]
    assert (
        "asked the Datamart for business area 2490 (found in the workspace NeuroDB holds) and it returned no "
        "record (it announced 0): enter the right code in Admin → Data and sync → eTools Datamart: PRP business "
        "area code (it comes first), or see what the Datamart holds with Run a job → Probe a Datamart dataset. "
        "The hosting team's ETOOLS_DATAMART_BUSINESS_AREA (“Lebanon”) is not a business area code: it was set "
        "aside."
    ) in verdict


def _progress_run(**details):
    """The last sync of PRP's own progress reports."""
    return SyncRun.objects.create(
        job=SyncRun.Job.ETOOLS_DATAMART,
        target="prp_progress_reports",
        status=SyncRun.Status.SUCCEEDED,
        started_at=timezone.make_aware(datetime.datetime(2026, 10, 14, 20, 30)),
        details=details,
    )


def test_check_11_names_the_code_of_prp_progress_reports_and_what_was_skipped(pd, settings):
    settings.ETOOLS_DATAMART_BUSINESS_AREA = "Lebanon"
    report_row(pd)
    _progress_run(business_area="Lebanon", business_area_from="environment", datamart_count=0, read=0,
                  empty_response=True)  # fmt: skip
    verdict = one("11")["verdict"]
    assert (
        "PRP's own progress reports list none of a running programme document for 2026: their last sync (14 Oct "
        "2026) asked for business area Lebanon (set by the hosting team), a name, not a business area code: the "
        "Datamart announced 0 and NeuroDB kept none. “Lebanon” is a name, not a business area code"
    ) in verdict
    assert "kept no record, so syncing again changes nothing" not in verdict
    SyncRun.objects.filter(target="prp_progress_reports").delete()
    _progress_run(business_area="2490", business_area_from="admin", datamart_count=3, read=3,
                  other_country_skipped=3, unnamed_skipped=2, empty_response=True)  # fmt: skip
    verdict = one("11")["verdict"]
    assert (
        "their last sync (14 Oct 2026) asked for business area 2490 (entered in the admin): the Datamart "
        "announced 3 and NeuroDB kept none (1 record of another business area; 2 named no business area and no "
        "PRP programme document NeuroDB holds). Syncing again changes nothing; see what the Datamart holds with "
        "Run a job → Probe a Datamart dataset."
    ) in verdict


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
    assert found["tone"] == "warning"
    assert (
        "asked for the reports from 1 Jan 2024 on: the Datamart announced no count, NeuroDB read 4, kept 4 rows "
        "and removed none." in found["verdict"]
    )
    (why,) = [t for t in found["tables"] if t["title"].startswith("Running programme documents with no")]
    assert why["rows"] == [("No active PD indicator", 1, "unknown (check 8)")]
    (group,) = found["groups"]
    assert group["summary"] == "No active PD indicator (1)"
    assert group["rows"] == [("LEB/PD/NONE", "PD", "1 Jan 2026", "31 Dec 2027", None, "unknown (check 8)", 0)]


def _partner_run(status=SyncRun.Status.SUCCEEDED, rows_in=0, **details):
    return SyncRun.objects.create(
        job=SyncRun.Job.ETOOLS_DATAMART,
        target="partner_reports",
        status=status,
        started_at=timezone.make_aware(datetime.datetime(2026, 10, 14, 22, 0)),
        finished_at=timezone.now(),
        rows_in=rows_in,
        details={"read_from": "2018-11-01", **details},
    )


def test_check_10_reads_the_windows_and_warns_of_a_cap(pd):
    report_row(pd, "QPR1", end=D(2026, 3, 31))
    windows = [
        {"from": "2018-11-01", "to": "2026-01-01", "announced": 4_000, "read": 4_000, "pages": 8, "outside": 0,
         "whole": True},
        {"from": "2026-01-01", "to": "", "announced": 4_100, "read": 4_100, "pages": 9, "outside": 0, "whole": True},
    ]  # fmt: skip
    _partner_run(rows_in=8_070, datamart_count=10_000, read=8_100, other_country_skipped=30, removed=12,
                 held_before=9_970, window_bound="reporting_period_start_date", windows=windows, windows_read=2,
                 windows_not_whole=0, whole_count_capped=True, whole_from="2018-11-01")  # fmt: skip
    found = one("10")
    verdict = found["verdict"]
    assert (
        "The latest partner reports sync (14 Oct 2026, Succeeded) asked for the reports from 1 Nov 2018 on: the "
        "Datamart announced 10,000, NeuroDB read 8,100 (30 of another country), kept 8,070 rows and removed 12."
    ) in verdict
    assert (
        "It read them in 2 windows by reporting period start, every one whole (the Datamart's own count, 10,000, "
        "was cut)." in verdict
    )
    numbers = figures(found)
    assert (numbers["… windows read"], numbers["… history held whole from"]) == (2, "1 Nov 2018")
    (read,) = [
        t for t in found["tables"] if t["title"] == "The latest partner reports read, window by window"
    ]
    assert read["rows"][1] == ("2026-01-01", "open", 4_100, 4_100, 9, 0, "yes")

    SyncRun.objects.all().delete()
    _partner_run(SyncRun.Status.PARTIAL, rows_in=9_970, datamart_count=10_000, read=10_000,
                 other_country_skipped=30, removed=0, removal_skipped=True, cap_suspected="windows_unproven",
                 window_bound="")  # fmt: skip
    found = one("10")
    assert found["tone"] == "warning"
    assert "kept 9,970 rows and removed none (removal skipped)." in found["verdict"]
    assert (
        "Exactly 10,000 is a limit of the Datamart's answer, not what it holds, and the Datamart ignores the "
        "window filters, so NeuroDB could not read it whole: tell the eTools Datamart team."
        in found["verdict"]
    )


def test_check_10_explains_the_running_pds_without_rows(pd):
    """One running PD per reason (the first that applies); a PD nothing explains makes the check a warning,
    and the reasons add up to the PDs with no row. Below the counts, one list per reason names every PD: its
    reference number, type, dates, sections, whether PRP's programme documents hold it and its active PD
    indicators."""
    report_row(
        pd, "QPR1", start=D(2025, 3, 1), end=D(2025, 5, 31)
    )  # the earliest period start held: 1 Mar 2025

    def running(number, start, *, end=None, kind="PD", indicator=True, high=False):
        found = other_pd(number)
        PCA.objects.filter(pk=found.pk).update(start=start, end=end or D(2027, 12, 31), document_type=kind)
        if indicator:
            dm.PDIndicator.objects.create(datamart_id=found.pk + 50_000, intervention=found, title="Reached",
                                          is_high_frequency=high)  # fmt: skip
        return found

    running("LEB/PD/LATER", D(2026, 12, 1))
    running("LEB/PD/SOON", D(2026, 9, 15), high=True)  # its first monthly report ends after today
    running("LEB/SSFA/1", D(2026, 1, 1), kind="SSFA")
    running("LEB/PD/NOIND", D(2026, 1, 1), indicator=False)
    running("LEB/PD/OVER", D(2025, 6, 1), end=D(2026, 6, 30))
    running("LEB/PD/EARLY", D(2024, 6, 1))
    running("LEB/PD/MISSING", D(2026, 1, 1))
    running("LEB/PD/MISSING2", D(2026, 2, 1))
    PCA.objects.filter(number="LEB/PD/MISSING2").update(
        section_names=["Health", " Child  Protection", "", "WASH", "Education", "PSEA", "Youth"]
    )
    dm.DatamartDocument.objects.create(dataset="prp_programme_documents", record_key="1",
                                       intervention=PCA.objects.get(number="LEB/PD/MISSING"))  # fmt: skip
    found = one("10")
    assert found["tone"] == "warning"
    (why,) = [t for t in found["tables"] if t["title"].startswith("Running programme documents with no")]
    assert why["columns"] == ("Reason", "Running programme documents", "In PRP's programme documents")
    assert why["rows"] == [
        ("Signed, starting later", 1, 0),
        ("First report not due yet", 1, 0),
        ("A document type PRP does not report (not PD, SPD or SHPD)", 1, 0),
        ("No active PD indicator", 1, 0),
        ("End passed, status still running", 1, 0),
        ("Started before the earliest period start held (the read explains them)", 1, 0),
        ("Left: their reports should be in the feed", 2, 1),
    ]
    # one list per reason, in the reasons' order, each PD by reference number
    assert [g["summary"] for g in found["groups"]] == [f"{row[0]} ({row[1]})" for row in why["rows"]]
    assert found["groups"][0]["columns"] == (
        "PD reference number",
        "Document type",
        "Start",
        "End",
        "Sections (first 5)",
        "In PRP's programme documents",
        "Active PD indicators",
    )
    groups = {g["summary"]: g["rows"] for g in found["groups"]}
    assert groups["Left: their reports should be in the feed (2)"] == [
        ("LEB/PD/MISSING", "PD", "1 Jan 2026", "31 Dec 2027", None, "yes", 1),
        ("LEB/PD/MISSING2", "PD", "1 Feb 2026", "31 Dec 2027",
         "Health, Child Protection, WASH, Education, PSEA (+1 more)", "no", 1),
    ]  # fmt: skip
    assert groups["Signed, starting later (1)"] == [
        ("LEB/PD/LATER", "PD", "1 Dec 2026", "31 Dec 2027", None, "no", 1)
    ]
    (ssfa,) = groups["A document type PRP does not report (not PD, SPD or SHPD) (1)"]
    assert ssfa[:2] == ("LEB/SSFA/1", "SSFA")
    assert groups["No active PD indicator (1)"] == [
        ("LEB/PD/NOIND", "PD", "1 Jan 2026", "31 Dec 2027", None, "no", 0)
    ]
    assert groups["End passed, status still running (1)"][0][2:4] == ("1 Jun 2025", "30 Jun 2026")
    numbers = figures(found)
    assert numbers["… with no partner report row"] == 8 == sum(row[1] for row in why["rows"])
    assert numbers["… of which nothing explains (their reports should be in the feed)"] == 2
    assert "2 running programme documents have no row although nothing explains it" in found["verdict"]

    # every PD without a row explained: information
    PCA.objects.filter(number__startswith="LEB/PD/MISSING").delete()
    report_row(pd, "QPR2", start=D(2025, 6, 1), end=D(2025, 8, 31))
    assert one("10")["tone"] == "info"


def test_check_10_groups_name_no_person(pd):
    """The lists of running PDs with no row read a PD's reference number, type, dates and sections only: a
    focal point, the partner and the title are never in the result the job stores."""
    report_row(pd, "QPR1", end=D(2026, 3, 31))
    focal = other_pd("LEB/PD/FOCAL")
    PCA.objects.filter(pk=focal.pk).update(
        unicef_focal_points=["Focal Person"], section_names=["Health"], title="A title of the PD"
    )
    (stored,) = [c for c in checks.run_job("test").details["checks"] if c["key"] == "10"]
    (group,) = stored["groups"]
    assert group["rows"] == [
        ["LEB/PD/FOCAL", "PD", "1 Jan 2026", "31 Dec 2027", "Health", "unknown (check 8)", 0]
    ]
    text = json.dumps(stored)
    assert "Focal Person" not in text and "Partner A" not in text and "A title of the PD" not in text


def test_check_10_says_why_the_read_was_windowed_and_counts_cut_pages(pd):
    report_row(pd, "QPR1", end=D(2026, 3, 31))
    _partner_run(rows_in=9_990, datamart_count=9_990, read=9_990, window_bound="reporting_period_start_date",
                 windows_read=2, windows_not_whole=0, windows_why="read_short")  # fmt: skip
    found = one("10")
    assert (
        "It read them in 2 windows by reporting period start, because the last read returned fewer records than "
        "announced; every one was read whole." in found["verdict"]
    )
    SyncRun.objects.all().delete()
    _partner_run(SyncRun.Status.PARTIAL, rows_in=9_980, datamart_count=9_990, read=9_988, removed=0,
                 removal_skipped=True, pages_cut=2)  # fmt: skip
    found = one("10")
    assert found["tone"] == "warning" and figures(found)["… pages shorter than the first"] == 2
    assert (
        "2 pages came back shorter than the first: the answer was cut, none was removed." in found["verdict"]
    )


def test_check_10_and_11_say_which_business_area_filter_the_read_took(pd):
    report_row(pd, "QPR1", end=D(2026, 3, 31))
    proven = {"key": "business_area", "code": "2490", "why": "proven", "unfiltered": 780_905,
              "tried": [{"key": "business_area", "verdict": "proven", "count": 9_990}]}  # fmt: skip
    _partner_run(rows_in=9_990, datamart_count=9_990, area_filter=proven)
    found = one("10")
    assert (
        "It read by business area 2490 (filter business_area: 9,990 records with it, 780,905 without)."
        in found["verdict"]
    )
    assert figures(found)["… business area filter"] == "business_area"
    none = {"key": "", "code": "2490", "why": "none_proven", "unfiltered": 150_322,
            "tried": [{"key": "business_area_code", "verdict": "ignored", "count": 150_322},
                      {"key": "business_area", "verdict": "refused", "status": 400}]}  # fmt: skip
    _progress_run(business_area="2490", business_area_from="admin", datamart_count=150_322, read=150_322,
                  other_country_skipped=148_631, area_filter=none)  # fmt: skip
    found = one("11")
    assert (
        "The last sync of PRP's progress reports read with no business area filter: business_area_code is "
        "ignored (as many records with it as without), business_area is refused (HTTP 400)."
        in found["verdict"]
    )
    assert figures(found)["PRP progress reports: business area filter"] == "none"


def test_check_10_counts_history_from_the_feed_when_the_datamart_holds_no_earlier_report(pd):
    """The sync asked from 1 Nov 2018 and the rows held start on 1 Jan 2026: history is counted from 1 Jan 2026,
    information, not a warning, and a running PD that started before it (its first report held ends long after
    its start) is the partial-history rule's, not a late first report. Before the first sync that records where
    the feed starts, the old words stay."""
    report_row(pd, "QPR1", start=D(2026, 1, 1), end=D(2026, 3, 31))
    early = other_pd("LEB/PD/EARLY")
    PCA.objects.filter(pk=early.pk).update(start=D(2025, 6, 1))
    report_row(early, "QPR1", start=D(2026, 1, 1), end=D(2026, 3, 31))
    _partner_run(rows_in=2, datamart_count=2)  # a run made before feed_from was recorded
    found = one("10")
    numbers = figures(found)
    assert numbers["History counted from"] == "1 Nov 2018 (the date the sync read from)"
    assert numbers["… whose first report held ends over 100 days after their start"] == 1
    assert "more than a year after the date asked" in found["verdict"]

    SyncRun.objects.all().delete()
    _partner_run(rows_in=2, datamart_count=2, feed_from="2026-01-01")
    found = one("10")
    numbers = figures(found)
    assert (
        numbers["History counted from"]
        == "1 Jan 2026 (the earliest report the Datamart holds; asked from 1 Nov 2018)"
    )
    assert numbers["… whose first report held ends over 100 days after their start"] == 0
    assert (
        "The Datamart holds no partner report starting before 1 Jan 2026 (NeuroDB asked from 1 Nov 2018): history "
        "is counted from 1 Jan 2026, and the running programme document that started before it has a partial "
        "history (Reached is PRP's cumulative)." in found["verdict"]
    )
    assert "more than a year after the date asked" not in found["verdict"] and found["tone"] == "ok"


def test_check_5_says_the_datamart_holds_no_earlier_report(pd):
    """The PD started on 1 Jan 2026; the Datamart holds no partner report starting before 15 Apr 2026 (the sync
    asked from 1 Jan 2025): its indicator has a partial history, and check 5 says why."""
    report_row(
        pd, "QPR1", start=D(2026, 4, 15), end=D(2026, 6, 30), total={"c": 50}, total_cumulative_progress="80"
    )
    _partner_run(rows_in=1, datamart_count=1, read_from="2025-01-01", feed_from="2026-04-15")
    found = one("5")
    assert figures(found)["Indicators with a partial history (Reached is PRP's cumulative)"] == 1
    assert (
        "1 more have a partial history (their Reached is PRP's cumulative: the Datamart holds no partner report "
        "starting before 15 Apr 2026)" in found["verdict"]
    )


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
