"""The management brief once the year is the calendar year (SPEC_HOME §3.4): like with like on the children tile,
the coverage guard against a pilot year, ActivityInfo's criteria only in its years, the decisions Home reuses, and
the sources of its lineage."""

from __future__ import annotations

import copy
import datetime
import re

import pytest
from django.urls import reverse

from neurodb.core.models import SyncRun
from neurodb.datamart import models as dm
from neurodb.facts.models import ActivityReportNew
from neurodb.indicators.models import Database, ReportingYear
from neurodb.indicators.services import activityinfo
from neurodb.partnerships.models import PCA
from neurodb.reports import brief
from neurodb.review import services as review_services
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


def test_the_activityinfo_projection_draws_no_month_after_its_last_as_reported():
    """R1 parity-1: the chart draws months 1 to ``actual_months`` (October) as reported. September and October
    hold ActivityInfo's total to date (August's), never a projected value; the projection starts in November, at
    the pace of its last reported months (June to August), and the headline is the series' October value."""
    now = _impact(fake_data(), ai=True, etools=(50,) * 12, activityinfo=TO_AUGUST)
    builder = _builder(now, _impact(fake_data(), ai=False), OCTOBER)
    projection = builder._pace()["projection"]
    series = projection["series"]["ActivityInfo"]
    shown = projection["actual_months"]
    assert shown == 10
    assert series[:8] == [150 * month for month in range(1, 9)]
    assert series[8:shown] == [1200, 1200]  # September, October: the total to date
    assert series[shown:] == [1350, 1500]  # November, December: 150 a month, June to August's pace
    assert builder._children_tile()["value"] == series[shown - 1] == 1200


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
    line never says "No 2026 data"); a year that has not started says so."""
    now = _impact(fake_data(), ai=False, etools=(0,) * 12, pds=(0, 0))
    now["impact"]["not_yet"] = True
    before = _impact(fake_data(), ai=False, etools=(120,) + (0,) * 11, pds=(1, 0))
    headline = _builder(now, before, datetime.date(2027, 1, 5), year=2027)._headline()
    assert headline["period"] == "January 2027"  # never "January to Jan 2027"
    assert headline["source"].startswith(
        "The overview's figures for this year, against the same months of 2026"
    )
    empty = _impact(fake_data(), ai=False, etools=(0,) * 12, pds=(0, 0))
    future = _builder(empty, before, OCTOBER, year=2027)._headline()["source"]
    assert future.startswith(
        "The overview's figures for 2027, which has not started: no change against 2026."
    )


def test_a_scope_without_etools_reports_this_year_is_not_a_drop_of_100_percent():
    now = _impact(fake_data(), ai=False, etools=(0,) * 12, pds=(0, 0))
    tile = _tile(now, _impact(fake_data(), ai=False, etools=(100,) * 12), OCTOBER)
    assert tile["value"] == 0 and tile["delta"] is None
    assert tile["hint"].endswith("Not compared: 2026 has no eTools partner reports yet.")


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
