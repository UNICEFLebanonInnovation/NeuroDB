"""The management brief once the year is the calendar year (SPEC_HOME §3.4): like with like on the children tile,
the coverage guard against a pilot year, ActivityInfo's criteria only in its years, the decisions Home reuses, and
the sources of its lineage."""

from __future__ import annotations

import copy

import pytest
from django.urls import reverse

from neurodb.core.models import SyncRun
from neurodb.indicators.models import ReportingYear
from neurodb.reports import brief
from neurodb.review import services as review_services
from tests.reports.overview_fake import fake_data
from tests.reports.overview_fixture import TODAY, make_overview_data

pytestmark = pytest.mark.django_db


# ------------------------------------------------------------------------------------------- the children tile
def _impact(data, *, ai: bool, first: int = 1, etools=(100,) * 12, activityinfo=(150,) * 12, pds=(4, 6)):
    impact = data["impact"]
    impact["activityinfo_used"] = ai
    impact["activityinfo_first_month"] = first if ai else 0
    impact["activityinfo_last_month"] = 12 if ai else 0
    impact["monthly"] = {
        "labels": impact["monthly"]["labels"],
        "etools": list(etools),
        "activityinfo": list(activityinfo) if ai else [0] * 12,
    }
    impact["children_etools"] = sum(etools)
    impact["children_activityinfo"] = sum(activityinfo) if ai else 0
    impact["children_awaiting"] = 0
    impact["main_indicators"] = {"flagged": pds[0], "largest": pds[1]}
    return data


def _tile(now, before, today=TODAY):
    scope = brief.Scope(year=2026, reporting_year=None, today=today)
    builder = brief._Builder(scope, now, before)
    return builder._children_tile()


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
