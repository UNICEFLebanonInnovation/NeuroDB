"""The eTools Power BI views on NeuroDB's pages (SPEC6 R3): Program Performance and Data quality on Partner
monitoring, Reporting Progress, Partner Performance, the report table and Report/Location on Progress
reports, and the flat PD/SPD Indicators list on Programmes. The same filter gives the same figures on every
view, in their exports and in Ask NeuroDB; nothing is added across indicators; no e-mail address is shown;
donors never reach them."""

# ruff: noqa: F401, F811 (the partner monitoring fixture "data" comes from test_pd_monitoring)
from __future__ import annotations

import csv
import datetime
import io
from decimal import Decimal

import pytest
from django.urls import reverse
from openpyxl import load_workbook

from neurodb.assistant import tools
from neurodb.datamart import models as dm
from neurodb.datamart import monitoring, reporting, services
from neurodb.partnerships import services as partnerships
from neurodb.partnerships import statuses
from neurodb.partnerships.models import PCA, PartnerOrganization
from tests.reports.test_pd_monitoring import TODAY, _FrozenDatetime, data, report_rows

pytestmark = pytest.mark.django_db
HX = {"HX-Request": "true"}
CLASSROOMS = "# of classrooms rehabilitated"


@pytest.fixture
def frozen(monkeypatch):
    """Today is 1 July 2026 for partner monitoring and the Progress reports page."""
    monkeypatch.setattr(monitoring, "datetime", _FrozenDatetime)
    monkeypatch.setattr(services, "datetime", _FrozenDatetime)


@pytest.fixture
def more(data):
    """The fixture's PDs and reports, plus: an Education indicator without a target or a report in the
    active PD, its PD's attributes (office, CP output, focal points), the closed PD's attributes and an
    overdue report of the closed PD."""
    pd, closed = data["pd"], data["closed"]
    PCA.objects.filter(pk=pd.pk).update(
        offices_set=["Beirut"],
        cp_outputs=["3.1 Children at risk reached with protection services"],
        section_names=["Child Protection"],
        unicef_focal_points=["Focal Point One", "focal.two@example.org", "Focal Three (three@example.org)"],
    )
    PCA.objects.filter(pk=closed.pk).update(document_type="SSFA", offices_set=["Zahle"])
    PartnerOrganization.objects.filter(pk=closed.partner_id).update(cso_type="National")
    dm.PDIndicator.objects.create(
        datamart_id=20,
        source_id=500,
        intervention=pd,
        pd_reference_number=pd.number,
        title=CLASSROOMS,
        section_name="Education",
        lower_result_name="2.1 Learning spaces",
        target_numerator=None,
        location_name="Akkar",
    )
    dm.PDIndicator.objects.filter(source_id=100).update(cp_output="1.2 Psychosocial support for children")
    report_rows(
        closed,
        closed.partner,
        "# of schools",
        "PR-9",
        "QPR1",
        "QPR",
        datetime.date(2024, 3, 31),
        {"Beirut": 0},
        0,
        status="Due",
    )
    data["pd"].refresh_from_db()
    data["closed"].refresh_from_db()
    return data


def _rows(**params):
    return monitoring.indicators(monitoring.Filters.from_params(params, today=TODAY), today=TODAY)


# ---------------------------------------------------------------------------------- PD statuses
def test_one_definition_of_active_and_running():
    assert statuses.RUNNING == monitoring.ACTIVE_PD_STATUSES == partnerships.RUNNING_STATUSES
    assert statuses.ACTIVE == partnerships.ACTIVE_STATUSES == ("active",)
    assert set(statuses.ACTIVE) < set(statuses.RUNNING)


def test_pd_status_filter_defaults_to_the_running_pds(more, frozen):
    signed = PCA.objects.create(etl_id="13", number="LEB/PD3", title="Signed", status="signed")
    dm.PDIndicator.objects.create(datamart_id=30, source_id=700, intervention=signed, title="# of kits")
    titles = {r.title for r in _rows()}
    assert "# of kits" in titles and "# of schools" not in titles  # running: active, signed, suspended
    # "pd_status=" with nothing chosen is every status; a chosen status keeps only its PDs
    assert "# of schools" in {r.title for r in _rows(pd_status="")}
    assert {r.title for r in _rows(pd_status="closed")} == {"# of schools"}
    assert {r.title for r in _rows(pd_status="signed")} == {"# of kits"}
    # the filter shows the default as chosen statuses, and an old "scope=all" link as every status
    assert monitoring.Filters.from_params({}).status_choice == list(statuses.RUNNING)
    assert monitoring.Filters.from_params({"scope": "all"}).status_choice == []
    # the Programmes page's "Active" is the eTools status alone
    assert list(partnerships.programme_documents(scope="active")) == [more["pd"]]


