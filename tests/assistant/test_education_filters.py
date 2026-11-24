"""Ask NeuroDB's education_figures takes the Makani and Dirasa pages' filters: a filter by its query name or its
label, a choice by its label, its value or a partner's full name (case ignored; an exact name first, then the whole
words of a unique part of a name, never inside a word or a number, and never for a category such as the gender; a
number is an id; "All" leaves the filter out). The figures are the page's own under the same query, with the
registrations and the unique children said apart (what each page's card counts), the filters applied echoed (a
partner with its full name, a choice found by a part of its name said so), and every list cut at 40 rows giving its
real length (every row is stored: a filter of the tab, or of the overview, reaches it). A choice that does not
match, or matches several, an unknown filter, a filter of another tab and a round the round argument contradicts
are refused, the choices listed (a centre's, a school's or a partner's under a key where Ask keeps its name after a
title). Counts only: nothing about a child. The payloads are built as Compiler builds them
(tests/education/payloads.py); the partners and centres are invented."""

import json
from types import SimpleNamespace

import pytest
from django.core.cache import cache
from django.urls import reverse

from neurodb.assistant import agent, hub_tools, privacy, tools
from neurodb.assistant.hub_tools import HUB_TOOLS, education_figures
from neurodb.assistant.tools import ToolInputError
from neurodb.education.models import PowerBiCard
from tests.education.payloads import (
    CENTERS,
    SCHOOLS,
    dirasa_payload,
    dirasa_year_payload,
    format_three,
    makani_payload,
)
from tests.education.test_dashboards import BRIDGING, OUTREACH, TEACHERS, store
from tests.education.test_dirasa_year import R2, YEAR_REGISTRATIONS, YEAR_TEACHERS
from tests.education.test_unique_children import MAKANI, PARTNERS

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _fresh_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def makani(db):
    return store(makani_payload(MAKANI, lists={"partners": PARTNERS}))


# a partner and a centre whose names hold "all"
ALLIANCE = {"id": 8, "name": "Example Alliance Four", "short_name": ""}
HALL = dict(CENTERS[1], id=103, name="Example Hall Centre", partner=8)


@pytest.fixture
def makani_short_names(db):
    partners = [dict(PARTNERS[0]), dict(PARTNERS[1], short_name="EP2"), dict(PARTNERS[2], short_name="EP3")]
    third = [{"child": 8, "center": 104, "sex": "Female"}]
    centre = dict(CENTERS[1], id=104, name="Example Third Centre", partner=7)
    return store(makani_payload([*MAKANI, *third], centers=[*CENTERS, centre], lists={"partners": partners}))


@pytest.fixture
def dirasa(db):
    payload = dirasa_year_payload(YEAR_REGISTRATIONS, YEAR_TEACHERS, OUTREACH, lists={"partners": PARTNERS})
    return store(payload)


def shown(client, name, **params):
    """The page's figures under the same query, as the tool returns them (JSON-safe)."""
    response = client.get(reverse(f"education:{name}"), params)
    assert response.status_code == 200
    return tools._clean(response.context["data"])


def keys(obj) -> set[str]:
    if isinstance(obj, dict):
        return set(obj) | set().union(*(keys(v) for v in obj.values()))
    if isinstance(obj, list):
        return set().union(*(keys(v) for v in obj))
    return set()


# --------------------------------------------------------------------------------------- the page's figures
def test_a_partners_boys_are_the_pages_figures_with_both_counts_explained(client_viewer, makani):
    answer = education_figures("makani", filters={"Partner": "example partner one", "child gender": "MALE"})
    assert answer["figures"] == shown(client_viewer, "makani", partner="5", gender="Male")
    assert answer["filters_applied"] == {"Partner": "Example Partner One", "Child gender": "Male"}
    # boy 1 is registered at two of the partner's centres: 3 registrations, 2 children
    assert answer["children"] == {"registrations": 3, "unique_children": 2}
    note = answer["children_note"]
    assert "counts twice" in note and "each child once" in note and "any combination" in note
    assert answer["figures"]["kpis"]["unique"] == 2  # Compiler's (partner, sex) grouping, as the page's card
    # counts only: no child, no cube or block of the payload
    assert not {"child", "person", "cubes", "blocks"} & keys(answer)
    assert answer["filters_not_applied"] is None


def test_a_combination_compiler_does_not_count_gives_the_registrations(makani):
    boys = {"partner": "one", "nationality": "syrian", "gender": "male"}  # no (nationality, partner, sex)
    answer = education_figures("makani", filters=boys)
    assert answer["children"] == {"registrations": 2, "unique_children": None}
    assert "give the registrations and say they are registrations" in answer["children_note"]
    service = education_figures("makani", tab="health", filters={"Education services": "bln"})
    assert service["children"] == {"registrations": 4, "unique_children": None}


