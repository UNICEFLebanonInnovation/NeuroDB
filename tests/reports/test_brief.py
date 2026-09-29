"""The management brief: its service on the hand-counted overview fixture, its page, the two
hand-entered tables (section plans, finding assignments) and their admin pages."""

import datetime
import json

import pytest
from django.core.cache import cache
from django.urls import reverse

from neurodb.core.models import PopulationFigure, SyncRun
from neurodb.datamart import models as dm
from neurodb.geo.models import Location, LocationType
from neurodb.partnerships.models import PartnerLink
from neurodb.reports import brief
from neurodb.reports.models import SectionPlan
from neurodb.review import services as review_services
from neurodb.review.models import FindingAssignment, ReviewFinding
from tests.reports.overview_fixture import TODAY, make_overview_data

pytestmark = pytest.mark.django_db


@pytest.fixture
def data(hierarchy, settings):
    settings.AI_ASSISTANT_ENABLED = False
    made = make_overview_data()
    # A district between the governorate and the cadaster, and its children, for the gap table.
    district_type = LocationType.objects.create(name="District", admin_level=2)
    district = Location.objects.create(
        id=101, name="Akkar District", p_code="LB11", type=district_type, parent=made["akkar"],
        lft=1, rght=2, level=0, tree_id=1,
    )  # fmt: skip
    made["halba"].parent = district
    made["halba"].save()
    PopulationFigure.objects.create(
        year=2025, category="children", level="district", nationality="ALL", area_name="Akkar District",
        area_code="LB11", parent_name="Akkar", value=3000,
    )  # fmt: skip
    # The plan, a grant behind the funds reservation, and the ActivityInfo link of the partner.
    SectionPlan.objects.create(
        year=2026, section="Child Protection", children_target=2000, required_usd=20000
    )
    dm.FundsReservation.objects.update(grant_number="SC1")
    dm.Grant.objects.create(datamart_id=1, name="SC1", donor="EU", expiry=TODAY + datetime.timedelta(days=30))
    PartnerLink.objects.create(label="Partner A", partner=made["partner"], method="manual")
    return made


def build(reporting_year, **scope):
    return brief.build(
        brief.Scope(year=2026, reporting_year=reporting_year, today=TODAY, **scope), cache=False
    )


def test_headline_tiles_are_the_overview_figures_with_their_comparisons(data, reporting_year):
    tiles = {t["key"]: t for t in build(reporting_year)["headline"]["tiles"]}
    assert tiles["children"]["value"] == 500 and tiles["children"]["delta"] is None  # no last year
    achievement = tiles["achievement"]
    assert achievement["value"] == 50.0 and achievement["delta"]["unit"] == "pts"
    assert 0 < achievement["delta"]["value"] < 1 and achievement["delta"]["good"]  # 50 % vs 49.7 % elapsed
    assert (tiles["disbursed"]["value"], tiles["disbursed"]["delta"]["value"]) == (4000, 40.0)
    assert tiles["cost"]["value"] == 8.0 and tiles["cost"]["delta"] is None
    assert tiles["on_track"]["value"] == 100.0 and tiles["on_track"]["delta"] is None  # no review 30 days ago


def test_pace_bullets_carry_the_cp_target_and_the_projection_extends_the_pace(data, reporting_year):
    pace = build(reporting_year)["pace"]
    (bullet,) = pace["bullets"]
    assert (bullet["section"], bullet["achieved"], bullet["target"], bullet["cp_target"]) == (
        "Child Protection",
        500,
        1000,
        2000,
    )
    assert 490 <= bullet["expected"] <= 500 and pace["has_cp_targets"]
    projection = pace["projection"]
    assert projection["actual_months"] == 7 and projection["target"] == 1000
    etools = projection["series"]["eTools"]
    assert etools[:7] == [0, 0, 300, 300, 300, 500, 500]  # cumulative to July
    assert etools[7] == 567 and etools[11] == 833  # + the average of May, June, July (67) per month
    assert projection["series"]["ActivityInfo"][:2] == [150, 500]


