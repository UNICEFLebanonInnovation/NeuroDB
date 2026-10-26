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
from neurodb.integrations.etools import datamart_sync as sync
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


def cell(day: str) -> str:
    """A date as check 10's tables write it, its day, month and year joined by non-breaking spaces: printed, a
    column breaks between words, and "1 Mar" over "2025" read as two values."""
    return day.replace(" ", "\u00a0")


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
    assert group["rows"] == [
        ("LEB/PD/NONE", "PD", cell("1 Jan 2026"), cell("31 Dec 2027"), None, "unknown (check 8)", 0)
    ]
    assert " " not in "".join(group["rows"][0][2:4])  # each date prints on one line


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
        ("LEB/PD/MISSING", "PD", cell("1 Jan 2026"), cell("31 Dec 2027"), None, "yes", 1),
        ("LEB/PD/MISSING2", "PD", cell("1 Feb 2026"), cell("31 Dec 2027"),
         "Health, Child Protection, WASH, Education, PSEA (+1 more)", "no", 1),
    ]  # fmt: skip
    assert groups["Signed, starting later (1)"] == [
        ("LEB/PD/LATER", "PD", cell("1 Dec 2026"), cell("31 Dec 2027"), None, "no", 1)
    ]
    (ssfa,) = groups["A document type PRP does not report (not PD, SPD or SHPD) (1)"]
    assert ssfa[:2] == ("LEB/SSFA/1", "SSFA")
    assert groups["No active PD indicator (1)"] == [
        ("LEB/PD/NOIND", "PD", cell("1 Jan 2026"), cell("31 Dec 2027"), None, "no", 0)
    ]
    assert groups["End passed, status still running (1)"][0][2:4] == (cell("1 Jun 2025"), cell("30 Jun 2026"))
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
        ["LEB/PD/FOCAL", "PD", cell("1 Jan 2026"), cell("31 Dec 2027"), "Health", "unknown (check 8)", 0]
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


def _documents_run(**details):
    """The last sync of PRP's programme documents."""
    return SyncRun.objects.create(
        job=SyncRun.Job.ETOOLS_DATAMART,
        target="prp_programme_documents",
        status=SyncRun.Status.SUCCEEDED,
        started_at=timezone.make_aware(datetime.datetime(2026, 10, 14, 20, 0)),
        details=details,
    )