def test_names_and_ids_resolve_whatever_their_case(makani):
    by_name = education_figures("makani", filters={"partner": "EXAMPLE PARTNER ONE", "Center": "halba"})
    by_id = education_figures("makani", filters={"PARTNER": 5, "center": "100"})
    assert by_name["figures"] == by_id["figures"]
    applied = {"Partner": "Example Partner One", "Center": "Halba center"}
    assert by_name["filters_applied"] == by_id["filters_applied"] == applied
    # through the assistant's own run, whose check lets an object of filters through
    asked = {"programme": "makani", "filters": {"cwd type": "difficulty hearing"}}
    hearing = tools.run("education_figures", asked)
    assert hearing["filters_applied"] == {"CWD type": "Difficulty hearing"}
    # "Not specified" where the page offers it, by its label
    answer = education_figures("makani", filters={"disability": "not specified"})
    assert answer["filters_applied"] == {"CWD type": "Not specified"}
    assert answer["children"] == {"registrations": 1, "unique_children": None}
    # "none" is no choice's name ("Not specified" holds it inside the page only): "No" or "Not specified"?
    with pytest.raises(ToolInputError, match="No CWD type choice matches 'none'") as none:
        education_figures("makani", filters={"disability": "none"})
    assert str(none.value).endswith("Its choices: Difficulty hearing, Difficulty seeing, No, Not specified.")


def test_all_leaves_a_filter_out_even_when_one_name_holds_all():
    """The page's "All" (an empty value) is no filter, never the one partner or centre whose name holds "all"."""
    store(
        makani_payload(
            [*MAKANI, {"child": 7, "center": 103, "sex": "Male", "nationality": 1, "disability": 1}],
            centers=[*CENTERS, HALL],
            lists={"partners": [*PARTNERS, ALLIANCE]},
        )
    )
    boys = education_figures("makani", filters={"gender": "Male"})
    assert boys["children"] == {"registrations": 5, "unique_children": 4}
    for everyone in ("All", "all", "", "Any"):
        answer = tools.run(
            "education_figures",
            {"programme": "makani", "filters": {"partner": everyone, "Center": everyone, "gender": "Male"}},
        )
        assert answer["figures"] == boys["figures"]
        assert answer["filters_applied"] == {"Child gender": "Male"}
    # still a filter of the tab, and a part of the name still finds the one partner
    with pytest.raises(ToolInputError, match="Unknown filter 'colour'"):
        education_figures("makani", filters={"colour": "All"})
    alliance = education_figures("makani", filters={"partner": "alliance"})
    assert alliance["filters_applied"] == {"Partner": "Example Alliance Four"}
    assert alliance["children"]["registrations"] == 1


def test_a_partners_full_name_finds_its_short_name(makani_short_names):
    for asked in ("Example Partner Two", "partner two", "EP2"):
        answer = education_figures("makani", filters={"partner": asked})
        assert answer["filters_applied"] == {"Partner": "EP2 (Example Partner Two)"}
    with pytest.raises(ToolInputError, match="3 Partner choices match 'example partner'") as several:
        education_figures("makani", filters={"partner": "example partner"})
    assert "EP2 (Example Partner Two), EP3 (Example Partner Three)" in str(several.value)
    with pytest.raises(ToolInputError, match="No Partner choice matches") as none:
        education_figures("makani", filters={"partner": "Nowhere"})
    assert str(none.value).endswith(
        "Its choices: EP2 (Example Partner Two), EP3 (Example Partner Three), Example Partner One."
    )


def test_common_words_for_a_filter_and_a_gender(makani):
    centre = education_figures("makani", filters={"centre": "halba", "sex": "boys"})
    assert centre["filters_applied"] == {"Center": "Halba center", "Child gender": "Male"}
    girls = education_figures("makani", filters={"Centres": "halba", "gender": "Girls"})
    assert girls["filters_applied"] == {"Center": "Halba center", "Child gender": "Female"}


def test_the_year_is_the_period_argument_not_a_filter(makani, dirasa):
    for programme, asked in (
        ("makani", "year"),
        ("makani", "round"),
        ("dirasa", "Year"),
        ("dirasa", "period"),
    ):
        with pytest.raises(ToolInputError, match="period argument"):
            education_figures(programme, filters={asked: "2026"})
    # Outreach's own Year filter stays one; "all" keeps its "All years"
    outreach = education_figures("dirasa", tab="outreach", filters={"year": "all"})
    assert outreach["filters_applied"] == {"Year": "All years"}


C = {c["id"]: c for c in CENTERS}


def test_a_part_of_a_name_is_whole_words_and_a_number_only_an_id():
    """Never a choice that only holds the same letters or digits: "Example Centre 1", which registered nobody this
    year, is not "Example Centre 12"."""
    centres = [
        dict(C[100], id=512, name="Example Centre 12", partner=5),
        dict(C[101], id=530, name="Example Centre 30", partner=6),
    ]
    regs = [{"child": 1, "center": 512, "sex": "Male"}, {"child": 2, "center": 530, "sex": "Female"}]
    store(makani_payload(regs, centers=centres, lists={"partners": PARTNERS}))
    for asked in ("Example Centre 1", "Centre 1", "centre 1", "Centre 3", 1, 2, 2.0, "2", "12"):
        with pytest.raises(ToolInputError, match="No Center choice matches"):
            education_figures("makani", filters={"center": asked})
    by_id = education_figures("makani", filters={"center": 512.0})
    assert by_id["filters_applied"] == {"Center": "Example Centre 12"}
    assert by_id["filters_matched_by_part"] is None and by_id["filters_note"] is None
    part = education_figures("makani", filters={"center": "centre 12"})
    assert part["filters_applied"] == {"Center": "Example Centre 12"}
    assert part["filters_matched_by_part"] == {"Center": "centre 12"}


