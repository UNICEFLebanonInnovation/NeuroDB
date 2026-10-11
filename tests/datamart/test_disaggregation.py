"""PRP disaggregations (SPEC6 R4): keys parsed, labels applied, values added up only within one
disaggregation (never across: female 5 + Male 15 + Lebanese 20 is a location total of 20, not 40), a
percentage subtotal as numerators over denominators, and values without labels left out."""

import pytest

from neurodb.datamart import disaggregation as dg
from neurodb.datamart import reached as rc
from neurodb.datamart.models import DisaggregationLabel

SEX, NATIONALITY = 7, 8
FEMALE, MALE, LEBANESE, SYRIAN = 11, 12, 21, 22


def labels() -> dg.Labels:
    return dg.Labels(
        values={
            FEMALE: ("female", SEX),
            MALE: ("Male", SEX),
            LEBANESE: ("Lebanese", NATIONALITY),
            SYRIAN: ("Syrian", NATIONALITY),
        },
        types={SEX: "Sex", NATIONALITY: "Nationality"},
    )


def test_keys_are_read_as_prp_writes_them():
    assert dg.parse_key("()") == ()
    assert dg.parse_key("(11,)") == (11,)
    assert dg.parse_key("(11, 21)") == (11, 21)
    assert dg.parse_key("('female',)") == ("female",)
    assert dg.parse_key("female") == ("female",)
    assert dg.parse_key("") is None
    assert dg.ids_in(["()", "(11,)", "(11, 21)", "female", "(x, 3)"]) == {11, 21}


def test_the_power_bi_example_adds_up_within_each_disaggregation_never_across():
    """b09: Akkar QPR1 female 5, Male 15, Lebanese 20: the location total is 20, never 40."""
    raw = {
        "()": {"v": 20, "d": 1, "c": 20},
        "(11,)": {"v": 5, "d": 1, "c": 5},
        "(12,)": {"v": 15, "d": 1, "c": 15},
        "(21,)": {"v": 20, "d": 1, "c": 20},
    }
    found = dg.breakdown(raw, ratio=False, percentage=False, labels=labels())
    assert found.total == 20
    by_name = {g.name: g for g in found.groups}
    assert [ln.label for ln in by_name["Sex"].lines] == ["female", "Male"]
    assert by_name["Sex"].subtotal == 20 and by_name["Sex"].matches is True
    assert by_name["Nationality"].subtotal == 20 and by_name["Nationality"].matches is True
    assert found.keys == 3 and found.unlabelled == 0
    # no figure of the breakdown is the sum of every value
    assert 40 not in {found.total, *(g.subtotal for g in found.groups)}


def test_a_cross_tab_has_its_own_subtotal_and_a_differing_subtotal_is_marked():
    raw = {
        "()": "30",
        "(11, 21)": "10",
        "(12, 21)": "12",
        "(11,)": "10",
        "(12,)": "12",
    }
    found = dg.breakdown(raw, ratio=False, percentage=False, labels=labels())
    sex = next(g for g in found.groups if g.name == "Sex")
    cross = next(g for g in found.groups if g.cross)
    assert cross.name == "Sex × Nationality" and [ln.label for ln in cross.lines] == [
        "female × Lebanese",
        "Male × Lebanese",
    ]
    assert cross.subtotal == 22 and cross.matches is False  # 22 against a location total of 30
    assert sex.subtotal == 22 and sex.matches is False
    assert [g.cross for g in found.groups] == [False, True]  # single disaggregations first


def test_a_percentage_subtotal_is_numerators_over_denominators_and_never_a_sum_of_percentages():
    raw = {"()": {"v": 30, "d": 40}, "(11,)": {"v": 10, "d": 20}, "(12,)": {"v": 20, "d": 20}}
    found = dg.breakdown(raw, ratio=True, percentage=True, labels=labels())
    sex = found.groups[0]
    assert [ln.value for ln in sex.lines] == [50.0, 100.0]
    assert sex.subtotal == pytest.approx(75.0) and found.total == pytest.approx(75.0) and sex.matches
    # a percentage without its numerator and denominator is never added up
    bare = dg.breakdown({"(11,)": "50", "(12,)": "100"}, ratio=True, percentage=True, labels=labels())
    assert bare.groups[0].subtotal is None and bare.total is None


