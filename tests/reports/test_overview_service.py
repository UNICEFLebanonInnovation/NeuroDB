"""The country overview's data, counted by hand on ``overview_fixture`` + the ActivityInfo hierarchy."""

import datetime
from decimal import Decimal

import pytest

from neurodb.datamart import models as dm
from neurodb.datamart.models import IndicatorFlag
from neurodb.geo.models import Location
from neurodb.reports import overview
from tests.reports.overview_fixture import TODAY, make_overview_data

pytestmark = pytest.mark.django_db


@pytest.fixture
def data(hierarchy):
    return make_overview_data()


def build(reporting_year, **scope):
    return overview.build(
        overview.Scope(year=2026, reporting_year=reporting_year, today=TODAY, **scope), cache=False
    )


def test_impact_counts_children_from_both_sources_apart(data, reporting_year):
    impact = build(reporting_year)["impact"]
    assert (impact["children_etools"], impact["children_activityinfo"], impact["children_reached"]) == (
        500,
        500,
        500,  # the larger of the two sources, never their sum
    )
    assert impact["monthly"]["etools"][2:6] == [300, 0, 0, 200]
    assert impact["monthly"]["activityinfo"][:2] == [150, 350]
    by_gov = {g["name"]: g for g in impact["by_governorate"]}
    assert (by_gov["Akkar"]["etools"], by_gov["Akkar"]["activityinfo"], by_gov["Akkar"]["reached"]) == (
        300,
        350,
        350,
    )
    assert (by_gov["Akkar"]["population"], by_gov["Akkar"]["coverage"]) == (10000, 3.5)
    assert (by_gov["Beirut"]["reached"], by_gov["Beirut"]["population"], by_gov["Beirut"]["coverage"]) == (
        200,
        4000,
        5.0,
    )
    assert by_gov["Akkar"]["level"] == 6 and impact["population_year"] == 2025
    (section,) = impact["by_section"]  # the teachers indicator does not count children
    assert (section["section"], section["achieved"], section["target"], section["percent"]) == (
        "Child Protection",
        500,
        1000,
        50.0,
    )
    assert 49 < section["elapsed"] < 50


def test_money_blocks(data, reporting_year):
    money = build(reporting_year)["money"]
    assert (money["reserved"], money["disbursed"], money["outstanding"], money["disbursed_percent"]) == (
        10000,
        4000,
        6000,
        40.0,
    )
    assert money["cost_per_child"] == 8.0
    (section,) = money["by_section"]
    assert (
        section["children"],
        section["cost_per_child"],
        section["achieved_percent"],
        section["ahead"],
    ) == (
        500,
        8.0,
        50.0,
        True,
    )
    assert money["by_donor"] == [["EU", 10000.0]]
    assert money["decisions"] == []  # ends in December, half the period elapsed, 40 % disbursed


def test_delivery_and_assurance(data, reporting_year):
    delivery = build(reporting_year)["delivery"]
    assert delivery["status_counts"]["on_track"] == 1 and delivery["status_counts"]["not_reported"] == 1
    assert (delivery["programme_documents"], delivery["partners"], delivery["partners_cso"]) == (1, 1, 1)
    assert delivery["assurance"] == {
        "field_monitoring_visits": 1,
        "tpm_visits": 2,
        "tpm_visits_planned": 2,
        "tpm_visits_completed": 1,  # the approved one; the other has no report yet
        "open_action_points": 1,
        "overdue_high_priority": 1,
        "high_risk_partners": 1,
        "partners_with_pd": 1,
    }
    assert delivery["findings_by_rating"] == [["Off Track", 1]]
    severities = [a["severity"] for a in delivery["attention"]]
    titles = " | ".join(a["title"] for a in delivery["attention"])
    assert severities[0] == "critical" and "high-priority action point past due" in titles
    assert "1 indicator never reported" in titles


