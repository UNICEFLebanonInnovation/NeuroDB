"""ActivityInfo database, Neuro Report and HPM pages and their internal API (integrity fixes)."""

import datetime
import re
from pathlib import Path

import pytest
from django.conf import settings
from django.core.cache import cache
from django.urls import reverse

from neurodb.indicators.models import (
    Database,
    MasterIndicator,
    MasterSubIndicator,
    NeuroReport,
    NeuroReportComment,
    NeuroReportMasterIndicator,
    ReportingYear,
)
from neurodb.partnerships.models import PCA

pytestmark = pytest.mark.django_db

STATIC_JS = Path(settings.BASE_DIR) / "neurodb" / "web" / "static" / "js"


@pytest.fixture(autouse=True)
def _fresh_cache():
    cache.clear()
    yield
    cache.clear()


def _kpi_classes(html: str) -> dict[str, str]:
    """{label: class attribute} of the KPI tiles of a page."""
    return {
        label: classes
        for classes, label in re.findall(r'<div class="(kpi[^"]*)">\s*<div class="kpi__label">([^<]*)<', html)
    }


def test_dashboard_reporting_counts_ratio_masters_and_status_filter_colours_only_its_tiles(
    client_viewer, hierarchy
):
    url = reverse("reports:database_dashboard", args=[hierarchy["database"].id])
    html = client_viewer.get(url + "?status=off_track").text
    tiles = _kpi_classes(html)
    assert tiles["Master indicators"] == "kpi" and tiles["Indicators with data"] == "kpi"
    assert tiles["Records"] == "kpi" and tiles["Off track"] == "kpi kpi--off_track"
    assert "2 of 2 master indicators" in html  # the ratio master has records too


def test_indicator_detail_zero_target_and_empty_effect(client_viewer, hierarchy):
    master = hierarchy["master"]
    MasterIndicator.objects.filter(pk=master.pk).update(awp_target=0)
    MasterSubIndicator.objects.filter(master=master).update(effect=None)
    html = client_viewer.get(
        reverse("reports:indicator_detail", args=[hierarchy["database"].id, master.id])
    ).text
    target = html.split('<div class="kpi__label">Target</div>', 1)[1].split("</div>", 2)[0]
    assert "—" in target and ">0<" not in target
    assert "Not counted" in html and ">TOTAL<" not in html


def test_hpm_view_is_only_for_hpm_reports(client_viewer, hierarchy):
    report = NeuroReport.objects.create(name="Results", is_hpm=False, ryear=hierarchy["report"].ryear)
    response = client_viewer.get(reverse("reports:report_hpm", args=[report.id]) + "?month=2")
    assert response.status_code == 302
    assert response.url == reverse("reports:report_dashboard", args=[report.id]) + "?month=2"


@pytest.mark.parametrize("query", ["?month=13", "?month=abc", "?quarter=Q5"])
@pytest.mark.parametrize("page", ["reports:report_dashboard", "reports:report_hpm"])
def test_report_pages_reject_bad_periods(client_viewer, hierarchy, page, query):
    assert client_viewer.get(reverse(page, args=[hierarchy["report"].id]) + query).status_code == 400


def test_report_period_picker_lists_only_ended_months(client_viewer, hierarchy, reporting_year):
    today = datetime.date.today()
    ReportingYear.objects.filter(pk=reporting_year.pk).update(name=str(today.year), year=str(today.year))
    html = client_viewer.get(
        reverse("reports:report_dashboard", args=[hierarchy["report"].id]) + "?month=12"
    ).text
    picker = html.split('id="period-month"', 1)[1].split("</select>", 1)[0]
    last = max(today.month - 1, 1)
    assert picker.count("<option") == last
    assert f'<option value="{last}" selected' in picker
    assert 'value="Q4"' not in html


