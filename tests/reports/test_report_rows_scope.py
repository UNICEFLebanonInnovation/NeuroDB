"""Which partner report rows the Progress reports page counts, and what it says about the others: the
running programme documents by default; a programme document NeuroDB does not hold only when every PD
status is chosen; a row that names no programme document never. The eTools reporting checks 10 and 11
show the rows by country and programme document, and the reports owed in PRP's two feeds."""

from __future__ import annotations

import datetime

import pytest
from django.urls import reverse

from neurodb.datamart import checks, reporting, services
from neurodb.datamart import models as dm
from neurodb.partnerships.models import PCA, PartnerOrganization

pytestmark = pytest.mark.django_db
D = datetime.date
TODAY = D(2026, 10, 15)  # the suite's day (tests/conftest.py SITE_NOW)
JUNE = D(2026, 6, 30)
_ids = iter(range(70_000, 10**9))
# check 11's table after its period end and feed: the six statuses, then the reports expected and not in the feed
COLUMNS = ["Accepted", "Submitted", "Sent back", "Due", "Overdue", "Not yet due", "Expected"]


def row(pd, number, pr, indicator, *, status="Accepted", end=JUNE, location="Akkar", **extra):
    """One location of one indicator in one progress report."""
    extra.setdefault("section", "Education")
    extra.setdefault("indicator_report", f"{pr}-{indicator}")
    return dm.ReportedIndicator.objects.create(
        datamart_id=next(_ids),
        intervention=pd,
        partner=pd.partner if pd else None,
        pd_reference_number=number,
        progress_report=pr,
        report_number="QPR1",
        report_type="QPR",
        report_status=status,
        period_start=end - datetime.timedelta(days=90),
        period_end=end,
        due_date=end + datetime.timedelta(days=30),
        submission_date=end + datetime.timedelta(days=10) if status in ("Accepted", "Submitted") else None,
        indicator=indicator,
        location=location,
        **extra,
    )


@pytest.fixture
def rows(db):
    partner = PartnerOrganization.objects.create(etl_id="71", name="Delta Org", vendor_number="2500000071")
    running = PCA.objects.create(etl_id="971", partner=partner, partner_name=partner.name, number="LEB/RUN",
                                 title="R", status="active")  # fmt: skip
    ended = PCA.objects.create(etl_id="972", partner=partner, partner_name=partner.name, number="LEB/END",
                               title="E", status="ended")  # fmt: skip
    row(running, "LEB/RUN", "R-1", "Girls enrolled")
    row(running, "LEB/RUN", "R-1", "Boys enrolled")
    row(ended, "LEB/END", "E-1", "Teachers trained", location="Akkar")
    row(ended, "LEB/END", "E-1", "Teachers trained", location="Bekaa")  # the same indicator report
    row(None, "LEB/GONE", "G-1", "Kits", status="Due")  # a PD NeuroDB does not hold
    # no PD named at all (PRP reports outside programme documents)...
    row(None, "", "", "Cluster children", status="Ove", section="Protection")
    row(None, "", "", "Cluster adults", status="Ove", section="Protection")
    # ...unless the eTools PD id is in the record: then a PD NeuroDB does not hold
    row(None, "", "X-1", "Supplies", data={"etools_intervention_id": 4242})
    return {"running": running, "ended": ended}


def test_the_default_page_counts_the_running_pds_and_says_what_it_leaves_out(rows, client_viewer):
    page = client_viewer.get(reverse("reports:partner_reporting"))
    data = page.context["data"]
    assert data["counts"]["indicator_reports"] == 2
    assert data["left_out"] == {"other_status": 1, "not_held": 2, "no_pd": 2, "by_pd_status": 3}
    note = page.text.split('id="reporting-left-out"', 1)[1].split("</p>", 1)[0]
    assert "leaves out 3 more indicator reports" in note
    assert (
        "1 of programme documents with another status, 2 of programme documents NeuroDB does not hold" in note
    )
    assert "2 indicator reports of these filters name no programme document" in note
    assert "pd_status=" in note and "Show every PD status" in note


def test_every_pd_status_reads_the_pds_not_held_but_never_a_report_of_no_pd(rows, client_viewer):
    page = client_viewer.get(reverse("reports:partner_reporting"), {"pd_status": ""})
    data = page.context["data"]
    numbers = {r["pd_reference_number"] for r in data["reports"]}
    assert numbers == {"LEB/RUN", "LEB/END", "LEB/GONE", ""}  # "" is the report naming an eTools PD id
    assert data["counts"]["indicator_reports"] == 5
    assert "Cluster children" not in {r.indicator for r in data["rows"]}
    assert data["by_section"]["total"] == 5 and "Protection" not in {
        r["section"] for r in data["by_section"]["rows"]
    }
    assert data["left_out"] == {"other_status": 0, "not_held": 0, "no_pd": 2, "by_pd_status": 0}
    note = page.text.split('id="reporting-left-out"', 1)[1].split("</p>", 1)[0]
    assert (
        "leaves out" not in note and "2 indicator reports of these filters name no programme document" in note
    )