def test_progress_of_tpm_visits_and_action_points(data, reporting_year):
    progress = build(reporting_year)["progress"]
    tpm = progress["tpm"]
    assert (tpm["planned"][1], tpm["planned"][2], tpm["completed"][1], tpm["overdue"][2]) == (1, 1, 1, 1)
    assert (
        tpm["planned_total"],
        tpm["completed_total"],
        tpm["completion_percent"],
        tpm["sites_visited"],
    ) == (
        2,
        1,
        50.0,
        1,
    )
    points = progress["action_points"]
    assert points["due"][1] == 1 and points["due"][4] == 1 and points["closed"][1] == 1
    assert points["past_due"][:8] == [0, 0, 0, 0, 1, 1, 1, 0]  # July as of today, August not yet
    assert points["closure_percent"] == 50.0  # of the two points due in 2026, one is closed
    assert (points["open_total"], points["high_priority_open"], points["median_days_to_close"]) == (1, 1, 10)
    assert points["age"] == [{"module": "TPM", "under_30": 0, "d30_90": 1, "over_90": 0, "high_priority": 1}]


def test_governorate_filter(data, reporting_year):
    result = build(reporting_year, governorate="Akkar")
    impact = result["impact"]
    assert (impact["children_etools"], impact["children_activityinfo"]) == (300, 350)
    assert [g["name"] for g in impact["by_governorate"]] == ["Akkar"]
    assert result["delivery"]["indicators"] == 1  # the teachers indicator is planned in Beirut


def test_section_filter_reaches_both_sources(data, reporting_year):
    assert build(reporting_year, sections=["Child Protection"])["impact"]["children_reached"] == 500
    other = build(reporting_year, sections=["Education"])
    assert other["impact"]["children_reached"] == 0
    assert other["money"]["reserved"] == 0 and other["delivery"]["indicators"] == 0


def test_flags_override_the_children_rule(data, reporting_year, hierarchy):
    IndicatorFlag.objects.create(source="etools", key="100", counts_children=False)
    IndicatorFlag.objects.create(
        source="activityinfo", key=str(hierarchy["master"].id), counts_children=False
    )
    impact = build(reporting_year)["impact"]
    assert impact["children_reached"] == 0 and impact["by_section"] == []


def test_empty_database_gives_zeros(db, reporting_year):
    result = build(reporting_year)
    assert result["impact"]["children_reached"] == 0 and result["money"]["cost_per_child"] is None
    assert result["delivery"]["status_counts"] == dict.fromkeys(overview.STATUSES, 0)
    assert result["progress"]["tpm"]["completion_percent"] is None


def test_no_reporting_year_still_reads_etools(data):
    assert build(None)["impact"]["children_etools"] == 500


def test_query_count_is_bounded(data, reporting_year, django_assert_max_num_queries):
    with django_assert_max_num_queries(60):
        build(reporting_year)


def test_result_is_cached_per_scope(data, reporting_year, settings, django_assert_max_num_queries):
    settings.DEBUG = False
    scope = overview.Scope(year=2026, reporting_year=reporting_year, today=TODAY)
    overview.build(scope)
    with django_assert_max_num_queries(2):  # the flags fingerprint and the last finished sync run
        overview.build(scope)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("Bekaa", "Beqaa"),
        ("Baalbek - El Hermel", "Baalbek-Hermel"),
        ("Nabatieh Governorate", "El Nabatieh"),
        ("Mount Lebanon", "mount-lebanon"),
        ("North Lebanon", "North"),
        ("South", "South Lebanon"),
        ("Akkar", "AKKAR"),
        ("Beirut", "Beirut Governorate"),
    ],
)
def test_governorate_names_match_across_sources(a, b):
    assert overview.governorate_key(a) == overview.governorate_key(b)


def test_options_list_sections_and_governorates(data):
    options = overview.options(2026)
    assert options == {"sections": ["Child Protection"], "governorates": ["Akkar", "Beirut"]}