def test_a_word_of_a_governorate_in_arabic_or_english(makani):
    for asked in ("بعلبك", "Baalbek", "baalbek hermel"):
        answer = education_figures("makani", filters={"governorate": asked})
        assert answer["filters_applied"] == {"Governorate": "بعلبك-الهرمل"}
        assert answer["filters_matched_by_part"] == {"Governorate": asked}
    exact = education_figures("makani", filters={"governorate": "Baalbek-Hermel"})  # one of its spellings
    assert (
        exact["filters_applied"] == {"Governorate": "بعلبك-الهرمل"}
        and exact["filters_matched_by_part"] is None
    )


def test_a_short_word_never_falls_into_another_choice():
    """Early in a year, no boy registered yet, no child without a disability, none working "No", no centre "No"
    in an emergency: "boys" is not "Female", "no" is not "Not specified"."""
    regs = [
        {"child": 1, "center": 100, "sex": "Female", "disability": 3, "working": "Yes"},
        {"child": 2, "center": 102, "sex": "Female", "working": "Yes"},
        {"child": 3, "center": 102, "sex": "Not specified", "disability": 6},
    ]
    store(makani_payload(regs, centers=[CENTERS[0], CENTERS[2]], lists={"partners": PARTNERS}))
    for name, asked in (
        ("gender", "male"),
        ("gender", "Male"),
        ("gender", "boys"),
        ("gender", "males"),
        ("disability", "no"),
        ("working", "No"),
        ("emergency", "no"),
        ("working", False),
    ):
        with pytest.raises(ToolInputError, match="choice matches"):
            education_figures("makani", filters={name: asked})
    girls = education_figures("makani", filters={"gender": "girls", "working": True, "emergency": "yes"})
    assert girls["filters_applied"] == {
        "Child gender": "Female",
        "Working children": "Yes",
        "Center active during emergency": "Yes",
    }


def test_a_partner_found_by_a_part_of_its_name_is_said_with_its_full_name():
    """Example Aid registered nobody this year, Example Aid International (shown as EAI) did: the answer says
    which partner it is and that it was found by a part of its name."""
    partners = [
        {"id": 5, "name": "Example Partner One", "short_name": ""},
        {"id": 6, "name": "Example Aid International", "short_name": "EAI"},
    ]
    regs = [
        {"child": 1, "center": 100, "sex": "Male", "nationality": 1},
        {"child": 2, "center": 101, "sex": "Male", "nationality": 5},
        {"child": 3, "center": 101, "sex": "Female", "nationality": 4},
    ]
    centres = [dict(C[100], partner=5), dict(C[101], partner=6)]
    store(makani_payload(regs, centers=centres, lists={"partners": partners}))
    answer = education_figures("makani", filters={"partner": "Example Aid", "gender": "boys"})
    assert answer["filters_applied"] == {"Child gender": "Male", "Partner": "EAI (Example Aid International)"}
    assert answer["filters_matched_by_part"] == {"Partner": "Example Aid"}
    assert "may not be the one meant" in answer["filters_note"]
    exact = education_figures("makani", filters={"partner": "eai"})
    assert exact["filters_applied"] == {"Partner": "EAI (Example Aid International)"}
    assert exact["filters_matched_by_part"] is None and exact["filters_note"] is None
    lebanon = education_figures("makani", filters={"nationality": "Lebanon"})
    assert lebanon["filters_applied"] == {"Child nationality": "Palestinian from Lebanon"}
    assert lebanon["filters_matched_by_part"] == {"Child nationality": "Lebanon"}
    _function, description, _schema, _label = HUB_TOOLS["education_figures"]
    assert "filters_matched_by_part" in description


def test_two_choices_of_one_name_are_told_apart_by_their_ids():
    twins = [{"id": 5, "name": "Example Twin Partner", "short_name": "ETP"}] * 2
    twins = [twins[0], dict(twins[1], id=6)]
    centres = [dict(C[100], id=103, name="Example Centre"), dict(C[101], id=104, name="Example Centre")]
    regs = [{"child": 1, "center": 103, "sex": "Male"}, {"child": 2, "center": 104, "sex": "Female"}]
    store(makani_payload(regs, centers=centres, lists={"partners": twins}))
    with pytest.raises(ToolInputError, match="2 Center choices match 'example centre'") as both:
        education_figures("makani", filters={"center": "example centre"})
    assert "Example Centre (103)" in str(both.value) and "Example Centre (104)" in str(both.value)
    for asked, shown in ((103, "Example Centre (103)"), ("104", "Example Centre (104)")):
        assert education_figures("makani", filters={"center": asked})["filters_applied"] == {"Center": shown}
    with pytest.raises(ToolInputError, match="2 Partner choices match 'etp'") as partners:
        education_figures("makani", filters={"partner": "etp"})
    assert "ETP (Example Twin Partner) (5)" in str(partners.value)
    assert "ETP (Example Twin Partner) (6)" in str(partners.value)
    one = education_figures("makani", filters={"partner": 6})
    assert one["filters_applied"] == {"Partner": "ETP (Example Twin Partner) (6)"}


