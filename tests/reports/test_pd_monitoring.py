"""Partner monitoring: PD indicators, reported values per month and location, on/off track."""

import datetime
from decimal import Decimal

import pytest
from django.urls import reverse

from neurodb.assistant import tools
from neurodb.datamart import models as dm
from neurodb.datamart import monitoring
from neurodb.indicators.services.tracking import percentage_elapsed, tracking_between
from neurodb.partnerships.models import PCA, PartnerOrganization

pytestmark = pytest.mark.django_db

TODAY = datetime.date(2026, 7, 1)  # half way through a 2026 programme document


def report_rows(
    pd, partner, title, progress_report, number, kind, end, values, cumulative, status="Accepted", **extra
):
    rows = []
    for location, value in values.items():
        rows.append(
            dm.ReportedIndicator(
                datamart_id=abs(hash((progress_report, title, location))) % 10**9,
                partner=partner,
                intervention=pd,
                partner_name=partner.name,
                pd_reference_number=pd.number,
                progress_report=progress_report,
                report_number=number,
                report_type=kind,
                report_status=status,
                period_start=end - datetime.timedelta(days=89 if kind == "QPR" else 29),
                period_end=end,
                due_date=end + datetime.timedelta(days=15),
                submission_date=end + datetime.timedelta(days=10) if status != "Due" else None,
                indicator=title,
                target="1000",
                location=location,
                achievement_in_period=str(value),
                total_cumulative_progress=str(cumulative),
                total_cumulative_progress_in_location=str(value * 2),
                pd_output_progress_status="On Track",
                **extra,
            )
        )
    return dm.ReportedIndicator.objects.bulk_create(rows)


@pytest.fixture
def data(db):
    partner = PartnerOrganization.objects.create(
        etl_id="1", name="Amel Association", partner_type="CSO", vendor_number="V1"
    )
    other = PartnerOrganization.objects.create(
        etl_id="2", name="Himaya", partner_type="CSO", vendor_number="V2"
    )
    pd = PCA.objects.create(
        etl_id="11",
        partner=partner,
        partner_name=partner.name,
        number="LEB/PD1",
        title="Child protection",
        status="active",
        start=datetime.date(2026, 1, 1),
        end=datetime.date(2026, 12, 31),
    )
    closed = PCA.objects.create(
        etl_id="12",
        partner=other,
        partner_name=other.name,
        number="LEB/PD2",
        title="Old",
        status="closed",
        start=datetime.date(2024, 1, 1),
        end=datetime.date(2024, 12, 31),
    )
    girls = "# of girls (12-17) with disabilities reached with PSS"
    boys = "# of Syrian boys reached with PSS"
    for n, (title, location) in enumerate([(girls, "Akkar"), (girls, "Bekaa"), (boys, "Akkar")], start=1):
        dm.PDIndicator.objects.create(
            datamart_id=n,
            source_id=100 if title == girls else 200,
            intervention=pd,
            pd_reference_number=pd.number,
            title=title,
            section_name="Child Protection",
            lower_result_name="1.1 Protection services",
            target_numerator=Decimal("1000"),
            baseline_numerator=Decimal("0"),
            display_type="number",
            location_name=location,
            is_high_frequency=title == girls,
            tag_gender="Girls" if title == girls else "Boys",
            tag_age_group="Adolescents" if title == girls else "Children",
            tag_nationality="" if title == girls else "Syrian",
            tag_disability="Yes" if title == girls else "",
        )
    dm.PDIndicator.objects.create(
        datamart_id=9,
        source_id=300,
        intervention=closed,
        pd_reference_number=closed.number,
        title="# of schools",
        section_name="Education",
        target_numerator=Decimal("10"),
        location_name="Beirut",
    )
    # girls: two quarterly reports (Q1 300 in total, Q2 250) and monthly HR reports
    report_rows(
        pd,
        partner,
        girls,
        "PR-1",
        "QPR1",
        "QPR",
        datetime.date(2026, 3, 31),
        {"Akkar": 200, "Bekaa": 100},
        300,
    )
    report_rows(
        pd,
        partner,
        girls,
        "PR-2",
        "QPR2",
        "QPR",
        datetime.date(2026, 6, 30),
        {"Akkar": 150, "Bekaa": 100},
        550,
    )
    report_rows(
        pd, partner, girls, "HR-4", "HR4", "HR", datetime.date(2026, 4, 30), {"Akkar": 90, "Bekaa": 10}, 400
    )
    # boys: one quarterly report, far behind its target
    report_rows(pd, partner, boys, "PR-1", "QPR1", "QPR", datetime.date(2026, 3, 31), {"Akkar": 20}, 20)
    # a due report not submitted yet
    report_rows(
        pd, partner, boys, "PR-3", "QPR3", "QPR", datetime.date(2026, 9, 30), {"Akkar": 0}, 20, status="Due"
    )
    return {"pd": pd, "partner": partner, "closed": closed, "girls": girls, "boys": boys}


