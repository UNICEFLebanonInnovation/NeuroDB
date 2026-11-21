"""The management brief once the year is the calendar year (SPEC_HOME §3.4): like with like on the children tile,
the coverage guard against a pilot year, eTools alone (Nov 2026: ActivityInfo is an archive, never compared,
projected or a criterion), the decisions Home reuses, and the sources of its lineage."""

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
from neurodb.indicators.models import ReportingYear
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
def _impact(data, *, etools=(100,) * 12, pds=(4, 6)):
    impact = data["impact"]
    impact["etools_last_month"] = max((m for m, v in enumerate(etools, 1) if v), default=0)
    impact["monthly"] = {"labels": impact["monthly"]["labels"], "etools": list(etools)}
    impact["children_etools"] = sum(etools)
    impact["children_reached"] = impact["children_etools"]
    impact["children_awaiting"] = 0
    impact["main_indicators"] = {"flagged": pds[0], "largest": pds[1]}
    impact["not_yet"] = False
    return data


def _builder(now, before, today=TODAY, *, year=2026):
    return brief._Builder(brief.Scope(year=year, today=today), now, before)


def _tile(now, before, today=TODAY, **scope):
    return _builder(now, before, today, **scope)._children_tile()


def test_the_tile_compares_etools_with_etools_and_names_nothing_else():
    now = _impact(fake_data(), etools=(100,) * 12)
    before = _impact(fake_data(), etools=(80,) * 12)
    tile = _tile(now, before)
    assert tile["value"] == 700 and tile["delta"]["label"] == "vs same period last year"
    assert tile["delta"]["value"] == 25.0  # 700 against 560
    assert tile["hint"].startswith("eTools 1,200 (") and tile["hint"].endswith(").")
    assert "ActivityInfo" not in tile["hint"] and "larger of the two" not in tile["hint"]


def test_a_pilot_year_is_not_compared_with():
    """Last year's eTools reports covered 1 programme document, this year's 10: no "+900 %"."""
    now = _impact(fake_data(), etools=(1000,) * 12, pds=(4, 6))
    before = _impact(fake_data(), etools=(100,) * 12, pds=(1, 0))
    tile = _tile(now, before)
    assert tile["delta"] is None
    assert tile["hint"].endswith(
        "Not compared: 2025 has eTools partner reports for only 1 programme document."
    )
    # half of this year's documents is enough
    enough = _impact(copy.deepcopy(before), etools=(100,) * 12, pds=(3, 2))
    assert _tile(now, enough)["delta"]["value"] == 900.0


# The suite's day, 15 October 2026 (x20)
OCTOBER = datetime.date(2026, 10, 15)


def test_quarterly_etools_reports_are_projected_from_their_last_quarter():
    """R2 brief-2b: on 15 October the third quarter's reports are not in (eTools reported to June). June is the
    last month drawn as reported and the pace is April to June's (100 a month), never the empty August to
    October: December = 600 + 6 x 100, not "no more children this year"."""
    quarterly = (0, 0, 300, 0, 0, 300) + (0,) * 6
    now = _impact(fake_data(), etools=quarterly)
    projection = _builder(now, _impact(fake_data()), OCTOBER)._pace()["projection"]
    assert projection["actual_months"] == 6 and projection["actual"] == {"eTools": 6}
    assert projection["series"]["eTools"] == [0, 0, 300, 300, 300, 600, 700, 800, 900, 1000, 1100, 1200]


def test_a_year_that_is_over_draws_no_projection():
    """``?year=2025`` on 15 October 2026: the year is over, so nothing is projected. R3 code-2 (changed): the line
    ends at its last reported month (March), with no value after it, never held at its total to December (drawn as
    reported, it said no child was reached in those months)."""
    now = _impact(fake_data(), etools=(0, 0, 300) + (0,) * 9)
    projection = _builder(now, _impact(fake_data()), OCTOBER, year=2025)._pace()["projection"]
    assert projection["actual"] == {"eTools": 3} and projection["actual_months"] == 3
    assert projection["projected"] == [] and list(projection["series"]) == ["eTools"]
    assert projection["series"]["eTools"] == [0, 0, 300] + [None] * 9


