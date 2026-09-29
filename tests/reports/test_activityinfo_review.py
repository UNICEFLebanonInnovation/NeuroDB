"""User review of the ActivityInfo pages: one indicator per chart and per map value, readable methods
and names, the indicator detail, the HPM table, search, the section-editor note."""

import datetime
import re
from pathlib import Path

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.urls import reverse
from django.utils import timezone

from neurodb.accounts.roles import SECTION_EDITOR
from neurodb.core.models import SyncRun
from neurodb.facts.models import ActivityReportNew
from neurodb.facts.services import dashboard as facts
from neurodb.indicators.models import (
    Database,
    IndicatorNew,
    NeuroReport,
    NeuroReportComment,
    NeuroReportMasterIndicator,
)

pytestmark = pytest.mark.django_db

STATIC_JS = Path(settings.BASE_DIR) / "neurodb" / "web" / "static" / "js"


def _chart_data(html: str, element_id: str = "chart-data") -> str:
    return re.search(rf'<script id="{element_id}" type="application/json">(.*?)</script>', html, re.S).group(
        1
    )


def test_monthly_chart_shows_one_indicator_at_a_time(hierarchy):
    db = hierarchy["database"]
    dash = facts.database_dashboard(db)
    monthly = facts.monthly_by_indicator(facts.fact_filter(db), dash.indicators)
    assert monthly["months"] == ["Jan", "Feb"]
    series = {s["id"]: s for s in monthly["indicators"]}
    assert monthly["default"] == hierarchy["master"].id  # the first SUM indicator
    assert series[hierarchy["master"].id]["values"] == [150.0, 350.0]
    assert series[hierarchy["master"].id]["reports"] == [2, 2]
    ratio = series[hierarchy["ratio"].id]
    assert [round(v, 1) for v in ratio["values"]] == [33.3, 42.9] and ratio["unit"] == "%"


def test_dashboard_header_methods_legend_and_selector(client_viewer, hierarchy):
    db = hierarchy["database"]
    html = client_viewer.get(reverse("reports:database_dashboard", args=[db.id])).text
    # The section has the database's name: said once, with what the page is instead.
    crumbs = html.split('class="breadcrumb"', 1)[1].split("</ol>", 1)[0]
    assert crumbs.count("Child Protection") == 1 and "ActivityInfo database · 2026" in html
    table = html.split('id="indicators"', 1)[1]
    assert ">Ratio<" in table and ">Sum<" in table and "SUM_OVER_SUM" not in table
    assert 'placeholder="Indicator name or AWP code"' in html
    legend = html.split('class="legend status-legend"', 1)[1].split("</div>", 1)[0]
    assert "Ahead of schedule" in legend and "No target" in legend
    assert 'id="monthly-indicator"' in html and 'data-select="monthly-indicator"' in html
    assert f'<option value="{hierarchy["master"].id}" selected>' in html
    assert "Print-ready one-page summary" in html  # what each view is for
    assert "section editor" not in html


def test_section_editor_sees_where_they_can_comment(client, hierarchy, section, roles):
    editor = get_user_model().objects.create_user(username="editor", password="editor-pass-123456")
    editor.section = section
    editor.save()
    editor.groups.add(Group.objects.get(name=SECTION_EDITOR))
    client.force_login(editor)
    html = client.get(reverse("reports:database_dashboard", args=[hierarchy["database"].id])).text
    note = html.split('class="editor-note"', 1)[1].split("</p>", 1)[0]
    assert "section editor" in note and reverse("reports:report_hpm", args=[hierarchy["report"].id]) in note


def test_indicator_detail_has_progress_status_and_months(client_viewer, hierarchy):
    db, master = hierarchy["database"], hierarchy["master"]
    html = client_viewer.get(reverse("reports:indicator_detail", args=[db.id, master.id])).text
    assert "Achieved" in html and "500" in html and "50%" in html
    assert 'class="status-ref"' in html and "Method: Sum." in html
    months = html.split('id="indicator-months"', 1)[1].split("</table>", 1)[0]
    assert "January" in months and "February" in months and "350" in months
    assert "Counts as" in html and "In the total" in html
    modal = client_viewer.get(
        reverse("reports:indicator_detail", args=[db.id, hierarchy["ratio"].id]), HTTP_HX_REQUEST="true"
    ).text
    assert "Numerator" in modal and "Denominator" in modal and "Ratio in the month" in modal