def test_other_words_for_a_filter_and_all(dirasa):
    plural = education_figures(
        "dirasa", filters={"partners": "three", "genders": "female", "schools": "akkar"}
    )
    assert plural["filters_applied"] == {
        "Gender": "Female",
        "School": "Akkar private school",
        "Partner": "Example Partner Three",
    }
    # Outreach's own Year: "years" and "period" name it there; every word for "All" gives its "All years"
    for word in ("years", "period", "Year"):
        outreach = education_figures("dirasa", tab="outreach", filters={word: "2024"})
        assert outreach["filters_applied"] == {"Year": "2024"}
    for everyone in ("All", "any", "", "all years"):
        outreach = education_figures("dirasa", tab="outreach", filters={"year": everyone})
        assert outreach["filters_applied"] == {"Year": "All years"}
    # "All" of a filter another tab shows is no filter: left out, not refused
    on_map = education_figures("dirasa", tab="map", filters={"gender": "All"})
    assert not on_map["filters_applied"]
    # the round argument, like the Round filter, whatever its case
    same = education_figures("dirasa", round="bridging round 2 2026")
    assert same["round"] == "Bridging Round 2 2026"


def test_a_filter_with_no_choices_and_a_round_compiler_could_not_count():
    store(dirasa_year_payload(YEAR_REGISTRATIONS, YEAR_TEACHERS, [], lists={"partners": PARTNERS}))
    with pytest.raises(ToolInputError, match=r"Partner\) has no choices in Dirasa"):
        education_figures("dirasa", tab="outreach", filters={"partner": "x"})
    payload = dirasa_year_payload(YEAR_REGISTRATIONS, YEAR_TEACHERS, OUTREACH, lists={"partners": PARTNERS})
    payload["year"], payload["cubes"]["enrolment"] = "2027", {"error": "QueryCanceled"}
    store(payload)
    with pytest.raises(ToolInputError, match="Compiler could not count") as refused:
        education_figures("dirasa", period="2027", filters={"round": "Round 2"})
    assert "of one round" not in str(refused.value)


# ------------------------------------------------------------------------------------------- refused, with choices
def test_a_choice_that_does_not_match_or_matches_several_lists_the_choices(makani):
    with pytest.raises(ToolInputError, match="No Partner choice matches 'Nowhere'") as none:
        education_figures("makani", filters={"partner": "Nowhere"})
    assert "Example Partner One, Example Partner Two" in str(none.value)
    with pytest.raises(ToolInputError, match="2 Partner choices match 'example partner'") as several:
        education_figures("makani", filters={"partner": "example partner"})
    assert "Example Partner One, Example Partner Two" in str(several.value)
    with pytest.raises(ToolInputError, match="Unknown filter 'colour'") as unknown:
        education_figures("makani", filters={"colour": "red"})
    assert "partner (Partner)" in str(unknown.value) and "gender (Child gender)" in str(unknown.value)


def test_a_filter_of_another_tab_names_the_tabs_that_show_it(dirasa):
    with pytest.raises(ToolInputError, match=r"gender \(Gender\) is not a filter of the map tab") as other:
        education_figures("dirasa", tab="map", filters={"gender": "male"})
    assert "the overview and barriers tabs show it" in str(other.value)
    with pytest.raises(ToolInputError, match=r"school \(School\) is not a filter of the outreach tab"):
        education_figures("dirasa", tab="outreach", filters={"school": "akkar"})
    # Outreach's own Partner, by its query name, on the overview (which has a Partner of its own)
    with pytest.raises(
        ToolInputError, match=r"outreach_partner \(Partner\) is not a filter of the overview"
    ) as own:
        education_figures("dirasa", filters={"outreach_partner": "dirasa partner"})
    assert "only the outreach tab shows it" in str(own.value)
    # on Outreach, "partner" is the outreach's own Partner filter
    outreach = education_figures("dirasa", tab="outreach", filters={"partner": "dirasa partner"})
    assert outreach["filters_applied"] == {"Year": "2025", "Partner": "Dirasa Partner"}


def test_the_round_argument_and_a_round_filter_must_agree(dirasa):
    with pytest.raises(ToolInputError, match="disagree"):
        education_figures("dirasa", round="Bridging Round 1 2026", filters={"round": "round 2"})
    with pytest.raises(ToolInputError, match="disagree"):  # a round's name as the period chooses that round
        education_figures("dirasa", period="Bridging Round 1 2026", filters={"Round": str(R2)})
    same = education_figures("dirasa", round="Bridging Round 2 2026", filters={"round": "round 2"})
    assert same["round"] == "Bridging Round 2 2026"
    # the round argument still works alone, and the page's note on a filter not applied is kept
    outreach = education_figures("dirasa", tab="outreach", round="Bridging Round 2 2026")
    assert (outreach["round"], outreach["filters_not_applied"]) == (None, ["Round"])
    assert outreach["children"] is None  # Outreach counts Kobo interviews: no children card


def test_dirasa_by_year_takes_a_round_a_partner_and_a_gender(client_viewer, dirasa):
    answer = education_figures("dirasa", filters={"round": "Round 2", "partner": "three", "gender": "female"})
    assert answer["figures"] == shown(
        client_viewer, "dirasa", year="2026", round=str(R2), partner="7", gender="Female"
    )
    assert answer["round"] == "Bridging Round 2 2026"
    assert answer["filters_applied"] == {
        "Round": "Bridging Round 2 2026",
        "Gender": "Female",
        "Partner": "Example Partner Three",
    }
    # Compiler groups no round with a partner and a sex: the registrations, the card saying so
    assert answer["children"] == {"registrations": 1, "unique_children": None}
    assert answer["figures"]["cards"]["children"]["variant"] == "registrations"
    boys = education_figures("dirasa", round="Bridging Round 2 2026", filters={"gender": "Male"})
    assert boys["children"] == {"registrations": 1, "unique_children": 1}  # the (round, sex) grouping