# --------------------------------------------------------------------------- Program Performance
def test_program_performance_filters(more, frozen):
    girls, boys = more["girls"], more["boys"]
    assert {r.title for r in _rows(emergency="yes")} == {girls}
    assert {r.title for r in _rows(emergency="no")} == {boys, CLASSROOMS}
    assert {r.title for r in _rows(pd_status="", document_type="SSFA")} == {"# of schools"}
    assert {r.title for r in _rows(pd_status="", office="Zahle")} == {"# of schools"}
    assert {r.title for r in _rows(pd_status="", partner_type="National")} == {"# of schools"}
    assert {r.title for r in _rows(cp_output="psychosocial")} == {girls}
    # an indicator not reported under a CP output takes its PD's, when the PD has only one
    by_title = {r.title: r for r in _rows()}
    assert by_title[boys].cp_output == "3.1 Children at risk reached with protection services"
    assert by_title[girls].cp_output == "1.2 Psychosocial support for children"
    assert {r.title for r in _rows(cp_output="children at risk")} == {boys, CLASSROOMS}


def test_program_performance_table_sorts_and_counts_sections(client_viewer, more, frozen):
    page = client_viewer.get(reverse("reports:pd_monitoring"), {"view": "table"})
    assert page.status_code == 200 and page.context["view"] == "table"
    # Reporting year "All" by default, as Power BI opens Program Performance; the grid opens on the year
    assert page.context["filters"].year is None
    assert client_viewer.get(reverse("reports:pd_monitoring")).context["filters"].year == TODAY.year
    rows = list(page.context["page_obj"])
    # % Reached, highest first; an indicator without a value last
    assert [r.title for r in rows] == [more["girls"], more["boys"], CLASSROOMS]
    assert page.context["sort"] == "-achieved"
    for heading in (
        "Section",
        "CP output",
        "Emergency",
        "Indicator",
        "Target",
        "Reached",
        "% Reached",
        "Partner",
    ):
        assert f">{heading}</a>" in page.text
    ascending = client_viewer.get(reverse("reports:pd_monitoring"), {"view": "table", "sort": "achieved"})
    assert [r.title for r in ascending.context["page_obj"]] == [more["boys"], more["girls"], CLASSROOMS]
    by_title = client_viewer.get(reverse("reports:pd_monitoring"), {"view": "table", "sort": "indicator"})
    assert [r.title for r in by_title.context["page_obj"]] == sorted(
        [more["girls"], more["boys"], CLASSROOMS], key=str.lower
    )
    # section tiles count every section the other filters keep, the chosen one or not
    tiles = {t["name"]: t["count"] for t in page.context["section_tiles"]}
    assert tiles == {"Child Protection": 2, "Education": 1}
    education = client_viewer.get(
        reverse("reports:pd_monitoring"), {"view": "table", "section": "Education"}, headers=HX
    )
    assert [r.title for r in education.context["page_obj"]] == [CLASSROOMS]
    assert {t["name"]: t["count"] for t in education.context["section_tiles"]} == tiles
    assert "<html" not in education.text and 'aria-current="true"' in education.text


