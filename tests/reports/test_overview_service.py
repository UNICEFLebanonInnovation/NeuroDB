"""The country overview's data, counted by hand on ``overview_fixture`` + the ActivityInfo hierarchy."""

import pytest

from neurodb.datamart.models import IndicatorFlag
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
    with django_assert_max_num_queries(1):  # the flags fingerprint only
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