# ---------------------------------------------------------------------------------------- lists cut at 40 rows
CENTRES = [dict(CENTERS[0], id=500 + n, name=f"Example Centre {n:02d}") for n in range(1, 46)]


def test_a_list_over_40_rows_gives_its_length_and_a_filter_reaches_every_row():
    store(
        makani_payload(
            [{"child": n, "center": 500 + n, "sex": "Male"} for n in range(1, 46)],
            centers=CENTRES,
            lists={"partners": PARTNERS},
        )
    )
    answer = education_figures("makani", tab="maps")
    figures = answer["figures"]
    assert len(figures["center_rows"]) == 40 and figures["center_rows"][-1]["name"] == "Example Centre 40"
    assert (figures["center_rows_total"], figures["center_rows_truncated"]) == (45, True)
    assert "points" not in figures  # the map's points: drawing data, their figures are the rows'
    assert "more" not in json.dumps(figures["center_rows"])  # no "… 5 more" row
    assert "every row is stored" in answer["lists_note"] and 'filter "center"' in answer["lists_note"]
    assert figures["kpis"]["children"] == 45  # every centre counted
    # the 41st row's figures, with a filter
    one = education_figures("makani", tab="maps", filters={"center": "example centre 41"})
    assert [(r["name"], r["children"]) for r in one["figures"]["center_rows"]] == [("Example Centre 41", 1)]
    assert "center_rows_total" not in one["figures"] and one["lists_note"] is None
    # 45 choices: the closest, how many, and to give a longer part of the name
    with pytest.raises(ToolInputError, match="No Center choice matches 'Example Centre 4'") as closest:
        education_figures("makani", filters={"center": "Example Centre 4"})
    message = str(closest.value)
    assert "45 choices" in message and "Example Centre 04" in message and "Example Centre 45" in message
    assert message.split("the closest: ")[1].startswith("Example Centre 04, Example Centre 14")
    with pytest.raises(ToolInputError, match="45 Center choices match 'Example Centre'") as several:
        education_figures("makani", filters={"center": "Example Centre"})
    assert "longer part of the name" in str(several.value)
    with pytest.raises(ToolInputError, match="No Center choice matches 'Centre 99'") as none:
        education_figures("makani", filters={"center": "Centre 99"})
    assert "45 choices" in str(none.value) and "longer part of the name" in str(none.value)


def test_the_lists_note_names_a_filter_of_the_tab(monkeypatch, dirasa):
    monkeypatch.setattr(hub_tools, "MAX_LIST", 2)  # Dirasa's three schools: a list cut
    on_map = education_figures("dirasa", tab="map")
    assert (on_map["figures"]["school_rows_total"], on_map["figures"]["school_rows_truncated"]) == (3, True)
    note = on_map["lists_note"]
    assert "the overview tab" in note and 'filter "school"' in note
    # following it gives one school's figures
    one = education_figures("dirasa", tab="overview", filters={"school": "akkar private"})
    assert one["filters_applied"] == {"School": "Akkar private school"}
    overview = education_figures("dirasa", tab="overview")["lists_note"]
    assert "every row is stored" in overview  # its charts were cut, not a list of schools
    assert "school" not in overview and "overview tab" not in overview


# ------------------------------------------------------------------------------------- the tool and the prompt
def test_the_children_note_says_what_each_pages_card_counts(makani, dirasa):
    makani_note = education_figures("makani")["children_note"]
    assert "registrations" in makani_note and "Power BI's 'Children enrolled' card" in makani_note
    assert makani_note.index("registrations") < makani_note.index("Power BI")
    dirasa_note = education_figures("dirasa")["children_note"]
    # Dirasa's card, on the page and in Power BI, counts each child once; the registrations are its second line
    card = "unique_children: each child once, as the page's 'Children enrolled in Dirasa' card and Power BI's"
    assert card in dirasa_note
    assert "second line" in dirasa_note and "registrations, as Power BI" not in dirasa_note
    for note in (makani_note, dirasa_note):
        assert "Registrations are counted under any combination of the page's filters" in note
        assert "unique children only where Compiler groups by them" in note


