import pytest

from neurodb.integrations.activityinfo.tags import NATIONALITIES, map_tags, parse_gender, parse_tags

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("1.1_SYR_Female_Under 5: # of children", "<5"),
        ("2.1_LEB_male_15-17 years", "15-17"),
        ("Children 3-5 years (3-5)", "3-5"),
        ("Adolescents 12-17 reached", "12-17"),
        ("Adults 18+ counselled", ">=18"),
        ("Caregivers of children 0-23 months", "0-23 months"),
        ("Women 25-49 screened", "25-49"),
        ("Elderly Above 60", ">60"),
        ("People above 14", ">14"),
        ("Youth 18-24 trained", "18-24"),
        ("Children 6-59 months supplemented", "6-59 months"),
        ("Children U18 enrolled", "<18"),
        ("Kids 0-59 months", "0-59 months"),
        ("Individuals reached", None),
    ],
)
def test_age_group_spellings(name, expected):
    assert parse_tags(name)["age_group"] == expected


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("1.1_SYR_Female", "SYR"),
        ("1.1_LEB_Male", "LEB"),
        ("Number of Lebanese children", "LEB"),
        ("1.1_PRS_x", "PRS"),
        ("1.1_PRL_x", "PRL"),
        ("1.1_OTH_x", "OTH"),
        ("1.1_MIG_x", "OTH"),
        ("Non-Lebanese adults", "NONLEB"),
        ("1.1_non_leb_syr", "NONLEB"),
        ("Total individuals", None),
    ],
)
def test_nationality_spellings(name, expected):
    assert map_tags(name.lower(), NATIONALITIES) == expected


@pytest.mark.parametrize(
    ("name", "gender"),
    [("x_Male", "Male"), ("x_FEMALE", "Female"), ("x_male_female", "Male"), ("male female", None)],
)
def test_gender(name, gender):
    assert parse_gender(name) == gender


@pytest.mark.parametrize(
    ("name", "programme"),
    [("3.1_BLN_x", "BLN"), ("3.1_ALP_x", "ALP"), ("3.1_CBECE_x", "CBECE"), ("3.1_bln_x", None)],
)
def test_programme_is_case_sensitive_like_v2(name, programme):
    assert parse_tags(name)["programme"] == programme


@pytest.mark.parametrize(
    ("name", "disability"),
    [
        ("x_Speaking", "Speaking"),
        ("x_intellectual", "Intellectual"),
        ("x_Audio", "Audio"),
        ("x_visual", "Visual"),
        ("x_motor", "Motor"),
        ("x_Mobility", "Motor"),
        ("x_none", None),
    ],
)
def test_disability(name, disability):
    assert parse_tags(name)["disability"] == disability


def test_parse_tags_full_name():
    tags = parse_tags("7.2.4.e_SYR_Female_Under 5_visual: # of individuals reached")
    assert tags == {
        "gender": "Female",
        "nationality": "SYR",
        "disability": "Visual",
        "programme": None,
        "age_group": "<5",
    }