def test_map_value_is_one_indicator_and_counts_are_records(client_viewer, hierarchy):
    db, master = hierarchy["database"], hierarchy["master"]
    url = reverse("reports:database_map", args=[db.id])
    html = client_viewer.get(url).text
    assert "Choose an indicator" in html and ">Reported<" not in html and "interventions" not in html.lower()
    html = client_viewer.get(url + f"?indicator={master.id}").text
    assert ">Reported<" in html and f'<option value="{master.id}" selected>' in html
    assert f'<option value="{hierarchy["ratio"].id}"' not in html  # a ratio does not add up per area
    # A record of another indicator is counted without the filter and left out with it.
    other = IndicatorNew.objects.create(database=db, ai_indicator="i_x", name="Other", awp_code="9")
    ActivityReportNew.objects.create(
        dbase=db, database_ai_id=str(db.ai_id), indicator_id=other.ai_indicator, indicator_value=9999,
        month_name="2026-01-01", location_adminlevel_governorate="Beirut",
        location_adminlevel_governorate_code="BEI", location_name="Site X",
    )  # fmt: skip
    every = {a["code"]: a for a in facts.map_data(db)["areas"]}
    one = {a["code"]: a for a in facts.map_data(db, indicator=str(master.id))["areas"]}
    assert every["BEI"]["interventions"] == 3 and one["BEI"]["interventions"] == 2
    assert one["BEI"]["value"] == 150.0
    api = client_viewer.get(reverse("api:map", args=[db.id]) + f"?indicator={master.id}").json()
    assert {a["code"]: a["value"] for a in api["areas"]}["BEI"] == 150.0


def test_snapshot_keeps_units_and_names_the_governorate_of_twin_districts(client_viewer, hierarchy):
    db = hierarchy["database"]
    ActivityReportNew.objects.filter(dbase=db).update(location_adminlevel_caza="West")
    html = client_viewer.get(reverse("reports:database_snapshot", args=[db.id])).text
    districts = html.split('id="snap-districts"', 1)[1].split("</table>", 1)[0]
    assert "West <span" in districts and "(Akkar)" in districts and "(Beirut)" in districts
    snapshot = html.split('id="snapshot-indicators"', 1)[1].split("</table>", 1)[0]
    assert 'class="legend status-legend"' in html and "Share of girls" in snapshot


def test_hpm_names_the_previous_month_rounds_counts_and_spans_comments(client_viewer, hierarchy):
    report = hierarchy["report"]
    link = NeuroReportMasterIndicator.objects.get(report=report)
    NeuroReportComment.objects.create(report=report, master=link, comment="Scaled up", related_month="02")
    ActivityReportNew.objects.filter(indicator_id="i_a", month="2026-02").update(indicator_value=200.4)
    html = client_viewer.get(reverse("reports:report_hpm", args=[report.id]) + "?month=2").text
    table = html.split('id="ht-', 1)[1].split("</table>", 1)[0]
    assert "End of January" in table and "Previous" not in table
    assert "500.4" not in table and ">500<" in table and "+350" in table
    cell = (
        table.split('class="hpm-comments"', 1)[0].rsplit("<td", 1)[1]
        + table.split('class="hpm-comments"', 1)[1]
    )
    assert 'rowspan="1"' in cell and "Scaled up" in cell and ">1<" in cell  # the indicator's code


def test_neuro_report_has_csv_and_says_the_section_once(client_viewer, hierarchy):
    report = NeuroReport.objects.create(name="Results", ryear=hierarchy["database"].reporting_year)
    NeuroReportMasterIndicator.objects.create(report=report, master=hierarchy["master"])
    html = client_viewer.get(reverse("reports:report_dashboard", args=[report.id]) + "?month=2").text
    toolbar = (
        html.split('id="rs-', 1)[1].split("</div>", 2)[0] + html.split('id="rs-', 1)[1].split("</div>", 2)[1]
    )
    assert "data-table-csv" in html and toolbar.count("Child Protection") == 1


def test_databases_page_explains_stale_and_the_id(client_viewer, hierarchy):
    db = hierarchy["database"]
    Database.objects.filter(pk=db.pk).update(
        last_monthly_update_date=timezone.now() - datetime.timedelta(days=60)
    )
    SyncRun.objects.create(
        job=SyncRun.Job.ACTIVITYINFO_DATA, target=str(db.ai_id), status=SyncRun.Status.FAILED
    )
    html = client_viewer.get(reverse("reports:databases")).text
    assert f"ActivityInfo ID {db.ai_id}" in html and "AI 2026" not in html
    assert "The last data import of this database failed" in html
    assert 'class="legend status-legend"' in html


def test_search_humanises_names_counts_and_lists_a_group_in_full(client_viewer, hierarchy):
    db = hierarchy["database"]
    for n in range(10):
        IndicatorNew.objects.create(database=db, ai_indicator=f"x{n}", name=f"Children reached_Extra_{n}")
    html = client_viewer.get(reverse("reports:search") + "?q=reached").text
    group = html.split("ActivityInfo indicators", 1)[1]
    assert "<mark>reached</mark> · Male" in group and "_Male" not in group
    assert "8 of 12" in group and "group=indicators" in group
    full = client_viewer.get(reverse("reports:search") + "?q=reached&group=indicators").text
    assert full.count('class="search-item"') == 12 and "Master indicators" not in full


def test_pivot_readable_fields_and_three_aggregators():
    pivot = (STATIC_JS / "pivot.js").read_text()
    assert 'value_role: "Counted in master?"' in pivot and 'pd: "Programme document"' in pivot
    assert 'const AGGREGATORS = { Sum: "Integer Sum", Count: "Count", Average: "Average" };' in pivot
    assert "rowTotals" in pivot and "colTotals" in pivot
    charts = (STATIC_JS / "charts.js").read_text()
    assert 'yaxis2: { overlaying: "y", side: "right", showgrid: false, rangemode: "tozero"' in charts