def test_a_centre_or_school_name_is_kept_after_a_title():
    kept = privacy.cleaned(
        {
            "filters_applied": {"Center": "Sheikh Example Centre", "School": "Sheikh Example School"},
            "figures": {
                "center_rows": [
                    {"name": "Sheikh Example Centre", "partner": "Dr. Example Social Foundation"}
                ],
                "school_rows": [{"name": "Sheikh Example School", "children": 3}],
            },
            "items": [{"kind": "makani_centre", "name": "Sheikh Example Centre"}],  # a knowledge hub card
        },
        names=frozenset(),
    )
    assert kept["filters_applied"] == {"Center": "Sheikh Example Centre", "School": "Sheikh Example School"}
    assert kept["figures"]["center_rows"][0]["name"] == "Sheikh Example Centre"
    assert kept["figures"]["school_rows"][0]["name"] == "Sheikh Example School"
    assert kept["items"][0]["name"] == "Sheikh Example Centre"
    # a person named beside a school or a centre is still withheld
    people = privacy.cleaned(
        {
            "school_principal": "Dr. Example Person",
            "centre_manager": "Sheikh Example Person",
            "Center manager": "Dr. Example Person",
            "School focal point": "Dr. Example Person",
            "focal_point": {
                "school": "Example School",
                "name": "Dr. Example Person",
                "director": "Dr. Example Person",
            },
            "center_rows": [{"name": "Example Centre", "manager": "Dr. Example Person"}],
        },
        names=frozenset(),
    )
    assert "Example Person" not in json.dumps(people) and people["focal_point"]["school"] == "Example School"


TITLED = {"id": 9, "name": "Dr. Example Social Foundation", "short_name": ""}
SHEIKH_CENTRE = dict(CENTERS[1], id=110, name="Sheikh Example Centre", partner=9)
SHEIKH_SCHOOL = dict(SCHOOLS[0], id=310, name="Sheikh Example Public School", listed=[7])


def sent(args):
    """What the AI reads of a call to education_figures: its result, or its refusal as Ask sends it, cleaned
    (agent._run_calls)."""
    call = SimpleNamespace(name="education_figures", arguments=json.dumps(args), call_id="c1")
    ((_call_id, output, ok),) = agent._run_calls([call], agent.Outcome())
    return json.loads(output), ok


def test_a_refusal_names_a_centre_school_or_partner_after_a_title_so_it_can_be_chosen():
    """The refusal's words are cleaned as any text; the choices it lists also come under a key that names
    centres, schools or partners, where Ask keeps a name after Sheikh or Dr, as in filters_applied."""
    regs = [*MAKANI, {"child": 9, "center": 110, "sex": "Male"}]
    store(makani_payload(regs, centers=[*CENTERS, SHEIKH_CENTRE], lists={"partners": [*PARTNERS, TITLED]}))
    refused, ok = sent({"programme": "makani", "filters": {"center": "Example centre Akkar"}})
    assert not ok and refused["error"].startswith("No Center choice matches 'Example centre Akkar'")
    centres = ["Beirut center", "Halba center", "Hermel center", "Sheikh Example Centre"]
    assert refused["choices"] == {"centres": centres}
    assert "Sheikh Example Centre" not in refused["error"]  # the refusal's own words: cleaned as any text
    chosen, ok = sent({"programme": "makani", "filters": {"center": refused["choices"]["centres"][-1]}})
    assert ok and chosen["filters_applied"] == {"Center": "Sheikh Example Centre"}
    assert chosen["children"] == {"registrations": 1, "unique_children": 1}
    partner, ok = sent({"programme": "makani", "filters": {"partner": "x9"}})
    assert not ok and "Dr. Example Social Foundation" in partner["choices"]["partners"]
    gender, ok = sent({"programme": "makani", "filters": {"gender": "x9"}})
    assert not ok and "choices" not in gender  # no name of a place or a partner: the words say them
    regs = [*YEAR_REGISTRATIONS, dict(YEAR_REGISTRATIONS[0], child=99, school=310)]
    schools = [*SCHOOLS, SHEIKH_SCHOOL]
    store(dirasa_year_payload(regs, YEAR_TEACHERS, OUTREACH, schools=schools, lists={"partners": PARTNERS}))
    school, ok = sent({"programme": "dirasa", "filters": {"school": "Nowhere"}})
    assert not ok and "Sheikh Example Public School" in school["choices"]["schools"]
    # Outreach's Partner is the Kobo form's own text, not a list of partners: in the refusal's words only
    kobo, ok = sent({"programme": "dirasa", "tab": "outreach", "filters": {"partner": "Nowhere"}})
    assert not ok and "choices" not in kobo and "Dirasa Partner" in kobo["error"]


def test_the_children_note_follows_the_version_of_the_card_shown():
    store(format_three(makani_payload(MAKANI, lists={"partners": PARTNERS})))
    PowerBiCard.objects.create(programme="mscc", card="children", variant="view_rows")
    answer = education_figures("makani")
    assert answer["figures"]["cards"]["children"]["variant"] == "view_rows"
    note = answer["children_note"]
    assert "registrations: what the page's" not in note and "another version" in note
    assert "figures.cards.children.name" in note and "counts twice" in note


def test_the_lists_note_speaks_of_centres_only_when_their_list_is_cut(monkeypatch, makani):
    monkeypatch.setattr(hub_tools, "MAX_LIST", 1)
    overview = education_figures("makani")
    assert overview["figures"]["charts"]["gender_truncated"] is True
    note = overview["lists_note"]
    assert "longer than 1 rows" in note and "centre" not in note and "center" not in note
    maps = education_figures("makani", tab="maps")["lists_note"]
    assert 'filter "center"' in maps and "centres" in maps