def test_program_performance_export_holds_every_row_with_the_grid_figures(
    client_viewer, more, frozen, monkeypatch
):
    monkeypatch.setattr(monitoring, "PAGE_SIZE", 1)
    page = client_viewer.get(reverse("reports:pd_monitoring"), {"view": "table"})
    assert len(page.context["page_obj"].object_list) == 1  # one row on the page...
    response = client_viewer.get(reverse("reports:pd_monitoring"), {"view": "table", "export": "csv"})
    assert response.status_code == 200 and response["Content-Type"].startswith("text/csv")
    lines = list(csv.DictReader(io.StringIO(b"".join(response.streaming_content).decode("utf-8-sig"))))
    assert len(lines) == 3  # ... every indicator of the filter in the file
    grid = {r.title: r for r in _rows(year="all")}
    for line in lines:
        row = grid[line["indicator"]]
        assert line["reached"] == ("" if row.reached is None else str(row.reached))
        assert line["programme_document"] == "LEB/PD1" and line["pd_status"] == "Active"
    assert "reached_in_year" not in lines[0]  # every year: no "Reached in" column
    workbook = client_viewer.get(
        reverse("reports:pd_monitoring"), {"view": "table", "export": "xlsx", "year": "2026"}
    )
    sheet = load_workbook(io.BytesIO(workbook.content))["Program Performance"]
    header = [c.value for c in next(sheet.iter_rows(max_row=1))]
    assert "reached_in_year" in header and sheet.max_row == 4
    values = {r[header.index("indicator")]: r for r in sheet.iter_rows(min_row=2, values_only=True)}
    assert values[more["girls"]][header.index("reached")] == 550
    assert values[more["girls"]][header.index("reached_in_year")] == 550


# ---------------------------------------------------------------------------------- Data quality
def test_data_quality_tiles_and_lists(client_viewer, more, frozen):
    page = client_viewer.get(reverse("reports:pd_monitoring"), {"view": "quality"})
    assert page.status_code == 200
    q = page.context["quality"]
    assert (q["programme_documents"], q["active_pds"], q["ended_pds"]) == (1, 1, 0)
    assert (q["indicators"], q["with_reached"], q["without_reached"]) == (3, 2, 1)
    assert (q["with_target"], q["without_target"]) == (2, 1)
    assert [r.title for r in q["missing_target"]] == [CLASSROOMS]
    assert [r.title for r in q["no_reached"]] == [CLASSROOMS]
    # the same filter gives the same figures as the grid's summary
    grid = client_viewer.get(reverse("reports:pd_monitoring"), {"year": "all"}).context["data"]
    assert (grid["indicators"], grid["with_reached"], grid["with_target"]) == (3, 2, 2)
    # every status: the closed PD is ended, its indicator without Reached
    everything = client_viewer.get(reverse("reports:pd_monitoring"), {"view": "quality", "pd_status": ""})
    assert (
        everything.context["quality"]["programme_documents"],
        everything.context["quality"]["ended_pds"],
    ) == (
        2,
        1,
    )
    for label in ("Active PDs", "Ended PDs", "With Reached", "Without Reached", "Without a target"):
        assert f">{label}<" in page.text


def test_data_quality_neurodb_checks(client_viewer, more, frozen, monkeypatch):
    # PRP's cumulative on the latest counted report says 700, the reports add up to 550
    dm.ReportedIndicator.objects.filter(progress_report="PR-2", indicator=more["girls"]).update(
        total_cumulative_progress="700"
    )
    dm.PDIndicator.objects.create(
        datamart_id=21,
        source_id=600,
        intervention=more["pd"],
        title=more["boys"],
        section_name="Child Protection",
    )
    q = monitoring.data_quality(_rows(), monitoring.Filters(), TODAY)
    assert [r.title for r in q["mismatch"]] == [more["girls"]] and q["mismatch"][0].prp_cumulative == 700
    (twice,) = q["same_title"]
    assert (twice["title"], twice["count"], twice["pd"]) == (more["boys"], 2, more["pd"])
    assert q["partial"] == []
    # reports held only from March 2026: the PD (started January) has a partial history
    monkeypatch.setattr(monitoring.rc, "reports_read_from", lambda today=None: datetime.date(2026, 3, 1))
    q = monitoring.data_quality(_rows(), monitoring.Filters(), TODAY)
    assert q["partial"] == [more["pd"]] and q["read_from"] == datetime.date(2026, 3, 1)
    page = client_viewer.get(reverse("reports:pd_monitoring"), {"view": "quality"})
    assert "Partial history" in page.text and "Same title twice" in page.text


def test_data_quality_in_a_year_reads_reached_in_that_year(more, frozen):
    q = monitoring.data_quality(_rows(year="2025"), monitoring.Filters(year=2025), TODAY)
    assert (q["with_reached"], q["without_reached"]) == (0, 3)  # nothing reported in 2025
    q = monitoring.data_quality(_rows(year="2026"), monitoring.Filters(year=2026), TODAY)
    assert (q["with_reached"], q["without_reached"]) == (2, 1)