# charts.js's projection builder, run in node on the projection the brief sends: each trace's first and last month
# and whether it is dotted, and the month where the shaded band of the months to come starts
PROJECTION_SCRIPT = """
globalThis.document = { documentElement: { getAttribute: () => "light" } };
globalThis.getComputedStyle = () => ({ getPropertyValue: (name) => (name.startsWith("--nd-") ? "#446ab3" : "") });
const charts = await import(process.argv[1]);
const drawn = charts.BUILDERS.projection({ dataset: {}, clientWidth: 600 }, JSON.parse(process.argv[2]));
const band = drawn.layout.shapes.find((s) => s.type === "rect");
// what each point hovers as: the trace's template with its month, its customdata and its value
const hover = (t, j) =>
  t.hovertemplate
    .replace("%{x}", t.x[j])
    .replace("%{customdata}", t.customdata ? t.customdata[j] : "")
    .replace(/%[{]y[^}]*[}]/, String(t.y[j]))
    .replace("<extra></extra>", "");
console.log(JSON.stringify({
  traces: drawn.traces.map((t) => [t.name, t.x[0], t.x[t.x.length - 1], t.line.dash || "solid"]),
  hovers: drawn.traces.map((t) => t.x.map((_, j) => hover(t, j))),
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
def test_the_chart_draws_the_line_as_reported_to_its_last_month():
    """R2 fixes-1, the drawing (the round's gate, charts.js): on 15 October eTools is reported to October. The line
    is solid to October and dotted after it, so September and October, which partners reported, are never drawn
    (nor hovered) as "projected". The shaded months to come start at the current month, as before the round. One
    line: ActivityInfo is an archive (Nov 2026)."""
    now = _impact(fake_data(), etools=(50,) * 12)
    drawn = _drawn(_builder(now, _impact(fake_data()), OCTOBER)._pace()["projection"])
    assert drawn["traces"] == [["eTools", "Jan", "Oct", "solid"], ["eTools projected", "Oct", "Dec", "dot"]]
    assert drawn["band"] == 9  # October, the current month
    # R3 browser-2: the last reported month hovers as reported, never as "projected", though the dotted line starts
    # there to join the solid one; the months after it do
    assert drawn["hovers"][1][:2] == ["Oct · eTools: 500", "Nov · eTools projected: 550"]
    assert not any("projected" in h for h in drawn["hovers"][0])
    # before the third quarter's reports (to June): solid to June only
    quarterly = (0, 0, 300, 0, 0, 300) + (0,) * 6
    now = _impact(fake_data(), etools=quarterly)
    drawn = _drawn(_builder(now, _impact(fake_data()), OCTOBER)._pace()["projection"])
    assert drawn["traces"][0] == ["eTools", "Jan", "Jun", "solid"] and drawn["band"] == 9
    # a year that is over (R3 code-2, changed: no line solid to December): solid to its last month and no further,
    # nothing dotted, and no months to come
    now = _impact(fake_data(), etools=(0, 0, 300) + (0,) * 9)
    drawn = _drawn(_builder(now, _impact(fake_data()), OCTOBER, year=2025)._pace()["projection"])
    assert drawn["traces"] == [["eTools", "Jan", "Mar", "solid"]]
    assert drawn["band"] is None


def test_a_last_year_without_etools_reports_is_not_compared_with():
    """R1 parity-2 (x20): 2025 has no eTools partner reports (no programme document adds a value). No change is
    shown, the hint says why and never "Compared on eTools only"."""
    now = _impact(fake_data(), etools=(100,) * 12)
    before = _impact(fake_data(), etools=(0,) * 12, pds=(0, 0))
    tile = _tile(now, before, OCTOBER)
    assert tile["delta"] is None
    assert tile["hint"].endswith("). Not compared: 2025 has no eTools partner reports.")
    assert "Compared on eTools only" not in tile["hint"]


def test_january_before_any_partner_report_reads_not_yet_and_compares_nothing():
    """R1 time_roles-F1: on 5 January 2027 no report of 2027 is in (``impact.not_yet``) while a programme document
    reported children for January 2026: "Not yet" as on Home, never "0, -100 %"."""
    now = _impact(fake_data(), etools=(0,) * 12, pds=(0, 0))
    now["impact"]["not_yet"] = True
    before = _impact(fake_data(), etools=(120,) + (0,) * 11, pds=(1, 0))
    tile = _tile(now, before, datetime.date(2027, 1, 5), year=2027)
    assert tile["value"] is None and tile["not_yet"] and tile["delta"] is None
    assert tile["hint"] == "No partner report for 2027 yet: the first ones come after the first quarter."


def test_a_year_that_has_not_started_is_not_compared():
    """R1 time_roles-F1: ``?year=2027`` on 15 October 2026 is never compared with the whole of 2026."""
    before = _impact(fake_data(), etools=(100,) * 12)
    empty = _tile(_impact(fake_data(), etools=(0,) * 12, pds=(0, 0)), before, OCTOBER, year=2027)
    assert empty["value"] is None and empty["delta"] is None
    early = _tile(_impact(fake_data(), etools=(5,) * 12), before, OCTOBER, year=2027)
    assert early["delta"] is None and early["hint"].endswith("Not compared: 2027 has not started.")


def test_the_source_line_says_what_is_compared_when_the_tile_is_not_yet():
    """R1 time_roles-F1: on 5 January 2027 the tile compares nothing, but last year's January is there (the source
    line never says "No 2026 data"); a year that has not started says so. R2 brief-3 changed the January line: the
    tile says "Not yet", so the line no longer claims a comparison with the same months of 2026."""
    now = _impact(fake_data(), etools=(0,) * 12, pds=(0, 0))
    now["impact"]["not_yet"] = True
    before = _impact(fake_data(), etools=(120,) + (0,) * 11, pds=(1, 0))
    headline = _builder(now, before, datetime.date(2027, 1, 5), year=2027)._headline()
    assert headline["period"] == "January 2027"  # never "January to Jan 2027"
    assert headline["source"] == (
        "The overview's figures for this year; children reached is not compared with 2026 (see its tile)."
    )
    empty = _impact(fake_data(), etools=(0,) * 12, pds=(0, 0))
    future = _builder(empty, before, OCTOBER, year=2027)._headline()
    assert future["source"].startswith(
        "The overview's figures for 2027, which has not started: no change against 2026."
    )
    assert future["period"] == "2027 (not started)"  # R2 brief-4: never "January to Dec 2027"


def test_the_source_line_agrees_with_a_tile_that_is_not_compared():
    """R2 brief-3: the pilot guard trips, so the line under the tiles never says the figures are compared with
    the same months of 2025; a tile that compares says so."""
    now = _impact(fake_data(), etools=(1000,) * 12, pds=(4, 6))
    pilot = _builder(now, _impact(fake_data(), etools=(100,) * 12, pds=(1, 0)), OCTOBER)._headline()
    assert pilot["tiles"][0]["delta"] is None
    assert "against the same months" not in pilot["source"]
    assert pilot["source"].startswith(
        "The overview's figures for this year; children reached is not compared with 2025 (see its tile)."
    )
    compared = _builder(now, _impact(fake_data(), etools=(800,) * 12), OCTOBER)._headline()
    assert compared["tiles"][0]["delta"] is not None
    assert compared["source"].startswith(
        "The overview's figures for this year, against the same months of 2025"
    )


def test_a_scope_without_etools_reports_this_year_is_not_a_drop_of_100_percent():
    now = _impact(fake_data(), etools=(0,) * 12, pds=(0, 0))
    tile = _tile(now, _impact(fake_data(), etools=(100,) * 12), OCTOBER)
    assert tile["value"] == 0 and tile["delta"] is None
    assert tile["hint"].endswith("Not compared: 2026 has no eTools partner reports yet.")


def test_a_year_that_is_over_has_no_etools_partner_reports_without_yet():
    """R2 fixes-4: 2024 viewed on 15 October 2026, no eTools programme document: "has no eTools partner reports.",
    never "yet" for a year that is over (2026's own case keeps "yet":
    test_a_scope_without_etools_reports_this_year_is_not_a_drop_of_100_percent)."""
    now = _impact(fake_data(), etools=(0,) * 12, pds=(0, 0))
    before = _impact(fake_data(), etools=(100,) * 12)
    tile = _tile(now, before, OCTOBER, year=2024)
    assert tile["delta"] is None
    assert tile["hint"].endswith("Not compared: 2024 has no eTools partner reports.")


# ----------------------------------------------------------------------------------------- the real service
@pytest.fixture
def data(hierarchy, settings):
    settings.AI_ASSISTANT_ENABLED = False
    return make_overview_data()


def build(reporting_year, year=2026):  # noqa: ARG001 - the year's ActivityInfo records count for nothing
    return brief.build(brief.Scope(year=year, today=TODAY), cache=False)


def test_the_brief_page_without_activityinfo_hides_its_words(client_viewer, data, reporting_year):
    """Neither 2025 nor 2026 (which has ActivityInfo records: hierarchy) shows a partner-link column, a two-source
    table or an ActivityInfo tooltip on the governorate tiles (Nov 2026); no "No reporting year" page either."""
    page = client_viewer.get(reverse("reports:brief") + "?year=2025")
    assert page.status_code == 200
    html = page.text
    assert "No reporting year is configured" not in html
    assert "Partners linked" not in html and "Where the two sources disagree" not in html
    assert page.context["page_subtitle"].startswith("2025 · ")
    assert 'value="2025" selected' in html and "(this year)" in html
    this_year = client_viewer.get(reverse("reports:brief")).text
    assert "Partners linked" not in this_year and "Where the two sources disagree" not in this_year


def test_the_brief_page_with_no_reporting_year_at_all_has_its_figures(client_viewer, settings, db):
    settings.AI_ASSISTANT_ENABLED = False
    make_overview_data()
    assert not ReportingYear.objects.exists()
    page = client_viewer.get(reverse("reports:brief"))
    assert page.status_code == 200 and "No reporting year is configured" not in page.text
    assert "Children reached" in page.text
    assert f'href="{reverse("reports:country_overview")}?year=2026"' in page.text  # "Country overview"


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


def test_the_lineage_never_names_activityinfo(data, reporting_year):
    """2026 has ActivityInfo records (hierarchy): the brief's sources are eTools' and the population figures, every
    year (Nov 2026: ActivityInfo is an archive)."""
    jobs = [SyncRun.Job.ETOOLS_DATAMART, SyncRun.Job.POPULATION]
    for year in (2025, 2026):
        assert [source["job"] for source in brief.lineage(year)["sources"]] == jobs
        assert [s["job"] for s in build(None, year=year)["lineage"]["sources"]] == jobs


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


def test_no_year_describes_an_activityinfo_criterion(client_viewer, data, reporting_year):
    """R1 spec_pages-7, Nov 2026: neither 2025 nor 2026 (with ActivityInfo records) names the partner-link
    criterion or the two sources, in the confidence block's source or the governorate line."""
    for year in (2025, 2026):
        assert "ActivityInfo" not in build(reporting_year, year=year)["confidence"]["source"]
        words = _words(client_viewer.get(reverse("reports:brief") + f"?year={year}").text)
        assert "ActivityInfo partner link" not in words and "larger of the two sources" not in words
        assert "Children reached per governorate (eTools), against the estimated children there" in words


