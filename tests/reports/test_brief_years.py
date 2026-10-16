"""The management brief once the year is the calendar year (SPEC_HOME §3.4): like with like on the children tile,
the coverage guard against a pilot year, ActivityInfo's criteria only in its years, the decisions Home reuses, and
the sources of its lineage."""

from __future__ import annotations

import copy
import datetime
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from django.conf import settings as django_settings
from django.urls import reverse

from neurodb.core.models import SyncRun
from neurodb.datamart import models as dm
from neurodb.facts.models import ActivityReportNew
from neurodb.indicators.models import Database, ReportingYear
from neurodb.indicators.services import activityinfo
from neurodb.partnerships.models import PCA
from neurodb.reports import brief
from neurodb.reports import overview as overview_service
from neurodb.review import services as review_services
from tests.reports import brief_world
from tests.reports.brief_world import months, quarters
from tests.reports.overview_fake import fake_data
from tests.reports.overview_fixture import CHILDREN_TITLE, TODAY, make_overview_data

pytestmark = pytest.mark.django_db


# ------------------------------------------------------------------------------------------- the children tile
def _impact(data, *, ai: bool, first: int = 1, etools=(100,) * 12, activityinfo=(150,) * 12, pds=(4, 6)):
    impact = data["impact"]
    impact["activityinfo_used"] = ai
    impact["activityinfo_first_month"] = first if ai else 0
    # the last month with an ActivityInfo value, as the overview's build says it
    impact["activityinfo_last_month"] = (
        max((m for m, v in enumerate(activityinfo, 1) if v), default=0) if ai else 0
    )
    impact["etools_last_month"] = max((m for m, v in enumerate(etools, 1) if v), default=0)
    impact["monthly"] = {
        "labels": impact["monthly"]["labels"],
        "etools": list(etools),
        "activityinfo": list(activityinfo) if ai else [0] * 12,
    }
    impact["children_etools"] = sum(etools)
    impact["children_activityinfo"] = sum(activityinfo) if ai else 0
    impact["children_reached"] = max(impact["children_etools"], impact["children_activityinfo"])
    impact["children_awaiting"] = 0
    impact["main_indicators"] = {"flagged": pds[0], "largest": pds[1]}
    impact["not_yet"] = False
    return data


def _builder(now, before, today=TODAY, *, year=2026, reporting_year=None, previous_reporting_year=None):
    scope = brief.Scope(
        year=year, reporting_year=reporting_year, previous_reporting_year=previous_reporting_year, today=today
    )
    return brief._Builder(scope, now, before)


def _tile(now, before, today=TODAY, **scope):
    return _builder(now, before, today, **scope)._children_tile()


def test_both_years_with_activityinfo_over_the_same_months_compare_the_larger_sources():
    now = _impact(fake_data(), ai=True, etools=(100,) * 12, activityinfo=(150,) * 12)
    before = _impact(fake_data(), ai=True, etools=(50,) * 12, activityinfo=(100,) * 12)
    tile = _tile(now, before)
    assert tile["value"] == 150 * 7  # January to July, the larger source
    # 1050 against 700
    assert tile["delta"]["label"] == "vs same period last year" and tile["delta"]["value"] == 50.0
    assert "the larger of the two, never added" in tile["hint"] and "eTools only" not in tile["hint"]


def test_activityinfo_this_year_only_compares_etools_with_etools():
    """S1: ActivityInfo in 2026, not in 2025. The tile still shows the larger source, but the change is eTools
    against eTools (no false drop because the larger source of last year was ActivityInfo)."""
    now = _impact(fake_data(), ai=True, etools=(100,) * 12, activityinfo=(150,) * 12)
    before = _impact(fake_data(), ai=False, etools=(80,) * 12)
    tile = _tile(now, before)
    assert tile["value"] == 1050  # the larger of the two this year
    assert tile["delta"]["label"] == "vs same period last year, eTools only"
    assert tile["delta"]["value"] == 25.0  # 700 against 560
    assert tile["hint"].endswith("Compared on eTools only: ActivityInfo was not used in 2025.")


def test_activityinfo_resumed_in_july_is_not_compared_with_a_full_activityinfo_year():
    now = _impact(fake_data(), ai=True, first=7, etools=(100,) * 12, activityinfo=(0,) * 6 + (500,) * 6)
    before = _impact(fake_data(), ai=True, first=1, etools=(80,) * 12, activityinfo=(150,) * 12)
    tile = _tile(now, before)
    assert tile["delta"]["label"] == "vs same period last year, eTools only"
    assert "Compared on eTools only: ActivityInfo did not cover the same months in 2025." in tile["hint"]