def test_check_11_says_how_prp_progress_reports_are_linked_to_their_programme_document(pd):
    """PRP's programme documents held for the business area and those linked to an eTools PD NeuroDB holds, a
    warning when records of another business area name one of NeuroDB's PDs by reference number, and the key
    pair the progress reports were linked by (with the links whose dates disagree), or why none was proven and
    the keys the documents carry. Key names and counts only."""
    report_row(pd, "QPR1", end=D(2026, 3, 31))
    for n, linked_to in ((1, pd), (2, None)):
        dm.DatamartDocument.objects.create(dataset="prp_programme_documents", record_key=f"prp_id:{n}",
                                           intervention=linked_to)  # fmt: skip
    _documents_run(
        business_area="2490", business_area_from="admin", linked_to_etools=1, other_code_named_here=3
    )
    proven = {"linked": 2, "pd_not_held": 1, "pd_not_linked": 1, "pd_not_named": 5,
              "by": ["programme_document", "prp_id"], "proven": True,
              "pairs": [{"report": "programme_document", "pd": "prp_id", "linked": 3, "dated": 3, "agree": 2,
                         "verdict": "proven"}],
              "pd_keys": {"record_key": 2, "prp_id": 2, "external_id": 2}}  # fmt: skip
    _progress_run(business_area="2490", business_area_from="admin", progress_reports_pd_link=proven)
    found = one("11")
    verdict = found["verdict"]
    assert (
        "PRP's programme documents: 2 held for business area 2490, 1 linked to an eTools programme document "
        "NeuroDB holds. 3 records of another business area name a programme document of Lebanon by its "
        "reference number: the business area key may be wrong; probe the dataset (Run a job → Probe a Datamart "
        "dataset). PRP's progress reports are linked to their programme document by programme_document = "
        "prp_id: 2 of the 4 reports naming one (1 of those links ends outside their document's dates)."
    ) in verdict
    assert found["tone"] == "warning"
    found = figures(found)
    assert (
        found["PRP programme document records held"],
        found["… linked to an eTools programme document NeuroDB holds"],
    ) == (2, 1)
    assert found["… records of another business area naming one of NeuroDB's by reference number"] == 3
    assert (found["PRP progress reports linked by"], found["… of the reports naming one"]) == (
        "programme_document = prp_id",
        2,
    )

    SyncRun.objects.filter(target="prp_progress_reports").delete()
    none = {"pd_not_held": 2, "by": ["named", "record_key"], "proven": False, "why": "not_clear", "pairs": [],
            "pd_keys": {"record_key": 2, "prp_id": 2, "legacy_id": 2}}  # fmt: skip
    _progress_run(business_area="2490", business_area_from="admin", progress_reports_pd_link=none)
    found = one("11")
    assert (
        "No key pair links PRP's progress reports to their programme documents clearly (two key pairs link them "
        "otherwise, neither twice as often as the other): they are linked by the id each programme document is "
        "held under, 0 of the 2 reports naming one. The keys PRP's programme documents carry: prp_id (2), "
        "legacy_id (2); tell the eTools Datamart team which one holds the id the progress reports name."
    ) in found["verdict"]
    assert figures(found)["PRP progress reports linked by"] == "no key pair proven"
    assert "Partner A" not in json.dumps(found) and "LEB/PD/CHK1" not in json.dumps(found)

    # the keys holding the id were left out as a programme document that changed during the read came twice:
    # the next read's copy counts, so the eTools Datamart team is not asked which one holds it
    SyncRun.objects.filter(target="prp_progress_reports").delete()
    repeated = {"pd_not_held": 2, "by": ["named", "record_key"], "proven": False, "why": "repeated", "pairs": [],
                "pd_keys": {"record_key": 3, "external_id": 3, "prp_id": 3, "reference_number": 3},
                "not_unique": ["external_id", "prp_id", "reference_number"],
                "came_twice": ["external_id", "prp_id", "reference_number"]}  # fmt: skip
    _progress_run(business_area="2490", business_area_from="admin", progress_reports_pd_link=repeated)
    verdict = one("11")["verdict"]
    assert (
        "No key pair links PRP's progress reports to their programme documents clearly (no key of PRP's programme "
        "documents whose values are unique holds an id the progress reports name): they are linked by the id each "
        "programme document is held under, 0 of the 2 reports naming one. Left out, as a programme document that "
        "changed during the read that wrote it came twice: external_id, prp_id, reference_number (the next "
        "read's copy will count)."
    ) in verdict
    assert "tell the eTools Datamart team which one" not in verdict

    # a value two documents one read wrote share (their partner's id) hides no key: no next read changes it, so
    # the eTools Datamart team is still asked which key holds the id the progress reports name
    SyncRun.objects.filter(target="prp_progress_reports").delete()
    pds = [
        ("h1", {"external_id": "11", "partner_id": 5, "reference_number": "LEB/PD1"}, None),
        ("h2", {"external_id": "12", "partner_id": 5, "reference_number": "LEB/PD2"}, None),
    ]
    shared = sync.prove_pd_link(pds, [{"programme_document": n} for n in (77, 78)], written=[3, 3])
    _progress_run(business_area="2490", business_area_from="admin",
                  progress_reports_pd_link={**shared.details(), "pd_not_held": 2})  # fmt: skip
    verdict = one("11")["verdict"]
    assert (
        "No key pair links PRP's progress reports to their programme documents clearly (no key of PRP's programme "
        "documents holds an id the progress reports name): they are linked by the id each programme document is "
        "held under, 0 of the 2 reports naming one. The keys PRP's programme documents carry: partner_id (2), "
        "external_id (2), reference_number (2); tell the eTools Datamart team which one holds the id the progress "
        "reports name."
    ) in verdict
    assert "Left out" not in verdict and "next read" not in verdict