# ------------------------------------------------------- worlds counted by hand (R2 brief review), the real builds
@pytest.fixture
def world(settings, db):
    settings.AI_ASSISTANT_ENABLED = False
    return brief_world.World()


def _brief(scope_year: int = 2026, today: datetime.date = OCTOBER, sections: tuple[str, ...] = ()) -> dict:
    from neurodb.core import compute

    compute.reset_memo()
    scope = brief.Scope(year=scope_year, sections=list(sections), today=today)
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
    assert tile["delta"]["label"] == "vs same period last year"
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


# --------------------------------------------- R3 code-1: a quarter whose partner reports are only partly in
def test_one_early_third_quarter_report_of_ten_is_not_a_drop(world):
    """R3 code-1 (world l1): 15 October 2026, ten programme documents report 300 a quarter in 2025 and 2026; one has
    sent its third-quarter report early, the nine others are due on 15 October. The overview says "eTools 6,300
    (Jan–Sep)", but September's reports are not all in (1 of 10): both years are compared from January to June
    (6,000 against 6,000: 0 %, never -30 % against 2025's 9,000 to September), and the hint says from which month
    the reports are not all in. The projection starts from June at April to June's pace: December 12,000, the
    programme's pace, never 6,600 from one report in ten."""
    world.section("Child Protection", "CP")
    world.pd("Child Protection", {**quarters(2025, (1, 2, 3, 4), 300), **quarters(2026, (1, 2, 3), 300)})
    for _ in range(9):
        world.pd("Child Protection", {**quarters(2025, (1, 2, 3, 4), 300), **quarters(2026, (1, 2), 300)})
    data = _brief()
    tile = _children(data)
    assert tile["value"] == 6300 and tile["delta"]["value"] == 0
    assert tile["hint"].endswith(
        "eTools compared over Jan–Jun of both years: the partner reports from Jul are not all in yet."
    )
    projection = data["pace"]["projection"]
    assert projection["actual"] == {"eTools": 6} and projection["projected"] == ["eTools"]
    assert projection["series"]["eTools"][5:] == [6000, 7000, 8000, 9000, 10000, 11000, 12000]