def test_confidence_per_section(data, reporting_year):
    rows = build(reporting_year)["confidence"]["rows"]
    (row,) = rows
    assert row["section"] == "Child Protection" and row["indicators"] == 2
    assert row["reported_percent"] == 50.0  # the teachers indicator never reported
    assert row["verified_percent"] == 100.0  # TPM/1 was approved on the PD
    assert row["linked_percent"] == 100.0  # Partner A → Amel
    assert row["sync_days"] is None and row["level"] == "low"  # reported and sync short
    SyncRun.objects.create(
        job=SyncRun.Job.ETOOLS_DATAMART,
        status=SyncRun.Status.SUCCEEDED,
        finished_at=datetime.datetime.now(datetime.UTC),
    )
    (row,) = build(reporting_year)["confidence"]["rows"]
    assert row["sync_days"] == 0 and row["level"] == "medium"


def test_the_two_sources_side_by_side_per_partner(data, reporting_year):
    (row,) = build(reporting_year)["confidence"]["reconcile"]
    assert (row["partner"], row["activityinfo"], row["etools"]) == ("Amel Association", 150, 500)
    assert row["gap_percent"] == 70.0 and not row["consistent"]


def test_timeliness_marks_each_quarter(data, reporting_year):
    timeliness = build(reporting_year)["confidence"]["timeliness"]
    (row,) = timeliness["rows"]
    assert row["cells"] == ["on_time", "on_time", "", ""] and row["late"] == 0
    assert timeliness["counts"] == {"on_time": 2, "late": 0, "missing": 0, "not_due": 0}
    dm.ReportedIndicator.objects.filter(progress_report="PR-2").update(
        submission_date=None, report_status="Due", due_date=TODAY - datetime.timedelta(days=10)
    )
    (row,) = build(reporting_year)["confidence"]["timeliness"]["rows"]
    assert row["cells"][1] == "missing" and row["late"] == 1  # due ten days ago, nothing submitted


def test_who_and_where(data, reporting_year):
    who = build(reporting_year)["who"]
    assert who["sex_age"]["labels"] == ["Children"] and who["sex_age"]["series"]["Not named"] == [500]
    assert who["children_tagged"] == 500 and who["nationality_named"] == 0
    assert [n["name"] for n in who["nationality"]] == ["Lebanese", "Syrian", "Palestinian"]
    assert who["disability"] == {"reached": 0, "share": 0.0}
    assert [g["name"] for g in who["governorates"]] == ["Akkar", "Beirut"]
    (district,) = who["districts"]
    assert (district["district"], district["governorate"], district["children"], district["reached"]) == (
        "Akkar District",
        "Akkar",
        3000,
        300,
    )
    assert (district["coverage"], district["not_reached"]) == (10.0, 2700)


def test_partner_scorecard_and_quadrant(data, reporting_year):
    partners = build(reporting_year)["partners"]
    (row,) = partners["scorecard"]
    assert (row["partner"], row["pds"], row["indicators"], row["reserved"]) == (
        "Amel Association",
        1,
        2,
        10000,
    )
    assert row["on_track_percent"] == 100.0 and row["reports_on_time_percent"] == 100.0
    assert row["points_on_time_percent"] == 0.0  # AP/1 open past due, AP/2 closed ten days late
    assert row["risk"] == "High" and row["high_risk"] and row["finding"] == "Off Track"
    assert (row["disbursed_percent"], row["achieved_percent"], row["quadrant"]) == (40.0, 50.0, "ahead")
    assert row["children"] == 500
    (point,) = partners["quadrant"]
    assert (point["x"], point["y"], point["size"], point["quadrant"]) == (40.0, 50.0, 10000, "ahead")
    assert partners["decisions"] == []