def test_check_11_alone_counts_each_prp_progress_report_once(pd):
    """Check 11's rule said PRP's progress reports were kept "for Ask NeuroDB", each counted once: Ask NeuroDB has
    no look-up by PD or partner for them, and it lists every version held, the older one a read not whole kept
    among them, until a whole read removes it. Counting each report once is this check's own."""
    from neurodb.assistant import tools
    from neurodb.datamart import query

    rule = " ".join(next(c for c in checks.CHECKS if c.key == "11").neurodb.split())
    assert (
        "This check counts each progress report once, as the latest read wrote it: an older version a read not "
        "whole kept is linked to none and counted nowhere here, while Ask NeuroDB, which has no look-up by PD or "
        "partner for them, lists it until a whole read removes it."
    ) in rule
    progress = query.dataset("prp_progress_reports")
    assert (progress.pd_link, progress.partner_link) == ("", False)
    night = timezone.make_aware(datetime.datetime(2026, 10, 13, 20, 30))
    qpr1 = {"programme_document": 77, "report_type": "QPR", "report_number": "QPR1", "end_date": "2026-03-31"}
    for n, status in enumerate(("Overdue", "Accepted")):
        SyncRun.objects.create(job=SyncRun.Job.ETOOLS_DATAMART, target="prp_progress_reports",
                               status=SyncRun.Status.PARTIAL, started_at=night + datetime.timedelta(days=n),
                               details={"removal_skipped": True})  # fmt: skip
        row = dm.DatamartDocument.objects.create(dataset="prp_progress_reports", record_key=f"hash {n}",
                                                 intervention=pd if n else None, date=D(2026, 3, 31),
                                                 data={**qpr1, "status": status})  # fmt: skip
        dm.DatamartDocument.objects.filter(pk=row.pk).update(
            synced_at=night + datetime.timedelta(days=n, minutes=5)
        )
    figures_11 = figures(one("11"))
    assert figures_11["PRP progress report records held"] == 2
    assert figures_11["… older versions of a report a later read wrote again (not counted)"] == 1
    asked = tools.run(
        "etools_query",
        {"dataset": "prp_progress_reports", "filters": {"programme_document": 77}, "group_by": "status"},
    )
    assert asked["matching_records"] == 2  # Ask NeuroDB: both versions, until a whole read removes the older


def test_check_11_counts_each_prp_progress_report_once_whatever_versions_are_held(pd):
    """PRP's progress reports carry no id: a report that changed (Due, then Accepted) while the reads were not
    whole is held twice, its old version under its old hash. The row an earlier read wrote is an older version
    of the report the latest read wrote again: check 11 counts the report once, as accepted, never also as
    overdue, and says how many older versions it left out."""
    report_row(pd, "QPR1", end=D(2026, 3, 31))
    night = timezone.make_aware(datetime.datetime(2026, 10, 13, 20, 30))
    qpr1 = {"programme_document": 77, "report_type": "QPR", "report_number": "QPR1", "end_date": "2026-03-31",
            "due_date": "2026-04-15"}  # fmt: skip
    versions = ({**qpr1, "status": "Due"}, {**qpr1, "status": "Accepted", "submission_date": "2026-04-10"})
    for n, data in enumerate(versions):
        SyncRun.objects.create(job=SyncRun.Job.ETOOLS_DATAMART, target="prp_progress_reports",
                               status=SyncRun.Status.PARTIAL, started_at=night + datetime.timedelta(days=n),
                               details={"removal_skipped": True})  # fmt: skip
        row = dm.DatamartDocument.objects.create(dataset="prp_progress_reports", record_key=f"hash {n}",
                                                 intervention=pd, date=D(2026, 3, 31), data=data)  # fmt: skip
        dm.DatamartDocument.objects.filter(pk=row.pk).update(
            synced_at=night + datetime.timedelta(days=n, minutes=5)
        )
    found = one("11")
    (by_end,) = [t for t in found["tables"] if t["title"].startswith("Progress reports of the running")]
    mine = {
        r[0]: dict(zip(COLUMNS[:6], r[2:], strict=True))
        for r in by_end["rows"]
        if r[1] == "PRP progress reports"
    }
    assert mine["Mar 2026"] == {**dict.fromkeys(COLUMNS[:6], 0), "Accepted": 1}
    numbers = figures(found)
    assert numbers["Progress reports due or overdue in PRP's progress reports"] == 0
    assert numbers["PRP progress report records held"] == 2
    assert numbers["… older versions of a report a later read wrote again (not counted)"] == 1
    assert numbers["PRP progress reports of the year linked to a programme document"] == 1
    assert "more reports still owed" not in found["verdict"]