def test_tracking_between_uses_the_pd_period():
    start, end = datetime.date(2026, 1, 1), datetime.date(2026, 12, 31)
    assert round(percentage_elapsed(start, end, TODAY)) == 50
    assert tracking_between(550, 1000, start, end, TODAY).status == "on_track"
    assert tracking_between(20, 1000, start, end, TODAY).status == "off_track"
    assert tracking_between(900, 1000, start, end, TODAY).status == "over_target"
    assert tracking_between(5, None, start, end, TODAY).status == "no_target"
    assert percentage_elapsed(start, end, datetime.date(2027, 3, 1)) == 100.0
    assert percentage_elapsed(None, None, datetime.date(2026, 7, 2)) == pytest.approx(50, abs=1)


def test_indicators_grid_values_cumulative_and_status(data):
    rows = monitoring.indicators(monitoring.Filters(year=2026), today=TODAY)
    by_title = {r.title: r for r in rows}
    assert set(by_title) == {data["girls"], data["boys"]}  # the closed PD is out of the default scope
    girls = by_title[data["girls"]]
    assert girls.months == {
        3: 300.0,
        6: 250.0,
    }  # quarterly totals over the locations, in the period-end month
    assert (girls.cumulative, girls.cumulative_as_of) == (550.0, datetime.date(2026, 6, 30))
    assert girls.tracking == "on_track" and round(girls.achieved) == 55
    assert girls.locations == ["Akkar", "Bekaa"] and girls.tags["disability"] == "Yes"
    assert girls.partner_status == "On Track" and girls.reports == 2
    boys = by_title[data["boys"]]
    assert boys.tracking == "off_track" and boys.months == {3: 20.0, 9: 0.0}


def test_report_type_location_tag_and_status_filters(data):
    hr = monitoring.indicators(monitoring.Filters(year=2026, report_type="HR"), today=TODAY)
    assert [r.title for r in hr] == [data["girls"]] and hr[0].months == {4: 100.0}
    bekaa = monitoring.indicators(monitoring.Filters(year=2026, locations=["Bekaa"]), today=TODAY)
    assert [r.title for r in bekaa] == [data["girls"]] and bekaa[0].months == {3: 100.0, 6: 100.0}
    syrian = monitoring.indicators(
        monitoring.Filters(year=2026, tags={"nationality": ["Syrian"]}), today=TODAY
    )
    assert [r.title for r in syrian] == [data["boys"]]
    off = monitoring.indicators(monitoring.Filters(year=2026, status="off_track"), today=TODAY)
    assert [r.title for r in off] == [data["boys"]]
    everything = monitoring.indicators(monitoring.Filters(year=2026, scope="all"), today=TODAY)
    assert len(everything) == 3 and any(
        r.tracking == "not_reported" and r.title == "# of schools" for r in everything
    )
    silent = monitoring.indicators(
        monitoring.Filters(year=2026, scope="all", status="not_reported"), today=TODAY
    )
    assert [r.title for r in silent] == ["# of schools"] and silent[0].achieved is None


def test_reports_match_their_indicator_by_etools_id_before_title(data):
    # PRP spells the title differently but carries the eTools indicator id of the girls indicator
    dm.ReportedIndicator.objects.filter(indicator=data["girls"]).update(
        indicator="# of girls reached (PRP wording)", etools_indicator_id="100"
    )
    rows = {r.title: r for r in monitoring.indicators(monitoring.Filters(year=2026), today=TODAY)}
    assert rows[data["girls"]].reports == 2 and rows[data["girls"]].cumulative == 550.0
    detail = monitoring.indicator_detail(data["pd"], "100", "QPR", 2026, today=TODAY)
    assert len(detail["periods"]) == 2