def test_other_years_menu_and_search_by_year(client_viewer, hierarchy, reporting_year):
    older = ReportingYear.objects.create(name="2025", year="2025", current=False)
    NeuroReport.objects.filter(pk=hierarchy["report"].pk).update(report_code="HPM")
    old = NeuroReport.objects.create(name="HPM 2025", report_code="HPM", is_hpm=True, ryear=older)
    html = client_viewer.get(reverse("reports:report_hpm", args=[hierarchy["report"].id])).text
    assert "Other years" in html and f'href="{reverse("reports:report_hpm", args=[old.id])}"' in html
    found = client_viewer.get(reverse("reports:search") + "?q=HPM&year=2025").text
    assert reverse("reports:report_hpm", args=[old.id]) in found
    current = client_viewer.get(reverse("reports:search") + "?q=HPM").text
    assert reverse("reports:report_hpm", args=[old.id]) not in current


def test_hpm_api_comment_master_id_is_the_item_id(client_viewer, hierarchy):
    link = NeuroReportMasterIndicator.objects.get(report=hierarchy["report"])
    NeuroReportComment.objects.create(
        report=hierarchy["report"], master=link, comment="Late", related_month="01"
    )
    data = client_viewer.get(reverse("api:hpm", args=[hierarchy["report"].id]) + "?month=2").json()
    item_ids = {i["id"] for s in data["sections"] for i in s["items"]}
    assert data["comments"][0]["master_id"] == hierarchy["master"].id in item_ids
    assert data["comments"][0]["report_master_id"] == link.id


def test_report_analytical_keeps_only_the_report_masters(client_viewer, hierarchy):
    master = hierarchy["master"]
    twin = MasterIndicator.objects.create(
        database=hierarchy["database"], name=master.name, awp_code=master.awp_code, aggregation_method="SUM"
    )
    MasterSubIndicator.objects.create(master=twin, sub=hierarchy["sub"], effect="TOTAL", sequence=1)
    rows = client_viewer.get(reverse("api:report_analytical", args=[hierarchy["report"].id])).json()
    assert rows and {r["master_id"] for r in rows} == {master.id}


def test_donors_api_counts_records_per_programme(client_viewer, hierarchy):
    PCA.objects.create(etl_id="31", partner_name="Partner A", number="LEB/PCA2026001-1", status="active")
    data = client_viewer.get(reverse("api:donors")).json()
    assert [p["interventions"] for p in data["programmes"]] == [4]


def test_sidebar_badge_counts_databases_and_topbar_marks_the_page_year(client_viewer, hierarchy, section):
    Database.objects.create(
        ai_id=202619, db_id="cp2", name="CP emergency 2026", label="CP emergency", username="", password="",
        section=section, reporting_year=hierarchy["database"].reporting_year, display=True,
    )  # fmt: skip
    older = ReportingYear.objects.create(name="2025", year="2025", current=False)
    old_db = Database.objects.create(
        ai_id=202501, db_id="old", name="CP 2025", label="CP 2025", username="", password="",
        section=section, reporting_year=older, display=True,
    )  # fmt: skip
    html = client_viewer.get(reverse("reports:databases")).text
    block = html.split('data-nav-key="databases"', 1)[1].split("</summary>", 1)[0]
    assert '<span class="count">2</span>' in block  # two databases in one section
    html = client_viewer.get(reverse("reports:database_dashboard", args=[old_db.id])).text
    menu = html.split('id="year-menu"', 1)[1].split("</ul>", 1)[0]
    assert "<span>2025</span>" in menu and 'class="dropdown-item active" href="/databases/?year=2025"' in menu


def test_pivot_and_map_scripts():
    pivot = (STATIC_JS / "pivot.js").read_text()
    assert "pivot rows" in pivot and "} records`" not in pivot
    assert 'value_role: ["Not counted in master indicator"]' in pivot and "exclusions: {}" not in pivot
    assert '["get", "interventions"], ...rampStops(max)]' in (STATIC_JS / "map.js").read_text()