def test_check_10_and_11_say_when_pages_shifted_during_the_read(pd):
    report_row(pd, "QPR1", end=D(2026, 3, 31))
    _partner_run(SyncRun.Status.PARTIAL, rows_in=9_982, datamart_count=780_905, read=780_903, removed=0,
                 removal_skipped=True, duplicates_read=2, pages_shifted={"twice": 2, "missing": 2})  # fmt: skip
    found = one("10")
    assert (
        "2 records came twice while the Datamart's pages were read: rows moved between pages, and then as many "
        "others did not come, or the Datamart holds them twice. Nothing was removed; the next read asks a stable "
        "order." in found["verdict"]
    )
    assert found["tone"] == "warning"
    _progress_run(business_area="2490", business_area_from="admin", datamart_count=150_295, read=150_221,
                  duplicates_read=74, held_twice=74, twice_identical=True, ordering={"key": "id"})  # fmt: skip
    assert (
        "PRP's progress reports, last read: The Datamart returned 74 identical records twice in a stable order "
        "(by id): it likely holds them twice; tell the eTools Datamart team. Nothing was removed."
        in one("11")["verdict"]
    )


def test_check_11_says_the_datamart_s_own_copies_of_a_whole_read(pd):
    """PRP's progress reports read whole: the 74 that came twice, every copy in one answer, are the Datamart's own
    copies, said as such, never as pages that shifted or a removal skipped."""
    report_row(pd, "QPR1", end=D(2026, 3, 31))
    _progress_run(business_area="2490", business_area_from="admin", datamart_count=151_307, documents=1_589,
                  duplicates_read=74, twice_identical=True,
                  copies_proven={"twice": 74, "same_page": 74, "asked_again": 0})  # fmt: skip
    verdict = one("11")["verdict"]
    assert (
        "PRP's progress reports, last read: The Datamart sent 74 records twice (identical), every copy in one of "
        "its answers (on one page): it holds them twice, as one answer never lists a row twice. NeuroDB keeps "
        "one of each." in verdict
    )
    assert "Nothing was removed" not in verdict and "rows moved between pages" not in verdict


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


# ------------------------------------------------- check 11: may the pages show PRP's progress reports still owed?
PROVEN = {"linked": 14, "pd_not_held": 0, "pd_not_linked": 0, "pd_not_named": 0,
          "by": ["programme_document", "prp_id"], "proven": True,
          "pairs": [{"report": "programme_document", "pd": "prp_id", "linked": 14, "dated": 14, "agree": 14,
                     "verdict": "proven"}],
          "pd_keys": {"record_key": 1, "prp_id": 1}}  # fmt: skip
SHOWN = {**PROVEN, "agrees": {"compared": 12, "same": 12, "elsewhere": 0, "dated": 11, "same_day": 11},
         "owed": 3, "owed_before_history": 1, "stale": 1, "twins": 1, "ove_read_due": 1, "lacking": 2,
         "unsent_other_word": 1, "passes": True, "days": 2, "shown": True, "shown_since": "2026-10-14",
         "shown_why": ""}  # fmt: skip
CAN_SHOW = "PRP's progress reports can be shown on the pages"
NOT_READ = (
    "no: the last sync of PRP's progress reports or of their programme documents failed or received nothing"
)