# ---------------------------------------------------------------------------- Reporting Progress
def _reporting(**params):
    return services.partner_reporting(params, today=TODAY)


def test_tiles_count_indicator_reports_with_progress_reports_beside(more):
    data = _reporting()
    counts = {s["key"]: (s["indicator_reports"], s["progress_reports"]) for s in data["counts"]["statuses"]}
    # PR-1 (girls and boys), PR-2 and HR-4 accepted; PR-3 still running; PR-9 (closed PD) overdue
    assert counts == {
        "accepted": (4, 3),
        "submitted": (0, 0),
        "sent_back": (0, 0),
        "due": (0, 0),
        "overdue": (1, 1),
        "not_yet_due": (1, 1),
    }
    assert (data["counts"]["indicator_reports"], data["counts"]["progress_reports"]) == (6, 5)
    assert data["summary"]["reports"] == 5  # progress reports per status, as before
    # one status per report: the section table and the months add up to the tiles
    assert data["by_section"]["total"] == 6
    assert data["by_section"]["totals"] == [4, 0, 0, 0, 1, 1]
    assert sum(sum(v) for v in data["by_month"]["series"].values()) == 6
    assert "Not yet due" in data["by_month"]["series"]
    # each indicator report is counted once, however many locations it has
    pr1 = next(r for r in data["reports"] if r["progress_report"] == "PR-1")
    assert pr1["indicators"] == 2


def test_section_table_and_section_filter(more):
    dm.ReportedIndicator.objects.filter(progress_report="PR-2").update(section="Education")
    data = _reporting()
    rows = {row["section"]: row for row in data["by_section"]["rows"]}
    # a report without a section of its own counts under its PD's sections (the closed PD has none);
    # nothing is counted twice
    assert {s: row["total"] for s, row in rows.items()} == {"Education": 1, "Child Protection": 4, "—": 1}
    assert sum(row["total"] for row in data["by_section"]["rows"]) == data["by_section"]["total"] == 6
    pr1 = next(r for r in data["reports"] if r["progress_report"] == "PR-1")
    assert pr1["section"] == "Child Protection"  # from the PD, for the table
    assert {r["progress_report"] for r in _reporting(section="Education")["reports"]} == {"PR-2"}
    # a report without a section of its own is in its PD's sections, by the rule of the table
    assert {r["progress_report"] for r in _reporting(section="Child Protection")["reports"]} == {
        "PR-1",
        "HR-4",
        "PR-3",
    }
    # a section in a list of sections ("C4D, Education") matches, a section containing the word does not
    dm.ReportedIndicator.objects.filter(progress_report="PR-2").update(section="C4D, Education")
    assert {r["progress_report"] for r in _reporting(section="Education")["reports"]} == {"PR-2"}
    assert _reporting(section="Educ")["reports"] == []


def test_filters_on_status_number_partner_and_dates(more):
    assert [r["progress_report"] for r in _reporting(status="not_yet_due")["reports"]] == ["PR-3"]
    # PRP's word for a status is understood too
    assert {r["progress_report"] for r in _reporting(status="Accepted")["reports"]} == {
        "PR-1",
        "PR-2",
        "HR-4",
    }
    assert {r["progress_report"] for r in _reporting(report_number="QPR1")["reports"]} == {"PR-1", "PR-9"}
    amel = str(more["partner"].id)
    assert "PR-9" not in {r["progress_report"] for r in _reporting(partner=amel)["reports"]}
    assert [r["progress_report"] for r in _reporting(partner_type="National")["reports"]] == ["PR-9"]
    assert [r["progress_report"] for r in _reporting(pd_status="closed")["reports"]] == ["PR-9"]
    later = {r["progress_report"] for r in _reporting(end_from="2026-04-01")["reports"]}
    assert later == {"PR-2", "HR-4", "PR-3"}
    assert {r["progress_report"] for r in _reporting(start_to="2026-01-01")["reports"]} == {"PR-1", "PR-9"}
    assert [r["progress_report"] for r in _reporting(report_type="HR")["reports"]] == ["HR-4"]
    # the options: six statuses, report numbers in order
    options = _reporting()["options"]
    assert [s["value"] for s in options["statuses"]] == list(reporting.rc.STATUS_LABELS)
    assert options["report_numbers"] == ["HR4", "QPR1", "QPR2", "QPR3"]