def test_money_flows_grants_and_requirements(data, reporting_year):
    money = build(reporting_year)["money"]
    assert money["flows"]["donors"] == [{"name": "EU", "amount": 10000.0}]
    assert money["flows"]["links"] == [{"donor": "EU", "section": "Child Protection", "amount": 10000.0}]
    assert money["flows"]["sections"] == [{"name": "Child Protection", "children": 500, "reserved": 10000.0}]
    (grant,) = money["grants"]
    assert (grant["grant"], grant["donor"], grant["unspent"], grant["days"], grant["at_risk"]) == (
        "SC1",
        "EU",
        6000.0,
        30,
        True,
    )
    (funded,) = money["funded"]
    assert (funded["required"], funded["reserved"], funded["percent"]) == (20000.0, 10000.0, 50.0)
    assert money["has_requirements"]


def test_decisions_and_action_read_the_review_and_its_assignments(data, reporting_year):
    review = review_services.run(date=TODAY, today=TODAY)
    overdue = review.findings.get(check_id="action_points_overdue")
    FindingAssignment.objects.create(
        key=overdue.key, title=overdue.title, section=overdue.section, owner="Chief of Child Protection",
        due_date=TODAY - datetime.timedelta(days=1), status=FindingAssignment.Status.ASSIGNED,
    )  # fmt: skip
    result = build(reporting_year)
    assert result["headline"]["decide"]["mode"] == "rules"  # the assistant is off in tests
    decide = result["headline"]["decide"]["items"]
    assert 1 <= len(decide) <= brief.MAX_DECISIONS and decide[0]["key"] == overdue.key
    assert (decide[0]["owner"], decide[0]["status"], decide[0]["status_label"]) == (
        "Chief of Child Protection",
        "assigned",
        "Assigned",
    )
    assert decide[0]["assign_url"].startswith(reverse("admin:index")) and decide[1]["status"] == "raised"
    assert "key=" in decide[1]["assign_url"]  # not assigned yet: the add form, prefilled
    action = result["action"]
    assert action["lifecycle"]["raised"] == review.findings.count()
    assert (
        action["lifecycle"]["acknowledged"],
        action["lifecycle"]["assigned"],
        action["lifecycle"]["closed"],
    ) == (
        1,
        1,
        0,
    )
    assert action["lifecycle"]["median_days"]["assigned"] == 0
    (owner,) = action["owners"]
    assert (owner["owner"], owner["open"], owner["past_due"]) == ("Chief of Child Protection", 1, 1)
    assert action["digest"]["reviews"] == 1 and action["digest"]["last"] == TODAY.isoformat()
    assert result["lineage"]["review"]["findings"] == review.findings.count()
    assert "Owner: Chief of Child Protection" in result["text"]


def test_a_finding_the_review_sees_resolved_counts_as_closed(data, reporting_year):
    review_services.run(date=TODAY, today=TODAY)
    dm.ActionPoint.objects.filter(reference_number="AP/1").update(status="completed")
    later = TODAY + datetime.timedelta(days=1)
    second = review_services.run(date=later, today=later)
    assert second.findings.filter(state=ReviewFinding.State.RESOLVED).exists()
    result = brief.build(brief.Scope(year=2026, reporting_year=reporting_year, today=later), cache=False)
    assert result["action"]["lifecycle"]["closed"] >= 1


def test_the_brief_as_text_carries_the_figures_on_screen(data, reporting_year):
    text = build(reporting_year)["text"]
    assert "Children reached, at least: 500." in text
    assert "Achievement of PD targets: 50%" in text and "Funds: $4k disbursed (40% of $10k reserved)." in text
    assert "Cost per child: $8." in text and "Indicators on track: 100%" in text
    assert "Grants expiring within 90 days with an unspent balance: SC1 (EU, $6k)." in text
    assert "Low confidence in the figures of: Child Protection." in text
    assert text.endswith("rules v1.")


def test_section_filter_and_empty_database(data, reporting_year, db):
    other = build(reporting_year, sections=["Education"])
    assert other["headline"]["tiles"][0]["value"] == 0 and other["confidence"]["rows"] == []
    assert other["partners"]["scorecard"] == [] and other["money"]["flows"]["links"] == []