def test_summary_and_grouping(data):
    filters = monitoring.Filters(year=2026)
    rows = monitoring.indicators(filters, today=TODAY)
    data_ = monitoring.summary(rows, filters, today=datetime.date(2026, 11, 1))
    assert data_["status_counts"] == {
        "on_track": 1,
        "off_track": 1,
        "over_target": 0,
        "no_target": 0,
        "not_reported": 0,
    }
    assert data_["overdue_reports"] == 1 and data_["programme_documents"] == 1 and data_["partners"] == 1
    groups = monitoring.grouped(rows)
    assert (
        groups[0]["section"] == "Child Protection"
        and groups[0]["pds"][0]["outputs"][0]["output"] == "1.1 Protection services"
    )
    assert len(groups[0]["pds"][0]["outputs"][0]["indicators"]) == 2


@pytest.fixture
def frozen_today(monkeypatch):
    monkeypatch.setattr(monitoring, "datetime", _FrozenDatetime)


def test_page_partial_filters_and_empty(client_viewer, data, frozen_today):
    page = client_viewer.get(reverse("reports:pd_monitoring"), {"year": "2026"})
    assert page.status_code == 200 and data["girls"] in page.text and "On track" in page.text
    partial = client_viewer.get(
        reverse("reports:pd_monitoring"),
        {"year": "2026", "status": "off_track"},
        headers={"HX-Request": "true"},
    )
    assert data["boys"] in partial.text and data["girls"] not in partial.text and "<html" not in partial.text
    assert client_viewer.get(reverse("reports:pd_monitoring"), {"year": "2019"}).status_code == 200
    dm.PDIndicator.objects.all().delete()
    assert "No indicators match" in client_viewer.get(reverse("reports:pd_monitoring")).text


def test_the_users_section_is_the_default_and_the_grid_is_paged(client_viewer, viewer, data, frozen_today):
    from neurodb.accounts.models import Section

    viewer.section = Section.objects.create(name="Child protection", code="CP")
    viewer.save()
    page = client_viewer.get(reverse("reports:pd_monitoring"))
    assert "Your section, Child Protection, by default" in page.text and data["girls"] in page.text
    assert page.context["filters"].sections == ["Child Protection"]
    everything = client_viewer.get(reverse("reports:pd_monitoring"), {"section": ""})
    assert "by default" not in everything.text and everything.context["filters"].sections == []
    viewer.section = Section.objects.create(name="Youth and Adolescents", code="YA")
    viewer.save()
    assert client_viewer.get(reverse("reports:pd_monitoring")).context["filters"].sections == []
    assert monitoring.default_sections(viewer, ["Child Protection", "Youth"]) == ["Youth"]
    monkey = monitoring.PAGE_SIZE
    monitoring.PAGE_SIZE = 1
    try:
        first = client_viewer.get(reverse("reports:pd_monitoring"), {"year": "2026", "page": "2"})
    finally:
        monitoring.PAGE_SIZE = monkey
    assert first.context["page_obj"].paginator.num_pages == 2 and len(first.context["groups"]) == 1
    assert "Showing 2–2 of 2" in first.text


def test_indicator_detail_modal_and_page(client_viewer, data, frozen_today):
    url = reverse("reports:pd_indicator", args=[data["pd"].id, "100"])
    modal = client_viewer.get(url, {"year": "2026"}, headers={"HX-Request": "true"})
    assert modal.status_code == 200 and "modal-header" in modal.text
    for expected in (
        "Akkar",
        "Bekaa",
        "QPR1",
        "QPR2",
        "All locations",
        "Planned locations",
    ):
        assert expected in modal.text
    page = client_viewer.get(url, {"year": "2026", "report_type": "HR"})
    assert page.status_code == 200 and "HR4" in page.text and "<html" in page.text
    assert client_viewer.get(reverse("reports:pd_indicator", args=[data["pd"].id, "999"])).status_code == 404


def test_programme_and_partner_pages_show_monitoring(client_viewer, data, frozen_today):
    text = client_viewer.get(reverse("reports:programme_detail", args=[data["pd"].id])).text
    assert "Monitoring by month and location" in text and "1 on track, 1 off track" in text
    text = client_viewer.get(reverse("reports:partner_profile", args=[data["partner"].id])).text
    assert "implementation monitoring" in text and reverse("reports:pd_monitoring") in text