def test_the_note_follows_the_other_filters(rows):
    found = reporting.left_out(reporting.ReportFilters(year=2025), TODAY)
    assert found == {"other_status": 0, "not_held": 0, "no_pd": 0, "by_pd_status": 0}
    # with the ended PDs chosen, the running PD's two indicator reports are of "another status"
    ended_only = reporting.ReportFilters.from_params({"pd_status": "ended"})
    found = reporting.left_out(ended_only, TODAY)
    assert found == {"other_status": 2, "not_held": 2, "no_pd": 2, "by_pd_status": 4}


def test_the_stored_tables_skip_the_note(rows):
    assert services.partner_reporting({}, TODAY, options=False)["left_out"] == {}


def test_check_10_shows_each_country_and_programme_document_link(rows):
    dm.ReportedIndicator.objects.filter(indicator="Cluster adults").update(data={"country_name": "Syria"})
    dm.ReportedIndicator.objects.filter(pd_reference_number="LEB/RUN").update(
        data={"country_name": " lebanon "}
    )
    found = {c["key"]: c for c in checks.run_all()}["10"]
    assert found["tone"] == "warning"
    figures = dict(found["figures"])
    assert figures["Rows naming another country"] == 1
    assert figures["Indicator reports of programme documents not held"] == 2
    assert figures["Indicator reports naming no programme document"] == 2
    table = {(r[0], r[1]): r for r in found["table"]["rows"]}
    assert table[("lebanon", checks.LINK_RUNNING)][2:4] == (2, 2)
    assert table[("Syria", checks.LINK_NONE)][6] == "Protection"
    assert table[(checks.NO_COUNTRY, checks.LINK_NOT_HELD)][7] == "LEB/GONE"
    assert table[(checks.NO_COUNTRY, checks.LINK_OTHER_STATUS)][2:4] == (2, 1)
    assert "1 row names another country than Lebanon" in found["verdict"]


def test_check_11_compares_the_reports_owed_in_the_two_feeds(rows):
    running = rows["running"]

    def document(key, status, end, **data):
        dm.DatamartDocument.objects.create(
            dataset="prp_indicator_reports",
            record_key=key,
            intervention=running,
            date=end,
            data={"report_status": status, "time_period_end": end.isoformat(), **data},
        )

    document("1", "Acc", D(2026, 6, 30), submission_date="2026-07-10")
    document("2", "Ove", D(2026, 8, 31), due_date="2026-09-30T00:00:00Z")
    document("3", "Due", D(2026, 9, 30), due_date="2026-10-30")
    document("4", "Due", D(2026, 11, 30), due_date="2026-12-30")
    document("5", "Ove", D(2025, 8, 31), due_date="2025-09-30")  # another year
    found = {c["key"]: c for c in checks.run_all()}["11"]
    assert found["tone"] == "warning"
    figures = dict(found["figures"])
    assert figures["Due or overdue in the partner reports"] == 0
    assert figures["Due or overdue in PRP's indicator reports"] == 2
    assert "does not carry every report still owed" in found["verdict"]
    labels = COLUMNS
    table = {(r[0], r[1]): dict(zip(labels, r[2:], strict=True)) for r in found["table"]["rows"]}
    assert table[("Jun 2026", "Partner reports")]["Accepted"] == 2
    assert table[("Jun 2026", "PRP indicator reports")]["Accepted"] == 1
    assert table[("Aug 2026", "PRP indicator reports")]["Overdue"] == 1
    assert table[("Sep 2026", "PRP indicator reports")]["Due"] == 1
    assert table[("Nov 2026", "PRP indicator reports")]["Not yet due"] == 1
    assert table[("Aug 2026", "Partner reports")] == dict.fromkeys(labels, 0)


def test_check_11_without_prp_indicator_reports(rows):
    found = {c["key"]: c for c in checks.run_all()}["11"]
    assert found["tone"] == "info" and "hold nothing of the running programme documents" in found["verdict"]


def test_the_partner_page_and_its_all_reports_link_leave_out_the_reports_of_no_pd(rows, client_viewer):
    partner = rows["running"].partner
    dm.ReportedIndicator.objects.filter(intervention=None).update(partner=partner)
    page = client_viewer.get(reverse("reports:partner_profile", args=[partner.id]))
    counted = page.context["datamart"]["reporting"]["reports"]
    listed = client_viewer.get(
        reverse("reports:partner_reporting"), {"partner": str(partner.id), "pd_status": ""}
    )
    assert listed.context["data"]["summary"]["reports"] == counted == 4  # R-1, E-1, G-1, X-1