def test_empty_database_gives_zeros(db, reporting_year):
    result = build(reporting_year)
    assert result["headline"]["tiles"][0]["value"] == 0
    assert result["headline"]["decide"] == {
        "mode": "none",
        "items": [],
        "review_date": None,
        "decided_by": "",
    }
    assert result["pace"]["projection"]["series"]["eTools"] == []
    assert result["confidence"]["rows"] == [] and result["who"]["sex_age"] == {}
    assert result["money"]["grants"] == [] and result["action"]["digest"] == {}
    assert result["lineage"]["review"] is None and "no sync yet" in result["text"]


def test_result_is_cached_and_a_new_plan_shows_at_once(
    data, reporting_year, settings, django_assert_max_num_queries
):
    settings.DEBUG = False
    scope = brief.Scope(year=2026, reporting_year=reporting_year, today=TODAY)
    assert brief.build(scope)["pace"]["bullets"][0]["cp_target"] == 2000
    with django_assert_max_num_queries(7):  # the fingerprints only
        brief.build(scope)
    SectionPlan.objects.filter(section="Child Protection").update(children_target=3000)
    SectionPlan.objects.get(section="Child Protection").save()  # a real save bumps updated_at
    assert brief.build(scope)["pace"]["bullets"][0]["cp_target"] == 3000


def test_query_count_is_bounded(data, reporting_year, django_assert_max_num_queries):
    with django_assert_max_num_queries(150):
        build(reporting_year)


# ------------------------------------------------------------------------------- the page
def test_page_renders_every_block(client_viewer, data, reporting_year):
    page = client_viewer.get(reverse("reports:brief"))
    assert page.status_code == 200
    html = page.text
    for text in (
        "Management brief",
        "Country management brief",
        "Things to decide",
        "Sections at a glance",
        "Copy summary",
        "Are we ahead or behind?",
        "Confidence in the figures",
        "Where the two sources disagree",
        "Reporting timeliness",
        "Districts with high need and low coverage",
        "Partner scorecard",
        "From donor to section to children reached",
        "Grants at risk",
        "Finding lifecycle",
        "Lineage of this brief",
        "Data dictionary",
    ):
        assert text in html, text
    assert "Akkar District" in html and "Amel Association" in html and 'id="brief-chart-data"' in html
    start = html.index('id="brief-chart-data"')
    charts = json.loads(html[html.index(">", start) + 1 : html.index("</script>", start)])
    assert charts["bullets"][0]["cp_target"] == 2000
    assert charts["projection"]["actual_months"] == datetime.date.today().month  # the page reads today
    assert (
        charts["quadrant"][0]["name"] == "Amel Association" and charts["flows"]["donors"][0]["name"] == "EU"
    )
    assert (charts["grants"][0]["label"], charts["grants"][0]["value"]) == ("EU · SC1", 6000.0)
    assert charts["funded"][0]["percent"] == 50.0 and charts["lifecycle"] == []
    assert (
        "Assign" not in html.split("Things to decide")[1].split("Sections at a glance")[0]
    )  # viewers cannot
    # The latest finding pill takes the rating's colour, as on the field monitoring page.
    assert '<span class="pill pill--danger pill--sm" data-status="off_track">' in html


def test_page_filters_and_staff_links(client, admin_user, data, reporting_year):
    client.force_login(admin_user)
    page = client.get(reverse("reports:brief") + "?section=Education&year=2026")
    assert page.status_code == 200 and page.context["selected"]["section"] == ["Education"]
    assert "Reporting year 2026 · Education" in page.text
    review_services.run(date=TODAY, today=TODAY)
    page = client.get(reverse("reports:brief"))
    assert reverse("admin:review_findingassignment_add") in page.text  # staff see the Assign links


def test_page_without_a_reporting_year(client_viewer, db):
    page = client_viewer.get(reverse("reports:brief"))
    assert page.status_code == 200 and "No reporting year is configured" in page.text