def test_report_table_columns_and_focal_point_names_only(client_viewer, more, frozen):
    page = client_viewer.get(reverse("reports:partner_reporting"))
    assert page.status_code == 200
    pr1 = next(r for r in page.context["data"]["reports"] if r["progress_report"] == "PR-1")
    assert (pr1["pd_status_label"], pr1["document_type"], pr1["focal_points"]) == (
        "Active",
        "PD",
        ["Focal Point One"],
    )
    for heading in (
        "Programme document",
        "Section",
        "Document type",
        "Period start",
        "Period end",
        "UNICEF focal points",
    ):
        assert f">{heading}<" in page.text
    assert (
        "Focal Point One" in page.text
        and "focal.two@example.org" not in page.text
        and "three@example.org" not in page.text
        and "Focal Three" not in page.text
    )
    for label in ("Indicator reports", "Accepted", "Submitted", "Sent back", "Due", "Overdue", "Not yet due"):
        assert f">{label}<" in page.text
    assert "3 progress reports" in page.text  # the hint under Accepted


def test_report_exports_hold_every_report_and_the_tile_counts(client_viewer, more, frozen, monkeypatch):
    monkeypatch.setattr("neurodb.reports.views.PAGE_SIZE", 2)
    response = client_viewer.get(reverse("reports:partner_reporting"), {"export": "csv"})
    text = b"".join(response.streaming_content).decode("utf-8-sig")
    lines = list(csv.DictReader(io.StringIO(text)))
    assert len(lines) == 5 and "@" not in text
    pr1 = next(line for line in lines if line["report"] == "QPR1" and line["programme_document"] == "LEB/PD1")
    assert pr1["indicator_reports"] == "2" and pr1["unicef_focal_points"] == "Focal Point One"
    workbook = load_workbook(
        io.BytesIO(client_viewer.get(reverse("reports:partner_reporting"), {"export": "xlsx"}).content)
    )
    assert workbook.sheetnames == ["Progress reports", "By status", "By section"]
    by_status = {r[0]: (r[1], r[2]) for r in workbook["By status"].iter_rows(min_row=2, values_only=True)}
    assert by_status["Accepted"] == (4, 3) and by_status["Overdue"] == (1, 1)
    # Ask NeuroDB gives the tiles' figures
    out = tools.run("partner_reporting", {})
    assert out["indicator_reports"]["Accepted"] == {"indicator_reports": 4, "progress_reports": 3}


# --------------------------------------------------------------------------- Partner Performance
def test_partners_tab(client_viewer, more, frozen):
    perf = _reporting()["partners"]
    assert (perf["partners"], perf["reported"], perf["not_reported"]) == (2, 1, 1)
    assert {s["key"]: s["partners"] for s in perf["by_status"]} == {
        "accepted": 1,
        "submitted": 0,
        "sent_back": 0,
        "due": 0,
        "overdue": 1,
        "not_yet_due": 1,
    }
    assert [p["name"] for p in perf["not_reported_list"]] == ["Himaya"]
    # Overdue, Sent back, Due and Submitted reports, the most pressing first (Not yet due is not listed)
    assert [r["progress_report"] for r in perf["follow_up"]] == ["PR-9"]
    dm.ReportedIndicator.objects.filter(progress_report="PR-2").update(report_status="Sent back")
    assert [r["progress_report"] for r in _reporting()["partners"]["follow_up"]] == ["PR-9", "PR-2"]
    page = client_viewer.get(reverse("reports:partner_reporting"), {"tab": "partners"})
    assert page.status_code == 200 and page.context["tab"] == "partners"
    for label in ("Partners", "With at least one report", "Not reported"):
        assert f">{label}<" in page.text
    workbook = load_workbook(
        io.BytesIO(
            client_viewer.get(
                reverse("reports:partner_reporting"), {"tab": "partners", "export": "xlsx"}
            ).content
        )
    )
    assert workbook.sheetnames[0] == "Follow up" and "Partners not reported" in workbook.sheetnames
    assert workbook["Follow up"].max_row == 3