def test_assistant_tool(data, frozen_today):
    out = tools.run("pd_indicator_progress", {"partner": "amel", "year": 2026})
    assert out["indicators"] == 2 and out["status_counts"]["off_track"] == 1
    girls = next(i for i in out["indicators_detail"] if "girls" in i["indicator"])
    assert girls["by_month"] == {"03": 300.0, "06": 250.0} and girls["status"] == "on_track"
    assert girls["tags"] == {"gender": "Girls", "age_group": "Adolescents", "disability": "Yes"}
    with pytest.raises(tools.ToolInputError):
        tools.run("pd_indicator_progress", {"partner": "nobody"})
    off = tools.run("pd_indicator_progress", {"status": "off_track", "section": "child", "year": 2026})
    assert [i["indicator"] for i in off["indicators_detail"]] == [data["boys"]]


class _FrozenDatetime:
    """``datetime`` as seen by the monitoring module, with today fixed inside the PD period."""

    class date(datetime.date):
        @classmethod
        def today(cls):
            return TODAY

    timedelta = datetime.timedelta


@pytest.fixture
def places(db):
    from neurodb.geo.models import Location, LocationType

    gov = LocationType.objects.create(name="Governorate", admin_level=1)
    dist = LocationType.objects.create(name="District", admin_level=2)
    village = LocationType.objects.create(name="Cadastral", admin_level=3)
    akkar = Location.objects.create(
        id=2,
        name="Akkar",
        p_code="LB1",
        type=gov,
        latitude=34.55,
        longitude=36.1,
        lft=1,
        rght=2,
        level=0,
        tree_id=2,
    )
    district = Location.objects.create(
        id=3, name="Akkar District", p_code="LB11", type=dist, parent=akkar, lft=1, rght=2, level=0, tree_id=3
    )
    bekaa = Location.objects.create(
        id=5,
        name="Bekaa",
        p_code="LB5",
        type=gov,
        latitude=33.85,
        longitude=35.9,
        lft=1,
        rght=2,
        level=0,
        tree_id=5,
    )
    halba = Location.objects.create(
        id=4, name="Halba", p_code="LB1101", type=village, parent=district, lft=1, rght=2, level=0, tree_id=4
    )  # no coordinates: placed at Akkar
    return {"akkar": akkar, "district": district, "halba": halba, "bekaa": bekaa}


def test_map_points_place_locations_by_etools_coordinates(data, places, frozen_today):
    pd = data["pd"]
    dm.PDIndicator.objects.filter(location_name="Akkar").update(
        location_pcode="LB1101", location=places["halba"]
    )
    dm.PDIndicator.objects.filter(location_name="Bekaa").update(
        location_pcode="LB5", location=places["bekaa"]
    )
    dm.ReportedIndicator.objects.filter(location="Akkar").update(
        p_code="LB1101", location_ref=places["halba"]
    )
    dm.ReportedIndicator.objects.filter(location="Bekaa").update(p_code="LB5", location_ref=places["bekaa"])
    dm.MonitoringFinding.objects.create(
        datamart_id=1, partner=data["partner"], location=places["halba"], overall_finding_rating="Off Track"
    )
    dm.ActionPoint.objects.create(
        datamart_id=1, partner=data["partner"], intervention=pd, location=places["halba"], status="open"
    )
    out = monitoring.map_points(monitoring.Filters(year=2026), today=TODAY)
    by_key = {p["key"]: p for p in out["points"]}
    halba = by_key["LB1101"]
    assert (halba["latitude"], halba["approximate"], halba["located_by"]) == (34.55, True, "Akkar")
    assert (halba["governorate"], halba["district"], halba["indicators"], halba["worst"]) == (
        "Akkar",
        "Akkar District",
        2,
        "off_track",
    )
    girls = next(r for r in halba["rows"] if "girls" in r["indicator"])
    assert (girls["achieved_here"], girls["cumulative_here"], girls["tracking"]) == (350.0, 300.0, "on_track")
    assert girls["period"] == datetime.date(2026, 6, 30) and girls["planned"] and girls["reported"]
    assert halba["monitoring"] == {
        "findings": 1,
        "findings_off_track": 1,
        "action_points": 1,
        "action_points_open": 1,
    }
    bekaa = by_key["LB5"]
    assert bekaa["approximate"] is False and bekaa["indicators"] == 1 and bekaa["pds"] == 1
    # a row whose link was not set yet is still placed through its P-code
    dm.PDIndicator.objects.filter(location_name="Bekaa").update(location=None)
    dm.ReportedIndicator.objects.filter(location="Bekaa").update(location_ref=None)
    again = {p["key"]: p for p in monitoring.map_points(monitoring.Filters(year=2026), today=TODAY)["points"]}
    assert again["LB5"]["latitude"] == 33.85
    funding = out["pds"][pd.id]
    assert funding["number"] == "LEB/PD1" and funding["partner"] == "Amel Association"
    assert out["totals"]["locations"] == 2 and out["totals"]["approximate"] == 1 and out["unlocated"] == []