def test_the_country_agrees_with_its_sections_while_a_quarter_comes_in(world):
    """R3 code-1 (world m): 15 October 2026, Child Protection reports 100 a month (to September), Education 300 a
    quarter (its third-quarter report is due on 15 October). Each section alone shows 0 %, and so does the country,
    compared from January to June (1,200 against 1,200), never -16.7 % from Education's quarter not in."""
    world.section("Child Protection", "CP")
    world.section("Education", "EDU")
    world.pd("Child Protection", {**months(2025, 12, 100), **months(2026, 9, 100)})
    world.pd("Education", {**quarters(2025, (1, 2, 3, 4), 300), **quarters(2026, (1, 2), 300)})
    country = _children(_brief())
    assert country["value"] == 1500 and country["delta"]["value"] == 0
    assert (
        "eTools compared over Jan–Jun of both years: the partner reports from Jul are not all in yet."
        in (country["hint"])
    )
    for section, months_compared in (("Education", "Jan–Jun"), ("Child Protection", "Jan–Sep")):
        tile = _children(_brief(sections=(section,)))
        assert tile["delta"]["value"] == 0, section
        assert f"eTools compared over {months_compared} of both years" in tile["hint"], section


def test_a_late_report_before_a_later_one_is_still_waited_for(world):
    """R3 code-1 (world l2): 15 October 2026, one programme document reports 300 a quarter; its second-quarter report
    of 2026 is late, though its third is in. Its second quarter is not all in (a later report came, so that one is
    missing): compared from January to March, 300 against 300 (0 %), never -33 % (600 against 900)."""
    world.section("Child Protection", "CP")
    world.pd("Child Protection", {**quarters(2025, (1, 2, 3, 4), 300), **quarters(2026, (1, 3), 300)})
    tile = _children(_brief())
    assert tile["value"] == 600 and tile["delta"]["value"] == 0
    assert tile["hint"].endswith(
        "eTools compared over Jan–Mar of both years: the partner reports from Apr are not all in yet."
    )


