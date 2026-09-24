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
        r.tracking == "off_track" and r.title == "# of schools" for r in everything
    )


def test_summary_and_grouping(data):
    filters = monitoring.Filters(year=2026)
    rows = monitoring.indicators(filters, today=TODAY)
    data_ = monitoring.summary(rows, filters, today=datetime.date(2026, 11, 1))
    assert data_["status_counts"] == {"on_track": 1, "off_track": 1, "over_target": 0, "no_target": 0}
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
    assert "Implementation monitoring" in text and reverse("reports:pd_monitoring") in text


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