def test_no_activityinfo_this_year_says_so_once():
    now = _impact(fake_data(), ai=False, etools=(100,) * 12)
    before = _impact(fake_data(), ai=True, etools=(80,) * 12)
    tile = _tile(now, before)
    assert tile["value"] == 700 and tile["delta"]["label"] == "vs same period last year, eTools only"
    assert (
        tile["hint"].endswith("· ActivityInfo was not used in 2026.")
        and "larger of the two" not in tile["hint"]
    )


def test_a_pilot_year_is_not_compared_with():
    """Last year's eTools reports covered 1 programme document, this year's 10: no "+900 %"."""
    now = _impact(fake_data(), ai=False, etools=(1000,) * 12, pds=(4, 6))
    before = _impact(fake_data(), ai=False, etools=(100,) * 12, pds=(1, 0))
    tile = _tile(now, before)
    assert tile["delta"] is None
    assert tile["hint"].endswith(
        "Not compared: 2025 has eTools partner reports for only 1 programme document."
    )
    # half of this year's documents is enough
    enough = _impact(copy.deepcopy(before), ai=False, etools=(100,) * 12, pds=(3, 2))
    assert _tile(now, enough)["delta"]["value"] == 900.0


# The suite's day, 15 October 2026 (x20): ActivityInfo's records come about two months late, so they end in August.
OCTOBER = datetime.date(2026, 10, 15)
TO_AUGUST = (150,) * 8 + (0,) * 4


def test_the_activityinfo_projection_draws_no_month_after_its_last_as_reported(activityinfo_in_use):
    """R1 parity-1, rewritten for R2 fixes-1 (the flat September and October understated the year-end): the chart
    draws every series as reported up to ``actual_months`` and dotted after it. ActivityInfo's records end in
    August (about two months late), so the solid line ends there: no month after it is drawn as reported, and
    September to December are projected from August at its pace (June to August, 150 a month), never held at the
    total to date. The headline is the series' August value."""
    now = _impact(fake_data(), ai=True, etools=(50,) * 12, activityinfo=TO_AUGUST)
    builder = _builder(now, _impact(fake_data(), ai=False), OCTOBER)
    projection = builder._pace()["projection"]
    series = projection["series"]["ActivityInfo"]
    assert projection["actual"] == {"eTools": 10, "ActivityInfo": 8}
    assert projection["actual_months"] == 8  # nothing after August is drawn as reported
    assert series[:8] == [150 * month for month in range(1, 9)]
    assert series[8:] == [1350, 1500, 1650, 1800]  # Dec = 1,200 + 150 x 4
    assert builder._children_tile()["value"] == series[7] == 1200
    # eTools, reported to October, is drawn as reported to October (``actual``), then projected
    assert projection["series"]["eTools"] == [50 * month for month in range(1, 13)]


def test_activityinfo_resumed_in_july_is_projected_at_its_own_pace(activityinfo_in_use):
    """R2 brief-2a: ActivityInfo 900 in July and in August. The pace is July and August's (900), never June's 0 in
    it: December = 1,800 + 4 x 900."""
    now = _impact(
        fake_data(), ai=True, first=7, etools=(100,) * 12, activityinfo=(0,) * 6 + (900, 900) + (0,) * 4
    )
    series = _builder(now, _impact(fake_data(), ai=False), OCTOBER)._pace()["projection"]["series"]
    assert series["ActivityInfo"][7] == 1800 and series["ActivityInfo"][11] == 1800 + 4 * 900


def test_quarterly_etools_reports_are_projected_from_their_last_quarter():
    """R2 brief-2b: on 15 October the third quarter's reports are not in (eTools reported to June). June is the
    last month drawn as reported and the pace is April to June's (100 a month), never the empty August to
    October: December = 600 + 6 x 100, not "no more children this year"."""
    quarterly = (0, 0, 300, 0, 0, 300) + (0,) * 6
    now = _impact(fake_data(), ai=False, etools=quarterly)
    projection = _builder(now, _impact(fake_data(), ai=False), OCTOBER)._pace()["projection"]
    assert projection["actual_months"] == 6 and projection["actual"] == {"eTools": 6}
    assert projection["series"]["eTools"] == [0, 0, 300, 300, 300, 600, 700, 800, 900, 1000, 1100, 1200]


def test_a_year_that_is_over_draws_no_projection(activityinfo_in_use):
    """``?year=2025`` on 15 October 2026, ActivityInfo records to April: the year is over, so every month is drawn
    as reported and its months after April hold its final total (ActivityInfo stopped), never a projected
    value."""
    to_april = (150,) * 4 + (0,) * 8
    now = _impact(fake_data(), ai=True, etools=(0, 0, 300) + (0,) * 9, activityinfo=to_april)
    projection = _builder(now, _impact(fake_data(), ai=False), OCTOBER, year=2025)._pace()["projection"]
    assert projection["actual_months"] == 12
    assert projection["series"]["ActivityInfo"] == [150, 300, 450] + [600] * 9
    assert projection["series"]["eTools"] == [0, 0] + [300] * 10