@pytest.mark.parametrize("sent,change", [(5, None), (6, None), (9, -10.0)])
def test_april_with_part_of_the_first_quarters_reports(world, site_today, sent, change):
    """R3 code-1 (world k2): 10 April 2027, ten programme documents reported 300 a quarter in 2026; ``sent`` of them
    have sent their first report of 2027 (the others are due on 15 April). With 5 or 6 in, the first quarter's
    reports are still coming in: no comparison and no projection, never "-50 %" or "-40 %" (the old guard let half
    of last year's documents through). Nine in ten is enough (REPORTS_IN_SHARE): -10 %."""
    world.section("Child Protection", "CP")
    for n in range(10):
        world.pd(
            "Child Protection",
            {**quarters(2026, (1, 2, 3, 4), 300), **quarters(2027, (1,) if n < sent else (), 300)},
        )
    site_today.set(datetime.date(2027, 4, 10))
    activityinfo.invalidate()
    data = _brief(2027, datetime.date(2027, 4, 10))
    tile = _children(data)
    assert tile["value"] == 300 * sent and (tile["delta"] or {}).get("value") == change
    if change is None:
        assert tile["hint"].endswith(
            f"Not compared: 2027's partner reports for Jan–Mar are still coming in ({sent} programme documents so "
            "far, 10 in 2026)."
        )
        assert data["pace"]["projection"]["series"]["eTools"] == []