# ------------------------------------------------------------------------------- Report/Location
def test_report_by_location_reads_each_location_once(client_viewer, more, frozen):
    girls = more["girls"]
    # a second row for Akkar in QPR1 (another disaggregation group): never added, the largest is used
    copy = dm.ReportedIndicator.objects.get(progress_report="PR-1", indicator=girls, location="Akkar")
    copy.pk, copy.datamart_id = None, 999_001
    copy.disaggregation = {"()": {"v": 50, "d": 1, "c": 50}}
    copy.save()
    # HR4: the report total repeated on both locations, no value of their own
    dm.ReportedIndicator.objects.filter(progress_report="HR-4").update(
        disaggregation={},
        achievement_in_period="100",
        total_cumulative_progress_in_location="",
        previous_location_progress="",
    )
    data = _reporting()
    found = reporting.location_rows(
        data["rows"], {r["progress_report"]: r["status"] for r in data["reports"]}, TODAY
    )
    rows = {(r["progress_report"], r["indicator"], r["location"]): r for r in found["rows"]}
    akkar = rows[("PR-1", girls, "Akkar")]
    assert (akkar["value_in_period"], akkar["rows"]) == (200.0, 2)
    assert rows[("PR-1", girls, "Bekaa")]["value_in_period"] == 100.0
    hr = rows[("HR-4", girls, "Akkar")]
    assert hr["repeated"] and hr["value_in_period"] is None
    assert rows[("PR-9", "# of schools", "Beirut")]["report_status"] == "Overdue"
    assert not rows[("PR-9", "# of schools", "Beirut")]["counted"]
    # one row per indicator report and location: PR-1 girls has two, not three
    assert sum(1 for k in rows if k[:2] == ("PR-1", girls)) == 2
    page = client_viewer.get(reverse("reports:partner_reporting"), {"tab": "locations", "status": "accepted"})
    assert page.status_code == 200
    assert {r["report_status"] for r in page.context["page_obj"]} == {"Accepted"}
    assert "report total" in page.text and "is-uncounted" not in page.text
    response = client_viewer.get(reverse("reports:partner_reporting"), {"tab": "locations", "export": "csv"})
    lines = list(csv.DictReader(io.StringIO(b"".join(response.streaming_content).decode("utf-8-sig"))))
    assert len(lines) == len(found["rows"])
    noted = next(
        line
        for line in lines
        if line["report"] == "QPR1" and line["location"] == "Akkar" and line["indicator"] == girls
    )
    assert noted["value_in_period"] == "200.0" and "never their sum" in noted["note"]


def test_report_by_location_asks_to_narrow_a_large_filter(client_viewer, more, frozen, monkeypatch):
    monkeypatch.setattr(reporting, "LOCATION_LIMIT", 2)
    page = client_viewer.get(reverse("reports:partner_reporting"), {"tab": "locations"})
    assert page.status_code == 200 and "choose a reporting year" in page.text
    response = client_viewer.get(reverse("reports:partner_reporting"), {"tab": "locations", "export": "xlsx"})
    assert response.status_code == 400


# --------------------------------------------------------------------------- PD/SPD Indicators
def test_programme_indicators_tab(client_viewer, more):
    pds = partnerships.programme_documents(scope="all")
    rows = reporting.pd_indicators(pds, {})
    # one row per indicator, though eTools lists the girls indicator once per location
    assert sorted(r["indicator"] for r in rows) == sorted(
        [more["girls"], more["boys"], CLASSROOMS, "# of schools"]
    )
    girls = next(r for r in rows if r["indicator"] == more["girls"])
    assert (
        girls["programme_document"],
        girls["pd_status"],
        girls["office"],
        girls["target"],
        girls["emergency"],
    ) == (
        "LEB/PD1",
        "Active",
        "Beirut",
        1000.0,
        "Yes",
    )
    assert [r["indicator"] for r in reporting.pd_indicators(pds, {"emergency": "yes"})] == [more["girls"]]
    assert [r["indicator"] for r in reporting.pd_indicators(pds, {"section": "Education"})] == [
        CLASSROOMS,
        "# of schools",
    ]
    assert [r["indicator"] for r in reporting.pd_indicators(pds, {"pd_q": "PD2"})] == ["# of schools"]
    assert [r["indicator"] for r in reporting.pd_indicators(pds, {"indicator": "syrian"})] == [more["boys"]]
    page = client_viewer.get(reverse("reports:programmes"), {"tab": "indicators"})
    assert page.status_code == 200 and page.context["tab"] == "indicators" and more["girls"] in page.text
    partial = client_viewer.get(
        reverse("reports:programmes"), {"tab": "indicators", "emergency": "no"}, headers=HX
    )
    assert more["girls"] not in partial.text and more["boys"] in partial.text and "<html" not in partial.text
    response = client_viewer.get(reverse("reports:programmes"), {"tab": "indicators", "export": "csv"})
    lines = list(csv.DictReader(io.StringIO(b"".join(response.streaming_content).decode("utf-8-sig"))))
    assert len(lines) == 4 and set(reporting.INDICATOR_COLUMNS) == set(lines[0])
    # the PD list keeps working, with the PD reference search
    documents = client_viewer.get(reverse("reports:programmes"), {"pd_q": "PD2"})
    assert [r["number"] for r in documents.context["rows"]] == ["LEB/PD2"]