def test_values_without_labels_are_counted_not_shown_and_words_have_no_subtotal():
    raw = {"()": 9, "(11,)": 4, "(99,)": 5, "(11, 98)": 4, "female": 4}
    found = dg.breakdown(raw, ratio=False, percentage=False, labels=labels())
    assert found.unlabelled == 2  # 99 and the cross-tab with 98 are not labelled yet
    assert [g.name for g in found.groups] == ["Sex"] and found.groups[0].subtotal == 4
    assert [(ln.label, ln.value) for ln in found.untyped] == [("female", 4.0)]
    nothing = dg.breakdown({}, ratio=False, percentage=False, labels=dg.Labels())
    assert nothing.total is None and nothing.groups == [] and nothing.keys == 0


def test_two_disaggregations_not_named_yet_are_never_subtotalled_together():
    """The values' labels are read but not their disaggregations' names: each disaggregation keeps its
    own subtotal (grouped by PRP's id, never by the stand-in name)."""
    unnamed = dg.Labels(values=labels().values, types={})
    raw = {"()": 20, "(11,)": 5, "(12,)": 15, "(21,)": 20}
    found = dg.breakdown(raw, ratio=False, percentage=False, labels=unnamed)
    assert [(g.name, g.subtotal) for g in found.groups] == [
        ("Disaggregation not named yet", 20),
        ("Disaggregation not named yet", 20),
    ]


@pytest.mark.django_db
def test_labels_are_loaded_with_their_disaggregation_names():
    DisaggregationLabel.objects.create(kind="value", prp_id=FEMALE, label="female", disaggregation_id=SEX)
    DisaggregationLabel.objects.create(
        kind="value", prp_id=MALE, label="", disaggregation_id=SEX, not_found=True
    )
    DisaggregationLabel.objects.create(kind="type", prp_id=SEX, label="Sex")
    found = dg.load_labels({FEMALE, MALE, 5})
    assert found.values == {FEMALE: ("female", SEX)} and found.types == {SEX: "Sex"}
    assert dg.load_labels(set()).values == {}
    assert dg.load_labels().value(FEMALE) == ("female", SEX)


def test_reached_per_disaggregation_has_a_subtotal_per_disaggregation_only_when_it_adds_up():
    import datetime

    rows = [
        {
            "progress_report": f"PR-{n}",
            "report_number": f"QPR{n}",
            "report_type": "QPR",
            "report_status": "Accepted",
            "period_end": datetime.date(2026, 3 * n, 28),
            "location": "Akkar",
            "p_code": "LB71",
            "calculation_across_locations": "sum",
            "calculation_across_periods": "sum",
            "disaggregation": {
                "()": {"v": total, "d": 1, "c": total},
                "(11,)": {"v": girls, "d": 1, "c": girls},
                "(12,)": {"v": total - girls, "d": 1, "c": total - girls},
                "(21,)": {"v": total, "d": 1, "c": total},
            },
        }
        for n, (total, girls) in enumerate([(20, 5), (30, 10)], start=1)
    ]
    result = rc.compute(rows, today=datetime.date(2026, 10, 1), year=2026)
    assert result.reached == 50
    (place,) = dg.reached_by_disaggregation(result, labels())
    assert place["location"] == "Akkar" and place["reached"] == 50
    sex = next(g for g in place["groups"] if g["name"] == "Sex")
    assert [(ln["label"], ln["reached"]) for ln in sex["lines"]] == [("female", 15), ("Male", 35)]
    assert sex["subtotal"] == 50 and sex["matches"] is True
    nationality = next(g for g in place["groups"] if g["name"] == "Nationality")
    assert nationality["subtotal"] == 50
    # the disaggregations' names not read yet: still one subtotal each, never 100 for both
    (place,) = dg.reached_by_disaggregation(result, dg.Labels(values=labels().values, types={}))
    assert sorted(g["subtotal"] for g in place["groups"]) == [50, 50]
    # a value PRP names in words: listed, with no disaggregation and no subtotal
    rows[0]["disaggregation"]["female"] = {"v": 5, "d": 1, "c": 5}
    (place,) = dg.reached_by_disaggregation(
        rc.compute(rows, today=datetime.date(2026, 10, 1), year=2026), labels()
    )
    words = place["groups"][-1]
    assert words["name"] == "—" and words["subtotal"] is None and words["lines"][0]["label"] == "female"
    del rows[0]["disaggregation"]["female"]
    for row in rows:
        row["calculation_across_periods"] = "max"
    maxed = rc.compute(rows, today=datetime.date(2026, 10, 1), year=2026)
    (place,) = dg.reached_by_disaggregation(maxed, labels())
    assert all(g["subtotal"] is None for g in place["groups"])  # max of periods: nothing is added
    assert dg.reached_by_disaggregation(None) == []