def test_sidebar_and_landing_name_the_brief(client_viewer, reporting_year):
    page = client_viewer.get(reverse("reports:overview"))
    assert reverse("reports:brief") in page.text


# ------------------------------------------------------------------------------- the tables
def test_assignment_status_stamps_the_lifecycle_dates(db):
    a = FindingAssignment.objects.create(key="k", status=FindingAssignment.Status.ACKNOWLEDGED)
    assert a.acknowledged_at and a.assigned_at is None and a.closed_at is None
    a.status = FindingAssignment.Status.CLOSED
    a.save()
    assert a.assigned_at and a.closed_at
    a.status = FindingAssignment.Status.ASSIGNED
    a.save()
    assert a.closed_at is None and a.assigned_at  # reopened: the closing date is cleared, the rest kept


def test_admin_pages_of_the_two_tables(client, admin_user, db):
    admin_user.is_superuser = True
    admin_user.save()
    client.force_login(admin_user)
    add = reverse("admin:reports_sectionplan_add")
    assert client.get(add).status_code == 200
    response = client.post(
        add,
        {
            "year": 2026,
            "section": "Education",
            "children_target": 50000,
            "required_usd": "1000000.00",
            "note": "CP",
        },
    )
    assert response.status_code == 302
    plan = SectionPlan.objects.get()
    assert (plan.section, plan.children_target, plan.updated_by) == ("Education", 50000, admin_user.username)
    assert client.get(reverse("admin:reports_sectionplan_changelist")).status_code == 200

    add = (
        reverse("admin:review_findingassignment_add") + "?key=reports_overdue:LEB/PD1&title=Late&section=WASH"
    )
    page = client.get(add)
    assert (
        page.status_code == 200
        and 'value="reports_overdue:LEB/PD1"' in page.text
        and 'value="Late"' in page.text
    )
    response = client.post(
        reverse("admin:review_findingassignment_add"),
        {"key": "reports_overdue:LEB/PD1", "title": "Late", "section": "WASH", "owner": "Chief of WASH",
         "due_date": "2026-08-01", "status": "assigned", "note": ""},
    )  # fmt: skip
    assert response.status_code == 302
    a = FindingAssignment.objects.get()
    assert (a.owner, a.status, a.updated_by) == ("Chief of WASH", "assigned", admin_user.username)
    assert a.assigned_at is not None
    assert client.get(reverse("admin:review_findingassignment_changelist")).status_code == 200


def test_review_admin_links_each_finding_to_its_assignment(client, admin_user, data, reporting_year):
    admin_user.is_superuser = True
    admin_user.save()
    client.force_login(admin_user)
    review = review_services.run(date=TODAY, today=TODAY)
    page = client.get(reverse("admin:review_reviewfinding_changelist"))
    assert page.status_code == 200 and ">Assign<" in page.text
    finding = review.findings.first()
    FindingAssignment.objects.create(key=finding.key, title=finding.title, owner="Deputy Representative")
    page = client.get(reverse("admin:review_dailyreview_change", args=[review.pk]))
    assert page.status_code == 200 and "Deputy Representative · Acknowledged" in page.text


def test_a_finished_sync_run_changes_the_cache_key(data, reporting_year, settings):
    from django.utils import timezone

    settings.DEBUG = False
    scope = brief.Scope(year=2026, reporting_year=reporting_year, today=TODAY)
    assert brief.build(scope)["money"]["reserved"] == 10000
    dm.FundsReservationHeader.objects.update(total_amt=12000)
    assert brief.build(scope)["money"]["reserved"] == 10000  # cached
    SyncRun.objects.create(job=SyncRun.Job.ETOOLS_DATAMART, target="all", finished_at=timezone.now())
    assert brief.build(scope)["money"]["reserved"] == 12000