def test_the_tool_and_the_prompt_say_the_filters_and_what_each_count_is():
    _function, description, schema, _label = HUB_TOOLS["education_figures"]
    assert schema["properties"]["filters"]["type"] == "object"
    assert "filters" in description and "registrations" in description and "unique children" in description
    line = next(line for line in agent.SYSTEM_PROMPT.splitlines() if "education_figures" in line)
    assert "filters" in line
    # only education_figures's cut lists are whole: another lookup's cut list is partial
    assert "A list cut in education_figures is not missing data" in agent.SYSTEM_PROMPT
    assert "A list cut in a lookup is not missing data" not in agent.SYSTEM_PROMPT


# ------------------------------------------------------------------------- a choice as the tool shows it, a period
def test_a_choice_sent_back_as_the_tool_shows_it_is_taken(client_viewer):
    """The AI picks a choice as a refusal lists it, or sends filters_applied again: a partner with its full name
    beside its short one, a name two choices share with the id."""
    partners = [dict(PARTNERS[0]), dict(PARTNERS[1], short_name="EP2"), dict(PARTNERS[2], short_name="EP3")]
    centres = [*CENTERS, dict(C[101], id=104, name="Halba center", partner=7)]
    regs = [*MAKANI, {"child": 8, "center": 104, "sex": "Female"}]
    store(makani_payload(regs, centers=centres, lists={"partners": partners}))
    refused, ok = sent({"programme": "makani", "filters": {"partner": "Example Partner"}})
    assert not ok and refused["choices"]["partners"][0] == "EP2 (Example Partner Two)"
    chosen, ok = sent({"programme": "makani", "filters": {"partner": "EP2 (Example Partner Two)"}})
    assert ok and chosen["filters_applied"] == {"Partner": "EP2 (Example Partner Two)"}
    assert chosen["filters_matched_by_part"] is None
    assert chosen["figures"] == shown(client_viewer, "makani", partner="6")
    # what one answer applied, asked again on another tab
    again, ok = sent({"programme": "makani", "tab": "health", "filters": chosen["filters_applied"]})
    assert ok and again["filters_applied"] == chosen["filters_applied"]
    twins, ok = sent({"programme": "makani", "filters": {"center": "Halba center"}})
    assert not ok and {"Halba center (100)", "Halba center (104)"} <= set(twins["choices"]["centres"])
    for listed, value in (("Halba center (104)", "104"), ("halba center (100)", "100")):
        one, ok = sent({"programme": "makani", "filters": {"center": listed}})
        assert ok and one["filters_applied"] == {"Center": f"Halba center ({value})"}
        assert one["figures"] == shown(client_viewer, "makani", center=value)
    # a name with its id, though no other choice reads the same
    beirut = education_figures("makani", filters={"center": "Beirut center (101)"})
    assert beirut["filters_applied"] == {"Center": "Beirut center"}


def test_choices_alike_but_for_case_spaces_or_a_full_name_can_each_be_chosen(client_viewer):
    """Compiler's names, and the Kobo form's own text on Outreach, may differ only in case or spaces, or a label
    be another partner's full name: each choice the page offers can be chosen, by its text as shown or its id."""
    centres = [
        *CENTERS,
        dict(C[101], id=104, name="Halba center ", partner=7),
        dict(C[101], id=105, name="HALBA CENTER", partner=7),
    ]
    partners = [
        dict(PARTNERS[0]),
        dict(PARTNERS[1], short_name="EP2"),
        dict(PARTNERS[2]),
        {"id": 9, "name": "Example Partner Two", "short_name": ""},
    ]
    regs = [
        *MAKANI,
        {"child": 8, "center": 104, "sex": "Female"},
        {"child": 9, "center": 105, "sex": "Male"},
        {"child": 10, "center": 101, "sex": "Male", "partner": 9},
    ]
    store(makani_payload(regs, centers=centres, lists={"partners": partners}))
    refused, ok = sent({"programme": "makani", "filters": {"center": "halba center"}})
    listed = {"Halba center (100)", "Halba center  (104)", "HALBA CENTER (105)"}
    assert not ok and listed <= set(refused["choices"]["centres"])
    for asked, value in (
        ("Halba center (100)", "100"),
        ("Halba center (104)", "104"),
        ("HALBA CENTER (105)", "105"),
    ):
        answer = education_figures("makani", filters={"center": asked})
        assert answer["figures"] == shown(client_viewer, "makani", center=value)
    for asked, value in (("Example Partner Two", "9"), ("EP2", "6"), ("EP2 (Example Partner Two)", "6")):
        answer = education_figures("makani", filters={"partner": asked})
        assert answer["figures"] == shown(client_viewer, "makani", partner=value)
        assert answer["filters_matched_by_part"] is None
    outreach = [
        *OUTREACH,
        {"year": "2025", "partner": "DIRASA PARTNER", "governorate": "akkar", "education_status": "x"},
        {"year": "2025", "partner": "Other", "governorate": "الشمال", "education_status": "x"},
        {"year": "2025", "partner": "Other", "governorate": "North Lebanon", "education_status": "x"},
    ]
    store(dirasa_year_payload(YEAR_REGISTRATIONS, YEAR_TEACHERS, outreach, lists={"partners": PARTNERS}))
    for name, asked in (
        ("governorate", "akkar"),
        ("governorate", "Akkar"),
        ("governorate", "الشمال"),
        ("governorate", "North Lebanon"),
        ("partner", "Dirasa Partner"),
        ("partner", "DIRASA PARTNER"),
    ):
        answer = education_figures("dirasa", tab="outreach", filters={name: asked})
        assert answer["filters_applied"] == {"Year": "2025", name.capitalize(): asked}
        page = shown(client_viewer, "dirasa", tab="outreach", **{f"outreach_{name}": asked})
        assert answer["figures"] == page
    for name, asked in (("governorate", "AKKAR"), ("governorate", "North"), ("partner", "dirasa partner")):
        with pytest.raises(ToolInputError, match=f"2 {name.capitalize()} choices match"):
            education_figures("dirasa", tab="outreach", filters={name: asked})


