import pytest

from neurodb.datamart.tags import Tags, tags_of


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("# of girls (12-17) with disabilities reached with PSS", Tags("Girls", "Adolescents", "", "Yes")),
        ("Number of Syrian boys under 5 receiving vaccines", Tags("Boys", "Under 5", "Syrian", "")),
        ("# of caregivers attending parenting sessions", Tags("", "Caregivers", "", "")),
        ("# of Lebanese and Palestinian youth (15-24) trained", Tags("", "Youth", "Palestinian", "")),
        ("# of women and men reached with cash", Tags("Women", "Adults", "", "")),
        ("# of schools rehabilitated", Tags()),
        ("", Tags()),
        (None, Tags()),
    ],
)
def test_tags_from_titles(title, expected):
    assert tags_of(title) == expected


def test_tags_as_dict_keys():
    assert list(tags_of("x").as_dict()) == ["gender", "age_group", "nationality", "disability"]