def test_map_page_api_and_links(client_viewer, data, places, frozen_today):
    dm.PDIndicator.objects.filter(location_name="Akkar").update(
        location_pcode="LB1101", location=places["halba"]
    )
    dm.ReportedIndicator.objects.filter(location="Akkar").update(
        p_code="LB1101", location_ref=places["halba"]
    )
    page = client_viewer.get(reverse("reports:pd_monitoring_map"), {"year": "2026"})
    assert page.status_code == 200 and 'data-module="pdmap"' in page.text
    assert reverse("api:pd_map") + "?year=2026" in page.text
    api = client_viewer.get(reverse("api:pd_map"), {"year": "2026"})
    assert api.status_code == 200
    keys = {p["key"] for p in api.json()["points"]} | {p["key"] for p in api.json()["unlocated"]}
    assert "LB1101" in keys and "name:bekaa" in {p["key"] for p in api.json()["unlocated"]}
    grid = client_viewer.get(reverse("reports:pd_monitoring"), {"year": "2026"})
    assert reverse("reports:pd_monitoring_map") + "?year=2026" in grid.text
    modal = client_viewer.get(
        reverse("reports:pd_indicator", args=[data["pd"].id, "100"]),
        {"year": "2026"},
        headers={"HX-Request": "true"},
    )
    assert "focus=LB1101" in modal.text and "On the map" in modal.text
    programme = client_viewer.get(reverse("reports:programme_detail", args=[data["pd"].id])).text
    assert "Implementation map" in programme
    partner = client_viewer.get(reverse("reports:partner_profile", args=[data["partner"].id])).text
    assert reverse("reports:pd_monitoring_map") + "?partner=" in partner


# ------------------------------------------------------------------ regressions: page integrity


def _girls(filters):
    rows = monitoring.indicators(filters, today=TODAY)
    return next(r for r in rows if "girls" in r.title and r.pd.number != "LEB/PD3")


def test_report_cumulative_does_not_depend_on_row_order_or_filters(data):
    """Location rows of one report carrying different totals: the report total is the largest,
    whichever row is read last, and the same indicator reads the same under every filter."""
    girls = data["girls"]
    for akkar, bekaa in (("450", "550"), ("550", "450")):
        dm.ReportedIndicator.objects.filter(indicator=girls, progress_report="PR-2", location="Akkar").update(
            total_cumulative_progress=akkar
        )
        dm.ReportedIndicator.objects.filter(indicator=girls, progress_report="PR-2", location="Bekaa").update(
            total_cumulative_progress=bekaa
        )
        assert _girls(monitoring.Filters(year=2026)).cumulative == 550.0
    # another PD in the query changes nothing for this one
    other = PCA.objects.create(
        etl_id="13",
        partner=data["partner"],
        partner_name=data["partner"].name,
        number="LEB/PD3",
        title="Other",
        status="active",
        start=datetime.date(2026, 1, 1),
        end=datetime.date(2026, 12, 31),
    )
    dm.PDIndicator.objects.create(
        datamart_id=50, source_id=500, intervention=other, title=girls, section_name="Education"
    )
    report_rows(
        other, data["partner"], girls, "PR-9", "QPR1", "QPR", datetime.date(2026, 3, 31), {"Tyre": 5}, 5
    )
    views = [
        monitoring.Filters(year=2026),
        monitoring.Filters(year=2026, scope="all"),
        monitoring.Filters(year=2026, scope="year"),
        monitoring.Filters(year=2026, pds=["LEB/PD1"]),
        monitoring.Filters(year=2026, partners=[str(data["partner"].id)]),
        monitoring.Filters(year=2026, sections=["Child Protection"]),
        monitoring.Filters(year=2026, tags={"gender": ["Girls"]}),
    ]
    seen = {
        (r.cumulative, r.tracking, round(r.achieved), r.cumulative_as_of, r.partner_status)
        for r in (_girls(f) for f in views)
    }
    assert seen == {(550.0, "on_track", 55, datetime.date(2026, 6, 30), "On Track")}
    detail = monitoring.indicator_detail(data["pd"], "100", "QPR", 2026, today=TODAY)
    assert detail["indicator"].cumulative == 550.0
    assert {r["progress_report"]: r["total_cumulative_progress"] for r in detail["reports"]}["PR-2"] == 550.0