def test_brief_text_rounds_and_formats_like_the_tiles():
    from django.template.defaultfilters import floatformat

    from neurodb.web.templatetags.ui import money

    tile = lambda key, value, delta=None: {"key": key, "value": value, "delta": delta, "hint": "h"}  # noqa: E731
    data = {
        "scope": {"sections": [], "today": "2026-07-01"},
        "headline": {
            "period": "January to Jul 2026",
            "decide": {"mode": "none", "items": [], "review_date": None, "decided_by": ""},
            "tiles": [
                tile("children", 1000, {"value": -2.5, "label": "vs same period last year"}),
                tile("achievement", 12.5),
                tile("disbursed", 7_000_000, {"value": 44.5, "label": "of $15.5M reserved"}),
                tile("cost", 1500.0, {"value": 12.5, "label": "vs last year's PDs"}),
                tile("on_track", 0.5),
            ],
        },
        "confidence": {"rows": []},
        "money": {"grants": [{"grant": "SC1", "donor": "EU", "unspent": 2500.0, "at_risk": True}]},
        "lineage": {"sources": [], "rules": "v1"},
    }
    text = brief.brief_text(data)
    # Halves round up, as floatformat does on the tile pills (44.5 -> 45, -2.5 -> -3, 12.5 -> 13).
    assert floatformat(44.5, "0") == "45" and floatformat(-2.5, "0") == "-3"
    assert "Children reached, at least: 1,000 (-3% vs same period last year)." in text
    assert "Achievement of PD targets: 13%." in text and "Indicators on track: 1% (h)." in text
    # Money as the money filter shows it on the same page.
    assert (
        f"Funds: {money(7_000_000)} disbursed (45% of $15.5M reserved)." in text and money(7_000_000) == "$7M"
    )
    assert "Cost per child: $1.5k (+13% vs last year's PDs)." in text
    assert "SC1 (EU, $2.5k)" in text


def test_on_track_a_month_ago_uses_the_tiles_formula_and_year(data, reporting_year):
    from neurodb.review.models import DailyReview

    counts = {"on_track": 1, "off_track": 2, "over_target": 1, "no_target": 0, "not_reported": 3}
    DailyReview.objects.create(
        date=TODAY - datetime.timedelta(days=40), status=DailyReview.Status.SUCCEEDED,
        stats={"on_track_percent": 10.0, "status_counts": {**counts, "on_track": 0}, "status_counts_year": counts},
    )  # fmt: skip
    tiles = {t["key"]: t for t in build(reporting_year)["headline"]["tiles"]}
    # (on track + over target) of the tracked, from the counts of the PDs running in the year: 50 %.
    assert tiles["on_track"]["delta"]["value"] == 100.0 - 50.0
    DailyReview.objects.update(date=datetime.date(2025, 12, 31))  # a review of another year is no reference
    tiles = {t["key"]: t for t in build(reporting_year)["headline"]["tiles"]}
    assert tiles["on_track"]["delta"] is None


def test_population_shares_name_the_year_of_their_own_figures(data, reporting_year):
    for nationality, value in (("LEB", 600), ("SYR", 400)):
        PopulationFigure.objects.create(
            year=2026, category="children", level="national", nationality=nationality, area_name="Lebanon",
            area_code="LB", value=value,
        )  # fmt: skip
    who = build(reporting_year)["who"]
    assert who["population_year"] == 2026  # the nationality shares' year, not the governorates' 2025
    assert {n["name"]: n["population_share"] for n in who["nationality"]}["Lebanese"] == 60.0


def test_prp_structured_values_are_read_not_glued(data, reporting_year):
    """PRP's {"v", "d", "c"} values once became one long number (300 -> 30011300...)."""
    for row in dm.ReportedIndicator.objects.all():
        row.achievement_in_period = (
            f"{{'c': {row.achievement_in_period}.0, 'd': 1, 'v': {row.achievement_in_period}}}"
        )
        row.total_cumulative_progress = f"{{'c': {row.total_cumulative_progress}.0, 'd': 1, 'v': 1}}"
        row.save()
    tiles = {t["key"]: t for t in build(reporting_year)["headline"]["tiles"]}
    assert tiles["children"]["value"] == 500 and tiles["achievement"]["value"] == 50.0