def test_an_archived_activityinfo_is_not_projected(activityinfo_archived):
    """ActivityInfo archived, its 2026 records to August (Rule 1: history): its records will not come, so its
    year ends at its total, as its forecasts are no longer made (SPEC_HOME §8), and it never pulls the solid line
    of eTools back to August."""
    now = _impact(fake_data(), ai=True, etools=(50,) * 12, activityinfo=TO_AUGUST)
    projection = _builder(now, _impact(fake_data(), ai=False), OCTOBER)._pace()["projection"]
    assert projection["series"]["ActivityInfo"] == [150 * month for month in range(1, 9)] + [1200] * 4
    assert projection["actual_months"] == 10


# charts.js's projection builder, run in node on the projection the brief sends: each trace's first and last month
# and whether it is dotted, and the month where the shaded band of the months to come starts
PROJECTION_SCRIPT = """
globalThis.document = { documentElement: { getAttribute: () => "light" } };
globalThis.getComputedStyle = () => ({ getPropertyValue: (name) => (name.startsWith("--nd-") ? "#446ab3" : "") });
const charts = await import(process.argv[1]);
const drawn = charts.BUILDERS.projection({ dataset: {}, clientWidth: 600 }, JSON.parse(process.argv[2]));
const band = drawn.layout.shapes.find((s) => s.type === "rect");
console.log(JSON.stringify({
  traces: drawn.traces.map((t) => [t.name, t.x[0], t.x[t.x.length - 1], t.line.dash || "solid"]),
  band: band ? band.x0 : null,
}));
"""
NODE = shutil.which("node")