def test_check_11_says_the_proof_that_prp_s_progress_reports_can_be_shown(pd):
    """The last sync recorded the proof: the twelve reports the partner reports also have end on the same day,
    eleven submitted on both sides on the same day, on two days in a row. Check 11 gives its figures, and says the
    pages can show the three reports still owed. Counts only."""
    report_row(pd, "QPR1", end=D(2026, 3, 31))
    _progress_run(business_area="2490", business_area_from="admin", progress_reports_pd_link=SHOWN)
    found = one("11")
    numbers = figures(found)
    assert (
        numbers["PRP progress reports linked that the partner reports also have (same type and number)"] == 12
    )
    assert (numbers["… with the same period end"], numbers["… only under another programme document"]) == (
        12,
        0,
    )
    assert numbers["… with the same submission day (of those submitted on both sides)"] == "11 of 11"
    assert (
        numbers[
            "Reports still owed for a period that has ended, in PRP's progress reports, never received from the "
            "partner reports"
        ]
        == 3
    )
    assert numbers["… of which ending before the partner reports' history"] == 1
    assert numbers["… no longer returned by PRP's progress reports (not counted)"] == 1
    assert numbers["… two versions of one report in one read (counted once)"] == 1
    assert numbers["… PRP says Overdue, but no due date: read as Due"] == 1
    assert (
        numbers[
            "Accepted or Submitted reports of the year in PRP's progress reports, not in the partner reports"
        ]
        == 2
    )
    assert numbers["Reports eTools no longer sends whose status differs in PRP's progress reports"] == 1
    assert numbers[CAN_SHOW] == "yes, since 14 Oct 2026"
    assert (
        "PRP's progress reports are tied to their programme documents: of the 12 reports the partner reports also "
        "have, 12 end on the same day. NeuroDB can show the 3 reports still owed."
    ) in found["verdict"]
    assert "Partner A" not in json.dumps(found) and "LEB/PD/CHK1" not in json.dumps(found)


@pytest.mark.parametrize(
    ("verdict", "sentence", "figure"),
    [
        (
            {"passes": True, "days": 1, "shown": False, "shown_since": "", "shown_why": "one_day"},
            "PRP's progress reports are tied to their programme documents: of the 12 reports the partner reports "
            "also have, 12 end on the same day. The link passed every test on 14 Oct 2026: NeuroDB can show them "
            "once the next read agrees.",
            "no: the link passed every test on 14 Oct 2026 only, and must pass on the next day too",
        ),
        (
            {
                "agrees": {"compared": 12, "same": 3, "elsewhere": 2, "dated": 12, "same_day": 3},
                "passes": False,
                "days": 0,
                "shown": False,
                "shown_since": "",
                "shown_why": "periods",
            },  # fmt: skip
            "The link ties 14 reports, but only 3 of the 12 the partner reports also have end on the same day: "
            "NeuroDB does not show them on the pages.",
            "no: too few of the reports also in the partner reports end on the same day",
        ),
        (
            {
                "agrees": {"compared": 5, "same": 5, "elsewhere": 0, "dated": 5, "same_day": 5},
                "passes": False,
                "days": 0,
                "shown": False,
                "shown_since": "",
                "shown_why": "few",
            },  # fmt: skip
            "The link ties 14 reports, but only 5 of them are also in the partner reports, too few to check it (10 "
            "are needed): NeuroDB does not show them on the pages.",
            "no: too few of them are also in the partner reports to check the link",
        ),
        (
            {
                "agrees": {"compared": 12, "same": 12, "elsewhere": 0, "dated": 4, "same_day": 4},
                "passes": False,
                "days": 0,
                "shown": False,
                "shown_since": "",
                "shown_why": "few_days",
            },  # fmt: skip
            "The link ties 14 reports, but only 4 of the 12 the partner reports also have were submitted on both "
            "sides, too few to check the submission day (10 are needed): NeuroDB does not show them on the pages.",
            "no: too few reports were submitted on both sides to check the submission day",
        ),
        (
            {
                "agrees": {"compared": 12, "same": 12, "elsewhere": 0, "dated": 12, "same_day": 0},
                "passes": False,
                "days": 0,
                "shown": False,
                "shown_since": "",
                "shown_why": "days",
            },  # fmt: skip
            "The link ties 14 reports, but only 0 of the 12 submitted on both sides were submitted on the same "
            "day: NeuroDB does not show them on the pages.",
            "no: too few of the reports submitted on both sides were submitted on the same day",
        ),
    ],
)
def test_check_11_says_why_prp_s_progress_reports_are_not_shown(pd, verdict, sentence, figure):
    report_row(pd, "QPR1", end=D(2026, 3, 31))
    _progress_run(
        business_area="2490", business_area_from="admin", progress_reports_pd_link={**SHOWN, **verdict}
    )
    found = one("11")
    assert sentence in found["verdict"]
    assert figures(found)[CAN_SHOW] == figure