def test_two_reports_ending_the_same_day_are_read_by_report_id(data):
    report_rows(
        data["pd"],
        data["partner"],
        data["girls"],
        "PR-2b",
        "QPR2b",
        "QPR",
        datetime.date(2026, 6, 30),
        {"Akkar": 1},
        700,
    )
    assert _girls(monitoring.Filters(year=2026)).cumulative == 700.0  # "PR-2b" sorts after "PR-2"


def test_a_row_of_a_filtered_out_indicator_never_falls_back_to_another_title(data):
    # a report row carrying the boys' eTools id but the girls' title belongs to the boys indicator
    report_rows(
        data["pd"],
        data["partner"],
        data["girls"],
        "PR-7",
        "QPR7",
        "QPR",
        datetime.date(2026, 6, 30),
        {"Tyre": 1},
        9999,
        etools_indicator_id="200",
    )
    assert _girls(monitoring.Filters(year=2026)).cumulative == 550.0
    assert _girls(monitoring.Filters(year=2026, tags={"gender": ["Girls"]})).cumulative == 550.0


def test_indicator_detail_keys_years_and_report_type_links(client_viewer, data, frozen_today):
    pd = data["pd"]
    for key in ("abc", "r", "rx", "1e3", "r-1"):
        assert client_viewer.get(reverse("reports:pd_indicator", args=[pd.id, key])).status_code == 404
    url = reverse("reports:pd_indicator", args=[pd.id, "100"])
    modal = client_viewer.get(url, {"year": "2026"}, headers={"HX-Request": "true"})
    # the report-type switch reloads this detail, not the page the modal was opened from
    assert (
        f'href="{url}?report_type=HR&amp;year=2026" hx-get="{url}?report_type=HR&amp;year=2026"' in modal.text
    )
    for year in ("99999", "10000", "0", "²"):
        for name, args in (
            ("reports:pd_monitoring", []),
            ("reports:pd_monitoring_map", []),
            ("reports:pd_indicator", [pd.id, "100"]),
            ("reports:partner_reporting", []),
            ("reports:funds", []),
            ("reports:monitoring", []),
            ("reports:assurance", []),
        ):
            assert (
                client_viewer.get(reverse(name, args=args), {"year": year, "scope": "year"}).status_code
                == 200
            )
        assert client_viewer.get(reverse("api:pd_map"), {"year": year, "scope": "year"}).status_code == 200
    assert monitoring.year_param("2026") == 2026 and monitoring.year_param("99999") is None


def test_indicator_detail_honours_the_year_and_combines_rows(data):
    pd = data["pd"]
    before = monitoring.indicator_detail(pd, "100", "QPR", 2025, today=TODAY)
    assert before["periods"] == [] and before["reports"] == [] and before["by_location"] == []
    assert before["indicator"].tracking == "not_reported"
    # a second Akkar row in QPR1 (another disaggregation) adds up in the cell as in the total
    report_rows(
        pd,
        data["partner"],
        data["girls"],
        "PR-1",
        "QPR1",
        "QPR",
        datetime.date(2026, 3, 31),
        {"akkar": 7},
        300,
    )
    detail = monitoring.indicator_detail(pd, "100", "QPR", 2026, today=TODAY)
    akkar = next(loc for loc in detail["by_location"] if loc["location"] == "Akkar")
    assert akkar["cells"]["PR-1"] == 207.0 and detail["periods"][0]["total"] == 307.0
    assert sum(loc["cells"].get("PR-1", 0) for loc in detail["by_location"]) == detail["periods"][0]["total"]
    # a "max across locations" indicator: the All locations total is the largest, as on the grid
    dm.ReportedIndicator.objects.filter(indicator=data["girls"]).update(calculation_across_locations="max")
    detail = monitoring.indicator_detail(pd, "100", "QPR", 2026, today=TODAY)
    assert [p["total"] for p in detail["periods"]] == [200.0, 150.0]
    assert _girls(monitoring.Filters(year=2026)).months == {3: 200.0, 6: 150.0}