def _drawn(projection: dict) -> dict:
    charts = Path(django_settings.BASE_DIR) / "neurodb" / "web" / "static" / "js" / "charts.js"
    result = subprocess.run(
        [NODE, "--input-type=module", "-e", PROJECTION_SCRIPT, charts.as_uri(), json.dumps(projection)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_the_chart_draws_each_source_as_reported_to_its_own_last_month(activityinfo_in_use):
    """R2 fixes-1, the drawing (the round's gate, charts.js): on 15 October eTools is reported to October and
    ActivityInfo to August. Each line is solid to its own last month and dotted after it, so eTools' September and
    October, which partners reported, are never drawn (nor hovered) as "projected", and ActivityInfo's solid line
    still ends in August. The shaded months to come start at the current month, as before the round."""
    now = _impact(fake_data(), ai=True, etools=(50,) * 12, activityinfo=TO_AUGUST)
    drawn = _drawn(_builder(now, _impact(fake_data(), ai=False), OCTOBER)._pace()["projection"])
    assert drawn["traces"] == [
        ["eTools", "Jan", "Oct", "solid"],
        ["eTools projected", "Oct", "Dec", "dot"],
        ["ActivityInfo", "Jan", "Aug", "solid"],
        ["ActivityInfo projected", "Aug", "Dec", "dot"],
    ]
    assert drawn["band"] == 9  # October, the current month
    # eTools before its third quarter's reports (to June), ActivityInfo to August: eTools is solid to June only
    quarterly = (0, 0, 300, 0, 0, 300) + (0,) * 6
    now = _impact(fake_data(), ai=True, etools=quarterly, activityinfo=TO_AUGUST)
    drawn = _drawn(_builder(now, _impact(fake_data(), ai=False), OCTOBER)._pace()["projection"])
    assert drawn["traces"][0] == ["eTools", "Jan", "Jun", "solid"]
    assert drawn["traces"][2] == ["ActivityInfo", "Jan", "Aug", "solid"]
    assert drawn["band"] == 9
    # a year that is over: every line solid to December, and no months to come
    to_april = (150,) * 4 + (0,) * 8
    now = _impact(fake_data(), ai=True, etools=(0, 0, 300) + (0,) * 9, activityinfo=to_april)
    drawn = _drawn(_builder(now, _impact(fake_data(), ai=False), OCTOBER, year=2025)._pace()["projection"])
    assert [t for t in drawn["traces"] if t[3] == "solid"] == [
        ["eTools", "Jan", "Dec", "solid"],
        ["ActivityInfo", "Jan", "Dec", "solid"],
    ]
    assert drawn["band"] is None


def test_a_last_year_without_etools_reports_is_not_compared_with():
    """R1 parity-2 (x20): 2025 has no eTools partner reports (no programme document adds a value). No change is
    shown, the hint says why and never "Compared on eTools only"."""
    now = _impact(fake_data(), ai=True, etools=(100,) * 12, activityinfo=TO_AUGUST)
    before = _impact(fake_data(), ai=False, etools=(0,) * 12, pds=(0, 0))
    tile = _tile(now, before, OCTOBER)
    assert tile["delta"] is None
    assert tile["hint"].endswith(
        "the larger of the two, never added. Not compared: 2025 has no eTools partner reports."
    )
    assert "Compared on eTools only" not in tile["hint"]


def test_a_pilot_year_with_activityinfo_this_year_says_only_not_compared():
    """R1 spec_pages-3: the coverage guard trips, so no "Compared on eTools only" beside "Not compared"."""
    now = _impact(fake_data(), ai=True, etools=(1000,) * 12, activityinfo=TO_AUGUST, pds=(4, 6))
    before = _impact(fake_data(), ai=False, etools=(100,) * 12, pds=(1, 0))
    tile = _tile(now, before, OCTOBER)
    assert tile["delta"] is None and "Compared on eTools only" not in tile["hint"]
    assert tile["hint"].endswith(
        "Not compared: 2025 has eTools partner reports for only 1 programme document."
    )


def test_january_before_any_partner_report_reads_not_yet_and_compares_nothing():
    """R1 time_roles-F1: on 5 January 2027 no report of 2027 is in (``impact.not_yet``) while a programme document
    reported children for January 2026: "Not yet" as on Home, never "0, -100 %"."""
    now = _impact(fake_data(), ai=False, etools=(0,) * 12, pds=(0, 0))
    now["impact"]["not_yet"] = True
    before = _impact(fake_data(), ai=False, etools=(120,) + (0,) * 11, pds=(1, 0))
    tile = _tile(now, before, datetime.date(2027, 1, 5), year=2027)
    assert tile["value"] is None and tile["not_yet"] and tile["delta"] is None
    assert tile["hint"] == "No partner report for 2027 yet: the first ones come after the first quarter."


def test_a_year_that_has_not_started_is_not_compared():
    """R1 time_roles-F1: ``?year=2027`` on 15 October 2026 is never compared with the whole of 2026."""
    before = _impact(fake_data(), ai=False, etools=(100,) * 12)
    empty = _tile(_impact(fake_data(), ai=False, etools=(0,) * 12, pds=(0, 0)), before, OCTOBER, year=2027)
    assert empty["value"] is None and empty["delta"] is None
    early = _tile(_impact(fake_data(), ai=False, etools=(5,) * 12), before, OCTOBER, year=2027)
    assert early["delta"] is None and early["hint"].endswith("Not compared: 2027 has not started.")


def test_the_source_line_says_what_is_compared_when_the_tile_is_not_yet():
    """R1 time_roles-F1: on 5 January 2027 the tile compares nothing, but last year's January is there (the source
    line never says "No 2026 data"); a year that has not started says so. R2 brief-3 changed the January line: the
    tile says "Not yet", so the line no longer claims a comparison with the same months of 2026."""
    now = _impact(fake_data(), ai=False, etools=(0,) * 12, pds=(0, 0))
    now["impact"]["not_yet"] = True
    before = _impact(fake_data(), ai=False, etools=(120,) + (0,) * 11, pds=(1, 0))
    headline = _builder(now, before, datetime.date(2027, 1, 5), year=2027)._headline()
    assert headline["period"] == "January 2027"  # never "January to Jan 2027"
    assert headline["source"] == (
        "The overview's figures for this year; children reached is not compared with 2026 (see its tile)."
    )
    empty = _impact(fake_data(), ai=False, etools=(0,) * 12, pds=(0, 0))
    future = _builder(empty, before, OCTOBER, year=2027)._headline()
    assert future["source"].startswith(
        "The overview's figures for 2027, which has not started: no change against 2026."
    )
    assert future["period"] == "2027 (not started)"  # R2 brief-4: never "January to Dec 2027"


def test_the_source_line_agrees_with_a_tile_that_is_not_compared():
    """R2 brief-3: the pilot guard trips, so the line under the tiles never says the figures are compared with
    the same months of 2025; a tile that compares says so."""
    now = _impact(fake_data(), ai=False, etools=(1000,) * 12, pds=(4, 6))
    pilot = _builder(now, _impact(fake_data(), ai=False, etools=(100,) * 12, pds=(1, 0)), OCTOBER)._headline()
    assert pilot["tiles"][0]["delta"] is None
    assert "against the same months" not in pilot["source"]
    assert pilot["source"].startswith(
        "The overview's figures for this year; children reached is not compared with 2025 (see its tile)."
    )
    compared = _builder(now, _impact(fake_data(), ai=False, etools=(800,) * 12), OCTOBER)._headline()
    assert compared["tiles"][0]["delta"] is not None
    assert compared["source"].startswith(
        "The overview's figures for this year, against the same months of 2025"
    )


def test_a_scope_without_etools_reports_this_year_is_not_a_drop_of_100_percent():
    now = _impact(fake_data(), ai=False, etools=(0,) * 12, pds=(0, 0))
    tile = _tile(now, _impact(fake_data(), ai=False, etools=(100,) * 12), OCTOBER)
    assert tile["value"] == 0 and tile["delta"] is None
    assert tile["hint"].endswith("Not compared: 2026 has no eTools partner reports yet.")


def test_a_year_that_is_over_has_no_etools_partner_reports_without_yet():
    """R2 fixes-4: 2024 viewed on 15 October 2026, ActivityInfo all year and no eTools programme document (2023's
    ActivityInfo began in July, so not both): "has no eTools partner reports.", never "yet" for a year that is
    over (2026's own case keeps "yet": test_a_scope_without_etools_reports_this_year_is_not_a_drop_of_100_percent)."""
    now = _impact(fake_data(), ai=True, etools=(0,) * 12, activityinfo=(150,) * 12, pds=(0, 0))
    before = _impact(fake_data(), ai=True, first=7, etools=(100,) * 12, activityinfo=(0,) * 6 + (150,) * 6)
    tile = _tile(now, before, OCTOBER, year=2024)
    assert tile["delta"] is None
    assert tile["hint"].endswith("Not compared: 2024 has no eTools partner reports.")


def test_activityinfo_records_two_months_late_are_compared_over_the_same_months():
    """R1 spec_state-1: this year's ActivityInfo records end in August, last year's cover the year, at the same
    pace of 150 a month. Both years are taken from January to August: 0 %, never -20 % (eight months against
    ten), and the hint says so."""
    now = _impact(fake_data(), ai=True, etools=(100,) * 12, activityinfo=TO_AUGUST)
    before = _impact(fake_data(), ai=True, etools=(100,) * 12, activityinfo=(150,) * 12)
    tile = _tile(now, before, OCTOBER)
    assert tile["value"] == 1200
    assert tile["delta"]["label"] == "vs same period last year" and tile["delta"]["value"] == 0
    assert tile["hint"].endswith(
        "ActivityInfo compared over the same months of both years (Jan–Aug): its 2026 records end in Aug."
    )


def test_last_years_activityinfo_ending_before_this_years_is_compared_on_etools_only():
    """§8 row 6: max against max only with the same ActivityInfo months; last year's records stopped in April."""
    now = _impact(fake_data(), ai=True, etools=(100,) * 12, activityinfo=TO_AUGUST)
    before = _impact(fake_data(), ai=True, etools=(100,) * 12, activityinfo=(150,) * 4 + (0,) * 8)
    tile = _tile(now, before, OCTOBER)
    assert tile["delta"]["label"] == "vs same period last year, eTools only"
    assert tile["hint"].endswith(
        "Compared on eTools only: ActivityInfo did not cover the same months in 2025."
    )


def test_a_year_with_activityinfo_records_never_says_activityinfo_was_not_used(reporting_year):
    """R1 time_roles-F2: in a year with ActivityInfo records (Rule 1) whose records count no children for the
    scope, the hint says that; "was not used" stays for a year without records. The same for last year's clause."""
    now = _impact(fake_data(), ai=False, etools=(100,) * 12)
    before = _impact(fake_data(), ai=False, etools=(100,) * 12)
    counted = _tile(now, before, OCTOBER, reporting_year=reporting_year)["hint"]
    assert "was not used" not in counted
    assert counted.endswith("· ActivityInfo's 2026 records count no children for this scope.")
    assert _tile(now, before, OCTOBER)["hint"].endswith("· ActivityInfo was not used in 2026.")
    now = _impact(fake_data(), ai=True, etools=(100,) * 12, activityinfo=TO_AUGUST)
    tile = _tile(now, before, OCTOBER, previous_reporting_year=reporting_year)
    assert tile["hint"].endswith(
        "Compared on eTools only: ActivityInfo's 2025 records count no children for this scope."
    )


# ----------------------------------------------------------------------------------------- the real service
@pytest.fixture
def data(hierarchy, settings):
    settings.AI_ASSISTANT_ENABLED = False
    return make_overview_data()


def build(reporting_year, year=2026):
    return brief.build(brief.Scope(year=year, reporting_year=reporting_year, today=TODAY), cache=False)


def test_the_linked_criterion_counts_only_in_activityinfo_years(data, reporting_year):
    with_ai = build(reporting_year)["confidence"]
    without = build(None)["confidence"]
    assert with_ai["linked_used"] and not without["linked_used"]
    assert any("not linked to ActivityInfo" in reason for row in with_ai["rows"] for reason in row["reasons"])
    assert not any("ActivityInfo" in reason for row in without["rows"] for reason in row["reasons"])
    assert without["reconcile"] == []


def test_the_brief_page_without_activityinfo_hides_its_words(client_viewer, data, reporting_year):
    """2025 has no ActivityInfo records: the page shows no partner-link column, no two-source table and no
    ActivityInfo tooltip on the governorate tiles; it has no "No reporting year" page either."""
    page = client_viewer.get(reverse("reports:brief") + "?year=2025")
    assert page.status_code == 200
    html = page.text
    assert "No reporting year is configured" not in html
    assert "Partners linked" not in html and "Where the two sources disagree" not in html
    assert page.context["page_subtitle"].startswith("2025 · ")
    assert 'value="2025" selected' in html and "(this year)" in html
    this_year = client_viewer.get(reverse("reports:brief")).text
    assert "Partners linked" in this_year  # 2026 has ActivityInfo records (hierarchy)


def test_the_brief_page_with_no_reporting_year_at_all_has_its_figures(client_viewer, settings, db):
    settings.AI_ASSISTANT_ENABLED = False
    make_overview_data()
    assert not ReportingYear.objects.exists()
    page = client_viewer.get(reverse("reports:brief"))
    assert page.status_code == 200 and "No reporting year is configured" not in page.text
    assert "Children reached" in page.text
    assert f'href="{reverse("reports:country_overview")}?year=2026"' in page.text  # "Country overview"


def test_the_previous_years_reporting_year_is_the_one_its_records_count_for(data, reporting_year):
    ReportingYear.objects.create(name="2025", year="2025", current=False)  # no records: no reporting year
    assert brief.previous_reporting_year(2026) is None
    assert brief.previous_reporting_year(2027) == reporting_year  # 2026 has records


def test_decisions_are_the_briefs_own(data, reporting_year):
    """Home reads the brief's decisions (brief.decisions): the same items as the brief's "Things to decide"."""
    review_services.run(date=TODAY, today=TODAY)
    from_brief = build(reporting_year)["headline"]["decide"]
    found = brief.decisions([], 2026)
    assert found == from_brief and found["items"]
    assert brief.decisions(["Education"], 2026)["items"] == [
        item for item in found["items"] if not item["section"] or item["section"] == "Education"
    ]
    assert brief.decisions([], 2026, review=None)["mode"] == found["mode"]


def test_no_review_no_decisions(db):
    assert brief.decisions([], 2026) == {"mode": "none", "items": [], "review_date": None, "decided_by": ""}


def test_the_lineage_names_activityinfo_only_in_its_years(data, reporting_year):
    assert brief.lineage_jobs(2026) == brief.LINEAGE_JOBS  # 2026 has ActivityInfo records
    assert SyncRun.Job.ACTIVITYINFO_DATA not in brief.lineage_jobs(2025)
    jobs = [source["job"] for source in brief.lineage(2025)["sources"]]
    assert jobs == [SyncRun.Job.ETOOLS_DATAMART, SyncRun.Job.POPULATION]
    assert [s["job"] for s in build(None, year=2025)["lineage"]["sources"]] == jobs
    assert "ai_data" in [s["job"] for s in brief.lineage(2026)["sources"]]


def _words(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", re.sub(r"<script.*?</script>", " ", html, flags=re.S)))


def test_the_brief_on_5_january_reads_not_yet_and_never_minus_100(client, admin_user, data, site_today):
    """R1 time_roles-F1, the page: a programme document reported children for January 2026; on 5 January 2027 no
    2027 report is in. The brief's tile reads "Not yet" (as Home's), with no "-100 %"."""
    pd = PCA.objects.get(number="LEB/PD1")
    pd.end = datetime.date(2027, 12, 31)
    pd.save()
    place = dm.ReportedIndicator.objects.first().location_ref
    dm.ReportedIndicator.objects.create(
        datamart_id=99, partner=pd.partner, intervention=pd, partner_name=pd.partner.name,
        pd_reference_number=pd.number, progress_report="PR-99", report_number="MPR1", report_type="QPR",
        report_status="Accepted", period_start=datetime.date(2026, 1, 1), period_end=datetime.date(2026, 1, 31),
        due_date=datetime.date(2026, 2, 15), submission_date=datetime.date(2026, 2, 10), indicator=CHILDREN_TITLE,
        target="1000", location=place.name, p_code=place.p_code, location_ref=place, achievement_in_period="120",
        total_cumulative_progress="120", total_cumulative_progress_in_location="120",
        calculation_across_locations="sum", etools_indicator_id="100",
    )  # fmt: skip
    site_today.set(datetime.date(2027, 1, 5))
    client.force_login(admin_user)
    page = client.get(reverse("reports:brief"))
    tile = next(t for t in page.context["data"]["headline"]["tiles"] if t["key"] == "children")
    assert tile["value"] is None and tile["delta"] is None
    words = _words(page.text)
    assert "Children reached Not yet" in words and "-100" not in words
    assert "Children reached: not yet." in page.context["data"]["text"]


def test_the_real_brief_of_a_scope_without_activityinfo_children_in_an_activityinfo_year(
    data, reporting_year
):
    """R1 time_roles-F2 / parity-3: 2026 has ActivityInfo records (``hierarchy``) but none for this scope."""
    scope = brief.Scope(year=2026, reporting_year=reporting_year, sections=["No such section"], today=TODAY)
    tile = next(t for t in brief.build(scope, cache=False)["headline"]["tiles"] if t["key"] == "children")
    assert "was not used in 2026" not in tile["hint"]
    assert "ActivityInfo's 2026 records count no children for this scope." in tile["hint"]


def test_the_brief_key_holds_last_years_activityinfo_reporting_year(data, reporting_year, monkeypatch):
    """R1 privacy_cache-PC-2: hiding last year's only database ("Show on dashboard") ends 2025 as an ActivityInfo
    year. The stored brief, which compared with 2025's ActivityInfo figures, is not served again: its key
    changes and the next build is computed."""
    from neurodb.core import compute

    ry25 = ReportingYear.objects.create(name="2025", year="2025", current=False)
    db25 = Database.objects.create(
        ai_id=202518, db_id="x2025", name="CP 2025", label="CP", username="", password="", reporting_year=ry25,
        section=reporting_year.database_set.first().section, is_funded_by_unicef=False,
    )  # fmt: skip
    ActivityReportNew.objects.create(
        dbase=db25, database_ai_id="202518", indicator_id="i_a", indicator_name="i_a", indicator_value=5,
        month_name="2025-03-01", month="2025-03", partner_label="P", project_label="X",
    )  # fmt: skip
    assert activityinfo.reporting_year_for(2025) == ry25
    computed = []
    real = brief._build

    def spy(scope, **kwargs):
        computed.append(brief.previous_reporting_year(scope.year))
        return real(scope, **kwargs)

    monkeypatch.setattr(brief, "_build", spy)
    scope = brief.Scope(year=2026, reporting_year=activityinfo.reporting_year_for(2026), today=OCTOBER)
    before = brief._cache_key(scope)
    brief.build(scope)
    compute.reset_memo()
    db25.display = False
    db25.save()  # the ActivityInfo state follows at once (indicators.signals)
    compute.reset_memo()
    assert activityinfo.reporting_year_for(2025) is None
    assert brief._cache_key(scope) != before
    brief.build(scope)
    assert computed == [ry25, None]


def test_a_year_without_activityinfo_describes_no_activityinfo_criterion(client_viewer, data, reporting_year):
    """R1 spec_pages-7: 2025 has no ActivityInfo records, so the confidence block's source and the governorate
    line name neither the partner-link criterion nor the two sources; 2026 still does."""
    without = build(None, year=2025)
    assert "ActivityInfo" not in without["confidence"]["source"]
    assert "partners with an ActivityInfo partner link" in build(reporting_year)["confidence"]["source"]
    words = _words(client_viewer.get(reverse("reports:brief") + "?year=2025").text)
    assert "ActivityInfo partner link" not in words and "larger of the two sources" not in words
    assert "Children reached per governorate (eTools), against the estimated children there" in words
    assert "The larger of the two sources per governorate" in _words(
        client_viewer.get(reverse("reports:brief")).text
    )


# ------------------------------------------------------- worlds counted by hand (R2 brief review), the real builds
@pytest.fixture
def world(settings, db):
    settings.AI_ASSISTANT_ENABLED = False
    return brief_world.World()


def _brief(scope_year: int = 2026, today: datetime.date = OCTOBER) -> dict:
    from neurodb.core import compute

    compute.reset_memo()
    scope = brief.Scope(
        year=scope_year, reporting_year=activityinfo.reporting_year_for(scope_year), today=today
    )
    return brief.build(scope, cache=False)


def _children(data: dict) -> dict:
    return next(t for t in data["headline"]["tiles"] if t["key"] == "children")


def test_october_before_the_third_quarters_reports_compares_january_to_june(world):
    """R2 brief-1a: one programme document reports 300 a quarter in 2025 and 2026; on 15 October its third-quarter
    report of 2026 is not in (the overview: "eTools 600 (Jan–Jun)"). Both years are compared from January to June:
    0 %, never -33 % (600 against 2025's 900 to October), and the hint says which months."""
    world.section("Child Protection", "CP")
    world.pd("Child Protection", {**quarters(2025, (1, 2, 3, 4), 300), **quarters(2026, (1, 2), 300)})
    data = _brief()
    tile = _children(data)
    assert tile["value"] == 600 and tile["delta"]["value"] == 0
    assert tile["delta"]["label"] == "vs same period last year, eTools only"
    assert tile["hint"].endswith(
        "eTools compared over Jan–Jun of both years: the partner reports for later months are not in yet."
    )
    # R2 brief-2b, through the real build: projected from June at April to June's pace, never flat
    projection = data["pace"]["projection"]
    assert projection["actual"] == {"eTools": 6}
    assert projection["series"]["eTools"][5:] == [600, 700, 800, 900, 1000, 1100, 1200]


def test_april_with_one_of_ten_first_quarter_reports_is_not_compared(world, site_today):
    """R2 brief-1b: 10 April 2027, ten programme documents reported 300 a quarter in 2026; one has sent its first
    report of 2027 (the others are due on 15 April). Not "-90 %": the reports are still coming in."""
    world.section("Child Protection", "CP")
    world.pd("Child Protection", {**quarters(2026, (1, 2, 3, 4), 300), **quarters(2027, (1,), 300)})
    for _ in range(9):
        world.pd("Child Protection", quarters(2026, (1, 2, 3, 4), 300))
    site_today.set(datetime.date(2027, 4, 10))
    activityinfo.invalidate()
    tile = _children(_brief(2027, datetime.date(2027, 4, 10)))
    assert tile["value"] == 300 and tile["delta"] is None
    assert tile["hint"].endswith(
        "Not compared: 2027's partner reports for Jan–Mar are still coming in (1 programme document so far, "
        "10 in 2026)."
    )


def test_the_pilot_guard_counts_last_years_documents_over_the_months_compared(world):
    """R2 brief-1c: four programme documents report 100 a month in 2026; in 2025 one reported from January and three
    only in November and December. January to October 2025 has one programme document, so the pilot guard trips
    (never "+300 %"), though the whole of 2025 has four."""
    world.section("Child Protection", "CP")
    world.pd("Child Protection", {**months(2025, 12, 100), **months(2026, 10, 100)})
    for _ in range(3):
        world.pd("Child Protection", {(2025, 11): 100, (2025, 12): 100, **months(2026, 10, 100)})
    data = _brief()
    tile = _children(data)
    assert tile["value"] == 4000 and tile["delta"] is None
    assert tile["hint"].endswith(
        "Not compared: 2025 has eTools partner reports for only 1 programme document over Jan–Oct."
    )
    # the overview counts the programme documents adding a value from January to each month
    before = overview_service.build(
        overview_service.Scope(year=2025, today=datetime.date(2025, 10, 15)), cache=False
    )
    counts = before["impact"]["monthly"]["etools_pds_to_date"]
    assert counts[9] == 1 and counts[11] == 4
    assert before["impact"]["main_indicators"] == {"flagged": 0, "largest": 4}
    # R2 brief-3: the line under the tiles agrees with the tile
    assert "against the same months" not in data["headline"]["source"]


def test_activityinfo_resumed_in_july_through_the_real_build(world, activityinfo_in_use):
    """R2 brief-2a with the real overview: ActivityInfo 900 in July and August 2026, none in 2025. The projection
    starts after August at 900 a month: December = 1,800 + 4 x 900; the solid line ends in August."""
    world.section("Child Protection", "CP")
    world.pd("Child Protection", {**months(2025, 12, 100), **months(2026, 10, 100)})
    world.ai(2026, "Child Protection", {7: 900, 8: 900}, current=True)
    data = _brief()
    projection = data["pace"]["projection"]
    assert projection["actual_months"] == 8 and projection["actual"] == {"eTools": 10, "ActivityInfo": 8}
    assert projection["series"]["ActivityInfo"][6:] == [900, 1800, 2700, 3600, 4500, 5400]
    assert _children(data)["value"] == projection["series"]["ActivityInfo"][7] == 1800


def test_a_year_that_has_not_started_reads_not_yet_on_the_overview_as_on_the_brief(world, client, admin_user):
    """R2 brief-4: on 15 October 2026 ``?year=2027`` (a typed address) reads "Not yet · 2027 has not started" on the
    Country overview, as on the brief, never "Children reached 0"; the brief's period is "2027 (not started)"."""
    world.section("Child Protection", "CP")
    world.pd("Child Protection", {**months(2025, 12, 100), **months(2026, 10, 100)})
    client.force_login(admin_user)
    page = client.get(reverse("reports:country_overview") + "?year=2027")
    assert page.status_code == 200
    from tests.web.parts import text

    words = _words(text(page))
    assert "Children reached 0 " not in words
    assert "Children reached Not yet 2027 has not started. See 2026" in words
    assert "No partner report for 2027 yet" not in words
    brief_page = client.get(reverse("reports:brief") + "?year=2027")
    data = brief_page.context["data"]
    assert _children(data)["value"] is None and data["headline"]["period"] == "2027 (not started)"