def test_reports_without_prp_ids_count_once_per_progress_report_everywhere(rows):
    """Rows without PRP's indicator report id are one indicator report per progress report: the note,
    check 10 and the page count them alike."""
    for pr, end in (("N-1", D(2026, 3, 31)), ("N-2", D(2026, 6, 30))):
        row(
            None,
            "LEB/GONE2",
            pr,
            "Kits",
            status="Due",
            end=end,
            indicator_report="",
            etools_indicator_id="55",
        )
    note = reporting.left_out(reporting.ReportFilters(), TODAY)
    every = services.partner_reporting({"pd_status": ""}, TODAY)["counts"]["indicator_reports"]
    default = services.partner_reporting({}, TODAY)["counts"]["indicator_reports"]
    assert note["by_pd_status"] == every - default == 5
    found = dict({c["key"]: c for c in checks.run_all()}["10"]["figures"])
    assert found["Indicator reports of programme documents not held"] == note["not_held"] == 4
    assert found["Indicator reports naming no programme document"] == note["no_pd"] == 2


def test_check_10_compares_countries_as_the_sync_does(rows, settings):
    dm.ReportedIndicator.objects.filter(pd_reference_number="LEB/RUN").update(
        data={"country_name": "Lebanon\t"}
    )
    dm.ReportedIndicator.objects.filter(pd_reference_number="LEB/END").update(
        data={"country_name": "  LEBANON"}
    )
    found = {c["key"]: c for c in checks.run_all()}["10"]
    assert dict(found["figures"])["Rows naming another country"] == 0
    dm.ReportedIndicator.objects.filter(pd_reference_number="LEB/END").update(data={"country_name": "Syria"})
    settings.ETOOLS_DATAMART_COUNTRY = ""  # no country set: nothing is another country's, as in the sync
    found = {c["key"]: c for c in checks.run_all()}["10"]
    assert dict(found["figures"])["Rows naming another country"] == 0


def _document(pd, key, end, **data):
    dm.DatamartDocument.objects.create(
        dataset="prp_indicator_reports",
        record_key=key,
        intervention=pd,
        date=end,
        data={"time_period_end": end.isoformat(), **data},
    )


def test_check_11_reads_the_progress_report_status_of_prp_records_first(rows):
    running = rows["running"]
    # Due as an indicator report, but its progress report was submitted: Submitted, as the Funds page reads it
    _document(running, "1", D(2026, 6, 30), report_status="Due", due_date="2026-07-30", pr_status="Sub",
              pr_submission_date="2026-07-20")  # fmt: skip
    # no word of its own: its progress report's dates decide
    _document(running, "2", D(2026, 6, 30), pr_end_date="2026-08-31", pr_due_date="2026-09-30")
    found = {c["key"]: c for c in checks.run_all()}["11"]
    labels = COLUMNS
    table = {(r[0], r[1]): dict(zip(labels, r[2:], strict=True)) for r in found["table"]["rows"]}
    assert table[("Jun 2026", "PRP indicator reports")]["Submitted"] == 1
    assert table[("Aug 2026", "PRP indicator reports")]["Overdue"] == 1
    assert "Aug 2026" in found["verdict"] and found["tone"] == "warning"


def test_check_11_compares_month_by_month(rows):
    running = rows["running"]
    # the yearly totals of reports owed agree (1 and 1), the months do not
    row(running, "LEB/RUN", "R-2", "Girls enrolled", status="Due", end=D(2026, 3, 31))
    _document(running, "1", D(2026, 6, 30), report_status="Acc", submission_date="2026-07-10")
    _document(running, "2", D(2026, 6, 30), report_status="Acc", submission_date="2026-07-10")
    _document(running, "3", D(2026, 8, 31), report_status="Ove", due_date="2026-09-30")
    found = {c["key"]: c for c in checks.run_all()}["11"]
    figures = dict(found["figures"])
    assert (
        figures["Due or overdue in the partner reports"]
        == figures["Due or overdue in PRP's indicator reports"]
        == 1
    )
    assert found["tone"] == "warning"
    assert "PRP lists more reports still owed in Aug 2026" in found["verdict"]
    assert "The partner reports list more reports still owed in Mar 2026" in found["verdict"]


def test_check_11_says_why_there_is_nothing_to_compare(rows):
    _document(None, "1", D(2026, 8, 31), report_status="Ove", due_date="2026-09-30")
    found = {c["key"]: c for c in checks.run_all()}["11"]
    assert found["tone"] == "info" and "none links to a programme document NeuroDB holds" in found["verdict"]
    assert dict(found["figures"])["… not linked to one"] == 1
    _document(rows["ended"], "2", D(2026, 8, 31), report_status="Ove", due_date="2026-09-30")
    found = {c["key"]: c for c in checks.run_all()}["11"]
    assert "are of PDs that are not running" in found["verdict"]