def test_an_older_dirasa_round_is_found_whatever_its_case_and_one_not_stored_is_refused():
    store(dirasa_payload(BRIDGING, TEACHERS, OUTREACH, lists={"partners": PARTNERS}))
    later = {"round_name": "Bridging 2026-2027", "start_date": "2026-10-01", "lists": {"partners": PARTNERS}}
    store(dirasa_payload(BRIDGING[:2], TEACHERS, OUTREACH, **later))
    exact = education_figures("dirasa", round="Bridging 2025-2026")
    assert exact["children"]["registrations"] == len(BRIDGING)
    for asked in ({"round": "bridging 2025-2026"}, {"period": "BRIDGING  2025-2026"}):
        answer = education_figures("dirasa", **asked)
        assert (answer["period"], answer["children"]) == ("Bridging 2025-2026", exact["children"])
    for asked in ({"period": "2025"}, {"round": "Bridging 2023-2024"}):
        with pytest.raises(ToolInputError, match="its periods are Bridging 2026-2027, Bridging 2025-2026"):
            education_figures("dirasa", **asked)


def test_a_round_named_as_the_period_is_found_whatever_its_case(dirasa):
    as_round = education_figures("dirasa", round="Bridging Round 2 2026")
    as_period = education_figures("dirasa", period="bridging round 2 2026")
    assert (as_period["period"], as_period["round"]) == ("2026", "Bridging Round 2 2026")
    assert as_period["children"] == as_round["children"] == {"registrations": 2, "unique_children": 2}
    with pytest.raises(ToolInputError, match="No period 'Bridging Round 7 2026' in Dirasa"):
        education_figures("dirasa", period="Bridging Round 7 2026")


def test_a_year_not_stored_is_refused_not_answered_with_another(makani):
    with pytest.raises(ToolInputError, match=r"No period '2025' in Makani \(MSCC\): its periods are 2026"):
        education_figures("makani", period="2025", filters={"gender": "boys"})
    assert education_figures("makani", period=" 2026")["period"] == "2026"


def test_outreach_counts_the_periods_year_unless_a_year_is_given(client_viewer):
    """Every payload holds the Kobo outreach of every year, its Year filter on the latest by default: the period
    asked for chooses it."""
    outreach = [
        *OUTREACH,
        {"year": "2026", "partner": "Dirasa Partner", "governorate": "Akkar", "education_status": "x"},
    ]
    for year in ("2025", "2026"):
        store(
            dirasa_year_payload(
                YEAR_REGISTRATIONS, YEAR_TEACHERS, outreach, year=year, lists={"partners": PARTNERS}
            )
        )
    answer = education_figures("dirasa", period="2025", tab="outreach")
    assert (answer["period"], answer["filters_applied"]) == ("2025", {"Year": "2025"})
    assert answer["figures"] == shown(
        client_viewer, "dirasa", year="2025", tab="outreach", outreach_year="2025"
    )
    assert answer["figures"]["kpis"] == {"outreached": 2, "year": "2025"} and answer["outreach_note"] is None
    latest = education_figures("dirasa", tab="outreach", filters={"partner": "dirasa partner"})
    assert latest["filters_applied"] == {"Year": "2026", "Partner": "Dirasa Partner"}
    # a Year given is the one counted
    given = education_figures("dirasa", period="2025", tab="outreach", filters={"year": "2024"})
    assert given["filters_applied"] == {"Year": "2024"} and given["outreach_note"] is None
    every = education_figures("dirasa", period="2025", tab="outreach", filters={"year": "all"})
    assert every["filters_applied"] == {"Year": "All years"}
    assert education_figures("dirasa", period="2025")["outreach_note"] is None  # not on Outreach
    # the tab whatever its case
    assert education_figures("dirasa", period="2025", tab="Outreach")["filters_applied"] == {"Year": "2025"}


def test_outreach_says_when_its_year_is_not_the_period(dirasa):
    answer = education_figures("dirasa", tab="outreach")  # 2026: no outreach of 2026 stored
    assert (answer["period"], answer["filters_applied"]) == ("2026", {"Year": "2025"})
    assert "none of 2026" in answer["outreach_note"] and "of 2025" in answer["outreach_note"]
    given = education_figures("dirasa", tab="outreach", filters={"year": "2024"})
    assert given["outreach_note"] is None


def test_outreach_of_an_older_dirasa_round_says_its_year():
    store(dirasa_payload(BRIDGING, TEACHERS, OUTREACH, lists={"partners": PARTNERS}))
    answer = education_figures("dirasa", tab="outreach")
    assert (answer["period"], answer["filters_applied"]) == ("Bridging 2025-2026", {"Year": "2025"})
    assert "not by round" in answer["outreach_note"] and "of 2025" in answer["outreach_note"]