def test_an_impossible_children_value_is_set_aside_and_flagged(data, reporting_year):
    dm.ReportedIndicator.objects.update(total_cumulative_progress="9999999", achievement_in_period="9999999")
    result = build(reporting_year)
    tiles = {t["key"]: t for t in result["headline"]["tiles"]}
    assert tiles["children"]["hint"].startswith("eTools 0 ·")  # not 20 million children (500 is ActivityInfo)
    (aside,) = result["confidence"]["implausible"]
    assert aside["value"] == 19999998 and "child population" in aside["reason"]
    (row,) = result["confidence"]["rows"]
    assert row["level"] == "low" and row["set_aside"] == 1
    assert "Left out of children reached as reporting errors: 1 indicator" in result["text"]


def test_section_names_are_trimmed(data, reporting_year):
    dm.PDIndicator.objects.update(section_name="Child Protection ")
    result = build(reporting_year)
    assert [r["section"] for r in result["confidence"]["rows"]] == ["Child Protection"]
    assert "Child Protection ," not in result["text"]


def test_the_brief_shows_the_ai_decisions_with_their_findings(data, reporting_year):
    review = review_services.run(date=TODAY, today=TODAY, narrate=False)
    first, second = review.findings.filter(
        severity__in=["critical", "warning"], section="Child Protection"
    ).order_by("rank")[:2]
    review.decisions = [
        {
            "decision": "Agree a catch-up plan with the partner.",
            "why": "Two indicators are off track.",
            "who": "Child Protection section chief",
            "urgency": "this week",
            "finding_keys": [first.key, second.key],
        }
    ]
    review.decided_by = "test-model"
    review.save()
    result = build(reporting_year)
    decide = result["headline"]["decide"]
    assert decide["mode"] == "ai" and decide["decided_by"] == "test-model"
    (item,) = decide["items"]
    assert item["title"] == "Agree a catch-up plan with the partner." and item["who"].startswith("Child")
    assert [f["title"] for f in item["findings"]] == [first.title, second.title]
    assert "To decide (chosen by the AI daily review of" in result["text"]
    assert "Who: Child Protection section chief, this week." in result["text"]
    # a section the decision's findings are not in does not show it (a country-wide finding would)
    assert build(reporting_year, sections=["Education"])["headline"]["decide"]["items"] == []


def test_scorecard_reads_each_section_in_five_status_cells(data, reporting_year):
    card = build(reporting_year)["scorecard"]
    assert card["columns"] == ["Pace", "On track", "Spending", "Reporting", "Confidence"]
    (row,) = card["rows"]
    assert row["section"] == "Child Protection" and row["url"].endswith("?section=Child+Protection")
    cells = {c["column"]: c for c in row["cells"]}
    # 50 % of the target against 49.7 % of the time: on pace
    assert cells["Pace"]["status"] == "good" and cells["Pace"]["value"] == "50%"
    assert "of the target reached" in cells["Pace"]["detail"]
    assert all(c["status"] in ("good", "watch", "act", "none") for c in row["cells"])
    assert sum(card["counts"].values()) == 5


def test_scorecard_bands():
    assert brief._pace_cell({"percent": 30, "elapsed": 50})["status"] == "watch"  # 20 points behind
    assert brief._pace_cell({"percent": 20, "elapsed": 50})["status"] == "act"
    assert brief._spending_cell({"disbursed_percent": 80, "achieved_percent": 50})["status"] == "act"
    assert brief._spending_cell({"disbursed_percent": 60, "achieved_percent": 50})["status"] == "good"
    assert brief._on_track_cell({"on_track": 1, "over_target": 0, "off_track": 1})["status"] == "watch"
    assert brief._reporting_cell({"total": 0})["status"] == "none"
    low = {"level": "low", "reasons": ["2 reported value(s) left out as errors", "eTools is not yet synced"]}
    assert brief._confidence_cell(low)["detail"] == (
        "Low because 2 reported value(s) left out as errors; eTools is not yet synced"
    )


