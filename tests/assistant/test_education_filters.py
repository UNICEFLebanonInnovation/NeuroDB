"""Ask NeuroDB's education_figures takes the Makani and Dirasa pages' filters: a filter by its query name or its
label, a choice by its label, its value or a partner's full name (case ignored; an exact name first, then a unique
part of a name; "All" leaves the filter out). The figures are the page's own under the same query, with the
registrations and the unique children said apart (what each page's card counts), the filters applied echoed, and
every list cut at 40 rows giving its real length (every row is stored: a filter of the tab, or of the overview,
reaches it). A choice that does not match, or matches several, an unknown filter, a filter of another tab and a
round the round argument contradicts are refused with the choices. Counts only: nothing about a child. The
payloads are built as Compiler builds them (tests/education/payloads.py); the partners and centres are
invented."""

import json

import pytest
from django.core.cache import cache
from django.urls import reverse

from neurodb.assistant import agent, hub_tools, privacy, tools
from neurodb.assistant.hub_tools import HUB_TOOLS, education_figures
from neurodb.assistant.tools import ToolInputError
from tests.education.payloads import CENTERS, dirasa_year_payload, makani_payload
from tests.education.test_dashboards import OUTREACH, store
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
        assert answer["filters_applied"] == {"Partner": "EP2"}
    with pytest.raises(ToolInputError, match="2 Partner choices match 'example partner t'") as several:
        education_figures("makani", filters={"partner": "example partner t"})
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
    with pytest.raises(ToolInputError, match="6 Center choices match 'Example Centre 4'") as several:
        education_figures("makani", filters={"center": "Example Centre 4"})
    message = str(several.value)
    assert "45 choices" in message and "Example Centre 45" in message and "longer part of the name" in message
    assert "Example Centre 01" not in message
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
    assert 'filter "school"' in overview and "overview tab" not in overview
    assert "a list of schools" in overview  # never one school as one row of a chart


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
        {"filters_applied": {"Center": "Sheikh Example Centre", "School": "Sheikh Example School"}},
        names=frozenset(),
    )
    assert kept["filters_applied"] == {"Center": "Sheikh Example Centre", "School": "Sheikh Example School"}


def test_the_tool_and_the_prompt_say_the_filters_and_what_each_count_is():
    _function, description, schema, _label = HUB_TOOLS["education_figures"]
    assert schema["properties"]["filters"]["type"] == "object"
    assert "filters" in description and "registrations" in description and "unique children" in description
    line = next(line for line in agent.SYSTEM_PROMPT.splitlines() if "education_figures" in line)
    assert "filters" in line