def test_reports_that_stopped_are_no_longer_waited_for_two_months_after_the_quarter(world):
    """15 October 2026: one programme document reports every quarter; another reported 2025 in full and only the
    first quarter of 2026. Its second quarter is no longer waited for (two months after June: from 1 September),
    its third still is (until 1 December): compared from January to June, its drop shows (900 against 1,200),
    never "still coming in" all year for a document whose reports stopped."""
    world.section("Child Protection", "CP")
    world.pd("Child Protection", {**quarters(2025, (1, 2, 3, 4), 300), **quarters(2026, (1, 2, 3), 300)})
    world.pd("Child Protection", {**quarters(2025, (1, 2, 3, 4), 300), **quarters(2026, (1,), 300)})
    tile = _children(_brief())
    assert tile["value"] == 1200 and tile["delta"]["value"] == -25.0
    assert tile["hint"].endswith(
        "eTools compared over Jan–Jun of both years: the partner reports from Jul are not all in yet."
    )


def test_a_programme_document_that_ended_is_not_waited_for(world):
    """The same two programme documents, the second one ended on 31 March 2026 (its last report): after March it
    is not expected, so the months to September are all in and compared (1,200 against 1,800: -33.3 %, the
    programme document that ended)."""
    world.section("Child Protection", "CP")
    world.pd("Child Protection", {**quarters(2025, (1, 2, 3, 4), 300), **quarters(2026, (1, 2, 3), 300)})
    ended = world.pd("Child Protection", {**quarters(2025, (1, 2, 3, 4), 300), **quarters(2026, (1,), 300)})
    PCA.objects.filter(pk=ended.pk).update(end=datetime.date(2026, 3, 31))
    tile = _children(_brief())
    assert tile["value"] == 1200 and tile["delta"]["value"] == -33.3
    assert tile["hint"].endswith(
        "eTools compared over Jan–Sep of both years: the partner reports for later months are not in yet."
    )


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_last_years_brief_in_january_draws_no_month_not_reported(world, client, admin_user, site_today):
    """R3 code-2 (world o): 5 January 2027, ``?year=2026``, one programme document's fourth-quarter report is due on
    15 January. The tile compares January to September; the chart draws eTools as reported to September and ends
    there, never solid and flat to December ("no child reached in the fourth quarter"). The key under the chart
    names only what is drawn: a year that is over has no dotted line and no months to come."""
    world.section("Child Protection", "CP")
    world.pd("Child Protection", {**quarters(2025, (1, 2, 3, 4), 300), **quarters(2026, (1, 2, 3), 300)})
    site_today.set(datetime.date(2027, 1, 5))
    activityinfo.invalidate()
    data = _brief(2026, datetime.date(2027, 1, 5))
    projection = data["pace"]["projection"]
    assert projection["series"]["eTools"][8:] == [900, None, None, None]
    assert _drawn(projection)["traces"] == [["eTools", "Jan", "Sep", "solid"]]
    client.force_login(admin_user)
    words = _words(client.get(reverse("reports:brief") + "?year=2026").text)
    assert "Solid line: reported." in words
    assert "Dotted line" not in words and "Shaded: the months to come" not in words


def test_the_chart_key_names_the_dotted_lines_and_the_months_to_come(world, client, admin_user):
    """R3 browser-N1: the brief's chart says what its marks mean on the page itself: solid, dotted and the shaded
    months to come (15 October 2026, the year in progress)."""
    world.section("Child Protection", "CP")
    world.pd("Child Protection", {**months(2025, 12, 100), **months(2026, 9, 100)})
    client.force_login(admin_user)
    words = _words(client.get(reverse("reports:brief")).text)
    assert (
        "Solid line: reported. Dotted line: at this pace, to December. Shaded: the months to come." in words
    )