# ------------------------------------------------------------------------------- user review
def test_confidence_says_what_lowers_it(client_viewer, data, reporting_year):
    from django.utils import timezone

    (row,) = build(reporting_year)["confidence"]["rows"]
    assert row["level"] != "high" and "eTools is not yet synced on this server" in row["reasons"]
    assert "because" in client_viewer.get(reverse("reports:brief")).text
    SyncRun.objects.create(
        job=SyncRun.Job.ETOOLS_DATAMART,
        target="all",
        status=SyncRun.Status.SUCCEEDED,
        finished_at=timezone.now(),
    )
    (row,) = build(reporting_year)["confidence"]["rows"]
    assert not any("synced" in reason for reason in row["reasons"])


def test_who_and_where_says_why_a_block_is_empty(client_viewer, data, reporting_year):
    html = client_viewer.get(reverse("reports:brief")).text
    # No indicator title names a nationality: no column of zeros, a reason instead.
    assert "No split by nationality or disability yet" in html and 'id="brief-nationality"' not in html
    assert 'id="brief-districts"' in html  # Akkar District matches the population figures
    PopulationFigure.objects.filter(level="district").update(area_name="Qobayat")
    cache.clear()  # a population load is a sync run, which the cache key follows; here it is a bare update
    who = build(reporting_year)["who"]
    assert not who["districts_matched"] and who["districts_reported"] == ["Akkar District"]
    html = client_viewer.get(reverse("reports:brief")).text
    assert "District names do not match" in html and 'id="brief-districts"' not in html


def test_the_brief_says_when_last_year_has_nothing_to_compare(data, reporting_year):
    headline = build(reporting_year)["headline"]
    assert headline["last_year"] == {"year": 2025, "has_data": False}
    assert "No 2025 data for the same months to compare with" in headline["source"]


def test_the_digest_counts_what_is_new_after_the_first_review(data, reporting_year):
    review_services.run(date=TODAY, today=TODAY)
    later = TODAY + datetime.timedelta(days=1)
    review_services.run(date=later, today=later)
    result = brief.build(brief.Scope(year=2026, reporting_year=reporting_year, today=later), cache=False)
    digest = result["action"]["digest"]
    assert digest["reviews"] == 2 and digest["since"] == TODAY.isoformat()
    assert digest["new_critical"] == 0  # the first review is the baseline, not "new"
    assert any(line.startswith("Open critical findings:") and " on " in line for line in digest["trend"])


def test_decisions_carry_one_reason_and_no_model_name(client_viewer, data, reporting_year):
    review = review_services.run(date=TODAY, today=TODAY, narrate=False)
    first, second = review.findings.filter(severity__in=["critical", "warning"]).order_by("rank")[:2]
    summary = "The daily review found many items to look at. " * 3
    review.decisions = [
        {"decision": f"Decide {n}.", "why": summary, "who": "Child Protection section chief",
         "urgency": "today", "finding_keys": [f.key]}
        for n, f in enumerate((first, second))
    ]  # fmt: skip
    review.decided_by = "test-model"
    review.save()
    items = build(reporting_year)["headline"]["decide"]["items"]
    assert [i["detail"] for i in items] == [first.detail, second.detail]  # not the repeated summary
    html = client_viewer.get(reverse("reports:brief")).text
    assert "AI-suggested" in html and "test-model" not in html
    assert "Owner: Child Protection section chief (suggested, not assigned yet)" in html


def test_the_brief_stamp_is_a_plain_data_as_of_line(client_viewer, data, reporting_year):
    html = client_viewer.get(reverse("reports:brief")).text
    stamp = html.split('class="brief-stamp"')[1].split("</p>")[0]
    assert "Figures as of" in stamp and "rules" not in stamp and "run" not in stamp.lower().split("title=")[0]
    assert "not yet synced" in stamp and "data-filters-collapse" in html and 'class="page-purpose"' in html