def test_r_keys_match_only_their_own_rows(data):
    pd = data["pd"]
    dm.PDIndicator.objects.create(datamart_id=70, intervention=pd, title="# of caregivers", section_name="CP")
    # a row of another indicator, without an eTools id
    report_rows(
        pd, data["partner"], data["boys"], "PR-5", "QPR5", "QPR", datetime.date(2026, 5, 31), {"Akkar": 3}, 3
    )
    detail = monitoring.indicator_detail(pd, "r70", "QPR", 2026, today=TODAY)
    assert detail["periods"] == [] and detail["reports"] == []


def test_pd_without_number_or_partner(client_viewer, data, frozen_today):
    pd = data["pd"]
    PCA.objects.filter(pk=pd.pk).update(number=None, partner=None, partner_name=None)
    PCA.objects.filter(pk=data["closed"].pk).update(status="active")
    grid = client_viewer.get(reverse("reports:pd_monitoring"), {"year": "2026", "scope": "all"})
    assert grid.status_code == 200 and ">None<" not in grid.text and " None " not in grid.text
    detail = monitoring.indicator_detail(PCA.objects.get(pk=pd.pk), "100", "QPR", 2026, today=TODAY)
    assert detail["indicator"] is not None and detail["indicator"].cumulative == 550.0
    # the map counts partners as the grid does: a PD with no linked partner adds none
    out = monitoring.map_points(monitoring.Filters(year=2026, scope="all"), today=TODAY)
    summary = monitoring.summary(
        monitoring.indicators(monitoring.Filters(year=2026, scope="all"), TODAY),
        monitoring.Filters(year=2026),
    )
    assert out["totals"]["partners"] == summary["partners"] == 1
    assert (out["year"], out["report_type"]) == (2026, "QPR")


def test_percentage_indicator_without_target(client_viewer, data, frozen_today):
    dm.PDIndicator.objects.filter(source_id=200).update(display_type="percentage", target_numerator=None)
    text = client_viewer.get(reverse("reports:pd_monitoring"), {"year": "2026"}).text
    assert "—%" not in text


def test_partner_reporting_lists_a_report_once_when_its_rows_differ(client_viewer, data):
    from neurodb.datamart import services

    before = len(services.progress_reports(dm.ReportedIndicator.objects.all()))
    # a partial re-sync: the partner link is missing on one row of PR-1
    row = dm.ReportedIndicator.objects.filter(progress_report="PR-1", location="Bekaa").first()
    dm.ReportedIndicator.objects.filter(pk=row.pk).update(partner=None, submission_date=None)
    reports = services.progress_reports(dm.ReportedIndicator.objects.all())
    assert len(reports) == before
    pr1 = next(r for r in reports if r["progress_report"] == "PR-1")
    assert pr1["partner_id"] == data["partner"].id and pr1["submission_date"] and pr1["indicators"] == 2
    page = client_viewer.get(reverse("reports:partner_reporting"))
    assert page.status_code == 200 and page.context["data"]["summary"]["reports"] == before


def test_map_counts_findings_and_filter_options_without_duplicates(data, places):
    pd = data["pd"]
    dm.PDIndicator.objects.filter(location_name="Akkar").update(
        location_pcode="LB1", location=places["akkar"]
    )
    dm.PDIndicator.objects.create(
        datamart_id=80, intervention=pd, title="# of girls in school", tag_gender="Girls", section_name="CP"
    )
    PCA.objects.create(
        etl_id="14", partner=data["partner"], number="LEB/PD4", title="Second", status="active"
    )
    for n in (1, 2):  # two findings, same place, rating and date; the partner has two PDs
        dm.MonitoringFinding.objects.create(
            datamart_id=n,
            partner=data["partner"],
            location=places["akkar"],
            overall_finding_rating="On Track",
            end_date=datetime.date(2026, 5, 1),
        )
    out = monitoring.map_points(monitoring.Filters(year=2026), today=TODAY)
    akkar = next(p for p in out["points"] if p["key"] == "LB1")
    assert akkar["monitoring"]["findings"] == 2
    options = monitoring.filter_options(monitoring.Filters(year=2026))
    assert options["gender"] == ["Boys", "Girls"]