def test_governorate_months_and_money_follow_the_locations_there(data, reporting_year):
    result = build(reporting_year, governorate="Beirut")
    assert result["impact"]["children_etools"] == 200
    assert (
        sum(result["impact"]["monthly"]["etools"]) == 200
    )  # the Beirut report only, not the national months
    money = result["money"]
    assert (money["disbursed"], money["cost_per_child"]) == (1600.0, 8.0)  # 200 of the PD's 500 children


def test_ended_programme_documents_still_count_for_their_year(data, reporting_year):
    data["pd"].status = "ended"
    data["pd"].save()
    result = build(reporting_year)
    assert result["impact"]["children_etools"] == 500 and result["money"]["reserved"] == 10000
    assert result["money"]["decisions"] == []  # only active PDs need a decision


def test_indicators_that_take_the_maximum_across_locations_are_not_split_by_governorate(data, reporting_year):
    from neurodb.datamart.models import ReportedIndicator

    ReportedIndicator.objects.update(calculation_across_locations="max")
    result = build(reporting_year)
    assert all(g["etools"] == 0 for g in result["impact"]["by_governorate"])
    assert build(reporting_year, governorate="Akkar")["impact"]["children_etools"] == 0


def test_a_new_flag_shows_at_once_despite_the_cache(data, reporting_year, settings):
    settings.DEBUG = False
    scope = overview.Scope(year=2026, reporting_year=reporting_year, today=TODAY)
    assert overview.build(scope)["impact"]["children_etools"] == 500
    IndicatorFlag.objects.create(source="etools", key="100", counts_children=False)
    assert overview.build(scope)["impact"]["children_etools"] == 0


def test_tpm_visits_on_the_same_days_are_all_counted(data, reporting_year):
    from neurodb.datamart.models import TPMVisit

    twin = TPMVisit.objects.get(reference_number="TPM/1")
    twin.pk, twin.datamart_id, twin.reference_number = None, 99, "TPM/1-bis"
    twin.save()
    assert build(reporting_year)["progress"]["tpm"]["planned"][1] == 2


def test_tpm_tile_counts_every_visit_like_the_field_monitoring_page(data, reporting_year):
    from django.http import QueryDict

    from neurodb.datamart import services
    from neurodb.datamart.models import TPMVisit

    TPMVisit.objects.create(
        datamart_id=3, partner=data["partner"], reference_number="TPM/3", status="draft",
        start_date=datetime.date(2026, 4, 1),
    )  # fmt: skip
    result = build(reporting_year)
    assurance = result["delivery"]["assurance"]
    assert (assurance["tpm_visits"], assurance["tpm_visits_planned"]) == (3, 2)
    assert assurance["tpm_visits"] == services.monitoring(QueryDict("year=2026"))["tpm_count"]
    assert result["progress"]["tpm"]["planned_total"] == 2  # the progress chart stays on planned visits


def test_funding_by_donor_is_pro_rated_with_a_governorate(data, reporting_year):
    money = build(reporting_year, governorate="Beirut")["money"]
    assert money["reserved"] == 4000.0  # 200 of the PD's 500 children
    assert money["by_donor"] == [["EU", 4000.0]]  # the same share, not the country amount


def _children_indicator_in_beirut(data, source_id=300, value=500):
    """A second children indicator of the PD, planned and reported in Beirut only."""
    pd, place = data["pd"], Location.objects.get(name="Achrafieh")
    dm.PDIndicator.objects.create(
        datamart_id=source_id, source_id=source_id, intervention=pd, pd_reference_number=pd.number,
        section_name="Child Protection", title="# of girls reached with learning support",
        display_type="number", unit="number", baseline_numerator=Decimal(0),
        target_numerator=Decimal(1000), location=place, location_name=place.name, location_pcode=place.p_code,
        tag_age_group="Children",
    )  # fmt: skip
    dm.ReportedIndicator.objects.create(
        datamart_id=source_id, partner=data["partner"], intervention=pd, partner_name="Amel Association",
        pd_reference_number=pd.number, progress_report="PR-9", report_number="QPR9", report_type="QPR",
        report_status="Accepted", period_start=datetime.date(2026, 4, 1), period_end=datetime.date(2026, 6, 30),
        due_date=datetime.date(2026, 7, 15), submission_date=datetime.date(2026, 7, 10),
        indicator="# of girls reached with learning support", target="1000", location=place.name,
        p_code=place.p_code, location_ref=place, achievement_in_period=str(value),
        total_cumulative_progress=str(value), total_cumulative_progress_in_location=str(value),
        calculation_across_locations="sum", etools_indicator_id=str(source_id),
    )  # fmt: skip