# --------------------------------------------------------------------------------- who sees them
NEW_VIEWS = [
    ("reports:pd_monitoring", {"view": "table"}),
    ("reports:pd_monitoring", {"view": "quality"}),
    ("reports:pd_monitoring", {"view": "table", "export": "xlsx"}),
    ("reports:partner_reporting", {"tab": "partners"}),
    ("reports:partner_reporting", {"tab": "locations"}),
    ("reports:partner_reporting", {"export": "csv"}),
    ("reports:programmes", {"tab": "indicators"}),
    ("reports:programmes", {"tab": "indicators", "export": "xlsx"}),
]


@pytest.mark.parametrize(("name", "params"), NEW_VIEWS)
def test_every_new_view_for_a_viewer(client_viewer, more, frozen, name, params):
    assert client_viewer.get(reverse(name), params).status_code == 200


@pytest.mark.parametrize(("name", "params"), NEW_VIEWS)
def test_donors_never_reach_the_new_views(client, more, name, params):
    from tests.donors.test_donor_access import make_donor

    client.force_login(make_donor().user)
    response = client.get(reverse(name), params)
    assert response.status_code == 302 and response["Location"] == reverse("donors:page")
    assert client.get(reverse(name), params, headers=HX).status_code == 403


@pytest.mark.parametrize(("name", "params"), NEW_VIEWS[:2] + NEW_VIEWS[3:5] + NEW_VIEWS[6:7])
def test_the_new_views_render_without_data(client_viewer, name, params):
    assert client_viewer.get(reverse(name), params).status_code == 200


def test_same_filter_same_numbers_across_the_new_views(client_viewer, more, frozen):
    """Grid, Program Performance, its export, Data quality and Ask NeuroDB agree for one filter."""
    params = {"year": "2026", "section": "Child Protection"}
    grid = client_viewer.get(reverse("reports:pd_monitoring"), params).context
    table = client_viewer.get(reverse("reports:pd_monitoring"), {**params, "view": "table"}).context
    quality = client_viewer.get(reverse("reports:pd_monitoring"), {**params, "view": "quality"}).context
    grid_reached = {r.title: r.reached for r in grid["rows"]}
    assert (
        {r.title: r.reached for r in table["rows"]}
        == grid_reached
        == {more["girls"]: 550.0, more["boys"]: 20.0}
    )
    assert {r.title: r.reached_in_year for r in table["rows"]} == {
        r.title: r.reached_in_year for r in grid["rows"]
    }
    assert quality["quality"]["indicators"] == grid["data"]["indicators"] == 2
    assert quality["quality"]["with_reached"] == grid["data"]["with_reached"] == 2
    response = client_viewer.get(
        reverse("reports:pd_monitoring"), {**params, "view": "table", "export": "csv"}
    )
    lines = list(csv.DictReader(io.StringIO(b"".join(response.streaming_content).decode("utf-8-sig"))))
    assert {line["indicator"]: float(line["reached"]) for line in lines} == grid_reached
    ask = tools.run("pd_indicator_progress", {"partner": "amel", "year": 2026})
    assert {i["indicator"]: i["reached"] for i in ask["indicators_detail"]} == {
        **grid_reached,
        CLASSROOMS: None,
    }