def test_check_11_never_shows_an_unproven_link_and_says_so(pd):
    report_row(pd, "QPR1", end=D(2026, 3, 31))
    unproven = {**SHOWN, "proven": False, "why": "not_clear", "by": ["named", "record_key"], "pairs": [],
                "passes": False, "days": 0, "shown": False, "shown_why": "not_proven"}  # fmt: skip
    _progress_run(business_area="2490", business_area_from="admin", progress_reports_pd_link=unproven)
    found = one("11")
    assert (
        "No key pair links PRP's progress reports to their programme documents clearly (two key pairs link them "
        "otherwise, neither twice as often as the other): they are linked by the id each programme document is "
        "held under, 14 of the 14 reports naming one. The keys PRP's programme documents carry: prp_id (1); tell "
        "the eTools Datamart team which one holds the id the progress reports name. NeuroDB does not show reports "
        "still owed until it is."
    ) in found["verdict"]
    assert figures(found)[CAN_SHOW] == "no: their link to the programme documents is not proven"
    # never compared over a link that is not proven
    assert (
        figures(found)["Reports eTools no longer sends whose status differs in PRP's progress reports"] == "—"
    )


def test_check_11_says_a_read_that_received_nothing_closes_the_link(pd):
    """A later sync of PRP's programme documents recorded no link (it failed before the link step): the proof the
    progress reports' sync recorded is not read over it, and the figures read "—"."""
    report_row(pd, "QPR1", end=D(2026, 3, 31))
    _progress_run(business_area="2490", business_area_from="admin", progress_reports_pd_link=SHOWN)
    _documents_run()
    SyncRun.objects.filter(target="prp_programme_documents").update(
        started_at=timezone.make_aware(datetime.datetime(2026, 10, 14, 21, 0)), status=SyncRun.Status.FAILED
    )
    found = one("11")
    numbers = figures(found)
    assert numbers[CAN_SHOW] == NOT_READ
    assert numbers["… with the same period end"] == "—"
    assert (
        "The last sync of PRP's progress reports or of their programme documents failed or received nothing: "
        "NeuroDB does not show reports still owed."
    ) in found["verdict"]


def test_check_11_counts_partner_report_rows_whose_id_and_number_name_other_pds(pd):
    """Two rows of one report name LEB/PD/OTHER1 by reference number while their eTools id linked them to
    LEB/PD/CHK1; an amendment's number (-2) is the same PD. A report kept although eTools no longer sends it is
    counted among the reports still owed of the partner reports."""
    report_row(pd, "QPR1", end=D(2026, 3, 31), reference="LEB/PD/OTHER1")
    report_row(pd, "QPR1", end=D(2026, 3, 31), reference="LEB/PD/OTHER1", title="Teachers trained")
    report_row(pd, "QPR2", end=D(2026, 6, 30), reference="LEB/PD/CHK1-2")
    report_row(pd, "QPR3", end=D(2026, 9, 30), status="Due", not_sent_since=timezone.now())
    numbers = figures(one("11"))
    assert (
        numbers["Partner report rows whose eTools id and reference number name different programme documents"]
        == 2
    )
    assert numbers["… progress reports they belong to"] == 1
    assert numbers["Progress reports due or overdue in the partner reports"] == 1
    assert numbers["… of which kept from earlier syncs (eTools no longer sends them)"] == 1
    assert numbers[CAN_SHOW] == NOT_READ


def test_check_11_says_when_the_proof_failed(pd):
    """The last sync linked the reports but its proof failed: check 11 says so, never "not checked yet"."""
    report_row(pd, "QPR1", end=D(2026, 3, 31))
    _progress_run(
        business_area="2490",
        business_area_from="admin",
        progress_reports_pd_link={**PROVEN, "proof_error": "x"},
    )
    found = one("11")
    assert figures(found)[CAN_SHOW] == "no: the check failed on the last sync: tell the NeuroDB team"
    assert (
        "Whether NeuroDB can show PRP's reports still owed could not be checked on the last sync: tell the NeuroDB "
        "team."
    ) in found["verdict"]
    assert "x" not in figures(found).values()