def test_governorate_share_of_a_pd_counts_all_its_children_indicators(data, reporting_year):
    _children_indicator_in_beirut(data)
    akkar = build(reporting_year, governorate="Akkar")["money"]
    beirut = build(reporting_year, governorate="Beirut")["money"]
    # Akkar: 300 of the PD's 1,000 children (not of the 500 of the one indicator located there).
    assert (akkar["reserved"], beirut["reserved"]) == (3000.0, 7000.0)
    assert akkar["reserved"] + beirut["reserved"] == build(reporting_year)["money"]["reserved"]


def test_attention_keeps_overdue_action_points_and_says_what_it_left_out(data, reporting_year, monkeypatch):
    dm.PDIndicator.objects.filter(source_id=100).update(target_numerator=Decimal(100000))  # now off track
    monkeypatch.setattr(overview, "MAX_ATTENTION", 2)
    delivery = build(reporting_year)["delivery"]
    kinds = [a["kind"] for a in delivery["attention"]]
    assert kinds == ["action_points", "not_reported"]  # the off-track PD alone would have taken a slot
    more = delivery["attention_more"]
    assert more["count"] == 1 and "status=off_track" in more["url"]
    monkeypatch.setattr(overview, "MAX_ATTENTION", 8)
    delivery = build(reporting_year)["delivery"]
    assert len(delivery["attention"]) == 3 and delivery["attention_more"]["count"] == 0


def test_overdue_action_points_of_several_sections_are_one_item(data, reporting_year):
    point = dm.ActionPoint.objects.get(reference_number="AP/1")
    point.pk, point.datamart_id, point.reference_number, point.section = None, 3, "AP/3", ""
    point.save()
    items = [a for a in build(reporting_year)["delivery"]["attention"] if a["kind"] == "action_points"]
    assert [(a["title"], a["section"]) for a in items] == [("2 high-priority action points past due", "")]
    assert "Child Protection 1" in items[0]["detail"] and "No section 1" in items[0]["detail"]


def test_attention_links_leave_a_blank_section_out(data, reporting_year):
    dm.PDIndicator.objects.update(section_name="")
    (item,) = [a for a in build(reporting_year)["delivery"]["attention"] if a["kind"] == "not_reported"]
    assert item["section"] == "Other" and "section=" not in item["url"]  # partner monitoring has no "Other"


def test_closed_this_year_leaves_out_completion_dates_after_today(data, reporting_year):
    dm.ActionPoint.objects.filter(reference_number="AP/1").update(
        status="completed", date_of_completion=datetime.datetime(2026, 8, 5, 10, 0, tzinfo=datetime.UTC)
    )
    points = build(reporting_year)["progress"]["action_points"]
    assert points["closed_total"] == 1 and points["closed"][7] == 0  # AP/2 only; August is still to come
    assert points["median_days_to_close"] == 10


def test_a_finished_sync_run_changes_the_cache_key(data, reporting_year, settings):
    from django.utils import timezone

    from neurodb.core.models import SyncRun

    settings.DEBUG = False
    scope = overview.Scope(year=2026, reporting_year=reporting_year, today=TODAY)
    assert overview.build(scope)["money"]["reserved"] == 10000
    dm.FundsReservationHeader.objects.update(total_amt=Decimal(12000))
    assert overview.build(scope)["money"]["reserved"] == 10000  # cached
    SyncRun.objects.create(job=SyncRun.Job.ETOOLS_DATAMART, target="all", finished_at=timezone.now())
    assert overview.build(scope)["money"]["reserved"] == 12000  # every worker's key changed with the sync