def test_checks_10_and_11_run_first_and_are_listed_in_order(db, monkeypatch):
    ran = []

    def run(key):
        def check(cursor):
            ran.append(key)
            return {"tone": checks.OK, "verdict": "Done.", "figures": []}

        return check

    stub = tuple(checks.Check(k, k, "?", "-", run(k)) for k in ("1", "2", "10", "11"))
    monkeypatch.setattr(checks, "CHECKS", stub)
    found = checks.run_all()
    assert ran == ["10", "11", "1", "2"]
    assert [c["key"] for c in found] == ["1", "2", "10", "11"]


def test_check_11_counts_prp_records_in_the_year_their_progress_report_ends(rows):
    running = rows["running"]
    # its own period ends in December 2025, its progress report in January 2026: owed in January 2026
    _document(running, "1", D(2025, 12, 31), pr_end_date="2026-01-15", pr_due_date="2026-02-15")
    # its own period ends in December 2026, its progress report in January 2027: not this year's
    _document(running, "2", D(2026, 12, 31), pr_end_date="2027-01-15", pr_due_date="2027-02-15")
    found = {c["key"]: c for c in checks.run_all()}["11"]
    labels = COLUMNS
    table = {(r[0], r[1]): dict(zip(labels, r[2:], strict=True)) for r in found["table"]["rows"]}
    assert table[("Jan 2026", "PRP indicator reports")]["Overdue"] == 1
    assert ("Dec 2026", "PRP indicator reports") not in table
    assert dict(found["figures"])["PRP indicator reports of the year linked to a programme document"] == 1


def test_a_check_left_out_names_the_checks_that_used_the_time(db, monkeypatch):
    now = [0.0]
    monkeypatch.setattr(checks.time, "monotonic", lambda: now[0])

    def slow(cursor):
        now[0] = 100.0  # the page's whole time
        return {"tone": checks.OK, "verdict": "Done.", "figures": []}

    def quick(cursor):
        return {"tone": checks.OK, "verdict": "Done.", "figures": []}

    stub = (
        checks.Check("1", "1", "?", "-", quick),
        checks.Check("10", "10", "?", "-", quick),
        checks.Check("11", "11", "?", "-", slow),
    )
    monkeypatch.setattr(checks, "CHECKS", stub)
    first, *_ = checks.run_all(budget=75)
    assert first["verdict"].startswith(
        "Not run: checks 10 and 11, run before it, used up the page's 75 seconds"
    )
    assert "checks 10 and 11 run first" in first["verdict"]


def test_data_health_tells_an_answer_of_another_country_from_an_empty_one():
    from django.template.loader import render_to_string

    def row(**extra):
        return {"name": "partner_reports", "run": None, "rows": 5, "not_linked": {}, "not_linked_total": 0,
                "removed": None, "errors": [], "relinked": None, "empty_response": False,
                "other_country_skipped": None, "other_country_removed": None, **extra}  # fmt: skip

    html = render_to_string(
        "reports/parts/health_datasets.html",
        {"datasets": [row(empty_response=True, other_country_skipped=3), row(empty_response=True)]},
    )
    assert "the Datamart returned only records of another country (3): check the country name" in html
    assert html.count("the Datamart returned nothing: check the country name") == 1


def test_administrators_find_the_reporting_checks_from_the_menu_and_the_pages(
    rows, client, admin_user, viewer
):
    checks_url = reverse("admin:datamart_reporting_checks")
    client.force_login(admin_user)
    admin_home = client.get(reverse("admin:index")).text
    assert admin_home.count(f'href="{checks_url}"') >= 2  # the admin menu and the quick actions
    assert f'href="{checks_url}"' in client.get(reverse("reports:data_health")).text
    page = client.get(reverse("reports:partner_reporting")).text
    assert 'id="reporting-checks-link"' in page and f'href="{checks_url}"' in page
    client.force_login(viewer)
    assert checks_url not in client.get(reverse("reports:partner_reporting")).text
    assert checks_url not in client.get(reverse("reports:data_health")).text


def test_the_admin_menu_lists_the_reporting_checks_for_administrators_only(rf, admin_user, viewer):
    from neurodb.web import admin_site

    request = rf.get("/admin/")
    request.user = admin_user
    assert [p["link"] for p in admin_site._datamart_pages(request)] == [
        reverse("admin:datamart_reporting_checks")
    ]
    request.user = viewer
    assert admin_site._datamart_pages(request) == []
